"""
STEP 3b: backtest GARY GOLD TRADER signals using the config in parameters.yml.

    python backtest.py

All knobs (exit strategy, cost, slippage, risk, filters) come from
parameters.yml. Writes the per-trade log to data/backtest_trades.csv and
prints a summary in pips, R, and % of account.
"""

import csv
from pathlib import Path

from engine import load_config, run_backtest

HERE = Path(__file__).parent


def main():
    cfg = load_config()
    res = run_backtest(cfg)
    if res["n"] == 0:
        print("No trades simulated. Did you run fetch_prices.py?")
        return

    # write per-trade log
    out = HERE / cfg["paths"]["trades_out"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(res["trades"][0].keys()))
        w.writeheader()
        w.writerows(res["trades"])

    e = cfg["exit"]; m = cfg["market"]; rk = cfg["risk"]
    print("=" * 64)
    print("GARY GOLD TRADER — backtest")
    print(f"  strategy={e['strategy']}  cost={m['round_trip_cost_pips']}p  "
          f"slip={m['slippage_pips']}p  tie={e['same_candle_tie']}  "
          f"entry={cfg['entry']['mode']}  risk={rk['risk_per_trade_pct']}%/trade")
    print("=" * 64)
    print(f"Trades simulated : {res['n']}   (skipped {res['skipped']})")
    print(f"Win rate         : {res['win_rate']:.1f}%   "
          f"({res['wins']}W / {res['losses']}L)")
    print(f"Total result     : {res['total_pips']:+.0f} pips  |  "
          f"{res['total_R']:+.2f} R  |  {res['total_pct']:+.1f}% of account")
    print(f"Avg per trade    : {res['avg_pips']:+.1f} pips  |  {res['avg_R']:+.3f} R")
    print(f"Profit factor    : {res['profit_factor']:.2f}")
    print(f"Max drawdown     : {res['max_dd_R']:.2f} R  "
          f"({res['max_dd_R']*rk['risk_per_trade_pct']:.1f}% of account)")
    print(f"Outcomes         : {res['outcomes']}")
    print(f"\nPer-trade log -> {out}")


if __name__ == "__main__":
    main()
