# Question to send GoatFundedTrader support (before buying a challenge)

Send via their official support/ticket channel. Get the answer **in writing**
and keep it. The first question is the one that matters most.

---

**Subject: Clarification on EA rules — self-coded EA that uses public signal ideas**

Hello, before purchasing an evaluation I want to confirm my trading approach is
allowed, so I don't risk a breach.

I have **written my own Expert Advisor** (I can provide the full source code on
request). It trades **XAUUSD only**. My questions:

1. **Most important:** My EA takes *entry ideas* from a **public Telegram
   channel**, but applies **my own coded logic** — my own risk sizing, my own
   exit management (partial close at the first target, move stop to breakeven,
   then a trailing stop up the levels), and my own position/hedging controls. It
   is **not** a copy-trading/MAM service and not a purchased or "marketed-to-pass"
   EA. **Does using publicly available signal ideas as the entry trigger for my
   own coded EA count as a prohibited "third-party strategy"?**

2. I will run this EA on **one single account only** and will **not** copy the
   trades or ideas to any other evaluation or funded account. Correct that this
   is compliant?

3. My EA **never holds opposing positions** on the same symbol — if a signal in
   the opposite direction arrives while a trade is open, it **skips** it (or
   closes the existing trade first). Confirm this satisfies the no-hedging rule.

4. Risk is a **fixed 0.5% per trade** (no increasing size after losses, no grid,
   no Martingale). Maximum simultaneous exposure is capped at 3 positions. Is
   this acceptable?

5. I intend to use the **same EA in both the evaluation and the funded phase**
   (not EA in one phase and manual in another). Correct that this is required?

Thank you — I want to be fully compliant before I begin.

---

### Why each question
- **Q1** is the only real risk to the whole plan — their answer decides if this
  is viable at all. Everything else we can already control in code.
- **Q2–Q5** confirm the rules we've already baked into `compliant_sim.py`, so
  there are no surprises later.

### If they say signal-following IS a third-party strategy
Then either (a) don't use it on GOAT, or (b) lean fully into "my own strategy":
the trend filter + exit logic already make most of the decisions — you could
treat the channel purely as a watchlist/alert and have the EA decide
independently. But **get their written interpretation first.**
