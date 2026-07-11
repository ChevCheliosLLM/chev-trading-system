"""
Standalone test for _detect_vp_anchor (dexter.py) — the VP-only auction anchor.
Does NOT import dexter.py (it starts a live bot on import). This reimplements the
exact algorithm as an isolated function, using the same helpers dexter.py already
has (_an_atr_series, _ca_volume_profile, _validate_anchor), copied verbatim, so the
logic can be verified without any network/worksheet/global-state dependencies.
Run: python test_vp_anchor.py
"""

import numpy as np
import pandas as pd

failures = []

def check(label, cond):
    if not cond:
        failures.append(label)
        print(f"  FAIL: {label}")
    else:
        print(f"  ok:   {label}")


# ---------------------------------------------------------------------------
# Verbatim copies of the relevant dexter.py helpers (see dexter.py for the
# canonical versions — kept in sync manually, same convention as the other
# test_*.py files in this repo).
# ---------------------------------------------------------------------------

def _an_atr_series(df, period=14):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"]  - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _ca_volume_profile(df, start_idx, end_idx, n_bins=24):
    lo, hi = (start_idx, end_idx) if start_idx <= end_idx else (end_idx, start_idx)
    segment = df.iloc[lo: hi + 1]
    if len(segment) < 2:
        return None
    price_min, price_max = segment["low"].min(), segment["high"].max()
    if price_max == price_min:
        return None
    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_volumes = np.zeros(n_bins)
    for _, row in segment.iterrows():
        low, high, vol = row["low"], row["high"], row["volume"]
        candle_range = high - low
        if candle_range == 0:
            idx = min(int((low - price_min) / (price_max - price_min) * n_bins), n_bins - 1)
            bin_volumes[idx] += vol
            continue
        for b in range(n_bins):
            overlap = max(0, min(high, bin_edges[b + 1]) - max(low, bin_edges[b]))
            if overlap > 0:
                bin_volumes[b] += vol * (overlap / candle_range)
    poc_idx = int(np.argmax(bin_volumes))
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2
    total_vol = bin_volumes.sum()
    if total_vol == 0:
        return None
    included, included_vol = {poc_idx}, bin_volumes[poc_idx]
    left, right = poc_idx - 1, poc_idx + 1
    while included_vol < total_vol * 0.7 and (left >= 0 or right < n_bins):
        lv = bin_volumes[left] if left >= 0 else -1
        rv = bin_volumes[right] if right < n_bins else -1
        if lv >= rv and left >= 0:
            included.add(left); included_vol += bin_volumes[left]; left -= 1
        elif right < n_bins:
            included.add(right); included_vol += bin_volumes[right]; right += 1
        else:
            break
    return {
        "poc": poc_price,
        "vah": bin_edges[max(included) + 1],
        "val": bin_edges[min(included)],
        "bin_edges": bin_edges.tolist(),
        "bin_volumes": bin_volumes.tolist(),
    }


def _validate_anchor(df, anchor):
    idx = anchor["idx"]
    n   = len(df)

    if idx >= n - 1:
        anchor["confirmed"] = False
        anchor["active"] = True
        anchor["invalidation_reason"] = None
        return anchor

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    anchor_t     = anchor["anchor_type"]
    anchor_price = anchor["price"]
    anchor_close = float(closes[idx])

    if anchor_t == "swing_high":
        is_high = True
    elif anchor_t == "swing_low":
        is_high = False
    else:
        is_high = float(closes[-1]) < anchor_close

    bal_start = max(0, idx - 20)
    if idx > bal_start:
        bal_high = float(highs[bal_start:idx].max())
        bal_low  = float(lows[bal_start:idx].min())
    else:
        bal_high = anchor_price * 1.01
        bal_low  = anchor_price * 0.99

    post_c = closes[idx + 1: min(idx + 6, n)]
    if len(post_c) < 2:
        confirmed = False
    elif is_high:
        confirmed = sum(1 for c in post_c if c < anchor_close) >= 2
    else:
        confirmed = sum(1 for c in post_c if c > anchor_close) >= 2

    rec_start = max(idx + 1, n - 10)
    rec_close = closes[rec_start:n]
    active = True
    invalidation_reason = None

    if len(rec_close) >= 3:
        inside_mask = [bal_low <= c <= bal_high for c in rec_close]
        n_inside = sum(inside_mask)
        if n_inside >= 3:
            recent_3_inside = sum(inside_mask[-3:])
            if recent_3_inside >= 2:
                active = False
                invalidation_reason = "price_returned_to_balance"
            else:
                anchor["confidence"] = min(100, anchor["confidence"] + 15)
        elif is_high:
            if sum(1 for c in rec_close if c > anchor_price) >= 2:
                active = False
                invalidation_reason = "structure_broken"
        else:
            if sum(1 for c in rec_close if c < anchor_price) >= 2:
                active = False
                invalidation_reason = "structure_broken"

    anchor["confirmed"] = confirmed
    anchor["active"] = active
    anchor["invalidation_reason"] = invalidation_reason

    if not active:
        anchor["confidence"] = max(0, anchor["confidence"] - 30)
    elif confirmed:
        anchor["confidence"] = min(100, anchor["confidence"] + 10)

    return anchor


def _detect_vp_anchor(df, max_lookback=200, pivot_bars=5, recent_window=40):
    """Mirrors dexter.py's _detect_vp_anchor exactly — see dexter.py for the
    canonical, documented version."""
    n = len(df)
    if n < 30:
        return None

    atr_s  = _an_atr_series(df, 14).values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    atr_now = float(atr_s[-1]) if not np.isnan(atr_s[-1]) else 0.0
    if atr_now <= 0:
        return None

    lb      = max(pivot_bars + 1, n - max_lookback)
    win     = atr_s[lb:]
    valid_w = win[~np.isnan(win)]
    med_atr = float(np.median(valid_w)) if len(valid_w) > 5 else atr_now

    candidates = []
    if len(valid_w) >= 15 and med_atr > 0:
        lo_t = med_atr * 0.70
        hi_t = med_atr * 1.10
        m = len(win)
        in_compression = False
        for j in range(m):
            v = win[j]
            if np.isnan(v):
                continue
            if not in_compression:
                if v <= lo_t:
                    in_compression = True
            else:
                if v >= hi_t:
                    idx_c = lb + j
                    if idx_c < n - pivot_bars:
                        candidates.append(idx_c)
                    in_compression = False

    tol = atr_now * 1.5
    MIN_OLDER_BARS   = 10
    OVERLAP_MIN_FRAC = 0.15
    for idx_c in candidates:
        vp_full = _ca_volume_profile(df, idx_c, n - 1, n_bins=24)
        if not vp_full:
            continue
        recent_start = max(idx_c, n - recent_window)
        if recent_start == idx_c:
            passed = True
        else:
            vp_recent = _ca_volume_profile(df, recent_start, n - 1, n_bins=24)
            if not vp_recent:
                continue
            value_stable = (
                abs(vp_full["poc"] - vp_recent["poc"]) <= tol and
                abs(vp_full["vah"] - vp_recent["vah"]) <= tol and
                abs(vp_full["val"] - vp_recent["val"]) <= tol
            )
            older_closes = closes[idx_c:recent_start]
            if len(older_closes) >= MIN_OLDER_BARS:
                lo_bound = vp_recent["val"] - tol
                hi_bound = vp_recent["vah"] + tol
                overlap_frac = float(np.mean((older_closes >= lo_bound) & (older_closes <= hi_bound)))
                returned_to_zone = overlap_frac >= OVERLAP_MIN_FRAC
            else:
                returned_to_zone = True
            passed = value_stable and returned_to_zone
        if passed:
            return _validate_anchor(df, {
                "idx": idx_c,
                "price": round(float(closes[idx_c]), 8),
                "anchor_type": "atr_breakout",
                "confidence": 55,
                "method": "vp_stability",
            })

    pb = 3
    current = float(closes[-1])
    fallback_candidates = []
    for i in range(lb + pb, n - pb):
        wh = highs[i - pb: i + pb + 1]
        if highs[i] == wh.max():
            d = highs[i] - current
            if d >= atr_now * 3:
                fallback_candidates.append((i, d, True))
        wl = lows[i - pb: i + pb + 1]
        if lows[i] == wl.min():
            d = current - lows[i]
            if d >= atr_now * 3:
                fallback_candidates.append((i, d, False))
    if fallback_candidates:
        best_f = max(fallback_candidates, key=lambda x: x[1])
        pv = highs[best_f[0]] if best_f[2] else lows[best_f[0]]
        return _validate_anchor(df, {
            "idx": best_f[0],
            "price": round(float(pv), 8),
            "anchor_type": "swing_high" if best_f[2] else "swing_low",
            "confidence": 30,
            "method": "fractal_fallback",
        })

    return None


# ---------------------------------------------------------------------------
# Synthetic candle builders
# ---------------------------------------------------------------------------

def make_df(rows):
    """rows: list of (open, high, low, close, volume) tuples -> OHLCV DataFrame."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def flat_segment(n, base, wiggle=0.05, vol=100):
    rows = []
    for i in range(n):
        c = base + (wiggle if i % 2 == 0 else -wiggle)
        rows.append((base, base + wiggle, base - wiggle, c, vol))
    return rows


def range_segment(n, lo, hi, poc_price, dip=None, vol_at_poc=800, vol_base=100):
    """Oscillate within [lo, hi], with extra volume concentrated near poc_price so
    the segment has a clear, well-defined POC. `dip` = (start_i, end_i, dip_low) to
    inject an ordinary mid-range dip-and-recover (a ordinary ATR-comparable wick, not
    a new compression/expansion event)."""
    rows = []
    span = hi - lo
    for i in range(n):
        frac = (i % 10) / 10.0
        mid = lo + span * (0.5 + 0.3 * np.sin(i * 0.7))
        o = mid
        h = min(hi, mid + span * 0.12)
        low_v = max(lo, mid - span * 0.12)
        c = mid + (span * 0.03 if i % 2 == 0 else -span * 0.03)
        if dip and dip[0] <= i <= dip[1]:
            low_v = dip[2]
            c = dip[2] + span * 0.02
            h = max(h, c + span * 0.02)
        near_poc = abs(mid - poc_price) < span * 0.15
        v = vol_at_poc if near_poc else vol_base
        rows.append((o, h, low_v, c, v))
    return rows


def breakout_segment(base, target, n=6, vol=500):
    """A handful of large-range candles pushing price from base to target quickly —
    this is what should register as an ATR expansion after a compression phase."""
    rows = []
    step = (target - base) / n
    price = base
    for i in range(n):
        nxt = price + step
        o = price
        h = max(price, nxt) + abs(step) * 0.3
        low_v = min(price, nxt) - abs(step) * 0.3
        c = nxt
        rows.append((o, h, low_v, c, vol))
        price = nxt
    return rows


# ---------------------------------------------------------------------------
print("Scenario 1: screenshot-reproduction - breakout into a range, ordinary mid-range")
print("dip partway through, continues to the end. Anchor must land at the breakout,")
print("not at the mid-range dip.")
compression = flat_segment(20, base=100, wiggle=0.05)
breakout    = breakout_segment(100, 120, n=6)
BREAKOUT_START = len(compression)
BREAKOUT_END   = BREAKOUT_START + len(breakout) - 1
new_range   = range_segment(70, lo=113, hi=127, poc_price=120,
                             dip=(25, 27, 110))
df1 = make_df(compression + breakout + new_range)
res1 = _detect_vp_anchor(df1)
check("scenario 1: anchor found", res1 is not None)
if res1:
    print(f"    -> idx={res1['idx']} (breakout region {BREAKOUT_START}-{BREAKOUT_END}), "
          f"method={res1['method']}, price={res1['price']}")
    dip_idx = BREAKOUT_START + len(breakout) + 25
    check("scenario 1: anchor lands well before the mid-range dip (near the breakout)",
          BREAKOUT_START - 2 <= res1["idx"] < dip_idx - 10)
    check("scenario 1: anchor is NOT the mid-range dip", abs(res1["idx"] - dip_idx) > 5)


# ---------------------------------------------------------------------------
print()
print("Scenario 2: noise tolerance - inject one sharp wick between breakout and now.")
print("Anchor must be unchanged from scenario 1.")
new_range_with_wick = list(new_range)
wick_i = 50
o, h, low_v, c, v = new_range_with_wick[wick_i]
new_range_with_wick[wick_i] = (o, h + 8, low_v - 8, c, v)  # one abnormal wick, closes back inline
df2 = make_df(compression + breakout + new_range_with_wick)
res2 = _detect_vp_anchor(df2)
check("scenario 2: anchor found", res2 is not None)
if res1 and res2:
    print(f"    -> idx={res2['idx']} (scenario 1 idx={res1['idx']})")
    check("scenario 2: anchor unchanged by a single wick", res2["idx"] == res1["idx"])


# ---------------------------------------------------------------------------
print()
print("Scenario 3: multiple compressions - an earlier true range-start and a later")
print("in-range retest. The EARLIER one must win.")
c0   = flat_segment(20, base=100, wiggle=0.05)
bo1  = breakout_segment(100, 118, n=6)
mid  = range_segment(30, lo=112, hi=124, poc_price=118)
c1   = flat_segment(12, base=118, wiggle=0.04)          # a second, in-range compression
bo2  = breakout_segment(118, 122, n=4)                  # a sub-swing, still inside the same range
tail = range_segment(40, lo=112, hi=124, poc_price=118)
EARLY_START = len(c0)
LATE_START  = len(c0) + len(bo1) + len(mid) + len(c1)
df3 = make_df(c0 + bo1 + mid + c1 + bo2 + tail)
res3 = _detect_vp_anchor(df3)
check("scenario 3: anchor found", res3 is not None)
if res3:
    print(f"    -> idx={res3['idx']} (early candidate ~{EARLY_START}, late candidate ~{LATE_START})")
    check("scenario 3: the EARLIER candidate wins, not the later in-range retest",
          res3["idx"] < LATE_START - 5)


# ---------------------------------------------------------------------------
print()
print("Scenario 4a: constant volatility throughout (no compression/expansion cycle) ->")
print("falls through to the farthest-fractal fallback, using one isolated distant swing.")
rows4 = []
price = 100.0
for i in range(80):
    # Constant-amplitude zigzag: true range never meaningfully deviates from its own
    # rolling median, so no compression (<=0.7x) or expansion (>=1.1x) ever registers.
    rng = 1.0
    o = price
    c = price + (0.3 if i % 2 == 0 else -0.3)
    h = max(o, c) + rng * 0.5
    low_v = min(o, c) - rng * 0.5
    rows4.append((o, h, low_v, c, 100))
    price = c
    if i == 5:
        # One isolated, far-away fractal high — well clear of ATR*3 from where price
        # (a flat zigzag around 100) ends up — for the fallback method to find.
        o5, h5, l5, c5, v5 = rows4[i]
        rows4[i] = (o5, h5 + 15, l5, c5, v5)
df4 = make_df(rows4)
res4 = _detect_vp_anchor(df4)
check("scenario 4a: falls back rather than crashing", res4 is None or res4.get("method") == "fractal_fallback")
if res4:
    print(f"    -> method={res4['method']}")

print()
print("Scenario 4b: completely flat market (atr_now <= 0) -> None, no crash.")
df4b = make_df(flat_segment(50, base=100, wiggle=0.0, vol=100))
res4b = _detect_vp_anchor(df4b)
check("scenario 4b: returns None on a flat/zero-ATR market", res4b is None)


# ---------------------------------------------------------------------------
print()
print("Scenario 5: shape contract - required keys present on a successful result.")
if res1:
    required = {"idx", "price", "anchor_type", "confidence", "method",
                "confirmed", "active", "invalidation_reason"}
    check("scenario 5: all required keys present", required.issubset(res1.keys()))


# ---------------------------------------------------------------------------
print()
print("Scenario 6: regime change - an old high-price range, a decisive breakdown")
print("that's NEVER revisited, then a new lower range. The anchor must NOT merge")
print("the old, abandoned range into the current one (Kev's real-chart finding).")
c0      = flat_segment(15, base=100, wiggle=0.05)
bo_old  = breakout_segment(100, 130, n=6)
old_rng = range_segment(45, lo=125, hi=135, poc_price=130)
quiet   = flat_segment(10, base=130, wiggle=0.05)      # compression right before the crash
crash   = breakout_segment(130, 90, n=6)               # sharp, sustained breakdown, never revisited
new_rng = range_segment(60, lo=85, hi=95, poc_price=90)
OLD_START   = len(c0)
CRASH_START = len(c0) + len(bo_old) + len(old_rng) + len(quiet)
df6 = make_df(c0 + bo_old + old_rng + quiet + crash + new_rng)
res6 = _detect_vp_anchor(df6)
check("scenario 6: anchor found", res6 is not None)
if res6:
    print(f"    -> idx={res6['idx']} (old range starts ~{OLD_START}, crash starts ~{CRASH_START})")
    check("scenario 6: anchor does NOT reach back into the old, abandoned range",
          res6["idx"] >= CRASH_START - 5)


# ---------------------------------------------------------------------------
print()
print("Scenario 7: same shape as scenario 6, but the 'breakdown' is just a fast dip")
print("WITHIN a single persistent range that recovers and keeps trading the same")
print("zone afterward. The anchor SHOULD reach back before the dip (not a regime change).")
c0b     = flat_segment(15, base=100, wiggle=0.05)
bo7     = breakout_segment(100, 118, n=6)
range7  = range_segment(90, lo=112, hi=124, poc_price=118, dip=(45, 48, 108))
BO_START7 = len(c0b)
df7 = make_df(c0b + bo7 + range7)
res7 = _detect_vp_anchor(df7)
check("scenario 7: anchor found", res7 is not None)
if res7:
    print(f"    -> idx={res7['idx']} (breakout region ~{BO_START7})")
    check("scenario 7: anchor still reaches back to the original breakout (dip was noise, not a regime change)",
          res7["idx"] < BO_START7 + 15)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
