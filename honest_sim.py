"""
honest_sim.py -- candle-true exit simulation, real trading costs, and a daily circuit breaker.

Pure decision functions (check_exit, check_pending_fill, apply_costs) take no I/O and no
globals -- everything they need is passed in. Only record_close_R / breaker_status touch
disk (the breaker state file), and get_exit_candles touches the network (via the fetch
function passed in by the caller).
"""
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

try:
    import risk_gauntlet
    FEE_SIDE      = risk_gauntlet.FEE_SIDE
    SLIPPAGE_SIDE = risk_gauntlet.SLIPPAGE_SIDE
    FUNDING_SWING = risk_gauntlet.FUNDING_EST_SWING
except Exception as _e:
    print(f"[honest_sim] WARNING: could not import risk_gauntlet ({_e}) -- using local cost "
          f"mirror. These numbers must stay in sync with risk_gauntlet.py by hand until fixed.")
    FEE_SIDE      = {"crypto": 0.0005, "forex": 0.00015, "stock": 0.0002}
    SLIPPAGE_SIDE = {"crypto": 0.0002, "forex": 0.0001,  "stock": 0.0002}
    FUNDING_SWING = 0.0006

EXIT_TF         = {"crypto": "1m", "forex": "15m", "stock": "15m"}
EXIT_TF_SECONDS = {"1m": 60, "15m": 900}

# crypto 1m has no scanner equivalent to share a cache entry with (dexter never scans 1m
# candles otherwise), so its fetch is always dedicated -- keep it small and dynamic.
# forex/stock 15m IS also fetched by the main confluence scan every cycle (500 candles,
# see tf_fetch_limits in dexter.py) -- requesting that SAME count here means whichever of
# the two callers (exit-check or scan) runs first this cycle populates fetch_candles'
# 60s cache for the other one, cutting a real network round-trip instead of adding one.
MAX_EXIT_LIMIT   = {"crypto": 120, "forex": 30, "stock": 30}
FIXED_FETCH_SIZE = {"forex": 500, "stock": 500}

DAILY_R_HALT  = -3.0
BREAKER_STATE = r"C:\ChevTools\daily_risk.json"

_breaker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Candle fetching (the only networked function in this module)
# ---------------------------------------------------------------------------

def get_exit_candles(symbol, asset_type, since_epoch, fetch_candles_fn):
    """
    Fetch EXIT_TF candles for `asset_type` since `since_epoch` via `fetch_candles_fn`
    (same signature as dexter.fetch_candles: (symbol, asset_type, timeframe, limit) ->
    a pandas-DataFrame-like object indexed by tz-naive-UTC open time, columns
    open/high/low/close). Returns CLOSED candles only, ascending, as
    [{"t": epoch, "o":, "h":, "l":, "c":}, ...].

    Two failure-shaped returns that callers MUST distinguish:
      None -> the fetch itself raised (network/exchange error) -- caller should fall
              back to spot logic for this cycle.
      []   -> the fetch succeeded but there is nothing new yet (still-forming candle,
              or no candle since_epoch) -- this is the NORMAL case on most cycles and
              means "no exit info this cycle", NOT "fall back to spot".
    This function itself never raises.
    """
    tf         = EXIT_TF.get(asset_type, "1m")
    tf_seconds = EXIT_TF_SECONDS[tf]
    now        = time.time()

    if asset_type in FIXED_FETCH_SIZE:
        limit = FIXED_FETCH_SIZE[asset_type]  # matches the main scanner's own 15m request size
    else:
        needed = max(2, int((now - since_epoch) / tf_seconds) + 2)
        limit  = min(needed, MAX_EXIT_LIMIT.get(asset_type, 30))

    try:
        df = fetch_candles_fn(symbol, asset_type, tf, limit)
    except Exception as e:
        print(f"[honest_sim] get_exit_candles fetch failed for {symbol} {tf}: {e}")
        return None

    if df is None or getattr(df, "empty", True):
        return []

    out = []
    for idx, row in df.iterrows():
        t = int(idx.to_pydatetime().replace(tzinfo=timezone.utc).timestamp())
        if t + tf_seconds > now:
            continue  # still forming -- high/low can still repaint, never trust it
        if t < since_epoch:
            continue
        out.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                    "l": float(row["low"]), "c": float(row["close"])})
    out.sort(key=lambda c: c["t"])
    return out


# ---------------------------------------------------------------------------
# The rulebook (pure, deterministic, no I/O)
# ---------------------------------------------------------------------------

def check_exit(trade, candles):
    """
    Walk `candles` chronologically applying R1-R6. Three-state return, DO NOT branch
    on dict truthiness (a partial-only dict is still truthy and has no exit):
      None                                        -> nothing happened at all this walk
      {"exit_price": None, "close_type": None,
       "candle_ts": int, "partial": {...}}         -> 1R partial fired, trade still open
      {"exit_price": float, "close_type": str,
       "candle_ts": int, "partial": dict|None}      -> trade exited (partial may have
                                                        also fired earlier in the walk)
    Callers must test `result is not None and result["exit_price"] is not None` to
    detect an actual exit -- `if result:` is true for the partial-only case too.
    """
    is_long = trade["direction"] == "long"
    entry   = trade["entry"]
    sl      = trade["sl"]
    tp      = trade["tp"]
    sip     = bool(trade.get("sip_active"))

    orig_sl   = trade.get("original_sl", sl)
    orig_tp   = trade.get("original_tp", tp)
    orig_risk = abs(entry - orig_sl)
    risk_usd  = trade.get("risk_amount_usd", 0) or 0
    orig_tp_rr = (abs(orig_tp - entry) / orig_risk) if orig_risk > 0 else 0

    partial_eligible = (
        not trade.get("partial_done")
        and not sip
        and orig_tp_rr > 1.05
        and risk_usd > 0
        and orig_risk > 0
    )
    r1_price = entry + orig_risk if is_long else entry - orig_risk
    sl_close_type = "SIP_HIT" if sip else "SL_HIT"

    pending_partial = None

    for c in candles:
        o, h, l = c["o"], c["h"], c["l"]

        # R1 -- gap through SL: open already at/beyond SL -> exit at OPEN (worse than SL)
        gapped_sl = (o <= sl) if is_long else (o >= sl)
        if gapped_sl:
            return {"exit_price": o, "close_type": sl_close_type,
                    "candle_ts": c["t"], "partial": pending_partial}

        # R2 -- SL touch, checked BEFORE any profit logic (ambiguous candle -> SL wins)
        touched_sl = (l <= sl) if is_long else (h >= sl)
        if touched_sl:
            return {"exit_price": sl, "close_type": sl_close_type,
                    "candle_ts": c["t"], "partial": pending_partial}

        # R3 -- partial at 1R (only once; SL already ruled out above for this candle)
        if partial_eligible:
            touched_1r = (h >= r1_price) if is_long else (l <= r1_price)
            if touched_1r:
                pending_partial = {"price": r1_price, "candle_ts": c["t"]}
                partial_eligible = False

        # R4 -- gap through TP: open already at/beyond TP -> exit at TP (never better)
        gapped_tp = (o >= tp) if is_long else (o <= tp)
        if gapped_tp:
            return {"exit_price": tp, "close_type": "TP_HIT",
                    "candle_ts": c["t"], "partial": pending_partial}

        # R5 -- TP touch
        touched_tp = (h >= tp) if is_long else (l <= tp)
        if touched_tp:
            return {"exit_price": tp, "close_type": "TP_HIT",
                    "candle_ts": c["t"], "partial": pending_partial}

        # R6 -- nothing touched this candle -> next candle

    if pending_partial is not None:
        return {"exit_price": None, "close_type": None,
                "candle_ts": pending_partial["candle_ts"], "partial": pending_partial}
    return None


def check_pending_fill(trade, candles):
    """
    Returns None or {"fill_price": float, "candle_ts": int, "immediate_exit": <check_exit result>}.

    Fill price depends on order style:
      STOP-style  (entry_trigger_above and long) or (not entry_trigger_above and short) --
                  i.e. the order chases a breakout in the trigger direction. If the candle's
                  OPEN already gapped past entry in that direction, fill_price = open (worse
                  than entry -- you can't get the stop price when the market already blew
                  through it). Otherwise fill_price = entry (stop level reached intra-candle).
      LIMIT-style (the other combination) -- fill_price is always exactly entry, never
                  better, even if the open gapped through entry in your favor.

    R1-R6 are then applied to the REMAINDER of that same fill candle using its REAL open
    (not the fill price) -- a fill candle can also gap you straight through your stop, and
    that must show up as an exit at the real open, not be hidden behind the fill price.
    """
    entry         = trade["entry"]
    is_long       = trade["direction"] == "long"
    trigger_above = trade.get("entry_trigger_above", False)
    is_stop_style = (trigger_above and is_long) or ((not trigger_above) and (not is_long))

    for i, c in enumerate(candles):
        o, h, l = c["o"], c["h"], c["l"]
        triggered = (h >= entry) if trigger_above else (l <= entry)
        if not triggered:
            continue

        if is_stop_style:
            gapped_at_open = (o >= entry) if trigger_above else (o <= entry)
            fill_price = o if gapped_at_open else entry
        else:
            fill_price = entry

        remainder      = [c] + candles[i + 1:]
        immediate_exit = check_exit(trade, remainder)
        return {"fill_price": fill_price, "candle_ts": c["t"], "immediate_exit": immediate_exit}

    return None


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

def apply_costs(trade, gross_pnl, fraction=1.0):
    """
    net_pnl, cost_usd = apply_costs(trade, gross_pnl, fraction)
    cost_rt = 2*(fee + slippage) [+ funding if crypto swing]; cost_usd is charged against
    `fraction` of trade["position_size_usd"] (the notional -- leverage is already priced in).
    """
    a = trade.get("asset_type", "crypto")
    a = a if a in FEE_SIDE else "crypto"
    cost_rt = 2 * (FEE_SIDE[a] + SLIPPAGE_SIDE[a])
    if a == "crypto" and trade.get("trade_type") == "swing":
        cost_rt += FUNDING_SWING
    cost_usd = round(trade.get("position_size_usd", 0) * fraction * cost_rt, 2)
    net_pnl  = round(gross_pnl - cost_usd, 2)
    return net_pnl, cost_usd


# ---------------------------------------------------------------------------
# Daily circuit breaker
# ---------------------------------------------------------------------------

def _today_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_breaker_state():
    if not os.path.exists(BREAKER_STATE):
        return {}
    try:
        with open(BREAKER_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_breaker_state_atomic(state):
    d = os.path.dirname(BREAKER_STATE) or "."
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".daily_risk_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, BREAKER_STATE)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def record_close_R(trade, net_pnl):
    """
    Computes r_multiple = net_pnl / risk_amount_usd (0.0 with a warning if missing/zero),
    accumulates it into today's (UTC) entry in the breaker state file, and returns it.
    Call this on FULL closes only -- partials should be summed into the trade's final
    net_pnl by the caller before the trade's one record_close_R call.
    """
    risk_usd = trade.get("risk_amount_usd", 0)
    if not risk_usd:
        print(f"[honest_sim] WARNING: record_close_R for {trade.get('symbol', '?')} has no "
              f"risk_amount_usd -- r_multiple defaulting to 0.0")
        r_multiple = 0.0
    else:
        r_multiple = round(net_pnl / risk_usd, 2)

    with _breaker_lock:
        state = _read_breaker_state()
        today = _today_utc_str()
        if state.get("date") != today:
            state = {"date": today, "daily_R": 0.0}
        state["daily_R"] = round(state.get("daily_R", 0.0) + r_multiple, 2)
        _write_breaker_state_atomic(state)

    return r_multiple


def breaker_status():
    """{"halted": bool, "daily_R": float, "date": "YYYY-MM-DD"}. A new UTC day auto-un-halts."""
    with _breaker_lock:
        state = _read_breaker_state()
    today = _today_utc_str()
    if state.get("date") != today:
        return {"halted": False, "daily_R": 0.0, "date": today}
    daily_R = state.get("daily_R", 0.0)
    return {"halted": daily_R <= DAILY_R_HALT, "daily_R": daily_R, "date": today}


# ---------------------------------------------------------------------------
# Self-test -- run: python -X utf8 honest_sim.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pandas as pd

    _pass = 0
    _fail = 0

    def _check(name, cond, detail=""):
        global _pass, _fail
        if cond:
            _pass += 1
            print(f"PASS  {name}")
        else:
            _fail += 1
            print(f"FAIL  {name}  {detail}")

    def _long_trade(**over):
        t = {
            "symbol": "TESTUSDT", "asset_type": "crypto", "direction": "long",
            "entry": 100.0, "sl": 95.0, "tp": 115.0,
            "original_sl": 95.0, "original_tp": 115.0,
            "risk_amount_usd": 50.0, "position_size_usd": 1000.0,
            "trade_type": "day", "sip_active": False, "partial_done": False,
            "entry_trigger_above": False,
        }
        t.update(over)
        return t

    # 1. Wick pierces SL and closes back above -> SL_HIT at SL price
    t1 = _long_trade()
    c1 = [{"t": 1000, "o": 100, "h": 101, "l": 94.0, "c": 99}]
    r1 = check_exit(t1, c1)
    _check("1 wick pierces SL, closes above -> SL_HIT@sl", r1 and r1["close_type"] == "SL_HIT" and r1["exit_price"] == 95.0, r1)

    # 2. Same candle touches both SL and TP -> SL_HIT (pessimism)
    t2 = _long_trade()
    c2 = [{"t": 1000, "o": 100, "h": 116, "l": 94, "c": 110}]
    r2 = check_exit(t2, c2)
    _check("2 both touched same candle -> SL_HIT wins", r2 and r2["close_type"] == "SL_HIT", r2)

    # 3. Gap: opens below SL -> exit at OPEN, worse than SL
    t3 = _long_trade()
    c3 = [{"t": 1000, "o": 93.0, "h": 93.5, "l": 92, "c": 92.5}]
    r3 = check_exit(t3, c3)
    _check("3 gap through SL -> exit at open (93.0), not sl (95.0)", r3 and r3["exit_price"] == 93.0 and r3["close_type"] == "SL_HIT", r3)

    # 4. Gap: opens above TP -> exit at TP exactly, never better
    t4 = _long_trade()
    c4 = [{"t": 1000, "o": 120.0, "h": 121, "l": 119, "c": 120.5}]
    r4 = check_exit(t4, c4)
    _check("4 gap through TP -> exit at tp (115.0), not open (120.0)", r4 and r4["exit_price"] == 115.0 and r4["close_type"] == "TP_HIT", r4)

    # 5. Clean TP touch -> TP_HIT at TP
    t5 = _long_trade()
    c5 = [{"t": 1000, "o": 110, "h": 116, "l": 109, "c": 114}]
    r5 = check_exit(t5, c5)
    _check("5 clean TP touch -> TP_HIT@tp", r5 and r5["close_type"] == "TP_HIT" and r5["exit_price"] == 115.0, r5)

    # 6. Partial: candle reaches 1R only -> partial dict, trade stays open
    t6 = _long_trade()  # 1R price = 100 + 5 = 105
    c6 = [{"t": 1000, "o": 100, "h": 106, "l": 99, "c": 104}]
    r6 = check_exit(t6, c6)
    _check("6 1R touched only -> partial present, no exit",
           r6 and r6["close_type"] is None and r6["exit_price"] is None and r6["partial"]["price"] == 105.0, r6)

    # 7. Partial + TP in one candle (SL untouched) -> partial at 1R first, then TP
    t7 = _long_trade()
    c7 = [{"t": 1000, "o": 100, "h": 116, "l": 99, "c": 114}]
    r7 = check_exit(t7, c7)
    _check("7 1R + TP same candle -> partial@105 then exit TP_HIT",
           r7 and r7["close_type"] == "TP_HIT" and r7["partial"] and r7["partial"]["price"] == 105.0, r7)

    # 8. SL + 1R in one candle -> SL_HIT, NO partial (R2 before R3)
    t8 = _long_trade()
    c8 = [{"t": 1000, "o": 100, "h": 106, "l": 94, "c": 99}]
    r8 = check_exit(t8, c8)
    _check("8 SL + 1R same candle -> SL_HIT, partial=None",
           r8 and r8["close_type"] == "SL_HIT" and r8["partial"] is None, r8)

    # 9. Pending fill candle whose range also covers SL -> fill at entry AND immediate SL_HIT
    t9 = _long_trade(sl=98.0)
    c9 = [{"t": 1000, "o": 99.0, "h": 100.5, "l": 97.5, "c": 100.2}]
    r9 = check_pending_fill(t9, c9)
    _check("9 fill candle also covers SL -> fill@entry + immediate SL_HIT",
           r9 and r9["fill_price"] == 100.0 and r9["immediate_exit"] and r9["immediate_exit"]["close_type"] == "SL_HIT", r9)

    # 9b. Fill-candle gap leak: real open gaps through SL -> immediate exit at REAL open,
    #     not hidden by the fill price (limit-style long here, so fill stays at entry)
    t9b = _long_trade(entry=100.0, sl=95.0, entry_trigger_above=False)
    c9b = [{"t": 1000, "o": 90.0, "h": 99.0, "l": 88.0, "c": 89.0}]  # opens below both entry and SL
    r9b = check_pending_fill(t9b, c9b)
    _check("9b fill-candle gap through SL -> fill@entry, exit@real open (90.0)",
           r9b and r9b["fill_price"] == 100.0
           and r9b["immediate_exit"] and r9b["immediate_exit"]["exit_price"] == 90.0
           and r9b["immediate_exit"]["close_type"] == "SL_HIT", r9b)

    # 9c. Gap-up through a long BUY-STOP (entry_trigger_above=True) -> fill at the open
    t9c = _long_trade(entry=100.0, sl=95.0, tp=115.0, entry_trigger_above=True)
    c9c = [{"t": 1000, "o": 105.0, "h": 106.0, "l": 104.0, "c": 105.5}]  # gapped up through stop
    r9c = check_pending_fill(t9c, c9c)
    _check("9c gap-up through long buy-stop -> fill at open (105.0), not entry (100.0)",
           r9c and r9c["fill_price"] == 105.0, r9c)

    # 9d. Gap-down through a long LIMIT (entry_trigger_above=False) -> fill stays at entry,
    #     never better, even though the open (97.0) would have been a better fill in reality
    t9d = _long_trade(entry=100.0, sl=95.0, entry_trigger_above=False)
    c9d = [{"t": 1000, "o": 97.0, "h": 98.0, "l": 96.5, "c": 97.5}]  # below entry, above SL
    r9d = check_pending_fill(t9d, c9d)
    _check("9d gap-down through long limit -> fill at entry (100.0), not open (97.0)",
           r9d and r9d["fill_price"] == 100.0 and r9d["immediate_exit"] is None, r9d)

    # 10. Forming candle excluded from get_exit_candles
    now = float(int(time.time()))
    idx = pd.to_datetime([now - 30], unit="s")  # 30s ago -> a 1m candle covering [now-30, now+30) is still forming
    df_forming = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5]}, index=idx)
    def _fake_fetch_forming(symbol, asset_type, tf, limit):
        return df_forming
    out10 = get_exit_candles("TESTUSDT", "crypto", int(now - 3600), _fake_fetch_forming)
    _check("10 forming candle excluded -> empty result", out10 == [], out10)

    # also prove a genuinely closed candle DOES come through
    idx_closed = pd.to_datetime([now - 300], unit="s")  # 5 min ago -> long closed
    df_closed = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5]}, index=idx_closed)
    def _fake_fetch_closed(symbol, asset_type, tf, limit):
        return df_closed
    out10b = get_exit_candles("TESTUSDT", "crypto", int(now - 3600), _fake_fetch_closed)
    _check("10b closed candle included", len(out10b) == 1 and out10b[0]["o"] == 100.0, out10b)

    # 10c. Genuine fetch failure -> None (distinct from [] "nothing new yet")
    def _fake_fetch_raises(symbol, asset_type, tf, limit):
        raise ConnectionError("simulated network failure")
    out10c = get_exit_candles("TESTUSDT", "crypto", int(now - 3600), _fake_fetch_raises)
    _check("10c fetch failure -> None, not []", out10c is None, out10c)

    # 11. apply_costs: crypto day, position 10,000 -> cost 14.00; swing adds funding; fraction halves
    day_trade   = {"asset_type": "crypto", "trade_type": "day",   "position_size_usd": 10000.0}
    swing_trade = {"asset_type": "crypto", "trade_type": "swing", "position_size_usd": 10000.0}
    net_d, cost_d   = apply_costs(day_trade, 0.0, 1.0)
    net_s, cost_s   = apply_costs(swing_trade, 0.0, 1.0)
    net_half, cost_half = apply_costs(day_trade, 0.0, 0.5)
    _check("11a day cost = 14.00", cost_d == 14.00, cost_d)
    _check("11b swing cost = 20.00 (14.00 + 6.00 funding)", cost_s == 20.00, cost_s)
    _check("11c fraction=0.5 halves cost -> 7.00", cost_half == 7.00, cost_half)

    # 14. explicit funding assertion -- crypto swing must include FUNDING_SWING, day must not
    _check("14 swing cost includes funding vs day (20.00 != 14.00)", cost_s == cost_d + 10000.0 * FUNDING_SWING, (cost_s, cost_d))
    _check("14b day trade cost has zero funding component", cost_d == 10000.0 * 2 * (FEE_SIDE["crypto"] + SLIPPAGE_SIDE["crypto"]), cost_d)

    # 12. Breaker: -3.1R on one UTC date -> halted True; state file survives reload; next day -> halted False
    if os.path.exists(BREAKER_STATE):
        os.remove(BREAKER_STATE)
    bt = {"symbol": "TESTUSDT", "risk_amount_usd": 100.0}
    record_close_R(bt, -150.0)   # -1.5R
    record_close_R(bt, -160.0)   # -1.6R  => running total -3.1R
    status_today = breaker_status()
    _check("12a -3.1R halts trading today", status_today["halted"] is True and status_today["daily_R"] == -3.1, status_today)

    # simulate reload (fresh read from disk only, no in-process state)
    reread = breaker_status()
    _check("12b state survives reload", reread == status_today, reread)

    # simulate next UTC day by writing a stale date directly
    stale_state = {"date": "2000-01-01", "daily_R": -9.0}
    _write_breaker_state_atomic(stale_state)
    status_next_day = breaker_status()
    _check("12c new UTC day auto-un-halts", status_next_day["halted"] is False and status_next_day["daily_R"] == 0.0, status_next_day)
    os.remove(BREAKER_STATE)

    # 13. last_exit_check_ts advances -> same candle never processed twice
    t13 = _long_trade()
    candles_full = [{"t": 1000, "o": 100, "h": 101, "l": 99, "c": 100.5}]
    first = check_exit(t13, candles_full)
    _check("13a first pass over the only candle -> no exit, no partial", first is None, first)
    # caller advances last_exit_check_ts past 1000; the same candle must not be resliced in
    advanced_since = 1001
    remaining = [c for c in candles_full if c["t"] >= advanced_since]
    second = check_exit(t13, remaining)
    _check("13b re-run after advancing since_epoch -> empty input, no double-processing", second is None and remaining == [], (remaining, second))

    total = _pass + _fail
    print(f"\n{_pass}/{total} tests passing")
