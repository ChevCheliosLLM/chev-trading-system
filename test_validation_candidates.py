"""
test_validation_candidates.py
Standalone test for the VALIDATION CANDIDATES ENGINE (engines.py).
Does NOT import dexter.py (it starts a live bot on import). Exercises
compute_validation_candidates + format_validation_candidates_for_chev directly.
Run: python test_validation_candidates.py
"""
failures = []


def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  ok:   {label}")


from engines import compute_validation_candidates, format_validation_candidates_for_chev

print("Scenario 1: LONG — target + structural extreme + R:R floor")
c = compute_validation_candidates(
    direction="long",
    entry_price=100.0,
    target_price=110.0,
    structural_rr=2.0,
    reward_profile="MID_BALANCE",
    expected_trigger="15m close back above 1.0490",
    auction_extreme=115.0,
    risk_distance=5.0,
)
check("returns 3 candidates", len(c) == 3)
check("candidate 1 is THESIS-TARGET = 110.0", c[0].label == "THESIS-TARGET" and c[0].price == 110.0)
check("thesis-target 10% from entry", c[0].pct_from_entry == 10.0)
check("thesis-target OK vs R:R floor (rr 2.0 >= 1.0)", c[0].meets_floor is True)
check("candidate 2 is STRUCTURAL EXTREME = 115.0", c[1].label == "STRUCTURAL EXTREME" and c[1].price == 115.0)
check("candidate 3 is R:R FLOOR = 105.0 (entry + 1.0*risk_dist)", c[2].label == "R:R FLOOR" and c[2].price == 105.0)
check("rr floor meets_floor True by construction", c[2].meets_floor is True)
print(format_validation_candidates_for_chev(c))

print("Scenario 2: SHORT — direction-awareness")
c = compute_validation_candidates(
    direction="short",
    entry_price=100.0,
    target_price=90.0,
    structural_rr=2.0,
    reward_profile="AT_BALANCE_EXTREME",
    auction_extreme=85.0,
    risk_distance=5.0,
)
check("thesis-target below entry for short", c[0].price == 90.0 and c[0].price < 100.0)
check("structural extreme furthest below entry", c[1].price == 85.0)
check("rr floor = entry - 1.0*risk_dist = 95.0", c[2].price == 95.0)
print(format_validation_candidates_for_chev(c))

print("Scenario 3: THIN reward — structural_rr below floor -> THESIS-TARGET THIN")
c = compute_validation_candidates(
    direction="long",
    entry_price=100.0,
    target_price=102.0,
    structural_rr=0.4,           # below the 1.0 floor
    reward_profile="MID_BALANCE",
    auction_extreme=103.0,
    risk_distance=5.0,
)
check("rr 0.4 < 1.0 -> thesis-target THIN", c[0].meets_floor is False)
check("formatter prints THIN", "THIN" in format_validation_candidates_for_chev(c))
print(format_validation_candidates_for_chev(c))

print("Scenario 4: no data at all -> empty list, 'none found' text")
c = compute_validation_candidates(direction="long", entry_price=100.0)
check("empty candidate list when nothing is available", c == [])
check("formatter prints 'none found'",
      format_validation_candidates_for_chev(c).strip() == "VALIDATION CANDIDATES: none found")
print(format_validation_candidates_for_chev(c))

print("Scenario 5: target present but structural extreme equals target -> dedupe (no duplicate)")
c = compute_validation_candidates(
    direction="long",
    entry_price=100.0,
    target_price=110.0,
    structural_rr=2.0,
    reward_profile="MID_BALANCE",
    auction_extreme=110.0,       # identical to target -> should be skipped
    risk_distance=5.0,
)
check("extreme identical to target is not duplicated", len(c) == 2)
check("only THESIS-TARGET + R:R FLOOR remain", c[0].label == "THESIS-TARGET" and c[1].label == "R:R FLOOR")
print(format_validation_candidates_for_chev(c))

print("Scenario 6: no opportunity (risk_distance 0) -> R:R FLOOR candidate skipped")
c = compute_validation_candidates(
    direction="long",
    entry_price=100.0,
    target_price=110.0,
    structural_rr=2.0,
    reward_profile="MID_BALANCE",
    auction_extreme=115.0,
    risk_distance=0.0,           # no stop distance -> can't price the floor
)
check("rr floor skipped when risk_distance is 0", all(x.label != "R:R FLOOR" for x in c))
print(format_validation_candidates_for_chev(c))

print("Scenario 7: label_suffix for direction-agnostic printing")
c = compute_validation_candidates(
    direction="long", entry_price=100.0, target_price=110.0,
    structural_rr=2.0, reward_profile="MID_BALANCE", auction_extreme=115.0, risk_distance=5.0,
)
check("suffix appears in header", "IF LONG" in format_validation_candidates_for_chev(c, " (IF LONG)"))
print(format_validation_candidates_for_chev(c, " (IF LONG)"))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
