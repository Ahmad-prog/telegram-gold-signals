"""
COMPLIANCE-AWARE backtest — models what the bot will ACTUALLY do on a single
GoatFundedTrader account, enforcing their rules:

  - NO HEDGING: never hold opposite-direction positions at the same time
    (skip the opposing signal, or flip — per compliance.hedge_policy).
  - NO ADD-TO-LOSER: ignore "Buy More/Again" add-ons while the open position is
    underwater (avoids grid/Martingale appearance).
  - MIN-STOP filter (filters.min_sl_pips): skip ultra-tight stops that need huge
    lots (flagged as too risky / large volume).
  - MAX CONCURRENT positions cap (risk + anti-"large-volume" control).
  - TREND FILTER (our own edge): only signals aligned with the 4h trend.

Unlike backtest.py (which treats every signal as an independent trade and lets
positions overlap freely), this steps the 1-min candles forward as an
event-driven portfolio, so overlapping/again signals are handled realistically.

    python compliant_sim.py
"""

import bisect
import copy
from datetime import datetime, timedelta

from engine import load_config, load_prices, load_signals, run_backtest

PIP_KEY = "pip_value"


class Position:
    """One open trade managed with Strategy D (50% at TP1 -> breakeven ->
    trail a synthesized TP ladder). Stepped one candle at a time."""

    def __init__(self, sig, entry, cfg):
        ex, m = cfg["exit"], cfg["market"]
        self.pip = m[PIP_KEY]
        self.cost = m["round_trip_cost_pips"] * self.pip
        self.slip = m["slippage_pips"] * self.pip
        self.side = sig["side"]
        self.long = self.side == "buy"
        self.sign = 1 if self.long else -1
        self.addon = sig.get("addon", False)
        self.entry = entry
        self.sl = sig["sl"]
        self.risk_dist = abs(entry - self.sl)
        self.date = sig["date"]

        # targets (pips or absolute), then build the ladder
        if sig["tp_mode"] == "pips":
            tps = sorted(sig["tp_raw"])
            tp1 = entry + self.sign * tps[0] * self.pip
            tp2 = entry + self.sign * (tps[1] if len(tps) > 1 else tps[0]) * self.pip
        else:
            ts = sorted(sig["tp_raw"], reverse=(self.side == "sell"))
            tp1, tp2 = ts[0], ts[1] if len(ts) > 1 else ts[0]
        self.tp1 = tp1
        rungs = max(2, int(ex.get("ladder_rungs", 5)))
        step = tp2 - tp1 if abs(tp2 - tp1) > 1e-9 else tp1 - entry
        self.levels = [tp1 + k * step for k in range(rungs)]
        self.rungs = rungs

        self.f_tp1 = ex.get("tp1_close_fraction", 0.5)
        self.move_be = ex.get("move_to_breakeven", True)
        self.tie_pess = ex["same_candle_tie"] == "pessimistic"
        self.remaining = 1.0
        self.stop = self.sl
        self.aim = 0
        self.half_done = False
        self.realized = 0.0

    def unrealized(self, price):
        return self.sign * (price - self.entry) * self.remaining

    def _finish(self, outcome):
        self.realized -= self.slip + self.cost
        return {"date": self.date, "side": self.side, "addon": self.addon,
                "outcome": outcome, "R": round(self.realized / self.risk_dist, 3)}

    def step(self, c):
        """Process one candle. Returns a result dict when closed, else None."""
        hi, lo = c[2], c[3]
        s_hit = (lo <= self.stop) if self.long else (hi >= self.stop)
        level = self.levels[self.aim]
        t_hit = (hi >= level) if self.long else (lo <= level)

        if s_hit and t_hit:                      # same-candle collision
            if self.tie_pess:
                self.realized += self.sign * (self.stop - self.entry) * self.remaining
                return self._finish("SL" if not self.half_done else f"trail@{self.aim}")
            s_hit = False

        if s_hit:
            self.realized += self.sign * (self.stop - self.entry) * self.remaining
            return self._finish("SL" if not self.half_done else f"trail@{self.aim}")

        if t_hit:
            if self.aim == 0:                    # TP1 -> bank fraction, BE
                self.realized += self.sign * (self.levels[0] - self.entry) * self.f_tp1
                self.remaining -= self.f_tp1
                self.half_done = True
                self.aim = 1
                if self.move_be:
                    self.stop = self.entry
            else:                                # trail up the ladder
                self.stop = self.levels[self.aim - 1]
                if self.aim == self.rungs - 1:
                    self.realized += self.sign * (self.levels[self.aim] - self.entry) * self.remaining
                    return self._finish(f"TP{self.aim+1}_final")
                self.aim += 1
        return None

    def close_at(self, price, outcome="data_end"):
        self.realized += self.sign * (price - self.entry) * self.remaining
        return self._finish(outcome)


def prep_signals(cfg, candles, times):
    """Attach entry index/price + trend, apply min-SL / no-TP / max-SL filters."""
    filt = cfg["filters"]
    pip = cfg["market"][PIP_KEY]
    comp = cfg["compliance"]
    LB = comp["trend_lookback_min"]
    out = []
    for s in load_signals(cfg):
        if s["sl"] is None or s["tp_mode"] is None or not s["tp_raw"]:
            continue
        t = datetime.fromisoformat(s["date"])
        i = bisect.bisect_right(times, t)
        if i >= len(candles) or (candles[i][0] - t) > timedelta(days=3):
            continue
        entry = candles[i][1]
        sl_dist = abs(entry - s["sl"])
        if sl_dist > filt["max_sl_dollars"] or sl_dist / pip < filt.get("min_sl_pips", 0):
            continue
        up = candles[i][1] > candles[i - LB][4] if i - LB >= 0 else None
        aligned = up is None or (s["side"] == "buy" and up) or (s["side"] == "sell" and not up)
        out.append({**s, "_idx": i, "_entry": entry, "_aligned": aligned})
    out.sort(key=lambda s: s["_idx"])
    return out


def run_compliant(cfg, enforce=True):
    candles = load_prices(cfg)
    times = [c[0] for c in candles]
    sigs = prep_signals(cfg, candles, times)
    comp = cfg["compliance"]

    open_pos = []
    closed = []
    skips = {"hedge": 0, "add_to_loser": 0, "max_concurrent": 0, "trend": 0}
    si = 0

    for i, c in enumerate(candles):
        # 1) open any signals whose entry candle is i
        while si < len(sigs) and sigs[si]["_idx"] == i:
            s = sigs[si]; si += 1
            price = c[1]
            if enforce and comp.get("trend_filter") and not s["_aligned"]:
                skips["trend"] += 1; continue
            opposite = [p for p in open_pos if p.side != s["side"]]
            same = [p for p in open_pos if p.side == s["side"]]
            if enforce and comp.get("no_hedge") and opposite:
                if comp.get("hedge_policy") == "flip":
                    for p in opposite:
                        closed.append(p.close_at(price, "flipped"))
                    open_pos = [p for p in open_pos if p.side == s["side"]]
                else:
                    skips["hedge"] += 1; continue
            if enforce and comp.get("no_add_to_loser") and s.get("addon"):
                if any(p.unrealized(price) < 0 for p in same):
                    skips["add_to_loser"] += 1; continue
            if enforce and len(open_pos) >= comp.get("max_concurrent", 99):
                skips["max_concurrent"] += 1; continue
            open_pos.append(Position(s, price, cfg))

        # 2) step open positions on candle i
        if open_pos:
            still = []
            for p in open_pos:
                r = p.step(c)
                (closed.append(r) if r else still.append(p))
            open_pos = still

    for p in open_pos:                           # close leftovers at last price
        closed.append(p.close_at(candles[-1][4]))

    closed.sort(key=lambda t: t["date"])
    return closed, skips


def stats(trades):
    n = len(trades)
    if not n:
        return None
    wins = [t for t in trades if t["R"] > 0]
    tot = sum(t["R"] for t in trades)
    eq = peak = mdd = 0.0
    for t in trades:
        eq += t["R"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    gl = -sum(t["R"] for t in trades if t["R"] < 0) or 1e-9
    return {"n": n, "win": len(wins) / n * 100, "R": tot,
            "avg": tot / n, "mdd": mdd, "pf": sum(t["R"] for t in wins) / gl}


def line(label, s):
    if not s:
        print(f"  {label:28} no trades"); return
    print(f"  {label:28} n={s['n']:3} win={s['win']:4.1f}% R={s['R']:+7.2f} "
          f"avg={s['avg']:+.3f} PF={s['pf']:.2f} maxDD={s['mdd']:.1f}R")


def main():
    cfg = load_config()
    print("=" * 72)
    print("COMPLIANCE-AWARE SIMULATION (single GOAT account, no-hedge, etc.)")
    print("=" * 72)

    naive = run_backtest(cfg)
    print("\n  [reference] naive backtest (independent trades, min-SL on):")
    line("naive", {"n": naive["n"], "win": naive["win_rate"], "R": naive["total_R"],
                   "avg": naive["avg_R"], "mdd": naive["max_dd_R"],
                   "pf": naive["profit_factor"]})

    unconstrained, _ = run_compliant(cfg, enforce=False)
    print("\n  [check] event-sim, compliance OFF (should ~match naive):")
    line("event-sim (gates off)", stats(unconstrained))

    trades, skips = run_compliant(cfg, enforce=True)
    print("\n  [RESULT] event-sim, ALL compliance rules ON:")
    line("compliant", stats(trades))
    print(f"\n  signals skipped for compliance: {skips}")
    tot_skip = sum(skips.values())
    print(f"  total taken {len(trades)}  |  total skipped {tot_skip}")


if __name__ == "__main__":
    main()
