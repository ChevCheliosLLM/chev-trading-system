/* ============================================================
   RSI sub-panel drawing canvas + divergence line drawing.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     RSI DRAW CANVAS — divergence line drawing on RSI sub-panel
     ============================================================ */
  const rsiDrawCanvas = document.getElementById('rsiDrawCanvas');
  const rdctx = rsiDrawCanvas.getContext('2d');

  let rsiDrawings = [];        // user-drawn lines, persisted per symbol
  let rsiOverlayLines = [];    // Chev's auto-drawn divergence (cleared with trade)
  let mtfCandles = [];         // Higher TF candles fetched when Chev tags ms_Xtf
  let mtfTf      = null;       // The TF string e.g. '4h'

  async function _fetchAndDrawMTF(tf) {
    try {
      const candles = await fetchCandles(currentSymbol, tf, currentType, 500);
      mtfCandles = candles;
      mtfTf      = tf;
      const lbl = document.getElementById('ovLabelMTF');
      if (lbl) lbl.lastChild.textContent = ` Structure (${tf.toUpperCase()})`;
    } catch (e) {
      console.warn('[MTF] fetch failed:', e);
    }
  }
  let rsiFirstAnchor = null;   // {time, value} — first click waiting for second
  let rsiHoverX = null;        // current cursor X on RSI panel
  let rsiHoverY = null;        // current cursor Y on RSI panel
  const RSI_TOOLS = ['trendline', 'hline', 'ray'];

  function rsiTimeToX(t) {
    if (t == null || !rsiChart) return null;
    const x = rsiChart.timeScale().timeToCoordinate(t);
    if (x != null) return x;
    return timeToX(t); // fallback: use main chart extrapolation when RSI time scale can't resolve
  }
  function rsiValToY(v) {
    if (!rsiLine) return null;
    const y = rsiLine.priceToCoordinate(v);
    if (y != null) return y;
    // RSI value is outside the auto-scaled visible range.
    // Extrapolate linearly: read the RSI value at top (y=0) and bottom (y=_rdh)
    // of the canvas, then map v proportionally.
    try {
      const topRsi = rsiLine.coordinateToPrice(0);
      const botRsi = rsiLine.coordinateToPrice(_rdh);
      if (topRsi == null || botRsi == null || topRsi === botRsi) return null;
      return _rdh * (v - topRsi) / (botRsi - topRsi);
    } catch(e) { return null; }
  }
  function rsiYToVal(y) {
    if (!rsiLine) return null;
    return rsiLine.coordinateToPrice(y);
  }
  function rsiXToTime(x) {
    if (!rsiChart) return null;
    return rsiChart.timeScale().coordinateToTime(x);
  }

  function _getRsiValAt(t) {
    if (!currentCandles.length) return null;
    const rsiData = calcRSI(currentCandles);
    let best = null, bestD = Infinity;
    for (const d of rsiData) { const dd = Math.abs(d.time - t); if (dd < bestD) { bestD = dd; best = d.value; } }
    return best;
  }
  // Find the RSI extreme (min for bull, max for bear) within ±halfWindow bars of timestamp t.
  // This ensures RSI panel anchors land on actual RSI lows (bull) or RSI highs (bear),
  // matching the side of the price chart being compared.
  function _getRsiExtremeNear(t, bias, halfWindow = 7) {
    if (!currentCandles.length) return { ts: t, val: null };
    const rsiData = calcRSI(currentCandles);
    if (!rsiData.length) return { ts: t, val: null };
    let centerIdx = -1, minDist = Infinity;
    for (let i = 0; i < rsiData.length; i++) {
      const d = Math.abs(rsiData[i].time - t);
      if (d < minDist) { minDist = d; centerIdx = i; }
    }
    if (centerIdx < 0) return { ts: t, val: null };
    const lo = Math.max(0, centerIdx - halfWindow);
    const hi = Math.min(rsiData.length - 1, centerIdx + halfWindow);
    let bestIdx = centerIdx, bestVal = rsiData[centerIdx].value;
    for (let i = lo; i <= hi; i++) {
      const v = rsiData[i].value;
      if (bias === 'bull' && v < bestVal) { bestVal = v; bestIdx = i; }
      if (bias === 'bear' && v > bestVal) { bestVal = v; bestIdx = i; }
    }
    return { ts: rsiData[bestIdx].time, val: bestVal };
  }
  function _getPriceAt(t, dir) {
    let bestC = null, bestD = Infinity;
    for (const c of currentCandles) { const dd = Math.abs(c.time - t); if (dd < bestD) { bestD = dd; bestC = c; } }
    if (!bestC) return null;
    if (dir === 'high') return bestC.high;
    if (dir === 'low')  return bestC.low;
    return bestC.close;
  }
  function _drawRsiDivergence(conf) {
    // Clear any previous RSI div lines
    rsiOverlayLines = [];
    overlayDrawings = overlayDrawings.filter(d => !d._rsiDiv);
    if (!conf || !conf.RSI_DIV_T1 || !conf.RSI_DIV_T2) { markDirty(); return; }
    const rsiV1 = _getRsiValAt(conf.RSI_DIV_T1);
    const rsiV2 = _getRsiValAt(conf.RSI_DIV_T2);
    if (rsiV1 == null || rsiV2 == null) return;
    const priceDir = rsiV2 < rsiV1 ? 'high' : 'low';
    const price1 = _getPriceAt(conf.RSI_DIV_T1, priceDir);
    const price2 = _getPriceAt(conf.RSI_DIV_T2, priceDir);
    rsiOverlayLines = [{ time1: conf.RSI_DIV_T1, value1: rsiV1, time2: conf.RSI_DIV_T2, value2: rsiV2, color: '#f0e027' }];
    if (price1 != null && price2 != null) {
      overlayDrawings.push({ type: 'trendline', time1: conf.RSI_DIV_T1, price1, time2: conf.RSI_DIV_T2, price2, color: '#f0e027', lineWidth: 1.5, visible: true, _rsiDiv: true });
    }
    markDirty();
  }
  // VP: fetch fresh anchor from server (move start → current candle) and redraw
  async function _fetchAndDrawVP() {
    if (!currentSymbol || !currentTf || !_activeTrade) return;
    try {
      const res = await _apiFetch(`/api/analysis/vp?symbol=${currentSymbol}&tf=${currentTf}`);
      if (!res.ok) return;
      const d = await res.json();
      if (!d.start_t || !d.end_t) return;
      _activeTrade.conf = _activeTrade.conf || {};
      _activeTrade.conf.VP_START_T = d.start_t;
      _activeTrade.conf.VP_END_T   = d.end_t;
      _refreshTradeOverlayLines();
    } catch(e) {}
  }

  let _rdw = 0, _rdh = 0; // cached RSI canvas CSS dimensions
  function syncRsiCanvasSize() {
    const wrap = document.getElementById('rsiPanelWrap');
    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    _rdw = w; _rdh = h;
    rsiDrawCanvas.width  = Math.round(w * dpr);
    rsiDrawCanvas.height = Math.round(h * dpr);
    rsiDrawCanvas.style.width  = w + 'px';
    rsiDrawCanvas.style.height = h + 'px';
    rdctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function rsiRedrawAll() {
    if (!_indicatorState.rsi || !rsiChart) return;
    const cw = _rdw, ch = _rdh;
    rdctx.clearRect(0, 0, cw, ch);

    function _rsiStroke(d, sx, sy, ex, ey, dots) {
      rdctx.save();
      rdctx.strokeStyle = d.color || '#f0e027';
      rdctx.lineWidth = d.lineWidth || 1.5;
      rdctx.setLineDash(d.dashed ? [6, 4] : []);
      rdctx.beginPath(); rdctx.moveTo(sx, sy); rdctx.lineTo(ex, ey); rdctx.stroke();
      rdctx.setLineDash([]);
      if (dots) {
        rdctx.fillStyle = d.color || '#f0e027';
        for (const [px, py] of dots) {
          rdctx.beginPath(); rdctx.arc(px, py, 3, 0, Math.PI * 2); rdctx.fill();
        }
      }
      rdctx.restore();
    }

    function _rayExtend(x1, y1, x2, y2) {
      if (x1 === x2) return { ex: x2, ey: y2 > y1 ? ch : 0 };
      const slope = (y2 - y1) / (x2 - x1);
      return { ex: cw, ey: y1 + slope * (cw - x1) };
    }

    function drawRsiShape(d, col) {
      const type = d.type || 'trendline';
      const c = col ? { ...d, color: col } : d;
      if (type === 'hline') {
        const y = rsiValToY(d.value);
        if (y == null) return;
        _rsiStroke(c, 0, y, cw, y, null);
        return;
      }
      const x1 = rsiTimeToX(d.time1), y1 = rsiValToY(d.value1);
      const x2 = rsiTimeToX(d.time2), y2 = rsiValToY(d.value2);
      if (x1 == null || y1 == null || x2 == null || y2 == null) return;
      if (type === 'ray') {
        const { ex, ey } = _rayExtend(x1, y1, x2, y2);
        _rsiStroke(c, x1, y1, ex, ey, [[x1, y1]]);
      } else {
        _rsiStroke(c, x1, y1, x2, y2, [[x1, y1], [x2, y2]]);
      }
    }

    rsiDrawings.forEach(d => { if (d.visible !== false) drawRsiShape(d); });
    rsiOverlayLines.forEach(d => drawRsiShape(d));

    // Preview ghost while placing second point
    if (activeTool && RSI_TOOLS.includes(activeTool) && rsiFirstAnchor && rsiHoverX != null) {
      const col = TOOL_COLORS[activeTool] || '#f0e027';
      const x1 = rsiTimeToX(rsiFirstAnchor.time), y1 = rsiValToY(rsiFirstAnchor.value);
      if (x1 != null && y1 != null) {
        rdctx.save();
        rdctx.strokeStyle = col + '99'; rdctx.lineWidth = 1.5; rdctx.setLineDash([4, 3]);
        if (activeTool === 'ray') {
          const { ex, ey } = _rayExtend(x1, y1, rsiHoverX, rsiHoverY);
          rdctx.beginPath(); rdctx.moveTo(x1, y1); rdctx.lineTo(ex, ey); rdctx.stroke();
        } else {
          rdctx.beginPath(); rdctx.moveTo(x1, y1); rdctx.lineTo(rsiHoverX, rsiHoverY); rdctx.stroke();
        }
        rdctx.setLineDash([]);
        rdctx.fillStyle = col; rdctx.shadowColor = col; rdctx.shadowBlur = 5;
        rdctx.beginPath(); rdctx.arc(x1, y1, 4, 0, Math.PI * 2); rdctx.fill();
        rdctx.restore();
      }
    }
    // Preview hline ghost (shows immediately on first hover before any click)
    if (activeTool === 'hline' && !rsiFirstAnchor && rsiHoverY != null) {
      rdctx.save();
      rdctx.strokeStyle = (TOOL_COLORS.hline || '#f0b429') + '66';
      rdctx.lineWidth = 1.5; rdctx.setLineDash([4, 3]);
      rdctx.beginPath(); rdctx.moveTo(0, rsiHoverY); rdctx.lineTo(cw, rsiHoverY); rdctx.stroke();
      rdctx.restore();
    }
  }

  function _rsiHitTest(d, mx, my) {
    const type = d.type || 'trendline';
    if (d.visible === false) return false;
    if (type === 'hline') {
      const y = rsiValToY(d.value);
      return y != null && Math.abs(my - y) < 10;
    }
    const x1 = rsiTimeToX(d.time1), y1 = rsiValToY(d.value1);
    const x2 = rsiTimeToX(d.time2), y2 = rsiValToY(d.value2);
    if (x1 == null || y1 == null || x2 == null || y2 == null) return false;
    return _ds(mx, my, x1, y1, x2, y2) < 10;
  }

  function saveRsiDrawings(sym) {
    sym = sym || currentSymbol;
    localStorage.setItem('chevRsiDrawings_' + sym, JSON.stringify(rsiDrawings));
  }
  function loadRsiDrawings(sym) {
    try {
      const raw = JSON.parse(localStorage.getItem('chevRsiDrawings_' + (sym || currentSymbol)) || '[]');
      // Drop any drawings with null timestamps — they crash timeToCoordinate
      return raw.filter(d => d.type === 'hline' || (d.time1 != null && d.time2 != null));
    } catch(e) { return []; }
  }

  /* RSI panel — tool drawing via shared activeTool */
  document.getElementById('rsiPanelWrap').addEventListener('click', e => {
    if (!activeTool || !RSI_TOOLS.includes(activeTool)) return;
    const r = document.getElementById('rsiPanelWrap').getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const t = rsiXToTime(x), v = rsiYToVal(y);
    if (t == null || v == null) return;
    const col = TOOL_COLORS[activeTool] || '#9598a1';
    if (activeTool === 'hline') {
      rsiDrawings.push({ type: 'hline', value: v, color: col, lineWidth: 1.5, visible: true });
      saveRsiDrawings(); rsiRedrawAll(); updateObjTree();
      return;
    }
    // trendline / ray: two-click
    if (!rsiFirstAnchor) {
      rsiFirstAnchor = { time: t, value: v };
    } else {
      rsiDrawings.push({ type: activeTool, time1: rsiFirstAnchor.time, value1: rsiFirstAnchor.value, time2: t, value2: v, color: col, lineWidth: 1.5, visible: true });
      rsiFirstAnchor = null;
      saveRsiDrawings(); rsiRedrawAll(); updateObjTree();
    }
  });
  // Right-click on RSI panel: cancel pending draw OR delete nearest line
  document.getElementById('rsiPanelWrap').addEventListener('contextmenu', e => {
    e.preventDefault();
    if (activeTool && RSI_TOOLS.includes(activeTool)) { rsiFirstAnchor = null; rsiRedrawAll(); return; }
    const r = document.getElementById('rsiPanelWrap').getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    for (let i = rsiDrawings.length - 1; i >= 0; i--) {
      if (_rsiHitTest(rsiDrawings[i], mx, my)) {
        rsiDrawings.splice(i, 1);
        saveRsiDrawings(); rsiRedrawAll(); updateObjTree();
        break;
      }
    }
  });

  new ResizeObserver(syncRsiCanvasSize).observe(document.getElementById('rsiPanelWrap'));

  // Re-sync axis widths whenever the main chart area resizes (Chev Chat open/close, window resize)
  let _axisSyncTimer = null;
  new ResizeObserver(() => {
    clearTimeout(_axisSyncTimer);
    _axisSyncTimer = setTimeout(_syncRsiAxisWidth, 80);
  }).observe(document.getElementById('chart'));

  // RSI drawing drag + hover cursor + mousemove preview
  (function() {
    const wrap = document.getElementById('rsiPanelWrap');
    let _drag = null;

    function _nearAnyLine(mx, my) {
      for (let i = rsiDrawings.length - 1; i >= 0; i--) {
        if (_rsiHitTest(rsiDrawings[i], mx, my)) return i;
      }
      return -1;
    }

    wrap.addEventListener('mousemove', e => {
      const r = wrap.getBoundingClientRect();
      rsiHoverX = e.clientX - r.left;
      rsiHoverY = e.clientY - r.top;

      if (_drag) {
        const dx = e.clientX - _drag.startMx;
        const dy = e.clientY - _drag.startMy;
        const d = rsiDrawings[_drag.idx];
        if ((d.type || 'trendline') === 'hline') {
          const v = rsiYToVal(_drag.ohval + dy);
          if (v != null) d.value = v;
        } else {
          const t1 = rsiXToTime(_drag.ox1 + dx), v1 = rsiYToVal(_drag.oy1 + dy);
          const t2 = rsiXToTime(_drag.ox2 + dx), v2 = rsiYToVal(_drag.oy2 + dy);
          if (t1 != null && v1 != null && t2 != null && v2 != null) {
            d.time1 = t1; d.value1 = v1; d.time2 = t2; d.value2 = v2;
          }
        }
        rsiRedrawAll();
        return;
      }

      rsiRedrawAll(); // refreshes preview ghost

      if (activeTool && RSI_TOOLS.includes(activeTool)) {
        wrap.style.cursor = 'crosshair';
        return;
      }
      wrap.style.cursor = _nearAnyLine(rsiHoverX, rsiHoverY) >= 0 ? 'grab' : '';
    });

    wrap.addEventListener('mouseleave', () => {
      rsiHoverX = null; rsiHoverY = null;
      rsiRedrawAll();
    });

    wrap.addEventListener('mousedown', e => {
      if (e.button !== 0 || (activeTool && RSI_TOOLS.includes(activeTool))) return;
      const r = wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const idx = _nearAnyLine(mx, my);
      if (idx < 0) return;
      const d = rsiDrawings[idx];
      const type = d.type || 'trendline';
      _drag = {
        idx, type,
        ohval: type === 'hline' ? rsiValToY(d.value) : null,
        ox1: type !== 'hline' ? rsiTimeToX(d.time1) : null,
        oy1: type !== 'hline' ? rsiValToY(d.value1) : null,
        ox2: type !== 'hline' ? rsiTimeToX(d.time2) : null,
        oy2: type !== 'hline' ? rsiValToY(d.value2) : null,
        startMx: e.clientX, startMy: e.clientY,
      };
      wrap.style.cursor = 'grabbing';
      e.preventDefault();
      e.stopPropagation();
    });

    document.addEventListener('mouseup', () => {
      if (!_drag) return;
      saveRsiDrawings(); updateObjTree();
      _drag = null;
      document.getElementById('rsiPanelWrap').style.cursor = '';
    });
  })();

  // RSI panel drag-to-resize
  (function() {
    const resizeBar = document.getElementById('rsiResizeBar');
    const rsiWrap   = document.getElementById('rsiPanelWrap');
    let resizing = false, startY = 0, startH = 0;
    resizeBar.addEventListener('mousedown', e => {
      resizing = true; startY = e.clientY; startH = rsiWrap.clientHeight;
      document.body.style.cursor = 'ns-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', e => {
      if (!resizing) return;
      const dy = startY - e.clientY; // drag up = bigger panel
      const newH = Math.max(80, Math.min(420, startH + dy));
      rsiWrap.style.height = newH + 'px';
      if (rsiChart) rsiChart.applyOptions({ height: newH });
      syncRsiCanvasSize(); syncCanvasSize();
    });
    document.addEventListener('mouseup', () => {
      if (!resizing) return;
      resizing = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });
  })();

  /* EMA main button: click toggles all 3 EMAs at once */
  emaMainBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    const allOn = _emaConfig.every(c => c.active);
    _setEmasActive(!allOn);
  });

  /* EMA ▾ button: opens/closes the per-line dropdown */
  emaDropBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    if (emaDropdown.style.display === 'flex') { emaDropdown.style.display = 'none'; return; }
    const rect = this.getBoundingClientRect();
    // Right-align dropdown's right edge with the ▾ button's right edge
    emaDropdown.style.left = '';
    emaDropdown.style.right = (window.innerWidth - rect.right) + 'px';
    emaDropdown.style.top   = (rect.bottom + 4) + 'px';
    emaDropdown.style.display = 'flex';
  });

  document.addEventListener('click', function(e) {
    if (!emaDropdown.contains(e.target) && e.target !== emaDropBtn && e.target !== emaMainBtn) {
      emaDropdown.style.display = 'none';
    }
  });

  /* dot = toggle on/off */
  emaDropdown.querySelectorAll('.emaDot').forEach((dot, i) => {
    dot.addEventListener('click', function(e) {
      e.stopPropagation();
      _emaConfig[i].active = !_emaConfig[i].active;
      this.classList.toggle('on', _emaConfig[i].active);
      emaSeries[i].applyOptions({ visible: _emaConfig[i].active });
      _syncEmaDropBtn();
      updateIndicators();
    });
  });

  /* period input = change period and redraw */
  emaDropdown.querySelectorAll('.emaPeriodInput').forEach((input, i) => {
    function applyPeriod() {
      const p = parseInt(input.value);
      if (!p || p < 1 || p > 999) { input.value = _emaConfig[i].period; return; }
      _emaConfig[i].period = p;
      if (_emaConfig[i].active) updateIndicators();
    }
    input.addEventListener('change', applyPeriod);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { applyPeriod(); input.blur(); } });
    input.addEventListener('click', e => e.stopPropagation());
  });

  /* ---- RSI toggle ---- */
  document.getElementById('indRsiBtn').addEventListener('click', function() {
    _setRsiActive(!_indicatorState.rsi);
  });
  document.getElementById('rsiCloseBtn').addEventListener('click', () => _setRsiActive(false));

  /* ---- BB toggle ---- */
  document.getElementById('indBBBtn').addEventListener('click', function() {
    _setBBActive(!_indicatorState.bb);
  });

  /* ---- VWAP toggle ---- */
  document.getElementById('indVwapBtn').addEventListener('click', function() {
    _setVwapActive(!_indicatorState.vwap);
  });

