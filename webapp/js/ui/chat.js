/* ============================================================
   Chev chat panel: send/receive, history, rendering.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     CHEV CHAT
     ============================================================ */
  const rightPanel = document.getElementById('rightPanel');
  const rightToggle = document.getElementById('rightToggle');
  const chatListItemsEl = document.getElementById('chatListItems');
  const chatMessagesEl = document.getElementById('chatMessages');
  const chatInput = document.getElementById('chatInput');
  const chatSendBtn = document.getElementById('chatSendBtn');
  const newChatBtn = document.getElementById('newChatBtn');

  function loadChats() {
    try {
      const saved = localStorage.getItem('chevChats');
      return saved ? JSON.parse(saved) : {};
    } catch (e) {
      console.warn('[Chat] localStorage parse failed, resetting:', e);
      localStorage.removeItem('chevChats');
      return {};
    }
  }
  function saveChats() { localStorage.setItem('chevChats', JSON.stringify(chats)); }

  let chats = loadChats();
  let activeChatId = localStorage.getItem('chevActiveChatId');
  if (!activeChatId || !chats[activeChatId]) {
    activeChatId = 'chat-' + Date.now();
    chats[activeChatId] = { title: 'New chat', messages: [] };
    saveChats();
  }

  // rightToggle handled by isolated script below

  function renderChatList() {
    chatListItemsEl.innerHTML = Object.entries(chats).map(([id, c]) => `
      <div class="chatListItem${id === activeChatId ? ' active' : ''}" data-id="${id}">
        <span class="chatListTitle">${c.title}</span>
        <span class="chatListDel" data-del="${id}" title="Delete">✕</span>
      </div>
    `).join('');
    chatListItemsEl.querySelectorAll('.chatListDel').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const delId = btn.dataset.del;
        delete chats[delId];
        if (activeChatId === delId) {
          const remaining = Object.keys(chats);
          if (remaining.length === 0) {
            activeChatId = 'chat-' + Date.now();
            chats[activeChatId] = { title: 'New chat', messages: [] };
          } else {
            activeChatId = remaining[remaining.length - 1];
          }
          localStorage.setItem('chevActiveChatId', activeChatId);
        }
        saveChats();
        renderChatList();
        renderMessages();
      });
    });
    chatListItemsEl.querySelectorAll('.chatListItem').forEach(item => {
      item.addEventListener('click', () => {
        activeChatId = item.dataset.id;
        localStorage.setItem('chevActiveChatId', activeChatId);
        renderChatList();
        renderMessages();
      });
    });
  }

  function renderMessages() {
    const msgs = chats[activeChatId].messages;
    chatMessagesEl.innerHTML = msgs.filter(m => m.role !== 'system').map(m => {
      const isUser = m.role === 'user';
      const who = isUser ? 'You' : 'CHEV ›';
      // Convert markdown-style bold (**text**) and code (`text`) to HTML
      const html = (m.content || '')
        .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\n/g, '<br>');
      return `<div class="msg ${isUser ? 'user' : 'assistant'}">
        <div class="who">${who}</div>
        <div class="bubble">${html}</div>
      </div>`;
    }).join('');
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  }

  newChatBtn.addEventListener('click', () => {
    const id = 'chat-' + Date.now();
    chats[id] = { title: 'New chat', messages: [] };
    activeChatId = id;
    localStorage.setItem('chevActiveChatId', id);
    saveChats();
    renderChatList();
    renderMessages();
  });

  // ---- Shared SR detection (same algorithm used by chart auto-draw AND Chev context) ----
  function _getSRTouches(candles) {
    const W = 3, THRESH = 0.3, CWIN = 8;
    const res = [], sup = [];
    for (let i = W; i < candles.length - W; i++) {
      const h = candles[i].high, l = candles[i].low;
      let isH = true, isL = true;
      for (let j = 1; j <= W; j++) {
        if (candles[i-j].high >= h || candles[i+j].high >= h) isH = false;
        if (candles[i-j].low  <= l || candles[i+j].low  <= l) isL = false;
      }
      if (isH) {
        const fut = candles.slice(i+1, Math.min(i+1+CWIN, candles.length));
        if (fut.length && (h - Math.min(...fut.map(c=>c.low))) / h * 100 >= THRESH)
          res.push(h);
      }
      if (isL) {
        const fut = candles.slice(i+1, Math.min(i+1+CWIN, candles.length));
        if (fut.length && (Math.max(...fut.map(c=>c.high)) - l) / l * 100 >= THRESH)
          sup.push(l);
      }
    }
    return { res, sup };
  }

  function _buildSRLevels(prices, minT=3, tolPct=0.5) {
    if (!prices.length) return [];
    const sorted = [...prices].sort((a,b)=>a-b);
    const clusters = [[sorted[0]]];
    for (let i = 1; i < sorted.length; i++) {
      const last = clusters[clusters.length-1];
      if (Math.abs(sorted[i]-last[last.length-1])/last[last.length-1]*100 <= tolPct)
        last.push(sorted[i]);
      else clusters.push([sorted[i]]);
    }
    return clusters.filter(c=>c.length>=minT)
      .map(c=>({price:c.reduce((s,v)=>s+v,0)/c.length, touches:c.length}));
  }

  function _pushZonesToDrawings(tag, zones, tfSec) {
    for (let i = drawings.length-1; i >= 0; i--) {
      if (drawings[i][tag]) drawings.splice(i, 1);
    }
    const t1 = currentCandles[0].time;
    const t2 = currentCandles[currentCandles.length-1].time + tfSec * 300;
    zones.forEach(z => drawings.push({...z, [tag]:true, visible:true, time1:t1, time2:t2}));
    saveDrawings();
    if (typeof redrawAll === 'function') redrawAll();
    updateObjTree();
  }

  let _toastTimer = null;
  function _showToast(html, durationMs = 7000) {
    const el = document.getElementById('analysisToast');
    el.innerHTML = html;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    // durationMs <= 0 means persistent — caller (ATR's checkbox-controlled readout,
    // 2026-07-05) is responsible for hiding it explicitly. Every other caller passes
    // a positive duration and is unaffected.
    if (durationMs > 0) {
      _toastTimer = setTimeout(() => el.classList.remove('show'), durationMs);
    }
  }

  function showNotification(title, msg='', type='info', iconFile='', durationMs=5000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const iconHtml = iconFile
      ? `<img src="emoji/${iconFile}" class="tIcon" alt="">`
      : `<span class="tIcon" style="font-size:16px">${{success:'✓',warning:'⚠',error:'✕',info:'ℹ'}[type]||'ℹ'}</span>`;
    const el = document.createElement('div');
    el.className = `gToast ${type}`;
    el.innerHTML = `${iconHtml}<div class="tBody"><div class="tTitle">${title}</div>${msg?`<div class="tMsg">${msg}</div>`:''}</div>`;
    el.addEventListener('click', () => dismiss());
    container.appendChild(el);
    const dismiss = () => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 220);
    };
    setTimeout(dismiss, durationMs);
  }
  window.showNotification = showNotification;

  // SR — toggle off if already showing, otherwise fetch and draw
  async function drawSRZones() {
    if (!currentCandles.length) return;
    // Target the VISIBLE Layers-panel button/card, not the permanently-hidden legacy
    // topbar twin — updating the hidden one gave zero visual feedback and never
    // disabled the button users actually click, which is why rapid clicks used to
    // fire overlapping duplicate requests (fixed 2026-07-05 across all Arsenal tools).
    const btn  = document.getElementById('lyrSrBtn');
    const card = document.getElementById('lyrSrCard');
    const vis  = document.getElementById('lyrSrVis');
    // Toggle off
    if (drawings.some(d => d._sr)) {
      for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._sr) drawings.splice(i,1); }
      saveDrawings(); redrawAll(); updateObjTree();
      btn.textContent = 'S/R'; card.classList.remove('active');
      vis.checked = false; vis.disabled = true;
      return;
    }
    btn.textContent = 'S/R…'; btn.disabled = true;
    const tfSec  = {'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400}[currentTf]||3600;
    const _atrCandles = currentCandles.slice(-14);
    const _atr = _atrCandles.reduce((s,c) => s + (c.high - c.low), 0) / _atrCandles.length;
    const halfZone = _atr * 0.10;
    const curPrice = currentCandles[currentCandles.length-1].close;

    try {
      const r = await _apiFetch(`/api/analysis/sr?symbol=${encodeURIComponent(currentSymbol)}&tf=${currentTf}`);
      if (!r.ok) throw new Error(`Dexter ${r.status} — restart Dexter if this persists`);
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      const zones = [];
      // Sort nearest to price first, take top 3 per side
      const res = (d.resistance||[]).filter(z=>z.price>curPrice).sort((a,b)=>a.price-b.price).slice(0,3);
      const sup = (d.support||[]).filter(z=>z.price<curPrice).sort((a,b)=>b.price-a.price).slice(0,3);
      res.forEach(z => {
        const isA = z.tier==='A';
        zones.push({type:'rect', color:'#f23645', fillOpacity:isA?0.08:0.04,
          lineWidth:isA?0.75:0.4, text:`R ×${z.instances}${isA?' [A]':' [B]'}`,
          fontSize:9, labelPos:'top-left', price1:z.price+halfZone, price2:z.price-halfZone});
      });
      sup.forEach(z => {
        const isA = z.tier==='A';
        zones.push({type:'rect', color:'#089981', fillOpacity:isA?0.08:0.04,
          lineWidth:isA?0.75:0.4, text:`S ×${z.instances}${isA?' [A]':' [B]'}`,
          fontSize:9, labelPos:'top-left', price1:z.price+halfZone, price2:z.price-halfZone});
      });
      _pushZonesToDrawings('_sr', zones, tfSec);
      const total = res.length + sup.length;
      btn.textContent = total ? `S/R (${total})` : 'S/R';
      card.classList.add('active');
      vis.checked = true; vis.disabled = false;
    } catch(e) {
      // JS fallback
      const { res, sup } = _getSRTouches(currentCandles);
      const resLvls = _buildSRLevels(res).filter(z=>z.price>curPrice).sort((a,b)=>a.price-b.price).slice(0,3);
      const supLvls = _buildSRLevels(sup).filter(z=>z.price<curPrice).sort((a,b)=>b.price-a.price).slice(0,3);
      const zones = [];
      resLvls.forEach(z => zones.push({type:'rect',color:'#f23645',fillOpacity:0.05,lineWidth:0.5,text:`R ×${z.touches}`,fontSize:9,labelPos:'top-left',price1:z.price+halfZone,price2:z.price-halfZone}));
      supLvls.forEach(z => zones.push({type:'rect',color:'#089981',fillOpacity:0.05,lineWidth:0.5,text:`S ×${z.touches}`,fontSize:9,labelPos:'top-left',price1:z.price+halfZone,price2:z.price-halfZone}));
      _pushZonesToDrawings('_sr', zones, tfSec);
      const total = resLvls.length + supLvls.length;
      btn.textContent = total ? `S/R (${total})` : 'S/R';
      card.classList.add('active');
      vis.checked = true; vis.disabled = false;
    } finally {
      btn.disabled = false;
    }
  }

  // Reset the VP button/card/checkbox/auto-refresh timer to their off state, without
  // touching drawings[] itself — called on manual toggle-off AND whenever we load a
  // (possibly different) symbol's drawing set, since VP is a live, on-demand overlay
  // that should never silently carry over from whatever pair you were looking at
  // before (see loadDrawings in drawing.js). Also resets the popup's TF checkboxes
  // back to the 1h-only default, mirroring Fib's own reset convention.
  function _deactivateVpUI() {
    clearInterval(_vpAutoTimer);
    if (_vpAutoChk)   _vpAutoChk.checked = false;
    if (_vpAutoLabel) _vpAutoLabel.classList.remove('active');
    const btn  = document.getElementById('lyrVpBtn');
    const card = document.getElementById('lyrVpCard');
    const desc = document.getElementById('lyrVpDesc');
    const vis  = document.getElementById('lyrVpVis');
    if (btn)  btn.textContent = 'VP';
    if (card) card.classList.remove('active');
    if (desc) desc.textContent = 'POC · VAH · VAL';
    if (vis)  { vis.checked = false; vis.disabled = true; }
    const ck15 = document.getElementById('vpCk15m'), ck1h = document.getElementById('vpCk1h'), ck4h = document.getElementById('vpCk4h');
    if (ck15) ck15.checked = false;
    if (ck1h) ck1h.checked = true;
    if (ck4h) ck4h.checked = false;
  }

  // Multi-timeframe Volume Profile — mirrors the Fib stack exactly: a popup with
  // per-TF checkboxes so 15m/1h/4h can be compared side by side instead of having
  // to change the chart's own timeframe and re-click VP to see each one.
  function _clearVpDrawings() {
    for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._vp_stack) drawings.splice(i,1); }
    saveDrawings(); redrawAll(); updateObjTree();
  }

  async function _fetchAndDrawVpStack() {
    const btn  = document.getElementById('lyrVpBtn');
    const card = document.getElementById('lyrVpCard');
    const vis  = document.getElementById('lyrVpVis');
    const show15m = document.getElementById('vpCk15m').checked;
    const show1h  = document.getElementById('vpCk1h').checked;
    const show4h  = document.getElementById('vpCk4h').checked;
    const tfFilter = new Set([...(show15m?['15m']:[]), ...(show1h?['1h']:[]), ...(show4h?['4h']:[])]);
    btn.textContent = 'VP…'; btn.disabled = true;
    try {
      const r = await _apiFetch(`/api/analysis/vp_stack?symbol=${encodeURIComponent(currentSymbol)}`);
      if (!r.ok) throw new Error(`Dexter ${r.status} — restart Dexter if this persists`);
      const data = await r.json();
      if (data.error) throw new Error(data.error);
      for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._vp_stack) drawings.splice(i,1); }
      // poc/vah/val/bin_edges/bin_volumes come straight from Dexter per timeframe — the
      // renderer draws these as-is rather than recomputing its own histogram, so the
      // chart always shows exactly what Dexter (and Chev) used, for every TF shown.
      (data.timeframes||[]).filter(tf => tfFilter.has(tf.tf)).forEach(tf => {
        drawings.push({
          type:'vp', time1:tf.start_t, time2:tf.end_t, color:tf.color, visible:true, _vp_stack:true,
          poc:tf.poc, vah:tf.vah, val:tf.val,
          bin_edges:tf.bin_edges, bin_volumes:tf.bin_volumes,
        });
      });
      saveDrawings(); redrawAll(); updateObjTree();
      const shown = (data.timeframes||[]).filter(tf => tfFilter.has(tf.tf)).length;
      btn.textContent = shown ? `VP (${shown}TF)` : 'VP';
      card.classList.toggle('active', shown > 0);
      vis.checked = shown > 0; vis.disabled = shown === 0;
    } catch(e) {
      _showToast(`<span class="tBear">VP — ${e.message}</span>`, 6000);
      btn.textContent = 'VP'; card.classList.remove('active');
      vis.checked = false; vis.disabled = true;
    } finally {
      btn.disabled = false;
    }
  }

  // Volume Profile — button click: toggle off if already drawn, otherwise force a
  // sane 1h-only default (mirrors drawFibStack's same "don't silently draw nothing"
  // reasoning) and open the popup so more timeframes can be added immediately.
  async function drawVP() {
    if (!currentCandles.length) return;
    if (drawings.some(d => d._vp_stack)) {
      _clearVpDrawings();
      _deactivateVpUI();
      _ctpHide();
      return;
    }
    const anySelected = document.getElementById('vpCk15m').checked ||
                        document.getElementById('vpCk1h').checked  ||
                        document.getElementById('vpCk4h').checked;
    if (!anySelected) document.getElementById('vpCk1h').checked = true;
    await _fetchAndDrawVpStack();
    _ctpShow('vp');
  }

  // Reactive: redraw whenever a TF checkbox changes, same convention as Fib's
  // checkbox listeners — no force-default here, only on the initial button click.
  ['vpCk15m','vpCk1h','vpCk4h'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      _clearVpDrawings();
      const anySelected = document.getElementById('vpCk15m').checked ||
                          document.getElementById('vpCk1h').checked  ||
                          document.getElementById('vpCk4h').checked;
      if (anySelected) {
        _fetchAndDrawVpStack();
      } else {
        document.getElementById('lyrVpBtn').textContent = 'VP';
        document.getElementById('lyrVpCard').classList.remove('active');
        document.getElementById('lyrVpVis').checked = false;
        document.getElementById('lyrVpVis').disabled = true;
      }
    });
  });

  // Auto-run "live" VP toggle — re-fetches whichever timeframes are checked every 5
  // minutes so the Arsenal VP boxes keep tracking the current auction instead of
  // freezing at the moment they were drawn (mirrors engine.js's engChkAuto pattern).
  let _vpAutoTimer = null;
  const _vpAutoChk   = document.getElementById('lyrVpAuto');
  const _vpAutoLabel = document.getElementById('lyrVpAutoLabel');
  if (_vpAutoChk) {
    _vpAutoChk.addEventListener('change', function() {
      clearInterval(_vpAutoTimer);
      if (_vpAutoLabel) _vpAutoLabel.classList.toggle('active', _vpAutoChk.checked);
      if (_vpAutoChk.checked) {
        _vpAutoTimer = setInterval(function() {
          const card = document.getElementById('lyrVpCard');
          if (_vpAutoChk.checked && card && card.classList.contains('active')) _fetchAndDrawVpStack();
        }, 5 * 60 * 1000);
      }
    });
  }

  // ATR — show volatility toast
  // ATR has no persistent chart drawing to hide/show — unlike S/R, VP, and Fib, there's
  // nothing tagged in drawings[] to toggle .visible on. So its checkbox IS the on/off
  // state directly: checked = readout showing (no auto-dismiss timer), unchecked = hidden.
  // Both the main button and the checkbox itself drive this through _setAtrVisible.
  async function _setAtrVisible(show) {
    const btn  = document.getElementById('lyrAtrBtn');
    const card = document.getElementById('lyrAtrCard');
    const vis  = document.getElementById('lyrAtrVis');
    if (!show) {
      document.getElementById('analysisToast').classList.remove('show');
      vis.checked = false; btn.textContent = 'ATR'; card.classList.remove('active');
      return;
    }
    btn.textContent = 'ATR…'; btn.disabled = true;
    try {
      const r = await _apiFetch(`/api/analysis/atr?symbol=${encodeURIComponent(currentSymbol)}&tf=${currentTf}`);
      if (!r.ok) throw new Error(`Dexter ${r.status} — restart Dexter if this persists`);
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      const stateCol = d.state==='volatile'?'tBear':d.state==='quiet'?'tBlue':'tGold';
      _showToast(`
        <div class="tLine tGold"><b>ATR (${currentTf}) — ${currentSymbol}</b></div>
        <div class="tLine">ATR: <b>${d.atr}</b> &nbsp; Avg(20): ${d.avg_atr}</div>
        <div class="tLine">Ratio: ${d.ratio} — <span class="${stateCol}">${d.state.toUpperCase()}</span></div>
        <div class="tLine tGold">SL 1×: ${d.sl_1atr} &nbsp; SL 1.5×: ${d.sl_1_5atr}</div>
      `, 0); // 0 = persistent, stays until the checkbox is unchecked
      btn.textContent = 'ATR';
      vis.checked = true;
      card.classList.add('active');
    } catch(e) {
      _showToast(`<span class="tBear">ATR — ${e.message}</span>`, 6000);
      btn.textContent = 'ATR';
      vis.checked = false;
    } finally {
      btn.disabled = false;
    }
  }

  // Button click: toggle based on current state (matches S/R, VP, Fib, RSI's
  // click-to-toggle pattern). Name kept as showATR — the Layers-panel wiring
  // already binds to this exact name.
  async function showATR() {
    const vis = document.getElementById('lyrAtrVis');
    await _setAtrVisible(!vis.checked);
  }

  // Multi-TF Fibonacci Golden Pocket
  const FIB_LEVELS = [
    {r:0.5,  lb:'0.5'},
    {r:0.786,lb:'0.786'},
  ];

  function _clearFibDrawings() {
    for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._fib_stack) drawings.splice(i,1); }
    saveDrawings(); redrawAll(); updateObjTree();
  }

  async function _fetchAndDrawFib() {
    const btn  = document.getElementById('lyrFibBtn');
    const card = document.getElementById('lyrFibCard');
    const vis  = document.getElementById('lyrFibVis');
    btn.textContent = 'Fib…'; btn.disabled = true;
    const tfSec    = {'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400}[currentTf]||3600;
    const show15m  = document.getElementById('fibCk15m').checked;
    const show1h   = document.getElementById('fibCk1h').checked;
    const show4h   = document.getElementById('fibCk4h').checked;
    const showLvls = document.getElementById('fibCkLevels').checked;
    const tfFilter = new Set([...(show15m?['15m']:[]), ...(show1h?['1h']:[]), ...(show4h?['4h']:[])]);
    try {
      const r = await _apiFetch(`/api/analysis/fib_stack?symbol=${encodeURIComponent(currentSymbol)}`);
      if (!r.ok) throw new Error(`Dexter ${r.status} — restart Dexter if this persists`);
      const data = await r.json();
      if (data.error) throw new Error(data.error);
      const zones = [];
      (data.timeframes||[]).filter(tf => tfFilter.has(tf.tf)).forEach(tf => {
        // GP golden zone fill — no border
        zones.push({type:'rect', _fib_stack:true, color:tf.color, fillOpacity:0.05, lineWidth:0,
          text:`GP ${tf.tf}`, fontSize:8, labelPos:'top-right',
          price1:tf.gp_high, price2:tf.gp_low});
        // GP top line (0.618 level)
        zones.push({type:'hline', _fib_stack:true, color:tf.color, lineWidth:0.8,
          text:`0.618 ${tf.tf}`, fontSize:8, labelPos:'top-left', opacity:0.35, price:tf.gp_high});
        // GP bottom line (0.65 level)
        zones.push({type:'hline', _fib_stack:true, color:tf.color, lineWidth:0.8,
          text:`0.65 ${tf.tf}`, fontSize:8, labelPos:'top-left', opacity:0.35, price:tf.gp_low});
        // Anchor dots at swing high and low
        if (tf.ts_high) zones.push({type:'dot', _fib_stack:true,
          time:tf.ts_high, price:tf.swing_high, radius:3.5, opacity:0.65});
        if (tf.ts_low) zones.push({type:'dot', _fib_stack:true,
          time:tf.ts_low,  price:tf.swing_low,  radius:3.5, opacity:0.65});
        // Optional full level grid
        if (showLvls && tf.swing_high != null && tf.swing_low != null) {
          const sh = tf.swing_high, rng = sh - tf.swing_low;
          FIB_LEVELS.forEach(({r:ratio, lb}) => {
            const lvl = tf.direction==='up' ? sh - ratio*rng : tf.swing_low + ratio*rng;
            const inGP = lvl <= tf.gp_high && lvl >= tf.gp_low;
            if (!inGP) zones.push({type:'hline', _fib_stack:true, color:tf.color,
              lineWidth:0.5, text:`${lb} ${tf.tf}`, fontSize:7, labelPos:'top-left',
              opacity:0.18, price:lvl});
          });
        }
      });
      // Overlap zones
      (data.overlaps||[]).filter(ov => ov.tfs.some(t => tfFilter.has(t))).forEach(ov => {
        zones.push({type:'rect', _fib_stack:true, color:'#ffffff', fillOpacity:0.09, lineWidth:0,
          text:`★ ${ov.tfs.join('+')}`, fontSize:9, labelPos:'top-left',
          price1:ov.high, price2:ov.low});
      });
      _pushZonesToDrawings('_fib_stack', zones, tfSec);
      const shown = (data.timeframes||[]).filter(tf => tfFilter.has(tf.tf)).length;
      const ovCount = (data.overlaps||[]).filter(ov => ov.tfs.some(t => tfFilter.has(t))).length;
      btn.textContent = ovCount ? `Fib ★${ovCount}` : `Fib (${shown}TF)`;
      card.classList.add('active');
      vis.checked = true; vis.disabled = false;
    } catch(e) {
      _showToast(`<span class="tBear">Fib — ${e.message}</span>`, 6000);
      btn.textContent = 'Fib'; card.classList.remove('active');
      vis.checked = false; vis.disabled = true;
    } finally {
      btn.disabled = false;
    }
  }

  async function drawFibStack() {
    if (!currentCandles.length) return;
    const btn  = document.getElementById('lyrFibBtn');
    const card = document.getElementById('lyrFibCard');
    // Toggle off if already drawn
    if (drawings.some(d => d._fib_stack)) {
      _clearFibDrawings();
      btn.textContent = 'Fib'; card.classList.remove('active');
      document.getElementById('lyrFibVis').checked = false;
      document.getElementById('lyrFibVis').disabled = true;
      _ctpHide();
      return;
    }
    // Nothing ticked (e.g. left over from unchecking everything last time) — force
    // 1H rather than silently drawing nothing or making the user hunt for a picker
    // they didn't ask to see (2026-07-05: default is 1H-only, not "show everything").
    const anySelected = document.getElementById('fibCk15m').checked ||
                        document.getElementById('fibCk1h').checked  ||
                        document.getElementById('fibCk4h').checked;
    if (!anySelected) document.getElementById('fibCk1h').checked = true;
    await _fetchAndDrawFib();
    _ctpShow('fib');
  }

  // Reactive: redraw whenever a checkbox changes — deliberately NOT gated on
  // "does fib already have something drawn." That gate used to make re-checking
  // a box after clearing everything a no-op: once all boxes were unchecked the
  // tool looked "off" by this same guard's own definition, so the very next
  // check-the-box-again change event got silently ignored — ticked the box, drew
  // nothing (found 2026-07-05, reported as "check them back and they don't show
  // up"). Every change now just reflects current checkbox state onto the chart,
  // whether that means going from nothing to something, something to something
  // else, or something to nothing. The "always show at least 1H" force-default
  // still applies ONLY to the very first click of the Fib button (drawFibStack),
  // never here — deliberately unchecking everything stays blank until a box is
  // checked again, per Kev 2026-07-05.
  ['fibCk15m','fibCk1h','fibCk4h','fibCkLevels'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      _clearFibDrawings();
      const anySelected = document.getElementById('fibCk15m').checked ||
                          document.getElementById('fibCk1h').checked  ||
                          document.getElementById('fibCk4h').checked;
      if (anySelected) {
        _fetchAndDrawFib();
      } else {
        document.getElementById('lyrFibBtn').textContent = 'Fib';
        document.getElementById('lyrFibCard').classList.remove('active');
        document.getElementById('lyrFibVis').checked = false;
        document.getElementById('lyrFibVis').disabled = true;
      }
    });
  });

  // ── Trendlines — auto-detect support/resistance trendlines from pivots ──
  // Self-contained (no backend): finds swing pivots on the visible candles, fits
  // straight lines through pairs of same-type pivots and keeps CLASSIC trend
  // lines — descending resistance through a series of lower highs, ascending
  // support through a series of higher lows — that price respected (never pierced
  // beyond an ATR-scaled tolerance), then extends each to the latest bar.
  // Resistance lines (through swing highs) draw red, support lines (lows) green. Pushed as normal
  // 'trendline' drawings so they're movable / deletable / hide-able like any
  // hand-drawn trendline, and toggle off by clearing the _tl-tagged drawings.
  function _computeTrendlines(candles, opts) {
    opts = opts || {};
    const N  = Math.min(candles.length, opts.lookback || 260);
    const cs = candles.slice(-N);
    if (cs.length < 30) return [];
    const highs = cs.map(c => c.high), lows = cs.map(c => c.low), times = cs.map(c => c.time);
    const last  = cs.length - 1;

    // ATR-ish tolerance — mean true range of the last 14 bars
    let atr = 0, cnt = 0;
    for (let i = Math.max(1, cs.length - 14); i < cs.length; i++) {
      atr += Math.max(cs[i].high - cs[i].low,
                      Math.abs(cs[i].high - cs[i-1].close),
                      Math.abs(cs[i].low  - cs[i-1].close));
      cnt++;
    }
    atr = cnt ? atr / cnt : (highs[last] * 0.01);
    const tol = atr * (opts.tolAtr || 0.6);

    // Fractal pivots: strict local extreme within ±k bars
    const k = opts.pivotK || 3;
    const pivHi = [], pivLo = [];
    for (let i = k; i < cs.length - k; i++) {
      let isHi = true, isLo = true;
      for (let j = i - k; j <= i + k; j++) {
        if (j === i) continue;
        if (highs[j] >= highs[i]) isHi = false;
        if (lows[j]  <= lows[i])  isLo = false;
      }
      if (isHi) pivHi.push(i);
      if (isLo) pivLo.push(i);
    }

    // Build & validate candidate lines for one side.
    //   'res' → line sits ABOVE prices (no high pierces above line + tol)
    //   'sup' → line sits BELOW prices (no low  pierces below line - tol)
    function build(pivots, vals, kind) {
      if (pivots.length < 2) return [];
      const isRes  = kind === 'res';
      const recent = pivots.slice(-Math.min(pivots.length, opts.recentB || 6));
      const cands  = [];
      recent.forEach(b => {
        pivots.forEach(a => {
          if (a >= b || (b - a) < (opts.minSpan || 8)) return;
          const slope = (vals[b] - vals[a]) / (b - a);
          // Classic trend line direction: resistance must ride a series of
          // DESCENDING lower highs, support a series of ASCENDING higher lows.
          // Require the line to move by at least one tolerance over its span so
          // it's genuinely trending (flat lines belong to the S/R tool, not here).
          const rise = slope * (b - a);
          if (isRes ? (rise > -tol) : (rise < tol)) return;
          // Reject if any bar between the anchors pierces through the line
          let ok = true;
          for (let i = a; i <= b; i++) {
            const line = vals[a] + slope * (i - a);
            if (isRes ? (highs[i] > line + tol) : (lows[i] < line - tol)) { ok = false; break; }
          }
          if (!ok) return;
          // Touches: same-side pivots lying within tol of the line
          let touches = 0;
          pivots.forEach(pi => {
            if (Math.abs(vals[pi] - (vals[a] + slope * (pi - a))) <= tol) touches++;
          });
          if (touches < 2) return;
          const projNow = vals[a] + slope * (last - a);
          const distNow = Math.abs(projNow - vals[last]) / (atr || 1);
          const score   = touches * 1000 + (b - a) + b * 0.5 - distNow * 20;
          cands.push({ a, slope, touches, projNow, score, kind });
        });
      });
      cands.sort((x, y) => y.score - x.score);
      const kept = [];
      for (const c of cands) {
        const dup = kept.some(kc =>
          Math.abs(kc.projNow - c.projNow) < tol * 1.5 &&
          Math.abs(kc.slope - c.slope) < (atr / cs.length) * 3);
        if (!dup) kept.push(c);
        if (kept.length >= (opts.perSide || 2)) break;
      }
      return kept.map(c => ({
        t1: times[c.a], p1: vals[c.a],
        t2: times[last], p2: vals[c.a] + c.slope * (last - c.a),
        kind: c.kind, touches: c.touches,
      }));
    }

    return [...build(pivHi, highs, 'res'), ...build(pivLo, lows, 'sup')];
  }

  // Read the current dropdown settings (lines per side + sensitivity).
  function _tlOpts() {
    const ps = document.querySelector('input[name="tlPerSide"]:checked');
    const st = document.querySelector('input[name="tlStrict"]:checked');
    return {
      perSide: ps ? parseInt(ps.value, 10) : 2,
      tolAtr:  st ? parseFloat(st.value) : 0.6,
    };
  }
  function _clearTrendlineDrawings() {
    for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._tl) drawings.splice(i,1); }
  }
  function _pushTrendlineDrawings(lines) {
    lines.forEach(L => drawings.push({
      type: 'trendline',
      time1: L.t1, price1: L.p1, time2: L.t2, price2: L.p2,
      color: L.kind === 'res' ? '#f23645' : '#089981',
      lineWidth: 1.4, dashed: false, opacity: 0.9,
      visible: true, _tl: true,
    }));
  }

  async function drawTrendlines() {
    if (!currentCandles.length) return;
    const btn  = document.getElementById('lyrTlBtn');
    const card = document.getElementById('lyrTlCard');
    const vis  = document.getElementById('lyrTlVis');
    // Toggle off — remove the auto trendlines
    if (drawings.some(d => d._tl)) {
      _clearTrendlineDrawings();
      saveDrawings(); redrawAll(); updateObjTree();
      btn.textContent = 'TL'; card.classList.remove('active');
      vis.checked = false; vis.disabled = true;
      _ctpHide();
      return;
    }
    btn.textContent = 'TL…'; btn.disabled = true;
    try {
      const lines = _computeTrendlines(currentCandles, _tlOpts());
      if (!lines.length) {
        showNotification('Trendlines', 'No clean trendlines found — try Loose sensitivity (▾)', 'info', 'lets-see.png', 4000);
        btn.textContent = 'TL'; card.classList.remove('active');
        vis.checked = false; vis.disabled = true;
        return;
      }
      _pushTrendlineDrawings(lines);
      saveDrawings(); redrawAll(); updateObjTree();
      const res = lines.filter(l => l.kind === 'res').length;
      const sup = lines.length - res;
      btn.textContent = `TL (${lines.length})`;
      card.classList.add('active');
      vis.checked = true; vis.disabled = false;
      showNotification('Trendlines drawn', `${res} resistance · ${sup} support`, 'success', 'ruler.png', 3500);
    } catch (e) {
      showNotification('Trendlines error', e.message, 'error', 'oh-no.png', 5000);
      btn.textContent = 'TL'; card.classList.remove('active');
    } finally {
      btn.disabled = false;
    }
  }

  // Reactive: when the Trendlines dropdown changes, recompute live if lines are
  // already showing (mirrors the Fib checkbox behavior). If nothing is drawn yet,
  // the change just sets the preference for the next draw.
  ['tlPerSide','tlStrict'].forEach(name => {
    document.querySelectorAll('input[name="' + name + '"]').forEach(radio => {
      radio.addEventListener('change', () => {
        if (!drawings.some(d => d._tl)) return;   // only live-update when active
        _clearTrendlineDrawings();
        const lines = _computeTrendlines(currentCandles, _tlOpts());
        _pushTrendlineDrawings(lines);
        saveDrawings(); redrawAll(); updateObjTree();
        const btn = document.getElementById('lyrTlBtn');
        if (btn) btn.textContent = lines.length ? 'TL (' + lines.length + ')' : 'TL';
      });
    });
  });

  // RSI Divergence — multi-TF scan, visual overlay on price + RSI canvas
  let _rsiDivData = {};   // {tf: [divs]} from last scan

  async function _clearRsiDivOverlay() {
    for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._rsi_div) drawings.splice(i,1); }
    rsiOverlayLines = rsiOverlayLines.filter(l => !l._rsi_div);
    document.getElementById('lyrRsiCard').classList.remove('active');
    document.getElementById('lyrRsiVis').checked = false;
    document.getElementById('lyrRsiVis').disabled = true;
    _ctpHide();
    // Do NOT call saveDrawings() — _rsi_div entries are never stored, so there's
    // nothing new to save. A saveDrawings() here would fire the Firebase SSE stream
    // which then replaces drawings[] and wipes any _rsi_div lines we draw next.
    redrawAll(); rsiRedrawAll(); updateObjTree();
  }

  function _divLabel(type) {
    if (!type) return '';
    const t = type.toLowerCase();
    if (t.includes('hidden') && t.includes('bull')) return 'HB';
    if (t.includes('hidden') && t.includes('bear')) return 'HBear';
    if (t.includes('bull')) return 'Bull';
    if (t.includes('bear')) return 'Bear';
    return type;
  }

  function _drawRsiDivVisual(dv, tf) {
    if (!dv.ts_t1 || !dv.ts_t2) return;
    const isForming = !!dv.forming;
    const col = isForming ? '#f0a500' : (dv.bias === 'bull' ? '#089981' : '#f23645');
    const label = `${isForming ? '~ ' : ''}${_divLabel(dv.type)} ${tf}`;

    // Price trendline — always draws, dashed + amber for forming
    drawings.push({type:'trendline', _rsi_div:true,
      time1:dv.ts_t1, price1:dv.price_t1,
      time2:dv.ts_t2, price2:dv.price_t2,
      color:col, lineWidth:isForming ? 1.2 : 1.5, dashed:isForming, visible:true,
      text:label, fontSize:9, labelPos:'top-left'});

    // Forming only: faint projection extending 4 bars forward on the same slope
    if (isForming && dv.ts_t2 > dv.ts_t1) {
      const TF_SECS = {'5m':300,'15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400};
      const barSecs = TF_SECS[tf] || 3600;
      const slope   = (dv.price_t2 - dv.price_t1) / (dv.ts_t2 - dv.ts_t1);
      const ts_proj = dv.ts_t2 + barSecs * 4;
      drawings.push({type:'trendline', _rsi_div:true,
        time1:dv.ts_t2, price1:dv.price_t2,
        time2:ts_proj,  price2:dv.price_t2 + slope * barSecs * 4,
        color:col + '55', lineWidth:0.8, dashed:true, visible:true});
    }

    // RSI panel — snap to nearest RSI extreme in the correct direction (±7 bars).
    // Bull div compares price LOWS → RSI anchors must also land on RSI LOWS.
    // Bear div compares price HIGHS → RSI anchors must also land on RSI HIGHS.
    const e1 = _getRsiExtremeNear(dv.ts_t1, dv.bias);
    const e2 = _getRsiExtremeNear(dv.ts_t2, dv.bias);
    if (e1.val !== null && e2.val !== null) {
      rsiOverlayLines.push({_rsi_div:true, dashed:isForming,
        time1:e1.ts, value1:e1.val,
        time2:e2.ts, value2:e2.val,
        color:col});
    }
  }

  async function _rsiTfSelected(tf, divs) {
    // Clear the old _rsi_div lines WITHOUT closing the popup
    for (let i = drawings.length-1; i >= 0; i--) { if (drawings[i]._rsi_div) drawings.splice(i,1); }
    rsiOverlayLines = rsiOverlayLines.filter(l => !l._rsi_div);
    // Highlight the selected row immediately so user gets feedback
    document.getElementById('ctpRsiBody').querySelectorAll('.rsi-tf-row').forEach(r => r.classList.toggle('selected', r.dataset.rsiTf === tf));
    try {
      if (!_indicatorState.rsi) document.getElementById('indRsiBtn').click();
      currentTf = tf;
      document.querySelectorAll('[data-tf]').forEach(b => b.classList.toggle('active', b.dataset.tf === tf));
      await loadChart(currentSymbol, currentTf, currentType);
      // 500ms: let the RSI chart finish populating + fitContent animation settle
      await new Promise(r => setTimeout(r, 500));
      // Ensure RSI canvas dimensions are current before drawing
      syncRsiCanvasSize(); _syncRsiAxisWidth();
      // If a forming div shares ts_t1 with a confirmed div, trim it to start from
      // the confirmed div's ts_t2 so they chain rather than overlap.
      const confirmedT2 = {};
      divs.filter(d => !d.forming).forEach(d => { confirmedT2[d.ts_t1] = d; });
      const divsToRender = divs.map(dv => {
        if (!dv.forming) return dv;
        const conf = confirmedT2[dv.ts_t1];
        if (!conf) return dv;
        // Trim: start forming at the confirmed div's t2 anchor
        return { ...dv, ts_t1: conf.ts_t2, price_t1: conf.price_t2 };
      });
      divsToRender.forEach(dv => _drawRsiDivVisual(dv, tf));
      // Single redraw after all lines are pushed
      redrawAll(); rsiRedrawAll(); updateObjTree();
      // Schedule another redraw one frame later in case chart time scale was still settling
      markDirty();
      document.getElementById('lyrRsiBtn').textContent = `RSI÷ ${tf}`;
      document.getElementById('lyrRsiCard').classList.add('active');
      document.getElementById('lyrRsiVis').checked = true;
      document.getElementById('lyrRsiVis').disabled = false;
    } catch(e) {
      console.error('[RSI÷]', e);
    } finally {
      // Guarantee the popup stays open regardless of success or failure
      _ctpShow('rsi');
    }
  }

  function _buildRsiDropdown(byTf) {
    _rsiDivData = byTf;
    const el = document.getElementById('ctpRsiBody');
    const TFS = ['15m','30m','1h','4h'];
    const CONFIRMED_ICON = {bull:'🟢', bear:'🔴'};
    const FORMING_ICON   = {bull:'🟡', bear:'🟠'};
    let html = '';
    TFS.forEach(tf => {
      const divs      = byTf[tf] || [];
      const confirmed = divs.filter(d => !d.forming);
      const forming   = divs.filter(d =>  d.forming);
      if (!divs.length) {
        html += `<div class="rsi-tf-row disabled"><span class="rsi-tf-label">${tf}</span><span class="rsi-tf-na">N/A</span></div>`;
      } else {
        const cStr = confirmed.map(d => `${CONFIRMED_ICON[d.bias]||''} ${_divLabel(d.type)}`).join(', ');
        const fStr = forming.map(d => `${FORMING_ICON[d.bias]||'🟡'} ~${_divLabel(d.type)}`).join(', ');
        const summary = [cStr, fStr].filter(Boolean).join(' · ');
        html += `<div class="rsi-tf-row" data-rsi-tf="${tf}"><span class="rsi-tf-label">${tf}</span><span class="rsi-tf-type">${summary}</span></div>`;
      }
    });
    el.innerHTML = html;
    el.querySelectorAll('[data-rsi-tf]').forEach(row => {
      row.addEventListener('click', () => {
        const tf = row.dataset.rsiTf;
        _rsiTfSelected(tf, _rsiDivData[tf] || []);
      });
    });
  }

  // Nearest-to-1H timeframe that actually has a divergence right now — 1H is
  // preferred outright; if it has none, 30m and 4h are equidistant neighbors, so
  // 4h wins the tie (higher timeframes are treated as more reliable everywhere
  // else in this system too). Returns null only if NO timeframe has anything.
  function _nearestRsiTf(byTf) {
    const DIST_FROM_1H    = {'15m':2, '30m':1, '1h':0, '4h':1};
    const TIE_BREAK_ORDER = ['4h','30m','15m'];
    const withDivs = ['15m','30m','1h','4h'].filter(tf => (byTf[tf]||[]).length > 0);
    if (!withDivs.length) return null;
    if (withDivs.includes('1h')) return '1h';
    let best = null, bestDist = Infinity;
    withDivs.forEach(tf => {
      const dist = DIST_FROM_1H[tf];
      if (dist < bestDist || (dist === bestDist && TIE_BREAK_ORDER.indexOf(tf) < TIE_BREAK_ORDER.indexOf(best))) {
        best = tf; bestDist = dist;
      }
    });
    return best;
  }

  async function showRSIDiv() {
    const btn = document.getElementById('lyrRsiBtn');
    // Toggle off
    if (document.getElementById('lyrRsiCard').classList.contains('active')) {
      await _clearRsiDivOverlay();
      btn.textContent = 'RSI÷';
      return;
    }
    btn.textContent = 'RSI÷…'; btn.disabled = true;
    document.getElementById('ctpRsiBody').innerHTML = '<div class="rsi-loading">Scanning timeframes…</div>';
    _ctpShow('rsi');
    try {
      const r = await _apiFetch(`/api/analysis/rsi_div?symbol=${encodeURIComponent(currentSymbol)}&tf=all`);
      if (!r.ok) throw new Error(`Dexter ${r.status} — restart Dexter if this persists`);
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      _buildRsiDropdown(d.by_tf || {});
      const totalFound = Object.values(d.by_tf||{}).reduce((s,a)=>s+a.length,0);
      btn.textContent = totalFound ? `RSI÷ (${totalFound})` : 'RSI÷';
      // Auto-select: 1H if it has a divergence, else the nearest TF that does —
      // click the button, something appears right away, no manual picking needed.
      const autoTf = _nearestRsiTf(d.by_tf || {});
      if (autoTf) await _rsiTfSelected(autoTf, (d.by_tf||{})[autoTf] || []);
    } catch(e) {
      _showToast(`<span class="tBear">RSI÷ — ${e.message}</span>`, 6000);
      btn.textContent = 'RSI÷';
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById('srAutoBtn').addEventListener('click', drawSRZones);
  document.getElementById('vpBtn').addEventListener('click', drawVP);
  document.getElementById('atrBtn').addEventListener('click', showATR);
  // Fib/RSI main-button + arrow clicks are wired later alongside the rest of the
  // Layers-panel tool buttons (see the lyr* wiring block) — the old topbar
  // fibStackBtn/rsiDivBtn/fibDropArrow/rsiDropArrow buttons this used to bind to
  // were removed 2026-07-05 (dead, permanently-hidden legacy UI; see arsenalToggleBtn
  // below — nothing has re-opened that topbar strip in a long time).

  // Arsenal button → opens LAYERS tab in right intel panel
  document.getElementById('arsenalToggleBtn').addEventListener('click', () => {
    // Close the topbar Arsenal strip if it happens to be open
    document.getElementById('chevArsenal').classList.remove('open');
    document.getElementById('arsenalToggleBtn').classList.remove('open');
    // Programmatically click the LAYERS tab — its handler opens the panel and switches panes
    const layersTab = document.querySelector('#intelTabBar [data-tab="layers"]');
    if (layersTab) layersTab.click();
  });

  // ---- Chart Tool Popup (Fib/RSI, added 2026-07-05) ----
  // Shared in-chart card, replacing the old button-anchored dropdowns that got
  // clipped by the panel's edge. Deliberately does NOT auto-close on outside
  // clicks — it stays open until the X is pressed or the tool itself is toggled
  // off, so you can freely click around the chart while it's showing.
  function _ctpShow(which) {
    const pop = document.getElementById('chartToolPopup');
    const titles = { fib: 'Fibonacci', rsi: 'RSI Divergence', tl: 'Trendlines', vp: 'Volume Profile' };
    document.getElementById('ctpTitle').textContent = titles[which] || '';
    document.getElementById('ctpFibBody').classList.toggle('shown', which === 'fib');
    document.getElementById('ctpRsiBody').classList.toggle('shown', which === 'rsi');
    const tlBody = document.getElementById('ctpTlBody');
    if (tlBody) tlBody.classList.toggle('shown', which === 'tl');
    const vpBody = document.getElementById('ctpVpBody');
    if (vpBody) vpBody.classList.toggle('shown', which === 'vp');
    pop.classList.add('open');
  }
  function _ctpHide() {
    document.getElementById('chartToolPopup').classList.remove('open');
  }
  document.getElementById('ctpClose').addEventListener('click', _ctpHide);

  function _buildChartContext() {
    const lines = [];
    lines.push(`CHART: ${currentSymbol} | ${currentTf.toUpperCase()} | ${currentType}`);
    if (currentCandles.length) {
      const last = currentCandles[currentCandles.length - 1];
      lines.push(`PRICE: O=${last.open} H=${last.high} L=${last.low} C=${last.close}`);

      // SR: uses same shared algorithm as the S/R button on the chart
      const { res: _rt, sup: _st } = _getSRTouches(currentCandles);
      const curPrice = last.close;
      const _resLvls = _buildSRLevels(_rt).filter(z=>z.price>curPrice).sort((a,b)=>a.price-b.price).slice(0,5);
      const _supLvls = _buildSRLevels(_st).filter(z=>z.price<curPrice).sort((a,b)=>b.price-a.price).slice(0,5);
      if (_resLvls.length) lines.push(`RESISTANCE ZONES (${currentTf.toUpperCase()}, reversal-confirmed, ${currentCandles.length} candles): ${_resLvls.map(z=>`${z.price.toFixed(5)}(${z.touches}x)`).join(', ')}`);
      else lines.push(`RESISTANCE ZONES: none confirmed on ${currentTf.toUpperCase()} yet`);
      if (_supLvls.length) lines.push(`SUPPORT ZONES (${currentTf.toUpperCase()}, reversal-confirmed, ${currentCandles.length} candles): ${_supLvls.map(z=>`${z.price.toFixed(5)}(${z.touches}x)`).join(', ')}`);
      else lines.push(`SUPPORT ZONES: none confirmed on ${currentTf.toUpperCase()} yet`);
    }
    if (_activeTrade) {
      const t = _activeTrade;
      lines.push(`ACTIVE TRADE: ${t.symbol || currentSymbol} ${(t.direction||'').toUpperCase()} | Status: ${t.status || 'OPEN'}`);
      if (t.entry != null) lines.push(`  Entry: ${t.entry}  SL: ${t.sl}  TP: ${t.tp}`);
      if (t.conf)          lines.push(`  Confluences: ${t.conf}`);
      if (t.reasoning)     lines.push(`  Reasoning: ${t.reasoning}`);
      if (t.open_ts)       lines.push(`  Opened: ${t.open_ts}`);
    } else {
      lines.push(`ACTIVE TRADE: none`);
    }
    if (typeof drawings !== 'undefined' && drawings.length) {
      const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.65, 0.786, 1, 1.272, 1.618];
      const fibs = drawings.filter(d => d.type === 'fib');
      const hlines = drawings.filter(d => d.type === 'hline');
      const tlines = drawings.filter(d => d.type === 'trendline' || d.type === 'ray');
      const srs = drawings.filter(d => d.type === 'rect');
      if (fibs.length) {
        lines.push(`FIBONACCI (${fibs.length}):`);
        fibs.forEach((d, i) => {
          const high = Math.max(d.price1, d.price2);
          const low  = Math.min(d.price1, d.price2);
          const rng  = high - low;
          const lvls = FIB_LEVELS.map(l => `  ${(l*100).toFixed(1)}%: ${(high - rng*l).toFixed(5)}`).join('\n');
          lines.push(`  Fib ${i+1}: anchor high=${high.toFixed(5)} low=${low.toFixed(5)}\n${lvls}`);
        });
      }
      if (hlines.length) {
        lines.push(`H-LINES: ${hlines.map(d => d.price.toFixed(5)).join(', ')}`);
      }
      if (tlines.length) {
        lines.push(`TRENDLINES (${tlines.length}): ${tlines.map(d => `${d.price1.toFixed(5)}→${d.price2.toFixed(5)}`).join(' | ')}`);
      }
      if (srs.length) {
        lines.push(`ZONES (${srs.length}): ${srs.map(d => `${Math.min(d.price1,d.price2).toFixed(5)}-${Math.max(d.price1,d.price2).toFixed(5)}`).join(' | ')}`);
      }
    }
    return lines.join('\n');
  }

  function _applyChevDrawings(text) {
    const match = text.match(/```chevdraw\s*([\s\S]*?)```/);
    if (!match) return text;
    try {
      const cmds = JSON.parse(match[1].trim());
      const tfSec = { '15m':900,'30m':1800,'1h':3600,'4h':14400,'1d':86400 }[currentTf] || 3600;
      const t1 = currentCandles.length ? currentCandles[0].time : 0;
      const t2 = currentCandles.length ? currentCandles[currentCandles.length-1].time + tfSec * 200 : Date.now()/1000 + 86400*365;
      cmds.forEach(cmd => {
        const base = { color: cmd.color || '#2962ff', text: cmd.label || cmd.text || '', lineWidth: 1.5, fontSize: 9, textAlign: 'right', fillOpacity: 0.12, visible: true, _chev: true };
        if (cmd.type === 'hline' && cmd.price != null) {
          drawings.push({ ...base, type: 'hline', price: cmd.price });
        } else if (cmd.type === 'rect' && cmd.price1 != null && cmd.price2 != null) {
          drawings.push({ ...base, type: 'rect', time1: t1, price1: cmd.price1, time2: t2, price2: cmd.price2 });
        } else if (cmd.type === 'fib' && cmd.price1 != null && cmd.price2 != null) {
          drawings.push({ ...base, type: 'fib', time1: t1, price1: cmd.price1, time2: t2, price2: cmd.price2 });
        }
      });
      saveDrawings();
      if (typeof redrawAll === 'function') redrawAll();
      updateObjTree();
    } catch(e) { console.warn('[ChevDraw] parse error:', e); }
    return text.replace(/```chevdraw[\s\S]*?```/g, '').trim();
  }

  async function sendChatMessage() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = '';
    const chat = chats[activeChatId];
    chat.messages.push({ role: 'user', content: text });
    if (chat.title === 'New chat') chat.title = text.slice(0, 24);
    saveChats();
    renderChatList();
    renderMessages();

    chatMessagesEl.innerHTML += `<div class="msg assistant typing" id="chevTyping"><div class="who">CHEV ›</div><div class="bubble"><img src="emoji/time.png" alt="" style="width:12px;height:12px;vertical-align:middle;margin-right:4px;opacity:0.6">thinking…</div></div>`;
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
    try {
      const drawInstructions = `\n\nCRITICAL — YOU CAN DRAW ON THIS CHART: You have the ability to draw directly on the chart. When the user asks you to draw, mark, highlight, or visualize any levels, you MUST do it — never say you cannot draw. Output a \`\`\`chevdraw JSON array at the END of your response.\n\nDrawing format (JSON array inside chevdraw block):\n- Horizontal line: {"type":"hline","price":1.2345,"label":"Resistance","color":"#f23645"}\n- Zone/rect: {"type":"rect","price1":1.250,"price2":1.230,"label":"Supply zone","color":"#f23645"}\n- Fibonacci: {"type":"fib","price1":1.280,"price2":1.180,"label":"Fib"}\nColor guide: #f23645=red (resistance/supply), #089981=green (support/demand), #2962ff=blue (neutral), #d4af37=gold (key level).\nUse real price values from the chart context. Draw multiple objects in one block if needed.`;
      const srRules = `\n\nSR RULES (follow strictly): Support and resistance are zones where price has REPEATEDLY reversed — not just any high or low. A level is only valid SR if price returned to it at least twice. The context provides RESISTANCE LEVELS and SUPPORT LEVELS with a touch count (e.g. 72.134(3x) means price tested that zone 3 times). Higher touch count = stronger level. Always use these real levels — never invent numbers. SR is a zone, not an exact price. When drawing, use the provided level prices exactly.\n\nTRENDLINE RULES: A valid trendline needs at least 3 touches. In an uptrend the line is drawn below price connecting swing lows. In a downtrend it is drawn above price connecting swing highs. Tell the user how many touches you identified and which swing points the line connects.`;
      const systemMsg = { role: 'system', content: `You are Chev Chelios, a professional trading assistant. Current screen context:\n${_buildChartContext()}\n\nAnswer concisely. If the user asks about the chart or trade, use this context.${srRules}${drawInstructions}` };
      const primeMsg = { role: 'assistant', content: 'Understood. I have full drawing capability on this chart via chevdraw blocks. I will draw when asked.' };
      const res = await fetch('/api/chev_chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Chev-Key': getDashboardKey() },
        body: JSON.stringify({ model: getChevModelId(), messages: [systemMsg, primeMsg, ...chat.messages.map(m => ({ role: m.role, content: m.content }))] }),
      });
      if (res.status === 401 || res.status === 403) {
        localStorage.removeItem('chevDashboardKey');
        chat.messages.push({ role: 'assistant', content: 'Dashboard key rejected — try sending again to re-enter it.' });
        renderMessages();
        return;
      }
      const data = await res.json();
      const rawReply = data.choices[0].message.content;
      const cleanReply = _applyChevDrawings(rawReply);
      chat.messages.push({ role: 'assistant', content: cleanReply });
      saveChats();
      renderMessages();
    } catch (e) {
      chat.messages.push({ role: 'assistant', content: 'Connection error - check Open WebUI is running.' });
      renderMessages();
    }
  }

  chatSendBtn.addEventListener('click', sendChatMessage);
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
  });

  renderChatList();
  renderMessages();

