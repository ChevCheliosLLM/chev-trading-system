/* ============================================================
   Right-panel (Intel) drawer toggle + tab switching. Isolated, runs independently of the main script.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* Isolated toggle — runs independently of the main script */
  var _rt = document.getElementById('rightToggle');
  var _rp = document.getElementById('rightPanel');
  if (_rt && _rp) {
    _rp.style.width = '0px';
    _rp.style.overflow = 'hidden';
    _rp.style.transition = 'width 0.18s ease';
    var _rpOpen = false;
    window._toggleChevPanel = function() {
      _rpOpen = !_rpOpen;
      _rp.style.width = _rpOpen ? '420px' : '0px';
      _rp.style.overflow = _rpOpen ? '' : 'hidden';
      var _targetW = _rpOpen ? 'calc(100% - 420px)' : '';
      var _els = ['chart', 'rsiPanelWrap', 'rsiDrawCanvas'];
      _els.forEach(function(id) {
        var el = document.getElementById(id);
        if (el) { el.style.transition = 'width 0.18s ease'; el.style.width = _targetW; }
      });
      var _ot = document.getElementById('objectTree');
      if (_ot) { _ot.style.transition = 'right 0.18s ease'; _ot.style.right = _rpOpen ? '430px' : '10px'; }
      setTimeout(function() {
        if (window._chevChart) window._chevChart.timeScale().fitContent();
        if (window.syncRsiCanvasSize) window.syncRsiCanvasSize();
        if (window._syncRsiAxisWidth) window._syncRsiAxisWidth();
      }, 220);
    };
    _rt.addEventListener('click', window._toggleChevPanel);
    var _intelClose = document.getElementById('intelCloseBtn');
    if (_intelClose) _intelClose.addEventListener('click', function() {
      if (_rpOpen) window._toggleChevPanel();
    });
    var _histBtn = document.getElementById('chatHistoryToggle');
    if (_histBtn) _histBtn.addEventListener('click', function() {
      var cl = document.getElementById('chatList');
      if (cl) cl.classList.toggle('open');
    });
    /* Tab switching */
    document.querySelectorAll('#intelTabBar .intelTab').forEach(function(tab) {
      tab.addEventListener('click', function() {
        var targetPane = tab.dataset.tab;
        document.querySelectorAll('#intelTabBar .intelTab').forEach(function(t) { t.classList.remove('active'); });
        document.querySelectorAll('#rightPanel .intelPane').forEach(function(p) { p.classList.remove('active'); });
        tab.classList.add('active');
        var pane = document.getElementById(targetPane + 'Pane');
        if (pane) pane.classList.add('active');
        /* Open panel if closed */
        if (!_rpOpen) window._toggleChevPanel();
        /* Refresh engine context bar when switching to engine tab */
        if (targetPane === 'engine') {
          var eSym = document.getElementById('engineContextSym');
          var eTf  = document.getElementById('engineContextTf');
          var sym  = window._currentSymbol || document.getElementById('symLabel')?.textContent || '';
          var tf   = window._currentTf || '';
          if (eSym) eSym.textContent = sym || '—';
          if (eTf)  eTf.textContent  = (tf || '—').toUpperCase();
          /* Restore cached ENGINE result for this symbol if available */
          if (sym && tf && typeof _loadEngineCache === 'function') {
            var cached = _loadEngineCache(sym, tf);
            if (cached && !window._engineData) {
              _applyEngineData(cached.data, true, cached.ts);
              var statusEl = document.getElementById('engineStatus');
              if (statusEl) statusEl.textContent = 'Showing cached result — press R to refresh.';
            }
          }
          /* Render hypothesis history */
          if (typeof _renderHypHistory === 'function') _renderHypHistory();
          /* Weight Lab moved to the Strategy panel's own tab (2026-07-13) —
             see drawing.js's stratTab click wiring, not triggered from here. */
        }
      });
    });
  }
