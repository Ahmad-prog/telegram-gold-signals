# data_archive — frozen research inputs (reproducibility)

These gzipped files are the EXACT inputs behind the numbers in `docs/design-doc.md` §8–9.
`data/` itself is gitignored (bulky / contains per-day API cache); restore with:

```bash
mkdir -p data
for f in data_archive/*.gz; do gzip -dkc "$f" > "data/$(basename "${f%.gz}")"; done
```

| File | What |
|---|---|
| `raw_gary_max.jsonl.gz` | Gary channel full history (17,030 msgs, 2022-09 → 2026-06-29) |
| `raw_gsn_max.jsonl.gz` | GoldScalperNinja full history (20,017 msgs, 2023-05 → 2026-06-29) |
| `signals_gary_3y.jsonl.gz` / `signals_gsn_3y.jsonl.gz` | parsed 3-year signal sets (research inputs) |
| `signals.jsonl.gz` / `signals_goldscalperninja.jsonl.gz` | original 6-month parsed sets (legacy scripts) |
| `xauusd_1m_3y.csv.gz` | 1,114,084 one-minute XAU/USD bars, 2023-06-29 → 2026-06-29, UTC (TwelveData) |

Do NOT re-fetch from Telegram to "refresh" these when reproducing — newer messages
shift every number. Re-fetching prices via `fetch_prices_3y.py` re-reads the same
per-day cache if `data/prices_raw/` is present, else re-downloads (results may
differ marginally if the vendor revises history).
