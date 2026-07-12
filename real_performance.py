"""
real_performance.py — canonical, era-aware real-performance readout (READ-ONLY)

Standalone, pure-analysis script. Reads chev_journal.json only (read-only —
never writes to it), writes exactly one output file
(real_performance_report.txt), and touches nothing else. No imports from
dexter.py, honest_sim.py, labeller.py, or weight_proposal.py.

WHY THIS EXISTS: two conflicting "real average R" numbers surfaced in the same
conversation — one from the 31-33 trades carrying a system-computed r_multiple
field, one back-derived as pnl/(position_size_usd x stop_pct) using the
journal's CURRENT `sl` field. The second number is unsound on its own: this
system TRAILS stops (breakeven and better exist in the journal — 11 records
have sl == entry exactly), so for any managed trade the current `sl` no longer
reflects the risk that was actually taken, and older records have no
sl_original to fall back on to recover the true original risk. This script
never derives R from the current `sl` field. R is claimed ONLY where an
authoritative record of the risk actually taken exists.

STRICT PRIORITY LADDER (see classify_r) — each trade's R (or UNRELIABLE) comes
from exactly one rung, recorded so the report can show its own math:
  1. r_multiple field present -> use verbatim (authoritative, from honest_sim).
  2. else risk_amount_usd present and > 0 -> R = pnl / risk_amount_usd.
  3. else sl_original present and != entry -> R = pnl / (position_size_usd x
     |entry - sl_original| / entry). NOTE: as of this writing, sl_original does
     not exist anywhere in the journal (confirmed in recon) -- this rung is
     dead code today, kept only in case that field is ever added later.
  4. else -> UNRELIABLE. Counted, but EXCLUDED from every R average. Never
     derived from the current (possibly trailed) `sl` field.

Run:            python -X utf8 real_performance.py
Self-test:      python -X utf8 real_performance.py --selftest
"""

import json
import os
import sys
from datetime import datetime, timezone

DATA_DIR     = r"C:\ChevTools"
JOURNAL_FILE = os.path.join(DATA_DIR, "chev_journal.json")
OUTPUT_FILE  = os.path.join(DATA_DIR, "real_performance_report.txt")


def load_journal(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def classify_r(rec):
    """Strict priority ladder. Returns (r_value_or_None, rung) where rung is
    1/2/3 (a value was derivable) or 4 (UNRELIABLE, r_value is None)."""
    r_multiple = rec.get("r_multiple")
    if r_multiple is not None:
        return float(r_multiple), 1

    risk_amount_usd = rec.get("risk_amount_usd")
    pnl = rec.get("pnl")
    if risk_amount_usd is not None and risk_amount_usd > 0 and pnl is not None:
        return pnl / risk_amount_usd, 2

    sl_original = rec.get("sl_original")
    entry = rec.get("entry")
    position_size_usd = rec.get("position_size_usd")
    if (sl_original is not None and entry is not None and sl_original != entry
            and position_size_usd is not None and pnl is not None and entry != 0):
        risk_usd = position_size_usd * abs(entry - sl_original) / entry
        if risk_usd > 0:
            return pnl / risk_usd, 3

    return None, 4


def outcome_bucket(rec):
    """Normalizes the 'outcome' field. PARTIAL_TP is its own bucket -- never
    folded into WIN or LOSS."""
    o = (rec.get("outcome") or "").upper()
    if "PARTIAL" in o:
        return "PARTIAL_TP"
    if o == "WIN":
        return "WIN"
    if o == "LOSS":
        return "LOSS"
    return "OTHER"


def build_overall_dollars(records):
    n = len(records)
    total_pnl = sum(r.get("pnl") or 0 for r in records)
    by_outcome = {"WIN": 0, "LOSS": 0, "PARTIAL_TP": 0, "OTHER": 0}
    for r in records:
        by_outcome[outcome_bucket(r)] += 1
    decided = by_outcome["WIN"] + by_outcome["LOSS"]
    win_rate = (by_outcome["WIN"] / decided * 100.0) if decided else None
    return {"n": n, "total_pnl": round(total_pnl, 2), "by_outcome": by_outcome, "win_rate": win_rate}


def build_per_era(records):
    eras = {}
    for r in records:
        era = r.get("system_era") or "pre-tracking"
        d = eras.setdefault(era, {
            "n": 0, "reliable_rs": [], "total_pnl": 0.0,
            "by_outcome": {"WIN": 0, "LOSS": 0, "PARTIAL_TP": 0, "OTHER": 0},
            "rung_counts": {1: 0, 2: 0, 3: 0, 4: 0},
        })
        d["n"] += 1
        d["total_pnl"] += r.get("pnl") or 0
        d["by_outcome"][outcome_bucket(r)] += 1
        r_val, rung = classify_r(r)
        d["rung_counts"][rung] += 1
        if r_val is not None:
            d["reliable_rs"].append(r_val)

    out = {}
    for era, d in eras.items():
        rs = d["reliable_rs"]
        decided = d["by_outcome"]["WIN"] + d["by_outcome"]["LOSS"]
        out[era] = {
            "n": d["n"], "n_reliable": len(rs),
            "avg_r": (sum(rs) / len(rs)) if rs else None,
            "total_r": sum(rs) if rs else None,
            "win_rate": (d["by_outcome"]["WIN"] / decided * 100.0) if decided else None,
            "total_pnl": round(d["total_pnl"], 2),
            "rung_counts": d["rung_counts"],
        }
    return out


def build_real_performance_dict(records):
    """Single source of truth for both the .txt report and dexter.py's
    /api/real_performance route (mirrors counterfactual_report.py's
    build_counterfactual() pattern) -- pure, no I/O beyond what the caller
    already loaded."""
    overall = build_overall_dollars(records)
    per_era = build_per_era(records)

    all_reliable_rs = []
    for r in records:
        r_val, _ = classify_r(r)
        if r_val is not None:
            all_reliable_rs.append(r_val)

    pre = per_era.get("pre-tracking", {"n": 0, "n_reliable": 0})
    n_pretracking_excluded = pre["n"] - pre["n_reliable"]
    reliable_avg = (sum(all_reliable_rs) / len(all_reliable_rs)) if all_reliable_rs else None

    return {
        "overall": overall,
        "per_era": per_era,
        "headline": {
            "total_pnl": overall["total_pnl"],
            "n_total": overall["n"],
            "n_reliable": len(all_reliable_rs),
            "avg_r_reliable": round(reliable_avg, 4) if reliable_avg is not None else None,
            "n_pretracking_excluded": n_pretracking_excluded,
        },
    }


def build_report(records):
    data = build_real_performance_dict(records)
    overall = data["overall"]
    per_era = data["per_era"]

    lines = []
    lines.append("=" * 78)
    lines.append("REAL PERFORMANCE REPORT (era-aware, R claimed only where recorded)")
    lines.append("=" * 78)
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    lines.append("")

    lines.append("-" * 78)
    lines.append("OVERALL DOLLARS (all records -- no R needed)")
    lines.append("-" * 78)
    lines.append(f"n = {overall['n']}   total pnl = ${overall['total_pnl']:+.2f}")
    bo = overall["by_outcome"]
    lines.append(f"WIN={bo['WIN']}  LOSS={bo['LOSS']}  PARTIAL_TP={bo['PARTIAL_TP']}  OTHER={bo['OTHER']}")
    if overall["win_rate"] is not None:
        lines.append(f"win rate (WIN / (WIN+LOSS); PARTIAL_TP excluded from this ratio) = {overall['win_rate']:.1f}%")
    lines.append("")

    lines.append("-" * 78)
    lines.append("PER-ERA (system_era; None grouped as 'pre-tracking' -- NOT necessarily")
    lines.append("older, just untagged; ranges can overlap tagged records in time)")
    lines.append("-" * 78)
    lines.append(f"{'ERA':<28}{'N':>5}{'N_REL':>7}{'AVG_R':>9}{'TOTAL_R':>9}{'WIN%':>7}{'PNL':>12}  RUNGS(1/2/3/4)")
    for era, d in per_era.items():
        avg_r_str = f"{d['avg_r']:+.3f}" if d["avg_r"] is not None else "n/a"
        total_r_str = f"{d['total_r']:+.2f}" if d["total_r"] is not None else "n/a"
        wr_str = f"{d['win_rate']:.1f}" if d["win_rate"] is not None else "n/a"
        rc = d["rung_counts"]
        lines.append(f"{era:<28}{d['n']:>5}{d['n_reliable']:>7}{avg_r_str:>9}{total_r_str:>9}{wr_str:>7}{d['total_pnl']:>+12.2f}  {rc[1]}/{rc[2]}/{rc[3]}/{rc[4]}")
    lines.append("")

    lines.append("-" * 78)
    lines.append("HEADLINE")
    lines.append("-" * 78)
    hl = data["headline"]
    reliable_avg_str = f"{hl['avg_r_reliable']:+.3f}" if hl["avg_r_reliable"] is not None else "n/a"
    lines.append(f"Whole history: ${hl['total_pnl']:+.2f} across {hl['n_total']} trades.")
    lines.append(f"Reliable-R subset (n={hl['n_reliable']}): avg R = {reliable_avg_str}.")
    lines.append(f"Pre-tracking era excluded from R claims: {hl['n_pretracking_excluded']} trades.")
    lines.append("")

    lines.append("=" * 78)
    lines.append("Read-only report. R is only claimed where risk was actually recorded.")
    lines.append("=" * 78)
    return "\n".join(lines) + "\n"


def run_real_report():
    records = load_journal(JOURNAL_FILE)
    report = build_report(records)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Wrote {OUTPUT_FILE}")


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — synthetic data only, no file reads
# ──────────────────────────────────────────────────────────────────────────────

def run_selftest():
    failures = []

    def check(label, cond):
        ok = bool(cond)
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if not ok:
            failures.append(label)

    # 1. r_multiple wins even when the other fields would give a WILDLY
    # different answer (0.4 via rung 3, ~0.001 via rung 2) -- proves priority,
    # not coincidence.
    rec1 = {"r_multiple": 2.0, "risk_amount_usd": 999.0, "pnl": 1.0,
            "entry": 100.0, "sl_original": 50.0, "position_size_usd": 5.0}
    r_val1, rung1 = classify_r(rec1)
    check(f"rung 1 (r_multiple) used verbatim despite disagreeing fields (got r={r_val1}, rung={rung1})",
          r_val1 == 2.0 and rung1 == 1)

    # 2. rung 2 computes pnl/risk_amount_usd correctly
    rec2 = {"risk_amount_usd": 50.0, "pnl": 25.0}
    r_val2, rung2 = classify_r(rec2)
    check(f"rung 2 computes pnl/risk_amount_usd correctly (got r={r_val2}, rung={rung2})",
          r_val2 == 0.5 and rung2 == 2)

    # 3. Trailed/breakeven record (sl == entry), no sl_original, no
    # risk_amount_usd -> UNRELIABLE, excluded -- never derived from current sl.
    rec3 = {"entry": 100.0, "sl": 100.0, "position_size_usd": 1000.0, "pnl": 10.0}
    r_val3, rung3 = classify_r(rec3)
    check(f"trailed/breakeven record with no authoritative field -> UNRELIABLE (got r={r_val3}, rung={rung3})",
          r_val3 is None and rung3 == 4)

    # 4. PARTIAL_TP is its own bucket, never WIN or LOSS
    check("PARTIAL_TP bucket != WIN", outcome_bucket({"outcome": "PARTIAL_TP"}) != "WIN")
    check("PARTIAL_TP bucket != LOSS", outcome_bucket({"outcome": "PARTIAL_TP"}) != "LOSS")
    check("PARTIAL_TP correctly bucketed as its own category",
          outcome_bucket({"outcome": "PARTIAL_TP"}) == "PARTIAL_TP")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return False
    print("ALL SELFTEST CASES PASSED")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        ok = run_selftest()
        raise SystemExit(0 if ok else 1)
    run_real_report()
