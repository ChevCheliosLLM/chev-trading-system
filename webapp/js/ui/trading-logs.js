/* ============================================================
   Live/closed trade feed, trade overlay, gold-glow active state, DNA panels.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  const tradingLogsEl  = document.getElementById('tradingLogs');
  const closedTradesEl = document.getElementById('closedTrades');
  let lastTradeStatuses = {};
  let _activeTradeKey = null;  // pair|open_ts of the live trade whose overlay is showing (for click-toggle + glow)

  function pnlClass(pnl) { return pnl >= 0 ? 'pnl-up' : 'pnl-down'; }
  function fmtPnl(pnl) { return (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2); }

  const _sidebarCollapsed = {};
  function toggleSidebar(id) {
    _sidebarCollapsed[id] = !_sidebarCollapsed[id];
    const group = document.getElementById(id + 'Group');
    const arrow = document.getElementById(id + 'Arrow');
    if (group) group.style.display = _sidebarCollapsed[id] ? 'none' : '';
    if (arrow) arrow.textContent = _sidebarCollapsed[id] ? '▶' : '▼';
  }
  function moodEmoji(pnl, isSip = false) {
    let img;
    if (isSip)          img = 'lifeline.png';  // SIP — the lifeline grabbed. Risk became profit.
    else if (pnl >= 8)  img = 'fire.png';      // big win — Chev is on fire
    else if (pnl >= 2)  img = 'not-bad-2.png'; // solid win
    else if (pnl > 0)   img = 'not-bad-1.png'; // small win — acceptable
    else if (pnl > -3)  img = 'lets-see.png';  // scratch / tiny loss — meh
    else if (pnl > -5)  img = 'angry.png';     // proper loss — annoyed
    else if (pnl > -10) img = 'oh-no.png';     // real damage
    else                img = 'furious.png';   // big hit — Chev is furious
    return `<img class="moodIcon" src="emoji/${img}" alt="">`;
  }

  let activeLogTrade = null;
  let confPriceLines = [];

  function clearConfLines() {
    confPriceLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    confPriceLines = [];
  }

  function drawConfLines(confPrices) {
    clearConfLines();
    if (!confPrices || !Object.keys(confPrices).length) return;
    const CONF_COLORS = {
      SR_S: '#089981', SR_R: '#f23645',
      FB_50: '#9b59b6', FB_618: '#d4af37', FB_65: '#d4af37', FB_786: '#e67e22',
    };
    Object.entries(confPrices).forEach(([key, price]) => {
      if (!price) return;
      const color = CONF_COLORS[key] || '#787b86';
      const line = candleSeries.createPriceLine({
        price: parseFloat(price), color, lineWidth: 1,
        lineStyle: 2, axisLabelVisible: true, title: key.replace('_', ' '),
      });
      confPriceLines.push(line);
    });
  }

  function renderTradingLogs(data) {
    const active = (data.trades || data.active || []).filter(t => t.status === 'OPEN');
    const closedList = (data.closed || []).slice().reverse().slice(0, 5);
    let html = '';
    if (!active.length) {
      html += '<div class="emptyNote chev"><img src="emoji/chilling.png" alt="" style="width:16px;height:16px;vertical-align:middle;margin-right:6px;opacity:0.8">I\'m flat. <b>Waiting for the right setup.</b></div>';
    } else {
      active.forEach(t => {
        const sym = t.symbol || t.pair || '';
        const justClosed = lastTradeStatuses[t.row] && lastTradeStatuses[t.row] !== t.status;
        const confData = encodeURIComponent(JSON.stringify(t.confluence_prices || {}));
        const assetType = sym.endsWith('USDT') ? 'crypto' : (sym.includes('/') ? 'forex' : 'stock');
        const isLong_l = (t.direction || '').toLowerCase() === 'long';
        const isSip_l  = t.is_sip || (isLong_l ? t.sl > t.entry : t.sl < t.entry);
        const sipBadge = isSip_l ? '<span style="font-size:8px;color:#d4af37;font-weight:700;letter-spacing:.5px">SIP</span>' : '';
        // Holding time
        let holdHtml = '';
        if (t.open_ts) {
          try {
            const openMs = new Date(t.open_ts.replace(' ', 'T')).getTime();
            if (!isNaN(openMs)) {
              const diffH = Math.floor((Date.now() - openMs) / 3600000);
              const diffM = Math.floor(((Date.now() - openMs) % 3600000) / 60000);
              holdHtml = `<span class="logHold">${diffH >= 24 ? Math.floor(diffH/24)+'d' : diffH > 0 ? diffH+'h' : diffM+'m'}</span>`;
            }
          } catch(e) {}
        }
        // R:R badge
        let rrHtml = '';
        if (t.entry != null && t.sl != null && t.tp != null) {
          const risk   = Math.abs(t.entry - t.sl);
          const reward = Math.abs(t.tp - t.entry);
          if (risk > 0) {
            const rr = (reward / risk).toFixed(1);
            const rrCls = reward >= risk * 1.5 ? 'good' : (reward < risk ? 'bad' : '');
            rrHtml = `<span class="rrBadge ${rrCls}">R ${rr}</span>`;
          }
        }
        html += `
          <div class="logRow${justClosed ? ' pulse' : ''}" data-pair="${sym}" data-type="${assetType}" data-conf="${confData}" data-row="${t.row||''}" data-entry="${t.entry||''}" data-sl="${t.sl||''}" data-tp="${t.tp||''}" data-direction="${t.direction||''}" data-ts="${t.open_ts||''}" data-tags="${t.tags||''}" data-status="${t.status||''}">
            <span class="logMood">${_pairLogoHtml(sym)}</span>
            <span class="logPair">${sym}</span>
            <span class="logType">${t.trade_type || 'day'}</span>
            <span class="logLev">${t.leverage}x</span>
            ${holdHtml}
            ${sipBadge}
            ${rrHtml}
            <span class="logPnl ${pnlClass(t.live_pnl)}">${moodEmoji(t.live_pnl ?? 0, t.is_sip)} ${fmtPnl(t.live_pnl)}</span>
            <button class="jump-to-chart-btn" data-symbol="${sym}" data-tf="1h" data-type="${assetType}" data-entry="${t.entry||''}" data-sl="${t.sl||''}" data-tp="${t.tp||''}" data-direction="${t.direction||''}" data-ts="${t.open_ts||''}" data-tags="${t.tags||''}" data-status="${t.status||''}">Chart</button>
          </div>`;
        lastTradeStatuses[t.row] = t.status;
      });
    }
    tradingLogsEl.innerHTML = html;

    // Update summary bar
    const netPnl = active.reduce((s, t) => s + (t.live_pnl || 0), 0);
    const balance = data.balance;
    const balEl   = document.getElementById('tsbBalance');
    const pnlEl   = document.getElementById('tsbNetPnl');
    const cntEl   = document.getElementById('tsbOpenCount');
    const badgeEl = document.getElementById('chevOpenBadge');
    if (balEl && balance != null) balEl.textContent = '$' + balance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (pnlEl) {
      pnlEl.textContent = (netPnl >= 0 ? '+$' : '-$') + Math.abs(netPnl).toFixed(2);
      pnlEl.className = 'tsbValue ' + (netPnl > 0 ? 'pos' : netPnl < 0 ? 'neg' : 'flat');
    }
    if (cntEl) cntEl.textContent = active.length;
    if (badgeEl) {
      const n = active.length;
      badgeEl.textContent = n;
      badgeEl.style.display = n ? '' : 'none';
    }
    const hdrLive = document.getElementById('hdrLive');
    if (hdrLive) hdrLive.classList.toggle('has-data', active.length > 0);
    const hdrClosed = document.getElementById('hdrClosed');
    // mark section headers with has-data
    document.querySelectorAll('.tradeSectionHeader').forEach(h => {
      const next = h.nextElementSibling;
      if (next && !next.querySelector('.emptyNote')) h.classList.add('has-data');
      else h.classList.remove('has-data');
    });

    // Closed trades — separate section
    if (closedTradesEl) {
      let closedHtml = '';
      if (!closedList.length) {
        closedHtml = '<div class="emptyNote chev">No closed trades on record yet.</div>';
      } else {
        closedList.forEach(c => {
          const sym = c.symbol || c.pair || '';
          const pnl = c.pnl != null ? c.pnl : (c.result != null ? c.result : 0);
          const outcome = (c.outcome || '').toUpperCase();
          const ct = c.close_type || '';
          const resultEmoji = moodEmoji(pnl, ct === 'SIP_HIT');
          const ctLabel = ct === 'TP_HIT' ? 'TP' : ct === 'SIP_HIT' ? 'SIP' : ct === 'SL_HIT' ? 'SL' : 'closed';
          const dirEmoji = (c.direction || '').toUpperCase() === 'LONG' ? '⬆️' : '⬇️';
          const assetType = sym.endsWith('USDT') ? 'crypto' : (sym.includes('/') ? 'forex' : 'stock');
          const openTs = c.open_ts || c.ts || '';
          const ctColor = ct === 'SIP_HIT' ? 'color:#d4af37' : ct === 'TP_HIT' ? 'color:#089981' : ct === 'SL_HIT' ? 'color:#f23645' : '';
          const dnaId = 'dna_' + (c.id || sym + '_' + (openTs || Math.random().toString(36).slice(2)));
          closedHtml += `
            <div class="logRow lastClosedRow" data-pair="${sym}" data-type="${assetType}" data-conf="{}" data-row="" data-entry="${c.entry||''}" data-sl="${c.sl||''}" data-tp="${c.tp||''}" data-direction="${c.direction||''}" data-ts="${openTs}" data-tags="${c.tags||''}" data-status="CLOSED" style="padding-right:4px">
              <span class="logDir">${dirEmoji}</span>
              <span class="logPair">${sym}</span>
              <span class="logType" style="${ctColor}" title="${ct ? esc(friendlyReason(ct)) : ''}">${ctLabel}</span>
              <span class="logLev">closed</span>
              <span class="logPnl ${pnlClass(pnl)}">${resultEmoji} ${fmtPnl(pnl)}</span>
              <button class="logDNABtn" data-dna="${dnaId}" data-sym="${sym}" data-open-ts="${openTs}" title="Why did Chev trade this?">WHY?</button>
            </div>
            <div class="tradeDNAPanel" id="${dnaId}"></div>`;
        });
      }
      closedTradesEl.innerHTML = closedHtml;
    }

    // Performance stats
    _updatePerfSection(closedList);

    // Live trailing — if user is viewing a trade whose SL/TP/entry changed, refresh overlay
    if (_activeTrade && _activeTrade.symbol) {
      const match = active.find(t => (t.symbol || t.pair) === _activeTrade.symbol);
      if (match) {
        const newSl = parseFloat(match.sl) || null, newTp = parseFloat(match.tp) || null, newEntry = parseFloat(match.entry) || null;
        const newIsSip = match.is_sip || false;
        if (newSl !== _activeTrade.sl || newTp !== _activeTrade.tp || newEntry !== _activeTrade.entry || newIsSip !== _activeTrade.is_sip) {
          _activeTrade.sl = newSl; _activeTrade.tp = newTp; _activeTrade.entry = newEntry;
          _activeTrade.is_sip = newIsSip;
          _refreshTradeOverlayLines();
        }
      }
    }

    function _attachLogRowHandlers(container) {
      container.querySelectorAll('.logRow[data-pair]').forEach(row => {
        row.addEventListener('click', async () => {
          const pair = row.dataset.pair;
          const type = row.dataset.type;
          const rowId = row.dataset.row;
          const conf = JSON.parse(decodeURIComponent(row.dataset.conf || '{}'));
          const rowKey = pair + '|' + (row.dataset.ts || '');
          // Toggle OFF — clicking the already-active trade clears its lines and returns to normal.
          if (_activeTradeKey === rowKey) {
            clearChevTools();
            redrawAll();
            return;
          }
          document.querySelectorAll('.logRow').forEach(r => { r.style.borderLeft = ''; r.classList.remove('activeTrade'); });
          const tradeSnap = {
            entry:     parseFloat(row.dataset.entry) || null,
            sl:        parseFloat(row.dataset.sl)    || null,
            tp:        parseFloat(row.dataset.tp)    || null,
            direction: row.dataset.direction,
            open_ts:   row.dataset.ts,
            tags:      row.dataset.tags,
            status:    row.dataset.status || 'OPEN',
            symbol:    pair,
          };
          currentSymbol = pair; currentType = type;
          drawings = loadDrawings(currentSymbol); rsiDrawings = loadRsiDrawings(currentSymbol); updateObjTree();
          _syncDrawings(currentSymbol); _subscribeDrawings(currentSymbol);
          watchlistInner.querySelectorAll('.watchRow').forEach(r => r.classList.remove('active'));
          clearChevTools();
          activeLogTrade = rowId;
          _activeTradeKey = rowKey;
          row.classList.add('activeTrade');
          if (conf && (conf.RSI_DIV_T1 || conf.RSI_DIV_T2) && currentTf !== '4h') {
            currentTf = '4h';
            document.querySelectorAll('[data-tf]').forEach(b => b.classList.toggle('active', b.dataset.tf === '4h'));
          }
          await loadChart(currentSymbol, currentTf, currentType);
          startPolling();
          activateTradeOverlay(tradeSnap, conf);
          _scrollToEntry();
        });
      });
    }
    _attachLogRowHandlers(tradingLogsEl);
    if (closedTradesEl) {
      _attachLogRowHandlers(closedTradesEl);
      _attachDNAHandlers(closedTradesEl);
    }

    tradingLogsEl.addEventListener('click', async (e) => {
      const btn = e.target.closest('.jump-to-chart-btn');
      if (!btn) return;
      e.stopPropagation();
      const symbol = btn.dataset.symbol;
      const tf = btn.dataset.tf || '1h';
      const type = btn.dataset.type || (symbol.endsWith('USDT') ? 'crypto' : (symbol.includes('/') ? 'forex' : 'stock'));
      const row = btn.closest('.logRow');
      const conf = row ? JSON.parse(decodeURIComponent(row.dataset.conf || '{}')) : {};
      const tradeSnap = {
        entry:     parseFloat(btn.dataset.entry) || null,
        sl:        parseFloat(btn.dataset.sl)    || null,
        tp:        parseFloat(btn.dataset.tp)    || null,
        direction: btn.dataset.direction,
        open_ts:   btn.dataset.ts,
        tags:      btn.dataset.tags,
        status:    btn.dataset.status || 'OPEN',
        symbol:    symbol,
      };
      currentSymbol = symbol; currentType = type;
      drawings = loadDrawings(currentSymbol); rsiDrawings = loadRsiDrawings(currentSymbol); updateObjTree();
      _syncDrawings(currentSymbol); _subscribeDrawings(currentSymbol);
      watchlistInner.querySelectorAll('.watchRow').forEach(r => r.classList.remove('active'));
      clearChevTools();
      await loadChart(currentSymbol, tf, currentType);
      startPolling();
      activateTradeOverlay(tradeSnap, conf);
      _scrollToEntry();
    });
    // Re-apply the gold glow to the active trade row after the list is rebuilt (polling re-renders it).
    if (_activeTradeKey) {
      document.querySelectorAll('.logRow[data-pair]').forEach(r => {
        const k = r.dataset.pair + '|' + (r.dataset.ts || '');
        r.classList.toggle('activeTrade', k === _activeTradeKey);
      });
    }
  }

  // A single missed poll (every-5s network blip) used to immediately overwrite good data
  // with "Can't reach Firebase" -- confirmed via live testing this was flapping on
  // transient failures, not a real outage (the badge count from the last success stayed
  // correct while only the row content got blanked). Only show the error after several
  // consecutive failures; a lone blip now just keeps showing the last known-good render.
  let _tradingLogsFailCount = 0;
  const TRADING_LOGS_FAIL_THRESHOLD = 3;
  async function refreshTradingLogs() {
    try {
      const res = await fetch(`${FIREBASE_BASE}/monitor.json?_t=${Date.now()}`);
      if (!res.ok) throw new Error('firebase error');
      const data = await res.json();
      if (data) {
        _tradingLogsFailCount = 0;
        _latestFbData = data;
        renderTradingLogs(data);
      }
    } catch (e) {
      _tradingLogsFailCount++;
      if (_tradingLogsFailCount >= TRADING_LOGS_FAIL_THRESHOLD) {
        tradingLogsEl.innerHTML = '<div class="emptyNote chev"><img src="emoji/oh-no.png" alt="" style="width:14px;height:14px;vertical-align:middle;margin-right:6px;opacity:0.8">Can\'t reach Firebase. <b>Is Dexter online?</b></div>';
      }
    }
  }

  document.getElementById('resetBalance').addEventListener('click', async () => {
    if (!confirm('Reset Dexter\'s balance to $10,000 and clear all open/pending trades?')) return;
    try {
      const res = await _apiFetch('/api/reset_balance', { method: 'POST' });
      const data = await res.json();
      if (data.ok) { alert('Done — balance reset to $10,000. Restart Dexter to fully reload state.'); refreshTradingLogs(); }
      else alert('Reset failed: ' + data.error);
    } catch(e) { alert('Could not reach Dexter.'); }
  });

