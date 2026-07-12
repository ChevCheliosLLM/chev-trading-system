import requests
import pandas as pd
import numpy as np
import time
import os
import functools
import hmac
from datetime import datetime, timezone
from datetime import timedelta
from collections import Counter
import gspread
import re
import json
from google.oauth2.service_account import Credentials
from flask import Flask, jsonify, send_from_directory, request as flask_request
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import engines
import labeller
import derivs
import trade_forensics
import patterns
import risk_gauntlet
import honest_sim
import counterfactual_report
import weight_proposal
import real_performance
import io
import contextlib

# Path configuration for cross-platform support (Windows C:\ vs Linux ~/ home dir)
CHEV_TOOLS_ROOT = os.getenv("CHEV_TOOLS_ROOT", r"C:\ChevTools" if os.name == 'nt' else os.path.expanduser("~/ChevTools"))

# Fallback logic: If the expected root doesn't contain the 'webapp' folder, 
# check if it exists in the directory where this script is located.
if not os.path.exists(os.path.join(CHEV_TOOLS_ROOT, "webapp")):
    _local_root = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(_local_root, "webapp")):
        print(f"[INIT] Webapp not found in {CHEV_TOOLS_ROOT}. Falling back to project dir: {_local_root}")
        CHEV_TOOLS_ROOT = _local_root

WEBAPP_FOLDER = os.path.join(CHEV_TOOLS_ROOT, "webapp")
flask_app = Flask(__name__, static_folder=WEBAPP_FOLDER, static_url_path='')

# PHASE 9: shared-secret auth for every mutating route. secrets.local is a plain
# key=value file, never committed (gitignored), created by Kev by hand -- e.g.
#   DASHBOARD_KEY=<a long random passphrase>
#   GITHUB_TOKEN=<a github personal access token, read by push_dashboard.py>
# Missing file or missing DASHBOARD_KEY -- never fall back to open access; every
# @require_key route returns 503 instead.
SECRETS_FILE = os.path.join(CHEV_TOOLS_ROOT, "secrets.local")

def _load_secrets():
    out = {}
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out

_SECRETS = _load_secrets()
DASHBOARD_KEY = _SECRETS.get("DASHBOARD_KEY") or None
if not DASHBOARD_KEY:
    print(f"[AUTH] WARNING: {SECRETS_FILE} missing or has no DASHBOARD_KEY -- "
          f"all mutating routes are DISABLED (503) until this is fixed.")

def require_key(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not DASHBOARD_KEY:
            return jsonify({"ok": False, "error": "auth not configured"}), 503
        supplied = flask_request.headers.get("X-Chev-Key", "")
        if not hmac.compare_digest(supplied, DASHBOARD_KEY):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper

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
    return flask_app.send_static_file("index.html")

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


# PHASE 12: read-only system vitals for the frontend's vitals strip -- heartbeat (last_scan_
# age), drought clock (hours since a surviving POST), slot occupancy vs caps, exploration
# mode, today's realized R, Chev deliberation latency, and per-source file freshness.
# GET-only, computes and serves, writes nothing. 30s module-level cache (same pattern as
# PHASE 7B-1's /api/strategy/counterfactual) so a phone poll never re-scans the growing
# jsonl files more than twice a minute.
VITALS_CACHE_SECS = 30
_vitals_cache = {"ts": 0.0, "payload": None}

@flask_app.route("/api/vitals")
def api_vitals():
    now = time.time()
    if _vitals_cache["payload"] is not None and (now - _vitals_cache["ts"]) < VITALS_CACHE_SECS:
        payload = dict(_vitals_cache["payload"])
        payload["cache_age_secs"] = round(now - _vitals_cache["ts"], 1)
        return jsonify(payload)

    # Last surviving POST: newest-first scan of chev_decisions.jsonl for decision=="POST".
    _last_post_ts = None
    for r in reversed(_read_jsonl(_CHEV_DECISIONS_LOG, max_lines=8000)):
        if r.get("decision") == "POST":
            _last_post_ts = _parse_dt(r.get("ts"))
            break
    _now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    hours_since_post = (
        round((_now_naive - _last_post_ts).total_seconds() / 3600, 2) if _last_post_ts else None
    )

    # Median deliberation_secs over the last 24h (PHASE 11's new field) -- absent/non-numeric
    # entries (pre-PHASE-11 history, or SKIP's untouched call site) are simply skipped.
    _cutoff = _now_naive - timedelta(hours=24)
    _delibs = []
    for r in _read_jsonl(_CHEV_DECISIONS_LOG, max_lines=8000):
        _ts = _parse_dt(r.get("ts"))
        if _ts is None or _ts < _cutoff:
            continue
        _d = r.get("deliberation_secs")
        if isinstance(_d, (int, float)):
            _delibs.append(_d)
    _delibs.sort()
    median_deliberation = None
    if _delibs:
        _n = len(_delibs)
        median_deliberation = (_delibs[_n // 2] if _n % 2
                               else round((_delibs[_n // 2 - 1] + _delibs[_n // 2]) / 2, 1))

    # Slot occupancy per trade_type -- SAME in-memory open_trades list /api/trades reads,
    # and the SAME active profile risk_gauntlet's own concurrency-cap gate reads. Never
    # re-derives balance/position math independently.
    _prof   = risk_gauntlet.get_active_profile(EXPLORATION_MODE)
    _cap    = _prof["CONCURRENCY_CAP"]
    _counts = {"scalp": 0, "day": 0, "swing": 0}
    for t in open_trades:
        if t.get("status") in ("OPEN", "PENDING"):
            _tt = (t.get("trade_type") or "day").lower()
            if _tt in _counts:
                _counts[_tt] += 1
    slots = {
        tt: {"open_pending": _counts[tt], "cap": _cap.get(tt, 0), "full": _counts[tt] >= _cap.get(tt, 0)}
        for tt in _counts
    }

    # Today's realized R -- reuses honest_sim's own circuit-breaker tracker (the same
    # number the daily-halt check itself reads), never re-derived from the journal here.
    try:
        today_r = honest_sim.breaker_status().get("daily_R", 0.0)
    except Exception:
        today_r = None

    def _age(path):
        try:
            return round(now - os.path.getmtime(path), 1)
        except Exception:
            return None

    payload = {
        "last_scan_ts": (
            datetime.fromtimestamp(_last_scan_completed_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if _last_scan_completed_ts else None
        ),
        "last_scan_age_secs": (round(now - _last_scan_completed_ts, 1) if _last_scan_completed_ts else None),
        "hours_since_last_surviving_post": hours_since_post,
        "slots":                       slots,
        "exploration_mode":             EXPLORATION_MODE,
        "today_realized_r":             today_r,
        "median_deliberation_secs_24h": median_deliberation,
        "decisions_log_age_secs":       _age(_CHEV_DECISIONS_LOG),
        "journal_age_secs":             _age(JOURNAL_PATH),
        "cache_age_secs":               0.0,
    }
    _vitals_cache["ts"]      = now
    _vitals_cache["payload"] = payload
    return jsonify(payload)


@flask_app.route("/api/reset_balance", methods=["POST"])
@require_key
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
@require_key
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

        is_long      = target["direction"] == "long"
        move_pct     = (price - target["entry"]) / target["entry"] if is_long else (target["entry"] - price) / target["entry"]
        _gross_pnl   = round(target.get("position_size_usd", 0) * move_pct, 2)
        exit_pnl, _trade_cost = honest_sim.apply_costs(target, _gross_pnl, 1.0)
        outcome      = "WIN" if exit_pnl >= 0 else "LOSS"
        outcome      = _classify_outcome(outcome, exit_pnl, target)  # Phase 6: force-close bypasses _do_postmortem, needs its own call
        try:
            honest_sim.record_close_R(target, round(exit_pnl + target.get("partial_net_pnl", 0.0), 2))
        except Exception as _rr_e:
            print(f"[honest_sim] record_close_R failed for {symbol}: {_rr_e}")

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
            _sym   = target["symbol"]
            _entry = target["entry"]
            _sl    = target["sl"]
            _tp    = target["tp"]
            if _is_duplicate_journal_entry(_sym, _entry, _sl, _tp, journal):
                print(f"[Journal] Duplicate detected for {_sym} (entry={_entry} SL={_sl} TP={_tp}) — skipping write.")
            else:
                journal.append({
                    "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol":           _sym,
                    "asset_type":       target.get("asset_type", "crypto"),
                    "direction":        target["direction"],
                    "entry":            _entry,
                    "sl":               _sl,
                    "tp":               _tp,
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
                    "system_era":       SYSTEM_ERA,
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
@require_key
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
    if worksheet is None:
        return jsonify([])
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
@require_key
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


# ============================================================
# STRATEGY DASHBOARD API (added 2026-07-05) — read-only aggregation over
# chev_decisions.jsonl, chev_journal.json, and the Examiner's shadow log
# (labeller.OPEN_FILE / CLOSED_FILE). Powers the webapp's Strategy tab.
# Nothing here touches trading logic — pure read + aggregate.
# ============================================================

def _read_jsonl(path, max_lines=None):
    """Read a JSONL file into a list of dicts. max_lines caps to the TAIL of the file
    (most recent entries) so this stays cheap even as logs grow over months."""
    recs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if max_lines:
            lines = lines[-max_lines:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return recs


def _parse_dt(ts_str):
    """Parse 'YYYY-MM-DD HH:MM:SS[...]' — tolerant of a trailing microsecond suffix."""
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


@flask_app.route("/api/strategy/feed")
def api_strategy_feed():
    """Recent Chev decisions (SKIP/REJECT/gate-rejects) — the 'Chev's thoughts' feed
    and the raw drill-down table both read this, just rendered differently."""
    hours    = float(flask_request.args.get("hours", 24))
    limit    = int(flask_request.args.get("limit", 300))
    decision = flask_request.args.get("decision", "").upper().strip()
    symbol   = flask_request.args.get("symbol", "").upper().strip()
    cutoff = datetime.now() - timedelta(hours=hours)

    out = []
    for r in _read_jsonl(_CHEV_DECISIONS_LOG, max_lines=8000):
        ts = _parse_dt(r.get("ts", ""))
        if ts is None or ts < cutoff:
            continue
        if decision and r.get("decision", "").upper() != decision:
            continue
        if symbol and r.get("symbol", "").upper() != symbol:
            continue
        out.append(r)
    out = out[-limit:]
    out.reverse()  # newest first
    return jsonify({"count": len(out), "decisions": out})


@flask_app.route("/api/strategy/funnel")
def api_strategy_funnel():
    """Pipeline attrition for the window: pre-escalation NOT_ESCALATED reasons (from
    the Examiner's shadow log, which sees setups even before Chev does) + Chev's own
    decision breakdown + how many actually opened. Answers 'where do things die.'"""
    hours = float(flask_request.args.get("hours", 24))
    cutoff_epoch = time.time() - hours * 3600
    cutoff_dt    = datetime.now() - timedelta(hours=hours)

    not_esc = Counter()
    for path in (labeller.OPEN_FILE, labeller.CLOSED_FILE):
        for r in _read_jsonl(path, max_lines=20000):
            if r.get("chev_decision") != "NOT_ESCALATED":
                continue
            ts_epoch = r.get("ts_epoch")
            if ts_epoch is None or ts_epoch < cutoff_epoch:
                continue
            not_esc[r.get("chev_reason") or "unknown"] += 1

    dec_counts = Counter()
    for r in _read_jsonl(_CHEV_DECISIONS_LOG, max_lines=8000):
        ts = _parse_dt(r.get("ts", ""))
        if ts is None or ts < cutoff_dt:
            continue
        dec_counts[r.get("decision") or "unknown"] += 1

    opened = 0
    for t in _load_journal():
        ts = _parse_dt(t.get("open_ts") or t.get("ts") or "")
        if ts and ts >= cutoff_dt:
            opened += 1
    for t in open_trades:
        ts = _parse_dt(t.get("open_ts") or "")
        if ts and ts >= cutoff_dt:
            opened += 1

    return jsonify({
        "hours":              hours,
        "not_escalated":      dict(not_esc),
        "not_escalated_total": sum(not_esc.values()),
        "decisions":          dict(dec_counts),
        "escalated_total":    sum(dec_counts.values()) + opened,
        "opened":             opened,
    })


@flask_app.route("/api/strategy/performance")
def api_strategy_performance():
    """Scorecard numbers + equity curve + R-multiple distribution + grade/tag win rates,
    all from chev_journal.json (closed trades) — the same file the Sheet exports from."""
    journal   = _load_journal()
    closed    = [t for t in journal if t.get("outcome") in ("WIN", "LOSS")]
    wins      = [t for t in closed if t["outcome"] == "WIN"]
    losses    = [t for t in closed if t["outcome"] == "LOSS"]
    # Phase 6: SCRATCH (partial banked, remainder trailed near breakeven) is neither a
    # win nor a loss -- excluded from `closed`/win_rate above, reported as its own count.
    scratches = [t for t in journal if t.get("outcome") == "SCRATCH"]

    total_pnl  = sum(float(t.get("pnl", 0) or 0) for t in closed)
    gross_win  = sum(float(t.get("pnl", 0) or 0) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl", 0) or 0) for t in losses))
    win_rate      = (len(wins) / len(closed) * 100) if closed else 0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    expectancy    = (total_pnl / len(closed)) if closed else 0

    # PHASE 25: one point per 4h bucket boundary across the account's full history (was
    # one point per trade) -- value is the running balance AS OF that boundary. A bucket
    # with no new close simply repeats the last known balance, which is what produces the
    # flat segments between trades "for free" (as real, honest data points, not a special
    # step-interpolation rendering mode the frontend would have to opt into).
    EQUITY_BUCKET_HOURS = 4

    def _eq_bucket_start(dt):
        floored_hour = (dt.hour // EQUITY_BUCKET_HOURS) * EQUITY_BUCKET_HOURS
        return dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)

    equity_curve = []
    planned_rrs  = []
    running = 0.0
    _bucket_end_balance = {}
    _first_bucket = _last_bucket = None
    for t in sorted(closed, key=lambda x: x.get("ts", "")):
        pnl = float(t.get("pnl", 0) or 0)
        running += pnl
        ts = _parse_dt(t.get("ts"))
        if ts is not None:
            bkt = _eq_bucket_start(ts)
            _bucket_end_balance[bkt] = round(running, 2)
            if _first_bucket is None or bkt < _first_bucket:
                _first_bucket = bkt
            if _last_bucket is None or bkt > _last_bucket:
                _last_bucket = bkt
        # Dexter's ACTUAL planned R:R (same cost-adjusted formula risk_gauntlet gates on),
        # not a realized-outcome approximation — journal doesn't store trade_type on
        # closed entries yet, so this defaults to "day" for the funding-cost term
        # (crypto swing only, a small effect either way).
        try:
            rr = risk_gauntlet.compute_planned_rr(
                float(t.get("entry") or 0), float(t.get("sl") or 0), float(t.get("tp") or 0),
                t.get("asset_type") or "crypto", trade_type="day"
            )
            if rr is not None:
                planned_rrs.append(round(rr, 3))
        except Exception:
            pass

    if _first_bucket is not None:
        _now_bucket = _eq_bucket_start(datetime.now(timezone.utc).replace(tzinfo=None))
        _last_bucket = max(_last_bucket, _now_bucket)
        last_balance = 0.0
        bkt = _first_bucket
        while bkt <= _last_bucket:
            if bkt in _bucket_end_balance:
                last_balance = _bucket_end_balance[bkt]
            equity_curve.append({
                "t": int(bkt.replace(tzinfo=timezone.utc).timestamp()),
                "cum_pnl": last_balance,
            })
            bkt = bkt + timedelta(hours=EQUITY_BUCKET_HOURS)

    grade_stats = {}
    for t in closed:
        g = (t.get("setup_grade") or "").strip() or "ungraded"
        gs = grade_stats.setdefault(g, {"n": 0, "wins": 0})
        gs["n"] += 1
        if t["outcome"] == "WIN":
            gs["wins"] += 1
    for gs in grade_stats.values():
        gs["win_rate"] = round(gs["wins"] / gs["n"] * 100, 1) if gs["n"] else 0

    # Combo win rates — real sample sizes are thin right now (see compute_combo_win_rates
    # docstring); returned as-is, dashboard is responsible for graying out low-n combos.
    combo_stats = risk_gauntlet.compute_combo_win_rates(journal)

    return jsonify({
        "total_closed":  len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "scratches":     len(scratches),
        "win_rate":      round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "expectancy":    round(expectancy, 2),
        "total_pnl":     round(total_pnl, 2),
        "equity_curve":  equity_curve,
        "planned_rrs":   planned_rrs,
        "grade_stats":   grade_stats,
        "tag_stats":     risk_gauntlet.compute_tag_win_rates(journal),
        "combo_stats":   combo_stats,
    })


# PHASE 7B-1: read-only counterfactual endpoint. GET-only, mutates nothing — computes from
# labels_closed.jsonl/labels_open.jsonl/chev_decisions.jsonl via counterfactual_report's
# shared build_counterfactual() (the SAME function the .txt report uses, so the phone always
# matches the file) and serves JSON. Never touches Chev, chev_journal.json, Sheets, or
# Firebase. A module-level cooldown cache avoids re-parsing the growing jsonl files on every
# phone poll; ?force=1 bypasses the cache but is itself rate-limited to once per 30s so a
# refresh-spamming client can't defeat the cache's whole purpose.
COUNTERFACTUAL_CACHE_SECS = 300
_counterfactual_cache = {"ts": 0.0, "payload": None}
_counterfactual_last_force_ts = 0.0

@flask_app.route("/api/strategy/counterfactual")
def api_strategy_counterfactual():
    """Read-only Examiner counterfactual summary (PHASE 7B-1) — see counterfactual_report.py
    for the full firewalled discovery behind this. GET-only; computes and serves, writes
    nothing, calls nothing (no Chev, no journal, no Sheets, no Firebase)."""
    global _counterfactual_last_force_ts
    now = time.time()
    force = flask_request.args.get("force") == "1"
    if force and (now - _counterfactual_last_force_ts) < 30:
        force = False  # rate-limit force-refresh so it can't defeat the cache
    cache_age = now - _counterfactual_cache["ts"]

    if force or _counterfactual_cache["payload"] is None or cache_age >= COUNTERFACTUAL_CACHE_SECS:
        data = counterfactual_report.build_counterfactual()
        _counterfactual_cache["ts"] = now
        _counterfactual_cache["payload"] = data
        if force:
            _counterfactual_last_force_ts = now
        cache_age = 0.0

    data = _counterfactual_cache["payload"]
    # Public schema only — strip build_counterfactual()'s "_"-prefixed internal fields
    # (raw winner/loser record dicts etc., used by the .txt renderer) before serving.
    public_buckets = [
        {k: b[k] for k in ("key", "label", "kind", "n", "resolved", "shadow_wr", "avg_r",
                            "total_r", "verdict_split")}
        for b in data["buckets"]
    ]
    return jsonify({
        "generated_at":    data["generated_at"],
        "baseline_ts":     data["baseline_ts"],
        "cache_age_secs":  round(cache_age, 1),
        "coverage":        data["coverage"],
        "headline":        data["headline"],
        "buckets":         public_buckets,
        "shadow_tags":     data["shadow_tags"],
        "shadow_combos":   data["shadow_combos"],
        "resolved_items":  data["resolved_items"],
        "weekly":          data["weekly"],
    })


# =============================================================================
# WEIGHT LAB — API routes
#
# Exposes weight_proposal.py's Ridge-regression tag analysis through a read-only
# GET route, plus two @require_key mutating routes that let Kev approve/revert a
# proposed delta into weight_overrides.json. Nothing here ever touches
# CONFLUENCE_SCORES directly — _apply_weight_overrides() (above) is the ONLY
# code path that ever mutates it, and only once, at startup.
# =============================================================================
WEIGHT_PROPOSAL_CACHE_SECS = 300
FREEZE_MIN_RECORDS = 200
_weight_proposal_cache = {"ts": 0.0, "payload": None}
_weight_proposal_significant_tags = set()   # last-served proposal set with significant=true;
                                             # /approve checks against this, never the client's claim


def _atomic_write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _read_weight_overrides_file():
    """Returns {"version": 1, "entries": [...]}; empty/default structure if the
    file is missing or malformed. Never raises."""
    if not os.path.exists(WEIGHT_OVERRIDES_FILE):
        return {"version": 1, "entries": []}
    try:
        with open(WEIGHT_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("entries"), list):
            raise ValueError("'entries' is not a list")
        return data
    except Exception:
        return {"version": 1, "entries": []}


def _run_weight_proposal_selftest():
    """Runs weight_proposal.py's own synthetic self-test, capturing its printed
    PASS/FAIL lines so a failure can be reported by name, not just true/false —
    the 'check the checker' gate."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = weight_proposal.run_selftest()
    fail_lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip().startswith("FAIL:")]
    return ok, fail_lines


def _tag_raw_crosscheck(tag, records):
    """Independent of the regression — straight from the records. Returns
    n_with/winrate_with/avg_netR_with/winrate_without/avg_netR_without."""
    with_recs    = [r for r in records if tag in r["features"]]
    without_recs = [r for r in records if tag not in r["features"]]

    def _stats(recs):
        if not recs:
            return 0, None, None
        wins = sum(1 for r in recs if r.get("label") == 1)
        wr = wins / len(recs) * 100.0
        avg = sum(weight_proposal.net_r(r) for r in recs) / len(recs)
        return len(recs), round(wr, 1), round(avg, 4)

    n_with, wr_with, avg_with = _stats(with_recs)
    n_without, wr_without, avg_without = _stats(without_recs)
    return {
        "n_with": n_with, "winrate_with": wr_with, "avg_netR_with": avg_with,
        "n_without": n_without, "winrate_without": wr_without, "avg_netR_without": avg_without,
    }


def _build_weight_proposal_payload():
    """Pure computation, no Flask objects. Returns a plain dict — {"ok": False,
    "error": ...} on self-test failure, otherwise the full proposal payload."""
    ok, fail_lines = _run_weight_proposal_selftest()
    if not ok:
        detail = "; ".join(fail_lines) if fail_lines else "unknown assertion failed"
        return {"ok": False, "error": f"self-test failed: {detail}"}

    records_raw, malformed = weight_proposal.load_records(weight_proposal.LABELS_CLOSED_FILE)
    kept, n_post, n_incomplete = weight_proposal.prepare_sample(records_raw)

    overrides = _read_weight_overrides_file()
    entries = overrides.get("entries", [])
    freeze_epoch = None
    frozen_since = None
    if entries:
        for e in entries:
            try:
                ts = datetime.strptime(e["approved_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if freeze_epoch is None or ts > freeze_epoch:
                freeze_epoch = ts
                frozen_since = e["approved_at"]

    eligible = [r for r in kept if (r.get("ts_epoch") or 0) > freeze_epoch] if freeze_epoch is not None else kept

    if len(eligible) < FREEZE_MIN_RECORDS:
        return {
            "ok": True, "frozen": True,
            "records_since_last_change": len(eligible),
            "needed": FREEZE_MIN_RECORDS,
            "frozen_since": frozen_since,
            "note": "window keys off approval time; records between approval and restart are conservatively excluded.",
            "proposals": [],
        }

    X, y, tag_names, tag_counts, dropped_tags, control_names, asset_ref, trade_ref = \
        weight_proposal.build_design_matrix(eligible)
    beta = weight_proposal.ridge_fit(X, y, weight_proposal.RIDGE_LAMBDA)
    rng = np.random.default_rng(weight_proposal.SEED)
    boot_draws = weight_proposal.bootstrap_ci(X, y, weight_proposal.RIDGE_LAMBDA, len(tag_names), rng, weight_proposal.BOOTSTRAP_B)
    ci_lo = np.percentile(boot_draws, 2.5, axis=0)
    ci_hi = np.percentile(boot_draws, 97.5, axis=0)

    proposals = []
    significant_tags = set()
    for i, tag in enumerate(tag_names):
        j = 1 + i
        coef = float(beta[j])
        lo, hi = float(ci_lo[j]), float(ci_hi[j])
        significant = (lo > 0) or (hi < 0)
        if tag in WEIGHT_LAB_VERIFIED_TAGS:
            mapping = "verified"
        elif tag in CONFLUENCE_SCORES:
            mapping = "unverified"
        else:
            mapping = "unmapped"
        current_weight = CONFLUENCE_SCORES.get(tag) if mapping == "verified" else None
        row = {
            "tag": tag, "n": tag_counts[tag], "coef": round(coef, 4),
            "ci": [round(lo, 4), round(hi, 4)], "significant": significant,
            "mapping": mapping,
            "current_weight": current_weight,
            "proposed_delta": None, "effective_weight_preview": None, "agreement": None,
        }
        if significant:
            significant_tags.add(tag)
            cross = _tag_raw_crosscheck(tag, eligible)
            row.update(cross)
            if cross["avg_netR_with"] is not None and cross["avg_netR_without"] is not None:
                raw_diff = cross["avg_netR_with"] - cross["avg_netR_without"]
                row["agreement"] = "agree" if (coef > 0) == (raw_diff > 0) else "conflict"
            if mapping == "verified" and current_weight is not None:
                delta = 1 if coef > 0 else -1
                row["proposed_delta"] = delta
                row["effective_weight_preview"] = current_weight + delta
        proposals.append(row)

    proposals.sort(key=lambda r: -abs(r["coef"]))
    ts_values = [r["ts"] for r in eligible if r.get("ts")]

    return {
        "ok": True, "frozen": False,
        "sample": {
            "n_used": len(eligible), "n_excluded_post": n_post,
            "n_excluded_incomplete": n_incomplete, "n_malformed": malformed,
            "date_range": [min(ts_values), max(ts_values)] if ts_values else None,
            "freeze_active": freeze_epoch is not None,
        },
        "proposals": proposals,
        "pending_overrides": entries,   # every entry in the file is, by definition, not yet
                                         # applied to THIS running process — only startup applies them
        "significant_tags": sorted(significant_tags),
    }


@flask_app.route("/api/weight_proposal")
def api_weight_proposal():
    """Read-only. Runs weight_proposal.py's Ridge regression in-process on
    labels_closed.jsonl, gated by its own self-test (never serves numbers if the
    checker itself is broken). 300s cache, same pattern as
    /api/strategy/counterfactual."""
    global _weight_proposal_significant_tags
    now = time.time()
    cache_age = now - _weight_proposal_cache["ts"]
    if _weight_proposal_cache["payload"] is None or cache_age >= WEIGHT_PROPOSAL_CACHE_SECS:
        payload = _build_weight_proposal_payload()
        if not payload.get("ok"):
            # Self-test failure — never cache a broken-checker result, so the
            # next request retries fresh rather than serving stale bad news.
            return jsonify(payload)
        _weight_proposal_cache["ts"] = now
        _weight_proposal_cache["payload"] = payload
        _weight_proposal_significant_tags = set(payload.get("significant_tags", []))
        cache_age = 0.0

    payload = dict(_weight_proposal_cache["payload"])
    payload["cache_age_secs"] = round(cache_age, 1)
    return jsonify(payload)


@flask_app.route("/api/weight_proposal/approve", methods=["POST"])
@require_key
def api_weight_proposal_approve():
    body = flask_request.get_json(silent=True) or {}
    tag = body.get("tag")
    delta = body.get("delta")
    evidence = body.get("evidence") or {}

    if not isinstance(tag, str) or not tag:
        return jsonify({"ok": False, "error": "missing tag"}), 400
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "delta must be an integer"}), 400
    if abs(delta) != 1:
        return jsonify({"ok": False, "error": "delta must be exactly +1 or -1"}), 400
    if tag not in CONFLUENCE_SCORES:
        return jsonify({"ok": False, "error": f"'{tag}' is not a known CONFLUENCE_SCORES tag (unmapped)"}), 400
    if tag not in WEIGHT_LAB_VERIFIED_TAGS:
        return jsonify({"ok": False, "error": f"'{tag}' shares a name with a CONFLUENCE_SCORES key but its mechanic has not been verified (unverified) — see handoff PHASE 16 no-aliasing rule"}), 400
    if tag not in _weight_proposal_significant_tags:
        return jsonify({"ok": False, "error": f"'{tag}' was not in the most recently served significant proposal set"}), 400

    data = _read_weight_overrides_file()
    entries = data.get("entries", [])
    if any(e.get("tag") == tag for e in entries):
        return jsonify({"ok": False, "error": f"a pending override for '{tag}' already exists — revert it first"}), 400

    entry = {
        "tag": tag, "delta": delta,
        "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "evidence": evidence, "source": "weight_lab",
    }
    entries.append(entry)
    data["entries"] = entries
    _atomic_write_json(WEIGHT_OVERRIDES_FILE, data)
    return jsonify({"ok": True, "pending_overrides": entries})


@flask_app.route("/api/weight_proposal/revert", methods=["POST"])
@require_key
def api_weight_proposal_revert():
    body = flask_request.get_json(silent=True) or {}
    idx = body.get("index")
    if not isinstance(idx, int):
        return jsonify({"ok": False, "error": "index must be an integer"}), 400

    data = _read_weight_overrides_file()
    entries = data.get("entries", [])
    if idx < 0 or idx >= len(entries):
        return jsonify({"ok": False, "error": f"index {idx} out of range (0..{len(entries)-1})"}), 400

    entries.pop(idx)
    data["entries"] = entries
    _atomic_write_json(WEIGHT_OVERRIDES_FILE, data)
    return jsonify({"ok": True, "pending_overrides": entries})


@flask_app.route("/api/weight_overrides")
def api_weight_overrides():
    """Read-only. Current weight_overrides.json contents, each entry annotated
    with whether it was already applied this run (compared against the snapshot
    _apply_weight_overrides() took at startup)."""
    data = _read_weight_overrides_file()
    entries = data.get("entries", [])
    annotated = [{**e, "active": e in _WEIGHT_OVERRIDES_STARTUP_SNAPSHOT} for e in entries]
    return jsonify({"ok": True, "version": data.get("version", 1), "entries": annotated})


# =============================================================================
# REAL PERFORMANCE — era-aware, R claimed only where actually recorded (see
# real_performance.py for the full rationale: two conflicting "real average R"
# claims led to this — one silently dropped the pre-tracking era, the other
# back-derived risk from the CURRENT `sl` field, which this system trails, so it
# no longer reflects the risk actually taken on a managed trade). Read-only,
# never derives R from the current `sl`, never touches chev_journal.json.
# =============================================================================
REAL_PERFORMANCE_CACHE_SECS = 300
_real_performance_cache = {"ts": 0.0, "payload": None}


@flask_app.route("/api/real_performance")
def api_real_performance():
    """Read-only. Same structured data as real_performance_report.txt (single
    source of truth — build_real_performance_dict()), 300s cache, same pattern
    as /api/strategy/counterfactual."""
    now = time.time()
    cache_age = now - _real_performance_cache["ts"]
    if _real_performance_cache["payload"] is None or cache_age >= REAL_PERFORMANCE_CACHE_SECS:
        records = real_performance.load_journal(real_performance.JOURNAL_FILE)
        data = real_performance.build_real_performance_dict(records)
        _real_performance_cache["ts"] = now
        _real_performance_cache["payload"] = data
        cache_age = 0.0

    payload = dict(_real_performance_cache["payload"])
    payload["cache_age_secs"] = round(cache_age, 1)
    return jsonify({"ok": True, **payload})


@flask_app.route("/api/strategy/heatmap")
def api_strategy_heatmap():
    """Setup volume by day-of-week x 4-hour block (UTC), from the Examiner's shadow log
    (every setup Dexter evaluates gets a dow/hour_utc stamp, escalated or not) — answers
    'what does weekend vs weekday, or session, actually look like.'
    dow follows Python's date.weekday(): 0=Monday .. 6=Sunday.
    PHASE 25: 6 four-hour blocks (00-04, 04-08, ... 20-24) instead of 24 hour columns --
    hour_utc // 4 is the only change; the stored per-record hour_utc field is untouched,
    this route is the one place the hour math is grouped."""
    days = float(flask_request.args.get("days", 14))
    cutoff_epoch = time.time() - days * 86400
    grid = [[0] * 6 for _ in range(7)]
    for path in (labeller.OPEN_FILE, labeller.CLOSED_FILE):
        for r in _read_jsonl(path, max_lines=20000):
            ts_epoch = r.get("ts_epoch")
            if ts_epoch is None or ts_epoch < cutoff_epoch:
                continue
            dow, hour = r.get("dow"), r.get("hour_utc")
            if dow is None or hour is None:
                continue
            try:
                grid[int(dow)][int(hour) // 4] += 1
            except (IndexError, ValueError, TypeError):
                continue
    return jsonify({"days": days, "grid": grid, "block_hours": 4})


# PHASE 13: read-only daily aggregates for the "System Over Time" charts (R:R pressure,
# threshold-vs-flow, stop-distance reality) plus annotation markers built from
# system_state.jsonl (PHASE 10). GET-only, computes and serves, writes nothing. 300s
# module-level cache, same pattern as PHASE 7B-1's counterfactual cache -- keyed on `days`
# so switching the window doesn't serve a stale window's cache.
TIMESERIES_CACHE_SECS = 300
_timeseries_cache = {"ts": 0.0, "days": None, "bucket_hours": None, "payload": None}

@flask_app.route("/api/strategy/timeseries")
def api_strategy_timeseries():
    days = int(float(flask_request.args.get("days", 30)))
    # PHASE 25: bucket granularity for the X axis -- 4h (default) or 24h (daily, the old
    # behavior). Restricted to values that divide 24 evenly so a simple floor-division
    # always lands on a clean boundary (00/04/08/12/16/20 for 4h; just 00 for 24h).
    bucket_hours = int(float(flask_request.args.get("bucket_hours", 4)))
    if bucket_hours not in (4, 24):
        bucket_hours = 4
    now = time.time()
    if (_timeseries_cache["payload"] is not None and _timeseries_cache["days"] == days
            and _timeseries_cache["bucket_hours"] == bucket_hours
            and (now - _timeseries_cache["ts"]) < TIMESERIES_CACHE_SECS):
        payload = dict(_timeseries_cache["payload"])
        payload["cache_age_secs"] = round(now - _timeseries_cache["ts"], 1)
        return jsonify(payload)

    cutoff_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    all_decisions = _read_jsonl(_CHEV_DECISIONS_LOG, max_lines=20000)

    # Earliest timestamp anywhere in the log carrying a PHASE 11 structured field -- NOT
    # windowed by `days`, since this is "when did structured data begin at all," used by
    # the frontend to render "collecting data since <date>" instead of empty axes. If
    # Dexter hasn't been restarted since PHASE 11 landed, every entry's new fields are
    # still absent and this stays None -- that is the correct, honest answer, not a bug.
    earliest_structured = None
    for r in all_decisions:
        if not any(isinstance(r.get(f), (int, float))
                   for f in ("planned_rr", "ev_advisory_rr", "enforced_rr_floor", "stop_pct")):
            continue
        ts = _parse_dt(r.get("ts"))
        if ts is not None and (earliest_structured is None or ts < earliest_structured):
            earliest_structured = ts

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 4)

    def _bucket_start(dt):
        floored_hour = (dt.hour // bucket_hours) * bucket_hours
        return dt.replace(hour=floored_hour, minute=0, second=0, microsecond=0)

    def _bucket_epoch(bucket_dt):
        return int(bucket_dt.replace(tzinfo=timezone.utc).timestamp())

    by_bucket = {}
    for r in all_decisions:
        ts = _parse_dt(r.get("ts"))
        if ts is None or ts < cutoff_dt:
            continue
        bkt = _bucket_start(ts)
        d = by_bucket.setdefault(bkt, {
            "enforced_rr_floor": [], "ev_advisory_rr": [], "planned_rr": [],
            "stop_pct": [], "escalations": 0, "posts": 0,
        })
        d["escalations"] += 1
        decision = r.get("decision") or ""
        if decision == "POST":
            d["posts"] += 1
        for fld in ("enforced_rr_floor", "ev_advisory_rr", "planned_rr"):
            v = r.get(fld)
            if isinstance(v, (int, float)):
                d[fld].append(v)
        sp = r.get("stop_pct")
        if isinstance(sp, (int, float)) and (decision == "POST" or decision.endswith("REJECT")):
            d["stop_pct"].append(sp)

    # PHASE 25: bucket key is now the bucket's start EPOCH (numeric UNIX seconds) instead
    # of a day-string -- LightweightCharts' "business day" string mode can only represent
    # a whole calendar day, so sub-day granularity (4h buckets) requires real timestamps
    # (numeric time mode) on the frontend. `label` carries the same info in human form for
    # tooltips, so nothing that used to read a date string loses information.
    rr_pressure, flow, stop_reality = [], [], []
    for bkt in sorted(by_bucket.keys()):
        d = by_bucket[bkt]
        t, label = _bucket_epoch(bkt), bkt.strftime("%Y-%m-%d %H:%M")
        rr_pressure.append({
            "t": t, "label": label,
            "enforced_floor":  _median(d["enforced_rr_floor"]),
            "ev_advisory":     _median(d["ev_advisory_rr"]),
            "proposed_median": _median(d["planned_rr"]),
            "proposed_min":    round(min(d["planned_rr"]), 3) if d["planned_rr"] else None,
            "proposed_max":    round(max(d["planned_rr"]), 3) if d["planned_rr"] else None,
        })
        flow.append({"t": t, "label": label, "escalations": d["escalations"], "posts": d["posts"]})
        sp_sorted = sorted(d["stop_pct"])
        stop_reality.append({
            "t": t, "label": label,
            "median_stop_pct": _median(sp_sorted),
            "p10": (sp_sorted[max(0, int(len(sp_sorted) * 0.1) - 1)] if sp_sorted else None),
            "p90": (sp_sorted[min(len(sp_sorted) - 1, int(len(sp_sorted) * 0.9))] if sp_sorted else None),
            "n":   len(sp_sorted),
        })

    # Threshold step-line + annotation markers, both from system_state.jsonl (PHASE 10).
    # Sparse, event-driven -- reported at their own exact timestamp (never bucket-aligned;
    # they're precise flight-recorder events, not aggregated counts) -- the frontend
    # forward-fills the step line between points and buckets an annotation into whichever
    # chart bucket its own epoch falls inside; we only ever report the snapshots that
    # actually exist, no synthesis.
    threshold_points = []
    annotations = []
    for r in _read_jsonl(SYSTEM_STATE_FILE, max_lines=5000):
        ts = _parse_dt(r.get("ts"))
        if ts is None or ts < cutoff_dt:
            continue
        t_epoch = int(ts.replace(tzinfo=timezone.utc).timestamp())
        try:
            score = r["snapshot"]["escalation_thresholds"]["active"]["crypto"]["score"]
            threshold_points.append({"ts": r.get("ts"), "t": t_epoch, "score": score})
        except Exception:
            pass
        changed = r.get("changed")
        if changed:
            summary = "; ".join(f"{k}: {v.get('old')}→{v.get('new')}" for k, v in changed.items())
        else:
            summary = r.get("event") or "event"
        annotations.append({"ts": r.get("ts"), "t": t_epoch, "event": r.get("event"), "summary": summary})

    # Crypto day-trade round-trip cost floor, as a stop_pct-comparable percentage (PHASE 2's
    # COST GATE concept) -- a stop tighter than this can't clear costs even on a full winner.
    crypto_day_cost_floor_pct = round(
        2 * (risk_gauntlet.FEE_SIDE["crypto"] + risk_gauntlet.SLIPPAGE_SIDE["crypto"]) * 100, 3
    )

    payload = {
        "days":                     days,
        "bucket_hours":             bucket_hours,
        "earliest_structured_ts": (earliest_structured.strftime("%Y-%m-%d %H:%M:%S")
                                    if earliest_structured else None),
        "rr_pressure":              rr_pressure,
        "flow":                     flow,
        "threshold":                threshold_points,
        "stop_reality":             stop_reality,
        "crypto_day_cost_floor_pct": crypto_day_cost_floor_pct,
        "annotations":              annotations,
        "cache_age_secs":           0.0,
    }
    _timeseries_cache["ts"] = now
    _timeseries_cache["days"] = days
    _timeseries_cache["bucket_hours"] = bucket_hours
    _timeseries_cache["payload"] = payload
    return jsonify(payload)


# PHASE 16: read-only tag registry -- friendly names/tooltips/definitions for every
# confluence code the system can emit, across all three tag vocabularies (see TAG_REGISTRY's
# own comment block above CONFLUENCE_SCORES for the full explanation). `points` is looked
# up fresh from CONFLUENCE_SCORES on every serve -- never duplicated into TAG_REGISTRY
# itself, so retuning a weight in code is automatically reflected here with no second edit.
# GET-only, computes and serves, writes nothing. 300s module-level cache (tag meanings and
# weights change only when Claude edits this file, never at runtime -- a long cache is safe).
TAG_REGISTRY_CACHE_SECS = 300
_tag_registry_cache = {"ts": 0.0, "payload": None}

@flask_app.route("/api/tag_registry")
def api_tag_registry():
    now = time.time()
    if _tag_registry_cache["payload"] is not None and (now - _tag_registry_cache["ts"]) < TAG_REGISTRY_CACHE_SECS:
        payload = dict(_tag_registry_cache["payload"])
        payload["cache_age_secs"] = round(now - _tag_registry_cache["ts"], 1)
        return jsonify(payload)

    tags = {
        code: {
            "code":       code,
            "name":       meta["name"],
            "tooltip":    meta["tooltip"],
            "definition": meta["definition"],
            "how":        meta["how"],
            "points":     CONFLUENCE_SCORES.get(code),
        }
        for code, meta in TAG_REGISTRY.items()
    }
    payload = {"tags": tags, "cache_age_secs": 0.0}
    _tag_registry_cache["ts"] = now
    _tag_registry_cache["payload"] = payload
    return jsonify(payload)


# PHASE 18: read-only per-tag weekly win-rate history -- the Indicator Scoreboard's
# sparkline column. Computed from chev_journal.json's closed trades using the SAME `tags`
# field compute_tag_win_rates() already reads (never re-derived, never touches that
# function or its call sites). GET-only, writes nothing. 300s cache, keyed on `all` since
# the two windows (current-era vs full-history) serve genuinely different payloads.
TAG_TRENDS_CACHE_SECS = 300
_tag_trends_cache = {"ts": 0.0, "all": None, "payload": None}

def _week_start_utc(dt):
    """Monday 00:00:00 UTC of the week containing dt -- same convention
    counterfactual_report.py's build_weekly_regret() uses, so 'week' means the same thing
    everywhere on the dashboard. Not imported from there (that module's own internal
    helper) -- reimplemented here since it's a one-line calculation, not machinery."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

@flask_app.route("/api/strategy/tag_trends")
def api_strategy_tag_trends():
    all_history = flask_request.args.get("all") == "1"
    now = time.time()
    if (_tag_trends_cache["payload"] is not None and _tag_trends_cache["all"] == all_history
            and (now - _tag_trends_cache["ts"]) < TAG_TRENDS_CACHE_SECS):
        payload = dict(_tag_trends_cache["payload"])
        payload["cache_age_secs"] = round(now - _tag_trends_cache["ts"], 1)
        return jsonify(payload)

    # Era boundary: pre-baseline trades were earned under different rules. SYSTEM_ERA
    # carries its own start date as a trailing YYYY-MM-DD (e.g. "explor_v2_2026-07-06") --
    # parsed here rather than requiring a per-trade `system_era` field, since that field is
    # only stamped on trades closed AFTER it was introduced (most of the real journal right
    # now predates it entirely). `?all=1` bypasses this filter for the full history.
    era_start = None
    _m = re.search(r"(\d{4}-\d{2}-\d{2})", SYSTEM_ERA)
    if _m:
        try:
            era_start = datetime.strptime(_m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            era_start = None

    journal = _load_journal()
    closed = [t for t in journal if t.get("outcome") in ("WIN", "LOSS")]

    buckets = {}   # tag -> week_start(datetime) -> {"n": int, "wins": int}
    for t in closed:
        ts = _parse_dt(t.get("ts"))
        if ts is None:
            continue
        ts = ts.replace(tzinfo=timezone.utc)   # journal "ts" is always UTC (see every _log_* call site)
        if not all_history and era_start is not None and ts < era_start:
            continue
        week = _week_start_utc(ts)
        raw = (t.get("tags") or "").replace(" manual-close", "")
        for tag in set(tok.strip().lower() for tok in raw.split(",") if tok.strip()):
            wk_bucket = buckets.setdefault(tag, {}).setdefault(week, {"n": 0, "wins": 0})
            wk_bucket["n"] += 1
            if t.get("outcome") == "WIN":
                wk_bucket["wins"] += 1

    # Weeks with n=0 never get a bucket at all (nothing to omit-after-the-fact) -- honest
    # sparse history, not zero-filled, per spec.
    tags_out = {
        tag: [
            {"week": wk.strftime("%Y-%m-%d"), "wr": round(b["wins"] / b["n"], 4), "n": b["n"]}
            for wk, b in sorted(weeks.items())
        ]
        for tag, weeks in buckets.items()
    }

    payload = {
        "all":   all_history,
        "since": (era_start.strftime("%Y-%m-%d") if (era_start and not all_history) else None),
        "tags":  tags_out,
        "cache_age_secs": 0.0,
    }
    _tag_trends_cache["ts"] = now
    _tag_trends_cache["all"] = all_history
    _tag_trends_cache["payload"] = payload
    return jsonify(payload)


@flask_app.route("/api/strategy/mode")
def api_strategy_mode():
    """Current EXPLORATION_MODE status + the actual active NORMAL/EXPLORATION numbers,
    so the dashboard can show which ruleset is live without opening a file."""
    return jsonify({
        "exploration_mode": EXPLORATION_MODE,
        "profile": risk_gauntlet.get_active_profile(EXPLORATION_MODE),
    })


@flask_app.route("/api/strategy/toggle_exploration", methods=["POST"])
@require_key
def api_strategy_toggle_exploration():
    """Flips the live EXPLORATION_MODE flag in this running process — takes effect on the
    very next scan/gauntlet call, no restart needed. This is a real, immediate change to
    the account's risk posture (ATR floor, cost gate, R:R, heat/correlation/concurrency
    caps all move with it — see THE BIG PICTURE / EXPLORATION MODE LINKAGE in handoff.txt),
    not a cosmetic toggle. The dashboard is expected to confirm with the user before
    calling this; nothing server-side gates it beyond that today. Parameter VALUES
    (the NORMAL/EXPLORATION numbers themselves) are not editable here or anywhere over
    HTTP — changing those still requires editing risk_gauntlet.py directly. That's
    deliberate: this endpoint only flips WHICH already-reviewed profile is active.
    """
    global EXPLORATION_MODE
    _old_mode = EXPLORATION_MODE
    EXPLORATION_MODE = not EXPLORATION_MODE
    print(f"[{datetime.now()}] EXPLORATION_MODE toggled via dashboard -> {EXPLORATION_MODE}")
    record_system_state("toggle", "web",
                        changed={"exploration_mode": {"old": _old_mode, "new": EXPLORATION_MODE}})
    return jsonify({
        "exploration_mode": EXPLORATION_MODE,
        "profile": risk_gauntlet.get_active_profile(EXPLORATION_MODE),
    })


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
    """Single-timeframe VP — kept for rsi-canvas.js's _fetchAndDrawVP(), which
    refreshes a Radar idea pill's trade-overlay VP box to the live anchor on the
    CURRENT chart timeframe (see chev-corner.js's _applyIdeaPill). The Arsenal
    tool itself now uses /api/analysis/vp_stack below instead."""
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    tf     = flask_request.args.get("tf", "1h")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    try:
        sym    = clean if atype == "crypto" else symbol
        df     = fetch_candles(sym, atype, tf, 700)
        anchor = _detect_vp_anchor(df)
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
            "bin_edges":   vp["bin_edges"],
            "bin_volumes": [round(v, 2) for v in vp["bin_volumes"]],
            "n_bins":      len(vp["bin_volumes"]),
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


@flask_app.route("/api/analysis/vp_stack")
def api_analysis_vp_stack():
    """Multi-timeframe VP, mirrors /api/analysis/fib_stack — VP's anchor/POC/VAH/VAL
    depend on which TF you look at, so rather than making Kev change the chart's TF
    and re-click VP to compare, this returns every TF at once so the Arsenal popup
    can draw them simultaneously, each in its own color, exactly like the Fib stack."""
    symbol = flask_request.args.get("symbol", "SOLUSDT")
    clean  = symbol.upper().replace("BINANCE:", "").replace(":", "").replace("/", "")
    atype  = _an_asset_type(symbol)
    TFS    = [("15m", "#5dade2"), ("1h", "#2962ff"), ("4h", "#e67e22")]
    results = []
    try:
        for ftf, color in TFS:
            try:
                sym = clean if atype == "crypto" else symbol
                df  = fetch_candles(sym, atype, ftf, 700)
                anchor = _detect_vp_anchor(df)
                if anchor is None:
                    continue
                start_idx = anchor["idx"]
                end_idx   = len(df) - 1
                vp = _ca_volume_profile(df, start_idx, end_idx)
                if not vp:
                    continue
                results.append({
                    "tf": ftf, "color": color,
                    "start_t": int(df.index[start_idx].timestamp()),
                    "end_t":   int(df.index[end_idx].timestamp()),
                    "poc": round(float(vp["poc"]), 5),
                    "vah": round(float(vp["vah"]), 5),
                    "val": round(float(vp["val"]), 5),
                    "bin_edges":   vp["bin_edges"],
                    "bin_volumes": [round(v, 2) for v in vp["bin_volumes"]],
                    "candles":             end_idx - start_idx + 1,
                    "anchor_method":       anchor["method"],
                    "anchor_confidence":   anchor["confidence"],
                    "anchor_confirmed":    anchor.get("confirmed", False),
                    "anchor_active":       anchor.get("active", True),
                    "anchor_invalidation": anchor.get("invalidation_reason"),
                })
            except Exception as e:
                print(f"[VpStack/{ftf}] {e}")
        return jsonify({"symbol": symbol, "timeframes": results})
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
                # Anchor the fib to the most recent clean swing pivot
                # (_detect_auction_anchor) — the origin is that swing, extended to
                # the opposite extreme reached since. NOTE: as of the VP anchor
                # rework, Volume Profile uses a separate, VP-specific anchor
                # (_detect_vp_anchor, tuned to find the TRUE start of the current
                # range rather than the most recent pivot), so the Fib and VP boxes
                # may legitimately start at different candles now. Falls back to a
                # 150-bar window when no significant structure is detected.
                anchor = _detect_auction_anchor(df)
                if anchor is not None:
                    a_idx   = anchor["idx"]
                    a_price = float(anchor["price"])
                    win     = df.iloc[a_idx:]
                    a_ts    = int(df.index[a_idx].timestamp())
                    end_ts  = int(df.index[-1].timestamp())   # right edge of the chart (now)
                    # First anchor = the auction origin; the second anchor keeps the
                    # extreme PRICE but is pinned to the end of the graph, so the fib
                    # spans anchor → now exactly like the Volume Profile box.
                    if anchor["anchor_type"] == "swing_low":
                        # move began at a low → impulse up, retracement pulls down
                        going_up = True
                        sw_low   = a_price
                        sw_high  = float(win["high"].max())
                        ts_low   = a_ts
                        ts_high  = end_ts
                    else:
                        # move began at a high → impulse down
                        going_up = False
                        sw_high  = a_price
                        sw_low   = float(win["low"].min())
                        ts_high  = a_ts
                        ts_low   = end_ts
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
            # Pivot lookback scales with TF so major structural swings are captured.
            # A bar qualifies as a swing high/low only if it is the extreme across
            # lb bars on each side — too small a window picks up micro-noise.
            lb  = {'15m': 10, '30m': 12, '1h': 16, '4h': 24}.get(tf_str, 10)

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

            # Return only the stronger confirmed div + any forming divs
            candidates = [d for d in [bull_div, bear_div] if d]
            confirmed = ([max(candidates, key=lambda d: (d["ts_t2"], abs(d["rsi_t2"] - d["rsi_t1"])))]
                         if candidates else [])
            # Forming divergence: use same df with RSI column added
            try:
                df_rsi = df.copy()
                df_rsi["RSI"] = _an_rsi_series(df)
                forming = _detect_forming_divergence(df_rsi, lb=lb)
            except Exception:
                forming = []
            return confirmed + forming
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
                "is_converging": bool(g.is_converging),
                "breakout_up": bool(g.breakout_up),
                "breakout_dn": bool(g.breakout_dn),
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
            import traceback; print("[patterns.run] FAILED:"); traceback.print_exc()
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


@flask_app.route("/api/analysis/hypothetical", methods=["POST"])
@require_key
def api_analysis_hypothetical():
    """Fire a real, one-off question at Chev: 'if you had to take this, where would you enter?' —
    even for a setup he'd pass on live. Off-the-record: not gated, not logged, not posted anywhere.

    Both external calls this makes (candle fetch, Chev's LLM call) are run through a bounded
    .result(timeout=...) rather than called directly, so a hung upstream API (yfinance has no
    built-in timeout, and Chev's call can queue behind the live scanner's shared rate-limit lock)
    can't leave this request — and the browser waiting on it — hanging forever. The underlying
    thread is abandoned (not killed) on timeout; it doesn't touch any state the live loop depends on.
    """
    data   = flask_request.get_json(force=True, silent=True) or {}
    symbol = str(data.get("symbol", "")).upper()
    tf     = data.get("tf", "1h")
    if not symbol:
        return jsonify({"error": "Missing symbol"}), 400
    from concurrent.futures import TimeoutError as _FutTimeout
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        atype = _an_asset_type(symbol)
        try:
            result = ex.submit(scan_pair_tf, symbol, atype, tf).result(timeout=30)
        except _FutTimeout:
            return jsonify({"error": "Timed out fetching market data for this symbol (30s) — the candle source may be stuck, try again"}), 200
        if not result or not result.get("count"):
            return jsonify({"error": "No confluence detected — nothing to ask Chev about"}), 200

        balance = get_balance(dashboard_ws)
        try:
            chev_response, _ = ex.submit(
                ask_chev_to_judge, result, balance, dashboard_ws, timeout=120, force_take=True
            ).result(timeout=200)
        except _FutTimeout:
            return jsonify({"error": "Chev didn't respond within 200s (he may be mid-conversation with the live scanner) — try again shortly"}), 200
        parsed = parse_chev_reply(chev_response)
        if not parsed or not parsed.get("trade"):
            return jsonify({"error": "Chev didn't return usable numbers — try again"}), 200

        td          = parsed["trade"]
        conf_prices = _build_confluence_prices(td)
        planned_rr  = risk_gauntlet.compute_planned_rr(td["entry"], td["sl"], td["tp"], atype, td.get("trade_type", "day"))

        return jsonify({
            "symbol": symbol, "tf": tf, "direction": td["direction"],
            "entry": td["entry"], "sl": td["sl"], "tp": td["tp"],
            "tags": td["tags"], "trade_type": td["trade_type"],
            "confluence_prices": conf_prices, "planned_rr": planned_rr,
            "would_take": parsed.get("would_take"),
            "reasoning": td.get("reasoning"),
            "structure_4h": parsed.get("structure_4h"),
            "invalidation": parsed.get("invalidation"),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        ex.shutdown(wait=False)


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
ESCALATION_MODEL_ID = "chev-chelios-clone"  # lean escalation model in Open WebUI — its API id is "chev-chelios-clone" (display name "chev-escalation"); must match the model id registered in Open WebUI
# Learning sessions deliberately run on MODEL_ID (chat-Chev). REQUIREMENT: the
# chev-chelios system prompt must stay <= ~5k tokens or learning prompts will
# silently overflow the 12,288 ctx -- see handoff, learning-overflow finding
# 2026-07-11. Do not let the prompt regrow.

FIREBASE_URL  = "https://chev-monitor-default-rtdb.firebaseio.com"
JOURNAL_PATH       = os.path.join(CHEV_TOOLS_ROOT, "chev_journal.json")
JANE_JOURNAL_PATH  = os.path.join(CHEV_TOOLS_ROOT, "jane_journal.json")
PLAYBOOK_PATH      = os.path.join(CHEV_TOOLS_ROOT, "chev_playbook.txt")            # legacy / generic fallback
PLAYBOOK_PATHS     = {
    "forex":  os.path.join(CHEV_TOOLS_ROOT, "chev_playbook_forex.txt"),
    "crypto": os.path.join(CHEV_TOOLS_ROOT, "chev_playbook_crypto.txt"),
    "stocks": os.path.join(CHEV_TOOLS_ROOT, "chev_playbook_stocks.txt"),
}

def _norm_asset_type(v):
    """Normalize an asset_type value to the vocabulary PLAYBOOK_PATHS/the learning
    session use ("forex"/"crypto"/"stocks", plural stocks). WATCHLIST entries and
    every real trade/journal record use singular "stock" (dexter.py:3117-3141 ->
    log_new_trade -> the journal's own "asset_type" field) -- that's correct and
    must NOT be changed, since MAX_LEVERAGE_BY_TYPE, ESCALATION_TF_FLOOR, fee/
    slippage tables, and risk_gauntlet/labeller all key off the singular form
    consistently. This helper exists so the handful of sites that key off the
    PLURAL playbook vocabulary can compare/look up correctly without touching
    that singular convention anywhere else. None-safe: passthrough on None."""
    if v == "stock":
        return "stocks"
    return v

MODEL_ID = "chev-chelios"

TRADE_TYPE_EXPIRY_HOURS = {"scalp": 2, "day": 6, "swing": 48}

# Hallucination guard (PHASE 6): a POST whose entry/sl/tp can't belong to this chart at all
# (a training-era price, or the Chev Prompt's own format example leaking through) -- caught
# right after parsing, before GEOMETRY REVIEW does real math on numbers that were never real.
# 0.03 is deliberately generous: the DISTANCE pre-gate already caps a legitimate pending
# entry at 1.5% (exploration) from the confluence zone, so 3% is well beyond any valid
# MODE-B pending and only catches numbers that plainly don't belong to this asset right now.
HALLUCINATION_MAX_ENTRY_DIST = 0.03
HALLUCINATION_MAX_SLTP_DIST  = 0.15
MAX_LEVERAGE_BY_TYPE = {
    "crypto": {"scalp": 10, "day": 10, "swing": 5},
    "forex": {"scalp": 5, "day": 5, "swing": 2},
    "stock": {"scalp": 5, "day": 5, "swing": 2},
}

# Google Sheets connection
GOOGLE_CREDENTIALS_FILE = os.path.join(CHEV_TOOLS_ROOT, "google_credentials.json")
SHEET_ID = "1V1b2aU3SJu_R7VjFKGp9J6uFwucGSamhRWyq6jgCbFs"
TRADE_LOG_TAB         = "Trade Log"
JANE_TAB              = "Jane"
SKIP_LOG_TAB          = "Skip Log"   # every non-POST Chev decision — see _log_chev_decision

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

# Labeller `features` codes confirmed by Kev to be the same mechanic as the
# identically-named CONFLUENCE_SCORES key. Never add entries without that
# confirmation — see handoff PHASE 16 no-aliasing rule. (Confirmed 2026-07-11:
# labeller's REASON_MAP tokenizes dexter's own emitted reason strings for all
# three, so these are the same computation quoted twice, not a name collision.)
WEIGHT_LAB_VERIFIED_TAGS = {"gp", "rsi_ob", "rsi_os"}

# =============================================================================
# WEIGHT LAB — startup-only override loader
#
# Applies human-approved confluence tag weight deltas from weight_overrides.json
# to CONFLUENCE_SCORES, ONCE at startup only. Deliberately NOT a per-cycle hot-
# reload like tunables.json (PHASE 14, below) — a weight change is a bigger
# behavioral commitment than a tunable, so it requires an explicit Dexter
# restart to arm, never a silent mid-session change. The Weight Lab UI enforces
# this too (a loud "restart to arm" banner), but this loader does not trust the
# UI alone — see the hard safety clamp below.
#
# File format:
#   {"version": 1, "entries": [
#       {"tag": "oi_divergence", "delta": 1, "approved_at": "2026-07-11 14:03:00",
#        "evidence": {"n": 41, "coef": 0.31, "ci": [0.12, 0.49]}, "source": "weight_lab"}
#   ]}
#
# Deltas for the same tag across multiple entries are summed. A hard safety
# clamp (independent of the UI's own +/-1-per-approval cap — defense in depth)
# holds each tag's final value inside [0, 2 * its original value + 2]. A tag not
# present in CONFLUENCE_SCORES is ignored with a logged warning — never added
# as a new key. A missing file is the normal steady state (nothing logged); a
# malformed file logs a warning and applies nothing, startup continues normally.
# =============================================================================
WEIGHT_OVERRIDES_FILE = os.path.join(CHEV_TOOLS_ROOT, "weight_overrides.json")

# Snapshot of entries present in the file AT STARTUP (i.e. already baked into
# CONFLUENCE_SCORES this run). /api/weight_overrides compares the file's CURRENT
# contents against this to flag each entry "active" (applied this run) or not
# (approved during this session, awaiting the next restart to take effect).
_WEIGHT_OVERRIDES_STARTUP_SNAPSHOT = []


def _apply_weight_overrides():
    global _WEIGHT_OVERRIDES_STARTUP_SNAPSHOT
    if not os.path.exists(WEIGHT_OVERRIDES_FILE):
        return
    try:
        with open(WEIGHT_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("'entries' is not a list")
    except Exception as e:
        print(f"[WEIGHT OVERRIDES] malformed {WEIGHT_OVERRIDES_FILE}, applying nothing: {e}")
        return

    _WEIGHT_OVERRIDES_STARTUP_SNAPSHOT = list(entries)

    deltas = {}
    for entry in entries:
        try:
            tag = entry["tag"]
            delta = float(entry["delta"])
        except (KeyError, TypeError, ValueError):
            continue
        deltas[tag] = deltas.get(tag, 0.0) + delta

    applied = []
    for tag, total_delta in deltas.items():
        if tag not in CONFLUENCE_SCORES:
            print(f"[WEIGHT OVERRIDES] tag '{tag}' not in CONFLUENCE_SCORES -- ignored (never added as a new key)")
            continue
        original = CONFLUENCE_SCORES[tag]
        proposed = original + total_delta
        clamped = max(0, min(2 * original + 2, proposed))
        if clamped != original:
            CONFLUENCE_SCORES[tag] = clamped
            applied.append((tag, original, clamped))

    if applied:
        summary = ", ".join(f"{t} {o:g}→{c:g}" for t, o, c in applied)
        print(f"WEIGHT OVERRIDES: {len(applied)} tags adjusted ({summary})")


_apply_weight_overrides()

# =============================================================================
# PHASE 16: TAG REGISTRY — one entry per confluence code the system can emit, across
# all THREE vocabularies that actually render on the Strategy dashboard:
#   1. CONFLUENCE_SCORES above (Dexter's own weights table).
#   2. Chev's free-typed `tags=` field on POSTed trades (chev_journal.json) — a superset
#      of #1 plus some codes Chev has typed that were never in the weights table at all
#      (spelling drift, or shorthand with no formal scoring weight).
#   3. labeller.py's normalize_reasons() tokens (REASON_MAP_ORDERED + sr_multi/sr_single +
#      pattern_* + "other") — used only by the Shadow Tag/Combo leaderboards (PHASE 7B-3),
#      a differently-named vocabulary (percentage-based Fib levels, no-underscore EMA
#      periods, etc.) that mostly does NOT string-match the weights table.
#
# `points` is deliberately NOT a field on these entries — /api/tag_registry looks it up
# fresh from CONFLUENCE_SCORES at serve time (the one and only source of truth for score
# contribution; retuning a weight in code is automatically reflected on the site, nothing
# to keep in sync by hand). A code with no CONFLUENCE_SCORES entry serves `points: null` —
# true for every #3-only shadow token except gp/rsi_ob/rsi_os (which DO string-match), and
# for a handful of things Chev has typed that never had a formal weight (see below).
#
# A few codes are honest best-guesses, not confirmed anywhere in code — flagged in their
# own tooltip text rather than silently asserted: `hd`/`rd` (Chev's own journal shorthand,
# never defined anywhere; inferred from labeller.py's own hidden/regular divergence
# distinction) and `tp_balance_extreme` (no definition found anywhere in the codebase).
# =============================================================================
TAG_REGISTRY = {
    # ── Support / Resistance ────────────────────────────────────────────────
    "sr_4h": {"name": "Support/Resistance (4H)",
              "tooltip": "A support or resistance level identified on the 4-hour chart.",
              "definition": "A price area where the market has repeatedly reversed or paused in the past, so traders expect buying or selling pressure to reappear there. The 4H version carries the most weight of any timeframe because a level respected on a slower chart tends to hold longer.",
              "how": "Counted from swing highs/lows that cluster near the same price on the 4H candles; more touches at the same level score higher."},
    "sr_1h":  {"name": "Support/Resistance (1H)",
               "tooltip": "A support or resistance level identified on the 1-hour chart.",
               "definition": "Same concept as the 4H version — a price area where the market has previously reversed or paused — but read from the 1-hour chart, so it reacts faster and carries less weight.",
               "how": "Counted from swing highs/lows that cluster near the same price on the 1H candles."},
    "sr_30m": {"name": "Support/Resistance (30M)",
               "tooltip": "A support or resistance level identified on the 30-minute chart.",
               "definition": "Same concept as the 4H/1H versions, read from the 30-minute chart — a faster, lower-weight structural level.",
               "how": "Counted from swing highs/lows that cluster near the same price on the 30M candles."},
    "sr_15m": {"name": "Support/Resistance (15M)",
               "tooltip": "A support or resistance level identified on the 15-minute chart.",
               "definition": "Same concept as the other SR tags, read from the 15-minute chart — the fastest, lowest-weight structural level Dexter scores.",
               "how": "Counted from swing highs/lows that cluster near the same price on the 15M candles."},
    "sr":     {"name": "Support/Resistance",
               "tooltip": "A support or resistance level without a specific timeframe recorded.",
               "definition": "The general support/resistance concept — a price area where the market has previously reversed — logged without a timeframe tag attached.",
               "how": "Same touch-clustering logic as the timeframe-specific SR tags, timeframe unspecified."},
    "sr_multi":  {"name": "Support/Resistance (Multiple Touches)",
                  "tooltip": "A support/resistance level price has touched 3 or more times.",
                  "definition": "A level is more trustworthy the more times price has approached and reversed from it without breaking through. This tag marks a level with 3+ recorded touches — the strongest tier of the touch-count check used for the shadow (Examiner) leaderboards.",
                  "how": "Read from Dexter's own reason text (e.g. \"Resistance(3x,3pt)\"); a touch count of 3 or more is classified sr_multi, below that is sr_single."},
    "sr_single": {"name": "Support/Resistance (1-2 Touches)",
                  "tooltip": "A support/resistance level price has touched only once or twice.",
                  "definition": "The weaker tier of the touch-count check — a level with only 1-2 recorded touches so far, less proven than sr_multi but still a real structural reference.",
                  "how": "Read from Dexter's own reason text (e.g. \"Support(2x,2pt)\"); a touch count below 3 is classified sr_single."},

    # ── Fibonacci retracement — timeframe-based (weights table) ────────────
    "fib_4h": {"name": "Fibonacci Retracement (4H)",
               "tooltip": "Price sitting at a Fibonacci retracement level on the 4-hour chart.",
               "definition": "Fibonacci retracement levels (50%, 61.8%, 65%, 78.6% of a recent swing) mark spots traders commonly watch for a bounce or rejection during a pullback. The 4H version is measured off the largest, slowest swing and carries the most weight.",
               "how": "Computed from the highest-high/lowest-low swing on the 4H chart; price sitting within a small tolerance of one of the retracement prices scores this tag."},
    "fib_1h": {"name": "Fibonacci Retracement (1H)",
               "tooltip": "Price sitting at a Fibonacci retracement level on the 1-hour chart.",
               "definition": "Same Fibonacci retracement concept as the 4H version, measured off the 1-hour swing — reacts faster, carries less weight.",
               "how": "Computed from the swing high/low on the 1H chart; price near a retracement price scores this tag."},
    "fib_30m": {"name": "Fibonacci Retracement (30M)",
                "tooltip": "Price sitting at a Fibonacci retracement level on the 30-minute chart.",
                "definition": "Same Fibonacci retracement concept, measured off the 30-minute swing.",
                "how": "Computed from the swing high/low on the 30M chart; price near a retracement price scores this tag."},
    "fib_15m": {"name": "Fibonacci Retracement (15M)",
                "tooltip": "Price sitting at a Fibonacci retracement level on the 15-minute chart.",
                "definition": "Same Fibonacci retracement concept, measured off the fastest swing Dexter scores.",
                "how": "Computed from the swing high/low on the 15M chart; price near a retracement price scores this tag."},
    "fib":    {"name": "Fibonacci Retracement",
               "tooltip": "Price at a Fibonacci retracement level without a specific timeframe recorded.",
               "definition": "The general Fibonacci retracement concept, logged without a timeframe tag attached.",
               "how": "Same retracement-level logic as the timeframe-specific fib tags, timeframe unspecified."},
    "fib_5m": {"name": "Fibonacci Retracement (5M)",
               "tooltip": "Price at a Fibonacci retracement level on the 5-minute chart.",
               "definition": "Same Fibonacci retracement concept, measured off the 5-minute swing. Not a formal Dexter weight — Chev has typed this in his own tags field, but the 5-minute timeframe isn't in Dexter's scored Fibonacci table.",
               "how": "Same retracement-level logic as the other fib tags; 5M isn't one of the timeframes CONFLUENCE_SCORES assigns a point value to."},

    # ── Fibonacci retracement — percentage-based (shadow-only vocabulary) ───
    "fib_618":  {"name": "Fibonacci 61.8%",
                 "tooltip": "Price at the 61.8% Fibonacci retracement level — the golden ratio.",
                 "definition": "The 61.8% retracement is the most-watched Fibonacci level, often called the golden ratio. It marks the deep-pullback boundary of the Golden Pocket zone.",
                 "how": "Computed as the swing high minus 61.8% of the swing's price range (or the mirrored calculation for a downswing)."},
    "fib_50":   {"name": "Fibonacci 50%",
                 "tooltip": "Price at the 50% retracement of a recent swing.",
                 "definition": "The halfway point of a recent price swing — not a true Fibonacci ratio, but the level traders watch most often alongside the real Fibonacci levels.",
                 "how": "Computed as the exact midpoint between the swing high and swing low."},
    "fib_786":  {"name": "Fibonacci 78.6%",
                 "tooltip": "Price at the 78.6% Fibonacci retracement level.",
                 "definition": "A deep retracement level — a pullback this far back into the prior swing is considered a late, high-risk entry zone by most Fibonacci traders.",
                 "how": "Computed as the swing high minus 78.6% of the swing's price range (or the mirrored calculation for a downswing)."},
    "fib_382":  {"name": "Fibonacci 38.2%",
                 "tooltip": "Price at the 38.2% Fibonacci retracement level.",
                 "definition": "A shallow retracement level — a pullback this small suggests the prior trend is still strong.",
                 "how": "Computed as the swing high minus 38.2% of the swing's price range (or the mirrored calculation for a downswing)."},
    "fib_236":  {"name": "Fibonacci 23.6%",
                 "tooltip": "Price at the 23.6% Fibonacci retracement level.",
                 "definition": "The shallowest standard Fibonacci retracement level — a very minor pullback.",
                 "how": "Computed as the swing high minus 23.6% of the swing's price range (or the mirrored calculation for a downswing)."},
    "fib_other": {"name": "Fibonacci (Other Level)",
                  "tooltip": "Price at a Fibonacci-family level not covered by the standard buckets.",
                  "definition": "A catch-all for a Fibonacci-related reason string that didn't match one of the named percentage levels.",
                  "how": "Assigned whenever Dexter's reason text contains \"Fib\" but doesn't match one of the specific percentage patterns."},

    # ── Golden Pocket ─────────────────────────────────────────────────────
    "gp": {"name": "Golden Pocket",
           "tooltip": "Price sitting inside the Golden Pocket — the 50%-61.8% Fibonacci zone.",
           "definition": "The Golden Pocket is the price band between the 50% and 61.8% Fibonacci retracement levels of a recent swing — widely considered the highest-probability reversal zone in Fibonacci trading. It is Dexter's single highest-weighted individual confluence tag.",
           "how": "Flagged when the current price sits between the chart's computed 50% and 61.8% retracement prices (with a small tolerance either side)."},
    "gp_sr_combo": {"name": "Golden Pocket + S/R Combo",
                    "tooltip": "A Golden Pocket zone that also lines up with a support/resistance level.",
                    "definition": "A Golden Pocket carries extra weight when it lands on top of an independent support/resistance level — two different tools agreeing on the same price is a stronger signal than either alone.",
                    "how": "Flagged from Dexter's own \"GP*SR DEADLY COMBO\" reason text, emitted when a Golden Pocket zone and an SR level overlap."},

    # ── RSI divergence — confirmed (weights table) ──────────────────────────
    "rsi_4h": {"name": "RSI Divergence (4H, Confirmed)",
               "tooltip": "A confirmed RSI divergence on the 4-hour chart.",
               "definition": "RSI divergence is when price makes a new high/low but the RSI momentum indicator does not follow — a classic warning that the current move is losing strength. \"Confirmed\" means the divergence pattern has fully completed, not still forming. The 4H version carries the most weight.",
               "how": "Compares price swing highs/lows against RSI swing highs/lows on the 4H chart; a completed mismatch (price higher, RSI lower, or vice versa) scores this tag."},
    "rsi_1h": {"name": "RSI Divergence (1H, Confirmed)",
               "tooltip": "A confirmed RSI divergence on the 1-hour chart.",
               "definition": "Same confirmed RSI divergence concept as the 4H version, read from the 1-hour chart.",
               "how": "Compares price swing highs/lows against RSI swing highs/lows on the 1H chart."},
    "rsi_30m": {"name": "RSI Divergence (30M, Confirmed)",
                "tooltip": "A confirmed RSI divergence on the 30-minute chart.",
                "definition": "Same confirmed RSI divergence concept, read from the 30-minute chart.",
                "how": "Compares price swing highs/lows against RSI swing highs/lows on the 30M chart."},
    "rsi_15m": {"name": "RSI Divergence (15M, Confirmed)",
                "tooltip": "A confirmed RSI divergence on the 15-minute chart.",
                "definition": "Same confirmed RSI divergence concept, read from the 15-minute chart — the fastest RSI divergence Dexter scores.",
                "how": "Compares price swing highs/lows against RSI swing highs/lows on the 15M chart."},
    "rsi":    {"name": "RSI Divergence (Confirmed)",
               "tooltip": "A confirmed RSI divergence without a specific timeframe recorded.",
               "definition": "The general confirmed RSI divergence concept, logged without a timeframe tag attached.",
               "how": "Same price-vs-RSI comparison as the timeframe-specific rsi tags, timeframe unspecified."},

    # ── RSI divergence — forming / not yet confirmed (weights table) ───────
    "rsi_form_4h": {"name": "RSI Divergence Forming (4H)",
                    "tooltip": "An RSI divergence on the 4-hour chart that hasn't fully confirmed yet.",
                    "definition": "The same price-vs-RSI mismatch as a confirmed divergence, but still developing — the final swing point hasn't closed yet, so it could still resolve either way. Scored lower than a confirmed divergence for that reason.",
                    "how": "Same comparison logic as rsi_4h, but flagged while the most recent swing point is still active rather than finalized."},
    "rsi_form_1h": {"name": "RSI Divergence Forming (1H)",
                    "tooltip": "An RSI divergence on the 1-hour chart that hasn't fully confirmed yet.",
                    "definition": "Same forming-divergence concept as the 4H version, read from the 1-hour chart.",
                    "how": "Same comparison logic as rsi_1h, flagged while the most recent swing point is still developing."},
    "rsi_form_30m": {"name": "RSI Divergence Forming (30M)",
                     "tooltip": "An RSI divergence on the 30-minute chart that hasn't fully confirmed yet.",
                     "definition": "Same forming-divergence concept, read from the 30-minute chart.",
                     "how": "Same comparison logic as rsi_30m, flagged while the most recent swing point is still developing."},
    "rsi_form_15m": {"name": "RSI Divergence Forming (15M)",
                     "tooltip": "An RSI divergence on the 15-minute chart that hasn't fully confirmed yet.",
                     "definition": "Same forming-divergence concept, read from the 15-minute chart.",
                     "how": "Same comparison logic as rsi_15m, flagged while the most recent swing point is still developing."},
    "rsi_form": {"name": "RSI Divergence Forming",
                 "tooltip": "An RSI divergence without a specific timeframe recorded that hasn't fully confirmed yet.",
                 "definition": "The general forming-divergence concept, logged without a timeframe tag attached.",
                 "how": "Same comparison logic as the timeframe-specific rsi_form tags, timeframe unspecified."},

    # ── RSI divergence — shadow vocabulary's own type distinction ──────────
    "rsi_div": {"name": "RSI Divergence",
                "tooltip": "A confirmed RSI divergence (shadow-log naming).",
                "definition": "The same confirmed price-vs-RSI mismatch described under the timeframe-specific rsi tags, recorded under the Examiner's own generic \"Divergence\" reason text.",
                "how": "Assigned whenever Dexter's reason text contains \"Divergence\"/\"divergence\" but isn't specifically flagged Hidden or Regular."},
    "rsi_div_hidden": {"name": "Hidden Divergence",
                       "tooltip": "A hidden RSI divergence — a continuation signal, not a reversal signal.",
                       "definition": "Hidden divergence is the less-common counterpart to regular divergence: price makes a shallower high/low while RSI makes a deeper one, which technical traders read as the existing trend continuing rather than reversing.",
                       "how": "Assigned from Dexter's own \"Hidden Bullish\"/\"Hidden Bearish\" reason text."},
    "rsi_div_regular": {"name": "Regular Divergence",
                        "tooltip": "A regular RSI divergence — the classic reversal-warning signal.",
                        "definition": "The standard divergence pattern: price pushes to a new high/low that RSI does not confirm, a warning sign the current trend may be running out of momentum.",
                        "how": "Assigned from Dexter's own \"Regular Bullish\"/\"Regular Bearish\" reason text."},
    "rsi_div_forming": {"name": "RSI Divergence Forming (Shadow)",
                        "tooltip": "An RSI divergence still developing, not yet confirmed (shadow-log naming).",
                        "definition": "The same still-developing divergence concept described under rsi_form, recorded under the Examiner's own reason text for a pattern that hasn't finished forming.",
                        "how": "Assigned from Dexter's own \"[WATCH - not yet confirmed]\" reason prefix."},

    # ── RSI level signals (weights table) ───────────────────────────────────
    "rsi_ob": {"name": "RSI Overbought",
               "tooltip": "RSI has moved into overbought territory.",
               "definition": "RSI (Relative Strength Index) above roughly 70 is considered overbought — the market has moved up quickly enough that a pause or pullback becomes more likely, though it isn't a guarantee.",
               "how": "Flagged when RSI crosses above its overbought threshold on the relevant chart."},
    "rsi_os": {"name": "RSI Oversold",
               "tooltip": "RSI has moved into oversold territory.",
               "definition": "RSI below roughly 30 is considered oversold — the market has moved down quickly enough that a bounce becomes more likely, though it isn't a guarantee.",
               "how": "Flagged when RSI crosses below its oversold threshold on the relevant chart."},
    "rsi_50":  {"name": "RSI at 50",
                "tooltip": "RSI sitting at or crossing its 50 midline.",
                "definition": "The RSI midline (50) separates bullish momentum (above) from bearish momentum (below) — a cross of this line is often read as an early momentum shift.",
                "how": "Flagged when RSI is at or has recently crossed the 50 level."},
    "rsi_50_cross": {"name": "RSI Crossed 50",
                     "tooltip": "RSI has just crossed its 50 midline (shadow-log naming).",
                     "definition": "The same RSI-midline concept as rsi_50, recorded under the Examiner's own \"RSI crossed 50\" reason text.",
                     "how": "Assigned from Dexter's own \"RSI crossed 50\" reason text, with the bar count since the cross."},

    # ── Journal shorthand for divergence type (Chev's own typing, no formal weight) ──
    "hd": {"name": "Hidden Divergence (shorthand)",
           "tooltip": "Best-guess reading: Chev's own shorthand for Hidden Divergence — not formally defined anywhere in code.",
           "definition": "This exact code isn't produced by Dexter's scorer or defined anywhere in the codebase — it only appears in Chev's own free-typed tags field. Given the system elsewhere distinguishes Hidden vs Regular divergence (see rsi_div_hidden), \"hd\" is inferred to mean the same thing, abbreviated. Treat this inference as unconfirmed.",
           "how": "Not computed by Dexter — this is a value Chev typed by hand."},
    "rd": {"name": "Regular Divergence (shorthand)",
           "tooltip": "Best-guess reading: Chev's own shorthand for Regular Divergence — not formally defined anywhere in code.",
           "definition": "This exact code isn't produced by Dexter's scorer or defined anywhere in the codebase — it only appears in Chev's own free-typed tags field. Given the system elsewhere distinguishes Hidden vs Regular divergence (see rsi_div_regular), \"rd\" is inferred to mean the same thing, abbreviated. Treat this inference as unconfirmed.",
           "how": "Not computed by Dexter — this is a value Chev typed by hand."},

    # ── EMA (weights table, underscore form) ────────────────────────────────
    "ema_55": {"name": "EMA 55",
               "tooltip": "Price testing or crossing the 55-period Exponential Moving Average.",
               "definition": "The 55-EMA is the slowest of Dexter's three tracked EMAs, used as the primary trend reference — price holding above or below it is read as a longer-term trend signal. It carries the most weight of the three EMA periods.",
               "how": "Flagged when price is at, or has just crossed, the 55-period EMA on the relevant chart."},
    "ema_21": {"name": "EMA 21",
               "tooltip": "Price testing or crossing the 21-period Exponential Moving Average.",
               "definition": "The 21-EMA is Dexter's medium-speed trend reference, faster to react than the 55-EMA and slower than the 13-EMA.",
               "how": "Flagged when price is at, or has just crossed, the 21-period EMA on the relevant chart."},
    "ema_13": {"name": "EMA 13",
               "tooltip": "Price testing or crossing the 13-period Exponential Moving Average.",
               "definition": "The 13-EMA is Dexter's fastest tracked EMA, reacting quickest to short-term price moves.",
               "how": "Flagged when price is at, or has just crossed, the 13-period EMA on the relevant chart."},
    "ema_4h": {"name": "EMA Test/Cross (4H)",
               "tooltip": "An EMA test or crossover event on the 4-hour chart (legacy tag).",
               "definition": "An older, timeframe-qualified EMA tag kept only so past journal entries still resolve to a name — new entries use the period-specific tags (ema_55/ema_21/ema_13) instead.",
               "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "ema_1h": {"name": "EMA Test/Cross (1H)",
               "tooltip": "An EMA test or crossover event on the 1-hour chart (legacy tag).",
               "definition": "An older, timeframe-qualified EMA tag kept only so past journal entries still resolve to a name.",
               "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "ema_30m": {"name": "EMA Test/Cross (30M)",
                "tooltip": "An EMA test or crossover event on the 30-minute chart (legacy tag).",
                "definition": "An older, timeframe-qualified EMA tag kept only so past journal entries still resolve to a name.",
                "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "ema_15m": {"name": "EMA Test/Cross (15M)",
                "tooltip": "An EMA test or crossover event on the 15-minute chart (legacy tag).",
                "definition": "An older, timeframe-qualified EMA tag kept only so past journal entries still resolve to a name.",
                "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "ema": {"name": "EMA Test/Cross",
            "tooltip": "An EMA test or crossover event without a specific period recorded.",
            "definition": "The general EMA concept, logged without a period tag attached.",
            "how": "Legacy/generic alias for an EMA-related reason."},
    "ema_5m": {"name": "EMA Test/Cross (5M)",
               "tooltip": "An EMA test or crossover event on the 5-minute chart.",
               "definition": "Same EMA concept as the other EMA tags, on the 5-minute chart. Not a formal Dexter weight — Chev has typed this in his own tags field, but 5-minute EMA isn't in Dexter's scored table.",
               "how": "Not in CONFLUENCE_SCORES — 5-minute EMA isn't a timeframe Dexter's scorer assigns a point value to."},

    # ── EMA (shadow vocabulary, no-underscore period + explicit crossover) ──
    "ema55": {"name": "EMA 55",
              "tooltip": "Price testing or crossing the 55-period Exponential Moving Average (shadow-log naming).",
              "definition": "The same 55-EMA concept described under ema_55, recorded under the Examiner's own no-underscore naming.",
              "how": "Assigned from Dexter's own \"EMA55\" reason text."},
    "ema21": {"name": "EMA 21",
              "tooltip": "Price testing or crossing the 21-period Exponential Moving Average (shadow-log naming).",
              "definition": "The same 21-EMA concept described under ema_21, recorded under the Examiner's own no-underscore naming.",
              "how": "Assigned from Dexter's own \"EMA21\" reason text."},
    "ema13": {"name": "EMA 13",
              "tooltip": "Price testing or crossing the 13-period Exponential Moving Average (shadow-log naming).",
              "definition": "The same 13-EMA concept described under ema_13, recorded under the Examiner's own no-underscore naming.",
              "how": "Assigned from Dexter's own \"EMA13\" reason text."},
    "ema_cross": {"name": "EMA Crossover",
                  "tooltip": "Two of Dexter's tracked EMAs have just crossed each other.",
                  "definition": "An EMA crossover — one moving average crossing above or below another — is a classic trend-change signal, distinct from price simply testing a single EMA.",
                  "how": "Assigned from Dexter's own \"EMA crossover\" reason text."},

    # ── Bollinger Bands (weights table, timeframe-based) ────────────────────
    "bb_4h": {"name": "Bollinger Bands (4H)",
              "tooltip": "A Bollinger Band signal (squeeze, touch, or expansion) on the 4-hour chart.",
              "definition": "Bollinger Bands measure price volatility around a moving average; a squeeze (narrow bands) often precedes a big move, while a touch of the upper/lower band flags a price extreme relative to recent volatility. The 4H version carries the most weight.",
              "how": "Computed from the standard deviation of price around its moving average on the 4H chart."},
    "bb_1h": {"name": "Bollinger Bands (1H)",
              "tooltip": "A Bollinger Band signal on the 1-hour chart.",
              "definition": "Same Bollinger Band concept as the 4H version, read from the 1-hour chart.",
              "how": "Computed from the standard deviation of price around its moving average on the 1H chart."},
    "bb_30m": {"name": "Bollinger Bands (30M)",
               "tooltip": "A Bollinger Band signal on the 30-minute chart.",
               "definition": "Same Bollinger Band concept, read from the 30-minute chart.",
               "how": "Computed from the standard deviation of price around its moving average on the 30M chart."},
    "bb_15m": {"name": "Bollinger Bands (15M)",
               "tooltip": "A Bollinger Band signal on the 15-minute chart.",
               "definition": "Same Bollinger Band concept, read from the 15-minute chart.",
               "how": "Computed from the standard deviation of price around its moving average on the 15M chart."},
    "bb": {"name": "Bollinger Bands",
           "tooltip": "A Bollinger Band signal without a specific timeframe recorded.",
           "definition": "The general Bollinger Band concept, logged without a timeframe tag attached.",
           "how": "Same volatility-band logic as the timeframe-specific bb tags, timeframe unspecified."},

    # ── Bollinger Bands (shadow vocabulary — specific mechanic, no weight match) ──
    "bb_squeeze": {"name": "Bollinger Band Squeeze",
                   "tooltip": "The Bollinger Bands have narrowed sharply — volatility is unusually low.",
                   "definition": "A squeeze is when the bands pull in tight around price, meaning recent volatility has dropped well below normal. Traders watch a squeeze as an early warning that a large move is building, without knowing the direction yet.",
                   "how": "Flagged when the band width (as a % of price) falls under a low threshold on the relevant chart. Not a formal Dexter weight — Chev has also typed variants of this (\"bbsq\", \"bbsqueeze\") that carry the identical meaning."},
    "bbsq": {"name": "Bollinger Band Squeeze",
             "tooltip": "Same as Bollinger Band Squeeze — an alternate spelling Chev has typed.",
             "definition": "Identical concept to bb_squeeze; this is simply a shorter spelling Chev has used in his own tags field at different points.",
             "how": "Not computed by Dexter under this exact spelling — this is a value Chev typed by hand, meaning the same thing as bb_squeeze."},
    "bbsqueeze": {"name": "Bollinger Band Squeeze",
                  "tooltip": "Same as Bollinger Band Squeeze — an alternate spelling Chev has typed.",
                  "definition": "Identical concept to bb_squeeze; this is simply a different spelling Chev has used in his own tags field at different points.",
                  "how": "Not computed by Dexter under this exact spelling — this is a value Chev typed by hand, meaning the same thing as bb_squeeze."},
    "bb_burst": {"name": "Bollinger Band Burst",
                 "tooltip": "Price has punched through the upper or lower Bollinger Band with force.",
                 "definition": "A burst is a strong, fast push through the band's edge — the opposite situation to a squeeze, signalling the volatility expansion is already underway rather than still building.",
                 "how": "Assigned from Dexter's own \"BB upper BURST\"/\"BB lower BURST\" reason text."},
    "bb_near": {"name": "Bollinger Band Edge",
                "tooltip": "Price is sitting near the upper or lower Bollinger Band.",
                "definition": "Price approaching but not yet punching through a band edge — a milder version of a burst, flagging a price extreme relative to recent volatility.",
                "how": "Assigned from Dexter's own \"BB near upper\"/\"BB near lower\" reason text."},
    "bb_mid": {"name": "Bollinger Band Midline",
               "tooltip": "Price sitting at the Bollinger Bands' middle moving-average line.",
               "definition": "The midline of the Bollinger Bands is itself a moving average — price sitting on it is often read as a balance point between the recent high and low volatility extremes.",
               "how": "Assigned from Dexter's own \"BB mid\" reason text."},

    # ── Chart patterns — reversal (weights table) ───────────────────────────
    "hs":  {"name": "Head & Shoulders",
            "tooltip": "A completed Head & Shoulders pattern — a classic bearish reversal shape.",
            "definition": "A Head & Shoulders pattern is three peaks, with the middle one (the head) higher than the two outer ones (the shoulders) — traditionally read as a topping signal that a rally is ending.",
            "how": "Detected geometrically from the sequence of swing highs matching the three-peak shape."},
    "ihs": {"name": "Inverse Head & Shoulders",
            "tooltip": "A completed Inverse Head & Shoulders pattern — a classic bullish reversal shape.",
            "definition": "The mirror image of a Head & Shoulders — three troughs, with the middle one lower than the two outer ones — traditionally read as a bottoming signal that a decline is ending.",
            "how": "Detected geometrically from the sequence of swing lows matching the three-trough shape."},
    "double_top": {"name": "Double Top",
                   "tooltip": "A completed Double Top pattern — a bearish reversal shape.",
                   "definition": "Two peaks at roughly the same price level with a pullback between them — a classic sign that buyers have twice failed to push higher.",
                   "how": "Detected geometrically from two swing highs at a similar price with a confirmed pullback between them."},
    "double_bottom": {"name": "Double Bottom",
                      "tooltip": "A completed Double Bottom pattern — a bullish reversal shape.",
                      "definition": "The mirror image of a Double Top — two troughs at roughly the same price level, a classic sign that sellers have twice failed to push lower.",
                      "how": "Detected geometrically from two swing lows at a similar price with a confirmed bounce between them."},
    "triple_top": {"name": "Triple Top",
                   "tooltip": "A completed Triple Top pattern — a rarer, stronger bearish reversal shape.",
                   "definition": "Three peaks at roughly the same price level — a rarer variant of the Double Top, scored higher because a level that has rejected price three times is a stronger signal.",
                   "how": "Detected geometrically from three swing highs at a similar price."},
    "triple_bottom": {"name": "Triple Bottom",
                      "tooltip": "A completed Triple Bottom pattern — a rarer, stronger bullish reversal shape.",
                      "definition": "The mirror image of a Triple Top — three troughs at roughly the same price level.",
                      "how": "Detected geometrically from three swing lows at a similar price."},
    "rising_wedge": {"name": "Rising Wedge",
                     "tooltip": "A Rising Wedge pattern — typically a bearish reversal shape.",
                     "definition": "A Rising Wedge is a narrowing price channel that slopes upward — despite the upward slope, it's traditionally read as a bearish pattern because the narrowing range shows buying pressure is weakening.",
                     "how": "Detected geometrically from two converging, upward-sloping trendlines connecting recent swing highs and lows."},
    "falling_wedge": {"name": "Falling Wedge",
                      "tooltip": "A Falling Wedge pattern — typically a bullish reversal shape.",
                      "definition": "The mirror image of a Rising Wedge — a narrowing channel that slopes downward, traditionally read as bullish because the narrowing range shows selling pressure is weakening.",
                      "how": "Detected geometrically from two converging, downward-sloping trendlines connecting recent swing highs and lows."},

    # ── Chart patterns — continuation (weights table) ───────────────────────
    "bull_flag": {"name": "Bull Flag",
                  "tooltip": "A Bull Flag pattern — a brief pause expected to resolve upward.",
                  "definition": "A short, tight consolidation (the flag) after a sharp upward move (the flagpole) — traditionally read as a pause before the prior uptrend continues.",
                  "how": "Detected geometrically from a strong prior up-move followed by a narrow, mildly downward or sideways consolidation channel."},
    "bear_flag": {"name": "Bear Flag",
                  "tooltip": "A Bear Flag pattern — a brief pause expected to resolve downward.",
                  "definition": "The mirror image of a Bull Flag — a narrow consolidation after a sharp downward move, traditionally read as a pause before the prior downtrend continues.",
                  "how": "Detected geometrically from a strong prior down-move followed by a narrow, mildly upward or sideways consolidation channel."},
    "bull_pennant": {"name": "Bull Pennant",
                     "tooltip": "A Bull Pennant pattern — a converging pause expected to resolve upward.",
                     "definition": "Similar to a Bull Flag but the consolidation converges to a point (like a small symmetrical triangle) rather than running parallel.",
                     "how": "Detected geometrically from a strong prior up-move followed by a converging consolidation."},
    "bear_pennant": {"name": "Bear Pennant",
                     "tooltip": "A Bear Pennant pattern — a converging pause expected to resolve downward.",
                     "definition": "The mirror image of a Bull Pennant — a converging consolidation after a sharp downward move.",
                     "how": "Detected geometrically from a strong prior down-move followed by a converging consolidation."},
    "bull_channel": {"name": "Bull Channel",
                     "tooltip": "Price is moving up inside a parallel upward channel.",
                     "definition": "A sustained uptrend bounded by two roughly parallel upward-sloping trendlines — one connecting the swing lows, one connecting the swing highs.",
                     "how": "Detected geometrically from two parallel, upward-sloping trendlines connecting recent swing highs and lows."},
    "bear_channel": {"name": "Bear Channel",
                     "tooltip": "Price is moving down inside a parallel downward channel.",
                     "definition": "The mirror image of a Bull Channel — a sustained downtrend bounded by two roughly parallel downward-sloping trendlines.",
                     "how": "Detected geometrically from two parallel, downward-sloping trendlines connecting recent swing highs and lows."},
    "asc_tri": {"name": "Ascending Triangle",
                "tooltip": "An Ascending Triangle pattern — typically a bullish continuation shape.",
                "definition": "A flat resistance line with a rising support line underneath — buyers are stepping in at progressively higher prices while sellers defend one fixed level, usually resolving upward when that level breaks.",
                "how": "Detected geometrically from a flat upper trendline and a rising lower trendline."},
    "desc_tri": {"name": "Descending Triangle",
                 "tooltip": "A Descending Triangle pattern — typically a bearish continuation shape.",
                 "definition": "The mirror image of an Ascending Triangle — a flat support line with a falling resistance line above it, usually resolving downward when that level breaks.",
                 "how": "Detected geometrically from a flat lower trendline and a falling upper trendline."},
    "sym_tri": {"name": "Symmetrical Triangle",
                "tooltip": "A Symmetrical Triangle pattern — a converging pause with no directional bias of its own.",
                "definition": "Two converging trendlines, one rising and one falling, squeezing price into a point — a pause in trading that can resolve in either direction, so it's read alongside the broader trend rather than on its own.",
                "how": "Detected geometrically from two converging trendlines, one rising and one falling."},
    "rectangle": {"name": "Rectangle (Range)",
                  "tooltip": "Price is bouncing between a flat floor and a flat ceiling.",
                  "definition": "A simple horizontal trading range bounded by a repeated support level below and a repeated resistance level above.",
                  "how": "Detected geometrically from roughly flat upper and lower trendlines connecting recent swing highs and lows."},

    # ── Chart patterns — legacy triangle timeframe aliases (weights table) ──
    "tri_4h": {"name": "Triangle (4H, legacy)",
               "tooltip": "A triangle pattern on the 4-hour chart (legacy tag).",
               "definition": "An older, timeframe-qualified triangle tag kept only so past journal entries still resolve to a name — new entries use the shape-specific tags (asc_tri/desc_tri/sym_tri) instead.",
               "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "tri_1h": {"name": "Triangle (1H, legacy)",
               "tooltip": "A triangle pattern on the 1-hour chart (legacy tag).",
               "definition": "An older, timeframe-qualified triangle tag kept only so past journal entries still resolve to a name.",
               "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "tri_30m": {"name": "Triangle (30M, legacy)",
                "tooltip": "A triangle pattern on the 30-minute chart (legacy tag).",
                "definition": "An older, timeframe-qualified triangle tag kept only so past journal entries still resolve to a name.",
                "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "tri_15m": {"name": "Triangle (15M, legacy)",
                "tooltip": "A triangle pattern on the 15-minute chart (legacy tag).",
                "definition": "An older, timeframe-qualified triangle tag kept only so past journal entries still resolve to a name.",
                "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "triangle_4h": {"name": "Triangle (4H, legacy)",
                    "tooltip": "A triangle pattern on the 4-hour chart (legacy tag).",
                    "definition": "An older, longer-named triangle tag kept only so past journal entries still resolve to a name.",
                    "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "triangle_1h": {"name": "Triangle (1H, legacy)",
                    "tooltip": "A triangle pattern on the 1-hour chart (legacy tag).",
                    "definition": "An older, longer-named triangle tag kept only so past journal entries still resolve to a name.",
                    "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "triangle_30m": {"name": "Triangle (30M, legacy)",
                     "tooltip": "A triangle pattern on the 30-minute chart (legacy tag).",
                     "definition": "An older, longer-named triangle tag kept only so past journal entries still resolve to a name.",
                     "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "triangle_15m": {"name": "Triangle (15M, legacy)",
                     "tooltip": "A triangle pattern on the 15-minute chart (legacy tag).",
                     "definition": "An older, longer-named triangle tag kept only so past journal entries still resolve to a name.",
                     "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},
    "triangle": {"name": "Triangle (legacy)",
                 "tooltip": "A triangle pattern without a specific timeframe recorded (legacy tag).",
                 "definition": "The general legacy triangle tag, logged without a timeframe attached.",
                 "how": "Legacy journal-compatibility alias; not emitted by the current scorer."},

    # ── Chart pattern confidence buckets (shadow vocabulary, no weight match) ──
    "pattern_breakout": {"name": "Pattern Breakout",
                         "tooltip": "A chart pattern has broken out of its shape.",
                         "definition": "The pattern's boundary (a trendline, a flat top/bottom) has just been broken, meaning the pattern is resolving rather than still forming.",
                         "how": "Assigned when a detected pattern's reason text includes \"BREAKOUT\" without an accompanying volume confirmation."},
    "pattern_breakout_vol": {"name": "Pattern Breakout (Volume Confirmed)",
                             "tooltip": "A chart pattern has broken out with above-average volume behind it.",
                             "definition": "The same breakout concept as pattern_breakout, but the move was accompanied by a volume surge — traders consider a volume-backed breakout more reliable than one on quiet volume.",
                             "how": "Assigned when a detected pattern's reason text includes \"BREAKOUT+vol\"."},
    "pattern_high_conf": {"name": "Pattern (High Confidence)",
                          "tooltip": "A chart pattern detected with high geometric confidence.",
                          "definition": "Dexter's pattern detector scores how cleanly a shape matches its textbook geometry; this bucket covers patterns scoring 70% confidence or higher.",
                          "how": "Assigned when the pattern detector's confidence score (\"conf=\") is 0.70 or above."},
    "pattern_mid_conf": {"name": "Pattern (Medium Confidence)",
                         "tooltip": "A chart pattern detected with moderate geometric confidence.",
                         "definition": "The same pattern-confidence scoring as pattern_high_conf, but for shapes matching less cleanly — still a real detection, just a looser fit to the textbook geometry.",
                         "how": "Assigned when the pattern detector's confidence score (\"conf=\") is below 0.70."},

    # ── Volume Profile / VWAP (weights table) ───────────────────────────────
    "vp": {"name": "Volume Profile",
           "tooltip": "Price interacting with a Volume Profile level (POC, Value Area High, or Value Area Low).",
           "definition": "Volume Profile maps how much trading volume occurred at each price level over a session, rather than over time. It highlights the prices where the most business was actually done, which tend to act as support/resistance later.",
           "how": "Built from the traded volume at each price bucket over the profiling window; price near the resulting POC/VAH/VAL levels scores this tag."},
    "volprofile": {"name": "Volume Profile",
                   "tooltip": "Same as Volume Profile — an alternate name used in some records.",
                   "definition": "Identical concept to vp; kept as a separate dict key purely for journal-compatibility.",
                   "how": "Same Volume Profile logic as vp."},
    "vw": {"name": "VWAP",
           "tooltip": "Price testing the Volume-Weighted Average Price.",
           "definition": "VWAP is the average price of the session weighted by how much volume traded at each price — institutional traders commonly use it as a fair-value reference, so price reacting to it is considered meaningful.",
           "how": "Computed as cumulative (price × volume) divided by cumulative volume over the session; price near this level scores this tag."},
    "vwap": {"name": "VWAP",
             "tooltip": "Same as VWAP — an alternate name used in some records.",
             "definition": "Identical concept to vw; kept as a separate dict key purely for journal-compatibility.",
             "how": "Same VWAP logic as vw."},

    # ── Volume Profile detail (shadow vocabulary, no weight match) ──────────
    "vp_poc": {"name": "Volume Profile POC",
               "tooltip": "Price is near the Volume Profile's Point of Control — the single most-traded price.",
               "definition": "The Point of Control is the exact price level with the highest traded volume in the profiling window — often the single strongest magnet/support-resistance price on the chart.",
               "how": "Identified as the price bucket with the largest traded volume in the Volume Profile calculation; price within a small tolerance of it scores this tag."},
    "vp_vah": {"name": "Volume Profile Value Area High",
               "tooltip": "Price is near the top of the Volume Profile's Value Area.",
               "definition": "The Value Area is the price band containing roughly 70% of a session's traded volume; the Value Area High is its upper edge — often acting as resistance from below or support once broken above.",
               "how": "Identified as the upper boundary of the price range containing ~70% of traded volume; price within a small tolerance of it scores this tag."},
    "vp_val": {"name": "Volume Profile Value Area Low",
               "tooltip": "Price is near the bottom of the Volume Profile's Value Area.",
               "definition": "The lower edge of the same 70%-of-volume Value Area described under vp_vah — often acting as support from above or resistance once broken below.",
               "how": "Identified as the lower boundary of the price range containing ~70% of traded volume; price within a small tolerance of it scores this tag."},

    # ── Divergence (non-RSI) / Candle pattern (weights table) ──────────────
    "dv": {"name": "Divergence (Non-RSI)",
           "tooltip": "A price-momentum divergence detected outside the RSI indicator.",
           "definition": "The same divergence concept as RSI divergence — price making a new extreme without a matching indicator extreme — but flagged from a different underlying signal than RSI (e.g. an open-interest or volume-based comparison, depending on what triggered it).",
           "how": "Compares price swing extremes against a non-RSI indicator's own swing extremes; a mismatch scores this tag."},
    "cp": {"name": "Candle Pattern",
           "tooltip": "A single- or multi-candle price pattern (e.g. engulfing, pin bar) confirming the setup.",
           "definition": "A short-term candlestick formation used as a confirmation signal rather than a standalone reason to trade — Dexter weights it lowest of all tags for this reason.",
           "how": "Detected from the shape/relationship of the most recent one to a few candles."},
    "candlepattern": {"name": "Candle Pattern",
                      "tooltip": "Same as Candle Pattern — an alternate name used in some records.",
                      "definition": "Identical concept to cp; kept as a separate dict key purely for journal-compatibility.",
                      "how": "Same candle-pattern logic as cp."},

    # ── Market structure (weights table) ────────────────────────────────────
    "ms_1d": {"name": "Market Structure (Daily)",
              "tooltip": "A higher-timeframe market structure signal on the daily chart.",
              "definition": "Market structure tracks the sequence of swing highs/lows to determine whether the broader trend is intact, weakening, or breaking. The daily version is the slowest, biggest-picture read and carries the most weight of any tag in the whole system.",
              "how": "Computed from the sequence of daily swing highs and lows (higher-highs/higher-lows for an uptrend, the reverse for a downtrend, or a break in that sequence)."},
    "ms_4h": {"name": "Market Structure (4H)",
              "tooltip": "A market structure signal on the 4-hour chart.",
              "definition": "Same market structure concept as the daily version, read from the 4-hour chart.",
              "how": "Computed from the sequence of 4H swing highs and lows."},
    "ms_1h": {"name": "Market Structure (1H)",
              "tooltip": "A market structure signal on the 1-hour chart.",
              "definition": "Same market structure concept, read from the 1-hour chart.",
              "how": "Computed from the sequence of 1H swing highs and lows."},
    "ms_30m": {"name": "Market Structure (30M)",
               "tooltip": "A market structure signal on the 30-minute chart.",
               "definition": "Same market structure concept, read from the 30-minute chart.",
               "how": "Computed from the sequence of 30M swing highs and lows."},
    "ms_15m": {"name": "Market Structure (15M)",
               "tooltip": "A market structure signal on the 15-minute chart.",
               "definition": "Same market structure concept, read from the 15-minute chart — the fastest structure read Dexter scores.",
               "how": "Computed from the sequence of 15M swing highs and lows."},
    "ms": {"name": "Market Structure",
           "tooltip": "A market structure signal without a specific timeframe recorded.",
           "definition": "The general market structure concept, logged without a timeframe tag attached.",
           "how": "Same swing-sequence logic as the timeframe-specific ms tags, timeframe unspecified."},

    # ── Derivatives (shadow vocabulary — Binance futures, no weight match) ──
    "funding_extreme": {"name": "Funding Rate Extreme",
                        "tooltip": "The perpetual futures funding rate has reached an extreme level.",
                        "definition": "Funding rate is the periodic payment between long and short traders on a perpetual futures contract; an extreme reading means one side is heavily overcrowded and paying a steep premium to stay in the trade — often a contrarian signal.",
                        "how": "Read from Binance futures funding-rate data; flagged when it moves beyond a normal range."},
    "oi_divergence": {"name": "Open Interest Divergence",
                      "tooltip": "Open interest is moving opposite to what the price move would suggest.",
                      "definition": "Open interest is the total number of outstanding futures contracts. When price rises but open interest falls (or vice versa), it suggests the move is being driven by short-covering/long-liquidation rather than fresh conviction — a weaker kind of move.",
                      "how": "Compares the direction of price change against the direction of open-interest change over the same window."},
    "oi_confirm": {"name": "Open Interest Confirmation",
                   "tooltip": "Open interest is moving in the same direction as price, confirming the move.",
                   "definition": "The opposite situation to oi_divergence — price and open interest rising (or falling) together suggests fresh positioning is backing the move, a stronger signal than a move on falling open interest.",
                   "how": "Compares the direction of price change against the direction of open-interest change over the same window."},

    # ── Fast intraday structural anchors (shadow vocabulary, no weight match) ──
    "fast_anchor_pdh": {"name": "Prior Day High",
                        "tooltip": "Price is near the prior trading day's high.",
                        "definition": "The previous day's high is a level intraday traders watch closely as a fast-forming reference point, useful when a slower structural level (like a multi-touch SR) hasn't had time to form yet.",
                        "how": "The high price of the previous calendar day; price within a small tolerance of it scores this tag."},
    "fast_anchor_pdl": {"name": "Prior Day Low",
                        "tooltip": "Price is near the prior trading day's low.",
                        "definition": "The mirror image of the Prior Day High — the previous day's low as a fast intraday reference point.",
                        "how": "The low price of the previous calendar day; price within a small tolerance of it scores this tag."},
    "fast_anchor_or_high": {"name": "Opening Range High",
                            "tooltip": "Price is near the high of today's opening range.",
                            "definition": "The opening range is the high/low price band set in the first part of today's session — a widely-watched intraday reference before the rest of the day's structure has formed.",
                            "how": "The highest price in the defined opening window of the current session; price within a small tolerance of it scores this tag."},
    "fast_anchor_or_low": {"name": "Opening Range Low",
                           "tooltip": "Price is near the low of today's opening range.",
                           "definition": "The mirror image of the Opening Range High — the low of the same opening window.",
                           "how": "The lowest price in the defined opening window of the current session; price within a small tolerance of it scores this tag."},

    # ── Liquidity sweep (shadow vocabulary, no weight match) ────────────────
    "sweep": {"name": "Liquidity Sweep",
              "tooltip": "A stop-hunt: price wicked through a cluster of equal highs/lows then closed back inside.",
              "definition": "A liquidity sweep happens when price briefly pierces a level where many traders' stop-losses are likely resting (equal highs or lows), triggers them, then reverses back inside the range — often read as a trap that clears out weak positioning before the real move.",
              "how": "Detected from a candle whose wick crosses a cluster of equal highs/lows by a small tolerance while its close stays back inside, within the most recent ~40 candles."},

    # ── Meta / catch-all (shadow vocabulary, no weight match) ───────────────
    "watch_signal": {"name": "Watch Signal",
                     "tooltip": "A developing signal Dexter is watching but hasn't confirmed yet.",
                     "definition": "A catch-all for any \"[WATCH]\"-prefixed reason that isn't the specific forming-RSI-divergence case — something Dexter is tracking as a possible setup ingredient before it's confirmed.",
                     "how": "Assigned to any reason text starting with \"[WATCH\" that doesn't match the forming-divergence pattern specifically."},
    "other": {"name": "Other / Unclassified",
              "tooltip": "A reason Dexter produced that doesn't match any named tag yet.",
              "definition": "A safety-net bucket for reason text the classifier doesn't recognize — keeps the Examiner's stats from silently dropping data when a new kind of reason string appears, at the cost of not being a specific, named concept.",
              "how": "Assigned whenever a reason string doesn't match any pattern in the classifier's lookup table; each new unmatched string is logged once so the table can be extended."},

    # ── Journal shorthand, unclear origin (Chev's own typing, no formal weight) ──
    "tp_balance_extreme": {"name": "TP at Balance Extreme (unconfirmed)",
                           "tooltip": "Best-guess reading: a take-profit set at the edge of a price balance/range area — not formally defined anywhere in code.",
                           "definition": "This exact code isn't produced by Dexter's scorer or defined anywhere in the codebase — it only appears in Chev's own free-typed tags field, and its precise meaning isn't confirmed. The most likely reading, given the name, is a take-profit placed at the extreme edge of a price range the market has been balancing inside. Treat this description as unconfirmed.",
                           "how": "Not computed by Dexter — this is a value Chev typed by hand."},
}

CONFLUENCE_THRESHOLD_CRYPTO = 10   # minimum score to open a trade on crypto
CONFLUENCE_THRESHOLD_FOREX  = 8    # raised from 7 — SR reweighting means 7 was too lenient
CONFLUENCE_THRESHOLD_STOCK  = 8    # stocks previously fell into the crypto branch (10) by accident
ESCALATION_MAX_DIST_PCT     = 0.75 # don't escalate setups further than this % from the confluence level
CONFLICT_DOMINANCE_RATIO    = 2.0  # dominant directional score must be ≥ this × the opposing side

# ── EXPLORATION MODE ─────────────────────────────────────────────────────────
# Temporary paper-trading data-collection phase: lower the entry bar so small
# graded trades flow and the learning loops (playbooks, the Examiner) get data
# to learn from. Flip EXPLORATION_MODE to False to restore normal thresholds
# everywhere at once. As of 2026-07-05 this single flag reaches THREE layers:
#   1. Escalation eligibility — score threshold + max distance via _active_thresholds().
#   2. Chev's own subjective judgment gates — loosened in the escalation message itself
#      (maturity/participation/direction-score become sizing inputs, not vetoes).
#   3. Setup-QUALITY math — GEOMETRY REVIEW pre-check AND risk_gauntlet.run_gauntlet()
#      both read the same NORMAL/EXPLORATION profile via risk_gauntlet.get_active_profile()
#      (ATR floor, cost gate, R:R, heat cap, correlation cap, concurrency cap).
# Still NEVER affected regardless of mode (account-survival math, not setup quality):
# STRUCT PRE-GATE, CONFLICT gate, equity floor, liquidation, leverage caps.
EXPLORATION_MODE            = True
EXPLORATION_THRESHOLD_CRYPTO = 7
EXPLORATION_THRESHOLD_FOREX  = 6
EXPLORATION_THRESHOLD_STOCK  = 6
EXPLORATION_MAX_DIST_PCT     = 1.5

# Floor below which a setup is scanned + shadow-labeled but never escalated to Chev.
# Crypto 5m: round-trip cost (fee+slippage, scalp trade_type) eats too large a share of
# the R:R achievable at a 5m-realistic ATR stop -- not fixable by loosening, the timeframe
# itself doesn't leave enough room. Forex/stock scalps don't scan 5m at all (SCAN_TFS_FOREX
# starts at 15m; stock has no 5m scan either), so their floor is a no-op today, kept explicit
# for when/if a faster TF is ever added.
ESCALATION_TF_FLOOR          = {"crypto": "15m", "forex": "5m", "stock": "5m"}

# Stamped onto every newly-closed Chev-trade journal entry (purely additive — nothing reads
# this yet). Lets EV enforcement, when it returns, be fed only trades earned under the
# current system instead of the pre-2026-07-06 era. Bump this string any time a change is
# significant enough that pre-change trades shouldn't count toward post-change EV stats.
SYSTEM_ERA = "explor_v2_2026-07-06"

VALID_TAGS = list(CONFLUENCE_SCORES.keys())

# Loop timing
CHECK_INTERVAL_SECONDS    = 2 * 60   # main loop sleep between cycles — crypto+forex scan every cycle
FOREX_SCAN_INTERVAL       = 0        # 0 = scan every cycle (yfinance is unlimited)
FOREX_TRADE_CHECK_INTERVAL = 60      # how often to check fills/SL/TP on open+pending forex trades
STOCK_SCAN_INTERVAL       = 10 * 60  # Twelve Data budget: 4 pairs × 2 tf × 144 cycles = ~1150 credits/day
STOCK_TRADE_CHECK_INTERVAL = 60      # how often to check fills/SL/TP on open+pending stocks (market hours only)

WATCHLIST = [
    # ── Crypto (Binance USDT) ─────────────────────────────────────────
    {"symbol": "BTCUSDT",  "type": "crypto"},
    {"symbol": "ETHUSDT",  "type": "crypto"},
    {"symbol": "XRPUSDT",  "type": "crypto"},
    {"symbol": "XLMUSDT",  "type": "crypto"},
    {"symbol": "ADAUSDT",  "type": "crypto"},
    {"symbol": "SOLUSDT",  "type": "crypto"},
    {"symbol": "BNBUSDT",  "type": "crypto"},
    {"symbol": "TRXUSDT",  "type": "crypto"},
    {"symbol": "DOGEUSDT", "type": "crypto"},
    {"symbol": "LINKUSDT", "type": "crypto"},
    {"symbol": "SUIUSDT",  "type": "crypto"},
    {"symbol": "AVAXUSDT", "type": "crypto"},
    {"symbol": "NEARUSDT", "type": "crypto"},
    {"symbol": "DOTUSDT",  "type": "crypto"},
    {"symbol": "AAVEUSDT", "type": "crypto"},
    {"symbol": "PEPEUSDT", "type": "crypto"},
    {"symbol": "ZECUSDT",  "type": "crypto"},
    {"symbol": "UNIUSDT",  "type": "crypto"},
    # ── Forex (Twelve Data) ───────────────────────────────────────────
    {"symbol": "EUR/USD", "type": "forex"},
    {"symbol": "GBP/USD", "type": "forex"},
    {"symbol": "USD/JPY", "type": "forex"},
    {"symbol": "AUD/USD", "type": "forex"},
    {"symbol": "USD/CAD", "type": "forex"},
    {"symbol": "USD/CHF", "type": "forex"},
    {"symbol": "NZD/USD", "type": "forex"},
    {"symbol": "USD/CNH", "type": "forex"},
    # ── Stocks (Twelve Data / Finnhub) ────────────────────────────────
    {"symbol": "NVDA",  "type": "stock"},
    {"symbol": "TSLA",  "type": "stock"},
    {"symbol": "AMZN",  "type": "stock"},
    {"symbol": "AMD",   "type": "stock"},
    {"symbol": "META",  "type": "stock"},
    {"symbol": "MSFT",  "type": "stock"},
    {"symbol": "AAPL",  "type": "stock"},
    {"symbol": "GOOGL", "type": "stock"},
    {"symbol": "NFLX",  "type": "stock"},
    {"symbol": "BABA",  "type": "stock"},
    {"symbol": "MRVL",  "type": "stock"},
    {"symbol": "NOW",   "type": "stock"},
    {"symbol": "HOOD",  "type": "stock"},
    {"symbol": "MARA",  "type": "stock"},
    {"symbol": "MRNA",  "type": "stock"},
    {"symbol": "BAC",   "type": "stock"},
    {"symbol": "GME",   "type": "stock"},
    {"symbol": "AMC",   "type": "stock"},
    {"symbol": "SQQQ",  "type": "stock"},
    {"symbol": "QQQ",   "type": "stock"},
    {"symbol": "ASTS",  "type": "stock"},
    {"symbol": "FCEL",  "type": "stock"},
    {"symbol": "POET",  "type": "stock"},
    {"symbol": "TE",    "type": "stock"},
    {"symbol": "NVTS",  "type": "stock"},
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
TF_MIN_CONFLUENCE_DISCOUNT = {"5m": 5, "15m": 5, "30m": 4, "1h": 4, "4h": 3}
# Points BELOW the active per-asset trade threshold (_active_thresholds) required just to
# be worth fully evaluating -- a cheap first-pass filter, not the real gate. Previously this
# was a flat absolute table (5/5/6/6/7) that never read EXPLORATION_MODE or asset_type at
# all, which violated the rule (see _active_thresholds' docstring) that every score/distance
# check must route through that helper. Calibrated so normal-mode crypto (threshold=10)
# reproduces the original absolute numbers exactly; forex/stock and EXPLORATION_MODE now
# scale off their own real threshold instead of inheriting crypto's old flat numbers.
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
_last_scan_completed_ts = 0.0  # PHASE 12: set at the end of every full main-loop iteration
                                # (right before the sleep) -- a hung/crashed loop stops
                                # advancing this even though Flask keeps serving, which a
                                # plain HTTP ping alone would never catch.

COINGECKO_IDS = {
    "BTCUSDT":  "bitcoin",
    "ETHUSDT":  "ethereum",
    "XRPUSDT":  "ripple",
    "XLMUSDT":  "stellar",
    "ADAUSDT":  "cardano",
    "SOLUSDT":  "solana",
    "BNBUSDT":  "binancecoin",
    "TRXUSDT":  "tron",
    "DOGEUSDT": "dogecoin",
    "LINKUSDT": "chainlink",
    "SUIUSDT":  "sui",
    "AVAXUSDT": "avalanche-2",
    "NEARUSDT": "near",
    "DOTUSDT":  "polkadot",
    "AAVEUSDT": "aave",
    "PEPEUSDT": "pepe",
    "ZECUSDT":  "zcash",
    "UNIUSDT":  "uniswap",
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
        # 2026-07-12 (two-tier memory audit, Phase 2): stays generic/legacy on
        # purpose -- this is a single account-wide dashboard display field with
        # no symbol or trade in scope (a periodic full-state snapshot, not a
        # decision input), so there's no asset class to derive here. Showing
        # the legacy generic playbook still has real display value for a human
        # glancing at the monitor; dropping it loses that for no benefit.
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
                "updated":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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


def _load_playbook(asset_type=None):
    path = PLAYBOOK_PATHS.get(_norm_asset_type(asset_type), PLAYBOOK_PATH) if asset_type else PLAYBOOK_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def _load_journal():
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _is_duplicate_journal_entry(symbol, entry_price, sl, tp, journal, lookback=30, tol=0.0005):
    """Return True if the last `lookback` journal entries already contain this exact trade.

    Matches on symbol + entry + SL + TP within `tol` fractional tolerance (0.05%).
    This catches the restart-logging bug where the same closed trade gets written twice,
    without false-positiving on legitimately similar setups at different prices.
    """
    for rec in journal[-lookback:]:
        if rec.get("symbol") != symbol:
            continue
        def _close(a, b):
            if b == 0:
                return a == 0
            return abs(a - b) / abs(b) <= tol
        if (_close(rec.get("entry", -1), entry_price) and
                _close(rec.get("sl", -1),    sl) and
                _close(rec.get("tp", -1),    tp)):
            return True
    return False


_chev_lock             = threading.Lock()
_chev_last_call        = 0.0
_CHEV_MIN_GAP          = 65   # seconds from call completion — keeps each call in its own 60-second TPM window
_chev_rate_limit_until = 0.0  # absolute timestamp; block all calls until then after a 429
_CHEV_RL_COOLDOWN      = 90   # extra seconds to back off after a 429
_last_escalated: dict  = {}   # symbol → unblock_timestamp; block escalation until this time
_ESCALATION_COOLDOWN   = 600   # 10 min cooldown after SKIP or malformed reply
_POST_COOLDOWN         = 14400 # 4 hour cooldown after a POST (trade is live — don't re-hammer same pair)
_force_closed_rows: set = set()  # row numbers force-closed by user; price thread skips these
_CHEV_DECISIONS_LOG = os.path.join(CHEV_TOOLS_ROOT, "chev_decisions.jsonl")  # one JSON-line per Chev decision
_recent_losses: list = []  # rolling store of loss fingerprints for confluence re-entry cooldown


def _trade_metrics_for_log(parsed):
    """PHASE 11: pure. Computes planned_rr/stop_pct/tp_pct/trade_type_chosen directly from
    Chev's own parsed TRADE: line -- independent of whether risk_gauntlet.run_gauntlet()
    ever ran, so these are available at every POST/REJECT-family decision (GATE_REJECT/
    STRUCT_REJECT/MTF_TAX_REJECT/GEOMETRY_REJECT/REJECT/POST all happen at or after Chev
    proposes concrete entry/sl/tp numbers, several of them before the gauntlet is ever
    called). Returns a dict of Nones if no usable trade/entry/sl/tp is present -- never
    raises, never guesses a number that isn't there."""
    _td = (parsed or {}).get("trade") or {}
    try:
        _entry = float(_td.get("entry") or 0)
        _sl    = float(_td.get("sl")    or 0)
        _tp    = float(_td.get("tp")    or 0)
    except (TypeError, ValueError):
        _entry = _sl = _tp = 0
    if not (_entry and _sl and _tp):
        return {"planned_rr": None, "stop_pct": None, "tp_pct": None, "trade_type_chosen": None}
    _sl_dist = abs(_entry - _sl)
    # stop_pct/tp_pct are stored as actual percentage numbers (1.622 means 1.622%, not the
    # fraction 0.01622) so "rounded to 4 decimals" keeps real precision on tight stops --
    # rounding the fraction itself to 4 places would collapse to 2 decimal digits of %.
    return {
        "planned_rr":        round(abs(_tp - _entry) / _sl_dist, 4) if _sl_dist > 0 else None,
        "stop_pct":          round(_sl_dist / _entry * 100, 4),
        "tp_pct":            round(abs(_tp - _entry) / _entry * 100, 4),
        "trade_type_chosen": _td.get("trade_type") or None,
    }


def _log_chev_decision(symbol, primary_tf, dexter_score, dexter_reasons, decision, reason, regime_4h=None, detail="",
                        planned_rr=None, stop_pct=None, tp_pct=None, cost_r=None, trade_type_chosen=None,
                        ev_advisory_rr=None, enforced_rr_floor=None, deliberation_secs=None):
    """Append one JSON-line to the Chev decision log for post-session review.
    PHASE 11: the fields after `detail` are additive/optional (default None -- absent data
    stays null, nothing is guessed). planned_rr/stop_pct/tp_pct/cost_r/trade_type_chosen are
    only ever passed by POST/REJECT-family call sites (never SKIP/FORMAT_ERROR, which have
    no trade numbers to report). ev_advisory_rr/enforced_rr_floor are only ever passed by
    call sites downstream of risk_gauntlet.run_gauntlet(). deliberation_secs is passed at
    every escalation call site (including SKIP) -- it times the Chev round-trip itself, not
    the trade proposal, so it applies regardless of what Chev ultimately said."""
    import json as _json
    entry = {
        "ts":             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":         symbol,
        "tf":             primary_tf,
        "dexter_score":   round(float(dexter_score), 1),
        "dexter_reasons": dexter_reasons if isinstance(dexter_reasons, list) else [dexter_reasons],
        "decision":       decision,      # SKIP | POST | GATE_REJECT | FORMAT_ERROR
        "reason":         reason,
        "detail":         detail,        # Chev's full REASONING (+ WHAT WAS MISSING for skips)
        "specific":       any(ch.isdigit() for ch in (detail or reason or "")),
        "regime_4h":      regime_4h,
        "planned_rr":        planned_rr,
        "stop_pct":          stop_pct,
        "tp_pct":            tp_pct,
        "cost_r":            cost_r,
        "trade_type_chosen": trade_type_chosen,
        "ev_advisory_rr":    ev_advisory_rr,
        "enforced_rr_floor": enforced_rr_floor,
        "deliberation_secs": deliberation_secs,
    }
    try:
        with open(_CHEV_DECISIONS_LOG, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(entry) + "\n")
    except Exception as _e:
        print(f"[Dexter] Decision log write failed: {_e}")

    # Mirror every non-POST decision into the Skip Log sheet tab, human-readable —
    # POST already gets its own row in Trade Log, so it's excluded here to avoid
    # duplication. Never allowed to block anything: any failure just logs one line.
    if decision != "POST" and skip_log_ws is not None:
        try:
            _reasons_str = "; ".join(dexter_reasons) if isinstance(dexter_reasons, list) else str(dexter_reasons)
            skip_log_ws.append_row([
                entry["ts"], symbol, primary_tf, decision,
                entry["dexter_score"], regime_4h or "",
                str(reason)[:500], _reasons_str[:500],
            ])
        except Exception as _e:
            print(f"[Dexter] Skip Log sheet write failed: {_e}")


def _call_chev(messages, timeout=120, model_id=MODEL_ID):
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
                json={"model": model_id, "messages": messages},
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
    # 2026-07-12 (two-tier memory audit, Phase 2): symbol IS in scope here (it's
    # a real parameter), unlike _push_to_firebase -- derive the asset class from
    # it (same heuristic used elsewhere for Jane's trades, e.g. dexter.py:10180)
    # so Chev judges her trade against the correct asset-specific playbook
    # (including its DURABLE LESSONS once Kev writes one) instead of the stale
    # generic fallback. _load_playbook normalizes "stock"->"stocks" internally.
    _jane_trade_asset_type = "crypto" if symbol.endswith("USDT") else ("forex" if "/" in symbol else "stock")
    playbook = _load_playbook(_jane_trade_asset_type)
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


# PRESERVED SECTIONS -- substring-AND anchor pairs identifying human-authored
# sections that survive every learning-session rewrite verbatim, mechanically
# rather than by asking the model nicely. A future third preserved section is a
# one-line addition here, nothing else needs to change. This tuple's order is
# also the deterministic re-splice order used when a file contains more than
# one: DURABLE LESSONS is spliced back in first, then TRENDING MARKET
# BEHAVIOUR -- CRITICAL.
_PRESERVED_SECTION_ANCHORS = (
    ("DURABLE LESSONS", "HUMAN-CURATED"),
    ("TRENDING MARKET BEHAVIOUR", "CRITICAL"),
)

# Known section names the playbook is actually built from: the headings
# _run_learning_session's own prompt asks for (dexter.py ~3873-3891) plus the
# two supplemental sections it appends afterward (dexter.py ~3995/3997), plus
# the human-curated durable section name. The model is inconsistent about which
# markup it wraps a heading in ('### NAME', '**NAME**', or a bare ALL-CAPS line
# with no markup at all) -- a generic heading-style regex missed the bare-line
# case, which let the boundary scan fall through to end-of-text and splice the
# ENTIRE old file tail back into the freshly generated playbook (the
# duplicate-sections bug found in the live chev_playbook_forex.txt). Matching
# against this known, finite name list instead of any markup pattern is what
# actually fixes that failure mode.
_PLAYBOOK_SECTION_NAMES = {
    "CONFLUENCE CONDITIONS", "MARKET STRUCTURE RULES", "ENTRY QUALITY STANDARDS",
    "TRADE MANAGEMENT PATTERNS", "REASONING QUALITY", "ASSET NOTES",
    "JANE'S PATTERNS", "STRUCTURAL RISK FACTORS", "WIN/LOSS INSIGHT",
    "DURABLE LESSONS (HUMAN-CURATED)",
}

# Reference-block cap for the recurrence mechanism (Phase 1c of the two-tier
# memory audit, 2026-07-12): keeps the worst-case combined learning prompt
# (this reference block + trade entries + stats + the ~5.0-5.7k-token system
# prompt) clearing 12,288 ctx under the pessimistic chars/3.5 estimate by a real
# ~148-token margin -- see handoff.txt for the full arithmetic, this number was
# derived from it, not guessed. Set to 1200 rather than a rounder 1000 because
# _build_reference_block_text's round-robin needs every one of the 9 fast-tier
# sections to fit at least its headline bullet in the first pass (9 sections x
# ~120 chars/heading+bullet ~= 1080 minimum) -- 1000 silently dropped whichever
# section came last. If this margin ever needs to be reclaimed, the two cheaper
# levers are: drop `recent` from 15 to 14 entries, or shave _fmt_entry's
# analysis cap from 400 to 350 chars -- both cost less signal than shrinking
# this reference block further, which is the piece doing the long-term-
# learning work.
_REFERENCE_BLOCK_CHAR_CAP = 1200


def _is_section_heading(line):
    """True if `line` is a recognized playbook section heading -- either one of
    the fixed fast-tier/supplement names above, or either preserved section's
    own heading. Checking preserved-section anchors here too (not just the
    fixed name set) is what lets a boundary scan correctly stop at whichever
    preserved section comes next, regardless of which order they appear in a
    real file -- without it, DURABLE LESSONS immediately followed by TRENDING
    MARKET BEHAVIOUR (or vice versa) would have one section's scan swallow the
    other's content whole."""
    for term1, term2 in _PRESERVED_SECTION_ANCHORS:
        if term1 in line and term2 in line:
            return True
    stripped = line.strip().lstrip("#").strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
        stripped = stripped[2:-2].strip()
    stripped = stripped.rstrip(":").strip()
    return stripped.upper() in _PLAYBOOK_SECTION_NAMES


def _extract_section(lines, anchor):
    """Find one preserved section's (start, end) line-index range in `lines`:
    start is the first line matching anchor's substring-AND pair, end is the
    next recognized heading after it (or end-of-file if none). Returns
    (None, None) if the anchor's start text isn't present in `lines` at all."""
    term1, term2 = anchor
    start = None
    for i, line in enumerate(lines):
        if term1 in line and term2 in line:
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _is_section_heading(lines[j]):
            end = j
            break
    return start, end


def _strip_preserved_sections(text):
    """Remove every preserved (human-authored) section from `text` before it is
    embedded as reference material in the learning prompt. The model must never
    see its own or Kev's prior teaching text verbatim and risk echoing it back
    marked '(recurring)' as if today's trade data had independently re-derived
    it -- that would launder human-written text back as a false promotion
    candidate. Applies to BOTH preserved sections, not just DURABLE LESSONS:
    TRENDING MARKET BEHAVIOUR -- CRITICAL is equally human-authored teaching,
    not something today's trades actually re-derived."""
    lines = text.splitlines()
    for anchor in _PRESERVED_SECTION_ANCHORS:
        start, end = _extract_section(lines, anchor)
        if start is None:
            continue
        del lines[start:end]
    return "\n".join(lines)


def _build_reference_block_text(stripped_playbook_text, char_cap):
    """Breadth-first, round-robin truncation of a (preserved-sections-already-
    stripped) previous playbook down to char_cap: take each fast-tier section's
    1st bullet, then each section's 2nd, etc., in file order, accumulating only
    WHOLE bullets (a bullet that would exceed the remaining budget is skipped,
    never split mid-line).

    Why round-robin instead of a plain head-slice: playbook sections front-load
    their strongest claim -- the first bullet under CONFLUENCE CONDITIONS is the
    headline finding, the third is garnish. A head-slice gives perfect recall of
    whichever section happens to be written first and zero recall of the last
    few, which would bias every '(recurring)' marker -- and therefore every
    promotion candidate -- toward one section. Round-robin keeps the recurrence
    check breadth-fair: every section's headline claim stays visible, at the
    cost of deeper bullets (#3/#4) never being checked for recurrence -- a
    lesson that only ever lives at bullet #4 wasn't a promotion candidate
    anyway.

    Output is grouped back by section (not interleaved in selection order) so
    it still reads like a normal playbook excerpt.
    """
    lines = stripped_playbook_text.splitlines()
    sections = []  # [(heading_line, [bullet_text, ...]), ...] in file order
    current_heading = None
    current_bullets = []
    current_bullet_lines = []

    def _flush_bullet():
        if current_bullet_lines:
            current_bullets.append("\n".join(current_bullet_lines).rstrip())
            current_bullet_lines.clear()

    def _flush_section():
        _flush_bullet()
        if current_heading is not None:
            sections.append((current_heading, list(current_bullets)))
        current_bullets.clear()

    for line in lines:
        if _is_section_heading(line):
            _flush_section()
            current_heading = line.strip()
            continue
        if current_heading is None:
            continue  # stray content before the first heading -- ignore
        stripped = line.strip()
        if stripped.startswith("-"):
            _flush_bullet()
            current_bullet_lines.append(line)
        elif stripped:
            current_bullet_lines.append(line)  # continuation of a wrapped bullet
    _flush_section()

    selected = {i: [] for i in range(len(sections))}
    total_chars = 0
    round_idx = 0
    progressed = True
    while progressed:
        progressed = False
        for i, (heading, bullets) in enumerate(sections):
            if round_idx >= len(bullets):
                continue
            bullet = bullets[round_idx]
            addition = bullet if selected[i] else (heading + "\n" + bullet)
            addition_len = len(addition) + 1  # +1 for the joining newline
            if total_chars + addition_len > char_cap:
                continue  # this bullet alone doesn't fit -- skip it, keep it whole
            selected[i].append(bullet)
            total_chars += addition_len
            progressed = True
        round_idx += 1

    out_parts = [
        heading + "\n" + "\n".join(selected[i])
        for i, (heading, _bullets) in enumerate(sections)
        if selected[i]
    ]
    return "\n\n".join(out_parts)


def _preserve_critical_section(existing_text, generated_text):
    """Protect every preserved section (see _PRESERVED_SECTION_ANCHORS) from the
    learning session's full playbook overwrite, mechanically rather than by
    asking the model nicely. For each preserved section found in the EXISTING
    playbook file, extracts it verbatim and re-inserts it into the freshly
    GENERATED text, replacing whatever the model produced there or inserting it
    at the same relative position if the model omitted it. Sections are
    processed -- and therefore re-spliced -- in _PRESERVED_SECTION_ANCHORS'
    order. A section absent from the existing file (e.g. no DURABLE LESSONS
    written yet, or a fresh file with neither) is simply left untouched.
    """
    g_lines = generated_text.splitlines()
    e_lines = existing_text.splitlines()

    for anchor in _PRESERVED_SECTION_ANCHORS:
        e_start, e_end = _extract_section(e_lines, anchor)
        if e_start is None:
            continue  # this preserved section doesn't exist in the existing file yet

        block = e_lines[e_start:e_end]
        while block and not block[-1].strip():
            block.pop()

        g_start, g_end = _extract_section(g_lines, anchor)
        if g_start is not None:
            g_lines = g_lines[:g_start] + block + [""] + g_lines[g_end:]
        else:
            # Model omitted the section entirely — insert at the same relative
            # position it held in the existing file.
            insert_at = min(e_start, len(g_lines))
            g_lines = g_lines[:insert_at] + block + [""] + g_lines[insert_at:]

    return "\n".join(g_lines)


def _run_learning_session(journal, jane_journal=None, asset_type=None):
    """Every 10 closed trades, Chev reads his journal for this asset class and rewrites the playbook.

    When asset_type is given, only trades of that type are used and the asset-specific
    playbook file is updated.  Generic (all-asset) mode still works when asset_type=None.
    """
    asset_journal = [e for e in journal if _norm_asset_type(e.get("asset_type")) == asset_type] if asset_type else journal
    if asset_type and len(asset_journal) < 5:
        return  # not enough data to write a meaningful asset-specific playbook yet
    # 15, not 20 -- shrunk to make room for the recurrence reference block below
    # while still clearing 12,288 ctx under the pessimistic chars/3.5 estimate.
    recent = asset_journal[-15:]
    _window_oldest = recent[0]["ts"][:10] if recent else "?"
    _window_newest = recent[-1]["ts"][:10] if recent else "?"

    def _jane_asset_type(symbol):
        # jane_journal.json entries carry no "asset_type" field at all (unlike Chev's
        # own journal, dexter.py:10358-10370) -- classify from symbol instead, using
        # the same heuristic already used for Jane's open trades (dexter.py:10180),
        # bucketed to "stocks" (plural) to match this function's own asset_type
        # vocabulary (the caller loop passes "forex"/"crypto"/"stocks").
        if symbol.endswith("USDT"):
            return "crypto"
        if "/" in symbol:
            return "forex"
        return "stocks"

    jane_journal = jane_journal or []
    jane_asset_journal = (
        [e for e in jane_journal if _jane_asset_type(e.get("symbol", "")) == asset_type]
        if asset_type else jane_journal
    )
    jane_recent = jane_asset_journal[-10:]

    def _fmt_entry(i, e):
        moves = e.get("chev_moves", [])
        moves_line = ("\nManagement: " + " | ".join(moves)) if moves else ""
        # Cap analysis so the playbook-rewrite prompt (sent to the 8192-ctx
        # chev-32b-learn brain) can never silently truncate the trade history.
        analysis = (e.get("analysis") or "none")
        if len(analysis) > 400:
            analysis = analysis[:400].rstrip() + "…"
        return (
            f"Trade {i+1}: {e['symbol']} {e['direction'].upper()} | {e['outcome']} ${e['pnl']:+.2f} | "
            f"held {e.get('duration','?')} | {e['ts'][:10]} [{e.get('close_type','?')}]\n"
            f"Confluences: {e.get('tags','none')}"
            f"{moves_line}\n"
            f"Analysis: {analysis}"
        )
    entries_text = "\n\n".join([_fmt_entry(i, e) for i, e in enumerate(recent)])

    # Build confluence combo win-rate + R-multiple breakdown for the last 30 trades (asset-filtered)
    from collections import defaultdict
    combo_stats = defaultdict(lambda: {"w": 0, "l": 0, "r_sum": 0.0, "r_count": 0})
    for e in asset_journal[-30:]:
        raw_tags = [t.strip().lower() for t in str(e.get("tags", "")).split(",") if t.strip()]
        valid = [t for t in raw_tags if t in CONFLUENCE_SCORES]
        combo = "+".join(sorted(valid)) if valid else "no-tags"
        if e.get("outcome") == "WIN":
            combo_stats[combo]["w"] += 1
        elif e.get("outcome") == "LOSS":
            combo_stats[combo]["l"] += 1
        _rm = e.get("r_multiple")
        if _rm is not None:
            combo_stats[combo]["r_sum"]   += float(_rm)
            combo_stats[combo]["r_count"] += 1
    combo_lines = []
    for combo, st in sorted(combo_stats.items(), key=lambda x: -(x[1]["w"] + x[1]["l"])):
        total = st["w"] + st["l"]
        wr    = round(st["w"] / total * 100) if total else 0
        avg_r = round(st["r_sum"] / st["r_count"], 2) if st["r_count"] > 0 else None
        r_str = f" | avg {avg_r:+.2f}R" if avg_r is not None else ""
        combo_lines.append(f"  {combo}: {st['w']}W/{st['l']}L ({wr}% WR{r_str})")
    combo_summary = "\n".join(combo_lines) if combo_lines else "  No data yet"

    # Management pattern stats across last 30 trades (asset-filtered)
    trades_30    = asset_journal[-30:]
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
        elif e.get("outcome") == "LOSS":
            grade_stats[g]["l"] += 1
        # SCRATCH is neither -- excluded from grade accuracy, same as everywhere else
        # in this function (Phase 6: don't train playbook lessons on fake losses).
    grade_lines = []
    for g in ("A+", "A", "B"):
        if g in grade_stats:
            st = grade_stats[g]
            total = st["w"] + st["l"]
            wr = round(st["w"] / total * 100) if total else 0
            grade_lines.append(f"  {g}: {st['w']}W/{st['l']}L ({wr}% WR)")
    grade_accuracy = "\n".join(grade_lines) if grade_lines else "  No grade data yet."

    # Trade Path Forensics aggregation — input to the learning session only,
    # not a new playbook section. Appended only once enough samples exist.
    _fx_entries = [e for e in asset_journal if e.get("forensics")]
    forensics_block = ""
    if len(_fx_entries) >= 10:
        import statistics as _fx_stats
        _fx_sl_losses = [e for e in _fx_entries if e.get("close_type") == "SL_HIT"]
        _fx_wick_pct = (round(sum(1 for e in _fx_sl_losses if e["forensics"].get("exit_type_detail") == "wick")
                              / len(_fx_sl_losses) * 100) if _fx_sl_losses else None)
        _fx_ran_pct  = (round(sum(1 for e in _fx_sl_losses if e["forensics"].get("stopped_then_ran") is True)
                              / len(_fx_sl_losses) * 100) if _fx_sl_losses else None)
        _fx_loss_mfe = [e["forensics"]["mfe_r"] for e in _fx_entries
                        if e.get("outcome") == "LOSS" and e["forensics"].get("mfe_r") is not None]
        _fx_win_mae  = [e["forensics"]["mae_r"] for e in _fx_entries
                        if e.get("outcome") == "WIN" and e["forensics"].get("mae_r") is not None]
        _fx_med_loss_mfe = round(_fx_stats.median(_fx_loss_mfe), 2) if _fx_loss_mfe else None
        _fx_med_win_mae  = round(_fx_stats.median(_fx_win_mae), 2) if _fx_win_mae else None
        _fx_bars_win  = [e["forensics"]["bars_held"] for e in _fx_entries
                         if e.get("outcome") == "WIN" and e["forensics"].get("bars_held") is not None]
        _fx_bars_loss = [e["forensics"]["bars_held"] for e in _fx_entries
                         if e.get("outcome") == "LOSS" and e["forensics"].get("bars_held") is not None]
        _fx_avg_bars_win  = round(sum(_fx_bars_win) / len(_fx_bars_win), 1) if _fx_bars_win else None
        _fx_avg_bars_loss = round(sum(_fx_bars_loss) / len(_fx_bars_loss), 1) if _fx_bars_loss else None
        forensics_block = (
            f"TRADE PATH FORENSICS (n={len(_fx_entries)}):\n"
            f"  SL losses classified 'wick' (stopped then recovered): {_fx_wick_pct if _fx_wick_pct is not None else 'n/a'}%\n"
            f"  SL losses where price later reached original TP anyway: {_fx_ran_pct if _fx_ran_pct is not None else 'n/a'}%\n"
            f"  Median MFE of losing trades: {_fx_med_loss_mfe if _fx_med_loss_mfe is not None else 'n/a'}R\n"
            f"  Median MAE of winning trades: {_fx_med_win_mae if _fx_med_win_mae is not None else 'n/a'}R\n"
            f"  Average bars held — wins: {_fx_avg_bars_win if _fx_avg_bars_win is not None else 'n/a'} | "
            f"losses: {_fx_avg_bars_loss if _fx_avg_bars_loss is not None else 'n/a'}\n\n"
        )

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

    # Recurrence reference block: the previous playbook, human-authored sections
    # stripped, capped, fenced, and marked reference-only -- see
    # _strip_preserved_sections and _REFERENCE_BLOCK_CHAR_CAP.
    _prev_playbook_text = _build_reference_block_text(
        _strip_preserved_sections(_load_playbook(asset_type)), _REFERENCE_BLOCK_CHAR_CAP
    )
    reference_block = ""
    if _prev_playbook_text.strip():
        reference_block = (
            f"PREVIOUS PLAYBOOK (REFERENCE ONLY — read this fence, then set it aside):\n"
            f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
            f"{_prev_playbook_text}\n"
            f"~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
            f"Reference only. Re-derive every claim from the trades below — a claim may appear "
            f"in your output ONLY if today's data independently supports it. Never copy reference "
            f"text forward. If a finding you derived today also appears in the reference, append "
            f"'(recurring)' to it. List every finding marked (recurring) in 2+ consecutive rewrites "
            f"under a final 'PROMOTION CANDIDATES' subsection addressed to Kev — you never promote "
            f"anything yourself, that section is a suggestion list, not an action.\n\n"
        )

    _asset_label = asset_type.upper() if asset_type else "ALL ASSETS"
    prompt = (
        f"You are reviewing your last {len(recent)} {_asset_label} trade post-mortems to update your {_asset_label} trading playbook.\n\n"
        f"{reference_block}"
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
        f"{forensics_block}"
        f"STATISTICAL HONESTY (hard rule for everything you write below):\n"
        f"  State every win-rate or performance claim with its sample size in parentheses, e.g. '3/3 (n=3)'.\n"
        f"  No absolute language ('always', 'never', '100% win rate', 'guaranteed') unless n ≥ 20.\n"
        f"  Patterns with fewer than 10 supporting trades: label them tentative.\n"
        f"  Any pattern with fewer than 5 supporting trades must be written as 'tentative (n=X)' — never stated as a rule.\n"
        f"  Never generalize a pattern beyond the regime stated in its window line — a lesson learned in a\n"
        f"    trending week does not automatically apply in a ranging one.\n"
        f"  A rule that contradicts a DURABLE LESSON must be flagged as a conflict for Kev, not written as a\n"
        f"    new rule — name the DURABLE LESSON it conflicts with and describe the conflict, don't silently\n"
        f"    override it.\n"
        f"  Only reference tag codes that actually appear in the journal data above — never invent shorthand.\n"
        f"  TRENDING MARKET BEHAVIOUR — CRITICAL is maintained by the system — do not attempt to rewrite it.\n"
        f"  DURABLE LESSONS (HUMAN-CURATED), if present, is maintained by Kev — do not attempt to rewrite it\n"
        f"    either, and do not treat its presence in this prompt as something you wrote.\n\n"
        f"WINDOW LABEL (hard requirement): every fast-tier section below (CONFLUENCE CONDITIONS through\n"
        f"JANE'S PATTERNS) must open with exactly one line before its bullets:\n"
        f"  \"Window: {_window_oldest} -> {_window_newest}, n={len(recent)}, regime: <your one-word read>\"\n"
        f"The dates and n are fixed for this rewrite, given above — only the regime word (trending/ranging/\n"
        f"mixed) is your own judgment from what these {len(recent)} trades show. Lessons are weather reports —\n"
        f"they must say which week's weather.\n\n"
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
        f"HARD RULE — NO ENTRY-TIMING GATES: Never write advice telling future-you to wait for a "
        f"confirmation candle, a rejection candle, a candle close, or any other price-action event AFTER "
        f"the setup is already identified. Entries are decided on the confluence, structure, and score "
        f"already present at scan time — never on watching for one more candle to print. If you're about "
        f"to write 'confirms' or 'confirmatory,' stop and name the specific structural or quantitative "
        f"factor instead (e.g. '4H SR level with 3+ historical touches,' not 'confirmed by price action').\n\n"
        f"FORMAT RULES (hard cap — no exceptions):\n"
        f"- Each section must be bullet points only, no prose paragraphs\n"
        f"- Maximum 3 bullets per section\n"
        f"- Maximum 21 bullets total across all sections\n"
        f"- Each bullet must be one sentence, specific and data-backed\n"
        f"- If you have nothing meaningful to say in a section, write one bullet: 'Insufficient data — revisit next session.'"
    )
    try:
        print(f"[Playbook] Learning session — {_asset_label}: {len(recent)} entries (+ {len(jane_recent)} Jane's)...")
        new_playbook = _call_chev([{"role": "user", "content": prompt}], timeout=120)
        if not new_playbook:
            raise Exception("No response from Chev")
        _pb_path = PLAYBOOK_PATHS.get(asset_type, PLAYBOOK_PATH) if asset_type else PLAYBOOK_PATH
        try:
            with open(_pb_path, "r", encoding="utf-8") as _epf:
                _existing_pb_text = _epf.read()
            new_playbook = _preserve_critical_section(_existing_pb_text, new_playbook)
        except Exception as _pres_e:
            print(f"[Playbook] CRITICAL-section preservation failed for {_asset_label}, writing generated text as-is: {_pres_e}")
        header = (f"CHEV TRADING PLAYBOOK — {_asset_label}\n"
                  f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} "
                  f"| Based on {len(asset_journal)} {_asset_label} trades\n{'='*40}\n\n")
        with open(_pb_path, "w", encoding="utf-8") as f:
            f.write(header + new_playbook)
        print(f"[Playbook] {_asset_label} playbook rewritten → {_pb_path}")

        # ── Step 2: Loss pattern analysis — what setups need more confirmation ──
        loss_trades = [e for e in asset_journal if e.get("outcome") == "LOSS"][-15:]
        if len(loss_trades) >= 3:
            def _fmt_loss(i, e):
                _rm  = e.get("r_multiple")
                _r   = f" ({_rm:+.2f}R)" if _rm is not None else ""
                _inv = e.get("invalidation", "")
                _inv_line = f"\n  Pre-entry invalidation stated: {_inv}" if _inv else ""
                return (
                    f"Trade {i+1}: {e['symbol']} {e.get('direction','').upper()} | {e['ts'][:10]} | "
                    f"Tags: {e.get('tags','none')} | Held: {e.get('duration','?')}{_r}"
                    f"{_inv_line}\n"
                    f"Post-mortem: {(e.get('analysis') or 'none')[:300]}"
                )
            loss_text = "\n\n".join([_fmt_loss(i, e) for i, e in enumerate(loss_trades)])
            loss_prompt = (
                f"Market conditions review — {_asset_label} trades where price did not move as expected.\n\n"
                f"Study these {len(loss_trades)} trades:\n\n"
                f"{loss_text}\n\n"
                f"Your task: identify setup patterns or market conditions that appear across these outcomes. "
                f"This is a data analysis exercise, not a self-criticism exercise. Markets are uncertain — "
                f"not every loss means the setup was wrong. But patterns across these outcomes can reveal "
                f"structural weaknesses that were knowable at entry time.\n\n"
                f"Write exactly 3 bullet points under the heading 'STRUCTURAL RISK FACTORS:'. "
                f"Each bullet describes a MARKET CONDITION or SETUP PATTERN (not a personal failing) "
                f"that these outcomes have in common, and what confluence, structural level, or data point "
                f"available AT SCAN TIME would have flagged the risk in advance.\n\n"
                f"Format: '- [Pattern/Condition]: [What structural/quantitative factor would have flagged the risk]'\n\n"
                f"Example: '- First-visit to a level with no prior touches: treat as untested — require an "
                f"additional timeframe or indicator confluence before sizing normally'\n"
                f"NOT: '- I made a mistake entering here'\n"
                f"HARD RULE: never recommend waiting for a confirmation candle, rejection candle, candle "
                f"close, or any other price-action event after the setup is already identified — entries "
                f"are decided on data available at scan time, not on watching for one more candle.\n\n"
                f"Write only the 3 bullet points. No intro, no conclusion, no additional text."
            )
            loss_analysis = _call_chev([{"role": "user", "content": loss_prompt}], timeout=90)
            if loss_analysis:
                # ── Step 3: Win/Loss comparison — what did winners have that losers didn't ──
                win_trades = [e for e in asset_journal if e.get("outcome") == "WIN"][-10:]
                if len(win_trades) >= 2:
                    def _fmt_win(i, e):
                        _rm  = e.get("r_multiple")
                        _r   = f" ({_rm:+.2f}R)" if _rm is not None else ""
                        return (
                            f"Trade {i+1}: {e['symbol']} {e.get('direction','').upper()} | "
                            f"Tags: {e.get('tags','none')} | {e['ts'][:10]}{_r}\n"
                            f"Post-mortem: {(e.get('analysis') or 'none')[:200]}"
                        )
                    win_text  = "\n\n".join([_fmt_win(i, e) for i, e in enumerate(win_trades[-4:])])
                    loss_text2 = "\n\n".join([_fmt_loss(i, e) for i, e in enumerate(loss_trades[-4:])])
                    compare_prompt = (
                        f"Pattern comparison — {_asset_label} market study.\n\n"
                        f"RECENT WINS (trades where price moved as expected):\n{win_text}\n\n"
                        f"RECENT LOSSES (trades where price moved differently):\n{loss_text2}\n\n"
                        f"What did the winning trades have that the losing trades did not? "
                        f"Look at: the tags (confluence pattern), the market conditions in the post-mortem, "
                        f"the structural quality described. Be specific — 'the wins had a 4H SR level with "
                        f"3+ prior touches' is useful. 'The wins were better setups' is not.\n\n"
                        f"Write exactly 2 bullet points under 'WIN/LOSS INSIGHT:'. "
                        f"Each bullet is one specific, concrete differentiator — never a recommendation to "
                        f"wait for a confirmation candle or additional price action after entry conditions are met. "
                        f"No intro, no conclusion — just the 2 bullets."
                    )
                    compare_analysis = _call_chev([{"role": "user", "content": compare_prompt}], timeout=90)
                else:
                    compare_analysis = None

                # Append both analyses to the playbook as supplemental sections
                supplement = f"\n\n### STRUCTURAL RISK FACTORS\n{loss_analysis}"
                if compare_analysis:
                    supplement += f"\n\n### WIN/LOSS INSIGHT\n{compare_analysis}"
                with open(_pb_path, "a", encoding="utf-8") as f:
                    f.write(supplement)
                print(f"[Playbook] Loss pattern + win/loss insight appended to {_pb_path}")

    except Exception as e:
        print(f"[Playbook] Learning session failed: {e}")

def _classify_outcome(base_outcome, pnl, trade):
    """Phase 6 — the ONE place outcome gets reclassified. A trade that booked a partial
    close earlier (e.g. +1R banked, then the remainder trailed out near breakeven) is
    not a LOSS just because the FINAL leg alone was slightly negative -- the combined
    realized PnL is what actually happened to the account.

    Reclassifies LOSS -> SCRATCH when combined (partial + final leg) PnL is >= -0.1R,
    or LOSS -> WIN when combined PnL is >= 0. Never fires for a trade with no partial
    event (trade.get("partial_done") falsy), and never touches close_type (SL_HIT/
    TP_HIT/etc. stay exactly as recorded) -- classification only, nothing about how
    the trade closed or how partials execute.

    Handles both the bare "WIN"/"LOSS" format and the "CLOSED (WIN)"/"CLOSED (LOSS)"
    wrapped format used by Chev's manual swing-management close, so SCRATCH round-trips
    correctly either way. Returns base_outcome unchanged in every other case.
    """
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
        return base_outcome  # still a real loss even combined -- nothing to change

    new_outcome = f"CLOSED ({inner})" if wrapped else inner
    print(f"[OUTCOME] {trade.get('symbol','?')} reclassified {base_outcome} -> {new_outcome} "
          f"(final leg ${pnl:+.2f} + partial ${partial_pnl:+.2f} = {combined_r:+.2f}R combined)")
    return new_outcome


def _do_postmortem(trade, outcome, pnl, exit_price):
    """Runs in background thread. Asks Chev to analyze the closed trade and saves to journal."""
    outcome = _classify_outcome(outcome, pnl, trade)
    duration = "unknown"
    # "opened_at" is only ever set in-memory at the moment a trade opens -- it does not survive
    # a Dexter restart while the trade is still open (unlike "open_ts", which is persisted to
    # and restored from the Sheet). Fall back to "open_ts" so duration keeps working across a
    # restart instead of silently going "unknown" for any trade that outlives one.
    _opened_raw = trade.get("opened_at") or trade.get("open_ts")
    if _opened_raw:
        try:
            opened = datetime.strptime(_opened_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            delta  = datetime.now(timezone.utc) - opened
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

    _risk_usd  = trade.get("risk_amount_usd", 0)
    _r_mult    = round(pnl / _risk_usd, 2) if _risk_usd > 0 else None
    _r_str     = f" | R-MULTIPLE: {_r_mult:+.2f}R" if _r_mult is not None else ""

    # Circuit breaker: every full close funnels through this function (except the
    # force-close API endpoint, which records its own), so this is the one place that
    # needs to sum any prior partial closes into the trade's total realized R.
    try:
        honest_sim.record_close_R(trade, round(pnl + trade.get("partial_net_pnl", 0.0), 2))
    except Exception as _rr_e:
        print(f"[honest_sim] record_close_R failed for {trade.get('symbol','?')}: {_rr_e}")

    # Trade forensics: reconstruct MAE/MFE/RSI path attribution for the learning session.
    # Never allowed to block the close/journal path — any failure just logs and moves on.
    _forensics = None
    try:
        _fx_tf_map  = {"scalp": "5m", "day": "15m", "swing": "1h"}
        _fx_tf      = _fx_tf_map.get(trade.get("trade_type", "day"), "15m")
        _fx_tf_secs = {"5m": 300, "15m": 900, "1h": 3600}[_fx_tf]
        if trade.get("opened_at"):
            _fx_opened_epoch = int(
                datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=timezone.utc).timestamp()
            )
            _fx_since_epoch = _fx_opened_epoch - 30 * _fx_tf_secs   # buffer for RSI warm-up
            _fx_candles = _fetch_candles_for_labeller(trade["symbol"], _fx_tf, _fx_since_epoch)
            _forensics  = trade_forensics.compute_forensics(trade, _fx_candles)
    except Exception as _fx_e:
        print(f"[Forensics] compute_forensics failed for {trade.get('symbol','?')}: {_fx_e}")
    _str_4h    = trade.get("structure_4h", "")
    _str_inv   = trade.get("invalidation", "")
    _str_conf  = trade.get("confirmation", "")
    _pre_trade_block = ""
    if _str_4h or _str_inv or _str_conf:
        _pre_trade_block = (
            f"YOUR PRE-TRADE ANALYSIS:\n"
            f"  4H Structure : {_str_4h or 'not recorded'}\n"
            f"  Invalidation : {_str_inv or 'not recorded'}\n"
            f"  Entry basis  : {_str_conf or 'not recorded'}\n\n"
        )

    prompt = (
        f"Trade closed. Analyze it as a technical trader.\n\n"
        f"PAIR: {trade['symbol']} | DIRECTION: {trade['direction'].upper()} | RESULT: {outcome} [{close_type}] (${pnl:+.2f}{_r_str})\n"
        f"ENTRY: {trade['entry']} | {sl_label}: {trade['sl']} | TP: {trade['tp']} | EXIT PRICE: {exit_price} | TIME HELD: {duration}{sip_note}\n"
        f"CONFLUENCES: {trade.get('tags', 'none recorded')}\n"
        f"YOUR ORIGINAL REASONING: {trade.get('reasoning', 'none recorded')}\n\n"
        f"{_pre_trade_block}"
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
        "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":           trade["symbol"],
        "asset_type":       trade.get("asset_type", "crypto"),
        "direction":        trade["direction"],
        "entry":            trade["entry"],
        "sl":               trade["sl"],
        "tp":               trade["tp"],
        "exit_price":       exit_price,
        "pnl":              round(pnl, 2),
        "r_multiple":       _r_mult,
        "risk_amount_usd":  _risk_usd,
        "outcome":          outcome,
        "close_type":       trade.get("close_type", "UNKNOWN"),
        "tags":             trade.get("tags", ""),
        "duration":         duration,
        "reasoning":        trade.get("reasoning", ""),
        "structure_4h":     trade.get("structure_4h", ""),
        "invalidation":     trade.get("invalidation", ""),
        "confirmation":     trade.get("confirmation", ""),
        "structural_read":  trade.get("structural_read", ""),
        "analysis":         analysis,
        "reasoning_quality": reasoning_quality,
        "setup_grade":      trade.get("setup_grade", ""),
        "session_quality":  trade.get("session_quality", ""),
        "heat_at_entry":    trade.get("heat_at_entry", 0),
        "position_size_usd": trade.get("position_size_usd", 0),
        "leverage":         trade.get("leverage", 1),
        "chev_moves":       trade.get("chev_moves", []),
        "open_ts":          trade.get("open_ts", ""),
        "forensics":        _forensics,
        "system_era":       SYSTEM_ERA,
    }
    try:
        journal = _load_journal()
        if _is_duplicate_journal_entry(trade["symbol"], trade["entry"], trade["sl"], trade["tp"], journal):
            print(f"[Journal] Duplicate detected for {trade['symbol']} (entry={trade['entry']} SL={trade['sl']} TP={trade['tp']}) — skipping write.")
            return
        journal.append(entry)
        with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
            json.dump(journal, f, indent=2)
        print(f"[Journal] Post-mortem saved for {trade['symbol']} ({outcome}). Total: {len(journal)} entries.")
        icon = "✓" if outcome == "WIN" else ("➖" if outcome == "SCRATCH" else "✗")
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        send_telegram_alert(f"{trade['symbol']} {trade['direction'].upper()} {icon} {pnl_str}")
        global _combined_closed_count
        _combined_closed_count += 1
        _maybe_run_cross_analysis()
        if len(journal) % 10 == 0:
            jane_j = _load_jane_journal()
            for _at in ("forex", "crypto", "stocks"):
                threading.Thread(target=_run_learning_session, args=(journal, jane_j, _at), daemon=True).start()
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


def _fetch_candles_for_labeller(symbol: str, tf: str, since_epoch: int) -> list:
    """
    Wrapper: translates labeller's (symbol, tf, since_epoch) call into fetch_candles().
    Returns list of {"t": int_epoch, "o", "h", "l", "c"} dicts for candles after since_epoch.
    Determines asset_type from symbol heuristic (USDT→crypto, "/"→forex, else stock).
    """
    if symbol.endswith("USDT") or symbol.endswith("BTC") or symbol.endswith("ETH"):
        asset_type = "crypto"
    elif "/" in symbol:
        asset_type = "forex"
    else:
        asset_type = "stock"
    df = fetch_candles(symbol, asset_type, tf, limit=700)
    if df is None or df.empty:
        return []
    out = []
    for idx, row in df.iterrows():
        # Index is tz-naive UTC from the exchange fetch functions — interpret as UTC
        t = int(idx.to_pydatetime().replace(tzinfo=timezone.utc).timestamp())
        if t >= since_epoch:
            out.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                        "l": float(row["low"]), "c": float(row["close"])})
    return out


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


def _div_strength_score(d, tf_str, confirmed=True):
    """
    Score a divergence 0–3.0 pts using depth, RSI delta, span, and price move.
    Geometric mean of depth × delta ensures both must be decent — a huge span
    with only 2 RSI points of divergence stays near zero.
    confirmed=False applies a 0.75 forming-div discount.
    """
    import math
    bias    = d.get('bias', 'bull')
    rsi_t1  = float(d.get('rsi_t1', 50))
    rsi_t2  = float(d.get('rsi_t2', 50))
    p1      = float(d.get('price_t1') or d.get('pivot_price') or 0)
    p2      = float(d.get('price_t2') or d.get('cur_price')   or 0)

    # Depth: how extreme was RSI at the first pivot
    if bias == 'bull':
        D = max(0.0, min(1.0, (50 - rsi_t1) / 30))   # RSI 20 → 1.0 | RSI 50 → 0
    else:
        D = max(0.0, min(1.0, (rsi_t1 - 50) / 30))   # RSI 80 → 1.0 | RSI 50 → 0

    # RSI delta: how far did RSI diverge from t1 to t2
    R = min(1.0, abs(rsi_t2 - rsi_t1) / 20.0)        # 20pt gap → 1.0

    # Span: bars between pivots, scaled per TF
    max_bars = {'15m': 40, '30m': 40, '1h': 32, '4h': 40}.get(tf_str, 40)
    age_bars = int(d.get('age_bars') or 0)
    if age_bars == 0 and d.get('ts_t1') and d.get('ts_t2'):
        tf_secs  = {'15m': 900, '30m': 1800, '1h': 3600, '4h': 14400}.get(tf_str, 3600)
        age_bars = max(0, (int(d['ts_t2']) - int(d['ts_t1'])) // tf_secs)
    S = min(1.0, age_bars / max_bars)

    # Price move: how far did price travel in the "wrong" direction (5% → P=1.0)
    P = 0.0
    if p1 > 0 and p2 > 0:
        P = min(1.0, abs(p2 - p1) / p1 * 20)

    # Geometric mean of D and R (both must be decent) — span and price add bonus
    base  = math.sqrt(D * R)
    score = base * (0.60 + 0.25 * S + 0.15 * P)

    if not confirmed:
        score *= 0.75

    return round(score * 3.0, 2)   # scale to 0–3.0 pts


def _detect_forming_divergence(df, lookback=150, mini=5, lb=10):
    """Detect forming divergences: confirmed first pivot (dual, lb-bar) + current mini-extreme has exceeded it + RSI diverging.
    Returns list of dicts sorted by score desc, up to one of each type."""
    if "RSI" not in df.columns:
        return []
    rsi   = df["RSI"].values
    highs = df["high"].values
    lows  = df["low"].values
    n     = len(df)
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

    def _ts(i):
        try:
            return int(df.index[i].timestamp())
        except Exception:
            return i

    results = []
    seen    = set()

    # Regular Bearish: price new high BUT RSI lower — momentum exhaustion
    if cur_rhi is not None:
        for pi, pp, pr in reversed(sw_h):
            if "rb" in seen: break
            if cur_hi > pp:
                gap = pr - cur_rhi
                if gap >= 5:
                    results.append({"type": "Regular Bearish", "bias": "bear", "forming": True,
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_hi, 5), "cur_rsi": round(cur_rhi, 2),
                                    "ts_t1": _ts(pi),        "price_t1": round(pp, 5),    "rsi_t1": round(pr, 2),
                                    "ts_t2": _ts(cur_hi_idx),"price_t2": round(cur_hi, 5),"rsi_t2": round(cur_rhi, 2)})
                    seen.add("rb")

    # Regular Bullish: price new low BUT RSI higher — selling exhaustion
    if cur_rlo is not None:
        for pi, pp, pr in reversed(sw_l):
            if "bull" in seen: break
            if cur_lo < pp:
                gap = cur_rlo - pr
                if gap >= 5:
                    results.append({"type": "Regular Bullish", "bias": "bull", "forming": True,
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_lo, 5), "cur_rsi": round(cur_rlo, 2),
                                    "ts_t1": _ts(pi),        "price_t1": round(pp, 5),    "rsi_t1": round(pr, 2),
                                    "ts_t2": _ts(cur_lo_idx),"price_t2": round(cur_lo, 5),"rsi_t2": round(cur_rlo, 2)})
                    seen.add("bull")

    # Hidden Bearish: price lower high BUT RSI higher high — downtrend continuation
    if cur_rhi is not None:
        for pi, pp, pr in reversed(sw_h):
            if "hbear" in seen: break
            if cur_hi < pp:
                gap = cur_rhi - pr
                if gap >= 5:
                    results.append({"type": "Hidden Bearish", "bias": "bear", "forming": True,
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_hi, 5), "cur_rsi": round(cur_rhi, 2),
                                    "ts_t1": _ts(pi),        "price_t1": round(pp, 5),    "rsi_t1": round(pr, 2),
                                    "ts_t2": _ts(cur_hi_idx),"price_t2": round(cur_hi, 5),"rsi_t2": round(cur_rhi, 2)})
                    seen.add("hbear")

    # Hidden Bullish: price higher low BUT RSI lower low — uptrend continuation
    if cur_rlo is not None:
        for pi, pp, pr in reversed(sw_l):
            if "hbull" in seen: break
            if cur_lo > pp:
                gap = pr - cur_rlo
                if gap >= 5:
                    results.append({"type": "Hidden Bullish", "bias": "bull", "forming": True,
                                    "rsi_gap": round(gap, 1), "score": _forming_div_score(gap),
                                    "age_bars": n - 1 - pi,
                                    "pivot_price": round(pp, 5), "pivot_rsi": round(pr, 2),
                                    "cur_price": round(cur_lo, 5), "cur_rsi": round(cur_rlo, 2),
                                    "ts_t1": _ts(pi),        "price_t1": round(pp, 5),    "rsi_t1": round(pr, 2),
                                    "ts_t2": _ts(cur_lo_idx),"price_t2": round(cur_lo, 5),"rsi_t2": round(cur_rlo, 2)})
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

    def _ts(i):
        try:
            return int(df.index[i].timestamp())
        except Exception:
            return i

    def _strongest(pivots, checks):
        best = None; best_score = (-1, 0.0)
        for k in range(len(pivots) - 1):
            i1, p1 = pivots[k]; i2, p2 = pivots[k + 1]
            r1, r2 = float(rsi[i1]), float(rsi[i2])
            if np.isnan(r1) or np.isnan(r2) or abs(r2 - r1) < 2.0:
                continue
            for pc, rc, typ, bias, note in checks:
                if pc(p1, p2) and rc(r1, r2):
                    score = (i2, abs(r2 - r1))
                    if score > best_score:
                        best_score = score
                        best = {
                            "type": typ, "bias": bias, "price": round(float(p2), 5), "note": note,
                            "ts_t1": _ts(i1), "price_t1": round(float(p1), 5), "rsi_t1": round(r1, 2),
                            "ts_t2": _ts(i2), "price_t2": round(float(p2), 5), "rsi_t2": round(r2, 2),
                        }
        return best

    found = []
    bull = _strongest(sw_l, [
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Regular Bullish Divergence", "bull", "price LL, RSI HL — selling momentum fading"),
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Hidden Bullish Divergence",  "bull", "price HL, RSI LL — uptrend continuation"),
    ])
    bear = _strongest(sw_h, [
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Regular Bearish Divergence", "bear", "price HH, RSI LH — momentum fading"),
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Hidden Bearish Divergence",  "bear", "price LH, RSI HH — downtrend continuation"),
    ])
    if bull: found.append(bull)
    if bear: found.append(bear)
    return found


def is_ny_market_hours():
    from datetime import datetime, timezone, timedelta
    ny_offset = timedelta(hours=-4)
    ny_time = datetime.now(timezone.utc) + ny_offset
    if ny_time.weekday() >= 5:   # Sat=5, Sun=6 — market closed
        return False
    return 9 <= ny_time.hour < 18


def is_forex_market_open():
    """Forex is closed from Friday 22:00 UTC to Sunday 22:00 UTC."""
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    wd = now_utc.weekday()          # Mon=0 ... Sun=6
    if wd == 5:
        return False                 # all of Saturday
    if wd == 4 and now_utc.hour >= 22:
        return False                 # Friday after close
    if wd == 6 and now_utc.hour < 22:
        return False                 # Sunday before open
    return True


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

    def _ts(i):
        try:
            return int(df.index[i].timestamp())
        except Exception:
            return i

    def _strongest(pivots, checks):
        best = None; best_score = (-1, 0.0)
        for k in range(len(pivots) - 1):
            i1, p1 = pivots[k]; i2, p2 = pivots[k + 1]
            r1, r2 = float(rsi[i1]), float(rsi[i2])
            if np.isnan(r1) or np.isnan(r2) or abs(r2 - r1) < 2.0:
                continue
            for pc, rc, typ, bias, note in checks:
                if pc(p1, p2) and rc(r1, r2):
                    score = (i2, abs(r2 - r1))
                    if score > best_score:
                        best_score = score
                        best = {
                            "type": typ, "bias": bias, "price": round(float(p2), 5), "note": note,
                            "ts_t1": _ts(i1), "price_t1": round(float(p1), 5), "rsi_t1": round(r1, 2),
                            "ts_t2": _ts(i2), "price_t2": round(float(p2), 5), "rsi_t2": round(r2, 2),
                        }
        return best

    found = []
    bull = _strongest(sw_l, [
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Regular Bullish Divergence", "bull", "price lower low, RSI higher low — selling momentum fading"),
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Hidden Bullish Divergence",  "bull", "price higher low, RSI lower low — uptrend likely continuing"),
    ])
    bear = _strongest(sw_h, [
        (lambda p1,p2: p2 > p1, lambda r1,r2: r2 < r1, "Regular Bearish Divergence", "bear", "price higher high, RSI lower high — upside momentum fading"),
        (lambda p1,p2: p2 < p1, lambda r1,r2: r2 > r1, "Hidden Bearish Divergence",  "bear", "price lower high, RSI higher high — downtrend likely continuing"),
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


def _score_level_health(symbol, asset_type, tf, level_price, level_kind, df=None):
    """Grade whether an SR/TP level ahead of a running trade is still likely to hold.

    Reuses _validate_anchor (built for auction-anchor entry validation) against a
    synthetic anchor built from the level's own price — same confirmed/active math,
    applied to trade management instead of entry validation.
    Returns {verdict, confirmed, active, confidence, invalidation_reason} or None if
    the level isn't represented in recent candles.
    """
    if df is None:
        try:
            df = fetch_candles(symbol, asset_type, tf, limit=200)
        except Exception as e:
            print(f"[LEVEL HEALTH] candle fetch failed for {symbol} {tf}: {e}")
            return None

    if df is None or len(df) == 0:
        return None

    closes   = df["close"].values
    n        = len(closes)
    lookback = min(100, n)

    best_idx, best_dist = None, None
    for i in range(n - 1, n - 1 - lookback, -1):
        dist = abs(float(closes[i]) - level_price)
        if best_dist is None or dist < best_dist:
            best_dist, best_idx = dist, i

    if best_idx is None or abs(float(closes[best_idx]) - level_price) / level_price * 100 > 3.0:
        return None

    anchor = {
        "idx":         best_idx,
        "price":       level_price,
        "anchor_type": "swing_high" if level_kind == "resistance" else "swing_low",
        "confidence":  50,
    }
    _validate_anchor(df, anchor)

    if not anchor["active"]:
        verdict = "BROKEN"
    elif anchor["confirmed"] and anchor["confidence"] >= 60:
        verdict = "HOLDING"
    else:
        verdict = "WEAKENING"

    return {
        "verdict":             verdict,
        "confirmed":           anchor["confirmed"],
        "active":              anchor["active"],
        "confidence":          anchor["confidence"],
        "invalidation_reason": anchor["invalidation_reason"],
    }


def _check_level_proximity(trade, current_price):
    """Is current_price inside the 1.5% proximity band of a level ahead of this trade
    (TP itself, or a validated SR zone between entry and TP)? Returns the nearest
    qualifying level with its health score, or None."""
    is_long = trade["direction"] == "long"

    candidates = [{"price": trade["tp"], "kind": "resistance" if is_long else "support", "label": "TP"}]

    res_levels, sup_levels = _get_levels_for_management(trade["symbol"], trade["asset_type"], trade["primary_tf"])

    for zone in (res_levels if is_long else sup_levels):
        between_price_and_tp = (
            (trade["entry"] < zone["price"] < trade["tp"]) if is_long
            else (trade["tp"] < zone["price"] < trade["entry"])
        )
        if between_price_and_tp:
            candidates.append({"price": zone["price"], "kind": zone["kind"],
                                "instances": zone.get("instances", 1), "label": "SR"})

    best = None
    for level in candidates:
        within_band = abs(current_price - level["price"]) / level["price"] * 100 <= 1.5
        if not within_band:
            continue
        health = _score_level_health(trade["symbol"], trade["asset_type"], trade["primary_tf"],
                                      level["price"], level["kind"])
        if health is None:
            continue
        dist_pct = abs(current_price - level["price"]) / level["price"] * 100
        if best is None or dist_pct < best["dist_pct"]:
            best = {"level_price": level["price"], "label": level["label"],
                    "health": health, "dist_pct": dist_pct}

    return best


def _get_levels_for_management(symbol, asset_type, tf):
    """Multi-TF validated SR zones for trade management — mirrors scan_pair_tf's SR
    block (same _ca_get_timed_touches/_ca_build_validated_levels pattern), since
    _ca_build_validated_levels only returns zones when touch data spans the full
    min_touches_map of timeframes, not just one. Returns (res_levels, sup_levels);
    ([], []) on any failure."""
    try:
        all_sr_tfs = ["15m", "30m", "1h", "4h"]
        if tf == "5m":
            all_sr_tfs = ["5m", "15m", "30m", "1h"]
        fetch_tfs = list({tf} | set(all_sr_tfs))

        min_touches_map = ({"5m": 8, "15m": 5, "30m": 3, "1h": 2, "4h": 2}
                           if asset_type == "crypto" else {"15m": 6, "30m": 4, "1h": 2, "4h": 2})

        tf_data = {}
        for t in fetch_tfs:
            df = fetch_candles(symbol, asset_type, t, limit=200)
            if df is not None:
                tf_data[t] = df

        res_touches_by_tf, sup_touches_by_tf = {}, {}
        for t in all_sr_tfs:
            df = tf_data.get(t)
            if df is None:
                continue
            touches = _ca_get_timed_touches(df)
            res_touches_by_tf[t] = [(ts, p) for ts, p, k in touches if k == "resistance"]
            sup_touches_by_tf[t] = [(ts, p) for ts, p, k in touches if k == "support"]

        res_levels = _ca_build_validated_levels(res_touches_by_tf, "resistance", min_touches_map=min_touches_map)
        sup_levels = _ca_build_validated_levels(sup_touches_by_tf, "support",    min_touches_map=min_touches_map)
        return res_levels, sup_levels
    except Exception as e:
        print(f"[LEVEL HEALTH] _get_levels_for_management failed for {symbol}: {e}")
        return [], []


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


def _detect_vp_anchor(df, max_lookback=200, pivot_bars=5, recent_window=40):
    """VP-only auction anchor — finds the TRUE start of the current auction/range,
    not just the most recent clean pivot.

    Unlike _detect_auction_anchor (which prefers the single strongest dual-pivot
    swing — correct for Fib/VWAP/SR, which want "the most recent clean swing"),
    Volume Profile needs to represent the FULL life of the current range. A
    dual-pivot swing is frequently just a recent retest inside an already-
    existing range (it scores well because RSI+price coincide there), not the
    range's true origin — verified against a real chart where the algorithm
    anchored at a mid-range retest dip instead of the original breakout candle.

    Method: walk ATR compression -> expansion transitions from OLDEST to NEWEST
    (the reverse of _detect_auction_anchor's ATR fallback, which stops at the
    first/most-recent transition). For each candidate, oldest first, test
    whether the value area (POC/VAH/VAL) computed from that candidate to now is
    still statistically the same as the value area computed from just the
    recent window — i.e. the whole span is still one auction. The comparison
    is ATR-scaled, not exact, so an ordinary wick/fakeout candle cannot break
    the chain (the range doesn't have to be perfect — noise/fakeouts expected).
    First (oldest) passing candidate wins, since that gives VP the longest
    history that's still provably the same auction. Falls back to the farthest
    qualifying fractal (same as _detect_auction_anchor's Method 3) if no
    candidate passes.

    Returns: { idx, price, anchor_type, confidence, method } (run through
    _validate_anchor) or None — same shape/failure contract as
    _detect_auction_anchor, so callers need no changes beyond which function
    they call.
    """
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

    # ── Step A: ATR compression -> expansion candidates, OLDEST first ────────
    # Same thresholds as _detect_auction_anchor's Method 2 (dexter.py ATR
    # fallback) — this is a generalization (collect every transition across
    # the lookback), not a re-tune.
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
                    if idx_c < n - pivot_bars:  # skip transitions too close to "now"
                        candidates.append(idx_c)
                    in_compression = False

    # ── Step B: value-area stability test, oldest candidate first ────────────
    # ATR-scaled tolerance — a starting constant, expect to retune after real-
    # chart validation. Requires ALL THREE of POC/VAH/VAL within tolerance (not
    # an average) so a shape change can't hide behind two levels cancelling out.
    tol = atr_now * 1.5
    # A candidate can fail for two very different reasons that this raw number
    # comparison alone can't tell apart: (a) an ordinary sharp move THAT HAPPENED
    # INSIDE a single persistent range (ordinary volatility — ordinary noise, not
    # a regime change) or (b) a genuine, un-retraced breakout/breakdown into a
    # separate distribution that the older history never traded back into (e.g.
    # an abandoned old high-price zone merged into a much lower current range).
    # The distinguishing fact is simple: did the older history ever actually
    # overlap the current value area, or did it leave and never come back? At
    # least MIN_OLDER_BARS is required for the check to be meaningful on a short
    # window; OVERLAP_MIN_FRAC is deliberately low (15%) — this only needs to
    # rule out "never once traded here again", not demand heavy time-in-zone.
    MIN_OLDER_BARS   = 10
    OVERLAP_MIN_FRAC = 0.15
    for idx_c in candidates:
        vp_full = _ca_volume_profile(df, idx_c, n - 1, n_bins=24)
        if not vp_full:
            continue
        recent_start = max(idx_c, n - recent_window)
        if recent_start == idx_c:
            passed = True  # candidate already inside the recent window — trivially stable
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
                returned_to_zone = True  # too little older history to judge either way
            passed = value_stable and returned_to_zone
        if passed:
            return _validate_anchor(df, {
                "idx":         idx_c,
                "price":       round(float(closes[idx_c]), 8),
                "anchor_type": "atr_breakout",
                "confidence":  55,  # above Method 2's flat 50 — stability-validated,
                                    # not just "most recent transition"
                "method":      "vp_stability",
            })

    # ── Fallback: farthest qualifying fractal ≥ 3 ATR (same as
    # _detect_auction_anchor's Method 3) ──────────────────────────────────────
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
    return {
        "poc": poc_price,
        "vah": bin_edges[max(included) + 1],
        "val": bin_edges[min(included)],
        "bin_edges": bin_edges.tolist(),
        "bin_volumes": bin_volumes.tolist(),
    }


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


_pdhl_cache = {}   # (symbol, asset_type) -> (utc_date_str, {"pdh","pdl","pdc"})

def _get_previous_day_hl(symbol, asset_type):
    """Fetch yesterday's high, low, close.

    Cached per (symbol, asset_type) for the rest of the current UTC day -- this value
    only changes once every 24h, so fetch_candles' shared 60s cache (sized for
    fast-moving intraday data) is the wrong invalidation window for it and would
    otherwise refetch on nearly every scan round. Same per-symbol day-guard pattern as
    derivs.py's cache. Failures are NOT cached -- a transient fetch error retries next
    scan instead of silently blocking this symbol for the rest of the day.
    """
    key   = (symbol, asset_type)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hit   = _pdhl_cache.get(key)
    if hit and hit[0] == today:
        return hit[1]
    try:
        df = fetch_candles(symbol, asset_type, "1d", limit=5)
        if len(df) < 2:
            return None
        yd = df.iloc[-2]
        result = {"pdh": float(yd["high"]), "pdl": float(yd["low"]), "pdc": float(yd["close"])}
        _pdhl_cache[key] = (today, result)
        return result
    except Exception:
        return None


def _get_session_context():
    """Identify current trading session and volatility expectation.

    Also returns session_start_ts: the UTC timestamp of the most recently opened
    currently-active main session (Asian/London/NY), or None if none is active right
    now (the dead zone / transition window). This is the single source of truth for
    "when did the current session open" -- opening-range logic reads this rather than
    re-deriving session boundaries independently. When London and NY overlap (13-16
    UTC), NY's later start wins -- it's the fresher, more relevant open.
    """
    now = datetime.now(timezone.utc)
    h   = now.hour
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sessions = []
    _session_starts = []   # (start_ts, label) for each currently-active main session
    if 0 <= h < 8:
        sessions.append("Asian (00:00-08:00 UTC) — low volatility, range-bound, watch for fakeouts")
        _session_starts.append((today_midnight, "Asian"))
    if 8 <= h < 16:
        sessions.append("London (08:00-16:00 UTC) — high volatility, trends form here")
        _session_starts.append((today_midnight.replace(hour=8), "London"))
    if 13 <= h < 21:
        sessions.append("New York (13:00-21:00 UTC) — second high-volatility window")
        _session_starts.append((today_midnight.replace(hour=13), "New York"))
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
    session_start_ts = max(_session_starts, key=lambda x: x[0])[0] if _session_starts else None
    return {"lines": sessions, "utc": now.strftime("%H:%M UTC"), "session_start_ts": session_start_ts}


def _get_opening_range(df, session_start_ts, num_candles=4):
    """First `num_candles` candles of `df` at/after `session_start_ts` -> {"or_high", "or_low"}.

    Returns None if no session is currently active (session_start_ts is None -- the
    dead zone/transition window) or if `df` doesn't reach back far enough to cover that
    session's open (never falls back to a stale/older range -- degrades cleanly instead).

    num_candles default=4: on the fast TFs Dexter actually scans (15m), 4 candles is
    the first hour after open -- a reasonable "opening range" window. Callers on a
    different primary TF should pass a count sized for that TF (see the
    _or_candles_by_tf map at the scan_pair_tf call site) rather than relying on this
    default, since a sensible window width scales with candle duration.
    """
    if session_start_ts is None or df is None or len(df) == 0:
        return None
    try:
        first_idx = None
        for i in range(len(df)):
            ts = df.index[i]
            if ts.tzinfo is None:
                ts = ts.tz_localize(timezone.utc)
            if ts >= session_start_ts:
                first_idx = i
                break
        if first_idx is None:
            return None  # session hasn't opened within the fetched window
        window = df.iloc[first_idx: first_idx + num_candles]
        if window.empty:
            return None
        return {"or_high": float(window["high"].max()), "or_low": float(window["low"].min())}
    except Exception:
        return None


def _get_adr(symbol, asset_type, lookback=14):
    """Average Daily Range over the past N days. Returns a dict with adr_abs and adr_pct, or None."""
    try:
        df = fetch_candles(symbol, asset_type, "1d", limit=lookback + 5)
        if len(df) < 5:
            return None
        recent = df.tail(lookback)
        daily_ranges = recent["high"] - recent["low"]
        adr_abs = float(daily_ranges.mean())
        mid     = float(df["close"].iloc[-1])
        adr_pct = round(adr_abs / mid * 100, 2) if mid > 0 else 0
        today_range = float(df["high"].iloc[-1]) - float(df["low"].iloc[-1])
        used_pct = round(today_range / adr_abs * 100, 0) if adr_abs > 0 else 0
        return {"adr_abs": adr_abs, "adr_pct": adr_pct, "today_range": today_range, "used_pct": used_pct}
    except Exception:
        return None


def _zone_dwell_analysis(df, zone_price, atr, lookback=30):
    """Count how many recent candles rejected at a confluence zone + classify current candle quality."""
    if df is None or len(df) < 5 or zone_price is None:
        return None
    try:
        margin    = max(0.0005, atr * 0.5) if atr else abs(zone_price) * 0.002
        zone_low  = zone_price - margin
        zone_high = zone_price + margin
        recent    = df.tail(lookback)
        test_count = 0
        for _, row in recent.iterrows():
            if not (row["low"] <= zone_high and row["high"] >= zone_low):
                continue
            body_low  = min(row["open"], row["close"])
            body_high = max(row["open"], row["close"])
            if not (body_low <= zone_high and body_high >= zone_low):
                test_count += 1  # wick touched zone, body closed outside = rejection
        last    = df.iloc[-1]
        body    = abs(last["close"] - last["open"])
        up_wick = last["high"] - max(last["open"], last["close"])
        dn_wick = min(last["open"], last["close"]) - last["low"]
        at_zone = last["low"] <= zone_high and last["high"] >= zone_low
        if not at_zone:
            candle_note = "price not yet touching zone — approaching"
        elif body > 0 and dn_wick >= body * 2.0 and up_wick <= body * 0.5:
            candle_note = "HAMMER — long lower wick, strong bullish rejection at zone"
        elif body > 0 and up_wick >= body * 2.0 and dn_wick <= body * 0.5:
            candle_note = "SHOOTING STAR — long upper wick, strong bearish rejection at zone"
        elif body > 0 and (up_wick + dn_wick) <= body * 0.3:
            dirn = "BULLISH" if last["close"] > last["open"] else "BEARISH"
            candle_note = f"STRONG {dirn} BODY — decisive close, clear directional commitment"
        elif body == 0 or body < (up_wick + dn_wick) * 0.15:
            candle_note = "DOJI — equal buying and selling pressure, no directional commitment yet"
        else:
            candle_note = "standard candle at zone — moderate, watch next close for confirmation"
        if test_count == 0:
            quality = "FIRST VISIT — no prior rejection data at this level (unproven)"
        elif test_count <= 2:
            quality = f"{test_count} prior rejection(s) — DEVELOPING, level beginning to show influence"
        else:
            quality = f"{test_count} prior rejections — ESTABLISHED, market repeatedly respects this level"
        return {"test_count": test_count, "quality": quality, "candle_note": candle_note}
    except Exception:
        return None


def _bos_choch_label(df, window=3):
    """Detect most recent Break of Structure (BOS) or Change of Character (CHoCH) from swing sequence."""
    if df is None or len(df) < 25:
        return None
    try:
        highs = df["high"].values
        lows  = df["low"].values
        n     = len(highs)
        sh, sl = [], []
        for i in range(window, n - window):
            if all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, window + 1)):
                sh.append(float(highs[i]))
            if all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, window + 1)):
                sl.append(float(lows[i]))
        if len(sh) < 2 or len(sl) < 2:
            return None
        last_sh, prev_sh = sh[-1], sh[-2]
        last_sl, prev_sl = sl[-1], sl[-2]
        hh = last_sh > prev_sh
        lh = last_sh < prev_sh
        hl = last_sl > prev_sl
        ll = last_sl < prev_sl
        if hh and hl:
            return {"event": "BOS BULLISH",
                    "detail": f"swing high {prev_sh:.5g} → {last_sh:.5g} (HH) + HL at {last_sl:.5g} — uptrend intact",
                    "hold":   f"above HL {last_sl:.5g}",
                    "warning": f"CHoCH bearish if price closes below last HL ({last_sl:.5g})"}
        if ll and lh:
            return {"event": "BOS BEARISH",
                    "detail": f"swing low {prev_sl:.5g} → {last_sl:.5g} (LL) + LH at {last_sh:.5g} — downtrend intact",
                    "hold":   f"below LH {last_sh:.5g}",
                    "warning": f"CHoCH bullish if price closes above last LH ({last_sh:.5g})"}
        if lh and hl:
            return {"event": "CHoCH BEARISH WARNING",
                    "detail": f"first LH printed ({last_sh:.5g}) after uptrend — momentum fading",
                    "hold":   f"above last HL {last_sl:.5g}",
                    "warning": f"confirmed BOS bearish if price closes below HL ({last_sl:.5g})"}
        if hh and ll:
            return {"event": "CHoCH BULLISH WARNING",
                    "detail": f"first HL printed ({last_sl:.5g}) after downtrend — momentum fading",
                    "hold":   f"below last LH {last_sh:.5g}",
                    "warning": f"confirmed BOS bullish if price closes above LH ({last_sh:.5g})"}
        return {"event": "STRUCTURE UNCLEAR",
                "detail": "mixed swing sequence — no dominant HH/HL or LH/LL direction",
                "hold":   None,
                "warning": "wait for a clear BOS before committing to a directional bias"}
    except Exception:
        return None


def _macro_trend_context(df_4h):
    """
    Compute multi-day 4H swing structure and return a formatted text block + verdict string.
    Verdict: STRONG_UPTREND | UPTREND | RANGING | DOWNTREND | STRONG_DOWNTREND
    """
    try:
        if df_4h is None or len(df_4h) < 7:
            return "", "UNKNOWN"

        candles = df_4h.tail(18)  # ~3 days of 4H candles
        opens   = candles["open"].values.tolist()
        highs   = candles["high"].values.tolist()
        lows    = candles["low"].values.tolist()
        closes  = candles["close"].values.tolist()
        volumes = candles["volume"].values.tolist() if "volume" in candles.columns else []

        # Bull/bear count
        bull_count = sum(1 for o, c in zip(opens, closes) if c > o)
        bear_count = len(closes) - bull_count
        net_pct    = round((closes[-1] - opens[0]) / opens[0] * 100, 2) if opens[0] else 0

        # Detect HH/HL (uptrend) or LH/LL (downtrend) from last 6 pivots using swing highs/lows
        # Use last 6 candles for pivot detection (each candle's high/low as a pivot)
        n = min(6, len(highs))
        recent_highs = highs[-n:]
        recent_lows  = lows[-n:]

        hh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] > recent_highs[i-1])
        hl_count = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  > recent_lows[i-1])
        lh_count = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] < recent_highs[i-1])
        ll_count = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  < recent_lows[i-1])

        total_swings = n - 1
        up_score   = hh_count + hl_count
        down_score = lh_count + ll_count

        # Volume analysis — is trending volume above average?
        vol_note = ""
        if volumes and len(volumes) >= 6:
            avg_vol  = sum(volumes) / len(volumes)
            bull_vols = [v for o, c, v in zip(opens, closes, volumes) if c > o]
            bear_vols = [v for o, c, v in zip(opens, closes, volumes) if c < o]
            if bull_vols and bear_vols:
                avg_bull = sum(bull_vols) / len(bull_vols)
                avg_bear = sum(bear_vols) / len(bear_vols)
                if avg_bull > avg_bear * 1.2:
                    vol_note = "  Volume profile: BUY volume dominates on up-candles — trend has institutional backing."
                elif avg_bear > avg_bull * 1.2:
                    vol_note = "  Volume profile: SELL volume dominates on down-candles — trend has institutional backing."
                else:
                    vol_note = "  Volume profile: balanced — no strong institutional directional commitment."

        # Verdict
        if net_pct >= 5 and up_score >= total_swings * 0.7:
            verdict = "STRONG_UPTREND"
            summary = f"STRONG UPTREND — {net_pct:+.1f}% over last {len(closes)} × 4H candles. {hh_count}/{total_swings} higher highs, {hl_count}/{total_swings} higher lows."
        elif net_pct >= 2 and up_score > down_score:
            verdict = "UPTREND"
            summary = f"UPTREND — {net_pct:+.1f}% over last {len(closes)} × 4H candles. {hh_count}/{total_swings} higher highs, {hl_count}/{total_swings} higher lows."
        elif net_pct <= -5 and down_score >= total_swings * 0.7:
            verdict = "STRONG_DOWNTREND"
            summary = f"STRONG DOWNTREND — {net_pct:+.1f}% over last {len(closes)} × 4H candles. {lh_count}/{total_swings} lower highs, {ll_count}/{total_swings} lower lows."
        elif net_pct <= -2 and down_score > up_score:
            verdict = "DOWNTREND"
            summary = f"DOWNTREND — {net_pct:+.1f}% over last {len(closes)} × 4H candles. {lh_count}/{total_swings} lower highs, {ll_count}/{total_swings} lower lows."
        else:
            verdict = "RANGING"
            summary = f"RANGING — {net_pct:+.1f}% net over last {len(closes)} × 4H candles. Mixed HH/LL structure, no dominant direction."

        candle_summary = f"  Last {len(closes)} candles: {bull_count} bullish / {bear_count} bearish"

        # Phase 2b: the TREND IMPLICATION paragraph (per-verdict resistance/support-as-
        # continuation-zone explanation) is deleted here -- it duplicated the wrapper's
        # counter_trend_warning almost verbatim. Verdict + HH/HL counts are the signal;
        # that explanation now lives once, in the wrapper.
        block = (
            f"\n--- 4H Macro Trend Context (last ~3 days) ---\n"
            f"  Verdict    : {summary}\n"
            f"{candle_summary}\n"
            f"{vol_note}\n"
        )
        return block, verdict

    except Exception as e:
        return "", "UNKNOWN"


def _build_rich_market_brief(symbol, asset_type, primary_tf="1h", confluence_zone=None):
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
        # Phase 2b diet: raw row counts, not fetch depth (tf_fetch_limits above still
        # fetches deep history for every computed section -- SR/VP/auction/swings/fib
        # all still see the full window). Position 2 (e.g. "1d" when primary=1h) gets 0
        # raw rows -- PDH/PDL, macro trend context, and the per-TF summary already carry
        # that timeframe's story; see the last-5-closes fallback line below instead.
        full_data_limits = dict(zip(full_data_tfs, [30, 15, 0]))

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

        # ── Primary TF stats ──────────────────────────────────────────────
        vwap      = primary_df["VWAP"].iloc[-1]
        ema20     = primary_df["EMA20"].iloc[-1]
        rsi_1h    = primary_df["RSI"].iloc[-1]
        atr_1h    = primary_df["ATR"].iloc[-1]   # always 1H for VWAP/BB reference
        bb_upper  = primary_df["BB_upper"].iloc[-1]
        bb_mid    = primary_df["BB_mid"].iloc[-1]
        bb_lower  = primary_df["BB_lower"].iloc[-1]
        bb_width  = round((bb_upper - bb_lower) / bb_mid * 100, 2) if bb_mid else 0
        _bb_pct_to_upper = round((bb_upper - current_price) / current_price * 100, 2)
        _bb_pct_to_lower = round((current_price - bb_lower) / current_price * 100, 2)
        _bb_pct_to_mid   = round(abs(current_price - bb_mid) / current_price * 100, 2)
        vol_trend = _ca_volume_trend(primary_df)
        fib_levels, fib_direction = _ca_fib_from_real_impulse(primary_df)
        divergences = (_ca_detect_rsi_divergence(primary_df)
                       + _ca_detect_rsi_divergence_forming(primary_df)
                       + _detect_hidden_divergence(primary_df))

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
                _anc4 = _detect_vp_anchor(_df4)
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
                _anc1 = _detect_vp_anchor(_df1)
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
        adr     = _get_adr(symbol, asset_type) if _norm_asset_type(asset_type) in ("forex", "stocks") else None

        # ── Assemble brief ────────────────────────────────────────────────
        lines = [f"=== MARKET BRIEF: {symbol} ({asset_type.upper()}) ===\n"]
        lines.append(f"Time: {session['utc']}  |  Session: {' | '.join(session['lines'])}")

        if pdhl:
            lines += [
                "\n--- Previous Day ---",
                f"  PDH: {pdhl['pdh']:.5f}  |  PDL: {pdhl['pdl']:.5f}  |  PDC: {pdhl['pdc']:.5f}",
            ]
        if adr:
            _adr_warn = ""
            if adr["used_pct"] >= 80:
                _adr_warn = f"  ⚠ TODAY'S RANGE IS {adr['used_pct']:.0f}% OF ADR — limited room left. TP targets beyond this distance are unlikely to hit today."
            elif adr["used_pct"] >= 60:
                _adr_warn = f"  Note: {adr['used_pct']:.0f}% of ADR already used today."
            lines += [
                "\n--- Average Daily Range (14-day) ---",
                f"  ADR     : {adr['adr_abs']:.5f}  ({adr['adr_pct']:.2f}% of price) — typical daily range",
                f"  Today   : {adr['today_range']:.5f}  ({adr['used_pct']:.0f}% of ADR used)",
            ]
            if _adr_warn:
                lines.append(_adr_warn)

        _atr_primary     = float(primary_df["ATR"].iloc[-1]) if "ATR" in primary_df.columns else atr_1h
        _atr_primary_str = (f"  ATR {primary_tf.upper()}: {_atr_primary:.5f}  ← USE THIS for SL sizing on {primary_tf.upper()} entries\n"
                            f"  ATR 1H : {atr_1h:.5f}  (reference only — use primary TF ATR for SL)")
        if primary_tf == "1h":
            _atr_primary_str = f"  ATR 1H: {atr_1h:.5f}  (avg candle range — SL needs at least 1 ATR breathing room beyond structure)"

        lines += [
            "\n--- Price Snapshot ---",
            f"  Price : {current_price:.5f}",
            f"  VWAP  : {vwap:.5f}",
            f"  EMA20 : {ema20:.5f}",
            f"  RSI {primary_tf.upper()}: {rsi_1h:.1f}" + (f"  |  RSI 4H: {rsi_4h:.1f}" if rsi_4h else ""),
            _atr_primary_str,
            f"  Volume: {vol_trend} (last 10 candles on {primary_tf.upper()})",
            f"  BB (20,2) 1H: upper={bb_upper:.5f}  mid={bb_mid:.5f}  lower={bb_lower:.5f}  width={bb_width}%",
            f"  BB position : {bb_pos}",
            f"  BB squeeze  : {bb_squeeze}",
        ]

        _macro_block, _macro_verdict = _macro_trend_context(df_4h)
        if _macro_block:
            lines.append(_macro_block)

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

                lines.append("  ASKS price|dist%|qty|tag (top 5 by size):")
                for p, q, label in sorted(agg["ask_clusters"], key=lambda c: -c[1])[:5]:
                    dist = (p - agg["mid_price"]) / agg["mid_price"] * 100
                    lines.append(f"  {p:.5f}|+{dist:.2f}%|{q:.3f}{label}")
                if agg["ask_air_pockets"]:
                    lines.append("  ASK AIR POCKETS (thin — price moves fast here):")
                    for p, dist in agg["ask_air_pockets"]:
                        lines.append(f"  {p:.5f}|{dist:.2f}%")

                lines.append("  BIDS price|dist%|qty|tag (top 5 by size):")
                for p, q, label in sorted(agg["bid_clusters"], key=lambda c: -c[1])[:5]:
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

            _dv_brief = derivs.get_derivs(symbol)
            if _dv_brief:
                fr = _dv_brief["funding_rate"] * 100
                oi = _dv_brief["open_interest"]
                fr_label = ("crowded longs — squeeze risk for buyers" if fr > 0.05
                            else "crowded shorts — squeeze risk for sellers" if fr < -0.05
                            else "neutral")
                _oi6  = _dv_brief.get("oi_chg_6h_pct")
                _oi24 = _dv_brief.get("oi_chg_24h_pct")
                _oi6_s  = f"{_oi6:+.2f}%" if _oi6 is not None else "n/a"
                _oi24_s = f"{_oi24:+.2f}%" if _oi24 is not None else "n/a"
                lines += [
                    "\n--- Futures (Binance perpetual) ---",
                    f"  Open Interest : {oi:,.2f} contracts  (6h: {_oi6_s} | 24h: {_oi24_s})",
                    f"  Funding Rate  : {fr:+.4f}%  ({fr_label})",
                    # Phase 2b: "how to read OI+price" teaching text deleted -- moved to
                    # the lean prompt (stated once, not per escalation).
                ]

        # ── Context summaries for non-primary TFs (no raw candles) ──────────
        lines.append(f"\n\n=== HIGHER / LOWER TIMEFRAME CONTEXT ===")

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
            df = tf_data.get(_ftf)
            if _lim == 0:
                # Phase 2b: no raw dump for this TF -- PDH/PDL, macro trend context, and
                # the per-TF summary loop already carry its story. One fallback line only.
                # (df.iloc[-0:] would be the WHOLE frame, not empty -- -0 == 0 in Python --
                # so this TF is handled entirely separately from the slicing below.)
                if df is not None and len(df) >= 5:
                    _last5_str = ", ".join(f"{c:.5f}" for c in df["close"].iloc[-5:].tolist())
                    lines.append(f"\n{_ftf.upper()} last 5 closes: {_last5_str}")
                continue
            lines.append(f"\n\n=== {_role}: {_ftf.upper()} — FULL CANDLE DATA (last {_lim} candles) ===")
            if _i == 0:
                lines.append("This is your primary chart. All indicators pre-computed per candle.")
            else:
                lines.append("Higher timeframe context — compile your own structure, swings, and key levels from this data.")
            lines.append(f"Trade type for this TF: {TF_TRADE_TYPE.get(_ftf, 'day')}")
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

                # MACD summary on primary TF
                if has_mh:
                    _mh_tail = df_out["MACD_hist"].dropna().tail(5)
                    if len(_mh_tail) >= 2:
                        _mh_now  = float(_mh_tail.iloc[-1])
                        _mh_prev = float(_mh_tail.iloc[-2])
                        _mh_dir  = "rising" if _mh_now > _mh_prev else "falling"
                        _mh_bias = "bullish" if _mh_now > 0 else "bearish"
                        _cross_note = ""
                        if _mh_prev < 0 < _mh_now:
                            _cross_note = " — BULLISH ZERO-LINE CROSS (momentum flipped positive)"
                        elif _mh_prev > 0 > _mh_now:
                            _cross_note = " — BEARISH ZERO-LINE CROSS (momentum flipped negative)"
                        lines.append(
                            f"MACD summary: histogram={_mh_now:+.5g} ({_mh_bias}, {_mh_dir}){_cross_note}"
                        )

        # ── BOS / CHoCH structural label (primary TF) ─────────────────────
        _bos = _bos_choch_label(primary_df)
        if _bos:
            lines.append(f"\nMARKET STRUCTURE ({primary_tf.upper()}) — most recent structural event:")
            lines.append(f"  Event   : {_bos['event']} — {_bos['detail']}")
            if _bos.get("hold"):
                lines.append(f"  Holds   : structure valid while price stays {_bos['hold']}")
            lines.append(f"  Warning : {_bos['warning']}")

        # ── Zone dwell analysis (tells Chev how established the level is) ──
        if confluence_zone is not None:
            _atr_dwell = float(primary_df["ATR"].iloc[-1]) if "ATR" in primary_df.columns else None
            _dwell = _zone_dwell_analysis(primary_df, confluence_zone, _atr_dwell)
            if _dwell:
                lines.append(f"\nZONE ANALYSIS (confluence at {confluence_zone:.5g}):")
                lines.append(f"  Prior tests  : {_dwell['quality']}")
                lines.append(f"  Current candle: {_dwell['candle_note']}")

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

    win_idx = window.index

    def _ts(pos):
        try:
            return int(win_idx[pos].timestamp())
        except Exception:
            return pos

    for i in range(5, len(window) - 1):
        # Find previous significant low / high in first half of window
        prev_half_low_idx  = prices_low[:i].argmin()
        prev_half_high_idx = prices_high[:i].argmax()

        # Hidden bullish: current low > prev low, but RSI now < prev RSI
        if prices_low[i] > prices_low[prev_half_low_idx] and rsi[i] < rsi[prev_half_low_idx]:
            results.append({
                "type": "hidden_bullish_divergence", "bias": "bull",
                "price": float(closes[i]),
                "note": f"Price HL at {closes[i]:.5f} but RSI lower — uptrend continuation signal",
                "ts_t1": _ts(prev_half_low_idx), "price_t1": round(float(prices_low[prev_half_low_idx]), 5),
                "rsi_t1": round(float(rsi[prev_half_low_idx]), 2),
                "ts_t2": _ts(i), "price_t2": round(float(prices_low[i]), 5),
                "rsi_t2": round(float(rsi[i]), 2),
            })
            break

        # Hidden bearish: current high < prev high, but RSI now > prev RSI
        if prices_high[i] < prices_high[prev_half_high_idx] and rsi[i] > rsi[prev_half_high_idx]:
            results.append({
                "type": "hidden_bearish_divergence", "bias": "bear",
                "price": float(closes[i]),
                "note": f"Price LH at {closes[i]:.5f} but RSI higher — downtrend continuation signal",
                "ts_t1": _ts(prev_half_high_idx), "price_t1": round(float(prices_high[prev_half_high_idx]), 5),
                "rsi_t1": round(float(rsi[prev_half_high_idx]), 2),
                "ts_t2": _ts(i), "price_t2": round(float(prices_high[i]), 5),
                "rsi_t2": round(float(rsi[i]), 2),
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
        # Only the nearer side scores when both R and S are in range — price sandwiched
        # mid-range is an argument against trading, not two arguments for it.
        sr_score = 0
        sr_reasons = []
        _sr_candidates = []
        for zone, label in [(top_r, "Resistance"), (top_s, "Support")]:
            if zone and abs(zone["price"] - current_price) / current_price * 100 <= 1.5:
                _dist_pct = abs(zone["price"] - current_price) / current_price * 100
                _sr_candidates.append((_dist_pct, zone, label))
        if _sr_candidates:
            _, zone, label = min(_sr_candidates, key=lambda c: c[0])
            instances = zone.get("instances", 1)
            pts = 3 if instances >= 3 else 2
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

        # ── Golden Pocket approaching signal ──────────────────────────────────
        # Fires when price is outside the GP zone but closing in on it.
        # Weight: 0pt — this is a preparation signal only, never an entry trigger.
        # Shown to Chev as [WATCH] so he can plan an entry before price arrives.
        # Approach direction tells him which bias to expect (long vs short setup).
        gp_approach_reasons = []
        if not in_golden_pocket and fib50 and fib618:
            _gp_lo = min(fib50, fib618)
            _gp_hi = max(fib50, fib618)
            if current_price > _gp_hi:
                _gp_dist      = current_price - _gp_hi
                _approach_dir = "approaching from above → expect long entry inside zone"
            elif current_price < _gp_lo:
                _gp_dist      = _gp_lo - current_price
                _approach_dir = "approaching from below → expect short entry inside zone"
            else:
                _gp_dist = 0
                _approach_dir = ""
            if _gp_dist > 0:
                try:
                    _gp_atr = float(primary_df["ATR"].iloc[-1]) if "ATR" in primary_df.columns else None
                    if _gp_atr and _gp_atr > 0:
                        _gp_atr_dist = _gp_dist / _gp_atr
                        _gp_pct      = _gp_dist / current_price * 100
                        if _gp_atr_dist <= 1.5:
                            gp_approach_reasons.append(
                                f"GP zone [{_gp_lo:.5f}–{_gp_hi:.5f}] "
                                f"{_gp_pct:.2f}% away ({_gp_atr_dist:.1f}× ATR) — {_approach_dir}"
                            )
                except Exception:
                    pass

        # ── RSI divergence (regular + hidden) ────────────────────────────────
        reg_divs    = _ca_detect_rsi_divergence(primary_df)
        hidden_divs = _detect_hidden_divergence(primary_df)
        all_divs    = reg_divs + hidden_divs
        for d in all_divs:
            d["tf"] = primary_tf
        div_score   = 0
        div_reasons = []
        _div_candidates = []
        for d in all_divs[:2]:
            d_price = d.get("price", d.get("price_t2", current_price))
            if abs(d_price - current_price) / current_price * 100 <= 2.0:
                pts = _div_strength_score(d, primary_tf, confirmed=True)
                if pts >= 0.3:
                    _div_candidates.append((pts, d))
        if _div_candidates:
            _div_candidates.sort(key=lambda c: c[0], reverse=True)
            _dom_pts, _dom_d = _div_candidates[0]
            div_score += _dom_pts
            div_reasons.append(f"{_dom_d['type']} (str={_dom_pts}pt)")
            _dom_bias = _dom_d.get("bias")
            for pts, d in _div_candidates[1:]:
                if d.get("bias") == _dom_bias:
                    div_score += pts
                    div_reasons.append(f"{d['type']} (str={pts}pt)")

        # ── Forming divergence (live — price new extreme + RSI diverging) ──
        _div_lb = {'15m': 10, '30m': 12, '1h': 16, '4h': 24}.get(primary_tf, 10)
        forming_divs  = _detect_forming_divergence(primary_df, lb=_div_lb)
        form_score    = 0
        form_reasons  = []
        if forming_divs:
            best = forming_divs[0]
            form_score = _div_strength_score(best, primary_tf, confirmed=False)
            form_reasons.append(
                f"FORMING {best['type']} (RSI gap {best['rsi_gap']}pt, {best['age_bars']} bars, str={form_score}pt)"
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
                # RSI 50-cross is context for Chev, not a scored confluence — too noisy alone
                rsi_level_reasons.append(
                    f"RSI crossed 50 {cross['direction']} {cross['bars_ago']} bar(s) ago"
                )

        # ── EMA 13/21/55 proximity and crossover ─────────────────────────────
        ema_score   = 0
        ema_reasons = []
        last = primary_df.iloc[-1]
        for ema_col, ema_weight, ema_label in [("EMA55", 2.0, "EMA55"), ("EMA21", 1.0, "EMA21"), ("EMA13", 0.5, "EMA13")]:
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

        # Cap total EMA contribution — prevents stacking all three EMAs + crossover from
        # dominating the score when price simply happens to be near a cluster of MAs.
        if ema_score > 3.0:
            ema_score = 3.0

        # ── Liquidity sweep ───────────────────────────────────────────────────
        sweeps     = _detect_liquidity_sweep(primary_df)
        sweep_score   = 0
        sweep_reasons = []
        for s in sweeps[:1]:
            sweep_score += 3
            sweep_reasons.append(f"Sweep:{s['type']} (3pt)")

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
                bb_score += 0.5
                bb_reasons.append(f"BB near upper (%B={_pct_b:.2f}, top 15% of band, 0.5pt)")
            elif _pct_b <= 0.15:
                bb_score += 0.5
                bb_reasons.append(f"BB near lower (%B={_pct_b:.2f}, bottom 15% of band, 0.5pt)")

            if 0.48 <= _pct_b <= 0.52:
                bb_score += 1.0
                _side = "support" if current_price >= _bbm else "resistance"
                bb_reasons.append(f"BB mid {_side} (%B={_pct_b:.2f}, 1pt)")

            if _bbw < 1.5:
                # Context only, like RSI 50-cross — the playbooks all say do NOT enter
                # during a squeeze, so it must not argue for entry via the score.
                bb_reasons.append(f"BB squeeze (width {_bbw:.2f}%, context, 0pt)")
        except Exception:
            pass

        # ── Volume Profile proximity (anchor-based: 4H + 1H only) ───────────────
        # Range starts at the structural first-anchor of each TF (same as Arsenal).
        # 4H VP = big-picture value area. 1H VP = tactical entry context.
        # Lower TFs excluded — their VP windows are too short to carry structural weight.
        vp_score   = 0
        vp_reasons = []
        try:
            # ATR-calibrated proximity — 25% of ATR as % of price so BTC/ADA/EUR/USD all
            # trigger at a structurally meaningful distance rather than a fixed percentage.
            # Floor 0.10% (tight forex pairs), ceiling 0.80% (highly volatile alts).
            _vp_atr_raw = float(primary_df["ATR"].iloc[-1]) if "ATR" in primary_df.columns else None
            _vp_prox = (
                max(0.10, min(0.80, 0.25 * (_vp_atr_raw / current_price * 100)))
                if _vp_atr_raw and current_price > 0 else 0.40
            )
            for _vp_tf, _vp_base in [("4h", 3), ("1h", 2)]:
                _dfv = tf_data.get(_vp_tf)
                if _dfv is None or len(_dfv) < 30:
                    continue
                _anc = _detect_vp_anchor(_dfv)
                if not _anc:
                    continue
                if not _anc.get("active", True):
                    continue
                _anc_confirmed = _anc.get("confirmed", True)
                _vp = _ca_volume_profile(_dfv, _anc["idx"], len(_dfv) - 1)
                if not _vp:
                    continue
                for _lbl, _px, _pts in [("POC", _vp["poc"], _vp_base),
                                         ("VAH", _vp["vah"], _vp_base - 1),
                                         ("VAL", _vp["val"], _vp_base - 1)]:
                    _dist = abs(current_price - _px) / current_price * 100
                    if _dist <= _vp_prox:
                        _pts_awarded = round(_pts / 2, 1) if not _anc_confirmed else _pts
                        _unconf_note = "unconfirmed-anchor, " if not _anc_confirmed else ""
                        vp_score += _pts_awarded
                        vp_reasons.append(f"VP {_lbl} {_vp_tf} ({_dist:.2f}% away ≤{_vp_prox:.2f}% prox, {_unconf_note}{_pts_awarded}pt)")
        except Exception:
            pass

        # ── Distance from level ───────────────────────────────────────────────
        # Find the dominant level being tested
        all_candidates = []
        if top_r: all_candidates.append(top_r["price"])
        if top_s: all_candidates.append(top_s["price"])
        for _, p in fib_levels.items(): all_candidates.append(p)

        # ── Fast intraday structural anchors: prior day H/L + opening range ────
        # Multi-touch SR/VP needs repeated historical touches to validate -- on thin
        # or quiet sessions almost nothing clears sr_score/vp_score even though real
        # intraday reference points still exist. These two need only yesterday's
        # candle or today's session open, not repeated touches. They fold into the
        # same candidate list as SR/Fib above (so dist_from_level/nearest_level
        # already account for them correctly, no separate exemption needed) and set
        # fast_anchor_pass, which only unlocks the struct pre-gate/STRUCT_REJECT --
        # they never add points to sr_score/vp_score/total_score.
        fast_anchor_reasons = []
        fast_anchor_pass    = False
        try:
            _pdhl = _get_previous_day_hl(symbol, asset_type)
        except Exception:
            _pdhl = None
        try:
            _sess_ctx      = _get_session_context()
            _session_start = _sess_ctx.get("session_start_ts")
            # First ~1 hour after session open on whatever TF is being scanned --
            # 4h has no sub-hour granularity available so it degrades to one candle.
            _or_candles_by_tf = {"5m": 12, "15m": 4, "30m": 2, "1h": 1, "4h": 1}
            _or_range = _get_opening_range(primary_df, _session_start,
                                           num_candles=_or_candles_by_tf.get(primary_tf, 4))
        except Exception:
            _or_range = None

        _fast_anchor_candidates = [
            ("PDH proximity",       _pdhl.get("pdh")    if _pdhl     else None),
            ("PDL proximity",       _pdhl.get("pdl")    if _pdhl     else None),
            ("Opening range high",  _or_range.get("or_high") if _or_range else None),
            ("Opening range low",   _or_range.get("or_low")  if _or_range else None),
        ]
        for _fa_label, _fa_price in _fast_anchor_candidates:
            if _fa_price is None or _fa_price <= 0:
                continue
            _fa_dist = abs(current_price - _fa_price) / _fa_price * 100
            if _fa_dist <= 1.5:
                all_candidates.append(_fa_price)
                fast_anchor_pass = True
                fast_anchor_reasons.append(f"{_fa_label} ({_fa_dist:.2f}% away, 0pt, context)")

        nearest_level = min(all_candidates, key=lambda p: abs(p - current_price)) if all_candidates else None
        dist_from_level = abs(current_price - nearest_level) / current_price * 100 if nearest_level else None
        at_level = dist_from_level is not None and dist_from_level <= 0.3

        # ── Aggregate score ───────────────────────────────────────────────────
        # Forming divergence is excluded from total_score intentionally.
        # It is a preparation signal (RSI is diverging but the second price pivot has not
        # closed yet).  Letting it count toward the entry threshold would cause Chev to
        # fire on unconfirmed setups.  It remains visible in all_reasons so Chev knows
        # it is developing — he just cannot use it as the primary entry trigger.
        # ── Derivatives context (crypto perps only): funding + OI trend ──────
        deriv_score   = 0.0
        deriv_reasons = []
        if asset_type == "crypto":
            try:
                _price_chg_6h = None
                _df1h_d = tf_data.get("1h")
                if _df1h_d is not None and len(_df1h_d) >= 7:
                    _p_then = float(_df1h_d["close"].iloc[-7])
                    if _p_then > 0:
                        _price_chg_6h = (current_price - _p_then) / _p_then * 100
                _dv = derivs.get_derivs(symbol)
                if _dv:
                    deriv_reasons, deriv_score = derivs.classify_derivs(
                        _dv["funding_rate"], _dv.get("oi_chg_6h_pct"), _price_chg_6h)
            except Exception:
                deriv_score, deriv_reasons = 0.0, []

        total_score = sr_score + fib_score + div_score + ema_score + sweep_score + pattern_score + rsi_level_score + bb_score + vp_score + deriv_score

        # GP × SR deadly combo bonus — multiplicative, not additive.
        # When price is at a confirmed multi-TF SR zone AND inside the golden pocket,
        # the combination is structurally more powerful than the sum of its parts:
        # big money defends a proven level AND the fib math says it should reverse here.
        # The bonus grows with the rest of the setup quality rather than being a flat add.
        _gp_sr_combo = in_golden_pocket and sr_score >= 3
        if _gp_sr_combo:
            total_score = round(total_score * 1.15, 2)

        # TF quality multiplier — higher TF signals require fewer confluences because
        # each one is structurally more significant.  Lower TF signals need more confluences
        # to overcome the discount.  Same thresholds apply; the multiplier shifts effective difficulty.
        _tf_mult = {"4h": 1.2, "1h": 1.0, "30m": 0.9, "15m": 0.9}.get(primary_tf, 1.0)
        total_score = round(total_score * _tf_mult, 2)
        # Prefix forming reasons so Chev can see them but knows their status
        _form_reasons_labelled  = [f"[WATCH — not yet confirmed] {r}" for r in form_reasons]
        _gp_approach_labelled   = [f"[WATCH — approaching GP] {r}" for r in gp_approach_reasons]
        _combo_reasons          = ["★★ GP×SR DEADLY COMBO (×1.15 quality bonus applied)"] if _gp_sr_combo else []
        all_reasons = (_combo_reasons
                       + sr_reasons + fib_reasons + div_reasons + ema_reasons
                       + sweep_reasons + pattern_reasons + rsi_level_reasons
                       + bb_reasons + vp_reasons + deriv_reasons
                       + _form_reasons_labelled + _gp_approach_labelled
                       + fast_anchor_reasons)

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
            "atr":             float(primary_df["ATR"].iloc[-1]) if "ATR" in primary_df.columns else None,
            "sr_score":        sr_score,
            "vp_score":        vp_score,
            "fast_anchor_pass": fast_anchor_pass,
            "tf_mult":         _tf_mult,
            "approaching_gp":  len(gp_approach_reasons) > 0,
            "gp_zone":         (min(fib50, fib618), max(fib50, fib618)) if fib50 and fib618 else None,
            "gp_sr_combo":     _gp_sr_combo,
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


_CRYPTO_CORR_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "XLMUSDT",
    "BNBUSDT", "DOGEUSDT", "LINKUSDT", "SUIUSDT", "AVAXUSDT", "NEARUSDT",
    "DOTUSDT", "AAVEUSDT", "PEPEUSDT", "UNIUSDT", "TRXUSDT",
}

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
    min_score = (CONFLUENCE_THRESHOLD_CRYPTO if asset_type == "crypto"
                 else CONFLUENCE_THRESHOLD_FOREX if asset_type == "forex"
                 else CONFLUENCE_THRESHOLD_STOCK)
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


def _record_loss_for_cooldown(trade):
    """Store a loss fingerprint so the confluence re-entry check can warn on repeat setups."""
    global _recent_losses
    try:
        tags = set(t.strip().lower() for t in (trade.get("tags") or "").split(",") if t.strip())
        _recent_losses.append({
            "symbol":    trade["symbol"],
            "direction": trade.get("direction", ""),
            "entry":     float(trade.get("entry", 0)),
            "tags":      tags,
            "atr":       float(trade.get("atr_at_entry") or trade.get("atr") or 0),
            "closed_at": time.time(),
        })
        _recent_losses[:] = _recent_losses[-30:]  # keep last 30 losses
    except Exception:
        pass


def _check_confluence_pattern(symbol, direction, proposed_entry, proposed_tags_str, current_atr):
    """
    Option A+C combined re-entry check.
    C — same price level (within 0.5 ATR of a recent losing entry).
    A — same reasoning (≥2 overlapping tags with the losing trade).
    Both together = strong PATTERN ALERT. Either alone = softer note.
    Returns a list of warning strings (empty = clean).
    """
    warnings = []
    cutoff = time.time() - 2 * 3600  # 2-hour look-back window
    proposed_tags = set(t.strip().lower() for t in proposed_tags_str.split(",") if t.strip())

    for loss in _recent_losses:
        if loss["symbol"] != symbol or loss["direction"] != direction:
            continue
        if loss["closed_at"] < cutoff:
            continue

        atr_ref    = current_atr or loss["atr"] or abs(proposed_entry * 0.005)
        same_level = abs(proposed_entry - loss["entry"]) <= 0.5 * atr_ref
        shared     = loss["tags"] & proposed_tags
        same_logic = len(shared) >= 2
        ago        = int((time.time() - loss["closed_at"]) / 60)

        if same_level and same_logic:
            warnings.append(
                f"⚠ PATTERN ALERT — {ago}m ago this exact setup FAILED on {symbol} {direction.upper()}.\n"
                f"  Failed entry  : {loss['entry']:.5g}  (current proposal within 0.5 ATR)\n"
                f"  Shared tags   : {', '.join(sorted(shared))} — same reasoning as the losing trade\n"
                f"  Action        : require at least ONE additional confluence not present in the losing trade\n"
                f"                  before entering. Same level + same logic = high repeat-mistake risk."
            )
        elif same_level:
            warnings.append(
                f"ℹ LEVEL NOTE — {ago}m ago a loss occurred near this price ({loss['entry']:.5g}) on {symbol} {direction.upper()}.\n"
                f"  Different confluences this time — approach with awareness. Level has rejected once recently."
            )
        elif same_logic:
            warnings.append(
                f"ℹ LOGIC NOTE — {ago}m ago a {symbol} {direction.upper()} trade with similar confluences "
                f"({', '.join(sorted(shared))}) closed at a loss at a different price level.\n"
                f"  This may be a genuinely different setup — use your judgement."
            )

    return warnings


def _build_executable_geometry_block(asset_type, atr, price, exploration_mode):
    """Pure string builder — the survivable SL/R:R/TP numbers the Risk Gauntlet will
    actually enforce, computed from risk_gauntlet's own constants (never duplicated here,
    always imported) so Chev sees the real limits before picking a stop, not after being
    rejected for one. Added 2026-07-06 (Phase 3) — Chev was being told "1x ATR" by an
    earlier paragraph while the cost gate silently demanded more; this makes both numbers
    explicit and tells him which one actually wins.
    """
    a = asset_type if asset_type in risk_gauntlet.FEE_SIDE else "crypto"
    prof = risk_gauntlet.get_active_profile(exploration_mode)
    atr_pct = (atr / price) if (atr and price) else None

    lines = ["EXECUTABLE GEOMETRY — hard limits the Risk Gauntlet will enforce on any POST:"]
    for trade_type in ("scalp", "day", "swing"):
        cost_rt = 2 * (risk_gauntlet.FEE_SIDE[a] + risk_gauntlet.SLIPPAGE_SIDE[a])
        if a == "crypto" and trade_type == "swing":
            cost_rt += risk_gauntlet.FUNDING_EST_SWING
        min_stop_cost = cost_rt / prof["MAX_COST_R"][trade_type]
        min_stop_atr  = (prof["ATR_FLOOR"][trade_type] * atr_pct) if atr_pct is not None else 0.0
        if min_stop_atr > min_stop_cost:
            eff_min_stop, floor_label = min_stop_atr, "ATR floor"
        else:
            eff_min_stop, floor_label = min_stop_cost, "cost floor"
        required_rr = prof["MIN_NET_RR"][trade_type]
        tp_min_pct  = required_rr * (eff_min_stop + cost_rt) + cost_rt
        lines.append(
            f"  {trade_type}: min SL {eff_min_stop:.2%} ({floor_label}) | "
            f"min R:R {required_rr:.2f}:1 (floor) | min TP {tp_min_pct:.2%} from entry"
        )
    lines.append(
        "If no real structural level exists at or beyond the minimum SL distance for any "
        "trade_type, that is a VALID SKIP — write exactly that, citing these numbers.\n"
    )
    return "\n".join(lines) + "\n"


def ask_chev_to_judge(result, balance, dashboard_ws=None, timeout=360, force_take=False):
    global _chev_online
    symbol = result["symbol"]
    confluence_zone = result['price']

    # Build playbook + journal context to prepend to the prompt
    asset_type     = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")
    min_score, _   = _active_thresholds(asset_type)
    _exploration_note = ""
    if EXPLORATION_MODE:
        _exploration_note = (
            "EXPLORATION MODE — DATA-COLLECTION PHASE (paper account):\n"
            "  This is a deliberate data-collection phase. Losses are tuition here — silence teaches nothing.\n"
            "  If this setup has a nameable entry, a structural stop, and a target, your DEFAULT is POST at the\n"
            "  suggested grade-based risk above. SKIP is still allowed, but must name ONE concrete broken element:\n"
            "  no invalidation level available, direction fights the 4H trend without meeting the counter-trend\n"
            "  requirements, or price nowhere near the zone. Never SKIP on general caution, 'not enough confluence',\n"
            "  a letter grade alone, or maturity/participation figures alone — those are sizing inputs in this phase,\n"
            "  not vetoes.\n\n"
        )
    session_label, session_quality = _session_grade(asset_type)
    grade, suggested_risk          = _setup_grade(result, asset_type)
    heat_context                   = _portfolio_heat_context(asset_type, symbol)
    playbook_text = _load_playbook(asset_type)
    journal = _load_journal()
    same_type = [e for e in journal if e.get("asset_type") == asset_type][-3:]
    journal_lines = "\n".join([
        f"• {e['symbol']} {e['direction'].upper()} → {e['outcome']} (${e['pnl']:+.2f}) | tags: {e.get('tags','none')}"
        for e in same_type
    ])
    # Playbook + journal are placed later, inside msg (Phase 2 reorder: sections f/g).
    playbook_block = f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else ""
    journal_block  = f"=== RELEVANT RECENT TRADES (context only) ===\n{journal_lines}\n\n" if journal_lines else ""
    context_prefix = ""

    # Show open/pending positions so Chev can self-assess correlation before taking new trade
    _open_now   = [t for t in open_trades if t.get("status") == "OPEN"]
    _pending_now = [t for t in open_trades if t.get("status") == "PENDING"]
    if _open_now or _pending_now:
        context_prefix += "=== YOUR CURRENT OPEN POSITIONS ===\n"
        for _t in _open_now:
            _rr = ""
            try:
                _entry = float(_t.get("entry", 0))
                _sl    = float(_t.get("sl", 0))
                _tp    = float(_t.get("tp", 0))
                if _entry and _sl and _tp and abs(_entry - _sl) > 0:
                    _rr = f"  R:R={abs(_tp - _entry)/abs(_entry - _sl):.1f}"
            except Exception:
                pass
            context_prefix += (
                f"  OPEN  {_t.get('symbol','?')} {_t.get('direction','?').upper()} "
                f"entry={_t.get('entry','?')} SL={_t.get('sl','?')} TP={_t.get('tp','?')}{_rr}\n"
            )
        for _t in _pending_now:
            context_prefix += (
                f"  PENDING {_t.get('symbol','?')} {_t.get('direction','?').upper()} "
                f"limit={_t.get('entry','?')} SL={_t.get('sl','?')} TP={_t.get('tp','?')}\n"
            )
        context_prefix += (
            "Before posting a new trade, consider: does this add correlated exposure to an existing position?\n"
            "If you already have a LONG on EUR/USD, adding a LONG on GBP/USD doubles your USD-short risk.\n\n"
        )
    else:
        context_prefix += "=== YOUR CURRENT OPEN POSITIONS: none ===\n\n"

    # Confluence re-entry pattern check (A+C) — warn if same level + same logic failed recently
    _pat_warnings = _check_confluence_pattern(
        symbol, result.get("direction", ""), confluence_zone or 0,
        result.get("tags", ""), result.get("atr") or 0
    )
    if _pat_warnings:
        context_prefix += "=== RECENT LOSS PATTERN WARNING ===\n"
        for _w in _pat_warnings:
            context_prefix += _w + "\n"
        context_prefix += "\n"

    # Full market data brief — primary TF gets full candles, others get rich summaries
    primary_tf = result.get("primary_tf", "1h")
    trade_type = result.get("trade_type", TF_TRADE_TYPE.get(primary_tf, "day"))
    market_brief = _build_rich_market_brief(symbol, asset_type, primary_tf, confluence_zone=confluence_zone)
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
    _near_sup = result.get("support") and abs(confluence_zone - result["support"]["price"]) / confluence_zone * 100 <= 1.5
    _near_res = result.get("resistance") and abs(confluence_zone - result["resistance"]["price"]) / confluence_zone * 100 <= 1.5
    if _near_sup:
        _sup_p = result["support"]["price"]
        direction_hint = (
            f"Dexter: confluence zone ({confluence_zone:.5f}) is at or within 1.5% of a SUPPORT level ({_sup_p:.5f}). "
            f"This is a potential LONG area — but confirm with RSI, divergence, and trend context. "
            f"It could also be a short setup if price is rejecting downward from a resistance-turned-support flip. "
            f"Your job: read the full structure and decide — do NOT assume direction from the level alone."
        )
    elif _near_res:
        _res_p = result["resistance"]["price"]
        direction_hint = (
            f"Dexter: confluence zone ({confluence_zone:.5f}) is at or within 1.5% of a RESISTANCE level ({_res_p:.5f}). "
            f"This is a potential SHORT area — but confirm with RSI, divergence, and trend context. "
            f"It could also be a long setup if this is a breakout above resistance with momentum. "
            f"Your job: read the full structure and decide — do NOT assume direction from the level alone."
        )

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

    # Counter-trend warning — fires when S/R proximity suggests a direction that opposes the 4H regime
    counter_trend_warning = ""
    _r4_regime = r4h.get("regime", "")
    if _r4_regime in ("TRENDING_UP", "TRENDING_DOWN"):
        _bias_long  = _near_sup   # near support → potential long
        _bias_short = _near_res   # near resistance → potential short
        _is_counter = (_bias_long and _r4_regime == "TRENDING_DOWN") or \
                      (_bias_short and _r4_regime == "TRENDING_UP")
        if _is_counter:
            _bias_dir = "LONG" if _bias_long else "SHORT"
            if _r4_regime == "TRENDING_UP":
                _ct_structure = (
                    "You are proposing a SHORT near a RESISTANCE level in a confirmed UPTREND.\n"
                    "KEY FACT: In an uptrend, resistance levels are CONTINUATION ZONES — price is expected to\n"
                    "break them, not reverse from them. The 4H has been making higher highs and higher lows.\n"
                    "Every resistance that held in a downtrend becomes a launchpad in an uptrend.\n"
                    "Historical data from this session: shorts at resistance in uptrends fail ~85% of the time.\n"
                    "The market doesn't care that the level 'looks like resistance' — it will walk straight through it."
                )
            else:
                _ct_structure = (
                    "You are proposing a LONG near a SUPPORT level in a confirmed DOWNTREND.\n"
                    "KEY FACT: In a downtrend, support levels are CONTINUATION ZONES — price is expected to\n"
                    "break them, not bounce from them. The 4H has been making lower highs and lower lows.\n"
                    "Every support that held in an uptrend becomes a ceiling in a downtrend.\n"
                    "Historical data from this session: longs at support in downtrends fail ~85% of the time.\n"
                    "The market doesn't care that the level 'looks like support' — it will walk straight through it."
                )
            _ct_req_score = 1.5 * (CONFLUENCE_THRESHOLD_CRYPTO if asset_type == "crypto"
                                    else CONFLUENCE_THRESHOLD_FOREX if asset_type == "forex"
                                    else CONFLUENCE_THRESHOLD_STOCK)
            counter_trend_warning = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️  COUNTER-TREND ALERT — READ BEFORE POSTING\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{_ct_structure}\n\n"
                f"See the '4H Macro Trend Context' section in the market brief above for the exact HH/HL count and % move.\n\n"
                f"To TAKE this trade you MUST satisfy ALL of the following — no exceptions:\n"
                f"  1. Score ≥ {_ct_req_score:.0f} (A+ grade)\n"
                f"  2. Confirmed RSI divergence on 1H or 4H (not 'forming' — must be confirmed with two pivots)\n"
                f"  3. A clear 4H BOS (Break of Structure) or CHoCH (Change of Character) proving trend reversal\n"
                f"  4. SL placed behind a 4H structural level — not a pip beyond a local candle wick\n"
                f"  5. SCALP only — max 1× risk, no swing hold\n"
                f"If you cannot clearly articulate points 2–3 from the candle data above → SKIP.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
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
        # Zone alignment check: Dexter's SR/Fib confluence zone and the Opportunity
        # Engine's entry_zone are two independently-computed prices -- state how close
        # they actually are (same proximity buckets as dist_from_level elsewhere) so
        # Chev reads a stated fact instead of re-judging "do these line up" himself.
        if pattern_block and _survey.opportunity is not None:
            try:
                _opp = _survey.opportunity
                _zlo, _zhi = _opp.entry_zone
                if _zlo <= confluence_zone <= _zhi:
                    _zone_dist_pct = 0.0
                else:
                    _zone_dist_pct = (min(abs(confluence_zone - _zlo), abs(confluence_zone - _zhi))
                                       / max(abs(confluence_zone), 1e-10) * 100.0)
                _zone_lbl = ("SAME ZONE" if _zone_dist_pct <= 0.5 else
                             "NEARBY"    if _zone_dist_pct <= 2.0 else
                             f"DIFFERENT ({_zone_dist_pct:.2f}% apart)")
                pattern_block += (
                    f"ZONE ALIGNMENT CHECK: Dexter's confluence zone ({confluence_zone:.5f}) vs the "
                    f"Opportunity Engine's entry zone ({_zlo:.5f}-{_zhi:.5f}) — {_zone_lbl}. Trust "
                    f"this stated distance rather than re-eyeballing whether SR/Fib/BB 'line up'.\n\n"
                )
            except Exception as _za_err:
                print(f"[Dexter] Zone alignment check failed: {_za_err}")
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

    geometry_block = _build_executable_geometry_block(
        asset_type, result.get("atr"), result.get("current_price") or confluence_zone, EXPLORATION_MODE
    )

    # ── Invalidation candidates (Phase 3) ─────────────────────────────────────
    # Direction isn't decided yet at escalation time (Chev picks long/short in his
    # reply) -- reuse the same _near_sup/_near_res signal that already drives
    # direction_hint above, rather than inventing a new bias heuristic.
    _hyp_type = ""
    if result.get("in_golden_pocket"):
        _hyp_type = "golden_pocket"
    elif r4h.get("regime") == "RANGING":
        _hyp_type = "range_fade"
    elif (_survey and _survey.hypotheses and _survey.hypotheses[0].status == "CONFIRMED"
          and _survey.hypotheses[0].breakout_level) or any(p.get("breakout") for p in result.get("all_patterns", [])):
        _hyp_type = "breakout"
    elif _survey and _survey.hypotheses:
        _hyp_type = "pattern"

    _ic_auction        = _survey.auction if _survey else None
    _ic_fib_anchor_hi  = getattr(_ic_auction, "fib_anchor_high", None)
    _ic_fib_anchor_lo  = getattr(_ic_auction, "fib_anchor_low", None)
    _ic_vah            = getattr(_ic_auction, "vah", None)
    _ic_val            = getattr(_ic_auction, "val", None)

    _ic_breakout_level = None
    if _survey and _survey.hypotheses and _survey.hypotheses[0].breakout_level:
        _ic_breakout_level = _survey.hypotheses[0].breakout_level
    else:
        for _p in result.get("all_patterns", []):
            if _p.get("breakout") and _p.get("breakout_level"):
                _ic_breakout_level = _p["breakout_level"]
                break

    _ic_pattern_invalidation = None
    for _p in result.get("all_patterns", []):
        _ic_neckline = (_p.get("details") or {}).get("neckline")
        if _ic_neckline:
            _ic_pattern_invalidation = _ic_neckline
            break

    _ic_sr_levels = []
    if result.get("support"):
        _ic_sr_levels.append({"price": result["support"]["price"], "kind": "support"})
    if result.get("resistance"):
        _ic_sr_levels.append({"price": result["resistance"]["price"], "kind": "resistance"})

    _ic_atr           = result.get("atr")
    _ic_floor_profile = risk_gauntlet.get_active_profile(EXPLORATION_MODE)
    _ic_noise_floor    = (_ic_floor_profile["ATR_FLOOR"][trade_type] * _ic_atr) if _ic_atr else 0.0

    def _ic_candidates_for(_dir):
        _nearest_beyond = (result["support"]["price"]    if _dir == "long"  and result.get("support")
                           else result["resistance"]["price"] if _dir == "short" and result.get("resistance")
                           else None)
        return engines.compute_invalidation_candidates(
            direction=_dir,
            entry_price=confluence_zone,
            hypothesis_type=_hyp_type,
            noise_floor_distance=_ic_noise_floor,
            fib_anchor_high=_ic_fib_anchor_hi,
            fib_anchor_low=_ic_fib_anchor_lo,
            breakout_level=_ic_breakout_level,
            pattern_invalidation=_ic_pattern_invalidation,
            sr_levels=_ic_sr_levels,
            val=_ic_val,
            vah=_ic_vah,
            nearest_swing_beyond=_nearest_beyond,
        )

    if _near_sup and not _near_res:
        invalidation_block = engines.format_invalidation_candidates_for_chev(_ic_candidates_for("long"))
    elif _near_res and not _near_sup:
        invalidation_block = engines.format_invalidation_candidates_for_chev(_ic_candidates_for("short"))
    else:
        invalidation_block = (
            engines.format_invalidation_candidates_for_chev(_ic_candidates_for("long"), " (IF LONG)") +
            engines.format_invalidation_candidates_for_chev(_ic_candidates_for("short"), " (IF SHORT)")
        )

    # ── Validation candidates (reward-side mirror of the invalidation engine) ──
    # Direction isn't decided yet at escalation time (same as invalidation above) —
    # reuse _near_sup/_near_res so the two blocks always agree on long/short/both.
    _vc_opp       = _survey.opportunity if _survey else None
    _vc_target    = getattr(_vc_opp, "target_price", None)
    _vc_rr        = getattr(_vc_opp, "structural_rr", None)
    _vc_profile   = getattr(_vc_opp, "reward_profile", None)
    _vc_trigger   = getattr(_vc_opp, "expected_trigger", None)
    _vc_inv       = getattr(_vc_opp, "invalidation_price", 0.0) or 0.0
    _vc_risk_dist = abs(confluence_zone - _vc_inv) if _vc_inv else 0.0

    def _vc_auction_extreme(_dir):
        _a = _survey.auction if _survey else None
        if not _a or not _vc_profile:
            return None
        if _vc_profile == "AT_BALANCE_EXTREME":
            return getattr(_a, "balance_high", None) if _dir == "long" else getattr(_a, "balance_low", None)
        if _vc_profile == "MID_BALANCE":
            _bh = getattr(_a, "balance_high", None)
            _bl = getattr(_a, "balance_low", None)
            return (_bh + _bl) / 2.0 if (_bh is not None and _bl is not None) else None
        if _vc_profile == "BREAKOUT_RETRACE":
            return getattr(_a, "fib_anchor_high", None) if _dir == "long" else getattr(_a, "fib_anchor_low", None)
        return None

    def _vc_candidates_for(_dir):
        return engines.compute_validation_candidates(
            direction=_dir,
            entry_price=confluence_zone,
            target_price=_vc_target,
            structural_rr=_vc_rr,
            reward_profile=_vc_profile,
            expected_trigger=_vc_trigger,
            auction_extreme=_vc_auction_extreme(_dir),
            risk_distance=_vc_risk_dist,
        )

    if _near_sup and not _near_res:
        validation_block = engines.format_validation_candidates_for_chev(_vc_candidates_for("long"))
    elif _near_res and not _near_sup:
        validation_block = engines.format_validation_candidates_for_chev(_vc_candidates_for("short"))
    else:
        validation_block = (
            engines.format_validation_candidates_for_chev(_vc_candidates_for("long"), " (IF LONG)") +
            engines.format_validation_candidates_for_chev(_vc_candidates_for("short"), " (IF SHORT)")
        )

    msg = (
        f"Hey Chev, Dexter here with REAL computed numbers. I've given you the full market data above — candles, volume, all levels. Study it yourself and make your own read.\n\n"
        f"I've detected a confluence zone at {confluence_zone:.5f} with {result['count']} factor(s) aligning: {', '.join(result['reasons'])}.\n"
        f"{direction_hint}\n\n"
        f"{counter_trend_warning}"
        f"{regime_context}"
        f"{pattern_block}"
        f"{invalidation_block}"
        f"{validation_block}"
        f"{geometry_block}"
        f"{playbook_block}"
        f"{journal_block}"
        f"Decide now. POST or SKIP, format per your instructions.\n\n"
        f"{rsi_block}"
        f"\n\n"
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
        f"  Confluence and structure ARE the entry basis — once they support the trade, act. Do NOT\n"
        f"    wait for an additional candle to close or a rejection wick to print before entering;\n"
        f"    that additional wait is not required and not requested.\n\n"
        f"ENTRY MODES — every POST must use exactly one, and say which in ENTRY BASIS:\n"
        f"  MODE A (market entry): price is AT the zone NOW. entry = current price area. Commit —\n"
        f"    don't wait for one more candle to print first.\n"
        f"  MODE B (pending limit at the zone): price hasn't reached the zone yet. Set entry = the\n"
        f"    zone price. Dexter parks it as a PENDING order that fills automatically if price\n"
        f"    arrives, and expires per trade_type.\n"
        f"  Choose MODE A vs MODE B purely on whether price is at the zone right now — never SKIP\n"
        f"    just because price hasn't arrived yet. SL must still sit at the structural\n"
        f"    invalidation level beyond the zone.\n\n"
        f"SIP — Stop in Profit (trade management concept):\n"
        f"  When Dexter asks you to manage an open trade, you may trail your SL above your entry (for LONG)\n"
        f"  or below your entry (for SHORT). Once SL crosses entry, it is NO LONGER an SL — it is a SIP.\n"
        f"  SIP = Stop in Profit. The trade CANNOT close at a loss. Your minimum exit is guaranteed profit.\n"
        f"  SIP ratchet rule: once SIP is active, it can ONLY move further in the direction of the trade.\n"
        f"    LONG SIP: can only move higher. SHORT SIP: can only move lower. Never backwards.\n"
        f"  When Dexter sends a checkpoint with SIP active, use TRAIL_SIP: [price] instead of TRAIL_SL.\n"
        f"  Once SIP is active, you no longer need to fear a stop out — focus on maximising profit.\n\n"
        f"CONFLUENCE SCORE — computed by Dexter, authoritative:\n"
        f"  Dexter has already computed this setup's score with its calibrated weights.\n"
        f"  This setup has ALREADY PASSED the minimum-score gate for its asset class.\n"
        f"  Do NOT re-count, re-score, or SKIP on score grounds — Dexter's arithmetic is final.\n"
        f"  Your job is judgment, not arithmetic: 4H structure, trend alignment, contradictions\n"
        f"  between signals, invalidation quality, and timing. Judge those. Nothing else.\n"
        f"  Still tag your confluences on the TRADE: line (tags=sr_4h,fib_1h,rsi_1h etc.) —\n"
        f"  tags drive the chart anchors and the learning system, they are not a score.\n\n"
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
        f"{_exploration_note}"
        f"Reminder: first word of your reply = POST or SKIP. No exceptions."
    )
    if force_take:
        msg += (
            "\n\nHYPOTHETICAL MODE — this is Kev asking off the record, not a real trade request. "
            "It will not be posted, logged, or executed. Even if your honest read is SKIP, answer "
            "using the POST:/TRADE: format above with your best hypothetical entry/SL/TP/tags. Add "
            "one line after TRADE: → WOULD_TAKE: yes or WOULD_TAKE: no (would you genuinely take "
            "this live), and say why in REASONING regardless of the format you're forced to use."
        )
    messages = [{"role": "user", "content": context_prefix + msg}]
    reply = _call_chev(messages, timeout=timeout, model_id=ESCALATION_MODEL_ID)

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
            reply = _call_chev(messages, timeout=timeout, model_id=ESCALATION_MODEL_ID)

    return reply, messages

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

    # REASONING is required on both POST and SKIP replies — parse it unconditionally so
    # a SKIP's full narrative isn't silently discarded (only the one-line SKIP: summary
    # and WHAT WAS MISSING used to survive).
    for line in lines:
        s = line.strip()
        if s.upper().startswith("REASONING:"):
            result["reasoning"] = s.split(":", 1)[1].strip()
            break

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
                    "leverage": _clean_numeric(fields["leverage"]) if fields.get("leverage") else 1.0,
                    "position_size_usd": _clean_numeric(fields["position_size_usd"]) if fields.get("position_size_usd") else 0.0,
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

        # REASONING already parsed unconditionally above — attach it to the trade dict too.
        if result["trade"] is not None and result.get("reasoning"):
            result["trade"]["reasoning"] = result["reasoning"]

        # Parse the three mandatory structured analysis fields. ENTRY BASIS replaced the old
        # CONFIRMATION field (2026-07-05 — dropped the "wait for a candle close" framing); both
        # labels are still accepted here so an in-flight thread that hasn't picked up the new
        # prompt wording yet doesn't silently lose the field.
        for _field, _key in [("4H STRUCTURE:", "structure_4h"),
                              ("INVALIDATION:", "invalidation"),
                              ("ENTRY BASIS:", "confirmation"),
                              ("CONFIRMATION:", "confirmation"),
                              ("STRUCTURAL READ:", "structural_read")]:
            for line in lines:
                s = line.strip()
                if s.upper().startswith(_field.upper()):
                    result[_key] = s.split(":", 1)[1].strip() if ":" in s else ""
                    break

        # WOULD_TAKE — only present on hypothetical (force_take) replies; harmless no-op otherwise
        for line in lines:
            s = line.strip()
            if s.upper().startswith("WOULD_TAKE:"):
                result["would_take"] = s.split(":", 1)[1].strip().lower().startswith("y")
                break

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

    # Parse WHAT WAS MISSING for SKIP responses
    if not result["post"]:
        for line in lines:
            s = line.strip()
            if s.upper().startswith("WHAT WAS MISSING:"):
                result["what_was_missing"] = s.split(":", 1)[1].strip() if ":" in s else ""
                break

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


def _trade_duration_str(trade):
    """Return human-readable time elapsed since trade opened, e.g. '3h 12m'.
    Falls back to "open_ts" when "opened_at" is missing -- see _do_postmortem for why."""
    try:
        opened = datetime.strptime(trade.get("opened_at") or trade.get("open_ts") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        secs   = int((datetime.now(timezone.utc) - opened).total_seconds())
        h, m   = secs // 3600, (secs % 3600) // 60
        return f"{h}h {m}m" if h > 0 else f"{m}m"
    except Exception:
        return "?"


def _maybe_send_bos_alert(trade, current_price):
    """Detect a new BOS/CHoCH on the trade's primary TF and, if unseen, fire a management message."""
    try:
        symbol     = trade["symbol"]
        asset_type = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")
        primary_tf = trade.get("primary_tf", "1h")
        df = fetch_candles(symbol, asset_type, primary_tf, limit=100)
        if df is None or len(df) < 25:
            return
        bos = _bos_choch_label(df)
        if not bos or bos["event"] == "STRUCTURE UNCLEAR":
            return

        # One alert per unique structural event (event type + price level to 4 sig figs)
        fingerprint = f"{bos['event']}_{round(current_price, 4)}"
        if "bos_alerts_sent" not in trade:
            trade["bos_alerts_sent"] = set()
        if fingerprint in trade["bos_alerts_sent"]:
            return
        trade["bos_alerts_sent"].add(fingerprint)

        is_warn = "WARNING" in bos["event"]
        urgency = "⚠ STRUCTURE CHANGING" if is_warn else "⚠ STRUCTURE BREAK DETECTED"
        bos_paragraph = (
            f"{urgency} — {bos['event']}\n"
            f"  What happened : {bos['detail']}\n"
            f"  Still valid if: price stays {bos['hold']}\n" if bos.get("hold") else ""
            f"  Watch for     : {bos['warning']}\n\n"
            f"This structural event was NOT present when you entered this trade. It does not\n"
            f"automatically invalidate your position — but review whether it changes your premise.\n"
            f"If your INVALIDATION condition has been met, that is your signal to act.\n"
        )
        print(f"[BOS Alert] {symbol} {bos['event']} — firing management message")
        threading.Thread(
            target=lambda t=trade, p=current_price, bp=bos_paragraph: ask_chev_manage_trade(t, p, bos_paragraph=bp),
            daemon=True
        ).start()
    except Exception as e:
        print(f"[BOS Alert] Error for {trade.get('symbol','?')}: {e}")


# Binance Futures taker: 0.05% per side × 2 = 0.10% round-trip on notional
# Slippage: ~0.02% per side on liquid Binance pairs = 0.04% round-trip
# Forex: FXPro avg ~1 pip spread on major pairs + slippage (~0.01% per side = 0.02% RT)
# Stocks: typical retail 0.01% per side = 0.02% RT + slippage
# Funding: 0.01% per 8h (Binance standard interval: 00:00 / 08:00 / 16:00 UTC)
_TRADE_COST_RATES = {
    "crypto": {"fee_rt": 0.0010, "slip_rt": 0.0004},  # 0.10% Binance taker + 0.04% slip
    "forex":  {"fee_rt": 0.0002, "slip_rt": 0.0002},  # FXPro ~1 pip avg + slippage = 0.04%
    "stock":  {"fee_rt": 0.0002, "slip_rt": 0.0002},  # retail broker + slippage = 0.04%
}
_FUNDING_RATE_8H = 0.0001  # 0.01% per 8-hour Binance funding period


def _sim_trading_cost(notional_usd, asset_type, trade_type="day", open_ts=None):
    """Return simulated cost to deduct from PnL when closing `notional_usd` of a position.
    Includes: round-trip fees + slippage + Binance funding for crypto swings."""
    rates = _TRADE_COST_RATES.get(asset_type, _TRADE_COST_RATES["crypto"])
    cost  = notional_usd * (rates["fee_rt"] + rates["slip_rt"])

    # Funding: crypto perpetuals only, counted in whole 8h Binance settlement periods
    if asset_type == "crypto" and trade_type == "swing" and open_ts:
        try:
            opened = datetime.fromisoformat(open_ts.replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            hours_held      = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
            funding_periods = int(hours_held / 8)
            cost += notional_usd * _FUNDING_RATE_8H * funding_periods
        except Exception:
            pass

    return round(max(0.0, cost), 2)


def _execute_partial_close(trade, price, fraction, close_type, r_at_milestone):
    """Close `fraction` of a running trade at current price (0.25 = TAKE25, 0.50 = TAKE50).
    Updates balance, shrinks position, logs a PARTIAL journal entry."""
    global _cached_balance
    symbol    = trade["symbol"]
    direction = trade["direction"]
    is_long   = direction == "long"
    move_pct        = ((price - trade["entry"]) / trade["entry"]
                       if is_long else (trade["entry"] - price) / trade["entry"])
    _notional_closed = round(trade["position_size_usd"] * fraction, 2)
    _gross_pnl       = round(_notional_closed * move_pct, 2)
    partial_pnl, _cost = honest_sim.apply_costs(trade, _gross_pnl, fraction)

    balance     = get_balance(dashboard_ws)
    new_balance = round(balance + partial_pnl, 2)
    set_balance(dashboard_ws, new_balance)
    _cached_balance = new_balance

    trade["position_size_usd"] = round(trade["position_size_usd"] * (1 - fraction), 2)
    trade["partial_done"]      = True
    trade["partial_net_pnl"]   = round(trade.get("partial_net_pnl", 0.0) + partial_pnl, 2)

    pct_label = int(fraction * 100)
    print(
        f"[PARTIAL TP] {symbol} {direction.upper()} — {close_type}: "
        f"{pct_label}% closed at ${partial_pnl:+.2f} gross ${_gross_pnl:+.2f} fees ${_cost:.2f} "
        f"(+{r_at_milestone:.2f}R) | Balance: ${balance:.2f} → ${new_balance:.2f} | "
        f"Remaining {100 - pct_label}% continues"
    )

    _risk_usd = trade.get("risk_amount_usd", 0)
    _entry = {
        "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":           symbol,
        "asset_type":       trade.get("asset_type", "crypto"),
        "direction":        direction,
        "entry":            trade["entry"],
        "sl":               trade["sl"],
        "tp":               trade["tp"],
        "exit_price":       price,
        "pnl":              partial_pnl,
        "outcome":          "PARTIAL_TP",
        "close_type":       close_type,
        "tags":             trade.get("tags", ""),
        "duration":         "partial",
        "reasoning":        trade.get("reasoning", ""),
        "analysis":         (
            f"Chev milestone {close_type}: {pct_label}% of {symbol} closed at "
            f"${partial_pnl:+.2f} net (gross ${_gross_pnl:+.2f}, fees ${_cost:.2f}, {r_at_milestone:.2f}R). "
            f"Remaining {100 - pct_label}% continues with current SL/TP."
        ),
        "trading_cost":     _cost,
        "r_multiple":       round(r_at_milestone * fraction, 3),
        "risk_amount_usd":  _risk_usd,
        "position_size_usd": trade["position_size_usd"],
        "leverage":         trade.get("leverage", 1),
        "setup_grade":      trade.get("setup_grade", ""),
        "session_quality":  trade.get("session_quality", ""),
        "heat_at_entry":    trade.get("heat_at_entry", 0),
        "chev_moves":       [],
        "open_ts":          trade.get("open_ts", ""),
    }
    try:
        _j = _load_journal()
        _j.append(_entry)
        with open(JOURNAL_PATH, "w", encoding="utf-8") as _f:
            json.dump(_j, _f, indent=2)
    except Exception as _pe:
        print(f"[PARTIAL TP] Journal write failed: {_pe}")


def _ask_chev_partial_tp(trade, current_price, price_r, level_verdict=None, level_label=None, level_price=None):
    """Send Chev a milestone prompt at the trade-type R threshold.
    Chev replies HOLD / TAKE25 / TAKE50 / TRAIL_SL and Dexter executes."""
    symbol     = trade["symbol"]
    direction  = trade["direction"]
    is_long    = direction == "long"
    trade_type = trade.get("trade_type", "day")
    primary_tf = trade.get("primary_tf", "1h")
    asset_type = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")

    _live_pnl  = trade.get("live_pnl", 0)
    _pos_size  = trade.get("position_size_usd", 0)
    _duration  = _trade_duration_str(trade)
    _opened_at = trade.get("opened_at", "?")
    _last_dec  = trade.get("last_chev_decision", "")
    _last_px   = trade.get("last_decision_price", "")
    _last_when = trade.get("last_decision_time", "")

    market_brief  = _build_management_brief(symbol, asset_type, primary_tf, direction, trade["entry"], trade["tp"])
    playbook_text = _load_playbook(asset_type)

    milestone_block = (
        f"══════════════════════════════════════════════════\n"
        f"PARTIAL TP MILESTONE — {symbol} {direction.upper()}\n"
        f"══════════════════════════════════════════════════\n"
        f"Trade type: {trade_type.upper()}  |  Opened: {_opened_at}  |  Running: {_duration}\n\n"
        f"Price has reached +{price_r:.2f}R — the milestone check for a {trade_type} trade.\n"
        f"This is NOT an automated close. You are making the call.\n\n"
        f"ENTRY CONTEXT (your original words):\n"
        f"  Entry:        {trade['entry']}\n"
        f"  Stop Loss:    {trade['sl']}\n"
        f"  TP target:    {trade['tp']}\n"
        f"  Tags:         {trade.get('tags', 'n/a')}\n"
        f"  POST:         {(trade.get('reasoning') or 'not recorded')[:150]}\n"
        f"  4H STRUCTURE: {trade.get('structure_4h') or 'not recorded'}\n"
        f"  INVALIDATION: {trade.get('invalidation') or 'not recorded'}\n\n"
    )
    if level_verdict is not None:
        milestone_block += (
            f"\n--- LEVEL HEALTH CONTEXT ---\n"
            f"Price is within 1.5% of {level_label or 'a known level'} at {level_price or 'N/A'}.\n"
            f"Dexter's structural verdict: {level_verdict}\n"
            f"  HOLDING  = strong touches, confirmed, likely to reject price\n"
            f"  WEAKENING = losing structure, could hold or break\n"
            f"  BROKEN   = price accepted through — Dexter already moved SL to breakeven\n\n"
            f"If WEAKENING: weigh whether to take profit here or trail and let it push through.\n"
            f"----------------------------\n\n"
        )
    if _last_dec:
        milestone_block += f"YOUR LAST DECISION: {_last_dec} (price was {_last_px} at {_last_when})\n\n"

    milestone_block += (
        f"CURRENT STATUS:\n"
        f"  Current price : {current_price:.5f}\n"
        f"  Live PnL      : ${_live_pnl:+.2f}  (+{price_r:.2f}R)\n"
        f"  Position size : ${_pos_size:.0f} USD (this is what you are deciding about)\n\n"
    )

    tool_block = (
        f"TOOLS — use at least one before deciding:\n"
        f"  get_support_resistance(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → Is there a resistance wall {'above' if is_long else 'below'} current price?\n"
        f"      If yes — is price likely to stall or reverse there?\n"
        f"  get_volume_profile(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → Is volume increasing (continuation) or fading (exhaustion)?\n"
        f"  detect_rsi_divergence(\"{symbol}\", \"{primary_tf}\")\n"
        f"    → Bearish divergence = take profit now. Hidden bullish divergence = let it run.\n\n"
    )

    decision_block = (
        f"YOUR FOUR OPTIONS — reply with exactly one:\n\n"
        f"  HOLD     — thesis intact, no overhead resistance, let the full position run to TP\n"
        f"  TAKE25   — lock 25% profit now, let 75% breathe to full TP\n"
        f"  TAKE50   — lock 50% profit now, let 50% run free with no pressure\n"
        f"  TRAIL_SL — move SL to entry ({trade['entry']}), hold full size, guarantee breakeven\n\n"
        f"How to decide:\n"
        f"  HOLD     when: clean air above, RSI has room, volume rising, original TP still valid\n"
        f"  TAKE25   when: approaching a minor wall, but the bigger target is still reachable\n"
        f"  TAKE50   when: hitting a major resistance, volume fading, or RSI showing reversal signs\n"
        f"  TRAIL_SL when: momentum is good but you want to remove the risk entirely\n\n"
        f"No other reply is valid. Do not explain your choice — just reply with the one word (or TRAIL_SL).\n"
    )

    content = (
        (f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else "")
        + milestone_block
        + market_brief + "\n\n"
        + tool_block
        + decision_block
    )

    print(f"[PARTIAL MILESTONE] {symbol} {direction.upper()} +{price_r:.1f}R — asking Chev HOLD/TAKE25/TAKE50/TRAIL_SL")
    reply = _call_chev([{"role": "user", "content": content}], timeout=90, model_id=ESCALATION_MODEL_ID)
    reply_upper = (reply or "").strip().upper()

    if reply:
        trade["last_chev_decision"]  = f"MILESTONE +{price_r:.1f}R: {reply.strip()[:60]}"
        trade["last_decision_price"] = round(current_price, 5)
        trade["last_decision_time"]  = datetime.now(timezone.utc).strftime("%H:%M")

    if reply_upper.startswith("TAKE25"):
        _execute_partial_close(trade, current_price, fraction=0.25, close_type="PARTIAL_25", r_at_milestone=price_r)
    elif reply_upper.startswith("TAKE50"):
        _execute_partial_close(trade, current_price, fraction=0.50, close_type="PARTIAL_1R", r_at_milestone=price_r)
    elif reply_upper.startswith("TRAIL_SL"):
        entry = trade["entry"]
        valid = (entry < current_price) if is_long else (entry > current_price)
        if valid and trade["sl"] != entry:
            old_sl = trade["sl"]
            trade["sl"] = entry
            try:
                worksheet.update_cell(trade["row"], 4, entry)
            except Exception as _e:
                print(f"[PARTIAL MILESTONE] Sheet SL update failed: {_e}")
            sl_entry = f"BE {old_sl} → {entry} @ {datetime.now(timezone.utc).strftime('%H:%M')} (milestone TRAIL_SL)"
            trade.setdefault("chev_moves", []).append(sl_entry)
            print(f"[PARTIAL MILESTONE] {symbol} SL → entry {entry} (breakeven protected)")
        else:
            print(f"[PARTIAL MILESTONE] {symbol} TRAIL_SL — SL already at entry or invalid, no change")
    elif reply_upper.startswith("HOLD"):
        print(f"[PARTIAL MILESTONE] {symbol} Chev says HOLD — full position continues to TP")
    else:
        print(f"[PARTIAL MILESTONE] {symbol} unexpected reply '{(reply or '').strip()[:50]}' — treating as HOLD")


def _ask_chev_time_stop(trade, current_price, hours_open, hours_allowed, price_r):
    """Send Chev the time-stop checkpoint for a trade that's OPEN past its expiry_at.
    Chev replies CLOSE_NOW or CONVERT_TO_SWING. Builds and sends only -- the caller in
    check_and_update_open_trades parses the reply and executes it (same division of
    responsibility as ask_chev_manage_trade, since this can also fully close the trade
    and that has to happen where still_open/balance/sheet are in scope)."""
    symbol     = trade["symbol"]
    direction  = trade["direction"]
    trade_type = trade.get("trade_type", "day")
    primary_tf = trade.get("primary_tf", "1h")
    asset_type = next((w["type"] for w in WATCHLIST if w["symbol"] == symbol), "crypto")

    market_brief  = _build_management_brief(symbol, asset_type, primary_tf, direction, trade["entry"], trade["tp"])
    playbook_text = _load_playbook(asset_type)

    checkpoint_block = (
        f"══════════════════════════════════════════\n"
        f"TIME-STOP CHECKPOINT — {symbol} {direction.upper()}\n"
        f"══════════════════════════════════════════\n"
        f"Trade type: {trade_type.upper()}  |  Hours open: {hours_open:.1f}  |  Hours allowed: {hours_allowed}\n\n"
        f"This trade has run past the time budget for a {trade_type} trade. It hasn't hit SL or\n"
        f"TP -- it's just sitting open. That is not the trade you originally chose; it's a\n"
        f"different trade nobody consciously picked. Decide, right now, which one this actually is.\n\n"
        f"CURRENT STATUS:\n"
        f"  Entry:          {trade['entry']}\n"
        f"  Stop Loss:      {trade['sl']}\n"
        f"  TP target:      {trade['tp']}\n"
        f"  Current price:  {current_price:.5f}\n"
        f"  Unrealized R:   {price_r:+.2f}R\n\n"
        f"YOUR TWO OPTIONS -- reply with exactly one:\n\n"
        f"  CLOSE_NOW        — exit the full position now at market. Use this if the original\n"
        f"                     thesis has gone stale or you can't name a reason it's still valid.\n"
        f"  CONVERT_TO_SWING — consciously re-choose this as a swing trade (new 48h time\n"
        f"                     budget). Only takes effect if this trade is not already a swing,\n"
        f"                     and only usable once per trade -- if it times out again later as\n"
        f"                     a swing, it closes, it does not convert again. Use this only if\n"
        f"                     the original structural thesis is still intact and simply needs\n"
        f"                     more room than its original trade_type allowed.\n\n"
        f"No other reply is valid. Do not explain your choice — just reply with the one word/phrase.\n"
    )

    content = (
        (f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else "")
        + checkpoint_block
        + market_brief
    )

    print(f"[TIME-STOP] {symbol} {direction.upper()} — {hours_open:.1f}h open vs {hours_allowed}h allowed "
          f"({price_r:+.2f}R) — asking Chev CLOSE_NOW/CONVERT_TO_SWING")
    reply = _call_chev([{"role": "user", "content": content}], timeout=90, model_id=ESCALATION_MODEL_ID)
    if reply:
        trade["last_chev_decision"]  = f"TIME-STOP: {reply.strip()[:60]}"
        trade["last_decision_price"] = round(current_price, 5)
        trade["last_decision_time"]  = datetime.now(timezone.utc).strftime("%H:%M")
    return reply


def ask_chev_manage_trade(trade, current_price, bos_paragraph=None):
    """Ask Chev to manage an open trade — trail SL/SIP, close, or hold."""

    # ── Cooldown: max one ask per 30 min. BOS alerts always bypass. ───
    _now_ts = time.time()
    if bos_paragraph is None and _now_ts - trade.get("last_management_at", 0) < 30 * 60:
        return None
    trade["last_management_at"] = _now_ts

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

    # ── Build "then vs now" context block ─────────────────────────────
    _opened_at  = trade.get("opened_at", "?")
    _duration   = _trade_duration_str(trade)
    _now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    _snap       = trade.get("entry_brief_snapshot", "")
    _snap_time  = trade.get("entry_snapshot_time", _opened_at)
    _last_dec   = trade.get("last_chev_decision", "")
    _last_price = trade.get("last_decision_price", "")
    _last_when  = trade.get("last_decision_time", "")
    _floor_lbl  = "Profit floor (SIP)" if sip_active else "Stop Loss"

    _context = ""
    if bos_paragraph:
        _context += f"{bos_paragraph}\n"

    _context += (
        f"══════════════════════════════════════════\n"
        f"TRADE CONTEXT — {symbol} {trade['direction'].upper()}\n"
        f"Opened: {_opened_at}  |  Now: {_now_str}  |  Running: {_duration}\n"
        f"══════════════════════════════════════════\n\n"
    )
    if _last_dec:
        _context += f"YOUR LAST DECISION: {_last_dec} (price was {_last_price} at {_last_when})\n\n"

    _context += (
        f"ORIGINAL ENTRY:\n"
        f"  Entry price       : {trade['entry']}\n"
        f"  {_floor_lbl:20}: {trade['sl']}\n"
        f"  Original TP       : {trade.get('original_tp', trade['tp'])}\n"
        f"  Current TP        : {trade['tp']}\n"
        f"  Entry tags        : {trade.get('tags', 'n/a')}\n\n"
        f"WHY YOU ENTERED (your words at open):\n"
        f"  POST        : {(trade.get('reasoning') or 'not recorded')[:150]}\n"
        f"  4H STRUCTURE: {trade.get('structure_4h') or 'not recorded'}\n"
        f"  INVALIDATION: {trade.get('invalidation') or 'not recorded'}\n"
        f"  ENTRY BASIS  : {trade.get('confirmation') or 'not recorded'}\n\n"
    )
    if _snap:
        _context += (
            f"─── MARKET AT ENTRY ({_snap_time}) ───────────────────────\n"
            f"{_snap}\n\n"
            f"─── MARKET NOW ({_now_str}) — {_duration} later ──────────\n"
        )

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
            f"{_context}"
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
            f"{_context}"
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
    playbook_text = _load_playbook(asset_type)
    content = (f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else "") + msg
    reply = _call_chev([{"role": "user", "content": content}], timeout=300, model_id=ESCALATION_MODEL_ID)
    # Store Chev's decision so the next management brief can quote it back to him
    if reply:
        trade["last_chev_decision"]  = reply.strip()[:80]
        trade["last_decision_price"] = round(current_price, 5)
        trade["last_decision_time"]  = datetime.now(timezone.utc).strftime("%H:%M")
    return reply


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

_MACRO_KEYWORDS = {"fomc", "fed", "interest rate", "cpi", "inflation", "nfp", "non-farm", "gdp", "pce"}

def _upcoming_high_impact(symbol, asset_type, window_hours=2):
    """Return warning strings for high-impact events within window_hours.
    Forex: filters by pair currencies. Crypto/stocks: filters for macro events (FOMC, CPI, NFP, etc.)
    that affect all markets."""
    events   = _fetch_economic_calendar()
    now_utc  = datetime.now(timezone.utc).replace(tzinfo=None)
    warnings = []

    if asset_type == "forex":
        clean      = symbol.upper().replace("/", "")
        currencies = {clean[:3], clean[3:6]} if len(clean) >= 6 else set()
    else:
        currencies = None  # all events — filtered by macro keywords below

    for ev in events:
        if ev.get("impact") != "High":
            continue
        country = ev.get("country", "").upper()
        title   = ev.get("title", "").lower()

        if currencies is not None:
            if country not in currencies:
                continue
        else:
            # Crypto/stocks: only warn on USD-denominated macro events with known market-wide impact
            is_usd = country in ("USD", "US")
            is_macro = any(kw in title for kw in _MACRO_KEYWORDS)
            if not (is_usd and is_macro):
                continue

        ev_time = _parse_ff_date(ev.get("date", ""))
        if ev_time is None:
            continue
        diff_h = (ev_time - now_utc).total_seconds() / 3600
        if -0.5 <= diff_h <= window_hours:
            label = f"{ev.get('country','?')} {ev.get('title','event')}"
            if diff_h < 0:
                warnings.append(f"⚠ {label} just released ({abs(diff_h)*60:.0f}min ago) — expect volatility spike")
            else:
                warnings.append(f"⚠ {label} in {diff_h:.1f}h — high market-wide volatility risk")
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

def _active_thresholds(asset_type):
    """Single source of truth for 'is this score/distance good enough right now'.
    Every gate that checks a confluence score or escalation distance must call
    this — never read CONFLUENCE_THRESHOLD_*/ESCALATION_MAX_DIST_PCT directly —
    so EXPLORATION_MODE can never be half-applied across gates.
    Returns (score_threshold, max_dist_pct) for the given asset_type.
    """
    if EXPLORATION_MODE:
        _score = (EXPLORATION_THRESHOLD_CRYPTO if asset_type == "crypto"
                  else EXPLORATION_THRESHOLD_FOREX if asset_type == "forex"
                  else EXPLORATION_THRESHOLD_STOCK)
        return _score, EXPLORATION_MAX_DIST_PCT
    _score = (CONFLUENCE_THRESHOLD_CRYPTO if asset_type == "crypto"
              else CONFLUENCE_THRESHOLD_FOREX if asset_type == "forex"
              else CONFLUENCE_THRESHOLD_STOCK)
    return _score, ESCALATION_MAX_DIST_PCT


def _directional_split(reasons):
    """Split a scan's reason strings into (bull_points, bear_points).
    Neutral/location signals (Fib, GP, VP, BB squeeze/mid, ranges, WATCH items)
    contribute to neither side. Weight = the number before 'pt)' in the string,
    default 1.0 when absent. Used to block escalation of contradictory setups."""
    import re as _re
    bull, bear = 0.0, 0.0
    for s in reasons:
        sl = s.lower()
        # Neutral / location-only signals — skip
        if ("[watch" in sl or "golden pocket" in sl or sl.startswith("fib")
                or "vp " in sl or sl.startswith("vp") or "squeeze" in sl
                or "bb mid" in sl or "(neutral" in sl):
            continue
        m = _re.search(r'([\d.]+)\s*pt\)', s)
        w = float(m.group(1)) if m else 1.0
        if "bearish" in sl or "sell_side" in sl or "overbought" in sl:
            bear += w
        elif "bullish" in sl or "buy_side" in sl or "oversold" in sl:
            bull += w
        elif "resistance" in sl:
            bear += w
        elif "support" in sl:
            bull += w
        # anything unclassified stays neutral
    return bull, bear


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
    try:
        skip_ws = sheet.worksheet(SKIP_LOG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        skip_ws = sheet.add_worksheet(title=SKIP_LOG_TAB, rows=2000, cols=10)
        skip_ws.append_row([
            "Timestamp UTC", "Pair", "TF", "Decision", "Score", "Regime",
            "Reason", "Confluences Seen",
        ])
        print("[Dexter] Created 'Skip Log' worksheet in Google Sheet.")
    return trade_log_ws, dashboard_ws, jane_ws, skip_ws

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
                    _raw_meta = json.loads(row[17]) if len(row) > 17 and row[17] else {}
                    # New format: {"prices": {...}, "setup_grade": "A", ...}
                    # Old format: {"SR_LEVEL": ..., "VP_START": ...} (just prices)
                    if "prices" in _raw_meta:
                        trade_entry["confluence_prices"] = _raw_meta.get("prices", {})
                        trade_entry["setup_grade"]       = _raw_meta.get("setup_grade", "")
                        trade_entry["session_quality"]   = _raw_meta.get("session_quality", "")
                        trade_entry["heat_at_entry"]     = _raw_meta.get("heat_at_entry", 0)
                        trade_entry["reasoning"]         = _raw_meta.get("reasoning", "")
                        trade_entry["primary_tf"]        = _raw_meta.get("primary_tf", "1h")
                    else:
                        trade_entry["confluence_prices"] = _raw_meta  # old format
                        trade_entry["setup_grade"]       = ""
                        trade_entry["session_quality"]   = ""
                        trade_entry["heat_at_entry"]     = 0
                        trade_entry["reasoning"]         = ""
                        trade_entry["primary_tf"]        = "1h"
                except Exception:
                    trade_entry["confluence_prices"] = {}
                    trade_entry.setdefault("setup_grade", "")
                    trade_entry.setdefault("session_quality", "")
                    trade_entry.setdefault("heat_at_entry", 0)
                    trade_entry.setdefault("reasoning", "")
                    trade_entry.setdefault("primary_tf", "1h")
                # Detect SIP state on load (SL already above entry from a previous trail)
                _is_long_load = direction.lower() == "long"
                _sl_val = float(sl)
                _en_val = float(entry)
                if (_is_long_load and _sl_val > _en_val) or (not _is_long_load and _sl_val < _en_val):
                    trade_entry["sip_active"] = True
                    trade_entry["sip_price"]  = _sl_val
                    print(f"[SIP] {pair} loaded with SIP at {_sl_val} — trade cannot lose.")
                # expiry_at is written once at proposal time (still PENDING) and never
                # rewritten at fill -- the same column holds the trade's original expiry
                # whether it's still PENDING or has since gone OPEN. Load it for both
                # statuses so the OPEN-branch time-stop check (PHASE 5) has it after a
                # restart, not just fresh-this-process trades.
                trade_entry["expiry_at"] = row[16] if len(row) > 16 and row[16] else (datetime.now(timezone.utc) + pd.Timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                if status_clean == "PENDING":
                    if len(row) > 18 and row[18] in ("True", "False"):
                        # Stored at order creation — safe across restarts
                        trade_entry["entry_trigger_above"] = row[18] == "True"
                    else:
                        # Fallback for older rows: LONGs wait for price to DROP to entry,
                        # SHORTs wait for price to RISE to entry (covers the common limit-order case)
                        trade_entry["entry_trigger_above"] = direction.lower() == "short"
                if len(row) > 19 and row[19]:
                    trade_entry["risk_amount_usd"] = float(row[19])
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
                    t["expiry_at"] = row[16] if len(row) > 16 else (datetime.now(timezone.utc) + pd.Timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
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

    timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    expiry_hrs = TRADE_TYPE_EXPIRY_HOURS.get(trade_type, 6)
    expiry_at  = (datetime.now(timezone.utc) + pd.Timedelta(hours=expiry_hrs)).strftime("%Y-%m-%d %H:%M:%S")
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
                expiry_time = datetime.strptime(trade["expiry_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expiry_time:
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

        hit_tp, hit_sl = _detect_sl_tp_hit(trade, price)
        trade["last_wick_check_ms"] = int(time.time() * 1000)

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
            if _is_duplicate_journal_entry(trade["symbol"], trade["entry"], trade["sl"], trade["tp"], jane_journal):
                print(f"[Jane journal] Duplicate detected for {trade['symbol']} — skipping write.")
            else:
                jane_journal.append({
                    "ts":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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


def _build_confluence_prices(td):
    """Translate Chev's visual-anchor fields (chev_sr_level, chev_fib_high/low, chev_vp_start,
    chev_rsi_div_t1/t2) on a parsed trade dict into the SR_S/FB_*/VP_*/RSI_DIV_* price-line
    shape the webapp's chart overlay draws. Pure — no I/O, no logging side effects."""
    conf_prices = {}

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

    return conf_prices


def log_new_trade(worksheet, dashboard_ws, symbol, asset_type, trade, current_price_at_creation, confluence_prices=None, primary_tf=None, regime_at_proposal=None):
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

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    expiry_hours = TRADE_TYPE_EXPIRY_HOURS[trade_type]
    expiry_at = (datetime.now(timezone.utc) + pd.Timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")
    entry_trigger_above = entry > current_price_at_creation
    risk_amount_usd = round((trade.get("risk_pct") or 1.0) / 100 * balance, 2)

    conf_json = json.dumps(confluence_prices) if confluence_prices else ""

    row_values = [
        symbol, direction.upper(), entry, sl, tp, risk_pct, leverage,
        position_size_usd, margin_used_usd, tags, "", "", "PENDING", "", timestamp,
        trade_type, expiry_at, conf_json, str(entry_trigger_above), risk_amount_usd
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
        "risk_amount_usd": risk_amount_usd,
        "confluence_prices": confluence_prices or {},
        "primary_tf": primary_tf,
        "regime_at_proposal": regime_at_proposal,
    }


def _fetch_1m_wicks_since(symbol, since_ms):
    """Return list of (low, high) floats from 1-minute Binance candles since since_ms.
    Used to detect SL/TP wicks that occurred between monitoring polls."""
    try:
        params = {
            "symbol":    symbol,
            "interval":  "1m",
            "startTime": int(since_ms),
            "endTime":   int(time.time() * 1000),
            "limit":     25,  # covers up to 25 minutes — more than any poll interval
        }
        resp = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=10)
        raw  = resp.json()
        if not isinstance(raw, list):
            return []
        # kline row: [open_time, open, high, low, close, volume, ...]
        return [(float(c[3]), float(c[2])) for c in raw]  # (low, high)
    except Exception:
        return []


def _honest_sim_last_check_ts(trade):
    """Epoch of the last candle already processed for this trade's exit checks.
    Falls back to the trade's creation time, then a 5-minute lookback, when absent
    (legacy trades loaded before this deploy never had this field)."""
    if trade.get("last_exit_check_ts"):
        return trade["last_exit_check_ts"]
    opened_at = trade.get("opened_at")
    if opened_at:
        try:
            return int(datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            pass
    return int(time.time() - 300)


def _detect_sl_tp_hit(trade, current_price):
    """
    Returns (hit_tp, hit_sl) for an open trade at current_price.
    For crypto: fetches 1-minute Binance candles since the last poll and inspects
    highs/lows so wicks that reversed before the next check are not invisible.
    When a single candle covers both SL and TP, SL is assumed to have hit first
    (pessimistic — keeps simulation honest).
    For forex/stocks: current price only (Twelve Data 1m costs API credits).
    """
    is_long = trade["direction"] == "long"
    sl, tp  = trade["sl"], trade["tp"]

    if is_long:
        spot_sl = current_price <= sl
        spot_tp = current_price >= tp
    else:
        spot_sl = current_price >= sl
        spot_tp = current_price <= tp

    if spot_sl:
        return False, True  # SL already visible at current price — done

    asset_type = trade.get("asset_type", "crypto")
    if asset_type == "crypto":
        since_ms = trade.get("last_wick_check_ms", (time.time() - 300) * 1000)
        for candle_low, candle_high in _fetch_1m_wicks_since(trade["symbol"], since_ms):
            if is_long:
                sl_wick = candle_low  <= sl
                tp_wick = candle_high >= tp
            else:
                sl_wick = candle_high >= sl
                tp_wick = candle_low  <= tp
            if sl_wick:
                return False, True   # SL first (also covers ambiguous candle)
            if tp_wick:
                return True, False

    return spot_tp, spot_sl


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

            since_epoch  = _honest_sim_last_check_ts(trade)
            exit_candles = honest_sim.get_exit_candles(trade["symbol"], trade["asset_type"], since_epoch, fetch_candles)

            if exit_candles is None:
                print(f"[{datetime.now()}] {trade['symbol']} candle fetch failed — falling back to spot check for pending fill this cycle.")
                triggered = (price >= trade["entry"]) if trade["entry_trigger_above"] else (price <= trade["entry"])
                fill = {"fill_price": trade["entry"], "candle_ts": int(time.time()), "immediate_exit": None} if triggered else None
            else:
                if exit_candles:
                    trade["last_exit_check_ts"] = exit_candles[-1]["t"] + 1
                fill = honest_sim.check_pending_fill(trade, exit_candles) if exit_candles else None

            if fill is None:
                expiry_time = datetime.strptime(trade["expiry_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expiry_time:
                    try:
                        worksheet.update_cell(trade["row"], 13, "EXPIRED")
                    except Exception as e:
                        print(f"[{datetime.now()}] Expiry update failed for {trade['symbol']}: {e}")
                    print(f"[{datetime.now()}] {trade['symbol']} PENDING order expired - never filled, dropped.")
                    continue
                still_open.append(trade)
                continue

            # ── Fill-time re-validation ───────────────────────────────────────
            # risk_gauntlet only ever runs once, at proposal time. A PENDING order can
            # sit for up to TRADE_TYPE_EXPIRY_HOURS (2h scalp / 6h day / 48h swing) before
            # filling -- far longer than the ~6-minute window the stale-price guard was
            # calibrated for. Re-check the two conditions that can meaningfully drift
            # with time (ATR floor, cost/R:R using the ACTUAL fill price, which can differ
            # from the quoted entry on a gap fill) before committing real capital.
            # Regime drift is logged but NOT auto-cancelled on -- deciding whether a
            # regime flip should kill an otherwise-valid pending order is a bigger
            # judgment call than this fix is scoped to make.
            _fill_price    = fill["fill_price"]
            _cancel_reason = None
            _fill_prof     = risk_gauntlet.get_active_profile(EXPLORATION_MODE)
            try:
                _fresh_tf = trade.get("primary_tf") or "1h"
                _fresh_df = fetch_candles(trade["symbol"], trade["asset_type"], _fresh_tf, limit=60)
                _fresh_df = _ca_add_indicators(_fresh_df)
                _fresh_atr = float(_fresh_df["ATR"].iloc[-1]) if "ATR" in _fresh_df.columns else None
            except Exception as _fe:
                _fresh_atr = None
                print(f"[FILL RE-CHECK] {trade['symbol']} fresh ATR fetch failed ({_fe}) -- skipping ATR floor re-check this fill.")

            if _fresh_atr and _fresh_atr > 0:
                _tt_check      = trade.get("trade_type", "day")
                _atr_floor_mult = _fill_prof["ATR_FLOOR"].get(_tt_check, 1.2)
                _sl_dist       = abs(_fill_price - trade["sl"])
                _atr_min       = _atr_floor_mult * _fresh_atr
                if _sl_dist < _atr_min:
                    _cancel_reason = (f"ATR floor no longer met at fill -- SL distance {_sl_dist:.6f} < "
                                      f"{_atr_floor_mult}x fresh ATR ({_atr_min:.6f}). Volatility shifted "
                                      f"since Chev's proposal.")

            if _cancel_reason is None:
                try:
                    _a_check  = trade["asset_type"] if trade["asset_type"] in risk_gauntlet.FEE_SIDE else "crypto"
                    _stop_pct = abs(_fill_price - trade["sl"]) / _fill_price if _fill_price else 0
                    _tp_pct   = abs(trade["tp"] - _fill_price) / _fill_price if _fill_price else 0
                    _cost_rt  = 2 * (risk_gauntlet.FEE_SIDE[_a_check] + risk_gauntlet.SLIPPAGE_SIDE[_a_check])
                    if _a_check == "crypto" and trade.get("trade_type") == "swing":
                        _cost_rt += risk_gauntlet.FUNDING_EST_SWING
                    _cost_R  = _cost_rt / _stop_pct if _stop_pct > 0 else float("inf")
                    _net_rr  = (_tp_pct - _cost_rt) / (_stop_pct + _cost_rt) if (_stop_pct + _cost_rt) > 0 else 0.0
                    _tt_check = trade.get("trade_type", "day")
                    if _cost_R > _fill_prof["MAX_COST_R"].get(_tt_check, 0.25):
                        _cancel_reason = (f"Cost gate fails at fill price -- cost_R {_cost_R:.2f} > "
                                          f"max {_fill_prof['MAX_COST_R'].get(_tt_check, 0.25)} for {_tt_check}.")
                    elif _net_rr < _fill_prof["MIN_NET_RR"].get(_tt_check, 2.0):
                        _cancel_reason = (f"Net R:R degraded at fill price -- {_net_rr:.2f} < "
                                          f"minimum {_fill_prof['MIN_NET_RR'].get(_tt_check, 2.0)} for {_tt_check}.")
                except Exception as _re:
                    print(f"[FILL RE-CHECK] {trade['symbol']} cost/R:R re-check failed ({_re}) -- proceeding on original gauntlet pass.")

            if _cancel_reason:
                try:
                    worksheet.update_cell(trade["row"], 13, "EXPIRED")
                except Exception as e:
                    print(f"[{datetime.now()}] Cancel-at-fill sheet update failed for {trade['symbol']}: {e}")
                print(f"[FILL RE-CHECK] {trade['symbol']} PENDING fill CANCELLED at trigger price {_fill_price} -- {_cancel_reason}")
                continue

            # Regime drift -- informational only, does not block the fill.
            try:
                _fresh_4h_df  = fetch_candles(trade["symbol"], trade["asset_type"], "4h", limit=60)
                _fresh_regime = _an_regime(_fresh_4h_df) if _fresh_4h_df is not None and len(_fresh_4h_df) >= 50 else None
                _fresh_regime_str = (_fresh_regime or {}).get("regime")
                _proposed_regime  = trade.get("regime_at_proposal")
                if _proposed_regime and _fresh_regime_str and _fresh_regime_str != _proposed_regime:
                    print(f"[FILL RE-CHECK] {trade['symbol']} regime drift at fill: proposed under "
                          f"{_proposed_regime}, now {_fresh_regime_str}. Not auto-cancelled -- flagging for review.")
            except Exception:
                pass

            trade["status"] = "OPEN"
            trade["entry"]  = fill["fill_price"]
            trade["original_sl"] = trade.get("original_sl", trade["sl"])
            trade["original_tp"] = trade.get("original_tp", trade["tp"])
            margin = round(trade.get("position_size_usd", 0) / max(trade.get("leverage", 1), 1), 2)
            trade["margin_reserved"] = margin
            balance = get_balance(dashboard_ws)
            new_balance = round(balance - margin, 2)
            set_balance(dashboard_ws, new_balance)
            _cached_balance = new_balance
            try:
                worksheet.update_cell(trade["row"], 13, "OPEN")
                worksheet.update_cell(trade["row"], 3, trade["entry"])  # fill price can differ from quoted entry on a stop-style gap
            except Exception as e:
                print(f"[{datetime.now()}] Status flip failed for {trade['symbol']}: {e}")
            print(f"[{datetime.now()}] {trade['symbol']} PENDING -> OPEN @ {trade['entry']} | Margin reserved: ${margin:.2f} | Balance: ${balance:.2f} -> ${new_balance:.2f}")

            imm = fill["immediate_exit"]

            # Book the 1R house partial from the fill candle FIRST (if one fired), so the
            # exit block below books on the halved position — mirrors the OPEN branch.
            if imm is not None and imm["partial"] is not None:
                _p = imm["partial"]
                is_long_p = trade["direction"] == "long"
                _partial_move = ((_p["price"] - trade["entry"]) / trade["entry"]) if is_long_p else ((trade["entry"] - _p["price"]) / trade["entry"])
                if _partial_move <= 0:
                    trade["partial_done"] = True
                    print(f"[PARTIAL TP] {trade['symbol']} fill gapped at/beyond 1R — no partial to take, flag set")
                else:
                    _gross_partial = round(trade["position_size_usd"] * 0.5 * _partial_move, 2)
                    partial_pnl, _partial_cost = honest_sim.apply_costs(trade, _gross_partial, 0.5)
                    balance     = get_balance(dashboard_ws)
                    new_balance = round(balance + partial_pnl, 2)
                    set_balance(dashboard_ws, new_balance)
                    _cached_balance = new_balance
                    trade["position_size_usd"] = round(trade["position_size_usd"] * 0.5, 2)
                    trade["partial_done"]      = True
                    trade["partial_net_pnl"]   = round(trade.get("partial_net_pnl", 0.0) + partial_pnl, 2)
                    print(f"[PARTIAL TP] {trade['symbol']} {trade['direction'].upper()} — 1R hit (candle-true, fill-candle @ {_p['price']}), 50% closed at ${partial_pnl:+.2f} (gross ${_gross_partial:+.2f} fees ${_partial_cost:.2f}) | Balance: ${balance:.2f} -> ${new_balance:.2f}")
                    _partial_entry = {
                        "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol":           trade["symbol"],
                        "asset_type":       trade.get("asset_type", "crypto"),
                        "direction":        trade["direction"],
                        "entry":            trade["entry"],
                        "sl":               trade["sl"],
                        "tp":               trade["tp"],
                        "exit_price":       _p["price"],
                        "pnl":              partial_pnl,
                        "outcome":          "PARTIAL_TP",
                        "close_type":       "PARTIAL_1R",
                        "tags":             trade.get("tags", ""),
                        "duration":         "partial",
                        "reasoning":        trade.get("reasoning", ""),
                        "analysis":         f"Candle-true partial close (fill-candle): 50% of {trade['symbol']} locked at 1R (${partial_pnl:+.2f} net, fees ${_partial_cost:.2f}). Remaining 50% continues.",
                        "trading_cost":     _partial_cost,
                        "r_multiple":       1.0,
                        "risk_amount_usd":  trade.get("risk_amount_usd", 0),
                        "position_size_usd": trade["position_size_usd"],
                        "leverage":         trade.get("leverage", 1),
                        "setup_grade":      trade.get("setup_grade", ""),
                        "session_quality":  trade.get("session_quality", ""),
                        "heat_at_entry":    trade.get("heat_at_entry", 0),
                        "chev_moves":       [],
                        "open_ts":          trade.get("open_ts", ""),
                    }
                    try:
                        _j = _load_journal()
                        _j.append(_partial_entry)
                        with open(JOURNAL_PATH, "w", encoding="utf-8") as _f:
                            json.dump(_j, _f, indent=2)
                    except Exception as _pe:
                        print(f"[PARTIAL TP] Journal write failed: {_pe}")

            if imm is not None and imm["exit_price"] is not None:
                is_long_p  = trade["direction"] == "long"
                exit_price = imm["exit_price"]
                close_type = imm["close_type"]
                exit_move  = ((exit_price - trade["entry"]) / trade["entry"]) if is_long_p else ((trade["entry"] - exit_price) / trade["entry"])
                _gross_pnl  = round(trade["position_size_usd"] * exit_move, 2)
                net_pnl, _fill_cost = honest_sim.apply_costs(trade, _gross_pnl, 1.0)
                outcome    = "WIN" if net_pnl >= 0 else "LOSS"
                if close_type == "SL_HIT":
                    _record_loss_for_cooldown(trade)  # flag for confluence re-entry check
                balance = get_balance(dashboard_ws)
                new_balance = round(balance + margin + net_pnl, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                try:
                    worksheet.update(values=[[exit_price, net_pnl, outcome, net_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
                except Exception as e:
                    print(f"[{datetime.now()}] Sheet update failed for {trade['symbol']}: {e}")
                print(f"[{datetime.now()}] {trade['symbol']} filled and immediately {close_type} in the same candle | ${net_pnl:+.2f} (gross ${_gross_pnl:+.2f} fees ${_fill_cost:.2f}) | Balance: ${balance:.2f} -> ${new_balance:.2f}")
                trade_copy = dict(trade)
                trade_copy["close_type"]   = close_type
                trade_copy["trading_cost"] = _fill_cost
                threading.Thread(target=_do_postmortem,
                                 args=(trade_copy, outcome, net_pnl, exit_price),
                                 daemon=True).start()
                continue

            still_open.append(trade)
            continue

        # ── Phase 4 safety net: risk_amount_usd must never be 0/missing on an OPEN trade ──
        # Catches two known-broken upstream paths: (1) load_state_from_sheet() reconstructs
        # OPEN/PENDING trades from the Google Sheet on every process restart, and the sheet
        # has no risk_amount_usd column at all -- reloaded trades carry none. (2) a PENDING
        # order that filled with a stale/zero value already on it. This runs every cycle for
        # every OPEN trade but is idempotent -- once patched, risk_amount_usd is nonzero and
        # this block no-ops on every later cycle. Does not touch the gauntlet's own sizing.
        if not trade.get("risk_amount_usd"):
            _net_balance  = get_balance(dashboard_ws) or 0
            _net_risk_pct = trade.get("risk_pct") or 1.0
            _net_risk     = round(_net_risk_pct / 100 * _net_balance, 2)
            _net_cap      = round(0.02 * _net_balance, 2)  # hard cap: 2% of balance
            if _net_risk > _net_cap and _net_risk > 0:
                _net_scale = _net_cap / _net_risk
                trade["position_size_usd"] = round(trade.get("position_size_usd", 0) * _net_scale, 2)
                _net_risk = _net_cap
            trade["risk_amount_usd"] = _net_risk
            print(f"[RISK NET] {trade['symbol']} OPEN with risk_amount_usd=0 -- computed ${_net_risk:.2f} "
                  f"({_net_risk_pct}% of ${_net_balance:.2f} balance, capped at 2%) -- upstream sizing path broken for this trade.")

        is_long = trade["direction"] == "long"

        move_pct = (price - trade["entry"]) / trade["entry"] if is_long else (trade["entry"] - price) / trade["entry"]
        live_pnl_dollars = round(trade["position_size_usd"] * move_pct, 2)

        print(f"[{datetime.now()}] {trade['symbol']} check - price: {price}, SL: {trade['sl']}, TP: {trade['tp']}, live PnL: ${live_pnl_dollars}")

        try:
            worksheet.update(values=[[price, live_pnl_dollars]], range_name=f"K{trade['row']}:L{trade['row']}")
        except Exception as e:
            print(f"[{datetime.now()}] Live price/PnL update failed for {trade['symbol']}: {e}")

        since_epoch  = _honest_sim_last_check_ts(trade)
        exit_candles = honest_sim.get_exit_candles(trade["symbol"], trade["asset_type"], since_epoch, fetch_candles)

        if exit_candles is None:
            print(f"[{datetime.now()}] {trade['symbol']} candle fetch failed — falling back to spot check this cycle.")
            hit_tp, hit_sl = _detect_sl_tp_hit(trade, price)
            trade["last_wick_check_ms"] = int(time.time() * 1000)

            if hit_tp or hit_sl:
                exit_price   = trade["tp"] if hit_tp else trade["sl"]
                exit_move    = ((exit_price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - exit_price) / trade["entry"])
                _gross_pnl   = round(trade["position_size_usd"] * exit_move, 2)
                exit_pnl, _trade_cost = honest_sim.apply_costs(trade, _gross_pnl, 1.0)
                outcome      = "WIN" if exit_pnl >= 0 else "LOSS"
                if hit_tp:
                    close_type = "TP_HIT"
                elif trade.get("sip_active"):
                    close_type = "SIP_HIT"
                else:
                    close_type = "SL_HIT"
                    _record_loss_for_cooldown(trade)  # flag for confluence re-entry check
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
                print(f"[{datetime.now()}] {arrow} {trade['symbol']} {close_type}  ${exit_pnl:+.2f} (gross ${_gross_pnl:+.2f} fees ${_trade_cost:.2f})  |  Piggy bank: ${balance:.2f} -> ${new_balance:.2f}")
                trade_copy = dict(trade)
                trade_copy["close_type"]   = close_type
                trade_copy["trading_cost"] = _trade_cost
                threading.Thread(target=_do_postmortem,
                                 args=(trade_copy, outcome, exit_pnl, exit_price),
                                 daemon=True).start()
                continue
        else:
            if exit_candles:
                trade["last_exit_check_ts"] = exit_candles[-1]["t"] + 1
            trade.setdefault("original_sl", trade["sl"])
            trade.setdefault("original_tp", trade["tp"])
            exit_result = honest_sim.check_exit(trade, exit_candles) if exit_candles else None

            if exit_result is not None and exit_result["partial"] is not None:
                _p = exit_result["partial"]
                _partial_move  = ((_p["price"] - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - _p["price"]) / trade["entry"])
                _gross_partial = round(trade["position_size_usd"] * 0.5 * _partial_move, 2)
                partial_pnl, _partial_cost = honest_sim.apply_costs(trade, _gross_partial, 0.5)
                balance     = get_balance(dashboard_ws)
                new_balance = round(balance + partial_pnl, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                trade["position_size_usd"] = round(trade["position_size_usd"] * 0.5, 2)
                trade["partial_done"]      = True
                trade["partial_net_pnl"]   = round(trade.get("partial_net_pnl", 0.0) + partial_pnl, 2)
                print(f"[PARTIAL TP] {trade['symbol']} {trade['direction'].upper()} — 1R hit (candle-true @ {_p['price']}), 50% closed at ${partial_pnl:+.2f} (gross ${_gross_partial:+.2f} fees ${_partial_cost:.2f}) | Balance: ${balance:.2f} -> ${new_balance:.2f}")
                _partial_entry = {
                    "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol":           trade["symbol"],
                    "asset_type":       trade.get("asset_type", "crypto"),
                    "direction":        trade["direction"],
                    "entry":            trade["entry"],
                    "sl":               trade["sl"],
                    "tp":               trade["tp"],
                    "exit_price":       _p["price"],
                    "pnl":              partial_pnl,
                    "outcome":          "PARTIAL_TP",
                    "close_type":       "PARTIAL_1R",
                    "tags":             trade.get("tags", ""),
                    "duration":         "partial",
                    "reasoning":        trade.get("reasoning", ""),
                    "analysis":         f"Candle-true partial close: 50% of {trade['symbol']} locked at 1R (${partial_pnl:+.2f} net, fees ${_partial_cost:.2f}). Remaining 50% continues.",
                    "trading_cost":     _partial_cost,
                    "r_multiple":       1.0,
                    "risk_amount_usd":  trade.get("risk_amount_usd", 0),
                    "position_size_usd": trade["position_size_usd"],
                    "leverage":         trade.get("leverage", 1),
                    "setup_grade":      trade.get("setup_grade", ""),
                    "session_quality":  trade.get("session_quality", ""),
                    "heat_at_entry":    trade.get("heat_at_entry", 0),
                    "chev_moves":       [],
                    "open_ts":          trade.get("open_ts", ""),
                }
                try:
                    _j = _load_journal()
                    _j.append(_partial_entry)
                    with open(JOURNAL_PATH, "w", encoding="utf-8") as _f:
                        json.dump(_j, _f, indent=2)
                except Exception as _pe:
                    print(f"[PARTIAL TP] Journal write failed: {_pe}")

            if exit_result is not None and exit_result["exit_price"] is not None:
                exit_price = exit_result["exit_price"]
                close_type = exit_result["close_type"]
                exit_move  = ((exit_price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - exit_price) / trade["entry"])
                _gross_pnl  = round(trade["position_size_usd"] * exit_move, 2)
                exit_pnl, _trade_cost = honest_sim.apply_costs(trade, _gross_pnl, 1.0)
                outcome     = "WIN" if exit_pnl >= 0 else "LOSS"
                if close_type == "SL_HIT":
                    _record_loss_for_cooldown(trade)  # flag for confluence re-entry check
                balance = get_balance(dashboard_ws)
                margin_to_return = trade.get("margin_reserved", 0)
                new_balance = round(balance + margin_to_return + exit_pnl, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                try:
                    worksheet.update(values=[[exit_price, exit_pnl, outcome, exit_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
                except Exception as e:
                    print(f"[{datetime.now()}] Sheet update failed for {trade['symbol']}: {e}")
                arrow = "+" if close_type == "TP_HIT" else ("-" if close_type == "SL_HIT" else "~")
                print(f"[{datetime.now()}] {arrow} {trade['symbol']} {close_type} (candle-true)  ${exit_pnl:+.2f} (gross ${_gross_pnl:+.2f} fees ${_trade_cost:.2f})  |  Piggy bank: ${balance:.2f} -> ${new_balance:.2f}")
                trade_copy = dict(trade)
                trade_copy["close_type"]   = close_type
                trade_copy["trading_cost"] = _trade_cost
                threading.Thread(target=_do_postmortem,
                                 args=(trade_copy, outcome, exit_pnl, exit_price),
                                 daemon=True).start()
                continue

        # Store original SL once (before any trailing modifies it)
        if "original_sl" not in trade:
            trade["original_sl"] = trade["sl"] if not trade.get("sip_active") else trade["entry"]
        if "original_tp" not in trade:
            trade["original_tp"] = trade["tp"]

        orig_risk = abs(trade["entry"] - trade["original_sl"])

        # Detect SIP transition (SL crossed above entry for LONG, or below for SHORT)
        if not trade.get("sip_active") and _is_sip(trade):
            trade["sip_active"] = True
            trade["sip_price"]  = trade["sl"]
            print(f"[SIP] {trade['symbol']} SL {trade['sl']} crossed entry {trade['entry']} — SIP active, trade cannot lose.")

        # ── TIME-STOP CHECKPOINT ────────────────────────────────────────────────
        # expiry_at is set at proposal time (TRADE_TYPE_EXPIRY_HOURS) and, until now, was
        # only ever re-checked while the trade was still PENDING -- once OPEN it squatted
        # forever even if it never hit SL or TP. One checkpoint per expiry event: Chev
        # gets CLOSE_NOW or CONVERT_TO_SWING (the latter only valid once per trade, and
        # only if this trade isn't already a swing). No reply, an invalid reply, or the
        # trade already being a swing -> force close via the same booking pattern every
        # other exit above uses, close_type TIME_EXIT.
        if not trade.get("time_stop_checkpoint_sent") and trade.get("expiry_at"):
            try:
                _ts_expiry = datetime.strptime(trade["expiry_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                _ts_expiry = None
            if _ts_expiry and datetime.now(timezone.utc) > _ts_expiry:
                trade["time_stop_checkpoint_sent"] = True  # set BEFORE the call -- guards a slow reply from double-firing
                _ts_hours_allowed = TRADE_TYPE_EXPIRY_HOURS.get(trade.get("trade_type", "day"), 6)
                _ts_hours_open = None
                if trade.get("opened_at"):
                    try:
                        _ts_opened = datetime.strptime(trade["opened_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        _ts_hours_open = (datetime.now(timezone.utc) - _ts_opened).total_seconds() / 3600
                    except Exception:
                        _ts_hours_open = None
                _ts_risk_usd = trade.get("risk_amount_usd", 0)
                _ts_price_r  = round(live_pnl_dollars / _ts_risk_usd, 2) if _ts_risk_usd > 0 else 0.0

                ts_reply = _ask_chev_time_stop(trade, price, _ts_hours_open or 0.0, _ts_hours_allowed, _ts_price_r)
                ts_reply_upper = (ts_reply or "").strip().upper()

                if trade.get("trade_type") != "swing" and ts_reply_upper.startswith("CONVERT_TO_SWING"):
                    _ts_new_expiry = (datetime.now(timezone.utc) + pd.Timedelta(hours=TRADE_TYPE_EXPIRY_HOURS["swing"])).strftime("%Y-%m-%d %H:%M:%S")
                    trade["trade_type"]    = "swing"
                    trade["expiry_at"]     = _ts_new_expiry
                    trade["time_extended"] = True
                    trade["time_stop_checkpoint_sent"] = False  # new expiry event -- allow exactly one more checkpoint if THIS one also expires
                    trade.setdefault("chev_moves", []).append(
                        f"TIME-STOP: CONVERT_TO_SWING @ {datetime.now(timezone.utc).strftime('%H:%M')} (new expiry {_ts_new_expiry})"
                    )
                    try:
                        worksheet.update_cell(trade["row"], 16, "swing")
                        worksheet.update_cell(trade["row"], 17, _ts_new_expiry)
                    except Exception as _e:
                        print(f"[TIME-STOP] Sheet update failed for {trade['symbol']}: {_e}")
                    print(f"[TIME-STOP] {trade['symbol']} CONVERT_TO_SWING — new expiry {_ts_new_expiry}")
                else:
                    _ts_exit_price = exit_candles[-1]["c"] if exit_candles else price
                    _ts_exit_move  = ((_ts_exit_price - trade["entry"]) / trade["entry"]) if is_long else ((trade["entry"] - _ts_exit_price) / trade["entry"])
                    _ts_gross_pnl  = round(trade["position_size_usd"] * _ts_exit_move, 2)
                    ts_exit_pnl, _ts_trade_cost = honest_sim.apply_costs(trade, _ts_gross_pnl, 1.0)
                    ts_outcome     = "WIN" if ts_exit_pnl >= 0 else "LOSS"
                    if ts_outcome == "LOSS":
                        _record_loss_for_cooldown(trade)  # flag for confluence re-entry check
                    balance = get_balance(dashboard_ws)
                    margin_to_return = trade.get("margin_reserved", 0)
                    new_balance = round(balance + margin_to_return + ts_exit_pnl, 2)
                    set_balance(dashboard_ws, new_balance)
                    _cached_balance = new_balance
                    try:
                        worksheet.update(values=[[_ts_exit_price, ts_exit_pnl, ts_outcome, ts_exit_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
                    except Exception as e:
                        print(f"[{datetime.now()}] Sheet update failed for {trade['symbol']}: {e}")
                    print(f"[{datetime.now()}] {trade['symbol']} TIME_EXIT @ {_ts_exit_price}  ${ts_exit_pnl:+.2f} "
                          f"(gross ${_ts_gross_pnl:+.2f} fees ${_ts_trade_cost:.2f})  |  Balance: ${balance:.2f} -> ${new_balance:.2f}")
                    trade_copy = dict(trade)
                    trade_copy["close_type"]   = "TIME_EXIT"
                    trade_copy["trading_cost"] = _ts_trade_cost
                    threading.Thread(target=_do_postmortem,
                                     args=(trade_copy, ts_outcome, ts_exit_pnl, _ts_exit_price),
                                     daemon=True).start()
                    continue

        # ── Chev milestone partial TP — thresholds still power the spot-fallback block below ──
        _risk_usd   = trade.get("risk_amount_usd", 0)
        _orig_tp    = trade.get("original_tp", trade["tp"])
        _orig_tp_rr = abs(_orig_tp - trade["entry"]) / orig_risk if orig_risk > 0 else 0

        # ── Level-health management (replaces R-distance milestone trigger) ────
        # R-distance alone doesn't distinguish a trade grinding into a wall from one
        # about to punch through — this keys off whether the level actually ahead of
        # the trade (TP or an SR zone) is HOLDING, WEAKENING, or BROKEN instead.
        if (
            not trade.get("partial_milestone_done")
            and not trade.get("sip_active")
            and move_pct > 0          # trade is in profit
            and orig_risk > 0
            and _risk_usd > 0
        ):
            _near_level = _check_level_proximity(trade, price)
            if _near_level is not None:
                trade["partial_milestone_done"] = True
                verdict = _near_level["health"]["verdict"]
                label   = _near_level["label"]
                lp      = _near_level["level_price"]
                dpct    = _near_level["dist_pct"]
                _price_r = abs(price - trade["entry"]) / orig_risk

                print(f"[LEVEL HEALTH] {trade['symbol']} {trade['direction'].upper()} "
                      f"— price within {dpct:.2f}% of {label} @ {lp} | verdict={verdict} | {_price_r:.2f}R in profit")

                if verdict == "BROKEN":
                    # Level ahead is broken — price is likely to push through to next target.
                    # Action: move SL to breakeven, let trade run. Do not take profit.
                    if not trade.get("sip_active"):
                        _new_sl = trade["entry"]
                        _is_long_lh = trade["direction"] == "long"
                        _sl_valid = (_new_sl < price) if _is_long_lh else (_new_sl > price)
                        if _sl_valid and trade["sl"] != _new_sl:
                            old_sl = trade["sl"]
                            trade["sl"] = _new_sl
                            trade["sip_active"] = True
                            trade["sip_price"]  = _new_sl
                            try:
                                worksheet.update_cell(trade["row"], 4, _new_sl)
                            except Exception as _e:
                                print(f"[LEVEL HEALTH] Sheet SL update failed: {_e}")
                            print(f"[LEVEL HEALTH] {trade['symbol']} BROKEN level ahead — SL moved to breakeven "
                                  f"({old_sl} → {_new_sl}). Trade running free.")

                elif verdict == "WEAKENING":
                    # Level ahead is softening but not broken.
                    # Action: ask Chev with context. Pass the verdict so Chev can weigh in.
                    # partial_done is NOT set here — Chev's reply decides whether to close anything.
                    threading.Thread(
                        target=lambda t=trade, p=price, r=round(_price_r, 2), v=verdict, lbl=label, lpr=lp:
                            _ask_chev_partial_tp(t, p, r, level_verdict=v, level_label=lbl, level_price=lpr),
                        daemon=True
                    ).start()

                elif verdict == "HOLDING":
                    # Level ahead is strong and likely to reject.
                    # Action: close 50% mechanically, trail SL to breakeven on the runner.
                    if not trade.get("partial_done") and _orig_tp_rr > 1.05:
                        _execute_partial_close(trade, price, fraction=0.50,
                                               close_type="PARTIAL_1R", r_at_milestone=round(_price_r, 2))
                    if not trade.get("sip_active"):
                        _new_sl = trade["entry"]
                        _is_long_lh = trade["direction"] == "long"
                        _sl_valid = (_new_sl < price) if _is_long_lh else (_new_sl > price)
                        if _sl_valid and trade["sl"] != _new_sl:
                            old_sl = trade["sl"]
                            trade["sl"] = _new_sl
                            trade["sip_active"] = True
                            trade["sip_price"]  = _new_sl
                            try:
                                worksheet.update_cell(trade["row"], 4, _new_sl)
                            except Exception as _e:
                                print(f"[LEVEL HEALTH] Sheet SL update failed: {_e}")
                            print(f"[LEVEL HEALTH] {trade['symbol']} HOLDING level ahead — 50% closed, "
                                  f"SL trailed to BE ({old_sl} → {_new_sl}).")

        # ── Partial TP at 1R: auto-close 50% swing fallback (scalp/day use Chev milestone) ───
        # Spot-fallback path only -- when candles are available the candle-true partial
        # above (inside the `else: exit_candles` branch) already handles this.
        if (
            exit_candles is None
            and _risk_usd > 0
            and orig_risk > 0
            and not trade.get("partial_done")       # only once
            and not trade.get("sip_active")         # SIP = risk already managed structurally
            and _orig_tp_rr > 1.05                  # skip if Chev set a tight TP (scalp <1R)
            and live_pnl_dollars >= _risk_usd       # price moved 1R in our favour
        ):
            _gross_partial  = round(live_pnl_dollars * 0.5, 2)
            partial_pnl, _partial_cost = honest_sim.apply_costs(trade, _gross_partial, 0.5)
            balance         = get_balance(dashboard_ws)
            new_balance     = round(balance + partial_pnl, 2)
            set_balance(dashboard_ws, new_balance)
            _cached_balance = new_balance
            trade["position_size_usd"] = round(trade["position_size_usd"] * 0.5, 2)
            trade["partial_done"]      = True
            trade["partial_net_pnl"]   = round(trade.get("partial_net_pnl", 0.0) + partial_pnl, 2)
            print(f"[PARTIAL TP] {trade['symbol']} {trade['direction'].upper()} — 1R hit, 50% closed at ${partial_pnl:+.2f} (gross ${_gross_partial:+.2f} fees ${_partial_cost:.2f}) | Balance: ${balance:.2f} → ${new_balance:.2f}")
            _partial_entry = {
                "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":           trade["symbol"],
                "asset_type":       trade.get("asset_type", "crypto"),
                "direction":        trade["direction"],
                "entry":            trade["entry"],
                "sl":               trade["sl"],
                "tp":               trade["tp"],
                "exit_price":       price,
                "pnl":              partial_pnl,
                "outcome":          "PARTIAL_TP",
                "close_type":       "PARTIAL_1R",
                "tags":             trade.get("tags", ""),
                "duration":         "partial",
                "reasoning":        trade.get("reasoning", ""),
                "analysis":         f"Auto partial close: 50% of {trade['symbol']} locked at 1R (${partial_pnl:+.2f} net, fees ${_partial_cost:.2f}). Remaining 50% continues.",
                "trading_cost":     _partial_cost,
                "r_multiple":       1.0,
                "risk_amount_usd":  _risk_usd,
                "position_size_usd": trade["position_size_usd"],
                "leverage":         trade.get("leverage", 1),
                "setup_grade":      trade.get("setup_grade", ""),
                "session_quality":  trade.get("session_quality", ""),
                "heat_at_entry":    trade.get("heat_at_entry", 0),
                "chev_moves":       [],
                "open_ts":          trade.get("open_ts", ""),
            }
            try:
                _j = _load_journal()
                _j.append(_partial_entry)
                with open(JOURNAL_PATH, "w", encoding="utf-8") as _f:
                    json.dump(_j, _f, indent=2)
            except Exception as _pe:
                print(f"[PARTIAL TP] Journal write failed: {_pe}")

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
                        target=lambda t=trade, p=price: ask_chev_manage_trade(t, p),
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
                    threading.Thread(
                        target=lambda t=trade, p=price: ask_chev_manage_trade(t, p),
                        daemon=True
                    ).start()

        # Ask Chev to manage swing trades when price has moved 0.5R (scalp/day rely on milestones)
        last_trail_price = trade.get("last_trail_price", trade["entry"])
        price_delta = abs(price - last_trail_price)
        if move_pct > 0 and orig_risk > 0 and price_delta >= 0.5 * orig_risk and trade.get("trade_type") == "swing":
            trade["last_trail_price"] = price
            reply = ask_chev_manage_trade(trade, price)
            reply_upper = (reply or "").strip().upper()

            if reply_upper.startswith("CLOSE"):
                close_type   = "CHEV_CLOSE"
                adj_pnl, _trade_cost = honest_sim.apply_costs(trade, live_pnl_dollars, 1.0)
                outcome      = "WIN" if adj_pnl > 0 else "LOSS"
                if outcome == "LOSS":
                    _record_loss_for_cooldown(trade)  # flag for confluence re-entry check
                balance = get_balance(dashboard_ws)
                margin_to_return = trade.get("margin_reserved", 0)
                new_balance = round(balance + margin_to_return + adj_pnl, 2)
                set_balance(dashboard_ws, new_balance)
                _cached_balance = new_balance
                try:
                    worksheet.update(values=[[price, adj_pnl, f"CLOSED ({outcome})", adj_pnl]], range_name=f"K{trade['row']}:N{trade['row']}")
                except Exception as e:
                    print(f"[{datetime.now()}] Sheet update failed on Chev close: {e}")
                print(f"[{datetime.now()}] Chev CLOSE {trade['symbol']} at {price} | ${adj_pnl:+.2f} (gross ${live_pnl_dollars:+.2f} fees ${_trade_cost:.2f}) | Balance: ${new_balance:.2f}")
                trade_copy = dict(trade)
                trade_copy["close_type"]   = close_type
                trade_copy["trading_cost"] = _trade_cost
                threading.Thread(target=_do_postmortem, args=(trade_copy, f"CLOSED ({outcome})", adj_pnl, price), daemon=True).start()
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
                            sl_entry = f"{label} {old_sl} → {new_sl} @ {datetime.now(timezone.utc).strftime('%H:%M')}"
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
                        tp_entry = f"TP {old_tp} → {new_tp} @ {datetime.now(timezone.utc).strftime('%H:%M')}"
                        if move_reason:
                            tp_entry += f" — {move_reason}"
                        trade.setdefault("chev_moves", []).append(tp_entry)
                        print(f"[{datetime.now()}] {trade['symbol']} TP trailed to {new_tp}" + (f" | reason: {move_reason}" if move_reason else ""))

                except (ValueError, IndexError) as e:
                    print(f"[{datetime.now()}] Couldn't parse Chev's TRAIL reply: {reply} — {e}")

        # BOS/CHoCH structural alert — fires a management message if new structure event detected
        _maybe_send_bos_alert(trade, price)

        still_open.append(trade)

    open_trades = still_open


# ============================================================
# THE FLIGHT RECORDER (PHASE 10)
# ============================================================
# Append-only log of every hand-tuned setting this system runs with -- the black-box
# recorder trader logic calls for: you journal your trades, this journals your rules.
# Written ONLY here. Never read by any gate/decision path -- pure observability, so a
# failure here (disk full, locked file, permissions) must never affect trading logic.
SYSTEM_STATE_FILE = os.path.join(CHEV_TOOLS_ROOT, "system_state.jsonl")


def snapshot_tunables():
    """Pure, read-only. Assembles a full snapshot of every hand-tuned setting currently in
    force -- never changes anything. Captures whole risk_gauntlet profile dicts (via the
    existing public risk_gauntlet.get_active_profile() accessor, never the private
    _NORMAL/_EXPLORATION names directly) rather than cherry-picked keys, so a future new
    key in either profile is captured automatically without this function needing an edit."""
    _thr = lambda asset: dict(zip(("score", "max_dist_pct"), _active_thresholds(asset)))
    return {
        "escalation_thresholds": {
            "active": {"crypto": _thr("crypto"), "forex": _thr("forex"), "stock": _thr("stock")},
            "normal": {
                "score": {"crypto": CONFLUENCE_THRESHOLD_CRYPTO, "forex": CONFLUENCE_THRESHOLD_FOREX,
                          "stock": CONFLUENCE_THRESHOLD_STOCK},
                "max_dist_pct": ESCALATION_MAX_DIST_PCT,
            },
            "exploration": {
                "score": {"crypto": EXPLORATION_THRESHOLD_CRYPTO, "forex": EXPLORATION_THRESHOLD_FOREX,
                          "stock": EXPLORATION_THRESHOLD_STOCK},
                "max_dist_pct": EXPLORATION_MAX_DIST_PCT,
            },
        },
        "escalation_tf_floor": dict(ESCALATION_TF_FLOOR),
        "hallucination_guard": {
            "max_entry_dist": HALLUCINATION_MAX_ENTRY_DIST,
            "max_sltp_dist":  HALLUCINATION_MAX_SLTP_DIST,
        },
        "trade_type_expiry_hours": dict(TRADE_TYPE_EXPIRY_HOURS),
        "max_leverage_by_type": {k: dict(v) for k, v in MAX_LEVERAGE_BY_TYPE.items()},
        "risk_gauntlet": {
            "active_profile":      risk_gauntlet.get_active_profile(EXPLORATION_MODE),
            "normal_profile":      risk_gauntlet.get_active_profile(False),
            "exploration_profile": risk_gauntlet.get_active_profile(True),
            "fee_side":                 dict(risk_gauntlet.FEE_SIDE),
            "slippage_side":            dict(risk_gauntlet.SLIPPAGE_SIDE),
            "funding_est_swing":        risk_gauntlet.FUNDING_EST_SWING,
            "ev_safety_margin":         risk_gauntlet.EV_SAFETY_MARGIN,
            "min_samples_for_ev":       risk_gauntlet.MIN_SAMPLES_FOR_EV,
            "tag_winrate_window_trades": risk_gauntlet.TAG_WINRATE_WINDOW_TRADES,
            "tag_winrate_smoothing_k":  risk_gauntlet.TAG_WINRATE_SMOOTHING_K,
        },
    }


def record_system_state(event, source, changed=None):
    """Appends ONE JSON line to system_state.jsonl -- never rewrites the file. A write
    failure (disk full, locked file, permissions) logs a console warning and never raises
    -- this is a pure observability side-channel, never allowed to crash the caller."""
    try:
        record = {
            "ts":               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "event":            event,
            "source":           source,
            "system_era":       SYSTEM_ERA,
            "exploration_mode": EXPLORATION_MODE,
            "snapshot":         snapshot_tunables(),
            "changed":          changed,
        }
        with open(SYSTEM_STATE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[system_state] record_system_state failed ({event}/{source}): {e}")


# =============================================================================
# PHASE 14: TUNABLES.JSON HOT-RELOAD
#
# A whitelisted subset of tunables can be changed by editing C:\ChevTools\tunables.json
# and waiting for the next scan cycle -- no restart. Hardcoded defaults above (e.g.
# CONFLUENCE_THRESHOLD_CRYPTO = 10) remain the fallback whenever the file, or a given key
# inside it, is absent -- the file only ever OVERRIDES, it is never the sole source. Every
# value is validated against TUNABLES_BOUNDS below before being applied; out-of-bounds or
# malformed values are rejected with a console warning and the last-good (previous-cycle,
# or hardcoded-default if never successfully overridden) value stays in force. Every
# APPLIED change (value actually differing from a moment ago) is recorded to the flight
# recorder (record_system_state) with source "file", so system_state.jsonl shows exactly
# when and what changed, same as a dashboard toggle or a restart.
#
# EXPLORATION_MODE itself is deliberately NOT here -- it already has its own toggle path
# (api_strategy_toggle_exploration), and two masters for one flag is a bug factory.
#
# WHITELIST + BOUNDS (dotted JSON path -> (min, max) inclusive, or a fixed allowed-set):
#   escalation_thresholds.normal.score.{crypto,forex,stock}       (1, 20)
#   escalation_thresholds.normal.max_dist_pct                     (0.1, 5.0)
#   escalation_thresholds.exploration.score.{crypto,forex,stock}  (1, 20)
#   escalation_thresholds.exploration.max_dist_pct                (0.1, 5.0)
#   escalation_tf_floor.{crypto,forex,stock}                      must be a key in TF_SECONDS
#   hallucination_max_entry_dist                                  (0.005, 0.10)
#   exploration_profile.min_net_rr.{scalp,day,swing}              (0.5, 5.0)
#   exploration_profile.max_cost_r.{scalp,day,swing}              (0.05, 0.60)
#   exploration_profile.concurrency_cap.{scalp,day,swing}         (1, 5)
# The last three groups are applied via risk_gauntlet.apply_exploration_overrides() (the
# ONLY sanctioned way to mutate risk_gauntlet's private _EXPLORATION dict from here) --
# _NORMAL is never reachable through this file, on purpose (see that function's docstring:
# it's the fixed "return to discipline" baseline and must never drift from a phone tap).
#
# BEHAVIOR NOTE, so this isn't mistaken for a bug later: the mutation this file drives is
# IN-MEMORY ONLY. Every restart resets every value to its hardcoded default above, and the
# very first scan cycle after that restart reapplies tunables.json on top. So you will
# correctly see a "startup" flight-recorder snapshot showing hardcoded defaults, followed
# immediately by a "file" change record showing tunables.json's values being reapplied --
# that's the system working, not double-toggling.
# =============================================================================
TUNABLES_FILE = os.path.join(CHEV_TOOLS_ROOT, "tunables.json")

TUNABLES_BOUNDS = {
    "escalation_thresholds.normal.score.crypto":      (1, 20),
    "escalation_thresholds.normal.score.forex":       (1, 20),
    "escalation_thresholds.normal.score.stock":       (1, 20),
    "escalation_thresholds.normal.max_dist_pct":      (0.1, 5.0),
    "escalation_thresholds.exploration.score.crypto": (1, 20),
    "escalation_thresholds.exploration.score.forex":  (1, 20),
    "escalation_thresholds.exploration.score.stock":  (1, 20),
    "escalation_thresholds.exploration.max_dist_pct": (0.1, 5.0),
    "hallucination_max_entry_dist":                   (0.005, 0.10),
    "exploration_profile.min_net_rr.scalp":           (0.5, 5.0),
    "exploration_profile.min_net_rr.day":             (0.5, 5.0),
    "exploration_profile.min_net_rr.swing":           (0.5, 5.0),
    "exploration_profile.max_cost_r.scalp":           (0.05, 0.60),
    "exploration_profile.max_cost_r.day":             (0.05, 0.60),
    "exploration_profile.max_cost_r.swing":           (0.05, 0.60),
    "exploration_profile.concurrency_cap.scalp":      (1, 5),
    "exploration_profile.concurrency_cap.day":        (1, 5),
    "exploration_profile.concurrency_cap.swing":      (1, 5),
}

_tunables_warned_once = False


def load_tunables():
    """PHASE 14: read TUNABLES_FILE, validate every whitelisted key against
    TUNABLES_BOUNDS (or TF_SECONDS for the TF-floor keys), apply only in-bounds values,
    and record every APPLIED change (value actually differing from a moment ago) to the
    flight recorder with source "file". Called once per scan cycle, at the very top of
    the main loop. File missing -> silently keep defaults (this is the normal pre-tuning
    state, not an error). File corrupt/unreadable, or a value out of bounds/malformed ->
    warn once (not spammed every cycle) and keep the last-good value; never raises.
    """
    global _tunables_warned_once
    global CONFLUENCE_THRESHOLD_CRYPTO, CONFLUENCE_THRESHOLD_FOREX, CONFLUENCE_THRESHOLD_STOCK
    global EXPLORATION_THRESHOLD_CRYPTO, EXPLORATION_THRESHOLD_FOREX, EXPLORATION_THRESHOLD_STOCK
    global ESCALATION_MAX_DIST_PCT, EXPLORATION_MAX_DIST_PCT, HALLUCINATION_MAX_ENTRY_DIST

    try:
        with open(TUNABLES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        if not _tunables_warned_once:
            print(f"[tunables] {TUNABLES_FILE} unreadable/corrupt ({e}) -- keeping last-good values.")
            _tunables_warned_once = True
        return
    if not isinstance(data, dict):
        if not _tunables_warned_once:
            print(f"[tunables] {TUNABLES_FILE} root is not a JSON object -- keeping last-good values.")
            _tunables_warned_once = True
        return
    _tunables_warned_once = False

    applied = {}

    def _num(path, current):
        """Look up dotted `path` in `data`; validate type + TUNABLES_BOUNDS. Returns
        (new_value, True) only when present, valid, in-bounds, AND different from
        `current` -- (None, False) means "leave current alone" for any other reason
        (absent, wrong type, out of bounds, or unchanged)."""
        node = data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None, False
            node = node[part]
        val = node
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            print(f"[tunables] {path}: non-numeric value {val!r} ignored, keeping {current}.")
            return None, False
        lo, hi = TUNABLES_BOUNDS[path]
        if not (lo <= val <= hi):
            print(f"[tunables] {path}: {val} outside [{lo}, {hi}] -- ignored, keeping {current}.")
            return None, False
        if val == current:
            return None, False
        return val, True

    new_val, changed = _num("escalation_thresholds.normal.score.crypto", CONFLUENCE_THRESHOLD_CRYPTO)
    if changed:
        applied["escalation_thresholds.normal.score.crypto"] = {"old": CONFLUENCE_THRESHOLD_CRYPTO, "new": new_val}
        CONFLUENCE_THRESHOLD_CRYPTO = new_val

    new_val, changed = _num("escalation_thresholds.normal.score.forex", CONFLUENCE_THRESHOLD_FOREX)
    if changed:
        applied["escalation_thresholds.normal.score.forex"] = {"old": CONFLUENCE_THRESHOLD_FOREX, "new": new_val}
        CONFLUENCE_THRESHOLD_FOREX = new_val

    new_val, changed = _num("escalation_thresholds.normal.score.stock", CONFLUENCE_THRESHOLD_STOCK)
    if changed:
        applied["escalation_thresholds.normal.score.stock"] = {"old": CONFLUENCE_THRESHOLD_STOCK, "new": new_val}
        CONFLUENCE_THRESHOLD_STOCK = new_val

    new_val, changed = _num("escalation_thresholds.normal.max_dist_pct", ESCALATION_MAX_DIST_PCT)
    if changed:
        applied["escalation_thresholds.normal.max_dist_pct"] = {"old": ESCALATION_MAX_DIST_PCT, "new": new_val}
        ESCALATION_MAX_DIST_PCT = new_val

    new_val, changed = _num("escalation_thresholds.exploration.score.crypto", EXPLORATION_THRESHOLD_CRYPTO)
    if changed:
        applied["escalation_thresholds.exploration.score.crypto"] = {"old": EXPLORATION_THRESHOLD_CRYPTO, "new": new_val}
        EXPLORATION_THRESHOLD_CRYPTO = new_val

    new_val, changed = _num("escalation_thresholds.exploration.score.forex", EXPLORATION_THRESHOLD_FOREX)
    if changed:
        applied["escalation_thresholds.exploration.score.forex"] = {"old": EXPLORATION_THRESHOLD_FOREX, "new": new_val}
        EXPLORATION_THRESHOLD_FOREX = new_val

    new_val, changed = _num("escalation_thresholds.exploration.score.stock", EXPLORATION_THRESHOLD_STOCK)
    if changed:
        applied["escalation_thresholds.exploration.score.stock"] = {"old": EXPLORATION_THRESHOLD_STOCK, "new": new_val}
        EXPLORATION_THRESHOLD_STOCK = new_val

    new_val, changed = _num("escalation_thresholds.exploration.max_dist_pct", EXPLORATION_MAX_DIST_PCT)
    if changed:
        applied["escalation_thresholds.exploration.max_dist_pct"] = {"old": EXPLORATION_MAX_DIST_PCT, "new": new_val}
        EXPLORATION_MAX_DIST_PCT = new_val

    new_val, changed = _num("hallucination_max_entry_dist", HALLUCINATION_MAX_ENTRY_DIST)
    if changed:
        applied["hallucination_max_entry_dist"] = {"old": HALLUCINATION_MAX_ENTRY_DIST, "new": new_val}
        HALLUCINATION_MAX_ENTRY_DIST = new_val

    # -- TF floor: an allowed-values check (must be a key in TF_SECONDS), not a numeric range --
    tf_node = data.get("escalation_tf_floor")
    if isinstance(tf_node, dict):
        for asset in ("crypto", "forex", "stock"):
            if asset not in tf_node:
                continue
            val = tf_node[asset]
            path = f"escalation_tf_floor.{asset}"
            current = ESCALATION_TF_FLOOR.get(asset)
            if val not in TF_SECONDS:
                print(f"[tunables] {path}: {val!r} is not a known timeframe {sorted(TF_SECONDS)} -- "
                      f"ignored, keeping {current}.")
                continue
            if val != current:
                applied[path] = {"old": current, "new": val}
                ESCALATION_TF_FLOOR[asset] = val

    # -- exploration-profile dials, via risk_gauntlet's own sanctioned mutator (never poke
    # risk_gauntlet._EXPLORATION directly from here) --
    ep_node = data.get("exploration_profile")
    if isinstance(ep_node, dict):
        _json_key_of_rg = {"MIN_NET_RR": "min_net_rr", "MAX_COST_R": "max_cost_r",
                            "CONCURRENCY_CAP": "concurrency_cap"}
        overrides = {}
        old_map = {}
        for rg_key, json_key in _json_key_of_rg.items():
            group_node = ep_node.get(json_key)
            if not isinstance(group_node, dict):
                continue
            current_group = risk_gauntlet.get_active_profile(True)[rg_key]
            group_changes, group_olds = {}, {}
            for trade_type in ("scalp", "day", "swing"):
                path = f"exploration_profile.{json_key}.{trade_type}"
                current = current_group.get(trade_type)
                new_val, changed = _num(path, current)
                if changed:
                    group_changes[trade_type] = new_val
                    group_olds[trade_type] = current
            if group_changes:
                overrides[rg_key] = group_changes
                old_map[rg_key] = group_olds
        if overrides:
            really_applied = risk_gauntlet.apply_exploration_overrides(overrides)
            for rg_key, group in really_applied.items():
                json_key = _json_key_of_rg[rg_key]
                for trade_type, new_val in group.items():
                    path = f"exploration_profile.{json_key}.{trade_type}"
                    applied[path] = {"old": old_map[rg_key][trade_type], "new": new_val}

    if applied:
        print(f"[tunables] applied {len(applied)} change(s) from {TUNABLES_FILE}: {sorted(applied.keys())}")
        try:
            record_system_state("tunables_applied", "file", changed=applied)
        except Exception as e:
            print(f"[tunables] record_system_state failed: {e}")


# ============================================================
# MAIN LOOP
# ============================================================

print("Dexter is watching the markets...")

# Flight recorder: one snapshot at every startup, placed before anything below that could
# fail/retry (Sheets connection, etc.) so a crash-loop restart still gets recorded.
record_system_state("startup", "startup")

# Start web server and price feed immediately — website should never wait on Google Sheets
threading.Thread(target=run_web_server, daemon=True).start()
threading.Thread(target=_fast_price_update, daemon=True).start()
threading.Thread(target=_push_ngrok_url, daemon=True).start()
print(f"[{datetime.now()}] Web terminal running at http://localhost:8080")
labeller.start_resolver_daemon(_fetch_candles_for_labeller)
if EXPLORATION_MODE:
    print(f"[{datetime.now()}] EXPLORATION MODE ACTIVE — thresholds crypto={EXPLORATION_THRESHOLD_CRYPTO} "
          f"forex={EXPLORATION_THRESHOLD_FOREX} stock={EXPLORATION_THRESHOLD_STOCK} | max_dist={EXPLORATION_MAX_DIST_PCT}%")
else:
    print(f"[{datetime.now()}] EXPLORATION MODE OFF — normal thresholds in force "
          f"crypto={CONFLUENCE_THRESHOLD_CRYPTO} forex={CONFLUENCE_THRESHOLD_FOREX} stock={CONFLUENCE_THRESHOLD_STOCK} "
          f"| max_dist={ESCALATION_MAX_DIST_PCT}%")

# Connect to Google Sheets with retry — timeout added to client so it fails fast
worksheet = dashboard_ws = jane_worksheet = skip_log_ws = None
for _attempt in range(3):
    try:
        worksheet, dashboard_ws, jane_worksheet, skip_log_ws = connect_to_sheet()
        print(f"[{datetime.now()}] Google Sheets connected.")
        break
    except Exception as _e:
        print(f"[{datetime.now()}] Google Sheets connect failed (attempt {_attempt+1}/3): {_e}")
        if _attempt < 2:
            import time as _time; _time.sleep(5)
if worksheet is None:
    print(f"[{datetime.now()}] WARNING: Running without Google Sheets — trade logging disabled.")

open_trades     = load_state_from_sheet(worksheet) if worksheet else []
_cached_balance = get_balance(dashboard_ws) if dashboard_ws else _cached_balance
print(f"[{datetime.now()}] Loaded {len(open_trades)} open trade(s) from sheet. Balance: ${_cached_balance:.2f}")
_load_jane_balance()
jane_trades = load_jane_trades_from_sheet() if jane_worksheet else []
print(f"[{datetime.now()}] Jane: {len(jane_trades)} open trade(s). Balance: ${jane_balance:.2f}")

# ── Startup SL audit ─────────────────────────────────────────────────────────
# When Dexter restarts after being down, price may have already blown through a
# trade's SL while we were offline.  The normal loop wouldn't catch it until the
# next price tick, which could be minutes away.  Close those trades immediately.
def _startup_sl_audit():
    global open_trades, _cached_balance
    survived = []
    for trade in list(open_trades):
        if trade.get("status") != "OPEN":
            survived.append(trade)
            continue
        try:
            price = get_current_price(trade["symbol"], trade["asset_type"])
        except Exception:
            price = None
        if price is None:
            survived.append(trade)
            continue

        is_long  = trade["direction"] == "long"
        hit_sl   = (price <= trade["sl"]) if is_long else (price >= trade["sl"])
        if not hit_sl:
            survived.append(trade)
            continue

        # SL was breached while offline — close at current price
        exit_price = price
        exit_move  = ((exit_price - trade["entry"]) / trade["entry"]) if is_long \
                     else ((trade["entry"] - exit_price) / trade["entry"])
        _gross_pnl = round(trade["position_size_usd"] * exit_move, 2)
        exit_pnl, _trade_cost = honest_sim.apply_costs(trade, _gross_pnl, 1.0)
        outcome    = "WIN" if exit_pnl >= 0 else "LOSS"

        balance          = get_balance(dashboard_ws)
        margin_to_return = trade.get("margin_reserved", 0)
        new_balance      = round(balance + margin_to_return + exit_pnl, 2)
        set_balance(dashboard_ws, new_balance)
        _cached_balance  = new_balance

        try:
            worksheet.update(
                values=[[exit_price, exit_pnl, outcome, exit_pnl]],
                range_name=f"K{trade['row']}:N{trade['row']}"
            )
        except Exception as e:
            print(f"[Startup SL audit] Sheet update failed for {trade['symbol']}: {e}")

        print(f"[Startup SL audit] {trade['symbol']} SL already breached (price={price} vs SL={trade['sl']}) "
              f"— closing at {exit_price} | PnL ${exit_pnl:+.2f} (gross ${_gross_pnl:+.2f} fees ${_trade_cost:.2f}) | Balance ${balance:.2f} -> ${new_balance:.2f}")
        send_telegram_alert(
            f"⚠️ STARTUP AUDIT: {trade['symbol']} SL breached while offline — closed at {exit_price} | PnL ${exit_pnl:+.2f}"
        )

        trade_copy               = dict(trade)
        trade_copy["close_type"]   = "SL_HIT"
        trade_copy["trading_cost"] = _trade_cost
        threading.Thread(
            target=_do_postmortem,
            args=(trade_copy, outcome, exit_pnl, exit_price),
            daemon=True
        ).start()
        # Don't add to survived — trade is done

    n_closed    = len(open_trades) - len(survived)
    open_trades = survived
    if n_closed:
        print(f"[Startup SL audit] Closed {n_closed} trade(s) that breached SL while Dexter was offline.")

_startup_sl_audit()
# ─────────────────────────────────────────────────────────────────────────────

last_forex_scan        = 0
last_stock_scan        = 0
last_forex_trade_check = 0
last_stock_trade_check = 0
_tf_last_scan: dict    = {}  # (symbol, tf) → unix timestamp of last scan initiation
_breaker_alert_sent_date = ""  # UTC date string; caps the circuit-breaker Telegram alert to once/day

while True:
    now = time.time()

    # PHASE 14: re-read tunables.json once per scan cycle -- hot-reload, no restart needed
    # for anything on the whitelist. Safe no-op every cycle nothing has changed.
    load_tunables()

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
    scan_forex_this_round  = (now - last_forex_scan)  >= FOREX_SCAN_INTERVAL and is_forex_market_open()
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
        _active_thr = _active_thresholds(asset_type)[0]
        min_conf    = max(1, _active_thr - TF_MIN_CONFLUENCE_DISCOUNT.get(primary_tf, 3)) + _session_confluence_bonus(asset_type)
        skip_cool   = TF_SKIP_COOLDOWN.get(primary_tf, _ESCALATION_COOLDOWN)
        post_cool   = TF_POST_COOLDOWN.get(primary_tf, _POST_COOLDOWN)

        if result and result["count"] >= min_conf:
            # Circuit breaker: halts NEW escalations only for the rest of the UTC day.
            # Open trades keep managing normally — see monitor_open_trades, untouched.
            _breaker = honest_sim.breaker_status()
            if _breaker["halted"]:
                print(f"[{datetime.now()}] CIRCUIT BREAKER — {result['symbol']}/{primary_tf}: "
                      f"daily R={_breaker['daily_R']:+.2f} <= {honest_sim.DAILY_R_HALT} — not escalating.")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "circuit_breaker"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (circuit_breaker): {_le}")
                if _breaker_alert_sent_date != _breaker["date"]:
                    _breaker_alert_sent_date = _breaker["date"]
                    try:
                        send_telegram_alert(
                            f"🛑 CIRCUIT BREAKER TRIPPED: daily R = {_breaker['daily_R']:+.2f} "
                            f"(halt threshold {honest_sim.DAILY_R_HALT}). No new trades will be escalated "
                            f"for the rest of the UTC day. Open positions continue to be managed normally."
                        )
                    except Exception as _te:
                        print(f"[Circuit Breaker] Telegram alert failed: {_te}")
                continue

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
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "cooldown"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (cooldown): {_le}")
                continue

            # Hard forex event block: no new trades within 30min of a high-impact release
            _blocked, _block_reason = _forex_event_block(result["symbol"], item["type"])
            if _blocked:
                print(f"[{datetime.now()}] EVENT BLOCK — {result['symbol']}/{primary_tf}: {_block_reason} — no new trades within 30min window.")
                _last_escalated[esc_key] = time.time() + 1800  # re-check in 30min
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "event_block"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (event_block): {_le}")
                continue

            # Regime filter: skip CHOPPY markets — ADX<15 means random noise, no edge
            _r4h = result.get("regime_4h") or {}
            if _r4h.get("regime") == "CHOPPY":
                print(f"[{datetime.now()}] REGIME BLOCK — {result['symbol']}/{primary_tf}: 4H CHOPPY (ADX={_r4h.get('adx')}) — no directional edge, skipping.")
                _last_escalated[esc_key] = time.time() + 1800
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "choppy_regime"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (choppy_regime): {_le}")
                continue

            # ── TRADEABILITY PRE-GATE ──────────────────────────────────────
            # Only escalate setups Chev is actually allowed to approve.
            # Everything blocked here is still shadow-logged so the Examiner
            # can measure whether these gates help or hurt.

            # TF FLOOR: below ESCALATION_TF_FLOOR for this asset class, don't ask Chev at
            # all -- the timeframe itself doesn't leave enough room for cost vs R:R (see
            # comment at ESCALATION_TF_FLOOR's definition). Unknown/unmapped TFs fail open
            # (escalate as normal) rather than silently blocking.
            _floor_tf = ESCALATION_TF_FLOOR.get(asset_type)
            if _floor_tf and primary_tf in TF_SECONDS and _floor_tf in TF_SECONDS \
                    and TF_SECONDS[primary_tf] < TF_SECONDS[_floor_tf]:
                print(f"[{datetime.now()}] TF FLOOR — {result['symbol']}/{primary_tf}: "
                      f"below {asset_type} escalation floor ({_floor_tf}) — not escalated (shadow-logged).")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "tf_below_escalation_floor"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (tf_below_escalation_floor): {_le}")
                continue

            _trade_thr, _trade_max_dist = _active_thresholds(asset_type)
            # REMINDER: weekend crypto threshold bump disabled while this is a paper account
            # (thin weekend books/sweeps only matter once real money + real slippage are on
            # the line). Once a live Binance API key + real funds are wired in, restore:
            #   if not EXPLORATION_MODE and asset_type == "crypto" and time.gmtime().tm_wday >= 5:
            #       _trade_thr += 2   # weekend crypto: thin books, sweep-prone — raise the bar

            if result["count"] < _trade_thr:
                print(f"[{datetime.now()}] TRADEABILITY — {result['symbol']}/{primary_tf}: "
                      f"score={result['count']:.1f} < trade threshold {_trade_thr} — not escalated (shadow-logged).")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "below_trade_threshold"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (below_trade_threshold): {_le}")
                continue

            _sr_pre = result.get("sr_score", 0) or 0
            _vp_pre = result.get("vp_score", 0) or 0
            if _sr_pre < 2 and _vp_pre < 2 and not result.get("fast_anchor_pass"):
                print(f"[{datetime.now()}] STRUCT PRE-GATE — {result['symbol']}/{primary_tf}: "
                      f"sr={_sr_pre:.1f} vp={_vp_pre:.1f} — no structural anchor, not escalated.")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "struct_pregate"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (struct_pregate): {_le}")
                continue

            _dist_pre = result.get("dist_from_level")
            if _dist_pre is not None and _dist_pre > _trade_max_dist and _vp_pre < 2:
                print(f"[{datetime.now()}] DISTANCE — {result['symbol']}/{primary_tf}: "
                      f"{_dist_pre:.2f}% from level (max {_trade_max_dist}%) — waiting for price to reach zone. (vp={_vp_pre:.1f})")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "too_far_from_level"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (too_far_from_level): {_le}")
                continue

            _bull_pts, _bear_pts = _directional_split(result["reasons"])
            _dominant = max(_bull_pts, _bear_pts)
            _minor    = min(_bull_pts, _bear_pts)
            if _minor >= 2.0 and _dominant < CONFLICT_DOMINANCE_RATIO * _minor:
                print(f"[{datetime.now()}] CONFLICT — {result['symbol']}/{primary_tf}: "
                      f"bull={_bull_pts:.1f} bear={_bear_pts:.1f} — contradictory signals, not escalated.")
                try:
                    labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                         chev_meta={"reason": "conflicting_signals"})
                except Exception as _le:
                    print(f"[labeller] record_setup error (conflicting_signals): {_le}")
                continue
            # ── END TRADEABILITY PRE-GATE ──────────────────────────────────

            if result.get("dist_from_level") is not None:
                _d = result["dist_from_level"]
                _dlabel = "AT LEVEL" if _d <= 0.5 else "APPROACHING" if _d <= 2.0 else f"FAR ({_d:.2f}%)"
                dist_str = f" | {_d:.2f}% from level ({_dlabel})"
            else:
                dist_str = ""
            gp_str   = " | ★ GOLDEN POCKET" if result.get("in_golden_pocket") else ""
            print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: score={result['count']:.1f} {result['reasons']}{dist_str}{gp_str} — escalating to Chev")
            balance = get_balance(dashboard_ws)
            _esc_start_ts = time.time()   # PHASE 11: deliberation_secs -- times the whole
                                           # escalation (incl. any retries), not just the first call
            chev_response, _chev_messages = ask_chev_to_judge(result, balance, dashboard_ws, timeout=360)

            parsed = parse_chev_reply(chev_response)
            # Captured before any gate below can null parsed["trade"] -- lets the final
            # POST-with-no-trade branch tell "Chev's reply never had a valid TRADE: line"
            # apart from "it had one, but SCORE/STRUCT/MTF TAX/GEOMETRY rejected it and
            # already printed a specific reason" -- those are not the same failure.
            _trade_originally_parsed = bool(parsed and parsed.get("trade"))

            # PHASE 23: shadow-track every trade proposal the MOMENT it parses as valid --
            # BEFORE any downstream gate (hallucination/score/struct/mtf-tax/geometry/
            # gauntlet) can kill it. Without this, a gate-killed proposal had ZERO Examiner
            # label to ever resolve against (confirmed empirically against real
            # labels_open/closed.jsonl: zero matches at any time tolerance) -- the Gate
            # Scoreboard's "n/a" pills were honest, not a join bug, because the label
            # genuinely never existed. record_setup()'s own is_post check requires the
            # LITERAL string "POST" to use Chev's real entry/direction (see its docstring)
            # rather than a generic zone-proxy, so this uses "POST" too -- record_setup's
            # existing symbol+tf+entry_ref dedup (see its docstring) collapses this into a
            # no-op for the common case where the same trade later locks in for real, so
            # nothing is double-counted there. counterfactual_report.py's build_counterfactual()
            # matches a gate-killed decision to this label by symbol+tf+timestamp only --
            # never by chev_decision -- since a gate-killed attempt IS what a "POST" shadow
            # record looks like when the kill happens downstream of Chev's reply.
            if _trade_originally_parsed:
                try:
                    labeller.record_setup(result, asset_type, "POST",
                                         chev_meta={
                                             "direction":  parsed["trade"].get("direction"),
                                             "entry":      parsed["trade"].get("entry"),
                                             "tags":       parsed["trade"].get("tags"),
                                             "reason":     parsed.get("reasoning"),
                                             "trade_type": parsed["trade"].get("trade_type"),
                                         })
                except Exception as _le:
                    print(f"[labeller] record_setup error (PHASE 23 early POST): {_le}")

            # ── Hallucination guard: catches numbers that cannot belong to this chart ──
            # (a training-era price, or the Chev Prompt's own format example leaking
            # through). Runs immediately after parsing, before every other gate, so no
            # later gate does real math (ATR ratios, R:R) on numbers that were never real.
            if parsed and parsed.get("trade"):
                _h_cur = result.get("current_price")
                if _h_cur:
                    _h_td    = parsed["trade"]
                    _h_entry = float(_h_td.get("entry") or 0)
                    _h_sl    = float(_h_td.get("sl")    or 0)
                    _h_tp    = float(_h_td.get("tp")    or 0)
                    _h_entry_dist = abs(_h_entry - _h_cur) / _h_cur if _h_entry else 0.0
                    _h_sl_dist    = abs(_h_sl    - _h_cur) / _h_cur if _h_sl    else 0.0
                    _h_tp_dist    = abs(_h_tp    - _h_cur) / _h_cur if _h_tp    else 0.0
                    _h_issues = []
                    if _h_entry and _h_entry_dist > HALLUCINATION_MAX_ENTRY_DIST:
                        _h_issues.append(f"entry {_h_entry} is {_h_entry_dist:.1%} from live {_h_cur}")
                    if _h_sl and _h_sl_dist > HALLUCINATION_MAX_SLTP_DIST:
                        _h_issues.append(f"sl {_h_sl} is {_h_sl_dist:.1%} from live {_h_cur}")
                    if _h_tp and _h_tp_dist > HALLUCINATION_MAX_SLTP_DIST:
                        _h_issues.append(f"tp {_h_tp} is {_h_tp_dist:.1%} from live {_h_cur}")

                    if _h_issues:
                        _h_correction = (
                            "HALLUCINATION CHECK — your numbers don't match this chart:\n"
                            + "\n".join(_h_issues)
                            + f"\n\nLive price right now is {_h_cur}. These prices cannot belong to this "
                              f"setup. Re-state the full TRADE: line using real numbers from THIS setup's "
                              f"data (keeping POST: format), or reply SKIP: <reason> if the setup no "
                              f"longer qualifies."
                        )
                        print(f"[{datetime.now()}] HALLUCINATION CHECK — {result['symbol']}: "
                              f"{len(_h_issues)} issue(s). Sending correction to Chev.")
                        _chev_messages.append({"role": "assistant", "content": chev_response})
                        _chev_messages.append({"role": "user",      "content": _h_correction})
                        _h_revised_reply = _call_chev(_chev_messages, timeout=180)
                        _h_ok = False
                        if _h_revised_reply:
                            _h_revised_parsed = parse_chev_reply(_h_revised_reply)
                            if _h_revised_parsed and _h_revised_parsed.get("trade"):
                                _h_rt      = _h_revised_parsed["trade"]
                                _h_r_entry = float(_h_rt.get("entry") or 0)
                                _h_r_sl    = float(_h_rt.get("sl")    or 0)
                                _h_r_tp    = float(_h_rt.get("tp")    or 0)
                                _h_r_entry_dist = abs(_h_r_entry - _h_cur) / _h_cur if _h_r_entry else 1.0
                                _h_r_sl_dist    = abs(_h_r_sl    - _h_cur) / _h_cur if _h_r_sl    else 1.0
                                _h_r_tp_dist    = abs(_h_r_tp    - _h_cur) / _h_cur if _h_r_tp    else 1.0
                                if (_h_r_entry_dist <= HALLUCINATION_MAX_ENTRY_DIST
                                        and _h_r_sl_dist <= HALLUCINATION_MAX_SLTP_DIST
                                        and _h_r_tp_dist <= HALLUCINATION_MAX_SLTP_DIST):
                                    _h_ok = True
                                    parsed = _h_revised_parsed
                                    chev_response = _h_revised_reply
                            elif _h_revised_parsed and _h_revised_parsed.get("post") is not None:
                                # Chev switched to SKIP on revision -- respect it, not a hallucination reject.
                                _h_ok = True
                                parsed = _h_revised_parsed
                                chev_response = _h_revised_reply

                        if not _h_ok:
                            _h_reason = "HALLUCINATED_LEVELS: " + "; ".join(_h_issues)
                            print(f"[{datetime.now()}] HALLUCINATION CHECK — {result['symbol']} rejected after retry: {_h_reason}")
                            _log_chev_decision(
                                result["symbol"], primary_tf, result["count"], result["reasons"],
                                "REJECT", _h_reason, (result.get("regime_4h") or {}).get("regime"),
                                **_trade_metrics_for_log(parsed),
                                deliberation_secs=round(time.time() - _esc_start_ts, 1)
                            )
                            if parsed:
                                parsed["trade"] = None
                            continue

            # Hard confluence score gate — reject below-threshold trades even if Chev said POST.
            # Uses Dexter's mechanically-verified score, not Chev's self-reported tags.
            # Chev cannot inflate the gate by adding unverified tags to the TRADE: line.
            if parsed and parsed.get("trade"):
                _dexter_score = result["count"]
                _tags_str     = parsed["trade"].get("tags", "")
                _threshold, _   = _active_thresholds(item["type"])
                if _dexter_score < _threshold:
                    _gate_reason = f"Dexter score={_dexter_score} < threshold={_threshold} ({item['type']}) | Chev tags={_tags_str}"
                    print(f"[{datetime.now()}] SCORE GATE — {result['symbol']} rejected: {_gate_reason}")
                    _log_chev_decision(
                        result["symbol"], primary_tf, _dexter_score, result["reasons"],
                        "GATE_REJECT", _gate_reason, (result.get("regime_4h") or {}).get("regime"),
                        **_trade_metrics_for_log(parsed),
                        deliberation_secs=round(time.time() - _esc_start_ts, 1)
                    )
                    parsed["trade"] = None

            # Structural prerequisite — must have at least one confirmed structural anchor
            # (SR zone or VP level) before any trade is allowed through.
            # Prevents trades that accumulate score purely from EMAs, BB, and RSI
            # without price being at a real supply/demand level where big money sits.
            if parsed and parsed.get("trade"):
                _sr_pts = result.get("sr_score", 0)
                _vp_pts = result.get("vp_score", 0)
                if _sr_pts < 2 and _vp_pts < 2 and not result.get("fast_anchor_pass"):
                    _struct_msg = (f"No structural anchor — sr_score={_sr_pts:.1f}, vp_score={_vp_pts:.1f}. "
                                   f"Need sr≥2 (confirmed multi-TF SR level) OR vp≥2 (VP POC/VAH/VAL within 0.4%). "
                                   f"Price is mid-range, not at a real level.")
                    print(f"[{datetime.now()}] STRUCT GATE — {result['symbol']} rejected: {_struct_msg}")
                    _log_chev_decision(
                        result["symbol"], primary_tf, _dexter_score, result["reasons"],
                        "STRUCT_REJECT", _struct_msg, (result.get("regime_4h") or {}).get("regime"),
                        **_trade_metrics_for_log(parsed),
                        deliberation_secs=round(time.time() - _esc_start_ts, 1)
                    )
                    parsed["trade"] = None

            # MTF Confidence Tax — counter-trend trades must clear a higher bar.
            # Counter-trend = Chev wants long but 4H is TRENDING_DOWN, or short vs TRENDING_UP.
            # They must score ≥ 1.5× the normal threshold AND have at least one confirmed
            # divergence (not just a forming div).  In a RANGING regime no tax is applied —
            # both directions are equally valid.
            if parsed and parsed.get("trade"):
                _proposed_dir = (parsed["trade"].get("direction") or "").lower()
                _regime_str   = (_r4h.get("regime") or "UNKNOWN")
                _is_counter   = (
                    (_regime_str == "TRENDING_UP"   and _proposed_dir == "short") or
                    (_regime_str == "TRENDING_DOWN" and _proposed_dir == "long")
                )
                if _is_counter:
                    _ct_threshold = _threshold * 1.5
                    _has_conf_div = len(result.get("divergences", [])) > 0
                    _ct_fails     = []
                    if _dexter_score < _ct_threshold:
                        _ct_fails.append(f"score {_dexter_score:.1f} < counter-trend bar {_ct_threshold:.1f}")
                    if not _has_conf_div:
                        _ct_fails.append("no confirmed divergence (forming div is not enough counter-trend)")
                    if _ct_fails:
                        _ct_msg = (f"Counter-trend {_proposed_dir} vs 4H {_regime_str}: "
                                   + " | ".join(_ct_fails))
                        print(f"[{datetime.now()}] MTF TAX — {result['symbol']} rejected: {_ct_msg}")
                        _log_chev_decision(
                            result["symbol"], primary_tf, _dexter_score, result["reasons"],
                            "MTF_TAX_REJECT", _ct_msg, _regime_str,
                            **_trade_metrics_for_log(parsed),
                            deliberation_secs=round(time.time() - _esc_start_ts, 1)
                        )
                        parsed["trade"] = None
                    else:
                        print(f"[{datetime.now()}] MTF TAX — {result['symbol']} counter-trend "
                              f"{_proposed_dir} vs 4H {_regime_str}: APPROVED "
                              f"(score={_dexter_score:.1f} ≥ {_ct_threshold:.1f}, confirmed div present)")

            # ── Trade geometry validation: ATR SL · R:R minimum · position size cap ──
            # Runs after the score gate and MTF tax — only if a trade survived both.
            # Issues are batched into a single retry message using the full conversation
            # context (_chev_messages) so Chev can correct without losing his reasoning.
            if parsed and parsed.get("trade"):
                _asset_type      = item["type"]
                _grade, _max_risk_pct = _setup_grade(result, _asset_type)
                _td              = parsed["trade"]
                _atr             = result.get("atr") or 0
                _entry           = float(_td.get("entry")            or 0)
                _sl              = float(_td.get("sl")               or 0)
                _tp              = float(_td.get("tp")               or 0)
                _pos_size        = float(_td.get("position_size_usd") or 0)
                _lev             = float(_td.get("leverage")         or 1) or 1
                _direction       = (_td.get("direction")             or "").lower()
                _is_long         = _direction == "long"
                _ttype           = (_td.get("trade_type") or result.get("trade_type") or "day").lower()
                _sl_dist         = abs(_entry - _sl) if _entry and _sl else 0
                _geo_issues      = []

                # Shared with risk_gauntlet.py — same profile, same numbers, so this earlier
                # sanity check and the gauntlet's real gate never quietly disagree with each
                # other or drift out of sync with EXPLORATION_MODE (2026-07-05 consolidation).
                _geo_prof = risk_gauntlet.get_active_profile(EXPLORATION_MODE)
                _atr_mult = _geo_prof["ATR_FLOOR"].get(_ttype, _geo_prof["ATR_FLOOR"]["day"])

                # ATR SL check — minimum SL: ATR_FLOOR[trade_type] from the active profile
                if _atr > 0 and _sl_dist > 0 and _entry > 0:
                    _atr_min  = _atr * _atr_mult
                    _sl_ratio = _sl_dist / _atr_min
                    if _sl_ratio < 0.5:   # red zone — hard reject + retry
                        _geo_issues.append(
                            f"SL GEOMETRY REJECT — Your SL ({_sl}) is only {_sl_ratio:.0%} of the minimum "
                            f"for a {_ttype} trade ({_atr_mult}× ATR = {_atr_min:.5f}). "
                            f"A single wick will stop you out before the setup plays. "
                            f"Widen SL to at least {_atr_min:.5f} from entry, change trade_type, or SKIP."
                        )
                    elif _sl_ratio < 1.0:  # yellow zone — warn only
                        print(f"[{datetime.now()}] SL WARN — {result['symbol']}: "
                              f"SL dist {_sl_dist:.5f} is {_sl_ratio:.0%} of ATR minimum "
                              f"({_atr_mult}× ATR = {_atr_min:.5f}) for {_ttype}. May get wicked out.")

                # R:R minimum check — flat MIN_NET_RR floor from the active profile (grade affects
                # position size, not the R:R bar; this pre-check has no tag-win-rate context yet,
                # so it uses the same flat floor the gauntlet falls back to, not the EV-based one)
                _min_rr = _geo_prof["MIN_NET_RR"].get(_ttype, _geo_prof["MIN_NET_RR"]["day"])
                if _sl_dist > 0 and _tp != 0 and _entry > 0:
                    _rr = abs(_tp - _entry) / _sl_dist
                    if _rr < _min_rr:
                        _needed_tp_dist = _sl_dist * _min_rr
                        _suggested_tp   = (_entry + _needed_tp_dist) if _is_long else (_entry - _needed_tp_dist)
                        _geo_issues.append(
                            f"R:R TOO LOW — Your TP gives {_rr:.2f}:1 but minimum for a {_grade}-grade "
                            f"setup is {_min_rr:.1f}:1. At {_min_rr:.1f}:1 your TP needs to reach "
                            f"at least {_suggested_tp:.5f}. "
                            f"Check the SR levels in the brief — is there a structural level near "
                            f"{_suggested_tp:.5f} or beyond? If yes, use it. "
                            f"If no structural level gives {_min_rr:.1f}:1, this setup is not worth taking — SKIP."
                        )

                # Position size cap — max margin = balance × grade_max_risk_pct
                if _pos_size > 0 and balance > 0:
                    _implied_margin = _pos_size / _lev
                    _max_margin     = balance * (_max_risk_pct / 100)
                    if _implied_margin > _max_margin * 1.25:   # 25% tolerance
                        _safe_size = round(_max_margin * _lev, 2)
                        _geo_issues.append(
                            f"POSITION SIZE TOO LARGE — position_size_usd={_pos_size} implies "
                            f"${_implied_margin:.2f} margin at {_lev:.0f}x leverage, "
                            f"but the cap for a {_grade}-grade setup is ${_max_margin:.2f} "
                            f"({_max_risk_pct}% of ${balance:.2f} balance). "
                            f"Revise position_size_usd to {_safe_size} or lower."
                        )

                if _geo_issues:
                    _correction = (
                        "\n\n".join(_geo_issues)
                        + "\n\nPlease correct the issue(s) above and re-state the full TRADE: line "
                          "(keeping POST: format), or reply SKIP: <reason> if the setup no longer qualifies."
                    )
                    print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: "
                          f"{len(_geo_issues)} issue(s). Sending revision request to Chev.")
                    _chev_messages.append({"role": "assistant", "content": chev_response})
                    _chev_messages.append({"role": "user",      "content": _correction})
                    _revised_reply = _call_chev(_chev_messages, timeout=180)
                    _revision_ok = False
                    if _revised_reply:
                        _revised_parsed = parse_chev_reply(_revised_reply)
                        if _revised_parsed and _revised_parsed.get("post") is not None:
                            # Re-check geometry on the revised trade before accepting
                            _rt = (_revised_parsed.get("trade") or {})
                            _r_entry = float(_rt.get("entry") or 0)
                            _r_sl    = float(_rt.get("sl")    or 0)
                            _r_tp    = float(_rt.get("tp")    or 0)
                            _r_dist  = abs(_r_entry - _r_sl) if _r_entry and _r_sl else 0
                            _r_rr    = abs(_r_tp - _r_entry) / _r_dist if _r_dist > 0 else 0
                            _r_atr_dist = abs(_r_entry - _r_sl)
                            _r_atr_ok   = (_r_atr_dist >= _atr * _atr_mult * 0.5
                                           if _atr > 0 else True)
                            if _revised_parsed.get("trade") is None:
                                # Chev chose to SKIP on revision — accept that
                                chev_response = _revised_reply
                                parsed        = _revised_parsed
                                _revision_ok  = True
                                print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: Chev chose SKIP on revision.")
                            elif _r_rr >= _min_rr and _r_atr_ok:
                                chev_response = _revised_reply
                                parsed        = _revised_parsed
                                _revision_ok  = True
                                print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: revision accepted (R:R={_r_rr:.2f}).")
                            else:
                                print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: "
                                      f"revision still fails geometry (R:R={_r_rr:.2f}, ATR_ok={_r_atr_ok}) — BLOCKING trade.")
                        else:
                            print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: "
                                  "revision unparseable — BLOCKING trade.")
                    else:
                        print(f"[{datetime.now()}] GEOMETRY REVIEW — {result['symbol']}: "
                              "no reply to revision request — BLOCKING trade.")
                    if not _revision_ok:
                        _log_chev_decision(
                            result["symbol"], primary_tf, _dexter_score, result["reasons"],
                            "GEOMETRY_REJECT", "; ".join(_geo_issues[:1]), _regime_str,
                            **_trade_metrics_for_log(parsed),
                            deliberation_secs=round(time.time() - _esc_start_ts, 1)
                        )
                        parsed["trade"] = None

            # ── Risk Gauntlet — deterministic sizing + portfolio checks ──────────
            # Runs after all existing gates. On PASS: overwrites Chev's size/leverage/risk_pct
            # with equity-based computed values. On REJECT: logs decision and continues.
            _g_res = None
            if parsed and parsed.get("trade"):
                _g_grade, _ = _setup_grade(result, asset_type)
                _g_live     = result.get("current_price") or 0
                _g_tag_stats = risk_gauntlet.compute_tag_win_rates(
                    _load_journal(),
                    window_trades=risk_gauntlet.TAG_WINRATE_WINDOW_TRADES,
                    smoothing_k=risk_gauntlet.TAG_WINRATE_SMOOTHING_K,
                )
                _g_res = risk_gauntlet.run_gauntlet(
                    parsed["trade"], result, balance, _g_live,
                    asset_type, open_trades, _CRYPTO_CORR_SYMBOLS, _g_grade,
                    tag_stats=_g_tag_stats, exploration_mode=EXPLORATION_MODE
                )
                if _g_res["verdict"] == "REJECT":
                    _g_rc = _g_res["reject_code"]
                    _g_rr = _g_res["reject_reason"]
                    print(f"[{datetime.now()}] GAUNTLET {_g_rc} -- {result['symbol']}: {_g_rr}")
                    if _g_res.get("fixable"):
                        # One retry in the same thread — same pattern as the geometry review
                        _g_fix_msg = (
                            f"RISK GAUNTLET REJECT ({_g_rc}): {_g_rr}\n\n"
                            f"Revise your SL/TP and resend the full POST: block, "
                            f"or reply SKIP: <reason> if the setup no longer qualifies."
                        )
                        _chev_messages.append({"role": "assistant", "content": chev_response})
                        _chev_messages.append({"role": "user",      "content": _g_fix_msg})
                        _g_revised  = _call_chev(_chev_messages, timeout=180)
                        _g_accepted = False
                        if _g_revised:
                            _g_rp = parse_chev_reply(_g_revised)
                            if _g_rp and _g_rp.get("trade"):
                                _g_res2 = risk_gauntlet.run_gauntlet(
                                    _g_rp["trade"], result, balance, _g_live,
                                    asset_type, open_trades, _CRYPTO_CORR_SYMBOLS, _g_grade,
                                    tag_stats=_g_tag_stats, exploration_mode=EXPLORATION_MODE
                                )
                                if _g_res2["verdict"] == "PASS":
                                    chev_response = _g_revised
                                    parsed        = _g_rp
                                    _g_res        = _g_res2
                                    _g_accepted   = True
                                    print(f"[{datetime.now()}] GAUNTLET RETRY -- {result['symbol']}: revision accepted.")
                            elif _g_rp and not _g_rp.get("post"):
                                # Chev chose to SKIP on the retry — let normal flow handle cooldown
                                chev_response = _g_revised
                                parsed        = _g_rp
                                _g_accepted   = True
                                print(f"[{datetime.now()}] GAUNTLET RETRY -- {result['symbol']}: Chev chose SKIP on revision.")
                        if not _g_accepted:
                            _log_chev_decision(
                                result["symbol"], primary_tf, result["count"], result["reasons"],
                                "REJECT", _g_rr, (result.get("regime_4h") or {}).get("regime"),
                                **_trade_metrics_for_log(parsed),
                                cost_r=_g_res.get("cost_r"), ev_advisory_rr=_g_res.get("ev_advisory_rr"),
                                enforced_rr_floor=_g_res.get("enforced_rr_floor"),
                                deliberation_secs=round(time.time() - _esc_start_ts, 1)
                            )
                            _last_escalated[esc_key] = time.time() + skip_cool
                            continue
                    else:
                        _log_chev_decision(
                            result["symbol"], primary_tf, result["count"], result["reasons"],
                            "REJECT", _g_rr, (result.get("regime_4h") or {}).get("regime"),
                            **_trade_metrics_for_log(parsed),
                            cost_r=_g_res.get("cost_r"), ev_advisory_rr=_g_res.get("ev_advisory_rr"),
                            enforced_rr_floor=_g_res.get("enforced_rr_floor"),
                            deliberation_secs=round(time.time() - _esc_start_ts, 1)
                        )
                        _last_escalated[esc_key] = time.time() + skip_cool
                        continue

                if _g_res["verdict"] == "PASS":
                    _sz            = _g_res["sized"]
                    _chev_sz_was   = parsed["trade"].get("position_size_usd", 0)
                    _chev_lev_was  = parsed["trade"].get("leverage", 1)
                    _chev_risk_was = parsed["trade"].get("risk_pct", 0)
                    parsed["trade"]["position_size_usd"] = _sz["position_size_usd"]
                    parsed["trade"]["leverage"]           = _sz["leverage"]
                    parsed["trade"]["risk_pct"]           = round(_sz["risk_pct"], 4)
                    print(f"[{datetime.now()}] GAUNTLET PASS -- {result['symbol']}: "
                          f"size ${_chev_sz_was}->${_sz['position_size_usd']} | "
                          f"lev {_chev_lev_was}x->{_sz['leverage']}x | "
                          f"risk {_chev_risk_was}%->{_sz['risk_pct']}%")

            if parsed is None:
                _last_escalated[esc_key] = time.time() + skip_cool
                if chev_response is not None:
                    print(f"[{datetime.now()}] Chev's reply didn't match POST:/SKIP: format — skipping.")
                    _log_chev_decision(
                        result["symbol"], primary_tf, result["count"], result["reasons"],
                        "FORMAT_ERROR", (chev_response or "")[:200],
                        (result.get("regime_4h") or {}).get("regime"),
                        deliberation_secs=round(time.time() - _esc_start_ts, 1)
                    )
            elif parsed["post"]:
                if parsed.get("trade"):
                    _t = parsed["trade"]
                    tg_msg = f"{result['symbol']} {_t.get('direction','').upper()} entry {_fmt_p(_t.get('entry',0))} · SL {_fmt_p(_t.get('sl',0))} · TP {_fmt_p(_t.get('tp',0))}"
                    send_telegram_alert(tg_msg)
                    print(f"[{datetime.now()}] Posted to Telegram: {tg_msg}")
                if parsed.get("trade"):
                    td = parsed["trade"]
                    conf_prices = _build_confluence_prices(td)
                    new_trade = log_new_trade(worksheet, dashboard_ws, result["symbol"], item["type"], parsed["trade"], result["current_price"], confluence_prices=conf_prices, primary_tf=result.get("primary_tf"), regime_at_proposal=(result.get("regime_4h") or {}).get("regime"))
                    new_trade["reasoning"]      = parsed["trade"].get("reasoning") or ""
                    new_trade["structure_4h"]  = parsed.get("structure_4h", "")
                    new_trade["invalidation"]  = parsed.get("invalidation", "")
                    new_trade["confirmation"]  = parsed.get("confirmation", "")
                    new_trade["structural_read"] = parsed.get("structural_read", "")
                    # Forensic audit trail: the full back-and-forth that produced this trade --
                    # the assembled prompt, every correction (hallucination/geometry/gauntlet
                    # fixable-reject), and Chev's reply at each step, ending with the final
                    # accepted reply. Carried through to the journal at close time so a future
                    # investigation doesn't have to reconstruct this from scratch again.
                    new_trade["chev_conversation"] = _chev_messages + [{"role": "assistant", "content": chev_response}]
                    new_trade["opened_at"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    new_trade["original_sl"]   = new_trade.get("sl", 0)
                    new_trade["original_tp"]   = new_trade.get("tp", 0)
                    new_trade["sl_original"]   = new_trade.get("sl", 0)
                    new_trade["tp_original"]   = new_trade.get("tp", 0)
                    new_trade["primary_tf"]    = primary_tf
                    # risk_amount_usd: use gauntlet's equity-based value when available
                    # (makes the 1R partial-TP trigger exact); fallback for any edge path.
                    if _g_res and _g_res["verdict"] == "PASS":
                        _sz = _g_res["sized"]
                        new_trade["risk_amount_usd"]      = _sz["risk_amount_usd"]
                        new_trade["chev_wanted_size"]     = _sz["chev_wanted_size"]
                        new_trade["chev_wanted_leverage"] = _sz["chev_wanted_leverage"]
                        new_trade["gauntlet_notes"]       = _sz["notes"]
                    else:
                        _bal_at_open = get_balance(dashboard_ws) or balance
                        new_trade["risk_amount_usd"] = round((trade.get("risk_pct") or 1.0) / 100 * _bal_at_open, 2)
                    new_trade["atr_at_entry"]    = result.get("atr", 0)
                    _g, _      = _setup_grade(result, asset_type)
                    _, _sq     = _session_grade(asset_type)
                    _open_risk = [t for t in open_trades if t.get("status") == "OPEN"]
                    _heat      = round(sum(t.get("risk_pct", 0) for t in _open_risk), 1)
                    new_trade["setup_grade"]     = _g
                    new_trade["session_quality"] = _sq
                    new_trade["heat_at_entry"]   = _heat
                    # Persist metadata into the sheet's conf_json cell (col 18) so it survives restarts
                    try:
                        _gauntlet_meta = {}
                        if _g_res and _g_res["verdict"] == "PASS":
                            _sz = _g_res["sized"]
                            _gauntlet_meta = {
                                "chev_wanted_size":     _sz["chev_wanted_size"],
                                "chev_wanted_leverage": _sz["chev_wanted_leverage"],
                                "chev_wanted_risk_pct": _sz["chev_wanted_risk_pct"],
                                "gauntlet_notes":       _sz["notes"],
                            }
                        _meta_json = json.dumps({
                            "prices":          conf_prices,
                            "setup_grade":     _g,
                            "session_quality": _sq,
                            "heat_at_entry":   _heat,
                            "reasoning":       new_trade["reasoning"],
                            "primary_tf":      primary_tf,
                            **_gauntlet_meta,
                        })
                        worksheet.update_cell(new_trade["row"], 18, _meta_json)
                    except Exception as _me:
                        print(f"[Dexter] Metadata sheet write failed: {_me}")
                    if new_trade.get("status") == "OPEN":
                        margin = round(new_trade.get("position_size_usd", 0) / max(new_trade.get("leverage", 1), 1), 2)
                        new_trade["margin_reserved"] = margin
                        bal_before = get_balance(dashboard_ws)
                        set_balance(dashboard_ws, round(bal_before - margin, 2))
                        _cached_balance = round(bal_before - margin, 2)
                        print(f"[Dexter] {new_trade['symbol']} OPEN ({primary_tf} {result.get('trade_type','day')}) | Margin: ${margin:.2f} | Balance: ${bal_before:.2f} -> ${_cached_balance:.2f}")
                    open_trades.append(new_trade)
                    # Snapshot market state at entry for "then vs now" management context
                    try:
                        _snap_brief = _build_management_brief(
                            new_trade["symbol"], item["type"], primary_tf,
                            new_trade["direction"], new_trade["entry"], new_trade["tp"]
                        )
                        new_trade["entry_brief_snapshot"] = _snap_brief
                        new_trade["entry_snapshot_time"]  = new_trade.get("opened_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
                    except Exception as _se:
                        print(f"[Entry snapshot] Failed for {new_trade['symbol']}: {_se}")
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
                        (result.get("regime_4h") or {}).get("regime"),
                        detail=parsed.get("reasoning", ""),
                        **_trade_metrics_for_log(parsed),
                        cost_r=(_g_res.get("cost_r") if _g_res else None),
                        ev_advisory_rr=(_g_res.get("ev_advisory_rr") if _g_res else None),
                        enforced_rr_floor=(_g_res.get("enforced_rr_floor") if _g_res else None),
                        deliberation_secs=round(time.time() - _esc_start_ts, 1)
                    )
                    try:
                        _td = parsed.get("trade") or {}
                        labeller.record_setup(
                            result, asset_type, "POST",
                            chev_meta={
                                "direction": _td.get("direction"),
                                "entry":     _td.get("entry"),
                                "tags":      _td.get("tags"),
                                "reason":    parsed.get("reasoning") or parsed.get("skip_reason"),
                                "trade_type": _td.get("trade_type"),
                            }
                        )
                    except Exception as _le:
                        print(f"[labeller] record_setup error (POST): {_le}")
                else:
                    if _trade_originally_parsed:
                        print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: POST rejected upstream "
                              f"(see gate message above) — not logged as a new trade.")
                    else:
                        print(f"[{datetime.now()}] Chev posted but TRADE: line missing/invalid.")
                _last_escalated[esc_key] = time.time() + post_cool
            else:
                _last_escalated[esc_key] = time.time() + skip_cool
                _skip_reason = parsed.get("skip_reason", "no reason captured") if parsed else "no reason captured"
                print(f"[{datetime.now()}] Chev skipped {result['symbol']}/{primary_tf}: {_skip_reason}")
                _skip_reasoning = (parsed.get("reasoning", "") if parsed else "")
                _skip_missing   = (parsed.get("what_was_missing", "") if parsed else "")
                _skip_detail    = (_skip_reasoning + (f"\n\nWhat was missing: {_skip_missing}" if _skip_missing else "")).strip()
                _log_chev_decision(
                    result["symbol"], primary_tf, result["count"], result["reasons"],
                    "SKIP", _skip_reason,
                    (result.get("regime_4h") or {}).get("regime"),
                    detail=_skip_detail
                )
                try:
                    labeller.record_setup(result, asset_type, "SKIP",
                                         chev_meta={"reason": _skip_reason})
                except Exception as _le:
                    print(f"[labeller] record_setup error (SKIP): {_le}")
        elif result:
            print(f"[{datetime.now()}] {result['symbol']}/{primary_tf}: score={result['count']:.1f} — below threshold ({min_conf})")
            try:
                labeller.record_setup(result, asset_type, "NOT_ESCALATED",
                                     chev_meta={"reason": "below_threshold"})
            except Exception as _le:
                print(f"[labeller] record_setup error (below_threshold): {_le}")

    _last_scan_completed_ts = time.time()   # PHASE 12: this iteration ran to completion
    time.sleep(CHECK_INTERVAL_SECONDS)