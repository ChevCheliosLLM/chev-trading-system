"""
counterfactual_report.py — PHASE 7: Examiner Counterfactual Report (READ-ONLY, FIREWALLED)
Extended in PHASE 7B-1 with build_counterfactual(), a pure dict-returning function shared
by both the .txt report (this file's __main__) and dexter.py's /api/strategy/counterfactual
route — single source of truth, so the phone always matches the .txt.

Standalone, manually-run script (or importable by dexter.py, read-only, for the endpoint).
Reads labels_closed.jsonl, labels_open.jsonl, and chev_decisions.jsonl; the __main__ text
report writes exactly one file, counterfactual_report.txt. Answers, per no-trade category
since BASELINE_TS: how many setups, how many have resolved shadow outcomes, shadow win
rate, average net R, and total net R at the Examiner's own cost model — "money left on the
table vs bullets dodged."

FIREWALL (see handoff.txt PHASE 7 / 7B-1 sections for the full rationale):
  - The __main__ text report writes exactly one file: counterfactual_report.txt. Nothing
    else, ever. build_counterfactual() itself performs NO writes at all — read-only in,
    dict out.
  - Never writes/appends/modifies chev_journal.json, labels_closed.jsonl, labels_open.jsonl,
    chev_decisions.jsonl, or any Sheets/Firebase/dashboard state.
  - This module registers no scheduler, thread, or Flask route itself — dexter.py's PHASE
    7B-1 route imports build_counterfactual() and calls it read-only, on its own cooldown.
  - Not importing labeller.py: labeller.py is safe to import (verified — __main__ guard,
    no module-level side effects beyond a threading.Lock() object and plain constants), but
    every number this report needs (including cost_R) is already stored on each closed
    record, so this script stays a fully standalone, dependency-free reader — plain json/
    os/re/datetime/collections only. This module itself has the same property (a __main__
    guard, no module-level side effects), confirmed safe for dexter.py to import.

Run manually:  python -X utf8 counterfactual_report.py
Self-test:     python -X utf8 counterfactual_report.py --selftest
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

DATA_DIR           = r"C:\ChevTools"
LABELS_CLOSED_FILE = os.path.join(DATA_DIR, "labels_closed.jsonl")
LABELS_OPEN_FILE   = os.path.join(DATA_DIR, "labels_open.jsonl")
DECISIONS_FILE     = os.path.join(DATA_DIR, "chev_decisions.jsonl")
OUTPUT_FILE        = os.path.join(DATA_DIR, "counterfactual_report.txt")

# Edit this to move the analysis window. Matches the exploration-mode baseline established
# 2026-07-06 04:26 (see handoff.txt top banner history) — everything before it was judged by
# pre-exploration gates/win-rates and isn't a fair read on the system as it runs today.
BASELINE_TS = "2026-07-06 04:26:00"

TOP_N = 3   # largest winners/losers shown per bucket in the .txt report

# Read-time cap on cost_R, mirroring labeller.py's COST_R_CAP constant (kept as a
# local literal, not an import, per this file's own "stays standalone" design —
# see the module docstring). Historical labels_closed.jsonl records can carry a
# cost_R computed before labeller.py's own fix, which blew up to 2-3R+ on low-
# ATR%-relative-to-price instruments (forex swing especially) — a shadow-model
# artifact, not a real cost the real gauntlet would ever have allowed through.
# NOTE: keep this in sync with labeller.py's COST_R_CAP (and weight_proposal.py's
# own copy) — all three are deliberately standalone, not imported, so a change to
# one won't propagate automatically.
COST_R_CAP = 0.50

# resolved_items is a flat per-record list -- capped so the JSON endpoint (PHASE 7B-1)
# never has to serve an unbounded, ever-growing list to a phone. Most recent first.
RESOLVED_ITEMS_CAP = 300

CAVEATS = (
    "SHADOW OUTCOMES ARE OPTIMISTIC. They assume the entry filled at the zone touch, no\n"
    "checkpoint management, no partial exits, no concurrency limits, and modelled costs only.\n"
    "These numbers may justify changing a threshold BY HAND. They must NEVER be merged into\n"
    "chev_journal.json or any tag win-rate calculation. Doing so will silently inflate George's\n"
    "confidence and produce unexplained losses."
)


# ── Loading (read-only) ───────────────────────────────────────────────────────

def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _baseline_epoch(baseline_ts=None):
    dt = datetime.strptime(baseline_ts or BASELINE_TS, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _decision_ts_epoch(rec):
    """chev_decisions.jsonl stores ts as a '%Y-%m-%d %H:%M:%S' string, not an epoch."""
    ts = rec.get("ts")
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


# ── Classification (no cross-file joins — see PHASE 7 discovery in handoff.txt) ──────
# SKIP and NOT_ESCALATED records already carry a usable reason on the label record itself
# (chev_reason). Downstream gate-kills (a POST that got rejected by a gate AFTER Chev
# proposed it) never get an Examiner shadow record at all — labeller.record_setup(...,
# "POST", ...) only fires once a trade is actually locked (dexter.py, confirmed by reading
# the call site relative to risk_gauntlet.run_gauntlet()) — so those are counted from
# chev_decisions.jsonl only, with no shadow win rate/net R possible.

def classify_skip_reason(reason):
    r = (reason or "").lower()
    if "invalidat" in r or "stop placement" in r or "stop-loss" in r or "sl placement" in r:
        return "SKIP - invalidation"
    if "r:r" in r or "reward" in r or "risk:reward" in r or "risk/reward" in r:
        return "SKIP - R:R"
    return "SKIP - other"


def classify_reject_reason(reason):
    """Sub-classifies risk_gauntlet's own REJECT decisions by reason text. Order matters —
    checked against the real chev_decisions.jsonl REJECT population during discovery."""
    r = (reason or "").lower()
    if "outside plausible band" in r or "data quality" in r:
        return "DATA_QUALITY"
    if "round-trip cost" in r:
        return "COST_GATE"
    if "total heat" in r:
        return "HEAT_CAP"
    if "open/pending" in r:
        return "CONCURRENCY"
    if "consumed" in r and "entry->sl" in r:
        return "PRICE_DRIFT"
    if "net r:r" in r:
        return "NET_RR"
    if "correlat" in r:
        return "CORR_CAP"
    if "liquidat" in r:
        return "LIQUIDATION"
    if "hallucinat" in r:
        return "HALLUCINATED_LEVELS"
    return "OTHER"


# Decisions that map directly to one downstream-kill bucket, no reason-text classification
# needed (each decision value IS the bucket).
DECISION_DIRECT_MAP = {
    "GATE_REJECT":     "SCORE_GATE",
    "STRUCT_REJECT":   "STRUCT_GATE",
    "MTF_TAX_REJECT":  "MTF_TAX",
    "GEOMETRY_REJECT": "GEOMETRY",
}


# ── Per-bucket stats over Examiner shadow records ─────────────────────────────

def _numeric_label(label):
    return label if label in (1, -1, 0) else None


def bucket_stats(closed_recs, open_count):
    """closed_recs: labels_closed.jsonl records already filtered to this bucket + baseline.
    open_count: how many still-open (labels_open.jsonl) records fall in this bucket — counted
    for coverage, NEVER given a guessed outcome (hard rule)."""
    n_closed  = len(closed_recs)
    n_setups  = n_closed + open_count
    coverage_pct = (n_closed / n_setups * 100.0) if n_setups else 0.0

    no_fill = sum(1 for r in closed_recs if r.get("label") == "NO_FILL")
    void    = sum(1 for r in closed_recs if r.get("label") == "VOID")
    filled  = [r for r in closed_recs if _numeric_label(r.get("label")) is not None]
    n_filled = len(filled)
    fill_denom = n_filled + no_fill
    fill_rate = (n_filled / fill_denom * 100.0) if fill_denom else None

    wins = sum(1 for r in filled if r.get("label") == 1)
    shadow_win_rate = (wins / n_filled * 100.0) if n_filled else None

    net_rs = []
    for r in filled:
        realized = r.get("realized_R")
        if realized is None:
            continue
        cost = min(r.get("cost_R") or 0.0, COST_R_CAP)
        net_rs.append((round(realized - cost, 4), r))

    avg_net_R   = (sum(x[0] for x in net_rs) / len(net_rs)) if net_rs else None
    total_net_R = sum(x[0] for x in net_rs) if net_rs else 0.0

    winners = sorted(net_rs, key=lambda x: x[0], reverse=True)[:TOP_N]
    losers  = sorted(net_rs, key=lambda x: x[0])[:TOP_N]

    return {
        "n_setups": n_setups, "n_closed": n_closed, "n_open_unresolved": open_count,
        "coverage_pct": coverage_pct, "no_fill": no_fill, "void": void,
        "n_filled": n_filled, "fill_rate": fill_rate,
        "wins": wins, "shadow_win_rate": shadow_win_rate,
        "avg_net_R": avg_net_R, "total_net_R": total_net_R,
        "winners": winners, "losers": losers,
        "net_rs": net_rs,   # full (net, record) list — used by verdict_split/resolved_items
    }


def _rec_date(r):
    ts = r.get("ts") or ""
    return ts[:10] if ts else "?"


def _fmt_examples(pairs):
    if not pairs:
        return ["    (none)"]
    lines = []
    for net, r in pairs:
        lines.append(f"    {r.get('symbol','?'):<10} {r.get('tf','?'):<4} {_rec_date(r):<10} net_R={net:+.2f}")
    return lines


def _fmt_stat_block(stats):
    lines = []
    lines.append(f"  setups (since baseline)      : {stats['n_setups']}")
    lines.append(f"  resolved (closed)            : {stats['n_closed']}  (coverage {stats['coverage_pct']:.1f}%)")
    lines.append(f"  still open / unresolved       : {stats['n_open_unresolved']}  (excluded from every stat below)")
    lines.append(f"  NO_FILL / VOID (closed)       : {stats['no_fill']} / {stats['void']}")
    if stats['fill_rate'] is not None:
        lines.append(f"  fill rate (filled vs NO_FILL) : {stats['fill_rate']:.1f}%  (n={stats['n_filled']})")
    else:
        lines.append(f"  fill rate                     : n/a (no filled or NO_FILL records)")
    if stats['shadow_win_rate'] is not None:
        lines.append(f"  shadow win rate               : {stats['shadow_win_rate']:.1f}%  ({stats['wins']}/{stats['n_filled']})")
        lines.append(f"  average net R (per filled)    : {stats['avg_net_R']:+.3f}R")
        lines.append(f"  TOTAL net R (Examiner cost)   : {stats['total_net_R']:+.2f}R")
    else:
        lines.append(f"  shadow win rate               : n/a (no barrier-filled outcomes yet)")
    if stats['winners'] or stats['losers']:
        lines.append(f"  largest would-be winners (top {TOP_N}):")
        lines.extend(_fmt_examples(stats['winners']))
        lines.append(f"  largest would-be losers (top {TOP_N}):")
        lines.extend(_fmt_examples(stats['losers']))
    return lines


# ── Grouping ───────────────────────────────────────────────────────────────────

def _group_closed_open(closed, open_recs, decision, classifier):
    """Groups closed+open label records for a given chev_decision into sub-buckets keyed
    by classifier(chev_reason). Returns {sub_bucket: (closed_list, open_count)}."""
    buckets = {}
    for r in closed:
        if r.get("chev_decision") != decision:
            continue
        key = classifier(r.get("chev_reason"))
        buckets.setdefault(key, ([], 0))
        buckets[key][0].append(r)
    for r in open_recs:
        if r.get("chev_decision") != decision:
            continue
        key = classifier(r.get("chev_reason"))
        buckets.setdefault(key, ([], 0))
        lst, cnt = buckets[key]
        buckets[key] = (lst, cnt + 1)
    return buckets


def _verdict_for(net_r):
    """A no-trade decision was 'right' if the shadow outcome would have net-lost or broken
    even (the block/skip correctly dodged a bullet), 'wrong' if it would have net-won (real
    edge was forfeited). Only meaningful for a resolved, barrier-filled record."""
    return "wrong" if net_r > 0 else "right"


def _bucket_label(kind, source, key):
    if source == "SKIP":
        return "Skip: " + key.replace("SKIP - ", "").replace("SKIP-", "").title()
    if source == "NOT_ESCALATED":
        return "Not Escalated: " + key.replace("_", " ").title()
    if source == "GATE_KILL":
        return "Gate Reject: " + key.replace("_", " ").title()
    return key


def _tag_combo_aggregate(closed_recs, open_recs):
    """Aggregates per-individual-tag and per-tag-combination shadow stats across the given
    (already SKIP+NOT_ESCALATED-filtered) record sets. Uses Dexter's own `features` token
    list (present on every record regardless of decision type) — NEVER chev_tags, which is
    only ever populated on POST records (confirmed in PHASE 7 discovery: the SKIP/
    NOT_ESCALATED record_setup call sites never pass a "tags" key). Independent of, and
    never merged with, compute_tag_win_rates' real-trade data (see DO-NOT-TOUCH)."""
    tag_n, tag_closed = {}, {}
    combo_n, combo_closed = {}, {}

    def _combo_key(feats):
        uniq = sorted(set(feats))
        return ",".join(uniq) if uniq else None

    for r in open_recs:
        feats = r.get("features") or []
        for t in set(feats):
            tag_n[t] = tag_n.get(t, 0) + 1
        ck = _combo_key(feats)
        if ck:
            combo_n[ck] = combo_n.get(ck, 0) + 1

    for r in closed_recs:
        feats = r.get("features") or []
        for t in set(feats):
            tag_n[t] = tag_n.get(t, 0) + 1
            tag_closed.setdefault(t, []).append(r)
        ck = _combo_key(feats)
        if ck:
            combo_n[ck] = combo_n.get(ck, 0) + 1
            combo_closed.setdefault(ck, []).append(r)

    def _stats_for(n_total, closed_list):
        filled = [r for r in closed_list
                  if _numeric_label(r.get("label")) is not None and r.get("realized_R") is not None]
        wins = sum(1 for r in filled if r.get("label") == 1)
        wr = (wins / len(filled) * 100.0) if filled else None
        net_total = sum((r.get("realized_R") - (r.get("cost_R") or 0.0)) for r in filled)
        return {"n": n_total, "resolved": len(closed_list), "shadow_wr": wr, "total_r": round(net_total, 2)}

    shadow_tags = [dict(tag=t, **_stats_for(tag_n[t], tag_closed.get(t, []))) for t in tag_n]
    shadow_combos = [dict(combo=c, **_stats_for(combo_n[c], combo_closed.get(c, []))) for c in combo_n]
    shadow_tags.sort(key=lambda x: x["n"], reverse=True)
    shadow_combos.sort(key=lambda x: x["n"], reverse=True)
    return shadow_tags, shadow_combos


# ── Weekly regret trend (PHASE 7B-4) ──────────────────────────────────────────
# Deliberately independent of BASELINE_TS and of resolved_items' cap: BASELINE_TS is a
# hand-edited, moving cutoff (see its own comment above), and resolved_items is capped at
# RESOLVED_ITEMS_CAP and already baseline-scoped for the phone feed -- neither is fit for a
# rolling "has the regret trend actually improved since Phase N?" view, which specifically
# needs to see weeks BEFORE the current baseline too. Computed once here, exposed via
# build_counterfactual()'s "weekly" key -- never computed a second time client-side.

def _week_start(dt):
    """Monday 00:00:00 UTC of the week containing dt."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def build_weekly_regret(closed_all, weeks=6, now=None):
    """Weekly (Monday-start UTC) forfeited/dodged shadow R totals across the FULL closed-
    records history passed in (no baseline filter, no cap). Only SKIP + NOT_ESCALATED
    records count (same shadow-trackable universe as every other bucket in this file).
    Returns the last `weeks` weeks, oldest-first, INCLUDING weeks with zero records -- an
    empty week is a real, honest data point, not an error."""
    now = now or datetime.now(timezone.utc)
    this_monday = _week_start(now)
    week_starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    earliest = week_starts[0]

    buckets = {ws: {"forfeited_r": 0.0, "dodged_r": 0.0, "n_resolved": 0} for ws in week_starts}

    for r in closed_all:
        if r.get("chev_decision") not in ("SKIP", "NOT_ESCALATED"):
            continue
        if _numeric_label(r.get("label")) is None:
            continue
        realized = r.get("realized_R")
        if realized is None:
            continue
        ts_epoch = r.get("ts_epoch")
        if not ts_epoch:
            continue
        dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
        if dt < earliest:
            continue
        ws = _week_start(dt)
        if ws not in buckets:
            continue   # newer than "now" (clock skew) or otherwise outside the tracked window
        cost = r.get("cost_R") or 0.0
        net = round(realized - cost, 4)
        b = buckets[ws]
        b["n_resolved"] += 1
        if net > 0:
            b["forfeited_r"] += net
        else:
            b["dodged_r"] += abs(net)

    return [
        {
            "week_start": ws.strftime("%Y-%m-%d"),
            "forfeited_r": round(buckets[ws]["forfeited_r"], 2),
            "dodged_r": round(buckets[ws]["dodged_r"], 2),
            "n_resolved": buckets[ws]["n_resolved"],
        }
        for ws in week_starts
    ]


# ── Shared bucketing (single source of truth for both the .txt report and the JSON endpoint) ──

def build_counterfactual(baseline_ts=None,
                          labels_closed_file=None, labels_open_file=None, decisions_file=None):
    """Pure, read-only. Loads the three source files, buckets everything since baseline_ts,
    and returns one dict consumed by both build_report() (text) and dexter.py's
    /api/strategy/counterfactual route (JSON) — single source of truth so the phone always
    matches the .txt. No writes, no project imports, no side effects of any kind.

    Public-schema keys (PHASE 7B-1 contract, extended in PHASE 7B-4 — the Flask route
    serves exactly these, plus cache_age_secs): coverage, headline, buckets, shadow_tags,
    shadow_combos, resolved_items, weekly. Keys prefixed with "_" are internal convenience
    data for the .txt renderer only (raw winners/losers, the POST reality-anchor block,
    load counts) — not part of the endpoint's contract, safe to ignore.

    NOTE on "weekly": unlike every other key here, it is NOT scoped to baseline_ts and NOT
    subject to resolved_items' cap — see build_weekly_regret()'s own docstring for why a
    rolling regret trend needs to see history from before the current baseline too.
    """
    baseline_ts = baseline_ts or BASELINE_TS
    baseline_epoch = _baseline_epoch(baseline_ts)

    closed_all = _load_jsonl(labels_closed_file or LABELS_CLOSED_FILE)
    open_all   = _load_jsonl(labels_open_file or LABELS_OPEN_FILE)
    dec_all    = _load_jsonl(decisions_file or DECISIONS_FILE)

    closed = [r for r in closed_all if (r.get("ts_epoch") or 0) >= baseline_epoch]
    opens  = [r for r in open_all   if (r.get("ts_epoch") or 0) >= baseline_epoch]
    decs   = [r for r in dec_all    if (_decision_ts_epoch(r) or 0) >= baseline_epoch]

    buckets = []
    resolved_items = []
    total_setups_shadow = 0
    total_resolved_shadow = 0
    dodged_r = 0.0
    forfeited_r = 0.0

    def _add_bucket(kind, source, key, closed_list, open_cnt, key_override=None):
        nonlocal total_setups_shadow, total_resolved_shadow, dodged_r, forfeited_r
        bucket_key = key_override or key
        stats = bucket_stats(closed_list, open_cnt)
        right = wrong = 0
        for net, r in stats["net_rs"]:
            v = _verdict_for(net)
            if v == "right":
                right += 1
                dodged_r += abs(net)
            else:
                wrong += 1
                forfeited_r += net
            resolved_items.append({
                "ts": r.get("ts"), "symbol": r.get("symbol"), "tf": r.get("tf"),
                "decision": r.get("chev_decision"), "bucket_key": bucket_key,
                "shadow_r": round(net, 4), "verdict": v,
            })
        pending = stats["n_setups"] - right - wrong
        total_setups_shadow += stats["n_setups"]
        total_resolved_shadow += stats["n_closed"]
        buckets.append({
            "key": bucket_key, "label": _bucket_label(kind, source, key), "kind": kind,
            "n": stats["n_setups"], "resolved": stats["n_closed"],
            "shadow_wr": stats["shadow_win_rate"], "avg_r": stats["avg_net_R"],
            "total_r": stats["total_net_R"],
            "verdict_split": {"right": right, "wrong": wrong, "pending": pending},
            "_winners": stats["winners"], "_losers": stats["losers"],
            "_no_fill": stats["no_fill"], "_void": stats["void"],
            "_n_open_unresolved": stats["n_open_unresolved"], "_fill_rate": stats["fill_rate"],
        })

    # ── SKIP buckets ───────────────────────────────────────────────────────────
    skip_buckets = _group_closed_open(closed, opens, "SKIP", classify_skip_reason)
    for key in sorted(skip_buckets.keys()):
        closed_list, open_cnt = skip_buckets[key]
        _add_bucket("skip", "SKIP", key, closed_list, open_cnt)

    # ── NOT_ESCALATED buckets (one per real pre-gate reason string) ───────────
    not_esc_buckets = _group_closed_open(closed, opens, "NOT_ESCALATED", lambda r: (r or "unknown"))
    for key in sorted(not_esc_buckets.keys(), key=lambda k: -len(not_esc_buckets[k][0])):
        closed_list, open_cnt = not_esc_buckets[key]
        _add_bucket("gate", "NOT_ESCALATED", key, closed_list, open_cnt)

    # ── Downstream gate-kills: PHASE 23 -- dexter.py now stamps a "POST" shadow label the
    # MOMENT Chev's reply first parses as a valid trade, BEFORE any gate can kill it (see
    # that call site's own comment for why "POST" is the correct chev_decision to pass,
    # and why this never double-counts a trade that goes on to lock in for real). Before
    # that fix, NO shadow label ever existed for a gate-killed proposal -- confirmed
    # empirically against real historical data (zero matches at any time tolerance) -- so
    # the join below correctly finds nothing for anything decided before a restart that
    # carries the fix, and increasingly resolves real numbers for anything decided after.
    # Matched by symbol+tf+timestamp proximity ONLY, never by chev_decision -- that
    # restriction was the actual bug (a gate-killed attempt legitimately IS a "POST" label,
    # since the kill happens downstream of Chev's reply, not because Chev's own decision
    # was anything other than POST).
    GATE_KILL_MATCH_WINDOW_SECS = 900   # 15 min -- comfortably covers deliberation + retries

    def _post_index(records):
        idx = {}
        for r in records:
            if r.get("chev_decision") != "POST":
                continue
            idx.setdefault((r.get("symbol"), r.get("tf")), []).append(r)
        return idx

    def _closest(records, target_epoch):
        best, best_delta = None, float("inf")
        if target_epoch is None:
            return best, best_delta
        for r in records:
            r_epoch = r.get("ts_epoch")
            if r_epoch is None:
                continue
            delta = abs(r_epoch - target_epoch)
            if delta <= GATE_KILL_MATCH_WINDOW_SECS and delta < best_delta:
                best, best_delta = r, delta
        return best, best_delta

    post_closed_by_symtf = _post_index(closed)
    post_open_by_symtf   = _post_index(opens)

    gate_kill_groups = {}   # key -> {"closed": [label dicts], "open_cnt": int}
    for d in decs:
        decision = d.get("decision")
        if decision in DECISION_DIRECT_MAP:
            key = DECISION_DIRECT_MAP[decision]
        elif decision == "REJECT":
            key = classify_reject_reason(d.get("reason"))
        else:
            continue
        group = gate_kill_groups.setdefault(key, {"closed": [], "open_cnt": 0})

        d_epoch = _decision_ts_epoch(d)
        symtf = (d.get("symbol"), d.get("tf"))
        closed_match, closed_delta = _closest(post_closed_by_symtf.get(symtf, []), d_epoch)
        open_match, open_delta     = _closest(post_open_by_symtf.get(symtf, []), d_epoch)
        if closed_match is not None and closed_delta <= open_delta:
            group["closed"].append(closed_match)
        else:
            # Either an unresolved label matched, or no label exists at all -- both count as
            # "pending" (still resolving, or no shadow data ever existed -- same treatment
            # this bucket has always given an unresolved/absent record).
            group["open_cnt"] += 1

    for key in sorted(gate_kill_groups.keys(),
                       key=lambda k: -(len(gate_kill_groups[k]["closed"]) + gate_kill_groups[k]["open_cnt"])):
        g = gate_kill_groups[key]
        _add_bucket("gate", "GATE_KILL", key, g["closed"], g["open_cnt"], key_override=f"GATE-{key}")

    # ── POST reality anchor (kept out of the public `buckets` list — it's not a no-trade
    # category, see handoff.txt) — internal-only field for the .txt renderer ─────────────
    post_buckets = _group_closed_open(closed, opens, "POST", lambda r: "all")
    post_closed, post_open = post_buckets.get("all", ([], 0))
    post_stats = bucket_stats(post_closed, post_open)

    # ── Shadow tag / combo leaderboards (SKIP + NOT_ESCALATED records only — the
    # shadow-trackable universe; downstream gate-kills have no records to tag) ───────────
    shadow_closed = [r for r in closed if r.get("chev_decision") in ("SKIP", "NOT_ESCALATED")]
    shadow_open   = [r for r in opens  if r.get("chev_decision") in ("SKIP", "NOT_ESCALATED")]
    shadow_tags, shadow_combos = _tag_combo_aggregate(shadow_closed, shadow_open)

    # ── resolved_items: most-recent-first, capped ─────────────────────────────
    resolved_items.sort(key=lambda it: it.get("ts") or "", reverse=True)
    resolved_items_total = len(resolved_items)
    resolved_items = resolved_items[:RESOLVED_ITEMS_CAP]

    # ── weekly regret trend (PHASE 7B-4) — full history, independent of baseline_ts ──
    weekly = build_weekly_regret(closed_all)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC",
        "baseline_ts": baseline_ts,
        "coverage": {"total": total_setups_shadow, "resolved": total_resolved_shadow},
        "headline": {"dodged_r": round(dodged_r, 2), "forfeited_r": round(forfeited_r, 2)},
        "buckets": buckets,
        "shadow_tags": shadow_tags,
        "shadow_combos": shadow_combos,
        "resolved_items": resolved_items,
        "weekly": weekly,
        "_resolved_items_total_before_cap": resolved_items_total,
        "_post": post_stats,
        "_meta": {
            "closed_total": len(closed_all), "closed_in_window": len(closed),
            "open_total": len(open_all), "open_in_window": len(opens),
            "decisions_total": len(dec_all), "decisions_in_window": len(decs),
        },
    }


# ── Text report (uses build_counterfactual() as its single source of truth) ──────────

def build_report(baseline_ts=None,
                  labels_closed_file=None, labels_open_file=None, decisions_file=None):
    data = build_counterfactual(baseline_ts, labels_closed_file, labels_open_file, decisions_file)
    meta = data["_meta"]

    lines = []
    lines.append(CAVEATS)
    lines.append("")
    lines.append("=" * 78)
    lines.append("EXAMINER COUNTERFACTUAL REPORT (COUNTERFACTUAL — shadow outcomes only)")
    lines.append("=" * 78)
    lines.append(f"Generated              : {data['generated_at']}")
    lines.append(f"Baseline (window start): {data['baseline_ts']} UTC  (edit BASELINE_TS in this script to move it)")
    lines.append(f"labels_closed.jsonl    : {meta['closed_total']} total records, {meta['closed_in_window']} at/after baseline")
    lines.append(f"labels_open.jsonl      : {meta['open_total']} total records, {meta['open_in_window']} at/after baseline")
    lines.append(f"chev_decisions.jsonl   : {meta['decisions_total']} total entries, {meta['decisions_in_window']} at/after baseline")
    lines.append("")
    lines.append("Framing: every bucket below answers 'money left on the table vs bullets")
    lines.append("dodged' for one no-trade (or gate-killed) category. A strongly positive")
    lines.append("shadow total net R means real edge was skipped; a strongly negative one")
    lines.append("means the gate that blocked it is earning its keep.")
    lines.append("")
    lines.append(f"HEADLINE — bullets dodged (shadow R avoided): {data['headline']['dodged_r']:+.2f}R")
    lines.append(f"HEADLINE — edge forfeited (shadow R missed):  {data['headline']['forfeited_r']:+.2f}R")
    lines.append(f"Shadow-trackable coverage: {data['coverage']['resolved']}/{data['coverage']['total']} resolved "
                 f"(SKIP + NOT_ESCALATED only — downstream gate-kills have no shadow record, see below)")

    skip_rows = [b for b in data["buckets"] if b["kind"] == "skip"]
    gate_rows = [b for b in data["buckets"] if b["kind"] == "gate" and not b["key"].startswith("GATE-")]
    kill_rows = [b for b in data["buckets"] if b["key"].startswith("GATE-")]
    summary_rows = [(b["key"], b) for b in data["buckets"] if b["resolved"] > 0 and b["shadow_wr"] is not None]

    lines.append("")
    lines.append("-" * 78)
    lines.append("SKIP BUCKETS (Chev's own judgment — real shadow outcomes, no join needed)")
    lines.append("-" * 78)
    for b in skip_rows:
        lines.append("")
        lines.append(f"[{b['key']}]")
        lines.extend(_fmt_stat_block_from_bucket(b))

    lines.append("")
    lines.append("-" * 78)
    lines.append("NOT_ESCALATED BUCKETS (Dexter's pre-gates — real shadow outcomes, no join needed)")
    lines.append("-" * 78)
    for b in gate_rows:
        lines.append("")
        lines.append(f"[NOT_ESCALATED - {b['key']}]")
        lines.extend(_fmt_stat_block_from_bucket(b))

    lines.append("")
    lines.append("-" * 78)
    lines.append("POST — SHADOW REPLAY (NOT real trade P&L)")
    lines.append("-" * 78)
    lines.append("NOTE: this bucket re-simulates Chev's stated entry through the same synthetic")
    lines.append("±1R box as SKIP. Real fills/stops/P&L live in chev_journal.json and are NOT")
    lines.append("reflected here.")
    lines.append("NOTE: only trades that survived every gate get an Examiner POST shadow record")
    lines.append("(record_setup fires once a trade is actually locked, not on every Chev POST —")
    lines.append("see handoff.txt PHASE 7 discovery). Sample is typically thin; read accordingly.")
    lines.append("")
    lines.append("[POST - all]")
    lines.extend(_fmt_stat_block(data["_post"]))

    lines.append("")
    lines.append("-" * 78)
    lines.append("DOWNSTREAM GATE-KILLS (a POST Chev proposed, killed by a gate AFTER his reply)")
    lines.append("-" * 78)
    lines.append("NO SHADOW OUTCOME EXISTS FOR THESE. The Examiner never runs a barrier-walk for")
    lines.append("a proposal a gate killed — only trades that are actually locked get tracked.")
    lines.append("Counts only, sourced from chev_decisions.jsonl.")
    lines.append("")
    if kill_rows:
        for b in kill_rows:
            lines.append(f"  {b['key'][5:]:<22}: {b['n']}  (n/a shadow win rate/net R — no Examiner record)")
    else:
        lines.append("  (none in this window)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("SHADOW TAG LEADERBOARD (Dexter's own feature tokens, SKIP + NOT_ESCALATED only)")
    lines.append("=" * 78)
    lines.append("(Independent of compute_tag_win_rates' real-trade data — never merged with it.)")
    lines.append("")
    lines.append(f"{'TAG':<20}{'N':>6}{'RESOLVED':>10}{'SHADOW WIN%':>13}{'TOTAL NET R':>14}")
    for t in data["shadow_tags"][:40]:
        wr = f"{t['shadow_wr']:.1f}%" if t['shadow_wr'] is not None else "n/a"
        lines.append(f"{t['tag']:<20}{t['n']:>6}{t['resolved']:>10}{wr:>13}{t['total_r']:>+14.2f}")
    if len(data["shadow_tags"]) > 40:
        lines.append(f"  ... and {len(data['shadow_tags']) - 40} more (see the JSON endpoint for the full list)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("SHADOW COMBO LEADERBOARD (full tag combination, SKIP + NOT_ESCALATED only)")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"{'COMBO':<50}{'N':>6}{'RESOLVED':>10}{'SHADOW WIN%':>13}{'TOTAL NET R':>14}")
    for c in data["shadow_combos"][:25]:
        wr = f"{c['shadow_wr']:.1f}%" if c['shadow_wr'] is not None else "n/a"
        combo_disp = c['combo'] if len(c['combo']) <= 48 else c['combo'][:45] + "..."
        lines.append(f"{combo_disp:<50}{c['n']:>6}{c['resolved']:>10}{wr:>13}{c['total_r']:>+14.2f}")
    if len(data["shadow_combos"]) > 25:
        lines.append(f"  ... and {len(data['shadow_combos']) - 25} more (see the JSON endpoint for the full list)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("SUMMARY — SHADOW NET R BY CATEGORY (ranked, most forfeited edge first)")
    lines.append("=" * 78)
    lines.append("(Only buckets with a real Examiner shadow outcome can appear here — downstream")
    lines.append(" gate-kills have no shadow R and are listed as counts above, not here.)")
    lines.append("")
    summary_rows.sort(key=lambda kv: kv[1]["total_r"], reverse=True)
    if summary_rows:
        col_w = max(28, max(len(key) for key, _ in summary_rows) + 2)
        lines.append(f"{'CATEGORY':<{col_w}}{'N':>6}{'SHADOW WIN%':>13}{'AVG NET R':>12}{'TOTAL NET R':>14}")
        for key, b in summary_rows:
            n_filled = b["verdict_split"]["right"] + b["verdict_split"]["wrong"]
            wr = f"{b['shadow_wr']:.1f}%" if b['shadow_wr'] is not None else "n/a"
            avg = f"{b['avg_r']:+.3f}" if b['avg_r'] is not None else "n/a"
            lines.append(f"{key:<{col_w}}{n_filled:>6}{wr:>13}{avg:>12}{b['total_r']:>+14.2f}")
    else:
        lines.append("  (no buckets with filled shadow outcomes in this window)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("WEEKLY REGRET TREND (PHASE 7B-4 — full history, NOT scoped to baseline_ts)")
    lines.append("=" * 78)
    lines.append("Unlike every section above, this one ignores BASELINE_TS on purpose — it's meant to")
    lines.append("show whether Phases 1-6 actually bent the money-left-on-table curve over time, which")
    lines.append("requires seeing weeks before the current baseline too.")
    lines.append("")
    lines.append(f"{'WEEK OF':<12}{'RESOLVED':>10}{'DODGED R':>12}{'FORFEITED R':>14}")
    for w in data["weekly"]:
        lines.append(f"{w['week_start']:<12}{w['n_resolved']:>10}{w['dodged_r']:>+12.2f}{w['forfeited_r']:>+14.2f}")

    lines.append("")
    lines.append("=" * 78)
    lines.append("RESOLVED ITEMS — RAW LOG (most recent first, full list served by the JSON endpoint)")
    lines.append("=" * 78)
    total_before_cap = data["_resolved_items_total_before_cap"]
    shown = data["resolved_items"][:15]
    lines.append(f"{total_before_cap} resolved shadow items in window (capped at {RESOLVED_ITEMS_CAP} for the JSON")
    lines.append(f"endpoint); showing the {len(shown)} most recent here — see the endpoint for the rest.")
    lines.append("")
    if shown:
        lines.append(f"{'TS':<22}{'SYMBOL':<11}{'TF':<5}{'DECISION':<15}{'BUCKET':<26}{'VERDICT':<8}{'R':>7}")
        for it in shown:
            lines.append(f"{(it['ts'] or '?'):<22}{it['symbol']:<11}{it['tf']:<5}{it['decision']:<15}"
                         f"{it['bucket_key'][:24]:<26}{it['verdict']:<8}{it['shadow_r']:>+7.2f}")
    else:
        lines.append("  (none in this window)")

    lines.append("")
    lines.append("=" * 78)
    lines.append("END OF REPORT — remember the caveats at the top before acting on any number here.")
    lines.append("=" * 78)

    return "\n".join(lines) + "\n"


def _fmt_stat_block_from_bucket(b):
    """Adapts a public bucket dict (from build_counterfactual) into the same stat-block
    text _fmt_stat_block() already renders, so the .txt report's SKIP/NOT_ESCALATED
    sections read identically to before this phase's refactor."""
    n_filled = b["verdict_split"]["right"] + b["verdict_split"]["wrong"]
    wins = None
    if b["shadow_wr"] is not None and n_filled:
        wins = round(b["shadow_wr"] / 100.0 * n_filled)
    stats = {
        "n_setups": b["n"], "n_closed": b["resolved"],
        "n_open_unresolved": b["_n_open_unresolved"],
        "coverage_pct": (b["resolved"] / b["n"] * 100.0) if b["n"] else 0.0,
        "no_fill": b["_no_fill"], "void": b["_void"], "fill_rate": b["_fill_rate"],
        "n_filled": n_filled, "wins": wins, "shadow_win_rate": b["shadow_wr"],
        "avg_net_R": b["avg_r"], "total_net_R": b["total_r"],
        "winners": b["_winners"], "losers": b["_losers"],
    }
    return _fmt_stat_block(stats)


def write_report():
    text = build_report()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    return OUTPUT_FILE


# ── Self-test (synthetic fixtures, hand-computed expected values) ─────────────

def _fixture_label(symbol, tf, decision, reason, label, realized_R, cost_R, ts_epoch,
                    ts=None, features=None):
    return {
        "symbol": symbol, "tf": tf, "chev_decision": decision, "chev_reason": reason,
        "label": label, "realized_R": realized_R, "cost_R": cost_R,
        "ts_epoch": ts_epoch, "ts": ts or "2026-07-06T05:00:00Z",
        "features": features or [],
    }


def _write_jsonl_tmp(records, tmpdir, name):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _run_selftest():
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}: {label}")
        if not cond:
            ok = False

    base = _baseline_epoch("2026-07-06 04:26:00")
    after = base + 3600

    # SKIP - invalidation: one win (+1R, cost 0.2R -> net +0.8), one loss (-1R, cost 0.2 -> net -1.2)
    skip_inval_closed = [
        _fixture_label("AAA", "1h", "SKIP", "no clear invalidation level", 1, 1.0, 0.2, after, features=["sr_multi", "gp"]),
        _fixture_label("BBB", "1h", "SKIP", "lacks a strong invalidation point", -1, -1.0, 0.2, after, features=["sr_multi"]),
    ]
    # SKIP - R:R: one time-expiry drift outcome (label=0), net R = drift - cost
    skip_rr_closed = [
        _fixture_label("CCC", "4h", "SKIP", "required r:r of 2.0:1 cannot be met", 0, 0.35, 0.05, after, features=["fib_50"]),
    ]
    # NOT_ESCALATED - cooldown: one NO_FILL (excluded from win rate), one win
    ne_cooldown_closed = [
        _fixture_label("DDD", "15m", "NOT_ESCALATED", "cooldown", "NO_FILL", None, None, after),
        _fixture_label("EEE", "15m", "NOT_ESCALATED", "cooldown", 1, 1.0, 0.1, after, features=["gp"]),
    ]
    # One still-open (unresolved) record in the cooldown bucket -- must count toward
    # setups/coverage but NEVER be given a guessed outcome.
    ne_cooldown_open = [
        _fixture_label("FFF", "15m", "NOT_ESCALATED", "cooldown", None, None, None, after),
    ]
    # A VOID record in its own bucket -- excluded from win rate same as NO_FILL.
    ne_choppy_closed = [
        _fixture_label("GGG", "1h", "NOT_ESCALATED", "choppy_regime", "VOID", None, None, after),
    ]
    # A pre-baseline record that must be filtered OUT entirely.
    pre_baseline = [
        _fixture_label("ZZZ", "1h", "SKIP", "no clear invalidation level", 1, 1.0, 0.2, base - 3600),
    ]

    # PHASE 23: a gate-killed decision that DOES have a matching Examiner label (the new
    # early-POST shadow stamp dexter.py now writes before any gate can kill a proposal).
    # Proves the symbol+tf+timestamp join actually works -- the label's chev_decision is
    # "POST", not "REJECT"; requiring them to match was the actual historical bug, fixed
    # both in dexter.py (the new record_setup call) and here (the join predicate).
    # net = realized_R(1.0) - cost_R(0.2) = +0.8 -> "wrong" (Chev's killed proposal would
    # have won; the gate forfeited a real winner).
    gate_kill_decision_ts = "2026-07-06 06:00:00"
    gate_kill_epoch = _decision_ts_epoch({"ts": gate_kill_decision_ts})
    gate_kill_matched_post = [
        _fixture_label("HHH", "1h", "POST", None, 1, 1.0, 0.2, gate_kill_epoch - 120),
    ]

    closed = (skip_inval_closed + skip_rr_closed + ne_cooldown_closed + ne_choppy_closed
              + pre_baseline + gate_kill_matched_post)
    opens  = ne_cooldown_open
    decs = [
        {"ts": "2026-07-06 05:00:00", "decision": "REJECT",
         "reason": "Round-trip cost 0.30% of notional = 0.35R at stop 0.40% — exceeds max 0.25R for day."},
        {"ts": "2026-07-06 05:05:00", "decision": "REJECT",
         "reason": "Already 3 day trades open/pending — max is 2."},
        {"ts": "2026-07-06 05:10:00", "decision": "MTF_TAX_REJECT", "reason": "counter-trend, score too low"},
        {"ts": "2026-07-05 05:10:00", "decision": "REJECT",
         "reason": "Round-trip cost — this one is BEFORE baseline and must be excluded."},
        {"ts": gate_kill_decision_ts, "symbol": "HHH", "tf": "1h", "decision": "REJECT",
         "reason": "Round-trip cost 0.30% of notional = 0.35R at stop 0.40% — exceeds max 0.25R for day."},
    ]

    closed_f = [r for r in closed if (r.get("ts_epoch") or 0) >= base]
    opens_f  = [r for r in opens  if (r.get("ts_epoch") or 0) >= base]
    decs_f   = [r for r in decs   if (_decision_ts_epoch(r) or 0) >= base]

    check("pre-baseline record filtered out of closed set", len(closed_f) == len(closed) - 1)

    # -- SKIP-invalidation bucket (via low-level helpers, unchanged from PHASE 7) --
    skip_buckets = _group_closed_open(closed_f, opens_f, "SKIP", classify_skip_reason)
    inval_closed, inval_open = skip_buckets["SKIP - invalidation"]
    inval_stats = bucket_stats(inval_closed, inval_open)
    check("SKIP-invalidation: n_setups == 2", inval_stats["n_setups"] == 2)
    check("SKIP-invalidation: shadow win rate == 50.0%", abs(inval_stats["shadow_win_rate"] - 50.0) < 1e-9)
    # net Rs: (1.0-0.2)=+0.8, (-1.0-0.2)=-1.2 -> avg = -0.2, total = -0.4
    check("SKIP-invalidation: avg net R == -0.200", abs(inval_stats["avg_net_R"] - (-0.2)) < 1e-9)
    check("SKIP-invalidation: total net R == -0.40", abs(inval_stats["total_net_R"] - (-0.4)) < 1e-9)

    # -- SKIP-R:R bucket --
    rr_closed, rr_open = skip_buckets["SKIP - R:R"]
    rr_stats = bucket_stats(rr_closed, rr_open)
    check("SKIP-R:R: n_filled == 1", rr_stats["n_filled"] == 1)
    check("SKIP-R:R: avg net R == 0.300", abs(rr_stats["avg_net_R"] - 0.30) < 1e-9)
    check("SKIP-R:R: shadow win rate == 0.0% (label=0 is not a win)", abs(rr_stats["shadow_win_rate"] - 0.0) < 1e-9)

    # -- NOT_ESCALATED - cooldown bucket (NO_FILL excluded from win rate; 1 unresolved) --
    ne_buckets = _group_closed_open(closed_f, opens_f, "NOT_ESCALATED", lambda r: (r or "unknown"))
    cd_closed, cd_open = ne_buckets["cooldown"]
    cd_stats = bucket_stats(cd_closed, cd_open)
    check("NOT_ESCALATED-cooldown: n_setups == 3 (2 closed + 1 open)", cd_stats["n_setups"] == 3)
    check("NOT_ESCALATED-cooldown: n_open_unresolved == 1", cd_stats["n_open_unresolved"] == 1)
    check("NOT_ESCALATED-cooldown: no_fill == 1", cd_stats["no_fill"] == 1)
    check("NOT_ESCALATED-cooldown: n_filled == 1 (the win only, NO_FILL excluded)", cd_stats["n_filled"] == 1)
    check("NOT_ESCALATED-cooldown: shadow win rate == 100.0%", abs(cd_stats["shadow_win_rate"] - 100.0) < 1e-9)
    check("NOT_ESCALATED-cooldown: coverage == 66.7%", abs(cd_stats["coverage_pct"] - (2 / 3 * 100)) < 1e-6)

    # -- NOT_ESCALATED - choppy_regime bucket (VOID only -> no numeric outcome) --
    ch_closed, ch_open = ne_buckets["choppy_regime"]
    ch_stats = bucket_stats(ch_closed, ch_open)
    check("NOT_ESCALATED-choppy_regime: void == 1", ch_stats["void"] == 1)
    check("NOT_ESCALATED-choppy_regime: shadow win rate is None (no filled outcomes)", ch_stats["shadow_win_rate"] is None)

    # -- Downstream gate-kill counts (classification + baseline filtering) --
    gate_kill_counts = {}
    for d in decs_f:
        decision = d.get("decision")
        if decision in DECISION_DIRECT_MAP:
            key = DECISION_DIRECT_MAP[decision]
        elif decision == "REJECT":
            key = classify_reject_reason(d.get("reason"))
        else:
            continue
        gate_kill_counts[key] = gate_kill_counts.get(key, 0) + 1
    check("gate-kill: pre-baseline REJECT excluded (4 decisions counted, not 5)", sum(gate_kill_counts.values()) == 4)
    check("gate-kill: COST_GATE == 2 (the original unmatched one + the new HHH/1h one)",
          gate_kill_counts.get("COST_GATE") == 2)
    check("gate-kill: CONCURRENCY == 1", gate_kill_counts.get("CONCURRENCY") == 1)
    check("gate-kill: MTF_TAX == 1", gate_kill_counts.get("MTF_TAX") == 1)

    # -- PHASE 7B-4: build_weekly_regret() -- full history, independent of BASELINE_TS --
    # Fixed reference "now" (a Monday) so week boundaries are deterministic in the test.
    _now_fixed = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)   # a Monday
    _this_monday = _week_start(_now_fixed)
    _last_monday = _this_monday - timedelta(weeks=1)
    weekly_fixtures = [
        # This week: one dodged (-1.0R gross, 0.2 cost -> net -1.2, "right"),
        #            one forfeited (+1.0R gross, 0.1 cost -> net +0.9, "wrong").
        _fixture_label("WWW", "1h", "SKIP", "no clear invalidation level", -1, -1.0, 0.2,
                        _this_monday.timestamp() + 3600),
        _fixture_label("XXX", "1h", "SKIP", "no clear invalidation level", 1, 1.0, 0.1,
                        _this_monday.timestamp() + 7200),
        # Last week: one forfeited (+1.0R gross, 0.05 cost -> net +0.95, "wrong").
        _fixture_label("YYY", "1h", "NOT_ESCALATED", "cooldown", 1, 1.0, 0.05,
                        _last_monday.timestamp() + 3600),
        # A POST record -- must be excluded (not SKIP/NOT_ESCALATED).
        _fixture_label("ZZZ", "1h", "POST", None, 1, 1.0, 0.0, _this_monday.timestamp() + 3600),
    ]
    weekly = build_weekly_regret(weekly_fixtures, weeks=3, now=_now_fixed)
    check("weekly_regret: returns exactly 3 weeks", len(weekly) == 3)
    check("weekly_regret: weeks are oldest-first (last week before this week)",
          weekly[-2]["week_start"] < weekly[-1]["week_start"])
    check("weekly_regret: this week's forfeited_r == 0.90 (XXX only; WWW is dodged)",
          abs(weekly[-1]["forfeited_r"] - 0.90) < 1e-6)
    check("weekly_regret: this week's dodged_r == 1.20 (WWW only)",
          abs(weekly[-1]["dodged_r"] - 1.20) < 1e-6)
    check("weekly_regret: this week's n_resolved == 2 (WWW + XXX, POST excluded)",
          weekly[-1]["n_resolved"] == 2)
    check("weekly_regret: last week's forfeited_r == 0.95 (YYY)",
          abs(weekly[-2]["forfeited_r"] - 0.95) < 1e-6)
    check("weekly_regret: oldest tracked week (3 back) is empty (no fixture data that far back)",
          weekly[0]["n_resolved"] == 0 and weekly[0]["forfeited_r"] == 0.0)

    # -- PHASE 7B-1: build_counterfactual() end-to-end, via temp fixture files --
    with tempfile.TemporaryDirectory() as tmp:
        closed_path = _write_jsonl_tmp(closed, tmp, "labels_closed.jsonl")
        open_path   = _write_jsonl_tmp(opens, tmp, "labels_open.jsonl")
        dec_path    = _write_jsonl_tmp(decs, tmp, "chev_decisions.jsonl")

        data = build_counterfactual(
            baseline_ts="2026-07-06 04:26:00",
            labels_closed_file=closed_path, labels_open_file=open_path, decisions_file=dec_path,
        )

        check("build_counterfactual: top-level keys present", all(
            k in data for k in ("generated_at", "baseline_ts", "coverage", "headline",
                                 "buckets", "shadow_tags", "shadow_combos", "resolved_items", "weekly")
        ))
        check("build_counterfactual: weekly is a 6-entry list (default weeks=6)",
              isinstance(data["weekly"], list) and len(data["weekly"]) == 6)

        by_key = {b["key"]: b for b in data["buckets"]}
        check("build_counterfactual: SKIP - invalidation bucket present, kind=skip",
              by_key["SKIP - invalidation"]["kind"] == "skip")
        check("build_counterfactual: SKIP - invalidation verdict_split right=1 wrong=1 pending=0",
              by_key["SKIP - invalidation"]["verdict_split"] == {"right": 1, "wrong": 1, "pending": 0})
        # AAA net=+0.8 -> wrong (missed a winner); BBB net=-1.2 -> right (dodged a loser)

        ne_cd = by_key["cooldown"]
        check("build_counterfactual: cooldown bucket kind=gate", ne_cd["kind"] == "gate")
        check("build_counterfactual: cooldown verdict_split right=0 wrong=1 pending=2 (NO_FILL + still-open)",
              ne_cd["verdict_split"] == {"right": 0, "wrong": 1, "pending": 2})

        gate_kill_keys = {b["key"] for b in data["buckets"] if b["key"].startswith("GATE-")}
        check("build_counterfactual: downstream gate-kill buckets present (GATE-COST_GATE etc.)",
              "GATE-COST_GATE" in gate_kill_keys and "GATE-CONCURRENCY" in gate_kill_keys
              and "GATE-MTF_TAX" in gate_kill_keys)
        # PHASE 23: GATE-COST_GATE now carries TWO decisions -- the original one (no
        # symbol/tf on its fixture, so it can never match anything -> stays pending,
        # exactly like every gate-kill did before this phase) and the new HHH/1h one
        # (matches the new early-POST label -> resolves for real). n=2, resolved=1.
        gk = by_key["GATE-COST_GATE"]
        check("build_counterfactual: GATE-COST_GATE n == 2 (1 unmatched + 1 matched)", gk["n"] == 2)
        check("build_counterfactual: GATE-COST_GATE resolved == 1 (the HHH/1h match only)",
              gk["resolved"] == 1)
        check("build_counterfactual: GATE-COST_GATE shadow_wr == 100.0% (HHH's label=1 is a win)",
              gk["shadow_wr"] is not None and abs(gk["shadow_wr"] - 100.0) < 1e-9)
        check("build_counterfactual: GATE-COST_GATE avg_r/total_r == +0.80 (1.0 realized - 0.2 cost)",
              abs(gk["avg_r"] - 0.80) < 1e-9 and abs(gk["total_r"] - 0.80) < 1e-9)
        check("build_counterfactual: GATE-COST_GATE verdict_split right=0 wrong=1 pending=1 "
              "(HHH net=+0.8 is a forfeited winner; the old unmatched decision stays pending)",
              gk["verdict_split"] == {"right": 0, "wrong": 1, "pending": 1})

        # A gate-kill bucket with NO matching label anywhere behaves exactly as before this
        # phase -- fully pending, no shadow data, nothing invented.
        gk_mtf = by_key["GATE-MTF_TAX"]
        check("build_counterfactual: GATE-MTF_TAX (no matching label anywhere) still resolved=0, shadow_wr=None",
              gk_mtf["resolved"] == 0 and gk_mtf["shadow_wr"] is None
              and gk_mtf["verdict_split"]["pending"] == gk_mtf["n"])

        # headline: dodged_r = |BBB net| -- only "right" verdicts count toward dodged_r.
        # Right verdicts across all buckets: BBB (-1.2). EEE (net=+0.9) is a win -> "wrong"
        # (forfeited). CCC (net=+0.30) -> "wrong". HHH (net=+0.8, the new gate-kill match)
        # -> "wrong" (a real winner the cost gate forfeited).
        # dodged_r = 1.2 ; forfeited_r = 0.8 (AAA) + 0.9 (EEE) + 0.30 (CCC) + 0.8 (HHH) = 2.80
        check("build_counterfactual: headline.dodged_r == 1.20", abs(data["headline"]["dodged_r"] - 1.20) < 1e-6)
        check("build_counterfactual: headline.forfeited_r == 2.80 (includes HHH's new +0.80)",
              abs(data["headline"]["forfeited_r"] - 2.80) < 1e-6)

        # PHASE 23: gate-kill buckets now go through the same _add_bucket() as everything
        # else, so they correctly count toward coverage too -- COST_GATE(2) + CONCURRENCY(1)
        # + MTF_TAX(1) = 4 additional setups, 1 additional resolved (the HHH match).
        check("build_counterfactual: coverage.total == SKIP+NOT_ESCALATED (7) + gate-kill (4) == 11",
              data["coverage"]["total"] == 11)
        check("build_counterfactual: coverage.resolved == SKIP+NOT_ESCALATED (6) + gate-kill (1) == 7",
              data["coverage"]["resolved"] == 7)

        tag_by_name = {t["tag"]: t for t in data["shadow_tags"]}
        check("build_counterfactual: shadow_tags includes sr_multi with n=2 (AAA+BBB)",
              tag_by_name.get("sr_multi", {}).get("n") == 2)
        check("build_counterfactual: shadow_tags sr_multi total_r == -0.40 (AAA +0.8, BBB -1.2)",
              abs(tag_by_name["sr_multi"]["total_r"] - (-0.40)) < 1e-6)

        combo_by_name = {c["combo"]: c for c in data["shadow_combos"]}
        check("build_counterfactual: shadow_combos includes 'gp,sr_multi' (AAA's combo) with n=1",
              combo_by_name.get("gp,sr_multi", {}).get("n") == 1)

        check("build_counterfactual: resolved_items non-empty and each has required fields",
              len(data["resolved_items"]) > 0 and all(
                  k in data["resolved_items"][0] for k in
                  ("ts", "symbol", "tf", "decision", "bucket_key", "shadow_r", "verdict")
              ))

        # Re-run build_report() against the same fixtures to prove the text renderer and
        # build_counterfactual() never disagree (single source of truth, not a fork).
        text = build_report(
            baseline_ts="2026-07-06 04:26:00",
            labels_closed_file=closed_path, labels_open_file=open_path, decisions_file=dec_path,
        )
        check("build_report: text output mentions the SKIP - invalidation bucket",
              "[SKIP - invalidation]" in text)
        check("build_report: text output's headline dodged_r matches build_counterfactual's",
              f"{data['headline']['dodged_r']:+.2f}R" in text)

    print()
    if ok:
        print("ALL SELFTEST CASES PASSED")
    else:
        print("SELFTEST FAILURES")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _ok = _run_selftest()
        raise SystemExit(0 if _ok else 1)
    out_path = write_report()
    print(f"Wrote {out_path}")
