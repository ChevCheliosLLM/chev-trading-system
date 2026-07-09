/* ============================================================
   Indicator toggles (EMA/BB/VWAP/RSI) and their series activation.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     INDICATOR TOGGLES
     ============================================================ */

  /* ---- EMA dropdown ---- */
  const emaMainBtn  = document.getElementById('emaMainBtn');
  const emaDropBtn  = document.getElementById('emaDropBtn');
  const emaDropdown = document.getElementById('emaDropdown');

  function _syncEmaDropBtn() {
    const anyActive = _emaConfig.some(c => c.active);
    emaMainBtn.classList.toggle('active', anyActive);
    emaDropBtn.classList.toggle('active', anyActive);
  }

  function _setRsiActive(active) {
    if (_indicatorState.rsi === active) return;
    _indicatorState.rsi = active;
    document.getElementById('indRsiBtn').classList.toggle('active', active);
    const wrap = document.getElementById('rsiPanelWrap');
    wrap.style.display = active ? 'block' : 'none';
    const chk = document.getElementById('ovChkRsi');
    if (chk) chk.checked = active;
    if (active) {
      _initRsiChart();
      updateIndicators();
      setTimeout(() => {
        try {
          const r = chart.timeScale().getVisibleLogicalRange();
          if (r) rsiChart.timeScale().setVisibleLogicalRange({ from: r.from - RSI_WARMUP, to: r.to - RSI_WARMUP });
        } catch(e) {}
        syncRsiCanvasSize();
        _syncRsiAxisWidth();
      }, 60);
    } else {
      // Destroy the RSI chart instance completely so it can't intercept
      // the main chart's document-level mouse event handlers (LightweightCharts
      // v4 attaches document listeners per instance — two live instances
      // compete for horizontal scroll events, bricking the main chart).
      if (rsiChart) {
        try { rsiChart.remove(); } catch(e) {}
        rsiChart = null;
        rsiLine  = null;
      }
      rsiFirstAnchor = null;
      _rsiCrossX = null;
      _rsiSyncing = false;
      // Give the layout a tick to reflow, then re-sync the main canvas size
      setTimeout(() => { syncCanvasSize(); markDirty(); }, 0);
    }
    updateObjTree();
  }

  function _setEmasActive(active) {
    _emaConfig.forEach((cfg, i) => {
      cfg.active = active;
      emaDropdown.querySelectorAll('.emaDot')[i].classList.toggle('on', active);
      emaSeries[i].applyOptions({ visible: active });
    });
    _syncEmaDropBtn();
    const chk = document.getElementById('ovChkEma');
    if (chk) chk.checked = active;
    if (active) updateIndicators();
  }

  function _setBBActive(active) {
    if (_indicatorState.bb === active) return;
    _indicatorState.bb = active;
    document.getElementById('indBBBtn').classList.toggle('active', active);
    bbUpperSeries.applyOptions({ visible: active });
    bbMidSeries.applyOptions(  { visible: active });
    bbLowerSeries.applyOptions({ visible: active });
    const chk = document.getElementById('ovChkBB');
    if (chk) chk.checked = active;
    if (active) updateIndicators();
    else { bbUpperSeries.setData([]); bbMidSeries.setData([]); bbLowerSeries.setData([]); }
  }

  function _setVwapActive(active) {
    if (_indicatorState.vwap === active) return;
    _indicatorState.vwap = active;
    document.getElementById('indVwapBtn').classList.toggle('active', active);
    vwapSeries.applyOptions({ visible: active });
    const chk = document.getElementById('ovChkVwap');
    if (chk) chk.checked = active;
    if (active) updateIndicators();
    else vwapSeries.setData([]);
  }

