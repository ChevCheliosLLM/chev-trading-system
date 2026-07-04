import math

# =============================================================================
# CONFIG — all tunable constants live here. Adjust after paper-trading review.
# =============================================================================

# Exchange costs per SIDE as fraction of notional
FEE_SIDE      = {"crypto": 0.0005, "forex": 0.00015, "stock": 0.0002}
SLIPPAGE_SIDE = {"crypto": 0.0002, "forex": 0.0001,  "stock": 0.0002}
FUNDING_EST_SWING = 0.0006  # extra round-trip cost for crypto swings (~48h of funding)

# ATR floor — SL distance must be >= this multiple of ATR
ATR_FLOOR = {"scalp": 1.0, "day": 1.2, "swing": 1.5}

# Cost gate — max round-trip cost expressed in R-multiples before rejecting
MAX_COST_R = {"scalp": 0.20, "day": 0.25, "swing": 0.30}

# Minimum R:R AFTER deducting costs
MIN_NET_RR = {"scalp": 2.0, "day": 1.8, "swing": 2.5}

# Risk bands by setup grade (percent of equity). Chev's risk_pct is clamped here.
RISK_BANDS = {"B": (0.5, 1.0), "A": (1.0, 1.75), "A+": (1.75, 2.5)}

# Leverage caps by asset_type and trade_type — mirrors MAX_LEVERAGE_BY_TYPE in dexter.py
LEV_CAPS = {
    "crypto": {"scalp": 10, "day": 10, "swing": 5},
    "forex":  {"scalp":  5, "day":  5, "swing": 2},
    "stock":  {"scalp":  5, "day":  5, "swing": 2},
}

MAX_MARGIN_PCT    = 0.10    # max margin per trade as fraction of equity
LIQ_SAFETY        = 0.5     # stop_pct must be < LIQ_SAFETY / leverage
MIN_EQUITY        = 100.0   # hard floor — no new trades when equity is below this
# ATR/price must land in this band or the candle data is garbage
ATR_SANITY_BAND   = (0.00005, 0.10)   # [0.005%, 10%]
STALE_PRICE_FRAC  = 0.5     # reject if price consumed > 50% of entry->SL while Chev deliberated
MAX_TOTAL_HEAT    = 8.0     # hard cap on sum of risk_pct across all open+pending trades
MAX_CORR_SAME_DIR = 2       # max same-direction positions in correlated crypto set
CONCURRENCY_CAP   = {"scalp": 1, "day": 2, "swing": 2}

# =============================================================================


def _reject(code, reason, fixable=False):
    return {
        "verdict":       "REJECT",
        "reject_code":   code,
        "reject_reason": reason,
        "fixable":       fixable,
        "sized":         None,
    }


def run_gauntlet(trade, result, balance, live_price, asset_type, open_trades, corr_symbols, grade="B"):
    """
    Validate Chev's trade proposal and compute deterministic position sizing.

    Args:
        trade:        dict from parse_chev_reply() — direction, entry, sl, tp, risk_pct, trade_type, etc.
        result:       scan result dict — atr, primary_tf, trade_type, current_price, symbol
        balance:      free cash from get_balance() (margin for open trades already deducted)
        live_price:   current market price at gauntlet time (for stale-price guard)
        asset_type:   "crypto" | "forex" | "stock"
        open_trades:  list of ALL trade dicts (OPEN + PENDING) for portfolio checks
        corr_symbols: set of correlated crypto symbols (_CRYPTO_CORR_SYMBOLS from dexter.py)
        grade:        "B" | "A" | "A+" from _setup_grade()

    Returns:
        {
            "verdict":       "PASS" | "REJECT",
            "reject_code":   str | None,
            "reject_reason": str | None,
            "fixable":       bool,   # True only for ATR_FLOOR and NET_RR
            "sized":         dict | None,
        }
    """
    notes = []
    active = [t for t in open_trades if t.get("status") in ("OPEN", "PENDING")]

    # ── 1. Equity floor ───────────────────────────────────────────────────────
    # equity = free cash + all reserved margin. Deliberately excludes unrealized PnL —
    # sizing off paper profits turns winners into overexposure.
    equity = balance + sum(float(t.get("margin_reserved", 0)) for t in active)
    if equity < MIN_EQUITY:
        return _reject(
            "NO_EQUITY",
            f"Account equity ${equity:.2f} is below minimum ${MIN_EQUITY:.2f} — "
            f"no new trades until equity recovers."
        )

    # ── 2. Side sanity ────────────────────────────────────────────────────────
    direction = (trade.get("direction") or "long").lower()
    is_long   = direction == "long"
    try:
        entry = float(trade["entry"])
        sl    = float(trade["sl"])
        tp    = float(trade["tp"])
    except (KeyError, TypeError, ValueError) as exc:
        return _reject("SL_WRONG_SIDE", f"Could not parse entry/sl/tp: {exc}")

    if is_long:
        if sl >= entry:
            return _reject("SL_WRONG_SIDE", f"LONG SL {sl} must be below entry {entry} — malformed trade.")
        if tp <= entry:
            return _reject("TP_WRONG_SIDE", f"LONG TP {tp} must be above entry {entry} — malformed trade.")
    else:
        if sl <= entry:
            return _reject("SL_WRONG_SIDE", f"SHORT SL {sl} must be above entry {entry} — malformed trade.")
        if tp >= entry:
            return _reject("TP_WRONG_SIDE", f"SHORT TP {tp} must be below entry {entry} — malformed trade.")

    stop_pct = abs(entry - sl) / entry
    tp_pct   = abs(tp  - entry) / entry

    # ── 3. ATR data quality ───────────────────────────────────────────────────
    # ATR/price outside [0.005%, 10%] means the candle data behind the whole confluence
    # analysis is garbage. Refuse to trade on untrustworthy data — don't patch around it.
    atr    = result.get("atr")
    atr_ok = False
    if not atr:
        print(f"[risk_gauntlet] WARNING: ATR missing/zero for {result.get('symbol', '?')} — ATR checks skipped.")
    else:
        atr_pct = atr / entry if entry else 0
        if not (ATR_SANITY_BAND[0] <= atr_pct <= ATR_SANITY_BAND[1]):
            return _reject(
                "DATA_QUALITY",
                f"ATR={atr} is {atr_pct:.5%} of price {entry} — outside plausible band "
                f"[{ATR_SANITY_BAND[0]:.3%}, {ATR_SANITY_BAND[1]:.0%}]. "
                f"Candle data is suspect; refusing to trade on untrustworthy ATR. Not retryable."
            )
        atr_ok = True

    # ── 4. Stale price guard ──────────────────────────────────────────────────
    # Chev's deliberation can take up to 6 minutes. If price has already consumed
    # > 50% of the SL distance on the wrong side, the R:R he judged no longer exists.
    if live_price and live_price > 0 and (entry - sl) != 0:
        if is_long:
            consumed = (entry - live_price) / (entry - sl)
        else:
            consumed = (live_price - entry) / (sl - entry)
        if consumed > STALE_PRICE_FRAC:
            return _reject(
                "STALE_PRICE",
                f"Price {live_price} has consumed {consumed:.0%} of the entry->SL distance "
                f"({entry} -> {sl}) while Chev deliberated — R:R he judged no longer exists. "
                f"Re-escalate if price returns to the zone."
            )

    # ── 5. ATR floor ─────────────────────────────────────────────────────────
    trade_type = (trade.get("trade_type") or result.get("trade_type") or "day").lower()
    if trade_type not in ATR_FLOOR:
        trade_type = "day"

    if atr_ok:
        atr_min  = ATR_FLOOR[trade_type] * atr
        sl_dist  = abs(entry - sl)
        if sl_dist < atr_min:
            return _reject(
                "ATR_FLOOR",
                f"SL distance {sl_dist:.5f} < ATR floor "
                f"({ATR_FLOOR[trade_type]}x ATR = {atr_min:.5f}) for a {trade_type} trade. "
                f"Widen SL to at least {atr_min:.5f} from entry, change trade_type, or SKIP.",
                fixable=True
            )

    # ── 6. Clamp risk ─────────────────────────────────────────────────────────
    band = RISK_BANDS.get(grade, RISK_BANDS["B"])
    try:
        chev_risk_pct = float(trade.get("risk_pct") or 1.0)
    except (TypeError, ValueError):
        chev_risk_pct = 1.0

    risk_pct = max(band[0], min(chev_risk_pct, band[1]))
    if risk_pct != chev_risk_pct:
        notes.append(
            f"risk_pct clamped {chev_risk_pct:.2f}% -> {risk_pct:.2f}% "
            f"(grade={grade} band={band[0]}-{band[1]}%)"
        )
    risk_usd = risk_pct / 100 * equity

    # ── 7. Cost model ─────────────────────────────────────────────────────────
    a = asset_type if asset_type in FEE_SIDE else "crypto"
    cost_rt = 2 * (FEE_SIDE[a] + SLIPPAGE_SIDE[a])
    if a == "crypto" and trade_type == "swing":
        cost_rt += FUNDING_EST_SWING
    cost_R = cost_rt / stop_pct if stop_pct > 0 else float("inf")
    if cost_R > MAX_COST_R[trade_type]:
        return _reject(
            "COST_GATE",
            f"Round-trip cost {cost_rt:.4%} of notional = {cost_R:.2f}R at stop {stop_pct:.4%} — "
            f"exceeds max {MAX_COST_R[trade_type]}R for {trade_type}. "
            f"Stop is so tight that costs consume {cost_R:.0%} of your risk reward. "
            f"Not fixable by SL adjustment alone."
        )

    # ── 8. Net R:R ────────────────────────────────────────────────────────────
    net_rr = (tp_pct - cost_rt) / (stop_pct + cost_rt) if (stop_pct + cost_rt) > 0 else 0.0
    if net_rr < MIN_NET_RR[trade_type]:
        return _reject(
            "NET_RR",
            f"Net R:R after costs = {net_rr:.2f} < minimum {MIN_NET_RR[trade_type]} for {trade_type}. "
            f"Revise TP/SL to achieve at least {MIN_NET_RR[trade_type]:.1f}:1 net R:R.",
            fixable=True
        )

    # ── 9. Size ───────────────────────────────────────────────────────────────
    notional = risk_usd / stop_pct if stop_pct > 0 else 0.0

    # ── 10. Leverage derivation (leverage is a consequence, never Chev's choice) ───
    lev_cap       = LEV_CAPS.get(a, {"scalp": 5, "day": 5, "swing": 2}).get(trade_type, 5)
    margin_budget = MAX_MARGIN_PCT * equity
    req_lev       = max(1, math.ceil(notional / margin_budget)) if margin_budget > 0 else lev_cap
    lev           = min(req_lev, lev_cap)

    # If leverage cap was hit, scale notional down to stay within margin budget
    if margin_budget > 0 and notional / max(lev, 1) > margin_budget:
        old_notional = notional
        notional     = lev * margin_budget
        new_risk_usd = notional * stop_pct
        new_risk_pct = new_risk_usd / equity * 100
        notes.append(
            f"Leverage cap ({lev}x) hit — notional ${old_notional:.2f} -> ${notional:.2f}; "
            f"realized risk {new_risk_pct:.2f}% (was {risk_pct:.2f}%)"
        )
        risk_pct = new_risk_pct
        risk_usd = new_risk_usd

    # ── 11. Liquidation gate ──────────────────────────────────────────────────
    # stop_pct must be < LIQ_SAFETY / leverage. Try lowering leverage step by step.
    while lev > 1 and stop_pct >= LIQ_SAFETY / lev:
        old_lev = lev
        lev    -= 1
        if margin_budget > 0 and notional / max(lev, 1) > margin_budget:
            old_notional = notional
            notional     = lev * margin_budget
            new_risk_usd = notional * stop_pct
            new_risk_pct = new_risk_usd / equity * 100
            notes.append(
                f"Lev {old_lev}x -> {lev}x for liquidation safety; "
                f"notional ${old_notional:.2f} -> ${notional:.2f}"
            )
            risk_pct = new_risk_pct
            risk_usd = new_risk_usd

    if stop_pct >= LIQ_SAFETY / max(lev, 1):
        return _reject(
            "LIQUIDATION",
            f"Stop distance {stop_pct:.4%} >= {LIQ_SAFETY:.0%} / {lev}x = {LIQ_SAFETY/max(lev,1):.4%} — "
            f"liquidation would occur before SL is reached. "
            f"No leverage in [1, {lev_cap}] resolves this. "
            f"Setup requires a wider SL or is untradeable at this risk level."
        )

    # Final realized numbers
    position_size_usd = round(notional, 2)
    margin_reserved   = round(notional / max(lev, 1), 2)
    realized_risk_usd = round(notional * stop_pct, 2)
    realized_risk_pct = round(notional * stop_pct / equity * 100, 4)

    # ── 12. Portfolio heat ────────────────────────────────────────────────────
    existing_heat = sum(float(t.get("risk_pct", 0)) for t in active)
    if existing_heat + realized_risk_pct > MAX_TOTAL_HEAT:
        return _reject(
            "HEAT_CAP",
            f"Adding {realized_risk_pct:.2f}% would bring total heat to "
            f"{existing_heat + realized_risk_pct:.2f}% — above hard cap {MAX_TOTAL_HEAT:.0f}%."
        )

    # ── 13. Correlation cap ───────────────────────────────────────────────────
    symbol = result.get("symbol", "")
    if a == "crypto" and symbol in corr_symbols:
        corr_count = sum(
            1 for t in active
            if t.get("symbol") in corr_symbols
            and (t.get("direction") or "").lower() == direction
        )
        if corr_count >= MAX_CORR_SAME_DIR:
            return _reject(
                "CORR_CAP",
                f"Already {corr_count} correlated {direction} crypto positions open/pending — "
                f"max is {MAX_CORR_SAME_DIR}."
            )

    # ── 14. Concurrency cap ───────────────────────────────────────────────────
    concurrent = sum(1 for t in active if (t.get("trade_type") or "day").lower() == trade_type)
    cap = CONCURRENCY_CAP.get(trade_type, 2)
    if concurrent >= cap:
        return _reject(
            "CONCURRENCY",
            f"Already {concurrent} {trade_type} trades open/pending — max is {cap}."
        )

    # ── PASS ──────────────────────────────────────────────────────────────────
    try:
        chev_size = float(trade.get("position_size_usd") or 0)
    except (TypeError, ValueError):
        chev_size = 0.0
    try:
        chev_lev = float(trade.get("leverage") or 1)
    except (TypeError, ValueError):
        chev_lev = 1.0

    return {
        "verdict":       "PASS",
        "reject_code":   None,
        "reject_reason": None,
        "fixable":       False,
        "sized": {
            "position_size_usd":    position_size_usd,
            "leverage":             lev,
            "margin_reserved":      margin_reserved,
            "risk_pct":             round(realized_risk_pct, 4),
            "risk_amount_usd":      realized_risk_usd,
            "stop_pct":             round(stop_pct, 6),
            "cost_R":               round(cost_R, 4),
            "net_rr":               round(net_rr, 4),
            "chev_wanted_size":     chev_size,
            "chev_wanted_leverage": chev_lev,
            "chev_wanted_risk_pct": chev_risk_pct,
            "notes":                notes,
        },
    }


# =============================================================================
# Self-test — python risk_gauntlet.py  (no network, no imports from dexter.py)
# =============================================================================
if __name__ == "__main__":

    _PASS = 0
    _FAIL = 0

    def _case(label, want_code, got):
        global _PASS, _FAIL
        if want_code in ("PASS", "REJECT"):
            ok = got["verdict"] == want_code
        else:
            ok = got.get("reject_code") == want_code
        status = "PASS" if ok else "FAIL"
        if ok:
            _PASS += 1
        else:
            _FAIL += 1
        detail = f"verdict={got['verdict']} code={got.get('reject_code')}"
        print(f"[{status}] {label}: {detail}")
        if not ok:
            print(f"       expected={want_code} | reason={got.get('reject_reason')}")

    CORR = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"}
    NO_TRADES = []

    # Shared base result and trade dicts for crypto/day at BTC price
    R = {"atr": 800.0, "symbol": "BTCUSDT", "primary_tf": "1h", "trade_type": "day", "count": 10}
    T = {
        "direction": "long", "entry": 100_000.0, "sl": 99_000.0, "tp": 103_000.0,
        "risk_pct": 1.0, "trade_type": "day",
        "position_size_usd": 0, "leverage": 1,
    }

    # 1. Long SL above entry
    _case("1. Long SL above entry",
          "SL_WRONG_SIDE",
          run_gauntlet({**T, "sl": 101_000.0}, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 2. Short TP above entry
    _case("2. Short TP above entry",
          "TP_WRONG_SIDE",
          run_gauntlet({**T, "direction": "short", "sl": 101_000.0, "tp": 102_000.0},
                       R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 3. BTC 1% stop vs SOL 3% stop — equal risk -> ~3:1 notional ratio
    R_BTC = {**R, "symbol": "BTCUSDT", "atr": 800.0}
    R_SOL = {**R, "symbol": "SOLUSDT", "atr": 3.0}
    T_BTC = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 103_000.0}           # 1% stop
    T_SOL = {**T, "entry": 150.0,     "sl": 145.5,    "tp": 159.0}               # 3% stop
    r_btc = run_gauntlet(T_BTC, R_BTC, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A")
    r_sol = run_gauntlet(T_SOL, R_SOL, 5_000, 150,     "crypto", NO_TRADES, CORR, "A")
    if r_btc["verdict"] == "PASS" and r_sol["verdict"] == "PASS":
        ratio = r_btc["sized"]["position_size_usd"] / max(r_sol["sized"]["position_size_usd"], 0.01)
        ok3   = abs(ratio - 3.0) < 0.5
        print(f"[{'PASS' if ok3 else 'FAIL'}] 3. BTC/SOL notional ratio={ratio:.2f} (expect ~3.0)")
        if ok3:
            _PASS += 1
        else:
            _FAIL += 1
    else:
        print(f"[FAIL] 3. One or both failed: BTC={r_btc.get('reject_code')} SOL={r_sol.get('reject_code')}")
        _FAIL += 1

    # 4. ATR floor — SL too tight (50 pts vs ATR_FLOOR[day]=1.2 * ATR=100 = 120 pts minimum)
    R4 = {**R, "atr": 100.0}
    T4 = {**T, "entry": 100_000.0, "sl": 99_950.0, "tp": 100_300.0}
    _case("4. ATR floor fail",
          "ATR_FLOOR",
          run_gauntlet(T4, R4, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 5. Cost gate — scalp with 0.05% stop (cost_rt=0.14%, cost_R=2.8R > MAX_COST_R[scalp]=0.20)
    R5 = {**R, "atr": 30.0, "trade_type": "scalp"}
    T5 = {**T, "entry": 100_000.0, "sl": 99_950.0, "tp": 100_200.0, "trade_type": "scalp"}
    _case("5. Cost gate (tight scalp stop)",
          "COST_GATE",
          run_gauntlet(T5, R5, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 6. Leverage cap reduction — small balance forces capping at 10x, notional scaled down
    T6 = {**T, "risk_pct": 2.5, "entry": 100_000.0, "sl": 99_000.0, "tp": 103_000.0}
    R6 = {**R, "atr": 600.0}
    r6 = run_gauntlet(T6, R6, 1_000, 100_000, "crypto", NO_TRADES, CORR, "A+")
    ok6 = r6["verdict"] == "PASS" and bool(r6["sized"]["notes"])
    print(f"[{'PASS' if ok6 else 'FAIL'}] 6. Leverage cap reduction: "
          f"verdict={r6['verdict']} notes={r6['sized']['notes'] if r6['sized'] else None}")
    _PASS += 1 if ok6 else 0
    _FAIL += 0 if ok6 else 1

    # 7. Liquidation gate — 50% stop at lev=1: stop_pct(0.5) >= LIQ_SAFETY/1(0.5)
    T7 = {**T, "entry": 100.0, "sl": 50.0, "tp": 300.0, "risk_pct": 1.0}
    R7 = {**R, "atr": 8.0, "symbol": "TESTUSDT"}
    _case("7. Liquidation gate fail",
          "LIQUIDATION",
          run_gauntlet(T7, R7, 5_000, 100, "crypto", NO_TRADES, CORR, "A"))

    # 8. Heat cap — 8% existing heat, adding any more trips the 8% hard cap
    HOT = [
        {"status": "OPEN",    "risk_pct": 3.0, "direction": "long", "symbol": "ETHUSDT", "trade_type": "day", "margin_reserved": 100},
        {"status": "OPEN",    "risk_pct": 3.0, "direction": "long", "symbol": "SOLUSDT", "trade_type": "day", "margin_reserved": 100},
        {"status": "PENDING", "risk_pct": 2.0, "direction": "long", "symbol": "BNBUSDT", "trade_type": "day", "margin_reserved": 100},
    ]
    R8 = {**R, "atr": 500.0}
    _case("8. Heat cap",
          "HEAT_CAP",
          run_gauntlet(T, R8, 5_000, 100_000, "crypto", HOT, CORR, "A"))

    # 9. Correlation cap — 2 same-direction correlated longs already open
    CORR_TRADES = [
        {"status": "OPEN", "risk_pct": 1.0, "direction": "long", "symbol": "ETHUSDT", "trade_type": "day", "margin_reserved": 100},
        {"status": "OPEN", "risk_pct": 1.0, "direction": "long", "symbol": "SOLUSDT", "trade_type": "day", "margin_reserved": 100},
    ]
    R9 = {**R, "symbol": "BTCUSDT", "atr": 500.0}
    _case("9. Correlation cap",
          "CORR_CAP",
          run_gauntlet(T, R9, 5_000, 100_000, "crypto", CORR_TRADES, CORR, "A"))

    # 10. NO_EQUITY — balance 50, no open trades, equity=50 < MIN_EQUITY=100
    _case("10. NO_EQUITY",
          "NO_EQUITY",
          run_gauntlet(T, R, 50, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 11. DATA_QUALITY — ATR=0.00001 on price=100000 -> atr_pct=1e-10 << 0.005%
    R11 = {**R, "atr": 0.00001}
    _case("11. DATA_QUALITY (junk ATR)",
          "DATA_QUALITY",
          run_gauntlet(T, R11, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    # 12. STALE_PRICE — live=99400, entry=100000, sl=99000 -> consumed=60% > 50%
    R12 = {**R, "atr": 500.0}
    _case("12. STALE_PRICE (60% consumed)",
          "STALE_PRICE",
          run_gauntlet(T, R12, 5_000, 99_400, "crypto", NO_TRADES, CORR, "A"))

    # 13. NET_RR — TP too close: entry=100000, sl=99000(1%), tp=100500(0.5%)
    #     net_rr = (0.005 - 0.0014) / (0.01 + 0.0014) = 0.316 < MIN_NET_RR[day]=1.8
    T13 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 100_500.0}
    R13 = {**R, "atr": 500.0}
    _case("13. NET_RR fail (TP too close)",
          "NET_RR",
          run_gauntlet(T13, R13, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))

    print(f"\n{'='*50}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'='*50}")
