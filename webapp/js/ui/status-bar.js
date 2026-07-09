/* ============================================================
   Status bar live clock + Dexter connection indicator.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     STATUS BAR — live clock + Dexter connection indicator
     ============================================================ */
  // Performance stats section (computed from Firebase closed trades)
  function _updatePerfSection(closed) {
    const total = closed.length;

    // Show perf section once we have data
    const perfSec = document.getElementById('perfSection');
    const perfHdr = document.getElementById('hdrPerf');
    if (perfSec) perfSec.style.display = total ? '' : 'none';
    if (perfHdr) perfHdr.style.display = total ? '' : 'none';

    if (!total) return;

    const isWin  = c => (c.outcome || '').toUpperCase() === 'WIN' || c.close_type === 'TP_HIT' || c.close_type === 'SIP_HIT';
    const isLoss = c => c.close_type === 'SL_HIT';
    const isSip  = c => c.close_type === 'SIP_HIT';
    const pnlOf  = c => parseFloat(c.pnl != null ? c.pnl : c.result) || 0;
    const dirOf  = c => (c.direction || c.side || '').toUpperCase();

    const wins   = closed.filter(isWin);
    const losses = closed.filter(isLoss);
    const sips   = closed.filter(isSip);
    const avgWin  = wins.length   ? wins.reduce((s,c)=>s+Math.abs(pnlOf(c)),0)/wins.length   : 0;
    const avgLoss = losses.length ? losses.reduce((s,c)=>s+Math.abs(pnlOf(c)),0)/losses.length : 0;
    const totalPnl = closed.reduce((s,c)=>s+pnlOf(c),0);
    const wr = Math.round(wins.length / total * 100);
    const expectancy = ((wr/100) * avgWin) - ((1 - wr/100) * avgLoss);

    // Basic bar
    const set = (id, txt, cls) => {
      const el = document.getElementById(id); if (!el) return;
      el.textContent = txt;
      if (cls) el.className = 'perfValue ' + cls;
    };
    set('perfTotal',    total);
    set('perfWinRate',  wr + '%', wr >= 50 ? 'up' : 'down');
    set('perfAvgWin',   '+$' + avgWin.toFixed(0),  'up');
    set('perfAvgLoss',  '-$' + avgLoss.toFixed(0), 'down');
    set('perfTotal_pnl', (totalPnl >= 0 ? '+$' : '-$') + Math.abs(totalPnl).toFixed(0), totalPnl >= 0 ? 'up' : 'down');

    // Also update topbar win rate
    const tsbWR = document.getElementById('tsbWinRate');
    if (tsbWR) { tsbWR.textContent = wr + '%'; tsbWR.style.color = wr >= 50 ? 'var(--green)' : 'var(--red)'; }

    // Directional breakdown
    const longs  = closed.filter(c => dirOf(c) === 'LONG'  || dirOf(c) === 'BUY');
    const shorts = closed.filter(c => dirOf(c) === 'SHORT' || dirOf(c) === 'SELL');
    const dirDiv = document.getElementById('perfDirBreakdown');
    if (dirDiv && (longs.length || shorts.length)) {
      dirDiv.style.display = '';
      const longWins  = longs.filter(isWin);
      const shortWins = shorts.filter(isWin);
      const lWR = longs.length  ? Math.round(longWins.length  / longs.length  * 100) : 0;
      const sWR = shorts.length ? Math.round(shortWins.length / shorts.length * 100) : 0;
      document.getElementById('longWR').textContent  = lWR + '%';
      document.getElementById('longSub').textContent = longWins.length + '/' + longs.length + ' long trades';
      document.getElementById('longBar').style.width = lWR + '%';
      document.getElementById('shortWR').textContent  = sWR + '%';
      document.getElementById('shortSub').textContent = shortWins.length + '/' + shorts.length + ' short trades';
      document.getElementById('shortBar').style.width = sWR + '%';
    }

    // Streak
    let streak = 0, streakType = '';
    for (let i = closed.length - 1; i >= 0; i--) {
      const w = isWin(closed[i]);
      if (!streakType) streakType = w ? 'W' : 'L';
      if ((streakType === 'W') === w) streak++;
      else break;
    }

    // Best/worst symbol
    const symPnl = {};
    closed.forEach(c => {
      const sym = c.pair || c.symbol || '?';
      symPnl[sym] = (symPnl[sym] || 0) + pnlOf(c);
    });
    const symEntries = Object.entries(symPnl).sort((a,b) => b[1] - a[1]);
    const bestSym  = symEntries[0];
    const worstSym = symEntries[symEntries.length - 1];

    // Profit factor
    const grossWin  = wins.reduce((s,c)=>s+Math.abs(pnlOf(c)),0);
    const grossLoss = losses.reduce((s,c)=>s+Math.abs(pnlOf(c)),0);
    const pf = grossLoss > 0 ? (grossWin / grossLoss) : (grossWin > 0 ? 99 : 0);

    // Deep stats grid
    const deepGrid = document.getElementById('deepStatsGrid');
    if (deepGrid) {
      deepGrid.style.display = '';
      const streakEmoji = streak >= 3 ? (streakType === 'W' ? '🔥' : '💀') : '';
      deepGrid.innerHTML = `
        <div class="deepStatCard">
          <div class="deepStatLabel">Expectancy</div>
          <div class="deepStatValue ${expectancy >= 0 ? 'green' : 'red'}">${expectancy >= 0 ? '+' : ''}$${Math.abs(expectancy).toFixed(0)}</div>
          <div class="deepStatSub">per trade avg</div>
        </div>
        <div class="deepStatCard">
          <div class="deepStatLabel">Profit Factor</div>
          <div class="deepStatValue ${pf >= 1.5 ? 'green' : pf >= 1 ? 'gold' : 'red'}">${pf >= 99 ? '∞' : pf.toFixed(2)}</div>
          <div class="deepStatSub">gross win ÷ gross loss</div>
        </div>
        <div class="deepStatCard">
          <div class="deepStatLabel">Current Streak</div>
          <div class="deepStatValue ${streakType === 'W' ? 'green' : 'red'}">${streak}${streakType}</div>
          <div class="deepStatSub">${streak >= 3 ? streakEmoji + ' running hot' : 'last ' + streak + ' trades'}</div>
        </div>
        <div class="deepStatCard">
          <div class="deepStatLabel">R:R Ratio</div>
          <div class="deepStatValue ${avgLoss > 0 ? (avgWin/avgLoss >= 1.5 ? 'green' : avgWin/avgLoss >= 1 ? 'gold' : 'red') : 'gold'}">${avgLoss > 0 ? (avgWin/avgLoss).toFixed(2) : '—'}</div>
          <div class="deepStatSub">avg win ÷ avg loss</div>
        </div>
        ${bestSym ? `<div class="deepStatCard">
          <div class="deepStatLabel">Best Symbol</div>
          <div class="deepStatValue green" style="font-size:13px">${bestSym[0]}</div>
          <div class="deepStatSub">+$${Math.abs(bestSym[1]).toFixed(0)} realized</div>
        </div>` : ''}
        ${worstSym && worstSym[1] < 0 ? `<div class="deepStatCard">
          <div class="deepStatLabel">Watch Out For</div>
          <div class="deepStatValue red" style="font-size:13px">${worstSym[0]}</div>
          <div class="deepStatSub">-$${Math.abs(worstSym[1]).toFixed(0)} realized</div>
        </div>` : ''}
      `;
    }

    // SIP row
    const sipRow = document.getElementById('perfSipRow');
    if (sipRow && sips.length) {
      sipRow.style.display = '';
      sipRow.innerHTML = `<img src="emoji/lifeline.png" style="width:12px;height:12px;vertical-align:middle;margin-right:4px"><span>${sips.length}</span> trades closed as SIP — risk became locked profit`;
    }

    // Auto-update hypothesis outcomes in localStorage, then refresh tracker display
    if (typeof _autoUpdateHypOutcomes === 'function') {
      _autoUpdateHypOutcomes(closed);
      if (typeof _renderHypHistory === 'function') _renderHypHistory();
    }

    // Regime win rate breakdown (uses hypothesis history to tag each trade's regime)
    const regimeEl = document.getElementById('regimeBreakdown');
    if (regimeEl && typeof _linkHypToTrade === 'function') {
      const regBuckets = { bull: { wins: 0, total: 0 }, bear: { wins: 0, total: 0 }, range: { wins: 0, total: 0 } };
      closed.forEach(trade => {
        const sym = trade.symbol || trade.pair || '';
        const openTs = trade.open_ts || trade.ts || null;
        const rec = _linkHypToTrade(sym, openTs);
        if (!rec || !rec.regime) return;
        const r = rec.regime.toUpperCase();
        const bucket = r.includes('BULL') ? 'bull' : r.includes('BEAR') ? 'bear' : 'range';
        regBuckets[bucket].total++;
        if (isWin(trade)) regBuckets[bucket].wins++;
      });
      const anyData = Object.values(regBuckets).some(b => b.total > 0);
      if (anyData) {
        regimeEl.style.display = '';
        ['bull', 'bear', 'range'].forEach(key => {
          const b = regBuckets[key];
          const wr = b.total > 0 ? Math.round(b.wins / b.total * 100) : null;
          const wrEl  = document.getElementById('regimeWR'  + key.charAt(0).toUpperCase() + key.slice(1));
          const barEl = document.getElementById('regimeBar' + key.charAt(0).toUpperCase() + key.slice(1));
          const cntEl = document.getElementById('regimeCnt' + key.charAt(0).toUpperCase() + key.slice(1));
          if (wrEl)  wrEl.textContent  = wr != null ? wr + '%' : '—';
          if (barEl) barEl.style.width = wr != null ? wr + '%' : '0%';
          if (cntEl) cntEl.textContent = b.total + 'T';
        });
      } else {
        regimeEl.style.display = 'none';
      }
    }
  }

  // Update chat header with current chart context
  function _updateChatContext() {
    const el = document.getElementById('chatContextLine');
    if (el) el.textContent = currentSymbol + ' · ' + (currentTf || '').toUpperCase();
  }

  let _dexterAlive = false;
  function _setDexterStatus(ok) {
    _dexterAlive = ok;
    const dot  = document.getElementById('sbDexterDot');
    const lbl  = document.getElementById('sbDexterLabel');
    if (!dot || !lbl) return;
    dot.className = 'sbDot ' + (ok ? 'live' : 'dead');
    lbl.className = ok ? 'live' : '';
    lbl.textContent = ok ? 'Dexter live' : 'Dexter offline';
  }
  function _updateSbClock() {
    const t = new Date();
    const pad = n => String(n).padStart(2,'0');
    const el = document.getElementById('sbTime');
    if (el) el.textContent = pad(t.getHours()) + ':' + pad(t.getMinutes()) + ':' + pad(t.getSeconds()) + ' UTC' + (t.getTimezoneOffset() < 0 ? '+' : '-') + Math.abs(t.getTimezoneOffset()/60);
  }
  _updateSbClock();
  setInterval(_updateSbClock, 1000);

  // Update BTC price in status bar from watchlist data when available
  function _updateSbBtc(price, change) {
    const el = document.getElementById('sbBtcPrice');
    if (!el || price == null) return;
    const sign = change >= 0 ? '+' : '';
    const arrow = change >= 0 ? '▲' : '▼';
    el.textContent = 'BTC ' + Number(price).toLocaleString('en-US', { maximumFractionDigits: 0 }) + ' ' + arrow + ' ' + sign + change.toFixed(2) + '%';
    el.className = change >= 0 ? 'up' : 'down';
  }

  /* ---- Twemoji: replace all emoji text with crisp SVGs ---- */
  if (window.twemoji) {
    const _twOpts = { folder: 'svg', ext: '.svg' };
    const _tw = (el) => twemoji.parse(el, _twOpts);
    // Auto-parse any new DOM content (dynamic trade logs, watchlist, chat, etc.)
    let _twBusy = false;
    new MutationObserver((mutations) => {
      if (_twBusy) return;
      _twBusy = true;
      for (const m of mutations) {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1 && !(n.tagName === 'IMG' && n.classList.contains('emoji'))) _tw(n);
        }
      }
      _twBusy = false;
    }).observe(document.body, { childList: true, subtree: true });
    // Parse everything already on the page
    _tw(document.body);
  }
