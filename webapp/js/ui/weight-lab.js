/* ============================================================
   WEIGHT LAB — Ridge-regression confluence tag weight proposals. Lives in the
   Strategy panel ("Chev's Edge") as its own tab (moved 2026-07-13 from the
   Engine pane's sidebar — too cramped to show real per-tag context; this
   panel already has the room and, via the Indicator Scoreboard, the exact
   same win-rate/trend data this file now also draws on). Fetches
   /api/weight_proposal + /api/weight_overrides on this tab's own first click
   (see drawing.js's stratTab wiring), manual refresh via the header's refresh
   icon, no polling, NOT part of the Strategy panel's 25s loadAll() cycle (a
   background refresh mid-edit in the manual section would be its own bug).
   Approve/revert/batch reuse the existing _apiFetch()/X-Chev-Key mechanism
   (webapp/js/config/state.js) — no separate auth flow. Tag display names
   reuse friendlyTag()/_loadTagRegistry() (webapp/js/ui/watchlist.js), same as
   every other tag-leaderboard render site.

   HOT-RELOAD (2026-07-13): weight_overrides.json changes now go live within
   one Dexter scan cycle (~5min), no restart — see dexter.py's
   _reload_weight_overrides(). Every apply path (single approve, batch
   approve, manual — weight-lab.js's Phase 3) goes through an in-panel
   CONFIRM/CANCEL step first, backed by a server-side dry-run
   (/api/weight_preview) so the numbers Kev confirms are the true post-clamp
   values, never a client-side guess that could disagree with the server.

   MANUAL EDITOR PERFORMANCE CONTEXT (2026-07-13): the tag list and the
   selected-tag detail panel show real win rate, a weekly-trend sparkline, and
   a losing/heating-streak flag — reusing the EXACT SAME /api/strategy/
   performance and /api/strategy/tag_trends endpoints the Indicator Scoreboard
   (Strategy → Performance tab) already calls, never a second, separately-
   computed analysis of the same journal.
   ============================================================ */
(function() {
  let _wlLoaded = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function fmtR(n) { return (n >= 0 ? '+' : '') + n.toFixed(2); }
  function fmtNum(n) { return (n >= 0 ? '+' : '') + (Math.round(n * 100) / 100); }

  function _wlBody() { return document.getElementById('weightLabBody'); }

  // Never call .json() on a non-JSON body (e.g. a 404's HTML error page, or a
  // proxy/tunnel error page) -- that's what produced "Unexpected token '<'".
  // Applied to every Weight Lab fetch, not just the proposal one.
  async function _wlSafeJson(res) {
    const ct = res.headers.get('content-type') || '';
    if (!res.ok || !ct.includes('application/json')) {
      throw new Error(`Weight Lab endpoint not available (HTTP ${res.status}). If you just deployed, restart Dexter — the routes load at startup.`);
    }
    return res.json();
  }

  function _wlSkeleton() {
    let rows = '';
    for (let i = 0; i < 3; i++) {
      rows += `<div style="height:52px;border-radius:6px;margin-bottom:8px;
        background:linear-gradient(90deg, var(--s1) 0%, var(--s2) 50%, var(--s1) 100%);
        background-size:200% 100%;animation:wlSkeleton 1.4s ease-in-out infinite"></div>`;
    }
    return rows;
  }

  function _wlBanner(kind, html) {
    const colors = {
      error: { bg: 'rgba(242,54,69,0.10)',  border: 'rgba(242,54,69,0.4)',  fg: '#ff8a95' },
      warn:  { bg: 'rgba(240,180,41,0.10)', border: 'rgba(240,180,41,0.4)', fg: '#f0b429' },
    };
    const c = colors[kind] || colors.warn;
    return `<div style="padding:10px 12px;border:1px solid ${c.border};background:${c.bg};
      border-radius:6px;color:${c.fg};font-family:'Inter',sans-serif;font-size:11.5px;
      margin-bottom:10px;line-height:1.5">${html}</div>`;
  }

  // ── Server-side preview + in-panel confirm (shared by approve / approve-all
  //    / manual). items: [{tag, delta}] or [{tag, new_value}]. Returns a
  //    Promise<boolean> resolving true iff CONFIRM ran onConfirm without
  //    throwing, false on cancel/backdrop-click/preview failure/onConfirm
  //    error (error is already toasted in that case). ──
  function _wlCloseConfirm() {
    const el = document.getElementById('wlConfirmOverlay');
    if (el) el.remove();
  }

  async function _wlPreview(items) {
    const r = await _apiFetch('/api/weight_preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    const d = await _wlSafeJson(r);
    if (!d.ok) throw new Error(d.error || 'preview failed');
    return d;
  }

  function _wlShowConfirm(items, onConfirm) {
    return new Promise(async (resolve) => {
      let preview;
      try {
        preview = await _wlPreview(items);
      } catch (e) {
        _showToast(`<span class="tBear">Weight Lab — ${esc(e.message)}</span>`, 6000);
        resolve(false);
        return;
      }
      _wlCloseConfirm();

      const rows = preview.items.map(p => {
        const clampNote = p.clamped
          ? ` <span style="color:#f0b429" title="Server clamp engaged — the requested value was outside [0, 2×baseline+2]">(clamped from ${fmtNum(p.requested)})</span>`
          : '';
        return `<div style="display:flex;justify-content:space-between;gap:10px;padding:5px 0;
          font-family:'Share Tech Mono',monospace;font-size:11.5px;color:var(--txt1);
          border-bottom:1px solid rgba(255,255,255,0.06)">
          <span>${esc(typeof friendlyTag === 'function' ? friendlyTag(p.tag) : p.tag)}</span>
          <span>${fmtNum(p.current_effective)}pt → <strong>${fmtNum(p.new_effective)}pt</strong>${clampNote}</span>
        </div>`;
      }).join('');

      const overlay = document.createElement('div');
      overlay.id = 'wlConfirmOverlay';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
      overlay.innerHTML = `
        <div style="background:var(--s2,#1c2030);border:1px solid rgba(212,175,55,0.4);border-radius:8px;
             padding:18px;max-width:440px;width:100%;font-family:'Inter',sans-serif;color:var(--txt1);
             box-shadow:0 8px 32px rgba(0,0,0,0.5)">
          <div style="font-size:13px;font-weight:700;margin-bottom:10px">
            Confirm ${preview.items.length > 1 ? preview.items.length + ' weight changes' : 'weight change'}</div>
          <div>${rows}</div>
          <div style="font-size:10.5px;color:#f0b429;margin-top:12px;line-height:1.5">
            Freeze window resets — proposals pause until ~${preview.freeze_min_records} fresh records accumulate.
          </div>
          <div style="display:flex;gap:8px;margin-top:16px">
            <button id="wlConfirmCancel" style="flex:1;padding:8px;border-radius:5px;cursor:pointer;
              background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.15);color:var(--txt1);
              font-family:'Share Tech Mono',monospace;font-size:11px;font-weight:700">CANCEL</button>
            <button id="wlConfirmOk" style="flex:1;padding:8px;border-radius:5px;cursor:pointer;
              background:rgba(212,175,55,0.15);border:1px solid var(--gold,#d4af37);color:var(--gold,#d4af37);
              font-family:'Share Tech Mono',monospace;font-size:11px;font-weight:700">CONFIRM</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);

      const finish = (result) => { _wlCloseConfirm(); resolve(result); };
      document.getElementById('wlConfirmCancel').addEventListener('click', () => finish(false));
      overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(false); });
      document.getElementById('wlConfirmOk').addEventListener('click', async () => {
        const okBtn = document.getElementById('wlConfirmOk');
        okBtn.disabled = true;
        okBtn.textContent = '…';
        try {
          await onConfirm();
          finish(true);
        } catch (e) {
          _showToast(`<span class="tBear">Weight Lab — ${esc(e.message)}</span>`, 6000);
          finish(false);
        }
      });
    });
  }

  async function _wlApprove(tag, delta, evidence, btn) {
    btn.disabled = true;
    const confirmed = await _wlShowConfirm([{ tag, delta }], async () => {
      const r = await _apiFetch('/api/weight_proposal/approve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag, delta, evidence }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'approve failed');
      _showToast(`<span>Weight Lab — ${esc(friendlyTag(tag))} queued (${delta > 0 ? '+' : ''}${delta}pt) — live within one scan cycle, no restart needed.</span>`, 6000);
      loadWeightLab(true);
    });
    if (!confirmed) btn.disabled = false;
  }

  async function _wlApproveAll(rows, btn) {
    btn.disabled = true;
    const items = rows.map(p => ({ tag: p.tag, delta: p.proposed_delta }));
    const confirmed = await _wlShowConfirm(items, async () => {
      const r = await _apiFetch('/api/weight_proposal/approve_batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: rows.map(p => ({ tag: p.tag, delta: p.proposed_delta, evidence: { n: p.n, coef: p.coef, ci: p.ci } })),
        }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'batch approve failed');
      let msg = `Weight Lab — ${d.approved.length} tag(s) queued, live within one scan cycle.`;
      if (d.rejected.length) msg += ` ${d.rejected.length} skipped: ${d.rejected.map(x => x.tag).join(', ')}.`;
      _showToast(`<span>${esc(msg)}</span>`, 7000);
      loadWeightLab(true);
    });
    if (!confirmed) btn.disabled = false;
  }

  async function _wlMarkReviewed(btn) {
    btn.disabled = true;
    try {
      const r = await _apiFetch('/api/weight_proposal/mark_reviewed', { method: 'POST' });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'failed');
      _showToast(`<span>Weight Lab — marked reviewed.</span>`, 3000);
      loadWeightLab(true);
    } catch (e) {
      _showToast(`<span class="tBear">Weight Lab — ${esc(e.message)}</span>`, 6000);
      btn.disabled = false;
    }
  }

  async function _wlUndo(index, btn) {
    btn.disabled = true;
    try {
      const r = await _apiFetch('/api/weight_proposal/revert', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'revert failed');
      _showToast(`<span>Weight Lab — change reverted, reverts to baseline within one scan cycle.</span>`, 4000);
      loadWeightLab(true);
    } catch (e) {
      _showToast(`<span class="tBear">Weight Lab — ${esc(e.message)}</span>`, 6000);
      btn.disabled = false;
    }
  }

  function _wlPendingBanner(entries) {
    const pendingIdx = [];
    entries.forEach((e, i) => { if (!e.active) pendingIdx.push(i); });
    if (!pendingIdx.length) return '';
    const rows = pendingIdx.map(i => {
      const e = entries[i];
      return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;
        font-family:'Share Tech Mono',monospace;font-size:11px">
        <span style="flex:1">${esc(friendlyTag(e.tag))} ${e.delta > 0 ? '+' : ''}${e.delta}pt
          ${e.source === 'manual' ? '<span style="opacity:0.6">(manual)</span>' : ''}</span>
        <button class="wlUndoBtn" data-index="${i}" style="background:none;
          border:1px solid rgba(240,180,41,0.4);color:#f0b429;border-radius:4px;
          font-size:10px;padding:2px 8px;cursor:pointer">undo</button>
      </div>`;
    }).join('');
    return _wlBanner('warn', `⚠ ${pendingIdx.length} approved change(s) not yet live — takes effect within one scan cycle (~5 min), no restart needed.<div style="margin-top:6px">${rows}</div>`);
  }

  function _wlDigestHeader(verifiedRows, lastReviewedAt) {
    if (verifiedRows.length < 2) return '';
    const reviewedLine = lastReviewedAt
      ? `since your last review (${esc(lastReviewedAt)} UTC)`
      : `— no review recorded yet`;
    const tableRows = verifiedRows.map(p => `
      <div style="display:flex;justify-content:space-between;gap:8px;font-family:'Share Tech Mono',monospace;
        font-size:10.5px;padding:2px 0;color:var(--txt2)">
        <span>${esc(friendlyTag(p.tag))}</span>
        <span>n=${p.n} · ${fmtR(p.coef)}R · ${p.current_weight}→${p.effective_weight_preview}pt</span>
      </div>`).join('');
    return `
      <div style="border:1px solid var(--gold,#d4af37);border-radius:6px;padding:12px;margin-bottom:12px;
           background:rgba(212,175,55,0.06)">
        <div style="font-family:'Inter',sans-serif;font-size:12.5px;font-weight:700;color:var(--gold,#d4af37)">
          📋 Weekly digest — ${verifiedRows.length} verified proposals ${reviewedLine}</div>
        <div style="margin-top:8px">${tableRows}</div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button id="wlApproveAllBtn" style="flex:1;padding:6px;border-radius:5px;cursor:pointer;
            background:rgba(212,175,55,0.15);border:1px solid var(--gold,#d4af37);color:var(--gold,#d4af37);
            font-family:'Share Tech Mono',monospace;font-size:11px;font-weight:700">
            APPROVE ALL ${verifiedRows.length}</button>
          <button id="wlMarkReviewedBtn" style="padding:6px 10px;border-radius:5px;cursor:pointer;
            background:none;border:1px solid rgba(255,255,255,0.15);color:var(--txt3);
            font-family:'Inter',sans-serif;font-size:10.5px">mark reviewed</button>
        </div>
      </div>`;
  }

  function _wlProposalCard(p) {
    const name = friendlyTag(p.tag);
    const evLine = `n=${p.n} · effect ${fmtR(p.coef)}R · 95% CI [${fmtR(p.ci[0])}, ${fmtR(p.ci[1])}]`;
    const hasCross = p.n_with != null && p.winrate_with != null && p.winrate_without != null;
    const crossLine = hasCross
      ? `With this tag: ${p.winrate_with}% WR, ${fmtR(p.avg_netR_with)}R avg · Without: ${p.winrate_without}% WR, ${fmtR(p.avg_netR_without)}R avg`
      : '';
    const conflict = p.agreement === 'conflict';
    // Missing mapping (e.g. a stale cached payload from before this field existed)
    // is treated as 'unmapped' -- never render an Approve button on an assumption.
    const mapping = p.mapping || 'unmapped';
    // The mapping gate is checked client-side too, independent of whatever
    // current_weight/proposed_delta the payload happens to carry -- a stale
    // cache must never be able to resurrect an Approve button for a
    // now-unverified tag.
    const canPropose = mapping === 'verified' && p.proposed_delta != null && p.current_weight != null;
    const deltaLabel = canPropose ? (p.proposed_delta > 0 ? `+${p.proposed_delta}` : `${p.proposed_delta}`) : null;
    const weightLine = canPropose
      ? `${p.current_weight}pt → ${p.effective_weight_preview}pt`
      : mapping === 'unverified'
        ? 'current: n/a (unverified mapping)'
        : 'current: n/a (unmapped)';

    let actionEl = '';
    if (canPropose) {
      actionEl = `<button class="wlApproveBtn" data-tag="${esc(p.tag)}" data-delta="${p.proposed_delta}"
           data-evidence='${esc(JSON.stringify({ n: p.n, coef: p.coef, ci: p.ci }))}'
           style="margin-left:auto;font-family:'Share Tech Mono',monospace;font-size:11px;
             font-weight:700;padding:4px 10px;border-radius:5px;cursor:pointer;
             ${conflict
               ? 'background:rgba(240,180,41,0.12);border:1px solid rgba(240,180,41,0.5);color:#f0b429'
               : 'background:rgba(212,175,55,0.12);border:1px solid var(--gold);color:var(--gold)'}">
           APPROVE ${deltaLabel}</button>`;
    } else if (mapping === 'unverified') {
      actionEl = `<span title="Same labeller code as a CONFLUENCE_SCORES key, but the mechanic behind it hasn't been confirmed identical — see handoff PHASE 16's no-aliasing rule."
        style="margin-left:auto;font-family:'Share Tech Mono',monospace;font-size:9.5px;font-weight:700;
          padding:4px 10px;border-radius:5px;color:var(--txt3);background:rgba(255,255,255,0.03);
          border:1px dashed rgba(255,255,255,0.15);cursor:help">same name, unconfirmed mechanic — not approvable</span>`;
    }
    const unmappedNote = mapping === 'unmapped'
      ? `<div style="font-size:9px;color:var(--txt3);margin-top:4px;font-family:'Inter',sans-serif">no live weight uses this code — informational only</div>`
      : '';

    return `
      <div style="border:1px solid rgba(147,112,219,0.25);border-radius:6px;padding:10px 12px;
        margin-bottom:8px;background:rgba(255,255,255,0.02)">
        <div style="font-family:'Inter',sans-serif;font-size:12px;font-weight:600;color:var(--txt1)"
             title="${esc(p.tag)}">${esc(name)}</div>
        <div style="font-family:'Share Tech Mono',monospace;font-size:10.5px;color:var(--txt2);margin-top:3px">${esc(evLine)}</div>
        ${crossLine ? `<div style="font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt2);margin-top:3px">${esc(crossLine)}</div>` : ''}
        ${conflict ? `<div style="margin-top:4px"><span style="font-size:9.5px;font-weight:700;color:#f0b429;
             background:rgba(240,180,41,0.12);border:1px dashed rgba(240,180,41,0.5);
             border-radius:10px;padding:2px 7px">⚠ regression and raw stats disagree — treat with suspicion</span></div>` : ''}
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px;
             font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--txt1)">
          <span>${esc(weightLine)}</span>
          ${actionEl}
        </div>
        ${unmappedNote}
        <div style="font-size:9px;color:var(--txt3);margin-top:6px;font-family:'Inter',sans-serif">
          Proposal only. Nothing changes until you approve — live within one scan cycle after that.</div>
      </div>`;
  }

  function _wlNonSignificant(rows) {
    if (!rows.length) return '';
    const items = rows.map(p => `
      <div style="display:flex;gap:10px;padding:3px 0;font-family:'Share Tech Mono',monospace;
        font-size:10.5px;color:var(--txt3)">
        <span style="flex:1;opacity:0.7">${esc(friendlyTag(p.tag))}</span>
        <span>n=${p.n} · ${fmtR(p.coef)}R</span>
      </div>`).join('');
    return `
      <details style="margin-top:6px">
        <summary style="font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt3)">
          Show ${rows.length} tags below the evidence bar</summary>
        <div style="margin-top:6px">${items}</div>
      </details>`;
  }

  // ── PHASE 3: Manual override editor — deliberately quiet, bottom of panel,
  //    collapsed by default. Not competing with the evidence cards: no
  //    evidence, no advice, no agreement flags here — this mode is Kev's
  //    judgment by definition. Rides on the same _wlShowConfirm/preview
  //    infrastructure the proposal paths use. ──
  let _wlLastProposalPayload = null;  // most-recently successfully-served /api/weight_proposal
                                       // `proposals` array — the manual editor's FYI line reads
                                       // ONLY from this cache, never a fresh fetch (Pin 2).
  let _wlManualTagsCache = null;       // /api/weight_manual/tags response, fetched lazily on first open
  let _wlManualPerfCache = null;       // merged real-WR + weekly-trend per tag (see below), same lazy fetch
  let _wlManualSelected = null;        // currently selected tag object

  // Same 2-stop lerp drawing.js's Indicator Scoreboard uses (winRateColor) —
  // duplicated here on purpose rather than reached across files, same
  // standalone-per-file convention this project already uses elsewhere
  // (COST_R_CAP is duplicated in labeller.py/counterfactual_report.py/
  // weight_proposal.py rather than imported). 0%=red, 50%=gold, 100%=green.
  function _wlWinRateColor(pct) {
    if (pct >= 50) {
      const t = Math.min((pct - 50) / 50, 1);
      return `rgb(${Math.round(212 - t*204)}, ${Math.round(175 + t*(153-175))}, ${Math.round(55 + t*(129-55))})`;
    }
    const t = Math.min((50 - pct) / 50, 1);
    return `rgb(${Math.round(212 + t*(242-212))}, ${Math.round(175 - t*(175-54))}, ${Math.round(55 + t*(69-55))})`;
  }

  // Small (list-row) sparkline: bars only, no labels — space is tight there on
  // purpose, per Kev's own "too cramped to read" complaint. The bigger one in
  // the detail panel below gets the hover tooltip with real numbers.
  function _wlMiniSpark(trend) {
    if (!trend || !trend.length) return '<span style="color:var(--txt3);font-size:9.5px">no history</span>';
    const bars = trend.slice(-8).map(w =>
      `<div style="width:3px;height:${Math.max(3, w.wr * 12).toFixed(1)}px;background:${_wlWinRateColor(w.wr * 100)};border-radius:1px"></div>`
    ).join('');
    return `<div style="display:flex;align-items:flex-end;gap:1.5px;height:12px">${bars}</div>`;
  }

  function _wlBigSpark(trend) {
    if (!trend || !trend.length) {
      return '<div style="font-size:10px;color:var(--txt3)">No closed-trade history yet for this tag.</div>';
    }
    const title = trend.map(w => `${w.week}: ${(w.wr * 100).toFixed(0)}% (n=${w.n})`).join(' | ');
    const bars = trend.slice(-16).map(w =>
      `<div style="width:6px;height:${Math.max(4, w.wr * 28).toFixed(1)}px;background:${_wlWinRateColor(w.wr * 100)};
        border-radius:1px" title="${esc(w.week)}: ${(w.wr*100).toFixed(0)}% (n=${w.n})"></div>`
    ).join('');
    return `<div style="display:flex;align-items:flex-end;gap:2px;height:28px" title="${esc(title)}">${bars}</div>`;
  }

  // "Is this on a losing streak" — compares the most recent week's win rate
  // against the tag's own all-time win rate. Deliberately simple and visible
  // (no hidden model): a real drop, on real recent volume, is what a losing
  // streak concretely looks like. n>=3 on the recent week guards against a
  // single bad trade reading as a "streak."
  function _wlStreakNote(allTimeWr, trend) {
    if (!trend || !trend.length) return null;
    const last = trend[trend.length - 1];
    if (last.n < 3 || allTimeWr == null) return null;
    const diffPts = (last.wr * 100) - allTimeWr;
    if (diffPts <= -20) {
      return { cold: true, text: `Cooling — last week ${(last.wr*100).toFixed(0)}% (n=${last.n}) vs ${allTimeWr.toFixed(0)}% all-time` };
    }
    if (diffPts >= 20) {
      return { cold: false, text: `Heating up — last week ${(last.wr*100).toFixed(0)}% (n=${last.n}) vs ${allTimeWr.toFixed(0)}% all-time` };
    }
    return null;
  }

  function _wlManualSectionHtml() {
    return `
      <details id="wlManualDetails" style="margin-top:14px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px">
        <summary style="font-family:'Inter',sans-serif;font-size:11px;color:var(--txt3);cursor:pointer;user-select:none">
          ✎ Manual adjustment</summary>
        <div style="margin-top:10px">
          <input id="wlManualSearch" type="text" placeholder="search tag (name or code)..."
            style="width:100%;box-sizing:border-box;padding:6px 8px;border-radius:5px;
              background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.12);
              color:var(--txt1);font-family:'Inter',sans-serif;font-size:11px" />
          <div id="wlManualList" style="max-height:190px;overflow-y:auto;margin-top:6px"></div>
          <div id="wlManualEditor" style="display:none;margin-top:10px;padding-top:10px;
            border-top:1px solid rgba(255,255,255,0.08)">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span id="wlManualEditorName" style="font-family:'Inter',sans-serif;font-size:12px;
                font-weight:600;color:var(--txt1)"></span>
              <span id="wlManualEditorRange" style="font-family:'Share Tech Mono',monospace;
                font-size:10px;color:var(--txt3)"></span>
            </div>
            <div id="wlManualPerf" style="margin-top:8px"></div>
            <div style="display:flex;gap:8px;margin-top:10px;align-items:center">
              <input id="wlManualValue" type="number" step="0.5" style="width:80px;padding:5px 8px;
                border-radius:5px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.15);
                color:var(--txt1);font-family:'Share Tech Mono',monospace;font-size:12px" />
              <button id="wlManualSetBtn" style="padding:6px 14px;border-radius:5px;cursor:pointer;
                background:rgba(212,175,55,0.12);border:1px solid var(--gold,#d4af37);color:var(--gold,#d4af37);
                font-family:'Share Tech Mono',monospace;font-size:11px;font-weight:700">SET</button>
            </div>
            <div id="wlManualFyi" style="font-family:'Inter',sans-serif;font-size:10px;color:var(--txt3);margin-top:6px"></div>
          </div>
        </div>
      </details>`;
  }

  function _wlManualRenderList(filterText) {
    const listEl = document.getElementById('wlManualList');
    if (!listEl || !_wlManualTagsCache) return;
    const q = (filterText || '').trim().toLowerCase();
    const rows = _wlManualTagsCache.filter(t =>
      !q || t.tag.toLowerCase().includes(q) || (t.name || '').toLowerCase().includes(q));
    listEl.innerHTML = rows.slice(0, 40).map(t => {
      const perf = (_wlManualPerfCache || {})[t.tag];
      const wr = perf && perf.n > 0 ? perf.win_rate * 100 : null;
      const wrHtml = wr != null
        ? `<span style="color:${_wlWinRateColor(wr)}">${wr.toFixed(0)}%</span><span style="color:var(--txt3)"> (n=${perf.n})</span>`
        : `<span style="color:var(--txt3)">n/a</span>`;
      return `
      <div class="wlManualTagRow" data-tag="${esc(t.tag)}" style="display:flex;align-items:center;justify-content:space-between;
        gap:8px;padding:5px 6px;cursor:pointer;border-radius:4px;font-family:'Share Tech Mono',monospace;
        font-size:10.5px;color:var(--txt2)">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.name)}
          <span style="opacity:0.5">(${esc(t.tag)})</span></span>
        <span style="white-space:nowrap">${wrHtml}</span>
        ${_wlMiniSpark(perf && perf.trend)}
        <span style="width:44px;text-align:right;color:var(--txt1)">${fmtNum(t.current_effective)}pt</span>
      </div>`;
    }).join('') || `<div style="font-size:10.5px;color:var(--txt3);padding:4px">no match</div>`;
    listEl.querySelectorAll('.wlManualTagRow').forEach(row => {
      row.addEventListener('click', () => _wlManualSelectTag(row.dataset.tag));
      row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,0.05)'; });
      row.addEventListener('mouseleave', () => { row.style.background = ''; });
    });
  }

  // Regression context for the detail panel — reads the SAME cached proposal
  // payload the FYI line uses (Pin 2: never a fresh fetch), but shown for
  // ANY tag with a row in it, not just significant+verified ones. This is the
  // honest answer to "could this have a better weight": what the regression
  // currently estimates, clearly labeled with its own confidence (or lack of
  // it) — informational only, same as the FYI line, never gating anything.
  function _wlRegressionContext(tag) {
    if (!_wlLastProposalPayload) return '';
    const row = _wlLastProposalPayload.find(p => p.tag === tag);
    if (!row) return '<div style="font-size:10px;color:var(--txt3);margin-top:4px">No regression data for this tag yet (needs enough closed shadow records).</div>';
    const sigTxt = row.significant
      ? `<span style="color:var(--gold,#d4af37)">statistically significant</span>`
      : `<span style="color:var(--txt3)">not yet significant — could be noise</span>`;
    const mapTxt = row.mapping === 'verified'
      ? ''
      : row.mapping === 'unverified'
        ? ` · same-named tag, mechanic unconfirmed (see Weight Lab's own gate)`
        : ` · not a live scored weight`;
    return `<div style="font-size:10px;color:var(--txt2);margin-top:4px;line-height:1.5">
      Regression read: ${fmtR(row.coef)}R effect (95% CI [${fmtR(row.ci[0])}, ${fmtR(row.ci[1])}], n=${row.n}) — ${sigTxt}${esc(mapTxt)}.
    </div>`;
  }

  function _wlManualRenderPerf(t) {
    const perfEl = document.getElementById('wlManualPerf');
    if (!perfEl) return;
    const perf = (_wlManualPerfCache || {})[t.tag];
    const wr = perf && perf.n > 0 ? perf.win_rate * 100 : null;
    const streak = perf ? _wlStreakNote(wr, perf.trend) : null;
    perfEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;font-family:'Share Tech Mono',monospace;font-size:11px">
        <span>Win rate: ${wr != null ? `<span style="color:${_wlWinRateColor(wr)};font-weight:700">${wr.toFixed(0)}%</span> (n=${perf.n})` : '<span style="color:var(--txt3)">no closed trades yet</span>'}</span>
      </div>
      <div style="margin-top:6px">${_wlBigSpark(perf && perf.trend)}</div>
      ${streak ? `<div style="font-size:10px;margin-top:4px;color:${streak.cold ? '#ff8a95' : '#5ee6a0'}">${streak.cold ? '📉' : '📈'} ${esc(streak.text)}</div>` : ''}
      ${_wlRegressionContext(t.tag)}
    `;
  }

  // Pin 2: FYI text is derived ONLY from _wlLastProposalPayload (already
  // fetched by the normal digest/card load) — never triggers a fresh
  // /api/weight_proposal call (and its regression run) just to decorate this
  // input. If nothing's cached yet, this renders nothing — stale-but-labeled
  // beats fresh-but-expensive, and "no note" is itself a valid, honest label.
  function _wlManualFyiText(tag, currentEffective, newValue) {
    if (!_wlLastProposalPayload) return '';
    const row = _wlLastProposalPayload.find(p => p.tag === tag && p.significant && p.proposed_delta != null);
    if (!row) return '';
    const editDir = Math.sign(newValue - currentEffective);
    const proposalDir = Math.sign(row.proposed_delta);
    if (editDir === 0 || proposalDir === editDir) return '';  // same direction or no change — nothing to flag
    return `FYI: current data proposes ${row.proposed_delta > 0 ? '+1' : '-1'} here (n=${row.n}, ${fmtR(row.coef)}R) — informational only, your call stands.`;
  }

  function _wlManualUpdateFyi() {
    if (!_wlManualSelected) return;
    const valueInput = document.getElementById('wlManualValue');
    const fyiEl = document.getElementById('wlManualFyi');
    if (!valueInput || !fyiEl) return;
    const newValue = parseFloat(valueInput.value);
    fyiEl.textContent = isNaN(newValue) ? '' : _wlManualFyiText(_wlManualSelected.tag, _wlManualSelected.current_effective, newValue);
  }

  function _wlManualSelectTag(tag) {
    const t = (_wlManualTagsCache || []).find(x => x.tag === tag);
    if (!t) return;
    _wlManualSelected = t;
    const editor = document.getElementById('wlManualEditor');
    if (!editor) return;
    editor.style.display = '';
    document.getElementById('wlManualEditorName').textContent = `${t.name} (${t.tag})`;
    document.getElementById('wlManualEditorRange').textContent = `allowed: ${fmtNum(t.min)}–${fmtNum(t.max)}`;
    const valueInput = document.getElementById('wlManualValue');
    valueInput.min = t.min; valueInput.max = t.max;
    valueInput.value = t.current_effective;
    _wlManualRenderPerf(t);
    _wlManualUpdateFyi();
  }

  async function _wlManualSet(btn) {
    if (!_wlManualSelected) return;
    const valueInput = document.getElementById('wlManualValue');
    const newValue = parseFloat(valueInput.value);
    if (isNaN(newValue)) {
      _showToast(`<span class="tBear">Weight Lab — enter a number.</span>`, 4000);
      return;
    }
    if (newValue < _wlManualSelected.min || newValue > _wlManualSelected.max) {
      _showToast(`<span class="tBear">Weight Lab — allowed range for ${esc(_wlManualSelected.name)} is ${fmtNum(_wlManualSelected.min)}–${fmtNum(_wlManualSelected.max)}.</span>`, 5000);
      return;
    }
    btn.disabled = true;
    const tag = _wlManualSelected.tag, name = _wlManualSelected.name;
    const confirmed = await _wlShowConfirm([{ tag, new_value: newValue }], async () => {
      const r = await _apiFetch('/api/weight_manual', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag, new_value: newValue }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'manual set failed');
      const msg = d.removed
        ? `Weight Lab — ${esc(name)} reverted to baseline — live within one scan cycle.`
        : `Weight Lab — ${esc(name)} set to ${fmtNum(newValue)}pt — live within one scan cycle.`;
      _showToast(`<span>${msg}</span>`, 6000);
      loadWeightLab(true);
    });
    if (!confirmed) btn.disabled = false;
  }

  async function _wlManualEnsureTagsLoaded() {
    if (_wlManualTagsCache) return;
    try {
      // Real win rate + weekly trend reuse the EXACT SAME endpoints the
      // Indicator Scoreboard (this same Strategy panel, Performance tab)
      // already calls — no new analysis invented, no per-tag stats
      // recomputed here. Both are plain public GETs, same as the scoreboard's
      // own fetches. A failure on either degrades gracefully (perf cache
      // stays empty, rows just show "n/a" — same "thin data isn't a bug"
      // posture the scoreboard itself uses) rather than blocking the tag list.
      const [tagsRes, perfRes, trendsRes] = await Promise.allSettled([
        _apiFetch('/api/weight_manual/tags').then(r => _wlSafeJson(r)),
        _apiFetch('/api/strategy/performance').then(r => r.json()),
        _apiFetch('/api/strategy/tag_trends').then(r => r.json()),
      ]);

      if (tagsRes.status !== 'fulfilled' || !tagsRes.value.ok) {
        throw new Error((tagsRes.status === 'fulfilled' && tagsRes.value.error) || 'failed to load tags');
      }
      _wlManualTagsCache = tagsRes.value.tags;

      const tagStats = (perfRes.status === 'fulfilled' && perfRes.value.tag_stats) || {};
      if (perfRes.status === 'rejected') console.warn('[Weight Lab] performance load failed', perfRes.reason);
      const trendsByTag = (trendsRes.status === 'fulfilled' && trendsRes.value.tags) || {};
      if (trendsRes.status === 'rejected') console.warn('[Weight Lab] tag_trends load failed', trendsRes.reason);

      _wlManualPerfCache = {};
      _wlManualTagsCache.forEach(t => {
        const s = tagStats[t.tag];
        _wlManualPerfCache[t.tag] = {
          n: s ? s.n : 0,
          win_rate: s ? s.win_rate : null,
          trend: trendsByTag[t.tag] || [],
        };
      });

      _wlManualRenderList('');
    } catch (e) {
      const listEl = document.getElementById('wlManualList');
      if (listEl) listEl.innerHTML = `<div style="font-size:10.5px;color:#ff8a95">${esc(e.message)}</div>`;
    }
  }

  function _wireManualSection() {
    const details = document.getElementById('wlManualDetails');
    if (!details) return;
    details.addEventListener('toggle', () => { if (details.open) _wlManualEnsureTagsLoaded(); });
    const search = document.getElementById('wlManualSearch');
    if (search) search.addEventListener('input', () => _wlManualRenderList(search.value));
    const valueInput = document.getElementById('wlManualValue');
    if (valueInput) valueInput.addEventListener('input', _wlManualUpdateFyi);
    const setBtn = document.getElementById('wlManualSetBtn');
    if (setBtn) setBtn.addEventListener('click', () => _wlManualSet(setBtn));
  }

  function _wireCardButtons(root, verifiedSignificant) {
    root.querySelectorAll('.wlApproveBtn').forEach(btn => {
      btn.addEventListener('click', () => {
        const tag = btn.dataset.tag, delta = parseInt(btn.dataset.delta, 10);
        let evidence = {};
        try { evidence = JSON.parse(btn.dataset.evidence); } catch (e) {}
        _wlApprove(tag, delta, evidence, btn);
      });
    });
    root.querySelectorAll('.wlUndoBtn').forEach(btn => {
      btn.addEventListener('click', () => _wlUndo(parseInt(btn.dataset.index, 10), btn));
    });
    const approveAllBtn = document.getElementById('wlApproveAllBtn');
    if (approveAllBtn) approveAllBtn.addEventListener('click', () => _wlApproveAll(verifiedSignificant, approveAllBtn));
    const markReviewedBtn = document.getElementById('wlMarkReviewedBtn');
    if (markReviewedBtn) markReviewedBtn.addEventListener('click', () => _wlMarkReviewed(markReviewedBtn));
  }

  async function loadWeightLab(forceRefresh) {
    const body = _wlBody();
    if (!body) return;
    if (!forceRefresh && _wlLoaded) return;  // first-open fetch only, unless manually refreshed
    body.innerHTML = _wlSkeleton();
    try {
      if (typeof _loadTagRegistry === 'function') await _loadTagRegistry();
      const [propRes, ovRes] = await Promise.all([
        _apiFetch('/api/weight_proposal'),
        _apiFetch('/api/weight_overrides'),
      ]);
      const prop = await _wlSafeJson(propRes);
      const ov = await _wlSafeJson(ovRes);
      _wlLoaded = true;

      // Pin 2: cache proposals for the manual editor's FYI line whenever the
      // engine actually returned something usable — on a self-test failure,
      // deliberately leave the previous cache (stale-but-labeled) rather than
      // wiping it to nothing.
      if (prop.ok) _wlLastProposalPayload = prop.proposals || [];

      let html = _wlPendingBanner(ov.entries || []);

      // Manual mode must work even if the regression engine is broken or
      // frozen (server-side rationale in /api/weight_manual/tags' docstring)
      // — so the manual section is appended and wired on EVERY reachable
      // path below, not just the happy path.
      if (!prop.ok) {
        html += _wlBanner('error', 'Proposal engine failed its own math check — no suggestions will be shown. See Dexter logs.');
        html += _wlManualSectionHtml();
        body.innerHTML = html;
        _wireManualSection();
        return;
      }
      if (prop.frozen) {
        html += _wlBanner('warn', `Collecting fresh data since your last change — ${prop.records_since_last_change} of ${prop.needed} new records. Proposals resume when the window fills.`);
        html += _wlManualSectionHtml();
        body.innerHTML = html;
        _wireManualSection();
        return;
      }

      const proposals = prop.proposals || [];
      const significant = proposals.filter(p => p.significant);
      const nonSignificant = proposals.filter(p => !p.significant);
      const verifiedSignificant = significant.filter(p =>
        (p.mapping || 'unmapped') === 'verified' && p.proposed_delta != null && p.current_weight != null);

      html += _wlDigestHeader(verifiedSignificant, prop.last_reviewed_at);

      if (!significant.length) {
        html += `<div style="font-family:'Inter',sans-serif;font-size:11.5px;color:var(--txt2);padding:6px 0">No tag currently clears the evidence bar. That's the system working, not broken.</div>`;
      } else {
        html += significant.map(_wlProposalCard).join('');
      }
      html += _wlNonSignificant(nonSignificant);
      html += _wlManualSectionHtml();
      body.innerHTML = html;
      _wireCardButtons(body, verifiedSignificant);
      _wireManualSection();
    } catch (e) {
      body.innerHTML = _wlBanner('error', esc(e.message));
      // No manual section here — a hard fetch/network failure means we can't
      // even confirm the backend is reachable; refresh is the right next
      // action, not a half-working editor with no data behind it.
    }
  }
  window.loadWeightLab = loadWeightLab;

  const refreshBtn = document.getElementById('weightLabRefreshBtn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => loadWeightLab(true));
})();
