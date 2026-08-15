"""
Broker layer — one interface, two implementations.

  PaperBroker : simulates fills and SL/TP against 1-min candles. Runs anywhere,
                used for the end-to-end test and the paper-trading phase.
  MT5Broker   : the real thing. REQUIRES WINDOWS + a running, logged-in MT5
                terminal (the MetaTrader5 package is Windows-only), so it
                cannot execute on this Linux box — it is written to spec and
                must be smoke-tested on the target host before any live use.

Both attach SL and TP to the order itself, so a position is protected from the
moment of fill and survives this process dying.
"""

from __future__ import annotations

from dataclasses import dataclass

PIP = 0.10
USD_PER_PIP_PER_LOT = 10.0


@dataclass
class Fill:
    ticket: int
    price: float
    lots: float


@dataclass
class Closure:
    ticket: int
    price: float
    reason: str          # 'tp' | 'sl' | 'manual'
    r_multiple: float


class Broker:
    def price(self) -> tuple[float, float]: raise NotImplementedError
    def place(self, side, lots, sl, tp, comment="") -> Fill | None: raise NotImplementedError
    def modify(self, ticket, sl=None, tp=None) -> bool: raise NotImplementedError
    def close(self, ticket) -> Closure | None: raise NotImplementedError
    def positions(self) -> list: raise NotImplementedError


# --------------------------------------------------------------- paper

class PaperBroker(Broker):
    """Steps through candles; fills at the next open, closes on SL/TP touch.

    Same pessimistic tie rule as the backtest engine: if one candle touches both
    the stop and the target, the STOP is assumed first.
    """

    def __init__(self, candles, spread_pips: float = 3.0):
        self.candles = candles          # [(dt, o, h, l, c), ...]
        self.i = 0
        self.spread = spread_pips * PIP
        self._next_ticket = 90_001
        self.open: dict[int, dict] = {}
        self.closed: list[Closure] = []

    # ---- clock
    def seek(self, dt) -> bool:
        """Jump to the first candle at/after dt WITHOUT evaluating stops.

        Only safe when nothing is open — skipping candles with a live position
        would miss the SL/TP that should have closed it. Use advance_to() when
        a position may be open.
        """
        if self.open:
            raise RuntimeError("seek() with an open position would skip its SL/TP; "
                               "use advance_to()")
        while self.i < len(self.candles) and self.candles[self.i][0] < dt:
            self.i += 1
        return self.i < len(self.candles)

    def advance_to(self, dt) -> list[Closure]:
        """Move the clock to dt, closing anything whose SL/TP is touched en route.

        Fast-forwards only across stretches where nothing is at risk, so a long
        quiet gap costs nothing while an open position is still evaluated candle
        by candle.
        """
        done = []
        while self.i < len(self.candles) and self.candles[self.i][0] < dt:
            if self.open:
                done += self.step()          # step() evaluates SL/TP and advances
            else:
                self.i += 1                  # nothing at risk — safe to skip
        return done

    def now(self):
        return self.candles[self.i][0] if self.i < len(self.candles) else None

    def price(self) -> tuple[float, float]:
        mid = self.candles[self.i][1]
        return mid - self.spread / 2, mid + self.spread / 2      # bid, ask

    # ---- orders
    def place(self, side, lots, sl, tp, comment="") -> Fill | None:
        if self.i >= len(self.candles):
            return None
        # Fill at the candle open and charge the round-trip cost ONCE in _r(),
        # matching engine.py exactly. Filling at bid/ask here *and* subtracting
        # the spread in _r() double-charged the cost and made every paper trade
        # ~0.035R worse than the backtest said.
        fill = self.candles[self.i][1]
        t = self._next_ticket
        self._next_ticket += 1
        self.open[t] = {"side": side, "lots": lots, "entry": fill,
                        "sl": sl, "tp": tp, "comment": comment}
        return Fill(t, round(fill, 3), lots)

    def modify(self, ticket, sl=None, tp=None) -> bool:
        p = self.open.get(ticket)
        if not p:
            return False
        if sl:
            p["sl"] = sl
        if tp:
            p["tp"] = tp
        return True

    def close(self, ticket, reason="manual") -> Closure | None:
        p = self.open.pop(ticket, None)
        if not p:
            return None
        bid, ask = self.price()
        px = bid if p["side"] == "buy" else ask
        c = Closure(ticket, round(px, 3), reason, self._r(p, px))
        self.closed.append(c)
        return c

    def positions(self):
        return [{"ticket": t, **p} for t, p in self.open.items()]

    def _r(self, p, exit_px) -> float:
        risk = abs(p["entry"] - p["sl"])
        if risk <= 0:
            return 0.0
        sign = 1 if p["side"] == "buy" else -1
        gross = sign * (exit_px - p["entry"])
        cost = self.spread                       # round-trip cost, charged once
        return round((gross - cost) / risk, 3)

    # ---- the tick that closes positions
    def step(self) -> list[Closure]:
        """Advance one candle; close anything whose SL or TP was touched."""
        if self.i >= len(self.candles):
            return []
        _, _, hi, lo, _ = self.candles[self.i]
        done = []
        for t, p in list(self.open.items()):
            long = p["side"] == "buy"
            sl_hit = (lo <= p["sl"]) if long else (hi >= p["sl"])
            tp_hit = (hi >= p["tp"]) if long else (lo <= p["tp"])
            if sl_hit:                                   # pessimistic tie
                px, reason = p["sl"], "sl"
            elif tp_hit:
                px, reason = p["tp"], "tp"
            else:
                continue
            self.open.pop(t)
            c = Closure(t, px, reason, self._r(p, px))
            self.closed.append(c); done.append(c)
        self.i += 1
        return done


# ----------------------------------------------------------------- MT5

class MT5Broker(Broker):
    """Real MetaTrader 5. Windows only — `pip install MetaTrader5` and a running
    terminal logged into the GFT account. Untested on this host by definition."""

    def __init__(self, symbol="XAUUSD", deviation=20, magic=770001):
        import MetaTrader5 as mt5           # noqa: N813  (Windows-only import)
        self.mt5 = mt5
        self.symbol = symbol
        self.deviation = deviation
        self.magic = magic
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed")

    def price(self) -> tuple[float, float]:
        t = self.mt5.symbol_info_tick(self.symbol)
        return t.bid, t.ask

    def spread_pips(self) -> float:
        bid, ask = self.price()
        return (ask - bid) / PIP

    def place(self, side, lots, sl, tp, comment="") -> Fill | None:
        mt5 = self.mt5
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
            "sl": float(sl),                      # attached at fill — never naked
            "tp": float(tp),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment[:31],              # MT5 truncates past 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            return None
        return Fill(res.order, res.price, res.volume)

    def modify(self, ticket, sl=None, tp=None) -> bool:
        mt5 = self.mt5
        pos = [p for p in (mt5.positions_get(symbol=self.symbol) or [])
               if p.ticket == ticket]
        if not pos:
            return False
        p = pos[0]
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
            "position": ticket,
            "sl": float(sl if sl else p.sl), "tp": float(tp if tp else p.tp),
        })
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

    def close(self, ticket, reason="manual") -> Closure | None:
        mt5 = self.mt5
        pos = [p for p in (mt5.positions_get(symbol=self.symbol) or [])
               if p.ticket == ticket]
        if not pos:
            return None
        p = pos[0]
        closing = (mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY
                   else mt5.ORDER_TYPE_BUY)
        bid, ask = self.price()
        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
            "volume": p.volume, "type": closing, "position": ticket,
            "price": bid if closing == mt5.ORDER_TYPE_SELL else ask,
            "deviation": self.deviation, "magic": self.magic,
            "type_filling": mt5.ORDER_FILLING_IOC,
        })
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            return None
        risk = abs(p.price_open - p.sl) or 1e-9
        sign = 1 if p.type == mt5.ORDER_TYPE_BUY else -1
        return Closure(ticket, res.price, reason,
                       round(sign * (res.price - p.price_open) / risk, 3))

    def positions(self):
        return [{"ticket": p.ticket, "side": "buy" if p.type == 0 else "sell",
                 "lots": p.volume, "entry": p.price_open, "sl": p.sl, "tp": p.tp,
                 "comment": p.comment}
                for p in (self.mt5.positions_get(symbol=self.symbol) or [])]

    def account_equity(self) -> float:
        return self.mt5.account_info().equity
