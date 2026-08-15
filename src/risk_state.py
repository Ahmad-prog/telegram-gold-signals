"""
Risk state machine — the three controls that were configured but never coded.

  1. RISK LADDER      1.75% -> 1.25% -> 1.0%.  Step DOWN one rung after any
                      losing trade, reset to the top rung after any win.
                      Persists across days and across evaluation phases.
  2. DAILY STOP       after 2 CONSECUTIVE losing trades in one trading day,
                      take no more trades that day. A win resets the streak.
                      Trading day cuts at 21:00 UTC (GFT's daily reset).
  3. KILL SWITCH      latch OFF if rolling-60-trade net R <= -10R, or after 2
                      consecutive losing calendar months. Requires a manual
                      clear — it exists for the regime dying, which is exactly
                      when an automatic restart would be wrong.

DESIGN: every counter is DERIVED from the closed-trade history in the registry
rather than incremented in a variable. A counter can drift from reality after a
crash, a replay, or a manual DB edit; a derivation cannot. Only the kill-switch
latch is stored, because "a human looked at this and re-armed it" is a fact that
cannot be derived.
"""

from __future__ import annotations

from datetime import datetime, timezone
from collections import OrderedDict

RESET_HOUR = 21          # GFT daily reset, 21:00 UTC


def day_key(ts: str | datetime) -> str:
    """Trading-day label honouring the 21:00 UTC cut."""
    t = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    shifted = t.timestamp() + (24 - RESET_HOUR) * 3600
    return datetime.fromtimestamp(shifted, tz=timezone.utc).date().isoformat()


def month_key(ts: str) -> str:
    return day_key(ts)[:7]


SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_latch (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    killed      INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    tripped_at  TEXT
);
INSERT OR IGNORE INTO risk_latch (id, killed) VALUES (1, 0);
"""


class RiskState:
    def __init__(self, registry, cfg: dict):
        self.reg = registry
        live = cfg["live"]
        prof = live["risk_profiles"][live["account_profile"]]
        self.rungs: list[float] = list(prof["risk_ladder_pct"])
        self.consec_stop: int = int(cfg["account"].get("daily_consec_loss_stop", 2))
        ks = live.get("kill_switch", {}) or {}
        self.ks_trades = int(ks.get("rolling_trades", 60))
        self.ks_floor = float(ks.get("rolling_r_floor", -10))
        self.ks_months = int(ks.get("max_consec_losing_months", 2))
        self.reg.db.executescript(SCHEMA)
        self.reg.db.commit()

    # ------------------------------------------------------------- history

    def _closed(self) -> list[dict]:
        """Closed trades with a realized R, oldest first — the source of truth."""
        rows = self.reg.db.execute(
            "SELECT msg_id, posted_at, closed_at, realized_r FROM trades "
            "WHERE state='CLOSED' AND realized_r IS NOT NULL ORDER BY msg_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def _when(self, t: dict) -> str:
        return t["closed_at"] or t["posted_at"] or ""

    # --------------------------------------------------------------- ladder

    def rung_index(self) -> int:
        """Consecutive losses since the last win, capped at the bottom rung."""
        streak = 0
        for t in reversed(self._closed()):
            if t["realized_r"] > 0:
                break
            streak += 1
        return min(streak, len(self.rungs) - 1)

    def current_risk_pct(self) -> float:
        return self.rungs[self.rung_index()]

    # ----------------------------------------------------------- daily stop

    def day_consec_losses(self, now: str | None = None) -> int:
        """Consecutive losses inside the CURRENT trading day (a win resets)."""
        today = day_key(now or datetime.now(timezone.utc).isoformat())
        streak = 0
        for t in reversed(self._closed()):
            when = self._when(t)
            if not when or day_key(when) != today:
                break                       # left today's window
            if t["realized_r"] > 0:
                break
            streak += 1
        return streak

    def daily_stop_hit(self, now: str | None = None) -> bool:
        return self.day_consec_losses(now) >= self.consec_stop

    # ---------------------------------------------------------- kill switch

    def rolling_r(self) -> float:
        closed = self._closed()[-self.ks_trades:]
        return sum(t["realized_r"] for t in closed)

    def consec_losing_months(self) -> int:
        by_month: "OrderedDict[str, float]" = OrderedDict()
        for t in self._closed():
            when = self._when(t)
            if when:
                by_month[month_key(when)] = by_month.get(month_key(when), 0.0) + t["realized_r"]
        streak = 0
        for m in reversed(list(by_month)):
            if by_month[m] < 0:
                streak += 1
            else:
                break
        return streak

    def latched(self) -> tuple[bool, str | None]:
        r = self.reg.db.execute("SELECT killed, reason FROM risk_latch WHERE id=1").fetchone()
        return (bool(r["killed"]), r["reason"]) if r else (False, None)

    def trip(self, reason: str):
        self.reg.db.execute(
            "UPDATE risk_latch SET killed=1, reason=?, tripped_at=? WHERE id=1",
            (reason, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.reg.db.commit()
        self.reg.log("KILL_SWITCH_TRIPPED", None, reason=reason)

    def clear(self, note: str = "manual"):
        """Manual re-arm. Deliberately not automatic."""
        self.reg.db.execute(
            "UPDATE risk_latch SET killed=0, reason=NULL, tripped_at=NULL WHERE id=1")
        self.reg.db.commit()
        self.reg.log("KILL_SWITCH_CLEARED", None, note=note)

    def check_kill_switch(self) -> tuple[bool, str | None]:
        """Evaluate and latch if breached. Returns (killed, reason)."""
        killed, reason = self.latched()
        if killed:
            return True, reason
        closed = self._closed()
        if len(closed) >= self.ks_trades:
            r = self.rolling_r()
            if r <= self.ks_floor:
                msg = f"rolling {self.ks_trades}-trade R = {r:+.1f} <= {self.ks_floor}"
                self.trip(msg)
                return True, msg
        m = self.consec_losing_months()
        if m >= self.ks_months:
            msg = f"{m} consecutive losing months"
            self.trip(msg)
            return True, msg
        return False, None

    # ------------------------------------------------------------ the gate

    def can_trade(self, now: str | None = None) -> tuple[bool, str]:
        """One call the pipeline makes before accepting any entry."""
        killed, reason = self.check_kill_switch()
        if killed:
            return False, f"KILL SWITCH: {reason}"
        if self.daily_stop_hit(now):
            return False, (f"daily stop: {self.day_consec_losses(now)} consecutive "
                           f"losses today (limit {self.consec_stop})")
        return True, "ok"

    def snapshot(self, now: str | None = None) -> dict:
        killed, reason = self.latched()
        closed = self._closed()
        return {
            "closed_trades": len(closed),
            "rung_index": self.rung_index(),
            "risk_pct": self.current_risk_pct(),
            "day_consec_losses": self.day_consec_losses(now),
            "daily_stop_hit": self.daily_stop_hit(now),
            "rolling_r": round(self.rolling_r(), 2),
            "consec_losing_months": self.consec_losing_months(),
            "killed": killed,
            "kill_reason": reason,
        }
