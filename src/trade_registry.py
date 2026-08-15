"""
Trade registry — the hinge of the live pipeline (docs/design-doc.md §11).

An ENTRY writes `msg_id -> ticket` here when a trade opens; a later UPDATE reads
it back to find which live position the post is talking about. Without this
store an update has nothing to attach to.

Correlation order (settled spec):
  1. edit of a known msg_id            -> that trade
  2. reply_to_msg_id matches a trade   -> that trade
  3. unlinked update                   -> the single open trade (we hold one)
  4. no match                          -> None; caller logs and alerts, never guesses

State machine:
  PENDING -> OPEN -> CLOSED
  PENDING -> SKIPPED (failed a gate)
Updates are accepted in PENDING/OPEN and recorded-but-ignored in CLOSED.

Everything is append-only journalled so any trade can be replayed from history.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DEFAULT = ROOT / "data" / "trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    msg_id        INTEGER PRIMARY KEY,
    channel       TEXT    NOT NULL,
    posted_at     TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    entry_low     REAL,
    entry_high    REAL,
    sl            REAL    NOT NULL,
    tp            REAL    NOT NULL,
    sl_source     TEXT    NOT NULL,   -- 'signal' | 'default'
    tp_source     TEXT    NOT NULL,
    lots          REAL,
    ticket        INTEGER,
    fill_price    REAL,
    state         TEXT    NOT NULL,   -- PENDING | OPEN | CLOSED | SKIPPED
    opened_at     TEXT,
    closed_at     TEXT,
    close_reason  TEXT,
    realized_r    REAL
);
CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    msg_id     INTEGER,
    event      TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_state ON trades(state);
"""

OPEN_STATES = ("PENDING", "OPEN")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeRegistry:
    def __init__(self, path: Path | str = DB_DEFAULT):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------------------------------------------------------------- journal

    def log(self, event: str, msg_id: int | None = None, **detail):
        self.db.execute(
            "INSERT INTO journal (at, msg_id, event, detail) VALUES (?,?,?,?)",
            (_now(), msg_id, event, json.dumps(detail, default=str)),
        )
        self.db.commit()

    def events(self, msg_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM journal"
        args = ()
        if msg_id is not None:
            q += " WHERE msg_id = ?"
            args = (msg_id,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]

    # ---------------------------------------------------------------- writes

    def register_entry(self, msg_id: int, channel: str, posted_at: str, side: str,
                       sl: float, tp: float, entry_low: float | None = None,
                       entry_high: float | None = None, sl_source: str = "signal",
                       tp_source: str = "signal") -> dict:
        """Create a PENDING trade. Idempotent on msg_id — a duplicate delivery
        of the same post must not create a second trade."""
        existing = self.get(msg_id)
        if existing:
            self.log("duplicate_entry_ignored", msg_id, state=existing["state"])
            return existing
        self.db.execute(
            "INSERT INTO trades (msg_id, channel, posted_at, side, entry_low, entry_high,"
            " sl, tp, sl_source, tp_source, state) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING')",
            (msg_id, channel, posted_at, side, entry_low, entry_high, sl, tp,
             sl_source, tp_source),
        )
        self.db.commit()
        self.log("registered", msg_id, side=side, sl=sl, tp=tp,
                 sl_source=sl_source, tp_source=tp_source)
        return self.get(msg_id)

    def mark_open(self, msg_id: int, ticket: int, fill_price: float, lots: float,
                  at: str | None = None):
        """`at` lets a simulation stamp the CANDLE time. Live callers omit it.
        Getting this wrong collapses every simulated trade into one trading day,
        which silently disables the daily stop and the monthly kill-switch."""
        self.db.execute(
            "UPDATE trades SET state='OPEN', ticket=?, fill_price=?, lots=?, opened_at=?"
            " WHERE msg_id=? AND state='PENDING'",
            (ticket, fill_price, lots, at or _now(), msg_id),
        )
        self.db.commit()
        self.log("opened", msg_id, ticket=ticket, fill=fill_price, lots=lots)
        return self.get(msg_id)

    def mark_closed(self, msg_id: int, reason: str, realized_r: float | None = None,
                    at: str | None = None):
        """`at` lets a simulation stamp the CANDLE time (see mark_open)."""
        self.db.execute(
            "UPDATE trades SET state='CLOSED', closed_at=?, close_reason=?, realized_r=?"
            " WHERE msg_id=? AND state IN ('PENDING','OPEN')",
            (at or _now(), reason, realized_r, msg_id),
        )
        self.db.commit()
        self.log("closed", msg_id, reason=reason, r=realized_r)
        return self.get(msg_id)

    def mark_skipped(self, msg_id: int, reason: str):
        """Record a signal we deliberately did not take, so the decision is auditable."""
        if not self.get(msg_id):
            self.db.execute(
                "INSERT INTO trades (msg_id, channel, posted_at, side, sl, tp,"
                " sl_source, tp_source, state) VALUES (?,'','','none',0,0,'','','SKIPPED')",
                (msg_id,),
            )
        else:
            self.db.execute("UPDATE trades SET state='SKIPPED' WHERE msg_id=?", (msg_id,))
        self.db.commit()
        self.log("skipped", msg_id, reason=reason)

    def apply_update(self, msg_id: int, new_sl: float | None = None,
                     new_tp: float | None = None, by_msg: int | None = None):
        """Amend the stop/target on a live trade. Refuses on CLOSED."""
        t = self.get(msg_id)
        if not t:
            return None
        if t["state"] == "CLOSED":
            self.log("update_on_closed_ignored", msg_id, by_msg=by_msg,
                     new_sl=new_sl, new_tp=new_tp)
            return t
        sets, args = [], []
        if new_sl:
            sets.append("sl=?"); args.append(new_sl)
        if new_tp:
            sets.append("tp=?"); args.append(new_tp)
        if sets:
            args.append(msg_id)
            self.db.execute(f"UPDATE trades SET {','.join(sets)} WHERE msg_id=?", args)
            self.db.commit()
        self.log("updated", msg_id, by_msg=by_msg, new_sl=new_sl, new_tp=new_tp)
        return self.get(msg_id)

    # ---------------------------------------------------------------- reads

    def get(self, msg_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM trades WHERE msg_id=?", (msg_id,)).fetchone()
        return dict(r) if r else None

    def open_trades(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM trades WHERE state IN ('PENDING','OPEN') ORDER BY msg_id")]

    def has_open(self) -> bool:
        """The one-trade-at-a-time gate."""
        return bool(self.open_trades())

    # ------------------------------------------------------------ correlation

    def correlate(self, msg_id: int | None = None, reply_to: int | None = None,
                  is_edit: bool = False) -> tuple[dict | None, str]:
        """Which trade is this update talking about? Returns (trade, how)."""
        if is_edit and msg_id is not None:
            t = self.get(msg_id)
            if t:
                return t, "edit_of_known_msg"
        if reply_to is not None:
            t = self.get(reply_to)
            if t:
                return t, "reply_to_msg_id"
        live = self.open_trades()
        if len(live) == 1:
            return live[0], "sole_open_trade"
        if len(live) > 1:                       # cannot happen under the locked
            return None, "ambiguous_multiple_open"   # strategy, but never guess
        return None, "no_match"


def lots_for(risk_pct: float, equity: float, sl_pips: float,
             min_lot: float = 0.01, step: float = 0.01) -> float:
    """lots = risk$ / (SL_pips x $10 per pip per lot), floored to the broker step."""
    if sl_pips <= 0:
        return 0.0
    raw = (risk_pct / 100.0 * equity) / (sl_pips * 10.0)
    stepped = int(raw / step) * step
    return round(max(stepped, min_lot) if raw >= min_lot else 0.0, 2)
