/* ============================================================
   WEIGHT LAB — Ridge-regression confluence tag weight proposals (Engine pane).
   Fetches /api/weight_proposal + /api/weight_overrides on Engine-pane first
   open (see panel-toggle.js's engine-tab branch), manual refresh via the
   header's refresh icon, no polling. Approve/revert reuse the existing
   _apiFetch()/X-Chev-Key mechanism (webapp/js/config/state.js) — no separate
   auth flow. Tag display names reuse friendlyTag()/_loadTagRegistry()
   (webapp/js/ui/watchlist.js), same as every other tag-leaderboard render site.
   ============================================================ */
(function() {
  let _wlLoaded = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }
  function fmtR(n) { return (n >= 0 ? '+' : '') + n.toFixed(2); }

  function _wlBody() { return document.getElementById('weightLabBody'); }

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

  async function _wlApprove(tag, delta, evidence, btn) {
    btn.disabled = true;
    try {
      const r = await _apiFetch('/api/weight_proposal/approve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tag, delta, evidence }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || 'approve failed');
      _showToast(`<span>Weight Lab — ${esc(friendlyTag(tag))} queued (${delta > 0 ? '+' : ''}${delta}pt). Restart Dexter to arm it.</span>`, 6000);
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
      _showToast(`<span>Weight Lab — change reverted.</span>`, 4000);
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
        <span style="flex:1">${esc(friendlyTag(e.tag))} ${e.delta > 0 ? '+' : ''}${e.delta}pt</span>
        <button class="wlUndoBtn" data-index="${i}" style="background:none;
          border:1px solid rgba(240,180,41,0.4);color:#f0b429;border-radius:4px;
          font-size:10px;padding:2px 8px;cursor:pointer">undo</button>
      </div>`;
    }).join('');
    return _wlBanner('warn', `⚠ ${pendingIdx.length} approved change(s) NOT yet live — restart Dexter to arm them.<div style="margin-top:6px">${rows}</div>`);
  }

  function _wlProposalCard(p) {
    const name = friendlyTag(p.tag);
    const evLine = `n=${p.n} · effect ${fmtR(p.coef)}R · 95% CI [${fmtR(p.ci[0])}, ${fmtR(p.ci[1])}]`;
    const hasCross = p.n_with != null && p.winrate_with != null && p.winrate_without != null;
    const crossLine = hasCross
      ? `With this tag: ${p.winrate_with}% WR, ${fmtR(p.avg_netR_with)}R avg · Without: ${p.winrate_without}% WR, ${fmtR(p.avg_netR_without)}R avg`
      : '';
    const conflict = p.agreement === 'conflict';
    const canPropose = p.proposed_delta != null && p.current_weight != null;
    const deltaLabel = canPropose ? (p.proposed_delta > 0 ? `+${p.proposed_delta}` : `${p.proposed_delta}`) : null;
    const weightLine = canPropose
      ? `${p.current_weight}pt → ${p.effective_weight_preview}pt`
      : `current: ${p.current_weight != null ? p.current_weight + 'pt' : 'n/a (unmapped)'}`;

    const approveBtn = canPropose
      ? `<button class="wlApproveBtn" data-tag="${esc(p.tag)}" data-delta="${p.proposed_delta}"
           data-evidence='${esc(JSON.stringify({ n: p.n, coef: p.coef, ci: p.ci }))}'
           style="margin-left:auto;font-family:'Share Tech Mono',monospace;font-size:11px;
             font-weight:700;padding:4px 10px;border-radius:5px;cursor:pointer;
             ${conflict
               ? 'background:rgba(240,180,41,0.12);border:1px solid rgba(240,180,41,0.5);color:#f0b429'
               : 'background:rgba(212,175,55,0.12);border:1px solid var(--gold);color:var(--gold)'}">
           APPROVE ${deltaLabel}</button>`
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
          ${approveBtn}
        </div>
        <div style="font-size:9px;color:var(--txt3);margin-top:6px;font-family:'Inter',sans-serif">
          Proposal only. Nothing changes until you approve AND restart Dexter.</div>
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

  function _wireCardButtons(root) {
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
      const prop = await propRes.json();
      const ov = await ovRes.json();
      _wlLoaded = true;

      let html = _wlPendingBanner(ov.entries || []);

      if (!prop.ok) {
        html += _wlBanner('error', 'Proposal engine failed its own math check — no suggestions will be shown. See Dexter logs.');
        body.innerHTML = html;
        return;
      }
      if (prop.frozen) {
        html += _wlBanner('warn', `Collecting fresh data since your last change — ${prop.records_since_last_change} of ${prop.needed} new records. Proposals resume when the window fills.`);
        body.innerHTML = html;
        return;
      }

      const proposals = prop.proposals || [];
      const significant = proposals.filter(p => p.significant);
      const nonSignificant = proposals.filter(p => !p.significant);

      if (!significant.length) {
        html += `<div style="font-family:'Inter',sans-serif;font-size:11.5px;color:var(--txt2);padding:6px 0">No tag currently clears the evidence bar. That's the system working, not broken.</div>`;
      } else {
        html += significant.map(_wlProposalCard).join('');
      }
      html += _wlNonSignificant(nonSignificant);
      body.innerHTML = html;
      _wireCardButtons(body);
    } catch (e) {
      body.innerHTML = _wlBanner('error', `Weight Lab failed to load: ${esc(e.message)}`);
    }
  }
  window.loadWeightLab = loadWeightLab;

  const refreshBtn = document.getElementById('weightLabRefreshBtn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => loadWeightLab(true));

  const toggleBtn = document.getElementById('weightLabToggleBtn');
  if (toggleBtn) toggleBtn.addEventListener('click', () => {
    const body = _wlBody();
    if (!body) return;
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? '' : 'none';
    toggleBtn.textContent = collapsed ? '▾' : '▸';
  });
})();
