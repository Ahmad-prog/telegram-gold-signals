"""
Risk state machine tests — ladder, daily stop, kill switch. NO API CALLS.

    python3 tests/test_risk_state.py    -> "N passed, 0 failed"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
from trade_registry import TradeRegistry
from risk_state import RiskState, day_key

CFG = yaml.safe_load((ROOT / "parameters.yml").read_text())

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn)); return fn
    return deco


def build(results, base_day="2026-08-14"):
    """results: list of (r_multiple, day_string). Creates closed trades in order."""
    reg = TradeRegistry(":memory:")
    for i, item in enumerate(results, start=1):
        r, day = item if isinstance(item, tuple) else (item, base_day)
        ts = f"{day}T10:0{i % 10}:00+00:00"
        reg.register_entry(i, "gsn", ts, "sell", 4062, 4046)
        reg.mark_open(i, ticket=1000 + i, fill_price=4054, lots=1.0)
        reg.db.execute("UPDATE trades SET state='CLOSED', closed_at=?, realized_r=? "
                       "WHERE msg_id=?", (ts, r, i))
    reg.db.commit()
    return reg, RiskState(reg, CFG)


# ------------------------------------------------------------------- ladder

@case("ladder: starts at the top rung with no history")
def _():
    _, rs = build([])
    return rs.current_risk_pct() == 1.75 and rs.rung_index() == 0, rs.snapshot()


@case("ladder: one loss steps down to rung 1")
def _():
    _, rs = build([-1.0])
    return rs.current_risk_pct() == 1.25, rs.snapshot()


@case("ladder: two losses step down to rung 2")
def _():
    _, rs = build([-1.0, -1.0])
    return rs.current_risk_pct() == 1.0, rs.snapshot()


@case("ladder: three losses stay at the bottom rung (no rung 3)")
def _():
    _, rs = build([-1.0, -1.0, -1.0, -1.0])
    return rs.current_risk_pct() == 1.0 and rs.rung_index() == 2, rs.snapshot()


@case("ladder: any win resets to the top rung")
def _():
    _, rs = build([-1.0, -1.0, +0.5])
    return rs.current_risk_pct() == 1.75 and rs.rung_index() == 0, rs.snapshot()


@case("ladder: loss AFTER a win steps down one, not from the old streak")
def _():
    _, rs = build([-1.0, -1.0, +0.5, -1.0])
    return rs.current_risk_pct() == 1.25, rs.snapshot()


@case("ladder: persists across days (not a daily counter)")
def _():
    _, rs = build([(-1.0, "2026-08-12"), (-1.0, "2026-08-13")])
    return rs.current_risk_pct() == 1.0, rs.snapshot()


# --------------------------------------------------------------- daily stop

@case("daily stop: one loss today does not stop trading")
def _():
    _, rs = build([(-1.0, "2026-08-14")])
    ok, why = rs.can_trade("2026-08-14T12:00:00+00:00")
    return ok is True, why


@case("daily stop: two consecutive losses today stops trading")
def _():
    _, rs = build([(-1.0, "2026-08-14"), (-1.0, "2026-08-14")])
    ok, why = rs.can_trade("2026-08-14T12:00:00+00:00")
    return ok is False and "daily stop" in why, why


@case("daily stop: a win between losses resets the CONSEC streak")
def _():
    # the consec counter resets (that is this rule's job) even though the
    # separate daily-loss cap may still block — they are independent guards
    _, rs = build([(-1.0, "2026-08-14"), (+0.5, "2026-08-14"), (-1.0, "2026-08-14")])
    now = "2026-08-14T12:00:00+00:00"
    return (rs.day_consec_losses(now) == 1 and not rs.daily_stop_hit(now)), rs.snapshot(now)


@case("daily stop: yesterday's losses do not block today")
def _():
    _, rs = build([(-1.0, "2026-08-13"), (-1.0, "2026-08-13")])
    ok, why = rs.can_trade("2026-08-14T12:00:00+00:00")
    return ok is True, why


@case("daily stop: the day cuts at 21:00 UTC, not midnight")
def _():
    # 20:59 UTC is still the previous trading day; 21:01 starts the next
    before = day_key("2026-08-14T20:59:00+00:00")
    after = day_key("2026-08-14T21:01:00+00:00")
    return before != after, (before, after)


@case("daily stop: two losses just before 21:00 do not block after the reset")
def _():
    _, rs = build([(-1.0, "x"), (-1.0, "x")])
    # force both closes to 20:30 UTC on the 14th
    reg = rs.reg
    reg.db.execute("UPDATE trades SET closed_at='2026-08-14T20:30:00+00:00'")
    reg.db.commit()
    blocked, _ = rs.can_trade("2026-08-14T20:45:00+00:00")   # same trading day
    freed, _ = rs.can_trade("2026-08-14T21:30:00+00:00")     # after the reset
    return blocked is False and freed is True, (blocked, freed)


# -------------------------------------------------------------- kill switch

@case("kill switch: quiet when the rolling window is not yet full")
def _():
    _, rs = build([-1.0] * 10)             # -10R but only 10 trades
    killed, _ = rs.check_kill_switch()
    return killed is False, rs.snapshot()


@case("kill switch: trips when rolling-60 R <= -10R")
def _():
    _, rs = build([-0.2] * 60)             # -12R over 60 trades
    killed, why = rs.check_kill_switch()
    return killed is True and "rolling" in why, why


@case("kill switch: does NOT trip when the 60-trade window is healthy")
def _():
    _, rs = build([+0.1] * 60)
    killed, _ = rs.check_kill_switch()
    return killed is False, rs.snapshot()


@case("kill switch: trips on 2 consecutive losing months")
def _():
    _, rs = build([(-1.0, "2026-06-10"), (-1.0, "2026-07-10")])
    killed, why = rs.check_kill_switch()
    return killed is True and "month" in why, why


@case("kill switch: a profitable month breaks the losing streak")
def _():
    _, rs = build([(-1.0, "2026-05-10"), (+2.0, "2026-06-10"), (-1.0, "2026-07-10")])
    killed, _ = rs.check_kill_switch()
    return killed is False, rs.snapshot()


@case("kill switch: LATCHES — stays tripped even after results improve")
def _():
    reg, rs = build([(-1.0, "2026-06-10"), (-1.0, "2026-07-10")])
    rs.check_kill_switch()                       # trips
    reg.register_entry(99, "gsn", "2026-08-14T10:00:00+00:00", "sell", 4062, 4046)
    reg.mark_open(99, 9999, 4054, 1.0)
    reg.db.execute("UPDATE trades SET state='CLOSED', closed_at='2026-08-14T10:00:00+00:00',"
                   " realized_r=5.0 WHERE msg_id=99")
    reg.db.commit()
    killed, _ = rs.check_kill_switch()
    return killed is True, "latch must survive a good trade"


@case("kill switch: manual clear re-arms it")
def _():
    _, rs = build([(-1.0, "2026-06-10"), (-1.0, "2026-07-10")])
    rs.check_kill_switch()
    rs.clear("reviewed by user")
    killed, _ = rs.latched()
    return killed is False, "clear() must re-arm"


@case("kill switch: blocks can_trade while latched")
def _():
    _, rs = build([(-1.0, "2026-06-10"), (-1.0, "2026-07-10")])
    ok, why = rs.can_trade("2026-08-14T12:00:00+00:00")
    return ok is False and "KILL SWITCH" in why, why


# ----------------------------------------------------------- persistence

@case("state survives a restart (derived from the DB, not memory)")
def _():
    import tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    reg = TradeRegistry(path)
    for i, r in enumerate([-1.0, -1.0], start=1):
        ts = f"2026-08-14T10:0{i}:00+00:00"
        reg.register_entry(i, "gsn", ts, "sell", 4062, 4046)
        reg.mark_open(i, 1000 + i, 4054, 1.0)
        reg.db.execute("UPDATE trades SET state='CLOSED', closed_at=?, realized_r=? "
                       "WHERE msg_id=?", (ts, r, i))
    reg.db.commit()
    RiskState(reg, CFG).check_kill_switch()
    reg.db.close()

    reg2 = TradeRegistry(path)               # fresh process
    rs2 = RiskState(reg2, CFG)
    return rs2.current_risk_pct() == 1.0 and rs2.daily_stop_hit("2026-08-14T12:00:00+00:00"), \
        rs2.snapshot()


# ---------------------------------------------------------- pipeline gate

@case("pipeline: risk block stops a signal that would otherwise be taken")
def _():
    from pipeline import handle_message

    class Sig:
        kind, side = "entry", "sell"
        entry_low, entry_high = 4051.0, 4057.0
        sl, tps, tp_unit = 4062.0, [4046.0], "price"
        is_addon, action = False, "open"
        new_sl = new_tp = 0.0
        reason, confidence = "stub", 0.9

    reg, rs = build([(-1.0, "2026-08-14"), (-1.0, "2026-08-14")])
    d = handle_message(
        {"msg_id": 500, "channel": "gsn", "date": "2026-08-14T12:00:00+00:00",
         "text": "XAUUSD SELL 4051 - 4057\nSL : 4062\nTP : 4046"},
        reg, CFG, lambda t: Sig(), live_price=4054.0, risk=rs)
    return d["decision"] == "skip" and d.get("risk_block") is True, d


@case("pipeline: sizes from the ladder rung, not always the top")
def _():
    from pipeline import handle_message

    class Sig:
        kind, side = "entry", "sell"
        entry_low, entry_high = 4051.0, 4057.0
        sl, tps, tp_unit = 4062.0, [4046.0], "price"
        is_addon, action = False, "open"
        new_sl = new_tp = 0.0
        reason, confidence = "stub", 0.9

    # one loss yesterday -> rung 1 (1.25%), daily stop not engaged today
    reg, rs = build([(-1.0, "2026-08-13")])
    d = handle_message(
        {"msg_id": 500, "channel": "gsn", "date": "2026-08-14T12:00:00+00:00",
         "text": "XAUUSD SELL 4051 - 4057\nSL : 4062\nTP : 4046"},
        reg, CFG, lambda t: Sig(), live_price=4054.0, risk=rs)
    return d["decision"] == "take" and d["risk_pct"] == 1.25 and d["rung_index"] == 1, d


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


# --------------------------------------------- GFT daily loss cap (compliance)

@case("daily cap: the L,W,L,L day that beats the consec-stop is blocked")
def _():
    # -1.75, +0.6, -1.75, then a 4th trade would push past -3%
    _, rs = build([(-1.0, "2026-08-14"), (+0.5, "2026-08-14"), (-1.0, "2026-08-14")])
    # never 2 losses in a row, so the consec stop is NOT engaged...
    consec_ok = not rs.daily_stop_hit("2026-08-14T15:00:00+00:00")
    # ...but the loss cap must still block
    ok, why = rs.can_trade("2026-08-14T15:00:00+00:00")
    return consec_ok and ok is False and "daily loss cap" in why, (consec_ok, why)


@case("daily cap: a clean winning day is not blocked")
def _():
    _, rs = build([(+1.0, "2026-08-14"), (+0.5, "2026-08-14")])
    ok, _ = rs.can_trade("2026-08-14T15:00:00+00:00")
    return ok is True, rs.snapshot("2026-08-14T15:00:00+00:00")


@case("daily cap: blocks pre-emptively when the NEXT trade could breach")
def _():
    # -1.75 today; one more 1.25% loss would reach -3.0% = the cap
    _, rs = build([(-1.0, "2026-08-14"), (+0.5, "2026-08-14"), (-1.0, "2026-08-14")])
    hit, why = rs.daily_loss_cap_hit("2026-08-14T15:00:00+00:00")
    return hit is True and "could reach" in why, why


@case("daily cap: resets the next trading day")
def _():
    _, rs = build([(-1.0, "2026-08-14"), (+0.5, "2026-08-14"), (-1.0, "2026-08-14")])
    ok, _ = rs.can_trade("2026-08-15T10:00:00+00:00")
    return ok is True, rs.snapshot("2026-08-15T10:00:00+00:00")


@case("daily cap: worst possible day stays inside GFT's -4% limit")
def _():
    # simulate the worst realistic sequence and confirm the cap holds it under 4
    _, rs = build([(-1.0, "2026-08-14"), (+0.5, "2026-08-14"), (-1.0, "2026-08-14")])
    pnl = rs.day_pnl_pct("2026-08-14T15:00:00+00:00")
    blocked, _ = rs.daily_loss_cap_hit("2026-08-14T15:00:00+00:00")
    return blocked and pnl > -4.0, f"day P&L {pnl:.2f}% and further trading blocked"
