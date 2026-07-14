/* ============================================================
   Engine tab Dexter overlays, engine cache, hypothetical cache, layers tab.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     ENGINE TAB — Dexter visual overlays
     ============================================================ */
  let _engineData = null;
  // Expose so the isolated tab-switch script can read it for cache restore check
  Object.defineProperty(window, '_engineData', { get: () => _engineData, set: v => { _engineData = v; } });
  let _engineOverlayState = {
    swings: true, legs: true, geometry: false, balance: true, hypothesis: false, patterns: false,
    rays: false
  };
  // Trendline Ray registry data (Phase W1) -- /api/rays response for whichever
  // symbol+tf the Arsenal "TL" card last fetched. Mirrors _engineData's pattern
  // but is its own state: a separate endpoint, a separate cache-or-fetch check.
  let _tlRaysData = null;
  // The ENGINE-tab overlays (swings/legs/balance) default to ON, but they should
  // only appear once the user actually engages the ENGINE tab ("Run Dexter" →
  // _applyEngineData). The Arsenal "PAT" tool loads _engineData purely to draw
  // pattern lines and must NOT drag those default-on overlays onto the chart.
  // This flag gates them; patterns draw independently of it.
  let _engineReadoutActive = false;

  function _drawEngineOverlays() {
    if (!_engineData) return;
    const d = _engineData;
    dctx.save();

    // 1. Balance zone
    if (_engineReadoutActive && _engineOverlayState.balance && d.auction) {
      const y1 = priceToY(d.auction.balance_high);
      const y2 = priceToY(d.auction.balance_low);
      if (y1 != null && y2 != null) {
        dctx.fillStyle = 'rgba(212,175,55,0.06)';
        dctx.fillRect(0, Math.min(y1, y2), _dw, Math.abs(y2 - y1));
        dctx.strokeStyle = 'rgba(212,175,55,0.25)';
        dctx.lineWidth = 0.5;
        dctx.setLineDash([5, 4]);
        dctx.beginPath(); dctx.moveTo(0, y1); dctx.lineTo(_dw, y1); dctx.stroke();
        dctx.beginPath(); dctx.moveTo(0, y2); dctx.lineTo(_dw, y2); dctx.stroke();
        dctx.setLineDash([]);
        // Equilibrium / POC
        const yPoc = priceToY(d.auction.poc || d.auction.anchor_price);
        if (yPoc != null) {
          dctx.strokeStyle = 'rgba(212,175,55,0.5)';
          dctx.lineWidth = 1;
          dctx.setLineDash([6, 3]);
          dctx.beginPath(); dctx.moveTo(0, yPoc); dctx.lineTo(_dw, yPoc); dctx.stroke();
          dctx.setLineDash([]);
          dctx.font = '9px Share Tech Mono, monospace';
          dctx.fillStyle = 'rgba(212,175,55,0.7)';
          dctx.fillText('POC', 6, yPoc - 3);
        }
      }
    }

    // 2. Legs
    if (_engineReadoutActive && _engineOverlayState.legs && d.legs) {
      const recent = d.legs.slice(-10);
      recent.forEach(leg => {
        const x1 = timeToX(leg.start_ts), y1 = priceToY(leg.start_price);
        const x2 = timeToX(leg.end_ts),   y2 = priceToY(leg.end_price);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        const isImp = leg.character === 'IMPULSIVE';
        const col = isImp ? (leg.direction === 'UP' ? '#089981' : '#f23645') : '#787b86';
        dctx.save();
        dctx.strokeStyle = col;
        dctx.globalAlpha = isImp ? 0.75 : 0.3;
        dctx.lineWidth = isImp ? 1.8 : 1;
        if (!isImp) dctx.setLineDash([4, 3]);
        dctx.beginPath(); dctx.moveTo(x1, y1); dctx.lineTo(x2, y2); dctx.stroke();
        dctx.setLineDash([]);
        if (isImp) {
          const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 6;
          dctx.globalAlpha = 0.85;
          dctx.font = 'bold 8px Share Tech Mono, monospace';
          dctx.fillStyle = col;
          dctx.fillText(leg.distance_atr.toFixed(1) + 'A · ' + leg.dist_atr_pct + 'p', mx + 4, my);
        }
        dctx.restore();
      });
    }

    // 3. Swing dots
    if (_engineReadoutActive && _engineOverlayState.swings && d.swings) {
      d.swings.slice(-20).forEach(sw => {
        const x = timeToX(sw.ts), y = priceToY(sw.price);
        if (x == null || y == null) return;
        const isH = sw.kind === 'HIGH';
        dctx.save();
        dctx.fillStyle = isH ? '#f23645' : '#089981';
        dctx.globalAlpha = sw.confirmed ? 0.9 : 0.4;
        dctx.beginPath();
        dctx.arc(x, y, 4, 0, Math.PI * 2);
        dctx.fill();
        dctx.font = 'bold 8px Share Tech Mono, monospace';
        dctx.fillStyle = isH ? '#f23645' : '#089981';
        dctx.fillText(isH ? 'H' : 'L', x - 3, isH ? y - 8 : y + 14);
        dctx.restore();
      });
    }

    // 4. Pattern trendlines (upper + lower boundary)
    if (_engineOverlayState.patterns && d.patterns) {
      const pat = d.patterns;

      function _drawPatternLine(ep, isUpper) {
        if (!ep || ep.t1 == null || ep.p1 == null) return;
        const x1 = timeToX(ep.t1), y1 = priceToY(ep.p1);
        const x2 = timeToX(ep.t2), y2 = priceToY(ep.p2);
        if (x1 == null || y1 == null || x2 == null || y2 == null) return;
        const slope = ep.slope_class || '';
        const baseCol = isUpper
          ? (slope === 'falling' ? '#f23645' : slope === 'rising' ? '#f23645' : '#d4af37')
          : (slope === 'rising'  ? '#089981' : slope === 'falling' ? '#089981' : '#d4af37');
        const r2 = ep.r2 || 0;
        dctx.save();
        dctx.globalAlpha = 0.35 + r2 * 0.45;  // more opaque = higher R²
        dctx.strokeStyle = baseCol;
        dctx.lineWidth = 1.5;
        dctx.setLineDash([6, 4]);
        dctx.beginPath();
        dctx.moveTo(x1, y1);
        dctx.lineTo(x2, y2);
        dctx.stroke();
        dctx.setLineDash([]);
        dctx.restore();
      }

      _drawPatternLine(pat.upper_trendline, true);
      _drawPatternLine(pat.lower_trendline, false);
    }

    // 5. Trendline Ray registry (Phase W1) — solid anchor->now, dashed now->horizon.
    // Data is /api/rays' response (_tlRaysData): Dexter has already computed every
    // coordinate, the slope class, and the label string -- this only draws
    // coordinates and prints strings, per this build's core rule. BROKEN rays are
    // never present here (select_live already excludes non-LIVE rays server-side).
    if (_engineOverlayState.rays && _tlRaysData && _tlRaysData.rays) {
      _tlRaysData.rays.forEach(ray => {
        const xA = timeToX(ray.t_anchor),  yA = priceToY(ray.p_anchor);
        const xN = timeToX(ray.t_now),     yN = priceToY(ray.p_now);
        const xH = timeToX(ray.t_horizon), yH = priceToY(ray.p_horizon);
        if (xA == null || yA == null || xN == null || yN == null || xH == null || yH == null) return;
        const col = ray.side === 'upper' ? '#f23645' : '#089981';  // site convention: resistance=red, support=green
        dctx.save();
        dctx.strokeStyle = col;
        dctx.lineWidth = 1.5;
        dctx.beginPath(); dctx.moveTo(xA, yA); dctx.lineTo(xN, yN); dctx.stroke();
        dctx.setLineDash([6, 4]);
        dctx.beginPath(); dctx.moveTo(xN, yN); dctx.lineTo(xH, yH); dctx.stroke();
        dctx.setLineDash([]);
        if (ray.label) {
          dctx.font = '9px Share Tech Mono, monospace';
          dctx.fillStyle = col;
          dctx.fillText(ray.label, xH + 4, yH);
        }
        dctx.restore();
      });
    }

    dctx.restore();
  }

  // Hook engine overlays into the main redrawAll (append at end)
  // Live position P&L badge — drawn at current price when trade overlay is on
  function _drawLivePosition() {
    if (!_overlayEntry || !_activeTrade || !_activeTrade.entry) return;
    if (!currentCandles || !currentCandles.length) return;
    const cp    = currentCandles[currentCandles.length - 1].close;
    const entry = _activeTrade.entry;
    const isLong = (_activeTrade.direction || '').toLowerCase().includes('long');
    const lev   = parseFloat(_activeTrade.leverage) || 1;
    const isPending = _activeTrade.status === 'PENDING';
    const y = priceToY(cp);
    if (y == null) return;
    dctx.save();
    let txt, color, bgColor, strokeColor;
    if (isPending) {
      // Trade idea not yet open — show how far price is from entry
      const distPct = (cp - entry) / entry * 100;
      const above = distPct >= 0;
      // Arrow points toward entry: price above entry → needs to drop ↓, below → needs to rise ↑
      const arrow = above ? '↓' : '↑';
      const approaching = isLong ? !above : above;
      txt = arrow + ' WATCHING  ' + Math.abs(distPct).toFixed(2) + '%';
      color      = approaching ? '#d4af37' : '#787b86';
      bgColor    = approaching ? 'rgba(212,175,55,0.08)' : 'rgba(30,34,45,0.85)';
      strokeColor = approaching ? 'rgba(212,175,55,0.35)' : 'rgba(60,65,80,0.5)';
      // Dashed line in gold for pending
      dctx.strokeStyle = 'rgba(212,175,55,0.25)';
      dctx.lineWidth = 1; dctx.setLineDash([2, 5]);
      dctx.beginPath(); dctx.moveTo(0, y); dctx.lineTo(_dw, y); dctx.stroke();
      dctx.setLineDash([]);
    } else {
      // Live position — show P&L
      const pctRaw = isLong ? (cp - entry) / entry * 100 : (entry - cp) / entry * 100;
      const pctLev = pctRaw * lev;
      const isProfit = pctLev >= 0;
      const prefix = isProfit ? '▲ +' : '▼ ';
      const levSuffix = lev > 1 ? ' · ' + lev + 'x' : '';
      txt = prefix + Math.abs(pctLev).toFixed(2) + '%' + levSuffix;
      color       = isProfit ? '#089981' : '#f23645';
      bgColor     = isProfit ? 'rgba(8,153,129,0.13)'  : 'rgba(242,54,69,0.13)';
      strokeColor = isProfit ? 'rgba(8,153,129,0.55)'  : 'rgba(242,54,69,0.55)';
      dctx.strokeStyle = isProfit ? 'rgba(8,153,129,0.3)' : 'rgba(242,54,69,0.3)';
      dctx.lineWidth = 1; dctx.setLineDash([3, 4]);
      dctx.beginPath(); dctx.moveTo(0, y); dctx.lineTo(_dw, y); dctx.stroke();
      dctx.setLineDash([]);
    }
    dctx.font = 'bold 6px "Share Tech Mono", monospace';
    const tw = dctx.measureText(txt).width;
    const ph = 9, pw = tw + 6;
    const px = 4;
    const pyTop = y - ph / 2;
    dctx.fillStyle   = bgColor;
    dctx.strokeStyle = strokeColor;
    dctx.lineWidth = 1;
    dctx.beginPath();
    if (dctx.roundRect) dctx.roundRect(px, pyTop, pw, ph, 2);
    else dctx.rect(px, pyTop, pw, ph);
    dctx.fill(); dctx.stroke();
    dctx.fillStyle  = color;
    dctx.textAlign  = 'left'; dctx.textBaseline = 'middle';
    dctx.fillText(txt, px + 3, y);
    dctx.restore();
  }

  const _origRedrawAll = redrawAll;
  redrawAll = function() {
    _origRedrawAll();
    _drawEngineOverlays();
    _drawLivePosition();
    // RSI → main chart crosshair sync: faint vertical dashed line when hovering RSI panel
    if (_rsiCrossX != null && _rsiCrossX >= 0 && _rsiCrossX <= _dw) {
      dctx.save();
      dctx.globalAlpha = 0.35;
      dctx.strokeStyle = '#787b86';
      dctx.lineWidth = 1;
      dctx.setLineDash([3, 4]);
      dctx.beginPath(); dctx.moveTo(_rsiCrossX, 0); dctx.lineTo(_rsiCrossX, _dh); dctx.stroke();
      dctx.setLineDash([]);
      dctx.restore();
    }
  };

  /* ============================================================
     ENGINE CACHE — persist Dexter results per symbol+tf in localStorage
     ============================================================ */
  const _ENG_CACHE_KEY = '_chev_eng_v1';

  function _saveEngineCache(sym, tf, data) {
    try {
      const store = JSON.parse(localStorage.getItem(_ENG_CACHE_KEY) || '{}');
      store[sym + '|' + tf] = { data, ts: Date.now() };
      // Keep only the 12 most recent symbols to avoid localStorage bloat
      const keys = Object.keys(store).sort((a,b) => (store[b].ts||0)-(store[a].ts||0));
      if (keys.length > 12) keys.slice(12).forEach(k => delete store[k]);
      localStorage.setItem(_ENG_CACHE_KEY, JSON.stringify(store));
    } catch(e) {}
  }

  function _loadEngineCache(sym, tf) {
    try {
      const store = JSON.parse(localStorage.getItem(_ENG_CACHE_KEY) || '{}');
      return store[sym + '|' + tf] || null;
    } catch(e) { return null; }
  }

  /* ============================================================
     HYPOTHETICAL CACHE — Chev's "where would you enter" answers,
     persisted per symbol+tf so they survive navigating away. Never
     written server-side (no journal/sheet/telegram) — cache-only.
     ============================================================ */
  const _HYPO_CACHE_KEY = '_chev_hypo_v1';

  function _saveHypoCache(sym, tf, data) {
    try {
      const store = JSON.parse(localStorage.getItem(_HYPO_CACHE_KEY) || '{}');
      store[sym + '|' + tf] = { data, ts: Date.now() };
      const keys = Object.keys(store).sort((a,b) => (store[b].ts||0)-(store[a].ts||0));
      if (keys.length > 12) keys.slice(12).forEach(k => delete store[k]);
      localStorage.setItem(_HYPO_CACHE_KEY, JSON.stringify(store));
    } catch(e) {}
  }

  function _loadHypoCache(sym, tf) {
    try {
      const store = JSON.parse(localStorage.getItem(_HYPO_CACHE_KEY) || '{}');
      return store[sym + '|' + tf] || null;
    } catch(e) { return null; }
  }

  /* ─── Conviction SVG gauge helper ─── */
  function _convictionGaugeSVG(pct, color) {
    const r = 20, cx = 26, cy = 26, stroke = 5;
    const circ = 2 * Math.PI * r;
    const dashArr = circ * 0.75;  // 270° arc
    const dashOff = dashArr * (1 - pct / 100);
    return `<svg viewBox="0 0 52 52" style="transform:rotate(135deg)">
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="${stroke}" stroke-dasharray="${dashArr.toFixed(1)} ${circ.toFixed(1)}" stroke-linecap="round"/>
      <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}" stroke-dasharray="${(dashArr*pct/100).toFixed(1)} ${circ.toFixed(1)}" stroke-linecap="round" style="transition:stroke-dasharray 0.6s ease"/>
    </svg>`;
  }

  /* ─── Structure timeline from legs ─── */
  function _buildStructureTimeline(legs) {
    if (!legs || !legs.length) return '';
    const recent = legs.slice(-7);
    let chips = recent.map((leg, i) => {
      const isImp = leg.character === 'IMPULSIVE';
      const isUp  = leg.direction === 'UP';
      const cls   = isImp ? (isUp ? 'impl-up' : 'impl-dn') : (isUp ? 'corr-up' : 'corr-dn');
      const arrow = isImp ? (isUp ? '▲' : '▼') : (isUp ? '△' : '▽');
      const sep   = i < recent.length - 1 ? '<span class="legConnector"></span>' : '';
      return `<span class="legChip ${cls}" title="${leg.character} ${leg.direction} · ${leg.distance_atr?.toFixed(1) || '?'}A">
        <span class="legArrow">${arrow}</span>
        <span class="legAtr">${leg.distance_atr?.toFixed(1) || '?'}A</span>
      </span>${sep}`;
    }).join('');
    chips += '<span class="legNow">NOW</span>';
    return `<div class="structureTimeline">${chips}</div>`;
  }

  /* ─── Hypothesis card ─── */
  function _buildHypCard(h, isPrimary) {
    const bias = (h.bias || 'NEUTRAL').toUpperCase();
    const dirCls = bias === 'LONG' ? 'long' : bias === 'SHORT' ? 'short' : 'neutral';
    const dirLabel = bias === 'LONG' ? '▲ LONG' : bias === 'SHORT' ? '▼ SHORT' : '◆ NEUTRAL';
    const confPct  = Math.round((h.confidence || 0) * 100);
    const confCls  = confPct >= 65 ? 'high' : confPct >= 40 ? 'mid' : 'low';
    const fillColor = bias === 'LONG' ? 'var(--green)' : bias === 'SHORT' ? 'var(--red)' : 'var(--gold)';
    const name = (h.name || h.label || '').replace(/_/g, ' ');
    const label = h.label || name;
    const trigger = h.expected_next_event || h.trigger || '';
    const kill    = h.invalidation || h.kill_switch || '';
    return `<div class="hypCard${isPrimary ? ' primary' : ''}">
      <div class="hypCardHeader">
        <span class="hypDir ${dirCls}">${dirLabel}</span>
        <span class="hypName">${name}</span>
        <span class="hypPct ${confCls}">${confPct}%</span>
      </div>
      <div class="hypConfTrack"><div class="hypConfFill" style="width:${confPct}%;background:${fillColor}"></div></div>
      ${label !== name ? `<div class="hypDesc">${label}</div>` : ''}
      ${(trigger || kill) ? `<div class="hypFooter">
        ${trigger ? `<div class="hypFooterRow"><img src="emoji/hope.png" class="hypFooterIcon" alt=""><span class="hypFooterLabel hypTrigger">Watch for:</span><span class="hypFooterText hypTrigger">${trigger}</span></div>` : ''}
        ${kill    ? `<div class="hypFooterRow"><img src="emoji/oh-no.png" class="hypFooterIcon" alt=""><span class="hypFooterLabel hypKill">Kills it:</span><span class="hypFooterText hypKill">${kill}</span></div>` : ''}
      </div>` : ''}
    </div>`;
  }

  /* ─── Scenario planner block ─── */
  function _buildScenario(state, topHyp) {
    if (!state) return '';
    const isBull = state.direction > 15, isBear = state.direction < -15;
    const bullTarget = topHyp && topHyp.bias === 'LONG'  ? topHyp.expected_next_event : null;
    const bearTarget = topHyp && topHyp.bias === 'SHORT' ? topHyp.expected_next_event : null;
    return `<div class="scenarioRow">
      <div class="scenarioBlock scenarioBull">
        <div class="scenarioLabel">▲ Bull Case</div>
        <div class="scenarioIf">If buyers hold structure:</div>
        <div class="scenarioThen">${bullTarget || (isBull ? 'Continuation higher, trend intact' : 'Potential reversal long')}</div>
      </div>
      <div class="scenarioBlock scenarioBear">
        <div class="scenarioLabel">▼ Bear Case</div>
        <div class="scenarioIf">If sellers regain control:</div>
        <div class="scenarioThen">${bearTarget || (isBear ? 'Continuation lower, momentum down' : 'Range breakdown expected')}</div>
      </div>
    </div>`;
  }

  /* ─── Update Brain State Banner in TRADES pane ─── */
  function _updateBrainStateBanner(d, runTs) {
    const banner  = document.getElementById('brainStateBanner');
    const regimeEl = document.getElementById('brainStateRegime');
    const hypEl   = document.getElementById('brainStateHyp');
    const confEl  = document.getElementById('brainStateConf');
    const barFill = document.getElementById('brainStateBarFill');
    const ageEl   = document.getElementById('brainStateAge');
    if (!banner) return;

    const state  = d.state || d.market_state;
    const hyps   = d.hypotheses || [];
    const topHyp = hyps[0];

    let regime = state?.regime || 'UNKNOWN';
    const isBull = regime.includes('BULL');
    const isBear = regime.includes('BEAR');
    const regCls = isBull ? 'bull' : isBear ? 'bear' : 'range';
    const regLabel = (isBull ? '▲ ' : isBear ? '▼ ' : '◆ ') + regime.replace(/_/g, ' ');

    regimeEl.textContent = regLabel;
    regimeEl.className   = regCls;

    const confPct = topHyp ? Math.round(topHyp.confidence * 100) : 0;
    const confCls = confPct >= 65 ? 'high' : confPct >= 40 ? 'mid' : 'low';

    if (topHyp) {
      const hypName = (topHyp.name || topHyp.label || '').replace(/_/g, ' ');
      hypEl.innerHTML = `<b>${hypName}</b> <em>· ${currentSymbol} ${currentTf?.toUpperCase() || ''}</em>`;
      confEl.textContent = confPct + '%';
      confEl.className   = confCls;
      barFill.style.width      = confPct + '%';
      barFill.style.background = isBull ? 'var(--green)' : isBear ? 'var(--red)' : 'var(--gold)';
    } else {
      hypEl.textContent  = regime.replace(/_/g, ' ').toLowerCase() + ' — no strong hypothesis';
      confEl.textContent = '';
      barFill.style.width = (state?.participation || 0) + '%';
    }

    if (runTs) {
      const ago = Math.round((Date.now() - runTs) / 60000);
      ageEl.textContent = '⟳ Dexter ran ' + (ago < 1 ? 'just now' : ago + 'm ago') + ' · ' + currentSymbol;
    }

    banner.classList.add('visible');
    _saveHypothesisHistory(d, runTs || Date.now());
  }

  /* ─── Hypothesis history (localStorage) ─── */
  const _HYP_HISTORY_KEY = '_chev_hyp_v1';
  function _saveHypothesisHistory(d, ts) {
    const hyps = d.hypotheses || [];
    const topHyp = hyps[0];
    if (!topHyp) return;
    const record = {
      sym: currentSymbol, tf: currentTf, ts,
      name: (topHyp.name || '').replace(/_/g, ' '),
      bias: topHyp.bias || 'NEUTRAL',
      conf: Math.round((topHyp.confidence || 0) * 100),
      regime: d.state?.regime || '',
      outcome: 'open'
    };
    try {
      const hist = JSON.parse(localStorage.getItem(_HYP_HISTORY_KEY) || '[]');
      // avoid duplicates within 10 min
      const recent = hist[hist.length - 1];
      if (recent && recent.sym === record.sym && Math.abs(recent.ts - ts) < 600000) return;
      hist.push(record);
      if (hist.length > 50) hist.splice(0, hist.length - 50);
      localStorage.setItem(_HYP_HISTORY_KEY, JSON.stringify(hist));
    } catch(e) {}
  }

  /* ─── Hypothesis History renderer ─── */
  function _renderHypHistory() {
    const section = document.getElementById('hypHistorySection');
    const container = document.getElementById('hypTracker');
    if (!section || !container) return;
    try {
      const hist = JSON.parse(localStorage.getItem(_HYP_HISTORY_KEY) || '[]');
      if (!hist.length) { section.style.display = 'none'; return; }
      section.style.display = '';
      const recent = [...hist].reverse().slice(0, 12);
      container.innerHTML = recent.map(r => {
        const bias = r.bias || 'NEUTRAL';
        const arrow = bias === 'LONG' ? '▲' : bias === 'SHORT' ? '▼' : '◆';
        const dotCls = r.outcome === 'win' ? 'win' : r.outcome === 'loss' ? 'loss' : 'open';
        const ago = Math.round((Date.now() - r.ts) / 60000);
        const agoStr = ago < 60 ? ago + 'm ago' : Math.round(ago/60) + 'h ago';
        const conf = r.conf ? r.conf + '%' : '';
        const nameStr = (r.name || '').length > 28 ? r.name.slice(0,28) + '…' : r.name;
        return `<div class="hypTrackItem">
          <span class="hypTrackDot ${dotCls}"></span>
          <span class="hypTrackText"><b style="color:${bias==='LONG'?'var(--green)':bias==='SHORT'?'var(--red)':'var(--txt2)'}">${arrow}</b> ${nameStr || bias}</span>
          <span class="hypTrackMeta">${r.sym} ${r.tf?.toUpperCase()||''} · ${conf} · ${agoStr}</span>
        </div>`;
      }).join('');
    } catch(e) { section.style.display = 'none'; }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const clearBtn = document.getElementById('clearHypHistory');
    if (clearBtn) clearBtn.addEventListener('click', () => {
      localStorage.removeItem(_HYP_HISTORY_KEY);
      _renderHypHistory();
      showNotification('History cleared', 'Hypothesis log reset', 'info', 'wrench.png', 3000);
    });
  });

  /* ─── Main ENGINE data renderer ─── */
  function _applyEngineData(d, fromCache, cacheTs) {
    _engineData = d;
    _engineReadoutActive = true;  // ENGINE tab engaged → its overlays may draw
    const readout   = document.getElementById('engineReadout');
    const toggles   = document.getElementById('engineOverlayToggles');
    const cacheNote = document.getElementById('engineCacheNote');
    const state  = d.state  || d.market_state;
    const prof   = d.asset_profile;
    const hyps   = d.hypotheses || [];
    const topHyp = hyps[0];
    let html = '';

    // ─── 1. CHEV'S READ (narrative) ───
    if (state) {
      const isBull = state.direction > 25, isBear = state.direction < -25;
      const moodImg = isBull ? 'fire.png' : isBear ? 'furious.png' : 'lets-see.png';
      const _mood   = isBull ? `I'm reading <b style="color:var(--green)">bullish</b>` :
                      isBear ? `I'm reading <b style="color:var(--red)">bearish</b>` :
                               `No clean trend — market is <b>ranging</b>`;
      const p = state.participation;
      const _partic = p >= 60 ? `participation <b>${p.toFixed(0)}/100</b> — institutions active`
                    : p <= 35  ? `participation <b>${p.toFixed(0)}/100</b> — thin, low conviction`
                               : `participation <b>${p.toFixed(0)}/100</b> — moderate`;
      const _phase  = state.phase ? ` <b>${state.phase}</b> phase.` : '';
      const hypColor = topHyp ? (topHyp.bias==='LONG' ? 'var(--green)' : topHyp.bias==='SHORT' ? 'var(--red)' : 'var(--gold)') : 'var(--gold)';
      const _bet = topHyp ? ` My thesis: <b style="color:${hypColor}">${(topHyp.name||topHyp.label||'').replace(/_/g,' ')}</b> at <b>${Math.round(topHyp.confidence*100)}%</b> confidence.` : '';
      html += `<div class="dexterNarrative">
        <img src="emoji/${moodImg}" alt="" style="width:16px;height:16px;vertical-align:middle;margin-right:6px">
        <b style="color:var(--gold);letter-spacing:0.05em">CHEV'S READ</b><br>
        ${_mood}, ${_partic}.${_phase}${_bet}
      </div>`;
    }

    // ─── 2. CONVICTION METER ───
    if (topHyp && state) {
      const conf   = Math.round(topHyp.confidence * 100);
      const isBull = (topHyp.bias === 'LONG');
      const isBear = (topHyp.bias === 'SHORT');
      const mColor = isBull ? 'var(--green)' : isBear ? 'var(--red)' : 'var(--gold)';
      const moodConf = conf >= 70 ? 'not-bad-2.png' : conf >= 45 ? 'not-bad-1.png' : 'lets-see.png';
      const phase = state.phase || 'unknown';
      html += `<div class="convictionMeter">
        <div class="convictionGauge">
          ${_convictionGaugeSVG(conf, mColor)}
          <div class="convictionVal">
            <span class="cvNum" style="color:${mColor}">${conf}</span>
            <span class="cvLbl">CONF</span>
          </div>
        </div>
        <div class="convictionBody">
          <div class="convictionTitle">
            <img src="emoji/${moodConf}" alt="" style="width:14px;height:14px;vertical-align:middle;margin-right:4px">
            Conviction: ${conf >= 70 ? 'High' : conf >= 45 ? 'Moderate' : 'Low'}
          </div>
          <div class="convictionSub">
            Participation ${state.participation?.toFixed(0) || '—'}/100 · ${phase}<br>
            ${state.participation_pct != null ? state.participation_pct + 'th percentile' : ''}
          </div>
        </div>
      </div>`;
    }

    // ─── 3. STRUCTURE TIMELINE ───
    if (d.legs && d.legs.length) {
      html += `<div class="engineBlock">
        <div class="engineBlockTitle">Structure Timeline <small style="color:var(--txt3);font-weight:400">${d.legs.length} legs total</small></div>
        ${_buildStructureTimeline(d.legs)}
      </div>`;
    }

    // ─── 4. HYPOTHESES BOARD ───
    if (hyps.length) {
      html += `<div class="engineBlock">
        <div class="engineBlockTitle">
          <img src="emoji/tools.png" alt="" style="width:12px;height:12px;vertical-align:middle;margin-right:4px;opacity:0.8">
          Chev's Hypotheses
          <small style="color:var(--txt3);font-weight:400;margin-left:4px">${hyps.length} active</small>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-top:4px">
      `;
      hyps.forEach((h, i) => { html += _buildHypCard(h, i === 0); });
      html += '</div></div>';
    }

    // ─── 5. SCENARIO PLANNER ───
    if (state && topHyp) {
      html += `<div class="engineBlock">
        <div class="engineBlockTitle">
          <img src="emoji/ruler.png" alt="" style="width:12px;height:12px;vertical-align:middle;margin-right:4px;opacity:0.8">
          Scenario Planner
        </div>
        <div style="margin-top:6px">${_buildScenario(state, topHyp)}</div>
      </div>`;
    }

    // ─── 6. MARKET STATE DETAIL ───
    if (state) {
      const dirCol = state.direction > 20 ? 'var(--green)' : state.direction < -20 ? 'var(--red)' : 'var(--gold)';
      html += `<div class="engineBlock"><div class="engineBlockTitle">Market Internals</div>
        <div class="engineRow"><span class="engineLabel">Direction score</span><span class="engineVal" style="color:${dirCol}">${(state.direction > 0 ? '+' : '')}${state.direction?.toFixed(0) || '—'}</span></div>
        <div class="engineRow"><span class="engineLabel">Participation</span><span class="engineVal">${state.participation?.toFixed(0) || '—'}/100 <small style="color:var(--txt3)">${state.participation_pct != null ? state.participation_pct + 'p' : ''}</small></span></div>
        <div class="engineBar"><div class="engineBarFill" style="width:${state.participation || 0}%;background:${dirCol}"></div></div>
        ${state.phase ? `<div class="engineRow"><span class="engineLabel">Phase</span><span class="engineVal">${state.phase}</span></div>` : ''}
        ${state.leg_sequence ? `<div class="engineRow"><span class="engineLabel">Sequence</span><span class="engineVal" style="font-size:9px">${state.leg_sequence}</span></div>` : ''}
      </div>`;
    }

    // ─── 7. PATTERN RECOGNITION ───
    if (d.patterns && d.patterns.pattern && d.patterns.pattern !== 'None') {
      const pat  = d.patterns;
      const conf = Math.round((pat.value || 0) * 100);
      const bias = pat.bias || 'neutral';
      const bCol = bias === 'bullish' ? 'var(--green)' : bias === 'bearish' ? 'var(--red)' : 'var(--gold)';
      const bArrow = bias === 'bullish' ? '▲' : bias === 'bearish' ? '▼' : '◆';
      const brkLabel = pat.breakout ? '<span style="color:var(--gold);font-size:9px;margin-left:6px">BREAKOUT</span>' : '';
      const volOk    = pat.volume_confirmed;
      const volIcon  = volOk ? '<span style="color:var(--green)">&#x2713; vol</span>' : '<span style="color:var(--red);opacity:0.7">no vol</span>';
      const allPats  = pat.all_patterns || [];
      const extraPats = allPats.filter(p => p.pattern !== pat.pattern)
        .map(p => `<span style="font-size:9px;color:var(--txt3);margin-right:8px">${p.pattern} <span style="color:var(--txt3)">${Math.round((p.confidence||0)*100)}%</span></span>`)
        .join('');
      const volNotes = (pat.volume_notes || []).map(n =>
        `<div style="font-size:9px;color:var(--txt3);margin-top:2px;padding-left:4px;border-left:2px solid rgba(255,255,255,0.08)">${n}</div>`).join('');

      html += `<div class="engineBlock" style="border-left:2px solid ${bCol}20">
        <div class="engineBlockTitle" style="display:flex;align-items:center;gap:6px">
          Pattern Recognition
          <span style="margin-left:auto;font-size:9px;color:var(--txt3);font-weight:400">toggle: Patterns checkbox above</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:6px;padding:8px;background:rgba(255,255,255,0.02);border-radius:6px">
          <div style="text-align:center;min-width:44px">
            <div style="font-size:18px;font-weight:700;color:${bCol};line-height:1">${conf}</div>
            <div style="font-size:8px;color:var(--txt3);margin-top:1px">CONF%</div>
          </div>
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;font-weight:700;color:var(--txt1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
              <span style="color:${bCol}">${bArrow}</span> ${pat.pattern}${brkLabel}
            </div>
            <div style="font-size:9px;color:var(--txt3);margin-top:3px;display:flex;align-items:center;gap:8px">
              <span style="color:${bCol}">${bias}</span>
              <span style="color:var(--txt3)">·</span>
              ${volIcon}
              <span style="color:var(--txt3)">·</span>
              <span>${pat.category || ''}</span>
            </div>
          </div>
          <div style="font-size:11px;font-weight:700;color:${pat.signal==='BUY'?'var(--green)':pat.signal==='SELL'?'var(--red)':'var(--txt3)'}">
            ${pat.signal || 'NEUTRAL'}
          </div>
        </div>
        ${volNotes}
        ${extraPats ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.05)">${extraPats}</div>` : ''}
      </div>`;
    }

    // ─── 8. ASSET PROFILE ───
    if (prof) {
      html += `<div class="engineBlock"><div class="engineBlockTitle">Asset Profile</div>
        <div class="engineRow"><span class="engineLabel">Sample size</span><span class="engineVal">${prof.n_legs} legs · ${prof.computed_from_bars} bars</span></div>
        ${prof.typical_impulse_atr ? `<div class="engineRow"><span class="engineLabel">Typical impulse</span><span class="engineVal">${prof.typical_impulse_atr.toFixed(1)} ATR (p50)</span></div>` : ''}
      </div>`;
    }

    readout.innerHTML = html;
    readout.style.display = 'flex';
    toggles.style.display = 'flex';

    // Cache note
    if (cacheNote) {
      if (fromCache && cacheTs) {
        const ago = Math.round((Date.now() - cacheTs) / 60000);
        cacheNote.textContent = '↻ cached · ' + (ago < 1 ? 'just now' : ago + 'm ago') + ' — press R to refresh';
        cacheNote.style.display = 'block';
      } else {
        cacheNote.textContent = '';
        cacheNote.style.display = 'none';
      }
    }

    // Regime chip in context bar
    if (state?.regime) {
      const regStr = state.regime;
      const isBull = regStr.includes('BULL'), isBear = regStr.includes('BEAR');
      const chipCls = isBull ? 'bull' : isBear ? 'bear' : 'range';
      const participPct = state.participation_pct != null ? ` · ${state.participation_pct}p` : '';
      const eTf = document.getElementById('engineContextTf');
      if (eTf?.parentNode) {
        eTf.parentNode.querySelector('.regimeChip')?.remove();
        const chipEl = document.createElement('span');
        chipEl.innerHTML = `<span class="regimeChip ${chipCls}">${regStr.replace(/_/g,' ')}${participPct}</span>`;
        eTf.after(chipEl.firstChild);
      }
      const biasChip = document.getElementById('chevBiasChip');
      if (biasChip) {
        biasChip.textContent = isBull ? '▲ BULL' : isBear ? '▼ BEAR' : '◆ RANGE';
        biasChip.className   = chipCls;
        biasChip.title       = 'Chev\'s last read: ' + regStr.replace(/_/g,' ');
        biasChip.style.display = 'inline-block';
      }
    }

    // Update Brain State Banner in TRADES pane
    _updateBrainStateBanner(d, fromCache ? cacheTs : Date.now());

    redrawAll();
  }

  async function runDexterEngine() {
    const btn      = document.getElementById('runDexterBtn');
    const statusEl = document.getElementById('engineStatus');
    const readout  = document.getElementById('engineReadout');
    const toggles  = document.getElementById('engineOverlayToggles');
    btn.disabled = true;
    btn.classList.add('running');
    // PHASE 3: was an emoji/time.png <img> — kept in sync with the button's own
    // static markup (index.html), which now uses the Lucide sprite. Left as its
    // own fix (not blanket-applied to every emoji in this file) because the mood/
    // conviction icons elsewhere in this function are Chev's personality voice,
    // explicitly out of scope for the icon-set sweep.
    btn.innerHTML = '<svg class="icon runDexterIcon"><use href="#icon-wrench"/></svg>Running...';
    statusEl.textContent = 'Fetching ' + currentSymbol + ' ' + currentTf.toUpperCase() + '...';
    try {
      const r = await _apiFetch('/api/analysis/engine?symbol=' + encodeURIComponent(currentSymbol) + '&tf=' + currentTf);
      if (!r.ok) throw new Error('Dexter ' + r.status + ' — is Dexter running?');
      const fresh = await r.json();
      if (fresh.error) throw new Error(fresh.error);
      const t = new Date().toLocaleTimeString();
      _setDexterStatus(true);
      const eSym = document.getElementById('engineContextSym');
      const eTf  = document.getElementById('engineContextTf');
      const eLR  = document.getElementById('engineLastRun');
      if (eSym) eSym.textContent = currentSymbol;
      if (eTf)  eTf.textContent  = currentTf.toUpperCase();
      if (eLR)  eLR.textContent  = 'Run ' + t;
      const d = fresh;
      statusEl.textContent = t + ' — ' + (d.swings ? d.swings.length : 0) + ' swings, ' + (d.legs ? d.legs.length : 0) + ' legs';
      _saveEngineCache(currentSymbol, currentTf, fresh);
      _applyEngineData(fresh, false, null);
      if (typeof _renderHypHistory === 'function') _renderHypHistory();
      const regime = (fresh.state?.regime || fresh.market_state?.regime || '').replace(/_/g,' ') || 'analyzed';
      const topH   = (fresh.hypotheses || [])[0];
      const hypMsg = topH ? `${(topH.name||'').replace(/_/g,' ')} — ${Math.round((topH.confidence||0)*100)}% conf` : regime;
      const moodFile = (fresh.state?.direction||0) > 25 ? 'fire.png' : (fresh.state?.direction||0) < -25 ? 'furious.png' : 'lets-see.png';
      showNotification('Dexter complete', hypMsg, 'success', moodFile, 5000);

    } catch(e) {
      _setDexterStatus(false);
      statusEl.textContent = 'Error: ' + e.message;
      _engineData = null;
      showNotification('Dexter error', e.message, 'error', 'oh-no.png', 6000);
    } finally {
      btn.disabled = false;
      btn.classList.remove('running');
      btn.innerHTML = '<svg class="icon runDexterIcon"><use href="#icon-wrench"/></svg>Run Dexter';
    }
  }

  document.getElementById('runDexterBtn').addEventListener('click', runDexterEngine);

  ['engChkSwings','engChkLegs','engChkGeometry','engChkBalance','engChkHypothesis','engChkPatterns'].forEach(function(id) {
    const chk = document.getElementById(id);
    if (!chk) return;
    const key = { engChkSwings:'swings', engChkLegs:'legs', engChkGeometry:'geometry', engChkBalance:'balance', engChkHypothesis:'hypothesis', engChkPatterns:'patterns' }[id];
    chk.addEventListener('change', function() { _engineOverlayState[key] = chk.checked; redrawAll(); });
  });

  /* ============================================================
     ARSENAL — Chart Patterns tool (patterns.py output)
     Draws the upper/lower pattern boundary trendlines + pivot
     touch-points that patterns.py returns in the engine payload under
     `patterns`. Rather than duplicate the drawing code, this reuses the
     existing _drawEngineOverlays pattern renderer (section 4) by flipping
     _engineOverlayState.patterns — so the Arsenal "PAT" card, the ENGINE
     tab "Patterns" checkbox, and the chart overlay all stay in sync.
     Data comes from the same /api/analysis/engine endpoint "Run Dexter"
     uses; if it's already loaded (or cached) for the current symbol+tf we
     draw instantly, otherwise we fetch it on click.
     ============================================================ */
  function _syncPatternCardUI(on, label) {
    const btn  = document.getElementById('lyrPatBtn');
    const card = document.getElementById('lyrPatCard');
    const vis  = document.getElementById('lyrPatVis');
    const eng  = document.getElementById('engChkPatterns');
    if (btn)  btn.textContent = label || 'PAT';
    if (card) card.classList.toggle('active', on);
    if (vis)  { vis.checked = on; vis.disabled = !on; }
    if (eng)  eng.checked = on;
  }

  function _patternCardLabel(pat) {
    if (!pat) return 'PAT';
    if (pat.pattern && pat.pattern !== 'None') {
      const conf = Math.round((pat.value || 0) * 100);
      return 'PAT ✓' + (conf ? ' ' + conf + '%' : '');
    }
    // No named pattern, but the boundary trendlines are still drawable
    return (pat.upper_trendline || pat.lower_trendline) ? 'PAT ~' : 'PAT';
  }

  async function drawPatternLines() {
    const btn = document.getElementById('lyrPatBtn');
    // Toggle OFF — hide the overlay, keep the data
    if (_engineOverlayState.patterns) {
      _engineOverlayState.patterns = false;
      _syncPatternCardUI(false, 'PAT');
      redrawAll();
      return;
    }
    if (!currentCandles || !currentCandles.length) {
      showNotification('No chart', 'Load a chart first', 'error', 'oh-no.png', 4000);
      return;
    }
    // Already have engine data for THIS symbol+tf? Draw instantly.
    const haveMatch = _engineData && _engineData.patterns &&
                      _engineData.symbol === currentSymbol && _engineData.tf === currentTf;
    if (!haveMatch) {
      // Try the engine cache first, then fetch fresh from the same endpoint.
      const cached = (typeof _loadEngineCache === 'function') ? _loadEngineCache(currentSymbol, currentTf) : null;
      if (cached && cached.data && cached.data.patterns &&
          cached.data.symbol === currentSymbol && cached.data.tf === currentTf) {
        _engineData = cached.data;
      } else {
        btn.textContent = 'PAT…'; btn.disabled = true;
        try {
          const r = await _apiFetch('/api/analysis/engine?symbol=' + encodeURIComponent(currentSymbol) + '&tf=' + currentTf);
          // Read the body even on a non-OK status — the backend puts the real
          // reason in {"error": ...}, which is far more useful than "Dexter 500".
          let fresh = null;
          try { fresh = await r.json(); } catch (_) {}
          if (!r.ok) throw new Error((fresh && fresh.error) ? fresh.error : ('Dexter ' + r.status + ' — is Dexter running?'));
          if (!fresh) throw new Error('Dexter returned no data');
          if (fresh.error) throw new Error(fresh.error);
          _engineData = fresh;
          if (typeof _saveEngineCache === 'function') _saveEngineCache(currentSymbol, currentTf, fresh);
        } catch (e) {
          _syncPatternCardUI(false, 'PAT');
          showNotification('Patterns error', e.message, 'error', 'oh-no.png', 6000);
          return;
        } finally {
          btn.disabled = false;
        }
      }
    }
    const pat = _engineData && _engineData.patterns;
    if (!pat || (!pat.upper_trendline && !pat.lower_trendline)) {
      _syncPatternCardUI(false, 'PAT');
      showNotification('No patterns', 'No chart-pattern structure on ' + currentSymbol + ' ' + (currentTf || '').toUpperCase(), 'info', 'lets-see.png', 5000);
      return;
    }
    _engineOverlayState.patterns = true;
    _syncPatternCardUI(true, _patternCardLabel(pat));
    redrawAll();
    const named = pat.pattern && pat.pattern !== 'None';
    showNotification('Patterns drawn',
      named ? (pat.pattern + ' · ' + Math.round((pat.value || 0) * 100) + '% conf')
            : 'Boundary trendlines drawn — no named pattern',
      'success', 'ruler.png', 4500);
  }

  // PAT visibility checkbox — hide/show the overlay without refetching.
  (function() {
    const pv = document.getElementById('lyrPatVis');
    if (pv) pv.addEventListener('change', function() {
      _engineOverlayState.patterns = pv.checked;
      const card = document.getElementById('lyrPatCard');
      if (card) card.classList.toggle('active', pv.checked);
      const eng = document.getElementById('engChkPatterns');
      if (eng) eng.checked = pv.checked;
      redrawAll();
    });
    // Reverse sync — toggling the ENGINE-tab "Patterns" checkbox reflects on the card.
    const eng = document.getElementById('engChkPatterns');
    if (eng) eng.addEventListener('change', function() {
      const card = document.getElementById('lyrPatCard');
      const vis  = document.getElementById('lyrPatVis');
      if (card) card.classList.toggle('active', eng.checked);
      if (vis)  { vis.checked = eng.checked; vis.disabled = !eng.checked; }
    });
  })();

  /* ============================================================
     ARSENAL — Trendline Ray tool (Phase W1, ray_registry.json via /api/rays)
     TL's primary click now shows Dexter's own tracked, trust-tallied rays
     instead of the legacy client-side quick-TL detector (still reachable —
     unchanged — from the dropdown, see "Quick TL (local)" wiring below).
     Follows drawPatternLines()'s exact shape: toggle off if already showing;
     otherwise use already-fetched data for this symbol+tf or fetch fresh;
     friendly empty state, not an error, when Dexter has nothing tracked yet.
     ============================================================ */
  function _syncTlCardUI(on, label) {
    const btn  = document.getElementById('lyrTlBtn');
    const card = document.getElementById('lyrTlCard');
    const vis  = document.getElementById('lyrTlVis');
    if (btn)  btn.textContent = label || 'TL';
    if (card) card.classList.toggle('active', on);
    if (vis)  { vis.checked = on; vis.disabled = !on; }
  }

  function _tlCardLabel(data) {
    if (!data || !data.rays || !data.rays.length) return 'TL';
    return 'TL (' + data.rays.length + ')';
  }

  async function drawRegistryRays() {
    const btn = document.getElementById('lyrTlBtn');
    // Toggle OFF — hide the overlay, keep the data (same as drawPatternLines()).
    if (_engineOverlayState.rays) {
      _engineOverlayState.rays = false;
      _syncTlCardUI(false, 'TL');
      redrawAll();
      return;
    }
    if (!currentCandles || !currentCandles.length) {
      showNotification('No chart', 'Load a chart first', 'error', 'oh-no.png', 4000);
      return;
    }
    const haveMatch = _tlRaysData && _tlRaysData.symbol === currentSymbol && _tlRaysData.tf === currentTf;
    if (!haveMatch) {
      btn.textContent = 'TL…'; btn.disabled = true;
      try {
        const r = await _apiFetch('/api/rays?symbol=' + encodeURIComponent(currentSymbol) + '&tf=' + currentTf);
        let fresh = null;
        try { fresh = await r.json(); } catch (_) {}
        if (!r.ok) throw new Error((fresh && fresh.error) ? fresh.error : ('Dexter ' + r.status + ' — is Dexter running?'));
        if (!fresh) throw new Error('Dexter returned no data');
        if (fresh.error) throw new Error(fresh.error);
        _tlRaysData = fresh;
      } catch (e) {
        _syncTlCardUI(false, 'TL');
        showNotification('Trendline Ray error', e.message, 'error', 'oh-no.png', 6000);
        return;
      } finally {
        btn.disabled = false;
      }
    }
    if (!_tlRaysData.rays || !_tlRaysData.rays.length) {
      _syncTlCardUI(false, 'TL');
      showNotification('No tracked rays', 'Dexter has no live trendline ray for ' +
        currentSymbol + ' ' + (currentTf || '').toUpperCase() + ' yet', 'info', 'lets-see.png', 5000);
      return;
    }
    _engineOverlayState.rays = true;
    _syncTlCardUI(true, _tlCardLabel(_tlRaysData));
    redrawAll();
    const res = _tlRaysData.rays.filter(r => r.side === 'upper').length;
    const sup = _tlRaysData.rays.length - res;
    showNotification('Trendline rays drawn', res + ' resistance · ' + sup + ' support', 'success', 'ruler.png', 3500);
  }

  // TL visibility checkbox — hide/show the registry-ray overlay without
  // refetching. Additive alongside the existing generic
  // _wireVisCheckbox('lyrTlVis', '_tl') call below (untouched) -- that one
  // still correctly governs the legacy quick-TL drawings when THOSE are what
  // is showing; this one governs the registry-ray overlay when THAT is
  // showing. Only one is ever populated at a time in practice.
  (function() {
    const tv = document.getElementById('lyrTlVis');
    if (tv) tv.addEventListener('change', function() {
      if (!_tlRaysData) return;
      _engineOverlayState.rays = tv.checked;
      const card = document.getElementById('lyrTlCard');
      if (card) card.classList.toggle('active', tv.checked);
      redrawAll();
    });
  })();

  // "Quick TL (local)" — the TL dropdown's escape hatch back to the legacy
  // client-side detector (chat.js drawTrendlines(), unchanged). TL's primary
  // click no longer calls it directly, but the old behaviour stays reachable.
  (function() {
    const qb = document.getElementById('ctpQuickTlBtn');
    if (qb) qb.addEventListener('click', function() { drawTrendlines(); });
  })();

  // Auto-run ENGINE toggle — re-runs Dexter every 5 minutes
  let _engAutoTimer = null;
  const _engAutoChk = document.getElementById('engChkAuto');
  const _engAutoLabel = document.getElementById('engAutoLabel');
  if (_engAutoChk) {
    _engAutoChk.addEventListener('change', function() {
      clearInterval(_engAutoTimer);
      _engAutoLabel.classList.toggle('active', _engAutoChk.checked);
      if (_engAutoChk.checked) {
        _engAutoTimer = setInterval(function() {
          if (_engAutoChk.checked) runDexterEngine();
        }, 5 * 60 * 1000);
      }
    });
  }

  /* ============================================================
     LAYERS TAB — per-tool hide/show checkboxes (added 2026-07-05)
     Replaces the old #layersList passive management list — each Analysis
     Tools row now owns its own checkbox instead of a separate list managing
     all of them. See _wireVisCheckbox below and the ATR-specific handling.
     ============================================================ */
  function _wireVisCheckbox(checkboxId, tag) {
    const chk = document.getElementById(checkboxId);
    if (!chk) return;
    chk.addEventListener('change', function() {
      drawings.forEach(function(d) { if (d[tag]) d.visible = chk.checked; });
      saveDrawings(); redrawAll(); updateObjTree();
    });
  }
  _wireVisCheckbox('lyrSrVis',  '_sr');
  _wireVisCheckbox('lyrVpVis',  '_vp_stack');
  _wireVisCheckbox('lyrFibVis', '_fib_stack');
  _wireVisCheckbox('lyrTlVis',  '_tl');
  // RSI is deliberately NOT wired through _wireVisCheckbox — _rsi_div drawings are
  // never persisted (see _clearRsiDivOverlay's comment: saveDrawings() here would
  // fire the Firebase SSE stream, which replaces drawings[] and wipes them). This
  // mirrors that same constraint and also needs rsiRedrawAll() for the RSI panel.
  document.getElementById('lyrRsiVis').addEventListener('change', function() {
    const checked = this.checked;
    drawings.forEach(function(d) { if (d._rsi_div) d.visible = checked; });
    redrawAll(); rsiRedrawAll(); updateObjTree();
  });
  // ATR's checkbox IS its on/off state (no chart drawing to toggle visible/hidden
  // on) — checking it fetches+shows the persistent readout, unchecking hides it.
  document.getElementById('lyrAtrVis').addEventListener('change', function() {
    _setAtrVisible(this.checked);
  });

  // Wire LAYERS tab tool buttons to existing Arsenal functions. Class toggling
  // AND the per-row checkbox enable/disable state are both handled synchronously
  // inside the draw functions themselves (2026-07-05) — no external panel refresh
  // needed anymore now that #layersList/renderLayersPanel are gone.
  function _rsiArrowClick() {
    // If RSI is already showing, just reopen the popup so the user can pick a
    // different timeframe — don't toggle the whole overlay off.
    if (document.getElementById('lyrRsiCard').classList.contains('active')) _ctpShow('rsi');
    else showRSIDiv();
  }
  (function() {
    const map = [
      ['lyrSrBtn',  function() { drawSRZones(); }],
      ['lyrVpBtn',  function() { drawVP(); }],
      ['lyrAtrBtn', function() { showATR(); }],
      ['lyrFibBtn', function() { drawFibStack(); }],
      ['lyrRsiBtn', function() { showRSIDiv(); }],
      ['lyrPatBtn', function() { drawPatternLines(); }],
      ['lyrTlBtn',  function() { drawRegistryRays(); }],
      ['lyrTlArrow', function(e) { e.stopPropagation(); _ctpShow('tl'); }],
      ['lyrFibArrow', function(e) { e.stopPropagation(); _ctpShow('fib'); }],
      ['lyrRsiArrow', function(e) { e.stopPropagation(); _rsiArrowClick(); }],
    ];
    map.forEach(function(pair) {
      const el = document.getElementById(pair[0]);
      if (el) el.addEventListener('click', pair[1]);
    });
  })();

