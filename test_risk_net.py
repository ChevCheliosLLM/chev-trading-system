"""
Standalone test for the Phase 4 risk_amount_usd safety net.
Does NOT import dexter.py (it starts a live bot on import). This reimplements the
exact snippet inserted at check_and_update_open_trades' OPEN-trade choke point
(dexter.py, anchor "Phase 4 safety net") as an isolated function so the math can
be verified without any network/worksheet/global-state dependencies.
Run: python test_risk_net.py
"""

failures = []

def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  ok:   {label}")


def apply_risk_net(trade, balance):
    """Mirrors the exact logic inserted in dexter.py's check_and_update_open_trades."""
    fired = False
    if not trade.get("risk_amount_usd"):
        fired = True
        net_risk_pct = trade.get("risk_pct") or 1.0
        net_risk     = round(net_risk_pct / 100 * balance, 2)
        net_cap      = round(0.02 * balance, 2)
        if net_risk > net_cap and net_risk > 0:
            net_scale = net_cap / net_risk
            trade["position_size_usd"] = round(trade.get("position_size_usd", 0) * net_scale, 2)
            net_risk = net_cap
        trade["risk_amount_usd"] = net_risk
    return trade, fired


# ---------------------------------------------------------------------------
print("Scenario 1: risk_amount_usd=0, risk under the 2% cap -> no position scaling")
t = {"symbol": "ZECUSDT", "risk_amount_usd": 0, "risk_pct": 1.5, "position_size_usd": 500.0}
t, fired = apply_risk_net(t, balance=1000.0)
check("net fired", fired is True)
check("risk computed = 1.5% of 1000 = 15.00", t["risk_amount_usd"] == 15.00)
check("position_size_usd unchanged (under cap)", t["position_size_usd"] == 500.0)


# ---------------------------------------------------------------------------
print("Scenario 2: risk_amount_usd=0, risk OVER the 2% cap -> capped + position scaled down")
t = {"symbol": "DOGEUSDT", "risk_amount_usd": 0, "risk_pct": 5.0, "position_size_usd": 1000.0}
t, fired = apply_risk_net(t, balance=1000.0)
check("net fired", fired is True)
check("risk capped at 2% of 1000 = 20.00 (not 50.00)", t["risk_amount_usd"] == 20.00)
check("position_size_usd scaled down proportionally (1000 * 20/50 = 400)", t["position_size_usd"] == 400.0)


# ---------------------------------------------------------------------------
print("Scenario 3: risk_amount_usd KEY MISSING entirely (not just 0) -> still fires")
t = {"symbol": "UNIUSDT", "risk_pct": 1.0, "position_size_usd": 300.0}
t, fired = apply_risk_net(t, balance=2000.0)
check("net fired on missing key", fired is True)
check("risk computed = 1% of 2000 = 20.00", t["risk_amount_usd"] == 20.00)


# ---------------------------------------------------------------------------
print("Scenario 4: risk_amount_usd already set (nonzero) -> net does NOT fire, values untouched")
t = {"symbol": "BNBUSDT", "risk_amount_usd": 42.50, "risk_pct": 1.5, "position_size_usd": 777.0}
t, fired = apply_risk_net(t, balance=1000.0)
check("net did not fire", fired is False)
check("risk_amount_usd untouched", t["risk_amount_usd"] == 42.50)
check("position_size_usd untouched", t["position_size_usd"] == 777.0)


# ---------------------------------------------------------------------------
print("Scenario 5: risk_pct explicitly 0 (not missing) -> falls back to 1.0, not 0")
t = {"symbol": "ETHUSDT", "risk_amount_usd": 0, "risk_pct": 0, "position_size_usd": 100.0}
t, fired = apply_risk_net(t, balance=1000.0)
check("net fired", fired is True)
check("risk_pct=0 falls back to 1.0 -> risk = 10.00, not 0.00", t["risk_amount_usd"] == 10.00)


# ---------------------------------------------------------------------------
print("Scenario 6: balance=0 edge case -> no crash, risk=0, no scaling")
t = {"symbol": "BTCUSDT", "risk_amount_usd": 0, "risk_pct": 2.0, "position_size_usd": 100.0}
t, fired = apply_risk_net(t, balance=0.0)
check("net fired without raising", fired is True)
check("risk = 0 when balance = 0", t["risk_amount_usd"] == 0.0)
check("position_size_usd unchanged when balance = 0", t["position_size_usd"] == 100.0)


# ---------------------------------------------------------------------------
print("Scenario 7: exactly AT the 2% cap boundary -> counts as passing, no scaling triggered")
t = {"symbol": "SOLUSDT", "risk_amount_usd": 0, "risk_pct": 2.0, "position_size_usd": 250.0}
t, fired = apply_risk_net(t, balance=1000.0)
check("risk exactly at cap (20.00) not reduced further", t["risk_amount_usd"] == 20.00)
check("position_size_usd unchanged at exact boundary", t["position_size_usd"] == 250.0)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
