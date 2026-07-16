"""주간 회고 — 통계 계산과 kill 시 회고 생략.

Claude 호출부는 API가 필요하므로 여기선 통계와 게이팅만 검증한다.
통계가 틀리면 Claude가 틀린 전제로 자기평가를 하게 되므로 회고 전체가 오염된다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agent.judge import Verdict
from agent.review import compute_stats, run_review
from state.store import now_kst

WEEK_AGO = (now_kst() - timedelta(days=7)).isoformat()


def verdict(**kw) -> Verdict:
    base = dict(success=True, killed=False, start_krw=100_000.0, end_krw=110_000.0,
                pnl_krw=10_000.0, pnl_pct=0.10, generation=1, level_before=0,
                level_after=1, week_start=WEEK_AGO)
    base.update(kw)
    return Verdict(**base)


def sell(store, market="KRW-BTC", pnl=1_000.0, reason="llm", text="테스트"):
    store.record_trade(side="sell", market=market, reason_type=reason, status="filled",
                       qty=1.0, price=100.0, amount_krw=100.0, fee_krw=0.05,
                       reason_text=text, realized_pnl_krw=pnl)


# ------------------------------------------------------------- 통계 계산

def test_win_rate_and_averages(store):
    sell(store, pnl=3_000.0)
    sell(store, pnl=1_000.0)
    sell(store, pnl=-2_000.0)
    sell(store, pnl=-500.0)

    s = compute_stats(store, WEEK_AGO)

    assert s.sells == 4
    assert s.wins == 2 and s.losses == 2
    assert s.win_rate == pytest.approx(0.5)
    assert s.avg_win == pytest.approx(2_000.0)
    assert s.avg_loss == pytest.approx(-1_250.0)
    assert s.max_loss == pytest.approx(-2_000.0)


def test_break_even_sell_counts_as_loss(store):
    """본전 매도는 수수료만큼 까먹은 것이므로 승리가 아니다."""
    sell(store, pnl=0.0)
    s = compute_stats(store, WEEK_AGO)
    assert s.wins == 0 and s.losses == 1


def test_best_and_worst_trades(store):
    sell(store, market="KRW-ETH", pnl=5_000.0, text="최고")
    sell(store, market="KRW-XRP", pnl=-3_000.0, text="최악")
    sell(store, market="KRW-BTC", pnl=100.0)

    s = compute_stats(store, WEEK_AGO)

    assert s.best["market"] == "KRW-ETH" and s.best["pnl"] == 5_000.0
    assert s.worst["market"] == "KRW-XRP" and s.worst["pnl"] == -3_000.0


def test_exit_reasons_are_counted(store):
    sell(store, reason="stop_loss", pnl=-700.0)
    sell(store, reason="stop_loss", pnl=-800.0)
    sell(store, reason="take_profit", pnl=1_500.0)
    sell(store, reason="trailing", pnl=900.0)
    sell(store, reason="circuit_breaker", pnl=-2_000.0)

    s = compute_stats(store, WEEK_AGO)

    assert (s.stop_losses, s.take_profits, s.trailing_exits, s.circuit_breakers) == (2, 1, 1, 1)


def test_buys_count_as_trades_but_not_as_wins(store):
    store.record_trade(side="buy", market="KRW-BTC", reason_type="llm", status="filled",
                       qty=1.0, price=100.0)
    sell(store, pnl=500.0)

    s = compute_stats(store, WEEK_AGO)

    assert s.total_trades == 2, "매수도 거래 횟수에는 들어간다"
    assert s.sells == 1, "승패는 매도(실현손익)에서만 나온다"


def test_rejected_orders_are_collected(store):
    store.record_trade(side="buy", market="KRW-DOGE", reason_type="llm", status="rejected",
                       reject_reason="유니버스 밖 종목")
    store.record_trade(side="sell", market="KRW-BTC", reason_type="stop_loss", status="failed",
                       reject_reason="주문 실패: 타임아웃")

    s = compute_stats(store, WEEK_AGO)

    assert len(s.rejected) == 2
    assert any("유니버스 밖" in r["reason"] for r in s.rejected)


def test_old_trades_are_excluded(store):
    """지난주 거래가 이번 주 통계에 섞이면 안 된다."""
    store._write(
        "INSERT INTO trades (ts, side, market, qty, price, reason_type, status, realized_pnl_krw)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ((now_kst() - timedelta(days=30)).isoformat(), "sell", "KRW-OLD", 1.0, 100.0,
         "llm", "filled", 9_999.0),
    )
    sell(store, pnl=100.0)

    s = compute_stats(store, WEEK_AGO)

    assert s.sells == 1 and s.best["pnl"] == 100.0


def test_no_trades_yields_empty_stats(store):
    s = compute_stats(store, WEEK_AGO)
    assert s.total_trades == 0
    assert s.win_rate is None and s.best is None and s.max_loss is None


def test_stats_text_renders_without_crashing(store):
    sell(store, pnl=1_000.0)
    text = compute_stats(store, WEEK_AGO).as_text(verdict())
    assert "총 수익률" in text and "승률" in text


def test_stats_text_handles_no_sells(store):
    """매도가 없으면 승률·최대손실이 None인데, 그 상태로도 렌더링돼야 한다."""
    store.record_trade(side="buy", market="KRW-BTC", reason_type="llm", status="filled")
    text = compute_stats(store, WEEK_AGO).as_text(verdict())
    assert "승률: -" in text


# ------------------------------------------------------------- 회고 게이팅

def test_killed_week_skips_review(store):
    """kill된 주에 회고를 남기면 새 세대가 '겪지 않은 실수'를 물려받아 백지가 아니게 된다."""
    sell(store, pnl=-5_000.0)

    result = run_review(verdict(success=False, killed=True), store)

    assert result is None
    assert store.read_journal().strip() == "", "회고가 기록되면 안 된다"


def test_no_trades_skips_review(store):
    assert run_review(verdict(), store) is None


def test_suspended_budget_skips_review(store):
    """예산이 소진됐으면 회고도 건너뛴다 — 평가 자체는 이미 규칙으로 끝나 있다."""
    from config import settings
    from tests.test_budget import spend

    sell(store, pnl=1_000.0)
    spend(store, settings.monthly_budget_usd)

    assert run_review(verdict(), store) is None
