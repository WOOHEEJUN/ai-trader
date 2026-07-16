"""스크리너 — Claude를 부를지 말지 규칙으로 결정한다. **API 비용 0원.**

왜 필요한가: 단타를 하려면 1시간 단위로 시장을 봐야 하는데, 매시간 Claude를 부르면
월 $13~19로 예산($10)을 넘는다. 원금 10만원 기준으로 원금의 20%가 API 비용이 된다.

해법: 지표 계산은 공짜다. 1시간마다 전부 계산하되, **볼 게 있을 때만** Claude를 부른다.
어차피 답이 HOLD인 사이클에 돈을 쓰지 않는다는 뜻이고, 이건 "매매를 위한 매매를 하지
않는다"는 원칙과도 방향이 같다.

호출 조건:
  1. Claude가 예약한 시각(`next_cycle_at`) 도달 — 무조건 호출
  2. 신호 등장 (셋업 후보 / 보유 종목 경보) — 단, 최소 간격과 일일 상한 안에서

이러면 조용한 장에선 Claude가 정한 간격대로(하루 2~4회), 움직이는 장에선 최대 하루
12회까지 깨어난다. 포지션 관리(손절/익절/트레일링)는 1분 감시가 이미 자동 처리하므로
Claude가 그것 때문에 깨어날 필요는 없다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from agent import indicators as ind
from agent.budget import BudgetManager
from agent.indicators import Indicators
from config import settings, tier_for
from exchange.upbit_client import Broker, get_broker, get_candles, get_tickers, get_universe
from state.store import Store, get_store, now_kst

LAST_CALL_TS_KEY = "last_strategist_call_ts"
SIGNAL_WAKES_KEY = "signal_wakes"  # {"date": "YYYY-MM-DD", "count": n}
STATE_LEVEL_KEY = "permission_level"


@dataclass
class Signal:
    market: str
    kind: str  # setup_trend | setup_reversal | alert_overheat | alert_trend_break | alert_stop_near
    detail: str


@dataclass
class ScreenResult:
    should_call: bool
    reason: str
    indicators: list[Indicators] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)

    @property
    def setups(self) -> list[Signal]:
        return [s for s in self.signals if s.kind.startswith("setup_")]

    @property
    def alerts(self) -> list[Signal]:
        return [s for s in self.signals if s.kind.startswith("alert_")]


def collect(universe: list[str]) -> list[Indicators]:
    """유니버스 전 종목의 지표. 업비트 공개 API만 쓰므로 비용이 없다."""
    tickers = get_tickers(universe)
    rows: list[Indicators] = []
    for market in universe:
        t = tickers.get(market)
        if not t:
            continue
        price = float(t["trade_price"])
        change = float(t.get("signed_change_rate", 0.0))
        volume = float(t.get("acc_trade_price_24h", 0.0))
        try:
            candles = get_candles(market, unit=60, count=100)
            rows.append(ind.compute(market, price, change, volume, candles))
            time.sleep(0.1)  # 업비트 공개 API 레이트리밋 여유
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 전체를 막으면 안 된다
            logger.warning(f"[스크리너] {market} 지표 계산 실패(시세만 사용): {e}")
            rows.append(Indicators(market=market, price=price, change_24h=change,
                                   volume_24h_krw=volume))
    return rows


# --------------------------------------------------------------- 신호 규칙

def _setup_signal(i: Indicators) -> Optional[Signal]:
    """무포지션 종목의 진입 후보. 여러 지표가 같은 방향을 가리킬 때만 잡는다."""
    if i.macd_hist is None or i.rsi14 is None or i.adx14 is None:
        return None

    # 추세 추종: 정배열 + MACD 양(+) + RSI 중립대 + 추세 확인 + 거래량 동반
    if (
        i.ema20 is not None and i.ema50 is not None and i.ema20 > i.ema50
        and i.macd_hist > 0
        and settings.setup_rsi_min <= i.rsi14 <= settings.setup_rsi_max
        and i.adx14 >= settings.setup_min_adx
        and (i.volume_ratio or 0) >= settings.setup_min_volume_ratio
    ):
        return Signal(i.market, "setup_trend",
                      f"정배열 + MACD히스토그램 {i.macd_hist:+.4f} + RSI {i.rsi14:.1f} "
                      f"+ ADX {i.adx14:.1f} + 거래량 {i.volume_ratio:.1f}배")

    # 과매도 반등: RSI 과매도 + 볼린저 하단 이탈 + MACD 히스토그램 반등
    bb_pos = i.bb_position
    if (
        i.rsi14 <= settings.oversold_rsi
        and bb_pos is not None and bb_pos <= 0.1
        and i.macd_hist > 0
    ):
        return Signal(i.market, "setup_reversal",
                      f"RSI {i.rsi14:.1f} 과매도 + 볼린저 하단({bb_pos:.2f}) + "
                      f"MACD히스토그램 반등 {i.macd_hist:+.4f}")
    return None


def _position_alerts(i: Indicators, pnl_pct: float, stop_loss_pct: float) -> list[Signal]:
    """보유 종목의 상태 변화. 손절/익절 자체는 감시 엔진이 처리하므로 여기선 '판단이 필요한' 것만."""
    out: list[Signal] = []

    if i.rsi14 is not None and i.rsi14 >= settings.overbought_rsi:
        out.append(Signal(i.market, "alert_overheat",
                          f"RSI {i.rsi14:.1f} 과열 (평가손익 {pnl_pct:+.2%})"))

    if i.ema_trend and i.ema_trend.startswith("역배열") and i.macd_hist is not None and i.macd_hist < 0:
        out.append(Signal(i.market, "alert_trend_break",
                          f"추세 이탈: {i.ema_trend} + MACD히스토그램 {i.macd_hist:+.4f} "
                          f"(평가손익 {pnl_pct:+.2%})"))

    # 손절선까지 얼마 안 남았다 — 감시 엔진이 곧 자동 청산할 수 있으니 미리 판단 기회를 준다
    if stop_loss_pct < 0 and pnl_pct <= stop_loss_pct * settings.stop_proximity_ratio:
        out.append(Signal(i.market, "alert_stop_near",
                          f"손절선 근접: {pnl_pct:+.2%} (손절선 {stop_loss_pct:.1%})"))
    return out


# ------------------------------------------------------------------ 게이트

def _signal_wakes_today(store: Store) -> int:
    rec = store.get_state(SIGNAL_WAKES_KEY) or {}
    if rec.get("date") != now_kst().strftime("%Y-%m-%d"):
        return 0
    return int(rec.get("count", 0))


def record_signal_wake(store: Store) -> None:
    store.set_state(SIGNAL_WAKES_KEY,
                    {"date": now_kst().strftime("%Y-%m-%d"), "count": _signal_wakes_today(store) + 1})


def screen(store: Optional[Store] = None, broker: Optional[Broker] = None) -> ScreenResult:
    store = store or get_store()
    broker = broker or get_broker(store)
    now = now_kst()

    tier = tier_for(store.get_state(STATE_LEVEL_KEY, 0))
    rows = collect(get_universe(tier.universe_size))
    by_market = {i.market: i for i in rows}

    positions = {p.market: p for p in store.list_positions()}
    signals: list[Signal] = []
    for i in rows:
        pos = positions.get(i.market)
        if pos is None:
            sig = _setup_signal(i)
            if sig:
                signals.append(sig)
        else:
            signals.extend(_position_alerts(i, pos.pnl_pct(i.price), pos.stop_loss_pct))

    # 유니버스 밖 보유 종목(거래대금 순위에서 밀려난 경우)도 경보 대상이다
    for market, pos in positions.items():
        if market not in by_market:
            signals.append(Signal(market, "alert_trend_break",
                                  "유니버스 이탈 — 거래대금 상위권에서 밀려났다"))

    # 1) Claude가 예약한 시각 도달 → 무조건 호출
    next_at = store.get_state("next_cycle_at")
    if not next_at:
        return ScreenResult(True, "첫 사이클", rows, signals)
    try:
        if now >= datetime.fromisoformat(next_at):
            return ScreenResult(True, "예약된 판단 시각 도달", rows, signals)
    except ValueError:
        return ScreenResult(True, f"next_cycle_at 파싱 실패({next_at}) — 즉시 판단", rows, signals)

    # 2) 신호 없으면 호출하지 않는다 (비용 0원으로 넘어감)
    if not signals:
        return ScreenResult(False, "신호 없음 — 예약 시각까지 대기", rows, signals)

    # 3) 신호가 있어도 최소 간격과 일일 상한은 지킨다
    budget = BudgetManager(store).status()
    last_call = store.get_state(LAST_CALL_TS_KEY)
    if last_call:
        try:
            elapsed = (now - datetime.fromisoformat(last_call)).total_seconds() / 3600
            if elapsed < budget.min_interval_hours:
                return ScreenResult(
                    False,
                    f"신호 {len(signals)}건 있으나 최소 간격 미달 "
                    f"({elapsed:.1f}h < {budget.min_interval_hours}h)",
                    rows, signals,
                )
        except ValueError:
            pass

    wakes = _signal_wakes_today(store)
    if wakes >= settings.max_signal_wakes_per_day:
        return ScreenResult(
            False, f"신호 {len(signals)}건 있으나 일일 신호 호출 상한 소진 ({wakes}회)", rows, signals
        )

    kinds = ", ".join(sorted({s.kind for s in signals}))
    return ScreenResult(True, f"신호 {len(signals)}건 ({kinds})", rows, signals)
