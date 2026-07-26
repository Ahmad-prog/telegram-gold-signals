"""Extend 1-min XAUUSD price cache through today; write data/xauusd_1m_recent.csv
covering 2026-06-20 -> now (for auditing the last month of candidate channels)."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_prices as fp

start = date(2026, 6, 20)
end = date(2026, 7, 26)

# refetch the old partial last day
partial = fp.RAW_DIR / "2026-06-29.json"
if partial.exists():
    partial.unlink()

days = []
d = start
while d <= end:
    if d.weekday() != 5:
        days.append(d.isoformat())
    d += timedelta(days=1)

import time
new = 0
for i, day in enumerate(days, 1):
    cached = (fp.RAW_DIR / f"{day}.json").exists()
    try:
        rows = fp.fetch_day(day)
    except Exception as e:
        print(f"  {day}: SKIP ({e})", flush=True)
        continue
    print(f"  [{i}/{len(days)}] {day}: {len(rows)} bars ({'cache' if cached else 'fetch'})", flush=True)
    if not cached:
        new += 1
        time.sleep(60 if new % 7 == 0 else 0.3)

all_rows = {}
for jf in sorted(fp.RAW_DIR.glob("*.json")):
    if jf.stem < start.isoformat():
        continue
    data = json.loads(jf.read_text())
    for v in (data.get("values") or []):
        all_rows[v["datetime"]] = (v["open"], v["high"], v["low"], v["close"])
lines = ["datetime_utc,open,high,low,close"]
for dt in sorted(all_rows):
    o, h, l, c = all_rows[dt]
    lines.append(f"{dt},{o},{h},{l},{c}")
(fp.DATA / "xauusd_1m_recent.csv").write_text("\n".join(lines))
print(f"DONE: {len(all_rows):,} bars {min(all_rows)[:10]} -> {max(all_rows)[:10]}", flush=True)
