/* ============================================================
   PHASE 4 Task 2: symbol header strip (symbol name, live price, 24h
   change, small muted 24h high/low/volume). Two data sources feed it:
     - window.updateSymbolHeader(candles, symbol), called from
       chart-core.js right after its existing updatePriceLabel(candles)
       calls in loadChart() and pollLatestCandle() -- covers symbol
       switches, timeframe switches, and every live poll tick;
     - window.updateSymbolHeaderStats(stats), called from watchlist.js's
       applyWatchPrice() (the single existing choke point for every
       watchlist price tick) whenever the ticker fetch that just landed
       is for the symbol currently on the chart. Crypto (Binance
       ticker/24hr) and stocks (Finnhub /quote's day h/l) already fetch
       real 24h-ish stats and previously discarded them.
   Forex has no 24h ticker anywhere in this codebase (see the "session
   open" comment in watchlist.js's refreshForexPrices), and Finnhub's
   /quote has no volume field at all. Wherever a real stat is missing,
   this file estimates it from the visible chart candles (last 24h of
   real candle timestamps, or the whole loaded range if there isn't 24h
   of history) and marks that one stat with a small "~" flag plus a
   tooltip -- so the limitation is visible, not silently papered over.
   ============================================================ */
(function () {
  let _tickerStats = null;   // {high, low, volume, changePct, changeAbs} for the CURRENT symbol, or null
  let _tickerSymbol = null;  // which symbol _tickerStats belongs to
  let _lastCandles = null;   // most recent candles passed to updateSymbolHeader, re-rendered when ticker stats land

  const elName = document.getElementById('symHeaderName');
  const elPrice = document.getElementById('symHeaderPrice');
  const elChangePct = document.getElementById('symHeaderChangePct');
  const elChangeAbs = document.getElementById('symHeaderChangeAbs');
  const elHigh = document.getElementById('symHeaderHigh');
  const elLow = document.getElementById('symHeaderLow');
  const elVol = document.getElementById('symHeaderVol');
  if (!elName) return;

  const EST_TIP = 'Estimated from chart (no 24h feed for this symbol)';

  function setStat(el, value, isEst, formatter) {
    el.textContent = formatter(value) + (isEst ? ' ~' : '');
    const statSpan = el.closest('.symHeaderStat');
    if (isEst) statSpan.setAttribute('data-tip', EST_TIP);
    else statSpan.removeAttribute('data-tip');
  }

  function fmtPrice(v) { return v.toLocaleString(undefined, { maximumFractionDigits: 6 }); }

  // Fallback 24h window computed straight from the candles already on the
  // chart -- used whenever a ticker doesn't cover a field (forex: always;
  // crypto/stock: only until their first ticker tick lands).
  function candleFallback(candles) {
    const last = candles[candles.length - 1];
    const dayAgo = last.time - 86400;
    let win = candles.filter(c => c.time >= dayAgo);
    if (win.length < 2) win = candles; // not enough history loaded -- use the whole visible range instead
    const ref = win[0];
    const high = Math.max(...win.map(c => c.high));
    const low = Math.min(...win.map(c => c.low));
    const volume = win.reduce((sum, c) => sum + (c.volume || 0), 0);
    const changePct = ref.close ? ((last.close - ref.close) / ref.close) * 100 : 0;
    const changeAbs = last.close - ref.close;
    return { high, low, volume, changePct, changeAbs };
  }

  function render(candles, symbol) {
    const last = candles[candles.length - 1];
    const fb = candleFallback(candles);
    const t = _tickerStats;

    elName.textContent = symbol;
    elPrice.textContent = fmtPrice(last.close);

    const changePct = (t && t.changePct != null) ? t.changePct : fb.changePct;
    const changeAbs = (t && t.changeAbs != null) ? t.changeAbs : fb.changeAbs;
    const up = changePct >= 0;
    elChangePct.textContent = (up ? '+' : '') + changePct.toFixed(2) + '%';
    elChangePct.className = up ? 'up' : 'down';
    elChangeAbs.textContent = (up ? '+' : '') + fmtPrice(changeAbs);
    elChangeAbs.className = up ? 'up' : 'down';

    setStat(elHigh, (t && t.high != null) ? t.high : fb.high, !(t && t.high != null), fmtPrice);
    setStat(elLow, (t && t.low != null) ? t.low : fb.low, !(t && t.low != null), fmtPrice);
    setStat(elVol, (t && t.volume != null) ? t.volume : fb.volume, !(t && t.volume != null), formatVolume);
  }

  window.updateSymbolHeader = function (candles, symbol) {
    if (!candles || !candles.length) return;
    if (symbol !== _tickerSymbol) { _tickerStats = null; _tickerSymbol = symbol; }
    _lastCandles = candles;
    render(candles, symbol);
  };

  // stats: {changePct, changeAbs, high, low, volume} -- any field may be
  // undefined (e.g. Finnhub stocks never provide volume); undefined fields
  // simply keep falling back to the candle estimate above. Re-renders right
  // away against the last candles seen, rather than waiting for the chart's
  // next poll tick, so a ticker update (every 1-3s) doesn't sit stale for up
  // to 15s between chart polls.
  window.updateSymbolHeaderStats = function (stats) {
    _tickerStats = stats;
    if (_lastCandles) render(_lastCandles, _tickerSymbol);
  };
})();
