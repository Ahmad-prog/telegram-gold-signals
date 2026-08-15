"""
Executor — turns pipeline decisions into broker orders and keeps the registry
in sync with reality.

    ex = Executor(broker, registry, cfg)
    ex.on_decision(decision)     # 'take' -> place; 'close'/'modify' -> amend
    ex.reconcile()               # on startup: DB vs broker truth
    ex.poll()                    # detect SL/TP closes, write realized R

Realized R written on close is what feeds the risk state machine (ladder,
daily stop, kill switch), so the whole risk system is driven by what the broker
actually did, not by what we hoped it would do.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from broker import Broker, PIP


class Executor:
    def __init__(self, broker: Broker, registry, cfg: dict, dry_run: bool = False):
        self.b = broker
        self.reg = registry
        self.cfg = cfg
        self.live = cfg["live"]
        self.dry_run = dry_run

    # ------------------------------------------------------------- guards

    def _spread_ok(self) -> tuple[bool, float]:
        try:
            bid, ask = self.b.price()
            sp = (ask - bid) / PIP
        except Exception:
            return True, 0.0
        return sp <= float(self.live.get("max_spread_pips", 5)), sp

    # ------------------------------------------------------------ actions

    def on_decision(self, d: dict) -> dict:
        if d["decision"] == "take":
            return self._open(d)
        if d["decision"] == "close":
            return self._close(d.get("target"), "provider")
        if d["decision"] == "modify":
            return self._modify(d)
        return {"action": "none", "decision": d["decision"]}

    def _open(self, d: dict) -> dict:
        mid = d["msg_id"]
        ok, sp = self._spread_ok()
        if not ok:
            self.reg.mark_skipped(mid, f"spread {sp:.1f}p over cap")
            self.reg.log("order_skipped_spread", mid, spread=round(sp, 2))
            return {"action": "skip", "reason": f"spread {sp:.1f}p over cap"}

        if self.dry_run:
            self.reg.log("order_dry_run", mid, **{k: d[k] for k in
                         ("side", "lots", "sl", "tp") if k in d})
            return {"action": "dry_run", **d}

        fill = self.b.place(d["side"], d["lots"], d["sl"], d["tp"],
                            comment=f"msg:{mid}")
        if fill is None:
            self.reg.mark_skipped(mid, "broker rejected the order")
            self.reg.log("order_rejected", mid)
            return {"action": "rejected"}

        self.reg.mark_open(mid, fill.ticket, fill.price, fill.lots)
        self.reg.log("order_filled", mid, ticket=fill.ticket,
                     price=fill.price, lots=fill.lots)
        return {"action": "opened", "ticket": fill.ticket, "price": fill.price}

    def _close(self, msg_id, reason) -> dict:
        t = self.reg.get(msg_id) if msg_id else None
        if not t or not t["ticket"]:
            return {"action": "none", "reason": "no ticket to close"}
        c = self.b.close(t["ticket"], reason)
        if c is None:
            return {"action": "none", "reason": "broker close failed"}
        self.reg.mark_closed(msg_id, reason, c.r_multiple)
        self.reg.log("order_closed", msg_id, ticket=c.ticket,
                     price=c.price, r=c.r_multiple, reason=reason)
        return {"action": "closed", "r": c.r_multiple}

    def _modify(self, d: dict) -> dict:
        t = self.reg.get(d.get("target"))
        if not t or not t["ticket"]:
            return {"action": "none"}
        ok = self.b.modify(t["ticket"], t["sl"], t["tp"])
        self.reg.log("order_modified", t["msg_id"], ok=ok, sl=t["sl"], tp=t["tp"])
        return {"action": "modified" if ok else "modify_failed"}

    # ------------------------------------------------------------- polling

    def advance_to(self, dt) -> list[dict]:
        """Move a paper broker's clock to dt, recording every SL/TP close.

        Correctness note: never fast-forward past candles while a position is
        open — that skips the stop that should have closed it. PaperBroker
        enforces this; advance_to() is the safe entry point.
        """
        if not hasattr(self.b, "advance_to"):
            return []
        return [self._record(c) for c in self.b.advance_to(dt)]

    def _record(self, c) -> dict:
        msg_id = self._msg_for_ticket(c.ticket)
        if msg_id is not None:
            self.reg.mark_closed(msg_id, c.reason, c.r_multiple)
            self.reg.log("position_closed", msg_id, ticket=c.ticket,
                         reason=c.reason, r=c.r_multiple)
        return {"msg_id": msg_id, "reason": c.reason, "r": c.r_multiple}

    def poll(self) -> list[dict]:
        """Advance the broker one candle and record any SL/TP closes."""
        events = []
        for c in (self.b.step() if hasattr(self.b, "step") else []):
            msg_id = self._msg_for_ticket(c.ticket)
            if msg_id is None:
                continue
            self.reg.mark_closed(msg_id, c.reason, c.r_multiple)
            self.reg.log("position_closed", msg_id, ticket=c.ticket,
                         reason=c.reason, r=c.r_multiple)
            events.append({"msg_id": msg_id, "reason": c.reason, "r": c.r_multiple})
        return events

    def _msg_for_ticket(self, ticket: int) -> int | None:
        r = self.reg.db.execute(
            "SELECT msg_id FROM trades WHERE ticket=?", (ticket,)).fetchone()
        return r["msg_id"] if r else None

    # --------------------------------------------------------- reconciliation

    def reconcile(self) -> dict:
        """Startup truth check: the broker is authoritative, not our DB.

        A crash between order_send and mark_open leaves the DB saying PENDING
        while a real position exists. Fix the DB, never the broker.
        """
        live_tickets = {p["ticket"] for p in self.b.positions()}
        fixed = {"orphan_db_rows": 0, "unknown_broker_positions": 0}

        for t in self.reg.open_trades():
            if t["state"] == "OPEN" and t["ticket"] not in live_tickets:
                # broker says it's gone -> it closed while we were down
                self.reg.mark_closed(t["msg_id"], "closed_while_offline", None)
                self.reg.log("reconcile_closed_offline", t["msg_id"], ticket=t["ticket"])
                fixed["orphan_db_rows"] += 1

        known = {t["ticket"] for t in self.reg.open_trades() if t["ticket"]}
        for p in self.b.positions():
            if p["ticket"] not in known:
                # a real position we have no row for — alert, never auto-close
                self.reg.log("reconcile_unknown_position", None, ticket=p["ticket"],
                             comment=p.get("comment"))
                fixed["unknown_broker_positions"] += 1
        self.reg.log("reconciled", None, **fixed)
        return fixed
