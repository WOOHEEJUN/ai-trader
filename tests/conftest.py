from __future__ import annotations

import uuid

import pytest

from exchange.upbit_client import Balance, Fill
from state.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    from state import store as store_mod

    monkeypatch.setattr(store_mod, "STRATEGY_MEMORY_PATH", tmp_path / "strategy_memory.md")
    return Store(db_path=tmp_path / "test.db")


class FakeBroker:
    """가격을 테스트가 직접 주입하는 브로커. 수수료만 반영하고 슬리피지는 없다."""

    def __init__(self, cash: float = 100_000.0, holdings: dict[str, float] | None = None):
        self.cash = cash
        self.holdings = dict(holdings or {})
        self.prices: dict[str, float] = {}
        self.sells: list[tuple[str, float]] = []
        self.buys: list[tuple[str, float]] = []
        self.fail_on: set[str] = set()  # 이 마켓의 매도는 예외를 던진다

    def get_balance(self) -> Balance:
        return Balance(cash_krw=self.cash, holdings=dict(self.holdings))

    def buy_market(self, market: str, krw_amount: float) -> Fill:
        price = self.prices[market]
        fee = krw_amount * 0.0005
        qty = (krw_amount - fee) / price
        self.cash -= krw_amount
        self.holdings[market] = self.holdings.get(market, 0.0) + qty
        self.buys.append((market, krw_amount))
        return Fill(market, "buy", qty, price, krw_amount - fee, fee, str(uuid.uuid4()))

    def sell_market(self, market: str, qty: float) -> Fill:
        if market in self.fail_on:
            raise RuntimeError("모의 주문 실패")
        price = self.prices[market]
        gross = qty * price
        fee = gross * 0.0005
        self.cash += gross - fee
        remaining = self.holdings.get(market, 0.0) - qty
        if remaining <= 1e-9:
            self.holdings.pop(market, None)
        else:
            self.holdings[market] = remaining
        self.sells.append((market, qty))
        return Fill(market, "sell", qty, price, gross, fee, str(uuid.uuid4()))


@pytest.fixture
def broker():
    return FakeBroker()
