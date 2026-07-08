import math

# =============================================================================
# CONFIG — all tunable constants live here. Adjust after paper-trading review.
# =============================================================================

# Exchange costs per SIDE as fraction of notional
FEE_SIDE      = {"crypto": 0.0005, "forex": 0.00015, "stock": 0.0002}
SLIPPAGE_SIDE = {"crypto": 0.0002, "forex": 0.0001,  "stock": 0.0002}
FUNDING_EST_SWING = 0.0006  # extra round-trip cost for crypto swings (~48h of funding)

# A tag needs at least this many closed WIN/LOSS trades before its win rate is trusted for
# the EV check. Below this, we don't have enough signal to distinguish real edge from noise.
# Not mode-dependent — this is about statistical trust, not risk appetite.
MIN_SAMPLES_FOR_EV = 10

# Cushion required above the mathematical breakeven R:R for a tag's win rate — win-rate
# estimates from a finite sample are noisy and costs can drift, so we don't size trades at
# the exact knife-edge of breakeven. Not mode-dependent, same reasoning as above.
EV_SAFETY_MARGIN = 1.15

# Live-gate tag win-rate stats only (see compute_tag_win_rates) -- NOT used for the Strategy
# Dashboard's leaderboard, which must keep showing raw full-history numbers for review.
# Fixes a closed loop found 2026-07-06: a tag's all-time win rate (mostly pre-dating
# EXPLORATION_MODE/the Opportunity Engine rework) was setting a near-impossible required R:R
# for the live gate, and the only way to correct that stale number was new trades closing --
# which the stale number itself was blocking. TAG_WINRATE_WINDOW_TRADES keeps the sample
# relevant (most recent N per tag, by trade count not calendar days, so an infrequent-but-
# good tag is never penalized for trading rarely). TAG_WINRATE_SMOOTHING_K keeps a thin
# sample (even a fresh one) from being trusted as exact truth -- pulled toward the overall
# average until enough of its own data accumulates to speak for itself.
TAG_WINRATE_WINDOW_TRADES = 35
TAG_WINRATE_SMOOTHING_K   = 10

# Risk bands by setup grade (percent of equity). Chev's risk_pct is clamped here.
RISK_BANDS = {"B": (0.5, 1.0), "A": (1.0, 1.75), "A+": (1.75, 2.5)}

# Leverage caps by asset_type and trade_type — mirrors MAX_LEVERAGE_BY_TYPE in dexter.py.
# Not mode-dependent — leverage/liquidation safety never loosens, paper account or not.
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

# =============================================================================
# SETUP-QUALITY PROFILES — the part that's actually mode-dependent (2026-07-05 redesign).
#
# Everything above this line never changes regardless of mode: account survival math
# (equity floor, liquidation, leverage caps) stays strict always, paper account or not.
# Everything below is about how PICKY the gauntlet is about setup quality — and THAT is
# what should move with EXPLORATION_MODE, driven from one single place, so turning
# exploration on/off in dexter.py automatically carries the whole risk posture with it
# instead of a scattered set of constants someone has to remember to change by hand.
#
# NORMAL = the real-money-ready bar. EXPLORATION = deliberately looser, paper-only,
# data-collection priority over polish — the paper account can afford a worse setup
# in order to learn faster; real money can't. Every trade this looser bar approves
# still passes every account-safety check above unchanged.
# =============================================================================

_NORMAL = {
    "ATR_FLOOR":         {"scalp": 1.0, "day": 1.2, "swing": 1.5},
    "MAX_COST_R":        {"scalp": 0.20, "day": 0.25, "swing": 0.30},
    "MIN_NET_RR":        {"scalp": 2.0, "day": 1.8, "swing": 2.5},
    "ABS_MIN_RR":        {"scalp": 1.1, "day": 1.1, "swing": 1.3},
    "MAX_TOTAL_HEAT":    8.0,
    "MAX_CORR_SAME_DIR": 2,
    "CONCURRENCY_CAP":   {"scalp": 1, "day": 2, "swing": 2},
}

_EXPLORATION = {
    "ATR_FLOOR":         {"scalp": 0.6, "day": 0.7, "swing": 1.0},
    "MAX_COST_R":        {"scalp": 0.35, "day": 0.40, "swing": 0.45},
    "MIN_NET_RR":        {"scalp": 1.3, "day": 1.2, "swing": 1.6},
    "ABS_MIN_RR":        {"scalp": 0.8, "day": 0.8, "swing": 1.0},
    "MAX_TOTAL_HEAT":    25.0,
    "MAX_CORR_SAME_DIR": 3,
    "CONCURRENCY_CAP":   {"scalp": 2, "day": 3, "swing": 3},
}
# Starting points, not gospel — tune freely. These are the ONLY numbers that change
# between modes; everything else in this file is identical regardless of exploration_mode.

# =============================================================================


def get_active_profile(exploration_mode):
    """Public accessor for the setup-quality profile dexter.py's GEOMETRY REVIEW pre-check
    (an earlier, simpler sanity check that runs before run_gauntlet ever sees the trade)
    should use — so it shares the exact same ATR floor / R:R numbers as the gauntlet
    itself instead of maintaining its own separate, driftable copy.
    """
    return _EXPLORATION if exploration_mode else _NORMAL


# PHASE 14: the ONLY sanctioned top-level keys tunables.json's hot-reload is allowed to
# touch on the EXPLORATION profile. Deliberately small and deliberately EXPLORATION-only —
# see apply_exploration_overrides()'s docstring for why _NORMAL is untouchable by design.
EXPLORATION_TUNABLE_KEYS = {"MIN_NET_RR", "MAX_COST_R", "CONCURRENCY_CAP"}


def apply_exploration_overrides(updates):
    """PHASE 14: the ONLY sanctioned way to mutate the live _EXPLORATION profile from
    outside this module. dexter.py's load_tunables() calls this once per scan cycle, after
    it has already validated every value against its own bounds table — this function is a
    SECOND, independent lock behind that one, not a replacement for it (two doors, both
    locked): it refuses (skips, with a console warning) any top-level key not in
    EXPLORATION_TUNABLE_KEYS, any trade_type not already a key in that profile's sub-dict,
    and any non-numeric value, regardless of what the caller already checked.

    Mutates _EXPLORATION's values IN PLACE. Every existing get_active_profile() caller
    (run_gauntlet, the GEOMETRY REVIEW pre-check, /api/vitals, snapshot_tunables, fill-time
    re-validation) holds a reference to this same dict object, so in-place mutation is what
    makes a change visible everywhere on the very next call — rebinding a new dict here
    would not.

    Physically cannot touch _NORMAL — there is no mode parameter; this function only ever
    references _EXPLORATION, by name, unconditionally. That's deliberate, not an oversight:
    _NORMAL is the fixed "return to discipline" baseline, and it only means something as a
    fixed reference point. If the baseline itself could drift from a phone tap or a bad
    tunables.json, "restore defaults" would become circular.

    updates: {"MIN_NET_RR": {"scalp": 1.3, ...}, "MAX_COST_R": {...}, "CONCURRENCY_CAP": {...}}
    — any subset of keys/trade_types; unrecognized ones are refused, not raised.

    Returns exactly what was actually applied, same nested shape, for the caller to
    diff/log against its own pre-call snapshot — never the full profile, never anything
    that wasn't actually written.
    """
    applied = {}
    for key, group in (updates or {}).items():
        if key not in EXPLORATION_TUNABLE_KEYS:
            print(f"[risk_gauntlet] apply_exploration_overrides: '{key}' is not a whitelisted "
                  f"exploration tunable ({sorted(EXPLORATION_TUNABLE_KEYS)}) -- refused.")
            continue
        if not isinstance(group, dict):
            continue
        applied_group = {}
        for trade_type, value in group.items():
            if trade_type not in _EXPLORATION[key]:
                print(f"[risk_gauntlet] apply_exploration_overrides: '{key}.{trade_type}' is not "
                      f"a known trade_type — refused.")
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                print(f"[risk_gauntlet] apply_exploration_overrides: '{key}.{trade_type}' non-numeric "
                      f"value {value!r} — refused.")
                continue
            _EXPLORATION[key][trade_type] = value
            applied_group[trade_type] = value
        if applied_group:
            applied[key] = applied_group
    return applied


def _reject(code, reason, fixable=False, cost_r=None, ev_advisory_rr=None, enforced_rr_floor=None):
    """PHASE 11: cost_r/ev_advisory_rr/enforced_rr_floor are additive, optional, and only
    ever passed by the two gates (COST_GATE, NET_RR) that actually compute them — every
    earlier reject (equity/side-sanity/data-quality/stale-price/ATR-floor) and every later
    one (liquidation/heat/corr/concurrency) correctly leaves them None, since those numbers
    genuinely don't exist yet/aren't relevant at that point in the gate sequence."""
    return {
        "verdict":           "REJECT",
        "reject_code":       code,
        "reject_reason":     reason,
        "fixable":           fixable,
        "sized":             None,
        "cost_r":            cost_r,
        "ev_advisory_rr":    ev_advisory_rr,
        "enforced_rr_floor": enforced_rr_floor,
    }


def compute_tag_win_rates(closed_trades, window_trades=None, smoothing_k=0):
    """Win rate per individual confluence tag from closed journal entries. Pure — no I/O,
    no imports from dexter.py. Caller (dexter.py) reads chev_journal.json and passes the
    list in; this function only does the counting.

    closed_trades: list of dicts with 'tags' (comma-separated string) and 'outcome'
    ('WIN'/'LOSS' — anything else is ignored, e.g. still-open or malformed entries).
    Assumed already in chronological order (oldest first) — true for chev_journal.json,
    which is only ever appended to as trades close.

    window_trades: if set, only the most recent N occurrences of EACH tag count — by trade
    count, not calendar time, so an infrequent-but-good tag is never penalized for trading
    rarely; it's judged on its most recent N results whenever they happened. None (default)
    = full history, unchanged behavior — this is what the Strategy Dashboard's leaderboard
    must keep using, since it's for review, not a live gate.

    smoothing_k: if > 0, blend each tag's win rate toward the overall win rate across all of
    `closed_trades` (win_rate = (wins + k*prior) / (n + k)) so a thin sample — even a fresh
    one — isn't trusted as exact truth. 0 (default) = no shrinkage, unchanged behavior.

    Returns {tag: {"n": int, "wins": int, "win_rate": float}} for every tag seen. `n`/`wins`
    reflect the windowed count when window_trades is set; `win_rate` reflects the smoothed
    value when smoothing_k is set. Manual-close entries append a literal " manual-close"
    suffix onto the tags string (not a real tag) — stripped here before splitting.
    """
    valid = [t for t in closed_trades if t.get("outcome") in ("WIN", "LOSS")]

    prior = 0.5
    if smoothing_k and valid:
        prior = sum(1 for t in valid if t["outcome"] == "WIN") / len(valid)

    stats = {}
    # Most-recent-first when windowing so the per-tag cap keeps the newest N; order doesn't
    # matter when there's no cap, so leave chronological (harmless, avoids reversing for
    # nothing on the Strategy Dashboard's unwindowed call).
    trades_iter = reversed(valid) if window_trades else valid
    for t in trades_iter:
        outcome = t["outcome"]
        raw = (t.get("tags") or "").replace(" manual-close", "")
        for tok in raw.split(","):
            tag = tok.strip().lower()
            if not tag:
                continue
            s = stats.setdefault(tag, {"n": 0, "wins": 0})
            if window_trades and s["n"] >= window_trades:
                continue
            s["n"] += 1
            if outcome == "WIN":
                s["wins"] += 1

    for s in stats.values():
        if smoothing_k:
            s["win_rate"] = (s["wins"] + smoothing_k * prior) / (s["n"] + smoothing_k)
        else:
            s["win_rate"] = s["wins"] / s["n"] if s["n"] else 0.0
    return stats


def compute_combo_win_rates(closed_trades, min_tags=2):
    """Win rate per exact confluence COMBINATION (order-independent) from closed journal
    entries — the sibling of compute_tag_win_rates, one level more specific. As of
    2026-07-05 this data has almost no usable sample size yet (checked directly against
    the real journal: every combination except one sits at n=1-2), so the EV gate in
    run_gauntlet() still uses single-tag weakest-link, not this. This function exists so
    the data is VISIBLE and trackable as it accumulates, not to feed a gate yet — surfaced
    on the Strategy dashboard, not wired into any reject decision.

    min_tags: combinations with fewer than this many tags are skipped (a single tag is
    already covered by compute_tag_win_rates; this is specifically about combinations).

    Returns {combo_key: {"n": int, "wins": int, "win_rate": float, "tags": [str, ...]}}
    where combo_key is the tags sorted and comma-joined, so 'fib,sr' and 'sr,fib' collapse
    to the same entry regardless of which order Chev listed them in.
    """
    stats = {}
    for t in closed_trades:
        outcome = t.get("outcome")
        if outcome not in ("WIN", "LOSS"):
            continue
        raw = (t.get("tags") or "").replace(" manual-close", "")
        tags = sorted(set(tok.strip().lower() for tok in raw.split(",") if tok.strip()))
        if len(tags) < min_tags:
            continue
        key = ",".join(tags)
        s = stats.setdefault(key, {"n": 0, "wins": 0, "tags": tags})
        s["n"] += 1
        if outcome == "WIN":
            s["wins"] += 1
    for s in stats.values():
        s["win_rate"] = s["wins"] / s["n"] if s["n"] else 0.0
    return stats


def compute_planned_rr(entry, sl, tp, asset_type, trade_type="day"):
    """The exact same cost-adjusted net R:R formula gate 8 (NET_RR) uses inside
    run_gauntlet() — pulled out standalone so anything reporting on CLOSED trades (the
    Strategy dashboard's R:R distribution, for one) shows Dexter's real planned R:R at
    entry time, not an approximation. Previously the dashboard derived R from
    pnl / (stop_pct * position_size_usd) — that's a REALIZED-outcome R-multiple, a
    different and also-legitimate number, but it is not what Dexter computed when judging
    the trade, and conflating the two was inaccurate. This function is that real number.

    trade_type defaults to "day" because chev_journal.json does not currently store
    trade_type on closed entries (a real gap, not fixed here) — this only shifts the
    funding-cost term for crypto swings, a small effect, but worth knowing the default
    is a guess for entries where the real trade_type wasn't logged.
    """
    if not entry:
        return None
    stop_pct = abs(entry - sl) / entry
    tp_pct   = abs(tp - entry) / entry
    a = asset_type if asset_type in FEE_SIDE else "crypto"
    cost_rt = 2 * (FEE_SIDE[a] + SLIPPAGE_SIDE[a])
    if a == "crypto" and trade_type == "swing":
        cost_rt += FUNDING_EST_SWING
    denom = stop_pct + cost_rt
    if denom <= 0:
        return None
    return (tp_pct - cost_rt) / denom


def run_gauntlet(trade, result, balance, live_price, asset_type, open_trades, corr_symbols, grade="B",
                  tag_stats=None, exploration_mode=False):
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
        tag_stats:    dict from compute_tag_win_rates(), or None/{} if not available yet —
                      drives the EV-aware NET_RR gate; falls back to the flat MIN_NET_RR
                      floor for any tag without enough samples.
        exploration_mode: mirrors dexter.py's EXPLORATION_MODE flag — selects the
                      EXPLORATION setup-quality profile (looser ATR floor/cost gate/R:R/
                      heat/correlation/concurrency) instead of NORMAL. Account-safety
                      checks (equity, liquidation, leverage caps) never change with this.

    Returns:
        {
            "verdict":       "PASS" | "REJECT",
            "reject_code":   str | None,
            "reject_reason": str | None,
            "fixable":       bool,   # True only for ATR_FLOOR and NET_RR
            "sized":         dict | None,
            # PHASE 11 — additive, top-level regardless of verdict (unlike sized["cost_R"],
            # which only ever existed on PASS): None until gate 7 (cost_r) / gate 8
            # (ev_advisory_rr, enforced_rr_floor) actually runs and computes them, so an
            # earlier reject (equity/side-sanity/data-quality/stale-price/ATR-floor) or a
            # later one (liquidation/heat/corr/concurrency) correctly carries None here.
            "cost_r":            float | None,
            "ev_advisory_rr":    float | None,  # the EV-computed R:R whenever a qualifying
                                                 # tag exists, whether or not it was actually
                                                 # enforced (exploration mode logs it as
                                                 # advisory-only, see gate 8 below) — always
                                                 # the raw number when computed
            "enforced_rr_floor": float | None,  # whichever R:R was actually enforced (flat
                                                 # floor or EV number, matches `basis`)
        }
    """
    prof = _EXPLORATION if exploration_mode else _NORMAL
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
    if trade_type not in prof["ATR_FLOOR"]:
        trade_type = "day"

    if atr_ok:
        atr_min  = prof["ATR_FLOOR"][trade_type] * atr
        sl_dist  = abs(entry - sl)
        if sl_dist < atr_min:
            return _reject(
                "ATR_FLOOR",
                f"SL distance {sl_dist:.5f} < ATR floor "
                f"({prof['ATR_FLOOR'][trade_type]}x ATR = {atr_min:.5f}) for a {trade_type} trade. "
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
    if cost_R > prof["MAX_COST_R"][trade_type]:
        min_stop_pct = cost_rt / prof["MAX_COST_R"][trade_type]
        return _reject(
            "COST_GATE",
            f"Round-trip cost {cost_rt:.4%} of notional = {cost_R:.2f}R at stop {stop_pct:.4%} — "
            f"exceeds max {prof['MAX_COST_R'][trade_type]}R for {trade_type}. "
            f"For a {trade_type} trade on this asset, the SL must sit at least "
            f"{min_stop_pct:.4%} from entry. Widen the SL to real structure at or beyond "
            f"that distance, pick a trade_type whose ceiling fits, or SKIP.",
            fixable=True, cost_r=round(cost_R, 4)
        )

    # ── 8. Net R:R — EV-aware where tag data supports it, flat floor otherwise ─────────
    net_rr = (tp_pct - cost_rt) / (stop_pct + cost_rt) if (stop_pct + cost_rt) > 0 else 0.0

    ts = tag_stats or {}
    trade_tags = [
        tok.strip().lower()
        for tok in (trade.get("tags") or "").replace(" manual-close", "").split(",")
        if tok.strip()
    ]
    eligible = [(tag, ts[tag]) for tag in trade_tags if tag in ts and ts[tag]["n"] >= MIN_SAMPLES_FOR_EV]

    ev_advisory_note = None
    if eligible:
        # WEAKEST LINK: size the requirement off the worst-performing qualifying tag, not
        # an average — matches Chev's own "confidence equals the weakest evidence" principle.
        weak_tag, weak_stat = min(eligible, key=lambda kv: kv[1]["win_rate"])
        p = min(max(weak_stat["win_rate"], 0.01), 0.99)  # clamp away from 0/1 degenerate breakeven
        breakeven_rr   = (1 - p) / p
        ev_required_rr = max(breakeven_rr * EV_SAFETY_MARGIN, prof["ABS_MIN_RR"][trade_type])
        ev_basis = (f"EV-based: weakest qualifying tag '{weak_tag}' win_rate={p:.0%} (n={weak_stat['n']}) "
                    f"-> breakeven {breakeven_rr:.2f}:1 x {EV_SAFETY_MARGIN} margin")

        if exploration_mode:
            # EXPLORATION: the win-rate data feeding the EV number was earned by a
            # different, pre-fix-era system — enforcing it here would silently re-tighten
            # the exact entry bar exploration exists to loosen. Compute and log it as an
            # advisory number so it stays visible, but enforce only the flat floor.
            required_rr = prof["MIN_NET_RR"][trade_type]
            basis = f"flat floor (exploration — EV advisory only, see notes): {required_rr:.2f}:1"
            ev_advisory_note = (
                f"EV advisory (not enforced, exploration): weakest tag '{weak_tag}' "
                f"win_rate={p:.0%} (n={weak_stat['n']}) would have required {ev_required_rr:.2f}:1; "
                f"enforcing flat floor {required_rr:.2f}:1"
            )
        else:
            required_rr = ev_required_rr
            basis = ev_basis
    else:
        required_rr = prof["MIN_NET_RR"][trade_type]
        basis = f"flat floor: no tag on this trade has >= {MIN_SAMPLES_FOR_EV} closed samples yet"

    if ev_advisory_note:
        notes.append(ev_advisory_note)

    if net_rr < required_rr:
        return _reject(
            "NET_RR",
            f"Net R:R after costs = {net_rr:.2f} < required {required_rr:.2f} for {trade_type} ({basis}). "
            f"Revise TP/SL to achieve at least {required_rr:.2f}:1 net R:R.",
            fixable=True, cost_r=round(cost_R, 4),
            ev_advisory_rr=(round(ev_required_rr, 4) if eligible else None),
            enforced_rr_floor=round(required_rr, 4)
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
    if existing_heat + realized_risk_pct > prof["MAX_TOTAL_HEAT"]:
        return _reject(
            "HEAT_CAP",
            f"Adding {realized_risk_pct:.2f}% would bring total heat to "
            f"{existing_heat + realized_risk_pct:.2f}% — above hard cap {prof['MAX_TOTAL_HEAT']:.0f}%."
        )

    # ── 13. Correlation cap ───────────────────────────────────────────────────
    symbol = result.get("symbol", "")
    if a == "crypto" and symbol in corr_symbols:
        corr_count = sum(
            1 for t in active
            if t.get("symbol") in corr_symbols
            and (t.get("direction") or "").lower() == direction
        )
        if corr_count >= prof["MAX_CORR_SAME_DIR"]:
            return _reject(
                "CORR_CAP",
                f"Already {corr_count} correlated {direction} crypto positions open/pending — "
                f"max is {prof['MAX_CORR_SAME_DIR']}."
            )

    # ── 14. Concurrency cap ───────────────────────────────────────────────────
    concurrent = sum(1 for t in active if (t.get("trade_type") or "day").lower() == trade_type)
    cap = prof["CONCURRENCY_CAP"].get(trade_type, 2)
    if concurrent >= cap:
        return _reject(
            "CONCURRENCY",
            f"Already {concurrent} {trade_type} trades open/pending — max is {cap}."
        )

    # ── PASS ──────────────────────────────────────────────────────────────────
    notes.append(f"profile={'EXPLORATION' if exploration_mode else 'NORMAL'}")
    notes.append(f"NET_RR basis: {basis} (required {required_rr:.2f}:1, actual {net_rr:.2f}:1)")
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
        "cost_r":            round(cost_R, 4),
        "ev_advisory_rr":    (round(ev_required_rr, 4) if eligible else None),
        "enforced_rr_floor": round(required_rr, 4),
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

    # 5. Cost gate — scalp with 0.05% stop (cost_rt=0.14%, cost_R=2.8R > MAX_COST_R[scalp]=0.20).
    #    Phase 2 (2026-07-06): must now be fixable=True, and the message must state the real
    #    minimum stop distance — computed from this test's own inputs, not hardcoded.
    R5 = {**R, "atr": 30.0, "trade_type": "scalp"}
    T5 = {**T, "entry": 100_000.0, "sl": 99_950.0, "tp": 100_200.0, "trade_type": "scalp"}
    r5 = run_gauntlet(T5, R5, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A")
    _cost_rt_5      = 2 * (FEE_SIDE["crypto"] + SLIPPAGE_SIDE["crypto"])
    _min_stop_pct_5 = _cost_rt_5 / _NORMAL["MAX_COST_R"]["scalp"]
    ok5 = (r5["verdict"] == "REJECT" and r5.get("reject_code") == "COST_GATE" and
           r5.get("fixable") is True and
           f"{_min_stop_pct_5:.4%}" in (r5.get("reject_reason") or ""))
    print(f"[{'PASS' if ok5 else 'FAIL'}] 5. Cost gate (tight scalp stop) — fixable=True, "
          f"states min stop {_min_stop_pct_5:.4%}: verdict={r5['verdict']} "
          f"code={r5.get('reject_code')} fixable={r5.get('fixable')}")
    _PASS += 1 if ok5 else 0
    _FAIL += 0 if ok5 else 1

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

    # 8. Heat cap — 25% existing heat, adding any more trips the 25% hard cap
    HOT = [
        {"status": "OPEN",    "risk_pct": 10.0, "direction": "long", "symbol": "ETHUSDT", "trade_type": "day", "margin_reserved": 100},
        {"status": "OPEN",    "risk_pct": 10.0, "direction": "long", "symbol": "SOLUSDT", "trade_type": "day", "margin_reserved": 100},
        {"status": "PENDING", "risk_pct": 5.0,  "direction": "long", "symbol": "BNBUSDT", "trade_type": "day", "margin_reserved": 100},
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

    # 14. EV-based PASS below the old flat floor — strong tag (80% win, n=20) only needs
    #     breakeven 0.25 x 1.15 margin = 0.29, floored by ABS_MIN_RR[day]=1.1. net_rr=1.30
    #     would have FAILED the old flat MIN_NET_RR[day]=1.8 — EV correctly allows it.
    TAGS_STRONG = {"ema_1h": {"n": 20, "wins": 16, "win_rate": 0.8}}
    T14 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 101_622.0, "tags": "ema_1h"}
    r14 = run_gauntlet(T14, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A", tag_stats=TAGS_STRONG)
    ok14 = r14["verdict"] == "PASS"
    print(f"[{'PASS' if ok14 else 'FAIL'}] 14. EV allows net_rr=1.30 on strong tag (old flat 1.8 would reject): "
          f"verdict={r14['verdict']} code={r14.get('reject_code')}")
    _PASS += 1 if ok14 else 0
    _FAIL += 0 if ok14 else 1

    # 15. EV-based REJECT above the old flat floor — weak tag (15% win, n=20) needs
    #     breakeven 5.67 x 1.15 margin = 6.52. net_rr=2.20 would have PASSED the old flat
    #     MIN_NET_RR[day]=1.8 — EV correctly catches it as a bad setup.
    TAGS_WEAK = {"cp": {"n": 20, "wins": 3, "win_rate": 0.15}}
    T15 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 102_648.0, "tags": "cp"}
    _case("15. EV rejects net_rr=2.20 on weak tag (old flat 1.8 would pass)",
          "NET_RR",
          run_gauntlet(T15, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A", tag_stats=TAGS_WEAK))

    # 16. Weakest link across multiple tags — one strong (80%) + one weak (15%) tag on the
    #     same trade. Requirement must be driven by the WEAK tag, not an average of the two.
    TAGS_MIXED = {**TAGS_STRONG, **TAGS_WEAK}
    T16 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 102_648.0, "tags": "ema_1h,cp"}
    _case("16. Weakest-link tag selection (strong+weak combo rejects like weak alone)",
          "NET_RR",
          run_gauntlet(T16, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A", tag_stats=TAGS_MIXED))

    # 17. Fallback to flat floor when sample size is too thin (n=5 < MIN_SAMPLES_FOR_EV=10) —
    #     same net_rr=1.30 as test 14, but this tag doesn't have enough data to trust yet,
    #     so it must fall back to the flat MIN_NET_RR[day]=1.8 and REJECT.
    TAGS_THIN = {"cp": {"n": 5, "wins": 1, "win_rate": 0.2}}
    T17 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 101_622.0, "tags": "cp"}
    _case("17. Thin sample (n=5) falls back to flat floor, rejects",
          "NET_RR",
          run_gauntlet(T17, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A", tag_stats=TAGS_THIN))

    # ── EXPLORATION_MODE LINKAGE (2026-07-05) — one flag drives the whole risk posture ──

    # 18/19. Same trade, same everything — ONLY exploration_mode differs. 900pt stop on
    #        1000 ATR: NORMAL floor (1.2x=1200) rejects it, EXPLORATION floor (0.7x=700)
    #        accepts it. Proves the toggle actually reaches the gate, not just cosmetic.
    T1819 = {**T, "entry": 100_000.0, "sl": 99_100.0, "tp": 101_500.0}
    R1819 = {**R, "atr": 1000.0}
    _case("18. exploration_mode=False (default) — ATR floor rejects a 900pt stop on 1000 ATR",
          "ATR_FLOOR",
          run_gauntlet(T1819, R1819, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A"))
    r19 = run_gauntlet(T1819, R1819, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A", exploration_mode=True)
    ok19 = r19["verdict"] == "PASS"
    print(f"[{'PASS' if ok19 else 'FAIL'}] 19. exploration_mode=True — same trade now PASSES (looser ATR floor): "
          f"verdict={r19['verdict']} code={r19.get('reject_code')}")
    _PASS += 1 if ok19 else 0
    _FAIL += 0 if ok19 else 1

    # 20/21. Heat cap toggle — 10% existing heat, NORMAL cap=8% rejects any addition,
    #        EXPLORATION cap=25% accepts it. Same portfolio, same proposal, only the mode differs.
    HEAT_TOGGLE = [
        {"status": "OPEN", "risk_pct": 10.0, "direction": "long", "symbol": "ETHUSDT", "trade_type": "day", "margin_reserved": 100},
    ]
    _case("20. exploration_mode=False (default) — 10% existing heat trips the 8% NORMAL cap",
          "HEAT_CAP",
          run_gauntlet(T, R, 5_000, 100_000, "crypto", HEAT_TOGGLE, CORR, "A"))
    r21 = run_gauntlet(T, R, 5_000, 100_000, "crypto", HEAT_TOGGLE, CORR, "A", exploration_mode=True)
    ok21 = r21["verdict"] == "PASS"
    print(f"[{'PASS' if ok21 else 'FAIL'}] 21. exploration_mode=True — same portfolio now PASSES (25% heat cap): "
          f"verdict={r21['verdict']} code={r21.get('reject_code')}")
    _PASS += 1 if ok21 else 0
    _FAIL += 0 if ok21 else 1

    # 22. Windowing lets recent performance supersede old performance once enough new trades
    #     close — this is the actual property that fixes the stale-win-rate bug. 20 old
    #     losses followed by 30 recent wins: full history is 30/50=60%, but the most recent
    #     35 trades (5 old losses + all 30 wins) should show a materially higher rate — the
    #     tag isn't stuck being judged by trades that happened before it started performing.
    _recency = ([{"tags": "improving_tag", "outcome": "LOSS"} for _ in range(20)] +
                [{"tags": "improving_tag", "outcome": "WIN"}  for _ in range(30)])
    full_stats     = compute_tag_win_rates(_recency)
    windowed_stats = compute_tag_win_rates(_recency, window_trades=35)
    full_rate = full_stats["improving_tag"]["win_rate"]
    win_n     = windowed_stats["improving_tag"]["n"]
    win_rate  = windowed_stats["improving_tag"]["win_rate"]
    ok22 = (win_n == 35 and abs(full_rate - 0.60) < 1e-9 and win_rate > 0.80)
    print(f"[{'PASS' if ok22 else 'FAIL'}] 22. Windowing lets recent improvement show: "
          f"full-history rate={full_rate:.0%} vs most-recent-{win_n} rate={win_rate:.0%}")
    _PASS += 1 if ok22 else 0
    _FAIL += 0 if ok22 else 1

    # 23. Smoothing pulls a thin sample toward the overall average; a thick sample barely
    #     moves. 45 trades total, overall win rate 40% (18/45). thin_tag: 2 trades, both
    #     losses (raw 0%) should land much closer to 40% than to 0%. thick_tag: 40 trades at
    #     the same 40% overall rate should barely move from its own raw rate.
    _mix = [{"tags": "thick_tag", "outcome": "WIN" if i % 5 < 2 else "LOSS"} for i in range(40)]  # 40% raw
    _mix += [{"tags": "thin_tag", "outcome": "LOSS"}, {"tags": "thin_tag", "outcome": "LOSS"}]
    _mix += [{"tags": "thick_tag", "outcome": "WIN"}, {"tags": "thick_tag", "outcome": "LOSS"},
             {"tags": "thick_tag", "outcome": "LOSS"}]  # padding so overall count/rate is clean
    stats_smoothed = compute_tag_win_rates(_mix, smoothing_k=10)
    thin_rate  = stats_smoothed["thin_tag"]["win_rate"]
    thick_rate = stats_smoothed["thick_tag"]["win_rate"]
    ok23 = (0.15 < thin_rate < 0.40) and abs(thick_rate - 0.40) < 0.10
    print(f"[{'PASS' if ok23 else 'FAIL'}] 23. Shrinkage: thin_tag (n=2, raw=0%) -> {thin_rate:.0%} "
          f"(pulled toward overall avg), thick_tag (n=43, raw~40%) -> {thick_rate:.0%} (barely moved)")
    _PASS += 1 if ok23 else 0
    _FAIL += 0 if ok23 else 1

    # 24/25. EV-NEUTRAL EXPLORATION (2026-07-06 phase 1) — a poisoned tag (20% win, n=30)
    #        would normally demand breakeven 4.0 x 1.15 margin = 4.60:1. Same trade+tags,
    #        same net_rr=1.30, only exploration_mode differs.
    TAGS_POISONED = {"cp": {"n": 30, "wins": 6, "win_rate": 0.20}}
    T2425 = {**T, "entry": 100_000.0, "sl": 99_000.0, "tp": 101_622.0, "tags": "cp"}
    r24 = run_gauntlet(T2425, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A",
                        tag_stats=TAGS_POISONED, exploration_mode=True)
    ok24 = (r24["verdict"] == "PASS" and r24["sized"] is not None and
            any("EV advisory" in n and "would have required" in n for n in r24["sized"].get("notes", [])))
    print(f"[{'PASS' if ok24 else 'FAIL'}] 24. exploration_mode=True — poisoned tag (20% win, n=30) "
          f"PASSES at flat floor, EV advisory logged not enforced: verdict={r24['verdict']} "
          f"notes={r24['sized'].get('notes') if r24['sized'] else None}")
    _PASS += 1 if ok24 else 0
    _FAIL += 0 if ok24 else 1

    # 24b. PHASE 11 — the EV number Phase 1 buried in a notes string is now ALSO a
    # structured top-level field, computed regardless of whether exploration enforces it.
    # breakeven = (1-0.20)/0.20 = 4.0; ev_advisory_rr = 4.0 * EV_SAFETY_MARGIN(1.15) = 4.60.
    # enforced_rr_floor in exploration mode is the FLAT floor (MIN_NET_RR["day"]=1.2), not
    # the EV number -- confirms advisory-only-in-exploration is preserved, unchanged.
    ok24b = (r24.get("ev_advisory_rr") is not None and abs(r24["ev_advisory_rr"] - 4.60) < 0.01
             and r24.get("enforced_rr_floor") is not None and abs(r24["enforced_rr_floor"] - 1.2) < 0.01
             and r24.get("cost_r") is not None and abs(r24["cost_r"] - 0.14) < 0.01
             and r24["sized"]["cost_R"] == r24["cost_r"])   # existing sized["cost_R"] unchanged, matches new top-level field
    print(f"[{'PASS' if ok24b else 'FAIL'}] 24b. structured fields on PASS: ev_advisory_rr="
          f"{r24.get('ev_advisory_rr')} (want ~4.60) enforced_rr_floor={r24.get('enforced_rr_floor')} "
          f"(want ~1.2, the flat floor) cost_r={r24.get('cost_r')} (want ~0.14)")
    _PASS += 1 if ok24b else 0
    _FAIL += 0 if ok24b else 1

    r25 = run_gauntlet(T2425, R, 5_000, 100_000, "crypto", NO_TRADES, CORR, "A",
                        tag_stats=TAGS_POISONED, exploration_mode=False)
    ok25 = r25["verdict"] == "REJECT" and r25.get("reject_code") == "NET_RR"
    print(f"[{'PASS' if ok25 else 'FAIL'}] 25. exploration_mode=False — identical poisoned-tag trade "
          f"still REJECTS on EV (unchanged from today): verdict={r25['verdict']} code={r25.get('reject_code')}")
    _PASS += 1 if ok25 else 0
    _FAIL += 0 if ok25 else 1

    # 25b. PHASE 11 — same structured fields, but non-exploration ENFORCES the EV number
    # directly, so enforced_rr_floor == ev_advisory_rr here (both 4.60), unlike test 24b.
    ok25b = (r25.get("ev_advisory_rr") is not None and abs(r25["ev_advisory_rr"] - 4.60) < 0.01
             and r25.get("enforced_rr_floor") is not None and abs(r25["enforced_rr_floor"] - 4.60) < 0.01
             and r25.get("cost_r") is not None and abs(r25["cost_r"] - 0.14) < 0.01
             and r25["sized"] is None)   # REJECT still carries sized=None, unchanged
    print(f"[{'PASS' if ok25b else 'FAIL'}] 25b. structured fields on REJECT: ev_advisory_rr="
          f"{r25.get('ev_advisory_rr')} enforced_rr_floor={r25.get('enforced_rr_floor')} "
          f"(both ~4.60 -- non-exploration enforces the EV number directly) cost_r={r25.get('cost_r')}")
    _PASS += 1 if ok25b else 0
    _FAIL += 0 if ok25b else 1

    # 26. Existing keys on an EARLY reject (equity floor, gate 1) are unchanged, and the
    # new PHASE 11 fields correctly stay None -- cost_r/ev_advisory_rr/enforced_rr_floor
    # are never computed that early in the gate sequence.
    r26 = run_gauntlet(T, R, 0, 100_000, "crypto", NO_TRADES, CORR, "A")
    ok26 = (r26["verdict"] == "REJECT" and r26["reject_code"] == "NO_EQUITY"
            and r26["sized"] is None and r26["fixable"] is False
            and r26.get("cost_r") is None and r26.get("ev_advisory_rr") is None
            and r26.get("enforced_rr_floor") is None)
    print(f"[{'PASS' if ok26 else 'FAIL'}] 26. early reject (NO_EQUITY): existing keys unchanged, "
          f"new PHASE 11 fields correctly None (not computed yet at gate 1)")
    _PASS += 1 if ok26 else 0
    _FAIL += 0 if ok26 else 1

    # 27. PHASE 14 — apply_exploration_overrides: a valid change to a whitelisted key/
    # trade_type is applied in place and returned; restore afterward so this suite stays
    # side-effect-free for any future test appended after it.
    _orig_min_net_rr_scalp = _EXPLORATION["MIN_NET_RR"]["scalp"]
    r27 = apply_exploration_overrides({"MIN_NET_RR": {"scalp": 1.7}})
    ok27 = (r27 == {"MIN_NET_RR": {"scalp": 1.7}}
            and _EXPLORATION["MIN_NET_RR"]["scalp"] == 1.7)
    _EXPLORATION["MIN_NET_RR"]["scalp"] = _orig_min_net_rr_scalp  # restore
    print(f"[{'PASS' if ok27 else 'FAIL'}] 27. valid override applied + returned, restored after")
    _PASS += 1 if ok27 else 0
    _FAIL += 0 if ok27 else 1

    # 28. A top-level key outside EXPLORATION_TUNABLE_KEYS (e.g. ATR_FLOOR) is refused —
    # not applied, not returned, and the live profile is untouched.
    _orig_atr_floor_day = _EXPLORATION["ATR_FLOOR"]["day"]
    r28 = apply_exploration_overrides({"ATR_FLOOR": {"day": 99.0}})
    ok28 = (r28 == {} and _EXPLORATION["ATR_FLOOR"]["day"] == _orig_atr_floor_day)
    print(f"[{'PASS' if ok28 else 'FAIL'}] 28. non-whitelisted key (ATR_FLOOR) refused")
    _PASS += 1 if ok28 else 0
    _FAIL += 0 if ok28 else 1

    # 29. An unknown trade_type within a whitelisted key is refused.
    r29 = apply_exploration_overrides({"MAX_COST_R": {"not_a_type": 0.5}})
    ok29 = (r29 == {} and "not_a_type" not in _EXPLORATION["MAX_COST_R"])
    print(f"[{'PASS' if ok29 else 'FAIL'}] 29. unknown trade_type refused")
    _PASS += 1 if ok29 else 0
    _FAIL += 0 if ok29 else 1

    # 30. A non-numeric value is refused, live profile unchanged.
    _orig_cc_day = _EXPLORATION["CONCURRENCY_CAP"]["day"]
    r30 = apply_exploration_overrides({"CONCURRENCY_CAP": {"day": "not_a_number"}})
    ok30 = (r30 == {} and _EXPLORATION["CONCURRENCY_CAP"]["day"] == _orig_cc_day)
    print(f"[{'PASS' if ok30 else 'FAIL'}] 30. non-numeric value refused")
    _PASS += 1 if ok30 else 0
    _FAIL += 0 if ok30 else 1

    # 31. _NORMAL is physically unreachable through this function — no mode parameter
    # exists to target it, regardless of what's in `updates`.
    _normal_snapshot_before = {k: dict(v) if isinstance(v, dict) else v for k, v in _NORMAL.items()}
    _orig_mcr_day, _orig_cc_swing = _EXPLORATION["MAX_COST_R"]["day"], _EXPLORATION["CONCURRENCY_CAP"]["swing"]
    apply_exploration_overrides({"MIN_NET_RR": {"scalp": 9.9}, "MAX_COST_R": {"day": 0.01},
                                  "CONCURRENCY_CAP": {"swing": 1}})
    _EXPLORATION["MIN_NET_RR"]["scalp"]      = _orig_min_net_rr_scalp  # restore this call's effects
    _EXPLORATION["MAX_COST_R"]["day"]        = _orig_mcr_day
    _EXPLORATION["CONCURRENCY_CAP"]["swing"] = _orig_cc_swing
    ok31 = all(_NORMAL[k] == v for k, v in _normal_snapshot_before.items())
    print(f"[{'PASS' if ok31 else 'FAIL'}] 31. _NORMAL untouched by any call to apply_exploration_overrides")
    _PASS += 1 if ok31 else 0
    _FAIL += 0 if ok31 else 1

    print(f"\n{'='*50}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'='*50}")
