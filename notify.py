"""알림. Telegram 토큰이 없으면 로그로만 남긴다 (1차 구현 기본값).

API 키·시크릿은 절대 알림 본문에 넣지 않는다.
"""
from __future__ import annotations

import requests
from loguru import logger

from config import settings

TIMEOUT = 5


def notify(message: str, *, level: str = "info") -> None:
    getattr(logger, level, logger.info)(f"[알림] {message}")
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 — 알림 실패가 매매를 막아선 안 된다
        logger.warning(f"텔레그램 전송 실패(무시): {e}")
