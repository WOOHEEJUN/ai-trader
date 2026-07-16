"""주간 평가 — 세대 관리와 "kill".

매주 월요일 09:00 KST. 이 모듈도 LLM을 호출하지 않는다 (순수 규칙) — 따라서 예산이
소진된 달에도 평가는 정상적으로 이뤄진다.

성공: 전략 메모리 보존 + 권한 레벨 +1 (일일 매매 +2건, 유니버스 +5개, 운용 한도 +5만원)
실패: 전략 메모리 초기화 + 레벨 0 복귀 + 세대 +1

거래 로그와 스냅샷은 어느 쪽이든 보존한다 — 사용자가 세대별 역사를 볼 수 있어야 하고,
그게 이 실험의 관전 포인트이기 때문. 초기화되는 건 `strategy_memory.md`뿐이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from agent.watchdog import compute_portfolio
from config import MAX_LEVEL, settings, tier_for
from exchange.upbit_client import Broker, get_broker
from notify import notify
from state.store import Store, get_store, now_iso, now_kst

WEEK_START_KEY = "week_start_ts"
STATE_LEVEL_KEY = "permission_level"
STATE_GENERATION_KEY = "generation"


@dataclass
class Verdict:
    success: bool
    killed: bool
    start_krw: float
    end_krw: float
    pnl_krw: float
    pnl_pct: float
    generation: int
    level_before: int
    level_after: int


def run_judge(store: Optional[Store] = None, broker: Optional[Broker] = None) -> Verdict:
    store = store or get_store()
    broker = broker or get_broker(store)
    now = now_kst()

    week_start = store.get_state(WEEK_START_KEY)
    if week_start:
        snap = store.snapshot_at_or_before(week_start)
        start_krw = float(snap["total_krw"]) if snap else float(settings.initial_capital_krw)
    else:
        # 가동 첫 주: 기준 스냅샷이 없으므로 초기 자본을 기준으로 잡는다.
        first = store.snapshot_at_or_before(now.isoformat())
        start_krw = float(first["total_krw"]) if first else float(settings.initial_capital_krw)
        week_start = first["ts"] if first else now_iso()

    end_krw = compute_portfolio(broker)[0]
    pnl = end_krw - start_krw
    pnl_pct = (pnl / start_krw) if start_krw > 0 else 0.0

    level_before = int(store.get_state(STATE_LEVEL_KEY, 0))
    generation_before = int(store.get_state(STATE_GENERATION_KEY, 1))
    generation_after = generation_before

    # 성공 기준: 순수익 > 0. 평가액은 이미 수수료·슬리피지가 반영된 실제 잔고 기준이므로
    # 여기서 따로 차감하지 않는다.
    success = pnl > 0

    if success:
        level_after = min(level_before + 1, MAX_LEVEL)
        killed = False
    else:
        level_after = 0
        killed = True
        generation_after = generation_before + 1
        store.reset_memory()  # kill — 전략 메모리만 초기화, 거래 로그는 보존

    store.set_state(STATE_LEVEL_KEY, level_after)
    store.set_state(STATE_GENERATION_KEY, generation_after)
    store.set_state(WEEK_START_KEY, now_iso())
    # 기록에는 "평가받은" 세대를 남긴다. kill 시 generation_after를 쓰면 1세대를 죽인 평가가
    # 2세대 기록으로 남아 히스토리가 한 칸씩 밀린다.
    store.record_evaluation(
        week_start=week_start, week_end=now_iso(),
        start_krw=start_krw, end_krw=end_krw, pnl_krw=pnl, pnl_pct=pnl_pct,
        success=success, killed=killed, generation=generation_before,
        level_before=level_before, level_after=level_after,
    )

    tier = tier_for(level_after)
    if success:
        msg = (
            f"✅ 주간 평가 성공: {start_krw:,.0f} → {end_krw:,.0f}원 ({pnl_pct:+.2%}, {pnl:+,.0f}원)\n"
            f"전략 메모리 보존. 권한 레벨 {level_before} → {level_after} "
            f"(일일 매매 {tier.max_daily_trades}건, 유니버스 {tier.universe_size}개, "
            f"운용 한도 {tier.capital_limit_krw:,}원)"
        )
        if tier.capital_limit_krw > end_krw:
            msg += (
                f"\n※ 운용 한도가 {tier.capital_limit_krw:,}원으로 올랐다. "
                f"입금하지 않으면 실제 운용자금은 현재 잔고({end_krw:,.0f}원) 그대로다."
            )
    else:
        msg = (
            f"💀 주간 평가 실패: {start_krw:,.0f} → {end_krw:,.0f}원 ({pnl_pct:+.2%}, {pnl:+,.0f}원)\n"
            f"전략 메모리 초기화(kill). 세대 {generation_before} → {generation_after}, "
            f"권한 레벨 {level_before} → 0. 거래 기록은 보존됨."
        )

    logger.info(f"[평가] {msg}")
    notify(msg, level="info" if success else "warning")

    return Verdict(success, killed, start_krw, end_krw, pnl, pnl_pct,
                   generation_after, level_before, level_after)
