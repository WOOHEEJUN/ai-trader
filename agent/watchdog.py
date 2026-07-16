"""청산 감시 — 손절 / 익절 / 트레일링 / 서킷브레이커.

**이 모듈은 Claude를 호출하지 않는다.** 순수 규칙 엔진이므로 API 비용이 0원이고,
예산이 소진돼 판단(strategist)이 정지해도 계속 돌면서 포지션을 지킨다.

1분 주기로 실행되는 이유: LLM 사이클은 최대 24시간 간격까지 벌어질 수 있는데,
크립토는 그 사이에 -20%가 나올 수 있는 시장이다. 손절을 사이클에 묶으면 손절선이
무의미해진다.

우선순위: 손절 > 트레일링 > 익절. 손절을 먼저 보는 이유는 어떤 상황에서도
"손실 확대 방지"가 "수익 실현"보다 앞서야 하기 때문.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from config import settings
from exchange.upbit_client import Balance, Broker, get_broker, get_prices
from notify import notify
from state.store import Position, Store, get_store, now_iso, now_kst

CIRCUIT_BREAKER_DATE_KEY = "circuit_breaker_date"
WATCHDOG_LAST_RUN_KEY = "watchdog_last_run"

REASON_LABELS = {
    "stop_loss": "손절",
    "take_profit": "익절",
    "trailing": "트레일링",
    "circuit_breaker": "서킷브레이커",
}


@dataclass
class Liquidation:
    market: str
    reason: str  # stop_loss | take_profit | trailing | circuit_breaker
    qty: float
    price: float
    pnl_pct: float
    detail: str


def compute_portfolio(broker: Broker, prices: dict[str, float] | None = None) -> tuple[float, float, dict]:
    """(총평가액, 현금, 코인별 상세). 스냅샷 잡과 서킷브레이커가 공유한다."""
    bal = broker.get_balance()
    markets = list(bal.holdings)
    if prices is None:
        prices = get_prices(markets) if markets else {}
    holdings = {
        m: {"qty": q, "price": prices.get(m, 0.0), "value_krw": q * prices.get(m, 0.0)}
        for m, q in bal.holdings.items()
    }
    total = bal.cash_krw + sum(h["value_krw"] for h in holdings.values())
    return total, bal.cash_krw, holdings


def is_circuit_breaker_active(store: Optional[Store] = None) -> bool:
    """오늘 서킷브레이커가 발동했는가. executor가 신규 매매를 막을 때 확인한다."""
    store = store or get_store()
    return store.get_state(CIRCUIT_BREAKER_DATE_KEY) == now_kst().strftime("%Y-%m-%d")


class Watchdog:
    def __init__(self, store: Optional[Store] = None, broker: Optional[Broker] = None) -> None:
        self.store = store or get_store()
        self.broker = broker or get_broker(self.store)

    # ------------------------------------------------------------------ 진입점
    def run_once(self) -> list[Liquidation]:
        """1분마다 호출된다. 어떤 예외도 스케줄러 밖으로 던지지 않는다."""
        self.store.set_state(WATCHDOG_LAST_RUN_KEY, now_iso())
        try:
            positions = self.store.list_positions()
            if not positions:
                return []

            prices = get_prices([p.market for p in positions])
            balance = self.broker.get_balance()

            breaker = self._check_circuit_breaker(positions, prices, balance)
            if breaker is not None:
                return breaker

            events: list[Liquidation] = []
            for pos in positions:
                price = prices.get(pos.market)
                if not price:
                    logger.warning(f"[감시] 시세 없음, 건너뜀: {pos.market}")
                    continue
                self.store.update_peak(pos.market, price)
                pos.peak_price = max(pos.peak_price, price)
                event = self._evaluate(pos, price, balance)
                if event:
                    events.append(event)
            return events
        except Exception as e:  # noqa: BLE001 — 감시 잡은 절대 죽으면 안 된다
            logger.exception(f"[감시] 실행 중 오류(다음 주기에 재시도): {e}")
            return []

    # ------------------------------------------------------------ 규칙 판정
    def _evaluate(self, pos: Position, price: float, balance: Balance) -> Optional[Liquidation]:
        pnl = pos.pnl_pct(price)

        # 1) 손절 — 최우선
        if pnl <= pos.stop_loss_pct:
            return self._liquidate(
                pos, price, "stop_loss", pos.qty, balance,
                detail=f"평단 {pos.avg_price:,.0f} 대비 {pnl:+.2%} (손절선 {pos.stop_loss_pct:.0%})",
            )

        # 2) 익절이 이미 발동한 포지션은 트레일링으로 관리
        if pos.take_profit_done:
            drop = price / pos.peak_price - 1 if pos.peak_price > 0 else 0.0
            if drop <= settings.trailing_stop_pct:
                return self._liquidate(
                    pos, price, "trailing", pos.qty, balance,
                    detail=f"고점 {pos.peak_price:,.0f} 대비 {drop:+.2%} (트레일링 {settings.trailing_stop_pct:.0%}), 실현 {pnl:+.2%}",
                )
            return None

        # 3) 익절 — 절반만 매도하고 잔량은 트레일링으로 전환
        if pnl >= settings.take_profit_pct:
            half = pos.qty * settings.take_profit_ratio
            remainder = pos.qty - half
            # 절반 또는 잔량이 업비트 최소주문금액(5,000원) 미만이면 쪼갤 수 없다 → 전량 익절
            if half * price < settings.min_order_krw or remainder * price < settings.min_order_krw:
                return self._liquidate(
                    pos, price, "take_profit", pos.qty, balance,
                    detail=f"{pnl:+.2%} 익절. 절반 매도 시 잔량이 최소주문금액 미만이라 전량 청산",
                )
            return self._liquidate(
                pos, price, "take_profit", half, balance,
                detail=f"{pnl:+.2%} 익절, 절반 매도. 잔량은 트레일링({settings.trailing_stop_pct:.0%})으로 전환",
                take_profit_done=True,
            )

        return None

    # ------------------------------------------------------- 서킷브레이커
    def _check_circuit_breaker(
        self, positions: list[Position], prices: dict[str, float], balance: Balance
    ) -> Optional[list[Liquidation]]:
        today = now_kst().strftime("%Y-%m-%d")
        if self.store.get_state(CIRCUIT_BREAKER_DATE_KEY) == today:
            return None  # 오늘 이미 발동 — 중복 청산 방지

        midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
        baseline = self.store.snapshot_at_or_before(midnight.isoformat())
        if baseline is None or baseline["total_krw"] <= 0:
            return None  # 기준 스냅샷이 아직 없다 (가동 첫날)

        total = balance.cash_krw + sum(p.qty * prices.get(p.market, 0.0) for p in positions)
        change = total / baseline["total_krw"] - 1
        if change > settings.daily_circuit_breaker_pct:
            return None

        self.store.set_state(CIRCUIT_BREAKER_DATE_KEY, today)
        notify(
            f"🚨 서킷브레이커 발동: 당일 {change:+.2%} "
            f"({baseline['total_krw']:,.0f} → {total:,.0f}원). 전 포지션 청산 후 당일 매매 중단.",
            level="error",
        )
        events = []
        for pos in positions:
            price = prices.get(pos.market)
            if not price:
                continue
            event = self._liquidate(
                pos, price, "circuit_breaker", pos.qty, balance,
                detail=f"당일 총평가액 {change:+.2%} (한도 {settings.daily_circuit_breaker_pct:.0%})",
            )
            if event:
                events.append(event)
        return events

    # ------------------------------------------------------------- 주문 실행
    def _liquidate(
        self,
        pos: Position,
        price: float,
        reason: str,
        qty: float,
        balance: Balance,
        *,
        detail: str,
        take_profit_done: bool | None = None,
    ) -> Optional[Liquidation]:
        # 거래소 실제 잔량을 넘어서 팔 수 없다 (실거래에서 내부 장부와 드리프트가 생길 수 있음)
        available = balance.holdings.get(pos.market, pos.qty)
        qty = min(qty, available)
        pnl = pos.pnl_pct(price)
        label = REASON_LABELS.get(reason, reason)

        if qty * price < settings.min_order_krw:
            logger.warning(
                f"[감시] {pos.market} {label} 조건 충족했으나 매도금액 {qty * price:,.0f}원이 "
                f"최소주문금액 미만이라 실행 불가"
            )
            return None

        try:
            fill = self.broker.sell_market(pos.market, qty)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[감시] {pos.market} {label} 매도 실패: {e}")
            self.store.record_trade(
                side="sell", market=pos.market, reason_type=reason, status="failed",
                qty=qty, price=price, reason_text=detail, reject_reason=str(e),
            )
            notify(f"⚠️ {pos.market} {label} 매도 실패: {e}", level="error")
            return None

        self.store.apply_sell(pos.market, fill.qty, take_profit_done=take_profit_done)
        self.store.record_trade(
            side="sell", market=pos.market, reason_type=reason, status="filled",
            qty=fill.qty, price=fill.price, amount_krw=fill.amount_krw, fee_krw=fill.fee_krw,
            reason_text=detail, order_uuid=fill.uuid,
        )
        logger.info(f"[감시] {pos.market} {label} 청산: {detail}")
        notify(f"{pos.market} {label} 청산 ({pnl:+.2%}) — {detail}")
        return Liquidation(pos.market, reason, fill.qty, fill.price, pnl, detail)
