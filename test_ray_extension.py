"""
test_ray_extension.py — offline self-test for Phase R1 (patterns.py ray extension fix)

No dexter import, no network. Verifies _trendline_endpoints() now honors
n_extend_bars instead of the previous min()-mixup that silently discarded it.

Run: python test_ray_extension.py
"""
import pandas as pd

from patterns import _trendline_endpoints, _project

T = []


def check(name, cond):
    T.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


# ── Synthetic fixture: 30 hourly bars, known linear pivots ──────────────────
N = 30
BAR_SECONDS = 3600  # 1h bars
df = pd.DataFrame(
    {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0},
    index=pd.date_range("2024-01-01", periods=N, freq="1h"),
)

SLOPE     = 2.0
INTERCEPT = 100.0
ANCHOR    = 0
SWING_INDICES = [0, 5, 10]  # start_local = swing_indices_local[0] = 0

last_idx = N - 1
last_ts  = int(df.index[-1].timestamp())

# ── Case A: default n_extend_bars=25 (the fixed behaviour) ──────────────────
ep25 = _trendline_endpoints(SWING_INDICES, SLOPE, INTERCEPT, ANCHOR, df, n_extend_bars=25)

# 1. t2 exceeds the last candle timestamp by exactly n_extend_bars * bar_seconds
check("t2 extends by exactly n_extend_bars*bar_seconds",
      ep25["t2"] - last_ts == 25 * BAR_SECONDS)

# 2. p2 equals _project(...) evaluated at the extended bar index, within float tolerance
expected_p2 = _project(SLOPE, INTERCEPT, ANCHOR, last_idx + 25)
check("p2 matches _project at extended bar index",
      abs(ep25["p2"] - expected_p2) < 1e-6)

# ── Case B: n_extend_bars=0 reproduces pre-fix behaviour exactly ────────────
ep0 = _trendline_endpoints(SWING_INDICES, SLOPE, INTERCEPT, ANCHOR, df, n_extend_bars=0)

check("n_extend_bars=0 -> t2 == last candle timestamp",
      ep0["t2"] == last_ts)

expected_p2_at_last = _project(SLOPE, INTERCEPT, ANCHOR, last_idx)
check("n_extend_bars=0 -> p2 == projection at last bar",
      abs(ep0["p2"] - expected_p2_at_last) < 1e-6)

# 4. t1/p1 unchanged by the fix (identical across both calls)
check("t1 unchanged between n_extend_bars=0 and n_extend_bars=25",
      ep25["t1"] == ep0["t1"])
check("p1 unchanged between n_extend_bars=0 and n_extend_bars=25",
      abs(ep25["p1"] - ep0["p1"]) < 1e-6)

# Sanity: p1 matches the known anchor-relative projection at start_local
expected_p1 = _project(SLOPE, INTERCEPT, ANCHOR, SWING_INDICES[0])
check("p1 matches known linear pivot value",
      abs(ep25["p1"] - expected_p1) < 1e-6)

failed = [n for n, ok in T if not ok]
print(f"\n{len(T) - len(failed)}/{len(T)} tests passing")
if failed:
    raise SystemExit("FAILED: " + ", ".join(failed))
