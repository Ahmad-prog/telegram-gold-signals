"""
STEP 3a: fetch 1-minute XAUUSD price history for the days we have signals on.

Reads  data/signals.jsonl  -> collects unique UTC signal-days
Fetches 1-min OHLC for each day from TwelveData (free tier)
Writes  data/xauusd_1m.csv  (datetime_utc, open, high, low, close) — deduped/sorted

Free tier limits: 8 requests/min, 800/day. One request per day-of-signals
(~120 days) fits easily. Cached: already-fetched days are skipped on re-run.

Needs TWELVEDATA_KEY in .env  (free key: https://twelvedata.com/register)
"""

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("TWELVEDATA_KEY", "").strip()
DATA = Path(__file__).parent / "data"
SIG = DATA / "signals.jsonl"
OUT = DATA / "xauusd_1m.csv"
RAW_DIR = DATA / "prices_raw"
RAW_DIR.mkdir(exist_ok=True)

URL = "https://api.twelvedata.com/time_series"
SYMBOL = "XAU/USD"


def signal_days():
    """ALL calendar days from first signal to last signal + 5-day buffer, so a
    trade opened near the end can still run to SL/TP. Saturdays skipped (gold
    closed). Already-cached days are reused."""
    from datetime import date, timedelta
    days = sorted(json.loads(l)["date"][:10] for l in SIG.open(encoding="utf-8"))
    d0 = date.fromisoformat(days[0])
    d1 = date.fromisoformat(days[-1]) + timedelta(days=5)
    out = []
    d = d0
    while d <= d1:
        if d.weekday() != 5:        # 5 = Saturday (no gold trading)
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def fetch_day(day):
    """Fetch all 1-min bars for one UTC calendar day. Returns list of rows."""
    cache = RAW_DIR / f"{day}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        params = {
            "symbol": SYMBOL,
            "interval": "1min",
            "start_date": f"{day} 00:00:00",
            "end_date": f"{day} 23:59:00",
            "timezone": "UTC",
            "outputsize": 5000,
            "format": "JSON",
            "apikey": KEY,
        }
        data = None
        for attempt in range(8):
            try:
                r = requests.get(URL, params=params, timeout=60)
                data = r.json()
            except requests.exceptions.RequestException as e:
                wait = min(60, 5 * (attempt + 1))
                print(f"  net error ({type(e).__name__}), retry in {wait}s ...", flush=True)
                time.sleep(wait)
                continue
            if data.get("status") == "error":
                msg = data.get("message", "")
                if "run out" in msg.lower() or "limit" in msg.lower():
                    print(f"  rate limit, waiting 60s ... ({msg[:60]})", flush=True)
                    time.sleep(60)
                    continue
                if "no data" in msg.lower():       # weekend / future day -> empty
                    data = {"values": []}
                    break
                raise RuntimeError(f"{day}: {msg}")
            break
        if data is None:
            raise RuntimeError(f"{day}: failed after retries (network)")
        cache.write_text(json.dumps(data))
    vals = data.get("values") or []
    rows = []
    for v in vals:
        rows.append((v["datetime"], v["open"], v["high"], v["low"], v["close"]))
    return rows


def main():
    if not KEY:
        raise SystemExit("Set TWELVEDATA_KEY in .env (https://twelvedata.com/register)")
    days = signal_days()
    print(f"{len(days)} unique signal-days to fetch (cached ones skipped).")

    all_rows = {}
    req_count = 0
    for i, day in enumerate(days, 1):
        cached = (RAW_DIR / f"{day}.json").exists()
        rows = fetch_day(day)
        for dt, o, h, l, c in rows:
            all_rows[dt] = (o, h, l, c)
        tag = "cache" if cached else "fetch"
        print(f"  [{i}/{len(days)}] {day}: {len(rows):4} bars ({tag})")
        if not cached:
            req_count += 1
            # stay under 8 req/min
            if req_count % 7 == 0:
                print("  ...pausing 60s for rate limit...")
                time.sleep(60)
            else:
                time.sleep(0.3)

    # write merged CSV sorted by time
    lines = ["datetime_utc,open,high,low,close"]
    for dt in sorted(all_rows):
        o, h, l, c = all_rows[dt]
        lines.append(f"{dt},{o},{h},{l},{c}")
    OUT.write_text("\n".join(lines))
    print(f"\nSaved {len(all_rows)} 1-min bars -> {OUT}")


if __name__ == "__main__":
    main()
