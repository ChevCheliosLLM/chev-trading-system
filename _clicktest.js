const { chromium } = require('playwright-core');
const path = require('path');

(async () => {
  const exe = 'C:\\Users\\kevin\\AppData\\Local\\ms-playwright\\chromium-1223\\chrome-win64\\chrome.exe';
  const browser = await chromium.launch({ executablePath: exe, headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE ERR: ' + m.text()); });
  page.on('pageerror', e => errors.push('PAGE ERR: ' + e.message));

  await page.goto('http://localhost:8081/', { waitUntil: 'load' });
  // wait for boot (DOMContentLoaded handler) + a bit
  await page.waitForTimeout(6000);

  // Inject a live BTC trade row and click it
  const result = await page.evaluate(async () => {
    const el = document.getElementById('tradingLogs');
    if (!el) return { ok: false, reason: 'no #tradingLogs' };
    el.innerHTML = `<div class="logRow" data-pair="BTCUSDT" data-type="crypto" data-conf="%7B%7D" data-row="5" data-entry="60000" data-sl="58000" data-tp="64000" data-direction="long" data-ts="2026-07-09 07:00:00" data-tags="" data-status="OPEN">BTC</div>`;
    const row = el.querySelector('.logRow');
    if (!row) return { ok: false, reason: 'row not created' };
    row.click();
    // give the async handler time to run loadChart (Binance) + overlay
    await new Promise(r => setTimeout(r, 4000));
    return {
      ok: true,
      currentSymbol: (typeof currentSymbol !== 'undefined') ? currentSymbol : 'UNDEFINED',
      currentTf: (typeof currentTf !== 'undefined') ? currentTf : 'UNDEFINED',
      activeTrade: (typeof _activeTrade !== 'undefined') ? _activeTrade : 'UNDEFINED',
      priceLines: (typeof candleSeries !== 'undefined' && candleSeries.seriesType)
        ? 'has series' : 'candleSeries?',
      confLineCount: (typeof confPriceLines !== 'undefined') ? confPriceLines.length : 'UNDEFINED',
    };
  });

  console.log('RESULT:', JSON.stringify(result, null, 2));
  console.log('ERRORS:', errors.length ? errors.join('\n') : 'none');
  await browser.close();
})();
