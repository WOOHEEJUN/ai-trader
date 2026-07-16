"""진입점: 스케줄러 + 대시보드 서버.

    python main.py            # 스케줄러 + 대시보드
    python main.py --no-web   # 스케줄러만
    python -m agent.cycle     # 사이클 1회 수동 실행
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

from config import LOG_DIR, ensure_dirs, settings


def setup_logging() -> None:
    ensure_dirs()
    logger.remove()
    # Windows 콘솔이 cp949라 한글이 깨진다 — 파일 로그는 UTF-8로 따로 남긴다.
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}")
    logger.add(LOG_DIR / "trader_{time:YYYY-MM-DD}.log", rotation="00:00", retention="90 days",
               encoding="utf-8", level="DEBUG",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} | {message}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 자율 암호화폐 매매 실험")
    parser.add_argument("--no-web", action="store_true", help="대시보드 없이 스케줄러만 실행")
    args = parser.parse_args()

    setup_logging()

    from scheduler import build_scheduler, describe_jobs

    if settings.dry_run:
        logger.info("모드: DRY-RUN — 실제 주문 없이 모의 체결만 기록한다")
    else:
        logger.warning("모드: ⚠️ 실거래 — 실제 자금이 움직인다")

    sched = build_scheduler(blocking=args.no_web)
    logger.info(describe_jobs(sched))

    if args.no_web:
        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("종료")
        return

    import uvicorn

    from web.app import app

    sched.start()
    logger.info(f"대시보드: http://{settings.web_host}:{settings.web_port}")
    try:
        uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="warning")
    finally:
        sched.shutdown(wait=False)
        logger.info("종료")


if __name__ == "__main__":
    main()
