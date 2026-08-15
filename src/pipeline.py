"""
The live pipeline — one function that turns a Telegram post into a decision.

    handle_message(msg, registry, cfg, classify_fn, live_price) -> decision dict

Implements the settled spec in docs/design-doc.md:

  Gemini classifies -> ENTRY / UPDATE / NOISE
  ENTRY  : regex cross-check -> shared guardrails -> strategy gates
           (sell-only, fresh-only, SL band, one-trade-at-a-time)
           -> size lots -> register PENDING -> caller places the order
  UPDATE : correlate to a trade (edit / reply_to / sole open) -> amend SL/TP,
           or close. Never flips side, never hedges.
  NOISE  : dropped and journalled.

Every path returns a decision dict and writes a journal row, so a paper run and
a live run produce the same auditable trail.

The SL/TP that go on the order are ALWAYS numeric: the signal's own values when
present, otherwise the configured defaults (90p / 50p medians). A position can
never run naked, even if Telegram or this process dies.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_parser import validate_extraction          # shared guardrails
from trade_registry import TradeRegistry, lots_for  # noqa: F401  (re-exported)
from risk_state import RiskState

PIP = 0.10


def _defaults(cfg: dict) -> tuple[float, float]:
    d = cfg["live"].get("defaults", {})
    return float(d.get("sl_pips", 90)), float(d.get("tp_pips", 50))


def resolve_sl_tp(side: str, entry: float, sig, cfg: dict) -> tuple[float, float, str, str]:
    """Always return numeric (sl, tp) plus where each came from.

    The default is a SAFETY NET, not a way to trade signals that lack a stop:
    whether a no-SL signal may be entered at all is governed by
    live.allow_default_sl_entry (default false, matching the backtest).
    """
    sl_pips, tp_pips = _defaults(cfg)
    sign = 1 if side == "buy" else -1

    sl, sl_src = getattr(sig, "sl", None), "signal"
    if not sl:
        sl, sl_src = entry - sign * sl_pips * PIP, "default"

    tps = getattr(sig, "tps", None) or []
    unit = getattr(sig, "tp_unit", None)
    tp, tp_src = None, "signal"
    if tps:
        if unit == "pips":
            tp = entry + sign * min(tps) * PIP
        else:                                   # nearest target on the winning side
            good = [t for t in tps if (t > entry) == (side == "buy")]
            tp = (min(good) if side == "buy" else max(good)) if good else None
    if not tp:
        tp, tp_src = entry + sign * tp_pips * PIP, "default"

    return round(sl, 2), round(tp, 2), sl_src, tp_src


def handle_message(msg: dict, reg: TradeRegistry, cfg: dict, classify_fn,
                   live_price: float | None = None, regex_sig: dict | None = None,
                   equity: float = 100_000.0, risk_pct: float | None = None,
                   risk: RiskState | None = None) -> dict:
    """msg: {msg_id, channel, date, text, reply_to (opt), is_edit (opt)}"""
    live = cfg["live"]
    mid = msg["msg_id"]
    risk = risk if risk is not None else RiskState(reg, cfg)

    def out(decision, reason, **kw):
        d = {"msg_id": mid, "decision": decision, "reason": reason, **kw}
        reg.log(f"decision:{decision}", mid, reason=reason, **kw)
        return d

    sig = classify_fn(msg["text"])
    if sig is None:
        return out("skip", "classifier unavailable")

    kind = sig.kind

    # ---------------------------------------------------------------- NOISE
    if kind == "noise":
        return out("ignore", sig.reason, kind="noise")

    # --------------------------------------------------------------- UPDATE
    if kind == "update":
        trade, how = reg.correlate(mid, msg.get("reply_to"), msg.get("is_edit", False))
        if trade is None:
            return out("alert", f"update could not be correlated ({how})", action=sig.action)
        if trade["state"] == "CLOSED":
            reg.apply_update(trade["msg_id"], by_msg=mid)   # journals, changes nothing
            return out("ignore", "update for an already-closed trade",
                       target=trade["msg_id"], via=how)

        act = sig.action

        # Acting on the provider's exit advice is a STRATEGY CHANGE, not a
        # feature: the backtest that produced our numbers used Strategy A
        # (enter at market, exit at TP1 or SL, no modifications). Honouring
        # "move SL to BE" changes the exit distribution and also weakens the
        # GFT position that the exit logic is our own. Default OFF: classify
        # and journal every update, act on none of it, and backtest the
        # "what if we followed them" question separately.
        if not live.get("follow_provider_updates", False):
            return out("ignore", f"provider update '{act}' logged, not acted on "
                                 f"(follow_provider_updates=false)",
                       target=trade["msg_id"], via=how, action=act)

        if act in ("close_all", "cancel", "close_partial"):
            reg.mark_closed(trade["msg_id"], f"provider:{act}")
            return out("close", f"provider said {act}", target=trade["msg_id"], via=how)
        if act in ("modify_sl", "modify_tp"):
            new_sl = getattr(sig, "new_sl", 0) or None
            new_tp = getattr(sig, "new_tp", 0) or None
            if not new_sl and not new_tp:
                return out("alert", "modify update carried no numeric level",
                           target=trade["msg_id"], via=how)
            reg.apply_update(trade["msg_id"], new_sl, new_tp, by_msg=mid)
            return out("modify", f"{act} -> sl={new_sl} tp={new_tp}",
                       target=trade["msg_id"], via=how)
        return out("ignore", f"update action '{act}' needs no broker call",
                   target=trade["msg_id"], via=how)

    # ---------------------------------------------------------------- ENTRY
    side = sig.side if sig.side in ("buy", "sell") else None
    if side is None:
        return out("skip", "entry without a usable side")

    # strategy gates (locked spec) --------------------------------------
    if live.get("side") == "sell_only" and side != "sell":
        reg.mark_skipped(mid, "buy signal (sell-only strategy)")
        return out("skip", "buy signal — strategy is sell-only")
    if live.get("fresh_only") and getattr(sig, "is_addon", False):
        reg.mark_skipped(mid, "add-on signal (fresh-only)")
        return out("skip", "add-on / re-entry — strategy is fresh-only")
    if reg.has_open():
        return out("skip", "a trade is already open (one at a time)")

    # kill switch + daily consecutive-loss stop, evaluated from closed history
    allowed, why = risk.can_trade(msg.get("date"))
    if not allowed:
        reg.mark_skipped(mid, why)
        return out("skip", why, risk_block=True)

    entry = live_price if live_price else ((sig.entry_low + sig.entry_high) / 2
                                           if sig.entry_low and sig.entry_high else None)
    if not entry:
        return out("skip", "no entry price and no live price available")

    has_signal_sl = bool(getattr(sig, "sl", None))
    if not has_signal_sl and not live.get("allow_default_sl_entry", False):
        reg.mark_skipped(mid, "no stated SL and default-SL entries are disabled")
        return out("skip", "signal states no SL (default-SL entries disabled)")

    sl, tp, sl_src, tp_src = resolve_sl_tp(side, entry, sig, cfg)
    sl_pips = abs(entry - sl) / PIP

    # guardrails — only meaningful when the signal supplied its own numbers
    if has_signal_sl:
        ok, why = validate_extraction(sig, msg["text"], cfg, live_price)
        if not ok:
            reg.mark_skipped(mid, f"guardrail: {why}")
            return out("skip", f"guardrail rejected: {why}")

    lo, hi = live.get("sl_pips_min", 0), live.get("sl_pips_max", 1e9)
    if not (lo <= sl_pips <= hi):
        reg.mark_skipped(mid, f"SL {sl_pips:.0f}p outside band")
        return out("skip", f"SL {sl_pips:.0f}p outside {lo}-{hi}p band")

    # regex cross-check — disagreement never trades
    if regex_sig:
        if regex_sig.get("side") != side:
            reg.mark_skipped(mid, "regex/LLM side disagreement")
            return out("skip", "DISAGREE on side (regex vs classifier)", conflict=True)
        r_sl = regex_sig.get("sl")
        if has_signal_sl and r_sl and abs(r_sl - sig.sl) > 0.01:
            reg.mark_skipped(mid, "regex/LLM SL disagreement")
            return out("skip", "DISAGREE on SL (regex vs classifier)", conflict=True)

    # risk ladder: rung is derived from consecutive losses since the last win
    rung = risk_pct if risk_pct is not None else risk.current_risk_pct()
    lots = lots_for(rung, equity, sl_pips)
    if lots <= 0:
        return out("skip", f"computed lots below broker minimum (SL {sl_pips:.0f}p)")

    reg.register_entry(mid, msg.get("channel", "?"), msg.get("date", ""), side,
                       sl, tp, sig.entry_low or None, sig.entry_high or None,
                       sl_src, tp_src)
    return out("take", f"entry accepted ({sl_src} SL / {tp_src} TP)",
               side=side, entry=round(entry, 2), sl=sl, tp=tp,
               sl_pips=round(sl_pips), lots=lots, risk_pct=rung,
               rung_index=risk.rung_index())
