"""
End-to-end paper run — the whole pipeline over recorded messages, no broker.

    python3 src/paper_run.py                 # replay the corpus cassette (FREE)
    python3 src/paper_run.py --live 20       # classify 20 fresh messages via API

Feeds each message through gemini classify -> regex cross-check -> guardrails ->
strategy gates -> trade registry, exactly as the live bot will, and prints the
order it WOULD place. Nothing touches a broker.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml
import parse_gary, parse_gsn
from trade_registry import TradeRegistry
from pipeline import handle_message

CASSETTE = ROOT / "tests" / "gemini_corpus_cassette.json"


class Replay:
    """Turns a recorded JSON dict back into the object the pipeline expects."""
    def __init__(self, d):
        self.__dict__.update(d)
        if self.side == "none":
            self.side = None
        if self.tp_unit == "none":
            self.tp_unit = None
        self.sl = self.sl or None
        self.entry_low = self.entry_low or None
        self.entry_high = self.entry_high or None
        self.tps = list(self.tps or [])


def main():
    cfg = yaml.safe_load((ROOT / "parameters.yml").read_text())
    live_cfg = cfg["live"]
    profile = live_cfg["risk_profiles"][live_cfg["account_profile"]]
    risk = profile["risk_ladder_pct"][0]

    rows = json.loads(CASSETTE.read_text())
    reg = TradeRegistry(":memory:")

    print("=" * 78)
    print(f"PAPER RUN — {len(rows)} recorded messages | profile={live_cfg['account_profile']} "
          f"risk={risk}% | gates: {live_cfg['side']}, fresh_only={live_cfg['fresh_only']}")
    print(f"  follow_provider_updates={live_cfg['follow_provider_updates']}  "
          f"allow_default_sl_entry={live_cfg['allow_default_sl_entry']}")
    print("=" * 78)

    tally = {}
    for r in rows:
        sig = Replay(r["gemini"])
        P = parse_gary if r["channel"] == "gary" else parse_gsn
        rx = P.parse(r["text"]) if P.looks_like_signal(r["text"]) else None
        entry = (sig.entry_low + sig.entry_high) / 2 if sig.entry_low and sig.entry_high else None

        d = handle_message(
            {"msg_id": r["msg_id"], "channel": r["channel"], "date": r["date"],
             "text": r["text"]},
            reg, cfg, lambda _t, s=sig: s,
            live_price=entry, regex_sig=rx, risk_pct=risk)

        tally[d["decision"]] = tally.get(d["decision"], 0) + 1
        first = r["text"].strip().splitlines()[0][:44]
        mark = {"take": "▶", "close": "■", "modify": "~", "alert": "!",
                "skip": "·", "ignore": " "}.get(d["decision"], "?")
        line = f" {mark} {r['date'][:10]} {r['channel']:4} {d['decision']:7}"
        if d["decision"] == "take":
            line += f" {d['side']} @{d['entry']} SL {d['sl']} TP {d['tp']} " \
                    f"({d['sl_pips']}p, {d['lots']} lots)"
        else:
            line += f" {d['reason'][:46]}"
        print(line + f"  | \"{first}\"")

    print("=" * 78)
    print("decisions:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    trades = [t for t in reg.open_trades()]
    print(f"registry: {len(trades)} open, {len(reg.events())} journal rows")
    for t in trades:
        print(f"  msg {t['msg_id']} {t['side']} SL {t['sl']} ({t['sl_source']}) "
              f"TP {t['tp']} ({t['tp_source']}) state={t['state']}")


if __name__ == "__main__":
    main()
