"""
labeller.py — Triple-Barrier Labelling & Meta-Labelling (The Examiner)
Two-stage shadow records: PENDING → ACTIVE → resolved.
"""
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

# ── Config ───────────────────────────────────────────────────────────────────
CONFIG_VER     = "1.1"
DATA_DIR       = r"C:\ChevTools"
OPEN_FILE      = os.path.join(DATA_DIR, "labels_open.jsonl")
CLOSED_FILE    = os.path.join(DATA_DIR, "labels_closed.jsonl")
REPORT_FILE    = os.path.join(DATA_DIR, "label_report.txt")
DISCOVERY_FILE = os.path.join(DATA_DIR, "labels_discovery.json")  # {key: ts}, frozen on write

# R multiples — must match risk_gauntlet.py so 1R means the same thing in both systems
R_MULT = {"scalp": 1.0, "day": 1.2, "swing": 1.5}

# Active window: hours from touch before time-expiry triggers label=0 (wall-clock, not bars)
EXPIRY_HOURS = {"scalp": 2, "day": 6, "swing": 48}

# Touch detection band: price must come within max(TOUCH_PCT, TOUCH_ATR_MULT × ATR) of entry_ref
TOUCH_PCT      = 0.001    # 0.1%
TOUCH_ATR_MULT = 0.25

# Minimum effective N (1/k overlap-weighted) before reporting any stat
MIN_N_EFF    = 30    # discovery threshold; tag lift requires eff_N >= this
MIN_N_WEIGHT = 50    # weight suggestions floor

# Dexter score bands for stratified scoreboard (lo inclusive, hi exclusive)
SCORE_BANDS       = [(10, 12), (12, 14), (14, float("inf"))]
SCORE_BAND_LABELS = ["10-12", "12-14", "14+"]

# Cost constants — local copy of risk_gauntlet rates (kept for independence)
_FEE_SIDE   = {"crypto": 0.0005, "forex": 0.00015, "stock": 0.0002}
_SLIP_SIDE  = {"crypto": 0.0002, "forex": 0.0001,  "stock": 0.0002}
_FUND_SWING = 0.0006

# Shadow cost can never exceed this, mirroring the real gauntlet's philosophy
# (risk_gauntlet.py's MAX_COST_R caps real trades at 0.20-0.45R depending on mode/
# trade_type). Without this cap, _compute_cost_R() divides a fixed round-trip cost
# by a flat R_MULT x ATR box width, which blows up to 2-3R+ on low-ATR%-relative-
# to-price instruments (forex swing especially) — a shadow-model artifact, not a
# real trading cost the real gauntlet would ever have allowed through.
# NOTE: counterfactual_report.py and weight_proposal.py each keep their own local
# copy of this same 0.50 value (by design — both stay standalone, no cross-file
# imports). If this ever changes, update it in all three places or the reports
# will silently drift out of sync with what labeller.py itself now produces.
COST_R_CAP = 0.50


# ── REASON_MAP (ordered: longest/most-specific first, first match wins) ──────
# Built against actual dexter scan strings. See scan_pair_tf() in dexter.py.
#
# Exact formats dexter emits:
#   SR:        "Resistance(3x,3pt)", "Support(2x,2pt)"  [parsed by _SR_RE regex]
#   Fib:       "Fib 61.8% (golden pocket) (2pt)", "Fib 50% (2pt)", "Fib 38.2%"
#   GP:        "* GOLDEN POCKET" (in-zone), "[WATCH - approaching GP] GP zone..."
#   Div:       "[WATCH — not yet confirmed] FORMING ...", "Regular Bearish Divergence (str=Xpt)"
#   EMA:       "EMA55 support (2.0pt)", "EMA21 resistance (1.0pt)", "EMA crossover bullish_cross 2c ago"
#   BB:        "BB upper BURST (%B=1.15, ...)", "BB mid support (%B=0.50, 1pt)", "BB squeeze (...)"
#   VP:        "VP POC 4h (0.15% away ...)", "VP VAH 1h ...", "VP VAL 4h ..."
#   RSI:       "RSI OVERBOUGHT (82.3, 0.5pt)", "RSI OVERSOLD (18.1, 0.5pt)"
#   Sweep:     "Sweep:buy_side (3pt)", "Sweep:sell_side (3pt)"
#   Patterns:  "ascending_triangle (bullish, conf=0.75) BREAKOUT+vol"  [parsed by _PATTERN_RE]
#   Combo:     "** GP*SR DEADLY COMBO (*1.15 quality bonus applied)"

_SR_RE      = re.compile(r"^(Resistance|Support)\((\d+)x,")   # must start with label(Nx,
_PATTERN_RE = re.compile(r"conf=")                             # only pattern reasons contain this

REASON_MAP_ORDERED = [
    # ── Meta / combo — before any component matches ─────────────────────────
    # Must check [WATCH] prefixes before "GP" so approaching-GP watch strings
    # ("[WATCH — approaching GP] GP zone ...") don't misfire as gp_sr_combo.
    # Dexter emits em-dash (—) not hyphen (-), so both variants are listed.
    ("[WATCH — not yet confirmed]",         "rsi_div_forming"),
    ("[WATCH - not yet confirmed]",         "rsi_div_forming"),  # hyphen fallback
    ("[WATCH",                             "watch_signal"),   # other [WATCH] tags
    ("GP",                                 "gp_sr_combo"),   # "GP×SR DEADLY COMBO"
    # ── Golden Pocket ────────────────────────────────────────────────────────
    ("GOLDEN POCKET",                      "gp"),
    # ── Fib levels — specific before generic ────────────────────────────────
    ("Fib 61.8%",                          "fib_618"),
    ("Fib 50%",                            "fib_50"),
    ("Fib 78.6%",                          "fib_786"),
    ("Fib 38.2%",                          "fib_382"),
    ("Fib 23.6%",                          "fib_236"),
    ("Fib",                                "fib_other"),
    # ── EMA — MUST appear before any "support"/"resistance" words ────────────
    ("EMA55",                              "ema55"),
    ("EMA21",                              "ema21"),
    ("EMA13",                              "ema13"),
    ("EMA crossover",                      "ema_cross"),
    # ── Bollinger Bands — specific before generic ───────────────────────────
    ("BB upper BURST",                     "bb_burst"),
    ("BB lower BURST",                     "bb_burst"),
    ("BB near upper",                      "bb_near"),
    ("BB near lower",                      "bb_near"),
    ("BB mid",                             "bb_mid"),
    ("BB squeeze",                         "bb_squeeze"),
    # ── Derivatives (Binance futures) — prefixes set in derivs.py ───────────
    ("Funding extreme",                    "funding_extreme"),
    ("OI divergence",                      "oi_divergence"),
    ("OI confirmation",                    "oi_confirm"),
    # ── Volume Profile ───────────────────────────────────────────────────────
    ("VP POC",                             "vp_poc"),
    ("VP VAH",                             "vp_vah"),
    ("VP VAL",                             "vp_val"),
    # ── Fast intraday structural anchors (prior day H/L, opening range) ─────
    ("PDH proximity",                      "fast_anchor_pdh"),
    ("PDL proximity",                      "fast_anchor_pdl"),
    ("Opening range high",                 "fast_anchor_or_high"),
    ("Opening range low",                  "fast_anchor_or_low"),
    # ── RSI extremes ─────────────────────────────────────────────────────────
    ("RSI OVERBOUGHT",                     "rsi_ob"),
    ("RSI OVERSOLD",                       "rsi_os"),
    ("RSI crossed 50",                     "rsi_50_cross"),
    # ── RSI divergence — specific types before generic "Divergence" ─────────
    ("Hidden Bullish",                     "rsi_div_hidden"),
    ("Hidden Bearish",                     "rsi_div_hidden"),
    ("Regular Bullish",                    "rsi_div_regular"),
    ("Regular Bearish",                    "rsi_div_regular"),
    ("Divergence",                         "rsi_div"),
    ("divergence",                         "rsi_div"),
    # ── Liquidity sweep ──────────────────────────────────────────────────────
    ("Sweep:",                             "sweep"),
]

_unmatched_logged: set = set()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _wilson_ci(n: int, k: int) -> tuple:
    """Wilson 95% CI for a proportion k/n. Returns (lo, hi)."""
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = k / n
    denom  = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))

def _session_from_ts(epoch: int) -> str:
    """
    Session label from UTC epoch.
    Matches dexter._session_grade() boundaries exactly:
      PEAK: London open 08-10, NY open/overlap 13-17
      GOOD: Mid-London 10-13, NY afternoon 17-22
      LOW:  Asian/early morning < 08 or >= 22
    """
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    h  = dt.hour + dt.minute / 60
    if (8.0 <= h < 10.0) or (13.0 <= h < 17.0):
        return "PEAK"
    if (10.0 <= h < 13.0) or (17.0 <= h < 22.0):
        return "GOOD"
    return "LOW"

def normalize_reasons(reasons_raw) -> list:
    """
    Map dexter scan strings to canonical feature tokens.

    Handling order:
    1. S/R regex: "Resistance(3x,3pt)" → sr_multi / sr_single (avoids substring pollution)
    2. Pattern regex: strings containing "conf=" → pattern_* tokens
    3. Ordered REASON_MAP: longest/most-specific first; first match wins
    4. Unmatched → single token "other" (logged once per unique string)

    Forming divergence strings start with "[WATCH - not yet confirmed]" and map to
    rsi_div_forming, NOT rsi_div — they must never contaminate confirmed-divergence stats.
    """
    if not reasons_raw:
        return []
    if isinstance(reasons_raw, str):
        parts = re.split(r"[\n]+", reasons_raw)
        reasons_raw = [p.strip() for p in parts if p.strip()]
    tokens = set()
    for r in reasons_raw:
        r = str(r).strip()
        if not r or re.fullmatch(r"[\d.+\-]+", r):
            continue  # numeric fragment

        # 1. S/R touch-count regex — format: "Resistance(3x,3pt)" or "Support(2x,2pt)"
        m_sr = _SR_RE.match(r)
        if m_sr:
            instances = int(m_sr.group(2))
            tokens.add("sr_multi" if instances >= 3 else "sr_single")
            continue

        # 2. Chart pattern — only strings with "conf=" (no other reason type uses this)
        if _PATTERN_RE.search(r):
            if "BREAKOUT+vol" in r:
                tokens.add("pattern_breakout_vol")
            elif "BREAKOUT" in r:
                tokens.add("pattern_breakout")
            else:
                m_conf = re.search(r"conf=([\d.]+)", r)
                conf = float(m_conf.group(1)) if m_conf else 0.0
                tokens.add("pattern_high_conf" if conf >= 0.70 else "pattern_mid_conf")
            continue

        # 3. Ordered map: first substring match wins
        matched = False
        for pattern, token in REASON_MAP_ORDERED:
            if pattern in r:
                tokens.add(token)
                matched = True
                break

        if not matched:
            tokens.add("other")
            if r not in _unmatched_logged:
                _unmatched_logged.add(r)
                print(f"[labeller] REASON_MAP miss: '{r[:60]}' -> 'other' (extend map if meaningful)")

    return sorted(tokens)


def _direction_proxy(result: dict):
    """
    Direction derives from zone position relative to current price.
    result['price']        = confluence zone (nearest level to current price)
    result['current_price'] = live market price

    Rule: zone below current_price → support → long
          zone above current_price → resistance → short
    At-level (zone ≈ price within 0.05%): regime tiebreak.
    NEVER uses support/resistance proximity of current price to other levels —
    entry_ref IS the zone, so a long is recorded at a support zone.
    """
    zone = result.get("price")
    cur  = result.get("current_price")
    if zone and cur and zone > 0 and cur > 0:
        rel = (zone - cur) / cur
        if rel < -0.0005:  return "long"   # zone > 0.05% below current
        if rel >  0.0005:  return "short"  # zone > 0.05% above current
    # At-level or unavailable: regime tiebreak
    regime = (result.get("regime_4h") or {}).get("regime", "")
    if regime == "TRENDING_UP":   return "long"
    if regime == "TRENDING_DOWN": return "short"
    return None


def _compute_cost_R(asset_type: str, trade_type: str, r_dist: float, entry_ref: float):
    """Round-trip cost in R-multiples. None if inputs missing."""
    if not (r_dist and entry_ref and entry_ref > 0):
        return None
    stop_pct = r_dist / entry_ref
    if stop_pct <= 0:
        return None
    cost_rt = 2 * (_FEE_SIDE.get(asset_type, 0.0005) + _SLIP_SIDE.get(asset_type, 0.0002))
    if trade_type == "swing":
        cost_rt += _FUND_SWING
    return min(round(cost_rt / stop_pct, 4), COST_R_CAP)


def _window(rec: dict) -> tuple:
    """(start_epoch, end_epoch) for overlap detection. Uses ACTIVE window if available."""
    if rec.get("touched_ts"):
        start = int(rec["touched_ts"])
        end   = int(rec.get("resolved_ts") or rec.get("active_expiry_ts") or rec.get("expiry_ts") or start + 86400)
    else:
        start = int(rec["ts_epoch"])
        end   = int(rec.get("expiry_ts") or start + 86400)
    return (start, end)


def _compute_k_map(records: list) -> dict:
    """
    For each record, k = count of records (including itself) that share
    asset_type + direction with an overlapping window. Returns {id: k}.
    Effective N = sum(1/k) — downweights overlapping, non-independent records.
    """
    k_map = {}
    windows = {r["id"]: _window(r) for r in records}
    for r in records:
        ws, we = windows[r["id"]]
        ra, rd = r.get("asset_type"), r.get("direction")
        k = sum(
            1 for s in records
            if s.get("asset_type") == ra and s.get("direction") == rd
            and ws < windows[s["id"]][1] and windows[s["id"]][0] < we
        )
        k_map[r["id"]] = max(k, 1)
    return k_map


# ── I/O ──────────────────────────────────────────────────────────────────────

_lock             = threading.Lock()
_resolver_started = False

def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out

def _append_jsonl(path: str, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

def _save_open(records: list):
    """Rewrite entire open file. Must be called under _lock."""
    with open(OPEN_FILE, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

def _load_discovery_map() -> dict:
    if not os.path.exists(DISCOVERY_FILE):
        return {}
    try:
        with open(DISCOVERY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_discovery_map(dmap: dict):
    """Must be called under _lock."""
    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(dmap, f, indent=2)


# ── Record construction ───────────────────────────────────────────────────────

def record_setup(result: dict, asset_type: str, chev_decision: str,
                 chev_meta: dict = None):
    """
    Create and persist a new PENDING shadow record.

    chev_meta keys (all optional): direction, entry, tags, reason, trade_type
      - Provide for POST records (direction + entry from Chev's TRADE: line).
      - Omit/None for NOT_ESCALATED/SKIP/REJECT — direction is inferred from zone.

    Deduplication: skips if an unresolved record for the same symbol+tf already
    has entry_ref within 0.5*ATR. Prevents the 2-min rescan loop from flooding
    the shadow book with near-identical records for the same persistent setup.

    entry_ref for non-POST records: result['price'] (the confluence zone).
    NEVER result['current_price'] — that would trivially pass the PENDING
    touch-check on the first candle and collapse the two-stage design.
    """
    meta         = chev_meta or {}
    is_post      = (chev_decision == "POST" and bool(meta.get("direction")))
    direction    = meta.get("direction") if is_post else _direction_proxy(result)
    direction_src = "chev" if is_post else "proxy"

    if not direction:
        return

    trade_type = meta.get("trade_type") or result.get("trade_type") or "day"
    atr        = result.get("atr") or result.get("atr_1h") or 0.0

    if is_post:
        entry_ref = float(meta.get("entry") or 0)
    else:
        # Confluence zone — result['price'] is the nearest level to current price.
        # Using current_price here would make every record activate instantly.
        entry_ref = float(result.get("price") or 0)

    if not (atr and entry_ref):
        return

    r_dist = R_MULT.get(trade_type, 1.2) * atr

    if direction == "long":
        upper    = entry_ref + r_dist
        lower    = entry_ref - r_dist
        upper_2r = entry_ref + 2 * r_dist
    else:
        upper    = entry_ref - r_dist    # profit barrier below entry for shorts
        lower    = entry_ref + r_dist    # loss barrier above entry for shorts
        upper_2r = entry_ref - 2 * r_dist

    now_e     = _now_epoch()
    expiry_ts = now_e + EXPIRY_HOURS.get(trade_type, 6) * 3600

    tf_key      = result.get("primary_tf") or result.get("tf") or "1h"
    sym_key     = result.get("symbol", "UNKNOWN")
    reasons_raw = result.get("reasons") or []
    features    = normalize_reasons(reasons_raw)

    rec = {
        "id":              str(uuid.uuid4())[:16],
        "config_ver":      CONFIG_VER,
        "ts_epoch":        now_e,
        "ts":              _now_iso(),
        "symbol":          sym_key,
        "asset_type":      asset_type,
        "tf":              tf_key,
        "trade_type":      trade_type,
        "direction":       direction,
        "direction_src":   direction_src,
        "entry_ref":       round(entry_ref, 8),
        "atr":             round(atr, 8),
        "r_dist":          round(r_dist, 8),
        "upper":           round(upper, 8),
        "lower":           round(lower, 8),
        "upper_2r":        round(upper_2r, 8),
        "cost_R":          _compute_cost_R(asset_type, trade_type, r_dist, entry_ref),
        "expiry_ts":       expiry_ts,
        "active_expiry_ts": None,
        "dexter_score":    result.get("count") or 0.0,
        "reasons_raw":     (reasons_raw if isinstance(reasons_raw, list) else [])[:30],
        "features":        features,
        "regime_4h":       (result.get("regime_4h") or {}).get("regime"),
        "session":         result.get("session") or _session_from_ts(now_e),
        "atr_pct":         round(atr / entry_ref * 100, 4) if entry_ref else None,
        "dist_from_level": result.get("dist_from_level"),
        "in_gp":           bool(result.get("in_golden_pocket") or result.get("in_gp")),
        "hour_utc":        datetime.fromtimestamp(now_e, tz=timezone.utc).hour,
        "dow":             datetime.fromtimestamp(now_e, tz=timezone.utc).weekday(),
        "chev_decision":   chev_decision,
        "chev_reason":     meta.get("reason"),
        "chev_tags":       meta.get("tags"),
        "stage":           "PENDING",
        "touched_ts":      None,
        "session_at_touch": None,
        "regime_at_touch":  None,
        "resolved":        False,
        "label":           None,
        "hit_2r":          None,
        "realized_R":      None,
        "bars_held":       None,
        "resolved_ts":     None,
    }

    with _lock:
        # Dedupe: skip if an unresolved record for same symbol+tf is within 0.5*ATR
        existing = _load_jsonl(OPEN_FILE)
        for ex in existing:
            if (not ex.get("resolved")
                    and ex.get("symbol") == sym_key
                    and ex.get("tf") == tf_key
                    and abs((ex.get("entry_ref") or 0) - entry_ref) <= 0.5 * atr):
                return  # duplicate zone already tracked
        _append_jsonl(OPEN_FILE, rec)


# ── Resolver ─────────────────────────────────────────────────────────────────

def resolve_open_labels(fetch_candles_fn):
    """
    Scan all open records. PENDING: check for zone touch.
    ACTIVE: walk barriers. Close resolved records.

    fetch_candles_fn(symbol, tf, since_epoch) -> list of
        {"t": int_epoch, "o": float, "h": float, "l": float, "c": float}
    """
    with _lock:
        open_records = _load_jsonl(OPEN_FILE)
    if not open_records:
        return

    now = _now_epoch()
    groups = {}
    for rec in open_records:
        groups.setdefault((rec["symbol"], rec["tf"]), []).append(rec)

    resolved_ids = set()
    mutated      = {}

    for (sym, tf), recs in groups.items():
        since = min((r.get("touched_ts") or r["ts_epoch"]) for r in recs)
        try:
            candles = fetch_candles_fn(sym, tf, since)
        except Exception as e:
            print(f"[labeller] fetch_candles failed {sym}/{tf}: {e}")
            continue
        if not candles:
            continue

        candle_end_ts = int(candles[-1]["t"])

        for rec in recs:
            try:
                resolved, final = _process_record(rec, candles, now, candle_end_ts)
            except Exception as e:
                print(f"[labeller] process error {rec['id']}: {e}")
                continue
            if resolved:
                resolved_ids.add(rec["id"])
                with _lock:
                    _append_jsonl(CLOSED_FILE, final)
            elif final is not rec:
                mutated[rec["id"]] = final

    if resolved_ids or mutated:
        remaining = [mutated.get(r["id"], r) for r in open_records
                     if r["id"] not in resolved_ids]
        with _lock:
            _save_open(remaining)


def _process_record(rec, candles, now, candle_end_ts):
    rec = dict(rec)
    if rec["stage"] == "PENDING":
        return _process_pending(rec, candles, now, candle_end_ts)
    if rec["stage"] == "ACTIVE":
        return _process_active(rec, candles, now, candle_end_ts)
    return (False, rec)


def _process_pending(rec, candles, now, candle_end_ts):
    """
    Scan candles for zone touch.
    at_level (dist_from_level <= 0.5%): activates on the first candle at or after ts_epoch.
    Otherwise: activates when candle range covers entry_ref +/- touch_band.
    PENDING + expiry passed without touch -> NO_FILL.
    """
    entry_ref  = rec["entry_ref"]
    atr        = rec["atr"]
    expiry_ts  = rec["expiry_ts"]
    ts_epoch   = rec["ts_epoch"]
    touch_band = max(TOUCH_PCT * entry_ref, TOUCH_ATR_MULT * atr)
    at_level   = (rec.get("dist_from_level") or 999.0) <= 0.5

    for c in candles:
        c_ts = int(c["t"])
        if c_ts < ts_epoch:
            continue

        touched = at_level or (c["l"] <= entry_ref + touch_band and c["h"] >= entry_ref - touch_band)

        if touched and c_ts <= expiry_ts:
            rec["stage"]            = "ACTIVE"
            rec["touched_ts"]       = c_ts
            rec["session_at_touch"] = _session_from_ts(c_ts)
            rec["regime_at_touch"]  = rec.get("regime_4h")   # best available snapshot
            rec["active_expiry_ts"] = c_ts + EXPIRY_HOURS.get(rec["trade_type"], 6) * 3600
            remaining = [cx for cx in candles if int(cx["t"]) >= c_ts]
            return _process_active(rec, remaining, now, candle_end_ts)

    if expiry_ts <= now:
        rec.update({"resolved": True, "label": "NO_FILL", "resolved_ts": now})
        return (True, rec)

    return (False, rec)


def _process_active(rec, candles, now, candle_end_ts):
    """
    Walk candles checking profit/loss barriers and expiry.

    Barrier semantics (conceptual, not numeric):
      upper    = profit barrier (long: above entry, short: below entry)
      lower    = loss barrier   (long: below entry, short: above entry)
      upper_2r = 2R extension in profit direction (same conceptual side)

    Pessimistic same-candle rule: both barriers in one candle -> label=-1.

    hit_2r definition: "touched +2R before -1R after the profit exit."
    When label=+1, we continue walking subsequent candles (profit exit candle
    excluded) until +2R or the loss barrier is reached, whichever comes first.
    Same-candle double-touch in this trailing walk: loss wins (pessimism).
    All other outcomes (label=-1, label=0, expiry reached): hit_2r=False.
    """
    direction     = rec["direction"]
    entry_ref     = rec["entry_ref"]
    upper         = rec["upper"]
    lower         = rec["lower"]
    upper_2r      = rec["upper_2r"]
    r_dist        = rec["r_dist"]
    touched_ts    = int(rec.get("touched_ts") or rec["ts_epoch"])
    active_expiry = int(rec.get("active_expiry_ts") or rec.get("expiry_ts") or touched_ts + 86400)

    hit_2r              = False
    resolved            = False
    label               = None
    realized            = None
    bars_held           = 0
    profit_exit_candle  = None   # timestamp of the candle that resolved label=+1

    for c in candles:
        c_ts = int(c["t"])
        if c_ts < touched_ts:
            continue

        bars_held += 1
        ch, cl, cc = float(c["h"]), float(c["l"]), float(c["c"])

        if direction == "long":
            hit_profit = ch >= upper
            hit_loss   = cl <= lower
            drift_R    = (cc - entry_ref) / r_dist if r_dist > 0 else 0.0
        else:
            hit_profit = cl <= upper     # upper is below entry for shorts
            hit_loss   = ch >= lower     # lower is above entry for shorts
            drift_R    = (entry_ref - cc) / r_dist if r_dist > 0 else 0.0

        if hit_profit and hit_loss:
            label, realized, resolved = -1, -1.0, True   # pessimism: loss fires first
            break

        if hit_profit:
            label, realized, resolved = 1, 1.0, True
            profit_exit_candle = c_ts
            break

        if hit_loss:
            label, realized, resolved = -1, -1.0, True
            break

        if c_ts >= active_expiry:
            label    = 0
            realized = round(drift_R, 4)
            resolved = True
            break

    if not resolved:
        if active_expiry <= now:
            if candle_end_ts < active_expiry:
                if now - active_expiry > 7 * 86400:
                    rec.update({"resolved": True, "label": "VOID", "resolved_ts": now})
                    return (True, rec)
                return (False, rec)  # retry later; don't corrupt hit_2r
            last = candles[-1]
            if direction == "long":
                realized = (float(last["c"]) - entry_ref) / r_dist if r_dist > 0 else 0.0
            else:
                realized = (entry_ref - float(last["c"])) / r_dist if r_dist > 0 else 0.0
            label, resolved = 0, True
        else:
            return (False, rec)

    # Post-resolution 2R trailing walk — only when label=+1
    # Walk candles strictly AFTER the profit exit candle.
    # +2R before loss barrier -> hit_2r=True
    # loss barrier before +2R -> hit_2r=False
    # same-candle double-touch -> loss wins, hit_2r=False
    # active_expiry reached or no subsequent candles -> hit_2r=False
    if label == 1 and profit_exit_candle is not None:
        for c in candles:
            c_ts = int(c["t"])
            if c_ts <= profit_exit_candle:   # skip up to and including profit candle
                continue
            if c_ts > active_expiry:
                break
            ch, cl = float(c["h"]), float(c["l"])
            if direction == "long":
                hit_2r_c   = ch >= upper_2r
                hit_loss_c = cl <= lower
            else:
                hit_2r_c   = cl <= upper_2r
                hit_loss_c = ch >= lower
            if hit_2r_c and hit_loss_c:   # same-candle: loss wins
                break
            if hit_2r_c:
                hit_2r = True
                break
            if hit_loss_c:
                break

    rec.update({
        "resolved":    True,
        "label":       label,
        "realized_R":  round(realized, 4) if realized is not None else None,
        "bars_held":   bars_held,
        "resolved_ts": now,
        "hit_2r":      hit_2r,
    })
    return (True, rec)


# ── Daemon ────────────────────────────────────────────────────────────────────

def start_resolver_daemon(fetch_candles_fn, interval_seconds: int = 900):
    """Start background resolver. Safe to call multiple times — starts only once."""
    global _resolver_started
    if _resolver_started:
        return
    _resolver_started = True

    def _loop():
        while True:
            try:
                resolve_open_labels(fetch_candles_fn)
            except Exception as e:
                print(f"[labeller] resolver error: {e}")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="labeller-resolver", daemon=True)
    t.start()
    print(f"[labeller] resolver daemon started (interval={interval_seconds}s)")


# ── Report helpers ────────────────────────────────────────────────────────────

def _load_closed() -> list:
    with _lock:
        return _load_jsonl(CLOSED_FILE)

def _numeric_label(label):
    """Returns +1/-1/0 for resolved numeric outcomes; None for NO_FILL/VOID/unresolved."""
    return label if label in (1, -1, 0) else None

def _base_rate(records: list) -> float:
    """Win rate across a collection of records (numeric labels only)."""
    wins  = sum(1 for r in records if r.get("label") == 1)
    n_bin = sum(1 for r in records if r.get("label") in (1, -1, 0))
    return wins / n_bin if n_bin else 0.5


# ── Reports ───────────────────────────────────────────────────────────────────

def chev_scoreboard() -> str:
    """
    POST vs SKIP expectancy stratified by dexter_score band.
    direction_src always split (chev vs proxy never mixed).
    Net expectancy uses per-record cost_R.
    Wilson 95% CI on all win rates.
    """
    closed = [r for r in _load_closed() if _numeric_label(r.get("label")) is not None]
    lines  = ["=" * 62, "CHEV SCOREBOARD -- stratified by Dexter score band", "=" * 62]

    global_br = _base_rate(closed)
    lines.append(f"Global base rate: {global_br*100:.0f}%  (n={len(closed)} resolved)")

    for (lo, hi), band_lbl in zip(SCORE_BANDS, SCORE_BAND_LABELS):
        band = [r for r in closed if lo <= (r.get("dexter_score") or 0.0) < hi]
        lines.append(f"\n-- Band {band_lbl}  (n={len(band)}) --")
        if not band:
            lines.append("  (no records)")
            continue

        for src in ("chev", "proxy"):
            for dec in ("POST", "SKIP", "NOT_ESCALATED"):
                grp = [r for r in band
                       if r.get("direction_src") == src and r.get("chev_decision") == dec]
                if not grp:
                    continue
                wins  = sum(1 for r in grp if r.get("label") == 1)
                n_bin = sum(1 for r in grp if r.get("label") in (1, -1, 0))
                ci    = _wilson_ci(n_bin, wins)
                avg_r = sum(r.get("realized_R") or 0 for r in grp) / len(grp)
                net_e = sum((r.get("realized_R") or 0) - (r.get("cost_R") or 0)
                            for r in grp) / len(grp)
                wr    = f"{wins/n_bin*100:.0f}%" if n_bin else "--"
                lines.append(
                    f"  {src:5s} {dec:16s} n={len(grp):3d} | "
                    f"wr={wr} [{ci[0]*100:.0f}%-{ci[1]*100:.0f}%] | "
                    f"gross={avg_r:+.3f}R  net={net_e:+.3f}R"
                )
    return "\n".join(lines)


def tag_lift_report() -> str:
    """
    Per-feature-token lift stats.

    Lift = P(win | token) / P(win overall, base rate).
    An edge exists when lift > 1.0 with sufficient effective N.

    Temporal discovery split (multiple-testing guard):
      discovery_ts[token] = first epoch where token first meets BOTH:
        (a) eff_N >= MIN_N_EFF (30), computed against running base rate at that point
        (b) lift > 1.0
      This timestamp is PERSISTED in labels_discovery.json and NEVER revised.
      Records before discovery_ts = in-sample (used to find the edge).
      Records after = out-of-sample. Only post-discovery tokens with
      post-discovery CI lower bound > post-discovery base rate are ACTIONABLE.

    direction_src split: chev and proxy always reported separately.
    """
    closed = [r for r in _load_closed() if _numeric_label(r.get("label")) is not None]
    if not closed:
        return "tag_lift_report: no resolved records."

    k_map = _compute_k_map(closed)
    closed_sorted = sorted(closed, key=lambda r: r.get("ts_epoch", 0))

    global_br = _base_rate(closed)

    with _lock:
        dmap = _load_discovery_map()
    dmap_updated = False

    all_tokens = sorted({tok for r in closed for tok in (r.get("features") or [])})
    lines = [
        "=" * 72,
        f"TAG LIFT REPORT  (base_rate={global_br*100:.0f}%, MIN_N_EFF={MIN_N_EFF})",
        "=" * 72,
    ]

    def _stats(recs):
        wins  = sum(1 for r in recs if r.get("label") == 1)
        n_bin = sum(1 for r in recs if r.get("label") in (1, -1, 0))
        eff_n = sum(1.0 / k_map.get(r["id"], 1) for r in recs)
        ci    = _wilson_ci(n_bin, wins)
        wr    = wins / n_bin if n_bin else 0.0
        lift  = wr / global_br if global_br > 0 else 1.0
        return wins, n_bin, eff_n, ci, wr, lift

    for tok in all_tokens:
        for src in ("chev", "proxy"):
            key = f"{tok}|{src}"
            src_recs = sorted(
                [r for r in closed
                 if tok in (r.get("features") or []) and r.get("direction_src") == src],
                key=lambda r: r.get("ts_epoch", 0)
            )
            if not src_recs:
                continue

            _, _, eff_chk, _, _, _ = _stats(src_recs)
            if eff_chk < MIN_N_EFF:
                lines.append(f"  {tok} [{src}]: eff_N={eff_chk:.1f} < {MIN_N_EFF} -- skip")
                continue

            # Persist discovery_ts: set ONCE when token FIRST meets lift criterion.
            # Uses running base rate at each step so we don't lookahead into future records.
            if key not in dmap:
                cum_wins_all = 0
                cum_n_all    = 0
                cum_eff_tok  = 0.0
                cum_wins_tok = 0
                cum_n_tok    = 0
                for r in closed_sorted:
                    if r.get("label") in (1, -1, 0):
                        cum_n_all += 1
                        if r.get("label") == 1:
                            cum_wins_all += 1
                    if tok in (r.get("features") or []) and r.get("direction_src") == src:
                        cum_eff_tok += 1.0 / k_map.get(r["id"], 1)
                        if r.get("label") in (1, -1, 0):
                            cum_n_tok += 1
                            if r.get("label") == 1:
                                cum_wins_tok += 1
                        if cum_eff_tok >= MIN_N_EFF:
                            running_br = cum_wins_all / cum_n_all if cum_n_all > 0 else 0.5
                            tok_wr     = cum_wins_tok / cum_n_tok if cum_n_tok > 0 else 0.0
                            if running_br > 0 and tok_wr / running_br > 1.0:
                                dmap[key]    = int(r.get("ts_epoch", 0))
                                dmap_updated = True
                                break  # discovery found

            discovery_ts = dmap.get(key)

            pre_disc  = [r for r in src_recs if r.get("ts_epoch", 0) <= (discovery_ts or 0)]
            post_disc = [r for r in src_recs if r.get("ts_epoch", 0) >  (discovery_ts or 0)]

            w_all, n_all, eff_all, ci_all, wr_all, lift_all = _stats(src_recs)
            disc_dt = (datetime.fromtimestamp(discovery_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                       if discovery_ts else "not yet")

            block = [
                f"\n{tok} [{src}]  raw_N={len(src_recs)}  eff_N={eff_all:.1f}  "
                f"discovery={disc_dt}",
                f"  ALL: wr={wr_all*100:.0f}% [{ci_all[0]*100:.0f}%-{ci_all[1]*100:.0f}%]  "
                f"lift={lift_all:.2f}x  (base={global_br*100:.0f}%)",
            ]

            if post_disc:
                w_pd, n_pd, eff_pd, ci_pd, wr_pd, lift_pd = _stats(post_disc)
                # Post-discovery base rate: wins/n across all records after discovery_ts
                post_all  = [r for r in closed if r.get("ts_epoch", 0) > (discovery_ts or 0)]
                post_br   = _base_rate(post_all)
                actionable = eff_pd >= MIN_N_EFF and ci_pd[0] > post_br
                block.append(
                    f"  POST-DISC (n={n_pd}, eff_N={eff_pd:.1f}): "
                    f"wr={wr_pd*100:.0f}% [{ci_pd[0]*100:.0f}%-{ci_pd[1]*100:.0f}%]  "
                    f"lift={lift_pd:.2f}x  post_base={post_br*100:.0f}%  "
                    f"{'ACTIONABLE' if actionable else '(edge not confirmed post-discovery)'}"
                )
            else:
                block.append("  POST-DISC: (no records after discovery date yet)")

            lines.append("\n".join(block))

    if dmap_updated:
        with _lock:
            _save_discovery_map(dmap)

    return "\n".join(lines)


def weight_suggestions() -> str:
    """
    Empirical weight adjustments for each feature token with eff_N >= MIN_N_WEIGHT.
    lift > 1.5 and CI_lo > base_rate -> INCREASE.
    lift < 0.8 -> DECREASE.
    Sorted by lift descending so the highest-value signals surface first.
    """
    closed = [r for r in _load_closed() if _numeric_label(r.get("label")) is not None]
    if len(closed) < MIN_N_WEIGHT:
        return f"weight_suggestions: need >= {MIN_N_WEIGHT} resolved records (have {len(closed)})"

    k_map  = _compute_k_map(closed)
    br     = _base_rate(closed)
    tokens = sorted({tok for r in closed for tok in (r.get("features") or [])})
    rows   = []

    for tok in tokens:
        recs  = [r for r in closed if tok in (r.get("features") or [])]
        wins  = sum(1 for r in recs if r.get("label") == 1)
        n_bin = sum(1 for r in recs if r.get("label") in (1, -1, 0))
        eff_n = sum(1.0 / k_map.get(r["id"], 1) for r in recs)
        if eff_n < MIN_N_WEIGHT:
            continue
        ci   = _wilson_ci(n_bin, wins)
        wr   = wins / n_bin if n_bin else 0.0
        lift = wr / br if br > 0 else 1.0
        if lift > 1.5 and ci[0] > br:
            verb = "INCREASE"
        elif lift < 0.8:
            verb = "DECREASE"
        else:
            verb = "KEEP    "
        rows.append((lift, f"  {verb} {tok:25s} lift={lift:.2f}x  wr={wr*100:.0f}%  "
                     f"CI_lo={ci[0]*100:.0f}%  eff_N={eff_n:.0f}"))

    rows.sort(key=lambda x: -x[0])
    lines = [f"WEIGHT SUGGESTIONS  (base_rate={br*100:.0f}%, floor eff_N={MIN_N_WEIGHT})"]
    if not rows:
        lines.append(f"  (no tokens with eff_N >= {MIN_N_WEIGHT} yet)")
    else:
        lines.extend(r for _, r in rows)
    return "\n".join(lines)


def skip_reason_audit() -> str:
    """
    Audit Dexter gate decisions: what outcome do blocked/skipped setups actually achieve?
    Compares realized outcomes of NOT_ESCALATED (by block cause), SKIP, and POST groups.
    If NOT_ESCALATED setups outperform POST, the gate is removing profit.
    """
    closed = [r for r in _load_closed() if _numeric_label(r.get("label")) is not None]
    if not closed:
        return "skip_reason_audit: no resolved records."

    lines = ["SKIP/GATE AUDIT -- what do blocked setups actually do?",
             f"(base rate across all resolved: {_base_rate(closed)*100:.0f}%)"]

    groups = {}
    for r in closed:
        dec    = r.get("chev_decision", "UNKNOWN")
        reason = r.get("chev_reason") or dec
        key    = f"{dec}:{reason}"
        groups.setdefault(key, []).append(r)

    for key, recs in sorted(groups.items()):
        wins  = sum(1 for r in recs if r.get("label") == 1)
        n_bin = sum(1 for r in recs if r.get("label") in (1, -1, 0))
        ci    = _wilson_ci(n_bin, wins)
        avg_r = sum(r.get("realized_R") or 0 for r in recs) / len(recs)
        if n_bin > 0:
            lines.append(
                f"  {key:35s} n={len(recs):4d}  wr={wins/n_bin*100:.0f}% "
                f"[{ci[0]*100:.0f}%-{ci[1]*100:.0f}%]  avg_R={avg_r:+.3f}"
            )
        else:
            lines.append(f"  {key:35s} n={len(recs):4d}  (no resolved outcomes)")
    return "\n".join(lines)


def fill_rate_report() -> str:
    """Fill rate: fraction of closed records that actually activated (excludes VOID)."""
    with _lock:
        open_recs = _load_jsonl(OPEN_FILE)
    closed   = _load_closed()
    fills    = sum(1 for r in closed if r.get("label") not in ("NO_FILL", "VOID", None))
    no_fills = sum(1 for r in closed if r.get("label") == "NO_FILL")
    voids    = sum(1 for r in closed if r.get("label") == "VOID")
    pending  = sum(1 for r in open_recs if r.get("stage") == "PENDING")
    active   = sum(1 for r in open_recs if r.get("stage") == "ACTIVE")
    denom    = fills + no_fills  # VOID excluded from fill rate
    fr       = f"{fills/denom*100:.1f}%" if denom else "--"
    return (
        f"FILL RATE\n"
        f"  Open:   {pending} PENDING  {active} ACTIVE\n"
        f"  Closed: {fills} filled  {no_fills} NO_FILL  {voids} VOID  fill_rate={fr}"
    )


# ── Self-test ────────────────────────────────────────────────────────────────

def _run_self_test() -> bool:
    results = []

    def check(name, cond, detail=""):
        tag = "PASS" if cond else "FAIL"
        results.append(f"  {tag} {name}" + (f" -- {detail}" if detail else ""))
        return cond

    def candles(entries):
        return [{"t": t, "o": o, "h": h, "l": l, "c": c} for t, o, h, l, c in entries]

    BASE = 1_700_000_000

    def make_rec(direction="long", stage="PENDING", touched_ts=None,
                 entry_ref=100.0, r_dist=1.0, atr=0.5,
                 trade_type="day", tf="1h",
                 ts_epoch=None, expiry_ts=None, active_expiry_ts=None,
                 dist_from_level=5.0):
        ts  = ts_epoch or BASE
        exp = expiry_ts or (ts + EXPIRY_HOURS.get(trade_type, 6) * 3600)
        is_long = direction == "long"
        return {
            "id":             str(uuid.uuid4())[:16],
            "config_ver":     CONFIG_VER,
            "ts_epoch":       ts,
            "ts":             datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbol":         "TEST", "asset_type": "crypto",
            "tf":             tf, "trade_type": trade_type,
            "direction":      direction, "direction_src": "chev",
            "entry_ref":      entry_ref, "atr": atr, "r_dist": r_dist,
            "upper":    (entry_ref + r_dist) if is_long else (entry_ref - r_dist),
            "lower":    (entry_ref - r_dist) if is_long else (entry_ref + r_dist),
            "upper_2r": (entry_ref + 2*r_dist) if is_long else (entry_ref - 2*r_dist),
            "cost_R":         _compute_cost_R("crypto", trade_type, r_dist, entry_ref),
            "expiry_ts":      exp, "active_expiry_ts": active_expiry_ts,
            "dexter_score":   12.0, "reasons_raw": [], "features": ["gp", "sr_multi"],
            "regime_4h":      "TRENDING_UP", "session": "GOOD",
            "atr_pct":        round(atr/entry_ref*100, 4),
            "dist_from_level": dist_from_level, "in_gp": False,
            "hour_utc":       9, "dow": 0,
            "chev_decision":  "POST", "chev_reason": None, "chev_tags": None,
            "stage":          stage, "touched_ts": touched_ts,
            "session_at_touch": None, "regime_at_touch": None,
            "resolved": False, "label": None, "hit_2r": None,
            "realized_R": None, "bars_held": None, "resolved_ts": None,
        }

    # Case 1: PENDING expires untouched -> NO_FILL
    r = make_rec(ts_epoch=BASE, expiry_ts=BASE+3600)
    cs = candles([(BASE+60, 99, 99.5, 98, 99)])
    resolved, rec = _process_pending(r, cs, BASE+7200, BASE+60)
    check("1: PENDING expires untouched -> NO_FILL",
          resolved and rec["label"] == "NO_FILL")

    # Case 2: PENDING -> touch -> win barrier in same candle walk
    r = make_rec(ts_epoch=BASE, expiry_ts=BASE+86400)
    # l=99.2 touches zone (100 +/- 0.125) but stays above lower=99
    cs = candles([(BASE+60, 99.5, 101.5, 99.2, 101)])
    resolved, rec = _process_pending(r, cs, BASE+86400, BASE+60)
    check("2: PENDING -> touch + win in same walk -> label=+1",
          resolved and rec["label"] == 1 and rec["stage"] == "ACTIVE",
          f"label={rec.get('label')}")

    # Case 3: ACTIVE -> loss barrier hit
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([(BASE+60, 100, 100.5, 98.5, 99)])  # l=98.5 <= lower=99
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+60)
    check("3: ACTIVE loss barrier -> label=-1",
          resolved and rec["label"] == -1)

    # Case 4: Pessimistic same-candle (both barriers) -> label=-1
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([(BASE+60, 100, 101.5, 98.5, 100)])
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+60)
    check("4: Pessimistic same-candle -> label=-1 not +1",
          resolved and rec["label"] == -1)

    # Case 5: ACTIVE expires with no barrier -> label=0 (drift)
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+3600)
    cs = candles([(BASE+3700, 100.3, 100.4, 100.2, 100.3)])
    resolved, rec = _process_active(r, cs, BASE+7200, BASE+3700)
    check("5: ACTIVE expires no barrier -> label=0 with drift",
          resolved and rec["label"] == 0 and rec["realized_R"] is not None)

    # Case 6: SHORT profit barrier (below entry)
    r = make_rec(direction="short", stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    # upper=99 (profit for short), lower=101 (loss for short)
    cs = candles([(BASE+60, 100, 100.5, 98.5, 99)])  # l=98.5 <= upper=99 -> profit
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+60)
    check("6: SHORT profit barrier (low <= upper=99) -> label=+1",
          resolved and rec["label"] == 1, f"label={rec.get('label')}")

    # Case 7: SHORT loss barrier (above entry)
    r = make_rec(direction="short", stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([(BASE+60, 100, 101.5, 99.8, 100)])  # h=101.5 >= lower=101 -> loss
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+60)
    check("7: SHORT loss barrier (high >= lower=101) -> label=-1",
          resolved and rec["label"] == -1)

    # Case 8: hit_2r via post-profit trailing walk (subsequent candle, no same-candle credit)
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    # upper=101, upper_2r=102, lower=99
    # candle 1: profit exit (h=101.5 >= 101); candle 2: 2R hit (h=102.5) with no loss touch
    cs = candles([
        (BASE+60,  100, 101.5, 99.5, 101),    # profit exit; does NOT credit 2R alone
        (BASE+120, 101, 102.5, 100.5, 102),   # subsequent candle hits upper_2r=102
    ])
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+120)
    check("8: hit_2r=True via post-profit trailing walk (candle 2 hits upper_2r)",
          rec.get("hit_2r") is True, f"hit_2r={rec.get('hit_2r')}")
    check("8b: label=+1 (profit barrier resolved on candle 1)",
          resolved and rec["label"] == 1)

    # Case 8c: hit_2r stays False under pessimistic rule (both barriers same candle)
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([(BASE+60, 100, 102.5, 98.5, 100)])  # both barriers AND 2R — loss wins
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+60)
    check("8c: hit_2r=False under pessimistic rule (loss assumed first, same candle)",
          rec.get("hit_2r") is False and rec.get("label") == -1,
          f"hit_2r={rec.get('hit_2r')} label={rec.get('label')}")

    # Case 9: at-level activates immediately
    r = make_rec(ts_epoch=BASE, expiry_ts=BASE+86400, dist_from_level=0.3)
    cs = candles([
        (BASE+60,  99.5, 100.3, 99.2, 100.1),  # activates (at-level), l=99.2 > lower=99
        (BASE+120, 100.1, 101.5, 99.5, 101),   # hits upper=101 -> label=+1
    ])
    resolved, rec = _process_pending(r, cs, BASE+86400, BASE+120)
    check("9: at-level activates immediately -> label=+1",
          resolved and rec["label"] == 1 and rec["stage"] == "ACTIVE",
          f"label={rec.get('label')}")

    # Case 10: session_at_touch stamped correctly (09:00 UTC = PEAK London open)
    peak_epoch = int(datetime(2023, 11, 14, 9, 0, tzinfo=timezone.utc).timestamp())
    r = make_rec(ts_epoch=peak_epoch - 100, expiry_ts=peak_epoch + 86400)
    cs = candles([(peak_epoch, 100, 100.4, 99.6, 100.2)])
    _, rec2 = _process_pending(r, cs, peak_epoch + 86400, peak_epoch + 86400)
    check("10: session_at_touch=PEAK at 09:00 UTC (London open)",
          rec2.get("session_at_touch") == "PEAK", f"got={rec2.get('session_at_touch')}")
    check("10b: regime_at_touch copied from regime_4h",
          rec2.get("regime_at_touch") == "TRENDING_UP")

    # Case 11: Wilson CI sanity
    lo, hi = _wilson_ci(10, 10)
    check("11: Wilson CI (10/10 wins) lo > 0.7, hi <= 1.0",
          lo > 0.7 and hi <= 1.0, f"CI=[{lo:.3f},{hi:.3f}]")
    lo0, hi0 = _wilson_ci(0, 0)
    check("11b: Wilson CI (n=0) returns (0,1)",
          lo0 == 0.0 and hi0 == 1.0)

    # Case 12: PENDING -> touch -> ACTIVE -> expires (no barrier) -> label=0 NOT NO_FILL
    # trade_type="scalp": EXPIRY_HOURS=2 -> active_expiry = touch_t + 7200s
    touch_t = BASE + 3600
    r = make_rec(ts_epoch=BASE, trade_type="scalp", tf="1h", dist_from_level=5.0)
    cs = candles([
        (touch_t,         100, 100.3, 99.8, 100.1),   # touch -> ACTIVE
        (touch_t + 3600,  100, 100.4, 99.8, 100.2),   # mid-window, no barrier
        (touch_t + 7500,  100, 100.4, 99.9, 100.3),   # past active_expiry (7200s)
    ])
    resolved, rec = _process_pending(r, cs, touch_t + 8000, touch_t + 7500)
    check("12: PENDING->ACTIVE->expired -> label=0 NOT NO_FILL",
          resolved and rec["label"] == 0 and rec.get("stage") == "ACTIVE",
          f"label={rec.get('label')} stage={rec.get('stage')}")
    check("12b: realized_R is float (drift)",
          isinstance(rec.get("realized_R"), (int, float)))
    check("12c: NO_FILL never assigned after ACTIVE transition",
          rec.get("label") != "NO_FILL")

    # Case 13: VOID -- active_expiry 8+ days past, no covering candles
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+3600)
    cs = candles([])  # no candles
    now_far = BASE + 9 * 86400
    resolved, rec = _process_active(r, cs, now_far, BASE - 1)  # candle_end < active_expiry
    check("13: VOID when active_expiry 8+ days past with no covering candles",
          resolved and rec["label"] == "VOID", f"label={rec.get('label')}")

    # Case 14: reason string mapping -- EMA55 never pollutes SR tokens
    tok14a = normalize_reasons(["EMA55 support (2.0pt)"])
    check("14: 'EMA55 support (2.0pt)' maps to ema55 not sr/sr_single/sr_multi",
          "ema55" in tok14a and "sr" not in tok14a
          and "sr_single" not in tok14a and "sr_multi" not in tok14a,
          f"tokens={tok14a}")
    tok14b = normalize_reasons(["[WATCH - not yet confirmed] FORMING bullish divergence (RSI gap 5pt)"])
    check("14b: forming divergence -> rsi_div_forming not rsi_div",
          "rsi_div_forming" in tok14b and "rsi_div" not in tok14b,
          f"tokens={tok14b}")
    tok14c = normalize_reasons(["Resistance(3x,3pt)"])
    check("14c: Resistance(3x,3pt) -> sr_multi",
          tok14c == ["sr_multi"], f"tokens={tok14c}")
    tok14d = normalize_reasons(["Support(2x,2pt)"])
    check("14d: Support(2x,2pt) -> sr_single",
          tok14d == ["sr_single"], f"tokens={tok14d}")

    # Case 15: profit on candle 1, +2R hit on candle 5, no loss touch between -> hit_2r=True
    # upper=101, upper_2r=102, lower=99
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([
        (BASE+60,  100, 101.5, 99.5, 101),    # candle 1: profit exit (h>=101)
        (BASE+120, 101, 101.3, 100.4, 101.2), # candle 2: no barrier
        (BASE+180, 101, 101.4, 100.8, 101.1), # candle 3: no barrier
        (BASE+240, 101, 101.8, 100.6, 101.5), # candle 4: no barrier (h<102)
        (BASE+300, 101, 102.5, 100.8, 102.1), # candle 5: hits upper_2r=102
    ])
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+300)
    check("15: profit candle 1, 2R candle 5, no loss between -> label=+1 AND hit_2r=True",
          resolved and rec["label"] == 1 and rec.get("hit_2r") is True,
          f"label={rec.get('label')} hit_2r={rec.get('hit_2r')}")
    check("15b-setup: bars_held frozen at profit exit (1 bar, not 5)",
          rec.get("bars_held") == 1, f"bars_held={rec.get('bars_held')}")

    # Case 15b: profit on candle 1, loss barrier on candle 4 before any 2R touch -> hit_2r=False
    r = make_rec(stage="ACTIVE", touched_ts=BASE,
                 entry_ref=100, r_dist=1, active_expiry_ts=BASE+86400)
    cs = candles([
        (BASE+60,  100, 101.5, 99.5, 101),    # candle 1: profit exit
        (BASE+120, 101, 101.3, 100.4, 101.2), # candle 2: no barrier
        (BASE+180, 101, 101.4, 100.3, 101.1), # candle 3: no barrier (l=100.3 > lower=99)
        (BASE+240, 101, 101.2, 98.5, 99.5),   # candle 4: loss barrier (l=98.5 <= 99) before 2R
    ])
    resolved, rec = _process_active(r, cs, BASE+86400, BASE+240)
    check("15b: profit candle 1, loss candle 4 before 2R -> label=+1 AND hit_2r=False",
          resolved and rec["label"] == 1 and rec.get("hit_2r") is False,
          f"label={rec.get('label')} hit_2r={rec.get('hit_2r')}")

    # Summary
    passed = sum(1 for l in results if l.strip().startswith("PASS"))
    total  = len(results)
    print(f"\n[labeller] SELF-TEST  {passed}/{total}")
    for line in results:
        print(line)
    return passed == total


if __name__ == "__main__":
    ok = _run_self_test()
    raise SystemExit(0 if ok else 1)
