"""가드레일 검증 + 리밸런싱 주문 실행.

Claude의 목표 비중을 받아 실제 주문으로 바꾼다. 가드레일은 세 가지로 나뉜다:

  거부(reject) — 실행하면 안 되는 주문. 통째로 버린다.
      · 유니버스 밖 종목 (#7)
      · 쿨다운 중 신규 매수/비중 확대 (#1)
      · 서킷브레이커 발동일의 모든 신규 매매 (#5)
      · 일일 매매 횟수 초과 (#4)

  조정(clamp) — 의도는 살리되 한도까지 깎는다.
      · 단일 코인 비중 상한 50% (#2)
      · 1회 주문 금액 상한 30% (#6)
      · 비중 합이 1.0을 넘을 때 비례 축소
      · 손절선 범위

  생략(skip) — 주문할 이유가 없다.
      · 목표-현재 비중 차이 5%p 미만 (#3)
      · 주문 금액이 최소주문금액(5,000원) 미만

조정을 택한 이유: "BTC 70%"를 통째로 거부하면 BTC를 0%로 두게 되어 Claude의 의도에서
더 멀어진다. 50%로 깎는 쪽이 안전하면서 의도에 가깝다. 거부/조정 내역은 모두 다음
사이클 컨텍스트로 피드백되어 Claude가 규칙을 학습한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from agent.strategist import Decision, hours_until_judge
from agent.watchdog import compute_portfolio, is_circuit_breaker_active
from config import settings, tier_for
from exchange.upbit_client import Broker, Fill, get_broker, get_prices, get_universe
from state.store import Store, get_store

STATE_LEVEL_KEY = "permission_level"


@dataclass
class Note:
    """Claude에게 돌려줄 거부/조정 사유."""
    market: str
    side: str
    kind: str  # rejected | clamped
    detail: str

    def as_dict(self) -> dict:
        return {"market": self.market, "side": self.side, "kind": self.kind, "detail": self.detail}


@dataclass
class ExecutionResult:
    fills: list[Fill] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    @property
    def traded(self) -> bool:
        return bool(self.fills)


class Executor:
    def __init__(self, store: Optional[Store] = None, broker: Optional[Broker] = None) -> None:
        self.store = store or get_store()
        self.broker = broker or get_broker(self.store)

    # ------------------------------------------------------------- 진입점
    def execute(self, decision: Decision, cycle_id: str) -> ExecutionResult:
        result = ExecutionResult()
        tier = tier_for(self.store.get_state(STATE_LEVEL_KEY, 0))

        # 서킷브레이커: 오늘은 신규 매매 전면 차단 (#5)
        if is_circuit_breaker_active(self.store):
            result.notes.append(Note("-", "-", "rejected",
                                     "서킷브레이커 발동일 — 당일 신규 매매 전면 차단"))
            logger.warning("[실행] 서킷브레이커 발동 상태 — 모든 주문 거부")
            return result

        universe = set(get_universe(tier.universe_size))
        targets = self._resolve_targets(decision, universe, result)

        balance = self.broker.get_balance()
        markets = set(targets) | set(balance.holdings)
        prices = get_prices(list(markets)) if markets else {}
        total, cash, holdings = compute_portfolio(self.broker, prices)
        if total <= 0:
            result.notes.append(Note("-", "-", "rejected", "총 평가액이 0 — 실행할 수 없다"))
            return result

        # 운용 한도 초과분은 매매 대상에서 제외한다 (권한 레벨이 허용하는 만큼만 굴린다)
        working_capital = min(total, tier.capital_limit_krw)
        if working_capital < total:
            result.notes.append(Note("-", "-", "clamped",
                                     f"운용 한도 {tier.capital_limit_krw:,}원 초과분은 비중 계산에서 제외"))

        cooldown = hours_until_judge() <= settings.pre_judge_cooldown_hours
        current = {m: h["value_krw"] / working_capital for m, h in holdings.items()}

        plans = self._build_plans(targets, current, prices, holdings, working_capital, cooldown, result)

        # 일일 매매 예산: 변화폭이 큰 주문부터 채운다 (#4)
        remaining = tier.max_daily_trades - self.store.count_trades_today()
        plans.sort(key=lambda p: (0 if p["side"] == "sell" else 1, -abs(p["delta"])))
        if len(plans) > max(remaining, 0):
            for p in plans[max(remaining, 0):]:
                result.notes.append(Note(p["market"], p["side"], "rejected",
                                         f"일일 매매 한도 {tier.max_daily_trades}건 소진"))
            plans = plans[:max(remaining, 0)]

        for plan in plans:
            self._place(plan, cycle_id, decision.rationale, result)

        self._sync_stop_losses(targets, result)
        return result

    # --------------------------------------------------- 목표 비중 정규화
    def _resolve_targets(self, decision: Decision, universe: set[str], result: ExecutionResult) -> dict[str, dict]:
        targets: dict[str, dict] = {}
        for alloc in decision.allocations:
            market = alloc.market.strip().upper()

            if market not in universe:
                result.notes.append(Note(market, "buy", "rejected",
                                         f"유니버스 밖 종목 — 거래대금 상위 {len(universe)}개만 허용"))
                continue

            weight = max(0.0, float(alloc.weight))
            if weight > settings.max_position_weight:
                result.notes.append(Note(market, "buy", "clamped",
                                         f"비중 {weight:.0%} → {settings.max_position_weight:.0%} "
                                         f"(단일 코인 상한)"))
                weight = settings.max_position_weight

            stop = alloc.stop_loss_pct
            if stop is not None:
                clamped = max(settings.stop_loss_floor_pct, min(float(stop), -0.01))
                if abs(clamped - float(stop)) > 1e-9:
                    result.notes.append(Note(market, "-", "clamped",
                                             f"손절선 {stop:.1%} → {clamped:.1%} "
                                             f"(허용 범위 {settings.stop_loss_floor_pct:.0%}~-1%)"))
                stop = clamped

            if market in targets:
                result.notes.append(Note(market, "-", "clamped", "중복 지정 — 마지막 값만 사용"))
            targets[market] = {"weight": weight, "stop_loss_pct": stop}

        # 비중 합이 1.0을 넘으면 비례 축소 (현금 없이는 살 수 없다)
        total_weight = sum(t["weight"] for t in targets.values())
        if total_weight > 1.0:
            for t in targets.values():
                t["weight"] /= total_weight
            result.notes.append(Note("-", "-", "clamped",
                                     f"비중 합 {total_weight:.0%} → 100%로 비례 축소"))
        return targets

    # ------------------------------------------------------- 주문 계획 수립
    def _build_plans(
        self, targets: dict, current: dict, prices: dict, holdings: dict,
        working_capital: float, cooldown: bool, result: ExecutionResult,
    ) -> list[dict]:
        plans: list[dict] = []
        for market in set(targets) | set(current):
            target_w = targets.get(market, {}).get("weight", 0.0)
            current_w = current.get(market, 0.0)
            delta = target_w - current_w
            price = prices.get(market, 0.0)
            if price <= 0:
                continue

            # #3 최소 변경 임계치 — 잔손질로 수수료 새는 것을 막는다
            if abs(delta) < settings.min_rebalance_delta:
                continue

            if delta < 0:
                qty = min(abs(delta) * working_capital / price, holdings.get(market, {}).get("qty", 0.0))
                if target_w == 0.0:  # 목표에서 빠진 코인은 전량 매도
                    qty = holdings.get(market, {}).get("qty", 0.0)
                if qty * price < settings.min_order_krw:
                    continue
                plans.append({"market": market, "side": "sell", "qty": qty, "delta": delta})
            else:
                if cooldown:
                    result.notes.append(Note(market, "buy", "rejected",
                                             "평가 전 24시간 쿨다운 — 신규 매수/비중 확대 차단"))
                    continue
                krw = delta * working_capital
                cap = settings.max_order_ratio * working_capital
                if krw > cap:
                    result.notes.append(Note(market, "buy", "clamped",
                                             f"주문금액 {krw:,.0f}원 → {cap:,.0f}원 "
                                             f"(1회 상한 {settings.max_order_ratio:.0%})"))
                    krw = cap
                plans.append({"market": market, "side": "buy", "krw": krw, "delta": delta,
                              "stop_loss_pct": targets[market].get("stop_loss_pct")})
        return plans

    # ---------------------------------------------------------- 주문 집행
    def _place(self, plan: dict, cycle_id: str, rationale: str, result: ExecutionResult) -> None:
        market, side = plan["market"], plan["side"]
        try:
            if side == "sell":
                fill = self.broker.sell_market(market, plan["qty"])
                self.store.apply_sell(market, fill.qty)
            else:
                # 현금은 매도 체결 이후에 확정되므로 이 시점에 다시 확인한다
                cash = self.broker.get_balance().cash_krw
                krw = min(plan["krw"], cash)
                if krw < settings.min_order_krw:
                    result.notes.append(Note(market, "buy", "rejected",
                                             f"현금 부족 — 가용 {cash:,.0f}원"))
                    return
                fill = self.broker.buy_market(market, krw)
                stop = plan.get("stop_loss_pct") or settings.stop_loss_pct
                self.store.apply_buy(market, fill.qty, fill.price, stop)

            self.store.record_trade(
                side=side, market=market, reason_type="llm", status="filled",
                qty=fill.qty, price=fill.price, amount_krw=fill.amount_krw, fee_krw=fill.fee_krw,
                reason_text=rationale, cycle_id=cycle_id, order_uuid=fill.uuid,
            )
            result.fills.append(fill)
            logger.info(f"[실행] {market} {side} 체결: {fill.qty:.8f} @ {fill.price:,.2f}")
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 나머지를 막으면 안 된다
            logger.error(f"[실행] {market} {side} 실패: {e}")
            self.store.record_trade(
                side=side, market=market, reason_type="llm", status="failed",
                reason_text=rationale, reject_reason=str(e), cycle_id=cycle_id,
            )
            result.notes.append(Note(market, side, "rejected", f"주문 실패: {e}"))

    # ------------------------------------------------------- 손절선 동기화
    def _sync_stop_losses(self, targets: dict, result: ExecutionResult) -> None:
        """Claude가 기존 포지션의 손절선을 바꿨으면 반영한다 (매수가 없어도)."""
        for market, spec in targets.items():
            stop = spec.get("stop_loss_pct")
            if stop is None:
                continue
            pos = self.store.get_position(market)
            if pos and abs(pos.stop_loss_pct - stop) > 1e-9:
                self.store.set_stop_loss(market, stop)
                logger.info(f"[실행] {market} 손절선 갱신: {pos.stop_loss_pct:.1%} → {stop:.1%}")
