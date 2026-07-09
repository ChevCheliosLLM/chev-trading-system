/* ============================================================
   js/utils/error-handler.js
   Global Error Boundary for Chev Terminal.

   Loaded as the VERY FIRST <script src> (deferred, so it is
   registered before any later-deferred module executes). It
   installs global handlers that:
     - never block the terminal (errors are caught, logged, and
       swallowed so one bad module cannot blank the whole app),
     - identify the source file + line when the stack allows,
     - classify the failure as a Critical UI Failure vs a Chart
       Engine Exception so operators can triage quickly.

   This is additive: it does not alter any existing module logic,
   so the "byte-equal" zero-regression invariant of the split is
   preserved.
   ============================================================ */
(function () {
  'use strict';

  var SEEN_MAX = 25;
  var _seen = Object.create(null);
  var _count = 0;

  function classify(message, stack) {
    var s = (stack || '') + ' ' + (message || '');
    var chartish = /lightweight-charts|createPriceLine|createSeries|candleSeries|rsiChart|priceToY|timeToX|drawing canvas|renderShape|markDirty|redrawAll|drawing\.js|chart-core\.js|rsi-canvas\.js/i;
    var uiish = /DOMException|getElementById|querySelector|addEventListener|classList|innerHTML|panel-toggle\.js|trading-logs\.js|watchlist\.js|chat\.js|chev-corner\.js/i;
    if (chartish.test(s)) return 'Chart Engine Exception';
    if (uiish.test(s)) return 'Critical UI Failure';
    return 'Unhandled Exception';
  }

  function sourceOf(stack) {
    if (!stack) return 'unknown';
    var lines = stack.split('\n');
    for (var i = 1; i < lines.length; i++) {
      var m = lines[i].match(/\((.*?):(\d+):(\d+)\)/) || lines[i].match(/at (.*?):(\d+):(\d+)/);
      if (m) return (m[1] || 'unknown') + ':' + m[2];
      // file path without parens (some browsers)
      var f = lines[i].match(/((?:[A-Za-z]:)?[\\/][^():\s]+\.js)/);
      if (f) return f[1];
    }
    return 'unknown';
  }

  function stamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function dedupeKey(kind, src) { return kind + '|' + src; }

  function report(kind, msg, src) {
    var key = dedupeKey(kind, src);
    if (_seen[key]) { _seen[key]++; return; }       // suppress repeat spam
    if (_count >= SEEN_MAX) return;                  // hard cap console noise
    _seen[key] = 1; _count++;
    try {
      var line = '[Chev ERROR][' + kind + '] ' + msg + (src !== 'unknown' ? '  (@ ' + src + ')' : '');
      if (typeof console !== 'undefined' && console.error) {
        console.error('%c' + line, 'color:#f23645;font-weight:700');
      }
    } catch (e) { /* never let the handler itself throw */ }
  }

  function install() {
    window.onerror = function (message, source, lineno, colno, error) {
      try {
        var stack = (error && error.stack) ? error.stack : '';
        var src = source ? (source + (lineno ? ':' + lineno : '')) : sourceOf(stack);
        var kind = classify(message, stack);
        report(kind, String(message || 'Script error'), src);
      } catch (e) { /* swallow */ }
      return true; // prevent the default (uncaught) handler; keep terminal alive
    };

    if (typeof window.addEventListener === 'function') {
      window.addEventListener('unhandledrejection', function (ev) {
        try {
          var reason = ev && (ev.reason || ev.detail);
          var msg = reason && reason.message ? reason.message : String(reason);
          var stack = reason && reason.stack ? reason.stack : '';
          var kind = classify(msg, stack);
          report(kind, 'Promise rejected: ' + msg, sourceOf(stack));
        } catch (e) { /* swallow */ }
      });
    }
  }

  if (typeof window !== 'undefined') install();

  /* ---- DIAGNOSTIC: log every click with the exact target ---- */
  if (typeof document !== 'undefined') {
    document.addEventListener('click', function (ev) {
      try {
        var t = ev.target;
        var row = t && t.closest && t.closest('.logRow');
        var btn = t && t.closest && t.closest('.jump-to-chart-btn');
        var tag = row
          ? ('[CLICK-DIAG] .logRow pair=' + (row.dataset.pair || '') + ' type=' + (row.dataset.type || '') + ' row=' + (row.dataset.row || '') + ' btn=' + !!btn)
          : ('[CLICK-DIAG] target=' + (t.tagName || '') + ' class=' + ((t && t.className) || ''));
        console.log(tag);
      } catch (e) { /* never throw from diagnostics */ }
    }, true);
  }
})();
