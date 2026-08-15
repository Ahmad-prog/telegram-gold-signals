"""
Registry + pipeline tests — NO API CALLS (a stub classifier stands in for Gemini).

Covers the state machine, msg_id correlation, the strategy gates, lot sizing,
and the always-attached SL/TP safety net.

    python3 tests/test_pipeline.py     -> "N passed, 0 failed"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from trade_registry import TradeRegistry, lots_for
from pipeline import handle_message, resolve_sl_tp

CFG = yaml.safe_load((ROOT / "parameters.yml").read_text())


class Sig:
    """Stands in for a Gemini response."""
    def __init__(self, kind="entry", side="sell", entry_low=4051.0, entry_high=4057.0,
                 sl=4062.0, tps=(4046.0,), tp_unit="price", is_addon=False,
                 action="open", new_sl=0.0, new_tp=0.0, reason="stub", confidence=0.9):
        self.kind, self.side = kind, side
        self.entry_low, self.entry_high = entry_low, entry_high
        self.sl, self.tps, self.tp_unit = sl, list(tps), tp_unit
        self.is_addon, self.action = is_addon, action
        self.new_sl, self.new_tp = new_sl, new_tp
        self.reason, self.confidence = reason, confidence


RAW = "XAUUSD SELL 4051 - 4057\nSL : 4062\nTP : 4046"


def msg(mid=1001, text=RAW, **kw):
    return {"msg_id": mid, "channel": "gsn", "date": "2026-08-14T09:08:00+00:00",
            "text": text, **kw}


def fresh():
    return TradeRegistry(":memory:")


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn)); return fn
    return deco


# ------------------------------------------------------------------ registry

@case("registry: entry -> PENDING -> OPEN -> CLOSED")
def _():
    r = fresh()
    r.register_entry(1, "gsn", "t", "sell", 4062, 4046)
    a = r.get(1)["state"]
    r.mark_open(1, ticket=555, fill_price=4054, lots=1.2)
    b = r.get(1)["state"]
    r.mark_closed(1, "tp_hit", 1.0)
    c = r.get(1)
    return (a, b, c["state"], c["ticket"]) == ("PENDING", "OPEN", "CLOSED", 555), c


@case("registry: duplicate delivery does not double-register")
def _():
    r = fresh()
    r.register_entry(1, "gsn", "t", "sell", 4062, 4046)
    r.register_entry(1, "gsn", "t", "sell", 9999, 8888)   # replayed message
    return r.get(1)["sl"] == 4062 and len(r.open_trades()) == 1, r.get(1)


@case("registry: has_open gates the one-trade rule")
def _():
    r = fresh()
    empty = r.has_open()
    r.register_entry(1, "gsn", "t", "sell", 4062, 4046)
    busy = r.has_open()
    r.mark_closed(1, "sl_hit")
    return (empty, busy, r.has_open()) == (False, True, False), None


@case("correlate: edit of a known msg_id")
def _():
    r = fresh()
    r.register_entry(77, "gsn", "t", "sell", 4062, 4046)
    t, how = r.correlate(msg_id=77, is_edit=True)
    return t and t["msg_id"] == 77 and how == "edit_of_known_msg", how


@case("correlate: reply_to points at the parent trade")
def _():
    r = fresh()
    r.register_entry(77, "gsn", "t", "sell", 4062, 4046)
    t, how = r.correlate(msg_id=99, reply_to=77)
    return t and t["msg_id"] == 77 and how == "reply_to_msg_id", how


@case("correlate: unlinked update falls back to the sole open trade")
def _():
    r = fresh()
    r.register_entry(77, "gsn", "t", "sell", 4062, 4046)
    t, how = r.correlate(msg_id=123)
    return t and t["msg_id"] == 77 and how == "sole_open_trade", how


@case("correlate: nothing open -> no match, never guesses")
def _():
    r = fresh()
    t, how = r.correlate(msg_id=123)
    return t is None and how == "no_match", how


@case("registry: update on a CLOSED trade is journalled, not applied")
def _():
    r = fresh()
    r.register_entry(1, "gsn", "t", "sell", 4062, 4046)
    r.mark_closed(1, "tp_hit")
    r.apply_update(1, new_sl=4055, by_msg=2)
    ev = [e["event"] for e in r.events(1)]
    return r.get(1)["sl"] == 4062 and "update_on_closed_ignored" in ev, ev


# ------------------------------------------------------------------ sizing

@case("sizing: wider stop buys fewer lots, same dollar risk")
def _():
    a = lots_for(1.75, 100_000, 60)
    b = lots_for(1.75, 100_000, 120)
    la, lb = a * 60 * 10, b * 120 * 10
    return abs(la - lb) < 25 and a > b, (a, b, la, lb)


@case("sizing: below broker minimum returns 0 (caller skips)")
def _():
    return lots_for(0.01, 500, 120) == 0.0, lots_for(0.01, 500, 120)


# ------------------------------------------------------- SL/TP safety net

@case("safety net: signal values used when present")
def _():
    sl, tp, ss, ts = resolve_sl_tp("sell", 4054.0, Sig(), CFG)
    return (sl, tp, ss, ts) == (4062.0, 4046.0, "signal", "signal"), (sl, tp, ss, ts)


@case("safety net: defaults fill in when the signal has none")
def _():
    sl, tp, ss, ts = resolve_sl_tp("sell", 4054.0, Sig(sl=0.0, tps=(), tp_unit="none"), CFG)
    # sell: SL 90p above entry, TP 50p below
    return (sl, tp, ss, ts) == (4063.0, 4049.0, "default", "default"), (sl, tp, ss, ts)


@case("safety net: buy side flips the default directions")
def _():
    sl, tp, _, _ = resolve_sl_tp("buy", 4054.0, Sig(side="buy", sl=0.0, tps=(), tp_unit="none"), CFG)
    return (sl, tp) == (4045.0, 4059.0), (sl, tp)


@case("safety net: pip-unit TP converts to an absolute price")
def _():
    sl, tp, _, ts = resolve_sl_tp("sell", 4054.0, Sig(tps=(50.0, 100.0), tp_unit="pips"), CFG)
    return tp == 4049.0 and ts == "signal", (tp, ts)


# ----------------------------------------------------------------- pipeline

@case("pipeline: clean sell is taken and registered")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: Sig(), live_price=4054.0)
    return d["decision"] == "take" and r.get(1001)["state"] == "PENDING", d


@case("pipeline: buy is skipped (sell-only strategy)")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: Sig(side="buy", sl=4046.0, tps=(4062.0,)),
                       live_price=4054.0)
    return d["decision"] == "skip" and "sell-only" in d["reason"], d


@case("pipeline: add-on is skipped (fresh-only)")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: Sig(is_addon=True), live_price=4054.0)
    return d["decision"] == "skip" and "fresh-only" in d["reason"], d


@case("pipeline: second signal skipped while one is open")
def _():
    r = fresh()
    handle_message(msg(1), r, CFG, lambda t: Sig(), live_price=4054.0)
    d = handle_message(msg(2), r, CFG, lambda t: Sig(), live_price=4054.0)
    return d["decision"] == "skip" and "already open" in d["reason"], d


@case("pipeline: no-SL signal skipped while the flag is off")
def _():
    r = fresh()
    d = handle_message(msg(text="Gold Sell Now @ 4051\nSl: in LiveTrade"), r, CFG,
                       lambda t: Sig(sl=0.0, tps=(), tp_unit="none"), live_price=4054.0)
    return d["decision"] == "skip" and "no SL" in d["reason"], d


@case("pipeline: no-SL signal taken with default when the flag is on")
def _():
    r = fresh()
    cfg = yaml.safe_load((ROOT / "parameters.yml").read_text())
    cfg["live"]["allow_default_sl_entry"] = True
    d = handle_message(msg(text="Gold Sell Now @ 4051\nSl: in LiveTrade"), r, cfg,
                       lambda t: Sig(sl=0.0, tps=(), tp_unit="none"), live_price=4054.0)
    return d["decision"] == "take" and r.get(1001)["sl_source"] == "default", d


@case("pipeline: regex/classifier side disagreement never trades")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: Sig(), live_price=4054.0,
                       regex_sig={"side": "buy", "sl": 4062.0})
    return d["decision"] == "skip" and d.get("conflict") is True, d


@case("pipeline: SL outside the 40-120p band is skipped")
def _():
    r = fresh()
    raw = "XAUUSD SELL 4051 - 4057\nSL : 4200\nTP : 4046"
    d = handle_message(msg(text=raw), r, CFG,
                       lambda t: Sig(sl=4200.0), live_price=4054.0)
    return d["decision"] == "skip" and "band" in d["reason"], d


@case("pipeline: hallucinated SL is caught by the shared guardrail")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: Sig(sl=4059.0), live_price=4054.0)
    return d["decision"] == "skip" and "guardrail" in d["reason"], d


@case("pipeline: noise is ignored, nothing registered")
def _():
    r = fresh()
    d = handle_message(msg(text="Sell Gold on TOP 🤑 +70PIPS TP HIT ✅"), r, CFG,
                       lambda t: Sig(kind="noise", action="none"))
    return d["decision"] == "ignore" and not r.open_trades(), d


def cfg_following():
    """Config with provider updates enabled (non-default)."""
    c = yaml.safe_load((ROOT / "parameters.yml").read_text())
    c["live"]["follow_provider_updates"] = True
    return c


@case("pipeline: DEFAULT — provider update is logged but NOT acted on")
def _():
    r = fresh()
    handle_message(msg(1), r, CFG, lambda t: Sig(), live_price=4054.0)
    d = handle_message(msg(2, text="Move SL to 4055"), r, CFG,
                       lambda t: Sig(kind="update", action="modify_sl", new_sl=4055.0))
    # stop unchanged, decision recorded, trade still open — our exits are our own
    return (d["decision"] == "ignore" and r.get(1)["sl"] == 4062.0
            and r.get(1)["state"] == "PENDING"), (d, r.get(1)["sl"])


@case("pipeline: update moves the stop when following is enabled")
def _():
    r = fresh()
    c = cfg_following()
    handle_message(msg(1), r, c, lambda t: Sig(), live_price=4054.0)
    d = handle_message(msg(2, text="Move SL to 4055"), r, c,
                       lambda t: Sig(kind="update", action="modify_sl", new_sl=4055.0))
    return d["decision"] == "modify" and r.get(1)["sl"] == 4055.0, (d, r.get(1)["sl"])


@case("pipeline: 'close now' closes the trade when following is enabled")
def _():
    r = fresh()
    c = cfg_following()
    handle_message(msg(1), r, c, lambda t: Sig(), live_price=4054.0)
    d = handle_message(msg(2, text="close gold now"), r, c,
                       lambda t: Sig(kind="update", action="close_all"))
    return d["decision"] == "close" and r.get(1)["state"] == "CLOSED", d


@case("pipeline: update with nothing open raises an alert, never trades")
def _():
    r = fresh()
    d = handle_message(msg(2, text="move sl to be"), r, CFG,
                       lambda t: Sig(kind="update", action="modify_sl", new_sl=4055.0))
    return d["decision"] == "alert" and not r.open_trades(), d


@case("pipeline: classifier failure skips, never guesses")
def _():
    r = fresh()
    d = handle_message(msg(), r, CFG, lambda t: None)
    return d["decision"] == "skip", d


@case("pipeline: every decision is journalled")
def _():
    r = fresh()
    handle_message(msg(1), r, CFG, lambda t: Sig(), live_price=4054.0)
    ev = [e["event"] for e in r.events()]
    return any(e.startswith("decision:") for e in ev) and "registered" in ev, ev


def main():
    passed = failed = 0
    for name, fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:
            import traceback
            ok, detail = False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n      -> {detail}"))
        passed += bool(ok); failed += (not ok)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
