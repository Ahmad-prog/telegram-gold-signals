# Quick Setup & Usage — Telegram Signal Fetcher

Pulls ~6 months of posts from the **ChartCraftAdminFx** channel into a JSONL
file. Step 1 of the pipeline: **gather data only** (no parsing, no trading).

---

## 1. Get Telegram API credentials (one time)

1. Open https://my.telegram.org and log in with your phone number.
2. Click **API development tools** → create an app (any name/short-name).
3. Copy the **`api_id`** (a number) and **`api_hash`** (a long string).

---

## 2. Configure `.env`

```bash
cd /mnt/c/Trading_project/telegram_signals
cp .env.example .env
```

Edit `.env` and fill in:

| Variable        | Value                                                        |
|-----------------|-------------------------------------------------------------|
| `TG_API_ID`     | your api_id                                                  |
| `TG_API_HASH`   | your api_hash                                                |
| `TG_PHONE`      | your phone in intl format, e.g. `+923001234567`             |
| `TG_PASSWORD`   | only if you have 2FA on Telegram (else leave blank)         |
| `TG_CHAT`       | already set to `https://t.me/ChartCraftAdminFx`            |
| `TG_SENDER`     | leave blank (broadcast channel — all posts are the channel) |
| `TG_MONTHS_BACK`| `6`                                                         |

> `.env` and the `.session` login file are gitignored — never commit them.

---

## 3. Install dependencies (one time)

```bash
pip install -r requirements.txt
```

---

## 4. Log in (one time — session is cached after)

The login needs the code Telegram sends to your app. Two ways:

**A. Interactive (you type the code)**
```bash
python fetch_history.py
```
It prompts for phone → code → (2FA password). On success it logs in *and*
fetches in one go.

**B. File-driven (lets an assistant/automation drive it)**
```bash
python login.py            # requests the code, then waits
# Telegram sends you a code; write it to the file:
echo 123456 > login_code.txt
```
`login.py` picks up the code, signs in, and caches the session. Then run the
fetch (step 5). Use this when you don't have an interactive terminal.

---

## 5. Fetch the history

```bash
python fetch_history.py
```

Output: **`data/raw_messages.jsonl`** — one JSON object per message:
```json
{"id":123,"date":"2026-06-01T08:30:00+00:00","sender_username":"ChartCraftAdminFx","text":"XAUUSD BUY 2345 ..."}
```
It prints progress every 500 messages and a final `Scanned X, kept Y` summary.

---

## Files in this folder

| File                | What it does                                            |
|---------------------|---------------------------------------------------------|
| `fetch_history.py`  | Pulls 6 months of posts → `data/raw_messages.jsonl`     |
| `login.py`          | File-driven one-time login (no interactive prompt)      |
| `list_chats.py`     | Lists your chats / senders (handy for finding handles)  |
| `.env` / `.env.example` | Config (secrets)                                    |
| `data/`             | Output JSONL (gitignored)                                |

---

## Troubleshooting

- **`kept` count is very low / near zero** → the channel likely posts signals as
  **images/screenshots**, which this text-only fetch skips. Next step would be
  downloading the media + OCR.
- **`PhoneCodeInvalidError`** → the code was wrong/expired; rerun `login.py` and
  write a fresh code quickly.
- **`SessionPasswordNeededError`** → you have 2FA; set `TG_PASSWORD` in `.env`.
- **`FloodWaitError`** → Telegram rate-limited you; wait the stated seconds and
  rerun. The session is saved, so it resumes login-free.

---

## What's next (not built yet)

- **Step 2 — parse:** convert raw text → structured signals
  (`side`, `entry`, `sl`, `tp1..tp3`) once we see the real format.
- **Step 3 — execute:** send parsed signals to a broker (MT5/cTrader/etc.).
  Built and tested last, in isolation — that's where the money risk is.

> ⚠️ Automating a personal Telegram account is against Telegram's ToS; keep
> volume low. Auto-trading third-party signals can lose money quickly — we
> validate parsing on logged history before wiring any live execution.
