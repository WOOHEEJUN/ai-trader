"""읽기 전용 대시보드.

주문 실행 경로는 여기에 없다 — 이 앱은 SQLite와 시세를 읽기만 한다. 대시보드가
털려도 매매가 일어날 수 없어야 하기 때문. (접근 통제는 Tailscale로 처리한다.)
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent.budget import BudgetManager
from agent.cycle import NEXT_CYCLE_AT_KEY
from agent.strategist import hours_until_judge
from agent.watchdog import (
    CIRCUIT_BREAKER_DATE_KEY,
    REASON_LABELS,
    WATCHDOG_LAST_RUN_KEY,
    compute_portfolio,
    is_circuit_breaker_active,
)
from config import settings, tier_for
from exchange.upbit_client import get_broker
from state.store import get_store, now_kst

BASE = Path(__file__).resolve().parent

app = FastAPI(title="AI Trader", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

PERIODS = {"1d": timedelta(days=1), "1w": timedelta(days=7), "1m": timedelta(days=30)}


def _krw(v) -> str:
    return f"{v:,.0f}" if v is not None else "-"


def _pct(v) -> str:
    return f"{v:+.2%}" if v is not None else "-"


def _sign(v) -> str:
    if v is None or abs(v) < 1e-9:
        return "flat"
    return "up" if v > 0 else "down"


def _dt(v) -> str:
    """ISO 문자열을 'MM-DD HH:MM'으로. 파싱 실패 시 원문 그대로."""
    if not v:
        return "-"
    try:
        from datetime import datetime
        return datetime.fromisoformat(v).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(v)


templates.env.filters.update(krw=_krw, pct=_pct, sign=_sign, dt=_dt)


# ------------------------------------------------------------------ 계산

def _return_since(store, total: float, since_iso: str) -> Optional[float]:
    snap = store.snapshot_at_or_before(since_iso)
    if not snap or snap["total_krw"] <= 0:
        return None
    return total / snap["total_krw"] - 1


def build_summary() -> dict:
    store = get_store()
    broker = get_broker(store)
    total, cash, holdings = compute_portfolio(broker)
    now = now_kst()

    positions = []
    for market, h in holdings.items():
        pos = store.get_position(market)
        row = {
            "market": market,
            "qty": h["qty"],
            "price": h["price"],
            "value_krw": h["value_krw"],
            "weight": h["value_krw"] / total if total > 0 else 0,
            "avg_price": pos.avg_price if pos else None,
            "pnl_pct": pos.pnl_pct(h["price"]) if pos else None,
            "stop_loss_pct": pos.stop_loss_pct if pos else None,
            "peak_price": pos.peak_price if pos else None,
            "take_profit_done": pos.take_profit_done if pos else False,
            # 손절선까지 남은 거리: 현재가가 몇 % 더 떨어지면 손절되는가
            "stop_distance": (
                (pos.avg_price * (1 + pos.stop_loss_pct)) / h["price"] - 1
                if pos and h["price"] > 0 else None
            ),
        }
        positions.append(row)
    positions.sort(key=lambda p: -p["value_krw"])

    budget = BudgetManager(store).status()
    tier = tier_for(store.get_state("permission_level", 0))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        "total_krw": total,
        "cash_krw": cash,
        "coin_krw": total - cash,
        "returns": {
            "today": _return_since(store, total, midnight.isoformat()),
            "week": _return_since(store, total, (now - timedelta(days=7)).isoformat()),
            "month": _return_since(store, total, (now - timedelta(days=30)).isoformat()),
            "all": total / settings.initial_capital_krw - 1,
        },
        "pnl_all_krw": total - settings.initial_capital_krw,
        "positions": positions,
        "budget": {
            "state": budget.state.value,
            "spent_usd": budget.spent_usd,
            "limit_usd": budget.limit_usd,
            "ratio": budget.ratio,
            "call_count": budget.call_count,
            "resets_at": budget.resets_at.strftime("%Y-%m-%d"),
        },
        "status": {
            "dry_run": settings.dry_run,
            "circuit_breaker": is_circuit_breaker_active(store),
            "circuit_breaker_date": store.get_state(CIRCUIT_BREAKER_DATE_KEY),
            "cooldown": hours_until_judge() <= settings.pre_judge_cooldown_hours,
            "hours_until_judge": hours_until_judge(),
            "next_cycle_at": store.get_state(NEXT_CYCLE_AT_KEY),
            "watchdog_last_run": store.get_state(WATCHDOG_LAST_RUN_KEY),
            "trades_today": store.count_trades_today(),
            "max_daily_trades": tier.max_daily_trades,
            "level": tier.level,
            "capital_limit_krw": tier.capital_limit_krw,
            "generation": store.get_state("generation", 1),
        },
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ------------------------------------------------------------------ 페이지

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"s": build_summary()})


@app.get("/trades")
def trades(request: Request, limit: int = 200):
    store = get_store()
    rows = [dict(r) for r in store.list_trades(limit=limit)]
    for r in rows:
        r["reason_label"] = "LLM 판단" if r["reason_type"] == "llm" else REASON_LABELS.get(
            r["reason_type"], r["reason_type"]
        )
    return templates.TemplateResponse(
        request, "trades.html", {"trades": rows, "s": build_summary()}
    )


@app.get("/brain")
def brain(request: Request):
    store = get_store()
    cycles = []
    for r in store.list_cycles(limit=60):
        c = dict(r)
        c["decision"] = json.loads(c["decision_json"]) if c["decision_json"] else None
        cycles.append(c)
    return templates.TemplateResponse(
        request, "brain.html",
        {"memory": store.read_memory(), "cycles": cycles, "s": build_summary()},
    )


def _lifespans(evals: list[dict]) -> list[tuple[int, list[bool]]]:
    """세대별 주간 결과. [(세대, [생존여부, ...]), ...] — 최신 세대가 먼저."""
    by_gen: dict[int, list[bool]] = {}
    for e in sorted(evals, key=lambda r: r["id"]):
        by_gen.setdefault(e["generation"], []).append(not e["killed"])
    return sorted(by_gen.items(), reverse=True)


@app.get("/generations")
def generations(request: Request):
    store = get_store()
    evals = [dict(r) for r in store.list_evaluations()]
    return templates.TemplateResponse(
        request, "generations.html",
        {"evals": evals, "lifespans": _lifespans(evals), "s": build_summary()},
    )


@app.get("/watchdog")
def watchdog_view(request: Request):
    return templates.TemplateResponse(request, "watchdog.html", {"s": build_summary()})


# --------------------------------------------------------------------- API

@app.get("/api/summary")
def api_summary():
    return JSONResponse(build_summary())


@app.get("/api/equity")
def api_equity(period: str = "1w"):
    store = get_store()
    if period == "all":
        since = "2000-01-01"
    else:
        since = (now_kst() - PERIODS.get(period, PERIODS["1w"])).isoformat()
    rows = store.snapshots_since(since)
    return JSONResponse([{"t": r["ts"], "v": r["total_krw"]} for r in rows])
