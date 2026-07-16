"""API 예산 관리 — 월 $10 상한, 3단계 감속.

상한에 닿았다고 즉시 죽이면 월 중순에 실험이 통째로 멈춘다. 대신:

  ~80%   normal     : 정상 (effort=high, 최소 사이클 간격 1시간)
  80~100% throttled : 감속 (effort=medium, 최소 사이클 간격 4시간)
  100%~  suspended  : Claude 호출 정지. **청산 감시(watchdog)는 계속 돈다.**

정지 상태는 "포지션 방치"가 아니다 — 손절·익절·트레일링·서킷브레이커는 LLM을 쓰지
않으므로 예산과 무관하게 작동한다. 매월 1일 00:00 KST에 카운터가 리셋되며 자동 재개된다.

집계는 추정이 아니라 매 호출 `response.usage`의 실측 토큰 × 모델 단가다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from config import MODEL_PRICING, settings
from state.store import Store, get_store, now_kst

# 예산 잔량 확인 시 쓰는 1회 호출 비용 추정치. 실측 이력이 쌓이면 그 평균을 우선한다.
FALLBACK_CALL_COST_USD = 0.05


class BudgetState(str, Enum):
    NORMAL = "normal"
    THROTTLED = "throttled"
    SUSPENDED = "suspended"


@dataclass
class BudgetStatus:
    state: BudgetState
    month: str
    spent_usd: float
    limit_usd: float
    remaining_usd: float
    ratio: float
    call_count: int
    resets_at: datetime
    effort: str              # 이 상태에서 써야 할 effort
    min_interval_hours: int  # 이 상태에서 강제되는 최소 사이클 간격

    @property
    def suspended(self) -> bool:
        return self.state is BudgetState.SUSPENDED


def cost_of(usage: Any, model: str) -> float:
    """`response.usage`(또는 동일 필드를 가진 객체/딕셔너리)를 USD 비용으로 환산."""
    price = MODEL_PRICING.get(model)
    if price is None:
        # 모르는 모델이면 가장 비싼 단가로 잡는다 — 예산은 과대평가가 안전하다.
        price = max(MODEL_PRICING.values(), key=lambda p: p["output"])

    def field(name: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(name) or 0)
        return int(getattr(usage, name, 0) or 0)

    return (
        field("input_tokens") * price["input"]
        + field("output_tokens") * price["output"]
        + field("cache_creation_input_tokens") * price["cache_write"]
        + field("cache_read_input_tokens") * price["cache_read"]
    ) / 1_000_000


def _next_month_start(dt: datetime) -> datetime:
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetManager:
    def __init__(self, store: Optional[Store] = None) -> None:
        self.store = store or get_store()

    def status(self) -> BudgetStatus:
        now = now_kst()
        month = now.strftime("%Y-%m")
        spent = self.store.month_cost_usd(month)
        limit = settings.monthly_budget_usd
        ratio = spent / limit if limit > 0 else 1.0

        if ratio >= 1.0:
            state = BudgetState.SUSPENDED
        elif ratio >= settings.budget_throttle_ratio:
            state = BudgetState.THROTTLED
        else:
            state = BudgetState.NORMAL

        throttling = state is not BudgetState.NORMAL
        return BudgetStatus(
            state=state,
            month=month,
            spent_usd=spent,
            limit_usd=limit,
            remaining_usd=max(0.0, limit - spent),
            ratio=ratio,
            call_count=self.store.month_call_count(month),
            resets_at=_next_month_start(now),
            effort=settings.effort_throttled if throttling else settings.effort_normal,
            min_interval_hours=(
                settings.throttled_min_cycle_interval_hours if throttling
                else settings.min_cycle_interval_hours
            ),
        )

    def estimated_call_cost(self) -> float:
        """이번 달 실측 평균 호출 비용. 이력이 없으면 보수적 기본값."""
        status = self.status()
        if status.call_count == 0:
            return FALLBACK_CALL_COST_USD
        return max(status.spent_usd / status.call_count, FALLBACK_CALL_COST_USD * 0.2)

    def can_call(self) -> tuple[bool, str]:
        """Claude 호출 전 확인. 초과 과금을 막기 위해 호출 *전에* 차단한다."""
        status = self.status()
        if status.suspended:
            return False, (
                f"API 월 예산 소진 (${status.spent_usd:.2f}/${status.limit_usd:.2f}). "
                f"{status.resets_at:%Y-%m-%d} 자동 재개. 청산 감시는 계속 작동 중."
            )
        estimate = self.estimated_call_cost()
        if status.remaining_usd < estimate:
            return False, (
                f"예산 잔여 ${status.remaining_usd:.2f} < 1회 호출 예상 ${estimate:.2f} — 호출 생략. "
                f"{status.resets_at:%Y-%m-%d} 자동 재개."
            )
        return True, ""

    def record(self, usage: Any, *, model: str | None = None, cycle_id: str = "") -> float:
        """호출 직후 실측 usage를 기록하고 비용을 반환한다."""
        model = model or settings.model

        def field(name: str) -> int:
            if isinstance(usage, dict):
                return int(usage.get(name) or 0)
            return int(getattr(usage, name, 0) or 0)

        cost = cost_of(usage, model)
        self.store.record_api_usage(
            model=model,
            cost_usd=cost,
            input_tokens=field("input_tokens"),
            output_tokens=field("output_tokens"),
            cache_write_tokens=field("cache_creation_input_tokens"),
            cache_read_tokens=field("cache_read_input_tokens"),
            cycle_id=cycle_id,
        )
        return cost
