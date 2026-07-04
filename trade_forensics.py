"""
trade_forensics.py — Post-trade path attribution for Dexter/Chev.

Pure computation, no network, no state. Given a closed trade dict and the
candles that span its life (entry -> exit, plus enough history/future to
answer the questions below), reconstructs what price actually did so the
learning session can eventually write TRADE MANAGEMENT PATTERNS from real
numbers instead of guesses.

Public API: compute_forensics(trade, candles) -> dict with EXACTLY these keys:
  r_dist, mae_r, mfe_r, bars_held, exit_type_detail, stopped_then_ran,
  rsi_entry, rsi_exit

candles: chronologically ordered list of {"t","o","h","l","c"} dicts — the
exact shape labeller's _fetch_candles_for_labeller wrapper already produces.

Self-test: python -X utf8 trade_forensics.py   (offline, synthetic candles)
"""

# Mirrors dexter.py's TRADE_TYPE_EXPIRY_HOURS / labeller.py's EXPIRY_HOURS.
# Duplicated locally (like labeller.py's cost constants) so this module stays
# a standalone, dependency-free pure-function file.
TRADE_TYPE_EXPIRY_HOURS = {"scalp": 2, "day": 6, "swing": 48}

RSI_PERIOD = 14
MIN_CANDLES_BEFORE_ENTRY_FOR_RSI = 20


def _parse_ts(ts_str):
    import calendar
    from datetime import datetime
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    return calendar.timegm(dt.timetuple())


def _rsi_series(closes, period=RSI_PERIOD):
    """Wilder's RSI. Returns a list aligned to `closes`; entries before the
    warm-up point (index `period`) are None."""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = deltas[i - 1]
        gain  = delta if delta > 0 else 0.0
        loss  = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def _find_exit_idx(candles, entry_idx, is_long, sl, tp):
    """Walk forward from entry looking for the candle that actually touched
    SL or TP (pessimistic: a candle touching both is still a valid exit
    candle — which side is irrelevant here, only its index matters).
    Falls back to the last candle if neither level is ever touched in the
    supplied window (e.g. the window doesn't reach that far)."""
    for i in range(entry_idx, len(candles)):
        c = candles[i]
        hit_sl = (c["l"] <= sl) if (is_long and sl is not None) else \
                 (c["h"] >= sl) if (not is_long and sl is not None) else False
        hit_tp = (c["h"] >= tp) if (is_long and tp is not None) else \
                 (c["l"] <= tp) if (not is_long and tp is not None) else False
        if hit_sl or hit_tp:
            return i
    return len(candles) - 1


def compute_forensics(trade, candles):
    """PURE function. See module docstring for the return contract."""
    direction = trade.get("direction", "long")
    is_long   = direction == "long"
    entry     = trade.get("entry")

    result = {
        "r_dist": None, "mae_r": None, "mfe_r": None, "bars_held": None,
        "exit_type_detail": None, "stopped_then_ran": None,
        "rsi_entry": None, "rsi_exit": None,
    }

    if not candles:
        return result

    # ── Locate entry index (first candle at/after entry time) and exit index
    #    (the candle that actually touched SL/TP — found by walking forward
    #    from entry, not assumed to be the last candle in the list, since
    #    callers may include post-exit candles for stopped_then_ran). ───────
    entry_epoch = None
    if trade.get("opened_at"):
        try:
            entry_epoch = _parse_ts(trade["opened_at"])
        except Exception:
            entry_epoch = None

    entry_idx = None
    if entry_epoch is not None:
        for i, c in enumerate(candles):
            if c["t"] >= entry_epoch:
                entry_idx = i
                break
    if entry_idx is None:
        entry_idx = len(candles) - 1

    exit_idx = _find_exit_idx(candles, entry_idx, is_long, trade.get("sl"), trade.get("tp"))

    window = candles[entry_idx:exit_idx + 1]
    result["bars_held"] = len(window)

    # ── R-denominated fields: require a valid sl_original ───────────────────
    sl_original = trade.get("sl_original")
    if sl_original and entry is not None:
        r_dist = abs(entry - sl_original)
        if r_dist > 0 and window:
            result["r_dist"] = r_dist
            if is_long:
                worst = min(c["l"] for c in window)
                best  = max(c["h"] for c in window)
                result["mae_r"] = round((entry - worst) / r_dist, 3)
                result["mfe_r"] = round((best - entry) / r_dist, 3)
            else:
                worst = max(c["h"] for c in window)
                best  = min(c["l"] for c in window)
                result["mae_r"] = round((worst - entry) / r_dist, 3)
                result["mfe_r"] = round((entry - best) / r_dist, 3)

    # ── SL-exit-only fields ──────────────────────────────────────────────────
    close_type   = trade.get("close_type")
    exit_candle  = candles[exit_idx]
    if close_type == "SL_HIT":
        sl_price = trade.get("sl")
        if sl_price is not None:
            recovered = (exit_candle["c"] > sl_price) if is_long else (exit_candle["c"] < sl_price)
            result["exit_type_detail"] = "wick" if recovered else "close"

        tp_original = trade.get("tp_original")
        if sl_original and tp_original:
            interval = candles[1]["t"] - candles[0]["t"] if len(candles) >= 2 else 0
            expiry_hours = TRADE_TYPE_EXPIRY_HOURS.get(trade.get("trade_type", "day"), 6)
            max_bars = int(expiry_hours * 3600 // interval) if interval > 0 else 0
            post_window = candles[exit_idx + 1: exit_idx + 1 + max_bars]
            ran = False
            for c in post_window:
                if is_long:
                    hit_tp = c["h"] >= tp_original
                    hit_sl = c["l"] <= sl_original
                else:
                    hit_tp = c["l"] <= tp_original
                    hit_sl = c["h"] >= sl_original
                if hit_tp and hit_sl:
                    ran = False   # pessimistic same-candle tie-break
                    break
                elif hit_tp:
                    ran = True
                    break
                elif hit_sl:
                    ran = False
                    break
            result["stopped_then_ran"] = ran

    # ── RSI at entry/exit ────────────────────────────────────────────────────
    if entry_idx >= MIN_CANDLES_BEFORE_ENTRY_FOR_RSI:
        closes = [c["c"] for c in candles]
        rsi_series = _rsi_series(closes)
        r_entry = rsi_series[entry_idx]
        r_exit  = rsi_series[exit_idx]
        result["rsi_entry"] = round(r_entry, 1) if r_entry is not None else None
        result["rsi_exit"]  = round(r_exit, 1) if r_exit is not None else None

    return result


# ── SELF-TEST (offline — synthetic candles only) ────────────────────────────
if __name__ == "__main__":
    T = []

    def check(name, cond):
        T.append((name, bool(cond)))
        print(("PASS  " if cond else "FAIL  ") + name)

    def candle(t, o, h, l, c):
        return {"t": t, "o": o, "h": h, "l": l, "c": c}

    ENTRY_TS = "2026-01-01 00:00:00"
    ENTRY_EPOCH = _parse_ts(ENTRY_TS)
    HOUR = 3600

    def warmup(n, start_price=100.0, step=0.0, before_epoch=ENTRY_EPOCH, interval=HOUR):
        """n candles strictly before entry, flat/gentle, for RSI/entry-index padding."""
        out = []
        t = before_epoch - (n + 1) * interval
        price = start_price
        for i in range(n):
            price += step
            out.append(candle(t, price, price + 0.1, price - 0.1, price))
            t += interval
        return out

    # 1. Long winner — hand-computed MFE/MAE
    #    entry=100, sl_original=95 -> r_dist=5. Window: low dips to 97 (MAE=3/5=0.6),
    #    high runs to 112 (MFE=12/5=2.4).
    trade1 = {"direction": "long", "entry": 100.0, "sl_original": 95.0, "sl": 95.0,
              "tp_original": 115.0, "trade_type": "day", "close_type": "TP_HIT",
              "opened_at": ENTRY_TS}
    win = [
        candle(ENTRY_EPOCH,           100, 101, 97,  99),
        candle(ENTRY_EPOCH + HOUR,     99, 106, 98, 105),
        candle(ENTRY_EPOCH + 2*HOUR,  105, 112,104, 111),
    ]
    r1 = compute_forensics(trade1, win)
    check("long winner MAE", r1["mae_r"] == 0.6)
    check("long winner MFE", r1["mfe_r"] == 2.4)
    check("long winner bars_held", r1["bars_held"] == 3)

    # 2. Short winner mirror — entry=100, sl_original=105 -> r_dist=5.
    #    Window: high spikes to 103 (MAE=3/5=0.6), low drops to 88 (MFE=12/5=2.4).
    trade2 = {"direction": "short", "entry": 100.0, "sl_original": 105.0, "sl": 105.0,
              "tp_original": 85.0, "trade_type": "day", "close_type": "TP_HIT",
              "opened_at": ENTRY_TS}
    win2 = [
        candle(ENTRY_EPOCH,           100, 103, 99,  99),
        candle(ENTRY_EPOCH + HOUR,     99,  99, 92,  93),
        candle(ENTRY_EPOCH + 2*HOUR,   93,  94, 88,  89),
    ]
    r2 = compute_forensics(trade2, win2)
    check("short winner MAE", r2["mae_r"] == 0.6)
    check("short winner MFE", r2["mfe_r"] == 2.4)

    # 3. SL wick-out — long: exit candle pierces SL(95) but closes back above it
    trade3 = {"direction": "long", "entry": 100.0, "sl_original": 95.0, "sl": 95.0,
              "tp_original": 115.0, "trade_type": "day", "close_type": "SL_HIT",
              "opened_at": ENTRY_TS}
    win3 = [candle(ENTRY_EPOCH, 100, 101, 94, 96)]   # low=94 pierces 95, close=96 recovers
    r3 = compute_forensics(trade3, win3)
    check("long SL wick-out", r3["exit_type_detail"] == "wick")

    # 3b. SL close-out — long: close ends below SL, no recovery
    trade3b = dict(trade3)
    win3b = [candle(ENTRY_EPOCH, 100, 101, 93, 94)]  # close=94 stays below 95
    r3b = compute_forensics(trade3b, win3b)
    check("long SL close-out", r3b["exit_type_detail"] == "close")

    # 3c. SL wick-out — short: SL=105, exit spikes to 106 but closes back below
    trade3c = {"direction": "short", "entry": 100.0, "sl_original": 105.0, "sl": 105.0,
               "tp_original": 85.0, "trade_type": "day", "close_type": "SL_HIT",
               "opened_at": ENTRY_TS}
    win3c = [candle(ENTRY_EPOCH, 100, 106, 99, 104)]  # high=106 pierces 105, close=104 recovers
    r3c = compute_forensics(trade3c, win3c)
    check("short SL wick-out", r3c["exit_type_detail"] == "wick")

    # 3d. SL close-out — short: close ends above SL, no recovery
    trade3d = dict(trade3c)
    win3d = [candle(ENTRY_EPOCH, 100, 107, 99, 106)]  # close=106 stays above 105
    r3d = compute_forensics(trade3d, win3d)
    check("short SL close-out", r3d["exit_type_detail"] == "close")

    # 4. stopped_then_ran = True — price reaches original TP before touching SL again
    trade4 = {"direction": "long", "entry": 100.0, "sl_original": 95.0, "sl": 95.0,
              "tp_original": 115.0, "trade_type": "day", "close_type": "SL_HIT",
              "opened_at": ENTRY_TS}
    win4 = [candle(ENTRY_EPOCH, 100, 101, 94, 96)]  # SL exit candle
    post4 = [
        candle(ENTRY_EPOCH + HOUR,     96, 100,  96, 99),   # no touch (low stays above sl_original=95)
        candle(ENTRY_EPOCH + 2*HOUR,   99, 116,  98,115),   # TP hit first
    ]
    r4 = compute_forensics(trade4, win4 + post4)
    check("stopped_then_ran True", r4["stopped_then_ran"] is True)

    # 5. stopped_then_ran = False — price touches sl_original again before TP
    trade5 = dict(trade4)
    win5 = [candle(ENTRY_EPOCH, 100, 101, 94, 96)]
    post5 = [
        candle(ENTRY_EPOCH + HOUR,     96, 100, 94, 97),   # touches sl_original(95) again
    ]
    r5 = compute_forensics(trade5, win5 + post5)
    check("stopped_then_ran False", r5["stopped_then_ran"] is False)

    # 6. stopped_then_ran pessimistic same-candle tie-break — both TP and SL
    #    touched in the same post-exit candle -> loss wins -> False
    trade6 = dict(trade4)
    win6 = [candle(ENTRY_EPOCH, 100, 101, 94, 96)]
    post6 = [candle(ENTRY_EPOCH + HOUR, 96, 116, 94, 110)]  # touches both 115 and 95
    r6 = compute_forensics(trade6, win6 + post6)
    check("stopped_then_ran pessimistic tie", r6["stopped_then_ran"] is False)

    # 7. Missing sl_original -> R fields None, no raise, bars_held still computed
    trade7 = {"direction": "long", "entry": 100.0, "sl": 95.0,
              "tp_original": 115.0, "trade_type": "day", "close_type": "TP_HIT",
              "opened_at": ENTRY_TS}
    win7 = [
        candle(ENTRY_EPOCH,          100, 101, 97,  99),
        candle(ENTRY_EPOCH + HOUR,    99, 106, 98, 105),
    ]
    r7 = compute_forensics(trade7, win7)
    check("missing sl_original -> R fields None", r7["r_dist"] is None and r7["mae_r"] is None and r7["mfe_r"] is None)
    check("missing sl_original -> bars_held still computed", r7["bars_held"] == 2)

    # 8. Short candle history -> RSI fields None (fewer than 20 candles precede entry)
    trade8 = {"direction": "long", "entry": 100.0, "sl_original": 95.0, "sl": 95.0,
              "tp_original": 115.0, "trade_type": "day", "close_type": "TP_HIT",
              "opened_at": ENTRY_TS}
    short_hist = warmup(5) + [candle(ENTRY_EPOCH, 100, 101, 99, 100)]
    r8 = compute_forensics(trade8, short_hist)
    check("short history -> rsi_entry None", r8["rsi_entry"] is None)
    check("short history -> rsi_exit None", r8["rsi_exit"] is None)

    # 9. Enough history -> RSI fields populated (>=20 candles precede entry)
    trade9 = dict(trade8)
    long_hist = warmup(25, step=0.3) + [candle(ENTRY_EPOCH, 100, 101, 99, 100),
                                         candle(ENTRY_EPOCH + HOUR, 100, 103, 99, 102)]
    r9 = compute_forensics(trade9, long_hist)
    check("enough history -> rsi_entry populated", r9["rsi_entry"] is not None)
    check("enough history -> rsi_exit populated", r9["rsi_exit"] is not None)

    # 10. bars_held correctness — 4-candle window
    trade10 = {"direction": "long", "entry": 100.0, "sl_original": 95.0, "sl": 95.0,
               "tp_original": 115.0, "trade_type": "day", "close_type": "TP_HIT",
               "opened_at": ENTRY_TS}
    win10 = [candle(ENTRY_EPOCH + i*HOUR, 100, 101, 99, 100) for i in range(4)]
    r10 = compute_forensics(trade10, win10)
    check("bars_held correctness", r10["bars_held"] == 4)

    failed = [n for n, ok in T if not ok]
    print(f"\n{len(T) - len(failed)}/{len(T)} tests passing")
    if failed:
        raise SystemExit("FAILED: " + ", ".join(failed))
