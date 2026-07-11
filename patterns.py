"""
patterns.py — Chart Pattern Recognition Engine (Chev Integration Edition)
=========================================================================
Integrated with Dexter's engine stack.  Can be called two ways:

    # Integrated: pass Dexter's SwingPoint objects (skips own pivot detection)
    result = patterns.run(df, dexter_highs=highs, dexter_lows=lows)

    # Standalone: omit dexter_highs/lows and it finds its own pivots
    result = patterns.run(df)

Fixes over the original (friend's) version:
  1. Local trendlines — pattern boundaries are fit to the last 2-3 pivots
     only, not a global regression across the full lookback window.
     A single line through 10 swing highs averages away the convergence
     in the final 20-40 bars.  The local line catches it.
     The global R² is still used as a quality gate (is the boundary
     broadly linear?).  The local slope decides the current direction.

  2. Volume confirmation — two explicit checks:
       • Contraction during formation (linear slope on volume, negative = good)
       • Expansion on breakout (recent bars vs 20-bar average > 1.4×)
     Both feed directly into confidence adjustments, not just metadata.

  3. Double/Triple deduplication — Triple Top/Bottom suppresses the
     redundant Double detection on the same swing set.

  4. ATR-scaled breakout threshold — replaces the fixed 1.5% with
     max(0.5%, min(2.5%, 0.5 × ATR / current_price)).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Literal, List, Optional

try:
    from scipy.signal import find_peaks
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False


# ── Volatility helpers ────────────────────────────────────────────────────────

def _calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float(df["high"].mean()) * 0.015
    hl  = df["high"] - df["low"]
    hcp = np.abs(df["high"] - df["close"].shift())
    lcp = np.abs(df["low"]  - df["close"].shift())
    tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else float(df["high"].mean()) * 0.015


def _atr_breakout_pct(atr: float, price: float) -> float:
    """
    Scale the breakout confirmation threshold with the asset's volatility.
    Fixed 1.5% was too tight for high-ATR assets (BTC daily) and far too
    loose for low-ATR forex on short timeframes.
    """
    if price <= 0:
        return 0.015
    dynamic = 0.5 * atr / price
    return max(0.005, min(0.025, dynamic))


# ── Pivot detection (standalone mode only) ───────────────────────────────────

def _find_pivots_standalone(hi_vals: np.ndarray, lo_vals: np.ndarray, atr: float,
                             prominence_mult: float = 1.1):
    if not _SCIPY_OK:
        raise RuntimeError("scipy not installed — pass dexter_highs/dexter_lows to skip this")
    min_prom = atr * prominence_mult
    sh, _ = find_peaks( hi_vals, prominence=min_prom, distance=5)
    sl, _ = find_peaks(-lo_vals, prominence=min_prom, distance=5)
    return list(sh), list(sl)


# ── Trendline fitting ─────────────────────────────────────────────────────────

def _global_trendline(indices: List[int], values: np.ndarray):
    """
    OLS through ALL pivot points.  Used for R² quality check only —
    tells us how well the whole boundary fits a straight line.
    """
    if len(indices) < 2:
        v0 = values[indices[0]] if indices else 0.0
        return 0.0, float(v0), 0.0
    x      = np.array(indices, dtype=float)
    y      = values[indices]
    xn     = x - x[0]
    coeffs = np.polyfit(xn, y, 1)
    y_pred = np.polyval(coeffs, xn)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 1e-8 else 1.0
    return float(coeffs[0]), float(coeffs[1]), max(0.0, r2)


def _local_trendline(indices: List[int], values: np.ndarray, n_last: int = 3):
    """
    Fit using only the most recent n_last pivots.

    WHY: a single regression over 10+ swing highs produces a slope that
    reflects the average of the entire history.  A triangle forming over
    the last 30 bars inside a 120-bar lookback will have its convergence
    diluted to near-zero in the global slope.  Using the last 3 highs
    gives a slope that reflects what the boundary is doing RIGHT NOW.

    Returns (slope, intercept, r2).
    intercept is the fitted value at the FIRST of the n_last pivots
    (x_norm=0 there), consistent with the original's convention so
    projection arithmetic stays the same.
    """
    use = indices[-n_last:]
    if len(use) < 2:
        v0 = values[use[0]] if use else 0.0
        return 0.0, float(v0), 1.0   # single point: zero slope, perfect "fit"
    x      = np.array(use, dtype=float)
    y      = values[use]
    xn     = x - x[0]
    coeffs = np.polyfit(xn, y, 1)
    y_pred = np.polyval(coeffs, xn)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 1e-8 else 1.0
    return float(coeffs[0]), float(coeffs[1]), max(0.0, r2)


def _project(slope: float, intercept: float, anchor_idx: int, target_idx: int) -> float:
    """Price on trendline at target_idx, given intercept is at anchor_idx."""
    return intercept + slope * (target_idx - anchor_idx)


def _classify_slope(slope: float, atr: float, flat_mult: float = 0.08) -> str:
    threshold = atr * flat_mult
    if slope > threshold:  return "rising"
    if slope < -threshold: return "falling"
    return "flat"


# ── Volume confirmation ───────────────────────────────────────────────────────

def _vol_contracting(df: pd.DataFrame, start_bar: int) -> bool:
    """
    Is volume trending down across the pattern window?
    Uses a simple OLS slope on raw volume.  Negative slope = contracting.
    Requires at least 6 bars to be meaningful — otherwise return False
    (not enough data to distinguish noise from trend).
    """
    if "volume" not in df.columns:
        return False
    seg = df["volume"].iloc[start_bar:].values.astype(float)
    if len(seg) < 6:
        return False
    x = np.arange(len(seg), dtype=float)
    slope = np.polyfit(x, seg, 1)[0]
    # bool(...) — np.polyfit returns numpy.float64, so `slope < 0.0` is a
    # numpy.bool_, which Flask's JSON encoder cannot serialize. Cast to native.
    return bool(slope < 0.0)


def _vol_expanding_on_breakout(df: pd.DataFrame,
                                breakout_bars: int = 3,
                                avg_window: int = 20) -> bool:
    """
    Are the most recent breakout_bars above average?
    Threshold: 1.4× the trailing avg_window average.
    Returns False if volume data is missing or insufficient.
    """
    if "volume" not in df.columns or len(df) < avg_window + breakout_bars:
        return False
    recent = float(df["volume"].iloc[-breakout_bars:].mean())
    avg    = float(df["volume"].iloc[-(avg_window + breakout_bars):-breakout_bars].mean())
    return avg > 1e-10 and (recent / avg) > 1.4


def _apply_volume_to_conf(conf: float, is_breakout: bool,
                           vol_contracting: bool,
                           vol_expanding: bool) -> tuple[float, list]:
    """
    Adjust confidence based on volume behaviour.

    Contraction during formation:  +5 pp  (healthy compression)
    Expansion on breakout:         +10 pp (breakout has conviction)
    Missing expansion on breakout: -15 pp (suspect — watch for fake-out)

    Caps at [0, 1].
    """
    notes = []
    if vol_contracting:
        conf = min(1.0, conf + 0.05)
        notes.append("volume contracting during formation ✓")
    if is_breakout:
        if vol_expanding:
            conf = min(1.0, conf + 0.10)
            notes.append("volume expanding on breakout ✓")
        else:
            conf = max(0.0, conf - 0.15)
            notes.append("breakout lacks volume confirmation ✗")
    return conf, notes


# ── Pattern container ─────────────────────────────────────────────────────────

@dataclass
class DetectedPattern:
    name:       str
    bias:       Literal["bullish", "bearish", "neutral"]
    category:   Literal["continuation", "reversal", "neutral"]
    signal:     Literal["BUY", "SELL", "NEUTRAL"]
    confidence: float
    breakout:   bool = False
    details:    dict = field(default_factory=dict)
    volume_notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "pattern":      self.name,
            "bias":         self.bias,
            "category":     self.category,
            "signal":       self.signal,
            "confidence":   round(float(self.confidence), 3),
            "breakout":     bool(self.breakout),
            "volume_notes": self.volume_notes,
            **self.details,
        }


# ── Pattern detectors ─────────────────────────────────────────────────────────

def _detect_triangles(hi_local_slope: str, lo_local_slope: str,
                      hi_global_r2: float,  lo_global_r2: float,
                      breakout_up: bool, breakout_dn: bool) -> List[DetectedPattern]:
    results = []
    if hi_global_r2 < 0.55 or lo_global_r2 < 0.55:
        return results
    conf = (hi_global_r2 + lo_global_r2) / 2

    if hi_local_slope == "falling" and lo_local_slope == "rising":
        signal = "BUY" if breakout_up else ("SELL" if breakout_dn else "NEUTRAL")
        bias   = "bullish" if breakout_up else ("bearish" if breakout_dn else "neutral")
        results.append(DetectedPattern(
            "Symmetrical Triangle", bias, "continuation",
            signal, conf, breakout_up or breakout_dn))

    elif hi_local_slope == "flat" and lo_local_slope == "rising":
        results.append(DetectedPattern(
            "Ascending Triangle", "bullish", "continuation",
            "BUY" if breakout_up else "NEUTRAL", conf, breakout_up))

    elif hi_local_slope == "falling" and lo_local_slope == "flat":
        results.append(DetectedPattern(
            "Descending Triangle", "bearish", "continuation",
            "SELL" if breakout_dn else "NEUTRAL", conf, breakout_dn))

    return results


def _detect_wedges(hi_local_slope: str, lo_local_slope: str,
                   hi_global_r2: float,  lo_global_r2: float,
                   breakout_up: bool, breakout_dn: bool) -> List[DetectedPattern]:
    results = []
    if hi_global_r2 < 0.52 or lo_global_r2 < 0.52:
        return results
    conf = (hi_global_r2 + lo_global_r2) / 2

    if hi_local_slope == "rising" and lo_local_slope == "rising":
        results.append(DetectedPattern(
            "Rising Wedge", "bearish", "neutral",
            "SELL" if breakout_dn else "NEUTRAL", conf, breakout_dn))
    elif hi_local_slope == "falling" and lo_local_slope == "falling":
        results.append(DetectedPattern(
            "Falling Wedge", "bullish", "neutral",
            "BUY" if breakout_up else "NEUTRAL", conf, breakout_up))

    return results


def _detect_rectangle(hi_local_slope: str, lo_local_slope: str,
                      hi_global_r2: float,  lo_global_r2: float,
                      breakout_up: bool, breakout_dn: bool) -> List[DetectedPattern]:
    results = []
    if hi_local_slope == "flat" and lo_local_slope == "flat" and hi_global_r2 > 0.50 and lo_global_r2 > 0.50:
        conf = (hi_global_r2 + lo_global_r2) / 2
        if breakout_up:
            results.append(DetectedPattern("Rectangle", "bullish", "continuation", "BUY",  conf, True))
        elif breakout_dn:
            results.append(DetectedPattern("Rectangle", "bearish", "continuation", "SELL", conf, True))
        else:
            results.append(DetectedPattern("Rectangle", "neutral", "continuation", "NEUTRAL", min(conf, 0.40), False))
    return results


def _detect_hs(swing_highs: List[int], swing_lows: List[int],
               hi_vals: np.ndarray, lo_vals: np.ndarray,
               current_price: float, breakout_pct: float) -> List[DetectedPattern]:
    """
    Head & Shoulders and Inverse H&S.
    Breakout is confirmed against the NECKLINE, not the channel trendline —
    they are not the same thing and conflating them causes false confirmations.
    """
    results = []

    # Head & Shoulders (bearish)
    if len(swing_highs) >= 3:
        h = swing_highs[-3:]
        p = hi_vals[h]
        if p[1] > p[0] and p[1] > p[2] and abs(p[0] - p[2]) / p[1] < 0.085:
            left_troughs  = [l for l in swing_lows if h[0] < l < h[1]]
            right_troughs = [l for l in swing_lows if h[1] < l < h[2]]
            details = {
                "left_shoulder": round(float(p[0]), 4),
                "head":          round(float(p[1]), 4),
                "right_shoulder":round(float(p[2]), 4),
            }
            if left_troughs and right_troughs:
                t1, t2      = left_troughs[-1], right_troughs[0]
                neck_slope  = (lo_vals[t2] - lo_vals[t1]) / (t2 - t1) if t2 != t1 else 0.0
                neck_now    = lo_vals[t1] + neck_slope * ((len(hi_vals) - 1) - t1)
                confirmed   = current_price < neck_now * (1 - breakout_pct)
                conf        = 0.78 if confirmed else 0.52
                details["neckline"] = round(float(neck_now), 4)
            else:
                confirmed, conf = False, 0.38
            results.append(DetectedPattern(
                "Head & Shoulders", "bearish", "reversal",
                "SELL" if confirmed else "NEUTRAL", conf, confirmed, details))

    # Inverse Head & Shoulders (bullish)
    if len(swing_lows) >= 3:
        l = swing_lows[-3:]
        p = lo_vals[l]
        if p[1] < p[0] and p[1] < p[2] and abs(p[0] - p[2]) / abs(p[1]) < 0.085:
            left_peaks  = [h for h in swing_highs if l[0] < h < l[1]]
            right_peaks = [h for h in swing_highs if l[1] < h < l[2]]
            details = {
                "left_shoulder": round(float(p[0]), 4),
                "head":          round(float(p[1]), 4),
                "right_shoulder":round(float(p[2]), 4),
            }
            if left_peaks and right_peaks:
                t1, t2      = left_peaks[-1], right_peaks[0]
                neck_slope  = (hi_vals[t2] - hi_vals[t1]) / (t2 - t1) if t2 != t1 else 0.0
                neck_now    = hi_vals[t1] + neck_slope * ((len(lo_vals) - 1) - t1)
                confirmed   = current_price > neck_now * (1 + breakout_pct)
                conf        = 0.78 if confirmed else 0.52
                details["neckline"] = round(float(neck_now), 4)
            else:
                confirmed, conf = False, 0.38
            results.append(DetectedPattern(
                "Inverse Head & Shoulders", "bullish", "reversal",
                "BUY" if confirmed else "NEUTRAL", conf, confirmed, details))

    return results


def _detect_double_triple(swing_highs: List[int], swing_lows: List[int],
                           hi_vals: np.ndarray, lo_vals: np.ndarray,
                           breakout_up: bool, breakout_dn: bool,
                           tol: float = 0.035) -> List[DetectedPattern]:
    """
    Double/Triple Top & Bottom detection.

    Fix: Triple Top/Bottom suppresses the Double detection on the same swings.
    In the original, both fired and were deduped only by the best-confidence
    picker — but the picker doesn't know they're the same formation.  Now,
    if 3 pivots qualify as a Triple, we don't also add a Double for those pivots.
    """
    results = []
    used_high_triple = False
    used_low_triple  = False

    # Triple Top (bearish) — check FIRST so it can block Double
    if len(swing_highs) >= 3:
        idx  = swing_highs[-3:]
        p    = hi_vals[idx]
        spread = (p.max() - p.min()) / p.mean()
        if spread < tol:
            used_high_triple = True
            results.append(DetectedPattern(
                "Triple Top", "bearish", "reversal",
                "SELL" if breakout_dn else "NEUTRAL",
                0.70 if breakout_dn else 0.47, breakout_dn,
                {"tops": [round(float(x), 4) for x in p]}))

    # Double Top (bearish) — skip if same set already formed a Triple
    if not used_high_triple and len(swing_highs) >= 2:
        idx = swing_highs[-2:]
        p   = hi_vals[idx]
        if abs(p[0] - p[1]) / p[0] < tol:
            results.append(DetectedPattern(
                "Double Top", "bearish", "reversal",
                "SELL" if breakout_dn else "NEUTRAL",
                0.64 if breakout_dn else 0.43, breakout_dn,
                {"top1": round(float(p[0]), 4), "top2": round(float(p[1]), 4)}))

    # Triple Bottom (bullish) — check FIRST
    if len(swing_lows) >= 3:
        idx  = swing_lows[-3:]
        p    = lo_vals[idx]
        spread = (p.max() - p.min()) / p.mean()
        if spread < tol:
            used_low_triple = True
            results.append(DetectedPattern(
                "Triple Bottom", "bullish", "reversal",
                "BUY" if breakout_up else "NEUTRAL",
                0.70 if breakout_up else 0.47, breakout_up,
                {"bottoms": [round(float(x), 4) for x in p]}))

    # Double Bottom (bullish) — skip if same set already formed a Triple
    if not used_low_triple and len(swing_lows) >= 2:
        idx = swing_lows[-2:]
        p   = lo_vals[idx]
        if abs(p[0] - p[1]) / p[0] < tol:
            results.append(DetectedPattern(
                "Double Bottom", "bullish", "reversal",
                "BUY" if breakout_up else "NEUTRAL",
                0.64 if breakout_up else 0.43, breakout_up,
                {"bottom1": round(float(p[0]), 4), "bottom2": round(float(p[1]), 4)}))

    return results


# ── Trendline endpoint export (for webapp drawing) ────────────────────────────

def _trendline_endpoints(swing_indices_local: List[int],
                          slope: float, intercept: float,
                          anchor_local: int,
                          df_window: pd.DataFrame,
                          n_extend_bars: int = 25):
    """
    Convert local trendline to two timestamp+price points the webapp can draw.

    start: first swing in the local set
    end  : last bar of the window + n_extend_bars (project forward for context)
    Timestamps come from the df_window index (DatetimeIndex).
    """
    start_local = swing_indices_local[0]
    end_local   = min(len(df_window) - 1 + n_extend_bars, len(df_window) - 1)

    p_start = _project(slope, intercept, anchor_local, start_local)
    p_end   = _project(slope, intercept, anchor_local, end_local)

    # Timestamps — clamp to df bounds
    ts_start = int(df_window.index[start_local].timestamp())
    ts_end   = int(df_window.index[min(end_local, len(df_window) - 1)].timestamp())

    # If we projected past the window end, estimate the future timestamp
    if end_local > len(df_window) - 1:
        extra_bars = end_local - (len(df_window) - 1)
        if len(df_window) >= 2:
            bar_seconds = int((df_window.index[-1] - df_window.index[-2]).total_seconds())
            ts_end = int(df_window.index[-1].timestamp()) + extra_bars * bar_seconds

    return {
        "t1": ts_start, "p1": round(float(p_start), 8),
        "t2": ts_end,   "p2": round(float(p_end),   8),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def run(
    df: pd.DataFrame,
    dexter_highs=None,   # List[SwingPoint]-like from Dexter (have .index, .price)
    dexter_lows=None,    # List[SwingPoint]-like from Dexter
    lookback: int = 120,
) -> dict:
    """
    Run pattern recognition on a candle DataFrame.

    When dexter_highs / dexter_lows are provided (from Dexter's swing engine),
    their pivot detection replaces the standalone find_peaks path.  This keeps
    pivot detection consistent across the whole stack.

    Returns a dict that is JSON-serializable and ready to be added to the
    /api/analysis/engine response payload under the key "patterns".
    """
    _empty = {
        "signal": "NEUTRAL", "value": 0.0, "pattern": "None",
        "bias": "neutral", "breakout": False, "volume_confirmed": False,
        "volume_notes": [], "all_patterns": [],
        "swing_highs": [], "swing_lows": [],
        "upper_trendline": None, "lower_trendline": None,
    }

    if len(df) < 30:
        return _empty

    # ── Window ───────────────────────────────────────────────────────────────
    offset    = max(0, len(df) - lookback)
    window_df = df.iloc[-lookback:].copy().reset_index(drop=True)
    # Re-attach datetime index that the original df had, shifted to window
    if hasattr(df.index, 'to_pydatetime'):
        window_df.index = df.index[-len(window_df):]

    hi_vals       = window_df["high"].values
    lo_vals       = window_df["low"].values
    current_price = float(window_df["close"].iloc[-1])
    current_atr   = _calculate_atr(window_df)
    breakout_pct  = _atr_breakout_pct(current_atr, current_price)

    # ── Pivot detection ───────────────────────────────────────────────────────
    if dexter_highs is not None and dexter_lows is not None:
        # Use Dexter's confirmed swing points — no double counting
        # Convert full-df indices to window-local indices
        swing_highs = [s.index - offset for s in dexter_highs
                       if 0 <= s.index - offset < len(window_df)]
        swing_lows  = [s.index - offset for s in dexter_lows
                       if 0 <= s.index - offset < len(window_df)]
    else:
        swing_highs, swing_lows = _find_pivots_standalone(hi_vals, lo_vals, current_atr)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        out = dict(_empty)
        out["swing_highs"] = [int(i) + offset for i in swing_highs]
        out["swing_lows"]  = [int(i) + offset for i in swing_lows]
        return out

    # ── Dual-trendline approach (the core fix) ───────────────────────────────
    # Global: full-window OLS → R² quality gate (is the boundary broadly linear?)
    _, _, hi_global_r2 = _global_trendline(swing_highs, hi_vals)
    _, _, lo_global_r2 = _global_trendline(swing_lows,  lo_vals)

    # Local: last 3 pivots → slope reflects the CURRENT boundary direction
    hi_loc_slope, hi_loc_intercept, _ = _local_trendline(swing_highs, hi_vals, n_last=3)
    lo_loc_slope, lo_loc_intercept, _ = _local_trendline(swing_lows,  lo_vals, n_last=3)

    hi_local_slope = _classify_slope(hi_loc_slope, current_atr)
    lo_local_slope = _classify_slope(lo_loc_slope, current_atr)

    # Project local lines to current bar for breakout detection
    last_idx        = len(window_df) - 1
    hi_anchor_local = swing_highs[-3] if len(swing_highs) >= 3 else swing_highs[-2]
    lo_anchor_local = swing_lows[-3]  if len(swing_lows)  >= 3 else swing_lows[-2]
    upper_proj = _project(hi_loc_slope, hi_loc_intercept, hi_anchor_local, last_idx)
    lower_proj = _project(lo_loc_slope, lo_loc_intercept, lo_anchor_local, last_idx)

    breakout_up = current_price > upper_proj * (1 + breakout_pct)
    breakout_dn = current_price < lower_proj * (1 - breakout_pct)

    # ── Volume checks (run once, shared across patterns) ─────────────────────
    pattern_start_bar = min(swing_highs[0], swing_lows[0])
    vol_contraction   = _vol_contracting(window_df, pattern_start_bar)
    vol_expansion     = _vol_expanding_on_breakout(window_df) if (breakout_up or breakout_dn) else False

    # ── Pattern detectors ─────────────────────────────────────────────────────
    all_found: List[DetectedPattern] = []
    all_found += _detect_triangles(hi_local_slope, lo_local_slope, hi_global_r2, lo_global_r2, breakout_up, breakout_dn)
    all_found += _detect_wedges(   hi_local_slope, lo_local_slope, hi_global_r2, lo_global_r2, breakout_up, breakout_dn)
    all_found += _detect_rectangle(hi_local_slope, lo_local_slope, hi_global_r2, lo_global_r2, breakout_up, breakout_dn)
    all_found += _detect_hs(       swing_highs, swing_lows, hi_vals, lo_vals, current_price, breakout_pct)
    all_found += _detect_double_triple(swing_highs, swing_lows, hi_vals, lo_vals, breakout_up, breakout_dn)

    # ── Apply volume adjustments ──────────────────────────────────────────────
    all_vol_notes: List[str] = []
    for pat in all_found:
        adj_conf, vol_notes = _apply_volume_to_conf(
            pat.confidence, pat.breakout, vol_contraction, vol_expansion)
        pat.confidence   = round(adj_conf, 3)
        pat.volume_notes = vol_notes
        all_vol_notes.extend(vol_notes)

    # ── Trendline endpoints for chart drawing ─────────────────────────────────
    upper_ep = _trendline_endpoints(
        swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs[-2:],
        hi_loc_slope, hi_loc_intercept, hi_anchor_local, window_df)
    lower_ep = _trendline_endpoints(
        swing_lows[-3:] if len(swing_lows) >= 3 else swing_lows[-2:],
        lo_loc_slope, lo_loc_intercept, lo_anchor_local, window_df)
    upper_ep["r2"] = round(hi_global_r2, 3)
    lower_ep["r2"] = round(lo_global_r2, 3)
    upper_ep["slope_class"] = hi_local_slope
    lower_ep["slope_class"] = lo_local_slope

    # ── Pivot timestamps (for chart drawing) ──────────────────────────────────
    def _pivot_ts(local_indices, values, is_high: bool):
        out = []
        for i in local_indices:
            try:
                out.append({"ts": int(window_df.index[i].timestamp()),
                            "price": round(float(values[i]), 8),
                            "kind": "HIGH" if is_high else "LOW"})
            except IndexError:
                pass
        return out

    pivot_highs = _pivot_ts(swing_highs, hi_vals, True)
    pivot_lows  = _pivot_ts(swing_lows,  lo_vals, False)

    if not all_found:
        return {
            **_empty,
            "upper_trendline": upper_ep,
            "lower_trendline": lower_ep,
            "swing_highs": [int(i) + offset for i in swing_highs],
            "swing_lows":  [int(i) + offset for i in swing_lows],
            "pivot_highs": pivot_highs,
            "pivot_lows":  pivot_lows,
        }

    # ── Pick the best pattern ─────────────────────────────────────────────────
    actionable = [p for p in all_found if p.signal != "NEUTRAL"]
    best = (max(actionable, key=lambda p: p.confidence) if actionable
            else max(all_found, key=lambda p: p.confidence))

    return {
        "signal":          best.signal,
        "value":           round(best.confidence, 3),
        "pattern":         best.name,
        "bias":            best.bias,
        "category":        best.category,
        "breakout":        bool(best.breakout),
        "volume_confirmed": bool(vol_expansion if (breakout_up or breakout_dn) else vol_contraction),
        "volume_notes":    list(dict.fromkeys(all_vol_notes)),  # deduplicated
        "all_patterns":    [p.to_dict() for p in all_found],
        "swing_highs":     [int(i) + offset for i in swing_highs],
        "swing_lows":      [int(i) + offset for i in swing_lows],
        "pivot_highs":     pivot_highs,
        "pivot_lows":      pivot_lows,
        "upper_trendline": upper_ep,
        "lower_trendline": lower_ep,
        "breakout_up":     bool(breakout_up),
        "breakout_dn":     bool(breakout_dn),
    }
