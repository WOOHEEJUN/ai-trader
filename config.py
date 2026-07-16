"""전역 설정, 리스크 한도, 권한 레벨.

모든 한도값은 이 파일에만 정의한다. 다른 모듈은 `settings` / `tier_for()` / `MODEL_PRICING`을
import해서 쓰고, 숫자를 직접 박아넣지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

KST = ZoneInfo("Asia/Seoul")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "trader.db"
STRATEGY_MEMORY_PATH = DATA_DIR / "strategy_memory.md"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ 모드
    dry_run: bool = True

    # -------------------------------------------------------------------- 키
    anthropic_api_key: str = ""
    upbit_access_key: str = ""
    upbit_secret_key: str = ""

    # ------------------------------------------------------------------ 자금
    initial_capital_krw: int = 100_000
    capital_step_krw: int = 50_000  # 주간 평가 성공 시 운용 상한 증액분

    # -------------------------------------------- 가드레일 (executor가 강제)
    pre_judge_cooldown_hours: int = 24  # #1 평가 전 신규매수/비중확대 차단
    max_position_weight: float = 0.50   # #2 단일 코인 집중도 상한
    min_rebalance_delta: float = 0.05   # #3 목표-현재 비중 차이가 이 미만이면 주문 생략
    max_order_ratio: float = 0.30       # #6 1회 최대 주문 금액 (운용자금 대비)

    # ------------------------------ 청산 규칙 (watchdog이 강제, LLM 미사용)
    stop_loss_pct: float = -0.07        # 진입 평단 대비 손절선 (기본값)
    stop_loss_floor_pct: float = -0.10  # Claude가 지정 가능한 손절선 하한
    take_profit_pct: float = 0.15       # 진입 평단 대비 익절선
    take_profit_ratio: float = 0.50     # 익절 시 매도 비율 (절반만)
    trailing_stop_pct: float = -0.05    # 익절 발동 후 고점 대비
    daily_circuit_breaker_pct: float = -0.15  # 당일 총평가액 손실 한도

    # ---------------------------------------------------------------- 사이클
    min_cycle_interval_hours: int = 1
    max_cycle_interval_hours: int = 24
    throttled_min_cycle_interval_hours: int = 4  # 예산 80% 초과 시
    watchdog_interval_seconds: int = 60
    snapshot_interval_minutes: int = 60

    # ------------------------------------------------------------- 주간 평가
    judge_weekday: int = 0  # 0=월요일
    judge_hour: int = 9     # KST

    # ---------------------------------------------------------------- 예산
    monthly_budget_usd: float = 10.0
    budget_throttle_ratio: float = 0.80  # 이 비율 초과 시 감속

    # ------------------------------------------------------------------ LLM
    model: str = "claude-sonnet-5"
    effort_normal: str = "high"
    effort_throttled: str = "medium"
    max_tokens: int = 8_000

    # --------------------------------------------------------------- 거래소
    upbit_fee_rate: float = 0.0005  # 편도 0.05%
    min_order_krw: int = 5_000      # 업비트 최소주문금액
    slippage_pct: float = 0.001     # DRY_RUN 모의 체결 시 가정 슬리피지
    universe_min_volume_krw: float = 5_000_000_000  # 유니버스 최소 24h 거래대금

    # ------------------------------------------------------------------- 웹
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # ---------------------------------------------------------- 알림 (선택)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


settings = Settings()


# --------------------------------------------------------------- 권한 레벨
# 주간 평가 성공 시 level +1, 실패("kill") 시 0으로 복귀.

MAX_LEVEL = 3


@dataclass(frozen=True)
class PermissionTier:
    level: int
    max_daily_trades: int   # 가드레일 #4
    universe_size: int      # 가드레일 #7
    capital_limit_krw: int  # 운용 가능 금액 상한


def tier_for(level: int) -> PermissionTier:
    """레벨에 해당하는 권한 한도를 계산한다. 범위를 벗어난 레벨은 clamp된다."""
    level = max(0, min(int(level), MAX_LEVEL))
    return PermissionTier(
        level=level,
        max_daily_trades=6 + 2 * level,   # 6 → 12
        universe_size=20 + 5 * level,     # 20 → 35
        capital_limit_krw=settings.initial_capital_krw + settings.capital_step_krw * level,  # 10만 → 25만
    )


# ------------------------------------------------------------------ 단가표
# 1M 토큰당 USD. 정가 기준으로 잡는다 — Sonnet 5는 2026-08-31까지 프로모션가($2/$10)가
# 적용되지만, 정가로 계산하면 실제보다 비용을 높게 잡아 예산 소진을 앞당긴다.
# 예산 관점에서는 과대평가가 안전한 방향이므로 의도적으로 정가를 쓴다.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
