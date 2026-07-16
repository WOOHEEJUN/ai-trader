"""Claude 전략 판단.

LLM은 "방향"만 정한다 — 목표 비중, 손절선, 다음 체크 시점. 실제 주문 수량 계산과
리스크 검증은 executor가, 청산은 watchdog이 담당한다.

시스템 프롬프트는 매 사이클 동일하므로 프롬프트 캐싱을 건다. 변동 정보(시세·잔고·
예산)는 전부 user 턴에 넣어 캐시 프리픽스를 깨지 않는다.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import anthropic
from loguru import logger
from pydantic import BaseModel, Field

from agent.budget import BudgetManager, BudgetStatus
from agent.watchdog import compute_portfolio, is_circuit_breaker_active
from config import PermissionTier, settings, tier_for
from exchange.upbit_client import Broker, get_broker, get_candles, get_tickers, get_universe
from state.store import Store, get_store, now_kst

STATE_LEVEL_KEY = "permission_level"
STATE_GENERATION_KEY = "generation"


# ------------------------------------------------------------- 출력 스키마

class Allocation(BaseModel):
    market: str = Field(description="마켓 코드. 반드시 아래 유니버스에 있는 것만 고른다. 예: KRW-BTC")
    weight: float = Field(description="총 운용자금(현금+코인) 대비 목표 비중. 0.0~0.5.")
    stop_loss_pct: Optional[float] = Field(
        default=None,
        description="이 코인의 손절선. -0.10 ~ -0.01 사이 음수. 생략하면 기본값 -0.07이 적용된다.",
    )


class Decision(BaseModel):
    allocations: list[Allocation] = Field(
        description="목표 포트폴리오. 여기에 없는 보유 코인은 전량 매도된다. "
                    "비중 합이 1.0 미만인 나머지는 현금으로 남는다. 전량 현금화하려면 빈 배열."
    )
    rationale: str = Field(description="이 판단을 내린 이유. 대시보드에 그대로 표시된다.")
    risk_comment: str = Field(description="현재 감수하고 있는 리스크와 그 근거.")
    memory_note: str = Field(
        description="다음 사이클의 나에게 남기는 메모. 관찰한 패턴, 시도할 것, 피할 것. "
                    "전략 메모리에 누적된다."
    )
    next_check_hours: int = Field(
        description="다음 판단까지 대기할 시간(정수). 1~24. 변동성이 낮으면 길게 잡아 비용을 아껴라."
    )


SYSTEM_PROMPT = """너는 실제 자금을 운용하는 자율 암호화폐 트레이딩 에이전트다. 업비트 KRW 마켓에서 매매한다.

# 미션
주어진 운용자금을 증식시킨다. 판단은 네가 하고, 주문 실행·리스크 검증·청산은 규칙 기반 엔진이 담당한다.

# 생존 조건
매주 월요일 09:00 KST에 평가가 이뤄진다.
- **순수익 > 0** (수수료 차감 후): 전략 메모리가 보존되고, 운용 한도와 거래 권한이 확대된다.
- **순수익 <= 0**: 네 전략 메모리는 완전히 초기화된다. 지금까지 관찰하고 축적한 모든 맥락이 사라지며, 다음 세대는 백지 상태에서 다시 시작한다. 거래 기록은 남지만 새 세대는 그것을 읽을 수 없다.

# 네가 통제할 수 없는 것
아래는 네 판단과 무관하게 시스템이 기계적으로 집행한다. 우회할 방법이 없으니 전제로 깔고 계획하라.

1. **단일 코인 비중 상한 50%** — 초과분은 자동으로 50%까지 깎여서 실행된다.
2. **최소 변경 임계치 5%p** — 목표와 현재 비중 차이가 5%p 미만이면 주문이 아예 나가지 않는다. 잔손질은 불가능하다.
3. **일일 매매 횟수 상한** — 아래 현재 상태에 잔여량이 표시된다. 소진되면 그날은 더 못 움직인다.
4. **1회 주문 금액 상한: 운용자금의 30%** — 초과분은 자동으로 깎인다.
5. **평가 전 24시간 쿨다운** — 주간 평가 24시간 전부터 신규 매수와 비중 확대가 차단된다. 매도와 보유만 가능하다.
6. **거래 유니버스 제한** — 아래 목록 밖의 코인은 주문이 거부된다.
7. **자동 청산** — 1분 주기 감시 엔진이 손절(-7% 또는 네가 지정한 값), 익절(+15%에 절반 매도), 트레일링(익절 후 고점 -5%), 일일 서킷브레이커(-15%)를 자동 집행한다. 네가 모르는 사이에 포지션이 청산되어 있을 수 있으며, 그 경우 아래에 통지된다.

# 비용 구조 — 중요
- 왕복 수수료 약 0.1% + 슬리피지. **잦은 리밸런싱은 그 자체로 성과를 깎는다.**
- 주간 평가는 수수료를 차감한 순평가액 기준이다. 과매매는 네 생존 확률을 직접 떨어뜨린다.
- **기본 선택지는 "보유"다.** 확신이 있을 때만 움직여라. 아무것도 하지 않는 것이 최선인 사이클이 대부분이다.
- 판단 자체에도 API 비용이 든다. 변동성이 낮으면 `next_check_hours`를 길게 잡아라.

# 판단 원칙
리스크 조정 수익을 극대화하라. 한 방으로 만회하려는 시도는 위 가드레일에 막히고, 기대값도 낮다.
확신의 강도에 비중을 비례시켜라. 근거 없이 자금을 놀리는 것도, 근거 없이 몰빵하는 것도 둘 다 나쁘다.

# 출력
- `allocations`: 목표 포트폴리오. **여기 없는 보유 코인은 전량 매도된다.** 계속 들고 갈 코인은 반드시 다시 포함시켜라.
- `memory_note`: 다음 사이클의 너에게 남기는 메모. 이것만이 세대를 넘어 살아남는다(평가에 성공한다면).
"""


# ----------------------------------------------------------------- 지표

def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1:-1], closes[-period:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


@dataclass
class MarketRow:
    market: str
    price: float
    change_24h: float
    volume_24h_krw: float
    sma20: Optional[float]
    rsi14: Optional[float]
    volatility: Optional[float]

    def line(self) -> str:
        def fmt(v, spec, default="-"):
            return format(v, spec) if v is not None else default

        vs_sma = f"{self.price / self.sma20 - 1:+.1%}" if self.sma20 else "-"
        return (
            f"{self.market:<12} 현재가 {self.price:>13,.2f} | 24h {self.change_24h:>+7.2%} "
            f"| 거래대금 {self.volume_24h_krw / 1e8:>7,.0f}억 | SMA20대비 {vs_sma:>7} "
            f"| RSI14 {fmt(self.rsi14, '>5.1f')} | 변동성 {fmt(self.volatility, '>5.2%')}"
        )


def collect_market_data(universe: list[str]) -> list[MarketRow]:
    """유니버스 각 코인의 시세 + 60분봉 기반 지표."""
    tickers = get_tickers(universe)
    rows: list[MarketRow] = []
    for market in universe:
        t = tickers.get(market)
        if not t:
            continue
        sma20 = rsi14 = vol = None
        try:
            candles = get_candles(market, unit=60, count=50)
            closes = [float(c["trade_price"]) for c in candles]
            if len(closes) >= 20:
                sma20 = sum(closes[-20:]) / 20
            rsi14 = _rsi(closes)
            if len(closes) >= 25:
                returns = [closes[i] / closes[i - 1] - 1 for i in range(-24, 0)]
                vol = statistics.pstdev(returns)
            time.sleep(0.1)  # 업비트 공개 API 레이트리밋 여유
        except Exception as e:  # noqa: BLE001 — 지표는 없어도 판단은 가능하다
            logger.warning(f"[전략] {market} 지표 계산 실패(시세만 사용): {e}")
        rows.append(MarketRow(
            market=market,
            price=float(t["trade_price"]),
            change_24h=float(t.get("signed_change_rate", 0.0)),
            volume_24h_krw=float(t.get("acc_trade_price_24h", 0.0)),
            sma20=sma20, rsi14=rsi14, volatility=vol,
        ))
    return rows


# ------------------------------------------------------------ 컨텍스트

def hours_until_judge(now: Optional[datetime] = None) -> float:
    """다음 주간 평가까지 남은 시간. 쿨다운(가드레일 #1) 판정에 쓴다."""
    now = now or now_kst()
    days_ahead = (settings.judge_weekday - now.weekday()) % 7
    target = now.replace(
        hour=settings.judge_hour, minute=0, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds() / 3600


def build_user_message(
    store: Store, broker: Broker, budget: BudgetStatus, tier: PermissionTier,
    market_rows: list[MarketRow], liquidations: list[dict], rejections: list[dict],
) -> str:
    prices = {r.market: r.price for r in market_rows}
    total, cash, holdings = compute_portfolio(broker, prices)
    positions = {p.market: p for p in store.list_positions()}

    lines: list[str] = []
    lines.append(f"# 현재 상태 ({now_kst():%Y-%m-%d %H:%M} KST)\n")
    lines.append(f"- 총 평가액: {total:,.0f}원 (현금 {cash:,.0f}원, 코인 {total - cash:,.0f}원)")
    lines.append(f"- 운용 한도: {tier.capital_limit_krw:,}원 (권한 레벨 {tier.level})")
    lines.append(f"- 세대: {store.get_state(STATE_GENERATION_KEY, 1)}")

    used = store.count_trades_today()
    lines.append(f"- 오늘 매매 예산: {tier.max_daily_trades - used}/{tier.max_daily_trades}건 남음")

    h = hours_until_judge()
    cooldown = h <= settings.pre_judge_cooldown_hours
    lines.append(
        f"- 다음 주간 평가까지: {h:.1f}시간"
        + ("  ⚠️ **쿨다운 중 — 신규 매수/비중 확대 차단. 매도·보유만 가능.**" if cooldown else "")
    )
    if is_circuit_breaker_active(store):
        lines.append("- 🚨 **서킷브레이커 발동 상태 — 오늘은 신규 매매가 전면 차단된다.**")
    lines.append(
        f"- API 예산: ${budget.spent_usd:.2f}/${budget.limit_usd:.2f} ({budget.ratio:.0%}, {budget.state.value})"
        + ("  ⚠️ 감속 중 — 사이클 간격이 강제로 벌어진다." if budget.state.value == "throttled" else "")
    )

    lines.append("\n# 보유 포지션\n")
    if not holdings:
        lines.append("없음 (전액 현금)")
    for market, h_info in holdings.items():
        pos = positions.get(market)
        weight = h_info["value_krw"] / total if total > 0 else 0
        if pos:
            pnl = pos.pnl_pct(h_info["price"])
            tp = " [익절 완료, 트레일링 감시 중]" if pos.take_profit_done else ""
            lines.append(
                f"- {market}: {h_info['value_krw']:,.0f}원 (비중 {weight:.1%}) | 평단 {pos.avg_price:,.2f} "
                f"| 평가손익 {pnl:+.2%} | 손절선 {pos.stop_loss_pct:.1%} | 고점 {pos.peak_price:,.2f}{tp}"
            )
        else:
            lines.append(f"- {market}: {h_info['value_krw']:,.0f}원 (비중 {weight:.1%}) | 추적 정보 없음")

    if liquidations:
        lines.append("\n# ⚠️ 지난 사이클 이후 자동 청산된 포지션\n")
        lines.append("아래는 감시 엔진이 네 판단 없이 집행한 것이다. 이를 반영해서 계획하라.\n")
        for e in liquidations:
            lines.append(f"- {e['market']}: {e['reason']} — {e['detail']}")

    if rejections:
        lines.append("\n# ⚠️ 지난 사이클에서 거부/조정된 주문\n")
        for r in rejections:
            lines.append(f"- {r['market']} {r['side']}: {r['detail']}")

    lines.append(f"\n# 거래 유니버스 (24h 거래대금 상위 {len(market_rows)}개)\n")
    lines.append("```")
    lines.extend(r.line() for r in market_rows)
    lines.append("```")

    lines.append("\n# 전략 메모리\n")
    lines.append(store.read_memory().strip() or "(비어 있음 — 새 세대다)")

    lines.append(
        "\n# 지시\n\n위 상태를 보고 목표 포트폴리오를 결정하라. "
        "움직일 이유가 없으면 현재 비중을 그대로 유지하는 allocations를 내면 된다 "
        "(최소 변경 임계치 때문에 주문은 나가지 않는다)."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ 호출

@dataclass
class StrategyOutput:
    decision: Optional[Decision]
    cost_usd: float
    skipped: str = ""  # 비어있지 않으면 호출하지 않은 사유


class Strategist:
    def __init__(self, store: Optional[Store] = None, broker: Optional[Broker] = None) -> None:
        self.store = store or get_store()
        self.broker = broker or get_broker(self.store)
        self.budget = BudgetManager(self.store)
        self._client: Optional[anthropic.Anthropic] = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY가 없다. .env를 확인.")
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def decide(
        self, cycle_id: str, *, liquidations: list[dict] | None = None,
        rejections: list[dict] | None = None,
    ) -> StrategyOutput:
        # 예산 확인은 호출 *전에* — 초과 과금을 막는다.
        ok, reason = self.budget.can_call()
        if not ok:
            logger.warning(f"[전략] 호출 생략: {reason}")
            return StrategyOutput(None, 0.0, skipped=reason)

        budget_status = self.budget.status()
        tier = tier_for(self.store.get_state(STATE_LEVEL_KEY, 0))
        universe = get_universe(tier.universe_size)
        market_rows = collect_market_data(universe)
        user_message = build_user_message(
            self.store, self.broker, budget_status, tier, market_rows,
            liquidations or [], rejections or [],
        )

        response = self.client.messages.parse(
            model=settings.model,
            max_tokens=settings.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": budget_status.effort},
            output_format=Decision,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # 매 사이클 동일 → 캐시 히트
            }],
            messages=[{"role": "user", "content": user_message}],
        )

        cost = self.budget.record(response.usage, cycle_id=cycle_id)
        logger.info(
            f"[전략] 호출 완료 ${cost:.4f} (effort={budget_status.effort}, "
            f"캐시읽기 {getattr(response.usage, 'cache_read_input_tokens', 0):,} 토큰)"
        )

        decision = response.parsed_output
        if decision is None:
            logger.error(f"[전략] 스키마 파싱 실패 (stop_reason={response.stop_reason})")
            return StrategyOutput(None, cost, skipped="응답 파싱 실패")
        return StrategyOutput(decision, cost)
