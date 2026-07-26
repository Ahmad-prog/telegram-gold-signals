"""
RIGID engine validation — synthetic candles with KNOWN outcomes.

This proves the backtest engine resolves trades correctly (SL/TP/breakeven/ties)
before we trust any result on real data. Run:

    python validate_engine.py

Exits non-zero if ANY case fails. Costs/slippage set to 0 so expected P&L is
exact and hand-checkable.
"""

import copy
import sys
from datetime import datetime, timezone, timedelta

from engine import simulate

BASE = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)


def C(o, h, l, c, minute):
    """One candle at BASE + minute."""
    return (BASE + timedelta(minutes=minute), o, h, l, c)


def cfg(strategy="C", tie="pessimistic"):
    return {
        "market": {"pip_value": 0.10, "round_trip_cost_pips": 0, "slippage_pips": 0.0},
        "entry": {"mode": "market_next", "zone_touch_max_wait_min": 60},
        "exit": {"strategy": strategy, "tp1_close_fraction": 0.5,
                 "move_to_breakeven": True, "same_candle_tie": tie,
                 "ladder_rungs": 4, "no_time_exit": True, "max_hold_minutes": 0},
        "risk": {"account_size": 100000, "risk_per_trade_pct": 0.5},
        "filters": {"max_sl_dollars": 40, "skip_no_tp": True, "skip_addons": False},
    }


def sig(side, sl, tp_raw, tp_mode="pips", entry_zone=(2000, 2000)):
    return {"date": BASE.isoformat(), "side": side, "sl": sl,
            "entry_low": entry_zone[0], "entry_high": entry_zone[1],
            "tp_mode": tp_mode, "tp_raw": tp_raw, "addon": False}


CASES = []


def case(name, **kw):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# Buy: entry 2000 (candle0 open), SL 1999 (=$1=10p), TP 50/100p -> tp1 2005, tp2 2010
@case("buy: clean TP1 then TP2")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),   # entry candle, no hit
          C(2001, 2005.0, 2001, 2004, 1),     # hits tp1=2005
          C(2004, 2010.0, 2004, 2009, 2)]     # hits tp2=2010
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="TP2", pips=75.0)   # .5*50 + .5*100


@case("buy: clean SL")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2000, 2000.5, 1999.0, 1999.2, 1)]  # low touches SL 1999
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="SL", pips=-10.0)


@case("buy: TP1 then back to breakeven")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1),      # tp1 -> half closed, stop->2000
          C(2004, 2004.5, 2000.0, 2000.5, 2)]  # low hits breakeven 2000
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="BE_after_TP1", pips=25.0)  # .5*50 + .5*0


@case("buy: same-candle SL+TP1 tie -> pessimistic = SL")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2002, 2005.0, 1999.0, 2003, 1)]    # hits BOTH tp1 and SL
    r = simulate(s, cs, 0, cfg(tie="pessimistic"))
    return r, dict(outcome="SL", pips=-10.0)


@case("buy: same-candle SL+TP1 tie -> optimistic = takes TP1")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2002, 2005.0, 1999.0, 2003, 1),    # tie: optimistic takes tp1, stop->BE
          C(2004, 2010.0, 2004, 2009, 2)]      # then tp2
    r = simulate(s, cs, 0, cfg(tie="optimistic"))
    return r, dict(outcome="TP2", pips=75.0)


@case("buy: gap straight through SL resolves as SL")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(1995, 1996, 1990, 1992, 1)]        # whole candle below SL
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="SL")               # (price exits at stop level by design)


@case("sell: clean TP1 then TP2")
def _():
    s = sig("sell", 2001.0, [50, 100])         # tp1 1995, tp2 1990
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(1999, 1999, 1995.0, 1996, 1),      # low hits tp1 1995
          C(1996, 1996, 1990.0, 1991, 2)]      # low hits tp2 1990
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="TP2", pips=75.0)


@case("strategy A: full close at TP1")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1)]      # tp1 -> full close (strategy A)
    r = simulate(s, cs, 0, cfg(strategy="A"))
    return r, dict(outcome="TP1", pips=50.0)


@case("strategy B: SL before TP2 = full loss")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1),      # tp1 reached but strat B ignores it
          C(2002, 2002.5, 1999.0, 1999.5, 2)]  # SL hit (still full size)
    r = simulate(s, cs, 0, cfg(strategy="B"))
    return r, dict(outcome="SL", pips=-10.0)


@case("strategy D: ladder runs to final TP4")
def _():
    s = sig("buy", 1999.0, [50, 100])          # tp1 2005, tp2 2010, step 5 -> 2015,2020
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1),       # TP1 -> bank half
          C(2004, 2010.0, 2004, 2009, 2),       # TP2 -> SL->2005
          C(2009, 2015.0, 2009, 2014, 3),       # TP3 -> SL->2010
          C(2014, 2020.0, 2014, 2019, 4)]       # TP4 -> close rest
    r = simulate(s, cs, 0, cfg(strategy="D"))
    return r, dict(outcome="TP4_final", pips=125.0)  # .5*50 + .5*200


@case("strategy D: trailed out at TP1 floor")
def _():
    s = sig("buy", 1999.0, [50, 100])
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1),       # TP1 -> bank half
          C(2004, 2010.0, 2004, 2009, 2),       # TP2 -> SL trails to 2005
          C(2009, 2009.5, 2005.0, 2005.2, 3)]   # drops to 2005 -> trailed stop
    r = simulate(s, cs, 0, cfg(strategy="D"))
    return r, dict(outcome="trail@2", pips=50.0)  # .5*50 + .5*50


@case("price-mode TP (absolute targets)")
def _():
    s = sig("buy", 1999.0, [2005, 2010], tp_mode="price")
    cs = [C(2000, 2000.2, 1999.8, 2000, 0),
          C(2001, 2005.0, 2001, 2004, 1),
          C(2004, 2010.0, 2004, 2009, 2)]
    r = simulate(s, cs, 0, cfg())
    return r, dict(outcome="TP2", pips=75.0)


def approx(a, b, tol=0.01):
    return a is not None and abs(a - b) <= tol


def main():
    passed = failed = 0
    for name, fn in CASES:
        r, exp = fn()
        ok = r is not None
        detail = ""
        if ok:
            for k, v in exp.items():
                got = r.get(k)
                if isinstance(v, float):
                    if not approx(got, v):
                        ok = False; detail += f" {k}={got}!={v}"
                else:
                    if got != v:
                        ok = False; detail += f" {k}={got}!={v}"
        else:
            detail = " returned None"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{'' if ok else '  ->'+detail}")
        passed += ok; failed += (not ok)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
