"""
Shared library for the 3-year configuration sweep.

- fast candle cache (npz in scratchpad; falls back to repo)
- exit-strategy VARIANTS (config overrides on top of parameters.yml)
- per-signal feature computation (session hour, 4h/24h momentum, ATR) — all
  computed from candles BEFORE the entry index (no lookahead)
- day-level aggregation under a daily-stop rule
- two-phase (P1 +8% -> P2 +6%) rolling-start evaluator on day vectors
"""

import bisect
import copy
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CACHE_DIRS = [
    Path("/tmp/claude-1000/-mnt-c-trading-project-telegram-signals/87ca797c-59e5-47bb-853a-1fc89aa5891c/scratchpad"),
    REPO / "research",
]
PRICES_CSV = REPO / "data" / "xauusd_1m_3y.csv"
NPZ_NAME = "candles_3y.npz"

DATA_START = "2025-07-01"       # user choice: search the LAST 1 YEAR only
TRAIN_END = "2026-03-01"        # train = Jul25..Feb26; test (OOS) = Mar26..Jun26
RESET_HOUR = 21                 # GFT daily reset 21:00 UTC

SIGNALS = {
    "gary": REPO / "data" / "signals_gary_3y.jsonl",
    "gsn": REPO / "data" / "signals_gsn_3y.jsonl",
}


# --------------------------- candles ---------------------------

def _cache_path():
    for d in CACHE_DIRS:
        if d.exists():
            return d / NPZ_NAME
    return CACHE_DIRS[-1] / NPZ_NAME


def build_candle_cache():
    import csv
    ep, o, h, l, c = [], [], [], [], []
    with PRICES_CSV.open() as f:
        for r in csv.DictReader(f):
            dt = datetime.fromisoformat(r["datetime_utc"]).replace(tzinfo=timezone.utc)
            ep.append(dt.timestamp())
            o.append(float(r["open"])); h.append(float(r["high"]))
            l.append(float(r["low"])); c.append(float(r["close"]))
    ep = np.array(ep); order = np.argsort(ep)
    arrs = dict(ep=ep[order], o=np.array(o)[order], h=np.array(h)[order],
                l=np.array(l)[order], c=np.array(c)[order])
    p = _cache_path()
    np.savez(p, **arrs)
    return p


def load_arrays():
    p = _cache_path()
    if not p.exists():
        build_candle_cache()
    z = np.load(p)
    return z["ep"], z["o"], z["h"], z["l"], z["c"]


def arrays_to_candles(ep, o, h, l, c):
    """Reconstruct the (datetime, o, h, l, c) tuple list engine.simulate expects."""
    dts = [datetime.fromtimestamp(e, tz=timezone.utc) for e in ep]
    return list(zip(dts, o.tolist(), h.tolist(), l.tolist(), c.tolist()))


# --------------------------- variants ---------------------------

def _base_cfg():
    from engine import load_config
    cfg = load_config()
    cfg["paths"]["prices"] = "data/xauusd_1m_3y.csv"
    cfg["market"]["round_trip_cost_pips"] = 3
    cfg["market"]["slippage_pips"] = 0.0
    cfg["entry"]["mode"] = "market_next"
    cfg["exit"]["same_candle_tie"] = "pessimistic"
    cfg["exit"]["no_time_exit"] = True
    cfg["exit"]["max_hold_minutes"] = 0
    cfg["exit"]["tp1_close_fraction"] = 0.5
    cfg["exit"]["move_to_breakeven"] = True
    return cfg


def variant_cfg(name, cost=None):
    cfg = _base_cfg()
    ex = cfg["exit"]
    if name == "A":
        ex["strategy"] = "A"
    elif name == "B":
        ex["strategy"] = "B"
    elif name == "C":
        ex["strategy"] = "C"
    elif name == "C_f07":
        ex["strategy"] = "C"; ex["tp1_close_fraction"] = 0.7
    elif name == "D3":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 3
    elif name == "D5":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 5
    elif name == "D5_noBE":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 5; ex["move_to_breakeven"] = False
    elif name == "D8":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 8
    elif name == "D5_hold480":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 5
        ex["no_time_exit"] = False; ex["max_hold_minutes"] = 480
    elif name == "zone_D5":
        ex["strategy"] = "D"; ex["ladder_rungs"] = 5
        cfg["entry"]["mode"] = "zone_touch"
    elif name == "zone_C":
        ex["strategy"] = "C"
        cfg["entry"]["mode"] = "zone_touch"
    else:
        raise ValueError(name)
    if cost is not None:
        cfg["market"]["round_trip_cost_pips"] = cost
    return cfg


VARIANTS = ["A", "B", "C", "C_f07", "D3", "D5", "D5_noBE", "D8",
            "D5_hold480", "zone_D5", "zone_C"]


# --------------------------- signals + features ---------------------------

def load_signals(channel):
    return [json.loads(l) for l in SIGNALS[channel].open(encoding="utf-8")]


def prep_signal_rows(channel, ep, o, h, l, c):
    """Attach entry index + entry-time features to each signal (engine filters
    like min_sl/max_sl/no-TP are applied later by engine.simulate itself).
    Only signals from DATA_START onward (last-1-year search window)."""
    start = os.environ.get("SWEEP_DATA_START", DATA_START)   # regime-check override
    start_ts = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()
    out = []
    eps = ep  # sorted epochs
    for s in load_signals(channel):
        t = datetime.fromisoformat(s["date"]).timestamp()
        if t < start_ts:
            continue
        i = int(np.searchsorted(eps, t, side="right"))
        if i >= len(eps) or (eps[i] - t) > 3 * 86400:
            continue
        dt = datetime.fromtimestamp(eps[i], tz=timezone.utc)
        feat = {
            "_idx": i,
            "hour": dt.hour,
            "dow": dt.weekday(),
        }
        # 4h / 24h momentum vs entry open (pre-entry info only)
        for name, lb in (("ret4h", 240), ("ret24h", 1440)):
            if i - lb >= 0:
                feat[name] = float(o[i] - c[i - lb])
            else:
                feat[name] = None
        # ATR proxy: mean 1-min range over the previous 60 candles
        if i >= 60:
            feat["atr60"] = float(np.mean(h[i - 60:i] - l[i - 60:i]))
        else:
            feat["atr60"] = None
        out.append({**s, **feat})
    return out


# --------------------------- day-level prop math ---------------------------

def session_day(epoch):
    """Trading-day integer respecting the 21:00 UTC reset."""
    return int((epoch + (24 - RESET_HOUR) * 3600) // 86400)


def day_aggregate(trades, stop_mode="consec", thr=2):
    """trades: list of dicts with 'epoch' and 'R', chronological.
    Returns (day_keys, dayR, intra_min) numpy arrays after applying the
    intraday stop rule. intra_min = most negative intraday prefix sum of R."""
    if not trades:
        return np.array([]), np.array([]), np.array([])
    days, dayR, intra = [], [], []
    cur_day = None
    run = []
    def flush():
        if cur_day is None:
            return
        taken = []
        consec = wins = losses = 0
        for R in run:
            if stop_mode == "consec" and consec >= thr:
                continue
            if stop_mode == "net" and (losses - wins) >= thr:
                continue
            taken.append(R)
            wins += R > 0; losses += R <= 0
            consec = 0 if R > 0 else consec + 1
        s = 0.0; mn = 0.0
        for R in taken:
            s += R; mn = min(mn, s)
        days.append(cur_day); dayR.append(s); intra.append(mn)
    for t in trades:
        d = session_day(t["epoch"])
        if d != cur_day:
            flush(); cur_day = d; run = []
        run.append(t["R"])
    flush()
    return np.array(days), np.array(dayR), np.array(intra)


def two_phase_from(dayR, intra, s, risk, tgt1=8.0, tgt2=6.0,
                   firm_daily=4.0, firm_max=10.0):
    """Start a fresh evaluation at day index s. Returns (outcome, days_used).
    outcome: 'pass' | 'breach' | 'incomplete'."""
    eq = 0.0
    phase = 1
    tgt = tgt1
    n = len(dayR)
    for i in range(s, n):
        if intra[i] * risk <= -firm_daily:
            return "breach", i - s + 1
        if (eq + intra[i]) * risk <= -firm_max:
            return "breach", i - s + 1
        eq += dayR[i]
        if eq * risk >= tgt:
            if phase == 1:
                phase = 2; tgt = tgt2; eq = 0.0
            else:
                return "pass", i - s + 1
    return "incomplete", n - s


def rolling_two_phase(dayR, intra, risk, day_keys=None, start_mask=None):
    """Evaluate a fresh two-phase challenge starting on every day.
    Returns dict with pass%, breach%, resolved count, median/max days-to-pass."""
    n = len(dayR)
    passes = breaches = incomplete = 0
    days_list = []
    for s in range(n):
        if start_mask is not None and not start_mask[s]:
            continue
        out, d = two_phase_from(dayR, intra, s, risk)
        if out == "pass":
            passes += 1; days_list.append(d)
        elif out == "breach":
            breaches += 1
        else:
            incomplete += 1
    resolved = passes + breaches
    return {
        "passes": passes, "breaches": breaches, "incomplete": incomplete,
        "resolved": resolved,
        "pass_rate": passes / resolved * 100 if resolved else 0.0,
        "med_days": float(np.median(days_list)) if days_list else None,
        "max_days": max(days_list) if days_list else None,
    }


# --------------------------- filters ---------------------------

SESSIONS = {
    "all": lambda hr: np.ones_like(hr, bool),
    "ldn": lambda hr: (hr >= 7) & (hr < 13),
    "ny": lambda hr: (hr >= 13) & (hr < 21),
    "ldnny": lambda hr: (hr >= 7) & (hr < 21),
    "asia": lambda hr: (hr >= 0) & (hr < 7),
    "noasia": lambda hr: hr >= 7,
}
SIDES = ["both", "buy", "sell"]
T4 = ["any", "aligned", "counter"]
T24 = ["any", "aligned"]
ADDON = ["any", "fresh"]
SLB = ["any", "le80", "gt80", "mid40_120"]
VOL = ["any", "hi", "lo"]


def build_masks(tr):
    """tr: dict of numpy arrays for one stream. Returns dict of dicts of masks."""
    hr = tr["hour"]; side = tr["side"]; r4 = tr["ret4h"]; r24 = tr["ret24h"]
    addon = tr["addon"]; slp = tr["sl_pips"]; atr = tr["atr60"]
    sgn = np.where(side == 1, 1.0, -1.0)      # 1=buy, 0=sell
    m = {}
    m["session"] = {k: f(hr) for k, f in SESSIONS.items()}
    m["side"] = {"both": np.ones_like(hr, bool),
                 "buy": side == 1, "sell": side == 0}
    al4 = np.where(np.isnan(r4), False, (r4 * sgn) > 0)
    ct4 = np.where(np.isnan(r4), False, (r4 * sgn) < 0)
    m["t4"] = {"any": np.ones_like(hr, bool), "aligned": al4, "counter": ct4}
    al24 = np.where(np.isnan(r24), False, (r24 * sgn) > 0)
    m["t24"] = {"any": np.ones_like(hr, bool), "aligned": al24}
    m["addon"] = {"any": np.ones_like(hr, bool), "fresh": ~addon}
    m["slb"] = {"any": np.ones_like(hr, bool), "le80": slp <= 80,
                "gt80": slp > 80, "mid40_120": (slp >= 40) & (slp <= 120)}
    med = np.nanmedian(atr[tr["is_train"]]) if np.any(tr["is_train"]) else np.nanmedian(atr)
    m["vol"] = {"any": np.ones_like(hr, bool),
                "hi": np.where(np.isnan(atr), False, atr > med),
                "lo": np.where(np.isnan(atr), False, atr <= med)}
    return m


def trades_to_arrays(rows):
    """rows: list of trade dicts from precompute. -> dict of numpy arrays."""
    ep = np.array([r["epoch"] for r in rows])
    order = np.argsort(ep)
    def arr(key, dtype=float, default=np.nan):
        return np.array([r.get(key) if r.get(key) is not None else default
                         for r in rows], dtype=dtype)[order]
    out = {
        "epoch": ep[order],
        "R": arr("R"),
        "hour": arr("hour", int, 0),
        "dow": arr("dow", int, 0),
        "side": np.array([1 if r["side"] == "buy" else 0 for r in rows], int)[order],
        "addon": np.array([bool(r.get("addon")) for r in rows], bool)[order],
        "sl_pips": arr("sl_pips"),
        "ret4h": arr("ret4h"),
        "ret24h": arr("ret24h"),
        "atr60": arr("atr60"),
    }
    train_cut = datetime.fromisoformat(TRAIN_END).replace(tzinfo=timezone.utc).timestamp()
    out["is_train"] = out["epoch"] < train_cut
    return out


def halfyear_key(epoch):
    """Consistency bucket: 2-month blocks (fits the 8-month train window)."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return f"{dt.year}B{(dt.month - 1) // 2}"
