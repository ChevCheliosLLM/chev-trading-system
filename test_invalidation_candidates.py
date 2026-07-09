"""
Standalone test for engines.compute_invalidation_candidates (Phase 3).
Imports ONLY engines.py -- never dexter.py (dexter.py starts a live bot on import).
Run: python test_invalidation_candidates.py
"""

from engines import compute_invalidation_candidates, format_invalidation_candidates_for_chev

failures = []

def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  ok:   {label}")


# ---------------------------------------------------------------------------
print("Scenario 1: LONG pullback / golden pocket")
# Entry at 100, fib leg anchored 80 (low) -> 110 (high). Long thesis dies below the anchor low.
c = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="golden_pocket",
    noise_floor_distance=1.0,   # ATR floor stop distance
    fib_anchor_high=110.0,
    fib_anchor_low=80.0,
    sr_levels=[{"price": 75.0, "kind": "support"}, {"price": 120.0, "kind": "resistance"}],
    val=78.0,
    vah=115.0,
)
check("returns 3 candidates", len(c) == 3)
check("candidate 1 is THESIS-KILLER", c[0].label == "THESIS-KILLER")
check("thesis-killer = fib anchor LOW (80.0) for a long", c[0].price == 80.0)
check("thesis-killer is below entry (correct side for long)", c[0].price < 100.0)
check("candidate 2 is STRUCTURAL BACKSTOP", c[1].label == "STRUCTURAL BACKSTOP")
check("backstop = nearest SR/VAL beyond 80.0 -> 78.0 (VAL, nearer than SR 75.0)", c[1].price == 78.0)
check("backstop is further from entry than the killer", c[1].price < c[0].price)
check("candidate 3 is NOISE FLOOR", c[2].label == "NOISE FLOOR")
check("noise floor = entry - floor_distance = 99.0", c[2].price == 99.0)
check("noise floor note says not structural", "not structural" in c[2].note)
print(format_invalidation_candidates_for_chev(c))


# ---------------------------------------------------------------------------
print("Scenario 2: SHORT range-fade")
# Entry at 100 (faded off range highs), range 90-100. Short thesis dies above the range high.
c = compute_invalidation_candidates(
    direction="short",
    entry_price=100.0,
    hypothesis_type="range_fade",
    noise_floor_distance=1.5,
    range_low=90.0,
    range_high=100.0,
    sr_levels=[{"price": 105.0, "kind": "resistance"}, {"price": 85.0, "kind": "support"}],
    val=88.0,
    vah=102.0,
)
check("returns 3 candidates", len(c) == 3)
check("thesis-killer = range_high (100.0) for a short range-fade", c[0].price == 100.0)
check("thesis-killer is at/above entry (correct side for short)", c[0].price >= 100.0)
check("backstop = nearest beyond 100.0 -> 102.0 (VAH, nearer than SR 105.0)", c[1].price == 102.0)
check("backstop is further from entry than the killer", c[1].price > c[0].price)
check("noise floor = entry + floor_distance = 101.5", c[2].price == 101.5)
print(format_invalidation_candidates_for_chev(c))


# ---------------------------------------------------------------------------
print("Scenario 3: LONG breakout")
# Entry at 100, broke out above prior resistance at 95. Thesis dies back inside (95.0).
c = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="breakout",
    noise_floor_distance=2.0,
    breakout_level=95.0,
    sr_levels=[{"price": 90.0, "kind": "support"}],
    val=None,
    vah=None,
)
check("returns 3 candidates", len(c) == 3)
check("thesis-killer = breakout_level (95.0)", c[0].price == 95.0)
check("thesis-killer is below entry (correct side for long)", c[0].price < 100.0)
check("backstop = nearest SR beyond 95.0 -> 90.0", c[1].price == 90.0)
check("noise floor = entry - floor_distance = 98.0", c[2].price == 98.0)
print(format_invalidation_candidates_for_chev(c))


# ---------------------------------------------------------------------------
print("Scenario 4: SHORT breakout (direction-awareness sanity check)")
c = compute_invalidation_candidates(
    direction="short",
    entry_price=50.0,
    hypothesis_type="breakout",
    noise_floor_distance=0.5,
    breakout_level=52.0,   # broke below 52, thesis dies back above it
    sr_levels=[{"price": 55.0, "kind": "resistance"}],
)
check("thesis-killer = breakout_level (52.0)", c[0].price == 52.0)
check("thesis-killer is above entry (correct side for short)", c[0].price > 50.0)
check("backstop = nearest SR beyond 52.0 -> 55.0", c[1].price == 55.0)


# ---------------------------------------------------------------------------
print("Scenario 5: unknown hypothesis type falls back to nearest_swing_beyond")
c = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="ASCENDING_TRIANGLE",  # not in any known bucket
    noise_floor_distance=1.0,
    nearest_swing_beyond=88.0,
)
check("falls back to nearest_swing_beyond", c[0].price == 88.0)
check("still labeled THESIS-KILLER", c[0].label == "THESIS-KILLER")


# ---------------------------------------------------------------------------
print("Scenario 6: no data at all -> empty list, 'none found' text")
c = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="unknown",
    noise_floor_distance=0.0,   # no floor either
)
check("empty candidate list when nothing is available", c == [])
check("formatter prints 'none found'", format_invalidation_candidates_for_chev(c).strip() == "INVALIDATION CANDIDATES: none found")


# ---------------------------------------------------------------------------
print("Scenario 7: PASS/FAIL vs noise floor")
c = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="golden_pocket",
    noise_floor_distance=5.0,   # wide floor
    fib_anchor_low=99.0,        # thesis-killer only 1.0 away -- should FAIL the floor
)
check("thesis-killer within the noise floor distance FAILS", c[0].passes_floor is False)

c2 = compute_invalidation_candidates(
    direction="long",
    entry_price=100.0,
    hypothesis_type="golden_pocket",
    noise_floor_distance=0.5,   # tight floor
    fib_anchor_low=80.0,        # far beyond the floor -- should PASS
)
check("thesis-killer well beyond the noise floor distance PASSES", c2[0].passes_floor is True)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
