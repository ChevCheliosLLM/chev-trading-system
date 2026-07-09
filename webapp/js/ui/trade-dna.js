/* ============================================================
   Trade DNA hypothesis-to-trade outcome linking.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     TRADE DNA — hypothesis-to-trade outcome linking
     ============================================================ */

  function _linkHypToTrade(sym, openTsRaw) {
    try {
      const hist = JSON.parse(localStorage.getItem(_HYP_HISTORY_KEY) || '[]');
      if (!hist.length) return null;
      const openTs = openTsRaw ? new Date(openTsRaw).getTime() : null;
      // Find the most recent hypothesis for this symbol that was recorded
      // no more than 24h BEFORE the trade opened (Chev's thesis that set up the trade)
      const candidates = hist.filter(r => r.sym === sym);
      if (!candidates.length) return null;
      if (!openTs) return candidates[candidates.length - 1]; // fallback: latest
      const WINDOW = 24 * 60 * 60 * 1000; // 24h
      const valid = candidates.filter(r => r.ts <= openTs && (openTs - r.ts) < WINDOW);
      if (!valid.length) {
        // fallback: closest in time (within 48h either direction)
        const fallback = candidates
          .map(r => ({ r, diff: Math.abs(r.ts - openTs) }))
          .filter(x => x.diff < 48 * 3600000)
          .sort((a, b) => a.diff - b.diff);
        return fallback.length ? fallback[0].r : null;
      }
      return valid[valid.length - 1]; // most recent valid
    } catch(e) { return null; }
  }

  function _autoUpdateHypOutcomes(closed) {
    try {
      const hist = JSON.parse(localStorage.getItem(_HYP_HISTORY_KEY) || '[]');
      if (!hist.length) return;
      let changed = false;
      closed.forEach(trade => {
        const sym = trade.symbol || trade.pair || '';
        const openTs = trade.open_ts || trade.ts || null;
        const closeType = trade.close_type || '';
        const pnl = trade.pnl != null ? trade.pnl : (trade.result != null ? trade.result : null);
        const isWin = closeType === 'TP_HIT' || closeType === 'SIP_HIT' || closeType === 'WIN' || (pnl != null && pnl > 0);
        const outcome = isWin ? 'win' : 'loss';
        const rec = _linkHypToTrade(sym, openTs);
        if (!rec) return;
        const idx = hist.findIndex(r => r.sym === rec.sym && r.ts === rec.ts);
        if (idx >= 0 && hist[idx].outcome !== outcome) {
          hist[idx].outcome = outcome;
          changed = true;
        }
      });
      if (changed) localStorage.setItem(_HYP_HISTORY_KEY, JSON.stringify(hist));
    } catch(e) {}
  }

  function _buildDNAContent(rec, trade) {
    if (!rec) {
      return `<div class="dnaNoData">No Dexter analysis found near this trade's open time. Run Dexter before entries to build thesis history.</div>`;
    }
    const bias = (rec.bias || 'NEUTRAL').toUpperCase();
    const arrow = bias === 'LONG' ? '▲' : bias === 'SHORT' ? '▼' : '◆';
    const biasColor = bias === 'LONG' ? 'var(--green)' : bias === 'SHORT' ? 'var(--red)' : 'var(--gold)';
    const ago = rec.ts ? Math.round((Date.now() - rec.ts) / 60000) : null;
    const agoStr = ago == null ? '' : ago < 60 ? ago + 'm ago' : Math.round(ago/60) + 'h ago';
    const regime = (rec.regime || '').replace(/_/g, ' ') || '—';
    const regColor = regime.includes('BULL') ? 'var(--green)' : regime.includes('BEAR') ? 'var(--red)' : 'var(--gold)';
    const outcomeCls = rec.outcome === 'win' ? 'green' : rec.outcome === 'loss' ? 'red' : 'gold';
    const outcomeLabel = rec.outcome === 'win' ? '✓ WON' : rec.outcome === 'loss' ? '✗ LOST' : '○ OPEN';
    return `
      <div class="dnaHeader">
        <span style="font-size:13px;color:${biasColor}">${arrow}</span>
        <span class="dnaHypName">${rec.name || bias}</span>
        <span class="deepStatValue ${outcomeCls}" style="font-size:10px;margin-left:auto;padding:2px 8px;border-radius:var(--r-pill);background:rgba(0,0,0,0.3)">${outcomeLabel}</span>
      </div>
      <div class="dnaRows">
        <div class="dnaRow"><span class="dnaRowLabel">Regime at entry</span><span class="dnaRowVal" style="color:${regColor}">${regime || '—'}</span></div>
        <div class="dnaRow"><span class="dnaRowLabel">Chev's confidence</span><span class="dnaRowVal">${rec.conf != null ? rec.conf + '%' : '—'}</span></div>
        <div class="dnaRow"><span class="dnaRowLabel">Timeframe</span><span class="dnaRowVal">${(rec.tf || '').toUpperCase() || '—'}</span></div>
        ${agoStr ? `<div class="dnaRow"><span class="dnaRowLabel">Analysis recorded</span><span class="dnaRowVal">${agoStr}</span></div>` : ''}
      </div>`;
  }

  function _attachDNAHandlers(container) {
    container.querySelectorAll('.logDNABtn').forEach(btn => {
      btn.addEventListener('click', function(e) {
        e.stopPropagation(); // don't trigger row chart-load
        const panelId = btn.dataset.dna;
        const panel = document.getElementById(panelId);
        if (!panel) return;
        const isOpen = panel.classList.contains('open');
        // close all open DNA panels first
        container.querySelectorAll('.tradeDNAPanel.open').forEach(p => p.classList.remove('open'));
        container.querySelectorAll('.logDNABtn.active').forEach(b => b.classList.remove('active'));
        if (!isOpen) {
          const sym = btn.dataset.sym || '';
          const openTs = btn.dataset.openTs || null;
          const rec = _linkHypToTrade(sym, openTs);
          panel.innerHTML = _buildDNAContent(rec, null);
          panel.classList.add('open');
          btn.classList.add('active');
        }
      });
    });
  }

