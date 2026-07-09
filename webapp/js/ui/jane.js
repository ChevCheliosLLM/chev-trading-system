/* ============================================================
   Jane's open/closed trades panel.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     JANE'S TRADES
     ============================================================ */
  function renderJaneTrades(data) {
    const active     = (data.active || []).slice(0, 8);
    const lastClosed = data.last_closed;
    const balance    = data.balance;

    if (balance != null) {
      const lbl = document.getElementById('janeBalanceLabel');
      if (lbl) lbl.textContent = '$' + balance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    const janeBadge = document.getElementById('janeOpenBadge');
    if (janeBadge) janeBadge.textContent = active.length > 0 ? `(${active.length})` : '';

    let html = '';
    if (!active.length) {
      html = '<div class="emptyNote chev">Jane\'s flat. <b>No live positions.</b></div>';
    } else {
      active.forEach(t => {
        const pnl = t.live_pnl || 0;
        const isPending = t.status === 'PENDING';
        const isLong_j  = (t.direction || '').toLowerCase() === 'long';
        const isSip_j   = t.is_sip || (isLong_j ? t.sl > t.entry : t.sl < t.entry);
        const verdictMap = { APPROVED: '✅', CAUTION: '⚠️', REJECTED: '❌' };
        const vIcon = verdictMap[t.chev_verdict] || '';
        const vTip  = t.chev_feedback ? ` title="${t.chev_feedback.replace(/"/g,"'")}"` : '';
        const verdictSpan = vIcon
          ? `<span${vTip} style="font-size:9px;opacity:0.9;cursor:default">${vIcon}</span>` : '';
        const dataPosAttrs = (t.entry != null && t.sl != null)
          ? ` data-jane-pos="1" data-pair="${t.pair}" data-direction="${t.direction||''}" data-entry="${t.entry}" data-sl="${t.sl}" data-tp="${t.tp||''}" data-ts="${t.open_ts||''}" style="cursor:pointer;border-left:2px solid rgba(212,175,55,0.45)"`
          : ` style="border-left:2px solid rgba(212,175,55,0.45)"`;
        html += `<div class="logRow"${dataPosAttrs}>
          <span class="logMood">${_pairLogoHtml(t.pair)}</span>
          <span class="logPair">${t.pair}</span>
          <span class="logType">${t.trade_type || 'day'}</span>
          <span class="logLev">${isPending ? '<img class="accentIcon" src="emoji/time.png" alt="" style="width:19px;height:19px;margin-right:2px;opacity:0.75">pend' : (t.leverage || 1) + 'x'}</span>
          ${verdictSpan}
          <span class="logPnl ${isPending ? '' : pnlClass(pnl)}">${isPending ? 'WAIT' : fmtPnl(pnl)}</span>
        </div>`;
      });
    }
    document.getElementById('janeTradesEl').innerHTML = html;

    // Wire Jane open trades for position view (entry/SL/TP zones, no confluence)
    document.getElementById('janeTradesEl').querySelectorAll('[data-jane-pos]').forEach(row => {
      row.addEventListener('click', async () => {
        const pair      = row.dataset.pair;
        const entry     = parseFloat(row.dataset.entry) || null;
        const sl        = parseFloat(row.dataset.sl)    || null;
        const tp        = parseFloat(row.dataset.tp)    || null;
        const direction = row.dataset.direction;
        const open_ts   = row.dataset.ts;
        document.querySelectorAll('[data-jane-pos]').forEach(r => r.style.outline = '');
        row.style.outline = '1px solid rgba(212,175,55,0.5)';
        clearChevTools();
        if (pair !== currentSymbol) {
          currentSymbol = pair;
          currentType   = pair.endsWith('USDT') ? 'crypto' : (pair.includes('/') ? 'forex' : 'stock');
          await loadChart(currentSymbol, currentTf, currentType);
          startPolling();
        }
        // Position-view only: entry/SL/TP zones without confluence analysis
        _activeTrade = { entry, sl, tp, direction, open_ts, status: 'OPEN', symbol: pair, conf: {} };
        _overlayEntry = true; _overlayAnalysisOn = false;
        document.getElementById('tradeOverlayBar').style.display = 'flex';
        document.getElementById('btnOverlayEntry').classList.add('active');
        document.getElementById('btnOverlayAnalysis').classList.remove('active');
        _refreshTradeOverlayLines();
        _scrollToEntry();
      });
    });

    let cornerHtml = '';
    const closedList = Array.isArray(data.closed) ? data.closed.slice().reverse().slice(0, 5) : (lastClosed ? [lastClosed] : []);
    if (closedList.length) {
      closedList.forEach(c => {
        const ct  = c.close_type || (c.outcome === 'WIN' ? 'TP_HIT' : c.is_sip ? 'SIP_HIT' : 'SL_HIT');
        const isSipClose = ct === 'SIP_HIT' || c.is_sip;
        const em  = moodEmoji(result, isSipClose);
        const ctLabel = ct === 'TP_HIT' ? 'TP' : ct === 'SIP_HIT' ? 'SIP' : ct === 'SL_HIT' ? 'SL' : 'closed';
        const ctColor = isSipClose ? 'color:#d4af37' : ct === 'TP_HIT' ? 'color:#089981' : ct === 'SL_HIT' ? 'color:#f23645' : '';
        const dirEm  = (c.direction || '').toUpperCase() === 'LONG' ? '⬆️' : '⬇️';
        const result = c.result != null ? c.result : c.pnl != null ? c.pnl : 0;
        cornerHtml += `<div class="logRow lastClosedRow${isSipClose ? ' sip-closed-bg' : ''}" style="border-left:2px solid rgba(212,175,55,0.45)">
          <span class="logDir">${dirEm}</span>
          <span class="logPair">${c.pair}</span>
          <span class="logType" style="${ctColor}" title="${esc(friendlyReason(ct))}">${ctLabel}</span>
          <span class="logLev">closed</span>
          <span class="logPnl ${isSipClose ? 'sip-closed' : pnlClass(result)}">${em} ${fmtPnl(result)}</span>
        </div>`;
      });
    } else {
      cornerHtml = '<div class="emptyNote chev">No Jane trades closed yet.</div>';
    }
    document.getElementById('janeCornerEl').innerHTML = cornerHtml;
  }

  async function refreshJaneTrades() {
    try {
      const res = await _apiFetch('/api/jane/trades');
      if (!res.ok) throw new Error(`Dexter ${res.status} — restart Dexter`);
      renderJaneTrades(await res.json());
      _setDexterStatus(true);
    } catch(e) {
      _setDexterStatus(false);
      const msg = e.message && e.message.includes('Dexter') ? e.message : 'Dexter offline or needs restart';
      document.getElementById('janeTradesEl').innerHTML = `<div class="emptyNote">${msg}</div>`;
    }
  }

