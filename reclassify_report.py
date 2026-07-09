"""
Phase 6 — read-only reclassification report. Read-only, no writes anywhere, does NOT
import dexter.py (it starts a live bot on import). Reads chev_journal.json directly.

Historical journal entries do not have a "partial_done"/"partial_net_pnl" field on the
final-close entry itself -- a partial close is written as its OWN separate journal
entry (outcome="PARTIAL_TP"). This script matches each PARTIAL_TP entry to its parent
trade's final close by (symbol, entry price, close ts >= partial ts, nearest match),
sums matched partials per trade, and applies the exact same rule as dexter.py's new
_classify_outcome(): LOSS -> SCRATCH if combined (partial + final leg) PnL is >= -0.1R,
LOSS -> WIN if combined PnL is >= 0R. Never touches the journal file. Run any time.

Run: python reclassify_report.py [days]   (default: last 30 days)
"""

import json
import sys
from datetime import datetime, timedelta, timezone

JOURNAL_PATH = r"C:\ChevTools\chev_journal.json"
SCRATCH_BAND_R = -0.1

days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
    journal = json.load(f)


def parse_ts(ts_str):
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


cutoff = datetime.now(timezone.utc) - timedelta(days=days)

closes_all    = [e for e in journal if e.get("outcome") in ("WIN", "LOSS")]
partials_all  = [e for e in journal if e.get("outcome") == "PARTIAL_TP"]
closes_window = [e for e in closes_all if (parse_ts(e.get("ts")) or cutoff) >= cutoff]

# Match each partial to the nearest same-(symbol, entry) close at or after it, then sum
# matched partial PnL per close entry (identified by object id -- journal has no trade id).
partial_pnl_by_close = {}
matches_report = []
for p in partials_all:
    p_ts = parse_ts(p.get("ts"))
    if p_ts is None:
        continue
    candidates = [
        c for c in closes_all
        if c.get("symbol") == p.get("symbol")
        and c.get("entry") == p.get("entry")
        and (parse_ts(c.get("ts")) or cutoff) >= p_ts
    ]
    candidates.sort(key=lambda c: c.get("ts", ""))
    if not candidates:
        matches_report.append((p, None))
        continue
    match = candidates[0]
    key = id(match)
    partial_pnl_by_close[key] = partial_pnl_by_close.get(key, 0.0) + float(p.get("pnl", 0) or 0)
    matches_report.append((p, match))

print("=" * 78)
print(f"PHASE 6 RECLASSIFICATION REPORT -- last {days} day(s), read-only, no writes")
print("=" * 78)

print(f"\nPartial-close matching ({len(partials_all)} PARTIAL_TP entries in full journal):")
for p, match in matches_report:
    if match is None:
        print(f"  {p['ts']} {p['symbol']} entry={p['entry']} pnl={p['pnl']:+.2f}  -> NO MATCHING CLOSE FOUND (skipped)")
    else:
        print(f"  {p['ts']} {p['symbol']} entry={p['entry']} pnl={p['pnl']:+.2f}  -> matched close {match['ts']} ({match['outcome']})")

# ---------------------------------------------------------------------------
# Apply the exact _classify_outcome rule to every LOSS in the window that has a
# matched partial.
# ---------------------------------------------------------------------------
reclassified = []
for c in closes_window:
    if c.get("outcome") != "LOSS":
        continue
    partial_pnl = partial_pnl_by_close.get(id(c), 0.0)
    if partial_pnl == 0.0:
        continue
    risk_usd = float(c.get("risk_amount_usd") or 0)
    if not risk_usd:
        continue
    pnl = float(c.get("pnl") or 0)
    combined_pnl = pnl + partial_pnl
    combined_r = combined_pnl / risk_usd
    if combined_r >= 0:
        new_outcome = "WIN"
    elif combined_r >= SCRATCH_BAND_R:
        new_outcome = "SCRATCH"
    else:
        continue
    reclassified.append((c, partial_pnl, combined_pnl, combined_r, new_outcome))

print(f"\nReclassifications found in the last {days} day(s): {len(reclassified)}")
for c, partial_pnl, combined_pnl, combined_r, new_outcome in reclassified:
    print(f"  {c['ts']} {c['symbol']} {c.get('direction','?').upper()} "
          f"final_leg=${c['pnl']:+.2f} partial=${partial_pnl:+.2f} "
          f"combined=${combined_pnl:+.2f} ({combined_r:+.2f}R) -> LOSS becomes {new_outcome}")

# ---------------------------------------------------------------------------
# Before / after summary over the window
# ---------------------------------------------------------------------------
wins_before   = sum(1 for c in closes_window if c["outcome"] == "WIN")
losses_before = sum(1 for c in closes_window if c["outcome"] == "LOSS")
total_before  = wins_before + losses_before
wr_before     = (wins_before / total_before * 100) if total_before else 0.0

new_wins    = sum(1 for _, _, _, _, o in reclassified if o == "WIN")
new_scratch = sum(1 for _, _, _, _, o in reclassified if o == "SCRATCH")
wins_after    = wins_before + new_wins
losses_after  = losses_before - len(reclassified)
scratches_after = new_scratch
total_after   = wins_after + losses_after   # scratches excluded from the denominator, same as live
wr_after      = (wins_after / total_after * 100) if total_after else 0.0

print(f"\n{'-'*78}")
print(f"BEFORE (current, as journaled):  {wins_before}W / {losses_before}L  "
      f"(n={total_before}, win rate {wr_before:.1f}%)")
print(f"AFTER  (reclassified):           {wins_after}W / {losses_after}L / {scratches_after} SCRATCH  "
      f"(n={total_after}, win rate {wr_after:.1f}%)")
print(f"{'-'*78}")
print("NOTE: this script never writes anything -- chev_journal.json is untouched.")
print("The live fix (dexter.py's _classify_outcome) applies going forward only, per")
print("the 'no retro-editing' rule -- this report is read-only visibility, not a migration.")
print("=" * 78)
