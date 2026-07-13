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

  // PHASE 6D: shared "label + raw code" renderer -- friendly name in normal
  // text, raw tag code in small muted text beside/beneath it, used everywhere
  // a tag is displayed. Reuses friendlyTag()/TAG_REGISTRY (already used
  // throughout this file and the rest of the app, backed by /api/tag_registry)
  // rather than a second, separately-maintained label dictionary -- one
  // registry, everything reads from it.
  function _wlTagLabel(tag, inline) {
    const rawStyle = inline
      ? "font-family:'Share Tech Mono',monospace;font-size:9.5px;color:var(--txt3);margin-left:6px"
      : "display:block;font-family:'Share Tech Mono',monospace;font-size:9.5px;color:var(--txt3);margin-top:1px";
    return `${esc(friendlyTag(tag))}<span style="${rawStyle}">${esc(tag)}</span>`;
  }

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

  // PHASE 6D PART 2: freeze-progress rate estimate. In-memory only (no
  // localStorage, resets on page reload -- fine, this is a rough "how's it
  // going" read, never a persisted fact). This panel has no auto-polling
  // (see the file header -- first-open + manual refresh-button only), so a
  // "sample" is really "whenever Kev last checked in"; the rate math doesn't
  // care how sparse that is, it just measures real elapsed wall-clock time
  // between check-ins. Capped small buffer; rate is computed oldest-vs-newest
  // in the buffer so one noisy refresh-to-refresh gap doesn't swing the
  // estimate wildly. Buffer resets whenever frozen_since changes (a new
  // freeze window started) so a stale rate from a PREVIOUS freeze can never
  // leak into a new one's estimate.
  let _wlFreezeSamples = [];
  let _wlFreezeSampleSince = null;
  const _WL_FREEZE_SAMPLE_CAP = 6;

  function _wlRecordFreezeSample(frozenSince, recordsSinceChange) {
    if (_wlFreezeSampleSince !== frozenSince) {
      _wlFreezeSamples = [];
      _wlFreezeSampleSince = frozenSince;
    }
    _wlFreezeSamples.push({ t: Date.now(), n: recordsSinceChange });
    if (_wlFreezeSamples.length > _WL_FREEZE_SAMPLE_CAP) _wlFreezeSamples.shift();
  }

  // Returns '' (no estimate) whenever the math isn't trustworthy yet -- fewer
  // than 2 samples, no elapsed time, or a zero/negative record delta (can
  // happen if the freeze window itself just changed). Never a placeholder
  // guess dressed as a real number.
  function _wlFreezeEtaText(needed, recordsSinceChange) {
    if (_wlFreezeSamples.length < 2) return '';
    const oldest = _wlFreezeSamples[0];
    const newest = _wlFreezeSamples[_wlFreezeSamples.length - 1];
    const dtMin = (newest.t - oldest.t) / 60000;
    const dn = newest.n - oldest.n;
    if (dtMin <= 0 || dn <= 0) return '';
    const perMin = dn / dtMin;
    const remaining = needed - recordsSinceChange;
    if (remaining <= 0 || perMin <= 0) return '';
    const etaMin = remaining / perMin;
    if (etaMin < 90) return `~${Math.max(1, Math.round(etaMin))} min to go`;
    const etaHr = etaMin / 60;
    return `~${etaHr < 10 ? etaHr.toFixed(1) : Math.round(etaHr)} hr to go`;
  }

  function _wlFreezeBanner(prop) {
    _wlRecordFreezeSample(prop.frozen_since, prop.records_since_last_change);
    const needed = prop.needed > 0 ? prop.needed : 0;
    const pct = needed > 0 ? Math.min(100, Math.max(0, (prop.records_since_last_change / needed) * 100)) : 0;
    const sinceTxt = prop.frozen_since ? ` (since ${esc(prop.frozen_since)} UTC)` : '';
    const eta = _wlFreezeEtaText(needed, prop.records_since_last_change);
    const etaTxt = eta ? ` — ${eta}` : '';
    const bar = `<div style="margin-top:8px;height:8px;background:rgba(255,255,255,0.08);border-radius:4px;overflow:hidden">
      <div style="height:100%;width:${pct}%;background:rgba(240,180,41,0.55);border-radius:4px"></div>
    </div>`;
    return _wlBanner('warn', `Collecting fresh data since your last change${sinceTxt} — ${prop.records_since_last_change} of ${prop.needed} new records${etaTxt}. Proposals resume when the window fills.${bar}`);
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
        <span style="flex:1">${_wlTagLabel(e.tag, true)} ${e.delta > 0 ? '+' : ''}${e.delta}pt
          ${e.source === 'manual' ? '<span style="opacity:0.6">(manual)</span>' : ''}</span>
        <button class="wlUndoBtn" data-index="${i}" style="background:none;
          border:1px solid rgba(240,180,41,0.4);color:#f0b429;border-radius:4px;
          font-size:10px;padding:2px 8px;cursor:pointer">undo</button>
      </div>`;
    }).join('');
    return _wlBanner('warn', `⚠ ${pendingIdx.length} approved change(s) not yet live — takes effect within one scan cycle (~5 min), no restart needed.<div style="margin-top:6px">${rows}</div>`);
  }

  // PHASE 6D PART 1: "what the strategy believes" -- one row per verified-
  // mapping tag in the payload, horizontal bar sized by current_weight
  // (largest = full width), colored by what the shadow evidence currently
  // says. Renders from whatever proposals array it's given -- loadWeightLab
  // decides whether that's the fresh payload or the cached one during a
  // freeze. No backend calls of its own; every field it reads (mapping,
  // current_weight, significant, coef) is already in /api/weight_proposal.
  function _wlSnapshotPanel(proposals) {
    const verified = (proposals || []).filter(p =>
      (p.mapping || 'unmapped') === 'verified' && p.current_weight != null);
    if (!verified.length) {
      return `<div style="font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt3);padding:4px 0 10px">snapshot appears after the first analysis</div>`;
    }
    const sorted = verified.slice().sort((a, b) => b.current_weight - a.current_weight);
    // Normalize bar width against the largest POSITIVE weight only -- a future
    // negative baseline shouldn't compress every other bar's scale.
    const positiveMax = Math.max(0.01, ...sorted.map(p => p.current_weight).filter(w => w > 0));

    // .tBull/.tBear (toasts.css) are this app's real green/red convention --
    // the CSS variable names in the original brief (--bg-success etc.) don't
    // exist anywhere in this codebase and would render no color at all.
    const COLORS = {
      earning:  { fg: '#089981', bg: 'rgba(8,153,129,0.16)' },
      costing:  { fg: '#f23645', bg: 'rgba(242,54,69,0.16)' },
      unproven: { fg: 'var(--txt3)', bg: 'rgba(255,255,255,0.07)' },
    };

    const rows = sorted.map(p => {
      const status = !p.significant ? 'unproven' : (p.coef > 0 ? 'earning' : 'costing');
      const c = COLORS[status];
      const barPct = p.current_weight > 0 ? Math.max(4, (p.current_weight / positiveMax) * 100) : 3;
      return `
        <div style="display:flex;align-items:center;gap:8px;padding:3px 0">
          <div style="width:112px;min-width:112px;overflow:hidden">
            <div style="font-family:'Inter',sans-serif;font-size:11px;color:var(--txt1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(friendlyTag(p.tag))}</div>
            <div style="font-family:'Share Tech Mono',monospace;font-size:9.5px;color:var(--txt3)">${esc(p.tag)}</div>
          </div>
          <div style="flex:1;height:14px;background:rgba(255,255,255,0.04);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:${barPct}%;background:${c.bg};border-left:2px solid ${c.fg};border-radius:3px"></div>
          </div>
          <div style="width:40px;text-align:right;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--txt1)">${p.current_weight}pt</div>
          <div style="width:58px;text-align:right;font-family:'Inter',sans-serif;font-size:9.5px;font-weight:700;color:${c.fg}">${status}</div>
        </div>`;
    }).join('');

    return `
      <div style="border:1px solid rgba(147,112,219,0.25);border-radius:6px;padding:10px 12px;
        margin-bottom:12px;background:rgba(255,255,255,0.015)">
        <div style="font-family:'Inter',sans-serif;font-size:11.5px;font-weight:700;color:var(--txt1);margin-bottom:6px">
          Strategy snapshot — what the weights currently say</div>
        <div>${rows}</div>
      </div>`;
  }

  // PHASE 6D PART 3: change history -- "date · label old → new · source",
  // with an honest per-entry verdict beneath. Everything here is display
  // logic only; /api/weight_history does all the actual computation
  // (before/after split, win rates, the too_early gate).
  const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function _wlShortDate(ts) {
    // ts format is always "YYYY-MM-DD HH:MM:SS" (UTC), the backend's own
    // strftime convention (_reload_weight_overrides, _build_weight_history_payload).
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(ts || '');
    if (!m) return esc(ts || '');
    return `${MONTH_ABBR[parseInt(m[2], 10) - 1] || m[2]} ${parseInt(m[3], 10)}`;
  }

  function _wlHistorySourceHtml(source) {
    // weight_lab (an approved regression proposal) and manual (Kev's own
    // direct edit / the Phase 3 freeze markers) are both Kev-approved paths --
    // shown the same friendly way, with the raw source word kept muted
    // beside it so the distinction isn't lost, just de-emphasized.
    const friendly = (source === 'weight_lab' || source === 'manual') ? 'ratified by you' : esc(source || 'unknown');
    return `${friendly}<span style="font-family:'Share Tech Mono',monospace;font-size:9.5px;color:var(--txt3);margin-left:6px">${esc(source || '')}</span>`;
  }

  // Derives "old → new" from the tag's CURRENT live weight (current_weight,
  // from the already-fetched /api/weight_proposal payload) minus this
  // entry's own delta. Only ever called when this is the tag's ONLY entry in
  // the whole timeline (checked by the caller) -- with 2+ entries for the
  // same tag, intermediate historical values can't be safely reconstructed
  // from current_weight alone (ordering/compounding would need re-deriving
  // the full delta sequence), so those cases fall back to showing the delta.
  function _wlHistoryOldNew(tag, delta, proposalsByTag) {
    const row = proposalsByTag[tag];
    if (!row || row.current_weight == null) return null;
    return { old: row.current_weight - delta, new: row.current_weight };
  }

  function _wlHistoryEntry(e, tagCounts, proposalsByTag) {
    const date = _wlShortDate(e.applied_at || e.approved_at);

    // delta=0 entries are freeze markers (Phase 3's hand-edit reconciliation,
    // or any future one) -- one muted line, no verdict block, per this
    // phase's own explicit spec.
    if (e.delta === 0) {
      const note = (e.evidence && e.evidence.note) ? e.evidence.note : `${friendlyTag(e.tag)} freeze marker recorded`;
      return `<div style="font-family:'Inter',sans-serif;font-size:10px;color:var(--txt3);padding:4px 0;opacity:0.7">
        ${date} — marker: ${esc(note)}</div>`;
    }

    const canDerive = (tagCounts[e.tag] || 0) === 1;
    const derived = canDerive ? _wlHistoryOldNew(e.tag, e.delta, proposalsByTag) : null;
    const changeText = derived
      ? `${derived.old}pt → ${derived.new}pt`
      : `(${e.delta > 0 ? '+' : ''}${e.delta}pt)`;

    const v = e.verdict || {};
    const verdictLine = v.verdict_status === 'too_early'
      ? `too early to judge (${v.n_after || 0} of ${v.needed || 200} records)`
      : `Since then: win rate ${v.winrate_after != null ? v.winrate_after + '%' : 'n/a'}${v.winrate_before != null ? ` (was ${v.winrate_before}%)` : ''}`;

    return `
      <div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.06)">
        <div style="font-family:'Inter',sans-serif;font-size:11px;color:var(--txt1)">
          ${date} — ${_wlTagLabel(e.tag, true)} ${changeText} · ${_wlHistorySourceHtml(e.source)}
        </div>
        <div style="font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--txt2);margin-top:2px">
          ${esc(verdictLine)}
        </div>
      </div>`;
  }

  function _wlHistorySection(timeline, proposalsByTag) {
    if (!timeline || !timeline.length) {
      return `<div style="margin-top:14px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px;
        font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt3)">No weight changes recorded yet.</div>`;
    }
    const tagCounts = {};
    timeline.forEach(e => { tagCounts[e.tag] = (tagCounts[e.tag] || 0) + 1; });
    const rows = timeline.map(e => _wlHistoryEntry(e, tagCounts, proposalsByTag)).join('');
    return `
      <div style="margin-top:14px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px">
        <div style="font-family:'Inter',sans-serif;font-size:11px;font-weight:700;color:var(--txt1);margin-bottom:4px">
          Change history</div>
        <div>${rows}</div>
      </div>`;
  }

  // PHASE 6C: last_reviewed_at moved to its own always-visible top-of-panel
  // line (see loadWeightLab) -- this header no longer carries it, so it no
  // longer needs to be gated on that field at all, just the row count.
  function _wlDigestHeader(verifiedRows) {
    if (verifiedRows.length < 2) return '';
    const tableRows = verifiedRows.map(p => `
      <div style="display:flex;justify-content:space-between;gap:8px;font-family:'Share Tech Mono',monospace;
        font-size:10.5px;padding:2px 0;color:var(--txt2)">
        <span>${_wlTagLabel(p.tag, true)}</span>
        <span>n=${p.n} · ${fmtR(p.coef)}R · ${p.current_weight}→${p.effective_weight_preview}pt</span>
      </div>`).join('');
    return `
      <div style="border:1px solid var(--gold,#d4af37);border-radius:6px;padding:12px;margin-bottom:12px;
           background:rgba(212,175,55,0.06)">
        <div style="font-family:'Inter',sans-serif;font-size:12.5px;font-weight:700;color:var(--gold,#d4af37)">
          📋 Weekly digest — ${verifiedRows.length} verified proposals</div>
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

  // PHASE 6C: always-visible top-of-panel line -- previously last_reviewed_at
  // only ever appeared inside _wlDigestHeader, which itself only renders with
  // 2+ actionable proposals, so it was invisible the rest of the time.
  function _wlLastReviewedLine(lastReviewedAt) {
    const text = lastReviewedAt ? `Last reviewed ${esc(lastReviewedAt)} UTC` : 'Not reviewed yet';
    return `<div style="font-family:'Inter',sans-serif;font-size:10px;color:var(--txt3);margin-bottom:8px">${text}</div>`;
  }

  // PHASE 6C: this function now only ever renders the "actionable" subset --
  // significant AND verified AND the backend gave a real delta/current_weight
  // to act on (see loadWeightLab's `actionable` filter). Everything that used
  // to render an inline "not approvable" badge here (unverified/unmapped, or
  // simply not significant) now lives in _wlWatchingSection instead, each
  // with its own plain-English reason -- so a card only ever appears next to
  // a button that actually does something.
  function _wlProposalCard(p) {
    const canPropose = (p.mapping || 'unmapped') === 'verified' && p.proposed_delta != null && p.current_weight != null;
    if (!canPropose) return '';  // defensive no-op; loadWeightLab should never pass one of these here

    const name = friendlyTag(p.tag);
    const evLine = `n=${p.n} · effect ${fmtR(p.coef)}R · 95% CI [${fmtR(p.ci[0])}, ${fmtR(p.ci[1])}]`;
    const hasCross = p.n_with != null && p.winrate_with != null && p.winrate_without != null;
    const crossLine = hasCross
      ? `With this tag: ${p.winrate_with}% WR, ${fmtR(p.avg_netR_with)}R avg · Without: ${p.winrate_without}% WR, ${fmtR(p.avg_netR_without)}R avg`
      : '';
    const conflict = p.agreement === 'conflict';

    // Plain-English one-liner in place of the bare tag name -- "<TAG> setups
    // are losing/making money — proposal: <cur> → <new>?" The direction is
    // read straight off proposed_delta's sign, which the server itself derives
    // directly from coef's sign (dexter.py: "delta = 1 if coef > 0 else -1"),
    // so this is never an independent guess about direction.
    const deltaLabel = p.proposed_delta > 0 ? `+${p.proposed_delta}` : `${p.proposed_delta}`;
    const losing = p.proposed_delta < 0;
    const oneLiner = `${esc(name)} setups are ${losing ? 'losing' : 'making'} money — proposal: ${p.current_weight} → ${p.effective_weight_preview}?`;

    // ✓/⚠ badge: previously only the conflict (⚠) state was ever rendered --
    // agreement === 'agree' produced no visible indicator at all, so Kev only
    // ever saw a warning, never the reassurance that the raw numbers back up
    // the regression.
    let agreementBadge = '';
    if (p.agreement === 'conflict') {
      agreementBadge = `<span style="font-size:9.5px;font-weight:700;color:#f0b429;
        background:rgba(240,180,41,0.12);border:1px dashed rgba(240,180,41,0.5);
        border-radius:10px;padding:2px 7px" title="Ridge regression and the simple raw-average comparison point in different directions -- treat this proposal with extra suspicion">⚠ regression vs raw stats disagree</span>`;
    } else if (p.agreement === 'agree') {
      agreementBadge = `<span style="font-size:9.5px;font-weight:700;color:#5ee6a0;
        background:rgba(94,230,160,0.10);border:1px solid rgba(94,230,160,0.35);
        border-radius:10px;padding:2px 7px" title="Ridge regression and the simple raw-average comparison agree on direction">✓ regression and raw stats agree</span>`;
    }

    const actionEl = `<button class="wlApproveBtn" data-tag="${esc(p.tag)}" data-delta="${p.proposed_delta}"
         data-evidence='${esc(JSON.stringify({ n: p.n, coef: p.coef, ci: p.ci }))}'
         style="margin-left:auto;font-family:'Share Tech Mono',monospace;font-size:11px;
           font-weight:700;padding:4px 10px;border-radius:5px;cursor:pointer;
           ${conflict
             ? 'background:rgba(240,180,41,0.12);border:1px solid rgba(240,180,41,0.5);color:#f0b429'
             : 'background:rgba(212,175,55,0.12);border:1px solid var(--gold);color:var(--gold)'}">
         APPROVE ${deltaLabel}</button>`;

    return `
      <div style="border:1px solid rgba(147,112,219,0.25);border-radius:6px;padding:10px 12px;
        margin-bottom:8px;background:rgba(255,255,255,0.02)">
        <div style="font-family:'Inter',sans-serif;font-size:12.5px;font-weight:600;color:var(--txt1)">${oneLiner}</div>
        <div style="font-family:'Share Tech Mono',monospace;font-size:9.5px;color:var(--txt3);margin-top:1px">${esc(p.tag)}</div>
        <div style="font-family:'Share Tech Mono',monospace;font-size:10.5px;color:var(--txt2);margin-top:3px">${esc(evLine)}</div>
        ${crossLine ? `<div style="font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt2);margin-top:3px">${esc(crossLine)}</div>` : ''}
        ${agreementBadge ? `<div style="margin-top:4px">${agreementBadge}</div>` : ''}
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px;
             font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--txt1)">
          <span>${p.current_weight}pt → ${p.effective_weight_preview}pt</span>
          ${actionEl}
        </div>
        <div style="font-size:9px;color:var(--txt3);margin-top:6px;font-family:'Inter',sans-serif">
          Proposal only. Nothing changes until you approve — live within one scan cycle after that.</div>
      </div>`;
  }

  // PHASE 6C: replaces _wlNonSignificant. Now groups by APPROVABILITY, not
  // significance -- every proposal that isn't in loadWeightLab's `actionable`
  // set lands here (not significant yet, regardless of mapping; OR
  // significant but unverified/unmapped), each with its own reason, so a
  // significant-but-unapprovable tag no longer gets a full card mixed in next
  // to real, actionable ones (a dead button dressed as a live one).
  function _wlApprovabilityReason(p) {
    const mapping = p.mapping || 'unmapped';
    if (mapping === 'unmapped') return 'no live weight uses this code — informational only';
    if (mapping === 'unverified') return 'same name, unconfirmed mechanic — not wired for one-tap changes yet';
    return 'not enough evidence yet';  // verified mapping, just not significant
  }

  function _wlWatchingSection(rows) {
    if (!rows.length) return '';
    const items = rows.map(p => `
      <div style="display:flex;gap:10px;padding:4px 0;font-family:'Share Tech Mono',monospace;
        font-size:10.5px;color:var(--txt3);align-items:baseline">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:0.8">${_wlTagLabel(p.tag, true)}</span>
        <span style="white-space:nowrap">n=${p.n} · ${fmtR(p.coef)}R</span>
        <span style="white-space:nowrap;font-family:'Inter',sans-serif;font-size:9.5px;opacity:0.7">${esc(_wlApprovabilityReason(p))}</span>
      </div>`).join('');
    return `
      <details style="margin-top:6px">
        <summary style="font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt3)">
          watching, not yet approvable (${rows.length})</summary>
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

  function _wireCardButtons(root, actionable) {
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
    if (approveAllBtn) approveAllBtn.addEventListener('click', () => _wlApproveAll(actionable, approveAllBtn));
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

      // Pin 2: cache proposals for the manual editor's FYI line (and now also
      // PHASE 6D's snapshot panel) whenever the engine actually returned real
      // proposals — deliberately excludes a frozen response (its own
      // "proposals" field is always []) so a freeze can never wipe out the
      // last real snapshot; on a self-test failure the previous cache is left
      // alone too (stale-but-labeled beats fresh-but-empty either way).
      if (prop.ok && !prop.frozen) _wlLastProposalPayload = prop.proposals || [];

      // Lookup used by the change-history section's old->new derivation --
      // same fresh-or-cached source as the snapshot panel above.
      const proposalsByTag = {};
      (prop.ok && !prop.frozen ? (prop.proposals || []) : (_wlLastProposalPayload || []))
        .forEach(p => { proposalsByTag[p.tag] = p; });

      // PHASE 6D PART 3: change history fetched INDEPENDENTLY of the main
      // Promise.all above -- a hiccup on this endpoint must never take down
      // the core proposal-cards experience, only degrade this one section.
      let historyHtml;
      try {
        const histRes = await _apiFetch('/api/weight_history');
        const hist = await _wlSafeJson(histRes);
        historyHtml = _wlHistorySection(hist.timeline || [], proposalsByTag);
      } catch (e) {
        historyHtml = `<div style="margin-top:14px;border-top:1px solid rgba(255,255,255,0.08);padding-top:10px;
          font-family:'Inter',sans-serif;font-size:10.5px;color:var(--txt3)">Change history unavailable — ${esc(e.message)}</div>`;
      }

      // PHASE 6C: always visible at the top, regardless of which branch below
      // this ends up taking (error/frozen/normal) -- previously last_reviewed_at
      // only ever showed up inside the digest header, invisible the rest of
      // the time.
      let html = _wlLastReviewedLine(prop.last_reviewed_at);

      // PHASE 6D PART 1: strategy snapshot at the very top, above everything
      // else -- fresh data when available, the cache preserved above when
      // frozen (never the frozen response's own empty "proposals": []).
      if (prop.ok) {
        html += _wlSnapshotPanel(prop.frozen ? _wlLastProposalPayload : (prop.proposals || []));
      }
      html += _wlPendingBanner(ov.entries || []);

      // Manual mode must work even if the regression engine is broken or
      // frozen (server-side rationale in /api/weight_manual/tags' docstring)
      // — so the manual section is appended and wired on EVERY reachable
      // path below, not just the happy path. Change history is appended on
      // every reachable path too, for the same reason -- it's Kev's record
      // of what happened, independent of whether today's regression run
      // succeeded.
      if (!prop.ok) {
        html += _wlBanner('error', 'Proposal engine failed its own math check — no suggestions will be shown. See Dexter logs.');
        html += _wlManualSectionHtml();
        html += historyHtml;
        body.innerHTML = html;
        _wireManualSection();
        return;
      }
      if (prop.frozen) {
        // PHASE 6D PART 2: progress bar + rate-based ETA, replacing the plain
        // count line from 6C (which is still shown as text alongside the bar).
        html += _wlFreezeBanner(prop);
        html += _wlManualSectionHtml();
        html += historyHtml;
        body.innerHTML = html;
        _wireManualSection();
        return;
      }

      const proposals = prop.proposals || [];
      // PHASE 6C: "actionable" = the exact gate _wlProposalCard/approve-all
      // require -- significant, verified mapping, and a real delta/current_weight
      // to act on. Everything else (not significant yet, OR significant but
      // unverified/unmapped) goes into the single "watching, not yet
      // approvable" drawer instead of a full card. This answers "what can I
      // actually do right now," not "what cleared the stats bar."
      const actionable = proposals.filter(p =>
        p.significant && (p.mapping || 'unmapped') === 'verified' && p.proposed_delta != null && p.current_weight != null);
      const watching = proposals.filter(p => !actionable.includes(p));

      html += _wlDigestHeader(actionable);

      if (!actionable.length) {
        html += `<div style="font-family:'Inter',sans-serif;font-size:11.5px;color:var(--txt2);padding:6px 0">No tag currently clears the evidence bar. That's the system working, not broken.</div>`;
      } else {
        html += actionable.map(_wlProposalCard).join('');
      }
      html += _wlWatchingSection(watching);
      html += _wlManualSectionHtml();
      html += historyHtml;
      body.innerHTML = html;
      _wireCardButtons(body, actionable);
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
