/* ============================================================
   Lightweight Charts main chart instantiation and data feeding.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     MAIN CHART
     ============================================================ */
  const chart = window._chevChart = LightweightCharts.createChart(document.getElementById('chart'), {
    autoSize: true,
    layout: { background: { color: '#131722' }, textColor: '#787b86' },
    grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
    rightPriceScale: { borderColor: '#2a2e39', scaleMargins: { top: 0.08, bottom: 0.25 } },
    timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  const candleSeries = chart.addCandlestickSeries({
    upColor: '#089981', downColor: '#f23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#f23645',
  });
  const volumeSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume' });
  chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });

  /* ---- EMA config: slot → { period, color, active } ---- */
  const _emaConfig = [
    { period: 13, color: '#00bcd4', active: false },
    { period: 21, color: '#ff9800', active: false },
    { period: 55, color: '#e040fb', active: false },
  ];
  const emaSeries = _emaConfig.map(cfg =>
    chart.addLineSeries({ color: cfg.color, lineWidth: 1, title: '', crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false, visible: false })
  );

  /* ---- BB series (3 lines: upper, mid, lower) ---- */
  const bbUpperSeries = chart.addLineSeries({ color: 'rgba(255,165,0,0.65)', lineWidth: 1, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false, visible: false });
  const bbMidSeries   = chart.addLineSeries({ color: 'rgba(255,165,0,0.3)',  lineWidth: 1, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false, visible: false, lineStyle: 1 });
  const bbLowerSeries = chart.addLineSeries({ color: 'rgba(255,165,0,0.65)', lineWidth: 1, crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false, visible: false });

  /* ---- VWAP series ---- */
  const vwapSeries = chart.addLineSeries({ color: 'rgba(139,90,250,0.85)', lineWidth: 1, title: 'VWAP', crosshairMarkerVisible: false, lastValueVisible: true, priceLineVisible: false, visible: false, lineStyle: 0 });

  /* ---- RSI sub-chart: lazy init (created only when first toggled on) ---- */
  let rsiChart = null;
  let rsiLine  = null;
  let _rsiSyncing = false;
  let _rsiCrossX  = null; // X coordinate on main chart canvas from RSI hover

  function _initRsiChart() {
    if (rsiChart) return;
    rsiChart = LightweightCharts.createChart(document.getElementById('rsiChart'), {
      autoSize: true,
      layout: { background: { color: '#131722' }, textColor: '#787b86' },
      grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
      rightPriceScale: { borderColor: '#2a2e39', scaleMargins: { top: 0.05, bottom: 0.05 } },
      timeScale: { borderColor: '#2a2e39', visible: false },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      handleScale: { mouseWheel: false, pinch: false, axisDoubleClickReset: false },
      handleScroll: false,
    });
    rsiLine = rsiChart.addLineSeries({ color: '#9b59b6', lineWidth: 1, lastValueVisible: true, priceLineVisible: false });
    rsiLine.createPriceLine({ price: 70, color: 'rgba(242,54,69,0.5)',   lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: false });
    rsiLine.createPriceLine({ price: 30, color: 'rgba(8,153,129,0.5)',   lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: false });
    rsiLine.createPriceLine({ price: 50, color: 'rgba(120,123,134,0.3)', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: false });
    // Sync RSI crosshair vertical back to main chart canvas
    rsiChart.subscribeCrosshairMove(function(param) {
      if (!param.time) { _rsiCrossX = null; markDirty(); return; }
      try { _rsiCrossX = chart.timeScale().timeToCoordinate(param.time); } catch(e) { _rsiCrossX = null; }
      markDirty();
    });
    // Ensure overlay canvas repaints when RSI chart pans (it syncs from main chart,
    // so this fires on every main-chart pan even if RSI chart's event handlers
    // compete with the main chart's handlers).
    rsiChart.timeScale().subscribeVisibleLogicalRangeChange(markDirty);
  }

  /* Force both charts to have identical right-axis widths so x-coordinates align pixel-perfect */
  function _syncRsiAxisWidth() {
    if (!rsiChart || !_indicatorState.rsi) return;
    try {
      const w = chart.priceScale('right').width();
      if (w > 0) rsiChart.applyOptions({ rightPriceScale: { minimumWidth: w } });
    } catch(e) {}
  }
  window._syncRsiAxisWidth = _syncRsiAxisWidth;

  /* Sync RSI time scale — subtract RSI warmup period so bar indices align with price chart */
  const RSI_WARMUP = 15;
  chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
    if (_rsiSyncing || range === null || !rsiChart || !_indicatorState.rsi) return;
    _rsiSyncing = true;
    try {
      rsiChart.timeScale().setVisibleLogicalRange({
        from: range.from - RSI_WARMUP,
        to:   range.to   - RSI_WARMUP,
      });
    } catch(e) {}
    _rsiSyncing = false;
  });

  /* ---- Indicator state ---- */
  let _indicatorState = { rsi: false, bb: false, vwap: false };

  /* ---- Calculation helpers ---- */
  function calcEMA(candles, period) {
    if (candles.length < period) return [];
    const k = 2 / (period + 1);
    let ema = candles.slice(0, period).reduce((s, c) => s + c.close, 0) / period;
    const result = [{ time: candles[period - 1].time, value: ema }];
    for (let i = period; i < candles.length; i++) {
      ema = candles[i].close * k + ema * (1 - k);
      result.push({ time: candles[i].time, value: ema });
    }
    return result;
  }

  function calcRSI(candles, period = 14) {
    if (candles.length <= period) return [];
    let avgGain = 0, avgLoss = 0;
    for (let i = 1; i <= period; i++) {
      const diff = candles[i].close - candles[i - 1].close;
      if (diff > 0) avgGain += diff; else avgLoss -= diff;
    }
    avgGain /= period; avgLoss /= period;
    const result = [];
    for (let i = period + 1; i < candles.length; i++) {
      const diff = candles[i].close - candles[i - 1].close;
      const gain = diff > 0 ? diff : 0;
      const loss = diff < 0 ? -diff : 0;
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
      result.push({ time: candles[i].time, value: 100 - (100 / (1 + rs)) });
    }
    return result;
  }

  function calcBB(candles, period = 20, mult = 2) {
    if (candles.length < period) return { upper: [], mid: [], lower: [] };
    const upper = [], mid = [], lower = [];
    for (let i = period - 1; i < candles.length; i++) {
      const slice = candles.slice(i - period + 1, i + 1);
      const mean  = slice.reduce((s, c) => s + c.close, 0) / period;
      const variance = slice.reduce((s, c) => s + (c.close - mean) ** 2, 0) / period;
      const std  = Math.sqrt(variance);
      const t    = candles[i].time;
      upper.push({ time: t, value: mean + mult * std });
      mid.push(  { time: t, value: mean });
      lower.push({ time: t, value: mean - mult * std });
    }
    return { upper, mid, lower };
  }

  function calcVWAP(candles) {
    const result = [];
    let cumTPV = 0, cumVol = 0, lastDay = null;
    for (const c of candles) {
      const day = Math.floor(c.time / 86400);
      if (day !== lastDay) { cumTPV = 0; cumVol = 0; lastDay = day; }
      const tp  = (c.high + c.low + c.close) / 3;
      const vol = c.volume || 0;
      cumTPV += tp * vol;
      cumVol += vol;
      if (cumVol > 0) result.push({ time: c.time, value: cumTPV / cumVol });
    }
    return result;
  }

  function updateIndicators() {
    if (!currentCandles.length) return;
    if (!_emaConfig.some(c => c.active) && !_indicatorState.rsi && !_indicatorState.bb && !_indicatorState.vwap) return;
    _emaConfig.forEach((cfg, i) => {
      if (cfg.active) emaSeries[i].setData(calcEMA(currentCandles, cfg.period));
    });
    if (_indicatorState.rsi && rsiLine) {
      const rsiData = calcRSI(currentCandles);
      rsiLine.setData(rsiData);
      if (rsiData.length) {
        requestAnimationFrame(() => {
          try {
            const r = chart.timeScale().getVisibleLogicalRange();
            if (r) rsiChart.timeScale().setVisibleLogicalRange({ from: r.from - RSI_WARMUP, to: r.to - RSI_WARMUP });
          } catch(e) {}
          _syncRsiAxisWidth();
        });
      }
    }
    if (_indicatorState.bb) {
      const bb = calcBB(currentCandles);
      bbUpperSeries.setData(bb.upper);
      bbMidSeries.setData(bb.mid);
      bbLowerSeries.setData(bb.lower);
    }
    if (_indicatorState.vwap) {
      vwapSeries.setData(calcVWAP(currentCandles));
    }
  }

  new ResizeObserver(() => syncCanvasSize()).observe(chartWrap);

  function volColor(c) { return c.close >= c.open ? 'rgba(8,153,129,0.5)' : 'rgba(242,54,69,0.5)'; }
  function formatVolume(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K';
    return v.toFixed(2);
  }

  /* ---------- Data fetching: crypto (Binance) vs forex/stock (Finnhub) ---------- */
  const FINNHUB_RES_MAP = { '15m': '15', '30m': '30', '1h': '60', '4h': '240', '1d': 'D' };

  function toFinnhubSymbol(symbol, type) {
    if (type === 'forex') return 'OANDA:' + symbol.replace('/', '_');
    return symbol;
  }

  async function fetchCandles(symbol, interval, type, limit = 1500) {
    if (type === 'crypto') {
      const url = `https://api.binance.com/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${limit}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Binance error ${res.status}`);
      const raw = await res.json();
      return raw.map(c => ({
        time: Math.floor(c[0] / 1000), open: parseFloat(c[1]), high: parseFloat(c[2]), low: parseFloat(c[3]), close: parseFloat(c[4]), volume: parseFloat(c[5]),
      }));
    } else if (type === 'forex') {
      const res  = await _apiFetch(`/api/forex_candles?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`);
      if (!res.ok) throw new Error(`Forex candles ${res.status}`);
      const data = await res.json();
      if (!Array.isArray(data)) throw new Error(data.error || 'No forex data');
      return data;
    } else {
      const key = getFinnhubKey();
      const fhSymbol = toFinnhubSymbol(symbol, type);
      const resolution = FINNHUB_RES_MAP[interval] || '60';
      const to = Math.floor(Date.now() / 1000);
      const secondsPerCandle = { '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400 }[interval] || 3600;
      const from = to - secondsPerCandle * limit;
      const url = `https://finnhub.io/api/v1/stock/candle?symbol=${encodeURIComponent(fhSymbol)}&resolution=${resolution}&from=${from}&to=${to}&token=${key}`;
      const res = await fetch(url);
      const data = await res.json();
      if (data.s !== 'ok') throw new Error('No stock data from Finnhub');
      const out = [];
      for (let i = 0; i < data.t.length; i++) {
        out.push({ time: data.t[i], open: data.o[i], high: data.h[i], low: data.l[i], close: data.c[i], volume: data.v[i] || 0 });
      }
      return out;
    }
  }

  function updatePriceLabel(candles) {
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2] || last;
    const up = last.close >= prev.close;
    priceLabel.textContent = last.close.toLocaleString(undefined, { maximumFractionDigits: 6 });
    priceLabel.className = 'price ' + (up ? 'up' : 'down');
    renderOhlc(last, up);
  }

  function renderOhlc(c, up) {
    const cls = up ? 'up' : 'down';
    ohlcInfo.innerHTML =
      `<span>O <span class="val">${c.open.toFixed(4)}</span></span>` +
      `<span>H <span class="val">${c.high.toFixed(4)}</span></span>` +
      `<span>L <span class="val">${c.low.toFixed(4)}</span></span>` +
      `<span>C <span class="val ${cls}">${c.close.toFixed(4)}</span></span>` +
      `<span>Vol <span class="val">${formatVolume(c.volume || 0)}</span></span>`;
  }

  let currentCandles = [];

  function _priceFormat(symbol, type) {
    if (type === 'forex') {
      const isJpy = symbol.toUpperCase().includes('JPY');
      return isJpy
        ? { type: 'price', precision: 3, minMove: 0.001 }
        : { type: 'price', precision: 5, minMove: 0.00001 };
    }
    if (type === 'crypto') return { type: 'price', precision: 4, minMove: 0.0001 };
    return { type: 'price', precision: 2, minMove: 0.01 };
  }

  async function loadChart(symbol, interval, type) {
    statusEl.textContent = 'Loading...';
    symLabel.textContent = symbol;
    try {
      candleSeries.applyOptions({ priceFormat: _priceFormat(symbol, type) });
      const candles = await fetchCandles(symbol, interval, type);
      currentCandles = candles;
      candleSeries.setData([]);
      volumeSeries.setData([]);
      candleSeries.setData(candles);
      volumeSeries.setData(candles.map(c => ({ time: c.time, value: c.volume, color: volColor(c) })));
      updatePriceLabel(candles);
      chart.priceScale('right').applyOptions({ autoScale: true });
      chart.timeScale().fitContent();
      setTimeout(() => chart.priceScale('right').applyOptions({ autoScale: false }), 100);
      statusEl.textContent = `${symbol} · ${interval} · live`;
      updateIndicators();
      _updateChatContext();
    } catch (err) {
      currentCandles = [];
      candleSeries.setData([]);
      volumeSeries.setData([]);
      statusEl.textContent = `${symbol} — ${err.message}`;
      console.error('[loadChart]', symbol, err);
    }
  }

  async function pollLatestCandle() {
    try {
      const candles = await fetchCandles(currentSymbol, currentTf, currentType, 2);
      const latest = candles[candles.length - 1];
      candleSeries.update(latest);
      volumeSeries.update({ time: latest.time, value: latest.volume, color: volColor(latest) });
      updatePriceLabel(candles);
      markDirty();
      // Keep currentCandles fresh for magnet
      if (currentCandles.length) {
        if (currentCandles[currentCandles.length - 1].time === latest.time) {
          currentCandles[currentCandles.length - 1] = latest;
        } else {
          currentCandles.push(latest);
        }
        updateIndicators();
      }
    } catch (err) {}
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => pollLatestCandle().then(() => _markFresh('chart')), currentType === 'crypto' ? 5000 : 15000);
  }

  const CANDLE_DURATION_MS = { '15m': 900000, '30m': 1800000, '1h': 3600000, '4h': 14400000, '1d': 86400000 };
  const candleCountdownEl = document.getElementById('candleCountdown');
  function updateCandleCountdown() {
    const ms = CANDLE_DURATION_MS[currentTf];
    if (!ms) { candleCountdownEl.textContent = ''; return; }
    let rem = Math.ceil((ms - (Date.now() % ms)) / 1000);
    const h = Math.floor(rem / 3600); rem -= h * 3600;
    const m = Math.floor(rem / 60);   rem -= m * 60;
    const s = rem;
    const pad = n => String(n).padStart(2, '0');
    candleCountdownEl.textContent = h > 0
      ? `candle closes in ${h}:${pad(m)}:${pad(s)}`
      : `candle closes in ${pad(m)}:${pad(s)}`;
  }
  updateCandleCountdown();
  setInterval(updateCandleCountdown, 1000);

  chart.subscribeCrosshairMove(param => {
    if (!param.time) {
      if (rsiChart) rsiChart.clearCrosshairPosition();
      return;
    }
    const candleData = param.seriesData.get(candleSeries);
    const volData = param.seriesData.get(volumeSeries);
    if (!candleData) return;
    renderOhlc({ ...candleData, volume: volData ? volData.value : 0 }, candleData.close >= candleData.open);
    // Sync crosshair to RSI panel — same candle, same timestamp
    if (rsiChart && rsiLine) {
      const rv = _getRsiValAt(param.time);
      if (rv !== null) rsiChart.setCrosshairPosition(rv, param.time, rsiLine);
      else rsiChart.clearCrosshairPosition();
    }
  });

