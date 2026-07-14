"""
Context audit script — measures escalation-message size WITHOUT importing dexter.py
(dexter.py starts a live bot on import; this script only reads plain text files and
reproduces the static message template as inline Python string literals copied from
dexter.py's ask_chev_to_judge()).

Reproduces _build_rich_market_brief() section-by-section in "PHASE 2b TASK 1"
(below) against synthetic worst-case data, so the real post-cut brief (~4.3K
tokens) is measured — not guessed from the pre-cut docstring's "~15-25K". This
script measures everything ELSE that gets concatenated around it, then adds the
reproduced brief to print an authoritative COMBINED TOTAL vs the 14k gate.

Updated for Phase 2: escalations now go to the dedicated chev-escalation model
(Phase 1), so the relevant "system prompt" for this budget is
prompts/chev_escalation_prompt.txt, NOT the chat-Chev "Chev Prompt" file. The msg
template below mirrors dexter.py's ask_chev_to_judge AFTER the Phase 2 edits:
tools block removed, output-format block removed (both copies), front section
reordered, journal trimmed to 3 same-asset one-line entries.
"""

import os
import sys

from engines import (compute_invalidation_candidates, format_invalidation_candidates_for_chev,
                     compute_validation_candidates, format_validation_candidates_for_chev)
from ray_registry import RayRecord, format_ray_block_for_chev

# Make stdout encoding-safe: the reproduced brief contains non-ASCII chars (⚠, →,
# ★, box-drawing). On a non-UTF-8 console or when redirected to a file with the
# ANSI codepage, printing those would raise UnicodeEncodeError and abort the run
# AFTER the useful totals — so force UTF-8 output (errors replaced, never fatal).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CHARS_PER_TOKEN = 3.5  # rough estimate used throughout this audit, per Phase 0 spec

def est_tokens(text):
    return len(text) / CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# 1) System prompt — the ESCALATION model's prompt (Phase 1), not chat Chev's
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_PATH = r"C:\ChevTools\prompts\chev_escalation_prompt.txt"
with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8", errors="replace") as f:
    system_prompt_text = f.read()

# ---------------------------------------------------------------------------
# 2) Playbook file (same paths as PLAYBOOK_PATHS in dexter.py)
# ---------------------------------------------------------------------------
PLAYBOOK_PATHS = {
    "forex":  r"C:\ChevTools\chev_playbook_forex.txt",
    "crypto": r"C:\ChevTools\chev_playbook_crypto.txt",
    "stocks": r"C:\ChevTools\chev_playbook_stocks.txt",
}
playbook_texts = {}
for asset, path in PLAYBOOK_PATHS.items():
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        playbook_texts[asset] = f.read()

# Use crypto playbook as the representative case (largest of the three on disk).
asset_type = "crypto"
playbook_text = playbook_texts[asset_type]

# ---------------------------------------------------------------------------
# 3) Sample journal context — Phase 2 trim: 3 same-asset entries, ONE line each
#    (symbol, direction, outcome, pnl, tags). Mirrors dexter.py:7850-7854.
# ---------------------------------------------------------------------------
sample_journal_entries = [
    {"symbol": "BTCUSDT", "direction": "long",  "outcome": "WIN",  "pnl": 42.50,  "tags": "sr_4h,fib_1h,rsi_1h"},
    {"symbol": "BTCUSDT", "direction": "short", "outcome": "LOSS", "pnl": -18.00, "tags": "bb_1h,sr_1h"},
    {"symbol": "BTCUSDT", "direction": "long",  "outcome": "WIN",  "pnl": 65.10,  "tags": "vp=3,sr_4h"},
]
journal_lines = "\n".join([
    f"• {e['symbol']} {e['direction'].upper()} → {e['outcome']} (${e['pnl']:+.2f}) | tags: {e.get('tags','none')}"
    for e in sample_journal_entries
])

# ---------------------------------------------------------------------------
# 4) context_prefix — mirrors dexter.py's ask_chev_to_judge AFTER Phase 2:
#    playbook/journal no longer live here (moved into msg, sections f/g below).
#    Still excludes _build_rich_market_brief() (measured separately, see top).
# ---------------------------------------------------------------------------
symbol          = "BTCUSDT"
confluence_zone = 61320.5
primary_tf      = "1h"
trade_type      = "day"
session_label   = "London/NY overlap"
session_quality = "PEAK"
session_note    = "PEAK volume window — cleanest price action and execution of the day."
dist_pct        = 0.42
grade           = "A"
suggested_risk  = 1.5
min_score       = 4
balance         = 1000.00
heat_context    = "Open heat: 1.2% of account across 1 position. Room for 1-2 more at current sizing."

context_prefix = ""
context_prefix += "=== YOUR CURRENT OPEN POSITIONS ===\n"
context_prefix += (
    "  OPEN  ETHUSDT LONG entry=3412.5 SL=3380.0 TP=3510.0  R:R=3.0\n"
    "Before posting a new trade, consider: does this add correlated exposure to an existing position?\n"
    "If you already have a LONG on EUR/USD, adding a LONG on GBP/USD doubles your USD-short risk.\n\n"
)
context_prefix += (
    "=== DEXTER'S DETECTED CONFLUENCE ===\n"
    f"Primary timeframe: {primary_tf.upper()}  |  Expected trade type: {trade_type}\n"
    f"Active session: {session_label} — {session_note}\n"
    f"Distance from confluence level: {dist_pct:.2f}%\n"
)

# [[ _build_rich_market_brief() candle/volume/SR dump is reproduced section-by-section
#    in "PHASE 2b TASK 1" below (post-cut worst-case ~4.3K tokens), NOT guessed from the
#    pre-cut docstring's 15-25K estimate. ]]

# ---------------------------------------------------------------------------
# 5) Small dynamic blocks that get spliced into msg — representative sample
#    text sized like real output (see dexter.py for the generators)
# ---------------------------------------------------------------------------
direction_hint = (
    f"Dexter: confluence zone ({confluence_zone:.5f}) is at or within 1.5% of a SUPPORT level (61180.00000). "
    f"This is a potential LONG area — but confirm with RSI, divergence, and trend context. "
    f"It could also be a short setup if price is rejecting downward from a resistance-turned-support flip. "
    f"Your job: read the full structure and decide — do NOT assume direction from the level alone."
)

counter_trend_warning = ""  # only present on counter-trend setups; empty in this representative case

regime_context = (
    "MARKET REGIME:\n"
    "  4H: TRENDING_UP (ADX=28.4, +DI=31.2, -DI=14.7)\n"
    "  LONG is WITH the 4H trend. SHORT is counter-trend — requires significantly stronger confluence to justify.\n\n"
)

rsi_block = (
    "RSI LEVEL SIGNALS:\n"
    "  1H RSI at 58.2 — neutral, no overbought/oversold signal\n"
    "  4H RSI at 61.4 — mild bullish momentum, no divergence flagged\n\n"
)

pattern_block = (
    "CHART PATTERN ENGINE — GEOMETRY ANALYSIS:\n"
    "  ASCENDING_TRIANGLE | bias=LONG | conf=62% | status=FORMING\n"
    "  Geometry: upper_slope=0.02 lower_slope=0.31 compression=0.44 parallelism=0.81 converging=True parallel=False impulse=True impulse_atr=1.8\n\n"
)

geometry_block = (
    "EXECUTABLE GEOMETRY — hard limits the Risk Gauntlet will enforce on any POST:\n"
    "  scalp: min SL 0.35% (ATR floor) | min R:R 1.20:1 (floor) | min TP 0.91% from entry\n"
    "  day: min SL 0.55% (ATR floor) | min R:R 1.30:1 (floor) | min TP 1.50% from entry\n"
    "  swing: min SL 1.10% (cost floor) | min R:R 1.50:1 (floor) | min TP 2.75% from entry\n"
    "If no real structural level exists at or beyond the minimum SL distance for any trade_type, "
    "that is a VALID SKIP — write exactly that, citing these numbers.\n\n"
)

playbook_block = f"=== YOUR TRADING PLAYBOOK ===\n{playbook_text}\n\n" if playbook_text else ""
journal_block  = f"=== RELEVANT RECENT TRADES (context only) ===\n{journal_lines}\n\n" if journal_lines else ""

# ---------------------------------------------------------------------------
# 5b) Invalidation candidates (Phase 3) — representative golden-pocket long,
#     same shape as the real dexter.py wiring in ask_chev_to_judge.
# ---------------------------------------------------------------------------
_ic_candidates = compute_invalidation_candidates(
    direction="long",
    entry_price=confluence_zone,
    hypothesis_type="golden_pocket",
    noise_floor_distance=0.55 * (confluence_zone * 0.008),  # ATR_FLOOR[day]=0.55 (exploration) * sample ATR
    fib_anchor_high=63500.0,
    fib_anchor_low=59800.0,
    sr_levels=[{"price": 59200.0, "kind": "support"}, {"price": 64100.0, "kind": "resistance"}],
    val=59600.0,
    vah=63900.0,
)
invalidation_block = format_invalidation_candidates_for_chev(_ic_candidates)

# 5c) Validation candidates (reward-side mirror, Phase 3b) — same golden-pocket long.
_vc_candidates = compute_validation_candidates(
    direction="long",
    entry_price=confluence_zone,
    target_price=64100.0,
    structural_rr=2.0,
    reward_profile="AT_BALANCE_EXTREME",
    expected_trigger="15m close back above 61350 (POC reclaim)",
    auction_extreme=65200.0,
    risk_distance=confluence_zone - 59800.0,  # entry - invalidation (fib_anchor_low)
)
validation_block = format_validation_candidates_for_chev(_vc_candidates)

# 5d) Trendline Ray block (Phase R4) — worst realistic case: 2 live rays
# (upper+lower, opposite sides, slopes within SLOPE_MATCH_TOL so the channel
# line also renders) + 2 crossings, one per ray, each level chosen to sit
# within that ray's own horizon so both crossing lines actually render.
# Same synthetic numbers as ray_registry.py's own self-test worst-case check
# (12b), reproduced here rather than imported so this audit measures the
# real formatter's real output, not a hand-typed guess at its size.
_ray_T0 = 1_700_000_000
_ray_upper = RayRecord(id="au", symbol="BTCUSDT", timeframe="15m", side="upper",
                       slope_raw=-0.5, slope_norm=-0.05, anchor_ts=_ray_T0, value_at_anchor=61500.0,
                       born_ts=_ray_T0, last_seen_ts=_ray_T0, respect_count=4, wick_rejection_count=1,
                       lifetime_span_bars=44)
_ray_lower = RayRecord(id="al", symbol="BTCUSDT", timeframe="15m", side="lower",
                       slope_raw=0.4, slope_norm=0.04, anchor_ts=_ray_T0, value_at_anchor=61200.0,
                       born_ts=_ray_T0, last_seen_ts=_ray_T0, respect_count=3, wick_rejection_count=2,
                       lifetime_span_bars=40)
_ray_levels = [(61495.0, "Fib 61.8% (golden pocket)"), (61203.2, "VP POC 4h")]
ray_block = format_ray_block_for_chev(
    [_ray_upper, _ray_lower], current_price=61350.0, timeframe="15m", levels=_ray_levels)

exploration_note = (
    "EXPLORATION MODE — DATA-COLLECTION PHASE (paper account):\n"
    "  This is a deliberate data-collection phase. Losses are tuition here — silence teaches nothing.\n"
    "  If this setup has a nameable entry, a structural stop, and a target, your DEFAULT is POST at the\n"
    "  suggested grade-based risk above. SKIP is still allowed, but must name ONE concrete broken element:\n"
    "  no invalidation level available, direction fights the 4H trend without meeting the counter-trend\n"
    "  requirements, or price nowhere near the zone. Never SKIP on general caution, 'not enough confluence',\n"
    "  a letter grade alone, or maturity/participation figures alone — those are sizing inputs in this phase,\n"
    "  not vetoes.\n\n"
)

result_count   = 3
result_reasons = ["sr_4h", "fib_1h", "rsi_1h"]

# ---------------------------------------------------------------------------
# 6) msg — copied verbatim from dexter.py's ask_chev_to_judge AFTER Phase 2:
#    - tools block DELETED
#    - output-format block DELETED (both copies)
#    - front section reordered: greeting+confluence(a), regime+counter-trend(b),
#      pattern_block(c), [Phase 3 slot](d), geometry_block(e), playbook(f),
#      journal(g), closing line(h) -- then everything else, unchanged, after.
# ---------------------------------------------------------------------------
msg = (
    f"Hey Chev, Dexter here with REAL computed numbers. I've given you the full market data above — candles, volume, all levels. Study it yourself and make your own read.\n\n"
    f"I've detected a confluence zone at {confluence_zone:.5f} with {result_count} factor(s) aligning: {', '.join(result_reasons)}.\n"
    f"{direction_hint}\n\n"
    f"{counter_trend_warning}"
    f"{regime_context}"
    f"{pattern_block}"
    f"{invalidation_block}"
    f"{validation_block}"
    f"{ray_block}"
    f"{geometry_block}"
    f"{playbook_block}"
    f"{journal_block}"
    f"Decide now. POST or SKIP, format per your instructions.\n\n"
    f"{rsi_block}"
    f"\n\n"
    f"IMPORTANT — you have TWO ways to enter a trade, not one:\n\n"
    f"  A) Price is already AT or very close to the confluence zone (within ~0.3%):\n"
    f"     -> Enter at market. Use trade_type=scalp/day/swing as appropriate.\n\n"
    f"  B) Price has NOT reached the confluence zone yet — it is above (for a long) or below (for a short):\n"
    f"     -> Do NOT skip just because price isn't there yet.\n"
    f"     -> Set a PENDING order: entry=<confluence level>, trade_type=day or swing.\n"
    f"     -> Dexter will watch and open the trade automatically when price arrives.\n"
    f"     -> This is the correct response when a level looks valid but price needs to come to you.\n\n"
    f"Only SKIP if the setup itself is genuinely weak — wrong trend, poor confluence quality, no clear invalidation level.\n"
    f"Do NOT skip simply because price hasn't arrived at the level yet.\n\n"
    f"ABSOLUTE RULES — trades that break these are REJECTED and wasted:\n"
    f"  LONG:  sl MUST be a number LOWER than entry  (e.g. entry=1.0445, sl=1.038 [ok] | sl=1.047 [WRONG])\n"
    f"  SHORT: sl MUST be a number HIGHER than entry (e.g. entry=1.0445, sl=1.055 [ok] | sl=1.040 [WRONG])\n"
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
    f"  Score {result_count} vs threshold {min_score} | "
    f"Regime: TRENDING_UP | "
    f"Session: {session_quality}\n"
    f"  Grade scale: B -> 0.75%  |  A -> 1.5%  |  A+ -> 2.5%\n"
    f"  This is a data-backed suggestion. Override it if you have stronger conviction — or size down "
    f"if heat and correlation concerns apply. The market only gives you clean A+ setups rarely — "
    f"when it does, don't treat it like a B.\n\n"
    f"Crypto leverage max 10x (swing: 5x). Forex/stock max 5x (swing: 2x). "
    f"Only push leverage on A+ setups in peak session. "
    f"trade_type = scalp (~2h expiry), day (~6h), swing (~48h).\n"
    f"{exploration_note}"
    f"Reminder: first word of your reply = POST or SKIP. No exceptions."
)

escalation_message = context_prefix + msg

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
sys_chars, sys_tok = len(system_prompt_text), est_tokens(system_prompt_text)
esc_chars, esc_tok = len(escalation_message), est_tokens(escalation_message)

# NOTE: the market brief is NOT estimated from a stale docstring here. It is
# reproduced section-by-section in "PHASE 2b TASK 1" (below); its real post-cut
# worst-case (~4.3K tokens) is added to sys_tok + esc_tok there to print the
# authoritative COMBINED TOTAL vs the 14k gate.

print("=" * 70)
print("CONTEXT AUDIT — POST-PHASE-2")
print("=" * 70)
print(f"\n[Escalation system prompt]  {SYSTEM_PROMPT_PATH}")
print(f"  chars={sys_chars:,}  est_tokens={sys_tok:,.0f}")

print(f"\n[Playbook files on disk]")
for asset, text in playbook_texts.items():
    print(f"  {asset:8s} chars={len(text):,}  est_tokens={est_tokens(text):,.0f}  ({PLAYBOOK_PATHS[asset]})")
print(f"  representative playbook used below: '{asset_type}'")

print(f"\n[Escalation message]  context_prefix + msg, EXCLUDING _build_rich_market_brief()")
print(f"  chars={esc_chars:,}  est_tokens={esc_tok:,.0f}")
print(f"  (of which playbook={len(playbook_text):,} chars, sample journal={len(journal_lines):,} chars — 3 entries, 1 line each)")

print(f"\n[INVALIDATION CANDIDATES block (Phase 3) — sample: LONG golden-pocket setup]")
print(invalidation_block.rstrip())

print(f"\n[VALIDATION CANDIDATES block (Phase 3b) — sample: LONG golden-pocket setup]")
print(validation_block.rstrip())

print(f"\n[_build_rich_market_brief() — reproduced section-by-section in 'PHASE 2b TASK 1' below]")
print(f"  Real post-cut worst-case (~4.3K tokens) is measured there and added to the")
print(f"  system prompt + escalation message to print the authoritative COMBINED TOTAL.")
print(f"  (The pre-cut docstring's 15-25K estimate is obsolete after the Task 2 cuts.)")

print("\n" + "=" * 70)
print("NOTE: the authoritative token-budget verdict (system + wrapper + brief vs the")
print("14k gate) is printed at the END of 'PHASE 2b TASK 1', after the brief is")
print("reproduced. The 'everything else' block still in msg (RSI block, reasoning")
print("framework, entry modes, absolute rules, SIP rules, confluence-score note,")
print("account/risk status, setup grade, leverage rules, exploration note, plus the")
print("invalidation + validation candidate blocks) is itemized in the per-section")
print("breakdown below.")
print("=" * 70)

# ---------------------------------------------------------------------------
# Section-by-section token inventory of "everything else" (untouched in Phase 2)
# so cuts can be proposed with their real cost, one at a time.
# ---------------------------------------------------------------------------
_sections = {
    "RSI block (sample)":                                  rsi_block,
    "Reasoning framework ('Your job: decide...' 5 Qs)":     msg.split("Your job: decide")[1].split("IMPORTANT — you have TWO")[0] if "Your job: decide" in msg else "",
    "Entry modes A/B (first explanation)":                  msg.split("IMPORTANT — you have TWO")[1].split("ABSOLUTE RULES")[0] if "IMPORTANT — you have TWO" in msg else "",
    "Banned-skip-language (removed from wrapper — see SKIP DISCIPLINE in prompt)": "0",
    "Absolute rules (SL/TP direction)":                      msg.split("ABSOLUTE RULES")[1].split("ENTRY MODES")[0] if "ABSOLUTE RULES" in msg else "",
    "Entry modes A/B (restated)":                            msg.split("ENTRY MODES —")[1].split("SIP —")[0] if "ENTRY MODES —" in msg else "",
    "SIP rules":                                              msg.split("SIP —")[1].split("CONFLUENCE SCORE")[0] if "SIP —" in msg else "",
    "Confluence score note":                                 msg.split("CONFLUENCE SCORE")[1].split("ACCOUNT & RISK STATUS")[0] if "CONFLUENCE SCORE" in msg else "",
    "Account & risk status + setup grade + leverage":       msg.split("ACCOUNT & RISK STATUS")[1] if "ACCOUNT & RISK STATUS" in msg else "",
}
print("\nSECTION-BY-SECTION INVENTORY (candidates for a future, sign-off'd diet pass):")
_sections["Validation candidates block (reward-side mirror, Phase 3b)"] = validation_block
_sections["Trendline Ray block (Phase R4, escalation, worst case)"] = ray_block
for name, text in _sections.items():
    print(f"  {name:55s} chars={len(text):6,}  est_tokens={est_tokens(text):6,.0f}")


# =============================================================================
# PHASE 2b TASK 1 — _build_rich_market_brief() worst-case section-by-section
# inventory. Reproduces the exact per-line f-string formats copied verbatim from
# dexter.py (_build_rich_market_brief, _build_tf_context_summary) against
# synthetic candle data sized at worst case (500/250/150 raw candle rows for the
# 3 "full data" timeframes, 3 summary-only timeframes, 30+30 order book clusters,
# every optional block present). Never imports dexter.py. This does NOT replace
# the pre-cut docstring's 15-25K estimate (now obsolete after the Task 2 cuts) --
# this is the real, section-level measurement Task 1 asked for, and the
# authoritative source for the COMBINED TOTAL printed at the end of this section.
# =============================================================================
import pandas as _pd
import numpy as _np
from datetime import datetime as _dt, timedelta as _td

print("\n" + "=" * 70)
print("PHASE 2b TASK 1 -- MARKET BRIEF WORST-CASE INVENTORY")
print("=" * 70)


def _make_synthetic_df(n_rows, tf_minutes=60):
    """Synthetic OHLCV + indicator DataFrame, same columns _build_rich_market_brief reads."""
    _rng = _np.random.default_rng(42)
    idx = [_dt(2026, 1, 1) + _td(minutes=tf_minutes * i) for i in range(n_rows)]
    price = 61000 + _np.cumsum(_rng.normal(0, 40, n_rows))
    price = _np.clip(price, 100, None)
    high = price + _rng.uniform(5, 80, n_rows)
    low  = price - _rng.uniform(5, 80, n_rows)
    open_ = price + _rng.normal(0, 20, n_rows)
    vol  = _rng.uniform(50, 5000, n_rows)
    df = _pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": price, "volume": vol,
        "RSI": _rng.uniform(20, 80, n_rows),
        "ATR": _rng.uniform(price.mean() * 0.002, price.mean() * 0.02, n_rows),
        "EMA13": price * (1 + _rng.normal(0, 0.001, n_rows)),
        "EMA20": price * (1 + _rng.normal(0, 0.001, n_rows)),
        "EMA21": price * (1 + _rng.normal(0, 0.001, n_rows)),
        "EMA55": price * (1 + _rng.normal(0, 0.002, n_rows)),
        "VWAP":  price * (1 + _rng.normal(0, 0.001, n_rows)),
        "BB_upper": price * 1.012, "BB_mid": price, "BB_lower": price * 0.988,
        "MACD_hist": _rng.normal(0, 25, n_rows),
    }, index=_pd.DatetimeIndex(idx))
    return df


def _candle_row_str(ts, row, has_mh=True):
    """EXACT format copied from dexter.py:6193-6202 (the primary candle-dump loop)."""
    r_str = f"{row['RSI']:.1f}"
    e13   = f"{row['EMA13']:.5f}"
    e21   = f"{row['EMA21']:.5f}"
    e55   = f"{row['EMA55']:.5f}"
    m_str = f"{row['MACD_hist']:+.5g}" if has_mh else "-"
    return (
        f"{str(ts)[5:16]}|{row['open']:.5f}|{row['high']:.5f}|"
        f"{row['low']:.5f}|{row['close']:.5f}|{row['volume']:.0f}|{r_str}|{e13}|{e21}|{e55}|{m_str}"
    )


_inv = {}   # section name -> (chars, classification)
_brief_render = []   # (name, text) in measuring order, for the rendered sample


def _measure(name, text, kind):
    _inv[name] = (len(text), kind)
    _brief_render.append((name, text))


# --- 1. Header / time / session (dexter.py:5989-5990) ---
_measure(
    "Header + time/session",
    "=== MARKET BRIEF: BTCUSDT (CRYPTO) ===\n"
    "Time: 2026-07-08 14:32:00 UTC  |  Session: LONDON/NY OVERLAP | PEAK volume",
    "COMPUTED SUMMARY",
)

# --- 2. Previous Day (5992-5996) ---
_measure(
    "Previous Day (PDH/PDL/PDC)",
    "\n--- Previous Day ---\n  PDH: 63500.00000  |  PDL: 61200.00000  |  PDC: 62100.00000",
    "COMPUTED SUMMARY",
)

# --- 3. ADR block (5997-6009), forex/stocks only ---
_measure(
    "Average Daily Range block (forex/stocks only)",
    "\n--- Average Daily Range (14-day) ---\n"
    "  ADR     : 0.00850  (0.52% of price) — typical daily range\n"
    "  Today   : 0.00680  (80% of ADR used)\n"
    "  ⚠ TODAY'S RANGE IS 80% OF ADR — limited room left. TP targets beyond this distance are unlikely to hit today.",
    "COMPUTED SUMMARY",
)

# --- 4. Price Snapshot (6011-6028) ---
_measure(
    "Price Snapshot (price/VWAP/EMA20/RSI/ATR/Volume/BB)",
    "\n--- Price Snapshot ---\n"
    "  Price : 61320.50000\n"
    "  VWAP  : 61280.30000\n"
    "  EMA20 : 61250.10000\n"
    "  RSI 1H: 58.2  |  RSI 4H: 61.4\n"
    "  ATR 1H: 612.40000  |  USE THIS for SL sizing on 1H entries\n"
    "  ATR 1H : 612.40000  (reference only — use primary TF ATR for SL)\n"
    "  Volume: rising (last 10 candles on 1H)\n"
    "  BB (20,2) 1H: upper=61980.00000  mid=61320.00000  lower=60660.00000  width=2.15%\n"
    "  BB position : between mid and upper — %B=0.62  (1.08% to upper  |  0.10% to mid)\n"
    "  BB squeeze  : no",
    "COMPUTED SUMMARY",
)

# --- 5. 4H Macro Trend Context (dexter.py:5843-5849 via _macro_trend_context) ---
_measure(
    "4H Macro Trend Context block",
    "\n--- 4H Macro Trend Context (last ~3 days) ---\n"
    "  Verdict    : UPTREND — +3.2% over last 18 × 4H candles. 4/5 higher highs, 3/5 higher lows.\n"
    "  Last 18 candles: 11 bullish / 7 bearish\n"
    "  Volume profile: BUY volume dominates on up-candles — trend has institutional backing.\n",
    "COMPUTED SUMMARY",
)

# --- 6. Fibonacci (6034-6036) ---
_measure(
    "Fibonacci levels",
    "\n--- Fibonacci (uptrend) ---\n"
    "  50%: 60100.00000\n  61.8% (golden pocket): 59800.00000\n  65%: 59700.00000\n  78.6%: 59300.00000",
    "COMPUTED SUMMARY",
)

# --- 7. Support/Resistance (6038-6050) ---
_measure(
    "Support/Resistance (validated + all-zones list)",
    "\n--- Support / Resistance (validated across 15m/30m/1h/4h) ---\n"
    "  RESISTANCE: 64100.00000  [4H, 3x]  (4.53% away)\n"
    "  SUPPORT   : 59200.00000  [1H, 5x]  (3.46% away)\n"
    "  All resistance: 64100.00000(3x), 65200.00000(2x), 66800.00000(2x), 68000.00000(4x), 69500.00000(2x), 71000.00000(3x)\n"
    "  All support   : 59200.00000(5x), 58000.00000(3x), 56500.00000(2x), 55000.00000(4x), 53200.00000(2x), 51000.00000(3x)",
    "COMPUTED SUMMARY",
)

# --- 8. Volume Profile (6052-6068), 4H + 1H, each with up to a few signal lines ---
_measure(
    "Volume Profile (4H + 1H, anchor-based)",
    "\n--- Volume Profile (anchor-based: 4H + 1H) ---\n"
    "  Range starts at the first structural anchor of each TF (same logic as Arsenal).\n"
    "  [4H — anchor 2026-06-20  (72 candles, swing_low, conf 82%)]\n"
    "    POC: 61500.00000  (0.29% away) — highest-volume price, acts as magnet\n"
    "    VAH: 62800.00000  (2.41% away) — top of 70% value area (resistance above)\n"
    "    VAL: 60100.00000  (1.99% away) — bottom of 70% value area (support below)\n"
    "    SIGNAL: POC_RECLAIM at 61480.00000 — price reclaimed POC with volume confirmation\n"
    "  [1H — anchor 2026-07-06  (36 candles, swing_low, conf 74%)]\n"
    "    POC: 61350.00000  (0.05% away) — highest-volume price, acts as magnet\n"
    "    VAH: 61900.00000  (0.95% away) — top of 70% value area (resistance above)\n"
    "    VAL: 60800.00000  (0.85% away) — bottom of 70% value area (support below)",
    "COMPUTED SUMMARY",
)

# --- 9. Active Auction Anchor (6082-6089) ---
_measure(
    "Active Auction Anchor",
    "\n--- Active Auction Anchor ---\n"
    "  Price  : 60100.00000  [swing low]\n"
    "  Candle : 2026-07-06 08:00 (36 bars ago)\n"
    "  Method : swing_low  |  Confidence: 78%\n"
    "  Status : ACTIVE + CONFIRMED\n"
    "  Note   : VP, Fib, and VWAP should reference this as the auction origin. SR near this price is structural.",
    "COMPUTED SUMMARY",
)

# --- 10. RSI Divergence (6093-6096) ---
_measure(
    "RSI Divergence",
    "\n--- RSI Divergence ---\n  Regular Bullish at 60200.00000 — price lower low, RSI higher low (2 pivots confirmed)",
    "COMPUTED SUMMARY",
)

# --- 11. Order Book Liquidity Map (crypto only, 6099-6135) -- top 5 per side by size ---
_ob_lines = [
    "\n--- Order Book Liquidity Map (Binance, KuCoin, OKX, ±10% range) ---",
    "  Mid price: 61320.50000  |  Bias: +0.120 — mild bid dominance",
    "  Per-exchange imbalance:",
    "    Binance              +0.150 (more bids)",
    "    KuCoin                +0.080 (more bids)",
    "    OKX                   +0.090 (more bids)",
    "  ASKS price|dist%|qty|tag (top 5 by size):",
]
for _i in range(5):
    _p = 61320.5 + _i * 12.3
    _ob_lines.append(f"  {_p:.5f}|+{(_i*0.02):.2f}%|{(3.5 - _i*0.05):.3f}")
_ob_lines.append("  ASK AIR POCKETS (thin — price moves fast here):")
for _i in range(3):
    _ob_lines.append(f"  {61400.0 + _i*50:.5f}|{(0.1 + _i*0.05):.2f}%")
_ob_lines.append("  BIDS price|dist%|qty|tag (top 5 by size):")
for _i in range(5):
    _p = 61320.5 - _i * 12.3
    _ob_lines.append(f"  {_p:.5f}|-{(_i*0.02):.2f}%|{(3.5 - _i*0.05):.3f}")
_ob_lines.append("  BID AIR POCKETS (thin — price drops fast here):")
for _i in range(3):
    _ob_lines.append(f"  {61200.0 - _i*50:.5f}|{(0.1 + _i*0.05):.2f}%")
_ob_lines.append("  *** MULTI-EXCHANGE BID WALLS (same wall seen on 2+ exchanges — institutional) ***")
_ob_lines.append("      60100.00000  on: Binance, KuCoin")
_ob_lines.append("  *** MULTI-EXCHANGE ASK WALLS (same wall seen on 2+ exchanges — institutional) ***")
_ob_lines.append("      62800.00000  on: Binance, OKX")
_measure("Order Book Liquidity Map (crypto only, top 5 per side + air pockets + walls)", "\n".join(_ob_lines), "RAW DATA")

# --- 12. Futures / derivs (crypto only, 6137-6147) -- OI + funding only (teaching moved to prompt) ---
_measure(
    "Futures (Binance perpetual) — OI + funding only (teaching text moved to lean prompt)",
    "\n--- Futures (Binance perpetual) ---\n"
    "  Open Interest : 145,230.50 contracts  (6h: +1.20% | 24h: -0.80%)\n"
    "  Funding Rate  : +0.0320%  (neutral)",
    "COMPUTED SUMMARY",
)

# --- 13. HIGHER/LOWER TIMEFRAME CONTEXT: header + 3x _build_tf_context_summary ---
_tf_summary_sample = (
    "\n══════════════════════════════════════════════════════\n"
    "  5M CONTEXT  (last close: 2026-07-08 14:30)\n"
    "══════════════════════════════════════════════════════\n"
    "TREND        : BULLISH — higher highs and higher lows, price above SMA50\n"
    "EMA ALIGNMENT: BULLISH STACK (13>21>55) — strong uptrend alignment\n"
    "  EMA13: 61310.00000  (0.02% above)\n"
    "  EMA21: 61280.00000  (0.07% above)\n"
    "  EMA55: 61150.00000  (0.28% above)\n"
    "  EMA CROSSOVER: EMA13 crossed bullish 4 candle(s) ago — trend shift signal\n"
    "RSI          : 58.2 — neutral zone\n"
    "  DIVERGENCE: Regular Bullish — price lower low, RSI higher low\n"
    "KEY LEVELS\n"
    "  Resistance : 61500.00000  [4H, 3x]  (+0.29% away)\n"
    "  Resistance : 62800.00000  [1H, 2x]  (+2.41% away)\n"
    "  Resistance : 64100.00000  [4H, 3x]  (+4.53% away)\n"
    "  Support    : 61100.00000  [1H, 4x]  (-0.36% away)\n"
    "  Support    : 60100.00000  [4H, 5x]  (-1.99% away)\n"
    "  Support    : 59200.00000  [1H, 5x]  (-3.46% away)\n"
    "FIBONACCI    : (uptrend)\n"
    "  50%: 60100.00000  (1.99% away)\n"
    "  61.8% (golden pocket): 59800.00000  (2.48% away) ★ GOLDEN POCKET\n"
    "  65%: 59700.00000  (2.65% away)\n"
    "  78.6%: 59300.00000  (3.30% away)\n"
    "  ★★ PRICE IN GOLDEN POCKET (50%–61.8% zone) — high-probability bounce area\n"
    "SWEEP ALERT  : BUY_SIDE — swept equal lows at 60950.00000 then reversed\n"
    "EQUAL HIGHS  : 61800.00000 (3x) — liquidity pool above (+0.78%)\n"
    "EQUAL LOWS   : 60900.00000 (2x) — liquidity pool below (-0.69%)\n"
)
_measure("Higher/Lower TF context header", "\n\n=== HIGHER / LOWER TIMEFRAME CONTEXT ===", "TEACHING TEXT (1 line) + header")
_measure("Per-TF summary block (×3 non-primary TFs, worst case each)", _tf_summary_sample * 3, "COMPUTED SUMMARY")

# --- 14. Full candle data ×3 TFs (RAW DATA -- the dominant cost) ---
_full_data_tfs = [("1h", 30, "PRIMARY TIMEFRAME"), ("4h", 15, "HIGHER TIMEFRAME +1"), ("1d", 0, "HIGHER TIMEFRAME +2")]
_candle_section_total_chars = 0
for _tf, _lim, _role in _full_data_tfs:
    if _lim == 0:
        # Phase 2b: 1D gets NO raw dump — PDH/PDL + macro trend context + the per-TF
        # summary already carry its story. One fallback line only (last 5 daily closes),
        # matching dexter.py:6177-6179.
        _fb = (f"\n\n=== {_role}: {_tf.upper()} — FULL CANDLE DATA (last {_lim} candles) ===\n"
               f"{_tf.upper()} last 5 closes: 59800.00000, 60100.00000, 60500.00000, 60900.00000, 61200.00000")
        _measure(f"Raw candle dump: {_tf.upper()} (0 candles — last-5-closes fallback)", _fb, "COMPUTED SUMMARY")
        _candle_section_total_chars += len(_fb)
        continue
    _df = _make_synthetic_df(_lim)
    _header = (
        f"\n\n=== {_role}: {_tf.upper()} — FULL CANDLE DATA (last {_lim} candles) ===\n"
        + ("This is your primary chart. All indicators pre-computed per candle.\n" if _role == "PRIMARY TIMEFRAME"
           else "Higher timeframe context — compile your own structure, swings, and key levels from this data.\n")
        + f"Trade type for this TF: day\n"
        + f"Candles {_tf.upper()} ({_lim}) time|O|H|L|C|V|RSI|E13|E21|E55|Mh\n"
    )
    _rows = "\n".join(_candle_row_str(ts, row) for ts, row in _df.iterrows())
    _block = _header + _rows
    _measure(f"Raw candle dump: {_tf.upper()} ({_lim} candles)", _block, "RAW DATA")
    _candle_section_total_chars += len(_block)

# --- 15-18. Primary-TF-only extras: patterns, equal H/L (+ teaching text), sweeps, MACD ---
_measure(
    "Patterns (primary TF, last 5 candles)",
    "\nPatterns (last 5 candles on primary TF):\n  Bullish Engulfing at 2026-07-08 13:00 — strong reversal signal\n  Hammer at 2026-07-08 14:00 — rejection wick, bullish",
    "COMPUTED SUMMARY",
)
_measure(
    "Equal Highs/Lows (primary TF)",
    "Equal Highs (liquidity above):\n"
    "  61800.00000  (3 touches)\n  62100.00000  (2 touches)\n"
    "Equal Lows (liquidity below):\n"
    "  60900.00000  (2 touches)\n  60500.00000  (3 touches)",
    "COMPUTED SUMMARY",
)
_measure(
    "Sweep + MACD summary (primary TF)",
    "SWEEP: BUY_SIDE sweep at 60950 then reversed — liquidity grab, not real breakdown\n"
    "MACD summary: histogram=+12.30000 (bullish, rising) — BULLISH ZERO-LINE CROSS (momentum flipped positive)",
    "COMPUTED SUMMARY",
)

# --- 19. BOS/CHoCH structural label (6240-6246) ---
_measure(
    "BOS/CHoCH structural label",
    "\nMARKET STRUCTURE (1H) — most recent structural event:\n"
    "  Event   : BOS (bullish) — broke above 61500.00000 swing high with volume confirmation\n"
    "  Holds   : structure valid while price stays above 61100.00000\n"
    "  Warning : a close back below 61100.00000 invalidates this structure read",
    "COMPUTED SUMMARY",
)

# --- 20. Zone dwell analysis (6249-6260), incl. teaching-text context note ---
_measure(
    "Zone dwell analysis",
    "\nZONE ANALYSIS (confluence at 61320.5):\n"
    "  Prior tests  : 3 prior touches, all held — well-established level\n"
    "  Current candle: DOJI — indecision at the level\n",
    "COMPUTED SUMMARY",
)

# ---------------------------------------------------------------------------
# Print the inventory table
# ---------------------------------------------------------------------------
print(f"\n{'Section':<55} {'Tokens':>8}  {'Kind'}")
print("-" * 100)
_total_chars = 0
_by_kind = {"RAW DATA": 0, "COMPUTED SUMMARY": 0, "TEACHING TEXT": 0}
for name, (chars, kind) in _inv.items():
    tok = chars / CHARS_PER_TOKEN
    _total_chars += chars
    # bucket mixed-kind entries under their primary kind for the rollup only
    _bucket = "RAW DATA" if kind.startswith("RAW DATA") else ("TEACHING TEXT" if kind.startswith("TEACHING TEXT") else "COMPUTED SUMMARY")
    _by_kind[_bucket] += chars
    print(f"{name:<55} {tok:>8,.0f}  {kind}")

_brief_total_tokens = _total_chars / CHARS_PER_TOKEN
print("-" * 100)
print(f"{'TOTAL (worst case: primary=1h/30, 4h/15, 1d/0-fallback, crypto w/ orderbook top-5 + futures)':<55} {_brief_total_tokens:>8,.0f}")
print(f"\nRollup by kind:")
for kind, chars in _by_kind.items():
    print(f"  {kind:<20} {chars/CHARS_PER_TOKEN:>8,.0f} tok  ({chars/_total_chars*100:.0f}% of brief)")

print(f"\nTarget for Task 2: brief <= 8,000 tokens worst-case.")
print(f"Current worst-case: {_brief_total_tokens:,.0f} tokens -- {'OVER' if _brief_total_tokens > 8000 else 'under'} by {abs(_brief_total_tokens-8000):,.0f}")

print(f"\n[COMBINED TOTAL] system_prompt ({sys_tok:,.0f}) + wrapper ({esc_tok:,.0f}) + brief ({_brief_total_tokens:,.0f}) "
      f"= {sys_tok + esc_tok + _brief_total_tokens:,.0f} tokens")
print(f"Target: <= 14,000 tokens total. {'OVER' if (sys_tok+esc_tok+_brief_total_tokens) > 14000 else 'under'} by "
      f"{abs(sys_tok+esc_tok+_brief_total_tokens-14000):,.0f}")

print("\n" + "=" * 70)
print("RENDERED SAMPLE BRIEF (crypto, post-cut: order book top-5/side, 1D = last-5-closes fallback)")
print("=" * 70)
print("\n".join(t for _, t in _brief_render))

# =============================================================================
# PHASE R4 -- CHECKPOINT COPY: the Trendline Ray block ALSO appears in
# ask_chev_manage_trade's periodic checkpoint (dexter.py), a SEPARATE message
# from the escalation this whole script otherwise measures. This section did
# not exist before Phase R4 -- added now specifically because Task 3 puts the
# ray block there too, and the handoff shows this file has lied before when
# text moved without the audit following (2026-07-08 audit-truthfulness fix).
# This is NOT the full checkpoint message (market_brief/_build_management_brief
# is a separate live fetch, same convention as the escalation's own big candle
# brief being measured separately above) -- just tool_instructions + the ray
# block, reproduced verbatim from dexter.py's ask_chev_manage_trade, to show
# where the ray block sits and what it costs in THIS context. NOTE: the 14k/
# 8k gates below are defined for the escalation message specifically -- there
# is no separately-defined budget gate for the checkpoint message; this
# section exists to make its real cost visible, not to grade it against a
# number nobody has set.
# =============================================================================
print("\n" + "=" * 70)
print("PHASE R4 -- CHECKPOINT COPY (ask_chev_manage_trade)")
print("=" * 70)

_symbol_cp     = "BTCUSDT"
_primary_tf_cp = "15m"
_is_long_cp    = True

# tool_instructions -- copied verbatim from dexter.py's ask_chev_manage_trade
tool_instructions_cp = (
    f"TOOLS — call these before deciding. You have NO memory of this trade's original setup.\n"
    f"Form a fresh, data-backed view RIGHT NOW using live data:\n\n"
    f"  get_support_resistance(\"{_symbol_cp}\", \"{_primary_tf_cp}\")\n"
    f"    → For Stop placement: find the nearest confirmed {'support' if _is_long_cp else 'resistance'} level\n"
    f"      {'below' if _is_long_cp else 'above'} current price — place your stop just {'below' if _is_long_cp else 'above'} it,\n"
    f"      leaving at least 0.5× ATR of breathing room above that level.\n"
    f"    → For TP: find the nearest confirmed {'resistance' if _is_long_cp else 'support'} level\n"
    f"      {'above' if _is_long_cp else 'below'} current price — a valid new TP must sit AT or just before it.\n\n"
    f"  get_volume_profile(\"{_symbol_cp}\", \"{_primary_tf_cp}\")\n"
    f"    → Is price near POC, VAH, or VAL? Clusters cause reversals or act as magnets.\n"
    f"      Volume INCREASING into the move = momentum. Volume FADING = exhaustion.\n\n"
    f"  detect_rsi_divergence(\"{_symbol_cp}\", \"{_primary_tf_cp}\")\n"
    f"    → Hidden {'bullish' if _is_long_cp else 'bearish'} divergence = continuation likely.\n"
    f"      Regular {'bearish' if _is_long_cp else 'bullish'} divergence = reversal warning — tighten stop or close.\n\n"
    f"Call these tools. Base every decision on what they show — not on memory or assumptions.\n\n"
)

# Checkpoint ray block -- SAME formatter call, SAME worst-case rays as the
# escalation sample above, but WITHOUT levels (dexter.py's real checkpoint
# call passes none, per Task 3 -- ask_chev_manage_trade doesn't have Fib/SR/VP
# in scope the way scan_pair_tf does), wrapped in the same "DATA Dexter
# already fetched" label dexter.py prepends.
ray_block_checkpoint = format_ray_block_for_chev(
    [_ray_upper, _ray_lower], current_price=61350.0, timeframe="15m")
if ray_block_checkpoint:
    ray_block_checkpoint = "DATA Dexter already fetched (no tool call needed for this):\n" + ray_block_checkpoint

_cp_tool_tok = est_tokens(tool_instructions_cp)
_cp_ray_tok  = est_tokens(ray_block_checkpoint)
print(f"\n[tool_instructions]  chars={len(tool_instructions_cp):,}  est_tokens={_cp_tool_tok:,.0f}")
print(f"[Trendline Ray block, checkpoint, worst case]  chars={len(ray_block_checkpoint):,}  est_tokens={_cp_ray_tok:,.0f}")
print(f"\n[Checkpoint copy total: tool_instructions + ray block]  "
      f"{_cp_tool_tok + _cp_ray_tok:,.0f} tokens (market_brief -- a separate live "
      f"fetch via _build_management_brief -- NOT measured here, same convention "
      f"as the escalation's own candle brief being measured separately above)")

print("\n--- Rendered checkpoint ray block sample ---")
print(ray_block_checkpoint.rstrip())
