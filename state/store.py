"""SQLite 저장소 + 전략 메모리 파일.

스케줄러 잡(여러 스레드)과 FastAPI가 같은 프로세스에서 이 저장소를 공유하므로
스레드별 커넥션 + WAL 모드를 쓴다.

거래 로그(`trades`)와 스냅샷은 "kill" 이벤트에도 절대 지우지 않는다 — 사용자가
세대별 역사를 확인할 수 있어야 하기 때문. 초기화되는 것은 `strategy_memory.md`뿐이다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from config import (
    DB_PATH,
    JOURNAL_PATH,
    KST,
    MISTAKES_PATH,
    STRATEGY_PATH,
    ensure_dirs,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    side          TEXT NOT NULL,              -- buy | sell
    market        TEXT NOT NULL,
    qty           REAL NOT NULL DEFAULT 0,
    price         REAL NOT NULL DEFAULT 0,
    amount_krw    REAL NOT NULL DEFAULT 0,
    fee_krw       REAL NOT NULL DEFAULT 0,
    reason_type   TEXT NOT NULL,              -- llm | stop_loss | take_profit | trailing | circuit_breaker
    reason_text   TEXT,
    status        TEXT NOT NULL,              -- filled | rejected | failed
    reject_reason TEXT,
    order_uuid    TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    total_krw     REAL NOT NULL,
    cash_krw      REAL NOT NULL,
    holdings_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS positions (
    market           TEXT PRIMARY KEY,
    qty              REAL NOT NULL,
    avg_price        REAL NOT NULL,
    peak_price       REAL NOT NULL,
    stop_loss_pct    REAL NOT NULL,
    take_profit_done INTEGER NOT NULL DEFAULT 0,
    opened_at        TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    month              TEXT NOT NULL,          -- YYYY-MM (KST)
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL,
    cycle_id           TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_usage_month ON api_usage(month);

CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    week_start   TEXT NOT NULL,
    week_end     TEXT NOT NULL,
    start_krw    REAL NOT NULL,
    end_krw      REAL NOT NULL,
    pnl_krw      REAL NOT NULL,
    pnl_pct      REAL NOT NULL,
    success      INTEGER NOT NULL,
    killed       INTEGER NOT NULL,
    generation   INTEGER NOT NULL,
    level_before INTEGER NOT NULL,
    level_after  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    decision_json TEXT,
    rationale     TEXT,
    next_check_at TEXT,
    traded        INTEGER NOT NULL DEFAULT 0,
    skipped       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_ts ON cycles(ts);

CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

STRATEGY_HEADER = """# 전략 (strategy.md)

현재 적용 중인 전략. Claude가 매 사이클 읽고, 바꿀 이유가 있을 때 갱신한다.
주간 평가 실패 시 이 파일은 초기화된다 — 거래 로그는 보존되지만 새 세대는 그것을
읽을 수 없으므로, "과거의 나"가 무엇을 했는지 모른 채 시작한다.
"""

MISTAKES_HEADER = """# 반복하지 말아야 할 실수 (mistakes.md)

주간 회고에서 확인된, 되풀이되는 실수. 매 사이클 전략 다음으로 읽는다.
kill 시 함께 초기화된다 — 새 세대는 같은 실수를 다시 겪어야 한다.
"""

JOURNAL_HEADER = """# 주간 회고 (journal.md)

주간 평가 직후 작성되는 사후 회고. 누적되며, 프롬프트에는 최근 것만 들어간다.
"""


def now_kst() -> datetime:
    return datetime.now(KST)


def now_iso() -> str:
    return now_kst().isoformat()


@dataclass
class Position:
    market: str
    qty: float
    avg_price: float
    peak_price: float
    stop_loss_pct: float
    take_profit_done: bool
    opened_at: str
    updated_at: str

    def pnl_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0


class Store:
    def __init__(self, db_path=DB_PATH) -> None:
        ensure_dirs()
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------ 커넥션
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchone()

    # ------------------------------------------------------- runtime_state
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._query_one("SELECT value FROM runtime_state WHERE key = ?", (key,))
        if row is None:
            return default
        return json.loads(row["value"])

    def set_state(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ------------------------------------------------------------- trades
    def record_trade(
        self,
        *,
        side: str,
        market: str,
        reason_type: str,
        status: str,
        qty: float = 0.0,
        price: float = 0.0,
        amount_krw: float = 0.0,
        fee_krw: float = 0.0,
        reason_text: str = "",
        reject_reason: str = "",
        cycle_id: str = "",
        order_uuid: str = "",
    ) -> int:
        cur = self._write(
            "INSERT INTO trades (ts, cycle_id, side, market, qty, price, amount_krw, fee_krw,"
            " reason_type, reason_text, status, reject_reason, order_uuid)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), cycle_id, side, market, qty, price, amount_krw, fee_krw,
             reason_type, reason_text, status, reject_reason, order_uuid),
        )
        return int(cur.lastrowid)

    def list_trades(self, limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def trades_since(self, since_iso: str, status: str = "filled") -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM trades WHERE ts >= ? AND status = ? ORDER BY id", (since_iso, status)
        )

    def count_trades_today(self) -> int:
        """오늘(KST) 체결된 매매 건수. 가드레일 #4(일일 매매 횟수)용.

        규칙 청산(손절/익절/트레일링/서킷브레이커)은 Claude의 판단이 아니므로 예산에서 제외한다.
        """
        day_start = now_kst().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = self._query_one(
            "SELECT COUNT(*) AS c FROM trades WHERE ts >= ? AND status = 'filled' AND reason_type = 'llm'",
            (day_start,),
        )
        return int(row["c"]) if row else 0

    # ---------------------------------------------------------- snapshots
    def record_snapshot(
        self, total_krw: float, cash_krw: float, holdings: dict, ts: str | None = None
    ) -> None:
        self._write(
            "INSERT INTO snapshots (ts, total_krw, cash_krw, holdings_json) VALUES (?,?,?,?)",
            (ts or now_iso(), total_krw, cash_krw, json.dumps(holdings)),
        )

    def latest_snapshot(self) -> Optional[sqlite3.Row]:
        return self._query_one("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")

    def snapshot_at_or_before(self, ts_iso: str) -> Optional[sqlite3.Row]:
        """해당 시각 이전의 가장 가까운 스냅샷. 없으면 가장 오래된 스냅샷으로 대체."""
        row = self._query_one(
            "SELECT * FROM snapshots WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts_iso,)
        )
        if row is None:
            row = self._query_one("SELECT * FROM snapshots ORDER BY ts ASC LIMIT 1")
        return row

    def snapshots_since(self, since_iso: str) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM snapshots WHERE ts >= ? ORDER BY ts", (since_iso,))

    # ---------------------------------------------------------- positions
    def _row_to_position(self, row: sqlite3.Row) -> Position:
        return Position(
            market=row["market"],
            qty=row["qty"],
            avg_price=row["avg_price"],
            peak_price=row["peak_price"],
            stop_loss_pct=row["stop_loss_pct"],
            take_profit_done=bool(row["take_profit_done"]),
            opened_at=row["opened_at"],
            updated_at=row["updated_at"],
        )

    def get_position(self, market: str) -> Optional[Position]:
        row = self._query_one("SELECT * FROM positions WHERE market = ?", (market,))
        return self._row_to_position(row) if row else None

    def list_positions(self) -> list[Position]:
        return [self._row_to_position(r) for r in self._query("SELECT * FROM positions ORDER BY market")]

    def apply_buy(self, market: str, qty: float, price: float, stop_loss_pct: float) -> Position:
        """매수 체결을 포지션에 반영한다. 분할 매수 시 평단은 가중평균으로 갱신."""
        pos = self.get_position(market)
        ts = now_iso()
        if pos is None:
            new = Position(market, qty, price, price, stop_loss_pct, False, ts, ts)
        else:
            total_qty = pos.qty + qty
            avg = (pos.avg_price * pos.qty + price * qty) / total_qty if total_qty > 0 else price
            new = Position(
                market=market,
                qty=total_qty,
                avg_price=avg,
                peak_price=max(pos.peak_price, price),
                stop_loss_pct=stop_loss_pct,
                take_profit_done=pos.take_profit_done,
                opened_at=pos.opened_at,
                updated_at=ts,
            )
        self._write(
            "INSERT INTO positions (market, qty, avg_price, peak_price, stop_loss_pct,"
            " take_profit_done, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(market) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price,"
            " peak_price=excluded.peak_price, stop_loss_pct=excluded.stop_loss_pct,"
            " take_profit_done=excluded.take_profit_done, updated_at=excluded.updated_at",
            (new.market, new.qty, new.avg_price, new.peak_price, new.stop_loss_pct,
             int(new.take_profit_done), new.opened_at, new.updated_at),
        )
        return new

    def apply_sell(self, market: str, qty: float, *, take_profit_done: bool | None = None) -> None:
        """매도 체결을 포지션에 반영한다. 잔량이 사실상 0이면 포지션을 삭제."""
        pos = self.get_position(market)
        if pos is None:
            return
        remaining = pos.qty - qty
        if remaining <= 1e-9:
            self._write("DELETE FROM positions WHERE market = ?", (market,))
            return
        tp_done = pos.take_profit_done if take_profit_done is None else take_profit_done
        self._write(
            "UPDATE positions SET qty = ?, take_profit_done = ?, updated_at = ? WHERE market = ?",
            (remaining, int(tp_done), now_iso(), market),
        )

    def update_peak(self, market: str, price: float) -> None:
        self._write(
            "UPDATE positions SET peak_price = ?, updated_at = ? WHERE market = ? AND peak_price < ?",
            (price, now_iso(), market, price),
        )

    def set_stop_loss(self, market: str, stop_loss_pct: float) -> None:
        self._write(
            "UPDATE positions SET stop_loss_pct = ?, updated_at = ? WHERE market = ?",
            (stop_loss_pct, now_iso(), market),
        )

    # ---------------------------------------------------------- api_usage
    def record_api_usage(
        self,
        *,
        model: str,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        cycle_id: str = "",
    ) -> None:
        self._write(
            "INSERT INTO api_usage (ts, month, model, input_tokens, output_tokens,"
            " cache_write_tokens, cache_read_tokens, cost_usd, cycle_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), now_kst().strftime("%Y-%m"), model, input_tokens, output_tokens,
             cache_write_tokens, cache_read_tokens, cost_usd, cycle_id),
        )

    def month_cost_usd(self, month: str | None = None) -> float:
        month = month or now_kst().strftime("%Y-%m")
        row = self._query_one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM api_usage WHERE month = ?", (month,)
        )
        return float(row["total"]) if row else 0.0

    def month_call_count(self, month: str | None = None) -> int:
        month = month or now_kst().strftime("%Y-%m")
        row = self._query_one("SELECT COUNT(*) AS c FROM api_usage WHERE month = ?", (month,))
        return int(row["c"]) if row else 0

    # -------------------------------------------------------- evaluations
    def record_evaluation(self, **kw: Any) -> None:
        self._write(
            "INSERT INTO evaluations (ts, week_start, week_end, start_krw, end_krw, pnl_krw,"
            " pnl_pct, success, killed, generation, level_before, level_after)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), kw["week_start"], kw["week_end"], kw["start_krw"], kw["end_krw"],
             kw["pnl_krw"], kw["pnl_pct"], int(kw["success"]), int(kw["killed"]),
             kw["generation"], kw["level_before"], kw["level_after"]),
        )

    def list_evaluations(self) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM evaluations ORDER BY id DESC")

    # ------------------------------------------------------------- cycles
    def record_cycle(
        self,
        cycle_id: str,
        *,
        decision: Any = None,
        rationale: str = "",
        next_check_at: str = "",
        traded: bool = False,
        skipped: str = "",
    ) -> None:
        self._write(
            "INSERT INTO cycles (id, ts, decision_json, rationale, next_check_at, traded, skipped)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET decision_json=excluded.decision_json,"
            " rationale=excluded.rationale, next_check_at=excluded.next_check_at,"
            " traded=excluded.traded, skipped=excluded.skipped",
            (cycle_id, now_iso(), json.dumps(decision) if decision is not None else None,
             rationale, next_check_at, int(traded), skipped),
        )

    def list_cycles(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM cycles ORDER BY ts DESC LIMIT ?", (limit,))

    # ----------------------------------------------------- strategy memory
    # 세 갈래로 나눈다. 매 사이클 strategy → mistakes → journal 순으로 읽히고,
    # kill 시엔 셋 다 초기화된다 — 새 세대는 백지에서 시작해야 하므로 mistakes도 예외가 아니다.

    def _read_or_init(self, path, header: str) -> str:
        if not path.exists():
            ensure_dirs()
            path.write_text(header, encoding="utf-8")
        return path.read_text(encoding="utf-8")

    def read_strategy(self) -> str:
        return self._read_or_init(STRATEGY_PATH, STRATEGY_HEADER)

    def write_strategy(self, text: str) -> None:
        """전략은 누적이 아니라 '현재 적용 중인 것'으로 통째로 교체한다."""
        ensure_dirs()
        STRATEGY_PATH.write_text(
            f"{STRATEGY_HEADER}\n_최종 갱신: {now_kst():%Y-%m-%d %H:%M} KST_\n\n{text.strip()}\n",
            encoding="utf-8",
        )

    def read_mistakes(self) -> str:
        return self._read_or_init(MISTAKES_PATH, MISTAKES_HEADER)

    def write_mistakes(self, text: str) -> None:
        ensure_dirs()
        MISTAKES_PATH.write_text(
            f"{MISTAKES_HEADER}\n_최종 갱신: {now_kst():%Y-%m-%d %H:%M} KST_\n\n{text.strip()}\n",
            encoding="utf-8",
        )

    def read_journal(self, last_n: int = 1) -> str:
        """주간 회고. 최근 last_n개만 — 전부 넣으면 매 사이클 컨텍스트가 샌다."""
        text = self._read_or_init(JOURNAL_PATH, JOURNAL_HEADER)
        entries = text.split("\n## ")[1:]
        if not entries:
            return ""
        return "\n\n".join(f"## {e.strip()}" for e in entries[-last_n:])

    def append_journal(self, entry: str) -> None:
        self._read_or_init(JOURNAL_PATH, JOURNAL_HEADER)
        with JOURNAL_PATH.open("a", encoding="utf-8") as f:
            f.write(f"\n## {now_kst():%Y-%m-%d} 주간 회고\n\n{entry.strip()}\n")

    def read_memory(self) -> str:
        """프롬프트용 통합 뷰. 스펙의 읽기 순서(전략 → 실수 → 최근 회고)를 따른다."""
        parts = [self.read_strategy().strip(), self.read_mistakes().strip()]
        journal = self.read_journal(last_n=1).strip()
        if journal:
            parts.append(f"# 최근 주간 회고\n\n{journal}")
        return "\n\n---\n\n".join(p for p in parts if p)

    def append_memory(self, note: str) -> None:
        """사이클 단위 관찰 노트. 전략 파일 뒤에 붙는다."""
        self._read_or_init(STRATEGY_PATH, STRATEGY_HEADER)
        with STRATEGY_PATH.open("a", encoding="utf-8") as f:
            f.write(f"\n## {now_kst():%Y-%m-%d %H:%M} KST\n\n{note.strip()}\n")

    def reset_memory(self) -> None:
        """kill 이벤트. 전략 메모리 3종만 초기화 — 거래 로그/스냅샷은 건드리지 않는다."""
        ensure_dirs()
        STRATEGY_PATH.write_text(STRATEGY_HEADER, encoding="utf-8")
        MISTAKES_PATH.write_text(MISTAKES_HEADER, encoding="utf-8")
        JOURNAL_PATH.write_text(JOURNAL_HEADER, encoding="utf-8")


_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
