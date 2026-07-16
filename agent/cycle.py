"""트레이딩 사이클 1회 = 판단 → 실행 → 다음 시점 예약.

스케줄러가 이 함수를 호출한다. 수동 실행(`python -m agent.cycle`)도 가능해서
실거래 전환 시 1회만 트리거해볼 때 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from agent.budget import BudgetManager
from agent.executor import Executor, Note
from agent.strategist import Decision, Strategist
from agent.watchdog import REASON_LABELS
from config import settings
from exchange.upbit_client import Broker, Fill, get_broker
from state.store import Store, get_store, now_iso, now_kst

LAST_CYCLE_TS_KEY = "last_cycle_ts"
PENDING_NOTES_KEY = "pending_notes"
NEXT_CYCLE_AT_KEY = "next_cycle_at"


@dataclass
class CycleResult:
    cycle_id: str
    next_check_at: datetime
    decision: Optional[Decision] = None
    fills: list[Fill] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    cost_usd: float = 0.0
    skipped: str = ""


def _recent_liquidations(store: Store) -> list[dict]:
    """지난 사이클 이후 감시 엔진이 집행한 청산. Claude가 이걸 알아야 판단이 성립한다."""
    since = store.get_state(LAST_CYCLE_TS_KEY)
    if not since:
        return []
    return [
        {
            "market": r["market"],
            "reason": REASON_LABELS.get(r["reason_type"], r["reason_type"]),
            "detail": r["reason_text"] or "",
        }
        for r in store.trades_since(since)
        if r["reason_type"] != "llm"
    ]


def run_cycle(store: Optional[Store] = None, broker: Optional[Broker] = None) -> CycleResult:
    store = store or get_store()
    broker = broker or get_broker(store)
    now = now_kst()
    cycle_id = f"cyc-{now:%Y%m%d-%H%M%S}"

    liquidations = _recent_liquidations(store)
    rejections = store.get_state(PENDING_NOTES_KEY, []) or []

    out = Strategist(store, broker).decide(
        cycle_id, liquidations=liquidations, rejections=rejections
    )
    budget = BudgetManager(store).status()

    # 예산 정지/부족으로 호출을 건너뛴 경우 — 감시 엔진은 계속 돌고 있다.
    if out.decision is None:
        next_at = (
            budget.resets_at if budget.suspended
            else now + timedelta(hours=budget.min_interval_hours)
        )
        store.record_cycle(cycle_id, skipped=out.skipped, next_check_at=next_at.isoformat())
        store.set_state(NEXT_CYCLE_AT_KEY, next_at.isoformat())
        store.set_state(LAST_CYCLE_TS_KEY, now_iso())
        logger.warning(f"[사이클] {cycle_id} 판단 생략: {out.skipped}")
        return CycleResult(cycle_id, next_at, cost_usd=out.cost_usd, skipped=out.skipped)

    result = Executor(store, broker).execute(out.decision, cycle_id)

    if out.decision.memory_note.strip():
        store.append_memory(out.decision.memory_note)

    # Claude가 정한 다음 체크 시점. 예산 상태에 따른 최소 간격과 최대 24시간으로 clamp.
    hours = max(
        budget.min_interval_hours,
        min(int(out.decision.next_check_hours), settings.max_cycle_interval_hours),
    )
    next_at = now + timedelta(hours=hours)

    store.set_state(PENDING_NOTES_KEY, [n.as_dict() for n in result.notes])
    store.set_state(LAST_CYCLE_TS_KEY, now_iso())
    store.set_state(NEXT_CYCLE_AT_KEY, next_at.isoformat())
    store.record_cycle(
        cycle_id,
        decision=out.decision.model_dump(),
        rationale=out.decision.rationale,
        next_check_at=next_at.isoformat(),
        traded=result.traded,
    )
    logger.info(
        f"[사이클] {cycle_id} 완료 — 체결 {len(result.fills)}건, 조정/거부 {len(result.notes)}건, "
        f"${out.cost_usd:.4f}, 다음 {hours}시간 뒤"
    )
    return CycleResult(cycle_id, next_at, out.decision, result.fills, result.notes, out.cost_usd)


if __name__ == "__main__":  # 수동 1회 실행
    run_cycle()
