"""잡 4종.

  watchdog  : 60초   — 손절/익절/트레일링/서킷브레이커 (LLM 미사용)
  snapshot  : 60분   — 자산 스냅샷 (수익률·자산곡선·서킷브레이커 기준선)
  screener  : 60분   — 지표 계산(무료) → 볼 게 있을 때만 Claude 호출
  judge     : 주 1회 — 월요일 09:00 KST 주간 평가

사이클을 고정 주기로 돌리지 않는 이유: 매시간 Claude를 부르면 월 $13~19로 예산을 넘는다.
스크리너가 1시간마다 지표를 계산하되(비용 0원), Claude가 예약한 시각이 됐거나 신호가
잡혔을 때만 호출한다. 예약 시각은 DB(`next_cycle_at`)에 있어 프로세스가 재시작해도
이어진다 — APScheduler의 date 트리거를 쓰면 재시작 시 유실된다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from agent.cycle import run_cycle
from agent.judge import run_judge
from agent.review import run_review
from agent.screener import screen
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


def judge_and_review(store: Store, broker: Broker) -> None:
    """주간 평가 → 사후 회고. 회고가 실패해도 평가 결과는 이미 확정이므로 따로 감싼다."""
    verdict = run_judge(store, broker)
    try:
        run_review(verdict, store)
    except Exception as e:  # noqa: BLE001 — 회고 실패가 평가를 무효화해선 안 된다
        logger.exception(f"[회고] 실패(평가 결과는 유효): {e}")


def screener_tick(store: Store, broker: Broker) -> None:
    """1시간마다 지표를 계산하고(무료), 볼 게 있을 때만 Claude를 부른다."""
    result = screen(store, broker)
    if not result.should_call:
        logger.info(f"[스크리너] 판단 생략 — {result.reason}")
        return
    logger.info(f"[스크리너] Claude 호출 — {result.reason}")
    run_cycle(store, broker, screen_result=result)


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
        _guard("screener", lambda: screener_tick(store, broker)),
        IntervalTrigger(minutes=settings.screener_interval_minutes),
        id="screener", max_instances=1, coalesce=True, misfire_grace_time=600,
        next_run_time=now_kst(),
    )

    sched.add_job(
        _guard("judge", lambda: judge_and_review(store, broker)),
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
