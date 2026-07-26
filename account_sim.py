"""
ACCOUNT-MODE simulator — runs the merged Gary+GSN strategy under different
account types, all parameterized via parameters.yml -> `account` + `risk`.

  mode=evaluation : pass the +8% / +6% challenge without breaching firm limits
  mode=funded     : trade firm capital; a breach LOSES the account (no pass target)
  mode=real       : your own money; no firm limits, but track DRAWDOWN and RUIN

  compounding=false : risk % of STARTING balance (prop accounting, additive)
  compounding=true  : risk % of CURRENT equity (normal real account)

    python account_sim.py                 # uses parameters.yml as-is
    python account_sim.py real 5          # quick override: mode=real, risk=5%

Reuses the merged one-trade-at-a-time FCFS stream from merged_sim.py.
"""

import copy
import sys
from collections import defaultdict

from engine import load_config, load_prices
from prop_sim import session_date, simulate_prop
from merged_sim import load_tagged, run_merged


def max_consec_losses(trades):
    mx = cur = 0
    for t in trades:
        cur = cur + 1 if t["R"] < 0 else 0
        mx = max(mx, cur)
    return mx


def simulate_real(trades, risk_pct, compounding, ruin_pct, daily_stop, reset_hour,
                  stop_mode="pct", net_thr=2, consec_thr=2):
    """Walk trades as a REAL account. Equity in % terms (start = 100).
    Daily stop: 'pct' (day loss reaches daily_stop), 'net_losses' (losses minus
    wins reaches net_thr), or 'consec_losses' (consec_thr losses in a row).
    Worst day is % of that day's equity."""
    by_day = defaultdict(list)
    for t in trades:
        by_day[session_date(t["date"], reset_hour)].append(t)

    equity = 100.0
    peak = 100.0
    max_dd = 0.0           # most negative % drop from peak
    worst_day = 0.0
    blown_on = None

    for day in sorted(by_day):
        day_start = equity
        day_wins = day_losses = day_consec = 0
        for t in by_day[day]:
            if stop_mode == "net_losses":
                if (day_losses - day_wins) >= net_thr:
                    continue
            elif stop_mode == "consec_losses":
                if day_consec >= consec_thr:
                    continue
            elif daily_stop and (equity - day_start) / day_start * 100 <= -daily_stop:
                continue
            base = equity if compounding else 100.0
            equity += base * (t["R"] * risk_pct / 100.0)
            day_wins += (t["R"] > 0)
            day_losses += (t["R"] <= 0)
            day_consec = 0 if t["R"] > 0 else day_consec + 1
            peak = max(peak, equity)
            max_dd = min(max_dd, (equity / peak - 1) * 100)
            if blown_on is None and (equity / 100.0 - 1) * 100 <= -ruin_pct:
                blown_on = day
        worst_day = min(worst_day, (equity - day_start) / day_start * 100)

    return {
        "final_pct": equity - 100.0, "mult": equity / 100.0,
        "max_dd": max_dd, "worst_day": worst_day, "blown_on": blown_on,
        "max_consec_losses": max_consec_losses(trades),
    }


def run_funded(trades, risk_pct, p, payout_target):
    """Funded account: NO pass target, but firm breach loses the account.
    Report profit reached, time to one payout cycle, and any breach."""
    rep = simulate_prop(trades, risk_pct, p["daily_loss_stop_pct"],
                        p["firm_daily_dd_pct"], p["firm_max_dd_pct"],
                        payout_target, p["max_dd_mode"], p["day_reset_utc_hour"])
    return rep


def main():
    cfg = load_config()
    acc = cfg["account"]
    if len(sys.argv) > 1:
        acc["mode"] = sys.argv[1]
    if len(sys.argv) > 2:
        cfg["risk"]["risk_per_trade_pct"] = float(sys.argv[2])
        if acc["mode"] == "real":
            acc["compounding"] = True

    risk_pct = cfg["risk"]["risk_per_trade_pct"]
    p = cfg["prop"]
    candles = load_prices(cfg)
    times = [c[0] for c in candles]
    sigs = load_tagged(cfg, candles, times)
    trades, dropped, occ = run_merged(cfg, candles, sigs)

    # daily-stop config (shared by all modes)
    stop_mode = acc.get("daily_stop_mode", "pct")
    net_thr = acc.get("daily_net_loss_stop", 2)
    consec_thr = acc.get("daily_consec_loss_stop", 2)
    stop_desc = {"net_losses": f"net-loss({net_thr})",
                 "consec_losses": f"{consec_thr}-consec-losses",
                 "pct": f"{p['daily_loss_stop_pct']}%-pct"}.get(stop_mode, stop_mode)

    R = sum(t["R"] for t in trades)
    print("=" * 74)
    print(f"ACCOUNT SIM  —  mode={acc['mode']}  risk={risk_pct}%/trade  "
          f"compounding={acc['compounding']}  daily-stop={stop_desc}")
    print(f"  strategy: merged Gary+GSN, 1-at-a-time FCFS")
    print("=" * 74)
    print(f"  trades {len(trades)} | net {R:+.1f}R | occupancy {occ/len(candles)*100:.1f}%")

    mode = acc["mode"]
    if mode in ("evaluation",):
        print("\n  [EVALUATION] pass the challenge without breaching limits:")
        for target, ph in [(8.0, "Phase1 +8%"), (6.0, "Phase2 +6%")]:
            rep = simulate_prop(trades, risk_pct, p["daily_loss_stop_pct"],
                                p["firm_daily_dd_pct"], p["firm_max_dd_pct"],
                                target, p["max_dd_mode"], p["day_reset_utc_hour"],
                                stop_mode=stop_mode, net_thr=net_thr, consec_thr=consec_thr)
            st = "BREACH/FAIL" if rep["breach"] else ("PASS" if rep["target_day"] else "not reached")
            print(f"    {ph:11} {st:12} "
                  + (f"in {rep['days_traded']}d/{rep['trades_taken']}t" if rep["target_day"]
                     else f"final {rep['final_equity']:+.2f}%")
                  + f"  worstDay {rep['worst_day']:.2f}% maxDD {rep['max_dd']:.2f}%"
                  + (f"  >>>{rep['breach']}" if rep["breach"] else ""))

    elif mode == "funded":
        print("\n  [FUNDED] trading firm capital — a breach LOSES the account:")
        rep = simulate_prop(trades, risk_pct, p["daily_loss_stop_pct"],
                            p["firm_daily_dd_pct"], p["firm_max_dd_pct"],
                            acc["funded_payout_target_pct"], p["max_dd_mode"],
                            p["day_reset_utc_hour"], stop_mode=stop_mode, net_thr=net_thr, consec_thr=consec_thr)
        breached = bool(rep["breach"])
        print(f"    Account status : {'LOST (breach)' if breached else 'SURVIVED'}")
        if rep["target_day"]:
            print(f"    Payout +{acc['funded_payout_target_pct']:.0f}% reached "
                  f"on {rep['target_day']} ({rep['days_traded']}d, {rep['trades_taken']}t)")
        print(f"    Net over period: {rep['final_equity']:+.2f}% of balance")
        print(f"    Max drawdown   : {rep['max_dd']:.2f}%  (firm limit -{p['firm_max_dd_pct']}%)")
        print(f"    Worst day      : {rep['worst_day']:.2f}%  (firm limit -{p['firm_daily_dd_pct']}%)")
        if breached:
            print(f"    >>> BREACH: {rep['breach']}")

    elif mode == "real":
        ds = acc["daily_loss_stop_pct"]
        ds = ds if ds is not None else p["daily_loss_stop_pct"]
        print(f"\n  [REAL] your own money — no firm limits; daily-stop {ds}%, "
              f"ruin at -{acc['ruin_pct']:.0f}%:")
        rep = simulate_real(trades, risk_pct, acc["compounding"],
                            acc["ruin_pct"], ds, p["day_reset_utc_hour"],
                            stop_mode=stop_mode, net_thr=net_thr, consec_thr=consec_thr)
        print(f"    Final return   : {rep['final_pct']:+.1f}%   (x{rep['mult']:.2f} the account)")
        print(f"    MAX DRAWDOWN   : {rep['max_dd']:.1f}%   <-- the number that matters")
        print(f"    Worst day      : {rep['worst_day']:.1f}%")
        print(f"    Max consec losses: {rep['max_consec_losses']}  "
              f"(= {rep['max_consec_losses']*risk_pct:.0f}% if all full SL, non-comp)")
        print(f"    Account blown? : {'YES on '+rep['blown_on'] if rep['blown_on'] else 'no'}")


if __name__ == "__main__":
    main()
