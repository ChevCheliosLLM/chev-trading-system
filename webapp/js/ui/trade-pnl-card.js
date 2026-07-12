/* ============================================================
   PHASE 6 Task 3: click-to-show P&L card for a live trade's entry line.
   RECON (done before writing this): entry/SL/TP lines are drawn two ways --
   real LightweightCharts price lines (candleSeries.createPriceLine, in
   chev-corner.js's _refreshTradeOverlayLines()) and canvas badges/zones
   (drawing.js's redrawAll(), engine.js's _drawLivePosition() -- which
   already shows an always-on floating %+leverage P&L badge at the CURRENT
   price line; this card is additive, not a replacement, and lives at the
   ENTRY line instead). Live P&L in dollars already flows to the frontend
   as `t.live_pnl` on each open trade (see trading-logs.js's logPnl render)
   -- reused here rather than invented; R-multiple is computed fresh from
   the live chart price against entry/stop, so it never goes stale between
   ticks even if live_pnl itself only updates when trading-logs.js's own
   change-detection block happens to fire (that block's own staleness gap
   is fixed alongside this, see the comment at its call site).
   Read-only card: no controls that touch the live trade appear here, ever.
   ============================================================ */
(function () {
  const card = document.getElementById('tradePnlCard');
  const closeBtn = document.getElementById('tpcCloseBtn');
  if (!card || !closeBtn) return;

  let _tpcOpen = false;

  function fmtSigned(v, decimals) {
    return (v >= 0 ? '+' : '') + v.toFixed(decimals);
  }

  function render() {
    if (!_activeTrade || _activeTrade.entry == null) { close(); return; }
    const entry = _activeTrade.entry, sl = _activeTrade.sl, tp = _activeTrade.tp;
    const isLong = (_activeTrade.direction || '').toLowerCase().includes('long');
    const sym = _activeTrade.symbol || _activeTrade.pair || '';
    const cp = currentCandles.length ? currentCandles[currentCandles.length - 1].close : null;

    const dirEl = document.getElementById('tpcDirection');
    dirEl.textContent = isLong ? 'LONG' : 'SHORT';
    dirEl.className = isLong ? 'long' : 'short';
    document.getElementById('tpcSymbol').textContent = sym;
    document.getElementById('tpcEntry').textContent = entry.toLocaleString(undefined, { maximumFractionDigits: 6 });

    const pnlEl = document.getElementById('tpcPnl');
    let rMultiple = null;
    if (cp != null && sl != null) {
      const riskDist = Math.abs(entry - sl);
      if (riskDist) rMultiple = ((cp - entry) / riskDist) * (isLong ? 1 : -1);
    }
    if (rMultiple != null || _activeTrade.live_pnl != null) {
      let txt = rMultiple != null ? fmtSigned(rMultiple, 2) + 'R' : '—';
      if (_activeTrade.live_pnl != null) {
        txt += ' · ' + (_activeTrade.live_pnl >= 0 ? '+$' : '-$') + Math.abs(_activeTrade.live_pnl).toFixed(2);
      }
      pnlEl.textContent = txt;
      const up = rMultiple != null ? rMultiple >= 0 : _activeTrade.live_pnl >= 0;
      pnlEl.className = up ? 'up' : 'down';
    } else {
      pnlEl.textContent = '—'; pnlEl.className = '';
    }

    const stopEl = document.getElementById('tpcStopDist');
    stopEl.textContent = (cp != null && sl != null) ? fmtSigned((sl - cp) / cp * 100, 2) + '%' : '—';
    const tpEl = document.getElementById('tpcTpDist');
    tpEl.textContent = (cp != null && tp != null) ? fmtSigned((tp - cp) / cp * 100, 2) + '%' : '—';
  }

  function openAt(clientX, clientY) {
    _tpcOpen = true;
    card.classList.add('open');
    render();
    const rect = card.getBoundingClientRect();
    let left = clientX + 12, top = clientY - rect.height / 2;
    left = Math.min(left, window.innerWidth - rect.width - 8);
    top = Math.max(8, Math.min(top, window.innerHeight - rect.height - 8));
    card.style.left = left + 'px';
    card.style.top = top + 'px';
  }

  function close() {
    _tpcOpen = false;
    card.classList.remove('open');
  }

  // Click the entry line (anywhere along its width, matching how a full-width
  // horizontal price line actually looks) to toggle the card. Only when no
  // drawing tool is active and nothing is mid-drag -- same guard drawing.js's
  // own click handler uses for its "plain click, not a tool placement" case.
  chartWrap.addEventListener('click', (e) => {
    if (activeTool || dragMode) return;
    if (!_overlayEntry || !_activeTrade || _activeTrade.entry == null) return;
    const ey = priceToY(_activeTrade.entry);
    if (ey == null) return;
    const raw = rawPos(e);
    if (Math.abs(raw.y - ey) < 6) {
      if (_tpcOpen) close(); else openAt(e.clientX, e.clientY);
    }
  });

  closeBtn.addEventListener('click', close);
  document.addEventListener('click', (e) => {
    if (_tpcOpen && !card.contains(e.target) && !chartWrap.contains(e.target)) close();
  });

  window._closeTradePnlCard = close;
  // Called from chart-core.js's existing loadChart()/pollLatestCandle() call
  // sites (same hook point as window.updateSymbolHeader) so the numbers stay
  // live without a second polling loop.
  window._updateTradePnlCard = function () { if (_tpcOpen) render(); };
})();
