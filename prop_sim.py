"""
PROP-FIRM SIMULATION — does the strategy actually PASS the challenge under
real risk rules (daily loss stop, daily drawdown limit, max drawdown limit,
profit target)?  Raw +R means nothing if a daily-limit breach fails the account.

    python prop_sim.py

Reads the `prop` section of parameters.yml. Processes the backtest trades
chronologically, day by day:
  - risk a fixed % per trade (1R = risk_per_trade_pct of the STARTING balance)
  - once a day's cumulative loss hits daily_loss_stop_pct, skip the rest of
    that day's signals (your "2 SL = stop trading" rule)
  - flag a FAIL if intraday loss breaches the firm daily limit, or equity
    drawdown breaches the firm max limit
  - stop with PASS once equity reaches the profit target

P&L is additive on the initial balance (standard prop accounting: % are of the
starting balance, non-compounding within the evaluation).
"""

import copy
from collections import defaultdict
from datetime import datetime, timedelta

from engine import load_config, run_backtest


def session_date(iso, reset_hour):
    """Trading-day label respecting a daily reset at `reset_hour` UTC
    (21:00 UTC = 2am Pakistan). A trade is shifted so the reset becomes
    midnight, then we take the calendar date."""
    t = datetime.fromisoformat(iso)
    return (t + timedelta(hours=24 - reset_hour)).date().isoformat()


def simulate_prop(trades, risk_pct, daily_stop, firm_daily, firm_max,
                  target, dd_mode, reset_hour,
                  stop_mode="pct", net_thr=2, consec_thr=2):
    """Walk trades (chronological) applying the daily-stop and limit rules.
    Daily stop: 'pct' (loss reaches daily_stop %), 'net_losses' (losses minus
    wins reaches net_thr), or 'consec_losses' (consec_thr losses in a row).
    Returns a report dict."""
    by_day = defaultdict(list)
    for t in trades:
        by_day[session_date(t["date"], reset_hour)].append(t)

    equity = 0.0          # % of starting balance, additive (0 = breakeven)
    peak = 0.0
    max_dd = 0.0
    worst_day = 0.0
    days_traded = 0
    trades_taken = 0
    trades_skipped = 0
    daily_stop_hits = 0
    breach = None
    target_day = None
    day_pnls = []

    for day in sorted(by_day):
        day_start = equity
        day_pnl = 0.0
        day_wins = day_losses = day_consec = 0
        took_any = False
        stopped = False
        for t in by_day[day]:
            # enforce the self-imposed daily stop BEFORE taking a trade
            if stop_mode == "net_losses":
                hit = (day_losses - day_wins) >= net_thr
            elif stop_mode == "consec_losses":
                hit = day_consec >= consec_thr
            else:
                hit = day_pnl <= -daily_stop
            if hit:
                trades_skipped += 1
                stopped = True
                continue
            r_pct = t["R"] * risk_pct          # this trade's P&L in % of balance
            equity += r_pct
            day_pnl += r_pct
            day_wins += (t["R"] > 0)
            day_losses += (t["R"] <= 0)
            day_consec = 0 if t["R"] > 0 else day_consec + 1
            trades_taken += 1
            took_any = True

            # drawdown tracking
            peak = max(peak, equity)
            ref = peak if dd_mode == "trailing" else 0.0
            dd = equity - ref
            max_dd = min(max_dd, dd)

            # --- breach checks (firm hard limits) ---
            if (equity - day_start) <= -firm_daily and breach is None:
                breach = f"DAILY limit: {equity-day_start:.2f}% on {day}"
            if dd <= -firm_max and breach is None:
                breach = f"MAX drawdown: {dd:.2f}% on {day}"

            # --- target reached? ---
            if equity >= target and target_day is None:
                target_day = day

        if stopped:
            daily_stop_hits += 1
        if took_any:
            days_traded += 1
            day_pnls.append((day, day_pnl))
            worst_day = min(worst_day, day_pnl)
        if target_day is not None:
            break   # phase 1: stop once target hit

    return {
        "final_equity": equity, "peak": peak, "max_dd": max_dd,
        "worst_day": worst_day, "days_traded": days_traded,
        "trades_taken": trades_taken, "trades_skipped": trades_skipped,
        "daily_stop_hits": daily_stop_hits, "breach": breach,
        "target_day": target_day, "day_pnls": day_pnls,
    }


def run(cfg, strategy, target, label, trades):
    p = cfg["prop"]
    risk_pct = p["risk_per_trade_pct"]
    rep = simulate_prop(
        trades, risk_pct, p["daily_loss_stop_pct"],
        p["firm_daily_dd_pct"], p["firm_max_dd_pct"],
        target, p["max_dd_mode"], p["day_reset_utc_hour"])

    status = "BREACH/FAIL" if rep["breach"] else (
        "PASS ✅" if rep["target_day"] else "target not reached")
    print(f"\n  [{label}]  strategy {strategy}, risk {risk_pct}%, "
          f"daily-stop {p['daily_loss_stop_pct']}%, target +{target}%")
    print(f"    Result        : {status}")
    if rep["target_day"]:
        print(f"    Hit +{target}% on {rep['target_day']} "
              f"after {rep['days_traded']} trading days, {rep['trades_taken']} trades")
    else:
        print(f"    Final equity  : {rep['final_equity']:+.2f}%  "
              f"({rep['trades_taken']} trades over {rep['days_traded']} days)")
    print(f"    Max drawdown  : {rep['max_dd']:.2f}%  "
          f"(firm static limit -{p['firm_max_dd_pct']}%)")
    print(f"    Worst day     : {rep['worst_day']:.2f}%  "
          f"(firm daily limit -{p['firm_daily_dd_pct']}%)")
    print(f"    Daily-stop hit: {rep['daily_stop_hits']} days  |  "
          f"skipped {rep['trades_skipped']} trades")
    if rep["breach"]:
        print(f"    >>> BREACH: {rep['breach']}")
    return rep


def rolling_start(cfg, trades, target):
    """Simulate STARTING the challenge on every distinct trading day, to see if
    the strategy passes regardless of entry timing. Returns a summary."""
    p = cfg["prop"]
    rh = p["day_reset_utc_hour"]
    days = sorted({session_date(t["date"], rh) for t in trades})
    passes, fails, incompletes, days_to_pass = 0, 0, 0, []
    breach_examples = []
    for start in days:
        fwd = [t for t in trades if session_date(t["date"], rh) >= start]
        rep = simulate_prop(fwd, p["risk_per_trade_pct"], p["daily_loss_stop_pct"],
                            p["firm_daily_dd_pct"], p["firm_max_dd_pct"],
                            target, p["max_dd_mode"], rh)
        if rep["breach"]:
            fails += 1
            if len(breach_examples) < 3:
                breach_examples.append((start, rep["breach"]))
        elif rep["target_day"]:
            passes += 1
            days_to_pass.append(rep["days_traded"])
        else:
            incompletes += 1            # ran out of data before target (late starts)
    return {"n": len(days), "passes": passes, "fails": fails,
            "incompletes": incompletes, "days_to_pass": days_to_pass,
            "breach_examples": breach_examples}


def main():
    cfg = load_config()
    p = cfg["prop"]
    print("=" * 70)
    print(f"PROP-FIRM SIMULATION  (static -{p['firm_max_dd_pct']}% max, "
          f"-{p['firm_daily_dd_pct']}% daily reset 21:00 UTC, 2% self-stop)")
    print(f"  risk {p['risk_per_trade_pct']}%/trade | Phase 1 target +8% | "
          f"Phase 2 target +6%")
    print("=" * 70)

    for strat in ["A", "C", "D"]:
        print(f"\n{'-'*70}\nSTRATEGY {strat}\n{'-'*70}")
        c = copy.deepcopy(cfg)
        c["exit"]["strategy"] = strat
        c["risk"]["risk_per_trade_pct"] = p["risk_per_trade_pct"]
        trades = run_backtest(c)["trades"]
        run(cfg, strat, 8.0, "Phase 1", trades)
        run(cfg, strat, 6.0, "Phase 2", trades)

    # ---------------- rolling-start robustness ----------------
    print(f"\n{'='*70}")
    print("ROLLING-START ROBUSTNESS  (start the challenge on EVERY trading day)")
    print(f"{'='*70}")
    import statistics as st
    for strat in ["A", "C", "D"]:
        c = copy.deepcopy(cfg)
        c["exit"]["strategy"] = strat
        c["risk"]["risk_per_trade_pct"] = p["risk_per_trade_pct"]
        trades = run_backtest(c)["trades"]
        for target, ph in [(8.0, "P1"), (6.0, "P2")]:
            r = rolling_start(cfg, trades, target)
            done = r["passes"] + r["fails"]
            pr = (r["passes"] / done * 100) if done else 0
            dtp = r["days_to_pass"]
            md = f"med {st.median(dtp):.0f}, max {max(dtp)}" if dtp else "—"
            print(f"  {strat} {ph}(+{target:.0f}%): {r['passes']}/{done} starts pass "
                  f"({pr:.0f}%), {r['fails']} breach, {r['incompletes']} ran out of data | "
                  f"days-to-pass {md}")
            for s, b in r["breach_examples"]:
                print(f"        breach if started {s}: {b}")


if __name__ == "__main__":
    main()
