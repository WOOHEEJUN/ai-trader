"""주간 평가 — 성공/실패 분기와 세대 관리."""
from __future__ import annotations

from datetime import timedelta

import pytest

from agent.judge import STATE_GENERATION_KEY, STATE_LEVEL_KEY, WEEK_START_KEY, run_judge
from config import MAX_LEVEL, settings, tier_for
from state.store import now_kst


@pytest.fixture
def week(store):
    """일주일 전 10만원으로 시작한 상태."""
    start = now_kst() - timedelta(days=7)
    store.set_state(WEEK_START_KEY, start.isoformat())
    store.record_snapshot(100_000, 100_000, {}, ts=start.isoformat())
    store.append_memory("지난주에 관찰한 패턴: BTC는 새벽에 변동성이 크다")
    return start


# ------------------------------------------------------------------- 성공

def test_success_preserves_memory_and_raises_level(week, store, broker):
    broker.cash = 110_000.0  # +10%

    v = run_judge(store, broker)

    assert v.success and not v.killed
    assert v.pnl_krw == pytest.approx(10_000)
    assert v.pnl_pct == pytest.approx(0.10)
    assert v.level_before == 0 and v.level_after == 1
    assert "BTC는 새벽에 변동성이 크다" in store.read_memory(), "성공하면 메모리가 보존된다"
    assert store.get_state(STATE_GENERATION_KEY, 1) == 1, "성공 시 세대는 그대로"


def test_success_expands_permissions(week, store, broker):
    broker.cash = 110_000.0
    base = tier_for(0)
    run_judge(store, broker)

    tier = tier_for(store.get_state(STATE_LEVEL_KEY))
    assert tier.max_daily_trades == base.max_daily_trades + 2, "레벨 1은 일일 매매 +2건"
    assert tier.universe_size == base.universe_size + 5, "레벨 1은 유니버스 +5개"
    assert tier.capital_limit_krw == base.capital_limit_krw + settings.capital_step_krw, (
        "레벨 1은 운용 한도가 capital_step_krw만큼 늘어야 한다"
    )


def test_level_is_capped_at_max(week, store, broker):
    store.set_state(STATE_LEVEL_KEY, MAX_LEVEL)
    broker.cash = 110_000.0

    v = run_judge(store, broker)

    assert v.level_after == MAX_LEVEL, "레벨은 상한을 넘지 않는다"


# ------------------------------------------------------------------- 실패

def test_failure_kills_memory_and_resets_level(week, store, broker):
    store.set_state(STATE_LEVEL_KEY, 2)
    broker.cash = 95_000.0  # -5%

    v = run_judge(store, broker)

    assert not v.success and v.killed
    assert v.level_before == 2 and v.level_after == 0, "권한은 기본값으로 복귀"
    assert "BTC는 새벽에 변동성이 크다" not in store.read_memory(), "실패하면 메모리가 초기화된다"


def test_failure_increments_generation(week, store, broker):
    broker.cash = 95_000.0
    v = run_judge(store, broker)
    assert v.generation == 2, "kill 될 때마다 세대가 올라간다"
    assert store.get_state(STATE_GENERATION_KEY) == 2


def test_kill_evaluation_records_the_generation_that_died(week, store, broker):
    """1세대를 죽인 평가는 1세대 기록이어야 한다 — 아니면 히스토리가 한 칸씩 밀린다."""
    broker.cash = 95_000.0
    run_judge(store, broker)

    row = store.list_evaluations()[0]
    assert row["generation"] == 1, "평가 대상은 죽은 세대(1)이지 새로 태어난 세대(2)가 아니다"
    assert store.get_state(STATE_GENERATION_KEY) == 2, "다음 세대 번호는 2"


def test_success_evaluation_records_current_generation(week, store, broker):
    broker.cash = 110_000.0
    run_judge(store, broker)
    assert store.list_evaluations()[0]["generation"] == 1


def test_kill_preserves_trade_log_and_snapshots(week, store, broker):
    """kill은 전략 메모리만 지운다. 거래 기록은 사용자가 역사를 볼 수 있어야 하므로 보존."""
    store.record_trade(side="buy", market="KRW-BTC", reason_type="llm", status="filled",
                       qty=1.0, price=100.0, reason_text="지난 세대의 판단")
    trades_before = len(store.list_trades())
    snaps_before = len(store.snapshots_since("2000-01-01"))
    broker.cash = 95_000.0

    run_judge(store, broker)

    assert len(store.list_trades()) == trades_before, "거래 로그는 절대 지우지 않는다"
    assert len(store.snapshots_since("2000-01-01")) == snaps_before, "스냅샷도 보존"


def test_break_even_counts_as_failure(week, store, broker):
    """순수익 > 0 이 성공 기준 — 본전은 실패다(수수료만큼 까먹은 것)."""
    broker.cash = 100_000.0

    v = run_judge(store, broker)

    assert not v.success and v.killed
    assert v.pnl_krw == 0


def test_tiny_profit_counts_as_success(week, store, broker):
    broker.cash = 100_001.0
    assert run_judge(store, broker).success


# --------------------------------------------------------- 기록 / 첫 주

def test_evaluation_is_recorded(week, store, broker):
    broker.cash = 112_500.0
    run_judge(store, broker)

    rows = store.list_evaluations()
    assert len(rows) == 1
    row = rows[0]
    assert row["start_krw"] == pytest.approx(100_000)
    assert row["end_krw"] == pytest.approx(112_500)
    assert row["success"] == 1 and row["killed"] == 0
    assert row["level_before"] == 0 and row["level_after"] == 1


def test_first_week_without_baseline_uses_initial_capital(store, broker):
    """가동 첫 주에는 기준 스냅샷이 없다 — 초기 자본을 기준으로 잡는다."""
    broker.cash = settings.initial_capital_krw + 5_000.0  # 초기 자본 대비 소폭 흑자

    v = run_judge(store, broker)

    assert v.start_krw == pytest.approx(settings.initial_capital_krw)
    assert v.success


def test_week_start_advances_after_judge(week, store, broker):
    broker.cash = 105_000.0
    before = store.get_state(WEEK_START_KEY)

    run_judge(store, broker)

    assert store.get_state(WEEK_START_KEY) != before, "다음 주 기준점이 갱신되어야 한다"


def test_consecutive_successes_accumulate(week, store, broker):
    broker.cash = 110_000.0
    assert run_judge(store, broker).level_after == 1

    store.record_snapshot(110_000, 110_000, {}, ts=store.get_state(WEEK_START_KEY))
    broker.cash = 120_000.0
    assert run_judge(store, broker).level_after == 2, "연속 성공 시 권한이 누적된다"
