/* ============================================================
   Global configuration, constants, and mutable application state (the 76 let/const globals). Loaded first.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     CONFIG
     ============================================================ */
  function getStoredKey(storageKey, promptText) {
    let val = localStorage.getItem(storageKey);
    if (!val) {
      val = prompt(promptText);
      if (val) localStorage.setItem(storageKey, val.trim());
    }
    return val;
  }
  if (!localStorage.getItem('chevFinnhubKey')) {
    localStorage.setItem('chevFinnhubKey', 'd8u6i89r01qinhug59b0d8u6i89r01qinhug59bg');
  }
  function getFinnhubKey() { return localStorage.getItem('chevFinnhubKey'); }
  if (!localStorage.getItem('chevOandaKey')) {
    localStorage.setItem('chevOandaKey', 'aff64f8cfa51d031b0537292a1a0e682-62e4e27a6ddb604f85684c4d9ccdafc6');
  }
  function getOandaKey() { return localStorage.getItem('chevOandaKey'); }
  const OANDA_BASE = 'https://api-fxtrade.oanda.com/v3';
  const OANDA_GRAN = { '15m': 'M15', '30m': 'M30', '1h': 'H1', '4h': 'H4', '1d': 'D' };

  // ── Remote access ──────────────────────────────────────────────────────────
  const FIREBASE_BASE = 'https://chev-monitor-default-rtdb.firebaseio.com';
  // API_BASE: empty = relative paths (works when served by Dexter locally).
  // On GitHub Pages this gets overwritten with the ngrok URL from Firebase config.
  let API_BASE = '';
  // When the page is served FROM Dexter's own origin — the local machine, or a
  // Cloudflare quick-tunnel that forwards straight to Dexter's port 8080 — the
  // /api routes live on this same origin, so relative paths just work. This also
  // makes remote access self-healing across cloudflared restarts: quick-tunnels
  // mint a fresh random subdomain each launch and Dexter can no longer publish
  // that to Firebase the way it did with ngrok's localhost:4040 API. For other
  // hosts (e.g. GitHub Pages) we still need the absolute backend URL from Firebase.
  async function _resolveApiBase() {
    const host = location.hostname;
    if (['localhost', '127.0.0.1'].includes(host)) return; // relative — Dexter local
    if (host.endsWith('trycloudflare.com')) return;        // relative — Dexter tunnel
    try {
      const r = await fetch(`${FIREBASE_BASE}/config/api_url.json`);
      if (r.ok) { const url = await r.json(); if (url) { API_BASE = url; return; } }
    } catch(e) {}
    console.warn('[Chev] No backend URL configured for this host; API calls may fail.');
  }
  // PHASE 9: shared-secret auth. One passphrase, entered once per device, sent on
  // every request via X-Chev-Key. Server only enforces it on mutating routes, so
  // sending it on read-only calls too is harmless.
  function getDashboardKey() {
    let k = localStorage.getItem('chevDashboardKey');
    if (!k) {
      k = window.prompt('Enter dashboard key:') || '';
      if (k) localStorage.setItem('chevDashboardKey', k);
    }
    return k;
  }
  // Wrapper: adds ngrok bypass header when going through a tunnel, and the dashboard
  // auth key on every call. A 401/403 clears the stored key so the next attempt
  // re-prompts, and surfaces a visible note -- never a silent failure.
  function _apiFetch(path, opts = {}) {
    const headers = { 'X-Chev-Key': getDashboardKey(), ...(opts.headers || {}) };
    if (API_BASE) headers['ngrok-skip-browser-warning'] = 'true';
    opts.headers = headers;
    return fetch(API_BASE + path, opts).then(res => {
      if (res.status === 401 || res.status === 403) {
        localStorage.removeItem('chevDashboardKey');
        if (window.showNotification) {
          showNotification('Dashboard key rejected', 'Cleared — try the action again to re-enter it.', 'error', '', 6000);
        }
      }
      return res;
    });
  }
  localStorage.setItem('chevOpenWebUIKey', 'sk-6f2e0053e0874980b76d019d6e619a01');
  localStorage.setItem('chevOpenWebUIUrl', 'http://localhost:3000/api/chat/completions');
  localStorage.setItem('chevModelId', 'chev-chelios');
  function getOpenWebUIKey() { return localStorage.getItem('chevOpenWebUIKey'); }
  function getOpenWebUIUrl() { return localStorage.getItem('chevOpenWebUIUrl'); }
  function getChevModelId() { return localStorage.getItem('chevModelId'); }

  /* ============================================================
     STATE
     ============================================================ */
  const chartWrap = document.getElementById('chartWrap');
  const statusEl = document.getElementById('status');
  const symLabel = document.getElementById('symLabel');
  const priceLabel = document.getElementById('priceLabel');
  const priceInfo = document.getElementById('priceInfo');
  const ohlcInfo = document.getElementById('ohlcInfo');
  const chevBanner = document.getElementById('chevActiveIdeaBanner');

  const STOCK_DOMAINS = {
    NVDA: 'nvidia.com', TSLA: 'tesla.com', AMZN: 'amazon.com', AMD: 'amd.com',
    META: 'meta.com', MSFT: 'microsoft.com', AAPL: 'apple.com', GOOGL: 'google.com',
    NFLX: 'netflix.com', BABA: 'alibaba.com', MRVL: 'marvell.com',
    NOW: 'servicenow.com', HOOD: 'robinhood.com', MARA: 'marathondh.com',
    MRNA: 'modernatx.com', BAC: 'bankofamerica.com', GME: 'gamestop.com',
    AMC: 'amctheatres.com', SQQQ: 'proshares.com', QQQ: 'invesco.com',
    ASTS: 'ast-science.com', FCEL: 'fuelcellenergy.com', POET: 'poet-technologies.com',
    TE: 'te.com', NVTS: 'navitassemi.com',
  };
  const FOREX_FLAGS = {
    'EUR/USD': '🇪🇺', 'GBP/USD': '🇬🇧', 'USD/JPY': '🇯🇵', 'AUD/USD': '🇦🇺',
    'USD/CAD': '🇨🇦', 'USD/CHF': '🇨🇭', 'NZD/USD': '🇳🇿', 'USD/CNH': '🇨🇳',
  };
  const TAG_NAMES = { SR: 'Support/Resistance', FB: 'Fibonacci', GP: 'Golden Pocket', RS: 'RSI Divergence', VW: 'VWAP', EM: 'EMA', VP: 'Volume Profile', CP: 'Candle Pattern', TR: 'Triangle' };
  const SHORT_TAG = {
    SR:'SR', FIB:'FIB', FB:'FIB', GP:'Golden Pocket', RS:'RSI', RSI:'RSI',
    EMA:'EMA', EM:'EMA', BB:'BB', VW:'VWAP', VWAP:'VWAP', VP:'Vol Profile',
    MS:'MTF', CP:'Pattern', TR:'Triangle', DV:'RSI Div',
    'GOLDEN POCKET':'Golden Pocket', 'VOLUME PROFILE':'Vol Profile',
  };
  // TF suffixes recognised in tag parts — e.g. the "4H" in ema_13_4h
  const _TF_PAT = /^(1M|3M|5M|15M|30M|1H|2H|4H|6H|8H|12H|1D|3D|1W|1MO)$/i;
  function _tagLabel(t, ideaTf) {
    const up = t.toUpperCase();
    if (SHORT_TAG[up]) return SHORT_TAG[up];
    const parts = up.split('_');
    const base = SHORT_TAG[parts[0]];
    if (base && parts.length > 1) {
      const suffix = parts.slice(1);
      const hasTf  = _TF_PAT.test(suffix[suffix.length - 1]);
      // For calculated indicators (EMA/BB) append idea TF when not already in the tag
      if (!hasTf && ideaTf && (base === 'EMA' || base === 'BB')) {
        return `${base} ${suffix.join(' ')} ${ideaTf.toUpperCase()}`;
      }
      return `${base} ${suffix.join(' ')}`;
    }
    return t;
  }
  function _getPillState(row) {
    try { return JSON.parse(localStorage.getItem('ideaPills_' + row) || '{}'); } catch(e) { return {}; }
  }
  function _setPillState(row, tag, on) {
    const s = _getPillState(row); s[tag] = on;
    localStorage.setItem('ideaPills_' + row, JSON.stringify(s));
  }
  // Pill-aware helpers — return true when not in idea mode OR when that pill is ON
  function _ipE()  { const ps = activeIdeaRow != null ? _getPillState(activeIdeaRow) : null; return !ps || !!ps['_E'];  }
  function _ipSL() { const ps = activeIdeaRow != null ? _getPillState(activeIdeaRow) : null; return !ps || !!ps['_SL']; }
  function _ipTP() { const ps = activeIdeaRow != null ? _getPillState(activeIdeaRow) : null; return !ps || !!ps['_TP']; }

  const DEFAULT_GROUPS = {
    crypto: [
      { symbol: 'BTCUSDT',  label: 'BTC',  icon: 'btc',  type: 'crypto' },
      { symbol: 'ETHUSDT',  label: 'ETH',  icon: 'eth',  type: 'crypto' },
      { symbol: 'SOLUSDT',  label: 'SOL',  icon: 'sol',  type: 'crypto' },
      { symbol: 'XRPUSDT',  label: 'XRP',  icon: 'xrp',  type: 'crypto' },
      { symbol: 'ADAUSDT',  label: 'ADA',  icon: 'ada',  type: 'crypto' },
      { symbol: 'XLMUSDT',  label: 'XLM',  icon: 'xlm',  type: 'crypto' },
      { symbol: 'BNBUSDT',  label: 'BNB',  icon: 'bnb',  type: 'crypto' },
      { symbol: 'TRXUSDT',  label: 'TRX',  icon: 'trx',  type: 'crypto' },
      { symbol: 'DOGEUSDT', label: 'DOGE', icon: 'doge', type: 'crypto' },
      { symbol: 'LINKUSDT', label: 'LINK', icon: 'link', type: 'crypto' },
      { symbol: 'SUIUSDT',  label: 'SUI',  icon: 'sui',  type: 'crypto' },
      { symbol: 'AVAXUSDT', label: 'AVAX', icon: 'avax', type: 'crypto' },
      { symbol: 'NEARUSDT', label: 'NEAR', icon: 'near', type: 'crypto' },
      { symbol: 'DOTUSDT',  label: 'DOT',  icon: 'dot',  type: 'crypto' },
      { symbol: 'AAVEUSDT', label: 'AAVE', icon: 'aave', type: 'crypto' },
      { symbol: 'PEPEUSDT', label: 'PEPE', icon: 'pepe', type: 'crypto' },
      { symbol: 'ZECUSDT',  label: 'ZEC',  icon: 'zec',  type: 'crypto' },
      { symbol: 'UNIUSDT',  label: 'UNI',  icon: 'uni',  type: 'crypto' },
    ],
    forex: [
      { symbol: 'EUR/USD', label: 'EUR/USD', type: 'forex' },
      { symbol: 'GBP/USD', label: 'GBP/USD', type: 'forex' },
      { symbol: 'USD/JPY', label: 'USD/JPY', type: 'forex' },
      { symbol: 'AUD/USD', label: 'AUD/USD', type: 'forex' },
      { symbol: 'USD/CAD', label: 'USD/CAD', type: 'forex' },
      { symbol: 'USD/CHF', label: 'USD/CHF', type: 'forex' },
      { symbol: 'NZD/USD', label: 'NZD/USD', type: 'forex' },
      { symbol: 'USD/CNH', label: 'USD/CNH', type: 'forex' },
    ],
    stocks: [
      { symbol: 'NVDA',  label: 'NVDA',  type: 'stock' },
      { symbol: 'TSLA',  label: 'TSLA',  type: 'stock' },
      { symbol: 'AMZN',  label: 'AMZN',  type: 'stock' },
      { symbol: 'AMD',   label: 'AMD',   type: 'stock' },
      { symbol: 'META',  label: 'META',  type: 'stock' },
      { symbol: 'MSFT',  label: 'MSFT',  type: 'stock' },
      { symbol: 'AAPL',  label: 'AAPL',  type: 'stock' },
      { symbol: 'GOOGL', label: 'GOOGL', type: 'stock' },
      { symbol: 'NFLX',  label: 'NFLX',  type: 'stock' },
      { symbol: 'BABA',  label: 'BABA',  type: 'stock' },
      { symbol: 'MRVL',  label: 'MRVL',  type: 'stock' },
      { symbol: 'NOW',   label: 'NOW',   type: 'stock' },
      { symbol: 'HOOD',  label: 'HOOD',  type: 'stock' },
      { symbol: 'MARA',  label: 'MARA',  type: 'stock' },
      { symbol: 'MRNA',  label: 'MRNA',  type: 'stock' },
      { symbol: 'BAC',   label: 'BAC',   type: 'stock' },
      { symbol: 'GME',   label: 'GME',   type: 'stock' },
      { symbol: 'AMC',   label: 'AMC',   type: 'stock' },
      { symbol: 'SQQQ',  label: 'SQQQ',  type: 'stock' },
      { symbol: 'QQQ',   label: 'QQQ',   type: 'stock' },
      { symbol: 'ASTS',  label: 'ASTS',  type: 'stock' },
      { symbol: 'FCEL',  label: 'FCEL',  type: 'stock' },
      { symbol: 'POET',  label: 'POET',  type: 'stock' },
      { symbol: 'TE',    label: 'TE',    type: 'stock' },
      { symbol: 'NVTS',  label: 'NVTS',  type: 'stock' },
    ],
  };

  const GROUPS_FB_URL = `${FIREBASE_BASE}/watchlist/groups.json`;
  const GROUPS_VERSION = '3';  // bump when DEFAULT_GROUPS changes — forces reset on old clients
  function loadGroups() {
    const saved = localStorage.getItem('chevWatchlistGroups');
    const ver   = localStorage.getItem('chevWatchlistGroupsVer');
    if (saved && ver === GROUPS_VERSION) { try { return JSON.parse(saved); } catch(e) {} }
    return JSON.parse(JSON.stringify(DEFAULT_GROUPS));
  }
  function saveGroups() {
    const payload = JSON.stringify(groups);
    localStorage.setItem('chevWatchlistGroups', payload);
    localStorage.setItem('chevWatchlistGroupsVer', GROUPS_VERSION);
    fetch(GROUPS_FB_URL, { method:'PUT', body:payload, headers:{'Content-Type':'application/json'} }).catch(()=>{});
  }
  // On load: pull watchlist from Firebase (friend's additions show up automatically)
  fetch(GROUPS_FB_URL).then(r => r.ok ? r.json() : null).then(d => {
    if (d && typeof d === 'object') {
      groups = d;
      localStorage.setItem('chevWatchlistGroups', JSON.stringify(d));
      buildWatchlistRows();
      refreshWatchlistPrices();
      if (typeof _renderRadar === 'function') _renderRadar();
    }
  }).catch(()=>{});

  let groups = loadGroups();
  let currentGroup = 'crypto';
  let currentSymbol = groups.crypto[2].symbol; // SOL default
  let currentType = 'crypto';
  let currentTf = '1h';
  // Expose for isolated script (engine context bar, etc.)
  Object.defineProperty(window, '_currentSymbol', { get: () => currentSymbol });
  Object.defineProperty(window, '_currentTf',     { get: () => currentTf });
  let pollTimer = null;
  let chevToolsOn = true;
  let chevPriceLines = [];
  let activeIdeaRow = null;

  // Shared mutable globals for the watchlist sparklines / proximity-alert subsystem
  // (declared here, which loads FIRST, so ui/watchlist.js can reference them during
  // its boot refresh without hitting a Temporal Dead Zone — they were previously
  // declared in ui/sparklines.js which loads AFTER watchlist.js).
  var _sparkBuffers = {};
  var _alertFired = {};
  var SPARK_MAX = 40;
  var PROXIMITY_PCT = 0.005; // 0.5% of entry


