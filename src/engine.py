"""
Backtest engine for GARY GOLD TRADER signals — fully config-driven.

Everything tunable lives in parameters.yml. backtest.py and stress_test.py both
import run_backtest() from here, passing a config dict (optionally overridden).

A "trade" is simulated candle-by-candle on 1-min XAUUSD data:
  entry  -> per cfg['entry']['mode']
  manage -> per cfg['exit']['strategy']  (A / B / C)
  costs  -> round_trip_cost_pips (flat, once) + slippage_pips (per fill, adverse)
  exit   -> SL / TP / breakeven; no time cutoff unless configured
Results are reported in pips, R-multiples, and % of account.
"""

import bisect
import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent   # repo root


# ----------------------------- config / data -----------------------------

def load_config(path=None):
    path = path or (HERE / "parameters.yml")
    with open(path) as f:
        return yaml.safe_load(f)


def load_prices(cfg):
    """One continuous time-sorted list of (dt, o, h, l, c)."""
    rows = []
    with (HERE / cfg["paths"]["prices"]).open() as f:
        for r in csv.DictReader(f):
            dt = datetime.fromisoformat(r["datetime_utc"]).replace(tzinfo=timezone.utc)
            rows.append((dt, float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])))
    rows.sort(key=lambda x: x[0])
    return rows


def load_signals(cfg):
    sigs = [json.loads(l) for l in (HERE / cfg["paths"]["signals"]).open(encoding="utf-8")]
    sigs.sort(key=lambda s: s["date"])      # chronological for equity curve
    return sigs


# ----------------------------- single trade -----------------------------

def _targets(side, entry, sig, pip):
    """Return (tp1, tp2) absolute prices from the fill, per signal tp_mode."""
    sign = 1 if side == "buy" else -1
    if sig["tp_mode"] == "pips" and sig["tp_raw"]:
        tps = sorted(sig["tp_raw"])
        tp1 = entry + sign * tps[0] * pip
        tp2 = entry + sign * (tps[1] if len(tps) > 1 else tps[0]) * pip
        return tp1, tp2
    if sig["tp_mode"] == "price" and sig["tp_raw"]:
        ts = sorted(sig["tp_raw"], reverse=(side == "sell"))   # nearest first
        return ts[0], ts[1] if len(ts) > 1 else ts[0]
    return None, None


def simulate(sig, candles, entry_idx, cfg):
    """Simulate one signal. Returns a result dict, or None if not tradeable."""
    m, ex, risk_c, filt = cfg["market"], cfg["exit"], cfg["risk"], cfg["filters"]
    pip = m["pip_value"]
    slip = m["slippage_pips"] * pip
    side = sig["side"]
    long = side == "buy"
    sign = 1 if long else -1

    sl = sig["sl"]
    if sl is None:
        return None
    if filt.get("skip_addons") and sig.get("addon"):
        return None

    # ---- entry fill ----
    mode = cfg["entry"]["mode"]
    if mode == "zone_touch":
        lo_z, hi_z = sig["entry_low"], sig["entry_high"]
        fill_edge = hi_z if long else lo_z          # conservative zone edge
        max_wait = cfg["entry"].get("zone_touch_max_wait_min", 60)
        t0 = candles[entry_idx][0]
        idx = None
        for i in range(entry_idx, len(candles)):
            c = candles[i]
            if (c[0] - t0) > timedelta(minutes=max_wait):
                break
            if c[3] <= hi_z and c[2] >= lo_z:        # candle overlaps the zone
                idx = i
                break
        if idx is None:
            return None                              # never entered the zone
        entry_idx = idx
        entry = fill_edge
    else:                                            # market_next
        entry = candles[entry_idx][1]                # open of next candle
    entry += sign * slip                             # adverse entry slippage

    sl_dist = abs(entry - sl)
    if sl_dist > filt["max_sl_dollars"]:             # parse junk / runaway SL
        return None
    if sl_dist / pip < filt.get("min_sl_pips", 0):   # untradeable tight stop -> R outlier
        return None

    tp1, tp2 = _targets(side, entry, sig, pip)
    if tp1 is None:
        if filt.get("skip_no_tp", True):
            return None
        return None

    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return None

    strat = ex["strategy"]
    tie_pess = ex["same_candle_tie"] == "pessimistic"
    no_time = ex.get("no_time_exit", True)
    max_hold = ex.get("max_hold_minutes", 0)
    t_entry = candles[entry_idx][0]

    def finalize(realized, outcome, exit_dt=None):
        realized -= slip                              # adverse exit slippage
        realized -= m["round_trip_cost_pips"] * pip   # spread+commission, once
        r_mult = realized / risk_dist
        return {
            "date": sig["date"], "side": side, "entry": round(entry, 2),
            "sl": sl, "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "outcome": outcome, "pips": round(realized / pip, 1),
            "R": round(r_mult, 3),
            "pct": round(r_mult * risk_c["risk_per_trade_pct"], 4),
            "addon": sig.get("addon", False),
            "exit_date": exit_dt.isoformat() if exit_dt else None,
        }

    # ---- Strategy D: split at TP1 then TRAIL the runner up a synthesized ladder ----
    if strat == "D":
        return _ladder(sig, candles, entry_idx, cfg, side, long, sign, entry,
                       sl, tp1, tp2, pip, finalize, tie_pess, no_time, max_hold,
                       t_entry, ex)

    # Exits accumulate realized price move (already weighted by fraction).
    realized = 0.0
    stop = sl
    f_tp1 = ex.get("tp1_close_fraction", 0.5)

    # Define behavior per strategy:
    #   A: single target tp1, full size, stop = SL
    #   B: single target tp2, full size, stop = SL
    #   C: tp1 closes f_tp1, stop -> breakeven, remainder targets tp2
    if strat == "A":
        targets = [(tp1, 1.0)]
    elif strat == "B":
        targets = [(tp2, 1.0)]
    else:
        targets = [(tp1, f_tp1), (tp2, 1.0 - f_tp1)]

    ti = 0                       # index into targets
    remaining = 1.0
    half_done = False

    def stop_hit(c):
        return (c[3] <= stop) if long else (c[2] >= stop)

    def tgt_hit(c, level):
        return (c[2] >= level) if long else (c[3] <= level)

    outcome = "data_end"
    exit_dt = candles[-1][0]
    for c in candles[entry_idx:]:
        exit_dt = c[0]
        if not no_time and max_hold and (c[0] - t_entry) > timedelta(minutes=max_hold):
            realized += sign * (c[1] - entry) * remaining
            outcome = "timeout"; break

        level, frac = targets[ti]
        s_hit = stop_hit(c)
        t_hit = tgt_hit(c, level)

        if s_hit and t_hit:                    # same-candle tie
            if tie_pess:
                realized += sign * (stop - entry) * remaining
                outcome = "SL" if not half_done else "BE_after_TP1"; break
            # optimistic -> treat as target hit (fall through to target logic)
            s_hit = False

        if s_hit:
            realized += sign * (stop - entry) * remaining
            outcome = "SL" if not half_done else "BE_after_TP1"; break

        if t_hit:
            realized += sign * (level - entry) * frac
            remaining -= frac
            ti += 1
            if ti >= len(targets) or remaining <= 1e-9:
                outcome = "TP1" if strat == "A" else "TP2"
                break
            # strategy C: first target hit -> move stop to breakeven
            half_done = True
            if ex.get("move_to_breakeven", True):
                stop = entry
            # check the NEXT target within the same candle
            level2, frac2 = targets[ti]
            if tgt_hit(c, level2):
                realized += sign * (level2 - entry) * frac2
                outcome = "TP2"; break
    else:
        # ran out of data: close remainder at last close
        realized += sign * (candles[-1][4] - entry) * remaining

    return finalize(realized, outcome, exit_dt)


def _ladder(sig, candles, entry_idx, cfg, side, long, sign, entry, sl, tp1, tp2,
            pip, finalize, tie_pess, no_time, max_hold, t_entry, ex):
    """Strategy D: close f_tp1 at TP1, then trail the remaining fraction up a
    ladder of TP rungs (TP3+ synthesized at the TP2-TP1 step). As each rung is
    reached, the stop trails to the PREVIOUS rung. Final rung closes the rest."""
    f_tp1 = ex.get("tp1_close_fraction", 0.5)
    rungs = max(2, int(ex.get("ladder_rungs", 4)))
    step = tp2 - tp1                                  # signed price step per rung
    if abs(step) < 1e-9:                              # only one TP available
        step = tp1 - entry
    levels = [tp1 + k * step for k in range(rungs)]   # levels[0]=TP1, [1]=TP2, ...

    def stop_hit(c, stop):
        return (c[3] <= stop) if long else (c[2] >= stop)

    def tgt_hit(c, level):
        return (c[2] >= level) if long else (c[3] <= level)

    realized = 0.0
    stop = sl
    remaining = 1.0
    aim = 0                          # index of the next rung we're trying to hit
    half_done = False

    for c in candles[entry_idx:]:
        if not no_time and max_hold and (c[0] - t_entry) > timedelta(minutes=max_hold):
            realized += sign * (c[1] - entry) * remaining
            return finalize(realized, "timeout", c[0])

        s_hit = stop_hit(c, stop)
        t_hit = tgt_hit(c, levels[aim])

        if s_hit and t_hit:                           # same-candle tie
            if tie_pess:
                realized += sign * (stop - entry) * remaining
                tag = "SL" if not half_done else f"trail@{aim}"
                return finalize(realized, tag, c[0])
            s_hit = False                             # optimistic: take the target

        if s_hit:
            realized += sign * (stop - entry) * remaining
            tag = "SL" if not half_done else f"trail@{aim}"
            return finalize(realized, tag, c[0])

        if t_hit:
            if aim == 0:                              # TP1: bank the first fraction
                realized += sign * (levels[0] - entry) * f_tp1
                remaining -= f_tp1
                half_done = True
                aim = 1
                if ex.get("move_to_breakeven", False):   # optional: protect at BE
                    stop = entry                          # else stop stays at SL until TP2
            else:
                # reached rung `aim` -> trail stop to the previous rung
                stop = levels[aim - 1]
                if aim == rungs - 1:                  # final rung: close the rest
                    realized += sign * (levels[aim] - entry) * remaining
                    return finalize(realized, f"TP{aim+1}_final", c[0])
                aim += 1

    # ran out of data: close remainder at last close
    realized += sign * (candles[-1][4] - entry) * remaining
    return finalize(realized, "data_end", candles[-1][0])


# ----------------------------- run / aggregate -----------------------------

def run_backtest(cfg, candles=None, signals=None):
    candles = candles if candles is not None else load_prices(cfg)
    signals = signals if signals is not None else load_signals(cfg)
    times = [c[0] for c in candles]

    results, skipped = [], 0
    for s in signals:
        t = datetime.fromisoformat(s["date"])
        i = bisect.bisect_right(times, t)
        if i >= len(candles) or (candles[i][0] - t) > timedelta(days=3):
            skipped += 1; continue
        r = simulate(s, candles, i, cfg)
        if r is None:
            skipped += 1; continue
        results.append(r)

    return summarize(results, skipped, cfg)


def summarize(results, skipped, cfg):
    n = len(results)
    if n == 0:
        return {"n": 0, "skipped": skipped, "trades": []}
    wins = [r for r in results if r["R"] > 0]
    losses = [r for r in results if r["R"] < 0]
    total_pips = sum(r["pips"] for r in results)
    total_R = sum(r["R"] for r in results)
    total_pct = sum(r["pct"] for r in results)
    gw = sum(r["R"] for r in wins)
    gl = -sum(r["R"] for r in losses)
    pf = gw / gl if gl else float("inf")

    # max drawdown on R equity curve (results already chronological)
    eq = peak = mdd = 0.0
    for r in results:
        eq += r["R"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    from collections import Counter
    return {
        "n": n, "skipped": skipped,
        "win_rate": len(wins) / n * 100,
        "wins": len(wins), "losses": len(losses),
        "total_pips": total_pips, "total_R": total_R, "total_pct": total_pct,
        "avg_pips": total_pips / n, "avg_R": total_R / n,
        "profit_factor": pf, "max_dd_R": mdd,
        "outcomes": dict(Counter(r["outcome"] for r in results)),
        "trades": results,
    }
