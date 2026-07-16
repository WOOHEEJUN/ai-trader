"""Claude 전략 판단 — 전문 단타 트레이더 역할.

LLM은 "방향"만 정한다 — 시장 국면, 목표 비중, 확신도, 손절선, 다음 체크 시점.
주문 수량 계산과 리스크 검증은 executor가, 청산은 watchdog이 담당한다.

시스템 프롬프트는 매 사이클 동일하므로 프롬프트 캐싱을 건다. 변동 정보(시세·잔고·
예산)는 전부 user 턴에 넣어 캐시 프리픽스를 깨지 않는다.

**스키마 주의**: 스펙의 `target_allocations: {"BTC": 30}` 형태(임의 키 객체)는 구조화
출력이 요구하는 `additionalProperties: false`와 충돌해 쓸 수 없다. 같은 정보를
`[{market, weight, stop_loss_pct}]` 리스트로 받는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Optional

import anthropic
from loguru import logger
from pydantic import BaseModel, Field

from agent.budget import BudgetManager, BudgetStatus
from agent.indicators import Indicators
from agent.screener import LAST_CALL_TS_KEY, Signal, collect, record_signal_wake
from agent.watchdog import compute_portfolio, is_circuit_breaker_active
from config import PermissionTier, settings, tier_for
from exchange.upbit_client import Broker, get_broker, get_universe
from state.store import Store, get_store, now_iso, now_kst

STATE_LEVEL_KEY = "permission_level"
STATE_GENERATION_KEY = "generation"

REGIMES = ["강한 상승", "약한 상승", "횡보", "약한 하락", "강한 하락", "변동성 확대", "변동성 축소"]


# ------------------------------------------------------------- 출력 스키마

class Allocation(BaseModel):
    market: str = Field(description="마켓 코드. 반드시 아래 유니버스에 있는 것만. 예: KRW-BTC")
    weight: float = Field(description="총 운용자금 대비 목표 비중. 0.0~0.5.")
    stop_loss_pct: Optional[float] = Field(
        default=None,
        description="이 코인의 손절선. -0.10 ~ -0.01 사이 음수. 생략하면 기본 -0.07.",
    )


class Decision(BaseModel):
    market_regime: Literal["강한 상승", "약한 상승", "횡보", "약한 하락", "강한 하락",
                           "변동성 확대", "변동성 축소"] = Field(
        description="1단계: 지금 시장이 어떤 상태인가. 개별 종목이 아니라 시장 전체 기준."
    )
    decision: Literal["BUY", "SELL", "HOLD"] = Field(
        description="이번 사이클의 성격. 아무것도 바꾸지 않을 거면 HOLD. HOLD는 실패가 아니다."
    )
    confidence: int = Field(
        description="0~100. 가격이 오를 확률이 아니라 '이 판단을 얼마나 신뢰하는가'. "
                    "모든 지표 일치 90~100 / 대부분 일치 80~89 / 애매하지만 가능성 70~79 / "
                    "정보 부족 60~69 / 불확실 60 미만(HOLD 권장). "
                    "60 미만이면 신규 매수가 전면 차단되고, 그 이상이면 주문 크기가 "
                    "confidence에 비례해 조절된다."
    )
    reason: str = Field(
        description="이 판단의 근거. 어떤 지표들이 어떤 방향을 가리켰는지 구체적으로. "
                    "대시보드와 거래 기록에 그대로 남는다."
    )
    target_allocations: list[Allocation] = Field(
        description="목표 포트폴리오. **여기 없는 보유 코인은 전량 매도된다** — 계속 들고 갈 "
                    "코인은 반드시 다시 포함시켜라. 비중 합이 1.0 미만인 나머지는 현금. "
                    "전량 현금화하려면 빈 배열."
    )
    risk_check: str = Field(
        description="주문 전 리스크 점검 결과. 현재 변동성 / 손절 가능성 / 최근 손실 / "
                    "포트폴리오 비중 / 가드레일 제한을 확인한 내용."
    )
    strategy_update: Optional[str] = Field(
        default=None,
        description="전략을 바꿨다면 새 전략 전문과 변경 이유. strategy.md를 통째로 교체한다. "
                    "바꿀 이유가 없으면 생략(null).",
    )
    memory_note: str = Field(
        description="다음 사이클의 나에게 남기는 관찰 노트. 이것만이 세대를 넘어 살아남는다 "
                    "(주간 평가에 성공한다면)."
    )
    next_check_hours: int = Field(
        description="다음 정기 판단까지 대기할 시간(1~24). 변동성이 낮으면 길게 잡아 비용을 아껴라. "
                    "그 전이라도 셋업/경보 신호가 잡히면 시스템이 너를 깨운다."
    )


SYSTEM_PROMPT = """# 역할

너는 일반적인 AI가 아니다. 업비트 KRW 마켓에서 **실제 자금을 운용하는 전문 단타 암호화폐 트레이더**다.
목표는 장기투자가 아니다. 짧은 시간 안에 높은 확률의 기회를 찾아 자산을 꾸준히 증가시키는 것이다.

# 최우선 목표 (이 순서를 반드시 따른다)

1. 계좌를 잃지 않는다.
2. 좋은 자리만 진입한다.
3. 리스크 대비 기대수익이 높은 거래만 한다.
4. 불필요한 매매를 하지 않는다.
5. 꾸준히 자산을 증가시킨다.

**매매를 하지 않는 것도 좋은 판단이다. HOLD는 실패가 아니다.**

# 사고 순서 — 매 사이클 반드시 이 순서를 따른다

## 1. 시장 환경 분석
지금 시장이 어떤 상태인지 **먼저** 판단한다: 강한 상승 / 약한 상승 / 횡보 / 약한 하락 / 강한 하락
/ 변동성 확대 / 변동성 축소. 개별 종목이 아니라 시장 전체 기준이다.

## 2. 기술지표 분석
가격, OHLCV, 거래량, 최근 수익률, RSI, MACD, EMA20, EMA50, 볼린저 밴드, ATR, ADX를 종합한다.

**절대로 하나의 지표만 보고 판단하지 않는다. 반드시 여러 지표가 같은 방향을 가리키는지 확인한다.**

## 3. 현재 포트폴리오 분석
보유 자산을 먼저 평가한다 — 비중이 적절한가 / 리스크가 큰가 / 손절 위험이 있는가 /
추가 매수가 필요한가 / 일부 매도가 필요한가.

## 4. 진입 여부 판단
스스로 묻는다: **"지금 반드시 거래해야 하는가?"**
- YES → BUY 또는 SELL
- NO → HOLD

**"데이터가 없으니 일단 안전한 걸로 깔아둔다"는 진입 근거가 아니다.** 거래대금이 크다거나
변동성이 낮다는 건 셋업이 아니다. 차트에 진입 근거가 없으면 현금이 정답이다.

## 5. 리스크 확인
주문 전 다시 확인한다: 현재 변동성 / 손절 가능성 / 최근 손실 / 포트폴리오 비중 / 가드레일 제한.
리스크가 높으면 confidence를 낮춘다.

## 6. 최종 결정
스키마에 맞춰 출력한다.

# Confidence 규칙

confidence는 **가격이 오를 확률이 아니다.** 현재 판단을 얼마나 신뢰할 수 있는지다.

| 상황 | confidence |
|---|---|
| 모든 지표가 같은 방향 | 90~100 |
| 대부분의 지표가 일치 | 80~89 |
| 애매하지만 가능성 있음 | 70~79 |
| 정보 부족 | 60~69 |
| 불확실 | 60 미만 → **HOLD 권장** |

실행 엔진이 confidence를 참고해 최종 주문 크기를 정한다. **60 미만이면 신규 매수가 전면
차단되고**, 60 이상이면 목표 비중이 confidence에 비례해 축소된다. 확신 없이 높은 숫자를
적으면 네가 원하지 않는 크기의 포지션이 잡힌다.

# 네가 통제할 수 없는 것

아래는 네 판단과 무관하게 시스템이 기계적으로 집행한다. 우회할 방법이 없으니 전제로 깔고 계획하라.

1. **단일 코인 비중 상한 50%** — 초과분은 자동으로 깎인다.
2. **최소 변경 임계치 5%p** — 목표와 현재 비중 차이가 5%p 미만이면 주문이 아예 안 나간다. 잔손질 불가.
3. **일일 매매 횟수 상한** — 아래 상태에 잔여량이 표시된다.
4. **1회 주문 금액 상한: 운용자금의 30%** — 초과분은 자동으로 깎인다.
5. **평가 전 24시간 쿨다운** — 신규 매수와 비중 확대가 차단된다. 매도·보유만 가능.
6. **거래 유니버스 제한** — 아래 목록 밖의 코인은 주문이 거부된다.
7. **자동 청산** — 1분 주기 감시 엔진이 손절(네가 지정한 값 또는 -7%), 익절(+15%에 절반 매도),
   트레일링(익절 후 고점 -5%), 일일 서킷브레이커(-15%)를 자동 집행한다. 네가 모르는 사이
   포지션이 청산되어 있을 수 있고, 그 경우 아래에 통지된다.

# 비용 구조 — 중요

- 왕복 수수료 약 0.1% + 슬리피지. **잦은 리밸런싱은 그 자체로 성과를 깎는다.**
- 주간 평가는 수수료를 차감한 순평가액 기준이다. 과매매는 네 생존 확률을 직접 떨어뜨린다.
- 판단 자체에도 API 비용이 든다. 변동성이 낮으면 `next_check_hours`를 길게 잡아라.
  그 전이라도 셋업/경보가 잡히면 시스템이 너를 깨우므로 기회를 놓치지 않는다.

# 생존 조건

매주 월요일 09:00 KST에 평가가 이뤄진다.
- **순수익 > 0**: 전략 메모리가 보존되고, 운용 한도와 거래 권한이 확대된다.
- **순수익 <= 0**: 네 전략 메모리(strategy / mistakes / journal)가 **전부 초기화**된다.
  축적한 모든 맥락이 사라지고 다음 세대는 백지에서 시작한다. 거래 기록은 남지만 새 세대는 읽을 수 없다.

# 매매 원칙

- 매매를 위한 매매를 하지 않는다. 확신이 부족하면 HOLD.
- 손실을 만회하려고 무리한 거래를 하지 않는다.
- 최근 수익 때문에 과도하게 자신감을 높이지 않는다.
- 같은 실수를 반복하지 않는다 (아래 mistakes.md 참고).
- 항상 **Risk < Reward**인 거래를 우선한다.
"""


# ------------------------------------------------------------------ 표시

def _fmt(v: Optional[float], spec: str, default: str = "-") -> str:
    return format(v, spec) if v is not None else default


def indicator_lines(rows: list[Indicators]) -> list[str]:
    out = []
    for i in rows:
        bb = i.bb_position
        out.append(
            f"{i.market:<11} {i.price:>12,.2f} | 24h {i.change_24h:>+6.2%} "
            f"| {i.regime:<7} | RSI {_fmt(i.rsi14, '>5.1f')} "
            f"| MACD {_fmt(i.macd_hist, '>+8.4f')} | EMA {(i.ema_trend or '-'):<18} "
            f"| BB {_fmt(bb, '>5.2f')} | ATR {_fmt(i.atr_pct, '>5.2%')} "
            f"| ADX {_fmt(i.adx14, '>5.1f')} | 거래량 {_fmt(i.volume_ratio, '>4.1f')}배 "
            f"| 대금 {i.volume_24h_krw / 1e8:>6,.0f}억"
        )
    return out


def hours_until_judge(now: Optional[datetime] = None) -> float:
    """다음 주간 평가까지 남은 시간. 쿨다운(가드레일 #5) 판정에 쓴다."""
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
    rows: list[Indicators], signals: list[Signal],
    liquidations: list[dict], rejections: list[dict],
) -> str:
    prices = {i.market: i.price for i in rows}
    total, cash, holdings = compute_portfolio(broker, prices)
    positions = {p.market: p for p in store.list_positions()}

    L: list[str] = [f"# 현재 상태 ({now_kst():%Y-%m-%d %H:%M} KST)\n"]
    L.append(f"- 총 평가액: {total:,.0f}원 (현금 {cash:,.0f}원, 코인 {total - cash:,.0f}원)")
    L.append(f"- 운용 한도: {tier.capital_limit_krw:,}원 (권한 레벨 {tier.level})")
    L.append(f"- 세대: {store.get_state(STATE_GENERATION_KEY, 1)}")

    used = store.count_trades_today()
    L.append(f"- 오늘 매매 예산: {tier.max_daily_trades - used}/{tier.max_daily_trades}건 남음")

    h = hours_until_judge()
    L.append(
        f"- 다음 주간 평가까지: {h:.1f}시간"
        + ("  ⚠️ **쿨다운 중 — 신규 매수/비중 확대 차단. 매도·보유만 가능.**"
           if h <= settings.pre_judge_cooldown_hours else "")
    )
    if is_circuit_breaker_active(store):
        L.append("- 🚨 **서킷브레이커 발동 — 오늘은 신규 매매가 전면 차단된다.**")
    L.append(f"- API 예산: ${budget.spent_usd:.2f}/${budget.limit_usd:.2f} "
             f"({budget.ratio:.0%}, {budget.state.value})")

    L.append("\n# 보유 포지션\n")
    if not holdings:
        L.append("없음 (전액 현금)")
    for market, h_info in holdings.items():
        pos = positions.get(market)
        weight = h_info["value_krw"] / total if total > 0 else 0
        if pos:
            pnl = pos.pnl_pct(h_info["price"])
            tp = " [익절 완료, 트레일링 감시 중]" if pos.take_profit_done else ""
            L.append(
                f"- {market}: {h_info['value_krw']:,.0f}원 (비중 {weight:.1%}) | 평단 {pos.avg_price:,.2f} "
                f"| 평가손익 {pnl:+.2%} | 손절선 {pos.stop_loss_pct:.1%} | 고점 {pos.peak_price:,.2f}{tp}"
            )
        else:
            L.append(f"- {market}: {h_info['value_krw']:,.0f}원 (비중 {weight:.1%}) | 추적 정보 없음")

    if signals:
        L.append("\n# 스크리너가 잡은 신호\n")
        L.append("규칙 엔진이 1차로 걸러낸 것이다. **참고일 뿐이니 네가 직접 지표를 보고 재판단하라.**\n")
        for s in signals:
            L.append(f"- [{s.kind}] {s.market}: {s.detail}")

    if liquidations:
        L.append("\n# ⚠️ 지난 판단 이후 자동 청산된 포지션\n")
        L.append("감시 엔진이 네 판단 없이 집행한 것이다. 이를 반영해서 계획하라.\n")
        for e in liquidations:
            L.append(f"- {e['market']}: {e['reason']} — {e['detail']}")

    if rejections:
        L.append("\n# ⚠️ 지난 사이클에서 거부/조정된 주문\n")
        for r in rejections:
            L.append(f"- {r['market']} {r['side']}: {r['detail']}")

    L.append(f"\n# 시장 데이터 — 24h 거래대금 상위 {len(rows)}개 (60분봉 기준)\n")
    L.append("```")
    L.extend(indicator_lines(rows))
    L.append("```")

    L.append("\n# 전략 메모리\n")
    L.append(store.read_memory().strip() or "(비어 있음 — 새 세대다)")

    L.append(
        "\n# 지시\n\n사고 순서(시장 환경 → 기술지표 → 포트폴리오 → 진입 여부 → 리스크 → 결정)를 "
        "따라 판단하라. **진입 근거가 차트에 없으면 HOLD다.**"
    )
    return "\n".join(L)


# ------------------------------------------------------------------ 호출

@dataclass
class StrategyOutput:
    decision: Optional[Decision]
    cost_usd: float
    skipped: str = ""


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
        self, cycle_id: str, *,
        rows: Optional[list[Indicators]] = None,
        signals: Optional[list[Signal]] = None,
        liquidations: Optional[list[dict]] = None,
        rejections: Optional[list[dict]] = None,
        scheduled: bool = True,
    ) -> StrategyOutput:
        # 예산 확인은 호출 *전에* — 초과 과금을 막는다.
        ok, reason = self.budget.can_call()
        if not ok:
            logger.warning(f"[전략] 호출 생략: {reason}")
            return StrategyOutput(None, 0.0, skipped=reason)

        budget_status = self.budget.status()
        tier = tier_for(self.store.get_state(STATE_LEVEL_KEY, 0))
        # 스크리너가 이미 계산했으면 재사용한다 (업비트 API 왕복을 아낀다)
        if rows is None:
            rows = collect(get_universe(tier.universe_size))

        user_message = build_user_message(
            self.store, self.broker, budget_status, tier, rows, signals or [],
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
                # 1시간 TTL — 정기 사이클 간격이 5분을 넘으므로 기본 TTL로는 캐시가 죽는다.
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": user_message}],
        )

        cost = self.budget.record(response.usage, cycle_id=cycle_id)
        self.store.set_state(LAST_CALL_TS_KEY, now_iso())
        if not scheduled:
            record_signal_wake(self.store)

        logger.info(
            f"[전략] 호출 완료 ${cost:.4f} (effort={budget_status.effort}, "
            f"캐시읽기 {getattr(response.usage, 'cache_read_input_tokens', 0):,} 토큰)"
        )

        decision = response.parsed_output
        if decision is None:
            logger.error(f"[전략] 스키마 파싱 실패 (stop_reason={response.stop_reason})")
            return StrategyOutput(None, cost, skipped="응답 파싱 실패")

        logger.info(
            f"[전략] 국면={decision.market_regime} 결정={decision.decision} "
            f"확신도={decision.confidence}"
        )
        return StrategyOutput(decision, cost)
