"""
weight_proposal.py — Confluence tag marginal-effect PROPOSAL report (READ-ONLY)

Standalone, pure-analysis script. Reads labels_closed.jsonl, writes exactly one
output file (weight_proposal_report.txt), and touches nothing else. No imports
from dexter.py, no reads of chev_journal.json, no network, no writes to any
existing file.

WHAT THIS ANSWERS: for each confluence tag (the "features" list on a shadow
record), what is its estimated MARGINAL contribution to net_R, holding every
other tag (and asset_type/trade_type as controls) constant? Raw per-tag totals
are misleading here — most tags co-occur constantly (ema13/ema21/bb_squeeze
show up on nearly every setup), so a univariate aggregate mostly just measures
the base rate of the whole population, not that tag's own effect. This uses a
Ridge regression across all tags simultaneously so each coefficient nets out
the shared overlap, and a bootstrap to know which effects are actually distinct
from zero rather than noise on a handful of trades.

THE POST BUCKET IS EXCLUDED FROM THE REGRESSION. POST shadow records use Chev's
real entry/direction; SKIP/NOT_ESCALATED use a mechanical mean-reversion-at-a-
touched-zone proxy. They are not the same population and mixing them would
confound the tag estimates with that structural difference (see
counterfactual_report.py's "POST — SHADOW REPLAY" section for the full story).

GUARDRAILS (from handoff.txt's brainstorm-item-6 discussion — a runaway feedback
loop was explicitly flagged as the risk here, same shape as the playbook self-
reinforcement bug found and fixed earlier in this project):
  (a) Minimum sample size (MIN_N) AND a bootstrap 95% CI that must not cross
      zero before a tag's effect is trusted at all.
  (b) A hard per-run ceiling (MAX_STEP) on how far any single proposal can move
      a tag's weight, regardless of how large the estimated effect looks.
  (c) This script never applies anything. It only prints a proposal. Applying a
      change to dexter.py's real confluence scoring is a manual, human step.

Run:            python -X utf8 weight_proposal.py
Self-test:      python -X utf8 weight_proposal.py --selftest
"""

import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np

DATA_DIR            = r"C:\ChevTools"
LABELS_CLOSED_FILE  = os.path.join(DATA_DIR, "labels_closed.jsonl")
OUTPUT_FILE         = os.path.join(DATA_DIR, "weight_proposal_report.txt")

# Read-time cap on cost_R, mirroring labeller.py's COST_R_CAP (kept as a local
# literal, not an import — this script stays standalone by design, same
# convention as counterfactual_report.py). NOTE: keep this in sync with the
# other two copies (labeller.py, counterfactual_report.py) if it ever changes.
COST_R_CAP = 0.50

# ── Guardrail (a): minimum sample + statistical significance ─────────────────
MIN_N = 30           # a tag with fewer occurrences than this is dropped before
                      # the regression even runs — not enough data to judge.

# ── Model constants ───────────────────────────────────────────────────────────
RIDGE_LAMBDA  = 1.0   # L2 penalty strength. Handles the heavy collinearity
                      # between tags (most co-occur constantly) far better
                      # than plain OLS, which would produce wild, unstable
                      # coefficients under near-perfect overlap.
BOOTSTRAP_B   = 1000  # resamples for the confidence interval
SEED          = 42    # fixed seed so runs are reproducible

# Informational only — shown alongside the raw coefficient so Kev can see what a
# naive linear scaling would have suggested vs. what compute_delta() below
# actually proposes.
SCALE = 10  # 1 point per 0.1R of estimated marginal effect

# ── Guardrail (b), decimal version (2026-07-16) ───────────────────────────────
# Original guardrail was "at most +/-1 point per run, sign-only" -- deliberately
# blunt to avoid a runaway feedback loop (see handoff.txt's brainstorm-item-6
# discussion). Kept the same SPIRIT -- small, capped, evidence-gated, still never
# auto-applies anything -- but fixed a real flaw found 2026-07-16: a flat 1-point
# step is a wildly different-sized bet depending on which tag it lands on. A tag
# worth 0.5 total (ema13) got wiped toward zero in one move; a tag worth 4 (gp)
# barely moved 25%. Two changes, both in compute_delta():
#   (a) the step is now sized as a PERCENTAGE of the tag's own current weight,
#       not a flat absolute number -- consistent relative risk across every tag.
#   (b) the step also scales down when the evidence is only just barely
#       significant (its confidence interval edge sits close to zero) and
#       toward the ceiling when the interval is comfortably clear of zero --
#       sized off the CONSERVATIVE (nearest-to-zero) edge of the interval,
#       never the point estimate. Same "judge a claim off its weakest defensible
#       number, not its best case" principle risk_gauntlet.py's own EV floor
#       already uses for tag win rates.
MAX_STEP            = 1     # unchanged: absolute ceiling on any single proposed move, still guardrail (b)'s hard cap
STEP_PCT_OF_WEIGHT  = 0.25  # ceiling: never propose moving more than 25% of a tag's OWN current value in one run
STEP_ABS_FLOOR       = 0.05  # rounding floor only, so a real (if weak) significant effect never proposes literally 0.0
STEP_EVIDENCE_REF    = 0.30  # a conservative (CI-edge) net-R/trade effect of this size or more counts as "full strength"; scales down linearly below it


def compute_delta(coef, lo, hi, current_weight):
    """Evidence- and scale-proportional weight-change proposal (replaces the old
    flat sign-only +/-1 step -- see the guardrail comment above). Returns a
    signed float, rounded to 0.01, sized as a percentage of current_weight and
    scaled by how far the confidence interval's conservative edge sits from
    zero. Returns 0.0 if current_weight is falsy/None (caller treats that the
    same as the old "no proposal" case)."""
    if not current_weight:
        return 0.0
    conservative = lo if coef > 0 else hi   # CI edge nearest zero -- the weakest defensible effect size
    strength = min(abs(conservative) / STEP_EVIDENCE_REF, 1.0)
    step = current_weight * STEP_PCT_OF_WEIGHT * strength
    step = max(STEP_ABS_FLOOR, min(step, MAX_STEP))
    return round(math.copysign(step, coef), 2)

# Fill this in BY HAND from dexter.py's current confluence scoring point values
# (see scan_pair_tf() in dexter.py for where each tag's points are assigned).
# Any tag not listed here is still analyzed and reported on, but no weight
# change will ever be proposed for it — deliberately: this script cannot know
# what's "current" unless told, and guessing would defeat the point of the
# human-approval guardrail.
#
# UNMAPPED — needs Kev's decision (found in dexter.py/derivs.py, but not a
# single unambiguous point value, so deliberately left out of CURRENT_WEIGHTS):
#   gp_sr_combo    — dexter.py scan_pair_tf(): "★★ GP×SR DEADLY COMBO" is a
#                     ×1.15 MULTIPLIER on total_score, not an additive point
#                     value. Nothing to put in a per-tag weight table.
#   bb_burst       — _bb_base = {"4h":3,"1h":2,"30m":2,"15m":1}.get(primary_tf,2)
#                     — depends on which TF is primary when it fires, not fixed.
#   vp_poc/vp_vah/vp_val — _vp_base is 3(4h)/2(1h), VAH/VAL = _vp_base-1, AND
#                     halved again if the VP anchor is unconfirmed. Two
#                     independent dimensions of variation, no single value.
#   rsi_div_regular, rsi_div_hidden, rsi_div, rsi_div_forming — all four route
#                     through _div_strength_score(d, primary_tf, confirmed=?),
#                     a dynamic function of divergence magnitude/TF — not a
#                     fixed point table entry anywhere in dexter.py.
#   pattern_mid_conf — genuinely ambiguous: the labeller token fires for BOTH
#                     a forming pattern with confidence 0.55-0.69 (dexter
#                     awards 1.0pt) AND a volume-confirmed pattern with no
#                     breakout at any confidence (dexter awards 1.5pt) —
#                     normalize_reasons() can't tell these apart from the
#                     string alone, so the token covers two different real
#                     weights. (pattern_high_conf was checked the same way and
#                     turned out safe — both of its trigger paths score 1.5 —
#                     which is why it IS included below.)
#   funding_extreme — derivs.py classify_derivs(): 2.0pt at the "notable"
#                     threshold, 3.0pt at the "extreme" threshold — tiered,
#                     not one number.
#   fib_382, fib_236 — dexter's fib engine (_ca_fib_from_real_impulse) only
#                     ever computes 50%, 61.8%, 65%, and 78.6% — it never
#                     emits a 38.2% or 23.6% level under current code, so
#                     there is no live point value to record for these two
#                     labeller.py REASON_MAP entries at all (they appear to be
#                     dead/legacy tokens).
#   other           — catch-all for any unmatched reason string by definition;
#                     not one feature, so not weight-eligible.
CURRENT_WEIGHTS = {
    # ── Support / Resistance (scan_pair_tf(): "Resistance/Support(Nx,Npt)") ──
    "sr_multi":  3,    # instances >= 3 -> pts = 3
    "sr_single": 2,    # instances < 3 (1-2 touches) -> pts = 2

    # ── Golden Pocket ─────────────────────────────────────────────────────
    # No explicit "(Npt)" text in the "★ GOLDEN POCKET" reason string itself
    # (dexter internally bumps fib_score to 3 as the *mechanism*, not a
    # separate gp_score var) — falls back to CONFLUENCE_SCORES["gp"] per this
    # phase's own precedence rule. Already Kev-confirmed as the same
    # computation, not a name collision (see WEIGHT_LAB_VERIFIED_TAGS above).
    "gp": 4,

    # ── Fibonacci (scan_pair_tf() fib branch) ────────────────────────────
    "fib_618":  2,     # "Fib 61.8% (golden pocket) (2pt)"
    "fib_50":   2,     # "Fib 50% (2pt)"
    "fib_786":  1,     # else-branch: fib_score += 1 (string has no inline "(Npt)", but the literal is unambiguous)
    "fib_other": 1,    # same else-branch; in practice only ever catches the "65%" ratio (the one computed level with no dedicated REASON_MAP entry)

    # ── EMA (scan_pair_tf() EMA branch) ──────────────────────────────────
    "ema55":     2.0,  # "EMA55 support/resistance (2.0pt)"
    "ema21":     1.0,  # "EMA21 support/resistance (1.0pt)"
    "ema13":     0.5,  # "EMA13 support/resistance (0.5pt)"
    "ema_cross": 1,    # "EMA crossover ... Xc ago" -> ema_score += 1 flat

    # ── Bollinger Bands ───────────────────────────────────────────────────
    "bb_near":    0.5, # "BB near upper/lower (...0.5pt)"
    "bb_mid":     1.0, # "BB mid support/resistance (...1pt)"
    "bb_squeeze": 0,   # "BB squeeze (..., context, 0pt)" -- explicitly unscored

    # ── RSI level signals ─────────────────────────────────────────────────
    "rsi_ob":       0.5, # "RSI OVERBOUGHT (X, 0.5pt)"; = CONFLUENCE_SCORES["rsi_ob"], WEIGHT_LAB_VERIFIED_TAGS confirmed
    "rsi_os":       0.5, # "RSI OVERSOLD (X, 0.5pt)"; = CONFLUENCE_SCORES["rsi_os"], WEIGHT_LAB_VERIFIED_TAGS confirmed
    "rsi_50_cross": 0,   # explicitly commented "context for Chev, not a scored confluence" -- never added to rsi_level_score

    # ── Liquidity sweep ───────────────────────────────────────────────────
    "sweep": 3,        # "Sweep:buy_side/sell_side (3pt)"

    # ── Chart patterns (only the two branches verified unambiguous) ──────
    "pattern_breakout":     2.0, # breakout, no volume -> pattern_score = 2.0
    "pattern_breakout_vol": 3.0, # breakout + volume_confirmed -> pattern_score = 3.0
    "pattern_high_conf":    1.5, # both trigger paths (conf>=0.70 forming, OR volume-confirmed alone) score 1.5 -- verified safe despite covering two dexter branches

    # ── Derivatives (Binance futures, derivs.py classify_derivs()) ───────
    "oi_divergence": 1.5, # both OI-divergence branches (px up/oi down, px down/oi down) score 1.5
    "oi_confirm":    1.0, # both OI-confirmation branches (px up/oi up, px down/oi up) score 1.0

    # ── Fast intraday structural anchors ──────────────────────────────────
    # All four explicitly "(X% away, 0pt, context)" -- gate a struct pre-check,
    # never added to sr_score/vp_score/total_score.
    "fast_anchor_pdh":     0,
    "fast_anchor_pdl":     0,
    "fast_anchor_or_high": 0,
    "fast_anchor_or_low":  0,

    # ── Watch-only signals ────────────────────────────────────────────────
    "watch_signal": 0, # GP-approach reasons; explicitly commented "Weight: 0pt -- preparation signal only, never an entry trigger"
}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ──────────────────────────────────────────────────────────────────────────────
# Data loading / preparation
# ──────────────────────────────────────────────────────────────────────────────

def load_records(path):
    """Line-by-line JSONL load. Returns (records, malformed_count)."""
    records = []
    malformed = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                malformed += 1
    return records, malformed


def prepare_sample(records):
    """
    Returns (kept_records, n_excluded_post, n_excluded_incomplete).
    kept_records are non-POST, have a non-null realized_R, and a non-empty
    'features' list.
    """
    n_excluded_post = 0
    n_excluded_incomplete = 0
    kept = []
    for rec in records:
        if rec.get("chev_decision") == "POST":
            n_excluded_post += 1
            continue
        if rec.get("realized_R") is None:
            n_excluded_incomplete += 1
            continue
        tags = rec.get("features")
        if not tags:
            n_excluded_incomplete += 1
            continue
        kept.append(rec)
    return kept, n_excluded_post, n_excluded_incomplete


def net_r(rec):
    realized = rec["realized_R"]
    cost = min(rec.get("cost_R") or 0.0, COST_R_CAP)
    return realized - cost


def most_common(values):
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def build_design_matrix(records):
    """
    Returns (X, y, tag_names, tag_counts, dropped_tags, control_names,
             asset_ref, trade_ref).
    Column order: [intercept, tag_1, ..., tag_k, asset_dummy_1, ..., trade_dummy_1, ...]
    """
    y = np.array([net_r(r) for r in records], dtype=float)

    tag_counts = {}
    for r in records:
        for t in r["features"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    dropped_tags = {t: n for t, n in tag_counts.items() if n < MIN_N}
    tag_names = sorted(t for t, n in tag_counts.items() if n >= MIN_N)

    asset_types  = [r.get("asset_type") or "unknown" for r in records]
    trade_types  = [r.get("trade_type") or "unknown" for r in records]
    asset_ref    = most_common(asset_types)   # reference/baseline category (dropped)
    trade_ref    = most_common(trade_types)   # reference/baseline category (dropped)
    asset_levels = sorted(set(asset_types) - {asset_ref})
    trade_levels = sorted(set(trade_types) - {trade_ref})

    n = len(records)
    ncols = 1 + len(tag_names) + len(asset_levels) + len(trade_levels)
    X = np.zeros((n, ncols), dtype=float)
    X[:, 0] = 1.0  # intercept

    tag_idx = {t: 1 + i for i, t in enumerate(tag_names)}
    asset_idx = {lvl: 1 + len(tag_names) + i for i, lvl in enumerate(asset_levels)}
    trade_idx = {lvl: 1 + len(tag_names) + len(asset_levels) + i for i, lvl in enumerate(trade_levels)}

    for row, r in enumerate(records):
        for t in r["features"]:
            j = tag_idx.get(t)
            if j is not None:
                X[row, j] = 1.0
        a = r.get("asset_type") or "unknown"
        if a in asset_idx:
            X[row, asset_idx[a]] = 1.0
        tt = r.get("trade_type") or "unknown"
        if tt in trade_idx:
            X[row, trade_idx[tt]] = 1.0

    control_names = [("asset", lvl) for lvl in asset_levels] + [("trade_type", lvl) for lvl in trade_levels]

    return X, y, tag_names, tag_counts, dropped_tags, control_names, asset_ref, trade_ref


def ridge_fit(X, y, lam):
    """beta = inv(X'X + lam*I) @ X'y, intercept (column 0) unpenalized."""
    ncols = X.shape[1]
    penalty = np.eye(ncols) * lam
    penalty[0, 0] = 0.0  # do not penalize the intercept
    xtx = X.T @ X
    xty = X.T @ y
    beta = np.linalg.solve(xtx + penalty, xty)
    return beta


def bootstrap_ci(X, y, lam, n_tag_cols, rng, B):
    """
    Returns array of shape (B, n_tag_cols+1) — bootstrap coefficients for the
    intercept + tag columns only (controls are refit each time but not
    returned; they're informational, not proposal-eligible).
    Actually returns full beta per resample so callers can pick any column.
    """
    n = X.shape[0]
    ncols = X.shape[1]
    draws = np.empty((B, ncols), dtype=float)
    for b in range(B):
        idx = rng.integers(0, n, size=n)
        Xb, yb = X[idx], y[idx]
        draws[b] = ridge_fit(Xb, yb, lam)
    return draws


# ──────────────────────────────────────────────────────────────────────────────
# Report building
# ──────────────────────────────────────────────────────────────────────────────

def build_report(records_raw_count, malformed, n_excluded_post, n_excluded_incomplete,
                  records, X, y, tag_names, tag_counts, dropped_tags, control_names,
                  asset_ref, trade_ref, beta, boot_draws):
    lines = []
    lines.append("=" * 78)
    lines.append("CONFLUENCE TAG WEIGHT PROPOSAL REPORT (PROPOSAL ONLY)")
    lines.append("=" * 78)
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append("")
    lines.append("-" * 78)
    lines.append("SAMPLE SUMMARY")
    lines.append("-" * 78)
    lines.append(f"labels_closed.jsonl records read : {records_raw_count}")
    lines.append(f"malformed lines skipped           : {malformed}")
    lines.append(f"excluded (chev_decision == POST)  : {n_excluded_post}")
    lines.append(f"excluded (no realized_R / no tags): {n_excluded_incomplete}")
    lines.append(f"used in regression                : {len(records)}")
    ts_values = [r["ts"] for r in records if r.get("ts")]
    if ts_values:
        lines.append(f"date range (ts field)             : {min(ts_values)}  to  {max(ts_values)}")
    lines.append(f"tags dropped (n < MIN_N={MIN_N})        : {len(dropped_tags)}")
    if dropped_tags:
        dropped_str = ", ".join(f"{t}(n={n})" for t, n in sorted(dropped_tags.items(), key=lambda kv: -kv[1]))
        lines.append(f"  {dropped_str}")
    lines.append(f"tags retained in regression        : {len(tag_names)}")
    lines.append("")

    ci_lo = np.percentile(boot_draws, 2.5, axis=0)
    ci_hi = np.percentile(boot_draws, 97.5, axis=0)

    lines.append("-" * 78)
    lines.append("TAG MARGINAL EFFECTS (Ridge, all tags + controls fit simultaneously)")
    lines.append("-" * 78)
    header = f"{'TAG':<22}{'N':>6}{'COEF':>9}{'95% CI':>20}{'SIG?':>6}{'CUR_W':>8}{'PROPOSAL':>10}"
    lines.append(header)
    rows = []
    for t in tag_names:
        j = 1 + tag_names.index(t)
        coef = beta[j]
        lo, hi = ci_lo[j], ci_hi[j]
        significant = (lo > 0) or (hi < 0)
        n = tag_counts[t]
        current_w = CURRENT_WEIGHTS.get(t)
        if significant and current_w is not None:
            delta = clamp(1 if coef > 0 else -1, -MAX_STEP, MAX_STEP)
            delta_str = f"{delta:+d}"
        else:
            delta_str = "—"
        rows.append((abs(coef), t, n, coef, lo, hi, significant, current_w, delta_str))

    rows.sort(key=lambda r: -r[0])
    for _, t, n, coef, lo, hi, significant, current_w, delta_str in rows:
        cur_str = f"{current_w:.2f}" if current_w is not None else "n/a"
        ci_str = f"[{lo:+.3f},{hi:+.3f}]"
        sig_str = "Y" if significant else "n"
        raw_suggestion = coef * SCALE
        lines.append(f"{t:<22}{n:>6}{coef:>+9.4f}{ci_str:>20}{sig_str:>6}{cur_str:>8}{delta_str:>10}")
    lines.append("")
    lines.append("(COEF = estimated net_R per trade when this tag is present, holding all other")
    lines.append(" tags and asset_type/trade_type constant. SIG = 95% bootstrap CI excludes zero.")
    lines.append(" PROPOSAL is capped at +/-1 point per run regardless of coefficient size —")
    lines.append(f" informational 'raw suggestion' (coef x SCALE={SCALE}) is NOT what's proposed,")
    lines.append(" only the sign-capped delta is. 'n/a' current weight = tag not in")
    lines.append(" CURRENT_WEIGHTS, so no proposal is made even if significant.)")
    lines.append("")

    lines.append("-" * 78)
    lines.append("CONTROLS (asset_type / trade_type — informational only, never proposed)")
    lines.append("-" * 78)
    lines.append(f"reference (dropped) categories: asset_type={asset_ref!r}, trade_type={trade_ref!r}")
    n_tags = len(tag_names)
    for i, (kind, lvl) in enumerate(control_names):
        j = 1 + n_tags + i
        coef = beta[j]
        lo, hi = ci_lo[j], ci_hi[j]
        significant = (lo > 0) or (hi < 0)
        lines.append(f"  {kind:<12}{lvl:<12}coef={coef:+.4f}  CI=[{lo:+.3f},{hi:+.3f}]  sig={'Y' if significant else 'n'}")
    lines.append("")

    lines.append("=" * 78)
    lines.append("PROPOSAL ONLY. Nothing has been changed. Apply by hand in dexter.py if")
    lines.append("approved. Do not re-run on data collected after applying a change without")
    lines.append("freezing a new window.")
    lines.append("=" * 78)
    return "\n".join(lines) + "\n"


def run_real_report():
    records_raw, malformed = load_records(LABELS_CLOSED_FILE)
    kept, n_post, n_incomplete = prepare_sample(records_raw)
    X, y, tag_names, tag_counts, dropped_tags, control_names, asset_ref, trade_ref = build_design_matrix(kept)
    beta = ridge_fit(X, y, RIDGE_LAMBDA)
    rng = np.random.default_rng(SEED)
    boot_draws = bootstrap_ci(X, y, RIDGE_LAMBDA, len(tag_names), rng, BOOTSTRAP_B)
    report = build_report(len(records_raw), malformed, n_post, n_incomplete,
                           kept, X, y, tag_names, tag_counts, dropped_tags, control_names,
                           asset_ref, trade_ref, beta, boot_draws)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Wrote {OUTPUT_FILE}")


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — synthetic data only, no file reads
# ──────────────────────────────────────────────────────────────────────────────

def _make_synthetic_records(rng, n=500):
    records = []
    asset_choices = ["crypto", "forex"]
    trade_choices = ["scalp", "day"]
    for i in range(n):
        has_good  = rng.random() < 0.5     # ~250 occurrences -> survives MIN_N
        has_noise = rng.random() < 0.5     # ~250 occurrences -> survives MIN_N, zero true effect
        has_rare  = i < 5                  # exactly 5 occurrences -> dropped by MIN_N
        tags = []
        if has_good:  tags.append("good")
        if has_noise: tags.append("noise")
        if has_rare:  tags.append("rare")
        if not tags:
            tags.append("filler")  # every real record has a non-empty features list
        base = float(rng.normal(0, 0.5))
        net = base
        if has_good:  net += 0.4
        if has_rare:  net += 2.0
        # 'noise' contributes nothing by construction
        records.append({
            "chev_decision": "NOT_ESCALATED",
            "realized_R": net,
            "cost_R": 0.0,
            "features": tags,
            "asset_type": asset_choices[i % 2],
            "trade_type": trade_choices[(i // 2) % 2],
            "ts": None,
        })
    return records


def run_selftest():
    rng = np.random.default_rng(SEED)
    records = _make_synthetic_records(rng, n=500)
    kept, n_post, n_incomplete = prepare_sample(records)
    X, y, tag_names, tag_counts, dropped_tags, control_names, asset_ref, trade_ref = build_design_matrix(kept)
    beta = ridge_fit(X, y, RIDGE_LAMBDA)
    boot_rng = np.random.default_rng(SEED)
    boot_draws = bootstrap_ci(X, y, RIDGE_LAMBDA, len(tag_names), boot_rng, BOOTSTRAP_B)
    ci_lo = np.percentile(boot_draws, 2.5, axis=0)
    ci_hi = np.percentile(boot_draws, 97.5, axis=0)

    results = []

    # "rare" must be dropped by MIN_N before it ever reaches the regression
    rare_dropped = "rare" in dropped_tags and "rare" not in tag_names
    results.append(("rare dropped by MIN_N (n=5 < 30)", rare_dropped))

    if "good" in tag_names:
        j = 1 + tag_names.index("good")
        coef, lo, hi = beta[j], ci_lo[j], ci_hi[j]
        good_significant_positive = (lo > 0) and (coef > 0.2)
        results.append((f"'good' significant & positive (coef={coef:+.3f}, CI=[{lo:+.3f},{hi:+.3f}])",
                         good_significant_positive))
    else:
        results.append(("'good' present in regression", False))

    if "noise" in tag_names:
        j = 1 + tag_names.index("noise")
        coef, lo, hi = beta[j], ci_lo[j], ci_hi[j]
        noise_not_significant = not ((lo > 0) or (hi < 0))
        results.append((f"'noise' NOT significant (coef={coef:+.3f}, CI=[{lo:+.3f},{hi:+.3f}])",
                         noise_not_significant))
    else:
        results.append(("'noise' present in regression", False))

    print(f"[weight_proposal] SELF-TEST {sum(1 for _, ok in results if ok)}/{len(results)}")
    all_ok = True
    for label, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
        all_ok = all_ok and ok
    return all_ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        ok = run_selftest()
        raise SystemExit(0 if ok else 1)
    run_real_report()
