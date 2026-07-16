"""청산 규칙 4종 검증.

실제 돈을 지키는 코드이므로 각 규칙의 발동/미발동 경계를 모두 확인한다.
"""
from __future__ import annotations

import pytest

from agent import watchdog as wd_mod
from agent.watchdog import CIRCUIT_BREAKER_DATE_KEY, Watchdog, is_circuit_breaker_active
from config import settings
from state.store import now_kst

ENTRY = 100.0  # 진입 평단 (계산이 눈에 보이도록 100원으로 잡는다)
QTY = 1_000.0  # 10만원어치


@pytest.fixture
def wd(store, broker, monkeypatch):
    """KRW-TEST에 10만원 포지션을 들고 있는 감시기."""
    monkeypatch.setattr(wd_mod, "get_prices", lambda markets: {m: broker.prices[m] for m in markets})
    store.apply_buy("KRW-TEST", QTY, ENTRY, settings.stop_loss_pct)
    broker.holdings["KRW-TEST"] = QTY
    broker.cash = 0.0
    return Watchdog(store=store, broker=broker)


def set_price(broker, price: float) -> None:
    broker.prices["KRW-TEST"] = price


# ---------------------------------------------------------------- 손절 (-7%)

def test_stop_loss_triggers_full_liquidation(wd, store, broker):
    set_price(broker, ENTRY * 0.92)  # -8% → 손절선 -7% 하회
    events = wd.run_once()

    assert len(events) == 1
    assert events[0].reason == "stop_loss"
    assert store.get_position("KRW-TEST") is None, "손절은 전량 청산이어야 한다"
    assert broker.sells == [("KRW-TEST", QTY)]

    trade = store.list_trades()[0]
    assert trade["reason_type"] == "stop_loss" and trade["status"] == "filled"


def test_stop_loss_not_triggered_above_threshold(wd, store, broker):
    set_price(broker, ENTRY * 0.94)  # -6% → 아직 손절선 위
    assert wd.run_once() == []
    assert store.get_position("KRW-TEST").qty == QTY


def test_stop_loss_threshold_is_at_seven_percent(wd, store, broker):
    """임계값을 -6.99% / -7.01%로 좁게 감싸 손절선이 실제로 -7%에 있음을 확인한다.

    부동소수점 때문에 정확히 -7.00%인 가격을 만들 수 없으므로(100*0.93 = 93.00000000000001)
    경계 '같음'이 아니라 경계의 '위치'를 검증한다. 1e-17 오차는 금액상 무의미하다.
    """
    set_price(broker, ENTRY * 0.9301)  # -6.99% → 미발동
    assert wd.run_once() == []

    set_price(broker, ENTRY * 0.9299)  # -7.01% → 발동
    events = wd.run_once()
    assert len(events) == 1 and events[0].reason == "stop_loss"


def test_custom_stop_loss_is_respected(wd, store, broker):
    store.set_stop_loss("KRW-TEST", -0.03)  # Claude가 타이트하게 잡은 경우
    set_price(broker, ENTRY * 0.96)  # -4% → 기본 -7%엔 안 걸리지만 -3%엔 걸린다
    events = wd.run_once()
    assert len(events) == 1 and events[0].reason == "stop_loss"


# -------------------------------------------------------------- 익절 (+15%)

def test_take_profit_sells_half_and_flags_position(wd, store, broker):
    set_price(broker, ENTRY * 1.16)  # +16%
    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "take_profit"
    pos = store.get_position("KRW-TEST")
    assert pos is not None, "익절은 절반만 팔아야 한다 (전량 청산 금지)"
    assert pos.qty == pytest.approx(QTY * 0.5)
    assert pos.take_profit_done is True, "잔량이 트레일링으로 전환되려면 플래그가 서야 한다"


def test_take_profit_not_triggered_below_threshold(wd, store, broker):
    set_price(broker, ENTRY * 1.14)  # +14%
    assert wd.run_once() == []
    assert store.get_position("KRW-TEST").qty == QTY


def test_take_profit_sells_all_when_remainder_below_min_order(store, broker, monkeypatch):
    """절반 매도 시 잔량이 5,000원 미만이면 쪼갤 수 없으므로 전량 익절한다."""
    monkeypatch.setattr(wd_mod, "get_prices", lambda markets: {m: broker.prices[m] for m in markets})
    # 8,000원어치 포지션 → 절반은 4,000원으로 최소주문금액 미달
    small_qty = 80.0
    store.apply_buy("KRW-SMALL", small_qty, ENTRY, settings.stop_loss_pct)
    broker.holdings["KRW-SMALL"] = small_qty
    broker.prices["KRW-SMALL"] = ENTRY * 1.16

    events = Watchdog(store=store, broker=broker).run_once()

    assert len(events) == 1 and events[0].reason == "take_profit"
    assert store.get_position("KRW-SMALL") is None, "쪼갤 수 없으면 전량 청산"
    assert broker.sells == [("KRW-SMALL", small_qty)]


# ------------------------------------------------------ 트레일링 (고점 -5%)

def test_trailing_stop_after_take_profit(wd, store, broker):
    set_price(broker, ENTRY * 1.20)  # +20% → 익절 발동, 고점 120 기록
    wd.run_once()
    assert store.get_position("KRW-TEST").take_profit_done

    set_price(broker, ENTRY * 1.13)  # 고점 120 대비 -5.8%
    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "trailing"
    assert store.get_position("KRW-TEST") is None, "트레일링은 잔량 전량 청산"


def test_trailing_not_triggered_within_band(wd, store, broker):
    set_price(broker, ENTRY * 1.20)
    wd.run_once()
    set_price(broker, ENTRY * 1.17)  # 고점 대비 -2.5% → 아직 여유
    assert wd.run_once() == []
    assert store.get_position("KRW-TEST") is not None


def test_trailing_inactive_before_take_profit(wd, store, broker):
    """익절 전에는 고점 대비 -5%여도 트레일링이 작동하면 안 된다."""
    set_price(broker, ENTRY * 1.10)  # 고점 110
    wd.run_once()
    set_price(broker, ENTRY * 1.04)  # 고점 대비 -5.5%, 하지만 익절 미발동
    assert wd.run_once() == []
    assert store.get_position("KRW-TEST").qty == QTY


def test_peak_tracks_high_water_mark(wd, store, broker):
    set_price(broker, ENTRY * 1.30)
    wd.run_once()  # 익절 + 고점 130
    set_price(broker, ENTRY * 1.25)
    wd.run_once()  # 고점 대비 -3.8% → 유지
    assert store.get_position("KRW-TEST").peak_price == pytest.approx(ENTRY * 1.30)


# ------------------------------------------------- 서킷브레이커 (당일 -15%)

def test_circuit_breaker_liquidates_all_and_blocks_day(wd, store, broker):
    midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    store.record_snapshot(100_000, 0.0, {}, ts=midnight.isoformat())
    set_price(broker, ENTRY * 0.84)  # 총평가액 84,000원 → 당일 -16%

    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "circuit_breaker"
    assert store.get_position("KRW-TEST") is None
    assert is_circuit_breaker_active(store), "당일 매매가 차단되어야 한다"


def test_circuit_breaker_not_triggered_above_threshold(wd, store, broker):
    midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    store.record_snapshot(100_000, 0.0, {}, ts=midnight.isoformat())
    set_price(broker, ENTRY * 0.94)  # -6% → 손절선(-7%)도 서킷브레이커(-15%)도 미달

    assert wd.run_once() == []
    assert not is_circuit_breaker_active(store)


def test_circuit_breaker_needs_baseline_snapshot(wd, store, broker):
    """가동 첫날처럼 기준 스냅샷이 없으면 서킷브레이커는 판단하지 않는다."""
    set_price(broker, ENTRY * 0.50)  # 반토막이어도
    events = wd.run_once()
    assert all(e.reason != "circuit_breaker" for e in events)
    assert not is_circuit_breaker_active(store)


def test_circuit_breaker_fires_once_per_day(wd, store, broker):
    midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    store.record_snapshot(100_000, 0.0, {}, ts=midnight.isoformat())
    set_price(broker, ENTRY * 0.80)
    wd.run_once()

    # 같은 날 다시 포지션이 생겨도 중복 발동하지 않는다 (이미 차단 상태)
    store.apply_buy("KRW-TEST", QTY, ENTRY, settings.stop_loss_pct)
    broker.holdings["KRW-TEST"] = QTY
    sells_before = len(broker.sells)
    wd.run_once()
    assert len(broker.sells) == sells_before + 1, "서킷브레이커가 아니라 손절로 처리되어야 한다"
    assert store.list_trades()[0]["reason_type"] == "stop_loss"


# ------------------------------------------------------------ 실패/견고성

def test_sell_failure_is_recorded_and_does_not_crash(wd, store, broker):
    broker.fail_on.add("KRW-TEST")
    set_price(broker, ENTRY * 0.90)  # 손절 조건

    events = wd.run_once()

    assert events == [], "매도 실패 시 청산 이벤트를 만들면 안 된다"
    assert store.get_position("KRW-TEST") is not None, "실패했으므로 포지션은 유지"
    trade = store.list_trades()[0]
    assert trade["status"] == "failed" and trade["reason_type"] == "stop_loss"


def test_price_fetch_failure_does_not_crash(wd, store, monkeypatch):
    def boom(markets):
        raise RuntimeError("업비트 장애")

    monkeypatch.setattr(wd_mod, "get_prices", boom)
    assert wd.run_once() == [], "감시 잡은 어떤 예외에도 죽으면 안 된다"


def test_no_positions_is_noop(store, broker, monkeypatch):
    monkeypatch.setattr(wd_mod, "get_prices", lambda markets: {})
    assert Watchdog(store=store, broker=broker).run_once() == []


def test_records_last_run_timestamp(wd, store, broker):
    set_price(broker, ENTRY)
    wd.run_once()
    assert store.get_state(wd_mod.WATCHDOG_LAST_RUN_KEY) is not None, (
        "감시 잡이 죽었는지 대시보드에서 확인할 수 있어야 한다"
    )
