"""
Standalone test for the Phase 6 outcome classification rule (_classify_outcome).
Does NOT import dexter.py (it starts a live bot on import). Mirrors the exact logic
inserted at dexter.py's _classify_outcome, anchored right before _do_postmortem.
Run: python test_outcome_classify.py
"""

failures = []

def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  ok:   {label}")


def _classify_outcome(base_outcome, pnl, trade):
    """Mirrors dexter.py's _classify_outcome exactly."""
    wrapped = base_outcome.startswith("CLOSED (") and base_outcome.endswith(")")
    inner = base_outcome[len("CLOSED ("):-1] if wrapped else base_outcome

    if inner != "LOSS" or not trade.get("partial_done"):
        return base_outcome

    risk_usd = trade.get("risk_amount_usd", 0)
    if not risk_usd:
        return base_outcome

    partial_pnl  = trade.get("partial_net_pnl", 0.0)
    combined_pnl = pnl + partial_pnl
    combined_r   = combined_pnl / risk_usd

    if combined_r >= 0:
        inner = "WIN"
    elif combined_r >= -0.1:
        inner = "SCRATCH"
    else:
        return base_outcome

    return f"CLOSED ({inner})" if wrapped else inner


# ---------------------------------------------------------------------------
print("Scenario 1: classic case — 1R partial banked, final leg trails out to a small net loss")
# Partial: +$8 booked at 1R. Final leg: -$9. risk_amount_usd = $20 (1R). combined = -$1 = -0.05R.
trade = {"partial_done": True, "partial_net_pnl": 8.0, "risk_amount_usd": 20.0}
result = _classify_outcome("LOSS", -9.0, trade)
check("reclassified to SCRATCH (combined -0.05R, inside the -0.1R band)", result == "SCRATCH")


# ---------------------------------------------------------------------------
print("Scenario 2: partial banked, final leg negative enough that combined is still ~breakeven (SCRATCH)")
# Partial: +$10. Final leg: -$12. risk_amount_usd = $20. combined = -$2 = -0.1R exactly.
trade = {"partial_done": True, "partial_net_pnl": 10.0, "risk_amount_usd": 20.0}
result = _classify_outcome("LOSS", -12.0, trade)
check("reclassified to SCRATCH at exactly -0.1R boundary", result == "SCRATCH")


# ---------------------------------------------------------------------------
print("Scenario 3: partial banked, combined is a genuine win")
# Partial: +$30. Final leg: -$5. risk_amount_usd = $20. combined = +$25 = +1.25R.
trade = {"partial_done": True, "partial_net_pnl": 30.0, "risk_amount_usd": 20.0}
result = _classify_outcome("LOSS", -5.0, trade)
check("reclassified to WIN (combined positive)", result == "WIN")


# ---------------------------------------------------------------------------
print("Scenario 4: partial banked but combined is still a real loss (beyond -0.1R)")
# Partial: +$5. Final leg: -$20. risk_amount_usd = $20. combined = -$15 = -0.75R.
trade = {"partial_done": True, "partial_net_pnl": 5.0, "risk_amount_usd": 20.0}
result = _classify_outcome("LOSS", -20.0, trade)
check("stays LOSS (still a real loss combined)", result == "LOSS")


# ---------------------------------------------------------------------------
print("Scenario 5: no partial event at all -> rule never fires, even if it 'looks' close")
trade = {"partial_done": False, "partial_net_pnl": 0.0, "risk_amount_usd": 20.0}
result = _classify_outcome("LOSS", -1.0, trade)
check("stays LOSS (no partial_done)", result == "LOSS")

trade2 = {"risk_amount_usd": 20.0}  # partial_done key missing entirely
result2 = _classify_outcome("LOSS", -1.0, trade2)
check("stays LOSS (partial_done key missing)", result2 == "LOSS")


# ---------------------------------------------------------------------------
print("Scenario 6: already WIN -> rule never touches it")
trade = {"partial_done": True, "partial_net_pnl": 30.0, "risk_amount_usd": 20.0}
result = _classify_outcome("WIN", 40.0, trade)
check("stays WIN unchanged", result == "WIN")


# ---------------------------------------------------------------------------
print("Scenario 7: risk_amount_usd missing/0 -> rule can't compute R, no reclassification")
trade = {"partial_done": True, "partial_net_pnl": 10.0, "risk_amount_usd": 0}
result = _classify_outcome("LOSS", -1.0, trade)
check("stays LOSS (no valid risk denominator)", result == "LOSS")


# ---------------------------------------------------------------------------
print("Scenario 8: 'CLOSED (X)' wrapped format (Chev manual swing-management close) round-trips")
trade = {"partial_done": True, "partial_net_pnl": 30.0, "risk_amount_usd": 20.0}
result = _classify_outcome("CLOSED (LOSS)", -5.0, trade)
check("wrapped format reclassified and re-wrapped", result == "CLOSED (WIN)")

trade2 = {"partial_done": True, "partial_net_pnl": 10.0, "risk_amount_usd": 20.0}
result2 = _classify_outcome("CLOSED (LOSS)", -12.0, trade2)
check("wrapped format -> CLOSED (SCRATCH)", result2 == "CLOSED (SCRATCH)")


# ---------------------------------------------------------------------------
print("Scenario 9: close_type is never touched by this function (classification only)")
trade = {"partial_done": True, "partial_net_pnl": 30.0, "risk_amount_usd": 20.0, "close_type": "SL_HIT"}
result = _classify_outcome("LOSS", -5.0, trade)
check("close_type untouched", trade["close_type"] == "SL_HIT")
check("outcome still reclassified", result == "WIN")


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
