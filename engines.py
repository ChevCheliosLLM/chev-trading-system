"""
engines.py — Dexter Analysis Engine Stack
==========================================
Architecture: OHLCV → AssetProfile → Swing → Leg → State → Geometry → Auction → Hypothesis → MarketSurvey

Dexter never predicts. Dexter measures.
Chev never measures. Chev interprets.

Engine dependency graph:
    OHLCV ──► Asset Profile Engine ──► AssetProfile (sorted distributions for percentile ranking)
              │  Runs FIRST. Every downstream engine uses it to answer:
              │  "How unusual is this measurement FOR THIS SPECIFIC ASSET?"
              │
              ├──► Swing Engine ──► SwingPoint[] + SwingAnalysis[]
              │                   └──► Leg Engine (+ profile) ──► Leg[] with dist_atr_pct, energy_pct
              │                                                  └──► State Engine (+ profile) ──► MarketState with participation_pct
              │                   └──► Geometry Engine (+ profile) ──► GeometryReport with compression_pct
              │
    Leg[] + MarketState ──────► Auction Engine ──► ActiveAuction
    GeometryReport + ActiveAuction + Leg[] ──► Hypothesis Engine ──► Hypothesis[]
    All of the above ─────────────────────────► MarketSurvey (what Chev reads)

    Trade outcomes ──► Performance Engine (separate — never modifies Dexter)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS — type definitions (the source of truth for the entire stack)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    """
    Immutable raw turning-point. Never modified after creation.
    SwingPoints are the atoms of the entire analysis — everything else derives from them.
    """
    id: str                          # "sw_H_<ts_ms>" or "sw_L_<ts_ms>"
    index: int                       # bar index in the source DataFrame
    ts: datetime
    price: float
    kind: Literal["HIGH", "LOW"]
    atr_at_swing: float              # ATR value at the time this swing formed
    volume_at_swing: float
    parent_id: Optional[str] = None  # higher-TF swing that contains this one (Swing Tree)


@dataclass
class SwingAnalysis:
    """
    Derived scoring for a SwingPoint.
    Recomputable — kept separate so algorithm changes don't corrupt historical SwingPoints.
    Never: raw price data, raw OHLCV.
    """
    swing_id: str
    prominence: float        # distance to nearest opposing swing / ATR (higher = more significant)
    retest_count: int        # how many times price returned within 0.5 ATR of this level
    age_bars: int
    confirmed: bool          # True when confirm_window candles have closed past this swing
    structural_weight: float # 0.0–1.0 composite significance score


@dataclass
class Leg:
    """
    Bridge between swings and auctions.
    A Leg is the directional move from one SwingPoint to the next.
    Everything in the hypothesis and auction analysis anchors to Legs.
    Never: names patterns, fits trendlines to non-swing data, assigns Phase.
    """
    id: str                  # "leg_<start_idx>_<end_idx>"
    start_swing: SwingPoint
    end_swing: SwingPoint
    direction: Literal["UP", "DOWN"]

    distance_pct: float      # abs(end - start) / start * 100
    distance_atr: float      # distance / ATR at leg start
    bar_count: int           # candles this leg spans

    character: Literal["IMPULSIVE", "CORRECTIVE", "NEUTRAL"]

    energy: float            # 0.0–1.0 composite (velocity + body ratio + volume)
    speed: float             # distance_pct / bar_count (% per bar)
    acceleration: float      # speed vs prior same-direction leg (positive = accelerating)

    corrects_leg_id: Optional[str] = None
    retracement_pct: float = 0.0    # 0.0 = no retrace, 1.0 = full

    confidence: float = 1.0  # min(start_confirmed, end_confirmed)
    created_because: str = ""

    # Set by Auction Engine after legs are built (requires auction context)
    auction_role: Literal["PRIMARY_IMPULSE", "CORRECTIVE", "BALANCE_LEG", "POST_BREAKOUT", "UNKNOWN"] = "UNKNOWN"
    anchor_score: float = 0.0   # 0–100: suitability as VP/Fib anchor
    is_anchor_candidate: bool = False

    # Set by Asset Profile Engine — percentile rank vs this asset's historical leg distribution
    dist_atr_pct: int = 50       # where distance_atr sits in this asset's history (0=smallest, 100=largest)
    energy_pct: int = 50         # where energy sits in this asset's energy distribution


@dataclass
class MarketState:
    """
    Continuous two-axis characterisation of market behaviour.
    No categorical labels — these are measurements, not verdicts.
    Answers: how much of the market is participating, and in which direction?
    Never: fits trendlines, detects patterns, assigns Phase.
    """
    participation: float    # 0.0–100.0 (0 = dead/drifting, 100 = full engagement)
    direction: float        # -100.0 to +100.0 (-100 = strongly bearish, +100 = strongly bullish)

    # Phase — describes the structural behaviour driving State, used by Auction Engine
    phase: Literal["EXPANSION", "COMPRESSION", "BREAKOUT", "UNKNOWN"]
    atr_trend: Literal["EXPANDING", "CONTRACTING", "FLAT"]
    volume_trend: Literal["RISING", "FALLING", "FLAT"]
    leg_sequence: str       # last 5 legs as human-readable: "IMP_UP ->CORR_DOWN ->IMP_UP"

    confidence: float = 1.0  # min swing confidence across all input legs
    participation_breakdown: dict = field(default_factory=dict)
    # {"atr_component": float, "volume_component": float, "leg_component": float, "range_component": float}
    participation_pct: int = 50  # where this participation level sits vs this asset's history


@dataclass
class TrendlineFit:
    """A linear fit to a sequence of swing highs or lows."""
    slope_norm: float   # slope per ATR per bar (for display/comparison)
    slope_raw: float    # actual slope in price/bar units (for evaluation — computing price at bar X)
    intercept: float    # y-intercept in price units
    r_squared: float    # 0.0–1.0 fit quality
    touch_count: int    # number of swing points used to fit


@dataclass
class GeometryReport:
    """
    Pure measurement of swing geometry. No pattern names. No predictions.
    Answers: what shape is price making, and how confident is the measurement?
    Input: SwingPoint[] (primary TF only)
    Never: names patterns, accesses market state, accesses auction.
    """
    upper: TrendlineFit
    lower: TrendlineFit

    compression: float    # 0.0 = wide open, 1.0 = fully converged (apex reached)
    parallelism: float    # 1.0 = perfect channel, 0.0 = fully converging/diverging

    is_converging: bool   # upper falling AND lower rising simultaneously
    is_diverging: bool    # upper rising AND lower falling
    is_parallel: bool     # parallelism > 0.85 AND not converging

    breakout_up: bool     # close above upper trendline by > 0.2%
    breakout_dn: bool     # close below lower trendline by > 0.2%
    breakout_bar: Optional[int] = None

    vol_at_breakout: float = 0.0
    avg_vol: float = 0.0

    has_impulse: bool = False                          # prior move ≥ 4 ATR (flag/pennant eligible)
    impulse_atr: float = 0.0
    impulse_direction: Optional[Literal["UP", "DOWN"]] = None

    # Measurement quality — propagated to Hypothesis as confidence input
    measurement_quality: float = 1.0   # min(upper.r_squared, lower.r_squared)
    sample_size: int = 0               # number of swing points used to fit
    confidence: float = 1.0           # weakest-link from input swing quality
    structure_axis: Literal["ASCENDING", "DESCENDING", "HORIZONTAL", "CONTRACTING", "EXPANDING", "ASYMMETRIC"] = "CONTRACTING"
    # ASCENDING/DESCENDING = parallel trend; HORIZONTAL = range; CONTRACTING = triangle family;
    # EXPANDING = megaphone; ASYMMETRIC = one flat + one sloped (ascending/descending triangle)
    compression_pct: int = 50    # where this compression sits vs this asset's historical compression distribution


@dataclass
class AcceptanceZone:
    """A price zone where the market spent time and confirmed value."""
    price_low: float
    price_high: float
    time_spent_pct: float  # fraction of auction bars where midpoint was in this zone
    visit_count: int       # how many times price re-entered this zone
    rejected: bool         # True: price spiked through but didn't stay (liquidity grab)


@dataclass
class ActiveAuction:
    """
    The current live negotiation zone.
    Tracks auction lifecycle: where is the active balance, is it still valid?
    Input: Leg[] + MarketState
    Never: determines trading direction, fits trendlines, names patterns.
    """
    id: str               # "auc_<anchor_ts_ms>"
    anchor_price: float   # midpoint of the balance area (approximation of fair value)
    anchor_bar: int       # bar index where this auction began
    age_bars: int

    state: Literal["ACTIVE", "BALANCING", "INACTIVE", "FAILED"]

    balance_high: float         # upper boundary of the balance area
    balance_low: float          # lower boundary
    balance_width_atr: float    # balance range / ATR (how wide is the auction?)

    accepted_zones: List[AcceptanceZone]
    rejected_above: Optional[float] = None  # price spiked above but returned — supply
    rejected_below: Optional[float] = None  # price spiked below but returned — demand

    poc: Optional[float] = None  # Volume Profile Point of Control
    vah: Optional[float] = None  # Value Area High (70% of volume)
    val: Optional[float] = None  # Value Area Low

    fib_anchor_high: Optional[float] = None  # swing high of the impulse before the auction
    fib_anchor_low: Optional[float] = None   # swing low of the impulse before the auction

    energy: float = 1.0           # decays with age; resets on breakout attempt
    imbalance_score: float = 0.0  # 0 = balanced, 1 = severe (breakout pressure building)

    created_because: str = ""     # e.g. "BOS + Volume Expansion + Participation=84"
    confidence: float = 1.0       # weakest-link from input leg confidence

    # Set by Auction Engine
    maturity: float = 0.0               # 0=fresh, 1=overripe (age_bars / typical_duration)
    balance_score: float = 0.0          # 0-1: quality of balance area (tight + POC + time-in-value)
    defended_levels: List[float] = field(default_factory=list)  # tested AND held
    anchor_leg_id: Optional[str] = None # ID of the impulse leg that created this auction


@dataclass
class RiskSurface:
    """
    Dynamic invalidation — updated every scan as price moves.
    Replaces static "invalidation_price" with a surface that tracks urgency.
    The hypothesis geometry is static. The risk proximity is dynamic.
    """
    invalidation_price: float      # hard price level that kills this hypothesis
    current_distance_pct: float    # how far price currently is from invalidation
    urgency: float                 # 0.0 = far away, 1.0 = imminent (price at the door)
    time_pressure: float           # 0.0 = fresh setup, 1.0 = stale (should have triggered by now)


@dataclass
class Hypothesis:
    """
    A competing explanation for what the geometry + auction mean.
    The Hypothesis Engine always produces a ranked list — never a single "the" pattern.
    Chev reads the list and decides which hypothesis (if any) to act on.
    """
    id: str                  # "hyp_<pattern_name>_<ts_ms>"
    name: str                # e.g. "BULL_FLAG", "ASCENDING_TRIANGLE", "COMPRESSION"
    bias: Literal["LONG", "SHORT", "NEUTRAL"]
    confidence: float        # 0.0–1.0 — weakest-link across geometry + state + auction

    because: List[str]       # conditions that support this hypothesis
    against: List[str]       # conditions that contradict it
    missing: List[str]       # required conditions not yet met (incomplete, not wrong)

    risk: RiskSurface
    age_bars: int            # how long this pattern has been forming
    urgency: float           # 0.0 = patient, 1.0 = act now or miss

    breakout_level: Optional[float] = None  # price that confirms the hypothesis

    status: Literal["FORMING", "CONFIRMED", "FAILED", "EXPIRED"] = "FORMING"
    created_because: str = ""     # single condition that triggered hypothesis creation

    geometry_confidence: float = 1.0
    state_confidence: float = 1.0
    auction_confidence: float = 1.0

    expected_next_event: str = ""    # what happens next if hypothesis is correct
    expected_entry_trigger: str = "" # specific price action that triggers entry


@dataclass
class Opportunity:
    """
    Final synthesis layer — the highest-confidence actionable setup right now.
    Combines top hypothesis + auction structure into one snapshot for Chev.
    Chev reads this first before digging into individual hypotheses.
    Never predicts. Reports structure + what must happen for the setup to be valid.
    """
    id: str
    auction_id: str
    hypothesis_name: str
    bias: Literal["LONG", "SHORT"]

    entry_zone: Tuple[float, float]      # (low, high) entry price range
    structural_rr: float                  # R:R from structure alone (target / stop)
    confluence_score: float               # 0–100 mechanical score

    urgency: float                         # 0=patient, 1=act now
    expected_trigger: str                 # price action that confirms entry
    expiry_bars: int                      # bars until setup expires if not triggered

    reward_profile: Literal["AT_BALANCE_EXTREME", "BREAKOUT_RETRACE", "MID_BALANCE"]
    quality: Literal["A+", "A", "B", "C"] = "B"

    invalidation_price: float = 0.0
    target_price: float = 0.0


@dataclass
class AssetProfile:
    """
    Statistical context for one (symbol, timeframe) combination.
    Built from the full available OHLCV history before active analysis.
    Allows every downstream engine to answer "how unusual is this FOR THIS ASSET?"
    instead of applying fixed, asset-agnostic thresholds.
    Not a prediction — a calibration.
    """
    symbol: str
    timeframe: str
    computed_from_bars: int       # how many bars of history were used
    n_legs: int                   # sample size — quality indicator for this profile

    # Sorted arrays — use _percentile_rank(value, sorted_array) to get a 0-100 rank
    leg_atr_sorted: List[float]        # all leg distances in ATR units
    leg_energy_sorted: List[float]     # all leg energy values (0–1)
    vol_expansion_sorted: List[float]  # leg avg volume vs rolling 20-bar baseline
    retracement_sorted: List[float]    # pullback depth as fraction of prior impulse
    balance_width_sorted: List[float]  # historical balance zone widths in ATR units
    participation_sorted: List[float]  # rolling participation estimates (0–100)


@dataclass
class MarketSurvey:
    """
    Complete Dexter output for one (symbol, primary_tf) scan.
    This is the object Chev reads. Chev never re-measures — it only interprets.
    """
    symbol: str
    primary_tf: str
    scanned_at: datetime
    current_price: float

    swings: List[SwingPoint]
    swing_analyses: List[SwingAnalysis]
    legs: List[Leg]
    state: MarketState
    geometry: GeometryReport
    auction: Optional[ActiveAuction]
    hypotheses: List[Hypothesis]   # ranked: highest confidence first

    dexter_score: float
    dexter_reasons: List[str]
    regime_4h: str

    sr_support: Optional[float] = None
    sr_resistance: Optional[float] = None
    opportunity: Optional[Opportunity] = None
    asset_profile: Optional[AssetProfile] = None


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE ENGINE — completely separate from all Dexter measurement engines
# Never modifies Dexter. Never feeds back into detections.
# Tracks which hypotheses Chev acted on and what the outcomes were.
# ─────────────────────────────────────────────────────────────────────────────

_PERF_LOG = r"C:\ChevTools\performance.jsonl"


def record_trade_hypothesis(trade_id: str, hypothesis_id: str, hypothesis_name: str,
                             asset_class: str, primary_tf: str, dexter_score: float,
                             chev_acted: bool) -> None:
    """Log the hypothesis that drove a trade decision at the moment of posting."""
    entry = {
        "ts":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_id":         trade_id,
        "hypothesis_id":    hypothesis_id,
        "hypothesis_name":  hypothesis_name,
        "asset_class":      asset_class,
        "primary_tf":       primary_tf,
        "dexter_score":     round(float(dexter_score), 1),
        "chev_acted":       chev_acted,
        "outcome":          None,
    }
    try:
        with open(_PERF_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[Performance] Write failed: {e}")


def record_trade_outcome(trade_id: str, outcome: str) -> None:
    """Update outcome ('WIN', 'LOSS', 'SIP') for a trade in the performance log."""
    if not os.path.exists(_PERF_LOG):
        return
    try:
        lines: List[str] = []
        with open(_PERF_LOG, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    if entry.get("trade_id") == trade_id and entry.get("outcome") is None:
                        entry["outcome"] = outcome
                    lines.append(json.dumps(entry))
                except Exception:
                    lines.append(raw)
        with open(_PERF_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[Performance] Outcome update failed: {e}")


def get_hypothesis_stats(hypothesis_name: str = None, asset_class: str = None,
                          primary_tf: str = None) -> List[dict]:
    """Return historical win-rate statistics for hypothesis types."""
    if not os.path.exists(_PERF_LOG):
        return []
    records: List[dict] = []
    try:
        with open(_PERF_LOG, "r", encoding="utf-8") as f:
            for raw in f:
                try:
                    records.append(json.loads(raw.strip()))
                except Exception:
                    pass
    except Exception:
        return []

    if hypothesis_name:
        records = [r for r in records if r.get("hypothesis_name") == hypothesis_name]
    if asset_class:
        records = [r for r in records if r.get("asset_class") == asset_class]
    if primary_tf:
        records = [r for r in records if r.get("primary_tf") == primary_tf]

    groups: Dict[tuple, List[dict]] = {}
    for r in records:
        if r.get("outcome") is None:
            continue
        key = (r.get("hypothesis_name", ""), r.get("asset_class", ""), r.get("primary_tf", ""))
        groups.setdefault(key, []).append(r)

    stats = []
    for (name, ac, tf), grp in groups.items():
        wins   = sum(1 for r in grp if r["outcome"] in ("WIN", "SIP"))
        losses = sum(1 for r in grp if r["outcome"] == "LOSS")
        total  = wins + losses
        stats.append({
            "name": name, "asset_class": ac, "tf": tf,
            "total": total, "wins": wins, "losses": losses,
            "win_rate": round(wins / total, 3) if total > 0 else None,
            "avg_score": round(sum(r.get("dexter_score", 0) for r in grp) / len(grp), 1),
        })
    return sorted(stats, key=lambda x: -(x["win_rate"] or 0))


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — shared ATR and utility functions used by all engines
# ─────────────────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    prev  = df["close"].shift(1)
    tr    = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _ts_to_datetime(ts_val) -> datetime:
    if isinstance(ts_val, datetime):
        return ts_val
    if hasattr(ts_val, "to_pydatetime"):
        return ts_val.to_pydatetime()
    if isinstance(ts_val, (int, float)):
        return datetime.fromtimestamp(ts_val / 1000)
    return datetime.now()


def _swing_id(ts_dt: datetime, kind: str) -> str:
    ts_ms = int(ts_dt.timestamp() * 1000)
    return f"sw_{kind[0]}_{ts_ms}"


def _percentile_rank(value: float, sorted_vals: List[float]) -> int:
    """Return the percentile rank (0–100) of a value within a sorted distribution.
    50 = median, 90 = unusually large for this asset, 10 = unusually small.
    Returns 50 (neutral) when the distribution has fewer than 3 samples."""
    if len(sorted_vals) < 3:
        return 50
    idx = int(np.searchsorted(sorted_vals, value, side="right"))
    return max(0, min(100, round(idx / len(sorted_vals) * 100)))


# ─────────────────────────────────────────────────────────────────────────────
# SWING ENGINE
# Input:  OHLCV DataFrame
# Output: SwingPoint[] + SwingAnalysis[]
# Never:  detect patterns, calculate Fibonacci, calculate VP
# ─────────────────────────────────────────────────────────────────────────────

def run_swing_engine(df: pd.DataFrame, window: int = 3,
                     confirm_window: int = 8) -> Tuple[List[SwingPoint], List[SwingAnalysis]]:
    """
    Find meaningful turning points in OHLCV data.
    Returns (SwingPoint[], SwingAnalysis[]) — always kept separate.
    """
    n = len(df)
    if n < window * 2 + confirm_window + 1:
        return [], []

    atr_series = _atr(df, 14)
    highs = df["high"].values.astype(float)
    lows  = df["low"].values.astype(float)
    vols  = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(n)

    swing_points: List[SwingPoint] = []

    for i in range(window, n - confirm_window):
        atr_val = float(atr_series.iloc[i]) if i < len(atr_series) else 1e-6
        ts_dt   = _ts_to_datetime(df.index[i])

        h_i = highs[i]
        l_i = lows[i]

        # Swing HIGH: bar i is higher than all bars in the before-window AND confirm-window
        is_swing_high = (
            all(h_i >= highs[i - j] for j in range(1, window + 1)) and
            all(h_i >= highs[i + j] for j in range(1, confirm_window + 1))
        )
        # Swing LOW: bar i is lower than all bars in the before-window AND confirm-window
        is_swing_low = (
            all(l_i <= lows[i - j] for j in range(1, window + 1)) and
            all(l_i <= lows[i + j] for j in range(1, confirm_window + 1))
        )

        if is_swing_high:
            swing_points.append(SwingPoint(
                id=_swing_id(ts_dt, "H"), index=i, ts=ts_dt,
                price=float(h_i), kind="HIGH",
                atr_at_swing=atr_val, volume_at_swing=float(vols[i]),
            ))

        if is_swing_low:
            swing_points.append(SwingPoint(
                id=_swing_id(ts_dt, "L"), index=i, ts=ts_dt,
                price=float(l_i), kind="LOW",
                atr_at_swing=atr_val, volume_at_swing=float(vols[i]),
            ))

    swing_points.sort(key=lambda s: s.index)
    swing_points = _deduplicate_swings(swing_points)
    analyses     = _compute_swing_analyses(swing_points, df, atr_series, n, confirm_window)
    return swing_points, analyses


def _deduplicate_swings(swings: List[SwingPoint]) -> List[SwingPoint]:
    """Remove consecutive same-kind swings — keep the more extreme one."""
    if not swings:
        return swings
    out = [swings[0]]
    for sp in swings[1:]:
        last = out[-1]
        if sp.kind == last.kind:
            if sp.kind == "HIGH" and sp.price >= last.price:
                out[-1] = sp
            elif sp.kind == "LOW" and sp.price <= last.price:
                out[-1] = sp
        else:
            out.append(sp)
    return out


def _compute_swing_analyses(swings: List[SwingPoint], df: pd.DataFrame,
                              atr_series: pd.Series, n: int,
                              confirm_window: int) -> List[SwingAnalysis]:
    analyses: List[SwingAnalysis] = []
    highs = df["high"].values.astype(float)
    lows  = df["low"].values.astype(float)

    for sp in swings:
        atr = max(sp.atr_at_swing, 1e-10)

        # Prominence: distance to nearest opposing swing / ATR
        opposing = [s for s in swings if s.kind != sp.kind]
        if opposing:
            nearest = min(opposing, key=lambda o: abs(o.price - sp.price))
            prominence = abs(sp.price - nearest.price) / atr
        else:
            prominence = 0.0

        # Retest count: price returned within 0.5 ATR of this level after formation
        retest_count = 0
        tol = atr * 0.5
        for j in range(sp.index + 1, min(sp.index + 60, n)):
            if abs(highs[j] - sp.price) < tol or abs(lows[j] - sp.price) < tol:
                retest_count += 1

        age_bars   = n - 1 - sp.index
        confirmed  = age_bars >= confirm_window
        structural_weight = min(1.0, min(prominence / 6.0, 1.0) * 0.7 + min(retest_count, 3) * 0.1)

        analyses.append(SwingAnalysis(
            swing_id=sp.id,
            prominence=round(prominence, 3),
            retest_count=retest_count,
            age_bars=age_bars,
            confirmed=confirmed,
            structural_weight=round(structural_weight, 3),
        ))
    return analyses


# ─────────────────────────────────────────────────────────────────────────────
# LEG ENGINE
# Input:  SwingPoint[] + SwingAnalysis[] + OHLCV DataFrame
# Output: Leg[]
# Never:  name patterns, fit trendlines to non-swing data, assign Phase
# ─────────────────────────────────────────────────────────────────────────────

def run_leg_engine(swings: List[SwingPoint], analyses: List[SwingAnalysis],
                   df: pd.DataFrame,
                   profile: Optional[AssetProfile] = None) -> List[Leg]:
    """
    Measure travel between consecutive SwingPoints.
    Every Leg is a directional move. Legs are the bridge between swing structure
    and the auction and hypothesis engines.
    When an AssetProfile is provided, each Leg is annotated with percentile ranks
    so downstream engines reason with context-aware measurements.
    """
    if len(swings) < 2:
        return []

    atr_series = _atr(df, 14)
    avg_vol    = float(df["volume"].mean()) if "volume" in df.columns else 1.0

    # Build analysis lookup by swing id
    ana_map = {a.swing_id: a for a in analyses}

    legs: List[Leg] = []
    for i in range(len(swings) - 1):
        start, end = swings[i], swings[i + 1]

        direction: Literal["UP", "DOWN"] = "UP" if end.price > start.price else "DOWN"
        distance_pct = abs(end.price - start.price) / max(start.price, 1e-10) * 100.0
        atr_val      = float(atr_series.iloc[min(start.index, len(atr_series) - 1)])
        distance_atr = abs(end.price - start.price) / max(atr_val, 1e-10)
        bar_count    = max(1, end.index - start.index)
        speed        = distance_pct / bar_count

        # Volume expansion within leg
        if "volume" in df.columns and start.index < end.index:
            seg_vols    = df["volume"].iloc[start.index: end.index + 1].values.astype(float)
            vol_expansion = float(np.mean(seg_vols)) / max(avg_vol, 1e-10)
        else:
            vol_expansion = 1.0

        # Body-to-range ratio within leg (impulsive legs have large bodies)
        if "open" in df.columns and "close" in df.columns and start.index < end.index:
            seg = df.iloc[start.index: end.index + 1]
            bodies = (seg["close"] - seg["open"]).abs().values.astype(float)
            ranges = (seg["high"]  - seg["low"]).values.astype(float)
            body_ratio = float(np.mean(bodies / np.maximum(ranges, 1e-10)))
        else:
            body_ratio = 0.5

        speed_norm = min(1.0, speed / 0.4)   # 0.4% per bar = full speed score
        energy     = min(1.0, speed_norm * 0.4 + body_ratio * 0.4 +
                         min(1.0, vol_expansion / 1.5) * 0.2)

        if distance_atr >= 2.0 and energy >= 0.55:
            character: Literal["IMPULSIVE", "CORRECTIVE", "NEUTRAL"] = "IMPULSIVE"
        elif distance_atr <= 1.0 or energy <= 0.35:
            character = "CORRECTIVE"
        else:
            character = "NEUTRAL"

        # Confidence: min of both swing analysis confirmations
        start_ana = ana_map.get(start.id)
        end_ana   = ana_map.get(end.id)
        start_conf = 0.9 if (start_ana and start_ana.confirmed) else 0.5
        end_conf   = 0.9 if (end_ana   and end_ana.confirmed)   else 0.5
        confidence = min(start_conf, end_conf)

        # Anchor score: suitability as a VP/Fib anchor for the auction engine.
        # Impulsive legs with strong energy and large distance make the best anchors.
        dist_norm = min(1.0, distance_atr / 8.0)   # 8 ATR = full score
        if character == "IMPULSIVE":
            raw_anchor = (dist_norm * 0.5 + energy * 0.35 + confidence * 0.15) * 100.0
        else:
            raw_anchor = energy * distance_atr * 5.0  # partial credit
        anchor_score_val = round(min(100.0, raw_anchor), 1)

        legs.append(Leg(
            id=f"leg_{start.index}_{end.index}",
            start_swing=start, end_swing=end,
            direction=direction,
            distance_pct=round(distance_pct, 4),
            distance_atr=round(distance_atr, 2),
            bar_count=bar_count,
            character=character,
            energy=round(energy, 3),
            speed=round(speed, 5),
            acceleration=0.0,
            confidence=round(confidence, 3),
            created_because=_leg_reason(character, distance_atr, energy, vol_expansion),
            anchor_score=anchor_score_val,
            is_anchor_candidate=anchor_score_val >= 50.0 and character == "IMPULSIVE",
        ))

    _fill_acceleration(legs)
    _fill_retracements(legs)

    # Annotate each leg with percentile rank vs this asset's historical distribution.
    # This happens inside the engine, not the formatter — downstream engines can use these.
    if profile and profile.n_legs >= 5:
        for leg in legs:
            leg.dist_atr_pct = _percentile_rank(leg.distance_atr, profile.leg_atr_sorted)
            leg.energy_pct   = _percentile_rank(leg.energy,        profile.leg_energy_sorted)

    return legs


def _leg_reason(character: str, distance_atr: float, energy: float, vol_exp: float) -> str:
    parts = [f"{distance_atr:.1f}ATR", f"energy={energy:.2f}"]
    if vol_exp > 1.4:
        parts.append(f"vol={vol_exp:.1f}×avg")
    return f"{character}: " + " + ".join(parts)


def _fill_acceleration(legs: List[Leg]) -> None:
    for i, leg in enumerate(legs):
        same_dir = [l for l in legs[:i] if l.direction == leg.direction]
        if same_dir:
            prior = same_dir[-1].speed
            if prior > 0:
                leg.acceleration = round((leg.speed - prior) / prior, 3)


def _fill_retracements(legs: List[Leg]) -> None:
    for i, leg in enumerate(legs):
        opposite_impulses = [
            l for l in legs[:i]
            if l.direction != leg.direction and l.character == "IMPULSIVE"
        ]
        if opposite_impulses:
            prior = opposite_impulses[-1]
            leg.corrects_leg_id = prior.id
            full_range    = abs(prior.end_swing.price - prior.start_swing.price)
            retrace_range = abs(leg.end_swing.price   - leg.start_swing.price)
            leg.retracement_pct = round(min(1.0, retrace_range / max(full_range, 1e-10)), 3)


# ─────────────────────────────────────────────────────────────────────────────
# ASSET PROFILE ENGINE
# Input:  OHLCV DataFrame (full history — the more the better)
# Output: AssetProfile — sorted distributions for percentile ranking
# Run FIRST. Every downstream engine uses this to answer "how unusual is this
# FOR THIS SPECIFIC ASSET?" rather than applying fixed, asset-agnostic thresholds.
# ChatGPT's advice: "percentiles should be computed inside the engine, not the formatter"
# ─────────────────────────────────────────────────────────────────────────────

def run_asset_profile_engine(df: pd.DataFrame, symbol: str = "",
                              timeframe: str = "") -> AssetProfile:
    """
    Build statistical distributions from full OHLCV history.
    Called once per survey before any other engine runs.
    The profile travels with the MarketSurvey so every engine can annotate its
    own output with percentile context — not the formatter.
    """
    swings, analyses = run_swing_engine(df, window=3, confirm_window=8)
    legs             = run_leg_engine(swings, analyses, df)  # no profile yet — bootstrap pass

    if not legs:
        return AssetProfile(
            symbol=symbol, timeframe=timeframe,
            computed_from_bars=len(df), n_legs=0,
            leg_atr_sorted=[], leg_energy_sorted=[],
            vol_expansion_sorted=[], retracement_sorted=[],
            balance_width_sorted=[], participation_sorted=[],
        )

    leg_atrs     = sorted(l.distance_atr for l in legs)
    leg_energies = sorted(l.energy       for l in legs)
    retracements = sorted(l.retracement_pct for l in legs if l.retracement_pct > 0.01)

    # Volume expansion per leg vs rolling 20-bar baseline before each leg
    avg_vol_all = float(df["volume"].mean()) if "volume" in df.columns else 1.0
    vol_expansions: List[float] = []
    if "volume" in df.columns:
        vols = df["volume"].values.astype(float)
        for leg in legs:
            s, e     = leg.start_swing.index, leg.end_swing.index
            seg_avg  = float(vols[s: e + 1].mean()) if e > s else avg_vol_all
            base_s   = max(0, s - 20)
            baseline = float(vols[base_s: s].mean()) if s > base_s else avg_vol_all
            vol_expansions.append(seg_avg / max(baseline, 1e-10))
    vol_expansion_sorted = sorted(vol_expansions)

    # Balance width distribution: pairs of consecutive corrective legs proxy historical auctions
    atr_arr = _atr(df, 14).values.astype(float)
    balance_widths: List[float] = []
    for i in range(1, len(legs)):
        if (legs[i].character in ("CORRECTIVE", "NEUTRAL") and
                legs[i - 1].character in ("CORRECTIVE", "NEUTRAL")):
            hi = max(legs[i].start_swing.price, legs[i].end_swing.price,
                     legs[i - 1].start_swing.price, legs[i - 1].end_swing.price)
            lo = min(legs[i].start_swing.price, legs[i].end_swing.price,
                     legs[i - 1].start_swing.price, legs[i - 1].end_swing.price)
            atr_at = float(atr_arr[min(legs[i - 1].start_swing.index, len(atr_arr) - 1)])
            if atr_at > 0 and hi > lo:
                balance_widths.append((hi - lo) / atr_at)
    balance_width_sorted = sorted(balance_widths)

    # Participation samples: rolling windows of 6 legs across history
    lookback = 6
    participation_samples: List[float] = []
    for i in range(lookback, len(legs)):
        recent = legs[i - lookback: i]
        participation_samples.append(float(np.mean([l.energy for l in recent])) * 100.0)
    participation_sorted = sorted(participation_samples)

    return AssetProfile(
        symbol=symbol, timeframe=timeframe,
        computed_from_bars=len(df), n_legs=len(legs),
        leg_atr_sorted=leg_atrs,
        leg_energy_sorted=leg_energies,
        vol_expansion_sorted=vol_expansion_sorted,
        retracement_sorted=retracements,
        balance_width_sorted=balance_width_sorted,
        participation_sorted=participation_sorted,
    )


# ─────────────────────────────────────────────────────────────────────────────
# STATE ENGINE
# Input:  Leg[] + OHLCV DataFrame
# Output: MarketState (participation 0-100, direction -100 to +100)
# Never:  fit trendlines, detect patterns, assign Phase (Phase goes to Auction)
# ─────────────────────────────────────────────────────────────────────────────

def run_state_engine(legs: List[Leg], df: pd.DataFrame, lookback: int = 6,
                     profile: Optional[AssetProfile] = None) -> MarketState:
    """
    Characterise the market on two continuous axes: participation and direction.
    No categorical labels — these are measurements, not verdicts.
    When an AssetProfile is provided, participation_pct is set so downstream
    engines know whether this participation level is unusual for this asset.
    """
    if not legs:
        return MarketState(
            participation=0.0, direction=0.0, phase="UNKNOWN",
            atr_trend="FLAT", volume_trend="FLAT", leg_sequence="", confidence=0.5,
        )

    recent = legs[-lookback:]

    # ── ATR series (computed early — needed for breakdown) ────────────────────
    atr_s = _atr(df, 14)

    # ── Participation (0-100): 4-component breakdown ──────────────────────────
    leg_component = round(float(np.mean([l.energy for l in recent])) * 100.0, 1)

    atr_component = 50.0
    if len(atr_s) >= 20:
        rec_a = float(atr_s.iloc[-5:].mean())
        ear_a = float(atr_s.iloc[-20:-5].mean())
        atr_component = round(min(100.0, max(0.0, (rec_a / max(ear_a, 1e-10) - 0.5) * 100.0)), 1)

    vol_component = 50.0
    if "volume" in df.columns and len(df) >= 20:
        rv = float(df["volume"].iloc[-5:].mean())
        ev = float(df["volume"].iloc[-20:-5].mean())
        vol_component = round(min(100.0, max(0.0, (rv / max(ev, 1e-10) - 0.5) * 100.0)), 1)

    atr_last = float(atr_s.iloc[-1]) if len(atr_s) > 0 else 1e-6
    range_component = 50.0
    if len(df) >= 5 and atr_last > 0:
        avg_range = float((df["high"].iloc[-5:] - df["low"].iloc[-5:]).mean())
        range_component = round(min(100.0, max(0.0, avg_range / atr_last * 50.0)), 1)

    participation = round(
        leg_component * 0.40 + atr_component * 0.25 + vol_component * 0.20 + range_component * 0.15,
        1
    )
    participation_breakdown = {
        "leg_component":    leg_component,
        "atr_component":    atr_component,
        "volume_component": vol_component,
        "range_component":  range_component,
    }

    # ── Direction (-100 to +100): weighted directional bias ──────────────────
    w_sum, w_total = 0.0, 0.0
    for leg in recent:
        sign = +1.0 if leg.direction == "UP" else -1.0
        w    = leg.distance_atr * leg.energy
        w_sum   += sign * w
        w_total += w
    direction = round(max(-100.0, min(100.0, w_sum / max(w_total, 1e-10) * 100.0)), 1)

    # ── ATR trend ─────────────────────────────────────────────────────────────
    if len(atr_s) >= 20:
        recent_atr  = float(atr_s.iloc[-5:].mean())
        earlier_atr = float(atr_s.iloc[-20:-5].mean())
        atr_trend: Literal["EXPANDING", "CONTRACTING", "FLAT"] = (
            "EXPANDING"   if recent_atr > earlier_atr * 1.15 else
            "CONTRACTING" if recent_atr < earlier_atr * 0.85 else
            "FLAT"
        )
    else:
        atr_trend = "FLAT"

    # ── Volume trend ──────────────────────────────────────────────────────────
    if "volume" in df.columns and len(df) >= 20:
        rv  = float(df["volume"].iloc[-5:].mean())
        ev  = float(df["volume"].iloc[-20:-5].mean())
        volume_trend: Literal["RISING", "FALLING", "FLAT"] = (
            "RISING"  if rv > ev * 1.2 else
            "FALLING" if rv < ev * 0.8 else
            "FLAT"
        )
    else:
        volume_trend = "FLAT"

    # ── Phase ─────────────────────────────────────────────────────────────────
    if atr_trend == "CONTRACTING" and participation < 50:
        phase: Literal["EXPANSION", "COMPRESSION", "BREAKOUT", "UNKNOWN"] = "COMPRESSION"
    elif atr_trend == "EXPANDING" and participation > 65:
        older = legs[:-lookback] if len(legs) > lookback else []
        was_compressed = any(l.energy < 0.4 for l in older[-3:]) if older else False
        phase = "BREAKOUT" if was_compressed else "EXPANSION"
    elif atr_trend == "EXPANDING":
        phase = "EXPANSION"
    else:
        phase = "UNKNOWN"

    # ── Leg sequence string ────────────────────────────────────────────────────
    def _lbl(l: Leg) -> str:
        c = "IMP" if l.character == "IMPULSIVE" else ("COR" if l.character == "CORRECTIVE" else "NEU")
        return f"{c}_{l.direction}"

    leg_sequence = " ->".join(_lbl(l) for l in recent[-5:])

    confidence = round(min(l.confidence for l in recent), 3) if recent else 0.5

    participation_pct = (
        _percentile_rank(participation, profile.participation_sorted)
        if profile and profile.participation_sorted else 50
    )

    return MarketState(
        participation=participation, direction=direction, phase=phase,
        atr_trend=atr_trend, volume_trend=volume_trend,
        leg_sequence=leg_sequence, confidence=confidence,
        participation_breakdown=participation_breakdown,
        participation_pct=participation_pct,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY ENGINE
# Input:  SwingPoint[] (primary TF only)
# Output: GeometryReport (slopes, R², touches, compression, parallelism, confidence)
# Never:  name patterns, access market state, access auction
# ─────────────────────────────────────────────────────────────────────────────

def run_geometry_engine(swings: List[SwingPoint], df: pd.DataFrame,
                         lookback: int = 50,
                         profile: Optional[AssetProfile] = None) -> GeometryReport:
    """
    Measure the mathematical shape that price is making.
    Does not name patterns — only measures geometry and propagates confidence.
    When an AssetProfile is provided, compression_pct is set so Chev knows
    whether the current compression is genuinely tight for this asset.
    """
    if len(swings) < 4:
        return _empty_geometry()

    atr_s = _atr(df, 14)
    atr   = float(atr_s.iloc[-1]) if len(atr_s) > 0 else 1e-6
    n     = len(df)

    # Use swings within the lookback window, fall back to last 10 swings
    recent = [s for s in swings if s.index >= n - lookback]
    if len(recent) < 4:
        recent = swings[-10:]
    if len(recent) < 4:
        return _empty_geometry()

    highs = [s for s in recent if s.kind == "HIGH"]
    lows  = [s for s in recent if s.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return _empty_geometry()

    upper = _fit_trendline(highs, atr)
    lower = _fit_trendline(lows,  atr)

    # Compression: ratio of current gap to initial gap
    start_bar = min(s.index for s in recent)
    upper_l   = _price_at(upper, start_bar)
    upper_r   = _price_at(upper, n - 1)
    lower_l   = _price_at(lower, start_bar)
    lower_r   = _price_at(lower, n - 1)
    gap_l     = max(1e-10, upper_l - lower_l)
    gap_r     = max(1e-10, upper_r - lower_r)
    compression = max(0.0, min(1.0, 1.0 - gap_r / gap_l))

    # Parallelism: how similar are the two slopes?
    sum_abs  = abs(upper.slope_norm) + abs(lower.slope_norm)
    slope_diff = abs(upper.slope_norm - lower.slope_norm)
    parallelism = max(0.0, min(1.0, 1.0 - slope_diff / max(sum_abs, 1e-10)))

    FLAT = 0.05  # slope within ±0.05 norm units = "flat"
    is_converging = upper.slope_norm < -FLAT and lower.slope_norm > FLAT
    is_diverging  = upper.slope_norm >  FLAT and lower.slope_norm < -FLAT
    is_parallel   = parallelism > 0.85 and not is_converging and not is_diverging

    # Structure axis — pure geometry descriptor, no pattern name
    upper_up = upper.slope_norm >  FLAT
    upper_dn = upper.slope_norm < -FLAT
    lower_up = lower.slope_norm >  FLAT
    lower_dn = lower.slope_norm < -FLAT
    one_flat_one_sloped = (upper_up or upper_dn) != (lower_up or lower_dn)

    if is_converging:
        structure_axis: Literal["ASCENDING", "DESCENDING", "HORIZONTAL", "CONTRACTING", "EXPANDING", "ASYMMETRIC"] = "CONTRACTING"
    elif is_diverging:
        structure_axis = "EXPANDING"
    elif is_parallel and upper_up and lower_up:
        structure_axis = "ASCENDING"
    elif is_parallel and upper_dn and lower_dn:
        structure_axis = "DESCENDING"
    elif one_flat_one_sloped:
        structure_axis = "ASYMMETRIC"
    else:
        structure_axis = "HORIZONTAL"

    # Breakout (last close vs trendlines)
    current_price = float(df["close"].iloc[-1])
    upper_now     = _price_at(upper, n - 1)
    lower_now     = _price_at(lower, n - 1)
    breakout_up   = current_price > upper_now * 1.002
    breakout_dn   = current_price < lower_now * 0.998

    avg_vol  = float(df["volume"].mean()) if "volume" in df.columns else 1.0
    last_vol = float(df["volume"].iloc[-1]) if "volume" in df.columns else avg_vol

    # Prior impulse (the move before this pattern — needed for flags/pennants)
    has_impulse, impulse_atr, impulse_dir = _prior_impulse(swings, recent, atr)

    measurement_quality = min(upper.r_squared, lower.r_squared)
    all_confs = [1.0 if s.atr_at_swing > 0 else 0.5 for s in recent]
    confidence = min(min(all_confs), measurement_quality)

    # compression is a 0-1 ratio — no separate distribution yet; keep at neutral 50
    compression_pct = 50

    return GeometryReport(
        upper=upper, lower=lower,
        compression=round(compression, 3),
        parallelism=round(parallelism, 3),
        is_converging=is_converging,
        is_diverging=is_diverging,
        is_parallel=is_parallel,
        breakout_up=breakout_up,
        breakout_dn=breakout_dn,
        breakout_bar=(n - 1) if (breakout_up or breakout_dn) else None,
        vol_at_breakout=round(last_vol, 4) if (breakout_up or breakout_dn) else 0.0,
        avg_vol=round(avg_vol, 4),
        has_impulse=has_impulse,
        impulse_atr=round(impulse_atr, 2),
        impulse_direction=impulse_dir,
        measurement_quality=round(measurement_quality, 3),
        sample_size=len(recent),
        confidence=round(confidence, 3),
        structure_axis=structure_axis,
        compression_pct=compression_pct,
    )


def _fit_trendline(swings: List[SwingPoint], atr: float) -> TrendlineFit:
    if len(swings) < 2:
        avg_price = swings[0].price if swings else 0.0
        return TrendlineFit(slope_norm=0.0, slope_raw=0.0, intercept=avg_price,
                            r_squared=0.0, touch_count=len(swings))

    xs = np.array([s.index for s in swings], dtype=float)
    ys = np.array([s.price for s in swings], dtype=float)
    x_mean, y_mean = xs.mean(), ys.mean()
    ss_xy = float(np.sum((xs - x_mean) * (ys - y_mean)))
    ss_xx = float(np.sum((xs - x_mean) ** 2))

    if ss_xx < 1e-10:
        return TrendlineFit(slope_norm=0.0, slope_raw=0.0, intercept=float(y_mean),
                            r_squared=1.0, touch_count=len(swings))

    slope_raw = ss_xy / ss_xx
    intercept = y_mean - slope_raw * x_mean

    y_pred = slope_raw * xs + intercept
    ss_res = float(np.sum((ys - y_pred) ** 2))
    ss_tot = float(np.sum((ys - y_mean) ** 2))
    r2 = max(0.0, 1.0 - ss_res / max(ss_tot, 1e-10))

    return TrendlineFit(
        slope_norm=round(slope_raw / max(atr, 1e-10), 6),
        slope_raw=slope_raw,
        intercept=round(intercept, 8),
        r_squared=round(r2, 3),
        touch_count=len(swings),
    )


def _price_at(fit: TrendlineFit, bar_index: int) -> float:
    """Evaluate a trendline at a given bar index using the raw slope."""
    return fit.intercept + fit.slope_raw * bar_index


def _prior_impulse(all_swings: List[SwingPoint], pattern_swings: List[SwingPoint],
                   atr: float) -> Tuple[bool, float, Optional[Literal["UP", "DOWN"]]]:
    """Detect a strong directional move before the current pattern window."""
    if not pattern_swings or len(all_swings) < 4:
        return False, 0.0, None
    first_bar = min(s.index for s in pattern_swings)
    pre = [s for s in all_swings if s.index < first_bar - 3]
    if len(pre) < 2:
        return False, 0.0, None
    a, b = pre[-2], pre[-1]
    size = abs(b.price - a.price) / max(atr, 1e-10)
    direction: Optional[Literal["UP", "DOWN"]] = "UP" if b.price > a.price else "DOWN"
    if size >= 4.0:
        return True, round(size, 2), direction
    return False, round(size, 2), None


def _empty_geometry() -> GeometryReport:
    empty = TrendlineFit(slope_norm=0.0, slope_raw=0.0, intercept=0.0, r_squared=0.0, touch_count=0)
    return GeometryReport(
        upper=empty, lower=empty,
        compression=0.0, parallelism=0.0,
        is_converging=False, is_diverging=False, is_parallel=False,
        breakout_up=False, breakout_dn=False,
        measurement_quality=0.0, sample_size=0, confidence=0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AUCTION ENGINE
# Input:  Leg[] + MarketState + OHLCV DataFrame
# Output: ActiveAuction (anchor, age, balance area, acceptance, VP, Fib anchors, state)
# Never:  determine trading direction, fit trendlines, name patterns
# ─────────────────────────────────────────────────────────────────────────────

def run_auction_engine(legs: List[Leg], df: pd.DataFrame, state: MarketState) -> Optional[ActiveAuction]:
    """
    Identify the active negotiation zone and track its lifecycle.
    The auction is where buyers and sellers are actively negotiating price.
    """
    if len(legs) < 2:
        return None

    atr_s = _atr(df, 14)
    atr   = float(atr_s.iloc[-1]) if len(atr_s) > 0 else 1e-6
    n     = len(df)
    current_price = float(df["close"].iloc[-1])

    # Find the balance zone: sequence of corrective/low-energy legs at the end
    balance_legs = _find_balance_legs(legs)
    if balance_legs:
        bal_high     = max(max(l.start_swing.price, l.end_swing.price) for l in balance_legs)
        bal_low      = min(min(l.start_swing.price, l.end_swing.price) for l in balance_legs)
        anchor_bar   = balance_legs[0].start_swing.index
        n_bal        = len(balance_legs)
        avg_e        = np.mean([l.energy for l in balance_legs])
        created_why  = f"Balance ({n_bal} corrective legs, avg_energy={avg_e:.2f})"
    else:
        # No balance detected: use last leg's range
        last         = legs[-1]
        bal_high     = max(last.start_swing.price, last.end_swing.price)
        bal_low      = min(last.start_swing.price, last.end_swing.price)
        anchor_bar   = last.start_swing.index
        created_why  = "No balance — using last leg range"

    anchor_price      = (bal_high + bal_low) / 2.0
    age_bars          = max(0, n - 1 - anchor_bar)
    balance_width_atr = (bal_high - bal_low) / max(atr, 1e-10)

    # Auction lifecycle state
    price_left_balance = current_price > bal_high * 1.005 or current_price < bal_low * 0.995
    if state.phase == "BREAKOUT":
        auction_state: Literal["ACTIVE", "BALANCING", "INACTIVE", "FAILED"] = "ACTIVE"
    elif price_left_balance:
        auction_state = "INACTIVE"
    elif balance_width_atr < 0.8:
        auction_state = "BALANCING"
    else:
        auction_state = "ACTIVE"

    # Acceptance zones (3 equal-size zones within balance area)
    accepted_zones = _acceptance_zones(df, anchor_bar, n, bal_high, bal_low)

    # Rejection levels (spike + close-back-in = rejection)
    rejected_above = _detect_rejection_above(df, bal_high)
    rejected_below = _detect_rejection_below(df, bal_low)

    # Volume profile within auction
    poc, vah, val = _vp_for_range(df, anchor_bar, n)

    # Fibonacci anchors from the impulse that preceded the balance
    fib_high, fib_low, anchor_leg_id = _impulse_anchors(legs, balance_legs)

    energy          = max(0.1, 1.0 - age_bars / 200.0)
    dist_from_mid   = abs(current_price - anchor_price)
    imbalance_score = min(1.0, dist_from_mid / max(bal_high - bal_low, atr))

    all_confs  = [l.confidence for l in (balance_legs or legs[-3:])]
    confidence = round(min(all_confs), 3) if all_confs else 0.5

    # Maturity: how old is this auction vs typical duration (30 bars = "full cycle")
    maturity = round(min(1.0, age_bars / 30.0), 3)

    # Balance score: quality of the balance area (tight + clear POC + time spent in value)
    tightness    = max(0.0, 1.0 - balance_width_atr / 5.0)
    poc_clarity  = 1.0 if poc else 0.3
    time_in_val  = float(np.mean([z.time_spent_pct for z in accepted_zones])) if accepted_zones else 0.3
    balance_score = round(tightness * 0.4 + poc_clarity * 0.3 + time_in_val * 0.3, 3)

    # Defended levels: rejection levels that stayed inside balance (tested + held)
    defended: List[float] = []
    if rejected_above and rejected_above < bal_high:
        defended.append(round(rejected_above, 8))
    if rejected_below and rejected_below > bal_low:
        defended.append(round(rejected_below, 8))

    # Assign auction roles to all legs (mutates Leg.auction_role in place)
    if balance_legs:
        balance_ids   = {l.id for l in balance_legs}
        first_bal_idx = balance_legs[0].start_swing.index
        for leg in legs:
            if leg.id in balance_ids:
                leg.auction_role = "BALANCE_LEG"
            elif leg.id == anchor_leg_id:
                leg.auction_role = "PRIMARY_IMPULSE"
            elif leg.end_swing.index <= first_bal_idx:
                leg.auction_role = "PRIMARY_IMPULSE" if leg.character == "IMPULSIVE" else "CORRECTIVE"
            else:
                leg.auction_role = "POST_BREAKOUT"

    try:
        ts_ms = int(_ts_to_datetime(df.index[anchor_bar]).timestamp() * 1000)
    except Exception:
        ts_ms = anchor_bar
    auc_id = f"auc_{ts_ms}"

    return ActiveAuction(
        id=auc_id,
        anchor_price=round(anchor_price, 8),
        anchor_bar=anchor_bar,
        age_bars=age_bars,
        state=auction_state,
        balance_high=round(bal_high, 8),
        balance_low=round(bal_low, 8),
        balance_width_atr=round(balance_width_atr, 2),
        accepted_zones=accepted_zones,
        rejected_above=round(rejected_above, 8) if rejected_above else None,
        rejected_below=round(rejected_below, 8) if rejected_below else None,
        poc=round(poc, 8) if poc else None,
        vah=round(vah, 8) if vah else None,
        val=round(val, 8) if val else None,
        fib_anchor_high=round(fib_high, 8) if fib_high else None,
        fib_anchor_low=round(fib_low, 8)  if fib_low  else None,
        energy=round(energy, 3),
        imbalance_score=round(imbalance_score, 3),
        created_because=created_why,
        confidence=confidence,
        maturity=maturity,
        balance_score=balance_score,
        defended_levels=defended,
        anchor_leg_id=anchor_leg_id,
    )


def _find_balance_legs(legs: List[Leg], lookback: int = 8) -> List[Leg]:
    """Find consecutive corrective/low-energy legs forming the balance zone."""
    recent = legs[-lookback:]
    out: List[Leg] = []
    for leg in reversed(recent):
        if leg.character in ("CORRECTIVE", "NEUTRAL") or leg.energy < 0.5:
            out.append(leg)
        else:
            break
    out.reverse()
    return out if len(out) >= 2 else []


def _acceptance_zones(df: pd.DataFrame, start_bar: int, end_bar: int,
                       bal_high: float, bal_low: float) -> List[AcceptanceZone]:
    seg = df.iloc[max(0, start_bar): end_bar]
    if len(seg) < 3:
        return []
    z_size  = (bal_high - bal_low) / 3.0
    total_b = len(seg)
    zones   = []
    for z in range(3):
        zlo = bal_low + z * z_size
        zhi = bal_low + (z + 1) * z_size
        in_z, visits, was_in = 0, 0, False
        for _, row in seg.iterrows():
            mid = (float(row["high"]) + float(row["low"])) / 2.0
            cur = zlo <= mid <= zhi
            if cur:
                in_z += 1
            if cur and not was_in:
                visits += 1
            was_in = cur
        t_pct    = in_z / max(total_b, 1)
        rejected = visits > 0 and t_pct < 0.04
        zones.append(AcceptanceZone(
            price_low=round(zlo, 8), price_high=round(zhi, 8),
            time_spent_pct=round(t_pct, 3), visit_count=visits, rejected=rejected,
        ))
    return zones


def _detect_rejection_above(df: pd.DataFrame, bal_high: float,
                              lookback: int = 20) -> Optional[float]:
    recent = df.iloc[-lookback:]
    spikes = [(float(row["high"]), float(row["close"]))
              for _, row in recent.iterrows()
              if float(row["high"]) > bal_high * 1.004]
    for spike_h, close_p in spikes:
        if close_p < bal_high:
            return spike_h
    return None


def _detect_rejection_below(df: pd.DataFrame, bal_low: float,
                              lookback: int = 20) -> Optional[float]:
    recent = df.iloc[-lookback:]
    spikes = [(float(row["low"]), float(row["close"]))
              for _, row in recent.iterrows()
              if float(row["low"]) < bal_low * 0.996]
    for spike_l, close_p in spikes:
        if close_p > bal_low:
            return spike_l
    return None


def _vp_for_range(df: pd.DataFrame, start_bar: int,
                   end_bar: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    seg = df.iloc[max(0, start_bar): end_bar]
    if len(seg) < 5 or "volume" not in seg.columns:
        return None, None, None
    seg_h = float(seg["high"].max())
    seg_l = float(seg["low"].min())
    if seg_h <= seg_l:
        return None, None, None
    N_BINS   = 24
    bin_size = (seg_h - seg_l) / N_BINS
    bins     = np.zeros(N_BINS)
    for _, row in seg.iterrows():
        bh = float(row["high"]); bl = float(row["low"]); vol = float(row.get("volume", 0))
        for b in range(N_BINS):
            blo = seg_l + b * bin_size; bhi = blo + bin_size
            if bh >= blo and bl <= bhi:
                spread = max(1, int((bh - bl) / bin_size + 1))
                bins[b] += vol / spread
    poc_b = int(np.argmax(bins))
    poc   = seg_l + (poc_b + 0.5) * bin_size
    # Value area (70% of total volume)
    total   = bins.sum(); target = total * 0.70
    val_b   = poc_b; vah_b = poc_b
    accum   = bins[poc_b]; lo, hi = poc_b - 1, poc_b + 1
    while accum < target:
        lo_v = bins[lo] if lo >= 0 else -1.0
        hi_v = bins[hi] if hi < N_BINS else -1.0
        if lo_v <= 0 and hi_v <= 0:
            break
        if lo_v >= hi_v:
            accum += lo_v; val_b = lo; lo -= 1
        else:
            accum += hi_v; vah_b = hi; hi += 1
    return (round(poc, 8),
            round(seg_l + (vah_b + 1) * bin_size, 8),
            round(seg_l + val_b * bin_size, 8))


def _impulse_anchors(legs: List[Leg], balance_legs: List[Leg]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not balance_legs:
        return None, None, None
    first_bal_bar = balance_legs[0].start_swing.index
    pre_impulses  = [l for l in legs if l.end_swing.index <= first_bal_bar
                     and l.character == "IMPULSIVE" and l not in balance_legs]
    if not pre_impulses:
        return None, None, None
    imp = pre_impulses[-1]
    return (max(imp.start_swing.price, imp.end_swing.price),
            min(imp.start_swing.price, imp.end_swing.price),
            imp.id)


# ─────────────────────────────────────────────────────────────────────────────
# HYPOTHESIS ENGINE
# Input:  GeometryReport + ActiveAuction + MarketState + Leg[] + current_price
# Output: Hypothesis[] ranked by confidence, highest first
# Never:  produce a single "the" pattern — always competing ranked hypotheses
# ─────────────────────────────────────────────────────────────────────────────

# Pattern definitions: required conditions, optional boosters, forbidden disqualifiers
# Key: the forbidden list kills a hypothesis instantly — no exceptions.
_PATTERN_DEFS: Dict[str, dict] = {
    "ASCENDING_TRIANGLE": {
        "bias": "LONG",
        "required":  ["flat_upper", "rising_lower", "is_converging"],
        "optional":  ["breakout_up", "vol_spike_on_breakout", "has_impulse_up"],
        "forbidden": ["is_diverging", "breakout_dn", "falling_lower"],
    },
    "DESCENDING_TRIANGLE": {
        "bias": "SHORT",
        "required":  ["flat_lower", "falling_upper", "is_converging"],
        "optional":  ["breakout_dn", "vol_spike_on_breakout", "has_impulse_down"],
        "forbidden": ["is_diverging", "breakout_up", "rising_upper"],
    },
    "SYMMETRICAL_TRIANGLE": {
        "bias": "NEUTRAL",
        "required":  ["is_converging", "falling_upper", "rising_lower"],
        "optional":  ["breakout_up", "breakout_dn", "vol_spike_on_breakout"],
        "forbidden": ["is_diverging", "is_parallel", "flat_upper", "flat_lower"],
    },
    "RISING_WEDGE": {
        "bias": "SHORT",
        "required":  ["is_converging", "rising_upper", "rising_lower", "lower_rising_faster"],
        "optional":  ["breakout_dn", "has_impulse_up", "high_compression"],
        "forbidden": ["flat_upper", "falling_upper", "is_parallel"],
    },
    "FALLING_WEDGE": {
        "bias": "LONG",
        "required":  ["is_converging", "falling_upper", "falling_lower", "upper_falling_faster"],
        "optional":  ["breakout_up", "has_impulse_down", "high_compression"],
        "forbidden": ["flat_lower", "rising_lower", "is_parallel"],
    },
    "BULL_CHANNEL": {
        "bias": "LONG",
        "required":  ["is_parallel", "rising_upper", "rising_lower"],
        "optional":  ["pullback_to_lower", "vol_expanding"],
        "forbidden": ["is_converging", "flat_upper", "flat_lower", "falling_upper"],
    },
    "BEAR_CHANNEL": {
        "bias": "SHORT",
        "required":  ["is_parallel", "falling_upper", "falling_lower"],
        "optional":  ["pullback_to_upper", "vol_expanding"],
        "forbidden": ["is_converging", "flat_upper", "flat_lower", "rising_lower"],
    },
    "RECTANGLE": {
        "bias": "NEUTRAL",
        "required":  ["flat_upper", "flat_lower", "is_parallel"],
        "optional":  ["breakout_up", "breakout_dn", "vol_spike_on_breakout"],
        "forbidden": ["is_converging", "rising_upper", "falling_lower"],
    },
    "BULL_FLAG": {
        "bias": "LONG",
        "required":  ["has_impulse_up", "is_parallel", "falling_upper", "falling_lower"],
        "optional":  ["breakout_up", "vol_declining"],
        "forbidden": ["is_converging", "rising_lower", "has_impulse_down"],
    },
    "BEAR_FLAG": {
        "bias": "SHORT",
        "required":  ["has_impulse_down", "is_parallel", "rising_upper", "rising_lower"],
        "optional":  ["breakout_dn", "vol_declining"],
        "forbidden": ["is_converging", "falling_upper", "has_impulse_up"],
    },
    "BULL_PENNANT": {
        "bias": "LONG",
        "required":  ["has_impulse_up", "is_converging"],
        "optional":  ["breakout_up", "vol_declining", "vol_spike_on_breakout"],
        "forbidden": ["is_parallel", "has_impulse_down"],
    },
    "BEAR_PENNANT": {
        "bias": "SHORT",
        "required":  ["has_impulse_down", "is_converging"],
        "optional":  ["breakout_dn", "vol_declining", "vol_spike_on_breakout"],
        "forbidden": ["is_parallel", "has_impulse_up"],
    },
    "COMPRESSION": {
        "bias": "NEUTRAL",
        "required":  ["is_converging", "high_compression"],
        "optional":  ["breakout_up", "breakout_dn", "vol_declining", "high_compression"],
        "forbidden": ["is_parallel"],
    },
}

_EXPECTED_EVENTS: Dict[str, Tuple[str, str]] = {
    # (expected_next_event, expected_entry_trigger)
    "ASCENDING_TRIANGLE":  (
        "Retest of flat resistance — volume should decline into test",
        "Impulsive close above flat resistance with volume >=1.5x avg"
    ),
    "DESCENDING_TRIANGLE": (
        "Retest of flat support — volume should decline into test",
        "Impulsive close below flat support with volume >=1.5x avg"
    ),
    "SYMMETRICAL_TRIANGLE": (
        "Apex approach — indecision builds; one side will give way",
        "Decisive close through either trendline on expanding volume"
    ),
    "RISING_WEDGE": (
        "Continued compression; possible final thrust high (throw-over)",
        "Close below lower wedge boundary after declining momentum and bearish candle"
    ),
    "FALLING_WEDGE": (
        "Continued compression; possible final thrust low (spring)",
        "Close above upper wedge boundary with momentum shift and bullish candle"
    ),
    "BULL_CHANNEL": (
        "Pullback to lower channel boundary (buying opportunity zone)",
        "Bullish reversal candle at lower channel line with rising volume"
    ),
    "BEAR_CHANNEL": (
        "Pullback to upper channel boundary (shorting opportunity zone)",
        "Bearish reversal candle at upper channel line with rising volume"
    ),
    "RECTANGLE": (
        "Test of either boundary — direction of breakout is the trade",
        "Close outside balance zone on volume >=1.3x avg with no immediate reversal"
    ),
    "BULL_FLAG": (
        "Completion of tight corrective channel (price reaches lower flag boundary)",
        "Break above flag's falling upper trendline on expanding volume"
    ),
    "BEAR_FLAG": (
        "Completion of corrective bounce (price reaches upper flag boundary)",
        "Break below flag's rising lower trendline on expanding volume"
    ),
    "BULL_PENNANT": (
        "Apex of converging corrective structure after bullish impulse",
        "Impulsive close above upper pennant line — extension of prior mast expected"
    ),
    "BEAR_PENNANT": (
        "Apex of converging corrective structure after bearish impulse",
        "Impulsive close below lower pennant line — extension of prior mast expected"
    ),
    "COMPRESSION": (
        "Apex approach — maximum indecision before directional resolution",
        "First impulsive close outside compression zone — direction defines bias"
    ),
}

_CONDITION_DESCRIPTIONS: Dict[str, str] = {
    "flat_upper":           "Upper trendline flat (horizontal resistance)",
    "flat_lower":           "Lower trendline flat (horizontal support)",
    "rising_upper":         "Upper trendline rising",
    "rising_lower":         "Lower trendline rising",
    "falling_upper":        "Upper trendline falling",
    "falling_lower":        "Lower trendline falling",
    "is_converging":        "Trendlines converging (compression building)",
    "is_diverging":         "Trendlines diverging (expansion)",
    "is_parallel":          "Trendlines parallel (channel structure)",
    "breakout_up":          "Price broke above upper trendline",
    "breakout_dn":          "Price broke below lower trendline",
    "lower_rising_faster":  "Lower trendline rising faster than upper (squeeze)",
    "upper_falling_faster": "Upper trendline falling faster than lower (squeeze)",
    "has_impulse_up":       "Prior bullish impulse ≥4 ATR (flag/pennant mast)",
    "has_impulse_down":     "Prior bearish impulse ≥4 ATR (flag/pennant mast)",
    "vol_spike_on_breakout":"Volume spike confirmed breakout",
    "vol_declining":        "Volume declining (consolidation quality)",
    "vol_expanding":        "Volume expanding (trend continuation)",
    "high_compression":     "High compression (>65% converged)",
    "pullback_to_lower":    "Price at lower channel line (pullback entry)",
    "pullback_to_upper":    "Price at upper channel line (pullback entry)",
}


def run_hypothesis_engine(geometry: GeometryReport, state: MarketState,
                           auction: Optional[ActiveAuction], legs: List[Leg],
                           df: pd.DataFrame, current_price: float) -> List[Hypothesis]:
    """
    Explain the geometry as a ranked list of competing hypotheses.
    Uses forbidden ->required ->optional scoring with weakest-link confidence propagation.
    """
    if geometry.sample_size < 4 or geometry.confidence < 0.15:
        return []

    features = _geometry_features(geometry, state, legs, df, current_price)
    hypotheses: List[Hypothesis] = []

    for name, pdef in _PATTERN_DEFS.items():
        hyp = _score_hypothesis(
            name=name,
            bias=pdef["bias"],
            required=pdef["required"],
            optional=pdef["optional"],
            forbidden=pdef["forbidden"],
            features=features,
            geometry=geometry,
            state=state,
            auction=auction,
            legs=legs,
            df=df,
            current_price=current_price,
        )
        if hyp is not None and hyp.confidence >= 0.30:
            hypotheses.append(hyp)

    hypotheses.sort(key=lambda h: -h.confidence)
    return hypotheses


def _geometry_features(geometry: GeometryReport, state: MarketState,
                         legs: List[Leg], df: pd.DataFrame,
                         current_price: float) -> Dict[str, bool]:
    u, l = geometry.upper, geometry.lower
    FLAT = 0.05

    upper_rising  = u.slope_norm >  FLAT
    upper_falling = u.slope_norm < -FLAT
    upper_flat    = not upper_rising and not upper_falling
    lower_rising  = l.slope_norm >  FLAT
    lower_falling = l.slope_norm < -FLAT
    lower_flat    = not lower_rising and not lower_falling

    lower_rising_faster  = lower_rising  and upper_rising  and l.slope_norm  >  u.slope_norm
    upper_falling_faster = upper_falling and lower_falling and abs(u.slope_norm) > abs(l.slope_norm)

    n         = len(df)
    upper_now = _price_at(u, n - 1)
    lower_now = _price_at(l, n - 1)
    range_now = max(abs(upper_now - lower_now), 1e-10)

    vol_spike = False
    if geometry.breakout_up or geometry.breakout_dn:
        vol_spike = (geometry.vol_at_breakout / max(geometry.avg_vol, 1e-10)) > 1.5

    pullback_lower = abs(current_price - lower_now) / range_now < 0.2
    pullback_upper = abs(current_price - upper_now) / range_now < 0.2

    return {
        "flat_upper":           upper_flat,
        "flat_lower":           lower_flat,
        "rising_upper":         upper_rising,
        "rising_lower":         lower_rising,
        "falling_upper":        upper_falling,
        "falling_lower":        lower_falling,
        "is_converging":        geometry.is_converging,
        "is_diverging":         geometry.is_diverging,
        "is_parallel":          geometry.is_parallel,
        "breakout_up":          geometry.breakout_up,
        "breakout_dn":          geometry.breakout_dn,
        "lower_rising_faster":  lower_rising_faster,
        "upper_falling_faster": upper_falling_faster,
        "has_impulse_up":       geometry.has_impulse and geometry.impulse_direction == "UP",
        "has_impulse_down":     geometry.has_impulse and geometry.impulse_direction == "DOWN",
        "vol_spike_on_breakout": vol_spike,
        "vol_declining":        state.volume_trend == "FALLING",
        "vol_expanding":        state.volume_trend == "RISING",
        "high_compression":     geometry.compression > 0.65,
        "pullback_to_lower":    pullback_lower,
        "pullback_to_upper":    pullback_upper,
    }


def _score_hypothesis(name: str, bias: str,
                       required: List[str], optional: List[str], forbidden: List[str],
                       features: Dict[str, bool],
                       geometry: GeometryReport, state: MarketState,
                       auction: Optional[ActiveAuction], legs: List[Leg],
                       df: pd.DataFrame, current_price: float) -> Optional[Hypothesis]:

    # Forbidden check — instant disqualification (no exceptions)
    if any(features.get(f, False) for f in forbidden):
        return None

    met_req  = [r for r in required if features.get(r, False)]
    miss_req = [r for r in required if not features.get(r, False)]
    met_opt  = [o for o in optional if features.get(o, False)]

    # Need at least half the required conditions to form a hypothesis
    if not met_req or len(met_req) < max(1, len(required) // 2):
        return None

    req_score  = len(met_req) / max(len(required), 1)
    opt_score  = len(met_opt) / max(len(optional), 1) if optional else 0.0
    base_conf  = req_score * 0.65 + opt_score * 0.15 + geometry.measurement_quality * 0.20

    # Weakest-link confidence propagation — the chain is only as strong as its weakest link
    geom_conf  = geometry.confidence
    state_conf = state.confidence
    auc_conf   = auction.confidence if auction else 0.8
    confidence = max(0.0, min(1.0, min(base_conf, geom_conf, state_conf, auc_conf)))

    # Build diagnostic fields
    because = ([f"[REQ] {_CONDITION_DESCRIPTIONS.get(r, r)}" for r in met_req] +
               [f"[OPT] {_CONDITION_DESCRIPTIONS.get(o, o)}" for o in met_opt])
    against = [f"[MISSING REQ] {_CONDITION_DESCRIPTIONS.get(r, r)}" for r in miss_req]

    # State-based against conditions
    if bias == "LONG" and state.direction < -30:
        against.append(f"Market direction bearish ({state.direction:+.0f})")
        confidence *= 0.85
    elif bias == "SHORT" and state.direction > 30:
        against.append(f"Market direction bullish ({state.direction:+.0f})")
        confidence *= 0.85

    missing = [_CONDITION_DESCRIPTIONS.get(r, r) for r in miss_req]

    # Auction state penalty
    if auction and auction.state == "FAILED":
        against.append("Auction FAILED — price rejected this zone")
        confidence *= 0.70

    confidence = round(max(0.0, min(1.0, confidence)), 3)

    # Breakout levels
    n         = len(df)
    upper_now = _price_at(geometry.upper, n - 1)
    lower_now = _price_at(geometry.lower, n - 1)
    breakout_level = round(upper_now if bias == "LONG" else lower_now, 8) if bias != "NEUTRAL" else None

    # Status
    if   geometry.breakout_up and bias == "LONG":   status: Literal["FORMING", "CONFIRMED", "FAILED", "EXPIRED"] = "CONFIRMED"
    elif geometry.breakout_dn and bias == "SHORT":   status = "CONFIRMED"
    elif geometry.breakout_up and bias == "SHORT":   status = "FAILED"
    elif geometry.breakout_dn and bias == "LONG":    status = "FAILED"
    else:                                             status = "FORMING"

    # RiskSurface
    invalidation = lower_now if bias == "LONG" else (upper_now if bias == "SHORT" else
                   (lower_now if current_price > (upper_now + lower_now) / 2 else upper_now))
    dist_pct  = abs(current_price - invalidation) / max(abs(current_price), 1e-10) * 100.0
    urgency   = max(0.0, min(1.0, 1.0 - dist_pct / 2.5))
    n_corr    = sum(1 for l in legs if l.character in ("CORRECTIVE", "NEUTRAL"))
    age_bars  = int(np.mean([l.bar_count for l in legs[-n_corr:]]) * n_corr) if n_corr else 0
    time_pres = min(1.0, age_bars / 80.0)

    risk = RiskSurface(
        invalidation_price=round(invalidation, 8),
        current_distance_pct=round(dist_pct, 3),
        urgency=round(urgency, 3),
        time_pressure=round(time_pres, 3),
    )

    ts_ms  = int(datetime.now().timestamp() * 1000)
    hyp_id = f"hyp_{name.lower()}_{ts_ms}"

    ev = _EXPECTED_EVENTS.get(name, ("", ""))

    return Hypothesis(
        id=hyp_id, name=name, bias=bias, confidence=confidence,
        because=because, against=against, missing=missing,
        risk=risk, age_bars=age_bars, urgency=round(urgency, 3),
        breakout_level=breakout_level, status=status,
        created_because=f"{len(met_req)}/{len(required)} required conditions met",
        geometry_confidence=round(geom_conf, 3),
        state_confidence=round(state_conf, 3),
        auction_confidence=round(auc_conf, 3),
        expected_next_event=ev[0],
        expected_entry_trigger=ev[1],
    )


# ─────────────────────────────────────────────────────────────────────────────
# OPPORTUNITY ENGINE
# Input:  MarketSurvey (without opportunity) — top hypothesis + auction
# Output: Optional[Opportunity] — the single best actionable setup right now
# Never:  predicts direction, overrides hypothesis ranking, sets leverage/risk
# ─────────────────────────────────────────────────────────────────────────────

def run_opportunity_engine(hypotheses: List[Hypothesis], auction: Optional[ActiveAuction],
                            current_price: float) -> Optional[Opportunity]:
    """
    Synthesize the top actionable opportunity from auction + hypothesis.
    Returns None if no setup clears the B-quality threshold.
    """
    if not hypotheses or not auction:
        return None

    top = hypotheses[0]
    if top.confidence < 0.35 or top.bias == "NEUTRAL":
        return None
    if auction.state == "FAILED":
        return None

    bias = top.bias

    # Entry zone: anchored to auction's value area or balance boundary
    if auction.vah is not None and auction.val is not None:
        if bias == "LONG":
            entry_lo = min(auction.val, auction.balance_low * 1.003)
            entry_hi = auction.val * 1.005
        else:
            entry_lo = auction.vah * 0.995
            entry_hi = max(auction.vah, auction.balance_high * 0.997)
    else:
        if bias == "LONG":
            entry_lo = auction.balance_low
            entry_hi = auction.balance_low * 1.005
        else:
            entry_lo = auction.balance_high * 0.995
            entry_hi = auction.balance_high

    invalidation = top.risk.invalidation_price

    # Target and reward profile — anchored to auction structure
    has_fib = auction.fib_anchor_high is not None and auction.fib_anchor_low is not None

    if top.status == "CONFIRMED" and has_fib:
        # Already broken out — measured move target from auction anchor
        impulse_range = auction.fib_anchor_high - auction.fib_anchor_low  # type: ignore[operator]
        target        = (entry_lo + impulse_range) if bias == "LONG" else (entry_hi - impulse_range)
        reward_profile: Literal["AT_BALANCE_EXTREME", "BREAKOUT_RETRACE", "MID_BALANCE"] = "BREAKOUT_RETRACE"
    elif abs(current_price - auction.balance_high) < abs(current_price - auction.balance_low):
        # Price near balance high
        target         = auction.balance_low  if bias == "SHORT" else auction.balance_high
        reward_profile = "AT_BALANCE_EXTREME" if bias == "SHORT" else "MID_BALANCE"
    else:
        # Price near balance low
        target         = auction.balance_high if bias == "LONG"  else auction.balance_low
        reward_profile = "AT_BALANCE_EXTREME" if bias == "LONG"  else "MID_BALANCE"

    risk_dist   = abs(entry_lo - invalidation)    if bias == "LONG" else abs(entry_hi - invalidation)
    reward_dist = abs(target   - (entry_lo if bias == "LONG" else entry_hi))
    structural_rr = round(reward_dist / max(risk_dist, 1e-10), 2)

    # Quality gate
    if   top.confidence >= 0.70 and structural_rr >= 2.0:
        quality: Literal["A+", "A", "B", "C"] = "A+"
    elif top.confidence >= 0.55 and structural_rr >= 1.5:
        quality = "A"
    elif top.confidence >= 0.40 and structural_rr >= 1.0:
        quality = "B"
    else:
        quality = "C"

    # Expiry: fresher setups get more time
    expiry_bars = max(5, int(40 * (1.0 - top.risk.time_pressure)))

    # Confluence score: mechanical composition
    confluence_score = round(
        top.confidence * 50.0
        + (1.0 - top.risk.time_pressure) * 20.0
        + min(1.0, structural_rr / 3.0) * 20.0
        + auction.balance_score * 10.0,
        1
    )

    ts_ms = int(datetime.now().timestamp() * 1000)

    return Opportunity(
        id=f"opp_{top.name.lower()}_{ts_ms}",
        auction_id=auction.id,
        hypothesis_name=top.name,
        bias=bias,
        entry_zone=(round(entry_lo, 8), round(entry_hi, 8)),
        structural_rr=structural_rr,
        confluence_score=confluence_score,
        urgency=round(top.urgency, 3),
        expected_trigger=top.expected_entry_trigger,
        expiry_bars=expiry_bars,
        reward_profile=reward_profile,
        quality=quality,
        invalidation_price=round(invalidation, 8),
        target_price=round(target, 8),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────────

def run_survey(df: pd.DataFrame, symbol: str, primary_tf: str,
               dexter_score: float, dexter_reasons: List[str],
               regime_4h: str = "UNKNOWN",
               sr_support: float = None,
               sr_resistance: float = None) -> MarketSurvey:
    """
    Orchestrate the full engine stack for one (symbol, primary_tf) scan.
    Order: Swing ->Leg ->State ->Geometry ->Auction ->Hypothesis ->MarketSurvey
    """
    current_price = float(df["close"].iloc[-1])

    # Asset Profile Engine runs FIRST — every downstream engine uses it to annotate
    # its own output with percentile context (not delegated to the formatter).
    asset_profile          = run_asset_profile_engine(df, symbol, primary_tf)

    swings, swing_analyses = run_swing_engine(df, window=3, confirm_window=8)
    legs                   = run_leg_engine(swings, swing_analyses, df, profile=asset_profile)
    state                  = run_state_engine(legs, df, profile=asset_profile)
    geometry               = run_geometry_engine(swings, df, profile=asset_profile)
    auction                = run_auction_engine(legs, df, state)
    hypotheses             = run_hypothesis_engine(geometry, state, auction, legs, df, current_price)
    opportunity            = run_opportunity_engine(hypotheses, auction, current_price)

    return MarketSurvey(
        symbol=symbol, primary_tf=primary_tf,
        scanned_at=datetime.now(), current_price=current_price,
        swings=swings, swing_analyses=swing_analyses, legs=legs,
        state=state, geometry=geometry, auction=auction, hypotheses=hypotheses,
        dexter_score=dexter_score, dexter_reasons=dexter_reasons,
        regime_4h=regime_4h, sr_support=sr_support, sr_resistance=sr_resistance,
        opportunity=opportunity, asset_profile=asset_profile,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SURVEY ->CHEV PROMPT BLOCK
# Converts a MarketSurvey into the text that goes into Chev's escalation prompt.
# ─────────────────────────────────────────────────────────────────────────────

def format_survey_for_chev(survey: MarketSurvey) -> str:
    """
    Build the MARKET STATE + GEOMETRY + AUCTION + HYPOTHESES text block
    that replaces the old pattern_block in Chev's prompt.
    """
    lines: List[str] = []

    # ── Asset Profile Context ─────────────────────────────────────────────────
    prof = survey.asset_profile
    if prof and prof.n_legs >= 8:
        imp_p50 = prof.leg_atr_sorted[len(prof.leg_atr_sorted) // 2] if prof.leg_atr_sorted else 0.0
        bal_p50 = (prof.balance_width_sorted[len(prof.balance_width_sorted) // 2]
                   if prof.balance_width_sorted else 0.0)
        lines += [
            f"ASSET CONTEXT [{survey.symbol} {survey.primary_tf} — "
            f"{prof.n_legs} legs from {prof.computed_from_bars} bars]:",
            f"  Typical impulse: {imp_p50:.1f} ATR (p50) | "
            f"Typical balance width: {bal_p50:.1f} ATR (p50)",
            f"  Percentile ranks below are specific to this asset — 90th means unusual for {survey.symbol}.",
            "",
        ]

    # ── Market State ─────────────────────────────────────────────────────────
    s = survey.state
    dir_lbl  = ("BULLISH" if s.direction > 30 else "BEARISH" if s.direction < -30 else "NEUTRAL")
    part_lbl = ("HIGH" if s.participation > 65 else "LOW" if s.participation < 35 else "MODERATE")
    part_pct_str = f" | {s.participation_pct}th pct" if (prof and prof.n_legs >= 8) else ""
    pb = s.participation_breakdown
    breakdown_str = ""
    if pb:
        breakdown_str = (f" [legs={pb.get('leg_component', 0):.0f} "
                         f"atr={pb.get('atr_component', 0):.0f} "
                         f"vol={pb.get('volume_component', 0):.0f} "
                         f"range={pb.get('range_component', 0):.0f}]")
    lines += [
        "MARKET STATE (Dexter measured — not a prediction, a measurement):",
        f"  Participation : {s.participation:.0f}/100 [{part_lbl}{part_pct_str}]{breakdown_str}",
        f"  Direction     : {s.direction:+.0f}/100 [{dir_lbl}]",
        f"  Phase         : {s.phase} | ATR {s.atr_trend} | Volume {s.volume_trend}",
        f"  Leg sequence  : {s.leg_sequence}",
        f"  Confidence    : {s.confidence:.0%}",
        "",
    ]

    # ── Geometry ─────────────────────────────────────────────────────────────
    g = survey.geometry
    if g.sample_size >= 4:
        geo_shape = ("CONVERGING" if g.is_converging else
                     "PARALLEL"   if g.is_parallel   else
                     "DIVERGING"  if g.is_diverging  else "OPEN")
        lines += ["GEOMETRY (Dexter measured — no pattern name, raw shape only):"]
        lines.append(f"  Shape       : {geo_shape} | Axis: {g.structure_axis} | "
                     f"Compression {g.compression:.0%} | Parallelism {g.parallelism:.0%}")
        lines.append(f"  Upper line  : slope_norm={g.upper.slope_norm:+.5f} "
                     f"R²={g.upper.r_squared:.2f} ({g.upper.touch_count} touches)")
        lines.append(f"  Lower line  : slope_norm={g.lower.slope_norm:+.5f} "
                     f"R²={g.lower.r_squared:.2f} ({g.lower.touch_count} touches)")
        if g.has_impulse:
            if prof and prof.leg_atr_sorted:
                imp_pct = _percentile_rank(g.impulse_atr, prof.leg_atr_sorted)
                imp_pct_str = f" | {imp_pct}th pct for {survey.symbol}"
            else:
                imp_pct_str = ""
            lines.append(f"  Prior impulse: {g.impulse_direction} {g.impulse_atr:.1f} ATR"
                         f"{imp_pct_str} (flag/pennant mast eligible)")
        if g.breakout_up:
            vol_mult = g.vol_at_breakout / max(g.avg_vol, 1e-10)
            lines.append(f"  ⚡ BREAKOUT UP confirmed | Volume {vol_mult:.1f}× avg")
        elif g.breakout_dn:
            vol_mult = g.vol_at_breakout / max(g.avg_vol, 1e-10)
            lines.append(f"  ⚡ BREAKOUT DOWN confirmed | Volume {vol_mult:.1f}× avg")
        lines.append(f"  Meas. quality: {g.measurement_quality:.0%} | "
                     f"Sample: {g.sample_size} swing points | Confidence: {g.confidence:.0%}")
        lines.append("")

    # ── Active Auction ────────────────────────────────────────────────────────
    if survey.auction:
        a = survey.auction
        lines += [f"ACTIVE AUCTION [{a.state}] | Maturity: {a.maturity:.0%} | Balance quality: {a.balance_score:.0%}:"]
        lines.append(f"  Anchor price : {a.anchor_price:.6f} | Age: {a.age_bars} bars")
        bal_pct_str = ""
        if prof and prof.balance_width_sorted:
            bal_pct = _percentile_rank(a.balance_width_atr, prof.balance_width_sorted)
            bal_pct_str = f" | {bal_pct}th pct width for {survey.symbol}"
        lines.append(f"  Balance zone : {a.balance_low:.6f} – {a.balance_high:.6f} "
                     f"({a.balance_width_atr:.1f} ATR wide{bal_pct_str})")
        if a.poc:
            lines.append(f"  Volume Profile: POC={a.poc:.6f} | VAH={a.vah:.6f} | VAL={a.val:.6f}")
        if a.rejected_above:
            lines.append(f"  Rejected above: {a.rejected_above:.6f} (supply absorbed)")
        if a.rejected_below:
            lines.append(f"  Rejected below: {a.rejected_below:.6f} (demand absorbed)")
        if a.defended_levels:
            lines.append(f"  Defended levels: {' | '.join(f'{p:.6f}' for p in a.defended_levels)}")
        if a.fib_anchor_high and a.fib_anchor_low:
            lines.append(f"  Fib anchors  : {a.fib_anchor_low:.6f} – {a.fib_anchor_high:.6f}")
        lines.append(f"  Energy: {a.energy:.2f} | Imbalance: {a.imbalance_score:.2f} | "
                     f"Created: {a.created_because}")
        lines.append("")

    # ── Hypotheses ────────────────────────────────────────────────────────────
    if survey.hypotheses:
        lines.append(f"PATTERN HYPOTHESES — {len(survey.hypotheses)} competing "
                     f"(Dexter measures geometry; Chev interprets meaning):")
        for i, h in enumerate(survey.hypotheses[:4], 1):
            lines.append(f"  [{i}] {h.name} | bias={h.bias} | "
                         f"confidence={h.confidence:.0%} | status={h.status} | "
                         f"age={h.age_bars} bars")
            lines.append(f"       RiskSurface: invalidation={h.risk.invalidation_price:.6f} "
                         f"({h.risk.current_distance_pct:.1f}% away) | "
                         f"urgency={h.risk.urgency:.0%} | time_pressure={h.risk.time_pressure:.0%}")
            if h.breakout_level:
                lines.append(f"       Breakout level: {h.breakout_level:.6f}")
            if h.because:
                lines.append(f"       FOR    : {' | '.join(h.because[:3])}")
            if h.against:
                lines.append(f"       AGAINST: {' | '.join(h.against[:2])}")
            if h.missing:
                lines.append(f"       MISSING: {' | '.join(h.missing[:2])} (incomplete, not wrong)")
            if h.expected_next_event:
                lines.append(f"       NEXT   : {h.expected_next_event}")
            if h.expected_entry_trigger:
                lines.append(f"       TRIGGER: {h.expected_entry_trigger}")
            lines.append("")
    else:
        lines.append("PATTERN HYPOTHESES: No hypothesis reached 30% confidence threshold "
                     "(geometry may be insufficient or too noisy).")
        lines.append("")

    # ── Opportunity ───────────────────────────────────────────────────────────
    if survey.opportunity:
        o = survey.opportunity
        lines += [
            f"OPPORTUNITY [{o.quality}] — {o.hypothesis_name} | {o.bias} | "
            f"Score: {o.confluence_score:.0f}/100:",
            f"  Entry zone    : {o.entry_zone[0]:.6f} – {o.entry_zone[1]:.6f}",
            f"  Invalidation  : {o.invalidation_price:.6f} | Target: {o.target_price:.6f}",
            f"  Structural R:R: {o.structural_rr:.1f}R | Profile: {o.reward_profile}",
            f"  Urgency: {o.urgency:.0%} | Expires in: {o.expiry_bars} bars",
            f"  Entry trigger : {o.expected_trigger}",
            "",
        ]

    # ── Performance Engine context ────────────────────────────────────────────
    if survey.hypotheses:
        top = survey.hypotheses[0]
        stats = get_hypothesis_stats(hypothesis_name=top.name)
        if stats:
            st = stats[0]
            if st["total"] >= 3:
                lines.append(f"PERFORMANCE ENGINE — {top.name} historical "
                             f"({st['asset_class']} {st['tf']}):")
                lines.append(f"  Win rate: {st['win_rate']:.0%} | "
                             f"Trades: {st['total']} | Avg Dexter score: {st['avg_score']:.1f}")
                lines.append("")

    return "\n".join(lines)
