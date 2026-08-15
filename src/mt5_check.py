"""
MT5 connection check — RUN THIS ON THE WINDOWS HOST, not WSL.

    pip install MetaTrader5
    python src\\mt5_check.py

Verifies the whole broker leg WITHOUT placing a single order:
  * terminal connects and the account logs in
  * whether the account is DEMO or REAL  (refuses to go further on REAL)
  * XAUUSD is visible, and its live spread vs our max_spread_pips cap
  * lot step / min / max, so our sizing math is valid for this broker
  * order_check() validates a real order request server-side — margin, stops
    distance, filling mode — and returns what WOULD happen, executing nothing

Only after this passes clean should executor.py be pointed at a DEMO account.

Credentials come from .env (never hard-code them, never commit them):
    MT5_LOGIN=<your account number>
    MT5_PASSWORD=<your password>
    MT5_SERVER=<your broker's exact server name>
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SYMBOL = "XAUUSD"


def main():
    try:
        import MetaTrader5 as mt5
    except ModuleNotFoundError:
        print("MetaTrader5 is Windows-only. Run this on the Windows host.")
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    login = int(os.environ["MT5_LOGIN"])
    password = os.environ["MT5_PASSWORD"]
    server = os.environ["MT5_SERVER"]

    print("=" * 70)
    print(f"MT5 CHECK — {server} / {login}")
    print("=" * 70)

    if not mt5.initialize(login=login, password=password, server=server):
        print(f"FAIL initialize: {mt5.last_error()}")
        print("  terminal running and logged in? server name exact?")
        sys.exit(1)

    acc = mt5.account_info()
    if acc is None:
        print(f"FAIL account_info: {mt5.last_error()}"); mt5.shutdown(); sys.exit(1)

    is_demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    kind = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(acc.trade_mode, "?")
    print(f"\n[1] ACCOUNT")
    print(f"    name      : {acc.name}")
    print(f"    type      : {kind}")
    print(f"    balance   : {acc.balance:,.2f} {acc.currency}")
    print(f"    equity    : {acc.equity:,.2f}")
    print(f"    leverage  : 1:{acc.leverage}")
    print(f"    trade_allowed (terminal): {acc.trade_allowed}")

    print(f"\n[2] SYMBOL {SYMBOL}")
    if not mt5.symbol_select(SYMBOL, True):
        print(f"    FAIL symbol_select — try the broker's exact name "
              f"(XAUUSD.r / GOLD / XAUUSDm). Available gold symbols:")
        for s in (mt5.symbols_get() or []):
            if "XAU" in s.name.upper() or "GOLD" in s.name.upper():
                print(f"      {s.name}")
        mt5.shutdown(); sys.exit(1)

    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    spread_price = tick.ask - tick.bid
    spread_pips = spread_price / 0.10
    print(f"    bid/ask   : {tick.bid} / {tick.ask}")
    print(f"    spread    : {spread_price:.3f}  = {spread_pips:.1f} of our pips "
          f"(cap {5})  {'OK' if spread_pips <= 5 else '** OVER CAP **'}")
    print(f"    lot min/step/max : {info.volume_min} / {info.volume_step} / {info.volume_max}")
    print(f"    stops_level      : {info.trade_stops_level} points "
          f"(min distance for SL/TP)")
    print(f"    contract_size    : {info.trade_contract_size}")

    # our sizing assumes $10 per pip per lot on a 100-oz contract
    implied = info.trade_contract_size * 0.10
    print(f"    $/pip/lot implied: {implied:.2f}  "
          f"{'OK — matches our sizing' if abs(implied - 10) < 0.01 else '** SIZING MISMATCH **'}")

    print(f"\n[3] DRY-RUN ORDER VALIDATION (order_check — places nothing)")
    lots = max(info.volume_min, 0.01)
    sl = round(tick.bid + 9.0, info.digits)     # 90 pips above for a sell
    tp = round(tick.bid - 5.0, info.digits)     # 50 pips below
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": lots,
        "type": mt5.ORDER_TYPE_SELL, "price": tick.bid, "sl": sl, "tp": tp,
        "deviation": 20, "magic": 770001, "comment": "msg:CHECK",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    chk = mt5.order_check(req)
    if chk is None:
        print(f"    FAIL order_check: {mt5.last_error()}")
    else:
        ok = chk.retcode == 0
        print(f"    retcode   : {chk.retcode} — {chk.comment}")
        print(f"    margin req: {chk.margin:,.2f}   free after: {chk.margin_free:,.2f}")
        print(f"    {'VALID — the broker would accept this order' if ok else '** REJECTED — fix before trading **'}")
        if not ok and chk.retcode == 10016:
            print("    (10016 = invalid stops: SL/TP inside trade_stops_level)")

    print("\n" + "=" * 70)
    if not is_demo:
        print("STOP: this is NOT a demo account.")
        print("  Do not point executor.py at it. The bot has never placed a real")
        print("  order, and two bugs were found in testing this week. Run the")
        print("  paper phase on a DEMO account first — GFT provides one.")
    else:
        print("DEMO account confirmed — safe to proceed to the paper phase.")
    mt5.shutdown()


if __name__ == "__main__":
    main()
