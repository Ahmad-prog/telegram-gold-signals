"""Fetch 1-min XAUUSD for all days in data/_pricedays_3y.json (cached ones
skipped), then rebuild data/xauusd_1m_3y.csv from every cached day in range."""
import json, time
from pathlib import Path
import fetch_prices as fp

days = json.load(open("data/_pricedays_3y.json"))
new = 0
failed = []
for i, day in enumerate(days, 1):
    cached = (fp.RAW_DIR / f"{day}.json").exists()
    try:
        rows = fp.fetch_day(day)
    except Exception as e:
        print(f"  [{i}/{len(days)}] {day}: SKIP after retries ({e})", flush=True)
        failed.append(day)
        continue
    if not cached:
        new += 1
        if new % 7 == 0:
            print(f"  [{i}/{len(days)}] {day}: {len(rows)} bars (fetched, pausing 60s)", flush=True)
            time.sleep(60)
        else:
            time.sleep(0.3)
    if i % 50 == 0:
        print(f"  progress {i}/{len(days)} ({new} fetched)", flush=True)

# rebuild CSV from all cached days within the 3y window
d0, d1 = days[0], days[-1]
all_rows = {}
for jf in sorted(Path("data/prices_raw").glob("*.json")):
    if not (d0 <= jf.stem <= d1):
        continue
    data = json.loads(jf.read_text())
    for v in (data.get("values") or []):
        all_rows[v["datetime"]] = (v["open"], v["high"], v["low"], v["close"])
lines = ["datetime_utc,open,high,low,close"]
for dt in sorted(all_rows):
    o, h, l, c = all_rows[dt]
    lines.append(f"{dt},{o},{h},{l},{c}")
Path("data/xauusd_1m_3y.csv").write_text("\n".join(lines))
print(f"\nDONE. {new} new days fetched. CSV: {len(all_rows):,} bars "
      f"({min(all_rows)[:10]} -> {max(all_rows)[:10]})", flush=True)
if failed:
    print(f"  {len(failed)} days failed (re-run to retry): {failed[:10]}{'...' if len(failed)>10 else ''}", flush=True)
