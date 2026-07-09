/* ============================================================
   Chev's Corner (hypothesis) clickable idea cards.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     CHEV'S CORNER - clickable idea cards, no embedded mini charts.
     Clicking an idea loads its pair into the MAIN chart and draws
     Chev's entry/SL/TP lines there, toggleable via "Chev Tools".
     ============================================================ */
  const chevIdeasEl = document.getElementById('chevIdeas');
  let currentIdeas = [];

  function _scrollToEntry() {
    if (!_activeTrade || _activeTrade.entry == null || !currentCandles.length) return;
    const ep     = _activeTrade.entry;
    const tfSec  = { '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400 }[currentTf] || 3600;
    let entryIdx = -1;

    if (_activeTrade.open_ts) {
      const tsUnix = Math.floor(new Date(_activeTrade.open_ts.replace(' ', 'T')).getTime() / 1000);
      const win    = tfSec * 50;
      let bestDist = Infinity;
      // Prefer: nearest candle to open_ts that also touched the entry price
      for (let i = 0; i < currentCandles.length; i++) {
        const c = currentCandles[i];
        if (Math.abs(c.time - tsUnix) <= win && c.low <= ep && ep <= c.high) {
          const d = Math.abs(c.time - tsUnix);
          if (d < bestDist) { bestDist = d; entryIdx = i; }
        }
      }
    }

    if (entryIdx === -1) return;

    // Place entry candle at 25% from left edge
    const visRange    = chart.timeScale().getVisibleLogicalRange();
    const visibleBars = visRange ? Math.round(visRange.to - visRange.from) : 150;
    chart.timeScale().setVisibleLogicalRange({
      from: entryIdx - Math.round(visibleBars * 0.25),
      to:   entryIdx + Math.round(visibleBars * 0.75),
    });
  }

  function clearChevTools() {
    chevPriceLines.forEach(line => candleSeries.removePriceLine(line));
    chevPriceLines = [];
    chevBanner.classList.remove('active');
    activeIdeaRow = null;
    document.querySelectorAll('.ideaCard').forEach(c => c.classList.remove('activeIdea'));
    clearConfLines();
    activeLogTrade = null;
    _activeTradeKey = null;
    document.querySelectorAll('.logRow').forEach(r => { r.style.borderLeft = ''; r.classList.remove('activeTrade'); });
    // Hide trade overlay bar and reset state
    document.getElementById('tradeOverlayBar').style.display = 'none';
    _overlayEntry = false; _overlayPanelOpen = false; _overlayAnalysisOn = true; _activeTrade = null;
    _overlayComponents = { sr: true, fib: true, vp: false };
    overlayDrawings = [];
    document.getElementById('btnOverlayEntry').classList.remove('active');
    document.getElementById('btnOverlayAnalysis').classList.remove('active');
    document.getElementById('btnOverlayAnalysisArrow').classList.remove('active');
    document.getElementById('overlayAnalysisPanel').style.display = 'none';
    document.getElementById('ovChkSR').checked            = true;
    document.getElementById('ovChkFib').checked           = true;
    document.getElementById('ovChkVP').checked            = false;
    document.getElementById('ovChkRsi').checked            = false;
    document.getElementById('ovChkEma').checked            = false;
    document.getElementById('ovChkBB').checked             = false;
    document.getElementById('ovChkVwap').checked           = false;
    document.getElementById('ovLabelSR').style.display    = '';
    document.getElementById('ovLabelFib').style.display   = '';
    document.getElementById('ovLabelVP').style.display    = '';
    document.getElementById('ovLabelRsi').style.display   = 'none';
    document.getElementById('ovLabelEma').style.display   = 'none';
    document.getElementById('ovLabelBB').style.display    = 'none';
    document.getElementById('ovLabelVwap').style.display  = 'none';
    document.getElementById('ovTagBadges').innerHTML     = '';
    rsiOverlayLines = [];
    document.getElementById('rsiLabel').textContent = 'RSI (14)';
    const _rsiLbl = document.getElementById('ovLabelRsi');
    if (_rsiLbl && _rsiLbl.lastChild) _rsiLbl.lastChild.textContent = '  RSI (14)';
    mtfCandles = []; mtfTf = null;
    document.getElementById('ovChkMTF').checked          = false;
    document.getElementById('ovLabelMTF').style.display  = 'none';
    const _mtfLbl = document.getElementById('ovLabelMTF');
    if (_mtfLbl && _mtfLbl.lastChild) _mtfLbl.lastChild.textContent = ' MTF Structure';
  }

  // Trade overlay state
  let _overlayEntry       = false;
  let _overlayPanelOpen   = false;
  let _overlayAnalysisOn  = true;
  let _overlayComponents  = { sr: true, fib: true, vp: false };
  let _activeTrade        = null;  // { entry, sl, direction, open_ts, conf, symbol }
  let _latestFbData       = null;  // cached Firebase snapshot for forex prices

  document.getElementById('btnOverlayEntry').addEventListener('click', () => {
    _overlayEntry = !_overlayEntry;
    document.getElementById('btnOverlayEntry').classList.toggle('active', _overlayEntry);
    _refreshTradeOverlayLines();
  });
  document.getElementById('btnOverlayAnalysis').addEventListener('click', (e) => {
    e.stopPropagation();
    _overlayAnalysisOn = !_overlayAnalysisOn;
    document.getElementById('btnOverlayAnalysis').classList.toggle('active', _overlayAnalysisOn);
    _refreshTradeOverlayLines();
    // When turning analysis on, activate any checked indicator tools
    if (_overlayAnalysisOn) {
      if (document.getElementById('ovChkRsi').checked)  _setRsiActive(true);
      if (document.getElementById('ovChkEma').checked)  _setEmasActive(true);
      if (document.getElementById('ovChkBB').checked)   _setBBActive(true);
      if (document.getElementById('ovChkVwap').checked) _setVwapActive(true);
    }
    // Note: turning analysis off does NOT deactivate RSI/EMA —
    // if they were already on globally they stay on.
  });
  document.getElementById('btnOverlayAnalysisArrow').addEventListener('click', (e) => {
    e.stopPropagation();
    _overlayPanelOpen = !_overlayPanelOpen;
    document.getElementById('btnOverlayAnalysisArrow').classList.toggle('active', _overlayPanelOpen);
    document.getElementById('overlayAnalysisPanel').style.display = _overlayPanelOpen ? 'flex' : 'none';
  });
  document.getElementById('ovChkSR').addEventListener('change', e => {
    _overlayComponents.sr = e.target.checked;
    _refreshTradeOverlayLines();
  });
  document.getElementById('ovChkFib').addEventListener('change', e => {
    _overlayComponents.fib = e.target.checked;
    _refreshTradeOverlayLines();
  });
  document.getElementById('ovChkVP').addEventListener('change', e => {
    _overlayComponents.vp = e.target.checked;
    _refreshTradeOverlayLines();
  });
  document.getElementById('ovChkRsi').addEventListener('change', e => {
    _setRsiActive(e.target.checked);
  });
  document.getElementById('ovChkEma').addEventListener('change', e => {
    _setEmasActive(e.target.checked);
  });
  document.getElementById('ovChkBB').addEventListener('change', e => {
    _setBBActive(e.target.checked);
  });
  document.getElementById('ovChkVwap').addEventListener('change', e => {
    _setVwapActive(e.target.checked);
  });
  document.addEventListener('click', e => {
    const panel = document.getElementById('overlayAnalysisPanel');
    const arrow = document.getElementById('btnOverlayAnalysisArrow');
    if (_overlayPanelOpen && panel && !panel.contains(e.target) && e.target !== arrow) {
      _overlayPanelOpen = false;
      arrow.classList.remove('active');
      panel.style.display = 'none';
    }
  });

  function _refreshTradeOverlayLines() {
    clearConfLines();
    overlayDrawings = [];
    if (!_activeTrade) { redrawAll(); return; }

    // Entry / SL / TP price lines — when in idea mode, respect pill toggle state
    if (_overlayEntry) {
      const { entry, sl, tp } = _activeTrade;
      const sipOn = _isSipActive();
      const ideaPs = (activeIdeaRow != null) ? _getPillState(activeIdeaRow) : null;
      const showE  = !ideaPs || ideaPs['_E'];
      const showSL = !ideaPs || ideaPs['_SL'];
      const showTP = !ideaPs || ideaPs['_TP'];
      if (entry != null && showE)  try { confPriceLines.push(candleSeries.createPriceLine({ price: entry, color: '#d4af37', lineWidth: 0.75, lineStyle: 0, axisLabelVisible: true, title: 'Entry' })); } catch(e){}
      if (sl    != null && showSL) try { confPriceLines.push(candleSeries.createPriceLine({ price: sl, color: sipOn ? '#d4af37' : '#f23645', lineWidth: 0.5, lineStyle: 2, axisLabelVisible: true, title: sipOn ? 'SIP' : 'SL' })); } catch(e){}
      if (tp    != null && showTP) try { confPriceLines.push(candleSeries.createPriceLine({ price: tp, color: '#089981', lineWidth: 0.5, lineStyle: 2, axisLabelVisible: true, title: 'TP' })); } catch(e){}
    }

    if (!_overlayAnalysisOn) { redrawAll(); return; }

    const cp = _activeTrade.conf || {};
    console.log('[OVERLAY] analysisOn=true confKeys=', Object.keys(cp), 'candles=', (currentCandles||[]).length, 'sr=', _overlayComponents.sr, 'fib=', _overlayComponents.fib);
    const prices = cp.prices || {};

    // S/R levels — clean price lines, no fib clutter
    if (_overlayComponents.sr) {
      if (prices.SR_S) { try { confPriceLines.push(candleSeries.createPriceLine({ price: parseFloat(prices.SR_S), color: '#089981', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: 'Support' })); } catch(e){} }
      if (prices.SR_R) { try { confPriceLines.push(candleSeries.createPriceLine({ price: parseFloat(prices.SR_R), color: '#f23645', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: 'Resist'  })); } catch(e){} }
    }

    // Fibonacci — canvas fib tool, no price axis labels
    if (_overlayComponents.fib && prices.FB_618 != null && prices.FB_50 != null && currentCandles && currentCandles.length >= 2) {
      let time1, price1, time2, price2;

      if (prices.FB_LOW_T && prices.FB_HIGH_T && prices.FB_LOW_P != null && prices.FB_HIGH_P != null) {
        // Chev provided exact swing anchor timestamps — draw precisely
        time1  = parseInt(prices.FB_LOW_T);  price1 = parseFloat(prices.FB_LOW_P);
        time2  = parseInt(prices.FB_HIGH_T); price2 = parseFloat(prices.FB_HIGH_P);
      } else {
        // Approximate from FB_50/FB_618 — find nearest candle to each price level
        const fb50  = parseFloat(prices.FB_50);
        const fb618 = parseFloat(prices.FB_618);
        const range = (fb50 - fb618) / (0.618 - 0.5);
        const pLow  = Math.min(fb50 + range * 0.5, fb50 - range * 0.5);
        const pHigh = Math.max(fb50 + range * 0.5, fb50 - range * 0.5);
        let tLow = currentCandles[0], tHigh = currentCandles[currentCandles.length - 1];
        let dLow = Infinity, dHigh = Infinity;
        for (const c of currentCandles) {
          const dl = Math.abs(c.low  - pLow);
          const dh = Math.abs(c.high - pHigh);
          if (dl < dLow)  { dLow  = dl; tLow  = c; }
          if (dh < dHigh) { dHigh = dh; tHigh = c; }
        }
        time1 = tLow.time; price1 = pLow; time2 = tHigh.time; price2 = pHigh;
      }

      overlayDrawings = [{
        type: 'fib',
        time1, price1, time2, price2,
        color: '#d4af37', lineWidth: 1.5, visible: true,
      }];
    }

    // Volume Profile — drawn from Chev's detected consolidation range
    if (_overlayComponents.vp && prices.VP_START_T && prices.VP_END_T) {
      overlayDrawings.push({
        type: 'vp',
        time1: parseInt(prices.VP_START_T),
        time2: parseInt(prices.VP_END_T),
        visible: true,
      });
    }

    redrawAll();
  }

  function activateTradeOverlay(trade, conf) {
    _activeTrade = { ...trade, conf };
    const tagArr = (trade.tags || '').split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
    // Check Chev's tags for each confluence type (handle timeframe suffixes like sr_4h, rsi_1h)
    const tagHasSR   = tagArr.some(t => t === 'sr'  || t.startsWith('sr_'));
    const tagHasFib  = tagArr.some(t => t === 'fib' || t.startsWith('fib_') || t === 'gp');
    const tagHasVP   = tagArr.some(t => t === 'vp'  || t === 'volprofile');
    const hasRsi     = tagArr.some(t => t === 'rsi' || t.startsWith('rsi_'));
    const hasEma     = tagArr.some(t => t === 'ema' || t.startsWith('ema_'));
    const hasBB      = tagArr.some(t => t === 'bb'  || t.startsWith('bb_'));
    const hasVwap    = tagArr.some(t => t === 'vw'  || t === 'vwap');
    const msTags     = tagArr.filter(t => t === 'ms' || t.startsWith('ms_'));
    const hasMTF     = msTags.length > 0;
    const mtfTagTf   = hasMTF ? (msTags[0].includes('_') ? msTags[0].split('_')[1] : '4h') : null;
    // SR/Fib/VP require conf data to draw; RSI/EMA/BB/VWAP are calculated from candles
    const prices = conf.prices || {};
    const hasSR  = tagHasSR  && !!(prices.SR_S || prices.SR_R);
    const hasFib = tagHasFib && prices.FB_618 != null;
    const hasVP  = tagHasVP  && !!(prices.VP_START_T && prices.VP_END_T);
    _overlayEntry = true; _overlayPanelOpen = false; _overlayAnalysisOn = true;
    // Default: no overlays auto-drawn — user ticks what they want to see
    _overlayComponents = { sr: false, fib: false, vp: false };
    document.getElementById('tradeOverlayBar').style.display = 'flex';
    document.getElementById('btnOverlayEntry').classList.add('active');
    document.getElementById('btnOverlayAnalysis').classList.add('active');
    document.getElementById('btnOverlayAnalysisArrow').classList.remove('active');
    document.getElementById('overlayAnalysisPanel').style.display = 'none';
    // All checkboxes off — user decides what to load
    document.getElementById('ovChkSR').checked            = false;
    document.getElementById('ovChkFib').checked           = false;
    document.getElementById('ovChkVP').checked            = false;
    document.getElementById('ovChkRsi').checked           = false;
    document.getElementById('ovChkEma').checked           = false;
    document.getElementById('ovChkBB').checked            = false;
    document.getElementById('ovChkVwap').checked          = false;
    document.getElementById('ovChkMTF').checked           = false;
    // Show labels only for tools Chev actually used in this trade (so user knows what's available)
    document.getElementById('ovLabelSR').style.display    = hasSR    ? '' : 'none';
    document.getElementById('ovLabelFib').style.display   = hasFib   ? '' : 'none';
    document.getElementById('ovLabelVP').style.display    = hasVP    ? '' : 'none';
    document.getElementById('ovLabelRsi').style.display   = hasRsi   ? '' : 'none';
    document.getElementById('ovLabelEma').style.display   = hasEma   ? '' : 'none';
    document.getElementById('ovLabelBB').style.display    = hasBB    ? '' : 'none';
    document.getElementById('ovLabelVwap').style.display  = hasVwap  ? '' : 'none';
    document.getElementById('ovLabelMTF').style.display   = hasMTF   ? '' : 'none';
    // NOTE: No auto-activation of RSI/EMA/BB/VWAP — user ticks to load
    mtfCandles = []; mtfTf = null;
    if (hasMTF && mtfTagTf && mtfTagTf !== currentTf) _fetchAndDrawMTF(mtfTagTf);
    // Auto-draw RSI divergence lines when Chev provided both anchor timestamps
    rsiOverlayLines = [];
    if (conf.RSI_DIV_T1 && conf.RSI_DIV_T2) {
      const rsiV1 = _getRsiValAt(conf.RSI_DIV_T1);
      const rsiV2 = _getRsiValAt(conf.RSI_DIV_T2);
      const priceDir = (rsiV1 != null && rsiV2 != null && rsiV2 < rsiV1) ? 'high' : 'low';
      const price1 = _getPriceAt(conf.RSI_DIV_T1, priceDir);
      const price2 = _getPriceAt(conf.RSI_DIV_T2, priceDir);
      if (rsiV1 !== null && rsiV2 !== null) {
        _setRsiActive(true);
        syncRsiCanvasSize();
        rsiOverlayLines = [{ time1: conf.RSI_DIV_T1, value1: rsiV1, time2: conf.RSI_DIV_T2, value2: rsiV2, color: '#f0e027' }];
        document.getElementById('rsiLabel').textContent = 'RSI Div (4H)';
        const lbl = document.getElementById('ovLabelRsi');
        if (lbl.lastChild) lbl.lastChild.textContent = '  RSI Div (4H)';
      }
      if (price1 != null && price2 != null) {
        overlayDrawings.push({ type: 'trendline', time1: conf.RSI_DIV_T1, price1: price1, time2: conf.RSI_DIV_T2, price2: price2, color: '#f0e027', lineWidth: 1.5, visible: true });
      }
    }
    // Filter out all tags handled by checkboxes (including timeframe variants like sr_4h, rsi_1h)
    const HANDLED_PREFIXES = ['sr', 'fib', 'rsi', 'ema', 'bb', 'gp', 'vp', 'volprofile', 'vw', 'vwap', 'rs', 'em', 'fb', 'ms'];
    const badges = tagArr.filter(t => !HANDLED_PREFIXES.some(p => t === p || t.startsWith(p + '_')));
    document.getElementById('ovTagBadges').innerHTML = badges.map(t => {
      const label = TAG_NAMES[t.toUpperCase()] || t.toUpperCase();
      return `<span style="background:rgba(212,175,55,0.12);border:1px solid rgba(212,175,55,0.3);color:#d4af37;border-radius:3px;padding:1px 5px;font-size:9px">${label}</span>`;
    }).join('');
    _refreshTradeOverlayLines();
  }

  function drawChevToolsForIdea(idea) {
    chevPriceLines.forEach(line => candleSeries.removePriceLine(line));
    chevPriceLines = [];
    if (!chevToolsOn) return;
    // Price lines are controlled by the E / SL / TP pills — see _refreshTradeOverlayLines
    // Rich position strip
    const isLong = (idea.direction || '').toLowerCase() === 'long';
    const dirCls = isLong ? 'long' : 'short';
    const dirArrow = isLong ? '▲' : '▼';
    const e = parseFloat(idea.entry), sl = parseFloat(idea.sl), tp = parseFloat(idea.tp);
    const slDist = (e && sl) ? ((Math.abs(e - sl) / e) * 100).toFixed(2) + '%' : '';
    const tpDist = (e && tp) ? ('+' + (Math.abs(tp - e) / e * 100).toFixed(2) + '%') : '';
    const rrStr  = (e && sl && tp && Math.abs(e - sl) > 0)
      ? 'R:R ' + (Math.abs(tp - e) / Math.abs(e - sl)).toFixed(1) : '';
    const lev = idea.leverage ? ' · ' + idea.leverage + 'x' : '';
    const note = idea.reason || idea.note || '';
    const noteHtml = note ? `<span class="posStatus" style="color:#5d6068;margin-left:4px">— ${note.slice(0, 60)}${note.length > 60 ? '…' : ''}</span>` : '';
    chevBanner.innerHTML = `
      <span style="font-size:7px;font-family:'Inter',sans-serif;letter-spacing:0.08em;font-weight:700;color:rgba(212,175,55,0.85);background:rgba(212,175,55,0.1);border:1px solid rgba(212,175,55,0.35);border-radius:3px;padding:2px 6px;flex-shrink:0;white-space:nowrap">ON RADAR</span>
      <span class="posBadge ${dirCls}">${dirArrow} ${(idea.direction || '').toUpperCase()}</span>
      <span class="posSymbol">${idea.pair}${lev}</span>
      <span class="posEntry">E <b>${idea.entry || '—'}</b></span>
      <span class="posSL">SL ${idea.sl || '—'}${slDist ? `<span style="color:#6d2f35;margin-left:3px">${slDist}</span>` : ''}</span>
      <span class="posTP">TP ${idea.tp || '—'}${tpDist ? `<span style="color:#1a5c52;margin-left:3px">${tpDist}</span>` : ''}</span>
      ${rrStr ? `<span class="posRR"><img src="emoji/ruler.png" alt="" style="width:9px;height:9px;margin-right:3px;vertical-align:middle;opacity:0.8">${rrStr}</span>` : ''}
      <span class="posStatus"><img src="emoji/lets-see.png" alt="" style="width:11px;height:11px;margin-right:3px;vertical-align:middle;opacity:0.85">PENDING</span>
      ${noteHtml}`;
    chevBanner.classList.add('active');
  }

  function _applyIdeaPill(idea, tag, isOn) {
    const base = tag.split('_')[0].toLowerCase();
    console.log('[PILL] click tag=', tag, 'base=', base, 'isOn=', isOn, 'row=', idea.row, 'hasConf=', !!(idea && idea.confluence_prices));
    if (tag === '_E' || tag === '_SL' || tag === '_TP') {
      // E/SL/TP price line pills — _refreshTradeOverlayLines reads pill state directly
      _refreshTradeOverlayLines();
    } else if (base === 'sr') {
      _overlayAnalysisOn = true;
      _overlayComponents.sr = true;
      console.log('[PILL] SR forced ON overlayAnalysisOn=', _overlayAnalysisOn, 'components.sr=', _overlayComponents.sr);
      _refreshTradeOverlayLines();
    } else if (base === 'fib' || base === 'fb' || base === 'gp' || tag.toLowerCase() === 'golden pocket') {
      _overlayAnalysisOn = true;
      _overlayComponents.fib = true;
      console.log('[PILL] FIB forced ON overlayAnalysisOn=', _overlayAnalysisOn, 'components.fib=', _overlayComponents.fib);
      _refreshTradeOverlayLines();
    } else if (base === 'vp' || tag.toLowerCase() === 'volume profile' || base === 'volprofile') {
      _overlayComponents.vp = isOn;
      if (isOn) { _overlayAnalysisOn = true; _fetchAndDrawVP(); }
      else _refreshTradeOverlayLines();
    } else if (base === 'rsi' || base === 'rs' || base === 'dv') {
      _setRsiActive(isOn);
      if (isOn) {
        showRSIDiv();
      } else {
        _clearRsiDivOverlay();
      }
    } else if (base === 'ema' || base === 'em') {
      _setEmasActive(isOn);
    } else if (base === 'bb') {
      _setBBActive(isOn);
    } else if (base === 'vw' || base === 'vwap') {
      _setVwapActive(isOn);
    } else if (base === 'ms') {
      const mtfTfTag = tag.includes('_') ? tag.split('_').slice(1).join('_') : '4h';
      if (isOn) { if (typeof _fetchAndDrawMTF === 'function') _fetchAndDrawMTF(mtfTfTag); }
      else { mtfCandles = []; markDirty(); }
    }
  }

  async function selectIdea(idea) {
    activeIdeaRow = idea.row;
    document.querySelectorAll('.ideaCard').forEach(c => c.classList.toggle('activeIdea', c.dataset.row === String(idea.row)));
    // Naked chart — clear all active indicators and any RSI div overlay before loading
    _clearRsiDivOverlay();
    if (_indicatorState.rsi)  _setRsiActive(false);
    if (_indicatorState.bb)   _setBBActive(false);
    if (_indicatorState.vwap) _setVwapActive(false);
    if (_emaConfig?.some(c => c.active)) _setEmasActive(false);
    const type = idea.pair.endsWith('USDT') ? 'crypto' : (idea.pair.includes('/') ? 'forex' : 'stock');
    currentSymbol = idea.pair;
    currentType = type;
    // Switch to idea's timeframe if specified
    if (idea.tf && idea.tf !== currentTf) {
      currentTf = idea.tf;
      document.querySelectorAll('[data-tf]').forEach(b => b.classList.toggle('active', b.dataset.tf === idea.tf));
    }
    drawings = loadDrawings(currentSymbol);
    rsiDrawings = loadRsiDrawings(currentSymbol);
    updateObjTree();
    _syncDrawings(currentSymbol); _subscribeDrawings(currentSymbol);
    watchlistInner.querySelectorAll('.watchRow').forEach(r => r.classList.remove('active'));
    clearChevTools();
    activeIdeaRow = idea.row; // re-set — clearChevTools() nulls it
    document.querySelectorAll('.ideaCard').forEach(c => c.classList.toggle('activeIdea', c.dataset.row === String(idea.row)));
    await loadChart(currentSymbol, currentTf, currentType);
    startPolling();
    drawChevToolsForIdea(idea);
    // Wire into position overlay so TP/SL zones appear on the canvas
    _activeTrade = {
      entry:     parseFloat(idea.entry)    || null,
      sl:        parseFloat(idea.sl)       || null,
      tp:        parseFloat(idea.tp)       || null,
      direction: (idea.direction || '').toUpperCase(),
      symbol:    idea.pair,
      open_ts:   null,
      status:    'PENDING',
      conf:      idea.confluence_prices || {},
      leverage:  idea.leverage || 1,
      is_sip:    false,
    };
    _overlayEntry = true;
    _overlayAnalysisOn = false;
    _overlayComponents = { sr: false, fib: false, vp: false };
    document.getElementById('tradeOverlayBar').style.display = 'flex';
    document.getElementById('btnOverlayEntry').classList.add('active');
    document.getElementById('btnOverlayAnalysis').classList.remove('active');
    // Restore previously toggled pills, then draw price lines once with correct state
    const _savedPills = _getPillState(idea.row);
    for (const [tag, on] of Object.entries(_savedPills)) {
      if (!on) continue;
      const b = tag.split('_')[0].toLowerCase();
      if (b === 'sr') { _overlayAnalysisOn = true; _overlayComponents.sr = true; }
      else if (b === 'fib' || b === 'fb' || b === 'gp' || tag.toLowerCase() === 'golden pocket') { _overlayAnalysisOn = true; _overlayComponents.fib = true; }
      else if (b === 'vp' || tag.toLowerCase() === 'volume profile' || b === 'volprofile') { _overlayAnalysisOn = true; _overlayComponents.vp = true; }
      else if (b === 'rsi' || b === 'rs' || b === 'dv') _setRsiActive(true);
      else if (b === 'ema' || b === 'em') _setEmasActive(true);
      else if (b === 'bb') _setBBActive(true);
      else if (b === 'vw' || b === 'vwap') _setVwapActive(true);
      else if (b === 'ms') { const mtfT = tag.includes('_') ? tag.split('_').slice(1).join('_') : '4h'; if (typeof _fetchAndDrawMTF === 'function') _fetchAndDrawMTF(mtfT); }
    }
    _refreshTradeOverlayLines();
    // Restore RSI divergence lines if that pill was saved ON
    const _rsiDivOn = Object.entries(_savedPills).some(([tag, on]) => {
      const b = tag.split('_')[0].toLowerCase();
      return on && (b === 'rsi' || b === 'rs' || b === 'dv');
    });
    if (_rsiDivOn) showRSIDiv();
    const _vpOn = Object.entries(_savedPills).some(([tag, on]) => {
      const b = tag.split('_')[0].toLowerCase();
      return on && (b === 'vp' || b === 'volprofile' || tag.toLowerCase() === 'volume profile');
    });
    if (_vpOn) _fetchAndDrawVP();
    _updateChatContext();
  }

  function _updateWatchlistIdeaDots() {
    const ideaSymbols = new Set(currentIdeas.map(i => i.pair));
    document.querySelectorAll('.watchRow').forEach(row => {
      const sym = row.dataset.symbol;
      const dot = row.querySelector('.ideaDot');
      if (dot) dot.classList.toggle('show', !!(sym && ideaSymbols.has(sym)));
    });
  }

  function _updatePipelineBadges() {
    const radarBadge = document.getElementById('radarBadge');
    const hdrOnRadar = document.getElementById('hdrOnRadar');
    if (radarBadge) {
      const n = currentIdeas.length;
      radarBadge.textContent = n;
      radarBadge.style.display = n ? '' : 'none';
    }
    if (hdrOnRadar) hdrOnRadar.classList.toggle('has-data', currentIdeas.length > 0);
  }

  function renderChevIdeas() {
    _updatePipelineBadges();
    if (!currentIdeas.length) {
      chevIdeasEl.innerHTML = '<div class="emptyNote chev"><img src="emoji/lets-see.png" alt="" style="width:14px;height:14px;vertical-align:middle;margin-right:6px;opacity:0.6">No setups on radar. Chev\'s watching.</div>';
      _updateWatchlistIdeaDots();
      return;
    }
    chevIdeasEl.innerHTML = currentIdeas.map(idea => {
      const isLong   = (idea.direction || '').toLowerCase() === 'long';
      const dirCls   = isLong ? 'long' : 'short';
      const dirArrow = isLong ? '⬆' : '⬇';
      const tags     = (idea.tags || '').split(',').map(t => t.trim()).filter(Boolean);
      // If a fib tag exists, drop standalone gp (golden pocket is already drawn inside the fib overlay)
      const _hasFibTag = tags.some(t => { const b = t.split('_')[0].toLowerCase(); return b === 'fib' || b === 'fb'; });
      const visibleTags = tags.filter(t => {
        if (!_hasFibTag) return true;
        const b = t.split('_')[0].toLowerCase();
        return b !== 'gp' && t.toLowerCase() !== 'golden pocket';
      });
      const _ps = _getPillState(idea.row);
      const tagChips = visibleTags.map(t => `<button class="ideaPill${_ps[t]?' active':''}" data-tag="${t}" data-row="${idea.row}">${_tagLabel(t, idea.tf)}</button>`).join('');

      // R:R chip
      let rrChip = '';
      if (idea.entry != null && idea.sl != null && idea.tp != null) {
        const risk   = Math.abs(idea.entry - idea.sl);
        const reward = Math.abs(idea.tp    - idea.entry);
        if (risk > 0) rrChip = `<span class="ideaRRChip"><img src="emoji/ruler.png" alt="" style="width:9px;height:9px;margin-right:2px;vertical-align:middle;opacity:0.7">R:R ${(reward/risk).toFixed(1)}</span>`;
      }

      // Leverage chip
      const levChip = idea.leverage ? `<span class="ideaLevChip">${idea.leverage}x</span>` : '';

      // Confidence bar (conf is 0–1 float or 0–100 int)
      let confHtml = '';
      if (idea.conf != null) {
        const confPct = idea.conf > 1 ? Math.round(idea.conf) : Math.round(idea.conf * 100);
        const confCls = confPct >= 65 ? 'high' : confPct >= 40 ? 'mid' : 'low';
        confHtml = `<div class="ideaConfRow">
          <span>CONF</span>
          <div class="ideaConfTrack"><div class="ideaConfFill ${confCls}" style="width:${confPct}%"></div></div>
          <span>${confPct}%</span>
        </div>`;
      }

      // Reasoning note (reason or note field)
      const noteText = idea.reason || idea.note || '';
      const noteHtml = noteText ? `<div class="ideaNote">${noteText.replace(/</g,'&lt;')}</div>` : '';

      const activeCls = idea.row === activeIdeaRow ? ' activeIdea' : '';
      return `
        <div class="ideaCard ${dirCls}${activeCls}" data-row="${idea.row}">
          <div class="ideaCardTop">
            <span class="ideaDirBadge ${dirCls}">${dirArrow} ${(idea.direction||'').toUpperCase()}</span>
            <span class="ideaPairName">${idea.pair}</span>
            <span class="ideaTypeLabel" style="margin-left:4px">${idea.trade_type || ''}</span>
            ${rrChip}${levChip}
          </div>
          ${tagChips ? `<div class="ideaTagRow">${tagChips}</div>` : ''}
          <div class="ideaPriceRow">
            ${idea.entry != null ? `<button class="ideaPill ideaPillE${_ps['_E']?' active':''}" data-tag="_E" data-row="${idea.row}">E ${idea.entry}</button>` : ''}
            ${idea.sl    != null ? `<button class="ideaPill ideaPillSL${_ps['_SL']?' active':''}" data-tag="_SL" data-row="${idea.row}">SL ${idea.sl}</button>` : ''}
            ${idea.tp    != null ? `<button class="ideaPill ideaPillTP${_ps['_TP']?' active':''}" data-tag="_TP" data-row="${idea.row}">TP ${idea.tp}</button>` : ''}
          </div>
          ${confHtml}
          ${noteHtml}
          <button class="ideaAskBtn" data-row="${idea.row}" title="Pre-fill chat with this trade context">
            <img src="emoji/call.png" onerror="this.style.display='none'" alt="">Ask Chev about this
          </button>
        </div>`;
    }).join('');
    chevIdeasEl.querySelectorAll('.ideaCard').forEach(card => {
      card.addEventListener('click', e => {
        if (e.target.closest('.ideaAskBtn') || e.target.closest('.ideaPill')) return;
        if (String(activeIdeaRow) === card.dataset.row) { clearChevTools(); return; }
        const idea = currentIdeas.find(i => String(i.row) === card.dataset.row);
        if (idea) selectIdea(idea);
      });
    });
    chevIdeasEl.querySelectorAll('.ideaPill').forEach(pill => {
      pill.addEventListener('click', e => {
        e.stopPropagation();
        const tag  = pill.dataset.tag;
        const row  = pill.dataset.row;
        const isOn = !pill.classList.contains('active');
        pill.classList.toggle('active', isOn);
        _setPillState(row, tag, isOn);
        const idea = currentIdeas.find(i => String(i.row) === row);
        if (idea && String(activeIdeaRow) === row) _applyIdeaPill(idea, tag, isOn);
      });
    });
    chevIdeasEl.querySelectorAll('.ideaAskBtn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const idea = currentIdeas.find(i => String(i.row) === btn.dataset.row);
        if (!idea) return;
        const rrStr = (idea.entry && idea.sl && idea.tp)
          ? ` R:R ${(Math.abs(idea.tp-idea.entry)/Math.abs(idea.entry-idea.sl)).toFixed(1)}`
          : '';
        const confStr = idea.conf != null
          ? ` Conf: ${idea.conf > 1 ? Math.round(idea.conf) : Math.round(idea.conf*100)}%.`
          : '';
        const noteStr = (idea.reason || idea.note) ? ` Your note: "${idea.reason||idea.note}".` : '';
        const prefill = `I'm looking at your ${idea.pair} ${(idea.direction||'').toUpperCase()} idea` +
          ` (E: ${idea.entry} / SL: ${idea.sl} / TP: ${idea.tp}${rrStr}).${confStr}${noteStr} What's your thinking here?`;
        const chatInput = document.getElementById('chatInput');
        const chatTab   = document.querySelector('#intelTabBar [data-tab="chat"]');
        if (chatInput) { chatInput.value = prefill; chatInput.focus(); }
        if (chatTab) chatTab.click();
      });
    });
    _updateWatchlistIdeaDots();
  }

  async function refreshChevsCorner() {
    try {
      const res = await _apiFetch('/api/pending');
      if (!res.ok) throw new Error('no response');
      currentIdeas = await res.json();
      renderChevIdeas();
      _setDexterStatus(true);
    } catch (e) {
      _setDexterStatus(false);
      chevIdeasEl.innerHTML = '<div class="emptyNote chev"><img src="emoji/furious.png" alt="" style="width:14px;height:14px;vertical-align:middle;margin-right:6px;opacity:0.8">Dexter unreachable. <b>Check the Flask server.</b></div>';
    }
  }

