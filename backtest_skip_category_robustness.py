"""
backtest_skip_category_robustness.py -- noise-vs-signal check on the 4 SKIP reason
categories that survived the earlier backtest without a traced mechanism (No
invalidation level found / Lacks confirmation / Structural support missing /
Other-price not at zone), plus a re-check of the 3 already-confirmed ones as a
sanity control.

Doesn't invent new statistical machinery -- reuses labeller.py's own noise-control
tools (_wilson_ci, _compute_k_map) exactly as tag_lift_report() already does, and
mirrors its in-sample/out-of-sample discipline: split each category chronologically
in half, and only call an edge real if it survives on the half it wasn't found on.
Standalone, read-only. Does not import dexter.py (unsafe -- see prior scripts).
"""
import json
import sys

sys.path.insert(0, r"C:\ChevTools")
from labeller import _wilson_ci, _compute_k_map, MIN_N_EFF  # noqa: E402


def load_jsonl(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def classify_chev_skip_reason(reason):
    """Verbatim copy of dexter.py's classifier (module-level, pure, no I/O) --
    dexter.py itself is unsafe to import (starts Flask/threads/resolver at load)."""
    r = (reason or "").lower()
    if ("too close" in r or "too tight" in r or "too near" in r
            or "noise floor" in r or "within the required distance" in r):
        return "INVALIDATION_TOO_CLOSE"
    if "invalidation" in r and ("lack" in r or "missing" in r or "no clear" in r or "not " in r):
        return "INVALIDATION_MISSING"
    if "r:r" in r or "round-trip cost" in r or "atr floor" in r:
        return "RISK_REWARD_OR_SIZING"
    if "confluence" in r and ("threshold" in r or "below" in r or "does not meet" in r
                              or "weak" in r or "insufficient" in r or "not strong enough" in r
                              or "not enough" in r):
        return "CONFLUENCE_BELOW_THRESHOLD"
    if "reach" in r and ("zone" in r or "level" in r or "price" in r):
        return "PRICE_NOT_AT_ZONE"
    if "confirmation" in r or "not yet confirmed" in r or "forming" in r:
        return "CONFIRMATION_MISSING"
    if ("structural support" in r or "structural anchor" in r or "validated sr" in r
            or "structural backstop" in r or "structural level" in r or "higher timeframe support" in r):
        return "STRUCTURAL_SUPPORT_MISSING"
    if "trend" in r or "counter-trend" in r:
        return "TREND_CONTEXT"
    return "OTHER"


def stats_for(recs, k_map):
    n = len(recs)
    wins = sum(1 for r in recs if r.get("label") == 1)
    eff_n = sum(1.0 / k_map.get(r["id"], 1) for r in recs)
    ci = _wilson_ci(n, wins)
    wr = wins / n if n else 0.0
    return n, wins, eff_n, wr, ci


def main():
    labels = load_jsonl(r"C:\ChevTools\labels_closed.jsonl")
    pop = [r for r in labels if r.get("chev_decision") == "SKIP" and r.get("resolved")
           and r.get("dexter_score", 0) >= 6 and r.get("label") is not None
           and r.get("id") and r.get("touched_ts")]
    pop.sort(key=lambda r: r["ts_epoch"])

    # SELF-AUDIT CORRECTION: v1 used the SKIP population's own average as the baseline
    # each category was compared against. That's contaminated -- a category that's 38%
    # of the whole population (INVALIDATION_TOO_CLOSE) can never look different from a
    # baseline that's mostly itself, which flipped it to a false "not robust" verdict
    # directly contradicting the independently-verified 486-record realistic-stop
    # re-simulation. Fixed: baseline is the EXTERNAL, non-contaminated NOT_ESCALATED
    # control at matched score (same population the original finding was validated
    # against), split into the same early/late halves so the comparison stays fair to
    # any baseline drift over time.
    ctrl = [r for r in labels if r.get("chev_decision") == "NOT_ESCALATED" and r.get("resolved")
            and r.get("dexter_score", 0) >= 6 and r.get("label") is not None]
    ctrl.sort(key=lambda r: r["ts_epoch"])
    ctrl_mid = len(ctrl) // 2
    ctrl_early, ctrl_late = ctrl[:ctrl_mid], ctrl[ctrl_mid:]
    br_early = sum(1 for r in ctrl_early if r.get("label") == 1) / len(ctrl_early)
    br_late  = sum(1 for r in ctrl_late  if r.get("label") == 1) / len(ctrl_late)
    print(f"External control (NOT_ESCALATED, score>=6) base rate -- EARLY: {br_early*100:.1f}% "
          f"(n={len(ctrl_early)})  LATE: {br_late*100:.1f}% (n={len(ctrl_late)})\n")

    k_map_full = _compute_k_map(pop)

    by_cat = {}
    for r in pop:
        by_cat.setdefault(classify_chev_skip_reason(r.get("chev_reason")), []).append(r)

    print("=" * 100)
    print(f"SKIP CATEGORY ROBUSTNESS CHECK  (external control base rate: early={br_early*100:.1f}%, "
          f"late={br_late*100:.1f}%, n_skip_pop={len(pop)}, MIN_N_EFF={MIN_N_EFF})")
    print("=" * 100)
    print(f"{'category':28s} {'half':6s} {'n':>5s} {'eff_N':>7s} {'wr':>6s} "
          f"{'CI_lo':>6s} {'CI_hi':>6s}  {'verdict'}")

    verdicts = {}
    for cat, recs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        recs = sorted(recs, key=lambda r: r["ts_epoch"])
        mid = len(recs) // 2
        early, late = recs[:mid], recs[mid:]

        row = {}
        for label, half in [("EARLY (in-sample)", early), ("LATE (out-of-sample)", late)]:
            n, wins, eff_n, wr, ci = stats_for(half, k_map_full)
            row[label] = (n, eff_n, wr, ci)
            flag = "OK" if eff_n >= MIN_N_EFF else f"eff_N<{MIN_N_EFF}"
            print(f"{cat:28s} {label[:6]:6s} {n:5d} {eff_n:7.1f} {wr*100:5.1f}% "
                  f"{ci[0]*100:5.1f}% {ci[1]*100:5.1f}%  {flag}")

        n_e, eff_e, wr_e, ci_e = row["EARLY (in-sample)"]
        n_l, eff_l, wr_l, ci_l = row["LATE (out-of-sample)"]
        both_enough_n = eff_e >= MIN_N_EFF and eff_l >= MIN_N_EFF
        # ACTIONABLE mirrors tag_lift_report's own rule: out-of-sample CI lower bound
        # must clear the base rate on its own -- not just the point estimate. Compared
        # against the EXTERNAL control's LATE base rate (br_late), not this category's
        # own population.
        actionable = both_enough_n and ci_l[0] > br_late
        stable = both_enough_n and abs(wr_e - wr_l) < 0.15  # no wild flip between halves
        if not both_enough_n:
            verdict = "INSUFFICIENT DATA (one half < MIN_N_EFF)"
        elif actionable and stable:
            verdict = "ROBUST — holds out-of-sample, worth fixing"
        elif actionable and not stable:
            verdict = "UNSTABLE — clears base rate but swings hard between halves"
        else:
            verdict = "NOT ROBUST — does not clear base rate out-of-sample (likely noise)"
        verdicts[cat] = verdict
        print(f"{'':28s} -> {verdict}")
        print("-" * 100)

    print("\n=== SUMMARY ===")
    for cat, v in verdicts.items():
        print(f"  {cat:28s} {v}")


if __name__ == "__main__":
    main()
