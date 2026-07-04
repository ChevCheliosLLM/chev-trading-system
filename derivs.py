"""
derivs.py — Binance USDT-M perpetual futures context for Dexter.

Funding rate + open interest trend for crypto symbols, with a 5-minute
per-symbol cache (shared by the scanner and the brief builder) and a
PURE classifier function so the signal logic is unit-testable offline.

Signal design notes:
- Reason strings deliberately contain the words 'bullish'/'bearish' so
  dexter's _directional_split() classifies them onto the correct side.
- Reason strings start with fixed prefixes ('Funding extreme',
  'OI divergence', 'OI confirmation') so labeller's REASON_MAP
  tokenises them into dedicated stat buckets.
- Points use the '(Xpt)' suffix format so the weight regex parses them.

Self-test: python -X utf8 derivs.py   (offline — classifier only)
"""
import time
import requests

CACHE_TTL = 300          # seconds — one network round per symbol per 5 min
_cache = {}              # clean_symbol -> (fetched_epoch, data_dict_or_None)

FUNDING_NOTABLE = 0.0005 # 0.05% per 8h funding — crowded
FUNDING_EXTREME = 0.0010 # 0.10% — very crowded
OI_MOVE_PCT     = 2.0    # 6h OI change (%) considered meaningful
PRICE_MOVE_PCT  = 1.0    # 6h price change (%) considered meaningful


def get_derivs(symbol):
    """Fetch funding + OI context for a Binance USDT-M perp.

    Returns {"funding_rate": float, "open_interest": float,
             "oi_chg_6h_pct": float|None, "oi_chg_24h_pct": float|None}
    or None when the symbol has no perp / any request fails.
    Result (including None) is cached for CACHE_TTL seconds.
    """
    clean = symbol.replace("/", "").upper()
    now = time.time()
    hit = _cache.get(clean)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data = None
    try:
        fr_r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                            params={"symbol": clean}, timeout=5)
        oi_r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                            params={"symbol": clean}, timeout=5)
        hist_r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                              params={"symbol": clean, "period": "1h", "limit": 25},
                              timeout=5)
        funding = float(fr_r.json().get("lastFundingRate", 0.0))
        oi_now  = float(oi_r.json().get("openInterest", 0.0))
        oi_chg_6h = oi_chg_24h = None
        hist = hist_r.json()
        if isinstance(hist, list) and len(hist) >= 7:
            vals = [float(h["sumOpenInterest"]) for h in hist]
            if vals[-7] > 0:
                oi_chg_6h = (vals[-1] - vals[-7]) / vals[-7] * 100
            if len(vals) >= 25 and vals[0] > 0:
                oi_chg_24h = (vals[-1] - vals[0]) / vals[0] * 100
        data = {"funding_rate": funding, "open_interest": oi_now,
                "oi_chg_6h_pct": oi_chg_6h, "oi_chg_24h_pct": oi_chg_24h}
    except Exception:
        data = None
    _cache[clean] = (now, data)
    return data


def classify_derivs(funding_rate, oi_chg_6h_pct, price_chg_6h_pct):
    """PURE function: (funding_rate, 6h OI change %, 6h price change %)
    -> (reasons list, total_points). No network, no state.

    Funding is a contrarian signal: extreme positive funding means the
    long side is crowded and paying to stay in — bearish squeeze fuel.
    OI x price is a participation matrix:
      price up + OI up     = bullish confirmation (real participation)
      price up + OI down   = bearish divergence   (weak rally)
      price down + OI up   = bearish confirmation
      price down + OI down = bullish divergence   (short covering)
    """
    reasons, score = [], 0.0
    fr = funding_rate or 0.0
    fr_pct = fr * 100

    if abs(fr) >= FUNDING_EXTREME:
        pts = 3.0
    elif abs(fr) >= FUNDING_NOTABLE:
        pts = 2.0
    else:
        pts = 0.0
    if pts:
        side = ("crowded longs, bearish squeeze risk" if fr > 0
                else "crowded shorts, bullish squeeze risk")
        reasons.append(f"Funding extreme {fr_pct:+.4f}% — {side} ({pts}pt)")
        score += pts

    if oi_chg_6h_pct is not None and price_chg_6h_pct is not None:
        oi_up   = oi_chg_6h_pct >=  OI_MOVE_PCT
        oi_down = oi_chg_6h_pct <= -OI_MOVE_PCT
        px_up   = price_chg_6h_pct >=  PRICE_MOVE_PCT
        px_down = price_chg_6h_pct <= -PRICE_MOVE_PCT
        if px_up and oi_down:
            reasons.append(f"OI divergence — price {price_chg_6h_pct:+.1f}% on OI "
                           f"{oi_chg_6h_pct:+.1f}% — weak rally, bearish (1.5pt)")
            score += 1.5
        elif px_down and oi_down:
            reasons.append(f"OI divergence — price {price_chg_6h_pct:+.1f}% on OI "
                           f"{oi_chg_6h_pct:+.1f}% — short covering, bullish (1.5pt)")
            score += 1.5
        elif px_up and oi_up:
            reasons.append(f"OI confirmation — price {price_chg_6h_pct:+.1f}% with OI "
                           f"{oi_chg_6h_pct:+.1f}% — bullish participation (1pt)")
            score += 1.0
        elif px_down and oi_up:
            reasons.append(f"OI confirmation — price {price_chg_6h_pct:+.1f}% with OI "
                           f"{oi_chg_6h_pct:+.1f}% — bearish participation (1pt)")
            score += 1.0

    return reasons, score


# ── SELF-TEST (offline — classifier only) ───────────────────────────────────
if __name__ == "__main__":
    T = []

    def check(name, cond):
        T.append((name, bool(cond)))
        print(("PASS  " if cond else "FAIL  ") + name)

    # 1. Neutral everything -> no signals
    r, s = classify_derivs(0.0001, 0.5, 0.2)
    check("neutral -> empty", r == [] and s == 0.0)

    # 2. Notable positive funding -> bearish 2pt
    r, s = classify_derivs(0.0006, None, None)
    check("funding notable +", len(r) == 1 and "bearish" in r[0] and s == 2.0)

    # 3. Extreme negative funding -> bullish 3pt
    r, s = classify_derivs(-0.0012, None, None)
    check("funding extreme -", len(r) == 1 and "bullish" in r[0] and s == 3.0)

    # 4. Exactly at notable threshold counts
    r, s = classify_derivs(0.0005, None, None)
    check("funding boundary", s == 2.0)

    # 5. Price up + OI down -> bearish divergence 1.5pt
    r, s = classify_derivs(0.0, -3.0, 2.0)
    check("weak rally", len(r) == 1 and "bearish" in r[0] and s == 1.5)

    # 6. Price down + OI down -> bullish divergence (short covering)
    r, s = classify_derivs(0.0, -3.0, -2.0)
    check("short covering", len(r) == 1 and "bullish" in r[0] and s == 1.5)

    # 7. Price up + OI up -> bullish confirmation 1pt
    r, s = classify_derivs(0.0, 3.0, 2.0)
    check("bull participation", len(r) == 1 and "bullish" in r[0] and s == 1.0)

    # 8. Price down + OI up -> bearish confirmation 1pt
    r, s = classify_derivs(0.0, 3.0, -2.0)
    check("bear participation", len(r) == 1 and "bearish" in r[0] and s == 1.0)

    # 9. Funding + OI stack -> two reasons, points sum
    r, s = classify_derivs(0.0011, -3.0, 2.0)
    check("stacked signals", len(r) == 2 and s == 4.5)

    # 10. None OI / None price -> funding-only path, no crash
    r, s = classify_derivs(0.0011, None, 5.0)
    check("partial data safe", len(r) == 1 and s == 3.0)

    # 11. Prefixes match the labeller REASON_MAP tokens
    r1, _ = classify_derivs(0.0011, None, None)
    r2, _ = classify_derivs(0.0, -3.0, 2.0)
    r3, _ = classify_derivs(0.0, 3.0, 2.0)
    check("prefixes stable", r1[0].startswith("Funding extreme")
          and r2[0].startswith("OI divergence") and r3[0].startswith("OI confirmation"))

    # 12. '(Xpt)' weight suffix present on every reason
    check("pt suffix", all(x.rstrip().endswith("pt)") for x in (r1 + r2 + r3)))

    failed = [n for n, ok in T if not ok]
    print(f"\n{len(T) - len(failed)}/{len(T)} tests passing")
    if failed:
        raise SystemExit("FAILED: " + ", ".join(failed))
