"""
backtest_chev_skips.py -- forensic backtest of Chev SKIP decisions.

Standalone, read-only. Does NOT import dexter.py (it starts a Flask server,
a price-update thread, and the labeller resolver daemon unconditionally at
module load -- unsafe to import). All data used here is already on disk,
produced by labeller.py's live resolver: labels_closed.jsonl holds the
Examiner's triple-barrier resolution (real forward price action) for every
shadow setup, including every Chev SKIP. This script joins that against
chev_decisions.jsonl (Chev's full free-text reasoning) and reports where
SKIPs were vindicated vs. where price went on to do what the setup called for.
"""
import json
import bisect
import statistics
from collections import defaultdict, Counter
from datetime import datetime, timezone

LABELS_CLOSED = r"C:\ChevTools\labels_closed.jsonl"
DECISIONS_LOG = r"C:\ChevTools\chev_decisions.jsonl"
LAST_N        = 1000
MIN_TAG_N     = 20  # minimum sample size before a tag's lift is reported


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def build_decision_index(decisions):
    idx = defaultdict(list)
    for d in decisions:
        if d.get("decision") != "SKIP":
            continue
        t = parse_ts(d.get("ts", ""))
        if t is None:
            continue
        idx[(d.get("symbol"), d.get("tf"))].append((t, d))
    for k in idx:
        idx[k].sort(key=lambda x: x[0])
    return idx


def nearest_decision(idx, symbol, tf, ts_epoch, tol=120):
    lst = idx.get((symbol, tf))
    if not lst:
        return None
    times = [t for t, _ in lst]
    i = bisect.bisect_left(times, ts_epoch)
    best, best_diff = None, tol + 1
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(lst):
            t, d = lst[j]
            diff = abs(t - ts_epoch)
            if diff < best_diff:
                best_diff, best = diff, d
    return best if best_diff <= tol else None


# Keyword probe: did Chev's own free text seem to acknowledge this tag existed,
# or does it look like it went unmentioned? Rough heuristic, not exact parsing.
TAG_KEYWORDS = {
    "gp":               ["golden pocket", "gp "],
    "fib_618":          ["fib", "fibonacci", "61.8"],
    "fib_50":           ["fib", "fibonacci", "50%"],
    "sr_multi":         ["support", "resistance", "s/r"],
    "sr_single":        ["support", "resistance", "s/r"],
    "ema55":            ["ema", "moving average"],
    "ema21":            ["ema", "moving average"],
    "ema13":            ["ema", "moving average"],
    "rsi_div":          ["rsi", "divergence"],
    "rsi_div_hidden":   ["rsi", "divergence", "hidden"],
    "rsi_div_regular":  ["rsi", "divergence", "regular"],
    "rsi_div_forming":  ["rsi", "divergence", "forming"],
    "sweep":            ["sweep", "liquidity"],
    "bb_squeeze":       ["bollinger", "bb ", "squeeze"],
    "bb_burst":         ["bollinger", "bb ", "burst"],
    "pattern_breakout_vol": ["breakout", "pattern"],
    "pattern_breakout":     ["breakout", "pattern"],
    "vp_poc":           ["volume profile", "poc"],
    "ray_respected":    ["trendline", "ray"],
    "ray_cross_ahead":  ["trendline", "ray"],
}


def mentions_tag(text, tag):
    if not text:
        return False
    t = text.lower()
    for kw in TAG_KEYWORDS.get(tag, []):
        if kw in t:
            return True
    return False


def main():
    labels = load_jsonl(LABELS_CLOSED)
    all_skips = [r for r in labels if r.get("chev_decision") == "SKIP" and r.get("resolved")]
    all_skips.sort(key=lambda r: r.get("ts_epoch", 0))
    skips = all_skips[-LAST_N:] if len(all_skips) > LAST_N else all_skips

    decisions = load_jsonl(DECISIONS_LOG)
    dec_idx = build_decision_index(decisions)

    buckets = {"missed_winner": [], "good_skip": [], "inconclusive": [], "void": []}
    for r in skips:
        rec = dict(r)
        dec = nearest_decision(dec_idx, r["symbol"], r["tf"], r["ts_epoch"])
        rec["chev_full_reason"] = dec.get("reason") if dec else r.get("chev_reason")
        rec["chev_detail"] = dec.get("detail") if dec else None
        label = r.get("label")
        if label is None:
            buckets["void"].append(rec)
        elif label == 1:
            buckets["missed_winner"].append(rec)
        elif label == -1:
            buckets["good_skip"].append(rec)
        else:
            buckets["inconclusive"].append(rec)

    n_total = len(skips)
    n_mw, n_gs, n_inc, n_void = (len(buckets[k]) for k in
                                  ["missed_winner", "good_skip", "inconclusive", "void"])
    overall_win_rate = n_mw / (n_mw + n_gs + n_inc) if (n_mw + n_gs + n_inc) else 0.0

    print("=" * 70)
    print(f"CHEV SKIP BACKTEST -- last {n_total} resolved SKIPs "
          f"(of {len(all_skips)} total on disk)")
    print("=" * 70)
    print(f"  missed_winner (label=+1, would've won): {n_mw:4d}  ({100*n_mw/n_total:.1f}%)")
    print(f"  good_skip     (label=-1, would've lost): {n_gs:4d}  ({100*n_gs/n_total:.1f}%)")
    print(f"  inconclusive  (label=0, expired/no touch): {n_inc:4d}  ({100*n_inc/n_total:.1f}%)")
    print(f"  void          (label=null, no data coverage): {n_void:4d}  ({100*n_void/n_total:.1f}%)")
    print(f"  win rate among resolved (mw / (mw+gs+inc)): {100*overall_win_rate:.1f}%")

    total_missed_R = sum(r.get("realized_R") or 0 for r in buckets["missed_winner"])
    total_avoided_R = sum(r.get("realized_R") or 0 for r in buckets["good_skip"])
    print(f"\n  Sum realized_R if missed_winner trades HAD been taken: {total_missed_R:+.1f}R")
    print(f"  Sum realized_R avoided by correctly skipping good_skip: {total_avoided_R:+.1f}R "
          f"(negative R avoided)")
    print(f"  Net R Chev's SKIP calls cost this sample: {total_missed_R + total_avoided_R:+.1f}R")

    # By asset type
    print("\n--- By asset type ---")
    by_asset = defaultdict(lambda: {"mw": 0, "gs": 0, "inc": 0})
    for k, bkey in [("missed_winner", "mw"), ("good_skip", "gs"), ("inconclusive", "inc")]:
        for r in buckets[k]:
            by_asset[r.get("asset_type", "?")][bkey] += 1
    for asset, c in sorted(by_asset.items()):
        n = c["mw"] + c["gs"] + c["inc"]
        wr = c["mw"] / n if n else 0
        print(f"  {asset:8s}  n={n:4d}  missed_winner={c['mw']:4d} ({100*wr:.1f}% win rate)")

    # Tag lift across ALL resolved skips (mw+gs+inc), not just mw/gs split
    resolved_all = buckets["missed_winner"] + buckets["good_skip"] + buckets["inconclusive"]
    tag_total = Counter()
    tag_win = Counter()
    tag_R = defaultdict(list)
    for r in resolved_all:
        won = r.get("label") == 1
        for f in r.get("features", []):
            tag_total[f] += 1
            if won:
                tag_win[f] += 1
            tag_R[f].append(r.get("realized_R") or 0)

    print(f"\n--- Tag lift (baseline win rate = {100*overall_win_rate:.1f}%, min n={MIN_TAG_N}) ---")
    rows = []
    for tag, n in tag_total.items():
        if n < MIN_TAG_N:
            continue
        wr = tag_win[tag] / n
        lift = wr / overall_win_rate if overall_win_rate else 0
        avg_R = statistics.mean(tag_R[tag])
        rows.append((tag, n, wr, lift, avg_R))
    rows.sort(key=lambda x: -x[3])
    print(f"  {'tag':22s} {'n':>5s} {'win%':>7s} {'lift':>6s} {'avgR':>7s}")
    for tag, n, wr, lift, avg_R in rows:
        print(f"  {tag:22s} {n:5d} {100*wr:6.1f}% {lift:5.2f}x {avg_R:+6.2f}")

    # Acknowledgment check on missed-winner bucket: for the tags Chev skipped THROUGH,
    # did his own reasoning text even mention the thing that ended up winning?
    print("\n--- Missed winners: did Chev's stated reasoning mention the tag that was present? ---")
    ack = Counter()
    unack = Counter()
    for r in buckets["missed_winner"]:
        text = (r.get("chev_full_reason") or "") + " " + (r.get("chev_detail") or "")
        for f in r.get("features", []):
            if f not in TAG_KEYWORDS:
                continue
            if mentions_tag(text, f):
                ack[f] += 1
            else:
                unack[f] += 1
    print(f"  {'tag':22s} {'unmentioned':>12s} {'mentioned':>10s}")
    for tag in sorted(set(ack) | set(unack), key=lambda t: -(unack[t] + ack[t])):
        total = ack[tag] + unack[tag]
        if total < 10:
            continue
        print(f"  {tag:22s} {unack[tag]:12d} {ack[tag]:10d}")

    # Worst misses by realized_R for narrative follow-up
    print("\n--- Top 15 worst missed winners (biggest realized_R left on the table) ---")
    worst = sorted(buckets["missed_winner"], key=lambda r: -(r.get("realized_R") or 0))[:15]
    for r in worst:
        print(f"  {r['ts']}  {r['symbol']:9s} {r['tf']:4s} {r['direction']:5s} "
              f"R={r.get('realized_R'):+.2f}  tags={','.join(r.get('features', []))}")
        print(f"      chev: {(r.get('chev_full_reason') or '')[:140]}")

    out = {
        "n_total": n_total,
        "n_total_on_disk": len(all_skips),
        "overall_win_rate": overall_win_rate,
        "net_R": total_missed_R + total_avoided_R,
        "tag_lift": [
            {"tag": t, "n": n, "win_rate": wr, "lift": lift, "avg_R": avg_R}
            for t, n, wr, lift, avg_R in rows
        ],
        "worst_misses": [
            {k: r.get(k) for k in
             ["ts", "symbol", "tf", "direction", "realized_R", "features",
              "chev_full_reason", "chev_detail", "entry_ref", "upper", "lower"]}
            for r in sorted(buckets["missed_winner"], key=lambda r: -(r.get("realized_R") or 0))[:50]
        ],
    }
    with open(r"C:\ChevTools\backtest_out_skip_analysis.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print("\nFull JSON written to C:\\ChevTools\\backtest_out_skip_analysis.json")


if __name__ == "__main__":
    main()
