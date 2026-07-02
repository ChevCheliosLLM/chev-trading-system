import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime, timezone
import gspread
import re
import json
from google.oauth2.service_account import Credentials
from flask import Flask, jsonify, send_from_directory, request as flask_request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import engines
import patterns

flask_app = Flask(__name__, static_folder=None)
WEBAPP_FOLDER = r"C:\ChevTools\webapp"

@flask_app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning"
    return response

@flask_app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@flask_app.route('/<path:path>', methods=['OPTIONS'])
def _options_preflight(path):
    return '', 204

@flask_app.route('/api/ping')
def api_ping():
    return jsonify({'ok': True, 'dexter': 'online'})

@flask_app.route("/")
def serve_index():
    return send_from_directory(WEBAPP_FOLDER, "index.html")

@flask_app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(WEBAPP_FOLDER, filename)

@flask_app.route("/api/trades")
def api_trades():
    # Active / pending: use in-memory list (updated by fast price thread every 10s)
    active = []
    for t in open_trades:
        status = t.get("status", "PENDING")
        active.append({
            "row": t["row"], "pair": t["symbol"], "direction": t["direction"],
            "leverage": t.get("leverage", 1), "status": status,
            "live_pnl": t.get("live_pnl", 0.0),
            "live_price": t.get("live_price"),
            "entry": t.get("entry"), "sl": t.get("sl"), "tp": t.get("tp"),
            "confluence_prices": t.get("confluence_prices", {}),
            "trade_type": t.get("trade_type", "day"),
            "tags":       t.get("tags", ""),
            "open_ts":    t.get("open_ts", ""),
        })
    # Last closed: still read from sheet (not held in memory)
    last_closed = None
    try:
        rows = worksheet.get_all_values()[1:]
        closed = []
        for i, row in enumerate(rows, start=2):
            if len(row) < 14:
                continue
            pair, direction, status = row[0], row[1], row[12]
            if status.strip().upper() in ("WIN", "LOSS"):
                result_dollar = row[13]
                closed.append({"row": i, "pair": pair, "direction": direction, "outcome": status, "result": float(result_dollar) if result_dollar else 0.0, "trade_type": (row[15].lower() if len(row) > 15 and row[15] else "day")})
        last_closed = closed[-1] if closed else None
    except Exception:
        pass
    return jsonify({"active": active, "last_closed": last_closed})


@flask_app.route("/api/reset_balance", methods=["POST"])
def api_reset_balance():
    global open_trades
    try:
        set_balance(dashboard_ws, 10000.0)
        # Mark all OPEN/PENDING rows in sheet as EXPIRED
        rows = worksheet.get_all_values()[1:]
        for i, row in enumerate(rows, start=2):
            if len(row) >= 13 and row[12].strip().upper() in ("OPEN", "PENDING"):
                try:
                    worksheet.update_cell(i, 13, "EXPIRED")
                except Exception:
                    pass
        open_trades = []
        print(f"[{datetime.now()}] Piggy bank reset to $10,000. All open/pending trades cleared.")
        return jsonify({"ok": True, "balance": 10000.0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@flask_app.route("/api/close_trade", methods=["POST"])
def api_close_trade():
    """Force-close an open trade by symbol. Marks it as LOSS at current price in the sheet."""
    global open_trades, _cached_balance
    try:
        data   = flask_request.get_json(force=True) or {}
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            return jsonify({"ok": False, "error": "symbol required"}), 400

        target = next((t for t in open_trades if t["symbol"].upper() == symbol
                       and t.get("status") in ("OPEN", "PENDING")), None)
        if target is None:
            return jsonify({"ok": False, "error": f"{symbol} not found in open trades"}), 404

        price = get_current_price(target["symbol"], target["asset_type"])
        if price is None:
            price = data.get("exit_price") or target.get("sl") or target.get("entry", 0)

        is_long = target["direction"] == "long"
        move_pct = (price - target["entry"]) / target["entry"] if is_long else (target["entry"] - price) / target["entry"]
        exit_pnl = round(target.get("position_size_usd", 0) * move_pct, 2)
        outcome  = "WIN" if exit_pnl >= 0 else "LOSS"

        balance = get_balance(dashboard_ws)
        margin  = target.get("margin_reserved", 0)
        new_bal = round(balance + margin + exit_pnl, 2)
        set_balance(dashboard_ws, new_bal)
        _cached_balance = new_bal

        try:
            worksheet.update(values=[[price, exit_pnl, outcome, exit_pnl]],
                             range_name=f"K{target['row']}:N{target['row']}")
        except Exception as e:
            print(f"[force-close] Sheet update failed: {e}")

        open_trades = [t for t in open_trades if t is not target]
        _force_closed_rows.add(target["row"])

        # Write to journal so the closed-trades section of the monitor picks it up
        try:
            journal = _load_journal()
            journal.append({
                "ts":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":           target["symbol"],
                "asset_type":       target.get("asset_type", "crypto"),
                "direction":        target["direction"],
                "entry":            target["entry"],
                "sl":               target["sl"],
                "tp":               target["tp"],
                "exit_price":       price,
                "pnl":              exit_pnl,
                "outcome":          outcome,
                "close_type":       "MANUAL",
                "tags":             (target.get("tags", "") + " manual-close").strip(),
                "duration":         "manual",
                "reasoning":        target.get("reasoning", ""),
                "analysis":         "Closed manually by user.",
                "position_size_usd": target.get("position_size_usd", 0),
                "leverage":         target.get("leverage", 1),
                "chev_moves":       target.get("chev_moves", []),
            })
            with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
                json.dump(journal, f, indent=2)
        except Exception as je:
            print(f"[force-close] Journal write failed: {je}")

        print(f"[{datetime.now()}] FORCE-CLOSED {symbol} | exit={price} | PnL=${exit_pnl:+.2f} | outcome={outcome} | balance ${balance:.2f} -> ${new_bal:.2f}")
        return jsonify({"ok": True, "symbol": symbol, "outcome": outcome,
                        "exit_price": price, "pnl": exit_pnl, "new_balance": new_bal})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@flask_app.route("/api/prices")
def api_prices():
    """Live price snapshot for all watchlist pairs. Updated every 3s by _fast_price_update."""
    with _watchlist_prices_lock:
        return jsonify(_watchlist_prices.copy())


_forex_cache = {}   # key → (timestamp, candles_list)
_FOREX_CACHE_TTL = 20  # seconds — poll interval is 15s so this avoids back-to-back Twelve Data hits

@flask_app.route("/api/chev_chat", methods=["POST"])
def api_chev_chat():
    """Proxy chat messages to Open WebUI — avoids CORS from the webapp."""
    body = flask_request.get_json(force=True, silent=True) or {}
    try:
        resp = requests.post(
            "http://localhost:3000/api/chat/completions",
            headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
        return (resp.content, resp.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/forex_candles")
def api_forex_candles():
    """Proxy forex OHLCV candles from Twelve Data — keeps API keys server-side."""
    import time as _time
    symbol   = flask_request.args.get("symbol", "EUR/USD")
    interval = flask_request.args.get("interval", "1h")
    limit    = min(int(flask_request.args.get("limit", "500")), 5000)
    td_map   = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}
    td_iv    = td_map.get(interval, "1h")

    # Polling calls (limit≤5) can reuse a recent full load from cache
    cache_key = f"{symbol}|{td_iv}"
    now = _time.time()
    cached_ts, cached_candles = _forex_cache.get(cache_key, (0, None))
    if cached_candles is not None and (now - cached_ts) < _FOREX_CACHE_TTL:
        return jsonify(cached_candles[-limit:])

    key = get_next_twelve_key()
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={td_iv}&outputsize={limit}&apikey={key}"
    try:
        resp = requests.get(url, timeout=20)
        data = resp.json()
        if data.get("status") != "ok" or "values" not in data:
            # On rate-limit, serve stale cache rather than erroring
            if cached_candles is not None:
                return jsonify(cached_candles[-limit:])
            print(f"[Forex] Twelve Data error for {symbol}: {data.get('message')}")
            return jsonify({"error": data.get("message", "No data from Twelve Data")}), 502
        candles = []
        for v in reversed(data["values"]):   # Twelve Data returns newest-first
            try:
                ts = int(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                ts = int(datetime.strptime(v["datetime"], "%Y-%m-%d").timestamp())
            candles.append({
                "time":   ts,
                "open":   float(v["open"]),
                "high":   float(v["high"]),
                "low":    float(v["low"]),
                "close":  float(v["close"]),
                "volume": float(v.get("volume") or 0),
            })
        _forex_cache[cache_key] = (now, candles)
        return jsonify(candles[-limit:])
    except Exception as e:
        if cached_candles is not None:
            return jsonify(cached_candles[-limit:])
        print(f"[Forex] Exception for {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/pending")
def api_pending():
    rows = worksheet.get_all_values()[1:]
    pending = []
    for i, row in enumerate(rows, start=2):
        if len(row) < 13:
            continue
        pair, direction, entry, sl, tp, risk_pct, leverage, position_size, margin_used, tags, live_price, live_pnl, status = row[:13]
        if status.strip().upper() == "PENDING":
            trade_type = row[15] if len(row) > 15 else "day"
            try:
                conf_prices = json.loads(row[17]) if len(row) > 17 and row[17] else {}
            except Exception:
                conf_prices = {}
            pending.append({
                "row": i, "pair": pair, "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "tags": tags, "trade_type": trade_type, "leverage": leverage,
                "confluence_prices": conf_prices,
            })
    return jsonify(pending)

@flask_app.route("/api/jane/idea", methods=["POST"])
def api_jane_idea():
    global jane_trades
    data = flask_request.get_json(force=True, silent=True) or {}
    for field in ("symbol", "direction", "entry", "sl", "tp"):
        if field not in data:
            return jsonify({"ok": False, "error": f"Missing field: {field}"}), 400
    symbol    = str(data["symbol"]).upper()
    direction = str(data["direction"]).lower()
    if direction not in ("long", "short"):
        return jsonify({"ok": False, "error": "direction must be 'long' or 'short'"}), 400
    try:
        entry = float(data["entry"])
        sl    = float(data["sl"])
        tp    = float(data["tp"])
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "entry/sl/tp must be numbers"}), 400

    for t in jane_trades:
        if t["symbol"] == symbol and t["status"] in ("OPEN", "PENDING"):
            return jsonify({"ok": False, "error": f"Jane already has an active trade on {symbol}"}), 409

    asset_type = "crypto" if symbol.endswith("USDT") else ("forex" if "/" in symbol else "stock")
    trade = {
        "direction":         direction,
        "entry":             entry,
        "sl":                sl,
        "tp":                tp,
        "risk_pct":          float(data.get("risk_pct", 1.0)),
        "leverage":          float(data.get("leverage", 1.0)),
        "position_size_usd": float(data.get("position_size_usd", 0.0)),
        "tags":              str(data.get("tags", "")),
        "trade_type":        str(data.get("trade_type", "day")).lower(),
    }
    current_price = get_current_price(symbol, asset_type) or entry
    try:
        trade_entry = log_jane_trade(symbol, asset_type, trade, current_price)
        jane_trades.append(trade_entry)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    tags_str = trade["tags"]
    print(f"[Jane] Idea received: {symbol} {direction.upper()} entry={entry} sl={sl} tp={tp}")
    threading.Thread(
        target=send_telegram_alert,
        args=(f"[Jane] {symbol} {direction.upper()} entry {_fmt_p(entry)} · SL {_fmt_p(sl)} · TP {_fmt_p(tp)}",),
        daemon=True,
    ).start()
    threading.Thread(target=_evaluate_jane_trade_with_chev, args=(trade_entry,), daemon=True).start()
    return jsonify({"ok": True, "row": trade_entry["row"], "status": "PENDING"})


@flask_app.route("/api/jane/trades")
def api_jane_trades():
    active = [
        {
            "row":           t["row"],
            "pair":          t["symbol"],
            "direction":     t["direction"],
            "leverage":      t.get("leverage", 1),
            "status":        t.get("status", "PENDING"),
            "live_pnl":      t.get("live_pnl", 0.0),
            "live_price":    t.get("live_price"),
            "entry":         t.get("entry"),
            "sl":            t.get("sl"),
            "tp":            t.get("tp"),
            "trade_type":    t.get("trade_type", "day"),
            "tags":          t.get("tags", ""),
            "open_ts":       t.get("open_ts", ""),
            "chev_verdict":  t.get("chev_verdict", ""),
            "chev_feedback": t.get("chev_feedback", ""),
        }
        for t in jane_trades
    ]
    last_closed = None
    try:
        rows   = jane_worksheet.get_all_values()[1:]
        closed = []
        for i, row in enumerate(rows, start=2):
            if len(row) < 14:
                continue
            if row[12].strip().upper() in ("WIN", "LOSS"):
                closed.append({
                    "row":        i,
                    "pair":       row[0],
                    "direction":  row[1],
                    "outcome":    row[12],
                    "result":     float(row[13]) if row[13] else 0.0,
                    "trade_type": row[15].lower() if len(row) > 15 and row[15] else "day",
                })
        last_closed = closed[-1] if closed else None
    except Exception:
        pass
    return jsonify({"active": active, "last_closed": last_closed, "balance": jane_balance})


# =============================================================================
# ANALYSIS ENDPOINTS — SR, Volume Profile, ATR, Fibonacci Stack, RSI Divergence
# Ported from Confluence_Analyzer and chart_drawer tools
# =============================================================================

def _an_asset_type(symbol):
    s = symbol.upper().replace("/", "").replace(":", "")
    if any(s.endswith(c) for c in ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "TUSD", "DAI"]):
        return "crypto"
    if "/" in symbol or len(symbol) == 6:
        return "forex"
    return "stock"


def _an_get_timed_touches(df, window=3, threshold_pct=0.3, confirm_window=8):
    """Reversal-confirmed swing highs/lows. A swing only counts if price moved away 0.3%+ in next 8 bars."""
    highs = df["high"].values
    lows  = df["low"].values
    n = len(df)
    touches = []
    for i in range(window, n - window):
        lo_i, hi_i = max(0, i - window), min(n, i + window + 1)
        if highs[i] == highs[lo_i:hi_i].max():
            fut = lows[i + 1: min(i + 1 + confirm_window, n)]
            if len(fut) and (highs[i] - fut.min()) / highs[i] * 100 >= threshold_pct:
                touches.append((df.index[i], highs[i], "resistance"))
        if lows[i] == lows[lo_i:hi_i].min():
            fut = highs[i + 1: min(i + 1 + confirm_window, n)]
            if len(fut) and (fut.max() - lows[i]) / lows[i] * 100 >= threshold_pct:
                touches.append((df.index[i], lows[i], "support"))
    return touches


def _an_cluster_levels(prices, tolerance_pct=0.5):
    if not prices:
        return []
    sp = sorted(prices)
    clusters, cur = [], [sp[0]]
    for p in sp[1:]:
        if abs(p - cur[-1]) / cur[-1] * 100 <= tolerance_pct:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)
    return [(sum(c) / len(c), len(c)) for c in clusters]


def _an_build_single_tf_levels(prices, min_touches=3, tolerance_pct=0.5):
    return [{"price": round(center, 5), "instances": count, "tier": "B"}
            for center, count in _an_cluster_levels(prices, tolerance_pct)
            if count >= min_touches]


def _an_build_validated_levels(touches_by_tf, tolerance_pct=0.6, episode_gap_hours=12):
    """Tier A: level must pass min-touch density on 15m/30m/1h/4h simultaneously in same episode."""
    MIN_TF = {"15m": 5, "30m": 3, "1h": 2, "4h": 2}
    all_prices = [p for tf in touches_by_tf for ts, p in touches_by_tf[tf]]
    results = []
    for center, _ in _an_cluster_levels(all_prices, tolerance_pct):
        tol_lo = center * (1 - tolerance_pct / 100)
        tol_hi = center * (1 + tolerance_pct / 100)
        combined = []
        for tf, touches in touches_by_tf.items():
            for ts, p in touches:
                if tol_lo <= p <= tol_hi:
                    combined.append((ts, tf))
        if not combined:
            continue
        combined.sort(key=lambda x: x[0])
        episodes, cur_ep = [], [combined[0]]
        for item in combined[1:]:
            try:
                gap_h = (item[0] - cur_ep[-1][0]).total_seconds() / 3600
            except Exception:
                gap_h = 0
            if gap_h > episode_gap_hours:
                episodes.append(cur_ep)
                cur_ep = [item]
            else:
                cur_ep.append(item)
        episodes.append(cur_ep)
        for ep in episodes:
            counts = {tf: 0 for tf in MIN_TF}
            for ts, tf in ep:
                if tf in counts:
                    counts[tf] += 1
            if all(counts[tf] >= MIN_TF[tf] for tf in MIN_TF):
                results.append({"price": round(center, 5), "instances": sum(counts.values()), "tier": "A"})
                break
    return sorted(results, key=lambda r: r["instances"], reverse=True)


def _an_find_last_impulse(touches):
    """Find the most recent swing-to-swing move (resistance→support or vice versa)."""
    if len(touches) < 2:
        return None
    ts = sorted(touches, key=lambda t: t[0])
    last = ts[-1]
    for t in reversed(ts[:-1]):
        if t[2] != last[2]:
            return (t[0], t[1], last[0], last[1])
    return None


def _an_vp(df, bin_count=75):
    """Volume Profile: POC, VAH, VAL (70% value area)."""
    p_min, p_max = df["low"].min(), df["high"].max()
    if p_max <= p_min:
        return None
    bw   = (p_max - p_min) / bin_count
    bins = np.zeros(bin_count)
    for _, row in df.iterrows():
        lo_i = max(0, min(int((row["low"]  - p_min) / bw), bin_count - 1))
        hi_i = max(0, min(int((row["high"] - p_min) / bw), bin_count - 1))
        span = hi_i - lo_i + 1
        bins[lo_i: hi_i + 1] += row["volume"] / span
    poc_i = int(np.argmax(bins))
    poc   = p_min + (poc_i + 0.5) * bw
    total, target = bins.sum(), bins.sum() * 0.70
    lo_i, hi_i, acc = poc_i, poc_i, bins[poc_i]
    while acc < target and (lo_i > 0 or hi_i < bin_count - 1):
        add_lo = bins[lo_i - 1] if lo_i > 0 else -1
        add_hi = bins[hi_i + 1] if hi_i < bin_count - 1 else -1
        if add_lo >= add_hi and lo_i > 0:
            lo_i -= 1; acc += bins[lo_i]
        elif hi_i < bin_count - 1:
            hi_i += 1; acc += bins[hi_i]
        else:
            break
    return {"poc": round(poc, 5),
            "vah": round(p_min + (hi_i + 1) * bw, 5),
            "val": round(p_min + lo_i * bw, 5)}


def _an_atr_series(df, period=14):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"]  - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _an_rsi_series(df, period=14):
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _an_regime(df, period=14):
    """ADX-based market regime. Returns regime, adx, plus_di, minus_di.
    CHOPPY=ADX<15, RANGING=ADX<25, TRENDING_UP/DOWN=ADX>=25 with EMA confirmation."""
    if len(df) < period * 3:
        return {"regime": "UNKNOWN", "adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0),   up_move,   0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    alpha    = 1.0 / period
    atr_s    = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * pd.Series(plus_dm,  index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / atr_s
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / di_sum
    adx      = dx.ewm(alpha=alpha, adjust=False).mean()
    cur_adx  = float(adx.iloc[-1])
    cur_pdi  = float(plus_di.iloc[-1])
    cur_mdi  = float(minus_di.iloc[-1])
    if np.isnan(cur_adx):
        return {"regime": "UNKNOWN", "adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    if cur_adx < 15:
        regime = "CHOPPY"
    elif cur_adx < 25:
        regime = "RANGING"
    elif cur_pdi > cur_mdi and ema20 > ema50:
        regime = "TRENDING_UP"
    elif cur_mdi > cur_pdi and ema20 < ema50:
        regime = "TRENDING_DOWN"
    else:
        regime = "RANGING"
    return {"regime": regime, "adx": round(cur_adx, 1),
            "plus_di": round(cur_pdi, 1), "minus_di": round(cur_mdi, 1)}


@flask_app.route("/api/analysis/regime")
def api_analysis_regime():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "4h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym = clean if atype == "crypto" else symbol
        df  = fetch_candles(sym, atype, tf, 200)
        reg = _an_regime(df)
        return jsonify({"symbol": symbol, "tf": tf,
                        "current_price": round(float(df["close"].iloc[-1]), 5), **reg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/sr")
def api_analysis_sr():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    TFS    = ["15m", "30m", "1h", "4h"]
    try:
        res_by_tf, sup_by_tf, primary_df = {}, {}, None
        for ftf in TFS:
            try:
                sym = clean if atype == "crypto" else symbol
                df  = fetch_candles(sym, atype, ftf, 700)
                if ftf == tf:
                    primary_df = df
                t = _an_get_timed_touches(df)
                res_by_tf[ftf] = [(ts, p) for ts, p, k in t if k == "resistance"]
                sup_by_tf[ftf] = [(ts, p) for ts, p, k in t if k == "support"]
            except Exception as e:
                print(f"[SR/{ftf}] {e}")
                res_by_tf[ftf] = []
                sup_by_tf[ftf] = []
        if primary_df is None:
            sym = clean if atype == "crypto" else symbol
            primary_df = fetch_candles(sym, atype, tf, 700)
        cur = float(primary_df["close"].iloc[-1])

        tier_a_res = [z for z in _an_build_validated_levels(res_by_tf) if z["price"] > cur]
        tier_a_sup = [z for z in _an_build_validated_levels(sup_by_tf) if z["price"] < cur]

        all_t = _an_get_timed_touches(primary_df)
        tier_b_res = _an_build_single_tf_levels([p for ts, p, k in all_t if k == "resistance" and p > cur])
        tier_b_sup = _an_build_single_tf_levels([p for ts, p, k in all_t if k == "support"    and p < cur])

        resistance = sorted(tier_a_res or tier_b_res, key=lambda z: z["price"])[:6]
        support    = sorted(tier_a_sup or tier_b_sup, key=lambda z: -z["price"])[:6]

        return jsonify({"symbol": symbol, "tf": tf, "current_price": round(cur, 5),
                        "resistance": resistance, "support": support})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/vp")
def api_analysis_vp():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym    = clean if atype == "crypto" else symbol
        df     = fetch_candles(sym, atype, tf, 700)
        anchor = _detect_auction_anchor(df)
        if anchor is None:
            return jsonify({"error": "No significant structure detected — price may be at equilibrium. Try a different timeframe."}), 400
        start_idx = anchor["idx"]
        end_idx   = len(df) - 1
        vp = _ca_volume_profile(df, start_idx, end_idx)
        if not vp:
            return jsonify({"error": "Insufficient price range in structure"}), 400
        start_t = int(df.index[start_idx].timestamp())
        end_t   = int(df.index[end_idx].timestamp())
        return jsonify({
            "symbol": symbol, "tf": tf,
            "start_t": start_t, "end_t": end_t,
            "poc": round(float(vp["poc"]), 5),
            "vah": round(float(vp["vah"]), 5),
            "val": round(float(vp["val"]), 5),
            "candles":                end_idx - start_idx + 1,
            "anchor_price":           anchor["price"],
            "anchor_type":            anchor["anchor_type"],
            "anchor_method":          anchor["method"],
            "anchor_confidence":      anchor["confidence"],
            "anchor_confirmed":       anchor.get("confirmed", False),
            "anchor_active":          anchor.get("active", True),
            "anchor_invalidation":    anchor.get("invalidation_reason"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/anchor")
def api_analysis_anchor():
    """Shared anchor endpoint — all tools (VP, Fib, VWAP, S/R) call this for the same anchor."""
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym    = clean if atype == "crypto" else symbol
        df     = fetch_candles(sym, atype, tf, 700)
        anchor = _detect_auction_anchor(df)
        if anchor is None:
            return jsonify({"error": "No anchor detected — market may be at equilibrium."}), 400
        idx = anchor["idx"]
        anchor_t    = int(df.index[idx].timestamp())
        current_t   = int(df.index[-1].timestamp())
        candles_ago = len(df) - 1 - idx
        return jsonify({
            "symbol":     symbol,
            "tf":         tf,
            "anchor_time":    anchor_t,
            "current_time":   current_t,
            "anchor_price":   anchor["price"],
            "anchor_type":    anchor["anchor_type"],
            "anchor_method":  anchor["method"],
            "confidence":          anchor["confidence"],
            "candles_ago":         candles_ago,
            "confirmed":           anchor.get("confirmed", False),
            "active":              anchor.get("active", True),
            "invalidation_reason": anchor.get("invalidation_reason"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/atr")
def api_analysis_atr():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym   = clean if atype == "crypto" else symbol
        df    = fetch_candles(sym, atype, tf, 150)
        atr_s = _an_atr_series(df)
        cur_a = float(atr_s.iloc[-1])
        avg_a = float(atr_s.tail(20).mean())
        ratio = round(cur_a / avg_a, 2) if avg_a else 1.0
        state = "quiet" if ratio < 0.7 else "volatile" if ratio > 1.3 else "normal"
        cur_p = float(df["close"].iloc[-1])
        return jsonify({"symbol": symbol, "tf": tf, "current_price": round(cur_p, 5),
                        "atr": round(cur_a, 5), "avg_atr": round(avg_a, 5),
                        "ratio": ratio, "state": state,
                        "sl_1atr":   round(cur_p - cur_a, 5),
                        "sl_1_5atr": round(cur_p - cur_a * 1.5, 5)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/fib_stack")
def api_analysis_fib_stack():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    TFS    = [("15m", "#5dade2"), ("1h", "#d4af37"), ("4h", "#e67e22")]
    results = []
    try:
        for ftf, color in TFS:
            try:
                sym     = clean if atype == "crypto" else symbol
                df      = fetch_candles(sym, atype, ftf, 300)
                touches = _an_get_timed_touches(df)
                impulse = _an_find_last_impulse(touches)
                if impulse:
                    ts1, p1, ts2, p2 = impulse
                    sw_low, sw_high = (p1, p2) if p1 < p2 else (p2, p1)
                    going_up = p2 > p1
                    try:
                        ts_high = int(ts2.timestamp()) if going_up else int(ts1.timestamp())
                        ts_low  = int(ts1.timestamp()) if going_up else int(ts2.timestamp())
                    except Exception:
                        ts_high = ts_low = None
                else:
                    w = df.tail(150)
                    sw_high, sw_low = float(w["high"].max()), float(w["low"].min())
                    going_up = df["close"].iloc[-1] > df["close"].iloc[0]
                    try:
                        ts_high = int(w["high"].idxmax().timestamp())
                        ts_low  = int(w["low"].idxmin().timestamp())
                    except Exception:
                        ts_high = ts_low = None
                diff = sw_high - sw_low
                if diff == 0:
                    continue
                if going_up:
                    gp_618 = sw_high - diff * 0.618
                    gp_65  = sw_high - diff * 0.650
                else:
                    gp_618 = sw_low + diff * 0.618
                    gp_65  = sw_low + diff * 0.650
                results.append({"tf": ftf, "color": color,
                                "swing_high": round(sw_high, 5), "swing_low": round(sw_low, 5),
                                "ts_high": ts_high, "ts_low": ts_low,
                                "gp_low":  round(min(gp_618, gp_65), 5),
                                "gp_high": round(max(gp_618, gp_65), 5),
                                "direction": "up" if going_up else "down"})
            except Exception as e:
                print(f"[FibStack/{ftf}] {e}")
        overlaps = []
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                a, b = results[i], results[j]
                o_lo = max(a["gp_low"],  b["gp_low"])
                o_hi = min(a["gp_high"], b["gp_high"])
                if o_hi > o_lo:
                    overlaps.append({"tfs": [a["tf"], b["tf"]],
                                     "low": round(o_lo, 5), "high": round(o_hi, 5)})
        return jsonify({"symbol": symbol, "timeframes": results, "overlaps": overlaps})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/api/analysis/rsi_div")
def api_analysis_rsi_div():
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)

    def _calc_for_tf(tf_str):
        try:
            sym = clean if atype == "crypto" else symbol
            df  = fetch_candles(sym, atype, tf_str, 500)
            rsi = _an_rsi_series(df).values
            highs, lows = df["high"].values, df["low"].values
            try:
                ts_arr = [int(t.timestamp()) for t in df.index]
            except Exception:
                ts_arr = list(range(len(df)))
            n   = len(df)
            lb  = 7  # bars each side — candle must be highest/lowest of 15-candle window

            # Dual-pivot: candle must be a swing on BOTH price AND RSI at the same bar
            sw_h, sw_l = [], []
            for i in range(lb, n - lb):
                r = rsi[i]
                if np.isnan(r):
                    continue
                lr = rsi[i - lb:i]; rr = rsi[i + 1:i + lb + 1]
                if np.any(np.isnan(lr)) or np.any(np.isnan(rr)):
                    continue
                is_price_h = highs[i] >= highs[i - lb:i].max() and highs[i] > highs[i + 1:i + lb + 1].max()
                is_price_l = lows[i]  <= lows[i - lb:i].min()  and lows[i]  < lows[i + 1:i + lb + 1].min()
                is_rsi_h   = r >= lr.max() and r > rr.max()
                is_rsi_l   = r <= lr.min() and r < rr.min()
                if is_price_h and is_rsi_h:
                    sw_h.append((i, float(highs[i]), r, ts_arr[i]))
                if is_price_l and is_rsi_l:
                    sw_l.append((i, float(lows[i]), r, ts_arr[i]))

            def _best_div(pivots, bull_checks):
                # bull_checks: list of (price_cond, rsi_cond, type, bias, note)
                best = None
                best_score = (-1, 0.0)
                for k in range(len(pivots) - 1):
                    i1, p1, r1, ts1 = pivots[k]
                    i2, p2, r2, ts2 = pivots[k + 1]
                    if np.isnan(r1) or np.isnan(r2):
                        continue
                    rsi_gap = abs(r2 - r1)
                    if rsi_gap < 2.0:  # ignore marginal noise
                        continue
                    for price_cond, rsi_cond, div_type, bias, note in bull_checks:
                        if price_cond(p1, p2) and rsi_cond(r1, r2):
                            score = (i2, rsi_gap)
                            if score > best_score:
                                best_score = score
                                best = {"type": div_type, "bias": bias, "price": round(p2, 5),
                                        "note": note,
                                        "ts_t1": ts1, "price_t1": round(p1, 5), "rsi_t1": round(r1, 2),
                                        "ts_t2": ts2, "price_t2": round(p2, 5), "rsi_t2": round(r2, 2)}
                return best

            bull_div = _best_div(sw_l, [
                (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Regular Bullish", "bull", "price LL but RSI HL — selling momentum fading"),
                (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Hidden Bullish",  "bull", "price HL but RSI LL — uptrend continuation"),
            ])
            bear_div = _best_div(sw_h, [
                (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Regular Bearish", "bear", "price HH but RSI LH — momentum fading"),
                (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Hidden Bearish",  "bear", "price LH but RSI HH — downtrend continuation"),
            ])

            # Return only the stronger of the two (most recent second pivot wins; tie → largest RSI gap)
            candidates = [d for d in [bull_div, bear_div] if d]
            if not candidates:
                return []
            return [max(candidates, key=lambda d: (d["ts_t2"], abs(d["rsi_t2"] - d["rsi_t1"])))]
        except Exception:
            return []

    if tf == "all":
        by_tf = {t: _calc_for_tf(t) for t in ["15m", "30m", "1h", "4h"]}
        return jsonify({"symbol": symbol, "by_tf": by_tf})
    else:
        return jsonify({"symbol": symbol, "tf": tf, "divergences": _calc_for_tf(tf)})


@flask_app.route("/api/analysis/engine")
def api_analysis_engine():
    """Full Dexter engine survey — feeds the ENGINE tab overlays in the webapp."""
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym = clean if atype == "crypto" else symbol
        df  = fetch_candles(sym, atype, tf, 500)
        survey = engines.run_survey(
            df=df, symbol=symbol, primary_tf=tf,
            dexter_score=0.0, dexter_reasons=[],
        )

        def _ts(dt):
            try: return int(dt.timestamp())
            except: return 0

        ana_map = {a.swing_id: a for a in (survey.swing_analyses or [])}
        swings_out = []
        for s in (survey.swings or []):
            ana = ana_map.get(s.id)
            swings_out.append({
                "id": s.id, "ts": _ts(s.ts), "price": float(s.price),
                "kind": s.kind, "confirmed": ana.confirmed if ana else True,
            })

        legs_out = []
        for leg in (survey.legs or []):
            legs_out.append({
                "id": leg.id,
                "start_ts": _ts(leg.start_swing.ts),
                "start_price": float(leg.start_swing.price),
                "end_ts": _ts(leg.end_swing.ts),
                "end_price": float(leg.end_swing.price),
                "direction": leg.direction,
                "character": leg.character,
                "distance_atr": round(float(leg.distance_atr), 2),
                "energy": round(float(leg.energy), 3),
                "dist_atr_pct": int(leg.dist_atr_pct),
                "energy_pct": int(leg.energy_pct),
                "bar_count": int(leg.bar_count),
            })

        state_out = None
        if survey.state:
            st = survey.state
            state_out = {
                "participation": round(float(st.participation), 1),
                "direction": round(float(st.direction), 1),
                "phase": st.phase,
                "atr_trend": st.atr_trend,
                "volume_trend": st.volume_trend,
                "leg_sequence": st.leg_sequence,
                "participation_pct": int(st.participation_pct),
            }

        geometry_out = None
        if survey.geometry:
            g = survey.geometry
            geometry_out = {
                "structure_axis": g.structure_axis,
                "compression": round(float(g.compression), 3),
                "parallelism": round(float(g.parallelism), 3),
                "is_converging": g.is_converging,
                "breakout_up": g.breakout_up,
                "breakout_dn": g.breakout_dn,
                "upper_slope": round(float(g.upper.slope_norm), 4),
                "lower_slope": round(float(g.lower.slope_norm), 4),
            }

        auction_out = None
        if survey.auction:
            a = survey.auction
            auction_out = {
                "balance_high": round(float(a.balance_high), 5),
                "balance_low":  round(float(a.balance_low), 5),
                "anchor_price": round(float(a.anchor_price), 5),
                "balance_width_atr": round(float(a.balance_width_atr), 2),
                "state": a.state,
                "poc":   round(float(a.poc), 5) if a.poc else None,
            }

        hyps_out = []
        for h in (survey.hypotheses or []):
            hyps_out.append({
                "name":   h.name,
                "bias":   h.bias,
                "confidence": round(float(h.confidence), 3),
                "label":  f"{h.name.replace('_', ' ')} ({h.bias})",
                "because": h.because[:3],
                "expected_next_event":   h.expected_next_event,
                "breakout_level": round(float(h.breakout_level), 5) if h.breakout_level else None,
                "status": h.status,
            })

        prof_out = None
        if survey.asset_profile:
            prof = survey.asset_profile
            atrs = prof.leg_atr_sorted
            prof_out = {
                "n_legs": int(prof.n_legs),
                "computed_from_bars": int(prof.computed_from_bars),
                "typical_impulse_atr": round(float(atrs[len(atrs) // 2]), 2) if atrs else None,
            }

        try:
            dexter_highs = [s for s in (survey.swings or []) if s.kind == "HIGH"]
            dexter_lows  = [s for s in (survey.swings or []) if s.kind == "LOW"]
            patterns_out = patterns.run(df, dexter_highs=dexter_highs, dexter_lows=dexter_lows)
        except Exception:
            patterns_out = {"signal": "NEUTRAL", "pattern": "None", "bias": "neutral",
                            "breakout": False, "volume_confirmed": False,
                            "volume_notes": [], "all_patterns": [],
                            "upper_trendline": None, "lower_trendline": None}

        return jsonify({
            "symbol": symbol, "tf": tf,
            "current_price": round(float(df["close"].iloc[-1]), 5),
            "swings": swings_out,
            "legs": legs_out,
            "state": state_out,
            "geometry": geometry_out,
            "auction": auction_out,
            "hypotheses": hyps_out,
            "asset_profile": prof_out,
            "patterns": patterns_out,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def run_web_server():
    try:
        flask_app.run(host="0.0.0.0", port=8080, threaded=True, use_reloader=False)
    except Exception as e:
        print(f"[Flask] Web server crashed: {e}")


def _fast_price_update():
    """Updates live prices for all watchlist pairs + open trade PnL every 3 seconds.
    Crypto: Binance → CoinGecko fallback. Forex/stocks: yfinance (free) → Twelve Data fallback.
    Prices cached in _watchlist_prices so /api/prices can serve them instantly."""
    all_pairs = {item["symbol"]: item["type"] for item in WATCHLIST}
    while True:
        # Refresh price cache for every watchlist pair
        for symbol, asset_type in all_pairs.items():
            try:
                price = get_current_price(symbol, asset_type)
                if price:
                    with _watchlist_prices_lock:
                        _watchlist_prices[symbol] = price
            except Exception:
                pass

        # Update open trade PnL from the freshly fetched cache
        try:
            for trade in list(open_trades):
                if trade.get("status") != "OPEN":
                    continue
                try:
                    with _watchlist_prices_lock:
                        price = _watchlist_prices.get(trade["symbol"])
                    if price:
                        is_long = trade["direction"] == "long"
                        move_pct = ((price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - price) / trade["entry"])
                        trade["live_price"] = price
                        trade["live_pnl"] = round(trade["position_size_usd"] * move_pct, 2)
                except Exception:
                    pass
        except Exception:
            pass

        # Update Jane trade PnL from the same price cache
        try:
            for trade in list(jane_trades):
                if trade.get("status") != "OPEN":
                    continue
                try:
                    with _watchlist_prices_lock:
                        price = _watchlist_prices.get(trade["symbol"])
                    if price:
                        is_long = trade["direction"] == "long"
                        move_pct = ((price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - price) / trade["entry"])
                        trade["live_price"] = price
                        trade["live_pnl"]   = round(trade["position_size_usd"] * move_pct, 2)
                except Exception:
                    pass
        except Exception:
            pass

        # Push snapshot to Firebase every 3s (runs in background thread, doesn't block price loop)
        threading.Thread(target=_push_to_firebase, daemon=True).start()

        time.sleep(3)

# ============================================================
# CONFIGURATION
# ============================================================

TWELVE_DATA_KEY   = "81e418343e4c4fdfbb5e79eaefc145a8"
TWELVE_DATA_KEY_2 = "5495cd88c050419093694dca6e783959"
TWELVE_DATA_KEY_3 = "87ea8e3195f44a4fb2336598573c1727"
_TWELVE_KEYS = [TWELVE_DATA_KEY, TWELVE_DATA_KEY_2, TWELVE_DATA_KEY_3]
_twelve_call_counter = 0

def get_next_twelve_key():
    global _twelve_call_counter
    key = _TWELVE_KEYS[_twelve_call_counter % len(_TWELVE_KEYS)]
    _twelve_call_counter += 1
    return key

BOT_TOKEN = "7890385799:AAHhQfEluupOYvgtrCQOOTBwlkko-2Jwguc"
CHAT_ID = "-5501297384"
_sl_notify_ts: dict = {}  # symbol -> last SL trail notification timestamp (15-min cooldown)
OPENWEBUI_API_KEY = "sk-91bd167cca0142c983379ebe27b4e621"
OPENWEBUI_URL     = "http://localhost:3000/api/chat/completions"

FIREBASE_URL  = "https://chev-monitor-default-rtdb.firebaseio.com"
JOURNAL_PATH       = r"C:\ChevTools\chev_journal.json"
JANE_JOURNAL_PATH  = r"C:\ChevTools\jane_journal.json"
PLAYBOOK_PATH      = r"C:\ChevTools\chev_playbook.txt"
MODEL_ID = "chev-chelios"

TRADE_TYPE_EXPIRY_HOURS = {"scalp": 2, "day": 6, "swing": 48}
MAX_LEVERAGE_BY_TYPE = {
    "crypto": {"scalp": 10, "day": 10, "swing": 5},
    "forex": {"scalp": 5, "day": 5, "swing": 2},
    "stock": {"scalp": 5, "day": 5, "swing": 2},
}

# Google Sheets connection
GOOGLE_CREDENTIALS_FILE = "google_credentials.json"
SHEET_ID = "1V1b2aU3SJu_R7VjFKGp9J6uFwucGSamhRWyq6jgCbFs"
TRADE_LOG_TAB         = "Trade Log"
JANE_TAB              = "Jane"

# Paper trading starting balance
STARTING_BALANCE      = 10000.0
JANE_STARTING_BALANCE = 10000.0

# Confluence scoring — timeframe-weighted point system
# Tags with a timeframe suffix (_4h/_1h/_30m/_15m) get weighted points.
# Timeframe-agnostic tools (gp, vp, vw, dv, cp) use a flat score.
CONFLUENCE_SCORES = {
    # Support / Resistance
    "sr_4h": 4, "sr_1h": 3, "sr_30m": 2, "sr_15m": 1, "sr": 2,
    # Fibonacci (non-golden pocket)
    "fib_4h": 3, "fib_1h": 2, "fib_30m": 2, "fib_15m": 1, "fib": 2,
    # RSI divergence (confirmed)
    "rsi_4h": 4, "rsi_1h": 4, "rsi_30m": 3, "rsi_15m": 2, "rsi": 4,
    # RSI forming divergence (dynamic 1.0–2.0 — Dexter adds exact score; these are Chev-tag aliases)
    "rsi_form_4h": 2.0, "rsi_form_1h": 1.5, "rsi_form_30m": 1.0, "rsi_form_15m": 1.0, "rsi_form": 1.5,
    # RSI level signals
    "rsi_ob": 0.5, "rsi_os": 0.5, "rsi_50": 0.5,
    # EMA (test or cross) — primary tag is the period (EMA55 > EMA21 > EMA13 structurally)
    "ema_55": 3, "ema_21": 2, "ema_13": 1,
    # Legacy timeframe aliases — kept for journal backward-compatibility only
    "ema_4h": 3, "ema_1h": 2, "ema_30m": 1, "ema_15m": 1, "ema": 2,
    # Bollinger Bands (squeeze / band touch / expansion)
    "bb_4h": 3, "bb_1h": 2, "bb_30m": 2, "bb_15m": 1, "bb": 2,
    # Chart patterns — named tags scored by structural significance
    # Reversal patterns (strong directional signal when complete):
    "hs": 4, "ihs": 4,                          # Head & Shoulders / Inverse H&S
    "double_top": 3, "double_bottom": 3,         # Double Top/Bottom
    "triple_top": 4, "triple_bottom": 4,         # Triple Top/Bottom (rarer, stronger)
    "rising_wedge": 3, "falling_wedge": 3,       # Wedges (reversal)
    # Continuation patterns (lower base — context-dependent):
    "bull_flag": 2, "bear_flag": 2,              # Flags
    "bull_pennant": 2, "bear_pennant": 2,        # Pennants
    "bull_channel": 2, "bear_channel": 2,        # Channels
    "asc_tri": 2, "desc_tri": 2, "sym_tri": 2,  # Triangles
    "rectangle": 1,                              # Range/rectangle
    # Legacy TF-qualified aliases (kept for journal backward-compatibility):
    "tri_4h": 3, "tri_1h": 2, "tri_30m": 1, "tri_15m": 1,
    "triangle_4h": 3, "triangle_1h": 2, "triangle_30m": 1, "triangle_15m": 1, "triangle": 1,
    # Timeframe-agnostic tools
    "gp": 4,                      # Golden Pocket (0.618-0.65 fib zone)
    "vp": 3, "volprofile": 3,     # Volume Profile (POC / VAH / VAL)
    "vw": 3, "vwap": 3,           # VWAP
    "dv": 3,                      # Divergence (non-RSI)
    "cp": 1, "candlepattern": 1,  # Candle pattern (confirmation only)
    # Market / price structure (higher timeframe context)
    "ms_1d": 5, "ms_4h": 4, "ms_1h": 3, "ms_30m": 2, "ms_15m": 1, "ms": 3,
}
CONFLUENCE_THRESHOLD_CRYPTO = 10   # minimum score to open a trade on crypto
CONFLUENCE_THRESHOLD_FOREX  = 7    # forex moves slower — fewer confluences expected

VALID_TAGS = list(CONFLUENCE_SCORES.keys())

# Loop timing
CHECK_INTERVAL_SECONDS    = 2 * 60   # main loop sleep between cycles — crypto+forex scan every cycle
FOREX_SCAN_INTERVAL       = 0        # 0 = scan every cycle (yfinance is unlimited)
FOREX_TRADE_CHECK_INTERVAL = 60      # how often to check fills/SL/TP on open+pending forex trades
STOCK_SCAN_INTERVAL       = 10 * 60  # Twelve Data budget: 4 pairs × 2 tf × 144 cycles = ~1150 credits/day
STOCK_TRADE_CHECK_INTERVAL = 60      # how often to check fills/SL/TP on open+pending stocks (market hours only)

WATCHLIST = [
    {"symbol": "BTCUSDT", "type": "crypto"},
    {"symbol": "ETHUSDT", "type": "crypto"},
    {"symbol": "XRPUSDT", "type": "crypto"},
    {"symbol": "XLMUSDT", "type": "crypto"},
    {"symbol": "ADAUSDT", "type": "crypto"},
    {"symbol": "SOLUSDT", "type": "crypto"},
    {"symbol": "EUR/USD", "type": "forex"},
    {"symbol": "GBP/USD", "type": "forex"},
    {"symbol": "USD/JPY", "type": "forex"},
    {"symbol": "AUD/USD", "type": "forex"},
    {"symbol": "NVDA", "type": "stock"},
    {"symbol": "TSLA", "type": "stock"},
    {"symbol": "AMZN", "type": "stock"},
    {"symbol": "AMD", "type": "stock"},
]

# Crypto gets the full 4-timeframe validated model (Binance is free).
# Forex/stocks get a lighter 2-timeframe model to protect the Twelve Data budget
# (they'll only ever produce Tier B / unconfirmed levels, never full Tier A).
TIMEFRAMES_CRYPTO = ["15m", "30m", "1h", "4h"]
TIMEFRAMES_FOREX_STOCK = ["1h", "4h"]
MIN_TOUCHES_PER_TF = {"15m": 5, "30m": 3, "1h": 2, "4h": 2}
TIMEFRAMES_TWELVE = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}

# ── Per-TF scan architecture ──────────────────────────────────────────────────
# Each TF is scanned independently at its candle-close interval.
# Smaller TF = scalp, larger = swing. Cooldowns scale with TF duration.
TF_SECONDS        = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
TF_TRADE_TYPE     = {"5m": "scalp", "15m": "scalp", "30m": "day", "1h": "day", "4h": "swing"}
TF_MIN_CONFLUENCE = {"5m": 5, "15m": 5, "30m": 6, "1h": 6, "4h": 7}
TF_SKIP_COOLDOWN  = {"5m": 300, "15m": 600, "30m": 1800, "1h": 3600, "4h": 14400}
TF_POST_COOLDOWN  = {"5m": 7200, "15m": 7200, "30m": 14400, "1h": 28800, "4h": 86400}
SCAN_TFS_CRYPTO   = ["5m", "15m", "30m", "1h", "4h"]
SCAN_TFS_FOREX    = ["15m", "30m", "1h", "4h"]   # no 5m — forex data too thin on fast TFs

open_trades      = []     # Chev's open/pending paper trades
jane_trades      = []     # Jane's open/pending paper trades
jane_balance     = JANE_STARTING_BALANCE
_jane_win_stats  = {"wins": 0, "losses": 0}

_combined_closed_count    = 0   # Chev + Jane closed trades total
_last_cross_analysis_count = 0  # count at last cross-analysis
CROSS_ANALYSIS_EVERY      = 20  # run cross-analysis every N combined closes

# Advisory accuracy: how often Chev's verdict matched Jane's actual outcome
_advisory_accuracy = {
    "approved_wins":   0,   # Chev APPROVED → Jane WON (correct)
    "approved_losses": 0,   # Chev APPROVED → Jane LOST (wrong)
    "rejected_wins":   0,   # Chev REJECTED → Jane WON (wrong — Jane ignored him correctly)
    "rejected_losses": 0,   # Chev REJECTED → Jane LOST (correct)
    "caution_wins":    0,
    "caution_losses":  0,
}

_chev_online = True          # False when Open WebUI is unreachable
_chev_last_health_check = 0.0  # timestamp of last health check while offline
CHEV_HEALTH_CHECK_INTERVAL = 60  # seconds between retries when offline

# Live price cache for ALL watchlist items — updated by _fast_price_update every 3s
_watchlist_prices = {}
_watchlist_prices_lock = threading.Lock()
_cached_balance = STARTING_BALANCE          # kept fresh by main loop, read by Firebase push
_firebase_win_stats = {"wins": 0, "losses": 0}  # kept fresh by main loop

COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "XRPUSDT": "ripple",
    "XLMUSDT": "stellar",
    "ADAUSDT": "cardano",
    "SOLUSDT": "solana",
}


# ============================================================
# DATA FETCHING
# ============================================================

def _check_chev_health():
    """Ping Open WebUI to see if it's reachable."""
    try:
        resp = requests.get("http://localhost:3000/", timeout=3)
        return resp.status_code < 500
    except Exception:
        return False


def _push_ngrok_url():
    """Discover ngrok tunnel and publish URL to Firebase. Retries every 30s so order-of-start doesn't matter."""
    import time as _time
    _last_url = None
    _time.sleep(4)
    while True:
        try:
            r = requests.get('http://localhost:4040/api/tunnels', timeout=3)
            tunnels = r.json().get('tunnels', [])
            for t in tunnels:
                if t.get('proto') == 'https':
                    url = t['public_url']
                    if url != _last_url:
                        requests.put(f"{FIREBASE_URL}/config/api_url.json", json=url, timeout=5)
                        print(f"[ngrok] Remote URL -> Firebase: {url}")
                        _last_url = url
                    break
            else:
                if _last_url:
                    print("[ngrok] Tunnel gone — waiting for ngrok to come back.")
                    _last_url = None
        except Exception:
            pass  # ngrok not running yet — will retry
        _time.sleep(30)


def _push_to_firebase():
    """Push live monitor snapshot to Firebase Realtime Database (remote monitoring page reads this)."""
    try:
        snapshot = [
            {
                "symbol":            t["symbol"],
                "direction":         t["direction"],
                "asset_type":        t.get("asset_type", "crypto"),
                "entry":             t.get("entry", 0),
                "sl":                t.get("sl", 0),
                "tp":                t.get("tp", 0),
                "is_sip":            t.get("sip_active", False) or _is_sip(t),
                "position_size_usd": t.get("position_size_usd", 0),
                "leverage":          t.get("leverage", 1),
                "tags":              t.get("tags", ""),
                "status":            t.get("status", "PENDING"),
                "live_pnl":          round(t.get("live_pnl", 0), 2),
                "live_price":        t.get("live_price"),
                "reasoning":         t.get("reasoning", ""),
                "chev_moves":        t.get("chev_moves", []),
                "trade_type":        t.get("trade_type", "day"),
                "row":               t.get("row"),
                "open_ts":           t.get("open_ts", ""),
                "confluence_prices": t.get("confluence_prices", {}),
            }
            for t in list(open_trades)
        ]
        with _watchlist_prices_lock:
            # Firebase keys can't contain / so EUR/USD → EUR_USD
            prices = {k.replace('/', '_'): round(v, 6) for k, v in _watchlist_prices.items()}
        jane_snapshot = [
            {
                "symbol":        t["symbol"],
                "direction":     t["direction"],
                "asset_type":    t.get("asset_type", "crypto"),
                "entry":         t.get("entry", 0),
                "sl":            t.get("sl", 0),
                "tp":            t.get("tp", 0),
                "leverage":      t.get("leverage", 1),
                "tags":          t.get("tags", ""),
                "status":        t.get("status", "PENDING"),
                "live_pnl":      round(t.get("live_pnl", 0), 2),
                "live_price":    t.get("live_price"),
                "trade_type":    t.get("trade_type", "day"),
                "chev_verdict":  t.get("chev_verdict", ""),
                "chev_feedback": t.get("chev_feedback", ""),
            }
            for t in list(jane_trades)
        ]
        journal      = _load_journal()
        playbook     = _load_playbook()
        jane_journal = _load_jane_journal()
        resp = requests.put(
            f"{FIREBASE_URL}/monitor.json",
            json={
                "trades":       snapshot,
                "closed":       journal[-20:],
                "prices":       prices,
                "balance":      _cached_balance,
                "wins":         _firebase_win_stats["wins"],
                "losses":       _firebase_win_stats["losses"],
                "playbook":     playbook,
                "updated":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "jane_trades":    jane_snapshot,
                "jane_closed":   jane_journal[-20:],
                "jane_balance":  jane_balance,
                "jane_wins":     _jane_win_stats["wins"],
                "jane_losses":   _jane_win_stats["losses"],
                "chev_advisory": _advisory_accuracy,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            print(f"[Firebase] PUT failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[Firebase push error] {e}")


def _load_playbook():
    try:
        with open(PLAYBOOK_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def _load_journal():
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

_chev_lock             = threading.Lock()
_chev_last_call        = 0.0
_CHEV_MIN_GAP          = 65   # seconds from call completion — keeps each call in its own 60-second TPM window
_chev_rate_limit_until = 0.0  # absolute timestamp; block all calls until then after a 429
_CHEV_RL_COOLDOWN      = 90   # extra seconds to back off after a 429
_last_escalated: dict  = {}   # symbol → unblock_timestamp; block escalation until this time
_ESCALATION_COOLDOWN   = 600   # 10 min cooldown after SKIP or malformed reply
_POST_COOLDOWN         = 14400 # 4 hour cooldown after a POST (trade is live — don't re-hammer same pair)
_force_closed_rows: set = set()  # row numbers force-closed by user; price thread skips these
_CHEV_DECISIONS_LOG = r"C:\ChevTools\chev_decisions.jsonl"  # one JSON-line per Chev decision


def _log_chev_decision(symbol, primary_tf, dexter_score, dexter_reasons, decision, reason, regime_4h=None):
    """Append one JSON-line to the Chev decision log for post-session review."""
    import json as _json
    entry = {
        "ts":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":         symbol,
        "tf":             primary_tf,
        "dexter_score":   round(float(dexter_score), 1),
        "dexter_reasons": dexter_reasons if isinstance(dexter_reasons, list) else [dexter_reasons],
        "decision":       decision,      # SKIP | POST | GATE_REJECT | FORMAT_ERROR
        "reason":         reason,
        "regime_4h":      regime_4h,
    }
    try:
        with open(_CHEV_DECISIONS_LOG, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(entry) + "\n")
    except Exception as _e:
        print(f"[Dexter] Decision log write failed: {_e}")


def _call_chev(messages, timeout=120):
    """Call Chev via Open WebUI — his identity, tools, and model config all live there."""
    global _chev_online, _chev_last_call, _chev_rate_limit_until
    with _chev_lock:
        wait = _CHEV_MIN_GAP - (time.time() - _chev_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.post(
                OPENWEBUI_URL,
                headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL_ID, "messages": messages},
                timeout=timeout
            )
            data = resp.json()
            if "choices" in data and data["choices"]:
                if not _chev_online:
                    _chev_online = True
                    print(f"[{datetime.now()}] Chev is back online.")
                return data["choices"][0]["message"]["content"].strip()
            content_len = sum(len(str(m.get("content", ""))) for m in messages)
            data_str = str(data)
            is_depleted = "prepayment credits are depleted" in data_str or "RESOURCE_EXHAUSTED" in data_str
            is_rate_limit = is_depleted or resp.status_code == 429 or (
                "'code': 429" in data_str or '"code": 429' in data_str or
                "free_tier" in data_str or "quota" in data_str.lower()
            )
            if is_rate_limit:
                if is_depleted:
                    _chev_rate_limit_until = time.time() + 14400  # 4 hours — credits gone, stop hammering
                    print(f"[{datetime.now()}] *** GEMINI CREDITS DEPLETED — pausing ALL escalations for 4 hours. Top up at console.cloud.google.com ***")
                else:
                    _chev_rate_limit_until = time.time() + _CHEV_RL_COOLDOWN
                    print(f"[{datetime.now()}] Chev rate-limited — cooling down {_CHEV_RL_COOLDOWN}s.")
                print(f"  payload {content_len:,} chars | {data_str[:300]}")
            elif _chev_online:
                _chev_online = False
                print(f"[{datetime.now()}] Chev is down — pausing escalations.")
                print(f"  HTTP {resp.status_code} | payload {content_len:,} chars")
                print(f"  Gemini error: {data_str[:400]}")
            return None
        except Exception as e:
            if _chev_online:
                _chev_online = False
                print(f"[{datetime.now()}] Chev is down — pausing escalations. ({e})")
            return None
        finally:
            _chev_last_call = time.time()


def _ask_chev_about_jane_trade(symbol, direction, entry, sl, tp, tags, jane_wins, jane_losses):
    """Ask Chev to evaluate Jane's trade idea. Returns raw response string."""
    playbook = _load_playbook()
    risk_pct   = abs(entry - sl) / entry * 100 if entry > 0 else 0
    reward_pct = abs(tp - entry) / entry * 100 if entry > 0 else 0
    rr         = round(reward_pct / risk_pct, 2) if risk_pct > 0 else 0
    prompt = (
        f"Jane — your trading partner — wants to open a trade. She is a human trader with real market instincts. You are her advisor, not her gatekeeper.\n\n"
        f"JANE'S TRADE:\n"
        f"  Symbol:     {symbol}\n"
        f"  Direction:  {direction.upper()}\n"
        f"  Entry:      {entry}\n"
        f"  Stop Loss:  {sl}  ({risk_pct:.1f}% risk)\n"
        f"  Take Profit:{tp}  ({rr:.1f}R reward)\n"
        f"  Tags: {tags or 'none provided'}\n\n"
        f"JANE'S RECORD: {jane_wins}W / {jane_losses}L\n\n"
        f"YOUR ADVISORY ACCURACY:\n"
        f"  APPROVED → WIN: {_advisory_accuracy['approved_wins']}  |  APPROVED → LOSS: {_advisory_accuracy['approved_losses']}\n"
        f"  REJECTED → LOSS: {_advisory_accuracy['rejected_losses']} |  REJECTED → WIN: {_advisory_accuracy['rejected_wins']} (you were wrong, she was right)\n"
        f"  CAUTION → WIN: {_advisory_accuracy['caution_wins']}  |  CAUTION → LOSS: {_advisory_accuracy['caution_losses']}\n\n"
        f"YOUR PLAYBOOK:\n{playbook if playbook else 'No playbook yet.'}\n\n"
        f"TOOLS AVAILABLE — use these to verify Jane's tags before deciding:\n"
        f"  get_support_resistance(\"{symbol}\", \"1h\") — check if her SR tag is backed by a real validated level near {entry}.\n"
        f"  get_atr_stop_suggestion(\"{symbol}\", \"1h\", entry_price={entry}, direction=\"{direction}\") — check if her SL is realistic vs current ATR.\n"
        f"Call these if the tags or SL placement look questionable. A CAUTION or REJECTED verdict carries more weight when backed by tool data.\n\n"
        f"Give your verdict. One trader to another — be direct and honest.\n"
        f"Reply in exactly this format:\n"
        f"VERDICT: [APPROVED / CAUTION / REJECTED]\n"
        f"FEEDBACK: [one sentence — your honest take as a trader]"
    )
    return _call_chev([{"role": "user", "content": prompt}], timeout=90)


def _parse_chev_verdict(response):
    """Parse VERDICT / FEEDBACK from Chev's structured reply."""
    result = {"verdict": "UNKNOWN", "feedback": ""}
    if not response:
        return result
    for line in response.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("APPROVED", "CAUTION", "REJECTED"):
                result["verdict"] = v
        elif line.upper().startswith("FEEDBACK:"):
            result["feedback"] = line.split(":", 1)[1].strip()
    return result


def _evaluate_jane_trade_with_chev(trade):
    """Background thread: Chev evaluates Jane's trade and posts verdict to Telegram."""
    try:
        response = _ask_chev_about_jane_trade(
            symbol     = trade["symbol"],
            direction  = trade["direction"],
            entry      = trade["entry"],
            sl         = trade["sl"],
            tp         = trade["tp"],
            tags       = trade.get("tags", ""),
            jane_wins  = _jane_win_stats["wins"],
            jane_losses= _jane_win_stats["losses"],
        )
        verdict = _parse_chev_verdict(response)

        # Store verdict on the live trade so Firebase / monitor can show it
        for t in jane_trades:
            if t.get("row") == trade.get("row"):
                t["chev_verdict"]  = verdict["verdict"]
                t["chev_feedback"] = verdict["feedback"]
                break

        emoji = {"APPROVED": "✅", "CAUTION": "⚠️", "REJECTED": "❌"}.get(verdict["verdict"], "🤔")
        print(f"[Jane/Chev] Verdict for {trade['symbol']}: {emoji} {verdict['verdict']} — {verdict['feedback'][:80]}")
    except Exception as e:
        print(f"[Jane/Chev] Evaluation failed: {e}")


def _maybe_run_cross_analysis():
    """Trigger cross-team analysis every CROSS_ANALYSIS_EVERY combined closed trades."""
    global _last_cross_analysis_count
    if _combined_closed_count - _last_cross_analysis_count >= CROSS_ANALYSIS_EVERY:
        _last_cross_analysis_count = _combined_closed_count
        threading.Thread(target=_run_cross_analysis, daemon=True).start()


def _run_cross_analysis():
    """Every 20 combined closed trades, Chev reviews both his and Jane's performance."""
    try:
        chev_j    = _load_journal()
        jane_j    = _load_jane_journal()
        chev_recent = chev_j[-20:]
        jane_recent = jane_j[-20:]
        if not chev_recent and not jane_recent:
            return

        chev_wins   = sum(1 for e in chev_recent if e.get("outcome") == "WIN")
        chev_losses = sum(1 for e in chev_recent if e.get("outcome") == "LOSS")
        jane_wins   = sum(1 for e in jane_recent if e.get("outcome") == "WIN")
        jane_losses = sum(1 for e in jane_recent if e.get("outcome") == "LOSS")

        chev_text = "\n".join([
            f"  {e['symbol']} {e['direction'].upper()} | {e['outcome']} | Tags: {e.get('tags','none')}"
            for e in chev_recent
        ]) or "  None yet"
        jane_text = "\n".join([
            f"  {e['symbol']} {e['direction'].upper()} | {e['outcome']} | Tags: {e.get('tags','none')}"
            for e in jane_recent
        ]) or "  None yet"

        prompt = (
            f"Team performance review — {len(chev_recent) + len(jane_recent)} combined trades.\n\n"
            f"YOUR TRADES ({chev_wins}W / {chev_losses}L):\n{chev_text}\n\n"
            f"JANE'S TRADES ({jane_wins}W / {jane_losses}L):\n{jane_text}\n\n"
            f"Answer these four questions directly:\n"
            f"1. What setups is Jane consistently better at than you?\n"
            f"2. What setups are you consistently better at than Jane?\n"
            f"3. When you both traded the same asset/tags, who was more accurate?\n"
            f"4. One specific adjustment to your advisory approach for Jane's next trades.\n\n"
            f"Maximum 200 words. Be a trader, not a consultant."
        )
        print("[Cross-analysis] Running team performance review...")
        response = _call_chev([{"role": "user", "content": prompt}], timeout=90)
        if response:
            print(f"[Cross-analysis] Review done ({_combined_closed_count} combined trades) — saved to journal, not posted to Telegram.")
    except Exception as e:
        print(f"[Cross-analysis] Failed: {e}")


def _run_learning_session(journal, jane_journal=None):
    """Every 10 closed trades, Chev reads both journals and rewrites his playbook."""
    recent = journal[-20:]
    jane_recent = (jane_journal or [])[-10:]

    def _fmt_entry(i, e):
        moves = e.get("chev_moves", [])
        moves_line = ("\nManagement: " + " | ".join(moves)) if moves else ""
        return (
            f"Trade {i+1}: {e['symbol']} {e['direction'].upper()} | {e['outcome']} ${e['pnl']:+.2f} | "
            f"held {e.get('duration','?')} | {e['ts'][:10]} [{e.get('close_type','?')}]\n"
            f"Confluences: {e.get('tags','none')}"
            f"{moves_line}\n"
            f"Analysis: {e.get('analysis','none')}"
        )
    entries_text = "\n\n".join([_fmt_entry(i, e) for i, e in enumerate(recent)])

    # Build confluence combo win-rate breakdown for the last 30 trades
    from collections import defaultdict
    combo_stats = defaultdict(lambda: {"w": 0, "l": 0})
    for e in journal[-30:]:
        raw_tags = [t.strip().lower() for t in str(e.get("tags", "")).split(",") if t.strip()]
        # Filter to known scored tags only (exclude leaked close-type strings)
        valid = [t for t in raw_tags if t in CONFLUENCE_SCORES]
        combo = "+".join(sorted(valid)) if valid else "no-tags"
        if e.get("outcome") == "WIN":
            combo_stats[combo]["w"] += 1
        else:
            combo_stats[combo]["l"] += 1
    combo_lines = []
    for combo, st in sorted(combo_stats.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
        total = st["w"] + st["l"]
        wr = round(st["w"] / total * 100)
        combo_lines.append(f"  {combo}: {st['w']}W/{st['l']}L ({wr}% WR)")
    combo_summary = "\n".join(combo_lines) if combo_lines else "  No data yet"

    # Management pattern stats across last 30 trades
    trades_30    = journal[-30:]
    with_moves   = [(e, e.get("chev_moves", [])) for e in trades_30 if e.get("chev_moves")]
    mgmt_summary = "  No management moves recorded yet."
    if with_moves:
        tp_raised_floor   = sum(1 for e, mv in with_moves if any("TP" in m for m in mv) and e.get("close_type") == "SIP_HIT")
        tp_raised_hit_tp  = sum(1 for e, mv in with_moves if any("TP" in m for m in mv) and e.get("close_type") == "TP_HIT")
        sl_moved_stopped  = sum(1 for e, mv in with_moves if any(("SL" in m or "SIP" in m) for m in mv) and e.get("outcome") == "LOSS")
        sl_moved_won      = sum(1 for e, mv in with_moves if any(("SL" in m or "SIP" in m) for m in mv) and e.get("outcome") == "WIN")
        no_moves_hit_tp   = sum(1 for e in trades_30 if not e.get("chev_moves") and e.get("close_type") == "TP_HIT")
        no_moves_stopped  = sum(1 for e in trades_30 if not e.get("chev_moves") and e.get("outcome") == "LOSS")
        mgmt_summary = "\n".join([
            f"  Raised TP → ended at SIP floor instead (chased, possibly too greedy): {tp_raised_floor}",
            f"  Raised TP → price hit the raised TP (raise was correct): {tp_raised_hit_tp}",
            f"  Moved SL/floor → trade still closed at loss (moved too early?): {sl_moved_stopped}",
            f"  Moved SL/floor → trade closed WIN (management worked): {sl_moved_won}",
            f"  No management (held original SL/TP) → hit TP: {no_moves_hit_tp}",
            f"  No management (held original SL/TP) → stopped out: {no_moves_stopped}",
        ])

    # Reasoning quality distribution across last 30 trades
    quality_counts = {"VALIDATED": 0, "PARTIAL": 0, "INVALIDATED": 0, "UNKNOWN": 0}
    for e in trades_30:
        q = e.get("reasoning_quality", "UNKNOWN")
        quality_counts[q] = quality_counts.get(q, 0) + 1
    quality_summary = (
        f"  VALIDATED: {quality_counts['VALIDATED']}  |  "
        f"PARTIAL: {quality_counts['PARTIAL']}  |  "
        f"INVALIDATED: {quality_counts['INVALIDATED']}"
    )

    # Grade accuracy: did A+ setups actually outperform B setups?
    grade_stats = {}
    for e in trades_30:
        g = e.get("setup_grade", "?")
        if g not in grade_stats:
            grade_stats[g] = {"w": 0, "l": 0}
        if e.get("outcome") == "WIN":
            grade_stats[g]["w"] += 1
        else:
            grade_stats[g]["l"] += 1
    grade_lines = []
    for g in ("A+", "A", "B"):
        if g in grade_stats:
            st = grade_stats[g]
            total = st["w"] + st["l"]
            wr = round(st["w"] / total * 100) if total else 0
            grade_lines.append(f"  {g}: {st['w']}W/{st['l']}L ({wr}% WR)")
    grade_accuracy = "\n".join(grade_lines) if grade_lines else "  No grade data yet."

    jane_text = ""
    if jane_recent:
        jane_lines = "\n".join([
            f"  {e['symbol']} {e['direction'].upper()} | {e['outcome']} | Tags: {e.get('tags','none')}"
            for e in jane_recent
        ])
        jane_wins   = sum(1 for e in jane_recent if e.get("outcome") == "WIN")
        jane_losses = sum(1 for e in jane_recent if e.get("outcome") == "LOSS")
        jane_text = (
            f"\nJANE'S EDGE (your trading partner — {jane_wins}W / {jane_losses}L):\n"
            f"{jane_lines}\n"
            f"Note: Jane is a human trader. Look for patterns where her instincts outperform your models.\n"
        )

    prompt = (
        f"You are reviewing your last {len(recent)} trade post-mortems to update your trading playbook.\n\n"
        f"{entries_text}\n"
        f"{jane_text}\n"
        f"CONFLUENCE COMBO WIN-RATE (last 30 trades — use this as statistical evidence):\n"
        f"{combo_summary}\n\n"
        f"MANAGEMENT PATTERN STATS (last 30 trades):\n"
        f"{mgmt_summary}\n\n"
        f"REASONING QUALITY (last 30 trades):\n"
        f"{quality_summary}\n\n"
        f"SETUP GRADE ACCURACY (last 30 trades):\n"
        f"{grade_accuracy}\n\n"
        f"Rewrite your playbook under these exact headings:\n\n"
        f"CONFLUENCE CONDITIONS\n"
        f"Based on the combo win-rate data above, which combinations are producing results and which are not? "
        f"When does SR need to be 4H vs 1H to matter? When does RSI divergence give real signals vs noise?\n\n"
        f"MARKET STRUCTURE RULES\n"
        f"What trend context improves setup quality? What makes you confident price reaches TP vs reverses early?\n\n"
        f"ENTRY QUALITY STANDARDS\n"
        f"What separates a high-conviction entry from a marginal one? What did winners have structurally that losers did not?\n\n"
        f"TRADE MANAGEMENT PATTERNS\n"
        f"Based on the management stats above: when you raised TP, did price typically hit it or reverse to the floor? "
        f"When you moved SL early, did it protect profit or get you stopped before TP? "
        f"What does the data say about your management tendencies vs leaving trades alone?\n\n"
        f"REASONING QUALITY\n"
        f"Based on the quality stats above: what % of your reasoning is being validated by price action? "
        f"When reasoning was INVALIDATED, was there a common theme — wrong bias, wrong timeframe, misread structure? "
        f"Are A+ setups showing better reasoning validation than B setups?\n\n"
        f"ASSET NOTES\n"
        f"Any patterns in how crypto, forex, or stocks behaved differently?\n\n"
        f"JANE'S PATTERNS (only if she has data above)\n"
        f"What setup types is Jane consistently profitable on? Where do her instincts diverge from your models — and who was right?\n\n"
        f"IMPORTANT: Build statistical understanding — not a list of prohibitions. A setup that failed once can be valid in different conditions. "
        f"Think in conditions, not outcomes.\n\n"
        f"FORMAT RULES (hard cap — no exceptions):\n"
        f"- Each section must be bullet points only, no prose paragraphs\n"
        f"- Maximum 3 bullets per section\n"
        f"- Maximum 21 bullets total across all sections\n"
        f"- Each bullet must be one sentence, specific and data-backed\n"
        f"- If you have nothing meaningful to say in a section, write one bullet: 'Insufficient data — revisit next session.'"
    )
    try:
        print(f"[Playbook] Running learning session on {len(recent)} journal entries (+ {len(jane_recent)} Jane's)...")
        new_playbook = _call_chev([{"role": "user", "content": prompt}], timeout=120)
        if not new_playbook:
            raise Exception("No response from Chev")
        header = (f"CHEV TRADING PLAYBOOK\n"
                  f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                  f"| Based on {len(journal)} Chev + {len(jane_journal or [])} Jane closed trades\n{'='*40}\n\n")
        with open(PLAYBOOK_PATH, "w", encoding="utf-8") as f:
            f.write(header + new_playbook)
        print(f"[Playbook] Rewritten after {len(journal)} Chev + {len(jane_journal or [])} Jane closed trades.")
    except Exception as e:
        print(f"[Playbook] Learning session failed: {e}")

def _do_postmortem(trade, outcome, pnl, exit_price):
    """Runs in background thread. Asks Chev to analyze the closed trade and saves to journal."""
    duration = "unknown"
    if trade.get("opened_at"):
        try:
            opened = datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
            delta  = datetime.now() - opened
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            duration = f"{h}h {m}m" if h > 0 else f"{m}m"
        except Exception:
            pass

    close_type = trade.get("close_type", "UNKNOWN")
    is_long_pm = trade.get("direction", "long") == "long"
    sip_note   = ""
    if close_type == "SIP_HIT":
        sip_note = (
            f"\n⚠ SIP HIT — Stop in Profit. Read this before writing your post-mortem:\n"
            f"Your stop floor was at {trade['sl']}, which is {'ABOVE' if is_long_pm else 'BELOW'} your entry {trade['entry']}.\n"
            f"This means price moved IN YOUR FAVOR after entry (a profitable move), then pulled back to the floor.\n"
            f"This is a WIN (${pnl:+.2f}). Do NOT write 'price moved against me' — that is factually wrong.\n"
            f"The right question is: was the floor set at the right level, or should it have been higher{'?' if is_long_pm else ' (lower for a short)?'} "
            f"Did raising the TP cause the SIP to be too tight? Could you have let it run further?"
        )
    elif close_type == "SL_HIT":
        sip_note = "\nThis closed via genuine stop loss — price moved against the position and hit the original risk level."
    sl_label   = "SIP floor" if close_type == "SIP_HIT" else "SL"

    moves = trade.get("chev_moves", [])
    moves_text = ""
    if moves:
        moves_lines = "\n".join(f"  {m}" for m in moves)
        moves_text = f"YOUR IN-TRADE MANAGEMENT DECISIONS:\n{moves_lines}\n\n"
    n_points  = "4" if moves else "3"
    mgmt_point = (
        f"4. MANAGEMENT REVIEW — Look at your in-trade decisions above. Did moving SL early get you stopped before TP? "
        f"Did raising TP mean price reversed and you ended at the floor instead? Or was the management correct given what price did? "
        f"One sentence on what you'd do the same, one on what you'd change.\n\n"
    ) if moves else ""

    prompt = (
        f"Trade closed. Analyze it as a technical trader.\n\n"
        f"PAIR: {trade['symbol']} | DIRECTION: {trade['direction'].upper()} | RESULT: {outcome} [{close_type}] (${pnl:+.2f})\n"
        f"ENTRY: {trade['entry']} | {sl_label}: {trade['sl']} | TP: {trade['tp']} | EXIT PRICE: {exit_price} | TIME HELD: {duration}{sip_note}\n"
        f"CONFLUENCES: {trade.get('tags', 'none recorded')}\n"
        f"YOUR ORIGINAL REASONING: {trade.get('reasoning', 'none recorded')}\n\n"
        f"{moves_text}"
        f"Write your post-mortem in exactly {n_points} points — 2 sentences max each:\n\n"
        f"1. SETUP QUALITY — Was this a high-quality entry location? Was price at a well-defined structural level or a marginal entry mid-range?\n"
        f"2. WHAT PRICE DID — What did price action do after entry and what does that reveal about market conditions at that moment?\n"
        f"3. KEY OBSERVATION — One specific, actionable observation about this setup type in these conditions. Not a rule — an observation.\n\n"
        f"{mgmt_point}"
        f"Do not avoid similar setups because this one failed. One trade is not a pattern. Be a trader, not a philosopher.\n\n"
        f"After your final point, on a new line write exactly one of:\n"
        f"QUALITY: VALIDATED\n"
        f"QUALITY: PARTIAL\n"
        f"QUALITY: INVALIDATED\n"
        f"VALIDATED = price did broadly what your original reasoning predicted.\n"
        f"PARTIAL = some aspects were right, price action was mixed or ambiguous.\n"
        f"INVALIDATED = price directly contradicted your reasoning."
    )
    # Performance Engine: record the outcome so hypothesis win-rates stay accurate
    try:
        _trade_row_id = str(trade.get("row", ""))
        if _trade_row_id:
            _perf_outcome = "SIP" if outcome == "WIN" and trade.get("close_type") == "SIP_HIT" else outcome
            engines.record_trade_outcome(_trade_row_id, _perf_outcome)
    except Exception as _pe:
        print(f"[Performance] record_trade_outcome failed: {_pe}")

    try:
        analysis = _call_chev([{"role": "user", "content": prompt}], timeout=90)
        if not analysis:
            raise Exception("No response from Chev")
    except Exception as e:
        analysis = f"Post-mortem unavailable: {e}"

    quality_match = re.search(r'QUALITY:\s*(VALIDATED|PARTIAL|INVALIDATED)', analysis or "", re.IGNORECASE)
    reasoning_quality = quality_match.group(1).upper() if quality_match else "UNKNOWN"

    entry = {
        "ts":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":           trade["symbol"],
        "asset_type":       trade.get("asset_type", "crypto"),
        "direction":        trade["direction"],
        "entry":            trade["entry"],
        "sl":               trade["sl"],
        "tp":               trade["tp"],
        "exit_price":       exit_price,
        "pnl":              round(pnl, 2),
        "outcome":          outcome,
        "close_type":       trade.get("close_type", "UNKNOWN"),
        "tags":             trade.get("tags", ""),
        "duration":         duration,
        "reasoning":        trade.get("reasoning", ""),
        "analysis":         analysis,
        "reasoning_quality": reasoning_quality,
        "setup_grade":      trade.get("setup_grade", ""),
        "session_quality":  trade.get("session_quality", ""),
        "heat_at_entry":    trade.get("heat_at_entry", 0),
        "position_size_usd": trade.get("position_size_usd", 0),
        "leverage":         trade.get("leverage", 1),
        "chev_moves":       trade.get("chev_moves", []),
        "open_ts":          trade.get("open_ts", ""),
    }
    try:
        journal = _load_journal()
        journal.append(entry)
        with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2)
        print(f"[Journal] Post-mortem saved for {trade['symbol']} ({outcome}). Total: {len(journal)} entries.")
        icon = "✓" if outcome == "WIN" else "✗"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        send_telegram_alert(f"{trade['symbol']} {trade['direction'].upper()} {icon} {pnl_str}")
        global _combined_closed_count
        _combined_closed_count += 1
        _maybe_run_cross_analysis()
        if len(journal) % 10 == 0:
            jane_j = _load_jane_journal()
            threading.Thread(target=_run_learning_session, args=(journal, jane_j), daemon=True).start()
    except Exception as e:
        print(f"[Journal] Save failed: {e}")


def fetch_binance_candles(symbol, interval, limit=700):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    df = pd.DataFrame(data, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tb_base","tb_quote","ignore"])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df[["open", "high", "low", "close", "volume"]]


def fetch_twelvedata_candles(symbol, interval_key, limit=700):
    interval = TIMEFRAMES_TWELVE.get(interval_key, "1h")
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": min(limit, 5000), "apikey": get_next_twelve_key()}
    response = requests.get(url, params=params, timeout=20)
    data = response.json()
    if data.get("status") == "error" or "values" not in data:
        raise Exception(data.get("message", "Twelve Data error"))
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float) if "volume" in df.columns else 0.0
    df.set_index("datetime", inplace=True)
    df = df.sort_index()
    return df[["open", "high", "low", "close", "volume"]]

def fetch_twelvedata_live_price(symbol):
    """Lightweight real-time quote - separate from candle data, used for SL/TP checks
    so we're not waiting on the last closed 1h candle to know what price actually is."""
    url = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": get_next_twelve_key()}
    response = requests.get(url, params=params, timeout=15)
    data = response.json()
    if "price" not in data:
        raise Exception(data.get("message", "Twelve Data live price error"))
    return float(data["price"])

def fetch_freeforexapi_price(symbol):
    """Free real-time forex via FreeForexAPI. No API key. EUR/USD → EURUSD."""
    fx_symbol = symbol.replace("/", "")
    resp = requests.get(
        "https://www.freeforexapi.com/api/live",
        params={"pairs": fx_symbol},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 200 or fx_symbol not in data.get("rates", {}):
        raise Exception(f"FreeForexAPI: no rate for {fx_symbol}")
    return float(data["rates"][fx_symbol]["rate"])


def fetch_yfinance_price(symbol, asset_type):
    """Free unlimited price via Yahoo Finance. Forex: EUR/USD → EURUSD=X. No API key needed."""
    yf_symbol = (symbol.replace("/", "") + "=X") if asset_type == "forex" else symbol
    hist = yf.Ticker(yf_symbol).history(period="1d", interval="5m")
    if hist.empty:
        raise Exception(f"yfinance: empty history for {yf_symbol}")
    price = float(hist["Close"].iloc[-1])
    if price <= 0:
        raise Exception(f"yfinance: invalid price {price} for {yf_symbol}")
    return price


def fetch_coingecko_price(symbol):
    """Free CoinGecko fallback for crypto. 30 req/min on free tier."""
    coin_id = COINGECKO_IDS.get(symbol)
    if not coin_id:
        raise Exception(f"No CoinGecko mapping for {symbol}")
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": coin_id, "vs_currencies": "usd"},
        timeout=10,
    )
    data = resp.json()
    if coin_id not in data or "usd" not in data[coin_id]:
        raise Exception(f"CoinGecko: no price in response for {coin_id}")
    return float(data[coin_id]["usd"])


def _to_exchange_symbol(symbol):
    """Convert Binance format (ADAUSDT) to KuCoin/OKX format (ADA-USDT)."""
    for quote in ("USDT", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote):
            return symbol[:-len(quote)] + "-" + quote
    return symbol


def fetch_kucoin_price(symbol):
    """Free KuCoin spot price. No API key needed."""
    resp = requests.get(
        "https://api.kucoin.com/api/v1/market/orderbook/level1",
        params={"symbol": _to_exchange_symbol(symbol)},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != "200000" or not data.get("data"):
        raise Exception(f"KuCoin price error: {data.get('msg', 'unknown')}")
    return float(data["data"]["price"])


def fetch_okx_price(symbol):
    """Free OKX spot price. No API key needed."""
    resp = requests.get(
        "https://www.okx.com/api/v5/market/ticker",
        params={"instId": _to_exchange_symbol(symbol)},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != "0" or not data.get("data"):
        raise Exception(f"OKX price error: {data.get('msg', 'unknown')}")
    return float(data["data"][0]["last"])


def fetch_kucoin_candles(symbol, interval_key, limit=700):
    """Free KuCoin OHLCV candles. No API key needed.
    KuCoin format: [time_sec, open, close, high, low, volume, turnover] — note close before high/low."""
    kc_symbol = _to_exchange_symbol(symbol)
    interval_map = {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day"}
    kc_interval = interval_map.get(interval_key, "1hour")
    resp = requests.get(
        "https://api.kucoin.com/api/v1/market/candles",
        params={"symbol": kc_symbol, "type": kc_interval},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != "200000" or not data.get("data"):
        raise Exception(f"KuCoin candles error: {data.get('msg', 'unknown')}")
    records = []
    for r in reversed(data["data"]):  # KuCoin returns newest-first; reverse to chronological
        records.append({
            "datetime": pd.Timestamp(int(r[0]), unit="s"),
            "open": float(r[1]), "high": float(r[3]), "low": float(r[4]),
            "close": float(r[2]), "volume": float(r[5]),
        })
    df = pd.DataFrame(records).set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]].tail(limit)


def fetch_okx_candles(symbol, interval_key, limit=700):
    """Free OKX OHLCV candles. No API key needed. Max 300 per request."""
    okx_symbol = _to_exchange_symbol(symbol)
    interval_map = {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D"}
    okx_interval = interval_map.get(interval_key, "1H")
    resp = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": okx_symbol, "bar": okx_interval, "limit": min(limit, 300)},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != "0" or not data.get("data"):
        raise Exception(f"OKX candles error: {data.get('msg', 'unknown')}")
    records = []
    for r in reversed(data["data"]):  # OKX returns newest-first
        records.append({
            "datetime": pd.Timestamp(int(r[0]), unit="ms"),
            "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
            "close": float(r[4]), "volume": float(r[5]),
        })
    df = pd.DataFrame(records).set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]].tail(limit)


def fetch_yfinance_candles(symbol, interval_key, limit=700):
    """Free unlimited candle data via Yahoo Finance. Used as Twelve Data fallback.
    Uses Ticker.history() — always returns flat column names regardless of yfinance version."""
    yf_symbol = (symbol.replace("/", "") + "=X") if "/" in symbol else symbol
    # yfinance has no native 4h — fetch 1h and resample
    yf_interval = "1h" if interval_key == "4h" else ("1d" if interval_key == "1d" else interval_key)
    period = {"5m": "7d", "15m": "7d", "30m": "30d", "1h": "60d", "4h": "90d", "1d": "2y"}.get(interval_key, "60d")
    df = yf.Ticker(yf_symbol).history(period=period, interval=yf_interval, auto_adjust=True)
    if df.empty:
        raise Exception(f"yfinance candles: no data for {yf_symbol}")
    df.columns = [c.lower() for c in df.columns]
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    if interval_key == "4h":
        df = df.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return df.tail(limit)


_candle_cache      = {}
_candle_cache_lock = threading.Lock()
_CANDLE_CACHE_TTL  = 60  # seconds

def fetch_candles(symbol, asset_type, timeframe, limit=700):
    key = (symbol, asset_type, timeframe, limit)
    with _candle_cache_lock:
        if key in _candle_cache:
            df, ts = _candle_cache[key]
            if time.time() - ts < _CANDLE_CACHE_TTL:
                return df.copy()

    if asset_type == "crypto":
        try:
            df = fetch_binance_candles(symbol, timeframe, limit)
        except Exception as e:
            print(f"[{datetime.now()}] Binance failed for {symbol} {timeframe} ({e}) — trying KuCoin")
            try:
                df = fetch_kucoin_candles(symbol, timeframe, limit)
            except Exception as e:
                print(f"[{datetime.now()}] KuCoin failed for {symbol} {timeframe} ({e}) — trying OKX")
                df = fetch_okx_candles(symbol, timeframe, limit)
    else:
        try:
            df = fetch_yfinance_candles(symbol, timeframe, limit)
        except Exception as e:
            print(f"[{datetime.now()}] yfinance failed for {symbol} {timeframe} ({e}) — trying Twelve Data")
            df = fetch_twelvedata_candles(symbol, timeframe, limit)

    with _candle_cache_lock:
        _candle_cache[key] = (df, time.time())
    return df


def get_current_price(symbol, asset_type):
    try:
        if asset_type == "crypto":
            try:
                df = fetch_binance_candles(symbol, "1m", limit=1)
                return float(df["close"].iloc[-1])
            except Exception:
                pass
            try:
                return fetch_kucoin_price(symbol)
            except Exception:
                pass
            try:
                return fetch_okx_price(symbol)
            except Exception:
                pass
            return fetch_coingecko_price(symbol)
        else:
            if asset_type == "forex":
                try:
                    return fetch_freeforexapi_price(symbol)
                except Exception:
                    pass
            try:
                return fetch_yfinance_price(symbol, asset_type)
            except Exception:
                return fetch_twelvedata_live_price(symbol)
    except Exception as e:
        print(f"[{datetime.now()}] Price check failed for {symbol}: {e}")
        return None

# ============================================================
# SWING / S-R / FIB / RSI CALCULATIONS
# ============================================================

def find_swings(df, window=3, threshold_pct=0.3, confirm_window=8):
    highs = df["high"].values
    lows = df["low"].values
    times = df.index
    n = len(df)
    swing_highs, swing_lows = [], []
    for i in range(window, n - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            future_lows = lows[i+1:min(i+1+confirm_window, n)]
            if len(future_lows) > 0 and (highs[i] - future_lows.min()) / highs[i] * 100 >= threshold_pct:
                swing_highs.append((times[i], highs[i]))
        if lows[i] == min(lows[i-window:i+window+1]):
            future_highs = highs[i+1:min(i+1+confirm_window, n)]
            if len(future_highs) > 0 and (future_highs.max() - lows[i]) / lows[i] * 100 >= threshold_pct:
                swing_lows.append((times[i], lows[i]))
    return swing_highs, swing_lows


def cluster(points, tolerance_pct=0.5):
    if not points:
        return []
    points = sorted(points, key=lambda p: p[1])
    clusters = []
    current = [points[0]]
    for p in points[1:]:
        if abs(p[1] - current[-1][1]) / current[-1][1] * 100 <= tolerance_pct:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)
    result = []
    for c in clusters:
        prices = [pt[1] for pt in c]
        timestamps = [pt[0] for pt in c]
        result.append({"price": sum(prices) / len(prices), "touches": len(c), "timestamps": timestamps})
    return result


def count_validated_instances(zone_price, per_tf_timestamps, primary_df, tolerance_pct=0.8, leave_pct=1.0):
    closes = primary_df["close"]
    visits = []
    visit_start, visit_end = None, None
    qualified_for_new_visit = True

    for ts, price in closes.items():
        deviation_pct = abs(price - zone_price) / zone_price * 100
        is_in_zone = deviation_pct <= tolerance_pct

        if is_in_zone:
            if visit_start is None and qualified_for_new_visit:
                visit_start = ts
                visit_end = ts
                qualified_for_new_visit = False
            elif visit_start is not None:
                visit_end = ts
        else:
            if visit_start is not None:
                visits.append((visit_start, visit_end))
                visit_start = None
            if deviation_pct >= leave_pct:
                qualified_for_new_visit = True

    if visit_start is not None:
        visits.append((visit_start, visit_end))

    instance_count = 0
    for start, end in visits:
        all_tfs_present = all(
            any(start <= t <= end for t in per_tf_timestamps.get(tf, []))
            for tf in MIN_TOUCHES_PER_TF
        )
        if all_tfs_present:
            instance_count += 1

    return instance_count


def compute_validated_levels(tf_clusters, primary_df, tolerance_pct=0.8, leave_pct=1.0):
    """
    A price zone only counts as REAL support/resistance if it has enough
    touches on EVERY timeframe at once - not just touches added together.
    Strength = how many separate times price actually left this zone and
    came back (an "instance"), not raw touch count.
    Returns (tier_a_levels, tier_b_levels).
    """
    all_points = []
    for tf, clusters_list in tf_clusters.items():
        for c in clusters_list:
            all_points.append({"price": c["price"], "touches": c["touches"], "timestamps": c["timestamps"], "tf": tf})
    if not all_points:
        return [], []
    all_points.sort(key=lambda x: x["price"])

    groups = []
    used = [False] * len(all_points)
    for i, p in enumerate(all_points):
        if used[i]:
            continue
        group = [p]
        used[i] = True
        for j in range(i + 1, len(all_points)):
            if used[j]:
                continue
            if abs(all_points[j]["price"] - p["price"]) / p["price"] * 100 <= tolerance_pct:
                group.append(all_points[j])
                used[j] = True
        groups.append(group)

    tier_a, tier_b = [], []
    for group in groups:
        per_tf_touches, per_tf_timestamps = {}, {}
        total_touches, weighted_price_sum = 0, 0
        for g in group:
            per_tf_touches[g["tf"]] = per_tf_touches.get(g["tf"], 0) + g["touches"]
            per_tf_timestamps.setdefault(g["tf"], []).extend(g["timestamps"])
            total_touches += g["touches"]
            weighted_price_sum += g["price"] * g["touches"]
        zone_price = weighted_price_sum / total_touches

        passes_all = all(per_tf_touches.get(tf, 0) >= min_t for tf, min_t in MIN_TOUCHES_PER_TF.items())

        if passes_all:
            instances = count_validated_instances(zone_price, per_tf_timestamps, primary_df, tolerance_pct, leave_pct)
            tier_a.append({
                "price": round(zone_price, 4),
                "touches": total_touches,
                "timeframes": sorted(per_tf_timestamps.keys()),
                "instances": instances,
                "tier": min(instances, 4),
            })
        else:
            for tf, count in per_tf_touches.items():
                if count >= MIN_TOUCHES_PER_TF.get(tf, 999):
                    tier_b.append({
                        "price": round(zone_price, 4),
                        "touches": count,
                        "timeframes": [tf],
                        "instances": None,
                        "tier": "unconfirmed (single timeframe only)",
                    })
                    break

    return tier_a, tier_b


def fib_from_real_impulse(df, lookback=150):
    window = df.tail(lookback)
    idx_max = window["high"].idxmax()
    idx_min = window["low"].idxmin()
    high_price = window.loc[idx_max, "high"]
    low_price  = window.loc[idx_min, "low"]
    diff = high_price - low_price
    if diff == 0:
        return {}, {}
    anchors = {
        "low_t":  int(idx_min.timestamp()),
        "low_p":  round(float(low_price),  6),
        "high_t": int(idx_max.timestamp()),
        "high_p": round(float(high_price), 6),
    }
    if idx_min < idx_max:
        return {
            "50%": high_price - diff * 0.5,
            "61.8% (golden pocket)": high_price - diff * 0.618,
            "65%": high_price - diff * 0.65,
            "78.6%": high_price - diff * 0.786,
        }, anchors
    return {
        "50%": low_price + diff * 0.5,
        "61.8% (golden pocket)": low_price + diff * 0.618,
        "65%": low_price + diff * 0.65,
        "78.6%": low_price + diff * 0.786,
    }, anchors


def add_rsi(df):
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df


def _find_swing_pivots(df, lb=7):
    """Price-only pivot detection (used outside divergence context)."""
    highs = df["high"].values
    lows  = df["low"].values
    n = len(df)
    sw_h, sw_l = [], []
    for i in range(lb, n - lb):
        if highs[i] >= highs[i - lb:i].max() and highs[i] > highs[i + 1:i + lb + 1].max():
            sw_h.append((i, float(highs[i])))
        if lows[i] <= lows[i - lb:i].min() and lows[i] < lows[i + 1:i + lb + 1].min():
            sw_l.append((i, float(lows[i])))
    return sw_h, sw_l


def _find_dual_pivots(highs, lows, rsi_vals, lb=7):
    """Divergence-quality pivot detection: candle must be a swing pivot on BOTH price AND RSI.
    A bullish divergence anchor must be a price swing low AND an RSI swing low at the same bar.
    A bearish divergence anchor must be a price swing high AND an RSI swing high at the same bar.
    Returns (sw_h, sw_l) as lists of (index, price) tuples."""
    n = len(highs)
    sw_h, sw_l = [], []
    for i in range(lb, n - lb):
        r = rsi_vals[i]
        if np.isnan(r):
            continue
        lr = rsi_vals[i - lb:i]
        rr = rsi_vals[i + 1:i + lb + 1]
        if np.any(np.isnan(lr)) or np.any(np.isnan(rr)):
            continue
        is_price_h = highs[i] >= highs[i - lb:i].max() and highs[i] > highs[i + 1:i + lb + 1].max()
        is_price_l = lows[i]  <= lows[i - lb:i].min()  and lows[i]  < lows[i + 1:i + lb + 1].min()
        is_rsi_h   = r >= lr.max() and r > rr.max()
        is_rsi_l   = r <= lr.min() and r < rr.min()
        if is_price_h and is_rsi_h:
            sw_h.append((i, float(highs[i])))
        if is_price_l and is_rsi_l:
            sw_l.append((i, float(lows[i])))
    return sw_h, sw_l


def _forming_div_score(rsi_gap):
    if rsi_gap >= 15: return 2.0
    if rsi_gap >= 10: return 1.5
    if rsi_gap >= 5:  return 1.0
    return 0.0


def _detect_forming_divergence(df, lookback=150, mini=5):
    """Detect forming divergences: confirmed first pivot (dual, 7-bar) + current mini-extreme has exceeded it + RSI diverging.
    Returns list of dicts sorted by score desc, up to one of each type."""
    if "RSI" not in df.columns:
        return []
    rsi   = df["RSI"].values
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)
    lb    = 7
    if n < lookback + lb:
        return []

    start = max(lb, n - lookback)
    end   = n - lb   # confirmed pivots only — lb-bar lookahead must be fully past

    sw_h, sw_l = [], []
    for i in range(start, end):
        r = rsi[i]
        if np.isnan(r): continue
        lr = rsi[i - lb:i]; rr = rsi[i + 1:i + lb + 1]
        if np.any(np.isnan(lr)) or np.any(np.isnan(rr)): continue
        is_ph = highs[i] >= highs[i-lb:i].max() and highs[i] > highs[i+1:i+lb+1].max()
        is_pl = lows[i]  <= lows[i-lb:i].min()  and lows[i]  < lows[i+1:i+lb+1].min()
        is_rh = r >= lr.max() and r > rr.max()
        is_rl = r <= lr.min() and r < rr.min()
        if is_ph and is_rh: sw_h.append((i, float(highs[i]), r))
        if is_pl and is_rl: sw_l.append((i, float(lows[i]),  r))

    if not sw_h and not sw_l:
        return []

    # Current mini-extreme: max/min of last `mini` bars
    cur_hi_idx = n - mini + int(np.argmax(highs[n-mini:n]))
    cur_lo_idx = n - mini + int(np.argmin(lows[n-mini:n]))
    cur_hi     = float(highs[cur_hi_idx])
    cur_lo     = float(lows[cur_lo_idx])
    cur_rhi    = float(rsi[cur_hi_idx]) if not np.isnan(rsi[cur_hi_idx]) else None
    cur_rlo    = float(rsi[cur_lo_idx]) if not np.isnan(rsi[cur_lo_idx]) else None

    results = []
    seen    = set()

    # Regular Bearish: price new high BUT RSI lower — momentum exhaustion
    if cur_rhi is not None:
        for pi, pp, pr in reversed(sw_h):
            if "rb" in seen: break
            if cur_hi > pp:
                gap = pr - cur_rhi
                if gap >= 5:
                    results.append({"type": "Regular Bearish", "bias": "bear",
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_hi, 5), "cur_rsi": round(cur_rhi, 2)})
                    seen.add("rb")

    # Regular Bullish: price new low BUT RSI higher — selling exhaustion
    if cur_rlo is not None:
        for pi, pp, pr in reversed(sw_l):
            if "bull" in seen: break
            if cur_lo < pp:
                gap = cur_rlo - pr
                if gap >= 5:
                    results.append({"type": "Regular Bullish", "bias": "bull",
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_lo, 5), "cur_rsi": round(cur_rlo, 2)})
                    seen.add("bull")

    # Hidden Bearish: price lower high BUT RSI higher high — downtrend continuation
    if cur_rhi is not None:
        for pi, pp, pr in reversed(sw_h):
            if "hbear" in seen: break
            if cur_hi < pp:
                gap = cur_rhi - pr
                if gap >= 5:
                    results.append({"type": "Hidden Bearish", "bias": "bear",
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_hi, 5), "cur_rsi": round(cur_rhi, 2)})
                    seen.add("hbear")

    # Hidden Bullish: price higher low BUT RSI lower low — uptrend continuation
    if cur_rlo is not None:
        for pi, pp, pr in reversed(sw_l):
            if "hbull" in seen: break
            if cur_lo > pp:
                gap = pr - cur_rlo
                if gap >= 5:
                    results.append({"type": "Hidden Bullish", "bias": "bull",
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_lo, 5), "cur_rsi": round(cur_rlo, 2)})
                    seen.add("hbull")

    return sorted(results, key=lambda x: -x["score"])


def _rsi_50_cross(df):
    """Return dict if RSI crossed 50 within the last 3 bars, else None."""
    if "RSI" not in df.columns or len(df) < 2:
        return None
    rsi = df["RSI"].values
    for i in range(1, min(4, len(rsi))):
        prev, curr = rsi[-i-1], rsi[-i]
        if np.isnan(prev) or np.isnan(curr): continue
        if prev < 50 <= curr:
            return {"direction": "bullish", "bars_ago": i}
        if prev >= 50 > curr:
            return {"direction": "bearish", "bars_ago": i}
    return None


def find_swing_points_indexed(df, window=3, threshold_pct=0.3, confirm_window=8):
    return _find_swing_pivots(df)


def detect_rsi_divergence(df):
    if "RSI" not in df.columns:
        return []
    rsi = df["RSI"].values
    sw_h, sw_l = _find_dual_pivots(df["high"].values, df["low"].values, rsi)

    def _strongest(pivots, checks):
        best = None; best_score = (-1, 0.0)
        for k in range(len(pivots) - 1):
            i1, p1 = pivots[k]; i2, p2 = pivots[k + 1]
            r1, r2 = float(rsi[i1]), float(rsi[i2])
            if np.isnan(r1) or np.isnan(r2) or abs(r2 - r1) < 2.0:
                continue
            for pc, rc, typ, note in checks:
                if pc(p1, p2) and rc(r1, r2):
                    score = (i2, abs(r2 - r1))
                    if score > best_score:
                        best_score = score
                        best = {"type": typ, "price": round(p2, 5), "note": note}
        return best

    found = []
    bull = _strongest(sw_l, [
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Regular Bullish Divergence", "price LL, RSI HL — selling momentum fading"),
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Hidden Bullish Divergence",  "price HL, RSI LL — uptrend continuation"),
    ])
    bear = _strongest(sw_h, [
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Regular Bearish Divergence", "price HH, RSI LH — momentum fading"),
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Hidden Bearish Divergence",  "price LH, RSI HH — downtrend continuation"),
    ])
    if bull: found.append(bull)
    if bear: found.append(bear)
    return found


def is_ny_market_hours():
    from datetime import datetime, timezone, timedelta
    ny_offset = timedelta(hours=-4)
    ny_time = datetime.now(timezone.utc) + ny_offset
    return 9 <= ny_time.hour < 18


# ============================================================
# FULL MARKET ANALYSIS (builds rich data brief for Chev)
# ============================================================

def _ca_add_indicators(df):
    df = df.copy()
    df["EMA13"] = df["close"].ewm(span=13, adjust=False).mean()
    df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA55"] = df["close"].ewm(span=55, adjust=False).mean()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["VWAP"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))
    # ATR
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    df["ATR"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()
    # Bollinger Bands (20-period, 2 std dev)
    bb_mid = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["BB_upper"] = bb_mid + 2 * bb_std
    df["BB_mid"]   = bb_mid
    df["BB_lower"] = bb_mid - 2 * bb_std
    # MACD (12, 26, 9)
    exp12 = df["close"].ewm(span=12, adjust=False).mean()
    exp26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"]        = exp12 - exp26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"]   = df["MACD"] - df["MACD_signal"]
    return df


def _parse_ob(bids_raw, asks_raw):
    """Normalise raw [[price, qty]] lists into (float, float) tuples."""
    bids = [(float(p), float(q)) for p, q in bids_raw]
    asks = [(float(p), float(q)) for p, q in asks_raw]
    return bids, asks


def _ob_stats(bids, asks, name):
    if not bids and not asks:
        return None
    total_b = sum(q for _, q in bids)
    total_a = sum(q for _, q in asks)
    denom = total_b + total_a
    imbalance = round((total_b - total_a) / denom, 3) if denom else 0
    top_bid = max(bids, key=lambda x: x[1]) if bids else None
    top_ask = max(asks, key=lambda x: x[1]) if asks else None
    return {"exchange": name, "imbalance": imbalance,
            "top_bid": top_bid, "top_ask": top_ask,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "bids": bids, "asks": asks}


def _fetch_ob_binance_spot(symbol):
    clean = symbol.replace("/", "").upper()
    r = requests.get("https://api.binance.com/api/v3/depth",
                     params={"symbol": clean, "limit": 500}, timeout=8)
    d = r.json()
    b, a = _parse_ob(d["bids"], d["asks"])
    return _ob_stats(b, a, "Binance Spot")


def _fetch_ob_binance_futures(symbol):
    clean = symbol.replace("/", "").upper()
    r = requests.get("https://fapi.binance.com/fapi/v1/depth",
                     params={"symbol": clean, "limit": 1000}, timeout=8)
    d = r.json()
    b, a = _parse_ob(d["bids"], d["asks"])
    return _ob_stats(b, a, "Binance Futures")


def _fetch_ob_bybit(symbol):
    clean = symbol.replace("/", "").upper()
    r = requests.get("https://api.bybit.com/v5/market/orderbook",
                     params={"category": "linear", "symbol": clean, "limit": 500}, timeout=8)
    d = r.json()["result"]
    b, a = _parse_ob(d["b"], d["a"])
    return _ob_stats(b, a, "Bybit")


def _fetch_ob_okx(symbol):
    inst_id = symbol.replace("/", "").upper().replace("USDT", "-USDT")
    r = requests.get("https://www.okx.com/api/v5/market/books",
                     params={"instId": inst_id, "sz": 400}, timeout=8)
    d = r.json()["data"][0]
    b, a = _parse_ob(d["bids"], d["asks"])
    return _ob_stats(b, a, "OKX")


def _fetch_ob_coinbase(symbol):
    _cb_map = {"BTC": "BTC", "ETH": "ETH", "XRP": "XRP", "SOL": "SOL",
               "ADA": "ADA", "XLM": "XLM", "DOGE": "DOGE", "AVAX": "AVAX"}
    base = symbol.upper().replace("USDT", "").replace("BUSD", "")
    cb_base = _cb_map.get(base, base)
    pair = f"{cb_base}-USD"
    r = requests.get(f"https://api.exchange.coinbase.com/products/{pair}/book",
                     params={"level": 2}, timeout=8)
    d = r.json()
    b, a = _parse_ob([[x[0], x[1]] for x in d["bids"][:50]],
                     [[x[0], x[1]] for x in d["asks"][:50]])
    return _ob_stats(b, a, "Coinbase")


def _fetch_ob_kraken(symbol):
    _kr_map = {"BTC": "XBT", "DOGE": "XDG"}
    base = symbol.upper().replace("USDT", "").replace("BUSD", "")
    kr_base = _kr_map.get(base, base)
    pair = f"{kr_base}USDT"
    r = requests.get("https://api.kraken.com/0/public/Depth",
                     params={"pair": pair, "count": 500}, timeout=8)
    d = r.json()
    result = d.get("result", {})
    key = next(iter(result), None)
    if not key:
        return None
    b, a = _parse_ob([[x[0], x[1]] for x in result[key]["bids"]],
                     [[x[0], x[1]] for x in result[key]["asks"]])
    return _ob_stats(b, a, "Kraken")


def _aggregate_orderbooks(symbol):
    """Fetch deep order books from 6 exchanges and build a full liquidity map."""
    fetchers = [_fetch_ob_binance_spot, _fetch_ob_binance_futures,
                _fetch_ob_bybit, _fetch_ob_okx, _fetch_ob_coinbase, _fetch_ob_kraken]
    results = []
    for fn in fetchers:
        try:
            ob = fn(symbol)
            if ob:
                results.append(ob)
        except Exception:
            pass
    if not results:
        return None

    # ── Combine every level from every exchange ───────────────────────────
    all_bids_raw, all_asks_raw = [], []
    for ob in results:
        all_bids_raw.extend(ob["bids"])
        all_asks_raw.extend(ob["asks"])

    if not all_bids_raw or not all_asks_raw:
        return None

    best_bid  = max(p for p, _ in all_bids_raw)
    best_ask  = min(p for p, _ in all_asks_raw)
    mid_price = (best_bid + best_ask) / 2

    # ── Cluster nearby price levels (merge within 0.2%) ──────────────────
    def cluster_levels(levels, descending=True):
        if not levels:
            return []
        srt = sorted(levels, key=lambda x: x[0], reverse=descending)
        out = []
        cp, cq = srt[0]
        for p, q in srt[1:]:
            if cp > 0 and abs(p - cp) / cp * 100 <= 0.2:
                total = cq + q
                cp = (cp * cq + p * q) / total
                cq = total
            else:
                out.append((cp, cq))
                cp, cq = p, q
        out.append((cp, cq))
        return out

    bid_clusters = cluster_levels(all_bids_raw, descending=True)   # highest bid first
    ask_clusters = cluster_levels(all_asks_raw, descending=False)   # lowest ask first

    # ── Keep only ±10% from mid price ────────────────────────────────────
    bid_in_range = [(p, q) for p, q in bid_clusters if mid_price > 0 and abs(p - mid_price) / mid_price * 100 <= 10.0]
    ask_in_range = [(p, q) for p, q in ask_clusters if mid_price > 0 and abs(p - mid_price) / mid_price * 100 <= 10.0]

    # ── Apply wall labels (pre-computed so display code is simple) ────────
    def _label(qty, qtys):
        if not qtys:
            return ""
        avg = sum(qtys) / len(qtys)
        if qty >= avg * 5:   return "  ⚡ MASSIVE"
        if qty >= avg * 2.5: return "  ← MAJOR"
        if qty >= avg * 1.4: return "  ← notable"
        return ""

    bid_qtys = [q for _, q in bid_in_range]
    ask_qtys = [q for _, q in ask_in_range]
    bid_labeled = [(p, q, _label(q, bid_qtys)) for p, q in bid_in_range]
    ask_labeled = [(p, q, _label(q, ask_qtys)) for p, q in ask_in_range]

    # ── Air pocket detection (thin zones where price will move fast) ───────
    def air_pockets(clusters, mid):
        if len(clusters) < 4:
            return []
        qtys = [q for _, q, _ in clusters]
        avg  = sum(qtys) / len(qtys)
        return [(p, abs(p - mid) / mid * 100)
                for p, q, _ in clusters
                if q < avg * 0.25 and abs(p - mid) / mid * 100 > 0.3][:4]

    # ── Cross-exchange wall detection (top wall per exchange within 0.15%) ─
    all_top_bids = [(ob["exchange"], p, q) for ob in results if ob["top_bid"] for p, q in [ob["top_bid"]]]
    all_top_asks = [(ob["exchange"], p, q) for ob in results if ob["top_ask"] for p, q in [ob["top_ask"]]]

    def multi_exchange_walls(walls):
        clusters, used = [], set()
        for i, (ex1, p1, q1) in enumerate(walls):
            if i in used:
                continue
            cluster = [(ex1, p1, q1)]
            for j, (ex2, p2, q2) in enumerate(walls):
                if j != i and j not in used and abs(p1 - p2) / p1 * 100 <= 0.15:
                    cluster.append((ex2, p2, q2))
                    used.add(j)
            if len(cluster) >= 2:
                clusters.append({
                    "price":     sum(p for _, p, _ in cluster) / len(cluster),
                    "exchanges": [ex for ex, _, _ in cluster],
                })
            used.add(i)
        return clusters

    multi_bid = multi_exchange_walls(all_top_bids)
    multi_ask = multi_exchange_walls(all_top_asks)

    avg_imbalance = sum(ob["imbalance"] for ob in results) / len(results)
    overall = ("bullish — more bids than asks" if avg_imbalance > 0.1
               else "bearish — more asks than bids" if avg_imbalance < -0.1
               else "balanced")

    return {
        "exchanges":                results,
        "avg_imbalance":            round(avg_imbalance, 3),
        "overall":                  overall,
        "mid_price":                mid_price,
        "bid_clusters":             bid_labeled,
        "ask_clusters":             ask_labeled,
        "bid_air_pockets":          air_pockets(bid_labeled, mid_price),
        "ask_air_pockets":          air_pockets(ask_labeled, mid_price),
        "multi_exchange_bid_walls": multi_bid,
        "multi_exchange_ask_walls": multi_ask,
    }


def _fetch_binance_futures_data(symbol):
    """Fetch open interest and funding rate from Binance perpetual futures."""
    try:
        clean = symbol.replace("/", "").upper()
        oi_r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                             params={"symbol": clean}, timeout=5)
        fr_r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             params={"symbol": clean}, timeout=5)
        return {
            "open_interest": float(oi_r.json().get("openInterest", 0)),
            "funding_rate":  float(fr_r.json().get("lastFundingRate", 0)),
        }
    except Exception:
        return None




def _ca_find_swing_points_indexed(df, window=3, threshold_pct=0.3, confirm_window=8):
    return _find_swing_pivots(df)


def _ca_get_timed_touches(df, window=3, threshold_pct=0.3, confirm_window=8):
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    touches = []
    for i in range(window, n - window):
        if highs[i] == max(highs[i - window: i + window + 1]):
            future_lows = lows[i + 1: min(i + 1 + confirm_window, n)]
            if len(future_lows) > 0 and (highs[i] - future_lows.min()) / highs[i] * 100 >= threshold_pct:
                touches.append((df.index[i], highs[i], "resistance"))
        if lows[i] == min(lows[i - window: i + window + 1]):
            future_highs = highs[i + 1: min(i + 1 + confirm_window, n)]
            if len(future_highs) > 0 and (future_highs.max() - lows[i]) / lows[i] * 100 >= threshold_pct:
                touches.append((df.index[i], lows[i], "support"))
    return touches


def _ca_build_validated_levels(touches_by_tf, kind, tolerance_pct=0.6, episode_gap_hours=12, min_touches_map=None):
    if min_touches_map is None:
        min_touches_map = {"15m": 5, "30m": 3, "1h": 2, "4h": 2}
    all_prices = [p for tf in touches_by_tf for (_, p) in touches_by_tf[tf]]
    if not all_prices:
        return []
    sorted_prices = sorted(all_prices)
    zones, current = [], [sorted_prices[0]]
    for p in sorted_prices[1:]:
        if abs(p - current[-1]) / current[-1] * 100 <= tolerance_pct:
            current.append(p)
        else:
            zones.append(current)
            current = [p]
    zones.append(current)
    results = []
    for center in [sum(z) / len(z) for z in zones]:
        zone_lo = center * (1 - tolerance_pct / 100)
        zone_hi = center * (1 + tolerance_pct / 100)
        combined = []
        for tf, touches in touches_by_tf.items():
            for ts, p in touches:
                if zone_lo <= p <= zone_hi:
                    combined.append((ts, tf))
        if not combined:
            continue
        combined.sort(key=lambda x: x[0])
        episodes, current_ep = [], [combined[0]]
        for item in combined[1:]:
            if (item[0] - current_ep[-1][0]).total_seconds() / 3600 > episode_gap_hours:
                episodes.append(current_ep)
                current_ep = [item]
            else:
                current_ep.append(item)
        episodes.append(current_ep)
        validated_count = 0
        for ep in episodes:
            counts = {tf: 0 for tf in min_touches_map}
            for ts, tf in ep:
                if tf in counts:
                    counts[tf] += 1
            if all(counts[tf] >= min_touches_map[tf] for tf in min_touches_map):
                validated_count += 1
        if validated_count >= 1:
            results.append({"price": round(center, 5), "instances": validated_count, "kind": kind})
    return sorted(results, key=lambda r: r["instances"], reverse=True)


def _ca_build_single_tf_levels(touches, min_touches=3, tolerance_pct=0.5):
    prices = [p for _, p in touches]
    if not prices:
        return []
    prices_sorted = sorted(prices)
    clusters, current = [], [prices_sorted[0]]
    for p in prices_sorted[1:]:
        if abs(p - current[-1]) / current[-1] * 100 <= tolerance_pct:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)
    return [{"price": round(sum(c) / len(c), 5), "touches": len(c)} for c in clusters if len(c) >= min_touches]


def _ca_pick_nearest_level(current_price, tier_a, tier_b, near_threshold_pct=8.0):
    nearest_a = min(tier_a, key=lambda z: abs(z["price"] - current_price)) if tier_a else None
    nearest_b = min(tier_b, key=lambda z: abs(z["price"] - current_price)) if tier_b else None
    if nearest_a and abs(nearest_a["price"] - current_price) / current_price * 100 <= near_threshold_pct:
        return {"price": nearest_a["price"], "label": f"{nearest_a['instances']}x validated (multi-TF)"}
    if nearest_b:
        return {"price": nearest_b["price"], "label": f"{nearest_b['touches']}x touches (single-TF, lower confidence)"}
    if nearest_a:
        return {"price": nearest_a["price"], "label": f"{nearest_a['instances']}x validated (multi-TF, distant)"}
    return None


def _ca_detect_rsi_divergence(df):
    if "RSI" not in df.columns:
        return []
    rsi = df["RSI"].values
    sw_h, sw_l = _find_dual_pivots(df["high"].values, df["low"].values, rsi)

    def _strongest(pivots, checks):
        best = None; best_score = (-1, 0.0)
        for k in range(len(pivots) - 1):
            i1, p1 = pivots[k]; i2, p2 = pivots[k + 1]
            r1, r2 = float(rsi[i1]), float(rsi[i2])
            if np.isnan(r1) or np.isnan(r2) or abs(r2 - r1) < 2.0:
                continue
            for pc, rc, typ, note in checks:
                if pc(p1, p2) and rc(r1, r2):
                    score = (i2, abs(r2 - r1))
                    if score > best_score:
                        best_score = score
                        best = {"type": typ, "price": round(float(p2), 5), "note": note}
        return best

    found = []
    bull = _strongest(sw_l, [
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Regular Bullish Divergence", "price lower low, RSI higher low — selling momentum fading"),
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Hidden Bullish Divergence",  "price higher low, RSI lower low — uptrend likely continuing"),
    ])
    bear = _strongest(sw_h, [
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Regular Bearish Divergence", "price higher high, RSI lower high — upside momentum fading"),
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Hidden Bearish Divergence",  "price lower high, RSI higher high — downtrend likely continuing"),
    ])
    if bull: found.append(bull)
    if bear: found.append(bear)
    return found


def _ca_detect_rsi_divergence_forming(df, lookback=10):
    if "RSI" not in df.columns:
        return []
    swing_highs, swing_lows = _ca_find_swing_points_indexed(df)
    found = []
    recent = df.tail(lookback)
    try:
        recent_high_pos = df.index.get_loc(recent["high"].idxmax())
        recent_low_pos  = df.index.get_loc(recent["low"].idxmin())
    except Exception:
        return []
    if swing_highs:
        i1, p1 = swing_highs[-1]
        p2 = df["high"].iloc[recent_high_pos]
        if recent_high_pos > i1:
            r1, r2 = df["RSI"].iloc[i1], df["RSI"].iloc[recent_high_pos]
            if p2 > p1 and r2 < r1 and df["RSI"].iloc[-1] > 65:
                found.append({"type": "Early Bearish Divergence (forming)", "price": p2, "note": "price to new highs, RSI lagging and overbought (>65)"})
    if swing_lows:
        i1, p1 = swing_lows[-1]
        p2 = df["low"].iloc[recent_low_pos]
        if recent_low_pos > i1:
            r1, r2 = df["RSI"].iloc[i1], df["RSI"].iloc[recent_low_pos]
            if p2 < p1 and r2 > r1 and df["RSI"].iloc[-1] < 35:
                found.append({"type": "Early Bullish Divergence (forming)", "price": p2, "note": "price to new lows, RSI lagging and oversold (<35)"})
    return found


def _ca_volume_trend(df, n=10):
    recent = df["volume"].tail(n).values
    avg = recent.mean()
    if avg == 0:
        return "flat"
    slope = np.polyfit(np.arange(len(recent)), recent, 1)[0]
    pct = (slope / avg) * 100
    return "increasing" if pct > 3 else "decreasing" if pct < -3 else "flat"


def _ca_fib_from_real_impulse(df, lookback=150):
    window = df.tail(lookback)
    idx_max, idx_min = window["high"].idxmax(), window["low"].idxmin()
    high_price, low_price = window.loc[idx_max, "high"], window.loc[idx_min, "low"]
    diff = high_price - low_price
    if diff == 0:
        return {}, "flat"
    if idx_min < idx_max:
        direction = "uptrend"
        levels = {"50%": high_price - diff * 0.5, "61.8% (golden pocket)": high_price - diff * 0.618,
                  "65%": high_price - diff * 0.65, "78.6%": high_price - diff * 0.786}
    else:
        direction = "downtrend"
        levels = {"50%": low_price + diff * 0.5, "61.8% (golden pocket)": low_price + diff * 0.618,
                  "65%": low_price + diff * 0.65, "78.6%": low_price + diff * 0.786}
    return levels, direction


def _ca_detect_consolidation_range(df, max_atr_mult=5.0, min_candles=20, max_candles=150):
    """Find the most recent consolidation range using ATR-relative threshold.
    max_atr_mult: total window range must be ≤ this × avg candle range.
    Scales automatically — no hardcoded % that breaks across asset types."""
    n = len(df)
    if n < min_candles:
        return None
    best = None
    for window in range(min_candles, min(max_candles, n) + 1, 5):
        segment = df.iloc[n - window: n]
        avg_candle_range = (segment["high"] - segment["low"]).mean()
        if avg_candle_range <= 0:
            continue
        total_range = segment["high"].max() - segment["low"].min()
        if total_range <= avg_candle_range * max_atr_mult:
            best = (n - window, n - 1)
    return best


def _validate_anchor(df, anchor):
    """Validate that a detected anchor is confirmed and still active.

    Confirmation: the 5 candles after the anchor close in the expected direction
    (down from a swing high, up from a swing low). At least 2 must follow through.
    Without confirmation the move may have been a fake-out.

    Active: the auction is live — price has not been accepted back inside the
    pre-anchor balance zone. Two distinct failure modes:

    1. price_returned_to_balance — multiple recent closes back inside the range the
       market broke out of. The move unwound. A fresh auction has not yet started.

    2. structure_broken — price has closed beyond the anchor price itself in the
       wrong direction (new HH in a downtrend, new LL in an uptrend). The original
       structural swing no longer defines the market.

    Special case — retest held: if price dipped back into the balance zone temporarily
    but the most recent closes are back outside it, confidence is INCREASED. That's
    the textbook confirmation pattern — break, retest, continuation.

    Mutates the anchor dict in place with: confirmed, active, invalidation_reason.
    """
    idx = anchor["idx"]
    n   = len(df)

    if idx >= n - 1:
        anchor["confirmed"]           = False
        anchor["active"]              = True
        anchor["invalidation_reason"] = None
        return anchor

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    anchor_t     = anchor["anchor_type"]
    anchor_price = anchor["price"]
    anchor_close = float(closes[idx])

    # Infer direction for atr_breakout (no explicit swing type)
    if anchor_t == "swing_high":
        is_high = True
    elif anchor_t == "swing_low":
        is_high = False
    else:
        is_high = float(closes[-1]) < anchor_close  # dropped from anchor = high anchor

    # Balance zone = the 20 candles before the anchor (what the market left)
    bal_start = max(0, idx - 20)
    if idx > bal_start:
        bal_high = float(highs[bal_start:idx].max())
        bal_low  = float(lows[bal_start:idx].min())
    else:
        bal_high = anchor_price * 1.01
        bal_low  = anchor_price * 0.99

    # ── Confirmation: 5 candles after anchor ─────────────────────────────────
    post_c = closes[idx + 1: min(idx + 6, n)]
    if len(post_c) < 2:
        confirmed = False
    elif is_high:
        confirmed = sum(1 for c in post_c if c < anchor_close) >= 2
    else:
        confirmed = sum(1 for c in post_c if c > anchor_close) >= 2

    # ── Active check: last 10 candles ────────────────────────────────────────
    rec_start  = max(idx + 1, n - 10)
    rec_close  = closes[rec_start:n]
    active              = True
    invalidation_reason = None

    if len(rec_close) >= 3:
        inside_mask = [bal_low <= c <= bal_high for c in rec_close]
        n_inside    = sum(inside_mask)

        if n_inside >= 3:
            # Enough closes inside the balance zone — but did price leave again?
            # If the MOST RECENT 3 closes are back outside = retest held = still active
            recent_3_inside = sum(inside_mask[-3:])
            if recent_3_inside >= 2:
                active              = False
                invalidation_reason = "price_returned_to_balance"
            else:
                # Retest held: price dipped back, reclaimed, continuing move
                # This is stronger confirmation, not invalidation
                anchor["confidence"] = min(100, anchor["confidence"] + 15)

        # Structure broken: closes beyond the anchor price in the wrong direction.
        # 1 close might be a liquidity sweep — require 2+ for structural change.
        elif is_high:
            if sum(1 for c in rec_close if c > anchor_price) >= 2:
                active              = False
                invalidation_reason = "structure_broken"
        else:
            if sum(1 for c in rec_close if c < anchor_price) >= 2:
                active              = False
                invalidation_reason = "structure_broken"

    anchor["confirmed"]           = confirmed
    anchor["active"]              = active
    anchor["invalidation_reason"] = invalidation_reason

    # Adjust confidence based on validation result
    if not active:
        anchor["confidence"] = max(0, anchor["confidence"] - 30)
    elif confirmed:
        anchor["confidence"] = min(100, anchor["confidence"] + 10)

    return anchor


def _detect_auction_anchor(df, max_lookback=200, pivot_bars=5):
    """Frankenstein auction anchor — single source of truth for where a move started.

    Synthesizes all signals learned across the project:
    Method 1 (primary): Dual-pivot — price swing AND RSI swing at the same candle.
                        Same dual-pivot logic used in RSI divergence detection.
                        Each candidate is scored by RSI extreme level, volume,
                        ATR expansion, regime alignment, and distance from current.
    Method 2 (fallback): ATR compression -> expansion transition. Pattern-agnostic:
                         triangles, flags, wedges all show ATR contracting before
                         the break. First expansion candle after compression = anchor.
    Method 3 (last resort): Farthest qualifying fractal pivot >= 3 ATR from current.

    Returns: { idx, price, anchor_type, confidence, method } or None.
    """
    n = len(df)
    if n < 30:
        return None

    atr_s  = _an_atr_series(df, 14).values
    rsi_s  = _an_rsi_series(df, 14).values
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    vol    = df["volume"].values

    current = float(closes[-1])
    atr_now = float(atr_s[-1]) if not np.isnan(atr_s[-1]) else 0.0
    if atr_now <= 0:
        return None

    vol_avg = pd.Series(vol).rolling(20).mean().values
    lb      = max(pivot_bars + 1, n - max_lookback)
    win     = atr_s[lb:]
    valid_w = win[~np.isnan(win)]
    med_atr = float(np.median(valid_w)) if len(valid_w) > 5 else atr_now
    regime  = _an_regime(df).get("regime", "UNKNOWN")

    # ── Method 1: Dual-pivot (price + RSI swing at the same bar) ─────────────
    # RSI confirmed fractal: price is a local high/low AND RSI is a local high/low
    # simultaneously. This is the strongest swing signal in the project.
    best    = None
    best_sc = 0

    for i in range(lb + pivot_bars, n - pivot_bars):
        r = rsi_s[i]
        if np.isnan(r):
            continue
        lr = rsi_s[i - pivot_bars: i]
        rr = rsi_s[i + 1: i + pivot_bars + 1]
        if np.any(np.isnan(lr)) or np.any(np.isnan(rr)):
            continue

        is_ph = (highs[i] >= highs[i - pivot_bars: i].max() and
                 highs[i] >  highs[i + 1: i + pivot_bars + 1].max())
        is_pl = (lows[i]  <= lows[i - pivot_bars: i].min() and
                 lows[i]  <  lows[i + 1: i + pivot_bars + 1].min())
        is_rh = r >= float(lr.max()) and r > float(rr.max())
        is_rl = r <= float(lr.min()) and r < float(rr.min())

        for is_high in ([True] if (is_ph and is_rh) else []) + ([False] if (is_pl and is_rl) else []):
            pv   = highs[i] if is_high else lows[i]
            dist = (pv - current) if is_high else (current - pv)
            if dist < atr_now * 2.0:
                continue

            sc = 30  # dual-pivot baseline: price AND RSI swing at same bar

            # RSI extreme at pivot (strongest swing confirmation in the system)
            if is_high:
                sc += 25 if r >= 70 else 15 if r >= 60 else 5 if r >= 50 else 0
            else:
                sc += 25 if r <= 30 else 15 if r <= 40 else 5 if r <= 50 else 0

            # Volume expansion — decisive candles have above-average volume
            va = vol_avg[i]
            if not np.isnan(va) and va > 0 and vol[i] >= va * 1.2:
                sc += 15

            # ATR expansion at pivot — volatile candle = energy leaving equilibrium
            ai = atr_s[i]
            if not np.isnan(ai) and med_atr > 0 and ai >= med_atr * 1.1:
                sc += 10

            # Distance bonus: farther = more structural significance (capped at +20)
            sc += min(20, int(dist / atr_now * 5))

            # Regime alignment — swing direction matches the current trend
            if   regime == "TRENDING_DOWN" and is_high:     sc += 15
            elif regime == "TRENDING_UP"   and not is_high: sc += 15
            elif regime in ("RANGING", "CHOPPY"):           sc +=  5

            if sc > best_sc:
                best_sc = sc
                best = {
                    "idx":         i,
                    "price":       round(float(pv), 8),
                    "anchor_type": "swing_high" if is_high else "swing_low",
                    "confidence":  min(100, sc),
                    "method":      "dual_pivot",
                }

    if best and best_sc >= 40:
        return _validate_anchor(df, best)

    # ── Method 2: ATR compression → expansion ────────────────────────────────
    m = len(win)
    if len(valid_w) >= 15 and med_atr > 0:
        lo_t      = med_atr * 0.70
        hi_t      = med_atr * 1.10
        found_exp = False
        for j in range(m - 1, -1, -1):
            v = win[j]
            if np.isnan(v):
                continue
            if not found_exp:
                if v >= hi_t:
                    found_exp = True
            else:
                if v <= lo_t:
                    for k in range(j + 1, m):
                        if not np.isnan(win[k]) and win[k] >= hi_t:
                            idx = min(lb + k, n - 1)
                            return _validate_anchor(df, {
                                "idx":         idx,
                                "price":       round(float(closes[idx]), 8),
                                "anchor_type": "atr_breakout",
                                "confidence":  50,
                                "method":      "atr_expansion",
                            })

    # ── Method 3: Farthest fractal pivot ≥ 3 ATR from current ─────────────────
    pb         = 3
    candidates = []
    for i in range(lb + pb, n - pb):
        wh = highs[i - pb: i + pb + 1]
        if highs[i] == wh.max():
            d = highs[i] - current
            if d >= atr_now * 3:
                candidates.append((i, d, True))
        wl = lows[i - pb: i + pb + 1]
        if lows[i] == wl.min():
            d = current - lows[i]
            if d >= atr_now * 3:
                candidates.append((i, d, False))
    if candidates:
        best_f = max(candidates, key=lambda x: x[1])
        pv = highs[best_f[0]] if best_f[2] else lows[best_f[0]]
        return _validate_anchor(df, {
            "idx":         best_f[0],
            "price":       round(float(pv), 8),
            "anchor_type": "swing_high" if best_f[2] else "swing_low",
            "confidence":  30,
            "method":      "fractal_fallback",
        })

    return None


def _ca_find_structure_start(df, **kwargs):
    """Thin wrapper — returns candle index only. Call _detect_auction_anchor for the full dict."""
    result = _detect_auction_anchor(df, **kwargs)
    return result["idx"] if result else None


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
    return {"poc": poc_price, "vah": bin_edges[max(included) + 1], "val": bin_edges[min(included)]}


def _ca_volume_profile_signals(df, vp, lookback=20):
    signals = []
    recent = df.tail(min(lookback, len(df)))
    closes, highs, lows = recent["close"].values, recent["high"].values, recent["low"].values
    if abs(closes[-1] - vp["poc"]) / vp["poc"] * 100 <= 1.0 and (
        (np.any(closes[:-1] < vp["poc"]) and closes[-1] > vp["poc"]) or
        (np.any(closes[:-1] > vp["poc"]) and closes[-1] < vp["poc"])
    ):
        signals.append({"type": "POC Break & Retest", "price": vp["poc"], "note": "price broke POC and is retesting — classic continuation/reversal zone"})
    if np.max(highs) >= vp["vah"] * 0.998 and closes[-1] < vp["vah"]:
        signals.append({"type": "VAH Rejection", "price": vp["vah"], "note": "price hit Value Area High and was rejected — potential short"})
    if np.min(lows) <= vp["val"] * 1.002 and closes[-1] > vp["val"]:
        signals.append({"type": "VAL Rejection", "price": vp["val"], "note": "price hit Value Area Low and bounced — potential long"})
    return signals


def _detect_candlestick_patterns(df, n=5):
    """Detect candlestick patterns in the last n candles."""
    patterns = []
    tail = df.tail(n + 1)
    for i in range(1, len(tail)):
        row  = tail.iloc[i]
        prev = tail.iloc[i - 1]
        o, h, l, c     = row["open"],  row["high"],  row["low"],  row["close"]
        po, ph, pl, pc  = prev["open"], prev["high"], prev["low"], prev["close"]
        total = h - l
        if total == 0:
            continue
        body       = abs(c - o)
        body_ratio = body / total
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        is_bull    = c >= o
        prev_body  = abs(pc - po)
        prev_bull  = pc >= po
        ts_str     = str(tail.index[i])[:16]
        found = []
        if body_ratio < 0.08:
            found.append("Doji — indecision, watch for breakout direction")
        elif lower_wick >= body * 2.5 and upper_wick <= body * 0.5:
            found.append("Hammer — strong rejection of lows, potential bullish reversal")
        elif upper_wick >= body * 2.5 and lower_wick <= body * 0.5 and is_bull:
            found.append("Shooting Star — strong rejection of highs, potential bearish reversal")
        elif upper_wick >= body * 2.5 and lower_wick <= body * 0.5 and not is_bull:
            found.append("Inverted Hammer — watch for bullish follow-through confirmation")
        elif body_ratio > 0.9:
            found.append(f"{'Bullish' if is_bull else 'Bearish'} Marubozu — strong momentum, minimal rejection")
        if is_bull and not prev_bull and body > prev_body * 1.1:
            found.append("Bullish Engulfing — buyers overwhelmed sellers, momentum shift up")
        elif not is_bull and prev_bull and body > prev_body * 1.1:
            found.append("Bearish Engulfing — sellers overwhelmed buyers, momentum shift down")
        if h <= ph and l >= pl:
            found.append("Inside Bar — consolidation inside prior candle, breakout imminent")
        for note in found:
            patterns.append(f"  {ts_str}: {note}")
    return patterns


def _detect_equal_highs_lows(df, tolerance_pct=0.1):
    """Find equal highs/lows — liquidity pools where retail stops cluster."""
    swing_highs, swing_lows = _ca_find_swing_points_indexed(df)
    recent_highs = [p for _, p in swing_highs[-25:]]
    recent_lows  = [p for _, p in swing_lows[-25:]]

    def cluster_prices(prices):
        if not prices:
            return []
        used = [False] * len(prices)
        clusters = []
        for i, pi in enumerate(prices):
            if used[i]:
                continue
            group = [pi]
            used[i] = True
            for j, pj in enumerate(prices):
                if not used[j] and abs(pi - pj) / pi * 100 <= tolerance_pct:
                    group.append(pj)
                    used[j] = True
            if len(group) >= 2:
                clusters.append((round(sum(group) / len(group), 8), len(group)))
        return sorted(clusters, key=lambda x: -x[1])

    return cluster_prices(recent_highs), cluster_prices(recent_lows)


def _get_previous_day_hl(symbol, asset_type):
    """Fetch yesterday's high, low, close."""
    try:
        df = fetch_candles(symbol, asset_type, "1d", limit=5)
        if len(df) < 2:
            return None
        yd = df.iloc[-2]
        return {"pdh": float(yd["high"]), "pdl": float(yd["low"]), "pdc": float(yd["close"])}
    except Exception:
        return None


def _get_session_context():
    """Identify current trading session and volatility expectation."""
    now = datetime.now(timezone.utc)
    h   = now.hour
    sessions = []
    if 0 <= h < 8:
        sessions.append("Asian (00:00-08:00 UTC) — low volatility, range-bound, watch for fakeouts")
    if 8 <= h < 16:
        sessions.append("London (08:00-16:00 UTC) — high volatility, trends form here")
    if 13 <= h < 21:
        sessions.append("New York (13:00-21:00 UTC) — second high-volatility window")
    if 13 <= h < 16:
        sessions.append("⚡ London/NY Overlap (13:00-16:00 UTC) — PEAK VOLUME, strongest and most reliable moves of the day")
    if 8 <= h < 9:
        sessions.append("🔔 London Open — expect stop hunt of Asian range before real direction sets")
    elif 13 <= h < 14:
        sessions.append("🔔 NY Open — second major stop hunt / volatility spike")
    if 21 <= h or h < 0:
        sessions.append("Dead zone (21:00-00:00 UTC) — avoid new positions, very low liquidity")
    if not sessions:
        sessions.append("Pre-market / transition period")
    return {"lines": sessions, "utc": now.strftime("%H:%M UTC")}


def _build_rich_market_brief(symbol, asset_type, primary_tf="1h"):
    """
    Market brief optimised for one primary TF.
    primary_tf gets full 500-candle raw dump. All other TFs get rich text summaries (no raw rows).
    Token usage: ~15-25K vs old 40-60K.
    """
    try:
        TF_HIERARCHY = ["5m", "15m", "30m", "1h", "4h", "1d"]
        all_tfs = ["5m", "15m", "30m", "1h", "4h", "1d"]
        sr_tfs  = ["15m", "30m", "1h", "4h"]
        min_touches_map = ({"15m": 5, "30m": 3, "1h": 2, "4h": 2} if asset_type == "crypto"
                           else {"30m": 4, "1h": 2, "4h": 2})
        tf_fetch_limits = {"5m": 500, "15m": 500, "30m": 500, "1h": 500, "4h": 500, "1d": 365}

        # Full candle dumps: primary TF + next 2 in hierarchy so Chev sees the bigger picture himself
        try:
            _hi = TF_HIERARCHY.index(primary_tf)
        except ValueError:
            _hi = TF_HIERARCHY.index("1h")
        full_data_tfs    = TF_HIERARCHY[_hi : _hi + 3]           # e.g. ["1h","4h","1d"]
        full_data_limits = dict(zip(full_data_tfs, [500, 250, 150]))

        # ── Fetch all TFs in parallel ──────────────────────────────────
        tf_data = {}
        def _fetch_tf(tf):
            df = fetch_candles(symbol, asset_type, tf, limit=tf_fetch_limits.get(tf, 500))
            return tf, _ca_add_indicators(df)

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_fetch_tf, tf): tf for tf in all_tfs}
            for fut in as_completed(futs):
                try:
                    tf, df = fut.result()
                    tf_data[tf] = df
                except Exception as e:
                    print(f"[brief] {symbol} {futs[fut]} skipped: {e}")

        primary_df = tf_data.get(primary_tf) or tf_data.get("1h")
        if primary_df is None or len(primary_df) < 30:
            return f"[Market brief unavailable for {symbol} — {primary_tf} candle fetch failed]"

        # ── S/R from the 4 senior TFs ─────────────────────────────────────
        resistance_touches_by_tf, support_touches_by_tf = {}, {}
        for tf in sr_tfs:
            df = tf_data.get(tf)
            if df is None:
                continue
            touches = _ca_get_timed_touches(df)
            resistance_touches_by_tf[tf] = [(ts, p) for ts, p, k in touches if k == "resistance"]
            support_touches_by_tf[tf]    = [(ts, p) for ts, p, k in touches if k == "support"]

        current_price     = primary_df["close"].iloc[-1]
        resistance_levels = _ca_build_validated_levels(resistance_touches_by_tf, "resistance", min_touches_map=min_touches_map)
        support_levels    = _ca_build_validated_levels(support_touches_by_tf,    "support",    min_touches_map=min_touches_map)
        res_zones  = [z for z in resistance_levels if z["price"] > current_price]
        sup_zones  = [z for z in support_levels    if z["price"] < current_price]
        tier_b_res = [z for z in _ca_build_single_tf_levels(resistance_touches_by_tf.get("1h", [])) if z["price"] > current_price]
        tier_b_sup = [z for z in _ca_build_single_tf_levels(support_touches_by_tf.get("1h", []))    if z["price"] < current_price]
        top_res = _ca_pick_nearest_level(current_price, res_zones, tier_b_res)
        top_sup = _ca_pick_nearest_level(current_price, sup_zones, tier_b_sup)

        # ── 1H primary stats ──────────────────────────────────────────────
        vwap      = primary_df["VWAP"].iloc[-1]
        ema20     = primary_df["EMA20"].iloc[-1]
        rsi_1h    = primary_df["RSI"].iloc[-1]
        atr_1h    = primary_df["ATR"].iloc[-1]
        bb_upper  = primary_df["BB_upper"].iloc[-1]
        bb_mid    = primary_df["BB_mid"].iloc[-1]
        bb_lower  = primary_df["BB_lower"].iloc[-1]
        bb_width  = round((bb_upper - bb_lower) / bb_mid * 100, 2) if bb_mid else 0
        _bb_pct_to_upper = round((bb_upper - current_price) / current_price * 100, 2)
        _bb_pct_to_lower = round((current_price - bb_lower) / current_price * 100, 2)
        _bb_pct_to_mid   = round(abs(current_price - bb_mid) / current_price * 100, 2)
        vol_trend = _ca_volume_trend(primary_df)
        fib_levels, fib_direction = _ca_fib_from_real_impulse(primary_df)
        divergences = _ca_detect_rsi_divergence(primary_df) + _ca_detect_rsi_divergence_forming(primary_df)

        df_4h  = tf_data.get("4h")
        rsi_4h = df_4h["RSI"].iloc[-1] if df_4h is not None else None

        _bb_range = bb_upper - bb_lower
        _pct_b_brief = (current_price - bb_lower) / _bb_range if _bb_range > 0 else 0.5
        if current_price > bb_upper:
            _overshoot = round((_pct_b_brief - 1.0) * 100, 1)
            bb_pos = f"BURST ABOVE upper band (%B={_pct_b_brief:.2f}, {_overshoot}% of band outside) — STRONG mean-reversion signal"
        elif current_price < bb_lower:
            _overshoot = round(abs(_pct_b_brief) * 100, 1)
            bb_pos = f"BURST BELOW lower band (%B={_pct_b_brief:.2f}, {_overshoot}% of band outside) — STRONG mean-reversion signal"
        elif current_price > bb_mid:
            bb_pos = f"between mid and upper — %B={_pct_b_brief:.2f}  ({_bb_pct_to_upper:.2f}% to upper  |  {_bb_pct_to_mid:.2f}% to mid)"
        else:
            bb_pos = f"between mid and lower — %B={_pct_b_brief:.2f}  ({_bb_pct_to_lower:.2f}% to lower  |  {_bb_pct_to_mid:.2f}% to mid)"
        bb_squeeze = "YES ⚡ BIG MOVE INCOMING" if bb_width < 1.5 else ("MODERATE" if bb_width < 3.0 else "no")

        # Anchor-based VP on 4H and 1H only — range starts at the structural swing that
        # launched the current move (same first-anchor logic as Arsenal/auction anchor).
        # Lower TFs excluded: 15m/30m VP ranges are too short and noisy to be meaningful.
        vp_4h, vp_sigs_4h, vp_4h_label = None, [], ""
        vp_1h, vp_sigs_1h, vp_1h_label = None, [], ""
        try:
            _df4 = tf_data.get("4h")
            if _df4 is not None and len(_df4) >= 30:
                _anc4 = _detect_auction_anchor(_df4)
                if _anc4:
                    vp_4h = _ca_volume_profile(_df4, _anc4["idx"], len(_df4) - 1)
                    if vp_4h:
                        vp_sigs_4h  = _ca_volume_profile_signals(_df4, vp_4h)
                        _n4         = len(_df4) - _anc4["idx"]
                        _dt4        = _df4.index[_anc4["idx"]].strftime("%Y-%m-%d")
                        vp_4h_label = f"4H — anchor {_dt4}  ({_n4} candles, {_anc4['method']}, conf {_anc4['confidence']}%)"
        except Exception:
            pass
        try:
            _df1 = tf_data.get("1h") if tf_data.get("1h") is not None else primary_df
            if _df1 is not None and len(_df1) >= 30:
                _anc1 = _detect_auction_anchor(_df1)
                if _anc1:
                    vp_1h = _ca_volume_profile(_df1, _anc1["idx"], len(_df1) - 1)
                    if vp_1h:
                        vp_sigs_1h  = _ca_volume_profile_signals(_df1, vp_1h)
                        _n1         = len(_df1) - _anc1["idx"]
                        _dt1        = _df1.index[_anc1["idx"]].strftime("%Y-%m-%d")
                        vp_1h_label = f"1H — anchor {_dt1}  ({_n1} candles, {_anc1['method']}, conf {_anc1['confidence']}%)"
        except Exception:
            pass

        session = _get_session_context()
        pdhl    = _get_previous_day_hl(symbol, asset_type)

        # ── Assemble brief ────────────────────────────────────────────────
        lines = [f"=== MARKET BRIEF: {symbol} ({asset_type.upper()}) ===\n"]
        lines.append(f"Time: {session['utc']}  |  Session: {' | '.join(session['lines'])}")

        if pdhl:
            lines += [
                "\n--- Previous Day ---",
                f"  PDH: {pdhl['pdh']:.5f}  |  PDL: {pdhl['pdl']:.5f}  |  PDC: {pdhl['pdc']:.5f}",
            ]

        lines += [
            "\n--- Price Snapshot (1H reference) ---",
            f"  Price : {current_price:.5f}",
            f"  VWAP  : {vwap:.5f}",
            f"  EMA20 : {ema20:.5f}",
            f"  RSI 1h: {rsi_1h:.1f}" + (f"  |  RSI 4h: {rsi_4h:.1f}" if rsi_4h else ""),
            f"  ATR 1h: {atr_1h:.5f}  (avg candle range — SL needs at least 1 ATR breathing room beyond structure)",
            f"  Volume: {vol_trend} (last 10 candles on 1H)",
            f"  BB (20,2) 1H: upper={bb_upper:.5f}  mid={bb_mid:.5f}  lower={bb_lower:.5f}  width={bb_width}%",
            f"  BB position : {bb_pos}",
            f"  BB squeeze  : {bb_squeeze}",
        ]

        lines.append(f"\n--- Fibonacci ({fib_direction}) ---")
        for name, price in fib_levels.items():
            lines.append(f"  {name}: {price:.5f}")

        lines.append("\n--- Support / Resistance (validated across 15m/30m/1h/4h) ---")
        if top_res:
            lines.append(f"  RESISTANCE: {top_res['price']:.5f}  [{top_res['label']}]  ({abs(top_res['price']-current_price)/current_price*100:.2f}% away)")
        else:
            lines.append("  RESISTANCE: none found nearby")
        if top_sup:
            lines.append(f"  SUPPORT   : {top_sup['price']:.5f}  [{top_sup['label']}]  ({abs(top_sup['price']-current_price)/current_price*100:.2f}% away)")
        else:
            lines.append("  SUPPORT   : none found nearby")
        if len(res_zones) > 1:
            lines.append("  All resistance: " + ", ".join(f"{z['price']:.5f}({z['instances']}x)" for z in res_zones[:6]))
        if len(sup_zones) > 1:
            lines.append("  All support   : " + ", ".join(f"{z['price']:.5f}({z['instances']}x)" for z in sup_zones[:6]))

        if vp_4h or vp_1h:
            lines.append("\n--- Volume Profile (anchor-based: 4H + 1H) ---")
            lines.append("  Range starts at the first structural anchor of each TF (same logic as Arsenal).")
            for _vp, _lbl, _sigs in [(vp_4h, vp_4h_label, vp_sigs_4h), (vp_1h, vp_1h_label, vp_sigs_1h)]:
                if not _vp:
                    continue
                _poc_d = round(abs(current_price - _vp['poc']) / current_price * 100, 2)
                _vah_d = round(abs(current_price - _vp['vah']) / current_price * 100, 2)
                _val_d = round(abs(current_price - _vp['val']) / current_price * 100, 2)
                lines += [
                    f"  [{_lbl}]",
                    f"    POC: {_vp['poc']:.5f}  ({_poc_d:.2f}% away) — highest-volume price, acts as magnet",
                    f"    VAH: {_vp['vah']:.5f}  ({_vah_d:.2f}% away) — top of 70% value area (resistance above)",
                    f"    VAL: {_vp['val']:.5f}  ({_val_d:.2f}% away) — bottom of 70% value area (support below)",
                ]
                for sig in _sigs:
                    lines.append(f"    SIGNAL: {sig['type']} at {sig['price']:.5f} — {sig['note']}")

        # ── Active auction anchor ──────────────────────────────────────────
        try:
            anchor_info = _detect_auction_anchor(primary_df)
            if anchor_info:
                anchor_dt   = primary_df.index[anchor_info["idx"]]
                candles_ago = len(primary_df) - 1 - anchor_info["idx"]
                status = (
                    f"STALE — {anchor_info.get('invalidation_reason','').replace('_',' ')}"
                    if not anchor_info.get("active", True) else
                    "ACTIVE + CONFIRMED" if anchor_info.get("confirmed", False) else
                    "ACTIVE (unconfirmed)"
                )
                lines += [
                    "\n--- Active Auction Anchor ---",
                    f"  Price  : {anchor_info['price']:.5f}  [{anchor_info['anchor_type'].replace('_',' ')}]",
                    f"  Candle : {anchor_dt.strftime('%Y-%m-%d %H:%M')} ({candles_ago} bars ago)",
                    f"  Method : {anchor_info['method']}  |  Confidence: {anchor_info['confidence']}%",
                    f"  Status : {status}",
                    f"  Note   : VP, Fib, and VWAP should reference this as the auction origin. SR near this price is structural.",
                ]
        except Exception:
            pass

        if divergences:
            lines.append("\n--- RSI Divergence ---")
            for d in divergences:
                lines.append(f"  {d['type']} at {d['price']:.5f} — {d['note']}")

        # ── Order book + futures ──────────────────────────────────────────
        if asset_type == "crypto":
            agg = _aggregate_orderbooks(symbol)
            if agg:
                ex_names = ', '.join(ob['exchange'] for ob in agg['exchanges'])
                lines.append(f"\n--- Order Book Liquidity Map ({ex_names}, ±10% range) ---")
                lines.append(f"  Mid price: {agg['mid_price']:.5f}  |  Bias: {agg['avg_imbalance']:+.3f} — {agg['overall']}")
                lines.append(f"  Per-exchange imbalance:")
                for ob in agg["exchanges"]:
                    imb = "more bids" if ob["imbalance"] > 0.1 else "more asks" if ob["imbalance"] < -0.1 else "balanced"
                    lines.append(f"    {ob['exchange']:20s} {ob['imbalance']:+.3f} ({imb})")

                lines.append("  ASKS price|dist%|qty|tag (closest first):")
                for p, q, label in agg["ask_clusters"][:30]:
                    dist = (p - agg["mid_price"]) / agg["mid_price"] * 100
                    lines.append(f"  {p:.5f}|+{dist:.2f}%|{q:.3f}{label}")
                if agg["ask_air_pockets"]:
                    lines.append("  ASK AIR POCKETS (thin — price moves fast here):")
                    for p, dist in agg["ask_air_pockets"]:
                        lines.append(f"  {p:.5f}|{dist:.2f}%")

                lines.append("  BIDS price|dist%|qty|tag (closest first):")
                for p, q, label in agg["bid_clusters"][:30]:
                    dist = (agg["mid_price"] - p) / agg["mid_price"] * 100
                    lines.append(f"  {p:.5f}|-{dist:.2f}%|{q:.3f}{label}")
                if agg["bid_air_pockets"]:
                    lines.append("  BID AIR POCKETS (thin — price drops fast here):")
                    for p, dist in agg["bid_air_pockets"]:
                        lines.append(f"  {p:.5f}|{dist:.2f}%")

                if agg["multi_exchange_bid_walls"]:
                    lines.append("  *** MULTI-EXCHANGE BID WALLS (same wall seen on 2+ exchanges — institutional) ***")
                    for w in agg["multi_exchange_bid_walls"]:
                        lines.append(f"      {w['price']:.5f}  on: {', '.join(w['exchanges'])}")
                if agg["multi_exchange_ask_walls"]:
                    lines.append("  *** MULTI-EXCHANGE ASK WALLS (same wall seen on 2+ exchanges — institutional) ***")
                    for w in agg["multi_exchange_ask_walls"]:
                        lines.append(f"      {w['price']:.5f}  on: {', '.join(w['exchanges'])}")

            futures_data = _fetch_binance_futures_data(symbol)
            if futures_data:
                fr = futures_data["funding_rate"] * 100
                oi = futures_data["open_interest"]
                fr_label = ("crowded longs — squeeze risk for buyers" if fr > 0.05
                            else "crowded shorts — squeeze risk for sellers" if fr < -0.05
                            else "neutral")
                lines += [
                    "\n--- Futures (Binance perpetual) ---",
                    f"  Open Interest : {oi:,.2f} contracts",
                    f"  Funding Rate  : {fr:+.4f}%  ({fr_label})",
                ]

        # ── Context summaries for non-primary TFs (no raw candles) ──────────
        lines.append(f"\n\n=== HIGHER / LOWER TIMEFRAME CONTEXT ===")
        lines.append("(Rich summaries — no raw rows. Use these to understand the bigger picture.)")

        for tf in all_tfs:
            if tf in full_data_tfs:
                continue  # these get full candle dumps below, not summaries
            df = tf_data.get(tf)
            if df is None:
                lines.append(f"\n{tf.upper()} — not available")
                continue
            tf_fib_levels, tf_fib_dir = _ca_fib_from_real_impulse(df)
            lines.append(_build_tf_context_summary(
                tf, df, current_price,
                res_zones, sup_zones,
                tf_fib_levels, tf_fib_dir,
                asset_type, symbol,
            ))

        # ── Full candle data: primary TF + next 2 in hierarchy ───────────────────
        for _i, _ftf in enumerate(full_data_tfs):
            _lim  = full_data_limits[_ftf]
            _role = "PRIMARY TIMEFRAME" if _i == 0 else f"HIGHER TIMEFRAME +{_i}"
            lines.append(f"\n\n=== {_role}: {_ftf.upper()} — FULL CANDLE DATA (last {_lim} candles) ===")
            if _i == 0:
                lines.append("This is your primary chart. All indicators pre-computed per candle.")
            else:
                lines.append("Higher timeframe context — compile your own structure, swings, and key levels from this data.")
            lines.append(f"Trade type for this TF: {TF_TRADE_TYPE.get(_ftf, 'day')}")
            df = tf_data.get(_ftf)
            if df is None:
                lines.append(f"  [{_ftf.upper()} data unavailable]")
                continue
            df_out = df.iloc[-_lim:]
            has_mh = "MACD_hist" in df_out.columns
            lines.append(f"Candles {_ftf.upper()} ({len(df_out)}) time|O|H|L|C|V|RSI|E13|E21|E55|Mh")
            for ts, row in df_out.iterrows():
                r_str = f"{row['RSI']:.1f}"       if "RSI"       in df_out.columns and not pd.isna(row["RSI"])       else "-"
                e13   = f"{row['EMA13']:.5f}"      if "EMA13"     in df_out.columns and not pd.isna(row["EMA13"])     else "-"
                e21   = f"{row['EMA21']:.5f}"      if "EMA21"     in df_out.columns and not pd.isna(row["EMA21"])     else "-"
                e55   = f"{row['EMA55']:.5f}"      if "EMA55"     in df_out.columns and not pd.isna(row["EMA55"])     else "-"
                m_str = f"{row['MACD_hist']:+.5g}" if has_mh      and not pd.isna(row["MACD_hist"])                  else "-"
                lines.append(
                    f"{str(ts)[5:16]}|{row['open']:.5f}|{row['high']:.5f}|"
                    f"{row['low']:.5f}|{row['close']:.5f}|{row['volume']:.0f}|{r_str}|{e13}|{e21}|{e55}|{m_str}"
                )
            # Patterns and liquidity only on primary TF (keep token budget in check)
            if _i == 0:
                patterns = _detect_candlestick_patterns(df_out, n=5)
                if patterns:
                    lines.append("\nPatterns (last 5 candles on primary TF):")
                    lines += patterns
                eq_highs, eq_lows = _detect_equal_highs_lows(df_out)
                if eq_highs:
                    lines.append("Equal Highs (liquidity above):")
                    for price, count in eq_highs[:5]:
                        lines.append(f"  {price:.5f}  ({count} touches)")
                if eq_lows:
                    lines.append("Equal Lows (liquidity below):")
                    for price, count in eq_lows[:5]:
                        lines.append(f"  {price:.5f}  ({count} touches)")
                sweeps = _detect_liquidity_sweep(df_out)
                for s in sweeps[:2]:
                    lines.append(f"SWEEP: {s['note']}")

        return "\n".join(lines)

    except Exception as e:
        return f"[Market brief error for {symbol}: {e}]"


# ============================================================
# NEW TF-SPLIT HELPERS
# ============================================================

def _detect_ema_crossover(df, fast="EMA13", slow="EMA21", lookback=6):
    """Return the most recent EMA13/EMA21 crossover within lookback candles, or None."""
    if fast not in df.columns or slow not in df.columns or len(df) < lookback + 1:
        return None
    tail = df[[fast, slow]].tail(lookback + 1).dropna()
    if len(tail) < 2:
        return None
    for i in range(len(tail) - 1, 0, -1):
        cf, cs = tail[fast].iloc[i],   tail[slow].iloc[i]
        pf, ps = tail[fast].iloc[i-1], tail[slow].iloc[i-1]
        if pf <= ps and cf > cs:
            return {"type": "bullish", "candles_ago": len(tail) - 1 - i}
        if pf >= ps and cf < cs:
            return {"type": "bearish", "candles_ago": len(tail) - 1 - i}
    return None


def _detect_hidden_divergence(df, lookback=50):
    """
    Hidden bullish : price makes higher low  but RSI makes lower low  → uptrend continuation.
    Hidden bearish : price makes lower high  but RSI makes higher high → downtrend continuation.
    Returns list of dicts with type, price, note.
    """
    if "RSI" not in df.columns or len(df) < 20:
        return []
    results = []
    window = df.tail(lookback).dropna(subset=["RSI"])
    prices_low  = window["low"].values
    prices_high = window["high"].values
    rsi         = window["RSI"].values
    closes      = window["close"].values

    for i in range(5, len(window) - 1):
        # Find previous significant low / high in first half of window
        prev_half_low_idx  = prices_low[:i].argmin()
        prev_half_high_idx = prices_high[:i].argmax()

        # Hidden bullish: current low > prev low, but RSI now < prev RSI
        if prices_low[i] > prices_low[prev_half_low_idx] and rsi[i] < rsi[prev_half_low_idx]:
            results.append({
                "type": "hidden_bullish_divergence",
                "price": float(closes[i]),
                "note": f"Price HL at {closes[i]:.5f} but RSI lower — uptrend continuation signal",
            })
            break

        # Hidden bearish: current high < prev high, but RSI now > prev RSI
        if prices_high[i] < prices_high[prev_half_high_idx] and rsi[i] > rsi[prev_half_high_idx]:
            results.append({
                "type": "hidden_bearish_divergence",
                "price": float(closes[i]),
                "note": f"Price LH at {closes[i]:.5f} but RSI higher — downtrend continuation signal",
            })
            break

    return results


def _detect_liquidity_sweep(df, tolerance_pct=0.12, lookback=40):
    """
    Detect stop-hunt sweeps: a wick that pierces through a cluster of equal highs/lows
    then closes back inside. Returns list of recent sweeps.
    """
    if len(df) < 10:
        return []
    sweeps = []
    tail = df.tail(lookback)
    eq_highs, eq_lows = _detect_equal_highs_lows(df)

    for level, count in eq_highs:
        # Look for a candle whose HIGH wicked above the level but CLOSE is below it
        for i in range(len(tail) - 5, len(tail)):
            row = tail.iloc[i]
            if row["high"] > level * (1 + tolerance_pct / 100) and row["close"] < level:
                sweeps.append({
                    "type": "bearish_sweep",
                    "level": round(level, 6),
                    "note": f"Wick swept equal highs at {level:.5f} ({count}x) then closed below — retail longs stopped out",
                })
                break

    for level, count in eq_lows:
        # Look for a candle whose LOW wicked below the level but CLOSE is above it
        for i in range(len(tail) - 5, len(tail)):
            row = tail.iloc[i]
            if row["low"] < level * (1 - tolerance_pct / 100) and row["close"] > level:
                sweeps.append({
                    "type": "bullish_sweep",
                    "level": round(level, 6),
                    "note": f"Wick swept equal lows at {level:.5f} ({count}x) then closed above — retail shorts stopped out",
                })
                break

    return sweeps


def _run_pattern_engine(df, window=3, lookback=100, breakout_pct=0.012):
    """
    Geometry Engine: reduces chart to swing structure, fits mathematical models,
    and scores each pattern against the geometry.

    Approach (following ChatGPT geometry primitive philosophy):
      1. Swing detection on actual high/low prices
      2. Trendline fitting → slopes, R², convergence/parallelism
      3. Geometry vector built from those primitives
      4. Each pattern scored against the geometry — no hard-coded pattern matching
      5. Returns sorted results with raw geometry exposed for Chev

    Patterns detected:
      Triangles: Symmetrical, Ascending, Descending
      Wedges: Rising, Falling
      Channels: Bull, Bear
      Rectangle / Range
      Flags: Bull, Bear (require prior impulse)
      Pennants: Bull, Bear (require prior impulse + convergence)
      Head & Shoulders (proper 5-point: SH-V-HD-V-SH)
      Inverse Head & Shoulders
      Double Top / Bottom (ATR-scaled tolerance + valley depth check)
      Triple Top / Bottom
    """
    if len(df) < 20:
        return []

    df = df.tail(lookback).copy().reset_index(drop=True)
    n = len(df)
    highs   = df["high"].values.astype(float)
    lows    = df["low"].values.astype(float)
    closes  = df["close"].values.astype(float)
    volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(n)
    atr_col = df["ATR"].values.astype(float) if "ATR" in df.columns else None
    atr     = float(np.nanmedian(atr_col)) if atr_col is not None and len(atr_col) > 0 else float(closes.mean() * 0.01)
    price   = float(closes[-1])

    # "flat" if slope magnitude < 15% of ATR per bar
    flat_thr = atr * 0.15

    # ── Pivot detection ───────────────────────────────────────────────────────
    confirm_w = min(6, window * 2)
    swing_highs, swing_lows = [], []
    for i in range(window, n - window):
        win_h = highs[max(0, i - window): i + window + 1]
        win_l = lows[max(0, i - window):  i + window + 1]
        if highs[i] >= win_h.max():
            future_lows = lows[i + 1: min(i + 1 + confirm_w, n)]
            if len(future_lows) and (highs[i] - future_lows.min()) / max(highs[i], 1e-10) >= 0.003:
                swing_highs.append(i)
        if lows[i] <= win_l.min():
            future_highs = highs[i + 1: min(i + 1 + confirm_w, n)]
            if len(future_highs) and (future_highs.max() - lows[i]) / max(lows[i], 1e-10) >= 0.003:
                swing_lows.append(i)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return []

    # ── Trendline fitting ─────────────────────────────────────────────────────
    def fit_tl(indices, values):
        x = np.array(indices, dtype=float)
        y = values[list(indices)]
        ref_x = x[0]
        xn = x - ref_x
        if np.std(xn) < 1e-10:
            return 0.0, ref_x, float(y.mean()), 1.0
        s, b = np.polyfit(xn, y, 1)
        y_pred = s * xn + b
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - y.mean()) ** 2)
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-10 else 1.0
        return float(s), float(ref_x), float(b), float(r2)

    def tl_val(slope, ref, intercept, x):
        return slope * (x - ref) + intercept

    k = min(5, max(2, min(len(swing_highs), len(swing_lows))))
    hi_idx = swing_highs[-k:]
    lo_idx = swing_lows[-k:]

    hi_slope, hi_ref, hi_int, hi_r2 = fit_tl(hi_idx, highs)
    lo_slope, lo_ref, lo_int, lo_r2 = fit_tl(lo_idx, lows)

    def slope_dir(s):
        return "rising" if s > flat_thr else ("falling" if s < -flat_thr else "flat")

    hd, ld = slope_dir(hi_slope), slope_dir(lo_slope)

    # ── Convergence ───────────────────────────────────────────────────────────
    x_first = float(min(hi_idx[0], lo_idx[0]))
    x_last  = float(n - 1)
    gap_start = tl_val(hi_slope, hi_ref, hi_int, x_first) - tl_val(lo_slope, lo_ref, lo_int, x_first)
    gap_end   = tl_val(hi_slope, hi_ref, hi_int, x_last)  - tl_val(lo_slope, lo_ref, lo_int, x_last)
    is_converging = gap_start > 0 and gap_end > 0 and gap_end < gap_start * 0.85
    conv_pct  = max(0.0, 1.0 - gap_end / gap_start) if gap_start > 1e-10 else 0.0
    gap_ratio = abs(gap_end / gap_start) if gap_start > 1e-10 else 1.0
    is_parallel = 0.75 <= gap_ratio <= 1.33 and not is_converging

    slope_diff  = abs(hi_slope - lo_slope)
    slope_avg   = (abs(hi_slope) + abs(lo_slope)) / 2 if (abs(hi_slope) + abs(lo_slope)) > 0 else 1.0
    parallelism = 1.0 - min(1.0, slope_diff / (slope_avg + 1e-10))

    upper_now   = tl_val(hi_slope, hi_ref, hi_int, x_last)
    lower_now   = tl_val(lo_slope, lo_ref, lo_int, x_last)
    breakout_up = price > upper_now * (1 + breakout_pct)
    breakout_dn = price < lower_now * (1 - breakout_pct)

    # ── Volume helpers ────────────────────────────────────────────────────────
    avg_vol = float(volumes.mean()) if volumes.mean() > 0 else 1.0

    def vol_at(idx, r=2):
        return float(volumes[max(0, idx - r): min(n, idx + r + 1)].mean())

    def vol_declining(left_idx, right_idx):
        return vol_at(right_idx) < vol_at(left_idx) * 0.88

    def vol_spike():
        return float(volumes[-3:].mean()) > avg_vol * 1.3

    def vol_contracting(start_idx, end_idx):
        seg = volumes[start_idx:end_idx]
        if len(seg) < 4:
            return False
        return seg[len(seg)//2:].mean() < seg[:len(seg)//2].mean() * 0.85

    # ── Impulse detection (flags / pennants require a prior strong move) ───────
    imp_bars = min(20, max(4, n // 5))
    pre_end  = max(0, n - imp_bars - 5)
    pre_start = max(0, pre_end - imp_bars)
    impulse_move = (closes[pre_end] - closes[pre_start]) / max(abs(closes[pre_start]), 1e-10) if pre_end > pre_start else 0.0
    impulse_atr_units = abs(impulse_move) * price / atr if atr > 0 else 0.0
    has_impulse = impulse_atr_units >= 4.0   # at least 4 ATRs of directional move
    impulse_up  = impulse_move > 0

    # ── Geometry vector (raw features — exposed in results for Chev) ──────────
    geometry = {
        "upper_slope_norm": round(hi_slope / max(price, 1e-10) * 1000, 4),
        "lower_slope_norm": round(lo_slope / max(price, 1e-10) * 1000, 4),
        "upper_r2":         round(hi_r2, 2),
        "lower_r2":         round(lo_r2, 2),
        "compression":      round(conv_pct, 2),          # 0=no compression, 1=fully converged
        "parallelism":      round(parallelism, 2),        # 1=perfectly parallel, 0=diverging
        "is_converging":    is_converging,
        "is_parallel":      is_parallel,
        "has_impulse":      has_impulse,
        "impulse_atr":      round(impulse_atr_units, 1),
        "breakout_up":      breakout_up,
        "breakout_dn":      breakout_dn,
        "vol_spike":        vol_spike(),
        "upper_line":       round(upper_now, 6),
        "lower_line":       round(lower_now, 6),
    }

    results = []
    min_r2   = 0.50

    # ─────────────────────────────────────────────────────────────────────────
    # TRIANGLES — require convergence
    # ─────────────────────────────────────────────────────────────────────────
    if hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_converging:
        conf_base = (hi_r2 + lo_r2) / 2 * (0.75 + 0.25 * conv_pct)

        if hd == "falling" and ld == "rising":
            sig  = "BUY" if breakout_up else ("SELL" if breakout_dn else "NEUTRAL")
            bias = "bullish" if breakout_up else ("bearish" if breakout_dn else "neutral")
            results.append({"name": "Symmetrical Triangle", "bias": bias, "signal": sig,
                            "confidence": conf_base, "breakout": breakout_up or breakout_dn,
                            "breakout_level": upper_now if breakout_up else lower_now,
                            "category": "continuation",
                            "volume_confirmed": vol_spike() if (breakout_up or breakout_dn) else False,
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6),
                                        "compression_pct": round(conv_pct * 100, 1)},
                            "geometry": geometry})

        elif hd == "flat" and ld == "rising":
            sig = "BUY" if breakout_up else "NEUTRAL"
            results.append({"name": "Ascending Triangle", "bias": "bullish", "signal": sig,
                            "confidence": conf_base, "breakout": breakout_up,
                            "breakout_level": upper_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_up else False,
                            "details": {"resistance": round(upper_now, 6), "rising_support": round(lower_now, 6),
                                        "compression_pct": round(conv_pct * 100, 1)},
                            "geometry": geometry})

        elif hd == "falling" and ld == "flat":
            sig = "SELL" if breakout_dn else "NEUTRAL"
            results.append({"name": "Descending Triangle", "bias": "bearish", "signal": sig,
                            "confidence": conf_base, "breakout": breakout_dn,
                            "breakout_level": lower_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_dn else False,
                            "details": {"falling_resistance": round(upper_now, 6), "support": round(lower_now, 6),
                                        "compression_pct": round(conv_pct * 100, 1)},
                            "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # WEDGES — same-direction slopes, converging
    # ─────────────────────────────────────────────────────────────────────────
    if hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_converging:
        conf_base = (hi_r2 + lo_r2) / 2 * (0.75 + 0.25 * conv_pct)

        if hd == "rising" and ld == "rising":
            sig = "SELL" if breakout_dn else "NEUTRAL"
            results.append({"name": "Rising Wedge", "bias": "bearish", "signal": sig,
                            "confidence": conf_base, "breakout": breakout_dn,
                            "breakout_level": lower_now, "category": "reversal",
                            "volume_confirmed": vol_spike() if breakout_dn else False,
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6)},
                            "geometry": geometry})

        elif hd == "falling" and ld == "falling":
            sig = "BUY" if breakout_up else "NEUTRAL"
            results.append({"name": "Falling Wedge", "bias": "bullish", "signal": sig,
                            "confidence": conf_base, "breakout": breakout_up,
                            "breakout_level": upper_now, "category": "reversal",
                            "volume_confirmed": vol_spike() if breakout_up else False,
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6)},
                            "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # CHANNELS — parallel same-direction slopes
    # ─────────────────────────────────────────────────────────────────────────
    if hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_parallel and parallelism >= 0.65:
        conf_ch = (hi_r2 + lo_r2) / 2 * parallelism
        mid_ch  = round((upper_now + lower_now) / 2, 6)

        if hd == "rising" and ld == "rising":
            sig = "BUY" if price <= mid_ch else "SELL"
            results.append({"name": "Bull Channel", "bias": "bullish", "signal": sig,
                            "confidence": conf_ch, "breakout": breakout_up or breakout_dn,
                            "breakout_level": lower_now if breakout_dn else upper_now,
                            "category": "continuation",
                            "volume_confirmed": vol_spike() if (breakout_up or breakout_dn) else False,
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6),
                                        "mid_channel": mid_ch},
                            "geometry": geometry})

        elif hd == "falling" and ld == "falling":
            sig = "SELL" if price >= mid_ch else "BUY"
            results.append({"name": "Bear Channel", "bias": "bearish", "signal": sig,
                            "confidence": conf_ch, "breakout": breakout_up or breakout_dn,
                            "breakout_level": upper_now if breakout_up else lower_now,
                            "category": "continuation",
                            "volume_confirmed": vol_spike() if (breakout_up or breakout_dn) else False,
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6),
                                        "mid_channel": mid_ch},
                            "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # RECTANGLE / RANGE — both slopes flat
    # ─────────────────────────────────────────────────────────────────────────
    if hd == "flat" and ld == "flat" and hi_r2 >= min_r2 and lo_r2 >= min_r2:
        sig  = "BUY" if breakout_up else ("SELL" if breakout_dn else "NEUTRAL")
        bias = "bullish" if breakout_up else ("bearish" if breakout_dn else "neutral")
        results.append({"name": "Rectangle / Range", "bias": bias, "signal": sig,
                        "confidence": (hi_r2 + lo_r2) / 2,
                        "breakout": breakout_up or breakout_dn,
                        "breakout_level": upper_now if breakout_up else lower_now,
                        "category": "continuation",
                        "volume_confirmed": vol_spike() if (breakout_up or breakout_dn) else False,
                        "details": {"resistance": round(upper_now, 6), "support": round(lower_now, 6)},
                        "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # FLAGS — prior impulse + small counter-trend parallel channel
    # ─────────────────────────────────────────────────────────────────────────
    if has_impulse and hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_parallel:
        conf_flag = (hi_r2 + lo_r2) / 2 * (0.65 + 0.35 * parallelism) * min(1.0, 0.7 + 0.3 * impulse_atr_units / 8.0)

        if impulse_up and hd == "falling" and ld == "falling":
            sig = "BUY" if breakout_up else "NEUTRAL"
            results.append({"name": "Bull Flag", "bias": "bullish", "signal": sig,
                            "confidence": conf_flag, "breakout": breakout_up,
                            "breakout_level": upper_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_up else vol_contracting(n // 2, n),
                            "details": {"flag_top": round(upper_now, 6), "flag_bottom": round(lower_now, 6),
                                        "impulse_atr": round(impulse_atr_units, 1)},
                            "geometry": geometry})

        elif not impulse_up and hd == "rising" and ld == "rising":
            sig = "SELL" if breakout_dn else "NEUTRAL"
            results.append({"name": "Bear Flag", "bias": "bearish", "signal": sig,
                            "confidence": conf_flag, "breakout": breakout_dn,
                            "breakout_level": lower_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_dn else vol_contracting(n // 2, n),
                            "details": {"flag_top": round(upper_now, 6), "flag_bottom": round(lower_now, 6),
                                        "impulse_atr": round(impulse_atr_units, 1)},
                            "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # PENNANTS — prior impulse + small converging triangle
    # ─────────────────────────────────────────────────────────────────────────
    if has_impulse and hi_r2 >= min_r2 and lo_r2 >= min_r2 and is_converging and hd == "falling" and ld == "rising":
        conf_penn = (hi_r2 + lo_r2) / 2 * (0.70 + 0.30 * conv_pct) * min(1.0, 0.7 + 0.3 * impulse_atr_units / 8.0)

        if impulse_up:
            sig = "BUY" if breakout_up else "NEUTRAL"
            results.append({"name": "Bull Pennant", "bias": "bullish", "signal": sig,
                            "confidence": conf_penn, "breakout": breakout_up,
                            "breakout_level": upper_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_up else vol_contracting(n // 2, n),
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6),
                                        "impulse_atr": round(impulse_atr_units, 1)},
                            "geometry": geometry})
        else:
            sig = "SELL" if breakout_dn else "NEUTRAL"
            results.append({"name": "Bear Pennant", "bias": "bearish", "signal": sig,
                            "confidence": conf_penn, "breakout": breakout_dn,
                            "breakout_level": lower_now, "category": "continuation",
                            "volume_confirmed": vol_spike() if breakout_dn else vol_contracting(n // 2, n),
                            "details": {"upper_line": round(upper_now, 6), "lower_line": round(lower_now, 6),
                                        "impulse_atr": round(impulse_atr_units, 1)},
                            "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # HEAD & SHOULDERS — proper 5-point: LS_peak, V1, Head_peak, V2, RS_peak
    # Old code used 3 consecutive swing highs with no valley check — this is
    # much more rigorous and matches how the pattern actually forms.
    # ─────────────────────────────────────────────────────────────────────────
    if len(swing_highs) >= 3 and len(swing_lows) >= 2:
        for _i in range(len(swing_highs) - 2):
            sh_l_i, hd_i, sh_r_i = swing_highs[_i], swing_highs[_i + 1], swing_highs[_i + 2]
            # Must have valleys between the three peaks
            v1_cands = [li for li in swing_lows if sh_l_i < li < hd_i]
            v2_cands = [li for li in swing_lows if hd_i  < li < sh_r_i]
            if not v1_cands or not v2_cands:
                continue
            v1_i = min(v1_cands, key=lambda li: lows[li])  # lowest valley between LS and head
            v2_i = min(v2_cands, key=lambda li: lows[li])  # lowest valley between head and RS
            sh_l, hd_p, sh_r = highs[sh_l_i], highs[hd_i], highs[sh_r_i]
            v1, v2 = lows[v1_i], lows[v2_i]
            # Head must be above both shoulders by at least 0.5%
            if hd_p <= max(sh_l, sh_r) * 1.005:
                continue
            # Shoulders must be within 6% of each other
            sh_sym = abs(sh_l - sh_r) / ((sh_l + sh_r) / 2)
            if sh_sym > 0.06:
                continue
            # Neckline through the two valleys
            nl_slope = (v2 - v1) / (v2_i - v1_i) if v2_i != v1_i else 0.0
            neckline  = v1 + nl_slope * (n - 1 - v1_i)
            brk_neck  = price < neckline * (1 - breakout_pct)
            vol_ok    = vol_declining(sh_l_i, sh_r_i)
            head_atr  = (hd_p - (sh_l + sh_r) / 2) / atr
            conf = min(0.85, 0.42 + 0.18 * (1 - sh_sym / 0.06) + 0.15 * min(1.0, head_atr / 3.0)
                       + (0.18 if brk_neck else 0) + (0.07 if vol_ok else 0))
            results.append({"name": "Head & Shoulders", "bias": "bearish",
                            "signal": "SELL" if brk_neck else "NEUTRAL",
                            "confidence": conf, "breakout": brk_neck,
                            "breakout_level": round(neckline, 6), "category": "reversal",
                            "volume_confirmed": vol_ok,
                            "details": {"left_shoulder": round(float(sh_l), 6),
                                        "head": round(float(hd_p), 6),
                                        "right_shoulder": round(float(sh_r), 6),
                                        "neckline": round(neckline, 6),
                                        "v1": round(float(v1), 6), "v2": round(float(v2), 6)},
                            "geometry": geometry})
            break  # use the most recent qualifying structure

    # ─────────────────────────────────────────────────────────────────────────
    # INVERSE HEAD & SHOULDERS — LS_trough, P1, Head_trough, P2, RS_trough
    # ─────────────────────────────────────────────────────────────────────────
    if len(swing_lows) >= 3 and len(swing_highs) >= 2:
        for _i in range(len(swing_lows) - 2):
            sh_l_i, hd_i, sh_r_i = swing_lows[_i], swing_lows[_i + 1], swing_lows[_i + 2]
            p1_cands = [hi for hi in swing_highs if sh_l_i < hi < hd_i]
            p2_cands = [hi for hi in swing_highs if hd_i  < hi < sh_r_i]
            if not p1_cands or not p2_cands:
                continue
            p1_i = max(p1_cands, key=lambda hi: highs[hi])
            p2_i = max(p2_cands, key=lambda hi: highs[hi])
            sh_l, hd_p, sh_r = lows[sh_l_i], lows[hd_i], lows[sh_r_i]
            p1, p2 = highs[p1_i], highs[p2_i]
            if hd_p >= min(sh_l, sh_r) * 0.995:
                continue
            sh_sym = abs(sh_l - sh_r) / ((sh_l + sh_r) / 2)
            if sh_sym > 0.06:
                continue
            nl_slope  = (p2 - p1) / (p2_i - p1_i) if p2_i != p1_i else 0.0
            neckline  = p1 + nl_slope * (n - 1 - p1_i)
            brk_neck  = price > neckline * (1 + breakout_pct)
            vol_ok    = vol_declining(sh_l_i, sh_r_i)
            head_atr  = ((sh_l + sh_r) / 2 - hd_p) / atr
            conf = min(0.85, 0.42 + 0.18 * (1 - sh_sym / 0.06) + 0.15 * min(1.0, head_atr / 3.0)
                       + (0.18 if brk_neck else 0) + (0.07 if vol_ok else 0))
            results.append({"name": "Inverse Head & Shoulders", "bias": "bullish",
                            "signal": "BUY" if brk_neck else "NEUTRAL",
                            "confidence": conf, "breakout": brk_neck,
                            "breakout_level": round(neckline, 6), "category": "reversal",
                            "volume_confirmed": vol_ok,
                            "details": {"left_shoulder": round(float(sh_l), 6),
                                        "head": round(float(hd_p), 6),
                                        "right_shoulder": round(float(sh_r), 6),
                                        "neckline": round(neckline, 6),
                                        "p1": round(float(p1), 6), "p2": round(float(p2), 6)},
                            "geometry": geometry})
            break

    # ─────────────────────────────────────────────────────────────────────────
    # DOUBLE TOP / BOTTOM — ATR-scaled tolerance, valley/peak depth check
    # ─────────────────────────────────────────────────────────────────────────
    atr_tol = min(0.025, atr / price * 2.5)  # tolerance scales with ATR, capped at 2.5%
    min_sep  = 8

    if len(swing_highs) >= 2:
        t1, t2 = swing_highs[-2], swing_highs[-1]
        p1, p2 = highs[t1], highs[t2]
        if abs(p1 - p2) / max(p1, 1e-10) < atr_tol and (t2 - t1) >= min_sep:
            valley_low  = lows[t1:t2 + 1].min() if t2 > t1 else price
            valley_depth = (min(p1, p2) - valley_low) / atr
            if valley_depth >= 1.0:
                neckline  = float(valley_low)
                brk_neck  = price < neckline * (1 - breakout_pct)
                vol_ok    = vol_declining(t1, t2)
                conf = min(0.88, 0.42 + 0.20 * min(1.0, valley_depth / 3.0)
                           + (0.22 if brk_neck else 0) + (0.08 if vol_ok else 0))
                results.append({"name": "Double Top", "bias": "bearish",
                                "signal": "SELL" if brk_neck else "NEUTRAL",
                                "confidence": conf, "breakout": brk_neck,
                                "breakout_level": neckline, "category": "reversal",
                                "volume_confirmed": vol_ok,
                                "details": {"top1": round(float(p1), 6), "top2": round(float(p2), 6),
                                            "neckline": round(neckline, 6),
                                            "valley_depth_atr": round(valley_depth, 1)},
                                "geometry": geometry})

    if len(swing_lows) >= 2:
        b1, b2 = swing_lows[-2], swing_lows[-1]
        p1, p2 = lows[b1], lows[b2]
        if abs(p1 - p2) / max(p1, 1e-10) < atr_tol and (b2 - b1) >= min_sep:
            peak_high   = highs[b1:b2 + 1].max() if b2 > b1 else price
            peak_height = (peak_high - max(p1, p2)) / atr
            if peak_height >= 1.0:
                neckline  = float(peak_high)
                brk_neck  = price > neckline * (1 + breakout_pct)
                vol_ok    = vol_declining(b1, b2)
                conf = min(0.88, 0.42 + 0.20 * min(1.0, peak_height / 3.0)
                           + (0.22 if brk_neck else 0) + (0.08 if vol_ok else 0))
                results.append({"name": "Double Bottom", "bias": "bullish",
                                "signal": "BUY" if brk_neck else "NEUTRAL",
                                "confidence": conf, "breakout": brk_neck,
                                "breakout_level": neckline, "category": "reversal",
                                "volume_confirmed": vol_ok,
                                "details": {"bottom1": round(float(p1), 6), "bottom2": round(float(p2), 6),
                                            "neckline": round(neckline, 6),
                                            "peak_height_atr": round(peak_height, 1)},
                                "geometry": geometry})

    # ─────────────────────────────────────────────────────────────────────────
    # TRIPLE TOP / BOTTOM
    # ─────────────────────────────────────────────────────────────────────────
    if len(swing_highs) >= 3:
        t1, t2, t3 = swing_highs[-3], swing_highs[-2], swing_highs[-1]
        p1, p2, p3 = highs[t1], highs[t2], highs[t3]
        avg_top = (p1 + p2 + p3) / 3
        if all(abs(p - avg_top) / avg_top < atr_tol * 1.5 for p in [p1, p2, p3]) and (t3 - t1) >= 12:
            neckline  = float(lows[t1:t3 + 1].min()) if t3 > t1 else price
            brk_neck  = price < neckline * (1 - breakout_pct)
            vol_ok    = vol_declining(t1, t3)
            conf = min(0.90, 0.52 + (0.22 if brk_neck else 0) + (0.08 if vol_ok else 0))
            results.append({"name": "Triple Top", "bias": "bearish",
                            "signal": "SELL" if brk_neck else "NEUTRAL",
                            "confidence": conf, "breakout": brk_neck,
                            "breakout_level": neckline, "category": "reversal",
                            "volume_confirmed": vol_ok,
                            "details": {"top1": round(float(p1), 6), "top2": round(float(p2), 6),
                                        "top3": round(float(p3), 6), "neckline": round(neckline, 6)},
                            "geometry": geometry})

    if len(swing_lows) >= 3:
        b1, b2, b3 = swing_lows[-3], swing_lows[-2], swing_lows[-1]
        p1, p2, p3 = lows[b1], lows[b2], lows[b3]
        avg_bot = (p1 + p2 + p3) / 3
        if all(abs(p - avg_bot) / avg_bot < atr_tol * 1.5 for p in [p1, p2, p3]) and (b3 - b1) >= 12:
            neckline  = float(highs[b1:b3 + 1].max()) if b3 > b1 else price
            brk_neck  = price > neckline * (1 + breakout_pct)
            vol_ok    = vol_declining(b1, b3)
            conf = min(0.90, 0.52 + (0.22 if brk_neck else 0) + (0.08 if vol_ok else 0))
            results.append({"name": "Triple Bottom", "bias": "bullish",
                            "signal": "BUY" if brk_neck else "NEUTRAL",
                            "confidence": conf, "breakout": brk_neck,
                            "breakout_level": neckline, "category": "reversal",
                            "volume_confirmed": vol_ok,
                            "details": {"bottom1": round(float(p1), 6), "bottom2": round(float(p2), 6),
                                        "bottom3": round(float(p3), 6), "neckline": round(neckline, 6)},
                            "geometry": geometry})

    # Sort: breakout + volume > breakout > confidence
    results.sort(key=lambda p: (
        int(bool(p.get("breakout") and p.get("volume_confirmed"))),
        int(bool(p.get("breakout"))),
        p["confidence"],
    ), reverse=True)

    return results


def _ema_proximity_pct(price, ema_val):
    """Return how close price is to an EMA as a percentage (absolute)."""
    if not ema_val or ema_val == 0:
        return 999.0
    return abs(price - ema_val) / ema_val * 100


def _build_tf_context_summary(tf, df, current_price, sr_resistance, sr_support,
                               fib_levels, fib_direction, asset_type, symbol):
    """
    Rich text summary for a non-primary timeframe. No raw candles — just computed conclusions.
    Chev gets everything he needs to understand this TF's picture in ~300-400 words.
    """
    lines = [f"\n{'═'*54}", f"  {tf.upper()} CONTEXT  (last close: {str(df.index[-1])[:16]})", f"{'═'*54}"]

    last = df.iloc[-1]
    tf_close   = float(last["close"])
    ema13      = last.get("EMA13")
    ema21      = last.get("EMA21")
    ema55      = last.get("EMA55")
    rsi_val    = last.get("RSI")
    atr_val    = last.get("ATR")
    vwap_val   = last.get("VWAP")
    bb_u       = last.get("BB_upper")
    bb_m       = last.get("BB_mid")
    bb_l       = last.get("BB_lower")
    macd_h     = last.get("MACD_hist")

    # ── Trend ─────────────────────────────────────────────────────────────────
    sma50 = df["close"].rolling(50).mean().iloc[-1] if len(df) >= 50 else None
    higher_highs = (df["high"].iloc[-1] > df["high"].iloc[-10] and
                    df["high"].iloc[-10] > df["high"].iloc[-20]) if len(df) >= 20 else None
    higher_lows  = (df["low"].iloc[-1] > df["low"].iloc[-10] and
                    df["low"].iloc[-10] > df["low"].iloc[-20]) if len(df) >= 20 else None
    if sma50 is not None and not pd.isna(sma50):
        if tf_close > sma50 and higher_highs and higher_lows:
            trend_str = "BULLISH — higher highs and higher lows, price above SMA50"
        elif tf_close > sma50:
            trend_str = "BULLISH BIAS — price above SMA50"
        elif tf_close < sma50 and not higher_highs and not higher_lows:
            trend_str = "BEARISH — lower highs and lower lows, price below SMA50"
        else:
            trend_str = "BEARISH BIAS — price below SMA50"
    else:
        trend_str = "UNKNOWN"
    lines.append(f"TREND        : {trend_str}")

    # ── EMA alignment ─────────────────────────────────────────────────────────
    ema_vals = {"EMA13": ema13, "EMA21": ema21, "EMA55": ema55}
    ema_lines = []
    for name, val in ema_vals.items():
        if val is not None and not pd.isna(val):
            dist = _ema_proximity_pct(tf_close, val)
            side = "above" if tf_close >= val else "below"
            at_flag = " ← price AT this level" if dist < 0.4 else ""
            ema_lines.append(f"  {name}: {val:.5f}  ({dist:.2f}% {side}){at_flag}")

    if ema13 and ema21 and ema55 and not pd.isna(ema13) and not pd.isna(ema21) and not pd.isna(ema55):
        if ema13 > ema21 > ema55:
            stack = "BULLISH STACK (13>21>55) — strong uptrend alignment"
        elif ema13 < ema21 < ema55:
            stack = "BEARISH STACK (13<21<55) — strong downtrend alignment"
        else:
            stack = "MIXED — EMAs not aligned, choppy/transitioning"
        lines.append(f"EMA ALIGNMENT: {stack}")
    if ema_lines:
        lines.extend(ema_lines)

    crossover = _detect_ema_crossover(df)
    if crossover:
        lines.append(f"  EMA CROSSOVER: EMA13 crossed {crossover['type']} {crossover['candles_ago']} candle(s) ago — trend shift signal")

    # ── RSI ───────────────────────────────────────────────────────────────────
    if rsi_val is not None and not pd.isna(rsi_val):
        rsi_note = (" — OVERBOUGHT, short bias" if rsi_val > 70
                    else " — OVERSOLD, long bias" if rsi_val < 30
                    else " — neutral zone" if 40 < rsi_val < 60
                    else "")
        lines.append(f"RSI          : {rsi_val:.1f}{rsi_note}")

    reg_divs    = _ca_detect_rsi_divergence(df)
    hidden_divs = _detect_hidden_divergence(df)
    all_divs    = reg_divs + hidden_divs
    if all_divs:
        for d in all_divs[:3]:
            lines.append(f"  DIVERGENCE: {d['type']} — {d['note']}")

    # ── Key S/R levels ────────────────────────────────────────────────────────
    lines.append("KEY LEVELS")
    res_close = [z for z in sr_resistance if z["price"] > tf_close]
    sup_close = [z for z in sr_support    if z["price"] < tf_close]
    res_close.sort(key=lambda z: z["price"])
    sup_close.sort(key=lambda z: -z["price"])
    if res_close:
        for z in res_close[:3]:
            dist = (z["price"] - tf_close) / tf_close * 100
            lines.append(f"  Resistance : {z['price']:.5f}  [{z.get('label','multi-tf')}]  (+{dist:.2f}% away)")
    else:
        lines.append("  Resistance : none detected nearby")
    if sup_close:
        for z in sup_close[:3]:
            dist = (tf_close - z["price"]) / tf_close * 100
            lines.append(f"  Support    : {z['price']:.5f}  [{z.get('label','multi-tf')}]  (-{dist:.2f}% away)")
    else:
        lines.append("  Support    : none detected nearby")

    # ── Fibonacci ─────────────────────────────────────────────────────────────
    if fib_levels:
        fib50  = fib_levels.get("50%")
        fib618 = fib_levels.get("61.8% (golden pocket)")
        in_gp  = (fib50 and fib618 and
                  min(fib50, fib618) * 0.999 <= tf_close <= max(fib50, fib618) * 1.001)
        lines.append(f"FIBONACCI    : ({fib_direction})")
        for name, price in fib_levels.items():
            dist = abs(tf_close - price) / tf_close * 100
            gp_tag = " ★ GOLDEN POCKET" if name in ("50%", "61.8% (golden pocket)") and in_gp else ""
            lines.append(f"  {name}: {price:.5f}  ({dist:.2f}% away){gp_tag}")
        if in_gp:
            lines.append("  ★★ PRICE IN GOLDEN POCKET (50%–61.8% zone) — high-probability bounce area")

    # ── Liquidity sweeps ──────────────────────────────────────────────────────
    sweeps = _detect_liquidity_sweep(df)
    if sweeps:
        for s in sweeps[:2]:
            lines.append(f"SWEEP ALERT  : {s['type'].upper()} — {s['note']}")

    # ── Equal highs/lows ──────────────────────────────────────────────────────
    eq_highs, eq_lows = _detect_equal_highs_lows(df)
    if eq_highs:
        for price, count in eq_highs[:2]:
            dist = (price - tf_close) / tf_close * 100
            lines.append(f"EQUAL HIGHS  : {price:.5f} ({count}x) — liquidity pool above (+{dist:.2f}%)")
    if eq_lows:
        for price, count in eq_lows[:2]:
            dist = (tf_close - price) / tf_close * 100
            lines.append(f"EQUAL LOWS   : {price:.5f} ({count}x) — liquidity pool below (-{dist:.2f}%)")

    # ── Volume / VWAP / BB ────────────────────────────────────────────────────
    if vwap_val is not None and not pd.isna(vwap_val):
        vwap_side = "above" if tf_close > vwap_val else "below"
        lines.append(f"VWAP         : {vwap_val:.5f} — price {vwap_side} VWAP ({'bullish' if vwap_side == 'above' else 'bearish'} bias)")
    if atr_val is not None and not pd.isna(atr_val):
        lines.append(f"ATR(14)      : {atr_val:.5f}")
    if bb_u and bb_m and bb_l and not pd.isna(bb_u):
        bb_w = round((bb_u - bb_l) / bb_m * 100, 2)
        squeeze = " ⚡ SQUEEZE — big move imminent" if bb_w < 1.5 else ""
        lines.append(f"BB(20,2)     : upper={bb_u:.5f}  mid={bb_m:.5f}  lower={bb_l:.5f}  width={bb_w}%{squeeze}")
    if macd_h is not None and not pd.isna(macd_h):
        lines.append(f"MACD hist    : {macd_h:+.5g} ({'bullish momentum' if macd_h > 0 else 'bearish momentum'})")

    # ── Volume trend ──────────────────────────────────────────────────────────
    if len(df) >= 10:
        vol_recent = df["volume"].iloc[-5:].mean()
        vol_older  = df["volume"].iloc[-20:-5].mean() if len(df) >= 20 else vol_recent
        vol_ratio  = vol_recent / vol_older if vol_older else 1.0
        vol_str = (f"HIGH ({vol_ratio:.1f}x avg) — institutional interest" if vol_ratio > 1.5
                   else f"LOW ({vol_ratio:.1f}x avg) — weak conviction" if vol_ratio < 0.7
                   else f"NORMAL ({vol_ratio:.1f}x avg)")
        lines.append(f"VOLUME       : {vol_str}")

    # ── Last 3 candle pattern summary ─────────────────────────────────────────
    patterns = _detect_candlestick_patterns(df, n=3)
    if patterns:
        lines.append("PATTERNS     : " + " | ".join(patterns[:3]))

    return "\n".join(lines)


# ============================================================
# MAIN SCAN
# ============================================================

def scan_pair(symbol, asset_type):
    try:
        timeframes = TIMEFRAMES_CRYPTO if asset_type == "crypto" else TIMEFRAMES_FOREX_STOCK
        tf_high_clusters, tf_low_clusters = {}, {}
        primary_df = None
        for tf in timeframes:
            df = fetch_candles(symbol, asset_type, tf, limit=700)
            if tf == "1h":
                primary_df = df.copy()
            sh, sl = find_swings(df)
            tf_high_clusters[tf] = cluster(sh)
            tf_low_clusters[tf] = cluster(sl)
            time.sleep(0.5)

        if primary_df is None or len(primary_df) < 30:
            return None

        primary_df = add_rsi(primary_df)
        current_price = primary_df["close"].iloc[-1]
        rsi_current = float(primary_df["RSI"].iloc[-1]) if "RSI" in primary_df.columns else None
        sma50 = float(primary_df["close"].rolling(50).mean().iloc[-1]) if len(primary_df) >= 50 else None
        trend = "uptrend" if (sma50 and current_price > sma50) else ("downtrend" if sma50 else "unknown")
        resistance_a, resistance_b = compute_validated_levels(tf_high_clusters, primary_df)
        support_a, support_b = compute_validated_levels(tf_low_clusters, primary_df)

        resistance_a = [z for z in resistance_a if z["price"] > current_price]
        support_a = [z for z in support_a if z["price"] < current_price]
        resistance_b = [z for z in resistance_b if z["price"] > current_price]
        support_b = [z for z in support_b if z["price"] < current_price]

        resistance_a.sort(key=lambda z: z["price"])
        support_a.sort(key=lambda z: -z["price"])
        resistance_b.sort(key=lambda z: z["price"])
        support_b.sort(key=lambda z: -z["price"])

        top_r = resistance_a[0] if resistance_a else (resistance_b[0] if resistance_b else None)
        top_s = support_a[0] if support_a else (support_b[0] if support_b else None)

        fibs, fib_anchors = fib_from_real_impulse(primary_df)
        divergences = detect_rsi_divergence(primary_df)
        vp_anchors = None
        try:
            consolidation = _ca_detect_consolidation_range(primary_df)
            if consolidation:
                vp_anchors = {
                    "start_t": int(primary_df.index[consolidation[0]].timestamp()),
                    "end_t":   int(primary_df.index[consolidation[1]].timestamp()),
                }
        except Exception:
            pass

        candidates = []
        if top_r:
            candidates.append(("resistance", top_r["price"]))
        if top_s:
            candidates.append(("support", top_s["price"]))
        for name, price in fibs.items():
            candidates.append((f"Fib {name}", price))
        for d in divergences:
            candidates.append((d["type"], d["price"]))

        best_count = 0
        best_price = current_price
        best_reasons = []
        for _, base_price in candidates:
            agree = []
            if top_r and abs(base_price - top_r["price"]) / base_price * 100 <= 1.5:
                agree.append("Resistance")
            if top_s and abs(base_price - top_s["price"]) / base_price * 100 <= 1.5:
                agree.append("Support")
            for name, price in fibs.items():
                if abs(base_price - price) / base_price * 100 <= 1.5:
                    agree.append(f"Fib {name}")
                    break
            for d in divergences:
                if abs(base_price - d["price"]) / base_price * 100 <= 1.5:
                    agree.append(d["type"])
                    break
            agree = list(set(agree))
            if len(agree) > best_count:
                best_count = len(agree)
                best_price = base_price
                best_reasons = agree

        return {
            "symbol": symbol,
            "price": best_price,
            "count": best_count,
            "reasons": best_reasons,
            "current_price": current_price,
            "trend": trend,
            "rsi": rsi_current,
            "resistance": top_r,
            "support": top_s,
            "fibs": fibs,
            "fib_anchors": fib_anchors,
            "vp_anchors": vp_anchors,
            "divergences": divergences,
        }
    except Exception as e:
        print(f"[{datetime.now()}] Error scanning {symbol}: {e}")
        return None


def scan_pair_tf(symbol, asset_type, primary_tf):
    """
    Per-TF confluence scanner. Replaces scan_pair for the new TF-split architecture.
    Detects: S/R (multi-TF validated), Fib (primary TF, golden pocket aware),
    RSI divergence (regular + hidden), EMA 13/21/55 proximity/crossover, liquidity sweeps.
    Returns a weighted confluence score and the primary_tf + trade_type for the brief builder.
    """
    try:
        # Which TFs to use for S/R validation (always include primary + higher TFs)
        all_sr_tfs = ["15m", "30m", "1h", "4h"]
        if primary_tf == "5m":
            all_sr_tfs = ["5m", "15m", "30m", "1h"]
        elif primary_tf == "15m":
            all_sr_tfs = ["15m", "30m", "1h", "4h"]
        elif primary_tf == "30m":
            all_sr_tfs = ["15m", "30m", "1h", "4h"]
        fetch_tfs = list({primary_tf} | set(all_sr_tfs))

        min_touches_map = ({"5m": 8, "15m": 5, "30m": 3, "1h": 2, "4h": 2}
                           if asset_type == "crypto" else {"15m": 6, "30m": 4, "1h": 2, "4h": 2})

        # Fetch candles — 500 for primary, 200 for supporting TFs (S/R only needs structure)
        tf_data = {}
        def _fetch(tf):
            limit = 500 if tf == primary_tf else 200
            df = fetch_candles(symbol, asset_type, tf, limit=limit)
            return tf, _ca_add_indicators(df)

        with ThreadPoolExecutor(max_workers=len(fetch_tfs)) as ex:
            futs = {ex.submit(_fetch, tf): tf for tf in fetch_tfs}
            for fut in as_completed(futs):
                try:
                    tf, df = fut.result()
                    tf_data[tf] = df
                except Exception as e:
                    print(f"[scan_tf] {symbol} {futs[fut]} skipped: {e}")

        primary_df = tf_data.get(primary_tf)
        if primary_df is None or len(primary_df) < 30:
            return None

        primary_df = add_rsi(primary_df)
        current_price = float(primary_df["close"].iloc[-1])
        rsi_current = float(primary_df["RSI"].iloc[-1]) if "RSI" in primary_df.columns else None

        # ── S/R multi-TF (using _ca helpers which return scored zone objects) ──
        res_touches_by_tf, sup_touches_by_tf = {}, {}
        for tf in all_sr_tfs:
            df = tf_data.get(tf)
            if df is None:
                continue
            touches = _ca_get_timed_touches(df)
            res_touches_by_tf[tf] = [(ts, p) for ts, p, k in touches if k == "resistance"]
            sup_touches_by_tf[tf] = [(ts, p) for ts, p, k in touches if k == "support"]

        res_levels = _ca_build_validated_levels(res_touches_by_tf, "resistance", min_touches_map=min_touches_map)
        sup_levels = _ca_build_validated_levels(sup_touches_by_tf, "support",    min_touches_map=min_touches_map)
        res_above  = sorted([z for z in res_levels if z["price"] > current_price], key=lambda z: z["price"])
        sup_below  = sorted([z for z in sup_levels if z["price"] < current_price], key=lambda z: -z["price"])
        top_r = res_above[0] if res_above else None
        top_s = sup_below[0] if sup_below else None

        # S/R confluence score: multi-TF validated zone is worth 2pts, single-TF is 1pt
        sr_score = 0
        sr_reasons = []
        for zone, label in [(top_r, "Resistance"), (top_s, "Support")]:
            if zone and abs(zone["price"] - current_price) / current_price * 100 <= 1.5:
                instances = zone.get("instances", 1)
                pts = 2 if instances >= 3 else 1
                sr_score += pts
                sr_reasons.append(f"{label}({instances}x,{pts}pt)")

        # ── Fibonacci + golden pocket ──────────────────────────────────────────
        fib_levels, fib_direction = _ca_fib_from_real_impulse(primary_df)
        fib_score   = 0
        fib_reasons = []
        fib50  = fib_levels.get("50%")
        fib618 = fib_levels.get("61.8% (golden pocket)")
        in_golden_pocket = False
        for name, price in fib_levels.items():
            if abs(current_price - price) / current_price * 100 <= 1.0:
                if name in ("50%", "61.8% (golden pocket)"):
                    fib_score += 2
                    fib_reasons.append(f"Fib {name} (2pt)")
                    if fib50 and fib618:
                        lo, hi = min(fib50, fib618), max(fib50, fib618)
                        if lo * 0.999 <= current_price <= hi * 1.001:
                            in_golden_pocket = True
                else:
                    fib_score += 1
                    fib_reasons.append(f"Fib {name}")
                break
        if in_golden_pocket and fib_score < 3:
            fib_score = 3
            fib_reasons = [r for r in fib_reasons if "Fib" in r]
            fib_reasons.insert(0, "★ GOLDEN POCKET")

        # ── RSI divergence (regular + hidden) ────────────────────────────────
        reg_divs    = _ca_detect_rsi_divergence(primary_df) + _ca_detect_rsi_divergence_forming(primary_df)
        hidden_divs = _detect_hidden_divergence(primary_df)
        all_divs    = reg_divs + hidden_divs
        div_score   = 0
        div_reasons = []
        for d in all_divs[:2]:
            if abs(d["price"] - current_price) / current_price * 100 <= 2.0:
                pts = 1.5 if "hidden" in d["type"] else 1
                div_score += pts
                div_reasons.append(d["type"])

        # ── Forming divergence (live — price new extreme + RSI diverging) ──
        forming_divs  = _detect_forming_divergence(primary_df)
        form_score    = 0
        form_reasons  = []
        if forming_divs:
            best = forming_divs[0]
            form_score = best["score"]
            form_reasons.append(
                f"FORMING {best['type']} (RSI gap {best['rsi_gap']}pt, {best['age_bars']} bars)"
            )

        # ── RSI overbought / oversold + 50-cross ─────────────────────────────
        rsi_level_score   = 0
        rsi_level_reasons = []
        if rsi_current is not None:
            if rsi_current >= 80:
                rsi_level_score += 0.5
                rsi_level_reasons.append(f"RSI OVERBOUGHT ({rsi_current:.1f}, 0.5pt)")
            elif rsi_current <= 20:
                rsi_level_score += 0.5
                rsi_level_reasons.append(f"RSI OVERSOLD ({rsi_current:.1f}, 0.5pt)")
            cross = _rsi_50_cross(primary_df)
            if cross:
                rsi_level_score += 0.5
                rsi_level_reasons.append(
                    f"RSI crossed 50 {cross['direction']} {cross['bars_ago']} bar(s) ago (0.5pt)"
                )

        # ── EMA 13/21/55 proximity and crossover ─────────────────────────────
        ema_score   = 0
        ema_reasons = []
        last = primary_df.iloc[-1]
        for ema_col, ema_weight, ema_label in [("EMA55", 2.0, "EMA55"), ("EMA21", 1.5, "EMA21"), ("EMA13", 1.0, "EMA13")]:
            val = last.get(ema_col)
            if val is None or pd.isna(val):
                continue
            dist_pct = _ema_proximity_pct(current_price, val)
            if dist_pct <= 0.5:
                ema_score += ema_weight
                side = "support" if current_price >= val else "resistance"
                ema_reasons.append(f"{ema_label} {side} ({ema_weight}pt)")

        crossover = _detect_ema_crossover(primary_df)
        if crossover and crossover["candles_ago"] <= 3:
            ema_score += 1
            ema_reasons.append(f"EMA crossover {crossover['type']} {crossover['candles_ago']}c ago")

        # ── Liquidity sweep ───────────────────────────────────────────────────
        sweeps     = _detect_liquidity_sweep(primary_df)
        sweep_score   = 0
        sweep_reasons = []
        for s in sweeps[:1]:
            sweep_score += 2
            sweep_reasons.append(f"Sweep:{s['type']}")

        # ── Chart pattern engine ──────────────────────────────────────────────
        # Geometry-based scoring: breakout + volume = highest conviction.
        # All qualifying patterns are kept (multiple may coexist — Chev decides).
        pattern_result  = None
        pattern_score   = 0
        pattern_reasons = []
        all_patterns    = []
        try:
            all_patterns = _run_pattern_engine(primary_df)
            if all_patterns:
                top = all_patterns[0]
                pattern_result = top
                # Score by strongest signal in the top pattern
                if top.get("breakout") and top.get("volume_confirmed"):
                    pattern_score = 3.0   # breakout confirmed + volume = high conviction
                elif top.get("breakout"):
                    pattern_score = 2.0   # breakout, no volume yet
                elif top.get("volume_confirmed"):
                    pattern_score = 1.5   # volume contraction confirmed, approaching breakout
                elif top["confidence"] >= 0.70:
                    pattern_score = 1.5   # forming high-confidence pattern
                elif top["confidence"] >= 0.55:
                    pattern_score = 1.0   # forming moderate-confidence pattern

                is_meaningful = (top.get("breakout") or top.get("volume_confirmed")
                                 or top["confidence"] >= 0.55)
                if is_meaningful:
                    bias_tag = f"{top['name']} ({top['bias']}, conf={top['confidence']:.2f})"
                    if top.get("breakout"):
                        bias_tag += " BREAKOUT"
                    if top.get("volume_confirmed"):
                        bias_tag += "+vol"
                    pattern_reasons.append(bias_tag)
        except Exception as _pe:
            pass

        # ── Bollinger Band signals ────────────────────────────────────────────
        # Uses %B = (price - lower) / (upper - lower) — self-normalizing across
        # all assets. %B > 1 = burst above, %B < 0 = burst below.
        # Thresholds are band-relative so BTC (tight bands) and SOL (wide bands)
        # trigger at the same structural position within their own band.
        bb_score   = 0
        bb_reasons = []
        try:
            _bbu = float(primary_df["BB_upper"].iloc[-1])
            _bbm = float(primary_df["BB_mid"].iloc[-1])
            _bbl = float(primary_df["BB_lower"].iloc[-1])
            _bbw = (_bbu - _bbl) / _bbm * 100 if _bbm else 0
            _bb_base = {"4h": 3, "1h": 2, "30m": 2, "15m": 1}.get(primary_tf, 2)
            _band_range = _bbu - _bbl
            _pct_b = (current_price - _bbl) / _band_range if _band_range > 0 else 0.5

            if _pct_b > 1.0:
                _overshoot = round((_pct_b - 1.0) * 100, 1)
                bb_score += _bb_base
                bb_reasons.append(f"BB upper BURST (%B={_pct_b:.2f}, {_overshoot}% of band outside, {_bb_base}pt)")
            elif _pct_b < 0.0:
                _overshoot = round(abs(_pct_b) * 100, 1)
                bb_score += _bb_base
                bb_reasons.append(f"BB lower BURST (%B={_pct_b:.2f}, {_overshoot}% of band outside, {_bb_base}pt)")
            elif _pct_b >= 0.85:
                _pts = round(_bb_base * 0.5, 1)
                bb_score += _pts
                bb_reasons.append(f"BB near upper (%B={_pct_b:.2f}, top 15% of band, {_pts}pt)")
            elif _pct_b <= 0.15:
                _pts = round(_bb_base * 0.5, 1)
                bb_score += _pts
                bb_reasons.append(f"BB near lower (%B={_pct_b:.2f}, bottom 15% of band, {_pts}pt)")

            if 0.48 <= _pct_b <= 0.52:
                bb_score += 1.0
                _side = "support" if current_price >= _bbm else "resistance"
                bb_reasons.append(f"BB mid {_side} (%B={_pct_b:.2f}, 1pt)")

            if _bbw < 1.5:
                bb_score += 1.5
                bb_reasons.append(f"BB squeeze (width {_bbw:.2f}%, 1.5pt)")
        except Exception:
            pass

        # ── Volume Profile proximity (anchor-based: 4H + 1H only) ───────────────
        # Range starts at the structural first-anchor of each TF (same as Arsenal).
        # 4H VP = big-picture value area. 1H VP = tactical entry context.
        # Lower TFs excluded — their VP windows are too short to carry structural weight.
        vp_score   = 0
        vp_reasons = []
        try:
            for _vp_tf, _vp_base in [("4h", 3), ("1h", 2)]:
                _dfv = tf_data.get(_vp_tf)
                if _dfv is None or len(_dfv) < 30:
                    continue
                _anc = _detect_auction_anchor(_dfv)
                if not _anc:
                    continue
                _vp = _ca_volume_profile(_dfv, _anc["idx"], len(_dfv) - 1)
                if not _vp:
                    continue
                for _lbl, _px, _pts in [("POC", _vp["poc"], _vp_base),
                                         ("VAH", _vp["vah"], _vp_base - 1),
                                         ("VAL", _vp["val"], _vp_base - 1)]:
                    _dist = abs(current_price - _px) / current_price * 100
                    if _dist <= 0.25:
                        vp_score += _pts
                        vp_reasons.append(f"VP {_lbl} {_vp_tf} ({_dist:.2f}% away, {_pts}pt)")
        except Exception:
            pass

        # ── Distance from level ───────────────────────────────────────────────
        # Find the dominant level being tested
        all_candidates = []
        if top_r: all_candidates.append(top_r["price"])
        if top_s: all_candidates.append(top_s["price"])
        for _, p in fib_levels.items(): all_candidates.append(p)
        nearest_level = min(all_candidates, key=lambda p: abs(p - current_price)) if all_candidates else None
        dist_from_level = abs(current_price - nearest_level) / current_price * 100 if nearest_level else None
        at_level = dist_from_level is not None and dist_from_level <= 0.3

        # ── Aggregate score ───────────────────────────────────────────────────
        total_score = sr_score + fib_score + div_score + ema_score + sweep_score + pattern_score + form_score + rsi_level_score + bb_score + vp_score
        all_reasons = sr_reasons + fib_reasons + div_reasons + ema_reasons + sweep_reasons + pattern_reasons + form_reasons + rsi_level_reasons + bb_reasons + vp_reasons

        sma50 = float(primary_df["close"].rolling(50).mean().iloc[-1]) if len(primary_df) >= 50 else None
        trend = "uptrend" if (sma50 and current_price > sma50) else ("downtrend" if sma50 else "unknown")
        rsi_current = float(primary_df["RSI"].iloc[-1]) if "RSI" in primary_df.columns else None

        # ── Market regime (4H preferred, fall back to highest available TF) ──
        regime_4h = None
        regime_primary = None
        for _rtf in ["4h", "1h", "30m", "15m"]:
            _rdf = tf_data.get(_rtf)
            if _rdf is not None and len(_rdf) >= 50:
                try:
                    regime_4h = _an_regime(_rdf)
                except Exception:
                    pass
                break
        if primary_tf not in ("4h", "1h") and len(primary_df) >= 50:
            try:
                regime_primary = _an_regime(primary_df)
            except Exception:
                pass

        # ── Engine Stack: Swing → Leg → State → Geometry → Auction → Hypothesis ──
        _survey = None
        try:
            _regime_str = (regime_4h or {}).get("regime", "UNKNOWN") if isinstance(regime_4h, dict) else "UNKNOWN"
            _sr_sup  = top_s["price"] if top_s else None
            _sr_res  = top_r["price"] if top_r else None
            _survey  = engines.run_survey(
                df=primary_df,
                symbol=symbol,
                primary_tf=primary_tf,
                dexter_score=total_score,
                dexter_reasons=all_reasons,
                regime_4h=_regime_str,
                sr_support=_sr_sup,
                sr_resistance=_sr_res,
            )
        except Exception as _se:
            print(f"[Dexter] Engine stack failed for {symbol}/{primary_tf}: {_se}")

        return {
            "symbol":          symbol,
            "price":           nearest_level or current_price,
            "count":           total_score,
            "reasons":         all_reasons,
            "current_price":   current_price,
            "trend":           trend,
            "rsi":             rsi_current,
            "resistance":      top_r,
            "support":         top_s,
            "fibs":            fib_levels,
            "divergences":     all_divs,
            "forming_divs":    forming_divs,
            "rsi_ob_os":       rsi_level_reasons,
            "at_level":        at_level,
            "dist_from_level": dist_from_level,
            "primary_tf":      primary_tf,
            "trade_type":      TF_TRADE_TYPE.get(primary_tf, "day"),
            "in_golden_pocket": in_golden_pocket,
            "sweeps":          sweeps,
            "pattern":         pattern_result,
            "all_patterns":    all_patterns,
            "regime_4h":       regime_4h,
            "regime_primary":  regime_primary,
            "survey":          _survey,
        }

    except Exception as e:
        print(f"[{datetime.now()}] Error in scan_pair_tf {symbol}/{primary_tf}: {e}")
        return None


# ============================================================
# CHEV COMMUNICATION
# ============================================================

def _calc_confluence_score(tags_str):
    """Return (score, [tags]) from a comma-separated confluence tag string."""
    tags = [t.strip().lower() for t in tags_str.split(",") if t.strip()]
    score = sum(CONFLUENCE_SCORES.get(t, 0) for t in tags)
    return score, tags


_CRYPTO_CORR_SYMBOLS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "XLMUSDT"}

def _portfolio_heat_context(asset_type, symbol):
    """Return a heat warning string for the escalation prompt."""
    open_only    = [t for t in open_trades if t.get("status") == "OPEN"]
    pending_only = [t for t in open_trades if t.get("status") == "PENDING"]
    risk_open    = sum(t.get("risk_pct", 0) for t in open_only)
    risk_pending = sum(t.get("risk_pct", 0) for t in pending_only)
    total_risk   = risk_open + risk_pending
    count_open   = len(open_only)
    count_pending = len(pending_only)

    heat_parts = [f"{count_open} open"]
    if count_pending:
        heat_parts.append(f"{count_pending} pending")
    heat_line = (
        f"PORTFOLIO HEAT: {' + '.join(heat_parts)} trade(s), {total_risk:.1f}% total risk committed "
        f"(open {risk_open:.1f}% + pending {risk_pending:.1f}%) "
        f"(soft cap 8% — you may exceed it for an A+ setup, but size consciously)."
    )
    corr_note = ""
    if asset_type == "crypto" and symbol in _CRYPTO_CORR_SYMBOLS:
        all_active = open_only + pending_only
        for direction in ("long", "short"):
            same_dir = [t for t in all_active
                        if t.get("symbol") in _CRYPTO_CORR_SYMBOLS
                        and t.get("direction") == direction]
            if len(same_dir) >= 2:
                names = ", ".join(t["symbol"] for t in same_dir)
                corr_note += (
                    f"\nCORRELATION: {len(same_dir)} correlated crypto {direction}s active "
                    f"({names}). They move together — this is concentrated macro exposure, not diversification."
                )
    return heat_line + corr_note

def _session_grade(asset_type):
    """Return (label, quality) where quality is PEAK / GOOD / LOW."""
    now_utc = datetime.now(timezone.utc)
    dec_h   = now_utc.hour + now_utc.minute / 60
    if  8.0 <= dec_h < 10.0:
        return "LONDON OPEN (08:00–10:00 UTC)", "PEAK"
    if 13.5 <= dec_h < 16.0:
        return "NY OPEN (13:30–16:00 UTC)", "PEAK"
    if 13.0 <= dec_h < 17.0:
        return "LONDON/NY OVERLAP (13:00–17:00 UTC)", "PEAK"
    if  8.0 <= dec_h < 17.0:
        return "London session", "GOOD"
    if 13.0 <= dec_h < 22.0:
        return "New York session", "GOOD"
    if asset_type == "forex":
        return "Asian session — thin forex volume", "LOW"
    return "Asian session", "GOOD"

def _setup_grade(result, asset_type):
    """Return (grade, suggested_risk_pct) based on confluence score, regime, session."""
    min_score = 10 if asset_type == "crypto" else 7
    score     = result.get("count", 0)
    regime    = (result.get("regime_4h") or {}).get("regime", "UNKNOWN")
    _, session_quality = _session_grade(asset_type)
    ratio = score / min_score if min_score else 1
    if ratio >= 2.0:
        grade = "A+"
    elif ratio >= 1.5:
        grade = "A"
    else:
        grade = "B"
    if regime in ("TRENDING_UP", "TRENDING_DOWN"):
        if grade == "B":   grade = "A"
        elif grade == "A": grade = "A+"
    if session_quality == "PEAK" and grade == "A":
        grade = "A+"
    if session_quality == "LOW" and asset_type == "forex":
        if grade == "A+": grade = "A"
        elif grade == "A": grade = "B"
    return grade, {"B": 0.75, "A": 1.5, "A+": 2.5}[grade]


def ask_chev_to_judge(result, balance, dashboard_ws=None, timeout=360):
    global _chev_online
    symbol = result["symbol"]
    confluence_zone = result['price']

    # Build playbook + journal context to prepend to the prompt
    asset_type     = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")
    min_score      = 10 if asset_type == "crypto" else 7
    session_label, session_quality = _session_grade(asset_type)
    grade, suggested_risk          = _setup_grade(result, asset_type)
    heat_context                   = _portfolio_heat_context(asset_type, symbol)
    playbook_text = _load_playbook()
    journal = _load_journal()
    same_type = [e for e in journal if e.get("asset_type") == asset_type][-3:]
    last_two  = [e for e in journal if e not in same_type][-2:]
    context_entries = same_type + last_two
    context_prefix = ""
    if playbook_text:
        context_prefix += f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n"
    if context_entries:
        journal_lines = "\n\n".join([
            f"• {e['symbol']} {e['direction'].upper()} → {e['outcome']} (${e['pnl']:+.2f}) | {e['ts'][:10]}\n"
            f"  Confluences: {e.get('tags','none')} | Duration: {e.get('duration','?')}\n"
            f"  Analysis: {e.get('analysis','none')}"
            for e in context_entries
        ])
        context_prefix += f"=== RELEVANT RECENT TRADES (context only) ===\n{journal_lines}\n\n"

    # Full market data brief — primary TF gets full candles, others get rich summaries
    primary_tf = result.get("primary_tf", "1h")
    trade_type = result.get("trade_type", TF_TRADE_TYPE.get(primary_tf, "day"))
    market_brief = _build_rich_market_brief(symbol, asset_type, primary_tf)
    context_prefix += market_brief + "\n\n"
    # Session + calendar context
    sessions     = _active_sessions()
    cal_warnings = _upcoming_high_impact(symbol, asset_type)
    dist_pct     = result.get("dist_from_level")

    context_prefix += f"=== DEXTER'S DETECTED CONFLUENCE ===\n"
    context_prefix += f"Primary timeframe: {primary_tf.upper()}  |  Expected trade type: {trade_type}\n"
    session_note = {
        "PEAK": "PEAK volume window — cleanest price action and execution of the day.",
        "GOOD": "Good liquidity.",
        "LOW":  "Thin market — consider waiting for session overlap before sizing up.",
    }.get(session_quality, "")
    context_prefix += f"Active session: {session_label} — {session_note}\n"
    if dist_pct is not None:
        context_prefix += f"Distance from confluence level: {dist_pct:.2f}% {'(price already past level)' if dist_pct < 0 else ''}\n"
    if cal_warnings:
        context_prefix += f"ECONOMIC CALENDAR — HIGH IMPACT:\n"
        for w in cal_warnings:
            context_prefix += f"  {w}\n"
        context_prefix += "Consider the event risk carefully before posting a trade.\n"

    direction_hint = ""
    if result.get("support") and abs(confluence_zone - result["support"]["price"]) / confluence_zone * 100 <= 1.5:
        direction_hint = "Dexter notes: confluence is AT or NEAR the support zone — bias LONG."
    elif result.get("resistance") and abs(confluence_zone - result["resistance"]["price"]) / confluence_zone * 100 <= 1.5:
        direction_hint = "Dexter notes: confluence is AT or NEAR the resistance zone — bias SHORT."

    r4h  = result.get("regime_4h")  or {}
    r_pr = result.get("regime_primary") or {}
    regime_context = ""
    if r4h.get("regime") and r4h["regime"] != "UNKNOWN":
        r4_label = r4h["regime"]
        r4_str   = f"4H: {r4_label} (ADX={r4h['adx']}, +DI={r4h['plus_di']}, -DI={r4h['minus_di']})"
        r_pr_str = ""
        if r_pr.get("regime") and r_pr["regime"] != "UNKNOWN" and primary_tf not in ("4h", "1h"):
            r_pr_str = f"  {primary_tf.upper()}: {r_pr['regime']} (ADX={r_pr['adx']})\n"
        trend_note = {
            "TRENDING_UP":   "LONG is WITH the 4H trend. SHORT is counter-trend — requires significantly stronger confluence to justify.",
            "TRENDING_DOWN": "SHORT is WITH the 4H trend. LONG is counter-trend — requires significantly stronger confluence to justify.",
            "RANGING":       "Both directions valid. Mean-reversion preferred — set TP at the opposite wall of the range, not beyond it.",
            "CHOPPY":        "Low ADX — noisy, directionless market. Only the absolute highest-tier confluence qualifies.",
        }.get(r4_label, "")
        regime_context = f"MARKET REGIME:\n  {r4_str}\n{r_pr_str}  {trend_note}\n\n"

    # Counter-trend warning — fires when Dexter's directional bias opposes the 4H regime
    counter_trend_warning = ""
    _r4_regime = r4h.get("regime", "")
    if direction_hint and _r4_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        _bias_long  = "LONG"  in direction_hint
        _bias_short = "SHORT" in direction_hint
        _is_counter = (_bias_long and _r4_regime == "TRENDING_DOWN") or \
                      (_bias_short and _r4_regime == "TRENDING_UP")
        if _is_counter:
            _bias_dir = "LONG" if _bias_long else "SHORT"
            counter_trend_warning = (
                f"⚠️  COUNTER-TREND ALERT: Dexter's confluence bias is {_bias_dir}, "
                f"but the 4H regime is {_r4_regime}.\n"
                f"Counter-trend trades have lower win-rate and require all of the following:\n"
                f"  1. Score ≥ 13 (A+ grade — no exceptions)\n"
                f"  2. Confirmed RSI divergence on 1H or 4H (not just forming)\n"
                f"  3. SL behind a clear 4H structural level\n"
                f"  4. This is a SCALP only — tight SL, reduced size, no swing hold\n"
                f"If any of those are missing → SKIP.\n\n"
            )

    # ── Forming divergence + RSI level context ────────────────────────────
    forming_divs = result.get("forming_divs", [])
    rsi_ob_os    = result.get("rsi_ob_os", [])
    rsi_block    = ""
    if forming_divs:
        rsi_block += "FORMING RSI DIVERGENCE (live signal — first pivot confirmed, price already at new extreme):\n"
        for fd in forming_divs[:2]:
            rsi_block += (
                f"  {fd['type']} — first pivot: price {fd['pivot_price']} / RSI {fd['pivot_rsi']}  "
                f"→ current extreme: price {fd['cur_price']} / RSI {fd['cur_rsi']}  "
                f"(RSI gap: {fd['rsi_gap']} pts, {fd['age_bars']} bars building, score: {fd['score']}pt)\n"
            )
        rsi_block += "\n"
    if rsi_ob_os:
        rsi_block += "RSI LEVEL SIGNALS:\n"
        for note in rsi_ob_os:
            rsi_block += f"  {note}\n"
        rsi_block += "\n"

    # ── Engine stack output (Market State + Geometry + Auction + Hypotheses) ──
    pattern_block = ""
    _survey = result.get("survey")
    if _survey is not None:
        try:
            pattern_block = engines.format_survey_for_chev(_survey) + "\n"
        except Exception as _sf_err:
            print(f"[Dexter] format_survey_for_chev failed: {_sf_err}")
            pattern_block = ""
    # Fallback to legacy pattern block if survey unavailable
    if not pattern_block:
        _all_pats = result.get("all_patterns", [])
        _meaningful_pats = [p for p in _all_pats
                            if p.get("breakout") or p.get("volume_confirmed") or p.get("confidence", 0) >= 0.55]
        if _meaningful_pats:
            pattern_block += "CHART PATTERN ENGINE — GEOMETRY ANALYSIS:\n"
            for _p in _meaningful_pats[:4]:
                _status = "BREAKOUT" if _p.get("breakout") else ("VOL_CONFIRM" if _p.get("volume_confirmed") else "FORMING")
                _vol_str = "+volume" if _p.get("volume_confirmed") else ""
                pattern_block += (
                    f"  {_p['name']} | bias={_p['bias'].upper()} | conf={_p['confidence']:.0%} | "
                    f"status={_status}{_vol_str} | breakout_level={_p.get('breakout_level', 'n/a')}\n"
                )
                _det = _p.get("details", {})
                if _det:
                    _lvls = "  " + "  ".join(f"{k}={v}" for k, v in _det.items() if isinstance(v, (int, float)))
                    if _lvls.strip():
                        pattern_block += _lvls + "\n"
            _geo = _meaningful_pats[0].get("geometry", {})
            if _geo:
                pattern_block += (
                    f"  Geometry: upper_slope={_geo.get('upper_slope_norm')} lower_slope={_geo.get('lower_slope_norm')} "
                    f"compression={_geo.get('compression')} parallelism={_geo.get('parallelism')} "
                    f"converging={_geo.get('is_converging')} parallel={_geo.get('is_parallel')} "
                    f"impulse={_geo.get('has_impulse')} impulse_atr={_geo.get('impulse_atr')}\n"
                )
            pattern_block += "\n"

    msg = (
        f"⚡ OUTPUT FORMAT — READ FIRST:\n"
        f"Your reply MUST begin with one of these two lines — nothing before it:\n"
        f"  POST: <your read in 15 words max>      ← you're taking the trade\n"
        f"  SKIP: <one sentence reason>             ← you're passing\n"
        f"No preamble. No intro. No analysis header. POST: or SKIP: is literally the first word you write.\n\n"
        f"Hey Chev, Dexter here with REAL computed numbers. I've given you the full market data above — candles, volume, all levels. Study it yourself and make your own read.\n\n"
        f"I've detected a confluence zone at {confluence_zone:.5f} with {result['count']} factor(s) aligning: {', '.join(result['reasons'])}.\n"
        f"{direction_hint}\n\n"
        f"{counter_trend_warning}"
        f"{regime_context}"
        f"{rsi_block}"
        f"{pattern_block}"
        f"TOOLS AVAILABLE — CALL THEM BEFORE WRITING POST: OR SKIP:\n"
        f"You have live analysis tools. Use them now to independently verify this setup and populate your tags correctly.\n\n"
        f"ALWAYS CALL FIRST:\n"
        f"  get_support_resistance(\"{symbol}\", \"{primary_tf}\") — confirm whether a validated SR level exists near {confluence_zone:.5f}.\n"
        f"  If no Tier A level is near that zone, the SR confluence is weak. Say so in REASONING and drop the sr tag.\n\n"
        f"CALL WHEN THE DATA ABOVE SUGGESTS IT:\n"
        f"  get_stacked_fibonacci(\"{symbol}\", \"{primary_tf}\") — call if price looks like it's at a fib retracement.\n"
        f"    Confirmed golden pocket (0.618–0.65 overlap across TFs) → add gp=4 tag.\n"
        f"    Single-TF fib level only → add fib_Xtf tag with fib_high=<swing high> and fib_low=<swing low> anchors.\n\n"
        f"  get_volume_profile(\"{symbol}\", \"{primary_tf}\") — call if you suspect price is near a volume cluster.\n"
        f"    Entry within 0.3% of POC, VAH, or VAL → add vp=3 tag and set vp_start=<YYYY-MM-DD of anchor candle>.\n\n"
        f"  detect_rsi_divergence(\"{symbol}\", \"{primary_tf}\") — call if RSI looks like it's diverging on the candle data.\n"
        f"    If confirmed → add rsi_Xtf tag and set rsi_div_t1=<older pivot date>, rsi_div_t2=<newer pivot date>.\n\n"
        f"  get_atr_stop_suggestion(\"{symbol}\", \"{primary_tf}\", entry_price=<your entry>, direction=<long or short>) — call before finalising your SL.\n"
        f"    Your SL must clear at least 1× ATR from entry. If your structural SL is tighter than that, widen it or SKIP.\n\n"
        f"TOOL INTEGRITY RULE: Only tag what your tools confirm from live data. No guessed levels, no fabricated pivot dates.\n"
        f"If a tool returns no level near the zone — drop that tag. Your REASONING must explain what the tools showed.\n\n"
        f"Your job: decide if this is a genuine trade setup. Be an actual trader — reason about:\n"
        f"  1. Is price at a meaningful level or mid-range noise?\n"
        f"  2. What is the higher timeframe (4H+) structure telling you? Always check 4H for context, even when the entry trigger is on a 15m or 30m chart. A 4H SR or Fib confluence adds significant weight. You have NO directional bias — if a short setup has better confluence than a long, take the short.\n"
        f"  3. Is RSI giving confirmation? (>70 = overbought/short bias, <30 = oversold/long bias, divergence = reversal signal)\n"
        f"  4. Where is the SL? Place it just beyond the structural level that invalidates the trade — the nearest significant swing high/low or SR zone. "
        f"A small buffer beyond that level is fine. Being stopped out by a wick that breaches the level is correct behaviour — the level was broken, the trade is wrong. "
        f"What is NOT acceptable is placing the SL just a few ticks from entry before any real structural level — that is not a stop loss, it is a guaranteed loss. "
        f"For forex, minimum 20 pips. For stocks, minimum 0.5%. The distance should come from where the level actually is, not be invented.\n"
        f"  5. Where is the FIRST meaningful structural level in the direction of the trade? "
        f"This is your initial target — a guide, not a ceiling. You will manage the trade actively after entry. "
        f"Do not force a fixed RR. Set the target where structure suggests the market may react next.\n\n"
        f"IMPORTANT — you have TWO ways to enter a trade, not one:\n\n"
        f"  A) Price is already AT or very close to the confluence zone (within ~0.3%):\n"
        f"     → Enter at market. Use trade_type=scalp/day/swing as appropriate.\n\n"
        f"  B) Price has NOT reached the confluence zone yet — it is above (for a long) or below (for a short):\n"
        f"     → Do NOT skip just because price isn't there yet.\n"
        f"     → Set a PENDING order: entry=<confluence level>, trade_type=day or swing.\n"
        f"     → Dexter will watch and open the trade automatically when price arrives.\n"
        f"     → This is the correct response when a level looks valid but price needs to come to you.\n\n"
        f"Only SKIP if the setup itself is genuinely weak — wrong trend, poor confluence quality, no clear invalidation level.\n"
        f"Do NOT skip simply because price hasn't arrived at the level yet.\n\n"
        f"ABSOLUTE RULES — trades that break these are REJECTED and wasted:\n"
        f"  LONG:  sl MUST be a number LOWER than entry  (e.g. entry=1.0445, sl=1.038 ✓ | sl=1.047 ✗ WRONG)\n"
        f"  SHORT: sl MUST be a number HIGHER than entry (e.g. entry=1.0445, sl=1.055 ✓ | sl=1.040 ✗ WRONG)\n"
        f"  LONG:  tp MUST be a number HIGHER than entry. SHORT: tp MUST be lower.\n"
        f"  SL goes at the structural invalidation level — where your trade idea is proven wrong. "
        f"The R:R that results from that is what it is. Do NOT invent an SL distance to hit a ratio target.\n"
        f"  That said — be aware: if the nearest structure gives you R:R below 1:1 (risking more than you make), "
        f"that is a signal the setup is weak, not that you should force a tighter SL.\n"
        f"  Do NOT enter just because confluence exists — require price action confirmation:\n"
        f"    a bullish candle close ABOVE the level (for longs), or bearish close BELOW (for shorts).\n"
        f"    Touching a level is not confirmation. A candle close through it is.\n\n"
        f"SIP — Stop in Profit (trade management concept):\n"
        f"  When Dexter asks you to manage an open trade, you may trail your SL above your entry (for LONG)\n"
        f"  or below your entry (for SHORT). Once SL crosses entry, it is NO LONGER an SL — it is a SIP.\n"
        f"  SIP = Stop in Profit. The trade CANNOT close at a loss. Your minimum exit is guaranteed profit.\n"
        f"  SIP ratchet rule: once SIP is active, it can ONLY move further in the direction of the trade.\n"
        f"    LONG SIP: can only move higher. SHORT SIP: can only move lower. Never backwards.\n"
        f"  When Dexter sends a checkpoint with SIP active, use TRAIL_SIP: [price] instead of TRAIL_SL.\n"
        f"  Once SIP is active, you no longer need to fear a stop out — focus on maximising profit.\n\n"
        f"CONFLUENCE SCORING — your trade must reach the minimum score or Dexter will REJECT it:\n"
        f"  Threshold: {'10' if asset_type == 'crypto' else '7'} points ({'crypto' if asset_type == 'crypto' else 'forex/stock'})\n\n"
        f"  Timeframe-weighted tools — always append the timeframe suffix to these tags:\n"
        f"    sr_4h=4  sr_1h=3  sr_30m=2  sr_15m=1\n"
        f"    fib_4h=3  fib_1h=2  fib_30m=2  fib_15m=1\n"
        f"    rsi_4h=4  rsi_1h=4  rsi_30m=3  rsi_15m=2   (confirmed RSI divergence)\n"
        f"    rsi_form_4h=2  rsi_form_1h=1.5  rsi_form_30m=1  rsi_form_15m=1   (forming divergence — Dexter reports score in context)\n"
        f"    ema_55=3  ema_21=2  ema_13=1   (tag by EMA period — EMA55 is structurally strongest)\n"
        f"    bb_4h=3  bb_1h=2  bb_30m=2  bb_15m=1   (Bollinger Band signal — see BB rules below)\n"
        f"    tri_4h=3  tri_1h=2  tri_30m=1  tri_15m=1\n\n"
        f"  Timeframe-agnostic tools (no suffix needed):\n"
        f"    gp=4  (Golden Pocket 0.618-0.65)\n"
        f"    vp=3  (Volume Profile POC/VAH/VAL)\n"
        f"    vw=3  (VWAP)\n"
        f"    dv=3  (Divergence, non-RSI)\n"
        f"    cp=1  (Candle Pattern — confirmation only)\n"
        f"    rsi_ob=0.5  (RSI >80 overbought)  rsi_os=0.5  (RSI <20 oversold)\n"
        f"    rsi_50=0.5  (RSI crossed 50 within last 3 bars — momentum shift)\n\n"
        f"  Example: tags=sr_4h,rsi_1h,fib_1h scores 4+4+2=10 — valid for crypto.\n"
        f"  Example: tags=sr_15m,fib_15m,ema_15m scores 1+1+1=3 — REJECTED.\n"
        f"  Count your score before writing POST:. If below threshold, write SKIP: instead.\n"
        f"  Always check 4H structure first — a 4H SR or Fib in your stack is worth much more than a 15m one.\n\n"
        f"BOLLINGER BAND RULES — how to read and use the BB data in the market brief:\n"
        f"  The brief shows: BB upper / mid / lower, band width %, %B position, and squeeze status.\n"
        f"  %B is a universal position reading — 1.0 = at upper band, 0.5 = at mid line, 0.0 = at lower band.\n"
        f"  It is self-normalising: the same %B threshold means the same structural position on any pair.\n\n"
        f"  BURST (strongest signal — %B > 1.0 or %B < 0.0):\n"
        f"    Price has closed OUTSIDE the band. This is overextension — the market has pushed too far.\n"
        f"    Expect mean reversion BACK INSIDE the band. This is a fade/reversal signal, not a breakout signal.\n"
        f"    BURST above upper band (%B > 1.0) → bias SHORT (fade the move)\n"
        f"    BURST below lower band (%B < 0.0) → bias LONG (fade the move)\n"
        f"    Tag this as bb_Xtf (e.g. bb_1h if 1H chart). No visual anchor needed.\n\n"
        f"  NEAR BAND (approaching signal — %B > 0.85 or %B < 0.15):\n"
        f"    Price is in the top or bottom 15% of the current band — approaching the edge.\n"
        f"    This is not yet a confirmed reversal, but you are entering the danger zone.\n"
        f"    Only tag as bb_Xtf if it coincides with a structural level (SR, Fib, EMA) — do not trade band touch alone.\n\n"
        f"  MID LINE (dynamic support/resistance — %B near 0.5):\n"
        f"    The blue middle line (20 SMA) acts as support when price is above it, resistance when below.\n"
        f"    A rejection or bounce off the mid line confirms directional bias. Tag as bb_Xtf if mid confirms your SR.\n\n"
        f"  SQUEEZE (big move warning — band width < 1.5%):\n"
        f"    Bands are compressed. A directional explosion is coming — do NOT predict which way.\n"
        f"    Wait for the burst direction THEN trade the mean reversion back in, or wait for BB to expand with clear direction.\n"
        f"    Do not enter a trade during a squeeze unless other confluences strongly confirm the direction.\n\n"
        f"  KEY RULE: BB signals are strongest when they COMBINE with SR, Fib, or divergence at the same level.\n"
        f"  A burst through the band AT a 4H resistance zone is a high-conviction SHORT. Alone, it is just a signal.\n\n"
        f"If you're posting a trade:\n"
        f"  POST: <your read in MAX 15 words — why this level, why now. No fluff.>\n"
        f"  TRADE: direction=long, entry=X, sl=Y, tp=Z, risk_pct=N, leverage=N, position_size_usd=N, tags=sr_4h,fib_1h,rsi, trade_type=day\n\n"
        f"VISUAL ANCHORS — for each tagged visual tool, append exact anchor fields to the TRADE: line.\n"
        f"These values are drawn on the chart so Kev can see exactly what YOU are seeing:\n"
        f"  Tagged sr or sr_Xtf  → add  sr_level=<your support or resistance price>\n"
        f"  Tagged fib or fib_Xtf → add  fib_high=<swing high price>, fib_low=<swing low price>\n"
        f"  Tagged vp             → add  vp_start=<YYYY-MM-DD of the first candle of your profile range>\n"
        f"  Tagged rsi or rsi_Xtf → add  rsi_div_t1=<YYYY-MM-DD of older RSI swing pivot>, rsi_div_t2=<YYYY-MM-DD of newer RSI swing pivot>\n"
        f"    (The system draws your RSI divergence line on the RSI panel AND the matching price trendline on the chart)\n"
        f"  Tagged ms or ms_Xtf  → no anchor needed. The chart auto-fetches and overlays the higher TF candles as transparent boxes.\n"
        f"    Use ms_4h when the 4H candle structure is a confluence, ms_1d for daily structure, ms_1h for 1H structure.\n"
        f"If anchors are missing for a tagged tool, it will not be drawn on the chart.\n"
        f"Example: TRADE: direction=long, entry=1.2340, sl=1.2280, tp=1.2500, ..., tags=sr_4h,fib_1h,rsi_1h, trade_type=day, sr_level=1.2300, fib_high=1.2650, fib_low=1.2100, rsi_div_t1=2024-06-01, rsi_div_t2=2024-06-15\n\n"
        f"REASONING — after your TRADE: line, on a new line write:\n"
        f"  REASONING: <your full analysis — no word limit. Why this setup, what the chart structure shows, what could invalidate it, what you're watching.>\n"
        f"This is stored for learning sessions and never censored for length. The POST: summary stays 15 words — REASONING is where you think out loud.\n\n"
        f"If not convinced: start with SKIP: <one sentence on why>\n\n"
        f"ACCOUNT & RISK STATUS:\n"
        f"  Balance: ${balance:.2f} (updates only when trades CLOSE at TP or SL)\n"
        f"  {heat_context}\n\n"
        f"SETUP GRADE: {grade}  |  Suggested risk_pct: {suggested_risk}%\n"
        f"  Score {result['count']} vs threshold {min_score} | "
        f"Regime: {(result.get('regime_4h') or {}).get('regime', '?')} | "
        f"Session: {session_quality}\n"
        f"  Grade scale: B → 0.75%  |  A → 1.5%  |  A+ → 2.5%\n"
        f"  This is a data-backed suggestion. Override it if you have stronger conviction — or size down "
        f"if heat and correlation concerns apply. The market only gives you clean A+ setups rarely — "
        f"when it does, don't treat it like a B.\n\n"
        f"Crypto leverage max 10x (swing: 5x). Forex/stock max 5x (swing: 2x). "
        f"Only push leverage on A+ setups in peak session. "
        f"trade_type = scalp (~2h expiry), day (~6h), swing (~48h).\n"
        f"Reminder: first word of your reply = POST or SKIP. No exceptions."
    )
    messages = [{"role": "user", "content": context_prefix + msg}]
    reply = _call_chev(messages, timeout=timeout)

    # If the reply didn't start with POST:/SKIP:, remind Chev in the same thread and retry once
    if reply:
        has_directive = any(
            l.strip().upper().startswith("POST:") or l.strip().upper().startswith("SKIP:")
            for l in reply.split("\n")
        )
        if not has_directive:
            print(f"[{datetime.now()}] Chev's reply was missing POST:/SKIP: — sending format reminder and retrying.")
            if dashboard_ws:
                increment_malformed_count(dashboard_ws)
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                "FORMAT ERROR — your reply had no POST: or SKIP: line.\n"
                "Do not repeat your analysis. Do not explain. Just answer:\n"
                "  POST: <15-word reason>   ← taking the trade\n"
                "  SKIP: <one reason>       ← passing\n"
                "Your very first word must be POST or SKIP. Nothing before it."
            )})
            reply = _call_chev(messages, timeout=timeout)

    return reply

def _clean_numeric(value):
    """Pulls the first real number out of a field, so '5x' or '162.35 (5x leverage)' still parses correctly."""
    match = re.search(r"-?\d+\.?\d*", value)
    if not match:
        raise ValueError(f"No numeric value found in: {value}")
    return float(match.group())


def parse_chev_reply(text):
    if not text:
        return None
    lines = text.strip().split("\n")

    result = {"post": False, "telegram_message": None, "trade": None}

    # Scan all lines — reasoning models sometimes add a preamble before POST:/SKIP:
    directive_line = None
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("POST:") or stripped.upper().startswith("SKIP:"):
            directive_line = stripped
            break

    if directive_line is None:
        print(f"[{datetime.now()}] parse_chev_reply: no POST:/SKIP: found in reply. Full text:\n{text[:400]}")
        return None

    if directive_line.upper().startswith("POST:"):
        result["post"] = True
        result["telegram_message"] = directive_line.split(":", 1)[1].strip()
    else:
        result["post"] = False
        result["skip_reason"] = directive_line.split(":", 1)[1].strip() if ":" in directive_line else directive_line

    if result["post"]:
        trade_line = None
        for line in lines[1:]:
            if line.strip().upper().startswith("TRADE:"):
                trade_line = line.strip()[6:].strip()
                break
        if trade_line:
            fields = {}
            parts = re.split(r',\s*(?=[a-zA-Z_]+\s*=)', trade_line)
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    fields[k.strip().lower()] = v.strip()
            try:
                trade_type = fields.get("trade_type", "day").lower()
                if trade_type not in TRADE_TYPE_EXPIRY_HOURS:
                    trade_type = "day"
                result["trade"] = {
                    "direction": fields.get("direction", "long").lower(),
                    "entry": _clean_numeric(fields["entry"]),
                    "sl": _clean_numeric(fields["sl"]),
                    "tp": _clean_numeric(fields["tp"]),
                    "risk_pct": _clean_numeric(fields["risk_pct"]),
                    "leverage": _clean_numeric(fields["leverage"]),
                    "position_size_usd": _clean_numeric(fields["position_size_usd"]),
                    "tags": fields.get("tags", "").lower(),
                    "trade_type": trade_type,
                    # Visual anchors — Chev's specific chart reference points
                    "chev_sr_level":  _clean_numeric(fields["sr_level"])  if fields.get("sr_level")  else None,
                    "chev_fib_high":  _clean_numeric(fields["fib_high"])  if fields.get("fib_high")  else None,
                    "chev_fib_low":   _clean_numeric(fields["fib_low"])   if fields.get("fib_low")   else None,
                    "chev_vp_start":  fields.get("vp_start", "").strip()  if fields.get("vp_start")  else None,
                    "chev_rsi_div_t1": fields.get("rsi_div_t1", "").strip() if fields.get("rsi_div_t1") else None,
                    "chev_rsi_div_t2": fields.get("rsi_div_t2", "").strip() if fields.get("rsi_div_t2") else None,
                    "reasoning": None,  # populated below after TRADE: line parse
                }
            except (KeyError, ValueError):
                result["trade"] = None

        # Parse REASONING: line (may appear on any line after POST:/TRADE:)
        reasoning = None
        for line in lines:
            s = line.strip()
            if s.upper().startswith("REASONING:"):
                reasoning = s.split(":", 1)[1].strip()
                break
        if result["trade"] is not None and reasoning:
            result["trade"]["reasoning"] = reasoning

            # Hard validation: SL/TP must be on the correct side of entry
            if result["trade"]:
                t = result["trade"]
                _is_long = t["direction"] == "long"
                _e, _sl, _tp = t["entry"], t["sl"], t["tp"]
                _reject = None
                if _is_long  and _sl >= _e:  _reject = f"LONG SL {_sl} must be BELOW entry {_e}"
                if _is_long  and _tp <= _e:  _reject = f"LONG TP {_tp} must be ABOVE entry {_e}"
                if not _is_long and _sl <= _e: _reject = f"SHORT SL {_sl} must be ABOVE entry {_e}"
                if not _is_long and _tp >= _e: _reject = f"SHORT TP {_tp} must be BELOW entry {_e}"
                if _reject:
                    print(f"[{datetime.now()}] TRADE REJECTED — bad SL/TP: {_reject}")
                    result["trade"] = None

    return result

def _is_sip(trade):
    """Return True if the trade's SL has crossed above entry (LONG) or below entry (SHORT) — Stop in Profit."""
    is_long = trade["direction"] == "long"
    return (is_long and trade["sl"] > trade["entry"]) or (not is_long and trade["sl"] < trade["entry"])


def _build_management_brief(symbol, asset_type, primary_tf, direction, entry, tp):
    """Compact self-contained market snapshot for management checkpoints.
    Chev has no memory between calls — everything he needs must be in this block."""
    try:
        df = fetch_candles(symbol, asset_type, primary_tf, limit=100)
        df = _ca_add_indicators(df)
        df = add_rsi(df)
        cur = float(df["close"].iloc[-1])
        is_long = direction == "long"

        rsi_val   = float(df["RSI"].iloc[-1]) if "RSI" in df.columns and not pd.isna(df["RSI"].iloc[-1]) else None
        rsi_str   = f"{round(rsi_val, 1)}" if rsi_val else "N/A"
        rsi_note  = " — OVERBOUGHT" if rsi_val and rsi_val > 70 else (" — OVERSOLD" if rsi_val and rsi_val < 30 else "")

        atr_s     = _an_atr_series(df)
        cur_atr   = float(atr_s.iloc[-1])
        avg_atr   = float(atr_s.tail(20).mean())
        ratio     = round(cur_atr / avg_atr, 2) if avg_atr else 1.0
        atr_state = "QUIET" if ratio < 0.7 else "VOLATILE" if ratio > 1.3 else "normal"

        recent_vol = df["volume"].tail(5).mean()
        prev_vol   = df["volume"].iloc[-15:-5].mean()
        vol_trend  = "INCREASING" if recent_vol > prev_vol * 1.1 else ("DECREASING" if recent_vol < prev_vol * 0.9 else "flat")

        last20 = df.tail(20)
        candle_lines = []
        for idx, row in last20.iterrows():
            ts_str = idx.strftime('%H:%M') if hasattr(idx, 'strftime') else str(idx)[-5:]
            candle_lines.append(
                f"  {ts_str}  O:{row['open']:.5f}  H:{row['high']:.5f}"
                f"  L:{row['low']:.5f}  C:{row['close']:.5f}  V:{int(row['volume'])}"
            )

        touches   = _an_get_timed_touches(df)
        swing_highs = sorted([p for _, p, k in touches if k == "resistance"], reverse=True)
        swing_lows  = sorted([p for _, p, k in touches if k == "support"])

        res_above = [p for p in swing_highs if p > cur][:3]
        sup_below = [p for p in swing_lows  if p < cur][-3:][::-1]

        sl_candidates = sup_below if is_long else res_above
        tp_candidates = res_above if is_long else sup_below
        sl_label = "swing lows below (candidate SL/floor levels)" if is_long else "swing highs above (candidate SL/floor levels)"
        tp_label = "swing highs above (candidate TP targets)"      if is_long else "swing lows below (candidate TP targets)"

        dist_to_tp = round(abs(cur - tp) / cur * 100, 3)
        dist_note  = "to go" if (is_long and cur < tp) or (not is_long and cur > tp) else "PASSED"

        lines = [
            f"=== LIVE MARKET SNAPSHOT — {symbol} {primary_tf.upper()} ===",
            f"Current price : {cur:.5f}",
            f"Entry         : {entry}",
            f"RSI           : {rsi_str}{rsi_note}",
            f"ATR (current) : {round(cur_atr, 5)}  state: {atr_state} ({ratio}× avg) — 0.5× ATR = {round(cur_atr * 0.5, 5)}",
            f"Volume trend  : {vol_trend} (last 5 bars vs prior 10)",
            f"",
            f"Last 20 candles ({primary_tf}):",
        ] + candle_lines + [
            f"",
            f"Recent structure:",
            f"  {sl_label}: {[round(p,5) for p in sl_candidates] or ['none identified']}",
            f"  {tp_label}: {[round(p,5) for p in tp_candidates] or ['none identified']}",
            f"",
            f"Current TP : {tp}  ({dist_to_tp}% {dist_note})",
        ]
        try:
            anchor_info = _detect_auction_anchor(df)
            if anchor_info:
                anchor_dt = df.index[anchor_info["idx"]]
                candles_ago = len(df) - 1 - anchor_info["idx"]
                active = anchor_info.get("active", True)
                confirmed = anchor_info.get("confirmed", False)
                why = anchor_info.get("invalidation_reason", "structure broken")
                if active:
                    a_status = "ACTIVE + CONFIRMED" if confirmed else "ACTIVE (unconfirmed)"
                else:
                    a_status = f"STALE — {why}"
                lines += [
                    f"",
                    f"Auction Anchor : {anchor_info['price']:.5f}  [{anchor_info['anchor_type'].replace('_',' ')}]",
                    f"  Set         : {anchor_dt.strftime('%Y-%m-%d %H:%M')} ({candles_ago} bars ago)",
                    f"  Method      : {anchor_info['method']}  |  Confidence: {anchor_info['confidence']}%",
                    f"  Status      : {a_status}",
                    f"  Note        : SR levels near the anchor carry structural weight. SL/TP decisions should account for this origin.",
                ]
        except Exception:
            pass
        return "\n".join(lines)
    except Exception as e:
        return f"[Market snapshot unavailable: {e}]"


def ask_chev_manage_trade(trade, current_price):
    """Ask Chev to manage an open trade — trail SL/SIP, close, or hold."""
    is_long    = trade["direction"] == "long"
    sip_active = trade.get("sip_active") or _is_sip(trade)
    orig_sl    = trade.get("original_sl", trade["sl"] if not sip_active else trade["entry"])
    orig_risk  = abs(trade["entry"] - orig_sl)
    tp_range   = abs(trade["tp"] - trade["entry"])
    tp_progress = round((abs(current_price - trade["entry"]) / tp_range) * 100, 0) if tp_range > 0 else 0

    symbol     = trade["symbol"]
    primary_tf = trade.get("primary_tf", "1h")
    asset_type = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")
    market_brief = _build_management_brief(symbol, asset_type, primary_tf, trade["direction"], trade["entry"], trade["tp"])

    tool_instructions = (
        f"TOOLS — call these before deciding. You have NO memory of this trade's original setup.\n"
        f"Form a fresh, data-backed view RIGHT NOW using live data:\n\n"
        f"  get_support_resistance(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → For Stop placement: find the nearest confirmed {'support' if is_long else 'resistance'} level\n"
        f"      {'below' if is_long else 'above'} current price — place your stop just {'below' if is_long else 'above'} it,\n"
        f"      leaving at least 0.5× ATR of breathing room above that level.\n"
        f"    → For TP: find the nearest confirmed {'resistance' if is_long else 'support'} level\n"
        f"      {'above' if is_long else 'below'} current price — a valid new TP must sit AT or just before it.\n\n"
        f"  get_volume_profile(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → Is price near POC, VAH, or VAL? Clusters cause reversals or act as magnets.\n"
        f"      Volume INCREASING into the move = momentum. Volume FADING = exhaustion.\n\n"
        f"  detect_rsi_divergence(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → Hidden {'bullish' if is_long else 'bearish'} divergence = continuation likely.\n"
        f"      Regular {'bearish' if is_long else 'bullish'} divergence = reversal warning — tighten stop or close.\n\n"
        f"Call these tools. Base every decision on what they show — not on memory or assumptions.\n\n"
    )

    _breathe = round(0.5 * tp_range, 5) if tp_range else '?'

    if sip_active:
        msg = (
            f"Dexter checkpoint — {symbol} {trade['direction'].upper()} — PROFIT FLOOR ACTIVE.\n\n"
            f"{market_brief}\n\n"
            f"CURRENT STATUS:\n"
            f"  Entry:          {trade['entry']}\n"
            f"  Profit floor:   {trade['sl']}  ← your stop is {'above' if is_long else 'below'} entry — this trade CANNOT close at a loss\n"
            f"  Current price:  {current_price:.5f}  ({tp_progress:.0f}% toward TP)\n"
            f"  TP target:      {trade['tp']}\n"
            f"  Live PnL:       ${trade.get('live_pnl', 0):+.2f}\n\n"
            f"{tool_instructions}"
            f"YOUR TWO INDEPENDENT LEVERS — decide each one separately:\n\n"
            f"  LEVER 1 — PROFIT FLOOR (currently {trade['sl']}):\n"
            f"    Your call: should the floor move {'higher' if is_long else 'lower'} to lock in more profit?\n"
            f"    Look at the recent swing {'lows' if is_long else 'highs'} in the snapshot above and from get_support_resistance.\n"
            f"    Place the floor just {'above' if is_long else 'below'} the last confirmed swing {'low' if is_long else 'high'},\n"
            f"    leaving at least 0.5× ATR ({_breathe} approx) of breathing room.\n"
            f"    Floor can only move {'higher' if is_long else 'lower'} — never back toward entry.\n"
            f"    You may leave floor unchanged if structure has not shifted.\n\n"
            f"  LEVER 2 — TP TARGET (currently {trade['tp']}):\n"
            f"    Your call: is there a specific structural level {'above' if is_long else 'below'} the current TP worth targeting?\n"
            f"    Only raise TP if: momentum is accelerating AND you can name the next structural level.\n"
            f"    If you cannot name it — keep TP. Let it hit. Bank the profit. Move on.\n"
            f"    If raising TP, append REASON: <one sentence — what structure, what confluence> so it is logged.\n\n"
            f"Reply on ONE line using any valid combination:\n"
            f"  TRAIL_SIP: [floor price]                              ← floor only\n"
            f"  TP: [new price] REASON: [why]                         ← TP only\n"
            f"  TRAIL_SIP: [floor price] TP: [new price] REASON: [why] ← both\n"
            f"  HOLD                                                   ← leave everything as-is\n"
            f"  CLOSE                                                  ← exit now at market (only if clear reversal)\n\n"
            f"Floor constraint: must be {'above ' + str(trade['sl']) + ' and below ' + str(round(current_price,5)) if is_long else 'below ' + str(trade['sl']) + ' and above ' + str(round(current_price,5))}."
        )
    else:
        at_50_pct = tp_progress >= 50
        halfway_to_tp = round(trade["entry"] + (trade["tp"] - trade["entry"]) * 0.5, 6)
        msg = (
            f"Dexter checkpoint — {symbol} {trade['direction'].upper()} — trade is running.\n\n"
            f"{market_brief}\n\n"
            f"CURRENT STATUS:\n"
            f"  Entry:         {trade['entry']}\n"
            f"  Stop Loss:     {trade['sl']}  ← {'below' if is_long else 'above'} entry — a stop-out here closes at a LOSS\n"
            f"  Current price: {current_price:.5f}  ({tp_progress:.0f}% toward TP)\n"
            f"  TP target:     {trade['tp']}\n"
            f"  Live PnL:      ${trade.get('live_pnl', 0):+.2f}\n\n"
        )
        if at_50_pct:
            msg += (
                f"NOTE — price is {tp_progress:.0f}% toward TP. Your Stop Loss is still at {trade['sl']}.\n"
                f"If the trade reverses now, you close at a loss. Consider whether the structure\n"
                f"justifies protecting this profit by moving SL closer to entry.\n\n"
            )
        msg += (
            f"{tool_instructions}"
            f"YOUR TWO INDEPENDENT LEVERS — decide each one separately:\n\n"
            f"  LEVER 1 — STOP LOSS (currently {trade['sl']}):\n"
            f"    Your call: should the SL move to better protect this trade?\n"
            f"    Look at the recent swing {'lows' if is_long else 'highs'} in the snapshot and from get_support_resistance.\n"
            f"    Place SL just {'below' if is_long else 'above'} a confirmed swing {'low' if is_long else 'high'},\n"
            f"    leaving at least 0.5× ATR of breathing room so normal movement doesn't trigger it.\n"
            f"    If you move SL to your entry ({trade['entry']}), the trade breaks even if stopped.\n"
            f"    If you move SL {'above' if is_long else 'below'} your entry, any stop-out closes in profit.\n"
            f"    You may leave SL unchanged if the original structural level still holds.\n\n"
            f"  LEVER 2 — TP TARGET (currently {trade['tp']}):\n"
            f"    Your call: is there a specific structural level {'above' if is_long else 'below'} the current TP worth targeting?\n"
            f"    Only raise TP if: momentum is accelerating AND you can name the next structural level.\n"
            f"    If you cannot name it — keep TP. Let it hit. Bank the profit. Move on.\n"
            f"    If raising TP, append REASON: <one sentence — what structure, what confluence> so it is logged.\n\n"
            f"Reply on ONE line using any valid combination:\n"
            f"  TRAIL_SL: [price]                                     ← SL only\n"
            f"  TP: [new price] REASON: [why]                         ← TP only\n"
            f"  TRAIL_SL: [price] TP: [new price] REASON: [why]       ← both\n"
            f"  HOLD                                                   ← leave everything as-is\n"
            f"  CLOSE                                                  ← exit now at market (only if clear reversal)\n\n"
            f"SL constraint: must be {'below current price ' + str(round(current_price,5)) if is_long else 'above current price ' + str(round(current_price,5))}."
        )
    playbook_text = _load_playbook()
    content = (f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else "") + msg
    return _call_chev([{"role": "user", "content": content}], timeout=60)


def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    response = requests.post(url, data=payload, timeout=15)
    if response.status_code != 200:
        print(f"[{datetime.now()}] Telegram send failed: {response.text}")


def _fmt_p(price):
    """Format a price cleanly for Telegram — no trailing zeros, commas on large numbers."""
    if price is None:
        return "—"
    if price >= 10000:
        return f"{price:,.0f}"
    if price >= 1000:
        return f"{price:,.2f}".rstrip("0").rstrip(".")
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"{price:.5f}".rstrip("0").rstrip(".")
    return f"{price:.8f}".rstrip("0").rstrip(".")


def _format_trade_signal(symbol, trade, chev_take=""):
    """
    Format a trade signal in Chev's voice — 3 clean lines, no fluff.
    Optionally appends Chev's one-sentence take if it's short enough.
    """
    direction  = trade.get("direction", "long").capitalize()
    entry      = trade.get("entry", 0)
    sl         = trade.get("sl", 0)
    tp         = trade.get("tp", 0)
    trade_type = trade.get("trade_type", "day")
    tags       = " · ".join(t.strip() for t in trade.get("tags", "").split(",") if t.strip())
    line3      = f"{trade_type}. {tags}." if tags else f"{trade_type}."
    msg = f"{symbol}. {direction}.\nEntry {_fmt_p(entry)} · SL {_fmt_p(sl)} · TP {_fmt_p(tp)}\n{line3}"
    if chev_take and len(chev_take) <= 120:
        msg += f"\n{chev_take}"
    return msg


# ── Economic calendar ────────────────────────────────────────────────────────

_calendar_cache:      list  = []
_calendar_last_fetch: float = 0.0

def _fetch_economic_calendar():
    global _calendar_cache, _calendar_last_fetch
    if time.time() - _calendar_last_fetch < 3600:
        return _calendar_cache
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"}
        )
        _calendar_cache = resp.json()
        _calendar_last_fetch = time.time()
        print(f"[Calendar] Fetched {len(_calendar_cache)} events.")
    except Exception as e:
        print(f"[Calendar] Fetch failed: {e}")
    return _calendar_cache

def _parse_ff_date(date_str):
    import re
    try:
        fixed = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', date_str)
        return datetime.fromisoformat(fixed).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None

def _upcoming_high_impact(symbol, asset_type, window_hours=2):
    """Return warning strings for high-impact events within window_hours for this symbol's currencies."""
    if asset_type != "forex":
        return []
    clean = symbol.upper().replace("/", "")
    currencies = {clean[:3], clean[3:6]} if len(clean) >= 6 else set()
    events = _fetch_economic_calendar()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    warnings = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        if ev.get("country", "").upper() not in currencies:
            continue
        ev_time = _parse_ff_date(ev.get("date", ""))
        if ev_time is None:
            continue
        diff_h = (ev_time - now_utc).total_seconds() / 3600
        if -0.5 <= diff_h <= window_hours:
            label = f"{ev.get('country','?')} {ev.get('title','event')}"
            if diff_h < 0:
                warnings.append(f"⚠ {label} just released ({abs(diff_h)*60:.0f}min ago)")
            else:
                warnings.append(f"⚠ {label} in {diff_h:.1f}h")
    return warnings


def _forex_event_block(symbol, asset_type, block_minutes=30):
    """Return (True, reason) if a high-impact event is within block_minutes of now for this forex pair.
    Used as a hard no-trade gate — Dexter won't escalate to Chev during this window."""
    if asset_type != "forex":
        return False, ""
    clean = symbol.upper().replace("/", "")
    currencies = {clean[:3], clean[3:6]} if len(clean) >= 6 else set()
    events = _fetch_economic_calendar()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    block_hours = block_minutes / 60
    for ev in events:
        if ev.get("impact") != "High":
            continue
        if ev.get("country", "").upper() not in currencies:
            continue
        ev_time = _parse_ff_date(ev.get("date", ""))
        if ev_time is None:
            continue
        diff_min = (ev_time - now_utc).total_seconds() / 60
        if -block_minutes <= diff_min <= block_minutes:
            direction = f"in {int(diff_min)}min" if diff_min >= 0 else f"{int(abs(diff_min))}min ago"
            label = f"{ev.get('country','?')} {ev.get('title','event')}"
            return True, f"{label} ({direction})"
    return False, ""


# ── Session detection ────────────────────────────────────────────────────────

def _active_sessions():
    """Return list of currently active trading sessions based on UTC hour."""
    h = datetime.now(timezone.utc).hour
    sessions = []
    if h >= 22 or h < 9:   sessions.append("Asian")
    if 8  <= h < 17:        sessions.append("London")
    if 13 <= h < 22:        sessions.append("New York")
    return sessions or ["Asian"]

def _session_confluence_bonus(asset_type):
    """
    Returns an int added to the minimum confluence threshold.
    +1 during thin Asian session for forex (harder to trust signals).
    -1 during London/NY overlap (most liquid, signals more reliable).
    """
    if asset_type != "forex":
        return 0
    sessions = _active_sessions()
    if "London" in sessions and "New York" in sessions:
        return -1  # overlap — very liquid
    if sessions == ["Asian"]:
        return 1   # thin market — raise the bar
    return 0


# ============================================================
# GOOGLE SHEETS - TRADE JOURNAL
# ============================================================

def connect_to_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)
    trade_log_ws = sheet.worksheet(TRADE_LOG_TAB)
    dashboard_ws = sheet.worksheet("Dashboard")
    try:
        jane_ws = sheet.worksheet(JANE_TAB)
    except gspread.exceptions.WorksheetNotFound:
        jane_ws = sheet.add_worksheet(title=JANE_TAB, rows=200, cols=20)
        jane_ws.append_row([
            "Pair", "Direction", "Entry", "SL", "TP", "Risk%", "Leverage",
            "Position USD", "Margin USD", "Tags", "Live Price", "Live PnL",
            "Status", "Result $", "Timestamp", "Trade Type", "Expiry",
            "Conf Prices", "Trigger Above",
        ])
        print("[Jane] Created 'Jane' worksheet in Google Sheet.")
    return trade_log_ws, dashboard_ws, jane_ws

def get_balance(dashboard_ws):
    try:
        val = dashboard_ws.acell("B1").value
        if val is None or str(val).strip() == "":
            print(f"[{datetime.now()}] WARNING: Balance cell B1 is empty — using last cached value ${_cached_balance:.2f}")
            return _cached_balance
        return float(val)
    except Exception as e:
        print(f"[{datetime.now()}] WARNING: Couldn't read balance — using last cached ${_cached_balance:.2f}: {e}")
        return _cached_balance


def set_balance(dashboard_ws, new_balance):
    try:
        dashboard_ws.update_acell("B1", new_balance)
    except Exception as e:
        print(f"[{datetime.now()}] Couldn't write new balance to Dashboard: {e}")


def increment_malformed_count(dashboard_ws):
    try:
        val = dashboard_ws.acell("B17").value
        count = int(val) if val and str(val).strip().lstrip("-").isdigit() else 0
        dashboard_ws.update_acell("B17", count + 1)
        print(f"[{datetime.now()}] Malformed reply count updated: {count + 1}")
    except Exception as e:
        print(f"[{datetime.now()}] Couldn't update malformed reply count on Dashboard: {e}")


def load_state_from_sheet(worksheet):
    rows = worksheet.get_all_values()
    if len(rows) <= 1:
        return []

    loaded_open = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 13:
            continue
        pair, direction, entry, sl, tp, risk_pct, leverage, position_size, margin_used, tags, live_price, live_pnl, status = row[:13]

        status_clean = status.strip().upper()
        if status_clean in ("OPEN", "PENDING"):
            try:
                asset_type = "crypto" if pair.endswith("USDT") else ("forex" if "/" in pair else "stock")
                trade_entry = {
                    "row": i,
                    "symbol": pair,
                    "asset_type": asset_type,
                    "direction": direction.lower(),
                    "entry": float(entry),
                    "sl": float(sl),
                    "tp": float(tp),
                    "risk_pct": float(risk_pct),
                    "leverage": float(leverage),
                    "position_size_usd": float(position_size),
                    "tags": tags,
                    "status": status_clean,
                }
                try:
                    trade_entry["margin_reserved"] = float(margin_used) if margin_used else round(float(position_size) / max(float(leverage), 1), 2)
                except Exception:
                    trade_entry["margin_reserved"] = round(float(position_size) / max(float(leverage), 1), 2)
                trade_entry.setdefault("chev_moves", [])
                trade_entry.setdefault("milestones_hit", set())
                trade_entry["open_ts"]    = row[14] if len(row) > 14 and row[14] else ""
                trade_entry["trade_type"] = (row[15].lower() if len(row) > 15 and row[15] else "day")
                try:
                    trade_entry["confluence_prices"] = json.loads(row[17]) if len(row) > 17 and row[17] else {}
                except Exception:
                    trade_entry["confluence_prices"] = {}
                # Detect SIP state on load (SL already above entry from a previous trail)
                _is_long_load = direction.lower() == "long"
                _sl_val = float(sl)
                _en_val = float(entry)
                if (_is_long_load and _sl_val > _en_val) or (not _is_long_load and _sl_val < _en_val):
                    trade_entry["sip_active"] = True
                    trade_entry["sip_price"]  = _sl_val
                    print(f"[SIP] {pair} loaded with SIP at {_sl_val} — trade cannot lose.")
                if status_clean == "PENDING":
                    trade_entry["expiry_at"] = row[16] if len(row) > 16 else (datetime.now() + pd.Timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                    if len(row) > 18 and row[18] in ("True", "False"):
                        # Stored at order creation — safe across restarts
                        trade_entry["entry_trigger_above"] = row[18] == "True"
                    else:
                        # Fallback for older rows: LONGs wait for price to DROP to entry,
                        # SHORTs wait for price to RISE to entry (covers the common limit-order case)
                        trade_entry["entry_trigger_above"] = direction.lower() == "short"
                loaded_open.append(trade_entry)
            except ValueError:
                continue

    return loaded_open


# ── Jane helpers ─────────────────────────────────────────────────────────────

def _load_jane_balance():
    global jane_balance
    try:
        r = requests.get(f"{FIREBASE_URL}/jane_balance.json", timeout=5)
        if r.ok:
            val = r.json()
            if val is not None:
                jane_balance = float(val)
                return
    except Exception:
        pass
    jane_balance = JANE_STARTING_BALANCE

def _save_jane_balance():
    try:
        requests.put(f"{FIREBASE_URL}/jane_balance.json", json=jane_balance, timeout=5)
    except Exception:
        pass

def _load_jane_journal():
    # Prefer local JSON (richer data: chev_verdict etc); fall back to Sheets
    try:
        with open(JANE_JOURNAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[Jane journal] JSON load failed: {e}")
    try:
        rows = jane_worksheet.get_all_values()[1:]
        closed = []
        for row in rows:
            if len(row) < 14:
                continue
            if row[12].strip().upper() in ("WIN", "LOSS"):
                closed.append({
                    "symbol":     row[0],
                    "direction":  row[1],
                    "outcome":    row[12],
                    "pnl":        float(row[13]) if row[13] else 0.0,
                    "ts":         row[14] if len(row) > 14 else "",
                    "tags":       row[9],
                    "trade_type": row[15] if len(row) > 15 else "day",
                })
        return closed
    except Exception:
        return []

def load_jane_trades_from_sheet():
    rows = jane_worksheet.get_all_values()
    if len(rows) <= 1:
        return []
    loaded = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 13:
            continue
        pair, direction, entry, sl, tp, risk_pct, leverage, position_size, margin_used, tags, live_price, live_pnl, status = row[:13]
        status_clean = status.strip().upper()
        if status_clean in ("OPEN", "PENDING"):
            try:
                asset_type = "crypto" if pair.endswith("USDT") else ("forex" if "/" in pair else "stock")
                t = {
                    "row":              i,
                    "symbol":           pair,
                    "asset_type":       asset_type,
                    "direction":        direction.lower(),
                    "entry":            float(entry),
                    "sl":               float(sl),
                    "tp":               float(tp),
                    "risk_pct":         float(risk_pct),
                    "leverage":         float(leverage),
                    "position_size_usd": float(position_size),
                    "tags":             tags,
                    "status":           status_clean,
                    "open_ts":          row[14] if len(row) > 14 and row[14] else "",
                    "trade_type":       row[15].lower() if len(row) > 15 and row[15] else "day",
                }
                if status_clean == "PENDING":
                    t["expiry_at"] = row[16] if len(row) > 16 else (datetime.now() + pd.Timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                    if len(row) > 18 and row[18] in ("True", "False"):
                        t["entry_trigger_above"] = row[18] == "True"
                    else:
                        t["entry_trigger_above"] = direction.lower() == "short"
                loaded.append(t)
            except ValueError:
                continue
    return loaded

def log_jane_trade(symbol, asset_type, trade, current_price_at_creation):
    global jane_balance
    direction        = trade["direction"]
    entry            = trade["entry"]
    sl               = trade["sl"]
    tp               = trade["tp"]
    risk_pct         = trade.get("risk_pct", 1.0)
    leverage         = trade.get("leverage", 1.0)
    position_size_usd = trade.get("position_size_usd", 0.0)
    tags             = trade.get("tags", "")
    trade_type       = trade.get("trade_type", "day")

    max_lev = MAX_LEVERAGE_BY_TYPE.get(asset_type, {}).get(trade_type, 5)
    leverage = max(1, min(leverage, max_lev))
    risk_pct = max(0.5, min(risk_pct, 3.0))

    if position_size_usd <= 0:
        margin = round(jane_balance * (risk_pct / 100), 2)
        position_size_usd = round(margin * leverage, 2)

    margin_used_usd = round(position_size_usd / leverage, 2) if leverage else position_size_usd
    if margin_used_usd > jane_balance:
        position_size_usd = round(jane_balance * leverage, 2)
        margin_used_usd   = jane_balance

    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expiry_hrs = TRADE_TYPE_EXPIRY_HOURS.get(trade_type, 6)
    expiry_at  = (datetime.now() + pd.Timedelta(hours=expiry_hrs)).strftime("%Y-%m-%d %H:%M:%S")
    entry_trigger_above = entry > current_price_at_creation

    row_values = [
        symbol, direction.upper(), entry, sl, tp, risk_pct, leverage,
        position_size_usd, margin_used_usd, tags, "", "", "PENDING", "", timestamp,
        trade_type, expiry_at, "", str(entry_trigger_above),
    ]
    existing_rows = len(jane_worksheet.get_all_values())
    row_index     = existing_rows + 1
    jane_worksheet.append_row(row_values, value_input_option="USER_ENTERED")

    return {
        "row":              row_index,
        "symbol":           symbol,
        "asset_type":       asset_type,
        "direction":        direction,
        "entry":            entry,
        "sl":               sl,
        "tp":               tp,
        "risk_pct":         risk_pct,
        "leverage":         leverage,
        "position_size_usd": position_size_usd,
        "tags":             tags,
        "trade_type":       trade_type,
        "status":           "PENDING",
        "expiry_at":        expiry_at,
        "entry_trigger_above": entry_trigger_above,
    }

def check_and_update_jane_trades():
    global jane_trades, jane_balance
    still_open = []
    for trade in jane_trades:
        price = get_current_price(trade["symbol"], trade["asset_type"])
        if price is None:
            still_open.append(trade)
            continue

        if trade.get("status") == "PENDING":
            try:
                jane_worksheet.update_cell(trade["row"], 11, price)
            except Exception:
                pass
            triggered = (price >= trade["entry"]) if trade["entry_trigger_above"] else (price <= trade["entry"])
            if triggered:
                trade["status"] = "OPEN"
                margin = round(trade.get("position_size_usd", 0) / max(trade.get("leverage", 1), 1), 2)
                trade["margin_reserved"] = margin
                jane_balance = round(jane_balance - margin, 2)
                _save_jane_balance()
                try:
                    jane_worksheet.update_cell(trade["row"], 13, "OPEN")
                except Exception:
                    pass
                print(f"[Jane] {trade['symbol']} PENDING -> OPEN | Margin: ${margin:.2f} | Balance: ${jane_balance:.2f}")
            else:
                expiry_time = datetime.strptime(trade["expiry_at"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() > expiry_time:
                    try:
                        jane_worksheet.update_cell(trade["row"], 13, "EXPIRED")
                    except Exception:
                        pass
                    print(f"[Jane] {trade['symbol']} PENDING expired.")
                    continue
            still_open.append(trade)
            continue

        is_long  = trade["direction"] == "long"
        move_pct = (price - trade["entry"]) / trade["entry"] if is_long else (trade["entry"] - price) / trade["entry"]
        live_pnl = round(trade["position_size_usd"] * move_pct, 2)
        trade["live_price"] = price
        trade["live_pnl"]   = live_pnl
        try:
            jane_worksheet.update(values=[[price, live_pnl]], range_name=f"K{trade['row']}:L{trade['row']}")
        except Exception:
            pass

        hit_tp = (price >= trade["tp"]) if is_long else (price <= trade["tp"])
        hit_sl = (price <= trade["sl"]) if is_long else (price >= trade["sl"])

        if hit_tp or hit_sl:
            exit_price = trade["tp"] if hit_tp else trade["sl"]
            exit_move  = ((exit_price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - exit_price) / trade["entry"])
            exit_pnl   = round(trade["position_size_usd"] * exit_move, 2)
            outcome    = "WIN" if exit_pnl >= 0 else "LOSS"
            margin_back = trade.get("margin_reserved", 0)
            jane_balance = round(jane_balance + margin_back + exit_pnl, 2)
            _save_jane_balance()
            _jane_win_stats["wins" if outcome == "WIN" else "losses"] += 1
            try:
                jane_worksheet.update(values=[[exit_price, exit_pnl, outcome, exit_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
            except Exception:
                pass
            arrow = "+" if hit_tp else "-"
            print(f"[Jane] {arrow} {trade['symbol']} {outcome} ${exit_pnl:+.2f} | Balance: ${jane_balance:.2f}")

            # Advisory accuracy + disagreement logging
            chev_v = trade.get("chev_verdict", "")
            if chev_v and chev_v != "UNKNOWN":
                key = f"{chev_v.lower()}_{'wins' if outcome == 'WIN' else 'losses'}"
                if key in _advisory_accuracy:
                    _advisory_accuracy[key] += 1

                disagree_msg = ""
                if chev_v == "REJECTED" and outcome == "WIN":
                    disagree_msg = (
                        f"📝 [Chev/Jane disagree] Chev REJECTED {trade['symbol']} but Jane WON ${exit_pnl:+.2f}. "
                        f"Chev's feedback: {trade.get('chev_feedback','—')}"
                    )
                elif chev_v == "APPROVED" and outcome == "LOSS":
                    disagree_msg = (
                        f"📝 [Chev/Jane disagree] Chev APPROVED {trade['symbol']} but Jane LOST ${exit_pnl:+.2f}. "
                        f"Chev's feedback: {trade.get('chev_feedback','—')}"
                    )
                if disagree_msg:
                    print(f"[Jane/Chev] Disagreement: {disagree_msg[:100]}")

            # Save to Jane's journal for shared learning
            jane_journal = _load_jane_journal()
            jane_journal.append({
                "ts":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":    trade["symbol"],
                "direction": trade["direction"],
                "entry":     trade["entry"],
                "sl":        trade["sl"],
                "tp":        trade["tp"],
                "exit_price": exit_price,
                "pnl":       exit_pnl,
                "outcome":   outcome,
                "tags":      trade.get("tags", ""),
                "chev_verdict": chev_v,
            })
            try:
                with open(JANE_JOURNAL_PATH, "w", encoding="utf-8") as f:
                    json.dump(jane_journal, f, indent=2)
            except Exception as ex:
                print(f"[Jane journal] Save failed: {ex}")

            global _combined_closed_count
            _combined_closed_count += 1
            _maybe_run_cross_analysis()

            threading.Thread(
                target=send_telegram_alert,
                args=(f"[Jane] {trade['symbol']} {trade['direction'].upper()} → {outcome} ${exit_pnl:+.2f}",),
                daemon=True,
            ).start()
            continue

        still_open.append(trade)

    jane_trades = still_open


def log_new_trade(worksheet, dashboard_ws, symbol, asset_type, trade, current_price_at_creation, confluence_prices=None):
    direction = trade["direction"]
    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    risk_pct = trade["risk_pct"]
    leverage = trade["leverage"]
    position_size_usd = trade["position_size_usd"]
    tags = trade["tags"]
    trade_type = trade["trade_type"]

    balance = get_balance(dashboard_ws)

    max_leverage = MAX_LEVERAGE_BY_TYPE.get(asset_type, {}).get(trade_type, 5)
    leverage = max(1, min(leverage, max_leverage))
    risk_pct = max(0.5, min(risk_pct, 3.0))

    margin_used_usd = round(position_size_usd / leverage, 2) if leverage else position_size_usd
    if margin_used_usd > balance:
        position_size_usd = round(balance * leverage, 2)
        margin_used_usd = balance
        print(f"[{datetime.now()}] {symbol}: Chev's position size exceeded available margin - capped to fit ${balance:.2f} balance.")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expiry_hours = TRADE_TYPE_EXPIRY_HOURS[trade_type]
    expiry_at = (datetime.now() + pd.Timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")
    entry_trigger_above = entry > current_price_at_creation

    conf_json = json.dumps(confluence_prices) if confluence_prices else ""

    row_values = [
        symbol, direction.upper(), entry, sl, tp, risk_pct, leverage,
        position_size_usd, margin_used_usd, tags, "", "", "PENDING", "", timestamp,
        trade_type, expiry_at, conf_json, str(entry_trigger_above)
    ]

    existing_rows = len(worksheet.get_all_values())
    row_index = existing_rows + 1
    worksheet.append_row(row_values, value_input_option="USER_ENTERED")

    return {
        "row": row_index,
        "symbol": symbol,
        "asset_type": asset_type,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "risk_pct": risk_pct,
        "leverage": leverage,
        "position_size_usd": position_size_usd,
        "tags": tags,
        "trade_type": trade_type,
        "status": "PENDING",
        "expiry_at": expiry_at,
        "entry_trigger_above": entry_trigger_above,
        "confluence_prices": confluence_prices or {},
    }


def check_and_update_open_trades(worksheet, dashboard_ws, asset_types_to_check):
    global open_trades, _cached_balance
    still_open = []

    for trade in open_trades:
        if trade.get("row") in _force_closed_rows:
            continue  # already force-closed by user — skip, don't re-add or re-log

        if trade["asset_type"] not in asset_types_to_check:
            still_open.append(trade)
            continue

        price = get_current_price(trade["symbol"], trade["asset_type"])
        if price is None:
            still_open.append(trade)
            continue

        if trade.get("status") == "PENDING":
            try:
                worksheet.update_cell(trade["row"], 11, price)
            except Exception as e:
                print(f"[{datetime.now()}] Live price update failed for pending {trade['symbol']}: {e}")

            triggered = (price >= trade["entry"]) if trade["entry_trigger_above"] else (price <= trade["entry"])
            if triggered:
                trade["status"] = "OPEN"
                trade["original_sl"] = trade.get("original_sl", trade["sl"])
                margin = round(trade.get("position_size_usd", 0) / max(trade.get("leverage", 1), 1), 2)
                trade["margin_reserved"] = margin
                balance = get_balance(dashboard_ws)
                new_balance = round(balance - margin, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                try:
                    worksheet.update_cell(trade["row"], 13, "OPEN")
                except Exception as e:
                    print(f"[{datetime.now()}] Status flip failed for {trade['symbol']}: {e}")
                print(f"[{datetime.now()}] {trade['symbol']} PENDING -> OPEN | Margin reserved: ${margin:.2f} | Balance: ${balance:.2f} -> ${new_balance:.2f}")
            else:
                expiry_time = datetime.strptime(trade["expiry_at"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() > expiry_time:
                    try:
                        worksheet.update_cell(trade["row"], 13, "EXPIRED")
                    except Exception as e:
                        print(f"[{datetime.now()}] Expiry update failed for {trade['symbol']}: {e}")
                    print(f"[{datetime.now()}] {trade['symbol']} PENDING order expired - never filled, dropped.")
                    continue

            still_open.append(trade)
            continue

        is_long = trade["direction"] == "long"

        move_pct = (price - trade["entry"]) / trade["entry"] if is_long else (trade["entry"] - price) / trade["entry"]
        live_pnl_dollars = round(trade["position_size_usd"] * move_pct, 2)

        print(f"[{datetime.now()}] {trade['symbol']} check - price: {price}, SL: {trade['sl']}, TP: {trade['tp']}, live PnL: ${live_pnl_dollars}")

        try:
            worksheet.update(values=[[price, live_pnl_dollars]], range_name=f"K{trade['row']}:L{trade['row']}")
        except Exception as e:
            print(f"[{datetime.now()}] Live price/PnL update failed for {trade['symbol']}: {e}")

        hit_tp = (price >= trade["tp"]) if is_long else (price <= trade["tp"])
        hit_sl = (price <= trade["sl"]) if is_long else (price >= trade["sl"])

        if hit_tp or hit_sl:
            exit_price = trade["tp"] if hit_tp else trade["sl"]
            exit_move  = ((exit_price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - exit_price) / trade["entry"])
            exit_pnl   = round(trade["position_size_usd"] * exit_move, 2)
            outcome    = "WIN" if exit_pnl >= 0 else "LOSS"
            if hit_tp:
                close_type = "TP_HIT"
            elif trade.get("sip_active"):
                close_type = "SIP_HIT"
            else:
                close_type = "SL_HIT"
            balance = get_balance(dashboard_ws)
            margin_to_return = trade.get("margin_reserved", 0)
            new_balance = round(balance + margin_to_return + exit_pnl, 2)
            set_balance(dashboard_ws, new_balance)
            _cached_balance = new_balance
            try:
                worksheet.update(values=[[exit_price, exit_pnl, outcome, exit_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
            except Exception as e:
                print(f"[{datetime.now()}] Sheet update failed for {trade['symbol']}: {e}")
            arrow = "+" if hit_tp else ("-" if close_type == "SL_HIT" else "~")
            print(f"[{datetime.now()}] {arrow} {trade['symbol']} {close_type}  ${exit_pnl:+.2f}  |  Piggy bank: ${balance:.2f} -> ${new_balance:.2f}")
            trade_copy = dict(trade)
            trade_copy["close_type"] = close_type
            threading.Thread(target=_do_postmortem,
                             args=(trade_copy, outcome, exit_pnl, exit_price),
                             daemon=True).start()
            continue

        # Store original SL once (before any trailing modifies it)
        if "original_sl" not in trade:
            trade["original_sl"] = trade["sl"] if not trade.get("sip_active") else trade["entry"]

        orig_risk = abs(trade["entry"] - trade["original_sl"])

        # Detect SIP transition (SL crossed above entry for LONG, or below for SHORT)
        if not trade.get("sip_active") and _is_sip(trade):
            trade["sip_active"] = True
            trade["sip_price"]  = trade["sl"]
            print(f"[SIP] {trade['symbol']} SL {trade['sl']} crossed entry {trade['entry']} — SIP active, trade cannot lose.")

        # ── Milestone alerts: 50% and 75% toward TP or SL ─────────────────────
        tp_range = abs(trade["tp"] - trade["entry"])
        sl_range = abs(trade["entry"] - trade["sl"])
        if "milestones_hit" not in trade:
            trade["milestones_hit"] = set()

        if tp_range > 0:
            tp_progress = max(0, (abs(price - trade["entry"]) / tp_range) * 100) if move_pct > 0 else 0
            for milestone in (50, 75):
                tag = f"tp_{milestone}"
                if tp_progress >= milestone and tag not in trade["milestones_hit"]:
                    trade["milestones_hit"].add(tag)
                    direction_label = "long" if is_long else "short"
                    print(f"[{datetime.now()}] {trade['symbol']} {direction_label} {milestone}% to TP — Chev reviewing")
                    # Call Chev at both 50% (SIP evaluation) and 75% (trail aggressively)
                    threading.Thread(
                        target=lambda t=dict(trade), p=price: ask_chev_manage_trade(t, p),
                        daemon=True
                    ).start()

        if sl_range > 0:
            sl_progress = max(0, (abs(price - trade["entry"]) / sl_range) * 100) if move_pct < 0 else 0
            floor_label = "SIP" if trade.get("sip_active") else "SL"
            for milestone in (50, 75):
                tag = f"sl_{milestone}"
                if sl_progress >= milestone and tag not in trade["milestones_hit"]:
                    trade["milestones_hit"].add(tag)
                    direction_label = "long" if is_long else "short"
                    print(f"[{datetime.now()}] {trade['symbol']} {direction_label} {milestone}% to {floor_label} — Chev reviewing")
                    if milestone == 75:
                        threading.Thread(
                            target=lambda t=dict(trade), p=price: ask_chev_manage_trade(t, p),
                            daemon=True
                        ).start()

        # Ask Chev to manage the trade only when price has moved 0.5R since the last check
        last_trail_price = trade.get("last_trail_price", trade["entry"])
        price_delta = abs(price - last_trail_price)
        if move_pct > 0 and orig_risk > 0 and price_delta >= 0.5 * orig_risk:
            trade["last_trail_price"] = price
            reply = ask_chev_manage_trade(trade, price)
            reply_upper = (reply or "").strip().upper()

            if reply_upper.startswith("CLOSE"):
                outcome = "WIN" if live_pnl_dollars > 0 else "LOSS"
                close_type = "CHEV_CLOSE"
                balance = get_balance(dashboard_ws)
                margin_to_return = trade.get("margin_reserved", 0)
                new_balance = round(balance + margin_to_return + live_pnl_dollars, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                try:
                    worksheet.update(values=[[price, live_pnl_dollars, f"CLOSED ({outcome})", live_pnl_dollars]], range_name=f"K{trade['row']}:N{trade['row']}")
                except Exception as e:
                    print(f"[{datetime.now()}] Sheet update failed on Chev close: {e}")
                print(f"[{datetime.now()}] Chev CLOSE {trade['symbol']} at {price} | ${live_pnl_dollars:+.2f} | Balance: ${new_balance:.2f}")
                trade_copy = dict(trade)
                trade_copy["close_type"] = close_type
                threading.Thread(target=_do_postmortem, args=(trade_copy, f"CLOSED ({outcome})", live_pnl_dollars, price), daemon=True).start()
                continue

            elif reply_upper.startswith("TRAIL_SL") or reply_upper.startswith("TRAIL_SIP"):
                reply_stripped = (reply or "").strip()
                try:
                    parts = reply_stripped.split()
                    new_sl = None
                    new_tp = None
                    for i, p in enumerate(parts):
                        if p.upper().rstrip(":") in ("TRAIL_SL", "TRAIL_SIP") and i + 1 < len(parts):
                            new_sl = _clean_numeric(parts[i + 1])
                        if p.upper().rstrip(":") == "TP" and i + 1 < len(parts):
                            new_tp = _clean_numeric(parts[i + 1])
                    reason_match = re.search(r'REASON:\s*(.+?)(?:\n|$)', reply_stripped, re.IGNORECASE)
                    move_reason = reason_match.group(1).strip() if reason_match else ""

                    if new_sl is not None:
                        valid = (new_sl < price) if is_long else (new_sl > price)

                        # SIP ratchet: if already in SIP, it can only move further in profit direction
                        if valid and trade.get("sip_active"):
                            ratchet_ok = (new_sl >= trade["sl"]) if is_long else (new_sl <= trade["sl"])
                            if not ratchet_ok:
                                print(f"[{datetime.now()}] {trade['symbol']} SIP ratchet — cannot move SIP backwards ({new_sl} vs current {trade['sl']})")
                                valid = False

                        if valid:
                            old_sl = trade["sl"]
                            trade["sl"] = new_sl
                            worksheet.update_cell(trade["row"], 4, new_sl)

                            # Detect SIP transition: SL just crossed above entry
                            if _is_sip(trade) and not trade.get("sip_active"):
                                trade["sip_active"] = True
                                trade["sip_price"]  = new_sl
                                print(f"[SIP] {trade['symbol']} SL {old_sl} -> SIP {new_sl} — trade locked in profit, cannot lose.")

                            label = "SIP" if trade.get("sip_active") else "SL"
                            sl_entry = f"{label} {old_sl} → {new_sl} @ {datetime.now().strftime('%H:%M')}"
                            trade.setdefault("chev_moves", []).append(sl_entry)
                            print(f"[{datetime.now()}] {trade['symbol']} {label} trailed to {new_sl}")
                            # Notify Telegram — one line, 15-min cooldown per symbol to avoid spam
                            _now = time.time()
                            if _now - _sl_notify_ts.get(trade["symbol"], 0) >= 900:
                                _sl_notify_ts[trade["symbol"]] = _now
                                _sl_arrow = "↑" if is_long else "↓"
                                send_telegram_alert(f"{trade['symbol']} {label} {_sl_arrow} {_fmt_p(old_sl)} → {_fmt_p(new_sl)}")
                        else:
                            print(f"[{datetime.now()}] {trade['symbol']} Chev's TRAIL move to {new_sl} rejected — wrong side of price {price}")

                    if new_tp is not None:
                        old_tp = trade["tp"]
                        trade["tp"] = new_tp
                        worksheet.update_cell(trade["row"], 5, new_tp)
                        tp_entry = f"TP {old_tp} → {new_tp} @ {datetime.now().strftime('%H:%M')}"
                        if move_reason:
                            tp_entry += f" — {move_reason}"
                        trade.setdefault("chev_moves", []).append(tp_entry)
                        print(f"[{datetime.now()}] {trade['symbol']} TP trailed to {new_tp}" + (f" | reason: {move_reason}" if move_reason else ""))

                except (ValueError, IndexError) as e:
                    print(f"[{datetime.now()}] Couldn't parse Chev's TRAIL reply: {reply} — {e}")

        still_open.append(trade)

    open_trades = still_open


# ============================================================
# MAIN LOOP
# ============================================================

print("Dexter is watching the markets...")

worksheet, dashboard_ws, jane_worksheet = connect_to_sheet()
threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=_fast_price_update, daemon=True).start()
threading.Thread(target=_push_ngrok_url, daemon=True).start()
print(f"[{datetime.now()}] Web terminal running at http://localhost:8080")
open_trades     = load_state_from_sheet(worksheet)
_cached_balance = get_balance(dashboard_ws)
print(f"[{datetime.now()}] Loaded {len(open_trades)} open trade(s) from sheet. Balance: ${_cached_balance:.2f}")
_load_jane_balance()
jane_trades = load_jane_trades_from_sheet()
print(f"[{datetime.now()}] Jane: {len(jane_trades)} open trade(s). Balance: ${jane_balance:.2f}")

last_forex_scan        = 0
last_stock_scan        = 0
last_forex_trade_check = 0
last_stock_trade_check = 0
_tf_last_scan: dict    = {}  # (symbol, tf) → unix timestamp of last scan initiation

while True:
    now = time.time()

    # Keep Firebase snapshot data fresh (runs every scan cycle ~5min)
    _cached_balance = get_balance(dashboard_ws)
    try:
        rows = worksheet.get_all_values()[1:]
        _firebase_win_stats["wins"]   = sum(1 for r in rows if len(r) >= 13 and r[12].strip().upper() == "WIN")
        _firebase_win_stats["losses"] = sum(1 for r in rows if len(r) >= 13 and r[12].strip().upper() == "LOSS")
    except Exception:
        pass
    try:
        jrows = jane_worksheet.get_all_values()[1:]
        _jane_win_stats["wins"]   = sum(1 for r in jrows if len(r) >= 13 and r[12].strip().upper() == "WIN")
        _jane_win_stats["losses"] = sum(1 for r in jrows if len(r) >= 13 and r[12].strip().upper() == "LOSS")
    except Exception:
        pass

    # Confluence scans — crypto+forex every cycle (unlimited sources), stocks on Twelve Data budget
    scan_forex_this_round  = (now - last_forex_scan)  >= FOREX_SCAN_INTERVAL
    scan_stocks_this_round = (now - last_stock_scan)  >= STOCK_SCAN_INTERVAL and is_ny_market_hours()
    # Trade checks (fill detection, SL/TP, management) — fast cadence so pending orders don't miss their price
    check_forex_trades  = (now - last_forex_trade_check) >= FOREX_TRADE_CHECK_INTERVAL
    check_stock_trades  = (now - last_stock_trade_check) >= STOCK_TRADE_CHECK_INTERVAL and is_ny_market_hours()

    if scan_forex_this_round:
        last_forex_scan = now
    if scan_stocks_this_round:
        last_stock_scan = now
    if check_forex_trades:
        last_forex_trade_check = now
    if check_stock_trades:
        last_stock_trade_check = now

    trade_check_types = ["crypto"]
    if check_forex_trades:
        trade_check_types.append("forex")
    if check_stock_trades:
        trade_check_types.append("stock")

    check_and_update_open_trades(worksheet, dashboard_ws, trade_check_types)
    check_and_update_jane_trades()

    # Per-TF scan: each (symbol, tf) fires independently at its candle-close interval.
    # 15m only escalates when a 15m candle has closed; 4H only when a 4H candle has closed, etc.
    open_symbols = {t["symbol"] for t in open_trades}
    scan_tasks = []
    for item in WATCHLIST:
        if item["type"] == "forex"  and not scan_forex_this_round:
            continue
        if item["type"] == "stock"  and not scan_stocks_this_round:
            continue
        if item["symbol"] in open_symbols:
            continue
        tfs = SCAN_TFS_CRYPTO if item["type"] == "crypto" else SCAN_TFS_FOREX
        for tf in tfs:
            key = (item["symbol"], tf)
            if now - _tf_last_scan.get(key, 0.0) >= TF_SECONDS[tf]:
                _tf_last_scan[key] = now
                scan_tasks.append((item, tf))

    scan_results = {}
    if scan_tasks:
        with ThreadPoolExecutor(max_workers=max(len(scan_tasks), 1)) as executor:
            futures = {executor.submit(scan_pair_tf, t[0]["symbol"], t[0]["type"], t[1]): t
                       for t in scan_tasks}
            for future in as_completed(futures):
                task = futures[future]
                item_t, tf_t = task
                try:
                    result = future.result()
                    if result:
                        scan_results[(item_t["symbol"], tf_t)] = (item_t, result)
                except Exception as e:
                    print(f"[{datetime.now()}] scan_pair_tf {item_t['symbol']}/{tf_t}: {e}")

    # Process results sequentially — Chev handles one escalation at a time
    for (sym_key, tf_key), (item, result) in scan_results.items():
        primary_tf  = result.get("primary_tf", "1h")
        esc_key     = (result["symbol"], primary_tf)
        asset_type  = item.get("type", "crypto")
        min_conf    = TF_MIN_CONFLUENCE.get(primary_tf, 3) + _session_confluence_bonus(asset_type)
        skip_cool   = TF_SKIP_COOLDOWN.get(primary_tf, _ESCALATION_COOLDOWN)
        post_cool   = TF_POST_COOLDOWN.get(primary_tf, _POST_COOLDOWN)

        if result and result["count"] >= min_conf:
            # If Chev is rate-limited, skip until cooldown expires
            rl_remaining = _chev_rate_limit_until - time.time()
            if rl_remaining > 0:
                print(f"[{datetime.now()}] Rate-limited — skipping {result['symbol']}/{primary_tf} (retry in {int(rl_remaining)}s).")
                continue

            # If Open WebUI is down, check health every 60s
            if not _chev_online:
                now_t = time.time()
                if now_t - _chev_last_health_check >= CHEV_HEALTH_CHECK_INTERVAL:
                    _chev_last_health_check = now_t
                    if _check_chev_health():
                        _chev_online = True
                        print(f"[{datetime.now()}] Chev is back — resuming escalations.")
                    else:
                        print(f"[{datetime.now()}] Chev still down, skipping {result['symbol']}/{primary_tf}.")
                if not _chev_online:
                    continue

            now_esc = time.time()
            unblock = _last_escalated.get(esc_key, 0.0)
            cooldown_left = unblock - now_esc
            if cooldown_left > 0:
                print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: cooldown active ({int(cooldown_left)}s remaining) — skipping.")
                continue

            # Hard forex event block: no new trades within 30min of a high-impact release
            _blocked, _block_reason = _forex_event_block(result["symbol"], item["type"])
            if _blocked:
                print(f"[{datetime.now()}] EVENT BLOCK — {result['symbol']}/{primary_tf}: {_block_reason} — no new trades within 30min window.")
                _last_escalated[esc_key] = time.time() + 1800  # re-check in 30min
                continue

            # Regime filter: skip CHOPPY markets — ADX<15 means random noise, no edge
            _r4h = result.get("regime_4h") or {}
            if _r4h.get("regime") == "CHOPPY":
                print(f"[{datetime.now()}] REGIME BLOCK — {result['symbol']}/{primary_tf}: 4H CHOPPY (ADX={_r4h.get('adx')}) — no directional edge, skipping.")
                _last_escalated[esc_key] = time.time() + 1800
                continue

            if result.get("dist_from_level") is not None:
                _d = result["dist_from_level"]
                _dlabel = "AT LEVEL" if _d <= 0.5 else "APPROACHING" if _d <= 2.0 else f"FAR ({_d:.2f}%)"
                dist_str = f" | {_d:.2f}% from level ({_dlabel})"
            else:
                dist_str = ""
            gp_str   = " | ★ GOLDEN POCKET" if result.get("in_golden_pocket") else ""
            print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: score={result['count']:.1f} {result['reasons']}{dist_str}{gp_str} — escalating to Chev")
            balance = get_balance(dashboard_ws)
            chev_response = ask_chev_to_judge(result, balance, dashboard_ws, timeout=360)

            parsed = parse_chev_reply(chev_response)

            # Hard confluence score gate — reject below-threshold trades even if Chev said POST.
            # Uses Dexter's mechanically-verified score, not Chev's self-reported tags.
            # Chev cannot inflate the gate by adding unverified tags to the TRADE: line.
            if parsed and parsed.get("trade"):
                _dexter_score = result["count"]
                _tags_str     = parsed["trade"].get("tags", "")
                _threshold    = CONFLUENCE_THRESHOLD_FOREX if item["type"] == "forex" else CONFLUENCE_THRESHOLD_CRYPTO
                if _dexter_score < _threshold:
                    _gate_reason = f"Dexter score={_dexter_score} < threshold={_threshold} ({item['type']}) | Chev tags={_tags_str}"
                    print(f"[{datetime.now()}] SCORE GATE — {result['symbol']} rejected: {_gate_reason}")
                    _log_chev_decision(
                        result["symbol"], primary_tf, _dexter_score, result["reasons"],
                        "GATE_REJECT", _gate_reason, (result.get("regime_4h") or {}).get("regime")
                    )
                    parsed["trade"] = None

            if parsed is None:
                _last_escalated[esc_key] = time.time() + skip_cool
                if chev_response is not None:
                    print(f"[{datetime.now()}] Chev's reply didn't match POST:/SKIP: format — skipping.")
                    _log_chev_decision(
                        result["symbol"], primary_tf, result["count"], result["reasons"],
                        "FORMAT_ERROR", (chev_response or "")[:200],
                        (result.get("regime_4h") or {}).get("regime")
                    )
            elif parsed["post"]:
                if parsed.get("trade"):
                    _t = parsed["trade"]
                    tg_msg = f"{result['symbol']} {_t.get('direction','').upper()} entry {_fmt_p(_t.get('entry',0))} · SL {_fmt_p(_t.get('sl',0))} · TP {_fmt_p(_t.get('tp',0))}"
                    send_telegram_alert(tg_msg)
                    print(f"[{datetime.now()}] Posted to Telegram: {tg_msg}")
                if parsed.get("trade"):
                    conf_prices = {}
                    td = parsed["trade"]

                    # --- Support / Resistance ---
                    # Only Chev's explicitly provided level. No Dexter fallback.
                    if td.get("chev_sr_level"):
                        conf_prices["SR_S"] = round(td["chev_sr_level"], 5)

                    # --- Fibonacci ---
                    # Only drawn if Chev gave both the swing high and swing low.
                    if td.get("chev_fib_high") and td.get("chev_fib_low"):
                        fh, fl = td["chev_fib_high"], td["chev_fib_low"]
                        rng = fh - fl
                        conf_prices["FB_50"]     = round(fh - rng * 0.500, 5)
                        conf_prices["FB_618"]    = round(fh - rng * 0.618, 5)
                        conf_prices["FB_65"]     = round(fh - rng * 0.650, 5)
                        conf_prices["FB_786"]    = round(fh - rng * 0.786, 5)
                        conf_prices["FB_HIGH_P"] = round(fh, 5)
                        conf_prices["FB_LOW_P"]  = round(fl, 5)

                    # --- Volume Profile ---
                    # Only drawn if Chev gave a specific start date for the range.
                    if td.get("chev_vp_start"):
                        try:
                            import calendar as _cal
                            vp_dt = datetime.strptime(td["chev_vp_start"].strip()[:10], "%Y-%m-%d")
                            conf_prices["VP_START_T"] = int(_cal.timegm(vp_dt.timetuple()))
                            conf_prices["VP_END_T"]   = int(time.time())
                        except Exception as _e:
                            print(f"[Dexter] VP start parse error ({td['chev_vp_start']}): {_e}")

                    # --- RSI Divergence ---
                    # Chev marks the two RSI swing pivots by date. System draws the RSI trendline
                    # on the RSI panel AND the matching price trendline on the main chart.
                    for _rdi_key, _conf_key in [("chev_rsi_div_t1", "RSI_DIV_T1"), ("chev_rsi_div_t2", "RSI_DIV_T2")]:
                        if td.get(_rdi_key):
                            try:
                                import calendar as _cal
                                _rdi_dt = datetime.strptime(td[_rdi_key].strip()[:10], "%Y-%m-%d")
                                conf_prices[_conf_key] = int(_cal.timegm(_rdi_dt.timetuple()))
                            except Exception as _e:
                                print(f"[Dexter] RSI div parse error ({td[_rdi_key]}): {_e}")
                    new_trade = log_new_trade(worksheet, dashboard_ws, result["symbol"], item["type"], parsed["trade"], result["current_price"], confluence_prices=conf_prices)
                    new_trade["reasoning"]   = parsed["trade"].get("reasoning") or parsed.get("telegram_message", "")
                    new_trade["opened_at"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    new_trade["original_sl"] = new_trade.get("sl", 0)
                    new_trade["primary_tf"]  = primary_tf
                    _g, _      = _setup_grade(result, asset_type)
                    _, _sq     = _session_grade(asset_type)
                    _open_risk = [t for t in open_trades if t.get("status") == "OPEN"]
                    new_trade["setup_grade"]     = _g
                    new_trade["session_quality"] = _sq
                    new_trade["heat_at_entry"]   = round(sum(t.get("risk_pct", 0) for t in _open_risk), 1)
                    if new_trade.get("status") == "OPEN":
                        margin = round(new_trade.get("position_size_usd", 0) / max(new_trade.get("leverage", 1), 1), 2)
                        new_trade["margin_reserved"] = margin
                        bal_before = get_balance(dashboard_ws)
                        set_balance(dashboard_ws, round(bal_before - margin, 2))
                        _cached_balance = round(bal_before - margin, 2)
                        print(f"[Dexter] {new_trade['symbol']} OPEN ({primary_tf} {result.get('trade_type','day')}) | Margin: ${margin:.2f} | Balance: ${bal_before:.2f} -> ${_cached_balance:.2f}")
                    open_trades.append(new_trade)
                    # Performance Engine: link this trade to the top hypothesis
                    try:
                        _perf_survey = result.get("survey")
                        if _perf_survey and _perf_survey.hypotheses:
                            _top_hyp = _perf_survey.hypotheses[0]
                            engines.record_trade_hypothesis(
                                trade_id=str(new_trade["row"]),
                                hypothesis_id=_top_hyp.id,
                                hypothesis_name=_top_hyp.name,
                                asset_class=item["type"],
                                primary_tf=primary_tf,
                                dexter_score=result["count"],
                                chev_acted=True,
                            )
                    except Exception as _pe:
                        print(f"[Performance] record_trade_hypothesis failed: {_pe}")
                    _all_active = [t for t in open_trades if t.get("status") in ("OPEN", "PENDING") and t.get("entry")]
                    total_risk = sum(
                        t.get("position_size_usd", 0) * abs(t.get("entry", 0) - t.get("sl", 0)) / max(t.get("entry", 0.0001), 0.0001)
                        for t in _all_active
                    )
                    _open_count    = sum(1 for t in open_trades if t.get("status") == "OPEN")
                    _pending_count = sum(1 for t in open_trades if t.get("status") == "PENDING")
                    print(f"[Dexter] Trade locked. Balance: ${_cached_balance:.2f} | Total at risk: ${total_risk:.2f} ({_open_count} open + {_pending_count} pending)")
                    print(f"[{datetime.now()}] Logged new trade: {new_trade}")
                    _log_chev_decision(
                        result["symbol"], primary_tf, result["count"], result["reasons"],
                        "POST", parsed.get("telegram_message", ""),
                        (result.get("regime_4h") or {}).get("regime")
                    )
                else:
                    print(f"[{datetime.now()}] Chev posted but TRADE: line missing/invalid.")
                _last_escalated[esc_key] = time.time() + post_cool
            else:
                _last_escalated[esc_key] = time.time() + skip_cool
                _skip_reason = parsed.get("skip_reason", "no reason captured") if parsed else "no reason captured"
                print(f"[{datetime.now()}] Chev skipped {result['symbol']}/{primary_tf}: {_skip_reason}")
                _log_chev_decision(
                    result["symbol"], primary_tf, result["count"], result["reasons"],
                    "SKIP", _skip_reason,
                    (result.get("regime_4h") or {}).get("regime")
                )
        elif result:
            print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: score={result['count']:.1f} — below threshold ({min_conf})")

    time.sleep(CHECK_INTERVAL_SECONDS)