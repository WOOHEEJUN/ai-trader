"""대시보드 라우트 + 수익률 계산.

대시보드는 읽기 전용이어야 한다 — 주문을 낼 수 있는 경로가 없다는 것도 여기서 강제한다.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from config import settings
from state.store import now_kst
from tests.conftest import FakeBroker


@pytest.fixture
def client(store, monkeypatch):
    from web import app as web_app

    broker = FakeBroker(cash=100_000.0)
    monkeypatch.setattr(web_app, "get_store", lambda: store)
    monkeypatch.setattr(web_app, "get_broker", lambda s=None: broker)
    monkeypatch.setattr(web_app, "compute_portfolio", lambda b, p=None: (100_000.0, 100_000.0, {}))
    return TestClient(web_app.app)


# ------------------------------------------------------------------ 라우트

@pytest.mark.parametrize("path", ["/", "/trades", "/brain", "/generations", "/watchdog"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "AI Trader" in r.text


@pytest.mark.parametrize("path", ["/api/summary", "/api/equity?period=1w", "/api/equity?period=all"])
def test_api_endpoints(client, path):
    assert client.get(path).status_code == 200


def test_static_assets_served(client):
    for path in ["/static/app.css", "/static/manifest.json", "/static/charts.js", "/static/icon.svg"]:
        assert client.get(path).status_code == 200, path


def test_manifest_is_valid_pwa(client):
    m = client.get("/static/manifest.json").json()
    assert m["display"] == "standalone", "홈 화면에서 앱처럼 뜨려면 standalone이어야 한다"
    assert m["start_url"] == "/"
    assert m["icons"]


def test_dashboard_is_read_only(client):
    """주문을 낼 수 있는 경로가 존재하면 안 된다."""
    from web.app import app

    methods = {m for route in app.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"쓰기 가능한 메서드가 노출됐다: {methods}"


# ------------------------------------------------------------- 수익률 계산

def test_returns_computed_against_snapshots(client, store):
    now = now_kst()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 스냅샷 잡이 매시 정각에 돌므로 당일 00:00 스냅샷이 존재한다
    store.record_snapshot(90_000, 90_000, {}, ts=midnight.isoformat())
    store.record_snapshot(80_000, 80_000, {}, ts=(now - timedelta(days=7)).isoformat())
    store.record_snapshot(50_000, 50_000, {}, ts=(now - timedelta(days=30)).isoformat())

    s = client.get("/api/summary").json()

    assert s["returns"]["today"] == pytest.approx(100_000 / 90_000 - 1)
    assert s["returns"]["week"] == pytest.approx(100_000 / 80_000 - 1)
    assert s["returns"]["month"] == pytest.approx(100_000 / 50_000 - 1)
    assert s["returns"]["all"] == pytest.approx(100_000 / settings.initial_capital_krw - 1)


def test_returns_are_null_without_snapshots(client):
    s = client.get("/api/summary").json()
    assert s["returns"]["today"] is None, "스냅샷이 없으면 0%가 아니라 '없음'이어야 한다"
    assert s["returns"]["all"] is not None, "전체 수익률은 초기 자본 기준이라 항상 계산된다"


def test_missing_midnight_snapshot_falls_back_to_previous(client, store):
    """00:00 스냅샷이 없으면(프로세스가 꺼져 있었다면) 그 이전 최근 스냅샷으로 대체한다."""
    now = now_kst()
    yesterday_evening = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
    store.record_snapshot(95_000, 95_000, {}, ts=yesterday_evening.isoformat())

    s = client.get("/api/summary").json()

    assert s["returns"]["today"] == pytest.approx(100_000 / 95_000 - 1)


def test_equity_series_shape(client, store):
    now = now_kst()
    for i in range(3):
        store.record_snapshot(100_000 + i, 0, {}, ts=(now - timedelta(hours=i)).isoformat())

    rows = client.get("/api/equity?period=1d").json()

    assert len(rows) == 3
    assert set(rows[0]) == {"t", "v"}
    assert rows[0]["t"] < rows[-1]["t"], "시간순 정렬"


def test_equity_period_filters(client, store):
    now = now_kst()
    store.record_snapshot(100_000, 0, {}, ts=(now - timedelta(days=20)).isoformat())
    store.record_snapshot(100_000, 0, {}, ts=now.isoformat())

    assert len(client.get("/api/equity?period=1d").json()) == 1
    assert len(client.get("/api/equity?period=1m").json()) == 2
    assert len(client.get("/api/equity?period=all").json()) == 2


# -------------------------------------------------------- 세대 생존 집계

def test_lifespans_group_by_generation(store):
    from web.app import _lifespans

    evals = [
        {"id": 1, "generation": 1, "killed": 0},
        {"id": 2, "generation": 1, "killed": 0},
        {"id": 3, "generation": 1, "killed": 1},  # 1세대는 3주 살고 죽음
        {"id": 4, "generation": 2, "killed": 1},  # 2세대는 1주 만에 죽음
    ]

    result = _lifespans(evals)

    assert result == [(2, [False]), (1, [True, True, False])]
