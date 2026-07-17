# PROPOSED — not applied. Insert into dexter.py at the SKIP branch (currently line
# 14606-14623), replacing that block. Requires Kev's review before Edit is used to
# actually apply this to dexter.py.
#
# WHY: backtest_chev_skips.py (2026-07-17) found two live problems in Chev's SKIP
# decisions during EXPLORATION_MODE, both currently unenforced by code:
#   1. 26.4% of score-qualified SKIPs cite a reason outside the exploration note's
#      sanctioned list (no invalidation level / counter-trend unmet / price not at
#      zone) or the brief's R:R-floor-citation path -- e.g. "not enough confluence"
#      (explicitly banned by name), "lacks confirmation" (never authorized).
#   2. Of the R:R-floor citations, many state 2.0:1 -- a number that appears nowhere
#      in chev-chelios-clone's prompt (verified exhaustively) and doesn't match the
#      real EXPLORATION MIN_NET_RR (1.3/1.2/1.6 by trade_type). The correct number is
#      right there in the prompt's own worked example and still gets overridden --
#      likely because chev-32b was built 2026-07-09, five days before the R:R floor
#      was ever split by mode (2026-07-14), so a prompt patch alone is not reliable.
# Both get ONE retry, same mechanism GAUNTLET RETRY / GEOMETRY REVIEW already use:
# append the reply + a correction message to _chev_messages, call _call_chev once
# more, accept whatever comes back (POST or a re-justified SKIP) -- never a second
# retry, never a silent block. If the retry doesn't fix it, log what actually
# happened; do not force an outcome.

import re

# Mirrors dexter.py's own _exploration_note (~10382): the three reasons the prompt
# authorizes on their own, plus RISK_REWARD_OR_SIZING and INVALIDATION_TOO_CLOSE/
# _MISSING, which the SKIP DISCIPLINE section separately sanctions via brief-number
# citation. Everything else is either explicitly banned (CONFLUENCE_BELOW_THRESHOLD)
# or was never authorized at all (CONFIRMATION_MISSING, STRUCTURAL_SUPPORT_MISSING).
_SKIP_TAXONOMY_SANCTIONED = {
    "INVALIDATION_TOO_CLOSE", "INVALIDATION_MISSING", "TREND_CONTEXT",
    "PRICE_NOT_AT_ZONE", "RISK_REWARD_OR_SIZING",
}

# v2 (self-audit correction): v1 took max(all cited numbers) as "the claimed floor."
# That breaks on a reply like "R:R only 0.9:1, not attractive" -- ONE number, the
# setup's own R:R, no floor claimed at all -- which v1 would have flagged as a false
# violation (0.9 matches no real floor) even though Chev never asserted a wrong
# number. Only trust a number as a FLOOR CLAIM if it's textually adjacent to
# floor/minimum/required/at-least language, matching how Chev actually phrases it
# ("at least 2.0:1", "1.3:1 floor", "minimum requirement of 2.0:1") -- verified
# against the real quotes in chev_decisions.jsonl, not guessed.
_FLOOR_CITE_RE = re.compile(
    r"(?:"
    r"at least\s+(\d+(?:\.\d+)?)\s*:\s*1"
    r"|(\d+(?:\.\d+)?)\s*:\s*1\s+floor"
    r"|(?:minimum|required?)\s+(?:requirement\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*:\s*1"
    r"|(\d+(?:\.\d+)?)\s*:\s*1\s+(?:minimum|required?|requirement)"
    r")",
    re.IGNORECASE,
)


def _real_rr_floors(exploration_mode):
    """The actual configured floors, all three trade_types -- a cited number is
    valid if it matches ANY of them; EXPLORATION_MODE only, mirrors the brief."""
    prof = risk_gauntlet.get_active_profile(exploration_mode)
    return prof["MIN_NET_RR"]  # {"scalp": .., "day": .., "swing": ..}


def _rr_citation_matches_config(reason_text, detail_text, exploration_mode, tol=0.05):
    """None if no explicit FLOOR claim was made (nothing to check -- citing only the
    setup's own R:R is not a violation). True/False if a floor number was cited."""
    combined = f"{reason_text or ''} {detail_text or ''}"
    cited = [float(g) for m in _FLOOR_CITE_RE.finditer(combined) for g in m.groups() if g]
    if not cited:
        return None
    stated_floor = max(cited)  # if multiple floor-style phrases appear, check the largest
    real_floors = set(_real_rr_floors(exploration_mode).values())
    return any(abs(stated_floor - f) <= tol for f in real_floors)


# ── Replaces the existing SKIP branch (dexter.py ~14606) ──────────────────────
else:
    _last_escalated[esc_key] = time.time() + skip_cool
    _skip_reason = parsed.get("skip_reason", "no reason captured") if parsed else "no reason captured"
    _skip_reasoning = (parsed.get("reasoning", "") if parsed else "")
    _skip_missing   = (parsed.get("what_was_missing", "") if parsed else "")
    _skip_detail    = (_skip_reasoning + (f"\n\nWhat was missing: {_skip_missing}" if _skip_missing else "")).strip()

    _retry_nudge = None
    if EXPLORATION_MODE:
        _skip_cat = classify_chev_skip_reason(_skip_reason)
        if _skip_cat not in _SKIP_TAXONOMY_SANCTIONED:
            _retry_nudge = (
                f"Your SKIP reason (\"{_skip_reason}\") isn't one of the exploration-mode "
                f"sanctioned reasons: no invalidation level available, direction fights the "
                f"4H trend without meeting counter-trend requirements, price nowhere near the "
                f"zone, or a stated R:R/ATR floor from THIS brief. Re-read the setup — either "
                f"name one of those concretely, citing real numbers from the brief, or POST."
            )
        elif _skip_cat == "RISK_REWARD_OR_SIZING":
            _rr_ok = _rr_citation_matches_config(_skip_reason, _skip_detail, EXPLORATION_MODE)
            if _rr_ok is False:
                _real = _real_rr_floors(EXPLORATION_MODE)
                _retry_nudge = (
                    f"The R:R floor you cited doesn't match this brief's EXECUTABLE GEOMETRY "
                    f"block. The real floor for this setup's trade_type is one of: "
                    f"scalp {_real['scalp']:.2f}:1, day {_real['day']:.2f}:1, "
                    f"swing {_real['swing']:.2f}:1 — not a general 2:1 convention. Re-check "
                    f"the R:R against the correct number and reconsider POST."
                )

    if _retry_nudge:
        _chev_messages.append({"role": "assistant", "content": chev_response})
        _chev_messages.append({"role": "user", "content": _retry_nudge})
        _skip_revised = _call_chev(_chev_messages, timeout=180, model_id=ESCALATION_MODEL_ID)
        _skip_revised_parsed = parse_chev_reply(_skip_revised) if _skip_revised else None
        if _skip_revised_parsed:
            print(f"[{datetime.now()}] SKIP TAXONOMY RETRY -- {result['symbol']}: "
                  f"original reason rejected as off-taxonomy/wrong-number, retried.")
            chev_response   = _skip_revised
            parsed          = _skip_revised_parsed
            # Falls through to the normal POST/SKIP handling above this block on the
            # NEXT loop pass would be wrong -- this needs the same branching dexter.py
            # already has for POST vs SKIP after a revision. Recurse into the same
            # POST/SKIP dispatch used earlier in this function rather than duplicating
            # it here -- exact wiring depends on how that dispatch is factored, left
            # for implementation review rather than guessed at in this draft.
        else:
            print(f"[{datetime.now()}] SKIP TAXONOMY RETRY -- {result['symbol']}: "
                  f"no usable reply on retry -- logging original SKIP as-is.")

    print(f"[{datetime.now()}] Chev skipped {result['symbol']}/{primary_tf}: {_skip_reason}")
    _log_chev_decision(
        result["symbol"], primary_tf, result["count"], result["reasons"],
        "SKIP", _skip_reason, (result.get("regime_4h") or {}).get("regime"),
        detail=_skip_detail
    )
    try:
        labeller.record_setup(result, asset_type, "SKIP", chev_meta={"reason": _skip_reason})
    except Exception as _le:
        print(f"[labeller] record_setup error (SKIP): {_le}")
