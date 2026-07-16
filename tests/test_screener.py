"""스크리너 게이팅 — 언제 Claude를 부르고 언제 안 부르는가.

이 판정이 곧 API 비용이다. 신호 없이 부르면 돈이 새고, 신호 있는데 안 부르면 기회를 놓친다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agent import screener as sc
from agent.indicators import Indicators
from agent.screener import LAST_CALL_TS_KEY, SIGNAL_WAKES_KEY, screen
from config import settings
from state.store import now_kst

MARKET = "KRW-BTC"


def make(market=MARKET, price=100.0, **kw) -> Indicators:
    """기본값은 '아무 신호도 안 나는 중립 상태'.

    역배열로 잡으면 안 된다 — 보유 종목에겐 역배열 자체가 추세 이탈 경보라 중립이 아니게 된다.
    그래서 정배열이되 MACD가 음수(셋업 아님)이고 ADX가 낮은(횡보) 상태를 중립으로 쓴다.
    """
    base = dict(
        ema20=100.5, ema50=100.0,  # 정배열 → 추세 이탈 경보 안 뜸
        rsi14=50.0, macd_line=0.0, macd_signal=0.0, macd_hist=-0.01,  # 음수 → 셋업 아님
        bb_upper=110.0, bb_mid=100.0, bb_lower=90.0,
        atr14=1.0, adx14=15.0, plus_di=20.0, minus_di=20.0,  # ADX 낮음 → 횡보
        volume_ratio=1.0,
    )
    base.update(kw)
    return Indicators(market=market, price=price, change_24h=0.0,
                      volume_24h_krw=1e11, **base)


def trend_setup(**kw) -> Indicators:
    """추세 추종 셋업 조건을 모두 만족하는 상태."""
    base = dict(ema20=101.0, ema50=99.0, macd_hist=0.5, rsi14=55.0,
                adx14=25.0, volume_ratio=1.5)
    base.update(kw)  # 개별 조건을 깨뜨리는 테스트가 덮어쓴다
    return make(**base)


@pytest.fixture
def sched(store, broker, monkeypatch):
    """예약 시각이 아직 멀리 있는 상태 — 신호가 있어야만 깨어난다."""
    monkeypatch.setattr(sc, "get_universe", lambda n: [MARKET])
    store.set_state("next_cycle_at", (now_kst() + timedelta(hours=6)).isoformat())
    return store, broker


def run(store, broker, rows, monkeypatch):
    monkeypatch.setattr(sc, "collect", lambda universe: rows)
    return screen(store, broker)


# ------------------------------------------------------- 예약 시각 게이트

def test_first_cycle_always_calls(store, broker, monkeypatch):
    monkeypatch.setattr(sc, "get_universe", lambda n: [MARKET])
    r = run(store, broker, [make()], monkeypatch)
    assert r.should_call and "첫 사이클" in r.reason


def test_scheduled_time_forces_call_even_without_signal(store, broker, monkeypatch):
    monkeypatch.setattr(sc, "get_universe", lambda n: [MARKET])
    store.set_state("next_cycle_at", (now_kst() - timedelta(minutes=1)).isoformat())

    r = run(store, broker, [make()], monkeypatch)

    assert r.should_call, "Claude가 정한 주기는 신호와 무관하게 지켜야 한다"
    assert "예약된 판단 시각" in r.reason


def test_no_signal_before_scheduled_time_skips_call(sched, monkeypatch):
    store, broker = sched
    r = run(store, broker, [make()], monkeypatch)

    assert not r.should_call, "볼 게 없으면 돈을 쓰지 않는다"
    assert "신호 없음" in r.reason
    assert r.indicators, "호출은 안 해도 지표는 계산해둔다 (무료)"


# ------------------------------------------------------------- 셋업 신호

def test_trend_setup_wakes_claude(sched, monkeypatch):
    store, broker = sched
    r = run(store, broker, [trend_setup()], monkeypatch)

    assert r.should_call
    assert len(r.setups) == 1 and r.setups[0].kind == "setup_trend"


@pytest.mark.parametrize("override,why", [
    ({"ema20": 98.0, "ema50": 99.0}, "역배열이면 추세 셋업이 아니다"),
    ({"macd_hist": -0.1}, "MACD가 음수면 셋업이 아니다"),
    ({"rsi14": 75.0}, "RSI 과열이면 진입하지 않는다"),
    ({"rsi14": 35.0}, "RSI가 너무 눌려도 추세 셋업은 아니다"),
    ({"adx14": 15.0}, "ADX가 낮으면 추세가 아니라 횡보다"),
    ({"volume_ratio": 1.0}, "거래량 동반이 없으면 셋업이 아니다"),
])
def test_trend_setup_requires_every_condition(sched, monkeypatch, override, why):
    """하나의 지표만으로 판단하지 않는다 — 전부 같은 방향이어야 신호다."""
    store, broker = sched
    r = run(store, broker, [trend_setup(**override)], monkeypatch)
    assert not r.should_call, why


def test_oversold_reversal_setup(sched, monkeypatch):
    store, broker = sched
    r = run(store, broker, [make(rsi14=25.0, price=91.0, macd_hist=0.2)], monkeypatch)

    assert r.should_call
    assert r.setups[0].kind == "setup_reversal"


def test_oversold_without_macd_rebound_is_not_a_setup(sched, monkeypatch):
    """떨어지는 칼날을 잡지 않는다 — 반등 확인이 있어야 한다."""
    store, broker = sched
    r = run(store, broker, [make(rsi14=25.0, price=91.0, macd_hist=-0.2)], monkeypatch)
    assert not r.should_call


def test_indicators_missing_produce_no_signal(sched, monkeypatch):
    """캔들 조회 실패로 지표가 비어도 죽지 않고 신호 없음으로 처리한다."""
    store, broker = sched
    bare = Indicators(market=MARKET, price=100.0, change_24h=0.0, volume_24h_krw=1e11)
    r = run(store, broker, [bare], monkeypatch)
    assert not r.should_call


# --------------------------------------------------------- 보유 종목 경보

def test_held_position_gets_alerts_not_setups(sched, store, monkeypatch):
    store, broker = sched
    store.apply_buy(MARKET, 10.0, 100.0, settings.stop_loss_pct)

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert r.setups == [], "이미 들고 있는 종목은 진입 후보가 아니다"


def test_overheat_alert_wakes_claude(sched, store, monkeypatch):
    store, broker = sched
    store.apply_buy(MARKET, 10.0, 100.0, settings.stop_loss_pct)

    r = run(store, broker, [make(rsi14=80.0, price=120.0)], monkeypatch)

    assert r.should_call
    assert any(s.kind == "alert_overheat" for s in r.alerts)


def test_trend_break_alert_wakes_claude(sched, store, monkeypatch):
    store, broker = sched
    store.apply_buy(MARKET, 10.0, 100.0, settings.stop_loss_pct)

    r = run(store, broker, [make(ema20=95.0, ema50=100.0, price=90.0, macd_hist=-0.5)], monkeypatch)

    assert r.should_call
    assert any(s.kind == "alert_trend_break" for s in r.alerts)


def test_stop_proximity_alert_wakes_claude(sched, store, monkeypatch):
    """손절선에 다가가면 자동 청산 전에 Claude에게 판단 기회를 준다."""
    store, broker = sched
    store.apply_buy(MARKET, 10.0, 100.0, -0.07)

    r = run(store, broker, [make(price=95.0)], monkeypatch)  # -5% → 손절선 -7%의 60% 초과

    assert r.should_call
    assert any(s.kind == "alert_stop_near" for s in r.alerts)


def test_no_stop_alert_when_comfortable(sched, store, monkeypatch):
    store, broker = sched
    store.apply_buy(MARKET, 10.0, 100.0, -0.07)

    r = run(store, broker, [make(price=99.0)], monkeypatch)  # -1% → 여유

    assert not r.should_call


def test_universe_dropout_alerts(sched, store, monkeypatch):
    """보유 종목이 거래대금 순위에서 밀려나면 유동성이 마른다는 뜻이라 알려야 한다."""
    store, broker = sched
    store.apply_buy("KRW-GONE", 10.0, 100.0, settings.stop_loss_pct)

    r = run(store, broker, [make()], monkeypatch)

    assert r.should_call
    assert any(s.market == "KRW-GONE" for s in r.alerts)


# ------------------------------------------------------------ 비용 브레이크

def test_min_interval_blocks_signal_wake(sched, store, monkeypatch):
    """신호가 있어도 방금 불렀으면 다시 부르지 않는다 — 연속 호출 방지."""
    store, broker = sched
    store.set_state(LAST_CALL_TS_KEY, (now_kst() - timedelta(minutes=20)).isoformat())

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert not r.should_call and "최소 간격 미달" in r.reason


def test_signal_wake_allowed_after_min_interval(sched, store, monkeypatch):
    store, broker = sched
    store.set_state(LAST_CALL_TS_KEY, (now_kst() - timedelta(hours=2)).isoformat())

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert r.should_call


def test_daily_signal_wake_cap(sched, store, monkeypatch):
    store, broker = sched
    store.set_state(LAST_CALL_TS_KEY, (now_kst() - timedelta(hours=2)).isoformat())
    store.set_state(SIGNAL_WAKES_KEY,
                    {"date": now_kst().strftime("%Y-%m-%d"),
                     "count": settings.max_signal_wakes_per_day})

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert not r.should_call and "일일 신호 호출 상한" in r.reason


def test_wake_counter_resets_daily(sched, store, monkeypatch):
    store, broker = sched
    store.set_state(LAST_CALL_TS_KEY, (now_kst() - timedelta(hours=2)).isoformat())
    store.set_state(SIGNAL_WAKES_KEY, {"date": "2020-01-01", "count": 99})

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert r.should_call, "어제 소진분이 오늘을 막으면 안 된다"


def test_throttled_budget_widens_min_interval(sched, store, monkeypatch):
    """예산 80% 초과 시 신호 호출 간격이 4시간으로 벌어진다."""
    from tests.test_budget import spend

    store, broker = sched
    spend(store, settings.monthly_budget_usd * 0.85)
    store.set_state(LAST_CALL_TS_KEY, (now_kst() - timedelta(hours=2)).isoformat())

    r = run(store, broker, [trend_setup()], monkeypatch)

    assert not r.should_call, "감속 중엔 2시간 전 호출이면 아직 이르다"
    assert "4h" in r.reason
