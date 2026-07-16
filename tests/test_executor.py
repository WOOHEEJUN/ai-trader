"""가드레일 7종 검증.

거부(reject) / 조정(clamp) / 생략(skip) 세 가지 동작이 각각 의도대로 나는지 확인한다.
"""
from __future__ import annotations

import pytest

from agent import executor as ex_mod
from agent.executor import Executor
from agent.strategist import Allocation, Decision
from agent.watchdog import CIRCUIT_BREAKER_DATE_KEY
from config import settings
from state.store import now_kst

UNIVERSE = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
PRICES = {"KRW-BTC": 100.0, "KRW-ETH": 50.0, "KRW-XRP": 10.0}


def decide(*allocs, **kw) -> Decision:
    # confidence 기본값 100 — 대부분의 테스트는 정확한 주문 금액을 검증하므로
    # 확신도 스케일링이 끼어들지 않게 한다. 스케일링 자체는 아래 전용 테스트에서 본다.
    return Decision(
        market_regime=kw.get("market_regime", "횡보"),
        decision=kw.get("decision", "BUY"),
        confidence=kw.get("confidence", 100),
        reason=kw.get("reason", "테스트 판단"),
        target_allocations=[Allocation(**a) for a in allocs],
        risk_check="",
        memory_note="",
        next_check_hours=4,
    )


@pytest.fixture
def ex(store, broker, monkeypatch):
    monkeypatch.setattr(ex_mod, "get_universe", lambda n: list(UNIVERSE))
    monkeypatch.setattr(ex_mod, "get_prices", lambda markets: {m: PRICES[m] for m in markets})
    monkeypatch.setattr(ex_mod, "hours_until_judge", lambda: 100.0)  # 쿨다운 아님
    broker.prices.update(PRICES)
    broker.cash = 100_000.0
    return Executor(store=store, broker=broker)


def notes_of(result, kind: str) -> list:
    return [n for n in result.notes if n.kind == kind]


# ------------------------------------------- #7 거래 유니버스 제한 (거부)

def test_market_outside_universe_is_rejected(ex, broker):
    result = ex.execute(decide({"market": "KRW-DOGE", "weight": 0.3}), "c1")

    assert broker.buys == [], "유니버스 밖 종목은 주문이 나가면 안 된다"
    rejected = notes_of(result, "rejected")
    assert len(rejected) == 1 and "유니버스 밖" in rejected[0].detail


# ------------------------------------- #2 단일 코인 집중도 상한 (조정)

def test_concentration_above_limit_is_clamped_not_rejected(ex, broker):
    """70%를 거부하면 BTC가 0%가 된다 — 의도에서 더 멀어지므로 50%로 깎아서 실행한다."""
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.7}), "c1")

    clamps = notes_of(result, "clamped")
    assert any("단일 코인 상한" in n.detail for n in clamps)
    assert broker.buys, "거부가 아니라 조정이므로 주문은 나가야 한다"


def test_weight_at_limit_is_not_clamped(ex, broker):
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.5}), "c1")
    assert not any("단일 코인 상한" in n.detail for n in notes_of(result, "clamped"))


# ------------------------------------- #3 최소 변경 임계치 (생략)

def test_delta_below_threshold_is_skipped(ex, store, broker):
    # BTC를 이미 30% 들고 있는데 목표가 33% → 3%p 차이 → 주문 없음
    store.apply_buy("KRW-BTC", 300.0, 100.0, settings.stop_loss_pct)
    broker.holdings["KRW-BTC"] = 300.0
    broker.cash = 70_000.0

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.33}), "c1")

    assert broker.buys == [] and broker.sells == [], "5%p 미만 잔손질은 수수료만 먹는다"
    assert not result.traded


def test_delta_above_threshold_executes(ex, store, broker):
    store.apply_buy("KRW-BTC", 300.0, 100.0, settings.stop_loss_pct)
    broker.holdings["KRW-BTC"] = 300.0
    broker.cash = 70_000.0

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.40}), "c1")  # +10%p

    assert result.traded and broker.buys


# ------------------------------------- #6 1회 최대 주문 금액 (조정)

def test_order_amount_is_clamped_to_max_ratio(ex, broker):
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.5}), "c1")

    cap = settings.max_order_ratio * 100_000  # 30,000원
    assert broker.buys == [("KRW-BTC", cap)]
    assert any("1회 상한" in n.detail for n in notes_of(result, "clamped"))


# ------------------------------------- #1 평가 전 쿨다운 (거부)

def test_cooldown_rejects_buys_but_allows_sells(ex, store, broker, monkeypatch):
    monkeypatch.setattr(ex_mod, "hours_until_judge", lambda: 12.0)  # 평가 12시간 전
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 50_000.0

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.5}), "c1")

    assert broker.buys == [], "쿨다운 중 신규 매수는 막혀야 한다 (막판 몰빵 방지)"
    assert broker.sells == [("KRW-ETH", 1_000.0)], "매도는 리스크를 줄이므로 허용"
    assert any("쿨다운" in n.detail for n in notes_of(result, "rejected"))


def test_no_cooldown_outside_window(ex, broker, monkeypatch):
    monkeypatch.setattr(ex_mod, "hours_until_judge", lambda: 25.0)
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")
    assert broker.buys, "쿨다운 밖에서는 매수가 가능해야 한다"


# ------------------------------------- #5 서킷브레이커 (거부)

def test_circuit_breaker_blocks_all_new_orders(ex, store, broker):
    store.set_state(CIRCUIT_BREAKER_DATE_KEY, now_kst().strftime("%Y-%m-%d"))

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")

    assert broker.buys == [] and broker.sells == []
    assert any("서킷브레이커" in n.detail for n in notes_of(result, "rejected"))


# ------------------------------------- #4 일일 매매 횟수 상한 (거부)

def test_daily_trade_limit_rejects_excess_orders(ex, store, broker):
    for _ in range(6):  # 레벨 0의 한도는 6건
        store.record_trade(side="buy", market="KRW-BTC", reason_type="llm", status="filled")

    result = ex.execute(
        decide({"market": "KRW-BTC", "weight": 0.3}, {"market": "KRW-ETH", "weight": 0.3}), "c1"
    )

    assert broker.buys == [], "한도 소진 후에는 주문이 나가면 안 된다"
    assert len(notes_of(result, "rejected")) == 2
    assert all("일일 매매 한도" in n.detail for n in notes_of(result, "rejected"))


def test_rule_liquidations_do_not_consume_daily_budget(ex, store, broker):
    """손절/익절은 Claude의 판단이 아니므로 일일 매매 예산을 깎지 않는다."""
    for _ in range(6):
        store.record_trade(side="sell", market="KRW-BTC", reason_type="stop_loss", status="filled")

    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")

    assert broker.buys, "규칙 청산이 판단 예산을 잡아먹으면 안 된다"


def test_partial_budget_prioritizes_larger_moves(ex, store, broker):
    for _ in range(5):  # 6건 중 5건 소진 → 1건만 가능
        store.record_trade(side="buy", market="KRW-BTC", reason_type="llm", status="filled")

    result = ex.execute(
        decide({"market": "KRW-BTC", "weight": 0.40}, {"market": "KRW-ETH", "weight": 0.08}), "c1"
    )

    assert len(broker.buys) == 1
    assert broker.buys[0][0] == "KRW-BTC", "잔여 예산은 변화폭이 큰 주문에 먼저 쓴다"
    assert any(n.market == "KRW-ETH" for n in notes_of(result, "rejected"))


# ------------------------------------------------ 비중 합 / 손절선 조정

def test_weights_over_100_percent_are_scaled_down(ex, broker):
    result = ex.execute(
        decide({"market": "KRW-BTC", "weight": 0.5}, {"market": "KRW-ETH", "weight": 0.5},
               {"market": "KRW-XRP", "weight": 0.5}), "c1"
    )
    assert any("비례 축소" in n.detail for n in notes_of(result, "clamped"))
    total_bought = sum(krw for _, krw in broker.buys)
    assert total_bought <= 100_000, "가진 돈보다 많이 살 수는 없다"


def test_stop_loss_clamped_to_allowed_range(ex, store, broker):
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3, "stop_loss_pct": -0.30}), "c1")

    assert any("손절선" in n.detail for n in notes_of(result, "clamped"))
    pos = store.get_position("KRW-BTC")
    assert pos.stop_loss_pct == settings.stop_loss_floor_pct, "-30%는 -10%까지만 허용"


def test_custom_stop_loss_within_range_is_applied(ex, store, broker):
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3, "stop_loss_pct": -0.05}), "c1")
    assert store.get_position("KRW-BTC").stop_loss_pct == pytest.approx(-0.05)


def test_default_stop_loss_when_unspecified(ex, store, broker):
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")
    assert store.get_position("KRW-BTC").stop_loss_pct == pytest.approx(settings.stop_loss_pct)


def test_stop_loss_updated_without_new_buy(ex, store, broker):
    """매수 없이 손절선만 조정하는 경우 (비중 변화가 임계치 미만)."""
    store.apply_buy("KRW-BTC", 300.0, 100.0, settings.stop_loss_pct)
    broker.holdings["KRW-BTC"] = 300.0
    broker.cash = 70_000.0

    ex.execute(decide({"market": "KRW-BTC", "weight": 0.31, "stop_loss_pct": -0.04}), "c1")

    assert broker.buys == [], "비중 변화가 임계치 미만이므로 주문은 없다"
    assert store.get_position("KRW-BTC").stop_loss_pct == pytest.approx(-0.04), "손절선은 갱신되어야 한다"


# ------------------------------------------------------- 매도/매수 순서

def test_dropped_coin_is_fully_sold(ex, store, broker):
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 50_000.0

    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")  # ETH가 목표에서 빠짐

    assert broker.sells == [("KRW-ETH", 1_000.0)], "목표에 없는 코인은 전량 매도"
    assert store.get_position("KRW-ETH") is None


def test_sells_execute_before_buys(ex, store, broker):
    """매도로 현금을 확보한 뒤 매수해야 한다 — 순서가 뒤집히면 현금 부족으로 실패한다."""
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 0.0  # 현금이 아예 없다

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")

    assert broker.sells == [("KRW-ETH", 1_000.0)]
    assert broker.buys, "매도 대금으로 매수가 가능해야 한다"
    assert result.traded


def test_buy_clamped_to_available_cash(ex, store, broker):
    """매도가 실패해 예상한 현금이 안 들어와도, 있는 현금만큼만 사고 죽지 않아야 한다.

    보유 코인 없이는 총자산=현금이라 이 클램프가 발동할 수 없다. 실제로 의미를 갖는
    상황은 "코인은 있는데 현금이 적고, 현금을 만들어줄 매도가 실패한" 경우다.
    """
    store.apply_buy("KRW-ETH", 1_840.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_840.0  # 92,000원어치
    broker.cash = 8_000.0                 # 총자산 100,000원, 현금은 8,000원뿐
    broker.fail_on.add("KRW-ETH")         # 현금을 만들어줄 매도가 실패한다

    ex.execute(decide({"market": "KRW-ETH", "weight": 0.6}, {"market": "KRW-BTC", "weight": 0.3}), "c1")

    # BTC 목표 30,000원이지만 매도 실패로 현금은 8,000원뿐
    assert broker.buys == [("KRW-BTC", 8_000.0)], "가진 현금 이상으로 주문할 수 없다"


def test_order_below_min_krw_is_skipped(ex, broker):
    broker.cash = 3_000.0  # 최소주문금액 5,000원 미만
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")
    assert broker.buys == []
    assert not result.traded


# --------------------------------------------------------------- 견고성

def test_order_failure_is_recorded_and_others_continue(ex, store, broker):
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 50_000.0
    broker.fail_on.add("KRW-ETH")  # ETH 매도가 실패한다

    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}), "c1")

    assert any("주문 실패" in n.detail for n in notes_of(result, "rejected"))
    assert broker.buys, "한 종목 실패가 나머지를 막으면 안 된다"
    failed = [t for t in store.list_trades() if t["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["market"] == "KRW-ETH"


def test_empty_allocations_liquidates_everything(ex, store, broker):
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 50_000.0

    ex.execute(decide(), "c1")  # 전량 현금화

    assert broker.sells == [("KRW-ETH", 1_000.0)]
    assert store.get_position("KRW-ETH") is None


def test_reason_is_stored_with_trade(ex, store, broker):
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, reason="RSI 과매도 진입"), "c1")
    trade = store.list_trades()[0]
    assert trade["reason_text"] == "RSI 과매도 진입", "대시보드에서 '왜 샀는지'를 봐야 한다"
    assert trade["cycle_id"] == "c1"


# --------------------------------------------------------- confidence 게이팅

def test_low_confidence_blocks_buys(ex, broker):
    """확신도 60 미만은 '불확실' 구간 — 신규 매수를 아예 막는다."""
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, confidence=55), "c1")

    assert broker.buys == []
    assert any("확신도 55" in n.detail for n in notes_of(result, "rejected"))


def test_low_confidence_still_allows_sells(ex, store, broker):
    """확신이 없어도 리스크를 줄이는 매도는 허용한다 — 막으면 손실을 방치하게 된다."""
    store.apply_buy("KRW-ETH", 1_000.0, 50.0, settings.stop_loss_pct)
    broker.holdings["KRW-ETH"] = 1_000.0
    broker.cash = 50_000.0

    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, confidence=40), "c1")

    assert broker.sells == [("KRW-ETH", 1_000.0)], "확신도와 무관하게 매도는 나가야 한다"
    assert broker.buys == []


def test_confidence_scales_order_size(ex, broker):
    result = ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, confidence=80), "c1")

    # 목표 30% × 확신도 80% = 24,000원
    assert broker.buys == [("KRW-BTC", 24_000.0)]
    assert any("확신도 80" in n.detail for n in notes_of(result, "clamped"))


def test_full_confidence_does_not_shrink_order(ex, broker):
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, confidence=100), "c1")
    assert broker.buys == [("KRW-BTC", 30_000.0)]


def test_confidence_boundary_at_60(ex, broker):
    ex.execute(decide({"market": "KRW-BTC", "weight": 0.3}, confidence=60), "c1")
    assert broker.buys == [("KRW-BTC", 18_000.0)], "60은 허용 구간 (30% × 60% = 18,000원)"


def test_low_confidence_does_not_force_selling(ex, store, broker):
    """확신도로 목표 비중 자체를 깎으면 보유분을 강제 매도하게 된다 — 그러면 안 된다."""
    store.apply_buy("KRW-BTC", 300.0, 100.0, settings.stop_loss_pct)
    broker.holdings["KRW-BTC"] = 300.0
    broker.cash = 70_000.0

    ex.execute(decide({"market": "KRW-BTC", "weight": 0.30}, confidence=50), "c1")

    assert broker.sells == [], "확신도가 낮다고 들고 있던 걸 팔라는 뜻은 아니다"
