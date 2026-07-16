"""기술지표 계산.

전부 순수 함수다 — 네트워크 호출도, 상태도 없다. 따라서 계산 비용이 0원이고,
스크리너(`agent/screener.py`)가 매시간 돌려도 API 예산을 쓰지 않는다.

Claude에게 "여러 지표가 같은 방향을 가리키는지" 확인시키려면 재료가 충분해야 한다.
RSI 하나로는 판단 근거가 안 되므로 추세(EMA/MACD/ADX), 모멘텀(RSI), 변동성(BB/ATR),
거래량까지 함께 낸다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

Series = Sequence[float]


# ------------------------------------------------------------- 기본 계산

def sma(values: Series, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Series, period: int) -> list[float]:
    """지수이동평균 전체 시계열. 초기값은 첫 `period`개의 SMA."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Series, period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series else None


def _rma_series(values: Series, period: int) -> list[float]:
    """Wilder 평활(RMA). ATR/ADX가 쓴다. 초기값은 첫 `period`개의 단순평균."""
    if len(values) < period:
        return []
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append((out[-1] * (period - 1) + v) / period)
    return out


def rsi(closes: Series, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = _rma_series(gains, period)
    avg_loss = _rma_series(losses, period)
    if not avg_gain or not avg_loss:
        return None
    if avg_loss[-1] == 0:
        return 100.0
    rs = avg_gain[-1] / avg_loss[-1]
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: Series, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(MACD선, 시그널선, 히스토그램). 히스토그램 부호 전환이 추세 전환 신호."""
    if len(closes) < slow + signal:
        return None, None, None
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    # 두 EMA는 시작 시점이 다르므로 뒤에서 맞춘다.
    n = min(len(fast_s), len(slow_s))
    macd_line = [fast_s[-n + i] - slow_s[-n + i] for i in range(n)]
    signal_s = ema_series(macd_line, signal)
    if not signal_s:
        return macd_line[-1], None, None
    return macd_line[-1], signal_s[-1], macd_line[-1] - signal_s[-1]


def bollinger(closes: Series, period: int = 20, k: float = 2.0
              ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(상단, 중심, 하단)."""
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((v - mid) ** 2 for v in window) / period
    sd = var ** 0.5
    return mid + k * sd, mid, mid - k * sd


def atr(highs: Series, lows: Series, closes: Series, period: int = 14) -> Optional[float]:
    """평균 진폭. 손절 폭을 변동성에 맞출 때 쓴다."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    series = _rma_series(trs, period)
    return series[-1] if series else None


def adx(highs: Series, lows: Series, closes: Series, period: int = 14
        ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(ADX, +DI, -DI). ADX>25면 추세장, <20이면 횡보장으로 본다."""
    if len(closes) < period * 2 + 1:
        return None, None, None

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    tr_s = _rma_series(trs, period)
    plus_s = _rma_series(plus_dm, period)
    minus_s = _rma_series(minus_dm, period)
    if not tr_s or not plus_s or not minus_s:
        return None, None, None

    dx = []
    for tr_v, p_v, m_v in zip(tr_s, plus_s, minus_s):
        if tr_v == 0:
            dx.append(0.0)
            continue
        pdi = 100.0 * p_v / tr_v
        mdi = 100.0 * m_v / tr_v
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)

    adx_s = _rma_series(dx, period)
    if not adx_s:
        return None, None, None
    pdi_last = 100.0 * plus_s[-1] / tr_s[-1] if tr_s[-1] else None
    mdi_last = 100.0 * minus_s[-1] / tr_s[-1] if tr_s[-1] else None
    return adx_s[-1], pdi_last, mdi_last


# ---------------------------------------------------------------- 묶음

@dataclass
class Indicators:
    """한 종목의 지표 묶음. 값이 없으면(캔들 부족) None."""
    market: str
    price: float
    change_24h: float
    volume_24h_krw: float

    ema20: Optional[float] = None
    ema50: Optional[float] = None
    rsi14: Optional[float] = None
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_lower: Optional[float] = None
    atr14: Optional[float] = None
    adx14: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    volume_ratio: Optional[float] = None  # 최근 봉 거래량 / 20봉 평균

    # ---- 파생 해석값 (프롬프트에 넣기 좋은 형태) ----

    @property
    def atr_pct(self) -> Optional[float]:
        if self.atr14 is None or self.price <= 0:
            return None
        return self.atr14 / self.price

    @property
    def bb_position(self) -> Optional[float]:
        """볼린저 밴드 내 위치. 0=하단, 1=상단, 밴드 밖이면 0~1을 벗어난다."""
        if self.bb_upper is None or self.bb_lower is None:
            return None
        width = self.bb_upper - self.bb_lower
        if width <= 0:
            return None
        return (self.price - self.bb_lower) / width

    @property
    def ema_trend(self) -> Optional[str]:
        if self.ema20 is None or self.ema50 is None:
            return None
        if self.ema20 > self.ema50:
            return "정배열" if self.price > self.ema20 else "정배열(가격 EMA20 하회)"
        return "역배열" if self.price < self.ema20 else "역배열(가격 EMA20 상회)"

    @property
    def regime(self) -> str:
        """시장 국면 1차 분류. Claude가 재판단하지만 스크리너 게이팅에 쓴다."""
        if self.adx14 is None:
            return "판단불가"
        if self.adx14 < 20:
            return "횡보"
        if self.plus_di is None or self.minus_di is None:
            return "추세"
        strong = self.adx14 >= 25
        if self.plus_di > self.minus_di:
            return "강한 상승" if strong else "약한 상승"
        return "강한 하락" if strong else "약한 하락"


def compute(market: str, price: float, change_24h: float, volume_24h_krw: float,
            candles: list[dict]) -> Indicators:
    """업비트 캔들 리스트(시간순)로 지표를 계산한다."""
    closes = [float(c["trade_price"]) for c in candles]
    highs = [float(c["high_price"]) for c in candles]
    lows = [float(c["low_price"]) for c in candles]
    volumes = [float(c["candle_acc_trade_volume"]) for c in candles]

    macd_line, macd_sig, macd_hist = macd(closes)
    bb_u, bb_m, bb_l = bollinger(closes)
    adx14, pdi, mdi = adx(highs, lows, closes)

    vol_ratio = None
    if len(volumes) >= 21:
        avg = sum(volumes[-21:-1]) / 20
        vol_ratio = volumes[-1] / avg if avg > 0 else None

    return Indicators(
        market=market, price=price, change_24h=change_24h, volume_24h_krw=volume_24h_krw,
        ema20=ema(closes, 20), ema50=ema(closes, 50), rsi14=rsi(closes),
        macd_line=macd_line, macd_signal=macd_sig, macd_hist=macd_hist,
        bb_upper=bb_u, bb_mid=bb_m, bb_lower=bb_l,
        atr14=atr(highs, lows, closes), adx14=adx14, plus_di=pdi, minus_di=mdi,
        volume_ratio=vol_ratio,
    )
