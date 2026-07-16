"""업비트 Open API 래퍼 + DRY_RUN 모의 체결 브로커.

공개 시세 API(`/ticker`, `/candles`, `/market/all`)는 인증이 필요 없다. 따라서 dry-run은
업비트 키 없이 실제 시세로 돌아간다 — 키는 실거래 전환 시점에만 발급하면 된다.

브로커는 두 구현이 같은 인터페이스를 만족한다:
  - `UpbitBroker`  : 실주문 (JWT 서명 + REST)
  - `PaperBroker`  : 실제 시세 + 모의 체결 (수수료·슬리피지 반영, 주문 전송 없음)

호출부(executor / watchdog)는 둘을 구분하지 않는다.
"""
from __future__ import annotations

import hashlib
import time
import uuid as uuid_lib
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urlencode

import jwt
import requests
from loguru import logger

from config import settings
from state.store import Store, get_store

API = "https://api.upbit.com/v1"
TIMEOUT = 10
PAPER_CASH_KEY = "paper_cash_krw"


class UpbitError(RuntimeError):
    pass


@dataclass
class Fill:
    market: str
    side: str  # buy | sell
    qty: float
    price: float       # 평균 체결가
    amount_krw: float  # 체결 총액 (수수료 제외)
    fee_krw: float
    uuid: str = ""


@dataclass
class Balance:
    cash_krw: float
    holdings: dict[str, float]  # market -> qty


# --------------------------------------------------------------- 공개 시세

def _public_get(path: str, params: dict | None = None, retries: int = 3):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 — 네트워크/HTTP 모두 재시도 대상
            last = e
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
    raise UpbitError(f"공개 API 실패: {path} — {last}")


def get_all_krw_markets() -> list[dict]:
    markets = _public_get("/market/all", {"isDetails": "true"})
    return [m for m in markets if m["market"].startswith("KRW-")]


def get_tickers(markets: list[str]) -> dict[str, dict]:
    if not markets:
        return {}
    rows = _public_get("/ticker", {"markets": ",".join(markets)})
    return {r["market"]: r for r in rows}


def get_prices(markets: list[str]) -> dict[str, float]:
    return {m: float(r["trade_price"]) for m, r in get_tickers(markets).items()}


def get_price(market: str) -> float:
    prices = get_prices([market])
    if market not in prices:
        raise UpbitError(f"시세 조회 실패: {market}")
    return prices[market]


def get_universe(size: int) -> list[str]:
    """24h 거래대금 상위 N개 KRW 마켓. 가드레일 #7(거래 유니버스 제한).

    유의종목(market_warning)과 거래대금 하한 미달 코인은 제외한다 — 저유동성 잡코인의
    슬리피지·조작 위험을 구조적으로 회피하기 위함.
    """
    all_markets = get_all_krw_markets()
    safe = [m["market"] for m in all_markets if m.get("market_warning") != "CAUTION"]
    tickers = get_tickers(safe)
    ranked = sorted(
        tickers.values(), key=lambda t: float(t.get("acc_trade_price_24h", 0)), reverse=True
    )
    return [
        t["market"] for t in ranked
        if float(t.get("acc_trade_price_24h", 0)) >= settings.universe_min_volume_krw
    ][:size]


def get_candles(market: str, unit: int = 60, count: int = 100) -> list[dict]:
    """분봉. 최신순으로 반환되므로 시간순 정렬해서 돌려준다."""
    rows = _public_get(f"/candles/minutes/{unit}", {"market": market, "count": count})
    return list(reversed(rows))


# ------------------------------------------------------------------ 브로커

class Broker(Protocol):
    def get_balance(self) -> Balance: ...
    def buy_market(self, market: str, krw_amount: float) -> Fill: ...
    def sell_market(self, market: str, qty: float) -> Fill: ...


class UpbitBroker:
    """실주문. 키에 출금 권한이 없어야 한다 (README/plan.md의 전제)."""

    def __init__(self) -> None:
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise UpbitError("업비트 키가 없다. .env의 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY를 확인.")

    def _headers(self, params: dict | None = None) -> dict:
        payload = {"access_key": settings.upbit_access_key, "nonce": str(uuid_lib.uuid4())}
        if params:
            query = urlencode(params)
            h = hashlib.sha512()
            h.update(query.encode())
            payload["query_hash"] = h.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, settings.upbit_secret_key)
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, params: dict | None = None):
        r = requests.get(f"{API}{path}", params=params, headers=self._headers(params), timeout=TIMEOUT)
        if r.status_code >= 400:
            raise UpbitError(f"{path} {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, params: dict):
        r = requests.post(f"{API}{path}", json=params, headers=self._headers(params), timeout=TIMEOUT)
        if r.status_code >= 400:
            raise UpbitError(f"{path} {r.status_code}: {r.text}")
        return r.json()

    def get_balance(self) -> Balance:
        accounts = self._get("/accounts")
        cash = 0.0
        holdings: dict[str, float] = {}
        for a in accounts:
            currency = a["currency"]
            qty = float(a["balance"]) + float(a["locked"])
            if currency == "KRW":
                cash = qty
            elif qty > 0:
                holdings[f"KRW-{currency}"] = qty
        return Balance(cash_krw=cash, holdings=holdings)

    def _wait_fill(self, order_uuid: str, market: str, side: str, timeout_s: float = 15.0) -> Fill:
        """시장가 주문은 즉시 체결되지만 응답은 비동기다. 체결 내역이 채워질 때까지 폴링."""
        deadline = time.time() + timeout_s
        last: dict = {}
        while time.time() < deadline:
            last = self._get("/order", {"uuid": order_uuid})
            trades = last.get("trades") or []
            if last.get("state") in ("done", "cancel") and trades:
                volume = sum(float(t["volume"]) for t in trades)
                funds = sum(float(t["funds"]) for t in trades)
                fee = float(last.get("paid_fee") or 0)
                price = funds / volume if volume > 0 else 0.0
                return Fill(market, side, volume, price, funds, fee, order_uuid)
            time.sleep(0.4)
        raise UpbitError(f"체결 확인 타임아웃: {order_uuid} (마지막 상태={last.get('state')})")

    def buy_market(self, market: str, krw_amount: float) -> Fill:
        krw = float(int(krw_amount))  # 업비트는 KRW 주문금액에 소수점을 허용하지 않는다
        res = self._post("/orders", {"market": market, "side": "bid", "ord_type": "price", "price": str(krw)})
        return self._wait_fill(res["uuid"], market, "buy")

    def sell_market(self, market: str, qty: float) -> Fill:
        res = self._post("/orders", {"market": market, "side": "ask", "ord_type": "market", "volume": str(qty)})
        return self._wait_fill(res["uuid"], market, "sell")


class PaperBroker:
    """DRY_RUN. 실제 시세를 읽되 주문은 전송하지 않고 체결을 시뮬레이션한다.

    수수료(편도 0.05%)와 슬리피지를 반영하므로, 모의 성과가 실거래보다 낙관적으로
    부풀지 않는다. 현금은 runtime_state에, 보유 수량은 positions 테이블에 둔다.
    """

    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or get_store()
        if self.store.get_state(PAPER_CASH_KEY) is None:
            self.store.set_state(PAPER_CASH_KEY, float(settings.initial_capital_krw))

    @property
    def cash(self) -> float:
        return float(self.store.get_state(PAPER_CASH_KEY, settings.initial_capital_krw))

    @cash.setter
    def cash(self, value: float) -> None:
        self.store.set_state(PAPER_CASH_KEY, round(float(value), 4))

    def get_balance(self) -> Balance:
        holdings = {p.market: p.qty for p in self.store.list_positions() if p.qty > 0}
        return Balance(cash_krw=self.cash, holdings=holdings)

    def buy_market(self, market: str, krw_amount: float) -> Fill:
        krw = float(int(krw_amount))
        if krw > self.cash + 1e-6:
            raise UpbitError(f"모의 현금 부족: 요청 {krw:,.0f}원 > 보유 {self.cash:,.0f}원")
        price = get_price(market) * (1 + settings.slippage_pct)
        fee = krw * settings.upbit_fee_rate
        qty = (krw - fee) / price
        self.cash = self.cash - krw
        logger.info(f"[PAPER] 매수 {market} {krw:,.0f}원 → {qty:.8f} @ {price:,.2f}")
        return Fill(market, "buy", qty, price, krw - fee, fee, f"paper-{uuid_lib.uuid4()}")

    def sell_market(self, market: str, qty: float) -> Fill:
        price = get_price(market) * (1 - settings.slippage_pct)
        gross = qty * price
        fee = gross * settings.upbit_fee_rate
        self.cash = self.cash + (gross - fee)
        logger.info(f"[PAPER] 매도 {market} {qty:.8f} @ {price:,.2f} → {gross - fee:,.0f}원")
        return Fill(market, "sell", qty, price, gross, fee, f"paper-{uuid_lib.uuid4()}")


def get_broker(store: Optional[Store] = None) -> Broker:
    if settings.dry_run:
        return PaperBroker(store)
    return UpbitBroker()
