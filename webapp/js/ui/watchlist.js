/* ============================================================
   Left watchlist panel: group tabs, watch rows, add/remove, price refresh.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     WATCHLIST
     ============================================================ */
  const leftPanel = document.getElementById('leftPanel');
  const leftToggle = document.getElementById('leftToggle');
  const watchlistInner = document.getElementById('watchlistInner');
  const groupTabsEls = document.querySelectorAll('.groupTab');
  const addPairBtn = document.getElementById('addPairBtn');

  leftToggle.addEventListener('click', () => {
    leftPanel.classList.toggle('open');
    // PHASE 4 Task 1: #leftPanel is now a real flex sibling (was an absolute
    // overlay), so #chartWrap genuinely reflows on its own -- the manual
    // priceInfo/statusEl left-offset hack this used to need is gone, and
    // charts need the same fitContent/resize-sync nudge panel-toggle.js
    // already uses for the right Intel panel, run after the same 220ms
    // width transition so it fires once the new layout has settled.
    setTimeout(() => {
      if (window._chevChart) window._chevChart.timeScale().fitContent();
      if (window.syncRsiCanvasSize) window.syncRsiCanvasSize();
      if (window._syncRsiAxisWidth) window._syncRsiAxisWidth();
    }, 220);
  });

  groupTabsEls.forEach(tab => {
    tab.addEventListener('click', () => {
      groupTabsEls.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      currentGroup = tab.dataset.group;
      buildWatchlistRows();
      refreshWatchlistPrices();
    });
  });

  function iconUrl(ticker) { return `https://cdn.jsdelivr.net/gh/atomiclabs/cryptocurrency-icons@master/32/color/${ticker}.png`; }
  function logoUrl(domain) { return `https://www.google.com/s2/favicons?sz=32&domain=${domain}`; }
  function cssId(symbol) { return symbol.replace(/[^a-zA-Z0-9]/g, ''); }

  function watchRowVisual(item) {
    if (item.type === 'crypto') return `<img class="logo" src="${iconUrl(item.icon)}" alt="" onerror="this.style.display='none'" />`;
    if (item.type === 'stock') {
      const domain = STOCK_DOMAINS[item.symbol];
      return domain ? `<img class="logo" src="${logoUrl(domain)}" alt="" onerror="this.style.display='none'" />` : '';
    }
    if (item.type === 'forex') return `<span class="flagEmoji">${FOREX_FLAGS[item.symbol] || '🏳️'}</span>`;
    return '';
  }

  function _pairLogoHtml(pair) {
    if (!pair) return '';
    if (pair.includes('/')) {
      const flag = FOREX_FLAGS[pair] || '🏳️';
      return `<span style="font-size:17px;line-height:1;flex-shrink:0">${flag}</span>`;
    }
    if (pair.endsWith('USDT') || pair.endsWith('BUSD')) {
      const ticker = pair.replace(/USDT$|BUSD$/, '').toLowerCase();
      return `<img src="${iconUrl(ticker)}" alt="${ticker}" style="width:20px;height:20px;border-radius:50%;flex-shrink:0;display:block" onerror="this.style.display='none'">`;
    }
    const domain = STOCK_DOMAINS[pair];
    if (domain) return `<img src="${logoUrl(domain)}" alt="${pair}" style="width:20px;height:20px;border-radius:4px;flex-shrink:0;display:block" onerror="this.style.display='none'">`;
    return `<span style="width:20px;height:20px;border-radius:50%;background:rgba(255,255,255,0.08);display:inline-flex;align-items:center;justify-content:center;font-size:9px;color:var(--txt2);font-weight:700;flex-shrink:0">${pair.charAt(0)}</span>`;
  }

  function buildWatchlistRows() {
    const items = groups[currentGroup] || [];
    watchlistInner.innerHTML = items.map(item => `
      <div class="watchRow${item.symbol === currentSymbol ? ' active' : ''}" data-symbol="${item.symbol}" data-type="${item.type}">
        <span class="left">
          <span class="tradeDot" id="dot-${cssId(item.symbol)}" title="No open trade"></span>
          <span class="ideaDot" id="idot-${cssId(item.symbol)}" title="Chev has an idea on this"></span>
          ${watchRowVisual(item)}
          <span class="sym">${item.label}</span>
        </span>
        <span class="right">
          <canvas class="sparkCanvas" id="sp-${cssId(item.symbol)}" width="52" height="22" title="${item.symbol} price movement"></canvas>
          <span class="priceBlock">
            <div class="px" id="px-${cssId(item.symbol)}">--</div>
            <div class="chg" id="chg-${cssId(item.symbol)}">--</div>
          </span>
          <button class="starBtn${item.starred ? ' active' : ''}" data-star="${item.symbol}" title="Star for Radar">★</button>
          <button class="removeBtn" data-remove="${item.symbol}" title="Remove">×</button>
        </span>
      </div>
    `).join('');

    watchlistInner.querySelectorAll('.watchRow').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.classList.contains('removeBtn') || e.target.classList.contains('starBtn')) return;
        _selectSymbol(row.dataset.symbol, row.dataset.type);
      });
    });
    watchlistInner.querySelectorAll('.removeBtn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        groups[currentGroup] = groups[currentGroup].filter(p => p.symbol !== btn.dataset.remove);
        saveGroups();
        buildWatchlistRows();
      });
    });
    watchlistInner.querySelectorAll('.starBtn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const item = groups[currentGroup].find(p => p.symbol === btn.dataset.star);
        if (item) {
          item.starred = !item.starred;
          saveGroups();
          btn.classList.toggle('active', item.starred);
          if (typeof _renderRadar === 'function') _renderRadar();
        }
      });
    });
    _filterWatchlist();
    // Re-draw sparklines from existing buffer after rows are rebuilt
    const items2 = groups[currentGroup] || [];
    items2.forEach(item => { if (_sparkBuffers && _sparkBuffers[item.symbol]) _drawSparkline(item.symbol); });
  }

  function _filterWatchlist() {
    const q = (document.getElementById('watchlistSearch')?.value || '').toLowerCase().trim();
    watchlistInner.querySelectorAll('.watchRow').forEach(row => {
      if (!q) { row.style.display = ''; return; }
      const sym = (row.dataset.symbol || '').toLowerCase();
      row.style.display = sym.includes(q) ? '' : 'none';
    });
  }

  document.getElementById('watchlistSearch')?.addEventListener('input', _filterWatchlist);

  // PHASE 2 bug fix / Task 1: shared symbol-switch path — the exact steps the
  // watchlist row click handler above already did, now used by it AND by the quick
  // topbar search below (both the dropdown-match and raw-typed-symbol paths), so
  // there is exactly one place that knows how to switch symbols, not several
  // slightly-different copies. loadChart() itself already calls _updateChatContext()
  // at the end, so callers don't need their own extra call.
  function _selectSymbol(symbol, type) {
    currentSymbol = symbol;
    currentType = type;
    drawings = loadDrawings(currentSymbol);
    rsiDrawings = loadRsiDrawings(currentSymbol);
    updateObjTree();
    _syncDrawings(currentSymbol);
    _subscribeDrawings(currentSymbol);
    watchlistInner.querySelectorAll('.watchRow').forEach(r => r.classList.toggle('active', r.dataset.symbol === symbol));
    clearChevTools();
    return loadChart(currentSymbol, currentTf, currentType).then(startPolling);
  }

  // Quick topbar symbol jump — autocomplete dropdown of matching watchlist symbols
  // (Task 1). The dropdown is a new addition; the underlying "type an arbitrary
  // symbol and press Enter to jump anyway" behavior already existed and is kept as
  // the fallback for anything typed that matches nothing in any watchlist group.
  const qsInput    = document.getElementById('quickSearchInput');
  const qsDropdown = document.getElementById('quickSearchDropdown');
  let _qsMatches      = [];
  let _qsSelectedIdx  = -1;

  function _qsAllSymbols() {
    // Reuses the SAME canonical watchlist data every other panel reads — never a
    // second symbol list. Searches all three groups (crypto/forex/stocks), not just
    // whichever tab is currently selected in the left sidebar.
    return Object.values(groups).flat();
  }

  function _qsFilter(q) {
    const needle = q.trim().toLowerCase();
    if (!needle) return [];
    return _qsAllSymbols()
      .filter(item => item.symbol.toLowerCase().includes(needle) || (item.label || '').toLowerCase().includes(needle))
      .slice(0, 8);
  }

  function _qsRender() {
    if (!qsDropdown) return;
    if (!_qsMatches.length) {
      qsDropdown.innerHTML = '<div class="qsEmpty">No matching symbol — press Enter to jump anyway</div>';
    } else {
      qsDropdown.innerHTML = _qsMatches.map((item, i) => `
        <div class="qsItem${i === _qsSelectedIdx ? ' active' : ''}" data-idx="${i}">
          <span class="qsItemSym">${item.label}</span>
          <span class="qsItemType">${item.type}</span>
        </div>
      `).join('');
      qsDropdown.querySelectorAll('.qsItem').forEach(el => {
        // mousedown (not click) fires before the input's blur, so the dropdown is
        // still in the DOM with its data intact when this handler reads it.
        el.addEventListener('mousedown', (e) => {
          e.preventDefault();
          const item = _qsMatches[+el.dataset.idx];
          _qsPick(item.symbol, item.type);
        });
      });
    }
    qsDropdown.classList.add('open');
  }

  function _qsClose() {
    if (!qsDropdown) return;
    qsDropdown.classList.remove('open');
    qsDropdown.innerHTML = '';
    _qsMatches = [];
    _qsSelectedIdx = -1;
  }

  function _qsPick(symbol, type) {
    qsInput.value = '';
    _qsClose();
    qsInput.blur();
    _selectSymbol(symbol, type);
  }

  qsInput?.addEventListener('input', function() {
    _qsMatches = _qsFilter(this.value);
    _qsSelectedIdx = _qsMatches.length ? 0 : -1;
    if (this.value.trim()) _qsRender(); else _qsClose();
  });

  qsInput?.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') { this.value = ''; _qsClose(); this.blur(); return; }
    if (e.key === 'ArrowDown') {
      if (!_qsMatches.length) return;
      e.preventDefault();
      _qsSelectedIdx = Math.min(_qsSelectedIdx + 1, _qsMatches.length - 1);
      _qsRender();
      return;
    }
    if (e.key === 'ArrowUp') {
      if (!_qsMatches.length) return;
      e.preventDefault();
      _qsSelectedIdx = Math.max(_qsSelectedIdx - 1, 0);
      _qsRender();
      return;
    }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (_qsMatches.length) {
      const item = _qsMatches[_qsSelectedIdx >= 0 ? _qsSelectedIdx : 0];
      _qsPick(item.symbol, item.type);
      return;
    }
    // Fallback: nothing in the dropdown matched — jump to the raw typed symbol
    // anyway (existing behavior, preserved exactly).
    const raw = this.value.trim().toUpperCase();
    if (!raw) return;
    const sym = raw.includes('/') ? raw : raw.replace(/[^A-Z0-9]/g,'');
    const type = sym.endsWith('USDT') || sym.endsWith('BTC') ? 'crypto' : sym.includes('/') ? 'forex' : 'stock';
    this.value = ''; _qsClose(); this.blur();
    _selectSymbol(sym, type);
  });

  document.addEventListener('click', (e) => {
    if (qsDropdown && qsDropdown.classList.contains('open') && !e.target.closest('#quickSearchWrap')) _qsClose();
  });

  async function refreshWatchlistPrices() {
    const items = groups[currentGroup] || [];
    const cryptoItems = items.filter(i => i.type === 'crypto');
    const otherItems = items.filter(i => i.type !== 'crypto');

    if (cryptoItems.length) {
      try {
        const symbolsParam = encodeURIComponent(JSON.stringify(cryptoItems.map(i => i.symbol)));
        const res = await fetch(`https://api.binance.com/api/v3/ticker/24hr?symbols=${symbolsParam}`);
        if (res.ok) {
          const data = await res.json();
          data.forEach(t => applyWatchPrice(t.symbol, parseFloat(t.lastPrice), parseFloat(t.priceChangePercent), {
            high: parseFloat(t.highPrice), low: parseFloat(t.lowPrice), volume: parseFloat(t.volume), changeAbs: parseFloat(t.priceChange),
          }));
        }
      } catch (e) {}
    }

    const stockItems = items.filter(i => i.type === 'stock');
    for (const item of stockItems) {
      try {
        const key = getFinnhubKey();
        const res = await fetch(`https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(item.symbol)}&token=${key}`);
        const data = await res.json();
        if (data.c) {
          const changePct = data.pc ? ((data.c - data.pc) / data.pc) * 100 : 0;
          // PHASE 4 Task 2: Finnhub's /quote gives today's h/l but no volume --
          // symbol-header.js falls back to a candle-derived volume estimate.
          applyWatchPrice(item.symbol, data.c, changePct, { high: data.h, low: data.l, changeAbs: data.d });
        }
      } catch (e) {}
    }
  }

  // Forex prices: Firebase primary (Dexter pushes every 3s) → FreeForexAPI fallback
  const _forexSessionOpen = {};
  let _forexApiDownUntil = 0;   // ms epoch; while in the future, skip the fallback fetch entirely
  async function refreshForexPrices() {
    const forexItems = groups.forex || [];
    if (!forexItems.length) return;

    // Primary: Firebase prices — near-instant, same feed as trade monitor
    if (_latestFbData && _latestFbData.prices) {
      let matched = 0;
      forexItems.forEach(item => {
        const key = item.symbol.replace('/', '_'); // EUR/USD → EUR_USD
        const price = _latestFbData.prices[key];
        if (!price) return;
        matched++;
        if (!_forexSessionOpen[item.symbol]) _forexSessionOpen[item.symbol] = price;
        const changePct = ((price - _forexSessionOpen[item.symbol]) / _forexSessionOpen[item.symbol]) * 100;
        applyWatchPrice(item.symbol, price, changePct);
      });
      if (matched > 0) return;
    }

    // Fallback: FreeForexAPI (slow, rate-limited, works when Dexter is offline).
    // Backed off after a failure -- this function is polled every 3s (see the
    // setInterval below), and the provider being down/CORS-blocked must never
    // turn into a request every 3s forever (that's the retry storm + console
    // spam this guard exists to stop). One warning, then quiet until the
    // cooldown expires -- watchlist prices just keep showing their last-known
    // value (or "—" if never set) in the meantime.
    if (Date.now() < _forexApiDownUntil) return;
    try {
      const pairParam = forexItems.map(i => i.symbol.replace('/', '')).join(',');
      const res = await fetch(`https://www.freeforexapi.com/api/live?pairs=${pairParam}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.code === 200 && data.rates) {
        forexItems.forEach(item => {
          const key = item.symbol.replace('/', '');
          const r = data.rates[key];
          if (!r || !r.rate) return;
          const price = r.rate;
          if (!_forexSessionOpen[item.symbol]) _forexSessionOpen[item.symbol] = price;
          const changePct = ((price - _forexSessionOpen[item.symbol]) / _forexSessionOpen[item.symbol]) * 100;
          applyWatchPrice(item.symbol, price, changePct);
        });
      }
    } catch (e) {
      console.warn('[watchlist] FreeForexAPI unavailable, backing off 5 min:', e.message);
      _forexApiDownUntil = Date.now() + 5 * 60 * 1000;
    }
  }

  const _lastPriceMap = {};
  function applyWatchPrice(symbol, price, changePct, stats) {
    const pxEl = document.getElementById(`px-${cssId(symbol)}`);
    const chgEl = document.getElementById(`chg-${cssId(symbol)}`);
    if (!pxEl || !chgEl) return;
    const isJpy = symbol.toUpperCase().includes('JPY');
    const decimals = symbol.includes('/') ? (isJpy ? 3 : 5) : 4;
    const formatted = price.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    // Flash animation: only when price actually changed direction
    const prevPrice = _lastPriceMap[symbol];
    if (prevPrice != null && price !== prevPrice) {
      const dir = price > prevPrice ? 'flash-up' : 'flash-down';
      pxEl.classList.remove('flash-up', 'flash-down');
      void pxEl.offsetWidth; // force reflow to restart animation
      pxEl.classList.add(dir);
    }
    _lastPriceMap[symbol] = price;
    pxEl.textContent = formatted;
    chgEl.textContent = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
    chgEl.className = 'chg ' + (changePct >= 0 ? 'up' : 'down');
    // Feed BTC price to status bar
    if (symbol === 'BTCUSDT') _updateSbBtc(price, changePct);
    // Sparkline
    _pushSpark(symbol, price);
    // Proximity alert
    _checkProximityAlert(symbol, price);
    // PHASE 4 Task 2: forward 24h stats to the symbol header, but only for the
    // symbol currently charted -- this fires for every row in the active
    // watchlist tab, not just the one on screen. Forex has no `stats` (no 24h
    // ticker source exists for it); symbol-header.js falls back to candles.
    if (symbol === currentSymbol && window.updateSymbolHeaderStats) {
      window.updateSymbolHeaderStats(Object.assign({ changePct }, stats));
    }
  }

  addPairBtn.addEventListener('click', () => {
    const input = prompt(
      `Add a pair to ${currentGroup.toUpperCase()}.\n\n` +
      (currentGroup === 'crypto' ? 'Binance symbol, e.g. DOGEUSDT' : currentGroup === 'forex' ? 'Pair, e.g. USD/CAD' : 'Stock ticker, e.g. AAPL')
    );
    if (!input) return;
    const symbol = input.trim().toUpperCase();
    if (groups[currentGroup].some(p => p.symbol === symbol)) { alert('Already in this list.'); return; }
    groups[currentGroup].push({
      symbol, label: symbol,
      icon: currentGroup === 'crypto' ? symbol.replace('USDT', '').toLowerCase() : undefined,
      type: currentGroup === 'crypto' ? 'crypto' : currentGroup === 'forex' ? 'forex' : 'stock',
    });
    saveGroups();
    buildWatchlistRows();
    refreshWatchlistPrices();
  });

  buildWatchlistRows();
  if (typeof _renderRadar === 'function') _renderRadar();
  // Resolve backend URL first (no-op on localhost, reads Firebase config on GitHub Pages),
  // then kick off all API-dependent polling.
  // Dedicated Dexter health ping — independent of all render logic
  async function _pingDexter() {
    try {
      const res = await _apiFetch('/api/trades');
      _setDexterStatus(res.ok);
    } catch(e) {
      _setDexterStatus(false);
    }
  }

  // PHASE 12: freshness stamps -- observes each panel's own last-successful-fetch time via
  // call-site wrapping only; no panel's fetch function body is touched.
  const _FRESH_INTERVALS_SECS = { chart: 5, watchlist: 3, tradeMonitor: 10, strategy: 25 };
  const _freshness = {};
  function _markFresh(name) { _freshness[name] = Date.now(); }
  function _renderFreshness() {
    const now = Date.now();
    for (const name in _FRESH_INTERVALS_SECS) {
      const elId = 'fresh' + name.charAt(0).toUpperCase() + name.slice(1);
      const el = document.getElementById(elId);
      if (!el) continue;
      const last = _freshness[name];
      el.classList.remove('warn', 'bad');
      if (last == null) { el.textContent = ''; continue; }
      const ageSecs = (now - last) / 1000;
      el.textContent = ageSecs < 60 ? `${Math.round(ageSecs)}s ago` : `${Math.round(ageSecs / 60)}m ago`;
      const staleAt = 3 * _FRESH_INTERVALS_SECS[name];
      if (ageSecs > staleAt * 2) el.classList.add('bad');
      else if (ageSecs > staleAt) el.classList.add('warn');
    }
  }
  setInterval(_renderFreshness, 2000);

  // PHASE 12: vitals strip -- polls read-only /api/vitals; single grey tile on any failure.
  async function loadVitals() {
    try {
      const res = await _apiFetch('/api/vitals');
      if (!res.ok) throw new Error('vitals http ' + res.status);
      _renderVitals(await res.json());
    } catch (e) {
      console.warn('[Vitals] load failed', e);
      _renderVitalsUnavailable();
    }
  }
  function _vitalsSetTile(id, valText, cls) {
    const tile = document.getElementById(id);
    const val = document.getElementById(id + 'Val');
    if (!tile || !val) return;
    tile.classList.remove('good', 'warn', 'bad');
    if (cls) tile.classList.add(cls);
    val.textContent = valText;
  }
  function _renderVitals(d) {
    const strip = document.getElementById('vitalsStrip');
    if (strip) strip.classList.remove('vitalsUnavailableMode');

    const age = d.last_scan_age_secs;
    let hbTxt = '—', hbCls = 'bad';
    if (age != null) {
      hbTxt = age < 60 ? `${Math.round(age)}s ago` : `${Math.round(age / 60)}m ago`;
      hbCls = age < 180 ? 'good' : age < 600 ? 'warn' : 'bad';
    }
    _vitalsSetTile('vtHeartbeat', hbTxt, hbCls);

    const hrs = d.hours_since_last_surviving_post;
    let drTxt = 'never', drCls = 'bad';
    if (hrs != null) {
      drTxt = hrs < 1 ? `${Math.round(hrs * 60)}m` : `${hrs.toFixed(1)}h`;
      drCls = hrs < 12 ? 'good' : hrs < 24 ? 'warn' : 'bad';
    }
    _vitalsSetTile('vtDrought', drTxt, drCls);

    const s = d.slots || {};
    const anyFull = Object.values(s).some(x => x.full);
    const slotsTxt = ['scalp', 'day', 'swing'].filter(k => s[k]).map(k => `${k[0].toUpperCase()}${s[k].open_pending}/${s[k].cap}`).join(' ');
    _vitalsSetTile('vtSlots', slotsTxt || '—', anyFull ? 'bad' : 'good');

    _vitalsSetTile('vtMode', d.exploration_mode ? 'EXPLORE' : 'NORMAL', d.exploration_mode ? 'warn' : 'good');

    const tr = d.today_realized_r;
    _vitalsSetTile('vtTodayR', tr != null ? `${tr >= 0 ? '+' : ''}${tr.toFixed(2)}R` : '—', tr == null ? '' : (tr >= 0 ? 'good' : 'bad'));

    const med = d.median_deliberation_secs_24h;
    _vitalsSetTile('vtChevLatency', med != null ? `${med.toFixed(0)}s` : '—', '');
  }
  function _renderVitalsUnavailable() {
    const strip = document.getElementById('vitalsStrip');
    if (strip) strip.classList.add('vitalsUnavailableMode');
  }

  // PHASE 16: shared tag-friendly-name helper. Fetches /api/tag_registry once per page
  // load and caches it (module-level, not per-panel) -- every render site (Tag Leaderboard,
  // Combo Leaderboard, both Shadow leaderboards) calls the same friendlyTag()/friendlyCombo()
  // rather than each keeping its own copy. Unknown codes (a brand-new tag the registry
  // hasn't been updated for yet) fall back to a plain prettifier -- never breaks.
  let _tagRegistry = null;
  let _tagRegistryPromise = null;

  function _prettifyTagFallback(code) {
    return String(code || '').trim().replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  function _loadTagRegistry() {
    if (_tagRegistry) return Promise.resolve(_tagRegistry);
    if (_tagRegistryPromise) return _tagRegistryPromise;
    _tagRegistryPromise = _apiFetch('/api/tag_registry')
      .then(r => r.json())
      .then(d => { _tagRegistry = d.tags || {}; return _tagRegistry; })
      .catch(e => { console.warn('[TagRegistry] load failed', e); _tagRegistry = {}; return _tagRegistry; });
    return _tagRegistryPromise;
  }

  function friendlyTag(code) {
    const c = String(code || '').trim().toLowerCase();
    if (_tagRegistry && _tagRegistry[c]) return _tagRegistry[c].name;
    return _prettifyTagFallback(c);
  }

  // Combo keys are comma-joined tag codes (compute_combo_win_rates / counterfactual's
  // _combo_key both use ",".join(sorted_tags)) -- split, map each through friendlyTag(),
  // join with " + " so e.g. "gp,ema_55,rsi_4h" reads "Golden Pocket + EMA 55 + RSI Divergence (4H)".
  function friendlyCombo(comboKey) {
    return String(comboKey || '').split(',').map(t => friendlyTag(t)).filter(Boolean).join(' + ');
  }

  // PHASE 22: friendly names for decision/reason CODES (distinct from PHASE 16's tag
  // registry -- this is "why did it die," not "which indicator fired"). Covers every code
  // actually found by discovery: chev_decisions.jsonl's decision types, the pre-escalation
  // NOT_ESCALATED shadow-log reasons, counterfactual_report.py's own gate-kill codes
  // (classify_reject_reason()/DECISION_DIRECT_MAP -- read for reference, never edited;
  // that file is do-not-touch machinery), and closed-trade close_type values. Raw code is
  // never invented -- unknown codes fall through to the same prettifier friendlyTag() uses.
  const REASON_REGISTRY = {
    // Decision types (chev_decisions.jsonl `decision` field)
    GATE_REJECT:      "Failed pre-checks (Dexter)",
    STRUCT_REJECT:     "No usable structure nearby",
    MTF_TAX_REJECT:    "Against the higher-timeframe trend",
    GEOMETRY_REJECT:   "Chev's numbers didn't hold up (Geometry)",
    REJECT:            "Approved by Chev, killed downstream",
    SKIP:              "Chev passed on it",
    FORMAT_ERROR:      "Chev's reply wasn't in the right format",
    POST:              "Chev posted a trade",
    // Pre-escalation NOT_ESCALATED reasons (labeller.py shadow log, before Chev ever sees it)
    tf_below_escalation_floor: "Timeframe too small (uneconomic)",
    below_trade_threshold:     "Confluence score too low",
    below_threshold:           "Below the cheap first-pass score filter",
    struct_pregate:            "No structural anchor nearby",
    too_far_from_level:        "Price hasn't reached the zone yet",
    conflicting_signals:       "Bullish and bearish signals contradicted",
    circuit_breaker:           "Daily loss limit hit — paused for the day",
    cooldown:                  "Same setup re-fired too soon (cooldown)",
    event_block:               "Paused around a high-impact news release",
    choppy_regime:             "Market too choppy for a directional edge",
    // Gate-kill codes (counterfactual_report.py's classify_reject_reason()/
    // DECISION_DIRECT_MAP -- NOTE: the phase brief named "ATR_SANITY" but the real code
    // produces DATA_QUALITY for that exact check; mapped under its real name)
    DATA_QUALITY:  "Numbers failed sanity check",
    COST_GATE:     "Fees too big for the stop (Cost Gate)",
    HEAT_CAP:      "Total risk cap reached",
    CONCURRENCY:   "Trade slots full",
    PRICE_DRIFT:   "Price moved while Chev decided",
    NET_RR:        "Reward too small after costs (Net R:R)",
    CORR_CAP:      "Too many correlated positions open",
    LIQUIDATION:   "Stop too tight for the leverage used",
    HALLUCINATED_LEVELS: "Chev's numbers weren't from this chart",
    OTHER:         "Rejected for another reason",
    SCORE_GATE:    "Failed pre-checks (Dexter)",
    STRUCT_GATE:   "No usable structure nearby",
    MTF_TAX:       "Against the higher-timeframe trend",
    GEOMETRY:      "Chev's numbers didn't hold up (Geometry)",
    // Closed-trade close_type (main terminal trade log, outside the Strategy panel)
    TIME_EXIT: "Closed by time-stop",
    SL_HIT:    "Stop loss hit",
    TP_HIT:    "Target hit",
    SIP_HIT:   "Stopped in profit (SIP)",
  };

  function friendlyReason(code) {
    const c = String(code || '').trim();
    if (REASON_REGISTRY[c]) return REASON_REGISTRY[c];
    return _prettifyTagFallback(c);
  }

  // PHASE 27: display-name-only rename, applied at render time so no backend text
  // (including the TAG_REGISTRY definitions dexter.py serves) ever needs editing or
  // a restart. "Examiner" is the only one with any live occurrences today (Gate
  // Scoreboard label + a handful of tag/concept definitions) -- Marc/The Guards
  // have no existing display strings anywhere on the site, so they're intentionally
  // left out of this map rather than invented. Internal names (labeller.py, etc.)
  // are never touched -- this only rewrites prose already destined for the screen.
  const CHARACTER_DISPLAY = { Examiner: "Mike Ross" };
  function _applyCharacterNames(text) {
    if (!text) return text;
    return String(text)
      .replace(/\bthe Examiner's\b/g, `${CHARACTER_DISPLAY.Examiner}'s`)
      .replace(/\bthe Examiner\b/g, CHARACTER_DISPLAY.Examiner)
      .replace(/\bExaminer's\b/g, `${CHARACTER_DISPLAY.Examiner}'s`)
      .replace(/\bExaminer\b/g, CHARACTER_DISPLAY.Examiner);
  }

  // Boot only after EVERY module script has executed. _resolveApiBase() resolves
  // synchronously on localhost, so its .then() would otherwise fire as a microtask
  // right after this file — before trading-logs/chev-corner/status-bar/drawing.js
  // have defined refreshTradingLogs/_latestFbData/_setDexterStatus/syncCanvasSize,
  // aborting the boot block before _pingDexter() is ever scheduled (dot stuck "offline").
  function _bootMonitor() {
    _resolveApiBase().then(() => {
      refreshWatchlistPrices().then(() => _markFresh('watchlist'));  refreshForexPrices();
      setInterval(() => refreshWatchlistPrices().then(() => _markFresh('watchlist')), 3000);
      setInterval(refreshForexPrices, 1000);
      refreshTradingLogs();
      setInterval(refreshTradingLogs, 5000);
      refreshChevsCorner();
      setInterval(refreshChevsCorner, 30000);
      refreshJaneTrades().then(() => _markFresh('tradeMonitor'));
      setInterval(() => refreshJaneTrades().then(() => _markFresh('tradeMonitor')), 10000);
      _pingDexter();
      setInterval(_pingDexter, 8000);
      loadVitals();
      setInterval(loadVitals, 20000);
      _loadTagRegistry();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _bootMonitor);
  else _bootMonitor();

