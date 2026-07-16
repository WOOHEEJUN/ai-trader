"""지표 계산 검증. 계산이 틀리면 판단 전체가 틀어지므로 손으로 검산 가능한 값으로 확인한다."""
from __future__ import annotations

import pytest

from agent.indicators import (Indicators, adx, atr, bollinger, compute, ema, ema_series,
                              macd, rsi, sma)


def candle(close, high=None, low=None, volume=100.0) -> dict:
    return {
        "trade_price": close,
        "high_price": high if high is not None else close * 1.001,
        "low_price": low if low is not None else close * 0.999,
        "candle_acc_trade_volume": volume,
    }


# --------------------------------------------------------------- SMA/EMA

def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2], 5) is None, "데이터가 모자라면 None"


def test_ema_hand_checked():
    """[1..10], period 3 → 초기값 SMA(1,2,3)=2, k=0.5로 손 검산하면 9.0."""
    assert ema(list(range(1, 11)), 3) == pytest.approx(9.0)


def test_ema_of_constant_is_constant():
    assert ema([7.0] * 30, 10) == pytest.approx(7.0)


def test_ema_series_length():
    assert len(ema_series(list(range(1, 11)), 3)) == 8  # 10 - 3 + 1


# -------------------------------------------------------------------- RSI

def test_rsi_all_gains_is_100():
    assert rsi([float(i) for i in range(1, 40)]) == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    assert rsi([float(i) for i in range(40, 1, -1)]) == pytest.approx(0.0)


def test_rsi_flat_market():
    """변화가 없으면 상승분/하락분이 모두 0 → 100으로 정의(0 나눗셈 방지)."""
    assert rsi([10.0] * 30) == 100.0


def test_rsi_in_range():
    closes = [10, 11, 10.5, 11.5, 11, 12, 11.8, 12.5, 12, 13,
              12.7, 13.5, 13, 14, 13.6, 14.5, 14, 15, 14.5, 15.5]
    value = rsi(closes)
    assert value is not None and 50 < value < 100, "상승 우위 구간이면 50 위"


def test_rsi_needs_enough_data():
    assert rsi([1, 2, 3]) is None


# ------------------------------------------------------------------ MACD

def test_macd_uptrend_is_positive():
    line, signal, hist = macd([float(i) for i in range(1, 60)])
    assert line > 0, "꾸준한 상승에선 단기 EMA가 장기 EMA 위"
    assert signal is not None and hist is not None


def test_macd_downtrend_is_negative():
    line, _, _ = macd([float(i) for i in range(60, 1, -1)])
    assert line < 0


def test_macd_flat_is_zero():
    line, signal, hist = macd([10.0] * 60)
    assert line == pytest.approx(0.0, abs=1e-9)
    assert hist == pytest.approx(0.0, abs=1e-9)


def test_macd_needs_enough_data():
    assert macd([1.0] * 10) == (None, None, None)


# ------------------------------------------------------------ 볼린저밴드

def test_bollinger_of_constant_has_zero_width():
    upper, mid, lower = bollinger([5.0] * 20)
    assert (upper, mid, lower) == (5.0, 5.0, 5.0)


def test_bollinger_band_ordering():
    closes = [10, 12, 11, 13, 12, 14, 13, 15, 14, 16,
              15, 17, 16, 18, 17, 19, 18, 20, 19, 21]
    upper, mid, lower = bollinger(closes)
    assert lower < mid < upper


def test_bollinger_needs_enough_data():
    assert bollinger([1.0] * 5) == (None, None, None)


# ------------------------------------------------------------------- ATR

def test_atr_constant_range():
    """고가-저가가 항상 2이고 종가가 일정하면 ATR은 정확히 2."""
    n = 30
    assert atr([11.0] * n, [9.0] * n, [10.0] * n, 14) == pytest.approx(2.0)


def test_atr_is_positive_and_scales():
    n = 30
    narrow = atr([10.5] * n, [9.5] * n, [10.0] * n, 14)
    wide = atr([12.0] * n, [8.0] * n, [10.0] * n, 14)
    assert wide > narrow > 0, "진폭이 크면 ATR도 커야 한다"


def test_atr_needs_enough_data():
    assert atr([1.0] * 5, [1.0] * 5, [1.0] * 5, 14) is None


# ------------------------------------------------------------------- ADX

def test_adx_flat_market_is_zero():
    """움직임이 없으면 방향성 지표도 0 — 횡보로 분류되어야 한다."""
    n = 40
    value, pdi, mdi = adx([11.0] * n, [9.0] * n, [10.0] * n, 14)
    assert value == pytest.approx(0.0)


def test_adx_strong_uptrend():
    n = 40
    closes = [10.0 + i for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    value, pdi, mdi = adx(highs, lows, closes, 14)
    assert value > 25, "꾸준한 상승은 추세장(ADX>25)"
    assert pdi > mdi, "상승이므로 +DI가 우세"


def test_adx_strong_downtrend():
    n = 40
    closes = [50.0 - i for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    value, pdi, mdi = adx(highs, lows, closes, 14)
    assert value > 25
    assert mdi > pdi, "하락이므로 -DI가 우세"


def test_adx_needs_enough_data():
    assert adx([1.0] * 10, [1.0] * 10, [1.0] * 10, 14) == (None, None, None)


# --------------------------------------------------------- 파생 해석값

def test_atr_pct():
    ind = Indicators("KRW-BTC", price=100.0, change_24h=0.0, volume_24h_krw=0.0, atr14=3.0)
    assert ind.atr_pct == pytest.approx(0.03)


def test_bb_position():
    ind = Indicators("KRW-BTC", price=110.0, change_24h=0, volume_24h_krw=0,
                     bb_upper=120.0, bb_lower=100.0)
    assert ind.bb_position == pytest.approx(0.5)


def test_bb_position_outside_band():
    ind = Indicators("KRW-BTC", price=130.0, change_24h=0, volume_24h_krw=0,
                     bb_upper=120.0, bb_lower=100.0)
    assert ind.bb_position > 1.0, "밴드 상단 돌파는 1을 넘는다"


def test_ema_trend_labels():
    up = Indicators("X", price=110, change_24h=0, volume_24h_krw=0, ema20=105, ema50=100)
    assert up.ema_trend == "정배열"
    down = Indicators("X", price=90, change_24h=0, volume_24h_krw=0, ema20=95, ema50=100)
    assert down.ema_trend == "역배열"


def test_regime_classification():
    assert Indicators("X", 1, 0, 0, adx14=15).regime == "횡보"
    assert Indicators("X", 1, 0, 0, adx14=30, plus_di=30, minus_di=10).regime == "강한 상승"
    assert Indicators("X", 1, 0, 0, adx14=22, plus_di=25, minus_di=15).regime == "약한 상승"
    assert Indicators("X", 1, 0, 0, adx14=30, plus_di=10, minus_di=30).regime == "강한 하락"
    assert Indicators("X", 1, 0, 0).regime == "판단불가"


# ------------------------------------------------------------- compute()

def test_compute_from_candles():
    candles = [candle(10.0 + i, volume=100.0) for i in range(60)]
    ind = compute("KRW-BTC", price=69.0, change_24h=0.05, volume_24h_krw=1e10, candles=candles)

    assert ind.market == "KRW-BTC"
    assert ind.ema20 is not None and ind.ema50 is not None
    assert ind.rsi14 == pytest.approx(100.0), "단조 상승이므로 RSI 100"
    assert ind.macd_line > 0
    assert ind.adx14 is not None and ind.adx14 > 25
    assert ind.regime == "강한 상승"


def test_compute_tolerates_short_history():
    """캔들이 모자라도 죽지 않고 None으로 채운다."""
    ind = compute("KRW-BTC", 100.0, 0.0, 1e9, [candle(100.0) for _ in range(5)])
    assert ind.price == 100.0
    assert ind.adx14 is None and ind.macd_line is None
    assert ind.regime == "판단불가"


def test_compute_volume_ratio():
    candles = [candle(10.0, volume=100.0) for _ in range(20)] + [candle(10.0, volume=300.0)]
    ind = compute("KRW-BTC", 10.0, 0.0, 1e9, candles)
    assert ind.volume_ratio == pytest.approx(3.0), "직전 20봉 평균 대비 3배"
