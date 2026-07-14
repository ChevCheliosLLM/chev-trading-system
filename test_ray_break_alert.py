"""
test_ray_break_alert.py — offline self-test for Phase R5 (ray break -> urgent alert)

No dexter import, no network, bot never run. dexter.py has no `if __name__ ==
"__main__"` guard (confirmed by reading it) -- its main scan loop sits at true
module level, so `import dexter` would start the live bot immediately. That is
exactly why the fire/skip/paragraph logic for _maybe_send_ray_break_alert was
split into two pure, importable functions in ray_registry.py
(select_break_alert_candidate, format_ray_break_paragraph_for_chev) -- this
test exercises those two functions directly, which is the entire testable
surface of Phase R5's decision logic. It does not call, thread, or send
anything -- format_ray_break_paragraph_for_chev only ever returns a string.

Run: python test_ray_break_alert.py
"""
from ray_registry import RayRecord, select_break_alert_candidate, format_ray_break_paragraph_for_chev

T = []


def check(name, cond):
    T.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


T0 = 1_700_000_000

live_ray = RayRecord(id="live1", symbol="BTCUSDT", timeframe="1h", side="upper",
                      slope_raw=-0.5, slope_norm=-0.05, anchor_ts=T0, value_at_anchor=61500.0,
                      born_ts=T0, last_seen_ts=T0, state="LIVE")

broken_ray = RayRecord(id="brk1", symbol="BTCUSDT", timeframe="1h", side="lower",
                        slope_raw=0.4, slope_norm=0.04, anchor_ts=T0, value_at_anchor=61000.0,
                        born_ts=T0, last_seen_ts=T0 + 3600 * 20, state="BROKEN",
                        last_break_ts=T0 + 3600 * 20, respect_count=4, wick_rejection_count=1,
                        lifetime_span_bars=44)

rays_for_trade = [live_ray, broken_ray]

# ── 1. Fires once: a fresh BROKEN ray with an empty alerted-set is selected ──
already_alerted = set()
target = select_break_alert_candidate(rays_for_trade, already_alerted)
check("1a: LIVE ray is never a candidate", target is not None and target.id != live_ray.id)
check("1b: the BROKEN ray is selected on first check", target is not None and target.id == broken_ray.id)

already_alerted.add(target.id)

# ── 2. Never twice for the same ray+trade ────────────────────────────────────
target_again = select_break_alert_candidate(rays_for_trade, already_alerted)
check("2: same BROKEN ray is never selected again once alerted for this trade",
      target_again is None)

# ── 3. Fires for a second, DISTINCT ray (new structure, not old news) ───────
second_broken_ray = RayRecord(id="brk2", symbol="BTCUSDT", timeframe="1h", side="upper",
                               slope_raw=-0.3, slope_norm=-0.03, anchor_ts=T0, value_at_anchor=62000.0,
                               born_ts=T0, last_seen_ts=T0 + 3600 * 30, state="BROKEN",
                               last_break_ts=T0 + 3600 * 30, respect_count=2, wick_rejection_count=0,
                               lifetime_span_bars=20)
rays_with_second = rays_for_trade + [second_broken_ray]
target_second = select_break_alert_candidate(rays_with_second, already_alerted)
check("3: a second, distinct BROKEN ray IS selected (already_alerted only blocks its own id)",
      target_second is not None and target_second.id == second_broken_ray.id)

# A reclaim never resurrects a BROKEN id (Phase R2) -- simulate the id staying
# BROKEN forever and confirm it still never re-fires once alerted, no matter
# how many more checks happen.
for _ in range(5):
    check("2b (repeat): still never re-selected after many subsequent checks",
          select_break_alert_candidate(rays_for_trade, already_alerted) is None)

# ── 4. Paragraph construction (fact-framed, BOS-style shape) ────────────────
# Does NOT thread, call, or send anything -- pure string construction only.
paragraph_with_reason = format_ray_break_paragraph_for_chev(
    broken_ray, "BTCUSDT", "1h",
    still_valid_line="your stated thesis/invalidation from entry: close back below 60800")

check("4a: paragraph names what broke (side + slope class)",
      "support" in paragraph_with_reason and "rising" in paragraph_with_reason)
check("4b: paragraph has the exact BOS-style three-part shape",
      "What happened" in paragraph_with_reason
      and "Still valid if" in paragraph_with_reason
      and "Watch for" in paragraph_with_reason)
check("4c: paragraph states the lifetime record (respect/wick-trap counts)",
      "4x" in paragraph_with_reason and "1 wick-traps" in paragraph_with_reason)
check("4d: still_valid_line is carried through verbatim",
      "close back below 60800" in paragraph_with_reason)
check("4e: watch-for names a retest from the other side (not a directive to act)",
      "retest" in paragraph_with_reason.lower())

paragraph_no_reason = format_ray_break_paragraph_for_chev(
    broken_ray, "BTCUSDT", "1h",
    still_valid_line="no invalidation or reasoning recorded at entry for this trade — "
                     "review whether this ray was part of your premise")
check("4f: missing-reasoning fallback line is used honestly, not fabricated",
      "no invalidation or reasoning recorded" in paragraph_no_reason)

failed = [n for n, ok in T if not ok]
print(f"\n{len(T) - len(failed)}/{len(T)} tests passing")
if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
