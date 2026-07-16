"""예산 정지와 청산 감시가 서로 독립임을 검증한다.

이 실험에서 가장 위험한 실패 모드는 "API 예산이 떨어져서 손절도 같이 멈추는 것"이다.
그러면 포지션이 무방비로 방치된다. watchdog은 LLM을 호출하지 않으므로 그런 일이
없어야 하고, 이 파일이 그것을 강제한다.
"""
from __future__ import annotations

import pytest

from agent import watchdog as wd_mod
from agent.budget import BudgetManager, BudgetState
from agent.watchdog import Watchdog, is_circuit_breaker_active
from config import settings
from state.store import now_kst
from tests.test_budget import spend

ENTRY = 100.0
QTY = 1_000.0


@pytest.fixture
def suspended_setup(store, broker, monkeypatch):
    """예산을 완전히 소진시키고 10만원 포지션을 들려놓은 상태."""
    monkeypatch.setattr(wd_mod, "get_prices", lambda markets: {m: broker.prices[m] for m in markets})
    spend(store, settings.monthly_budget_usd)  # $10 전액 소진
    store.apply_buy("KRW-TEST", QTY, ENTRY, settings.stop_loss_pct)
    broker.holdings["KRW-TEST"] = QTY
    broker.cash = 0.0
    return Watchdog(store=store, broker=broker), BudgetManager(store)


def test_stop_loss_works_while_budget_suspended(suspended_setup, store, broker):
    wd, budget = suspended_setup

    # 판단은 정지 상태
    assert budget.status().state is BudgetState.SUSPENDED
    assert budget.can_call()[0] is False

    # 그런데 급락이 온다
    broker.prices["KRW-TEST"] = ENTRY * 0.90  # -10%
    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "stop_loss"
    assert store.get_position("KRW-TEST") is None, (
        "예산이 소진돼도 손절은 반드시 작동해야 한다 — 이게 무너지면 포지션이 방치된다"
    )
    assert broker.cash > 0, "청산 대금이 현금으로 회수되어야 한다"


def test_circuit_breaker_works_while_budget_suspended(suspended_setup, store, broker):
    wd, budget = suspended_setup
    assert budget.status().suspended

    midnight = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
    store.record_snapshot(100_000, 0.0, {}, ts=midnight.isoformat())
    broker.prices["KRW-TEST"] = ENTRY * 0.83  # 당일 -17%

    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "circuit_breaker"
    assert is_circuit_breaker_active(store)


def test_take_profit_works_while_budget_suspended(suspended_setup, store, broker):
    wd, budget = suspended_setup
    assert budget.status().suspended

    broker.prices["KRW-TEST"] = ENTRY * 1.20
    events = wd.run_once()

    assert len(events) == 1 and events[0].reason == "take_profit"
    assert store.get_position("KRW-TEST").take_profit_done


def test_watchdog_consumes_no_api_budget(suspended_setup, store, broker):
    """감시 잡은 어떤 경로로도 API 비용을 쓰지 않는다."""
    wd, budget = suspended_setup
    spent_before = budget.status().spent_usd
    calls_before = budget.status().call_count

    broker.prices["KRW-TEST"] = ENTRY * 0.90
    wd.run_once()

    after = budget.status()
    assert after.spent_usd == spent_before, "청산이 일어나도 API 비용은 0이어야 한다"
    assert after.call_count == calls_before, "감시 잡은 Claude를 호출하지 않는다"
