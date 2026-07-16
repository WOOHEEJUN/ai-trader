"""잡 4종.

  watchdog  : 60초    — 손절/익절/트레일링/서킷브레이커 (LLM 미사용)
  snapshot  : 60분    — 자산 스냅샷 (수익률·자산곡선·서킷브레이커 기준선)
  cycle     : 5분 틱  — `next_cycle_at`이 지났으면 트레이딩 사이클 실행
  judge     : 주 1회  — 월요일 09:00 KST 주간 평가

사이클을 고정 주기가 아니라 "틱 + 예정시각 확인"으로 돌리는 이유: Claude가 다음 체크
시점을 직접 정하는데(1~24시간), 그 시점을 DB에 저장해두면 프로세스가 재시작해도
스케줄이 그대로 이어진다. APScheduler의 date 트리거를 쓰면 재시작 시 유실된다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from agent.cycle import NEXT_CYCLE_AT_KEY, run_cycle
from agent.judge import run_judge
from agent.watchdog import Watchdog, compute_portfolio
from config import KST, settings
from exchange.upbit_client import Broker, get_broker
from state.store import Store, get_store, now_kst


def _guard(name: str, fn: Callable) -> Callable:
    """어떤 잡도 예외로 스케줄러를 죽이지 못하게 감싼다."""
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[스케줄러] '{name}' 잡 실패(다음 주기에 재시도): {e}")
    return wrapped


def snapshot_job(store: Store, broker: Broker) -> None:
    total, cash, holdings = compute_portfolio(broker)
    store.record_snapshot(total, cash, holdings)
    logger.debug(f"[스냅샷] 총 {total:,.0f}원 (현금 {cash:,.0f}원)")


def cycle_tick(store: Store, broker: Broker) -> None:
    """예정 시각이 지났을 때만 사이클을 돈다."""
    next_at = store.get_state(NEXT_CYCLE_AT_KEY)
    if next_at:
        try:
            if now_kst() < datetime.fromisoformat(next_at):
                return
        except ValueError:
            logger.warning(f"[스케줄러] next_cycle_at 파싱 실패({next_at}) — 즉시 실행")
    run_cycle(store, broker)


def build_scheduler(
    store: Optional[Store] = None, broker: Optional[Broker] = None, blocking: bool = False
):
    store = store or get_store()
    broker = broker or get_broker(store)
    watchdog = Watchdog(store, broker)

    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=KST)

    # 청산 감시 — 이 잡만은 무슨 일이 있어도 계속 돌아야 한다.
    sched.add_job(
        _guard("watchdog", watchdog.run_once),
        IntervalTrigger(seconds=settings.watchdog_interval_seconds),
        id="watchdog", max_instances=1, coalesce=True, misfire_grace_time=30,
    )

    # 자산 스냅샷 — 즉시 1회 실행해 서킷브레이커 기준선을 만든다.
    sched.add_job(
        _guard("snapshot", lambda: snapshot_job(store, broker)),
        IntervalTrigger(minutes=settings.snapshot_interval_minutes),
        id="snapshot", max_instances=1, coalesce=True,
        next_run_time=now_kst(),
    )

    sched.add_job(
        _guard("cycle", lambda: cycle_tick(store, broker)),
        IntervalTrigger(minutes=5),
        id="cycle", max_instances=1, coalesce=True, misfire_grace_time=300,
    )

    sched.add_job(
        _guard("judge", lambda: run_judge(store, broker)),
        CronTrigger(day_of_week=settings.judge_weekday, hour=settings.judge_hour,
                    minute=0, timezone=KST),
        id="judge", max_instances=1, coalesce=True, misfire_grace_time=3600,
    )

    return sched


def describe_jobs(sched) -> str:
    lines = ["등록된 잡:"]
    for job in sched.get_jobs():
        # 스케줄러 시작 전 잡은 pending 상태라 next_run_time 속성 자체가 없다.
        run_at = getattr(job, "next_run_time", None)
        nxt = run_at.strftime("%Y-%m-%d %H:%M:%S") if run_at else "(시작 대기)"
        lines.append(f"  {job.id:<9} {str(job.trigger):<44} 다음 {nxt}")
    return "\n".join(lines)


if __name__ == "__main__":
    from main import setup_logging

    setup_logging()
    sched = build_scheduler(blocking=True)
    logger.info(f"모드: {'DRY-RUN (모의 체결)' if settings.dry_run else '⚠️ 실거래'}")
    logger.info(describe_jobs(sched))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")
