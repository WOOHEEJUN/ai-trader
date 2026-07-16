"""주간 사후 회고 (Post Trade Review).

주간 평가(judge) 직후 실행된다. 이번 주 거래 통계를 계산해 Claude에게 주고, 회고를
받아 journal.md와 mistakes.md에 기록한다.

통계 계산은 무료(규칙 기반)이고, Claude 호출은 주 1회뿐이라 월 4회 × ~$0.05 = $0.2.
비용 대비 가치가 크다 — 이게 없으면 세대가 같은 실수를 반복하는 걸 스스로 못 본다.

평가에 실패해 kill된 주에는 회고를 남기지 않는다. 메모리가 초기화된 마당에 회고만
남기면 새 세대가 "내가 겪지 않은 실수"를 물려받게 되어 백지 재시작이 아니게 된다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

import anthropic
from loguru import logger
from pydantic import BaseModel, Field

from agent.budget import BudgetManager
from agent.judge import Verdict
from config import settings
from state.store import Store, get_store


class Review(BaseModel):
    did_well: str = Field(description="이번 주 잘한 점.")
    biggest_mistake: str = Field(description="이번 주 가장 큰 실수.")
    repeated_mistakes: str = Field(
        description="반복된 실수. 지난 mistakes.md와 이번 주 거래를 비교해 되풀이된 것만."
    )
    strategy_changes: str = Field(description="다음 주 수정할 전략.")
    strategy_keep: str = Field(description="유지해야 할 전략.")
    strategy_new: str = Field(description="새롭게 시도할 전략.")
    one_line: str = Field(description="이번 주 한 줄 요약.")
    mistakes_md: str = Field(
        description="갱신된 mistakes.md 전문. 기존 항목 중 유효한 것은 남기고, 이번 주에 "
                    "확인된 것을 추가한다. 더 이상 반복되지 않는 항목은 지운다. "
                    "마크다운 목록 형식."
    )


@dataclass
class WeeklyStats:
    total_trades: int
    sells: int
    wins: int
    losses: int
    win_rate: Optional[float]
    avg_win: Optional[float]
    avg_loss: Optional[float]
    max_loss: Optional[float]
    stop_losses: int
    take_profits: int
    trailing_exits: int
    circuit_breakers: int
    best: Optional[dict]
    worst: Optional[dict]
    rejected: list[dict]

    def as_text(self, verdict: Verdict) -> str:
        def money(v):
            return f"{v:+,.0f}원" if v is not None else "-"

        L = ["# 이번 주 거래 통계\n"]
        L.append(f"- 총 수익률: {verdict.pnl_pct:+.2%} ({verdict.pnl_krw:+,.0f}원, "
                 f"{verdict.start_krw:,.0f} → {verdict.end_krw:,.0f})")
        L.append(f"- 총 거래: {self.total_trades}건 (매도 {self.sells}건)")
        L.append(f"- 승률: {f'{self.win_rate:.0%}' if self.win_rate is not None else '-'} "
                 f"(승 {self.wins} / 패 {self.losses})")
        L.append(f"- 평균 수익: {money(self.avg_win)} / 평균 손실: {money(self.avg_loss)}")
        L.append(f"- 최대 손실: {money(self.max_loss)}")
        L.append(f"- 청산 사유별: 손절 {self.stop_losses} / 익절 {self.take_profits} / "
                 f"트레일링 {self.trailing_exits} / 서킷브레이커 {self.circuit_breakers}")
        if self.best:
            L.append(f"- 가장 성공한 거래: {self.best['market']} {self.best['pnl']:+,.0f}원 "
                     f"({self.best['reason_type']}) — {self.best['reason_text'][:80]}")
        if self.worst:
            L.append(f"- 가장 실패한 거래: {self.worst['market']} {self.worst['pnl']:+,.0f}원 "
                     f"({self.worst['reason_type']}) — {self.worst['reason_text'][:80]}")
        if self.rejected:
            L.append(f"\n# 거부/실패한 주문 {len(self.rejected)}건\n")
            for r in self.rejected[:10]:
                L.append(f"- {r['market']} {r['side']}: {r['reason']}")
        return "\n".join(L)


def compute_stats(store: Store, since_iso: str) -> WeeklyStats:
    """이번 주 거래 통계. 전부 거래 로그에서 나오므로 비용이 없다."""
    filled = [t for t in store.trades_since(since_iso, status="filled")]
    sells = [t for t in filled if t["side"] == "sell" and t["realized_pnl_krw"] is not None]
    pnls = [float(t["realized_pnl_krw"]) for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    def by_reason(kind: str) -> int:
        return sum(1 for t in filled if t["reason_type"] == kind)

    def summarize(row) -> dict:
        return {"market": row["market"], "pnl": float(row["realized_pnl_krw"]),
                "reason_type": row["reason_type"], "reason_text": row["reason_text"] or ""}

    rejected = [
        {"market": t["market"], "side": t["side"],
         "reason": t["reject_reason"] or t["reason_text"] or ""}
        for t in store._query(
            "SELECT * FROM trades WHERE ts >= ? AND status IN ('rejected','failed') ORDER BY id",
            (since_iso,),
        )
    ]

    return WeeklyStats(
        total_trades=len(filled),
        sells=len(sells),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(pnls)) if pnls else None,
        avg_win=statistics.mean(wins) if wins else None,
        avg_loss=statistics.mean(losses) if losses else None,
        max_loss=min(pnls) if pnls else None,
        stop_losses=by_reason("stop_loss"),
        take_profits=by_reason("take_profit"),
        trailing_exits=by_reason("trailing"),
        circuit_breakers=by_reason("circuit_breaker"),
        best=summarize(max(sells, key=lambda t: t["realized_pnl_krw"])) if sells else None,
        worst=summarize(min(sells, key=lambda t: t["realized_pnl_krw"])) if sells else None,
        rejected=rejected,
    )


SYSTEM_PROMPT = """너는 업비트 KRW 마켓에서 실제 자금을 운용하는 전문 단타 트레이더다.
방금 주간 평가를 마쳤고, 이번 주 거래를 스스로 복기하는 중이다.

# 회고 원칙

- **결과가 아니라 과정을 평가한다.** 운으로 번 거래는 잘한 게 아니고, 잘 판단했는데 진 거래는
  실수가 아니다. 진입 근거가 타당했는지를 본다.
- **구체적으로 쓴다.** "리스크 관리를 잘하자" 같은 건 쓸모없다. "RSI 70 이상에서 진입한 3건이
  모두 손절됐다" 같이 다음 사이클에 바로 적용할 수 있게 쓴다.
- **거래가 없었으면 없었다고 쓴다.** HOLD만 한 주는 실패가 아니다. 다만 놓친 셋업이 있었는지는 본다.
- **mistakes.md는 누적 관리한다.** 기존 항목 중 더 이상 반복되지 않는 건 지우고, 이번 주에
  확인된 것만 추가한다. 목록이 길어지면 다음 세대가 읽지 않는다.

# 자기기만 금지

수익이 났다고 과정이 옳았다고 결론짓지 마라. 손실이 났다고 전략을 전부 갈아엎지도 마라.
표본이 작다는 걸 인정하라 — 거래 3건으로 승률을 논하는 건 의미가 없다.
"""


def run_review(
    verdict: Verdict, store: Optional[Store] = None
) -> Optional[Review]:
    """주간 평가 직후 호출된다. 실패(kill)한 주에는 회고를 남기지 않는다."""
    store = store or get_store()

    if verdict.killed:
        logger.info("[회고] kill된 주 — 회고를 남기지 않는다 (새 세대는 백지에서 시작한다)")
        return None

    budget = BudgetManager(store)
    ok, reason = budget.can_call()
    if not ok:
        logger.warning(f"[회고] 생략: {reason}")
        return None

    stats = compute_stats(store, verdict.week_start)
    if stats.total_trades == 0:
        logger.info("[회고] 이번 주 거래 없음 — 회고 생략")
        return None

    if not settings.anthropic_api_key:
        logger.warning("[회고] ANTHROPIC_API_KEY 없음 — 생략")
        return None

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user = (
        f"{stats.as_text(verdict)}\n\n"
        f"# 현재 전략 (strategy.md)\n\n{store.read_strategy().strip()}\n\n"
        f"# 기존 실수 목록 (mistakes.md)\n\n{store.read_mistakes().strip()}\n\n"
        f"# 지시\n\n위 결과를 복기하고 회고를 작성하라."
    )

    response = client.messages.parse(
        model=settings.model,
        max_tokens=settings.max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": budget.status().effort},
        output_format=Review,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": user}],
    )
    cost = budget.record(response.usage, cycle_id="weekly-review")
    review = response.parsed_output
    if review is None:
        logger.error("[회고] 스키마 파싱 실패")
        return None

    entry = (
        f"**{review.one_line}**\n\n"
        f"- 수익률: {verdict.pnl_pct:+.2%} ({verdict.pnl_krw:+,.0f}원) / "
        f"거래 {stats.total_trades}건 / "
        f"승률 {f'{stats.win_rate:.0%}' if stats.win_rate is not None else '-'}\n\n"
        f"### 잘한 점\n{review.did_well}\n\n"
        f"### 가장 큰 실수\n{review.biggest_mistake}\n\n"
        f"### 반복된 실수\n{review.repeated_mistakes}\n\n"
        f"### 다음 주 수정할 전략\n{review.strategy_changes}\n\n"
        f"### 유지할 전략\n{review.strategy_keep}\n\n"
        f"### 새로 시도할 전략\n{review.strategy_new}"
    )
    store.append_journal(entry)
    if review.mistakes_md.strip():
        store.write_mistakes(review.mistakes_md)

    logger.info(f"[회고] 작성 완료 ${cost:.4f} — {review.one_line}")
    return review
