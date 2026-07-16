"""API 예산 관리 검증 — 상태 전환 경계와 정지 중 안전성."""
from __future__ import annotations

import pytest

from agent.budget import BudgetManager, BudgetState, cost_of
from config import settings
from state.store import now_kst


class Usage:
    """anthropic response.usage 형태의 더미."""

    def __init__(self, input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                 cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


def spend(store, usd: float, month: str | None = None) -> None:
    """비용을 직접 주입한다 (토큰 환산을 거치지 않는 경로)."""
    store._write(
        "INSERT INTO api_usage (ts, month, model, cost_usd) VALUES (?,?,?,?)",
        (now_kst().isoformat(), month or now_kst().strftime("%Y-%m"), settings.model, usd),
    )


# ------------------------------------------------------------- 비용 환산

def test_cost_calculation_uses_all_token_types():
    # Sonnet 5 정가: 입력 $3 / 출력 $15 / 캐시쓰기 $3.75 / 캐시읽기 $0.30 (1M당)
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_of(usage, "claude-sonnet-5") == pytest.approx(3.0 + 15.0 + 3.75 + 0.30)


def test_cost_accepts_dict_usage():
    assert cost_of({"input_tokens": 1_000_000}, "claude-sonnet-5") == pytest.approx(3.0)


def test_cost_of_realistic_cycle_is_small():
    """입력 6K / 출력 2K 한 사이클이 1센트 남짓이어야 월 $10 안에 들어온다."""
    cost = cost_of(Usage(input_tokens=6_000, output_tokens=2_000), "claude-sonnet-5")
    assert cost == pytest.approx(0.048, abs=0.001)


def test_unknown_model_falls_back_to_most_expensive():
    """모르는 모델은 과소평가하지 않는다 — 예산은 과대평가가 안전한 방향."""
    unknown = cost_of(Usage(output_tokens=1_000_000), "claude-future-9")
    opus = cost_of(Usage(output_tokens=1_000_000), "claude-opus-4-8")
    assert unknown == opus


def test_missing_usage_fields_default_to_zero():
    assert cost_of(Usage(), "claude-sonnet-5") == 0.0


# ------------------------------------------------------------- 상태 전환

def test_normal_state_below_throttle(store):
    spend(store, 5.0)  # 50%
    s = BudgetManager(store).status()
    assert s.state is BudgetState.NORMAL
    assert s.effort == settings.effort_normal
    assert s.min_interval_hours == settings.min_cycle_interval_hours
    assert not s.suspended


def test_throttled_at_80_percent(store):
    spend(store, 8.0)  # 정확히 80%
    s = BudgetManager(store).status()
    assert s.state is BudgetState.THROTTLED
    assert s.effort == settings.effort_throttled, "감속 시 effort를 낮춰야 한다"
    assert s.min_interval_hours == settings.throttled_min_cycle_interval_hours
    assert not s.suspended, "감속은 정지가 아니다"


def test_still_normal_just_below_throttle(store):
    spend(store, 7.99)
    assert BudgetManager(store).status().state is BudgetState.NORMAL


def test_suspended_at_100_percent(store):
    spend(store, 10.0)
    s = BudgetManager(store).status()
    assert s.state is BudgetState.SUSPENDED
    assert s.suspended
    assert s.remaining_usd == 0.0


def test_suspended_when_over_budget(store):
    spend(store, 12.5)
    s = BudgetManager(store).status()
    assert s.suspended
    assert s.remaining_usd == 0.0, "잔여 예산이 음수로 표시되면 안 된다"


# --------------------------------------------------------------- can_call

def test_can_call_when_normal(store):
    spend(store, 1.0)
    ok, reason = BudgetManager(store).can_call()
    assert ok and reason == ""


def test_can_call_blocked_when_suspended(store):
    spend(store, 10.0)
    ok, reason = BudgetManager(store).can_call()
    assert not ok
    assert "예산 소진" in reason
    assert "청산 감시는 계속 작동" in reason, "정지 시 안전장치 상태를 사유에 명시해야 한다"


def test_can_call_blocked_when_remaining_below_estimate(store):
    """상한을 넘기 전에 미리 막는다 — 초과 과금 방지."""
    for _ in range(100):
        spend(store, 0.0995)  # $9.95, 호출당 평균 ~$0.0995
    mgr = BudgetManager(store)
    assert not mgr.status().suspended, "아직 상한에 닿지 않았다"
    ok, reason = mgr.can_call()
    assert not ok and "호출 생략" in reason


def test_estimated_cost_uses_measured_average(store):
    for _ in range(4):
        spend(store, 0.20)
    assert BudgetManager(store).estimated_call_cost() == pytest.approx(0.20)


def test_estimated_cost_fallback_without_history(store):
    from agent.budget import FALLBACK_CALL_COST_USD

    assert BudgetManager(store).estimated_call_cost() == FALLBACK_CALL_COST_USD


# ------------------------------------------------------------ 월 경계/리셋

def test_previous_month_usage_is_excluded(store):
    spend(store, 50.0, month="2020-01")  # 과거 폭주분
    s = BudgetManager(store).status()
    assert s.spent_usd == 0.0
    assert s.state is BudgetState.NORMAL, "매월 1일 카운터가 리셋되어야 한다"


def test_resets_at_is_next_month_first_day(store):
    resets = BudgetManager(store).status().resets_at
    now = now_kst()
    assert resets.day == 1
    assert (resets.hour, resets.minute) == (0, 0)
    assert resets > now
    expected_month = 1 if now.month == 12 else now.month + 1
    assert resets.month == expected_month


def test_record_persists_measured_usage(store):
    mgr = BudgetManager(store)
    cost = mgr.record(Usage(input_tokens=6_000, output_tokens=2_000), cycle_id="c1")
    assert cost > 0
    assert mgr.status().spent_usd == pytest.approx(cost)
    assert mgr.status().call_count == 1
