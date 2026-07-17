"""
backtest_invalidation_recheck.py -- re-simulate ALL INVALIDATION_TOO_CLOSE missed-winners
using a REALISTIC stop distance instead of the Examiner's synthetic box.

labels_closed.jsonl's r_dist = ATR x R_MULT[trade_type] (R_MULT: scalp=1.0, day=1.2,
swing=1.5) -- a fixed, generous box, NOT the actual tight stop Chev rejected the setup
for. This re-fetches real forward candles and re-runs the same pessimistic triple-barrier
rule against a tighter box: ATR x ATR_FLOOR_EXPLORATION[trade_type] (0.6/0.7/1.0) --
the real minimum stop distance the system would have required.

v2: full population (not a recency-biased tail sample), with per-(symbol,tf) candle
caching so overlapping records share one fetch instead of re-requesting the same window.
Also fixes v1's gap where forex/stock "4h" was fetched as unaggregated 1h candles.

Standalone, read-only against labels_closed.jsonl. Does not import dexter.py.
"""
import json
import time
import datetime as dt
import requests
import pandas as pd
import yfinance as yf

ATR_FLOOR_EXPLORATION = {"scalp": 0.6, "day": 0.7, "swing": 1.0}
PROFIT_RR             = {"scalp": 1.3, "day": 1.2, "swing": 1.6}   # EXPLORATION MIN_NET_RR


def load_jsonl(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def classify(reason):
    r = (reason or "").lower()
    if ("too close" in r or "too tight" in r or "too near" in r
            or "noise floor" in r or "within the required distance" in r):
        return "INVALIDATION_TOO_CLOSE"
    return None


def fetch_binance_full(symbol, interval, start_ms, end_ms):
    out = []
    cursor = start_ms
    for _ in range(20):  # hard cap: 20 * 1000 candles is far more than any window here needs
        params = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000}
        resp = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        out.extend({"t": int(r[0] // 1000), "o": float(r[1]), "h": float(r[2]),
                     "l": float(r[3]), "c": float(r[4])} for r in data)
        last_t = data[-1][0]
        if last_t >= end_ms or len(data) < 1000:
            break
        cursor = last_t + 1
    return out


def fetch_yf_full(symbol, tf, asset_type, start_dt, end_dt):
    yf_symbol = (symbol.replace("/", "") + "=X") if asset_type == "forex" else symbol
    native_interval = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "1h"}.get(tf, "15m")
    try:
        df = yf.Ticker(yf_symbol).history(start=start_dt, end=end_dt, interval=native_interval, auto_adjust=True)
    except Exception:
        return []
    if df.empty:
        return []
    if tf == "4h":
        df = df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    out = []
    for idx, row in df.iterrows():
        out.append({"t": int(idx.timestamp()), "o": float(row["Open"]), "h": float(row["High"]),
                     "l": float(row["Low"]), "c": float(row["Close"])})
    return out


_candle_cache = {}


def get_candles_cached(symbol, tf, asset_type, earliest_touch, latest_touch):
    """One fetch per (symbol, tf) spanning every record that needs it, not one per record."""
    key = (symbol, tf)
    if key in _candle_cache:
        return _candle_cache[key]
    end_dt = dt.datetime.utcfromtimestamp(latest_touch) + dt.timedelta(days=6)
    try:
        if asset_type == "crypto":
            candles = fetch_binance_full(symbol, tf, earliest_touch * 1000,
                                          int(end_dt.timestamp() * 1000))
        else:
            start_dt = dt.datetime.utcfromtimestamp(earliest_touch)
            candles = fetch_yf_full(symbol, tf, asset_type, start_dt, end_dt)
    except Exception as e:
        candles = []
        print(f"    [fetch error {symbol}/{tf}: {e}]")
    _candle_cache[key] = candles
    time.sleep(0.2)
    return candles


def resolve_tight_box(rec, candles):
    trade_type = rec["trade_type"]
    atr        = rec["atr"]
    direction  = rec["direction"]
    entry_ref  = rec["entry_ref"]
    touched_ts = int(rec["touched_ts"])

    tight_r = ATR_FLOOR_EXPLORATION[trade_type] * atr
    req_rr  = PROFIT_RR[trade_type]
    if direction == "long":
        upper, lower = entry_ref + req_rr * tight_r, entry_ref - tight_r
    else:
        upper, lower = entry_ref - req_rr * tight_r, entry_ref + tight_r

    touched = False
    for c in candles:
        if c["t"] < touched_ts:
            continue
        touched = True
        if direction == "long":
            hit_profit, hit_loss = c["h"] >= upper, c["l"] <= lower
        else:
            hit_profit, hit_loss = c["l"] <= upper, c["h"] >= lower
        if hit_profit and hit_loss:
            return -1, "same_candle_pessimistic"
        if hit_profit:
            return 1, "profit"
        if hit_loss:
            return -1, "loss"
    return (0, "no_touch_in_window") if touched else (None, "no_candles")


def main():
    labels = load_jsonl(r"C:\ChevTools\labels_closed.jsonl")
    pop = [r for r in labels if r.get("chev_decision") == "SKIP" and r.get("resolved")
           and r.get("dexter_score", 0) >= 6 and r.get("label") == 1
           and classify(r.get("chev_reason")) == "INVALIDATION_TOO_CLOSE"
           and r.get("atr") and r.get("entry_ref")]
    print(f"Full population: {len(pop)} INVALIDATION_TOO_CLOSE missed-winners (score>=6).")

    by_symtf = {}
    for r in pop:
        by_symtf.setdefault((r["symbol"], r["tf"], r["asset_type"]), []).append(r)
    print(f"Spanning {len(by_symtf)} distinct (symbol, tf) pairs -- one fetch each, not {len(pop)}.")

    results = {"still_win": 0, "flips_to_loss": 0, "no_touch": 0, "no_data": 0}
    detail = []
    done = 0
    for (symbol, tf, asset_type), recs in by_symtf.items():
        earliest = min(r["touched_ts"] for r in recs)
        latest   = max(r["touched_ts"] for r in recs)
        candles  = get_candles_cached(symbol, tf, asset_type, int(earliest), int(latest))
        for rec in recs:
            label, why = resolve_tight_box(rec, candles)
            done += 1
            if label == 1:
                results["still_win"] += 1
            elif label == -1:
                results["flips_to_loss"] += 1
            elif label == 0:
                results["no_touch"] += 1
            else:
                results["no_data"] += 1
            detail.append({"symbol": symbol, "tf": tf, "trade_type": rec["trade_type"],
                            "tight_box_result": label, "reason": why, "ts": rec["ts"]})
        if done % 50 < len(recs):
            print(f"  ...{done}/{len(pop)} resolved")

    print(f"\n=== FULL-POPULATION RESULTS (n={len(pop)}) ===")
    resolved_n = results["still_win"] + results["flips_to_loss"]
    for k, v in results.items():
        print(f"  {k:15s} {v:4d}  ({100*v/len(pop):.1f}% of all, {100*v/resolved_n:.1f}% of resolved)" if k in ("still_win","flips_to_loss") else f"  {k:15s} {v:4d}  ({100*v/len(pop):.1f}%)")
    if resolved_n:
        print(f"\n  Win rate among touched+resolved: {100*results['still_win']/resolved_n:.1f}%  (n={resolved_n})")

    with open(r"C:\ChevTools\backtest_invalidation_recheck_out.json", "w", encoding="utf-8") as f:
        json.dump({"summary": results, "population_n": len(pop), "detail": detail}, f, indent=2)


if __name__ == "__main__":
    main()
