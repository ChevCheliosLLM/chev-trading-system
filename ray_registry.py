"""
ray_registry.py — Persistent identity, trust tallies, and break state for
Dexter's trendline rays (Phase R2 of the "Trendline Ray" build).

dexter.py's own trendline/pattern fit (_run_pattern_engine, dexter.py ~8396)
refits from scratch every scan — it has no memory that "this specific line
has already been respected 4 times over its life." This module IS that
memory. Built standalone in Phase R2 (nothing imported it yet); wired into
the real scan loop (scan_pair_tf, via _run_pattern_engine's fit -- NOT
patterns.py, which is display-only for the webapp) in Phase R3.

Design mirrors derivs.py: pure logic functions, constants at the top with a
WHY comment each, an offline self-test under __main__. The one exception to
"pure" is reconcile(), which is required to append a human-readable decision
line to ray_identity_log.jsonl on every call — that I/O is isolated to a thin
wrapper around a pure decision helper, same split derivs.py uses between
classify_derivs() (pure) and get_derivs() (network).

Thread-safety: scan_pair_tf runs concurrently across (symbol, timeframe)
pairs via a ThreadPoolExecutor (dexter.py ~13149). REGISTRY_LOCK (below)
must be held by the caller across the full load -> reconcile -> touches ->
break-state -> save sequence for a given scan, since the race is a lost
update across that whole span, not any single call.

Self-test: python -X utf8 ray_registry.py   (offline, no dexter import, no network)
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── Persistence paths (mirrors labeller.py's DATA_DIR convention) ───────────
DATA_DIR           = r"C:\ChevTools"
REGISTRY_PATH      = os.path.join(DATA_DIR, "ray_registry.json")
RAY_IDENTITY_LOG_PATH = os.path.join(DATA_DIR, "ray_identity_log.jsonl")

# ── Timeframe -> seconds-per-bar ─────────────────────────────────────────────
# WHY: ray math needs elapsed wall-clock time converted to a bar count for a
# given timeframe. Keys match the interval strings already in use elsewhere
# (dexter.py's td_map, ~line 398). Unknown timeframe falls back to "1h"'s
# value rather than raising, mirroring that same td_map.get(interval, "1h")
# fallback convention instead of crashing on an unexpected string.
_TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

# ── Identity / touch / break constants (each with a WHY) ────────────────────
TOUCH_TOL_ATR = 0.3
# A bar "touches" the ray if its relevant extreme comes within 0.3x ATR of
# the ray's value at that bar -- consistent with the ATR-tolerance philosophy
# already used for fit quality (engines.py TrendlineFit / patterns.py).

TOUCH_RELEASE_ATR = 0.5
# A touch only CLOSES (counts as one respected touch) once price has moved
# >=0.5x ATR away from the ray again. Price hugging the line for many bars
# must count as ONE touch, not one per bar.

SLOPE_MATCH_TOL = 0.15
# Same-line identity test: normalized slopes (ATR/bar) within this of each
# other are "the same line, refined." Deliberately loose at launch -- every
# identity decision is logged to ray_identity_log.jsonl so Kev can eyeball
# whether this is merging lines that should be considered different.

VALUE_MATCH_TOL_ATR = 0.5
# Same-line identity test: the existing ray's own value projected forward to
# the new current bar must be within 0.5x ATR of the new fit's value at that
# same bar for it to count as a continuation rather than a new line.

RAY_HORIZON_FRACTION = 1.0 / 3.0
RAY_HORIZON_MAX_BARS = 40
RAY_HORIZON_MIN_BARS = 5
# Forecast reach = one third of the ray's persisted lifetime span (bars from
# its first recorded touch under this identity to its most recent), hard-
# capped at 40 bars regardless of age, floored at 5 so a brand-new ray still
# gets a minimal, honest reach. Direction always comes from the current local
# fit (patterns.py); only REACH is earned by lifetime -- a young line gets a
# short leash, a proven one gets a longer one, same rules throughout.

MAX_LIVE_PER_SIDE = 2
DISTINCT_MIN_ATR = 1.0
# Render/narrate at most 2 rays per (symbol, timeframe, side). A second ray
# only qualifies if it describes genuinely different structure: its
# projected value at the current bar differs from the first's by >=1.0x ATR,
# OR its normalized slope differs by more than SLOPE_MATCH_TOL. Otherwise
# it's a near-duplicate and gets dropped -- the registry can still remember
# it, this cap only governs what gets rendered/narrated.

RAY_STALE_BARS = 100
# A LIVE ray unmatched by any scan for this many bars of ITS OWN timeframe is
# retired. Bar-count, not wall-clock hours, so a 15m ray and a 4h ray aren't
# held to the same clock.

# ── Breakout threshold (replicated, not imported) ───────────────────────────
# Same formula as patterns.py's _atr_breakout_pct (patterns.py ~line 59-68):
# dynamic = 0.5 * ATR / price, clamped to [0.5%, 2.5%]. Replicated here rather
# than imported so this module stays standalone/pure like derivs.py -- if
# patterns.py's formula ever changes, this constant trio must be updated to
# match by hand (same tradeoff derivs.py and labeller.py already accept for
# their own local copies of shared constants, e.g. labeller.py's COST_R_CAP).
BREAKOUT_ATR_MULT  = 0.5
BREAKOUT_PCT_FLOOR = 0.005
BREAKOUT_PCT_CEIL  = 0.025


def _atr_breakout_pct(atr: float, price: float) -> float:
    """Replica of patterns.py's _atr_breakout_pct — see constants above."""
    if price <= 0:
        return BREAKOUT_PCT_FLOOR * 3  # patterns.py's own fallback (0.015) is 3x the floor
    dynamic = BREAKOUT_ATR_MULT * atr / price
    return max(BREAKOUT_PCT_FLOOR, min(BREAKOUT_PCT_CEIL, dynamic))


# ── Ray record ───────────────────────────────────────────────────────────────

@dataclass
class RayRecord:
    id: str
    symbol: str
    timeframe: str
    side: str  # "upper" or "lower"
    slope_raw: float          # price per bar
    slope_norm: float         # ATR per bar
    anchor_ts: int
    value_at_anchor: float
    born_ts: int
    last_seen_ts: int
    lifetime_span_bars: float = 0.0
    respect_count: int = 0
    wick_rejection_count: int = 0
    state: str = "LIVE"       # "LIVE" / "BROKEN" / "RETIRED"
    last_break_ts: Optional[int] = None
    alerted_trade_ids: List[str] = field(default_factory=list)
    # Internal state-machine bookkeeping (not in the phase's listed field set,
    # but required to make update_touches/update_break_state work statelessly
    # across separate calls -- see report for why these were added).
    touch_open: bool = False
    pending_break: bool = False
    first_touch_ts: Optional[int] = None
    last_touch_ts: Optional[int] = None


def _bars_since(anchor_ts: int, target_ts: int, timeframe: str) -> float:
    sec = _TF_SECONDS.get(timeframe, _TF_SECONDS["1h"])
    return (target_ts - anchor_ts) / sec


def _project_value(ray: RayRecord, target_ts: int) -> float:
    return ray.value_at_anchor + ray.slope_raw * _bars_since(ray.anchor_ts, target_ts, ray.timeframe)


def _key(symbol: str, timeframe: str, side: str) -> str:
    return f"{symbol}|{timeframe}|{side}"


# ── Identity reconciliation ──────────────────────────────────────────────────

def _reconcile_decision(rays_for_key: List[RayRecord], slope_norm: float,
                         value_at_current_bar: float, current_bar_ts: int,
                         atr: float) -> Tuple[str, Optional[RayRecord], dict]:
    """Pure: decide MATCH or MINT against the LIVE rays already at this key.
    Returns (decision, matched_ray_or_None, info_for_log)."""
    best = None
    best_value_delta_atr = None
    best_slope_delta = None
    for ray in rays_for_key:
        if ray.state != "LIVE":
            continue
        slope_delta = abs(ray.slope_norm - slope_norm)
        existing_value_now = _project_value(ray, current_bar_ts)
        value_delta_atr = abs(existing_value_now - value_at_current_bar) / atr if atr > 0 else float("inf")
        if slope_delta <= SLOPE_MATCH_TOL and value_delta_atr <= VALUE_MATCH_TOL_ATR:
            if best is None or value_delta_atr < best_value_delta_atr:
                best = ray
                best_value_delta_atr = value_delta_atr
                best_slope_delta = slope_delta

    if best is not None:
        info = {"slope_delta": round(best_slope_delta, 6),
                "value_delta_atr": round(best_value_delta_atr, 6)}
        return "MATCHED", best, info

    return "MINTED", None, {}


def _retire_stale(rays_for_key: List[RayRecord], current_bar_ts: int) -> List[dict]:
    """Pure-ish (mutates ray.state in place): retire any LIVE ray unmatched
    for >= RAY_STALE_BARS bars of its own timeframe. Returns log entries."""
    logs = []
    for ray in rays_for_key:
        if ray.state != "LIVE":
            continue
        bars_idle = _bars_since(ray.last_seen_ts, current_bar_ts, ray.timeframe)
        if bars_idle >= RAY_STALE_BARS:
            ray.state = "RETIRED"
            logs.append({"decision": "RETIRED", "id": ray.id, "symbol": ray.symbol,
                          "timeframe": ray.timeframe, "side": ray.side,
                          "bars_idle": round(bars_idle, 2)})
    return logs


def reconcile(registry: Dict[str, List[RayRecord]], symbol: str, timeframe: str,
              side: str, slope_raw: float, slope_norm: float,
              value_at_current_bar: float, current_bar_ts: int, atr: float,
              log_path: str = RAY_IDENTITY_LOG_PATH) -> RayRecord:
    """
    The identity function. Mutates `registry` in place (adding/updating a
    RayRecord under this (symbol, timeframe, side) key) and returns the
    matched-or-minted record. Appends one human-readable JSON line per
    decision (MATCHED / MINTED / RETIRED) to log_path.

    NOTE: `registry` is an explicit parameter rather than hidden module
    state, matching derivs.py's style of pure functions over explicit
    inputs -- the phase's listed signature didn't show a registry argument,
    but reconcile() has nothing to reconcile against without one.
    """
    key = _key(symbol, timeframe, side)
    rays_for_key = registry.setdefault(key, [])

    log_lines = []
    log_lines.extend(_retire_stale(rays_for_key, current_bar_ts))

    decision, matched, info = _reconcile_decision(
        rays_for_key, slope_norm, value_at_current_bar, current_bar_ts, atr)

    if decision == "MATCHED":
        matched.slope_raw = slope_raw
        matched.slope_norm = slope_norm
        matched.anchor_ts = current_bar_ts
        matched.value_at_anchor = value_at_current_bar
        matched.last_seen_ts = current_bar_ts
        result = matched
        log_lines.append({"decision": "MATCHED", "id": matched.id, "symbol": symbol,
                           "timeframe": timeframe, "side": side, **info})
    else:
        new_ray = RayRecord(
            id=str(uuid.uuid4()), symbol=symbol, timeframe=timeframe, side=side,
            slope_raw=slope_raw, slope_norm=slope_norm,
            anchor_ts=current_bar_ts, value_at_anchor=value_at_current_bar,
            born_ts=current_bar_ts, last_seen_ts=current_bar_ts,
        )
        rays_for_key.append(new_ray)
        result = new_ray
        log_lines.append({"decision": "MINTED", "id": new_ray.id, "symbol": symbol,
                           "timeframe": timeframe, "side": side,
                           "slope_norm": round(slope_norm, 6),
                           "value_at_current_bar": round(value_at_current_bar, 6)})

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            for line in log_lines:
                line["ts"] = current_bar_ts
                f.write(json.dumps(line, default=str) + "\n")
    except OSError as e:
        print(f"[ray_registry] WARNING: could not write {log_path}: {e}")

    return result


# ── Touch state machine ──────────────────────────────────────────────────────

def update_touches(ray: RayRecord, recent_bars: List[dict], atr: float) -> RayRecord:
    """
    Pure (mutates and returns `ray`). Walks recent_bars IN ORDER (caller must
    pass only bars not already processed for this ray -- this function has no
    memory of which bars it has already seen). Each bar: {"ts","high","low","close"}.

    A bar's relevant extreme is its high for an "upper" ray, its low for a
    "lower" ray. Within TOUCH_TOL_ATR of the ray's value opens a touch, which
    stays open (hugging counts once) until price closes/pulls back
    TOUCH_RELEASE_ATR away -- a normal respected touch -- OR a wick pierces
    meaningfully beyond tolerance while the close still lands back on the
    correct side -- a fakeout/trap, which ALSO closes the touch and additionally
    increments wick_rejection_count (a trap is evidence FOR the line).
    """
    if ray.state != "LIVE":
        return ray

    tol     = TOUCH_TOL_ATR * atr
    release = TOUCH_RELEASE_ATR * atr

    for bar in recent_bars:
        line_value = _project_value(ray, bar["ts"])
        extreme = bar["high"] if ray.side == "upper" else bar["low"]

        if ray.side == "upper":
            beyond      = extreme - line_value          # + = high pierced above the line
            away        = line_value - bar["close"]     # + = close pulled back below (away from resistance)
            close_beyond = bar["close"] - line_value     # + = closed above (candidate break, not this fn's job)
        else:
            beyond      = line_value - extreme           # + = low pierced below the line
            away        = bar["close"] - line_value       # + = close bounced back above (away from support)
            close_beyond = line_value - bar["close"]       # + = closed below (candidate break)

        if not ray.touch_open:
            if abs(extreme - line_value) <= tol:
                ray.touch_open = True
                if ray.first_touch_ts is None:
                    ray.first_touch_ts = bar["ts"]
            continue

        # touch is open — decide whether it closes this bar
        if beyond > tol and close_beyond <= 0:
            # wick pierced through, close snapped back to the correct side
            ray.respect_count += 1
            ray.wick_rejection_count += 1
            ray.touch_open = False
            ray.last_touch_ts = bar["ts"]
            _update_lifetime_span(ray)
        elif away >= release:
            ray.respect_count += 1
            ray.touch_open = False
            ray.last_touch_ts = bar["ts"]
            _update_lifetime_span(ray)
        # else: still hugging — stays open, no change (counts as one touch overall)

    return ray


def _update_lifetime_span(ray: RayRecord) -> None:
    if ray.first_touch_ts is None or ray.last_touch_ts is None:
        return
    ray.lifetime_span_bars = _bars_since(ray.first_touch_ts, ray.last_touch_ts, ray.timeframe)


# ── Break state machine ──────────────────────────────────────────────────────

def update_break_state(ray: RayRecord, recent_bars: List[dict], atr: float) -> RayRecord:
    """
    Pure (mutates and returns `ray`). A ray becomes BROKEN only when a candle
    CLOSES beyond the line by more than the ATR-scaled breakout threshold
    (_atr_breakout_pct above, replicated from patterns.py) AND the very NEXT
    candle also closes beyond. One qualifying close alone leaves the ray LIVE
    with ray.pending_break=True (persisted across calls so "next candle" can
    span a call boundary). A reclaim (non-qualifying close) resets the pending
    flag — two CONSECUTIVE qualifying closes are required, not just two total.
    """
    if ray.state != "LIVE":
        return ray

    for bar in recent_bars:
        line_value = _project_value(ray, bar["ts"])
        pct = _atr_breakout_pct(atr, bar["close"])
        if ray.side == "lower":
            qualifies = bar["close"] < line_value * (1 - pct)
        else:
            qualifies = bar["close"] > line_value * (1 + pct)

        if ray.pending_break:
            if qualifies:
                ray.state = "BROKEN"
                ray.last_break_ts = bar["ts"]
                ray.pending_break = False
                break
            else:
                ray.pending_break = False
        elif qualifies:
            ray.pending_break = True

    return ray


# ── Horizon / future-crossing ─────────────────────────────────────────────────

def horizon_bars(ray: RayRecord) -> float:
    reach = ray.lifetime_span_bars * RAY_HORIZON_FRACTION
    reach = min(reach, RAY_HORIZON_MAX_BARS)
    return max(reach, RAY_HORIZON_MIN_BARS)


def time_to_cross(ray: RayRecord, value_now: float, levels: List[Tuple[float, str]]) -> List[dict]:
    """
    Pure. `levels` is a flat list of (price, label) statics — Fib, validated
    horizontal S/R, VP POC/VAH/VAL — supplied by the CALLER (Phase R3/R4);
    this function computes nothing about what those levels are. Adding a new
    static tool later means only appending to that input list.
    Returns levels the ray is heading toward within its horizon, nearest first.
    """
    if ray.slope_raw == 0:
        return []
    h = horizon_bars(ray)
    out = []
    for price, label in levels:
        bars = (price - value_now) / ray.slope_raw
        if bars <= 0:
            continue  # behind the ray's path — already passed or receding
        if bars <= h:
            out.append({"label": label, "price": price, "bars": round(bars, 2)})
    out.sort(key=lambda r: r["bars"])
    return out


# ── Rendering/narration cap ───────────────────────────────────────────────────

def select_live(rays: List[RayRecord], current_bar_ts: int,
                atr_by_key: Dict[Tuple[str, str], float],
                r_squared_by_id: Optional[Dict[str, float]] = None) -> List[RayRecord]:
    """
    Per (symbol, timeframe, side) group: keep at most MAX_LIVE_PER_SIDE LIVE
    rays, ranked by respect_count (r_squared_by_id supplied by the caller as
    tiebreak). A candidate is dropped as a near-duplicate of an already-kept
    ray if BOTH its projected value is within DISTINCT_MIN_ATR x ATR of that
    ray's AND its normalized slope is within SLOPE_MATCH_TOL of it.
    """
    r_squared_by_id = r_squared_by_id or {}
    groups: Dict[str, List[RayRecord]] = {}
    for ray in rays:
        if ray.state != "LIVE":
            continue
        groups.setdefault(_key(ray.symbol, ray.timeframe, ray.side), []).append(ray)

    kept_all: List[RayRecord] = []
    for key, group in groups.items():
        symbol, timeframe, _side = key.split("|")
        atr = atr_by_key.get((symbol, timeframe), 0.0)
        ranked = sorted(group, key=lambda r: (r.respect_count, r_squared_by_id.get(r.id, 0.0)),
                         reverse=True)
        kept: List[RayRecord] = []
        for cand in ranked:
            cand_value = _project_value(cand, current_bar_ts)
            is_dup = False
            for k in kept:
                k_value = _project_value(k, current_bar_ts)
                value_delta_atr = abs(cand_value - k_value) / atr if atr > 0 else 0.0
                slope_delta = abs(cand.slope_norm - k.slope_norm)
                if value_delta_atr < DISTINCT_MIN_ATR and slope_delta <= SLOPE_MATCH_TOL:
                    is_dup = True
                    break
            if is_dup:
                continue
            kept.append(cand)
            if len(kept) >= MAX_LIVE_PER_SIDE:
                break
        kept_all.extend(kept)

    return kept_all


# ── Chev-facing formatting (Phase R4) ────────────────────────────────────────
# FACT FRAMING ONLY: state where the ray is, where it will be, and its record.
# No trade suggestions, no "this favours a long", no requirement language
# ("wait for" / "must" / "confirm before"). The ray is information Chev MAY
# use -- arithmetic is Dexter's, judgment is Chev's. Mirrors the style of
# engines.py's format_invalidation_candidates_for_chev / format_validation_
# candidates_for_chev: takes an already-computed list (the caller runs
# select_live() and filters to one symbol/timeframe first -- this function
# recomputes nothing), "none found"-style empty handling (here: nothing at
# all -- a bare respected ray is common enough that a line on every single
# escalation would be noise, unlike invalidation which the skip discipline
# requires Chev to always see something for).

def format_ray_block_for_chev(rays: List[RayRecord], current_price: float,
                              timeframe: str, levels: Optional[List[Tuple[float, str]]] = None) -> str:
    """
    Build the "TRENDLINE RAY" text block. `rays` must already be select_live()-
    filtered to a single (symbol, timeframe) -- at most 2 per side. `levels`
    (optional) is the same flat (price, label) static-level list time_to_cross()
    takes; omit it (checkpoint caller) to skip the crossing line entirely.
    Returns "" when `rays` is empty -- nothing at all, not a "none found" line.

    No ATR parameter: every value this renders (slope_norm, slope_raw,
    horizon_bars, time_to_cross) is already ATR-normalized on the RayRecord
    itself at reconcile() time by the caller -- there is nothing left here
    that needs a fresh ATR.
    """
    if not rays:
        return ""

    now_ts = max(r.last_seen_ts for r in rays)
    lines = ["TRENDLINE RAY:"]

    for ray in rays:
        side_word  = "resistance" if ray.side == "upper" else "support"
        slope_word = ("rising" if ray.slope_norm > 0
                      else "falling" if ray.slope_norm < 0 else "flat")
        value_now  = _project_value(ray, now_ts)
        h_bars     = horizon_bars(ray)
        tf_seconds = _TF_SECONDS.get(timeframe, _TF_SECONDS["1h"])
        h_hours    = round(h_bars * tf_seconds / 3600.0, 1)
        value_h    = value_now + ray.slope_raw * h_bars
        life_hours = round(ray.lifetime_span_bars * tf_seconds / 3600.0, 1)

        lines.append(
            f"  {slope_word} {side_word}: now {value_now:.5f}, ~{h_bars:.0f}c/{timeframe} "
            f"(≈{h_hours}h) → {value_h:.5f}. Held {ray.respect_count}x, "
            f"{ray.wick_rejection_count} wick-traps, {life_hours}h."
        )

        if levels:
            crossings = time_to_cross(ray, value_now, levels)
            if crossings:
                nearest = crossings[0]
                lines.append(f"    → {nearest['label']} in ~{nearest['bars']:.0f}c, tentative.")

    upper = next((r for r in rays if r.side == "upper"), None)
    lower = next((r for r in rays if r.side == "lower"), None)
    if upper and lower and abs(upper.slope_norm - lower.slope_norm) <= SLOPE_MATCH_TOL:
        u_now = _project_value(upper, now_ts)
        l_now = _project_value(lower, now_ts)
        u_h   = u_now + upper.slope_raw * horizon_bars(upper)
        l_h   = l_now + lower.slope_raw * horizon_bars(lower)
        pct_in = ((current_price - l_now) / (u_now - l_now) * 100.0) if (u_now - l_now) else 0.0
        lines.append(
            f"  channel: {l_now:.5f}-{u_now:.5f} now (price {pct_in:.0f}% through); "
            f"horizon {l_h:.5f}-{u_h:.5f}."
        )

    lines.append("")
    return "\n".join(lines) + "\n"


# ── Concurrency ───────────────────────────────────────────────────────────────

REGISTRY_LOCK = threading.Lock()
# scan_pair_tf() runs concurrently across (symbol, timeframe) pairs (dexter.py
# ~13149, ThreadPoolExecutor). A lock only around save_registry()'s own write
# would NOT be enough -- the race is load-mutate-save as a whole: two threads
# could both load the same on-disk state, mutate their own in-memory copies,
# and the second save silently discards the first thread's changes (the same
# class of bug already fixed once for chev_journal.json/weight_overrides.json
# elsewhere in this project). Callers must hold this lock across their entire
# load -> reconcile(...) -> update_touches(...) -> update_break_state(...) ->
# save_registry(...) sequence for a given scan.

# ── Persistence ───────────────────────────────────────────────────────────────

def load_registry(path: str = REGISTRY_PATH) -> Dict[str, List[RayRecord]]:
    """Missing or corrupt file -> empty registry, logged, never raises."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: [RayRecord(**r) for r in v] for k, v in raw.items()}
    except Exception as e:
        print(f"[ray_registry] WARNING: {path} unreadable/corrupt ({e}) — "
              f"starting with an empty registry.")
        return {}


def save_registry(registry: Dict[str, List[RayRecord]], path: str = REGISTRY_PATH) -> None:
    """Atomic write (temp file + os.replace) — same crash-safe pattern as
    dexter.py's _atomic_write_json (dexter.py ~line 844), replicated here to
    keep this module standalone rather than importing dexter.py."""
    serializable = {k: [dataclasses.asdict(r) for r in v] for k, v in registry.items()}
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)
    os.replace(tmp_path, path)


# ── SELF-TEST (offline, no dexter import, no network) ───────────────────────
if __name__ == "__main__":
    import tempfile

    T = []

    def check(name, cond):
        T.append((name, bool(cond)))
        print(("PASS  " if cond else "FAIL  ") + name)

    _tmp_dir = tempfile.mkdtemp(prefix="ray_registry_selftest_")
    _log_path = os.path.join(_tmp_dir, "identity_log.jsonl")

    # ── 1. Identity match vs mint at both tolerance boundaries ──────────────
    reg = {}
    ATR = 10.0
    T0 = 1_700_000_000
    HOUR = 3600

    r1 = reconcile(reg, "BTCUSDT", "1h", "lower", slope_raw=2.0, slope_norm=0.2,
                    value_at_current_bar=100.0, current_bar_ts=T0, atr=ATR, log_path=_log_path)
    check("1a: first call mints", r1.state == "LIVE" and r1.respect_count == 0)
    check("1b: registry has exactly 1 ray after mint", len(reg[_key("BTCUSDT", "1h", "lower")]) == 1)

    # existing ray projects to 100 + 2.0*1 = 102.0 one bar later; push slope
    # delta to exactly SLOPE_MATCH_TOL and value delta to exactly the ATR tol
    # boundary -- spec says "within" (<=), so this must still MATCH.
    r2 = reconcile(reg, "BTCUSDT", "1h", "lower",
                    slope_raw=3.5, slope_norm=0.2 + SLOPE_MATCH_TOL,
                    value_at_current_bar=102.0 + VALUE_MATCH_TOL_ATR * ATR,
                    current_bar_ts=T0 + HOUR, atr=ATR, log_path=_log_path)
    check("1c: exact boundary (slope+value) still MATCHES", r2.id == r1.id)
    check("1d: MATCH updates slope/anchor, keeps tallies", r2.slope_raw == 3.5 and r2.respect_count == 0)

    # existing ray (updated) now projects to 107.0 + 3.5*1 = 110.5 one bar
    # later; push value delta just OVER tolerance -> must MINT a new id.
    r3 = reconcile(reg, "BTCUSDT", "1h", "lower",
                    slope_raw=3.5, slope_norm=0.2 + SLOPE_MATCH_TOL,
                    value_at_current_bar=110.5 + VALUE_MATCH_TOL_ATR * ATR + 0.01,
                    current_bar_ts=T0 + 2 * HOUR, atr=ATR, log_path=_log_path)
    check("1e: just-over-boundary value delta MINTS a new id", r3.id != r2.id)
    check("1f: registry now holds 2 live rays (old kept, not evicted by a non-match)",
          len(reg[_key("BTCUSDT", "1h", "lower")]) == 2)

    # ── 2. Retire-on-stale ────────────────────────────────────────────────────
    reg2 = {}
    base = reconcile(reg2, "ETHUSDT", "1h", "upper", slope_raw=-1.0, slope_norm=-0.1,
                       value_at_current_bar=50.0, current_bar_ts=T0, atr=ATR, log_path=_log_path)
    # 99 bars later, non-matching fit -> old ray should NOT yet be stale
    reconcile(reg2, "ETHUSDT", "1h", "upper", slope_raw=-9.0, slope_norm=-9.0,
              value_at_current_bar=9999.0, current_bar_ts=T0 + 99 * HOUR, atr=ATR, log_path=_log_path)
    check("2a: not yet stale at 99 bars idle", base.state == "LIVE")
    # 100 bars idle from base's last_seen (still T0, since it was never matched again) -> retire
    reconcile(reg2, "ETHUSDT", "1h", "upper", slope_raw=-9.0, slope_norm=-9.0,
              value_at_current_bar=9999.0, current_bar_ts=T0 + 100 * HOUR, atr=ATR, log_path=_log_path)
    check("2b: retired at 100 bars idle", base.state == "RETIRED")

    # ── 3. Hugging counts as one touch ───────────────────────────────────────
    hug_ray = RayRecord(id="hug", symbol="X", timeframe="1h", side="lower",
                         slope_raw=0.0, slope_norm=0.0, anchor_ts=T0, value_at_anchor=100.0,
                         born_ts=T0, last_seen_ts=T0)
    hug_bars = []
    for i in range(5):
        # lows hug within tolerance (line value stays 100.0 since slope 0)
        hug_bars.append({"ts": T0 + i * HOUR, "high": 101.0, "low": 100.0 + 0.1, "close": 100.5})
    # then release far away (>= 0.5*ATR = 5.0)
    hug_bars.append({"ts": T0 + 5 * HOUR, "high": 108.0, "low": 106.0, "close": 107.0})
    update_touches(hug_ray, hug_bars, ATR)
    check("3: hugging for 5 bars then releasing = exactly ONE respected touch",
          hug_ray.respect_count == 1 and not hug_ray.touch_open)

    # ── 4. Wick-through-snap-back increments both tallies ───────────────────
    wick_ray = RayRecord(id="wick", symbol="X", timeframe="1h", side="lower",
                          slope_raw=0.0, slope_norm=0.0, anchor_ts=T0, value_at_anchor=100.0,
                          born_ts=T0, last_seen_ts=T0)
    wick_bars = [
        {"ts": T0,          "high": 101.0, "low": 100.1, "close": 100.5},  # opens touch (within 0.3*ATR=3)
        {"ts": T0 + HOUR,   "high": 101.0, "low": 92.0,  "close": 100.2},  # wick THROUGH (low far below tol),
                                                                            # close back on correct side (>=100)
    ]
    update_touches(wick_ray, wick_bars, ATR)
    check("4: wick-through-snap-back increments respect_count AND wick_rejection_count",
          wick_ray.respect_count == 1 and wick_ray.wick_rejection_count == 1 and not wick_ray.touch_open)

    # ── 5. Two-close break confirmation (one close alone = not broken) ──────
    brk_ray = RayRecord(id="brk", symbol="X", timeframe="1h", side="lower",
                         slope_raw=0.0, slope_norm=0.0, anchor_ts=T0, value_at_anchor=100.0,
                         born_ts=T0, last_seen_ts=T0)
    pct = _atr_breakout_pct(ATR, 90.0)  # ~5.5% clamped to 2.5% ceiling at these numbers
    qualifying_close = 100.0 * (1 - pct) - 0.5  # comfortably beyond threshold
    update_break_state(brk_ray, [{"ts": T0, "high": 100, "low": qualifying_close, "close": qualifying_close}], ATR)
    check("5a: one qualifying close alone -> still LIVE, pending flag set",
          brk_ray.state == "LIVE" and brk_ray.pending_break is True)
    update_break_state(brk_ray, [{"ts": T0 + HOUR, "high": 100, "low": 99, "close": 99.5}], ATR)  # reclaim
    check("5b: non-qualifying follow-up resets pending, stays LIVE",
          brk_ray.state == "LIVE" and brk_ray.pending_break is False)
    update_break_state(brk_ray, [{"ts": T0 + 2 * HOUR, "high": 100, "low": qualifying_close, "close": qualifying_close}], ATR)
    update_break_state(brk_ray, [{"ts": T0 + 3 * HOUR, "high": 100, "low": qualifying_close, "close": qualifying_close}], ATR)
    check("5c: two CONSECUTIVE qualifying closes -> BROKEN", brk_ray.state == "BROKEN")

    # ── 6. Horizon growth with lifetime + 40-bar cap ─────────────────────────
    r_young = RayRecord(id="y", symbol="X", timeframe="1h", side="lower", slope_raw=1, slope_norm=0.1,
                         anchor_ts=T0, value_at_anchor=100, born_ts=T0, last_seen_ts=T0, lifetime_span_bars=3)
    r_mid   = RayRecord(id="m", symbol="X", timeframe="1h", side="lower", slope_raw=1, slope_norm=0.1,
                         anchor_ts=T0, value_at_anchor=100, born_ts=T0, last_seen_ts=T0, lifetime_span_bars=30)
    r_old    = RayRecord(id="o", symbol="X", timeframe="1h", side="lower", slope_raw=1, slope_norm=0.1,
                         anchor_ts=T0, value_at_anchor=100, born_ts=T0, last_seen_ts=T0, lifetime_span_bars=200)
    check("6a: young ray floors at RAY_HORIZON_MIN_BARS", horizon_bars(r_young) == RAY_HORIZON_MIN_BARS)
    check("6b: mid-life ray = lifetime/3", abs(horizon_bars(r_mid) - 10.0) < 1e-9)
    check("6c: old ray caps at RAY_HORIZON_MAX_BARS", horizon_bars(r_old) == RAY_HORIZON_MAX_BARS)

    # ── 7. time_to_cross: direction-awareness + horizon filtering ────────────
    cross_ray = RayRecord(id="c", symbol="X", timeframe="1h", side="lower", slope_raw=1.0, slope_norm=0.1,
                          anchor_ts=T0, value_at_anchor=100.0, born_ts=T0, last_seen_ts=T0,
                          lifetime_span_bars=30)  # horizon = 10 bars
    levels = [
        (105.0, "ahead_within_horizon"),   # bars = 5 -> included
        (200.0, "ahead_beyond_horizon"),   # bars = 100 -> excluded
        (90.0,  "behind_the_path"),        # bars = -10 -> excluded (direction-awareness)
    ]
    crossings = time_to_cross(cross_ray, value_now=100.0, levels=levels)
    labels = [c["label"] for c in crossings]
    check("7: only the within-horizon, ahead-of-path level survives",
          labels == ["ahead_within_horizon"])

    # ── 8. select_live drops a near-duplicate, keeps a distinct one ──────────
    ray_a = RayRecord(id="a", symbol="X", timeframe="1h", side="upper", slope_raw=0, slope_norm=0.0,
                       anchor_ts=T0, value_at_anchor=100.0, born_ts=T0, last_seen_ts=T0, respect_count=5)
    ray_b = RayRecord(id="b", symbol="X", timeframe="1h", side="upper", slope_raw=0, slope_norm=0.0,
                       anchor_ts=T0, value_at_anchor=100.4, born_ts=T0, last_seen_ts=T0, respect_count=4)  # near-dup of A
    ray_c = RayRecord(id="cc", symbol="X", timeframe="1h", side="upper", slope_raw=0, slope_norm=0.0,
                       anchor_ts=T0, value_at_anchor=120.0, born_ts=T0, last_seen_ts=T0, respect_count=3)  # distinct
    kept = select_live([ray_a, ray_b, ray_c], current_bar_ts=T0, atr_by_key={("X", "1h"): ATR})
    kept_ids = sorted(r.id for r in kept)
    check("8: near-duplicate (b) dropped, distinct ray (cc) kept alongside (a)",
          kept_ids == sorted(["a", "cc"]))

    # ── 9. Persistence round-trip ─────────────────────────────────────────────
    reg_path = os.path.join(_tmp_dir, "registry.json")
    round_trip_reg = {_key("X", "1h", "upper"): [ray_a, ray_c]}
    save_registry(round_trip_reg, reg_path)
    loaded = load_registry(reg_path)
    loaded_ray = loaded[_key("X", "1h", "upper")][0]
    check("9: persistence round-trip preserves id/respect_count/state",
          loaded_ray.id == ray_a.id and loaded_ray.respect_count == ray_a.respect_count
          and loaded_ray.state == ray_a.state)

    # ── 10. Corrupt-file recovery ─────────────────────────────────────────────
    corrupt_path = os.path.join(_tmp_dir, "corrupt.json")
    with open(corrupt_path, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ]]]")
    recovered = load_registry(corrupt_path)
    check("10: corrupt file -> empty registry, no exception", recovered == {})

    missing_path = os.path.join(_tmp_dir, "does_not_exist.json")
    check("10b: missing file -> empty registry, no exception", load_registry(missing_path) == {})

    # ── 11. Concurrency: REGISTRY_LOCK prevents a lost-update race ──────────
    # Mirrors the REAL call pattern (each scan does its own independent
    # load -> mutate -> save round trip, not a shared in-memory object) --
    # without the lock serializing that whole span, two threads racing this
    # would silently drop each other's minted ray (last save wins).
    import threading as _threading

    _conc_path    = os.path.join(_tmp_dir, "concurrent_registry.json")
    _conc_symbols = [f"SYM{i}" for i in range(8)]

    def _conc_worker(sym):
        with REGISTRY_LOCK:
            _r = load_registry(_conc_path)
            reconcile(_r, sym, "1h", "upper", slope_raw=1.0, slope_norm=0.1,
                      value_at_current_bar=100.0, current_bar_ts=T0, atr=ATR, log_path=_log_path)
            save_registry(_r, _conc_path)

    _threads = [_threading.Thread(target=_conc_worker, args=(s,)) for s in _conc_symbols]
    for t in _threads:
        t.start()
    for t in _threads:
        t.join()

    _conc_loaded = load_registry(_conc_path)
    check("11: concurrent independent load->mutate->save round trips under "
          "the lock lose no symbol's ray",
          set(_conc_loaded.keys()) == {_key(s, "1h", "upper") for s in _conc_symbols})

    # ── 12. format_ray_block_for_chev (Phase R4) ─────────────────────────────
    CHARS_PER_TOKEN = 3.5  # matches audit_context.py's own estimator exactly

    check("12a: empty rays list -> empty string (nothing at all, not 'none found')",
          format_ray_block_for_chev([], current_price=100.0, timeframe="1h") == "")

    _fmt_upper = RayRecord(id="fu", symbol="BTCUSDT", timeframe="15m", side="upper",
                           slope_raw=-0.5, slope_norm=-0.05, anchor_ts=T0, value_at_anchor=61500.0,
                           born_ts=T0, last_seen_ts=T0, respect_count=4, wick_rejection_count=1,
                           lifetime_span_bars=44)
    _fmt_lower = RayRecord(id="fl", symbol="BTCUSDT", timeframe="15m", side="lower",
                           slope_raw=0.4, slope_norm=0.04, anchor_ts=T0, value_at_anchor=61200.0,
                           born_ts=T0, last_seen_ts=T0, respect_count=3, wick_rejection_count=2,
                           lifetime_span_bars=40)
    # Levels chosen to be within EACH ray's own horizon and ahead of its path,
    # so both crossing lines actually render -- the true worst case (2 rays +
    # channel + 2 crossings), not an accidental best case.
    _fmt_levels = [(61495.0, "Fib 61.8% (golden pocket)"), (61203.2, "VP POC 4h")]
    _worst_case_block = format_ray_block_for_chev(
        [_fmt_upper, _fmt_lower], current_price=61350.0,
        timeframe="15m", levels=_fmt_levels)
    _worst_case_tokens = len(_worst_case_block) / CHARS_PER_TOKEN
    check(f"12b: worst case (2 rays + channel + 2 crossings) <= 130 tokens "
          f"(got {_worst_case_tokens:.0f})",
          _worst_case_tokens <= 130)
    check("12c: channel line renders (opposite sides, slopes within SLOPE_MATCH_TOL)",
          "channel:" in _worst_case_block)
    check("12d: both crossing lines render in the worst case",
          _worst_case_block.count("tentative.") == 2)

    # Checkpoint-style call: single ray, no levels -- no crossing/channel line,
    # never crashes on the omitted optional argument.
    _checkpoint_block = format_ray_block_for_chev([_fmt_lower], current_price=61250.0,
                                                  timeframe="15m")
    check("12e: single ray, no levels (checkpoint style) -> no crossing/channel line",
          "tentative" not in _checkpoint_block and "channel:" not in _checkpoint_block)

    # Fact-framing rule: no trade suggestions, no requirement language, no
    # directional bias words -- this block states the ray's facts only.
    _banned_phrases = ["must", "confirm before", "wait for", "favours", "should enter",
                       "bullish", "bearish"]
    _lower_block = _worst_case_block.lower()
    check("12f: fact-framing -- no banned suggestion/requirement/bias language",
          not any(p in _lower_block for p in _banned_phrases))

    failed = [n for n, ok in T if not ok]
    print(f"\n{len(T) - len(failed)}/{len(T)} tests passing")
    if failed:
        raise SystemExit("FAILED: " + ", ".join(failed))
