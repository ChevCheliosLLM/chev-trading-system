/* ============================================================
   Drawing tools v3, custom canvas annotations, object tree, popups (kept intact: contains 2 IIFEs + internal forward calls).
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     DRAWING TOOLS v3
     — two-click workflow, magnet snap indicator, drag-to-move,
       right-side extrapolation, personalization context menu,
       object tree, volume profile, measure tool
     ============================================================ */
  const drawCanvas = document.getElementById('drawingCanvas');
  const dctx = drawCanvas.getContext('2d');

  const TOOL_COLORS = {
    hline:'#f0b429', ray:'#2962ff', trendline:'#9598a1',
    rect:'#2962ff', fib:'#d4af37', channel:'#a78bfa',
    triangle:'#2dd4bf', vp:'#2962ff', measure:'#089981', pencil:'#e0e0e0',
  };

  let activeTool   = null;
  let magnetOn     = true;
  let cursorPos    = null; // tracks mouse for measure crosshairs
  let pencilDrawing = false;
  let pencilPoints  = [];
  let _drawStream  = null;
  let drawings     = loadDrawings(currentSymbol);
  rsiDrawings = loadRsiDrawings(currentSymbol);
  _syncDrawings(currentSymbol); _subscribeDrawings(currentSymbol); // pull latest from Firebase + live updates
  let previewShape = null;
  let magnetDot    = null;  // {x,y} snap point shown before click
  let overlayDrawings = []; // temporary analysis overlay (not saved)

  // Undo / Copy-Paste
  const MAX_UNDO = 10;
  let undoStack = [];
  let _copiedDrawing = null;
  function pushUndo() {
    undoStack.push(JSON.parse(JSON.stringify(drawings)));
    if (undoStack.length > MAX_UNDO) undoStack.shift();
  }

  // Two-click state
  let clickCount    = 0;
  let firstClickPos = null;

  // Channel 3-click state
  let channelBase = null;
  let channelStep = 0;

  // Triangle 3-click state
  let triangleP2 = null;

  // Drag state
  let dragMode     = false;
  let dragIndex    = -1;
  let dragEndpoint = null;  // null=whole shape, 1=ep1, 2=ep2
  let dragStartRaw = null;
  let dragOrigDraw = null;
  let hoverIndex   = -1;
  let hoverXPos    = null;
  let hoverEP      = null;  // which endpoint is under the cursor (null/1/2)

  // Context-menu target
  let ctxTarget = -1;

  function _drawFbUrl(sym) {
    return `${FIREBASE_BASE}/drawings/${(sym||currentSymbol).replace('/','_')}.json`;
  }
  function loadDrawings(sym) {
    try { return JSON.parse(localStorage.getItem('chevDrawings_' + sym) || '[]'); } catch(e) { return []; }
  }
  function saveDrawings(sym) {
    sym = sym || currentSymbol;
    const payload = JSON.stringify(drawings.filter(d => !d._rsi_div));
    localStorage.setItem('chevDrawings_' + sym, payload);
    fetch(_drawFbUrl(sym), { method:'PUT', body:payload, headers:{'Content-Type':'application/json'} }).catch(()=>{});
  }
  // Pull drawings from Firebase and update the canvas (called when switching symbols)
  async function _syncDrawings(sym) {
    try {
      const r = await fetch(_drawFbUrl(sym));
      if (!r.ok) return;
      const d = await r.json();
      if (!Array.isArray(d)) return;
      const live_rsi = drawings.filter(x => x._rsi_div);
      drawings = d;
      if (live_rsi.length) drawings.push(...live_rsi);
      localStorage.setItem('chevDrawings_' + sym, JSON.stringify(d));
      updateObjTree(); markDirty();
    } catch(e) {}
  }
  // Real-time SSE listener — friend's drawings appear on your canvas instantly
  function _subscribeDrawings(sym) {
    if (_drawStream) { _drawStream.close(); _drawStream = null; }
    const sseUrl = _drawFbUrl(sym).replace('.json', '.json?stream=true&timeout=600s');
    try {
      _drawStream = new EventSource(sseUrl);
      _drawStream.addEventListener('put', ev => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.path === '/' && msg.data !== undefined) {
            const live_rsi = drawings.filter(d => d._rsi_div);
            drawings = Array.isArray(msg.data) ? msg.data : [];
            if (live_rsi.length) drawings.push(...live_rsi);
            localStorage.setItem('chevDrawings_' + sym, JSON.stringify(drawings.filter(d => !d._rsi_div)));
            updateObjTree(); markDirty();
          }
        } catch(e) {}
      });
      _drawStream.onerror = () => {};
    } catch(e) {}
  }
  function dv(d, k, def) { return d[k] !== undefined ? d[k] : def; }

  /* ---- Canvas sizing ---- */
  let _dw = 0, _dh = 0; // cached drawing canvas CSS dimensions (avoids clientWidth reads in RAF)
  function syncCanvasSize() {
    const dpr = window.devicePixelRatio || 1;
    const w = chartWrap.clientWidth;
    const h = chartWrap.clientHeight;
    _dw = w; _dh = h;
    drawCanvas.width  = Math.round(w * dpr);
    drawCanvas.height = Math.round(h * dpr);
    drawCanvas.style.width  = w + 'px';
    drawCanvas.style.height = h + 'px';
    dctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  syncCanvasSize();
  window.addEventListener('resize', syncCanvasSize);

  /* ---- Coordinate helpers with right-side extrapolation ---- */
  function priceToY(p) { return candleSeries.priceToCoordinate(p); }
  function yToPrice(y) { return candleSeries.coordinateToPrice(y); }

  function _bm() {
    const n = currentCandles.length;
    if (n < 2) return null;
    const la = currentCandles[n-1], pr = currentCandles[n-2];
    const lx = chart.timeScale().timeToCoordinate(la.time);
    const px = chart.timeScale().timeToCoordinate(pr.time);
    if (lx==null||px==null||lx===px) return null;
    return { lx, lt:la.time, ppb:lx-px, bd:la.time-pr.time };
  }

  function timeToX(t) {
    const x = chart.timeScale().timeToCoordinate(t);
    if (x!=null) return x;
    const m = _bm(); if (!m) return null;
    return m.lx + ((t - m.lt) / m.bd) * m.ppb;
  }

  function xToTime(x) {
    const t = chart.timeScale().coordinateToTime(x);
    if (t!=null) return t;
    const m = _bm(); if (!m) return null;
    return Math.round(m.lt + ((x - m.lx) / m.ppb) * m.bd);
  }

  /* ---- Channel: interpolate base-line price at a given time ---- */
  function basePriceAt(base, time) {
    if (base.time2 === base.time1) return base.price1;
    return base.price1 + (base.price2 - base.price1) * (time - base.time1) / (base.time2 - base.time1);
  }

  /* ---- Magnet ---- */
  function snapMagnet(x, y) {
    if (!magnetOn || !currentCandles.length) return { x, y, hit:false };
    const R = 22;
    let best=R, bx=x, by=y, hit=false;
    for (const c of currentCandles) {
      const cx = timeToX(c.time);
      if (cx==null || Math.abs(cx-x) > R*2) continue;
      for (const p of [c.high, c.low, c.open, c.close]) {
        const cy = priceToY(p); if (cy==null) continue;
        const d = Math.hypot(cx-x, cy-y);
        if (d < best) { best=d; bx=cx; by=cy; hit=true; }
      }
    }
    return { x:bx, y:by, hit };
  }

  function rawPos(e) {
    const r = chartWrap.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  /* ---- Volume Profile computation ---- */
  function computeVP(t1, t2, N) {
    N = N||40;
    const lo=Math.min(t1,t2), hi=Math.max(t1,t2);
    const cc = currentCandles.filter(c=>c.time>=lo&&c.time<=hi);
    if (!cc.length) return null;
    const pMin=Math.min(...cc.map(c=>c.low)), pMax=Math.max(...cc.map(c=>c.high));
    const bs=(pMax-pMin)/N;
    const vols=Array(N).fill(0);
    for (const c of cc) {
      const vol=c.volume||0;
      for (let i=0;i<N;i++) {
        const bLo=pMin+i*bs, bHi=bLo+bs;
        const ov=Math.max(0,Math.min(c.high,bHi)-Math.max(c.low,bLo));
        const ratio=c.high>c.low?ov/(c.high-c.low):1/N;
        vols[i]+=vol*ratio;
      }
    }
    // Value area (70% around POC)
    const pocI=vols.reduce((b,v,i)=>v>vols[b]?i:b,0);
    const total=vols.reduce((s,v)=>s+v,0);
    let loI=pocI, hiI=pocI, acc=vols[pocI];
    while (acc<total*0.70 && (loI>0||hiI<N-1)) {
      const aLo=loI>0?vols[loI-1]:-1, aHi=hiI<N-1?vols[hiI+1]:-1;
      if (aLo>=aHi&&loI>0){loI--;acc+=vols[loI];}
      else if (hiI<N-1){hiI++;acc+=vols[hiI];}
      else break;
    }
    return { vols, pMin, bs, N, pocI, vah: pMin+(hiI+1)*bs, val: pMin+loI*bs };
  }

  /* ---- Measure helpers ---- */
  function countBars(t1,t2) {
    const lo=Math.min(t1,t2), hi=Math.max(t1,t2);
    return currentCandles.filter(c=>c.time>=lo&&c.time<=hi).length;
  }
  function fmtTime(s) {
    s=Math.abs(s);
    if (s<3600) return Math.round(s/60)+'m';
    if (s<86400) return (s/3600).toFixed(1)+'h';
    return (s/86400).toFixed(1)+'d';
  }

  /* ---- Hit testing ---- */
  function _ds(px,py,ax,ay,bx,by) {
    const dx=bx-ax,dy=by-ay,l2=dx*dx+dy*dy;
    if (!l2) return Math.hypot(px-ax,py-ay);
    const t=Math.max(0,Math.min(1,((px-ax)*dx+(py-ay)*dy)/l2));
    return Math.hypot(px-(ax+t*dx),py-(ay+t*dy));
  }

  function hitTest(mx,my) {
    const H=9;
    for (let i=drawings.length-1;i>=0;i--) {
      const d=drawings[i];
      if (d.visible===false) continue;
      if (d._sr||d._vp||d._fib_stack||d._rsi_div||d._chev) continue;
      if (d.type==='hline') {
        const y=priceToY(d.price); if (y!=null&&Math.abs(my-y)<H) return i;
      } else if (d.type==='ray') {
        const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
        if (x1==null||y1==null||x2==null||y2==null) continue;
        if (_ds(mx,my,x1,y1,x1+(x2-x1)*200,y1+(y2-y1)*200)<H) return i;
      } else if (d.type==='trendline') {
        const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
        if (x1==null||y1==null||x2==null||y2==null) continue;
        if (_ds(mx,my,x1,y1,x2,y2)<H) return i;
      } else if (d.type==='vp') {
        const _ay=18,x1=timeToX(d.time1),x2=timeToX(d.time2);
        if (x1==null||x2==null) continue;
        if (Math.hypot(mx-x1,my-_ay)<H*2||Math.hypot(mx-x2,my-_ay)<H*2) return i;
      } else if (d.type==='rect'||d.type==='measure') {
        const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
        if (x1==null||y1==null||x2==null||y2==null) continue;
        const nx=Math.min(x1,x2),xx=Math.max(x1,x2),ny=Math.min(y1,y2),xy=Math.max(y1,y2);
        if (mx>=nx-H&&mx<=xx+H&&my>=ny-H&&my<=xy+H&&(mx<=nx+H||mx>=xx-H||my<=ny+H||my>=xy-H)) return i;
      } else if (d.type==='fib') {
        // Only hit at the two anchor endpoints — fib lines are too plentiful to safely hit
        const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
        if (x1==null||y1==null||x2==null||y2==null) continue;
        if (Math.hypot(mx-x1,my-y1)<H*1.5) return i;
        if (Math.hypot(mx-x2,my-y2)<H*1.5) return i;
      } else if (d.type==='channel') {
        const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
        if (x1==null||y1==null||x2==null||y2==null) continue;
        if (Math.hypot(mx-x1,my-y1)<H*2||Math.hypot(mx-x2,my-y2)<H*2) return i;
        if (d.priceOffset!=null) {
          const y1b=priceToY(d.price1+d.priceOffset),y2b=priceToY(d.price2+d.priceOffset);
          if (y1b!=null&&y2b!=null&&Math.hypot(mx-(x1+x2)/2,my-(y1b+y2b)/2)<H*2) return i;
        }
      } else if (d.type==='triangle') {
        const x1=timeToX(d.time1),y1=priceToY(d.price1);
        const x2=timeToX(d.time2),y2=priceToY(d.price2);
        const x3=timeToX(d.time3),y3=priceToY(d.price3);
        if (x1==null||y1==null||x2==null||y2==null||x3==null||y3==null) continue;
        if (_ds(mx,my,x1,y1,x2,y2)<H||_ds(mx,my,x2,y2,x3,y3)<H||_ds(mx,my,x3,y3,x1,y1)<H) return i;
      } else if (d.type==='pencil') {
        if (!d.points||d.points.length<2) continue;
        for (let j=0;j<d.points.length-1;j++) {
          const ax=timeToX(d.points[j].time),ay=priceToY(d.points[j].price);
          const bx=timeToX(d.points[j+1].time),by=priceToY(d.points[j+1].price);
          if (ax==null||ay==null||bx==null||by==null) continue;
          if (_ds(mx,my,ax,ay,bx,by)<H) return i;
        }
      }
    }
    return -1;
  }

  /* ---- Endpoint hit detection ---- */
  function getEndpointHit(mx, my, d) {
    const EP = 10;
    if (['trendline','ray','fib'].includes(d.type)) {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1!=null&&y1!=null&&Math.hypot(mx-x1,my-y1)<EP) return 1;
      if (x2!=null&&y2!=null&&Math.hypot(mx-x2,my-y2)<EP) return 2;
    } else if (d.type==='triangle') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1);
      const x2=timeToX(d.time2),y2=priceToY(d.price2);
      const x3=timeToX(d.time3),y3=priceToY(d.price3);
      if (x1!=null&&y1!=null&&Math.hypot(mx-x1,my-y1)<EP) return 1;
      if (x2!=null&&y2!=null&&Math.hypot(mx-x2,my-y2)<EP) return 2;
      if (x3!=null&&y3!=null&&Math.hypot(mx-x3,my-y3)<EP) return 3;
    } else if (d.type==='channel') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1!=null&&y1!=null&&Math.hypot(mx-x1,my-y1)<EP) return 1;
      if (x2!=null&&y2!=null&&Math.hypot(mx-x2,my-y2)<EP) return 2;
      if (d.priceOffset!=null) {
        const y1b=priceToY(d.price1+d.priceOffset),y2b=priceToY(d.price2+d.priceOffset);
        if (y1b!=null&&y2b!=null&&Math.hypot(mx-(x1+x2)/2,my-(y1b+y2b)/2)<EP) return 3;
      }
    } else if (d.type==='rect'||d.type==='measure') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null) return null;
      // 4 corners: EP1=(x1,y1), EP2=(x2,y2), EP3=(x1,y2), EP4=(x2,y1)
      if (Math.hypot(mx-x1,my-y1)<EP) return 1;
      if (Math.hypot(mx-x2,my-y2)<EP) return 2;
      if (Math.hypot(mx-x1,my-y2)<EP) return 3;
      if (Math.hypot(mx-x2,my-y1)<EP) return 4;
    } else if (d.type==='vp') {
      const _ay=18,x1=timeToX(d.time1),x2=timeToX(d.time2);
      if (x1!=null&&Math.hypot(mx-x1,my-_ay)<EP) return 1;
      if (x2!=null&&Math.hypot(mx-x2,my-_ay)<EP) return 2;
    }
    return null;
  }

  function _isSipActive() {
    if (!_activeTrade || _activeTrade.sl == null || _activeTrade.entry == null) return false;
    const isLong = (_activeTrade.direction || '').toLowerCase().includes('long');
    return isLong ? _activeTrade.sl > _activeTrade.entry : _activeTrade.sl < _activeTrade.entry;
  }

  /* ---- Rendering ---- */
  function redrawAll() {
    dctx.clearRect(0,0,_dw,_dh);
    // MTF candle overlay — higher TF candles as transparent boxes (bottom layer)
    if (mtfCandles.length && document.getElementById('ovChkMTF')?.checked) {
      const tfSec = { '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400 }[mtfTf] || 14400;
      dctx.save();
      for (let i = 0; i < mtfCandles.length; i++) {
        const c     = mtfCandles[i];
        const nextT = mtfCandles[i + 1] ? mtfCandles[i + 1].time : c.time + tfSec;
        const x1    = timeToX(c.time);
        const x2    = timeToX(nextT);
        if (x1 == null || x2 == null || x2 <= x1) continue;
        const isBull  = c.close >= c.open;
        const bodyTop = priceToY(Math.max(c.open, c.close));
        const bodyBot = priceToY(Math.min(c.open, c.close));
        if (bodyTop == null || bodyBot == null) continue;
        const midX    = (x1 + x2) / 2;
        const wickTop = priceToY(c.high);
        const wickBot = priceToY(c.low);
        const bull = 'rgba(8,153,129,', red = 'rgba(242,54,69,';
        const col  = isBull ? bull : red;
        // Wicks
        if (wickTop != null && wickBot != null) {
          dctx.strokeStyle = col + '0.3)'; dctx.lineWidth = 1;
          dctx.beginPath();
          dctx.moveTo(midX, wickTop); dctx.lineTo(midX, bodyTop);
          dctx.moveTo(midX, bodyBot); dctx.lineTo(midX, wickBot);
          dctx.stroke();
        }
        // Body fill + border
        const h = Math.max(Math.abs(bodyBot - bodyTop), 1);
        dctx.fillStyle   = col + '0.12)';
        dctx.strokeStyle = col + '0.5)'; dctx.lineWidth = 0.5;
        dctx.fillRect(x1, bodyTop, x2 - x1, h);
        dctx.strokeRect(x1, bodyTop, x2 - x1, h);
      }
      dctx.restore();
    }
    // Colored zones between entry/SL/TP (Binance/TradingView style)
    if (_overlayEntry && _activeTrade) {
      const { entry, sl, tp } = _activeTrade;
      const sipOn = _isSipActive();
      const ey = (entry != null && _ipE()) ? priceToY(entry) : null;
      const cw = _dw;
      if (ey != null) {
        if (sl != null && _ipSL()) {
          const sy = priceToY(sl);
          if (sy != null) {
            dctx.save();
            dctx.fillStyle = sipOn ? 'rgba(8,153,129,0.05)' : 'rgba(242,54,69,0.04)';
            dctx.fillRect(0, Math.min(ey,sy), cw, Math.abs(sy-ey));
            dctx.restore();
          }
        }
        if (tp != null && _ipTP()) {
          const ty = priceToY(tp);
          if (ty != null) {
            dctx.save();
            dctx.fillStyle = 'rgba(8,153,129,0.04)';
            dctx.fillRect(0, Math.min(ey,ty), cw, Math.abs(ty-ey));
            dctx.restore();
          }
        }
      }
    }
    drawings.forEach((d,i)=>{ if (d.visible!==false) renderShape(dctx,d,false,i===hoverIndex&&!activeTool); });
    overlayDrawings.forEach(d=>renderShape(dctx,d,false,false));
    // Crosshairs — faint X+Y guidelines for every active tool
    if (activeTool&&cursorPos) {
      const crossCol=TOOL_COLORS[activeTool]||'#9598a1';
      dctx.save(); dctx.globalAlpha=0.07; dctx.strokeStyle=crossCol;
      dctx.lineWidth=1; dctx.setLineDash([4,4]);
      dctx.beginPath(); dctx.moveTo(cursorPos.x,0); dctx.lineTo(cursorPos.x,_dh); dctx.stroke();
      dctx.beginPath(); dctx.moveTo(0,cursorPos.y); dctx.lineTo(_dw,cursorPos.y); dctx.stroke();
      dctx.restore();
    }
    // SL/SIP / TP canvas badge labels (TradingView-style pill on left side of price axis)
    if (_overlayEntry && _activeTrade) {
      const { entry, sl, tp } = _activeTrade;
      const sipOn = _isSipActive();
      dctx.save();
      dctx.font = 'bold 6px "Share Tech Mono",monospace';
      dctx.textBaseline = 'middle';
      function _drawPriceBadge(price, label, bgCol, textCol) {
        const y = priceToY(price);
        if (y == null) return;
        const txt = label + ' ' + price;
        const tw  = dctx.measureText(txt).width;
        const ph  = 9, pw = tw + 6, px = 4;
        dctx.fillStyle = bgCol;
        dctx.beginPath();
        dctx.roundRect(px, y - ph/2, pw, ph, 2);
        dctx.fill();
        dctx.fillStyle = textCol || '#fff';
        dctx.fillText(txt, px + 3, y);
      }
      if (entry != null && _ipE())  _drawPriceBadge(entry, 'ENTRY', 'rgba(212,175,55,0.85)', '#131722');
      if (sl    != null && _ipSL()) _drawPriceBadge(sl,    sipOn ? 'SIP' : 'SL', sipOn ? 'rgba(212,175,55,0.85)' : 'rgba(242,54,69,0.85)', '#131722');
      if (tp    != null && _ipTP()) _drawPriceBadge(tp,    'TP',   'rgba(8,153,129,0.85)', '#131722');
      dctx.restore();
    }
    // Trade entry marker — only for OPEN trades (PENDING hasn't triggered yet)
    if (_overlayEntry && _activeTrade && _activeTrade.entry != null && (_activeTrade.status || 'OPEN') !== 'PENDING') {
      const ep = _activeTrade.entry;
      const tfSec = { '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400 }[currentTf] || 3600;
      let best = null;
      if (_activeTrade.open_ts) {
        const tsUnix = Math.floor(new Date(_activeTrade.open_ts.replace(' ','T')).getTime() / 1000);
        // Search within ±50 candles of open_ts for a candle that also touched the entry price
        const window = tfSec * 50;
        const candidates = currentCandles.filter(c =>
          Math.abs(c.time - tsUnix) <= window && c.low <= ep && ep <= c.high
        );
        if (candidates.length > 0) {
          // Among matches, pick the one closest to open_ts
          best = candidates.reduce((a, b) =>
            Math.abs(a.time - tsUnix) < Math.abs(b.time - tsUnix) ? a : b
          );
        }
      }
      if (best) {
        const ex  = timeToX(best.time);
        const ey  = priceToY(_activeTrade.entry); // exact entry price Y (not candle high/low)
        const isLong = _activeTrade.direction.toLowerCase().includes('long');
        if (ex != null && ey != null) {
          const offset = isLong ? 11 : -11; // 11px away from entry line
          const baseY  = ey + offset;       // triangle base
          dctx.save();
          dctx.fillStyle = '#d4af37';
          dctx.shadowColor = '#d4af37'; dctx.shadowBlur = 4;
          dctx.beginPath();
          // tip touches the entry price line, base extends away
          dctx.moveTo(ex, ey);
          dctx.lineTo(ex - 3.3, baseY);
          dctx.lineTo(ex + 3.3, baseY);
          dctx.closePath(); dctx.fill();
          dctx.font = '8px "Share Tech Mono",monospace';
          dctx.fillStyle = '#d4af37'; dctx.shadowBlur = 0;
          dctx.textAlign = 'center'; dctx.textBaseline = isLong ? 'top' : 'bottom';
          const _lblY = baseY + (isLong ? 6 : -6);
          dctx.fillText(`Entry ${_activeTrade.entry}`, ex, _lblY);
          // When did he enter — show the open timestamp under the entry label
          if (_activeTrade.open_ts) {
            let _whenTxt = '';
            try {
              const _d = new Date(_activeTrade.open_ts.replace(' ', 'T'));
              if (!isNaN(_d)) _whenTxt = _d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            } catch (e) {}
            if (_whenTxt) {
              dctx.font = '7px "Share Tech Mono",monospace';
              dctx.fillStyle = 'rgba(212,175,55,0.7)';
              dctx.fillText(_whenTxt, ex, _lblY + (isLong ? 9 : -9));
            }
          }
          dctx.restore();
        }
      }
    }

    if (activeTool&&clickCount>=1&&firstClickPos) {
      const col=TOOL_COLORS[activeTool]||'#fff';
      [[firstClickPos],[triangleP2]].forEach(arr=>{
        const p=arr[0]; if(!p) return;
        const ax=timeToX(p.time),ay=priceToY(p.price);
        if (ax!=null&&ay!=null) {
          dctx.save(); dctx.shadowColor=col; dctx.shadowBlur=8; dctx.fillStyle=col;
          dctx.beginPath(); dctx.arc(ax,ay,5,0,Math.PI*2); dctx.fill(); dctx.restore();
        }
      });
    }
    if (previewShape) renderShape(dctx,previewShape,true,false);
    if (magnetDot&&activeTool) {
      dctx.save();
      dctx.strokeStyle='#fff'; dctx.lineWidth=1.5; dctx.fillStyle='rgba(255,255,255,0.85)';
      dctx.shadowColor='#fff'; dctx.shadowBlur=5;
      dctx.beginPath(); dctx.arc(magnetDot.x,magnetDot.y,4,0,Math.PI*2);
      dctx.fill(); dctx.stroke(); dctx.restore();
    }
    // Hover-X: faint delete hint on hovered drawing
    if (hoverIndex>=0&&!activeTool&&drawings[hoverIndex]) {
      _drawHoverX(dctx,drawings[hoverIndex]);
    } else { hoverXPos=null; }
  }

  function _drawHoverX(ctx,d) {
    let cx=null,cy=null;
    if (d.type==='hline') {
      const ly=priceToY(d.price);
      if (ly!=null) { cx=_dw-56; cy=Math.max(10,ly-10); }
    } else if (d.type==='trendline'||d.type==='ray'||d.type==='channel') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1);
      if (x1!=null&&y1!=null) { cx=Math.max(12,x1+12); cy=Math.max(10,y1-12); }
    } else if (d.type==='triangle') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1);
      if (x1!=null&&y1!=null) { cx=Math.max(12,x1+12); cy=Math.max(10,y1-12); }
    } else if (d.type==='vp') {
      // Place X just to the right of the rightmost anchor circle at y=18
      const x1=timeToX(d.time1),x2=timeToX(d.time2);
      if (x1!=null&&x2!=null) { cx=Math.min(_dw-12,Math.max(x1,x2)+12); cy=26; }
    } else {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1!=null&&y1!=null&&x2!=null&&y2!=null) {
        cx=Math.min(_dw-12,Math.max(x1,x2)+10);
        cy=Math.max(10,Math.min(y1,y2)-10);
      }
    }
    if (cx==null||cy==null) { hoverXPos=null; return; }
    hoverXPos={cx,cy};
    const s=2;
    ctx.save(); ctx.globalAlpha=0.85; ctx.strokeStyle='#f23645'; ctx.lineWidth=1.2;
    ctx.lineCap='round';
    ctx.fillStyle='rgba(28,32,48,0.88)'; ctx.beginPath(); ctx.arc(cx,cy,7,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.moveTo(cx-s,cy-s); ctx.lineTo(cx+s,cy+s); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx+s,cy-s); ctx.lineTo(cx-s,cy+s); ctx.stroke();
    ctx.restore();
  }

  function renderShape(ctx,d,isPreview,highlight) {
    if (d.visible===false) return;
    ctx.save();
    ctx.globalAlpha=isPreview?0.6:(d.opacity!=null?d.opacity:1);
    const col=highlight?'#fff':(d.color||TOOL_COLORS[d.type]||'#9598a1');
    const lw=(highlight?1.6:1)*dv(d,'lineWidth',1.5);

    if (d.type==='hline') {
      const y=priceToY(d.price); if (y==null){ctx.restore();return;}
      if (d.opacity!=null) ctx.globalAlpha=isPreview?d.opacity*0.6:d.opacity;
      ctx.strokeStyle=col; ctx.lineWidth=lw; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(_dw,y); ctx.stroke();
      const txt=dv(d,'text',''), fs=dv(d,'fontSize',9);
      const full=txt||d.price.toFixed(5);
      const _lpos=d.labelPos||'top-left';
      const _lLeft=_lpos.endsWith('left');
      const lx=_lLeft?4:_dw-4;
      const ly=_lpos.startsWith('top')?y-2:y+fs+4;
      _lbl(ctx,full,lx,ly,col,_lLeft?'left':'right',fs);
    }

    else if (d.type==='dot') {
      const x=timeToX(d.time),y=priceToY(d.price);
      if (x==null||y==null){ctx.restore();return;}
      const r=dv(d,'radius',3);
      ctx.globalAlpha=isPreview?0.35:(d.opacity!=null?d.opacity:0.65);
      ctx.fillStyle='#ffffff'; ctx.strokeStyle='rgba(255,255,255,0.4)'; ctx.lineWidth=1;
      ctx.shadowColor='#ffffff'; ctx.shadowBlur=4;
      ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2); ctx.fill(); ctx.stroke();
    }

    else if (d.type==='ray') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      const W=_dw,H=_dh,dx=x2-x1,dy=y2-y1;
      let tM=1e9;
      if (dx>0) tM=Math.min(tM,(W-x1)/dx); else if (dx<0) tM=Math.min(tM,-x1/dx);
      if (dy>0) tM=Math.min(tM,(H-y1)/dy); else if (dy<0) tM=Math.min(tM,-y1/dy);
      ctx.strokeStyle=col; ctx.lineWidth=lw; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x1+dx*Math.max(0,tM),y1+dy*Math.max(0,tM)); ctx.stroke();
      // EP1 — origin dot
      const _r1=highlight?(hoverEP===1?6:4):3;
      ctx.fillStyle=highlight&&hoverEP===1?'#fff':col;
      ctx.beginPath(); ctx.arc(x1,y1,_r1,0,Math.PI*2); ctx.fill();
      // EP2 — direction anchor dot (small, ghosted unless hovered)
      const _r2=highlight?(hoverEP===2?6:4):3;
      ctx.save(); ctx.globalAlpha=highlight&&hoverEP===2?1:0.45;
      ctx.fillStyle=highlight&&hoverEP===2?'#fff':col;
      ctx.beginPath(); ctx.arc(x2,y2,_r2,0,Math.PI*2); ctx.fill(); ctx.restore();
      const txt=dv(d,'text','');
      if (txt) {
        const _rpos=d.labelPos||'top-right', _rLeft=_rpos.endsWith('left'), fs=dv(d,'fontSize',9);
        const _rx=_rLeft?x1:x1+dx*Math.max(0,tM);
        const _ry=_rLeft?y1:y1+dy*Math.max(0,tM);
        _lbl(ctx,txt,_rx,_ry,col,_rLeft?'left':'right',fs);
      }
    }

    else if (d.type==='trendline') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      ctx.strokeStyle=col; ctx.lineWidth=lw; ctx.setLineDash(d.dashed ? [6,4] : []);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
      ctx.setLineDash([]);
      [[x1,y1,1],[x2,y2,2]].forEach(([ax,ay,ep])=>{
        const r=highlight?(hoverEP===ep?6:4):3;
        ctx.fillStyle=highlight&&hoverEP===ep?'#fff':col;
        ctx.beginPath(); ctx.arc(ax,ay,r,0,Math.PI*2); ctx.fill();
      });
      const txt=dv(d,'text',''), fs=dv(d,'fontSize',9);
      if (txt) {
        const _tpos=d.labelPos||'top-right', _tLeft=_tpos.endsWith('left');
        const _useP1=_tLeft?(x1<=x2):(x1>=x2);
        const _tlx=_useP1?x1:x2, _tly=_useP1?y1:y2;
        _lbl(ctx,txt,_tlx,_tly,col,_tLeft?'left':'right',fs);
      }
    }

    else if (d.type==='rect') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      const fo=dv(d,'fillOpacity',0.12);
      ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*fo; ctx.fillStyle=col; ctx.fillRect(x1,y1,x2-x1,y2-y1); ctx.restore();
      if (lw>0){ctx.strokeStyle=col; ctx.lineWidth=lw; ctx.setLineDash([]); ctx.strokeRect(x1,y1,x2-x1,y2-y1);}
      // 4 corner handles
      if (highlight) {
        [[x1,y1,1],[x2,y2,2],[x1,y2,3],[x2,y1,4]].forEach(([ax,ay,ep])=>{
          const hot=hoverEP===ep;
          ctx.save(); ctx.fillStyle=hot?'#fff':col; ctx.strokeStyle=col; ctx.lineWidth=1; ctx.globalAlpha=hot?1:0.6;
          ctx.beginPath(); ctx.arc(ax,ay,hot?5:3,0,Math.PI*2); ctx.fill(); ctx.stroke(); ctx.restore();
        });
      }
      const txt=dv(d,'text',''), fs=dv(d,'fontSize',9);
      if (txt) {
        const _rctpos=d.labelPos||'top-left', _rctLeft=_rctpos.endsWith('left'), _rctTop=_rctpos.startsWith('top');
        const _rbx=_rctLeft?Math.min(x1,x2)+4:Math.max(x1,x2)-4;
        const _rby=_rctTop?Math.min(y1,y2)+fs+2:Math.max(y1,y2)-4;
        _lbl(ctx,txt,_rbx,_rby,col,_rctLeft?'left':'right',fs);
      }
    }

    else if (d.type==='fib') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      const pr=d.price1-d.price2, xL=Math.min(x1,x2), xR=_dw;
      const gpHY=priceToY(d.price1-pr*0.618), gpLY=priceToY(d.price1-pr*0.65);
      if (gpHY!=null&&gpLY!=null) {
        const top=Math.min(gpHY,gpLY),bot=Math.max(gpHY,gpLY);
        ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*.15; ctx.fillStyle='#d4af37';
        ctx.fillRect(xL,top,xR-xL,bot-top); ctx.restore();
        ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*.85;
        ctx.font='8px "Share Tech Mono",monospace'; ctx.fillStyle='#d4af37';
        ctx.textAlign='center'; ctx.textBaseline='middle';
        ctx.fillText('GOLDEN POCKET',xL+(xR-xL)*.5,(top+bot)*.5); ctx.restore();
      }
      for (const {l,lb} of [{l:.5,lb:'0.5'},{l:.786,lb:'0.786'}]) {
        const fp=d.price1-pr*l, fy=priceToY(fp); if (fy==null) continue;
        ctx.strokeStyle='#5d6068'; ctx.lineWidth=1; ctx.setLineDash([5,4]);
        ctx.beginPath(); ctx.moveTo(xL,fy); ctx.lineTo(xR,fy); ctx.stroke(); ctx.setLineDash([]);
        _lbl(ctx,`${lb}  ${fp.toFixed(5)}`,xR-4,fy,'#5d6068','right',9);
      }
      ctx.setLineDash([]);
      // Anchor dots — 75% smaller than original, ghosted unless hovered directly
      for (const [ax,ay,ep] of [[x1,y1,1],[x2,y2,2]]) {
        const hot=highlight&&hoverEP===ep;
        const r=hot?2:1.5;
        ctx.save();
        ctx.globalAlpha=hot?1:0.57;
        ctx.fillStyle='#fff'; ctx.strokeStyle=hot?'#fff':col; ctx.lineWidth=1;
        ctx.beginPath(); ctx.arc(ax,ay,r,0,Math.PI*2); ctx.fill(); ctx.stroke();
        ctx.restore();
      }
      const _ftxt=dv(d,'text',''), _ffs=dv(d,'fontSize',9);
      if (_ftxt) {
        const _fpos=d.labelPos||'top-left', _fLeft=_fpos.endsWith('left'), _fTop=_fpos.startsWith('top');
        const _fbx=_fLeft?xL+4:xR-4;
        const _fby=_fTop?Math.min(y1,y2)+_ffs+2:Math.max(y1,y2)-4;
        _lbl(ctx,_ftxt,_fbx,_fby,col,_fLeft?'left':'right',_ffs);
      }
    }

    else if (d.type==='triangle') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1);
      const x2=timeToX(d.time2),y2=priceToY(d.price2);
      const x3=timeToX(d.time3),y3=priceToY(d.price3);
      if (x1==null||y1==null||x2==null||y2==null||x3==null||y3==null){ctx.restore();return;}
      ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*0.03; ctx.fillStyle=col;
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.lineTo(x3,y3); ctx.closePath(); ctx.fill(); ctx.restore();
      ctx.strokeStyle=col; ctx.lineWidth=lw*0.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.lineTo(x3,y3); ctx.closePath(); ctx.stroke();
      [[x1,y1,1],[x2,y2,2],[x3,y3,3]].forEach(([ax,ay,ep])=>{
        const r=highlight?(hoverEP===ep?6:3):3;
        ctx.fillStyle=highlight&&hoverEP===ep?'#fff':col;
        ctx.beginPath(); ctx.arc(ax,ay,r,0,Math.PI*2); ctx.fill();
      });
    }

    else if (d.type==='channel') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      const y1b=priceToY(d.price1+d.priceOffset),y2b=priceToY(d.price2+d.priceOffset);
      if (y1b!=null&&y2b!=null) {
        ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*.06; ctx.fillStyle=col;
        ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.lineTo(x2,y2b); ctx.lineTo(x1,y1b); ctx.closePath(); ctx.fill(); ctx.restore();
        ctx.strokeStyle=col; ctx.lineWidth=lw*0.5; ctx.setLineDash([4,3]);
        ctx.beginPath(); ctx.moveTo(x1,y1b); ctx.lineTo(x2,y2b); ctx.stroke();
        ctx.strokeStyle=col; ctx.lineWidth=Math.max(0.5,lw*.28); ctx.setLineDash([2,5]);
        ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*.33;
        ctx.beginPath(); ctx.moveTo(x1,(y1+y1b)/2); ctx.lineTo(x2,(y2+y2b)/2); ctx.stroke(); ctx.restore();
      }
      ctx.strokeStyle=col; ctx.lineWidth=lw*0.5; ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
      // Top anchor dots (ep1/ep2)
      [[x1,y1,1],[x2,y2,2]].forEach(([ax,ay,ep])=>{
        const hot=highlight&&hoverEP===ep;
        ctx.save(); ctx.fillStyle=hot?'#fff':col; ctx.strokeStyle=col; ctx.lineWidth=1.5; ctx.globalAlpha=hot?1:0.7;
        ctx.beginPath(); ctx.arc(ax,ay,hot?6:3,0,Math.PI*2); ctx.fill(); ctx.stroke(); ctx.restore();
      });
      // Bottom anchor: single midpoint dot (ep3)
      if (y1b!=null&&y2b!=null) {
        const xM=(x1+x2)/2, yMb=(y1b+y2b)/2, hot=highlight&&hoverEP===3;
        ctx.save(); ctx.fillStyle=hot?'#fff':col; ctx.strokeStyle=col; ctx.lineWidth=1.5; ctx.globalAlpha=hot?1:0.7;
        ctx.beginPath(); ctx.arc(xM,yMb,hot?6:3,0,Math.PI*2); ctx.fill(); ctx.stroke(); ctx.restore();
      }
      const txt=dv(d,'text','');
      if (txt) {
        const _cpos=d.labelPos||'top-left', _cLeft=_cpos.endsWith('left'), _cTop=_cpos.startsWith('top'), _cfs=dv(d,'fontSize',9);
        const _allY=[y1,y2]; if(y1b!=null)_allY.push(y1b); if(y2b!=null)_allY.push(y2b);
        const _cbx=_cLeft?Math.min(x1,x2)+4:Math.max(x1,x2)-4;
        const _cby=_cTop?Math.min(..._allY)+_cfs+2:Math.max(..._allY)-4;
        _lbl(ctx,txt,_cbx,_cby,col,_cLeft?'left':'right',_cfs);
      }
    }

    else if (d.type==='vp') {
      const x1=timeToX(d.time1),x2=timeToX(d.time2);
      if (x1==null||x2==null){ctx.restore();return;}
      const vp=computeVP(d.time1,d.time2,40); if (!vp){ctx.restore();return;}
      const xAnchorL=Math.min(x1,x2), xAnchorR=Math.max(x1,x2);
      const xL=Math.max(0,xAnchorL), xR=Math.min(_dw,xAnchorR);
      const maxV=Math.max(...vp.vols,1), maxW=(xR-xL)*.38;
      const pocI=vp.vols.reduce((b,v,i)=>v>vp.vols[b]?i:b,0);
      const vpCol=d.color||'#2962ff'; // always use original color — highlight must not turn bars white
      vp.vols.forEach((v,i)=>{
        const pr=vp.pMin+(i+.5)*vp.bs, cy=priceToY(pr); if (cy==null) return;
        const bH=Math.max(1,Math.abs((priceToY(vp.pMin+i*vp.bs)||cy)-(priceToY(vp.pMin+(i+1)*vp.bs)||cy)));
        ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*(i===pocI?.85:.4)*0.25;
        ctx.fillStyle=i===pocI?'#f0b429':'#1a3a8a';
        ctx.fillRect(xL,cy-bH/2,(v/maxV)*maxW,bH); ctx.restore();
      });
      const pocPr=vp.pMin+(pocI+.5)*vp.bs, pocY=priceToY(pocPr);
      if (pocY!=null) {
        ctx.save(); ctx.globalAlpha=0.25; ctx.strokeStyle='#f23645'; ctx.lineWidth=0.5; ctx.setLineDash([4,3]);
        ctx.beginPath(); ctx.moveTo(xL,pocY); ctx.lineTo(xR,pocY); ctx.stroke(); ctx.setLineDash([]);
        _lbl(ctx,`POC ${pocPr.toFixed(5)}`,xL+4,pocY,'#f23645','left',9); ctx.restore();
      }
      // VAH / VAL — pink solid lines at 30% opacity
      ctx.save(); ctx.globalAlpha=0.3; ctx.strokeStyle='#e879a0'; ctx.lineWidth=0.5;
      const vahY=priceToY(vp.vah), valY=priceToY(vp.val);
      if (vahY!=null){ctx.beginPath();ctx.moveTo(xL,vahY);ctx.lineTo(xR,vahY);ctx.stroke();_lbl(ctx,`VAH ${vp.vah.toFixed(5)}`,xL+4,vahY,'#e879a0','left',8);}
      if (valY!=null){ctx.beginPath();ctx.moveTo(xL,valY);ctx.lineTo(xR,valY);ctx.stroke();_lbl(ctx,`VAL ${vp.val.toFixed(5)}`,xL+4,valY,'#e879a0','left',8);}
      ctx.restore();
      // Faint vertical origin lines (5% opacity) — shows where the anchor boundaries are
      const _vpAnchorY=18;
      ctx.save(); ctx.globalAlpha=0.25; ctx.strokeStyle=vpCol; ctx.lineWidth=1; ctx.setLineDash([]);
      if (xAnchorL>=0&&xAnchorL<=_dw){ctx.beginPath();ctx.moveTo(xAnchorL,_vpAnchorY);ctx.lineTo(xAnchorL,_dh);ctx.stroke();}
      if (xAnchorR>=0&&xAnchorR<=_dw){ctx.beginPath();ctx.moveTo(xAnchorR,_vpAnchorY);ctx.lineTo(xAnchorR,_dh);ctx.stroke();}
      ctx.restore();
      // Anchor circles at the top (65% smaller — hot:2, normal:1.5)
      [xAnchorL,xAnchorR].forEach((ax,i)=>{
        if (ax<0||ax>_dw) return;
        const hot=highlight&&hoverEP===(i===0?1:2);
        ctx.save(); ctx.fillStyle=hot?'#fff':vpCol; ctx.strokeStyle=vpCol; ctx.lineWidth=1;
        ctx.globalAlpha=hot?1:0.7;
        ctx.beginPath(); ctx.arc(ax,_vpAnchorY,hot?2:1.5,0,Math.PI*2); ctx.fill(); ctx.stroke();
        ctx.restore();
      });
    }

    else if (d.type==='measure') {
      const x1=timeToX(d.time1),y1=priceToY(d.price1),x2=timeToX(d.time2),y2=priceToY(d.price2);
      if (x1==null||y1==null||x2==null||y2==null){ctx.restore();return;}
      const pd=d.price2-d.price1, up=pd>=0, ac=up?'#089981':'#f23645';
      const pct=(pd/Math.abs(d.price1)*100).toFixed(2);
      const bars=countBars(d.time1,d.time2), td=fmtTime(d.time2-d.time1);
      ctx.save(); ctx.globalAlpha=(isPreview?.6:1)*.18; ctx.fillStyle=ac;
      ctx.fillRect(x1,Math.min(y1,y2),x2-x1,Math.abs(y2-y1)); ctx.restore();
      ctx.strokeStyle=ac; ctx.lineWidth=lw; ctx.setLineDash([3,3]);
      ctx.strokeRect(x1,Math.min(y1,y2),x2-x1,Math.abs(y2-y1)); ctx.setLineDash([]);
      ctx.font='9px "Share Tech Mono",monospace'; ctx.fillStyle=ac;
      ctx.textAlign='center'; ctx.textBaseline='top';
      ctx.fillText(`${bars} bars · ${td}`,(x1+x2)/2,Math.max(y1,y2)+3);
      ctx.textAlign='left'; ctx.textBaseline='middle';
      ctx.fillText(`${up?'+':''}${pd.toFixed(5)} (${up?'+':''}${pct}%)`,Math.max(x1,x2)+4,(y1+y2)/2);
    }

    else if (d.type==='pencil') {
      if (!d.points||d.points.length<2){ctx.restore();return;}
      ctx.strokeStyle=col; ctx.lineWidth=lw; ctx.setLineDash([]);
      ctx.lineCap='round'; ctx.lineJoin='round';
      ctx.beginPath();
      let started=false;
      for (const pt of d.points) {
        const x=timeToX(pt.time),y=priceToY(pt.price);
        if (x==null||y==null){started=false;continue;}
        if (!started){ctx.moveTo(x,y);started=true;}
        else ctx.lineTo(x,y);
      }
      ctx.stroke();
    }

    ctx.restore();
  }

  function _lbl(ctx,txt,x,y,col,align,fs) {
    ctx.save(); ctx.font=`${fs||9}px "Share Tech Mono",monospace`;
    ctx.fillStyle=col; ctx.textAlign=align||'left'; ctx.textBaseline='bottom';
    ctx.fillText(txt,x,y-2); ctx.restore();
  }

  let _rafDirty = true;
  function markDirty() { _rafDirty = true; }
  chart.timeScale().subscribeVisibleTimeRangeChange(markDirty);
  chart.timeScale().subscribeVisibleLogicalRangeChange(markDirty);
  chart.subscribeCrosshairMove(markDirty);
  (function raf(){
    if (_rafDirty) {
      try { redrawAll(); } catch(e) { console.error('[redrawAll]', e); }
      try { rsiRedrawAll(); } catch(e) { console.error('[rsiRedrawAll]', e); }
      _rafDirty = false;
    }
    requestAnimationFrame(raf);
  })();

  /* ---- Pointer-events ---- */
  function setCap(on,cur) {
    drawCanvas.style.pointerEvents=on?'all':'none';
    drawCanvas.style.cursor=cur||'default';
  }

  /* ---- Tool buttons ---- */
  const drawToolBtns = document.querySelectorAll('.drawTool');
  function deactivateTool() {
    pencilDrawing=false; pencilPoints=[];
    activeTool=null; clickCount=0; firstClickPos=null; triangleP2=null; previewShape=null;
    channelStep=0; channelBase=null; magnetDot=null; cursorPos=null;
    rsiFirstAnchor=null; rsiRedrawAll();
    drawToolBtns.forEach(b=>b.classList.remove('active'));
    setCap(false);
  }
  drawToolBtns.forEach(btn=>{
    btn.addEventListener('click',()=>{
      const t=btn.dataset.tool;
      if (activeTool===t){deactivateTool();return;}
      activeTool=t; clickCount=0; firstClickPos=null; triangleP2=null; previewShape=null; channelStep=0; channelBase=null;
      drawToolBtns.forEach(b=>b.classList.toggle('active',b.dataset.tool===t));
      setCap(true,'crosshair');
    });
  });

  const magnetToggle = document.getElementById('magnetToggle');
  magnetToggle.classList.add('active');
  magnetToggle.addEventListener('click',()=>{
    magnetOn=!magnetOn; magnetToggle.classList.toggle('active',magnetOn);
  });

  document.getElementById('clearDrawings').addEventListener('click',()=>{
    drawings=[];saveDrawings();previewShape=null;updateObjTree();
  });

  /* ===== COMMAND PALETTE ===== */
  (function(){
    const overlay = document.getElementById('cmdPaletteOverlay');
    const palette = document.getElementById('cmdPalette');
    const cmdInput = document.getElementById('cmdInput');
    const cmdResults = document.getElementById('cmdResults');
    let selectedIdx = 0;
    let filteredItems = [];

    const ACTIONS = [
      { group:'Tabs', label:'Trades',     sub:'View Chev trade ideas',   kbd:'1',   icon:'emoji/fire.png',    action:()=>clickTab('trades') },
      { group:'Tabs', label:'Engine',     sub:'Run Dexter analysis',     kbd:'2',   icon:'emoji/tools.png',   action:()=>clickTab('engine') },
      { group:'Tabs', label:'Layers',     sub:'Drawing layers & objects', kbd:'3',   icon:'',                  action:()=>clickTab('layers') },
      { group:'Tabs', label:'Chat',       sub:'Ask Chev anything',       kbd:'4',   icon:'emoji/call.png',    action:()=>clickTab('chat') },
      { group:'Tabs', label:'Radar',      sub:'Chev\'s read on all symbols', kbd:'5', icon:'',                  action:()=>clickTab('radar') },
      { group:'Tools', label:'Run Engine',sub:'Analyze current chart',   kbd:'R',   icon:'emoji/tools.png',   action:()=>{ closePalette(); runDexterEngine?.(); } },
      { group:'Tools', label:'Undo',      sub:'Undo last drawing',       kbd:'⌘Z',  icon:'',                  action:()=>{ closePalette(); if(undoStack?.length){drawings=undoStack.pop();saveDrawings?.();updateObjTree?.();} } },
      { group:'Tools', label:'Clear Drawings', sub:'Remove all drawings',kbd:'',   icon:'',                  action:()=>{ closePalette(); if(confirm('Clear all drawings?')){drawings=[];saveDrawings?.();updateObjTree?.();redrawAll?.();} } },
      { group:'TF', label:'15m',          sub:'Switch to 15 minute',     kbd:'',    icon:'',                  action:()=>{ closePalette(); document.querySelector('[data-tf="15m"]')?.click(); } },
      { group:'TF', label:'1h',           sub:'Switch to 1 hour',        kbd:'',    icon:'',                  action:()=>{ closePalette(); document.querySelector('[data-tf="1h"]')?.click(); } },
      { group:'TF', label:'4h',           sub:'Switch to 4 hour',        kbd:'',    icon:'',                  action:()=>{ closePalette(); document.querySelector('[data-tf="4h"]')?.click(); } },
      { group:'TF', label:'1d',           sub:'Switch to 1 day',         kbd:'',    icon:'',                  action:()=>{ closePalette(); document.querySelector('[data-tf="1d"]')?.click(); } },
    ];

    function clickTab(name) {
      closePalette();
      const t = document.querySelector(`#intelTabBar [data-tab="${name}"]`);
      if (t) t.click();
    }

    function buildSymbolActions() {
      const syms = [...document.querySelectorAll('.watchRow')].map(r=>r.dataset.symbol).filter(Boolean);
      return syms.map(s => ({
        group:'Symbols', label:s, sub:'Go to symbol', kbd:'', icon:'',
        action:()=>{ closePalette(); loadSymbol?.(s); }
      }));
    }

    function render(q='') {
      const all = [...buildSymbolActions(), ...ACTIONS];
      filteredItems = q ? all.filter(i => (i.label+i.sub).toLowerCase().includes(q.toLowerCase())) : all;
      selectedIdx = 0;

      const groups = {};
      filteredItems.forEach(item => {
        if (!groups[item.group]) groups[item.group] = [];
        groups[item.group].push(item);
      });

      cmdResults.innerHTML = Object.entries(groups).map(([grp, items]) => `
        <div class="cmdGroup">${grp}</div>
        ${items.map((item, _) => {
          const globalIdx = filteredItems.indexOf(item);
          const iconHtml = item.icon ? `<img src="${item.icon}" alt="">` : `<span style="font-size:13px;color:var(--txt3)">›</span>`;
          return `<div class="cmdItem${globalIdx===0?' selected':''}" data-idx="${globalIdx}">
            <span class="cmdIcon">${iconHtml}</span>
            <span class="cmdLabel">${item.label}<br><span class="cmdSub">${item.sub}</span></span>
            ${item.kbd ? `<span class="cmdKbd">${item.kbd}</span>` : ''}
          </div>`;
        }).join('')}
      `).join('');

      cmdResults.querySelectorAll('.cmdItem').forEach(el => {
        el.addEventListener('click', () => { filteredItems[+el.dataset.idx]?.action(); });
        el.addEventListener('mouseenter', () => { setSelected(+el.dataset.idx); });
      });
    }

    function setSelected(idx) {
      selectedIdx = idx;
      cmdResults.querySelectorAll('.cmdItem').forEach(el => {
        el.classList.toggle('selected', +el.dataset.idx === idx);
      });
      const sel = cmdResults.querySelector('.cmdItem.selected');
      if (sel) sel.scrollIntoView({ block:'nearest' });
    }

    function openPalette() {
      overlay.classList.add('open');
      cmdInput.value = '';
      render('');
      cmdInput.focus();
    }

    function closePalette() {
      overlay.classList.remove('open');
    }

    cmdInput.addEventListener('input', () => render(cmdInput.value));
    cmdInput.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSelected(Math.min(selectedIdx+1, filteredItems.length-1)); }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setSelected(Math.max(selectedIdx-1, 0)); }
      if (e.key === 'Enter')     { e.preventDefault(); filteredItems[selectedIdx]?.action(); }
      if (e.key === 'Escape')    { closePalette(); }
    });
    overlay.addEventListener('click', e => { if (e.target === overlay) closePalette(); });

    window._openCmdPalette = openPalette;
    window._closeCmdPalette = closePalette;
  })();

  /* ===== STRATEGY DASHBOARD (added 2026-07-05) ===== */
  (function() {
    const overlay = document.getElementById('strategyOverlay');
    let refreshTimer = null;
    let feedFilter = 'ALL';
    let equityChart = null, equitySeries = null;

    const EMOJI = {
      POST: 'emoji/fire.png', SKIP: 'emoji/lets-see.png',
      REJECT: 'emoji/wrench.png', GATE_REJECT: 'emoji/wrench.png',
      STRUCT_REJECT: 'emoji/wrench.png', MTF_TAX_REJECT: 'emoji/wrench.png',
      GEOMETRY_REJECT: 'emoji/wrench.png', FORMAT_ERROR: 'emoji/oh-no.png',
    };
    function emojiFor(dec) { return EMOJI[dec] || 'emoji/lets-see.png'; }
    function cardClassFor(dec) {
      if (dec === 'POST') return 'isPost';
      if (dec === 'SKIP') return 'isSkip';
      return 'isReject';
    }
    function winRateColor(pct) {
      // 0% -> red, 50% -> gold-ish, 100% -> green. Simple 2-stop lerp around 50.
      if (pct >= 50) {
        const t = Math.min((pct - 50) / 50, 1);
        return `rgb(${Math.round(212 - t*204)}, ${Math.round(175 + t*(153-175))}, ${Math.round(55 + t*(129-55))})`;
      }
      const t = Math.min((50 - pct) / 50, 1);
      return `rgb(${Math.round(212 + t*(242-212))}, ${Math.round(175 - t*(175-54))}, ${Math.round(55 + t*(69-55))})`;
    }
    let _tagStatsCache = null;
    async function _ensureTagStats() {
      if (_tagStatsCache) return _tagStatsCache;
      try {
        const r = await _apiFetch('/api/strategy/performance');
        const d = await r.json();
        _tagStatsCache = d.tag_stats || {};
      } catch(e) { _tagStatsCache = {}; }
      return _tagStatsCache;
    }
    function fmtTs(ts) {
      if (!ts) return '';
      return ts.replace(' ', ' · ').slice(5); // drop year, MM-DD · HH:MM:SS
    }
    function esc(s) { return (s == null ? '' : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

    async function openStrategy() {
      overlay.classList.add('open');
      await loadAll();
      _markFresh('strategy');
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = setInterval(() => loadAll().then(() => _markFresh('strategy')), 25000);
    }
    function closeStrategy() {
      overlay.classList.remove('open');
      if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    }

    async function loadAll() {
      await Promise.all([loadMode(), loadFeed(), loadFunnel(), loadPerformance(), loadHeatmap(), loadShadow(), loadTimeseries(), loadScoreboard()]);
      // Runs only after BOTH loadFunnel (renders the bars) and loadShadow (populates
      // _shadowBucketsCache) have settled -- no race between the two, regardless of which
      // resolves first.
      applyShadowSplitsToReasonBars();
      // PHASE 7B-4: re-render the feed once more now that _shadowResolvedItemsCache is
      // guaranteed populated (loadFeed and loadShadow ran concurrently above, so the very
      // first paint this cycle may have missed badges -- renderFeed is a cheap, idempotent,
      // pure-DOM-from-globals function, safe to call twice).
      renderFeed();
    }

    async function loadMode() {
      try {
        const r = await _apiFetch('/api/strategy/mode');
        const d = await r.json();
        const badge = document.getElementById('stratModeBadge');
        if (d.exploration_mode) {
          badge.textContent = `⚡ EXPLORATION — R:R ${d.profile.MIN_NET_RR.day}:1 day · heat cap ${d.profile.MAX_TOTAL_HEAT}%`;
        } else {
          badge.textContent = `🔒 NORMAL — R:R ${d.profile.MIN_NET_RR.day}:1 day · heat cap ${d.profile.MAX_TOTAL_HEAT}%`;
        }
      } catch (e) { console.warn('[Strategy] mode load failed', e); }
    }

    async function toggleExploration() {
      const badge = document.getElementById('stratModeBadge');
      const goingTo = badge.textContent.includes('EXPLORATION') ? 'NORMAL (strict, real-money-appropriate)' : 'EXPLORATION (loose, paper data-collection)';
      if (!confirm(`Flip live risk posture to ${goingTo}?\n\nThis changes ATR floor, cost gate, R:R, heat/correlation/concurrency caps immediately for every trade Dexter judges from now on — not just cosmetic.`)) return;
      try {
        const r = await _apiFetch('/api/strategy/toggle_exploration', { method: 'POST' });
        const d = await r.json();
        badge.textContent = d.exploration_mode
          ? `⚡ EXPLORATION — R:R ${d.profile.MIN_NET_RR.day}:1 day · heat cap ${d.profile.MAX_TOTAL_HEAT}%`
          : `🔒 NORMAL — R:R ${d.profile.MIN_NET_RR.day}:1 day · heat cap ${d.profile.MAX_TOTAL_HEAT}%`;
      } catch (e) { console.warn('[Strategy] toggle failed', e); alert('Toggle failed — check Dexter is running.'); }
    }

    let _lastFeedData = [];
    async function loadFeed() {
      try {
        const r = await _apiFetch('/api/strategy/feed?hours=48&limit=400');
        const d = await r.json();
        _lastFeedData = d.decisions || [];
        renderQuietAlert(_lastFeedData);
        renderFeed();
        renderRawTable(_lastFeedData);
      } catch (e) { console.warn('[Strategy] feed load failed', e); }
    }

    function renderQuietAlert(decisions) {
      const alertEl = document.getElementById('stratQuietAlert');
      const now = Date.now();
      const posts = decisions.filter(d => d.decision === 'POST');
      const lastPostTs = posts.length ? new Date(posts[0].ts.replace(' ', 'T')).getTime() : null;
      const hoursSincePost = lastPostTs ? (now - lastPostTs) / 3600000 : null;
      if (hoursSincePost === null || hoursSincePost > 12) {
        document.getElementById('stratQuietText').textContent =
          hoursSincePost === null
            ? "No POSTs in the last 48 hours of logs — could be genuinely quiet conditions, could be worth checking the live gates."
            : `${hoursSincePost.toFixed(1)}h since Chev's last POST — check whether that's a quiet market or a stuck gate.`;
        alertEl.classList.add('show');
      } else {
        alertEl.classList.remove('show');
      }
    }

    // ── PHASE 7B-4: feed-card shadow badge matching ───────────────────────────
    // Pure, side-effect-free, testable standalone (paste-run in the browser console --
    // see handoff.txt PHASE 7B-4 section for the exact fixture arrays used to verify
    // this before it was wired into renderFeed()).

    // chev_decisions.jsonl uses "YYYY-MM-DD HH:MM:SS" (implicit UTC, no T/Z);
    // resolved_items (sourced from labels_closed.jsonl via labeller's _now_iso()) uses
    // "YYYY-MM-DDTHH:MM:SSZ" (explicit UTC). Normalizes both to epoch millis.
    function _shadowParseTs(ts) {
      if (!ts) return NaN;
      const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';
      return Date.parse(iso);
    }

    // Longest EXPIRY_HOURS (swing=48h) plus margin -- inside this window since the skip,
    // "no match yet" is a timing fact (the shadow barrier walk hasn't had time to resolve),
    // not a guessed outcome. Outside it with still no match, stay silent (silence over
    // guesses) rather than imply an outcome that may have been NO_FILL/VOID/dropped.
    const SHADOW_UNRESOLVED_GRACE_MS = 48 * 3600 * 1000;

    // Matches one feed record (a SKIP decision, {symbol, tf, ts, ...}) to a resolved_items
    // entry by symbol + tf + timestamp within +/-5 minutes (closest match wins). Returns
    // {kind:'dodged', shadow_r} / {kind:'missed', shadow_r} / {kind:'unresolved'} / null.
    function shadowBadgeForSkip(d, resolvedItems, nowMs) {
      nowMs = (nowMs != null) ? nowMs : Date.now();
      const dTs = _shadowParseTs(d.ts);
      if (isNaN(dTs)) return null;
      const FIVE_MIN = 5 * 60 * 1000;
      let best = null, bestDelta = Infinity;
      (resolvedItems || []).forEach(item => {
        if (item.symbol !== d.symbol || item.tf !== d.tf) return;
        const itemTs = _shadowParseTs(item.ts);
        if (isNaN(itemTs)) return;
        const delta = Math.abs(itemTs - dTs);
        if (delta <= FIVE_MIN && delta < bestDelta) { best = item; bestDelta = delta; }
      });
      if (best) {
        return best.verdict === 'right'
          ? { kind: 'dodged', shadow_r: best.shadow_r }
          : { kind: 'missed', shadow_r: best.shadow_r };
      }
      if ((nowMs - dTs) < SHADOW_UNRESOLVED_GRACE_MS) return { kind: 'unresolved' };
      return null;   // silence over guesses
    }

    function _shadowBadgeHtml(badge) {
      if (badge.kind === 'dodged') {
        return `<span class="stratShadowBadge dodged" title="Shadow outcome: this skip would have LOST ${Math.abs(badge.shadow_r).toFixed(2)}R net — correctly dodged.">✓ dodged ${badge.shadow_r.toFixed(1)}R</span>`;
      }
      if (badge.kind === 'missed') {
        return `<span class="stratShadowBadge missed" title="Shadow outcome: this skip would have WON +${badge.shadow_r.toFixed(2)}R net — real edge forfeited.">✗ missed +${badge.shadow_r.toFixed(1)}R</span>`;
      }
      return `<span class="stratShadowBadge pending" title="Shadow outcome not resolved yet.">⏳ unresolved</span>`;
    }

    function renderFeed() {
      const list = document.getElementById('stratFeedList');
      const filtered = _lastFeedData.filter(d => {
        if (feedFilter === 'ALL') return true;
        if (feedFilter === 'REJECT') return !['POST','SKIP'].includes(d.decision);
        return d.decision === feedFilter;
      }).slice(0, 150);
      if (!filtered.length) { list.innerHTML = '<div class="stratEmptyState">Nothing here yet.</div>'; return; }
      list.innerHTML = filtered.map(d => {
        const _vagueTag = (d.decision === 'SKIP' || d.decision === 'POST') && d.specific === false
          ? '<span class="stratVagueTag" title="No specific number/level cited">⚠ vague</span>' : '';
        const _detailHtml = d.detail ? `<div class="stratDetail">${esc(d.detail).replace(/\n/g, '<br>')}</div>` : '';
        // PHASE 7B-4: shadow badge on resolved SKIP cards only -- silently absent (no
        // badge at all) if _shadowResolvedItemsCache hasn't loaded yet or no match/grace
        // window applies; re-rendered once more after loadShadow settles (see loadAll).
        const _badge = d.decision === 'SKIP' ? shadowBadgeForSkip(d, _shadowResolvedItemsCache) : null;
        const _badgeHtml = _badge ? _shadowBadgeHtml(_badge) : '';
        return `
        <div class="stratFeedCard ${cardClassFor(d.decision)}">
          <img class="stratEmoji" src="${emojiFor(d.decision)}" alt="" onerror="this.style.display='none'">
          <div class="stratFeedMain">
            <div class="stratFeedTop">
              <span class="stratSym">${esc(d.symbol)}</span>
              <span class="stratTf">${esc(d.tf)}</span>
              <span class="stratDecPill ${esc(d.decision)}" title="${esc(friendlyReason(d.decision))}">${esc(d.decision)}</span>
              <span class="stratScore">score ${d.dexter_score ?? '—'}</span>
              ${_vagueTag}
              ${_badgeHtml}
            </div>
            <div class="stratReason">${esc(d.reason)}</div>
            ${_detailHtml}
            <div class="stratTs">${fmtTs(d.ts)} ${d.regime_4h ? '· ' + esc(d.regime_4h) : ''}</div>
          </div>
        </div>
      `;
      }).join('');
    }

    function renderRawTable(decisions) {
      const body = document.getElementById('stratRawBody');
      body.innerHTML = decisions.slice(0, 500).map(d => `
        <tr>
          <td>${fmtTs(d.ts)}</td><td>${esc(d.symbol)}</td><td>${esc(d.tf)}</td>
          <td>${d.dexter_score ?? ''}</td><td>${esc(d.decision)}</td><td>${esc(d.reason)}</td>
          <td>${esc(d.detail || '')}</td>
        </tr>
      `).join('');
    }

    async function loadFunnel() {
      try {
        const r = await _apiFetch('/api/strategy/funnel?hours=24');
        const d = await r.json();
        const stages = [
          { label: 'Not escalated (pre-gates)', count: d.not_escalated_total, color: 'var(--txt3)' },
          { label: 'Escalated to Chev', count: d.escalated_total, color: 'var(--blue)' },
          { label: '↳ POST', count: d.decisions.POST || 0, color: 'var(--green)' },
          { label: '↳ SKIP', count: d.decisions.SKIP || 0, color: 'var(--red)' },
          { label: '↳ Gate/Gauntlet rejects', count: Object.entries(d.decisions).filter(([k])=>!['POST','SKIP'].includes(k)).reduce((s,[,v])=>s+v,0), color: '#ff8a3d' },
          { label: 'Actually opened', count: d.opened, color: 'var(--gold)' },
        ];
        const max = Math.max(1, ...stages.map(s => s.count));
        document.getElementById('stratFunnel').innerHTML = stages.map(s => `
          <div class="stratFunnelRow">
            <div class="stratFunnelLabel">${s.label}</div>
            <div class="stratFunnelBarWrap"><div class="stratFunnelBar" style="width:${(s.count/max*100).toFixed(1)}%;background:${s.color}"></div></div>
            <div class="stratFunnelCount">${s.count}</div>
          </div>
        `).join('');

        const reasons = { ...d.not_escalated, ...d.decisions };
        delete reasons.POST;
        const sorted = Object.entries(reasons).sort((a,b) => b[1]-a[1]).slice(0, 12);
        const rmax = Math.max(1, ...sorted.map(([,v]) => v));
        document.getElementById('stratReasonBars').innerHTML = sorted.length
          ? sorted.map(([k,v]) => `
              <div class="stratBarRow">
                <div class="stratBarLabel" title="${esc(k)}">${esc(friendlyReason(k))}</div>
                <div class="stratBarTrack"><div class="stratBarFill" style="width:${(v/rmax*100).toFixed(1)}%;background:var(--red)"></div></div>
                <div class="stratBarVal">${v}</div>
              </div>
            `).join('')
          : '<div class="stratEmptyState">No rejections in this window.</div>';
      } catch (e) { console.warn('[Strategy] funnel load failed', e); }
    }

    /* ── PHASE 7B-2: Shadow Outcomes (counterfactual) ──────────────────────────
       Self-contained fetch + render, same 25s poll cadence as every other pane
       (see loadAll/openStrategy). A fetch failure degrades quietly -- never
       throws past its own try/catch, never breaks the rest of the diagnostics pane.
       Read-only: /api/strategy/counterfactual never touches Chev/journal/Sheets/
       Firebase, and this code never reads or writes the REAL performance pane's
       objects/functions (loadPerformance, _tagStatsCache, etc.) -- separate cache,
       separate DOM ids, separate render path throughout. */
    let _shadowBucketsCache = null;
    let _shadowResolvedItemsCache = null;   // PHASE 7B-4: for feed-card badge matching

    // Maps the OLDER /api/strategy/funnel reason keys (a mix of fine-grained
    // NOT_ESCALATED reasons and coarse decision-type names) onto the counterfactual
    // endpoint's bucket keys, so the existing reason bars can be matched without
    // forking either endpoint's data model.
    const SHADOW_DECISION_TO_GATE_KEY = {
      'MTF_TAX_REJECT':  'GATE-MTF_TAX',
      'STRUCT_REJECT':   'GATE-STRUCT_GATE',
      'GATE_REJECT':     'GATE-SCORE_GATE',
      'GEOMETRY_REJECT': 'GATE-GEOMETRY',
    };
    const SHADOW_SKIP_KEYS = ['SKIP - invalidation', 'SKIP - R:R', 'SKIP - other'];
    // Every downstream-kill bucket the bare "REJECT" funnel bar aggregates (the ones
    // NOT already broken out into their own funnel bar via the map above).
    const SHADOW_BARE_REJECT_KEYS = [
      'GATE-COST_GATE', 'GATE-PRICE_DRIFT', 'GATE-CONCURRENCY', 'GATE-HEAT_CAP',
      'GATE-DATA_QUALITY', 'GATE-NET_RR', 'GATE-CORR_CAP', 'GATE-LIQUIDATION',
      'GATE-HALLUCINATED_LEVELS', 'GATE-OTHER',
    ];

    // PHASE 7B-3: shadow-specific trust minimums (deliberately separate from the real
    // leaderboards' own thresholds -- MIN_COMBO_N=5 above, tag filter n>=3 in
    // loadPerformance -- shadow samples come from ~95% of escalations, a much larger
    // pool, so a higher tag minimum is warranted). Keyed on `resolved`, never on `n`
    // (which includes still-open records with no outcome yet).
    const SHADOW_MIN_TAG_N   = 10;
    const SHADOW_MIN_COMBO_N = 5;

    // Shared renderer for both shadow leaderboards — same bar-row visual language as the
    // real Tag/Combo leaderboards (winRateColor, .stratBarRow/.stratBarFill), sorted by
    // total shadow R descending per spec, greyed out below the resolved-n minimum (same
    // opacity/grey-track convention as the real Combo leaderboard). Never reads from or
    // writes to d.tag_stats/d.combo_stats/_tagStatsCache — entirely separate data path.
    function renderShadowLeaderboard(elId, entries, kind, minN) {
      const el = document.getElementById(elId);
      if (!el) return;
      const nameKey = kind === 'tag' ? 'tag' : 'combo';
      const sorted = [...entries].sort((a, b) => b.total_r - a.total_r).slice(0, kind === 'combo' ? 15 : 40);
      if (!sorted.length) {
        el.innerHTML = `<div class="stratEmptyState">No shadow ${kind} data in this window.</div>`;
        return;
      }
      el.innerHTML = sorted.map(s => {
        const trusted = s.resolved >= minN;
        // PHASE 24: plain-English phrasing -- "If taken: won 71% · would've made +8.28R ·
        // from 73 setups" -- same underlying numbers as before (shadow_wr/total_r/resolved),
        // wording only. The win-rate fragment gets its own honest phrase when there's no
        // resolved data yet, rather than a bare "n/a" sitting next to a real R figure.
        const wrFrag = s.shadow_wr != null ? `won ${s.shadow_wr.toFixed(0)}%` : 'win rate not yet known';
        const rFrag  = `would've made ${s.total_r >= 0 ? '+' : ''}${s.total_r.toFixed(2)}R`;
        const nFrag  = `from ${s.resolved} setup${s.resolved === 1 ? '' : 's'}`;
        const barW  = s.shadow_wr != null ? s.shadow_wr.toFixed(1) : 0;
        const barColor = !trusted ? 'var(--txt3)' : (s.shadow_wr != null ? winRateColor(s.shadow_wr) : 'var(--txt3)');
        // PHASE 16: friendly display text; raw code stays in the title for cross-referencing logs.
        const label = kind === 'tag' ? friendlyTag(s[nameKey]) : friendlyCombo(s[nameKey]);
        const fullPhrase = `If taken: ${wrFrag} · ${rFrag} · ${nFrag}`;
        return `
          <div class="stratBarRow" style="opacity:${trusted ? 1 : 0.4}">
            <div class="stratBarLabel" title="${esc(s[nameKey])}">${esc(label)}</div>
            <div class="stratBarTrack"><div class="stratBarFill" style="width:${barW}%;background:${barColor}"></div></div>
            <div class="stratBarVal" title="${esc(fullPhrase)}">
              <span class="svLead">If taken:</span>
              <span class="svFrag">${esc(wrFrag)}</span>
              <span class="svFrag">${esc(rFrag)}</span>
              <span class="svFrag">${esc(nFrag)}${trusted ? '' : ' ⚠'}</span>
            </div>
          </div>`;
      }).join('');
    }

    // PHASE 7B-4: hand-rolled sparkline (no charting library, matching the funnel/heatmap's
    // own hand-rolled-div convention). `weekly` is build_weekly_regret()'s full-history,
    // NOT baseline-scoped list of {week_start, forfeited_r, dodged_r, n_resolved}, oldest
    // first. Bar height is proportional to forfeited_r only (the metric named in the spec);
    // an empty week renders as a near-flat bar, an honest data point, not an error.
    function renderShadowSparkline(weekly) {
      const bars = document.getElementById('stratShadowSpark');
      const labels = document.getElementById('stratShadowSparkLabels');
      if (!bars || !labels) return;
      if (!weekly.length) {
        bars.innerHTML = '<div class="stratEmptyState">No weekly data yet.</div>';
        labels.innerHTML = '';
        return;
      }
      const max = Math.max(1, ...weekly.map(w => w.forfeited_r));
      bars.innerHTML = weekly.map(w => {
        const pct = Math.max(2, (w.forfeited_r / max * 100));
        return `<div class="stratSparkBar" style="height:${pct.toFixed(1)}%"
                     title="week of ${esc(w.week_start)} — forfeited ${w.forfeited_r >= 0 ? '+' : ''}${w.forfeited_r.toFixed(2)}R, dodged +${w.dodged_r.toFixed(2)}R, n=${w.n_resolved}"></div>`;
      }).join('');
      labels.innerHTML = weekly.map(w => `<span>${esc(w.week_start.slice(5))}</span>`).join('');
    }

    function _shadowSumSplits(keys, bucketsByKey) {
      const out = { right: 0, wrong: 0, pending: 0 };
      let any = false;
      keys.forEach(k => {
        const b = bucketsByKey[k];
        if (!b) return;
        any = true;
        out.right   += b.verdict_split.right;
        out.wrong   += b.verdict_split.wrong;
        out.pending += b.verdict_split.pending;
      });
      return any ? out : null;
    }

    function _shadowSplitForReasonKey(reasonKey, bucketsByKey) {
      if (reasonKey === 'SKIP')   return _shadowSumSplits(SHADOW_SKIP_KEYS, bucketsByKey);
      if (reasonKey === 'REJECT') return _shadowSumSplits(SHADOW_BARE_REJECT_KEYS, bucketsByKey);
      if (SHADOW_DECISION_TO_GATE_KEY[reasonKey]) {
        const b = bucketsByKey[SHADOW_DECISION_TO_GATE_KEY[reasonKey]];
        return b ? b.verdict_split : null;
      }
      const b = bucketsByKey[reasonKey];   // direct match -- NOT_ESCALATED reasons (cooldown, etc.)
      return b ? b.verdict_split : null;
    }

    async function loadShadow() {
      try {
        const [r] = await Promise.all([_apiFetch('/api/strategy/counterfactual'), _loadTagRegistry()]);
        const d = await r.json();
        _shadowBucketsCache = d.buckets || [];
        _shadowResolvedItemsCache = d.resolved_items || [];

        const dodged = d.headline.dodged_r, forfeited = d.headline.forfeited_r;
        document.getElementById('stratShadowDodged').textContent    = (dodged >= 0 ? '+' : '') + dodged.toFixed(2) + 'R';
        document.getElementById('stratShadowForfeited').textContent = (forfeited >= 0 ? '+' : '') + forfeited.toFixed(2) + 'R';
        document.getElementById('stratShadowCoverage').textContent =
          `${d.coverage.resolved} of ${d.coverage.total} resolved`;

        const gateBuckets = (d.buckets || [])
          .filter(b => b.key.startsWith('GATE-'))
          .sort((a, b) => Math.abs(b.total_r) - Math.abs(a.total_r));
        document.getElementById('stratShadowGateGrid').innerHTML = gateBuckets.length
          ? gateBuckets.map(b => {
              const wrTxt = b.shadow_wr != null ? b.shadow_wr.toFixed(0) + '%' : 'n/a';
              const rTxt  = b.shadow_wr != null ? (b.total_r >= 0 ? '+' : '') + b.total_r.toFixed(1) + 'R' : 'n/a';
              const _gateCode = b.key.replace('GATE-', '');   // PHASE 22: friendly name from the raw code, not the mechanically title-cased label
              return `
                <div class="stratGradeCard">
                  <div class="g" title="${esc(_gateCode)}">${esc(friendlyReason(_gateCode))}</div>
                  <div class="wr ${b.shadow_wr == null ? 'na' : (b.shadow_wr >= 50 ? 'good' : 'bad')}">${wrTxt}</div>
                  <div class="n">n=${b.n} · shadow WR ${wrTxt} · shadow R ${rTxt}${b.shadow_wr == null ? ` (no ${CHARACTER_DISPLAY.Examiner} record)` : ''}</div>
                </div>`;
            }).join('')
          : '<div class="stratEmptyState">No downstream gate-kills in this window.</div>';

        renderShadowLeaderboard('stratShadowTagBars', d.shadow_tags || [], 'tag', SHADOW_MIN_TAG_N);
        renderShadowLeaderboard('stratShadowComboBars', d.shadow_combos || [], 'combo', SHADOW_MIN_COMBO_N);
        renderShadowSparkline(d.weekly || []);
      } catch (e) {
        console.warn('[Strategy] shadow load failed', e);
        _shadowBucketsCache = null;
        _shadowResolvedItemsCache = null;
        const cov = document.getElementById('stratShadowCoverage');
        if (cov) cov.textContent = 'shadow data unavailable';
        ['stratShadowDodged', 'stratShadowForfeited'].forEach(id => {
          const el = document.getElementById(id); if (el) el.textContent = '—';
        });
        const spark = document.getElementById('stratShadowSpark');
        const sparkLbl = document.getElementById('stratShadowSparkLabels');
        if (spark) spark.innerHTML = '';
        if (sparkLbl) sparkLbl.innerHTML = '';
        const grid = document.getElementById('stratShadowGateGrid');
        if (grid) grid.innerHTML = '';
        ['stratShadowTagBars', 'stratShadowComboBars'].forEach(id => {
          const el = document.getElementById(id); if (el) el.innerHTML = '';
        });
      }
    }

    // Upgrades the reason bars loadFunnel() already rendered with a green/red/grey
    // verdict split INSIDE each bar's existing width -- run only after both loadFunnel
    // and loadShadow have settled (see loadAll). A reason with no matching shadow bucket
    // is left exactly as loadFunnel rendered it.
    function applyShadowSplitsToReasonBars() {
      if (!_shadowBucketsCache) return;
      const bucketsByKey = {};
      _shadowBucketsCache.forEach(b => { bucketsByKey[b.key] = b; });

      document.querySelectorAll('#stratReasonBars .stratBarRow').forEach(row => {
        const labelEl = row.querySelector('.stratBarLabel');
        const trackEl = row.querySelector('.stratBarTrack');
        const fillEl  = trackEl ? trackEl.querySelector('.stratBarFill') : null;
        if (!labelEl || !trackEl || !fillEl) return;   // already upgraded or unexpected markup -- skip
        const reasonKey = labelEl.getAttribute('title') || labelEl.textContent;
        const vs = _shadowSplitForReasonKey(reasonKey, bucketsByKey);
        if (!vs) return;   // no matching bucket -- leave the bar exactly as it is today
        const tot = vs.right + vs.wrong + vs.pending;
        if (tot <= 0) return;
        const outerWidth = fillEl.style.width;   // preserve the bar's existing outer proportion
        const rPct = vs.right   / tot * 100;
        const wPct = vs.wrong   / tot * 100;
        const pPct = 100 - rPct - wPct;
        trackEl.innerHTML = `
          <div class="stratBarSplit" style="width:${outerWidth}"
               title="shadow verdict — ${vs.right} right (dodged) / ${vs.wrong} wrong (forfeited) / ${vs.pending} pending or no ${CHARACTER_DISPLAY.Examiner} record">
            <div class="right"   style="width:${rPct.toFixed(1)}%"></div>
            <div class="wrong"   style="width:${wPct.toFixed(1)}%"></div>
            <div class="pending" style="width:${pPct.toFixed(1)}%"></div>
          </div>`;
      });
    }

    async function loadPerformance() {
      try {
        const [r] = await Promise.all([_apiFetch('/api/strategy/performance'), _loadTagRegistry()]);
        const d = await r.json();
        _tagStatsCache = d.tag_stats || _tagStatsCache;
        document.getElementById('scWinRate').textContent = d.total_closed ? d.win_rate.toFixed(1) + '%' : '—';
        document.getElementById('scWinRate').className = 'v ' + (d.win_rate >= 50 ? 'good' : 'bad');
        document.getElementById('scProfitFactor').textContent = d.profit_factor != null ? d.profit_factor.toFixed(2) : '—';
        document.getElementById('scProfitFactor').className = 'v ' + (d.profit_factor >= 1.5 ? 'good' : d.profit_factor != null && d.profit_factor < 1 ? 'bad' : '');
        document.getElementById('scExpectancy').textContent = '$' + d.expectancy.toFixed(2);
        document.getElementById('scExpectancy').className = 'v ' + (d.expectancy >= 0 ? 'good' : 'bad');
        document.getElementById('scTotalClosed').textContent = d.total_closed;
        document.getElementById('scTotalPnl').textContent = (d.total_pnl >= 0 ? '+$' : '-$') + Math.abs(d.total_pnl).toFixed(2);
        document.getElementById('scTotalPnl').className = 'v ' + (d.total_pnl >= 0 ? 'good' : 'bad');
        document.getElementById('scScratches').textContent = d.scratches ?? '—';

        // Grade grid
        const gEntries = Object.entries(d.grade_stats).sort((a,b) => b[1].n - a[1].n);
        document.getElementById('stratGradeGrid').innerHTML = gEntries.length
          ? gEntries.map(([g, gs]) => `
              <div class="stratGradeCard">
                <div class="g">${esc(g).toUpperCase()}</div>
                <div class="wr" style="color:${winRateColor(gs.win_rate)}">${gs.win_rate}%</div>
                <div class="n">n=${gs.n}</div>
              </div>
            `).join('')
          : '<div class="stratEmptyState">No graded trades yet.</div>';

        // Tag leaderboard
        const tEntries = Object.entries(d.tag_stats).filter(([,s]) => s.n >= 3).sort((a,b) => b[1].win_rate - a[1].win_rate);
        document.getElementById('stratTagBars').innerHTML = tEntries.length
          ? tEntries.map(([tag, s]) => `
              <div class="stratBarRow">
                <div class="stratBarLabel" title="${esc(tag)}">${esc(friendlyTag(tag))}</div>
                <div class="stratBarTrack"><div class="stratBarFill" style="width:${(s.win_rate*100).toFixed(1)}%;background:${winRateColor(s.win_rate*100)}"></div></div>
                <div class="stratBarVal">${(s.win_rate*100).toFixed(0)}% (n=${s.n})</div>
              </div>
            `).join('')
          : '<div class="stratEmptyState">Not enough closed trades per tag yet.</div>';

        // Confluence COMBINATION leaderboard — same idea as tags, one level more specific.
        // Real sample sizes are thin (checked against actual data: almost everything is
        // n=1-2) — shown anyway, dimmed, so it's visible as it accumulates rather than
        // hidden until some day it's "ready."
        const MIN_COMBO_N = 5;
        const cEntries = Object.entries(d.combo_stats || {}).sort((a,b) => b[1].n - a[1].n).slice(0, 15);
        document.getElementById('stratComboBars').innerHTML = cEntries.length
          ? cEntries.map(([combo, s]) => {
              const trusted = s.n >= MIN_COMBO_N;
              return `<div class="stratBarRow" style="opacity:${trusted ? 1 : 0.4}">
                <div class="stratBarLabel" title="${esc(combo)}">${esc(friendlyCombo(combo))}</div>
                <div class="stratBarTrack"><div class="stratBarFill" style="width:${(s.win_rate*100).toFixed(1)}%;background:${trusted ? winRateColor(s.win_rate*100) : 'var(--txt3)'}"></div></div>
                <div class="stratBarVal">${(s.win_rate*100).toFixed(0)}% (n=${s.n})${trusted ? '' : ' ⚠'}</div>
              </div>`;
            }).join('')
          : '<div class="stratEmptyState">No multi-tag combinations logged yet.</div>';

        // Planned R:R histogram (Dexter's real cost-adjusted formula, positive-only range)
        renderRHist(d.planned_rrs || []);

        // Equity curve
        renderEquityCurve(d.equity_curve || []);
      } catch (e) { console.warn('[Strategy] performance load failed', e); }
    }

    function renderRHist(rrs) {
      const edges = [0, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5];
      const buckets = edges.map((lo, i) => ({ lo, hi: edges[i+1] ?? Infinity, count: 0 }));
      rrs.forEach(r => { const b = buckets.find(b => r >= b.lo && r < b.hi); if (b) b.count++; });
      const max = Math.max(1, ...buckets.map(b => b.count));
      const el = document.getElementById('stratRHist');
      if (!rrs.length) { el.innerHTML = '<div class="stratEmptyState">No R:R data yet.</div>'; return; }
      el.innerHTML = buckets.map(b => {
        const label = b.hi === Infinity ? `≥ ${b.lo}:1` : `${b.lo}:1 – ${b.hi}:1`;
        const color = b.lo >= 2 ? 'var(--green)' : b.lo >= 1 ? 'var(--gold)' : 'var(--red)';
        return `<div class="stratBarRow">
          <div class="stratBarLabel">${label}</div>
          <div class="stratBarTrack"><div class="stratBarFill" style="width:${(b.count/max*100).toFixed(1)}%;background:${color}"></div></div>
          <div class="stratBarVal">${b.count}</div>
        </div>`;
      }).join('');
    }

    function renderEquityCurve(curve) {
      const el = document.getElementById('stratEquityChart');
      if (!curve.length) { el.innerHTML = '<div class="stratEmptyState">No closed trades yet — equity curve will appear here.</div>'; return; }
      if (!equityChart) {
        el.innerHTML = '';
        equityChart = LightweightCharts.createChart(el, {
          autoSize: true,
          layout: { background: { color: 'transparent' }, textColor: '#787b86' },
          grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
          rightPriceScale: { borderColor: '#2a2e39' },
          timeScale: { borderColor: '#2a2e39', timeVisible: true },
        });
        equitySeries = equityChart.addAreaSeries({
          lineColor: '#d4af37', topColor: 'rgba(212,175,55,0.25)', bottomColor: 'rgba(212,175,55,0.0)', lineWidth: 2,
        });
      }
      // PHASE 25: `t` is now a real UNIX epoch (one point per 4h bucket boundary, backend-
      // computed) instead of a date-only string -- LightweightCharts' numeric time mode,
      // required for sub-day granularity (its "business day" string mode can only
      // represent a whole calendar day).
      const seen = new Set();
      const data = curve.map(pt => ({ time: pt.t, value: pt.cum_pnl })).filter(pt => {
        if (pt.time == null || seen.has(pt.time)) return false; // lightweight-charts needs unique/ascending time keys
        seen.add(pt.time); return true;
      });
      equitySeries.setData(data);
      equityChart.timeScale().fitContent();
    }

    // PHASE 13: system-over-time charts. Same LightweightCharts instance the equity curve
    // above already uses -- no new library. Each chart is built once then updated via
    // setData()/setMarkers() on every loadAll() cycle (25s), matching the equity curve's
    // own lazy-init-then-reuse pattern.
    let tsRrChart = null, tsRrFloor = null, tsRrAdvisory = null, tsRrMedian = null, tsRrMin = null, tsRrMax = null;
    let tsFlowChart = null, tsFlowThreshold = null, tsFlowEsc = null, tsFlowPosts = null;
    let tsStopChart = null, tsStopMedian = null, tsStopP10 = null, tsStopP90 = null;

    async function loadTimeseries() {
      try {
        // PHASE 25: bucket_hours=4 explicit (matches the route's own default -- spelled
        // out here so intent is visible at the call site, not just implied server-side).
        const r = await _apiFetch('/api/strategy/timeseries?days=30&bucket_hours=4');
        renderTimeseries(await r.json());
      } catch (e) { console.warn('[Strategy] timeseries load failed', e); }
    }

    function _tsHasData(d) {
      return (d.rr_pressure || []).some(p => p.proposed_median != null || p.ev_advisory != null || p.enforced_floor != null)
          || (d.threshold || []).length > 0;
    }

    // PHASE 25: rows now carry a real epoch (`t`) per bucket instead of a day-string, so
    // matching an annotation to a bucket is a floor-to-bucket-boundary computation instead
    // of exact-string equality. An event whose own bucket isn't present in THIS chart's
    // data is simply not markable on THIS chart (no synthesis of a bucket that doesn't
    // exist) -- it may still show on another chart whose data does have that bucket.
    function _tsMarkersFor(annotations, availableEpochs, bucketSecs) {
      const have = new Set(availableEpochs);
      return annotations
        .map(a => ({ t: Math.floor(a.t / bucketSecs) * bucketSecs, summary: a.summary }))
        .filter(a => have.has(a.t))
        .map(a => ({ time: a.t, position: 'aboveBar', color: '#2962ff', shape: 'arrowDown', text: a.summary }));
    }

    function _tsBaseChartOpts() {
      return {
        autoSize: true,
        layout: { background: { color: 'transparent' }, textColor: '#787b86', fontSize: 10 },
        grid: { vertLines: { color: 'rgba(255,255,255,0.03)' }, horzLines: { color: 'rgba(255,255,255,0.03)' } },
        rightPriceScale: { borderColor: '#2a2e39' },
        // PHASE 25: was timeVisible:false (daily buckets only needed a date). Sub-day
        // buckets need the time shown too -- relying on LightweightCharts' own adaptive
        // tick formatter (it already switches between date-only and date+time labels
        // depending on zoom) rather than a custom tickMarkFormatter.
        timeScale: { borderColor: '#2a2e39', timeVisible: true },
      };
    }

    function renderRrPressureChart(rows, annotations, bucketSecs) {
      const el = document.getElementById('tsRrChart');
      const epochs = rows.map(r => r.t);
      if (!tsRrChart) {
        tsRrChart = LightweightCharts.createChart(el, _tsBaseChartOpts());
        tsRrMax     = tsRrChart.addLineSeries({ color: 'rgba(120,123,134,0.5)', lineWidth: 1, lineStyle: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
        tsRrMin     = tsRrChart.addLineSeries({ color: 'rgba(120,123,134,0.5)', lineWidth: 1, lineStyle: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
        tsRrAdvisory = tsRrChart.addLineSeries({ color: '#2962ff', lineWidth: 2, lastValueVisible: true, priceLineVisible: false, title: 'EV-advisory' });
        tsRrFloor   = tsRrChart.addLineSeries({ color: '#f23645', lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps, lastValueVisible: true, priceLineVisible: false, title: 'Enforced floor' });
        tsRrMedian  = tsRrChart.addLineSeries({ color: '#d4af37', lineWidth: 2, lastValueVisible: true, priceLineVisible: false, title: "Chev's proposals" });
      }
      const _ser = (key) => rows.filter(r => r[key] != null).map(r => ({ time: r.t, value: r[key] }));
      tsRrMax.setData(_ser('proposed_max'));
      tsRrMin.setData(_ser('proposed_min'));
      tsRrAdvisory.setData(_ser('ev_advisory'));
      tsRrFloor.setData(_ser('enforced_floor'));
      const medianData = _ser('proposed_median');
      tsRrMedian.setData(medianData);
      tsRrMedian.setMarkers(_tsMarkersFor(annotations, epochs, bucketSecs));
      tsRrChart.timeScale().fitContent();
    }

    function renderFlowChart(flowRows, thresholdRows, annotations, bucketSecs) {
      const el = document.getElementById('tsFlowChart');
      const epochs = flowRows.map(r => r.t);
      if (!tsFlowChart) {
        tsFlowChart = LightweightCharts.createChart(el, _tsBaseChartOpts());
        tsFlowThreshold = tsFlowChart.addLineSeries({
          color: 'rgba(212,175,55,0.8)', lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps,
          priceScaleId: 'left', lastValueVisible: true, priceLineVisible: false, title: 'Score threshold',
        });
        tsFlowChart.priceScale('left').applyOptions({ borderColor: '#2a2e39', visible: true });
        tsFlowEsc = tsFlowChart.addHistogramSeries({ color: 'rgba(120,123,134,0.5)', priceScaleId: 'right', lastValueVisible: false, priceLineVisible: false });
        tsFlowPosts = tsFlowChart.addLineSeries({ color: '#089981', lineWidth: 2, priceScaleId: 'right', lastValueVisible: true, priceLineVisible: false, title: 'Surviving POSTs' });
      }
      tsFlowThreshold.setData(thresholdRows.filter(r => r.score != null).map(r => ({ time: r.t, value: r.score })));
      tsFlowEsc.setData(flowRows.map(r => ({ time: r.t, value: r.escalations })));
      const postsData = flowRows.map(r => ({ time: r.t, value: r.posts }));
      tsFlowPosts.setData(postsData);
      tsFlowPosts.setMarkers(_tsMarkersFor(annotations, epochs, bucketSecs));
      tsFlowChart.timeScale().fitContent();
    }

    function renderStopChart(rows, costFloorPct, annotations, bucketSecs) {
      const el = document.getElementById('tsStopChart');
      const epochs = rows.filter(r => r.n > 0).map(r => r.t);
      if (!tsStopChart) {
        tsStopChart = LightweightCharts.createChart(el, _tsBaseChartOpts());
        tsStopP90 = tsStopChart.addLineSeries({ color: 'rgba(120,123,134,0.5)', lineWidth: 1, lineStyle: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
        tsStopP10 = tsStopChart.addLineSeries({ color: 'rgba(120,123,134,0.5)', lineWidth: 1, lineStyle: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
        tsStopMedian = tsStopChart.addLineSeries({ color: '#d4af37', lineWidth: 2, lastValueVisible: true, priceLineVisible: false, title: 'Median stop %' });
      }
      const _ser = (key) => rows.filter(r => r.n > 0 && r[key] != null).map(r => ({ time: r.t, value: r[key] }));
      tsStopP90.setData(_ser('p90'));
      tsStopP10.setData(_ser('p10'));
      const medianData = _ser('median_stop_pct');
      tsStopMedian.setData(medianData);
      tsStopMedian.setMarkers(_tsMarkersFor(annotations, epochs, bucketSecs));
      if (tsStopMedian._costFloorLine) { try { tsStopMedian.removePriceLine(tsStopMedian._costFloorLine); } catch(e){} }
      if (costFloorPct != null) {
        tsStopMedian._costFloorLine = tsStopMedian.createPriceLine({
          price: costFloorPct, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true, title: 'cost floor',
        });
      }
      tsStopChart.timeScale().fitContent();
    }

    function renderTimeseries(d) {
      const wrap = document.getElementById('tsChartsWrap');
      const note = document.getElementById('tsEmptyNote');
      if (!_tsHasData(d)) {
        wrap.style.display = 'none';
        note.style.display = 'block';
        note.textContent = d.earliest_structured_ts
          ? `Collecting data since ${d.earliest_structured_ts} — check back after a few more days of escalations.`
          : "No structured data yet — appears after Dexter's next restart (PHASE 10/11 fields aren't live in this running process yet).";
        return;
      }
      wrap.style.display = '';
      note.style.display = 'none';
      const annotations = d.annotations || [];
      const bucketSecs = (d.bucket_hours || 4) * 3600;
      renderRrPressureChart(d.rr_pressure || [], annotations, bucketSecs);
      renderFlowChart(d.flow || [], d.threshold || [], annotations, bucketSecs);
      renderStopChart(d.stop_reality || [], d.crypto_day_cost_floor_pct, annotations, bucketSecs);
    }

    // ===== PHASE 18: INDICATOR SCOREBOARD =====
    // One row per registry tag (all 123, so "never used yet" tools are visible too, not
    // just ones with real trades). Real WR reuses /api/strategy/performance's tag_stats --
    // the SAME field the Tag Leaderboard reads -- never recomputed here. Shadow WR reuses
    // the existing counterfactual endpoint. Trend is the one NEW backend piece (PHASE 18's
    // own /api/strategy/tag_trends).
    const SCOREBOARD_MIN_N = 3;   // same trust threshold the Tag Leaderboard already uses
    let _scoreboardRows = [];
    let _scoreboardSort = { col: 'points', dir: 'desc' };   // Points desc by default, per spec

    // PHASE 23: each of the 4 sources fails INDEPENDENTLY now -- the original version
    // wrapped all four fetches in one shared try/catch, so a single failing endpoint
    // (e.g. a not-yet-restarted Dexter still missing /api/strategy/tag_trends -- 404 ->
    // non-JSON body -> .json() throws) zeroed out the WHOLE table instead of just the one
    // column that source feeds. Points/Indicator always render from the registry alone;
    // Real WR/Shadow WR/Trend individually degrade to "--" only for whichever source
    // actually failed. An empty section is the bug; thin or partially-missing data is not.
    async function loadScoreboard() {
      const [regRes, perfRes, shadowRes, trendsRes] = await Promise.allSettled([
        _loadTagRegistry(),
        _apiFetch('/api/strategy/performance').then(r => r.json()),
        _apiFetch('/api/strategy/counterfactual').then(r => r.json()),
        _apiFetch('/api/strategy/tag_trends').then(r => r.json()),
      ]);

      const registry = regRes.status === 'fulfilled' ? regRes.value : {};
      if (regRes.status === 'rejected') console.warn('[Strategy] scoreboard: registry load failed', regRes.reason);

      const tagStats = (perfRes.status === 'fulfilled' && perfRes.value.tag_stats) || {};
      if (perfRes.status === 'rejected') console.warn('[Strategy] scoreboard: performance load failed', perfRes.reason);

      const shadowByTag = {};
      if (shadowRes.status === 'fulfilled') {
        (shadowRes.value.shadow_tags || []).forEach(s => { shadowByTag[s.tag] = s; });
      } else {
        console.warn('[Strategy] scoreboard: counterfactual load failed', shadowRes.reason);
      }

      const trendsByTag = (trendsRes.status === 'fulfilled' && trendsRes.value.tags) || {};
      if (trendsRes.status === 'rejected') console.warn('[Strategy] scoreboard: tag_trends load failed', trendsRes.reason);

      const tbody = document.getElementById('stratScoreboardBody');
      const codes = Object.keys(registry);
      if (!codes.length) {
        // Only a genuinely-failed registry produces a truly empty table -- nothing else
        // to show Points/Indicator from at all.
        if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="stratEmptyState">Indicator registry unavailable — check the console.</td></tr>';
        return;
      }

      _scoreboardRows = codes.map(code => {
        const t = registry[code];
        const real = tagStats[code];
        const sh = shadowByTag[code];
        return {
          code, name: t.name, points: t.points,
          wr: real ? real.win_rate : null, n: real ? real.n : 0,
          shadowWr: sh ? sh.shadow_wr : null,
          trend: trendsByTag[code] || [],
        };
      });
      renderScoreboard();
    }

    function _sbCompare(a, b, col, dir) {
      const mul = dir === 'asc' ? 1 : -1;
      if (col === 'name') {
        const av = (a.name || '').toLowerCase(), bv = (b.name || '').toLowerCase();
        return av < bv ? -mul : av > bv ? mul : 0;
      }
      const av = a[col], bv = b[col];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;    // nulls always sort last, regardless of direction
      if (bv == null) return -1;
      return (av - bv) * mul;
    }

    function _sbSparkHTML(trend) {
      if (!trend || !trend.length) {
        return '<span style="color:var(--txt3)" title="No closed trades this era yet">—</span>';
      }
      const title = trend.map(w => `${w.week}: ${(w.wr * 100).toFixed(0)}% (n=${w.n})`).join(' | ');
      const bars = trend.map(w => {
        const h = Math.max(8, w.wr * 100);
        return `<div class="sbSparkBar" style="height:${h.toFixed(1)}%;background:${winRateColor(w.wr * 100)}"></div>`;
      }).join('');
      return `<div class="sbSpark" title="${esc(title)}">${bars}</div>`;
    }

    function renderScoreboard() {
      const tbody = document.getElementById('stratScoreboardBody');
      if (!tbody) return;
      const { col, dir } = _scoreboardSort;
      const rows = [..._scoreboardRows].sort((a, b) => _sbCompare(a, b, col, dir));
      tbody.innerHTML = rows.map(r => {
        const dim = r.n < SCOREBOARD_MIN_N;
        // PHASE 23: a tag with zero all-time closed trades is thin data, not a bug -- say
        // so on hover/tap rather than leaving a bare dash with no explanation.
        const wrTxt   = r.wr != null ? (r.wr * 100).toFixed(0) + '%' : '—';
        const wrColor = r.wr != null ? winRateColor(r.wr * 100) : 'var(--txt3)';
        const wrTitle = r.wr != null ? '' : 'title="No closed trades yet (all-time)"';
        const shTxt = r.shadowWr != null ? r.shadowWr.toFixed(0) + '%' : '—';
        return `<tr class="${dim ? 'dimRow' : ''}">
          <td><span class="sbIndicatorName">${esc(r.name)}</span><span class="sbIndicatorCode">${esc(r.code)}</span></td>
          <td>${r.points != null ? r.points : '—'}</td>
          <td style="color:${wrColor}" ${wrTitle}>${wrTxt}</td>
          <td>${_sbSparkHTML(r.trend)}</td>
          <td><span class="sbShadowWr">${shTxt}</span></td>
          <td>${r.n}</td>
        </tr>`;
      }).join('');
      document.querySelectorAll('#stratScoreboardTable th[data-sort]').forEach(th => {
        const arrow = th.dataset.sort === col ? (dir === 'asc' ? '▲' : '▼') : '';
        th.innerHTML = esc(th.dataset.label) + (arrow ? ` <span class="sbSortArrow">${arrow}</span>` : '');
      });
    }

    document.querySelectorAll('#stratScoreboardTable th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.dataset.sort;
        if (_scoreboardSort.col === col) {
          _scoreboardSort.dir = _scoreboardSort.dir === 'asc' ? 'desc' : 'asc';
        } else {
          _scoreboardSort = { col, dir: col === 'name' ? 'asc' : 'desc' };
        }
        renderScoreboard();
      });
    });

    async function loadHeatmap() {
      try {
        const r = await _apiFetch('/api/strategy/heatmap?days=14');
        const d = await r.json();
        const grid = d.grid;
        // PHASE 25: 6 four-hour blocks (00-04, 04-08, ... 20-24) instead of 24 hour
        // columns -- block_hours comes from the response but this render path only ever
        // requests the (now-default) 4h grouping, so blocks.length is always 6 today.
        const blockHours = d.block_hours || 4;
        const blocks = [];
        for (let b = 0; b * blockHours < 24; b++) {
          const startH = b * blockHours, endH = Math.min(24, startH + blockHours);
          blocks.push(`${String(startH).padStart(2,'0')}–${String(endH).padStart(2,'0')}`);
        }
        const max = Math.max(1, ...grid.flat());
        const days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
        let html = '<div></div>';
        blocks.forEach(label => html += `<div class="stratHeatHourLabel">${label}</div>`);
        days.forEach((day, di) => {
          html += `<div class="stratHeatDayLabel">${day}</div>`;
          blocks.forEach((label, bi) => {
            const v = grid[di][bi];
            const alpha = (v / max) * 0.85 + (v > 0 ? 0.1 : 0);
            html += `<div class="stratHeatCell" style="background:rgba(212,175,55,${alpha.toFixed(2)})" title="${day} ${label} UTC — ${v} setups"></div>`;
          });
        });
        document.getElementById('stratHeatmap').innerHTML = html;
      } catch (e) { console.warn('[Strategy] heatmap load failed', e); }
    }

    // Tab switching. PHASE 21: pulled the actual switch into its own function so the (i)
    // deep-link handler (further down) can reuse the exact same logic instead of a copy.
    function _switchStratTab(spane) {
      document.querySelectorAll('.stratTab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.stratPane').forEach(p => p.classList.remove('active'));
      document.querySelector(`.stratTab[data-spane="${spane}"]`).classList.add('active');
      document.querySelector(`.stratPane[data-spane="${spane}"]`).classList.add('active');
    }
    document.querySelectorAll('.stratTab').forEach(tab => {
      tab.addEventListener('click', () => _switchStratTab(tab.dataset.spane));
    });

    // Feed filter pills
    document.querySelectorAll('.stratFilterPill').forEach(pill => {
      pill.addEventListener('click', () => {
        document.querySelectorAll('.stratFilterPill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        feedFilter = pill.dataset.filt;
        renderFeed();
      });
    });

    document.getElementById('stratModeBadge').addEventListener('click', toggleExploration);
    document.getElementById('stratCloseBtn').addEventListener('click', closeStrategy);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeStrategy(); });
    window.addEventListener('keydown', e => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeStrategy(); });

    // ===== PHASE 17/21: LEARN TAB (per-section explanations + Glossary) =====
    // One entry per section label -- what it shows AND how it's calculated, read from
    // the actual code that produces each section's data, not folklore. PHASE 21 turned
    // this from a tap-popup into deep-links: every (i) icon is always visible and jumps
    // straight to its entry in the Learn tab instead of floating a tooltip in place.
    const SECTION_TOOLTIPS = {
      scorecard: "Your all-time paper-trading track record across every closed trade Chev has posted: win rate, profit factor, expectancy per trade, how many trades have closed, and net P&L. This is real performance on the paper account, not a projection — but it's all-time, so a rough early stretch can still weigh on the numbers long after the system has improved.",
      equity_curve: "Your paper account balance over time — every closed trade moves this line. Each point is the cumulative P&L up to that trade's close; a rising line means the account is growing, a flat or falling stretch means it isn't.",
      setup_volume_heatmap: "When opportunities show up — which days and hours (UTC) the scanner finds the most setups, over the last 14 days, whether or not they were ever escalated to Chev. Useful for spotting which sessions actually produce activity worth watching.",
      funnel: "Every 2 minutes Dexter scans the watchlist. This counts how many setups he saw, how many were filtered out before Chev ever saw them, how many he sent to Chev, what Chev decided, and how many survived George's risk checks to actually open — all in the last 24 hours.",
      skip_reject_reasons: "A breakdown of exactly why setups didn't become trades in the last 24 hours — Chev's own skip reasoning, or which specific gate or gauntlet check rejected it. Longer bars mean that reason is killing more setups; use this to see what's actually holding trades back right now.",
      shadow_outcomes: "These are \"what-if\" numbers from trades we did NOT take — \"bullets dodged\" (skips that would have lost) versus \"money left on the table\" (skips that would have won). They assume everything went perfectly (fills, no management, no slot limits) — treat them as clues for tuning, never as results, and they never merge into real performance stats.",
      gate_scoreboard: "Breaks the shadow outcomes down by exactly which gate rejected the setup (cost gate, R:R gate, ATR floor, etc.), showing each gate's own hypothetical win rate and R. Answers \"which specific rule is doing the most damage or the most good.\"",
      shadow_tag_leaderboard: "Hypothetical win rate per confluence tag, but only from setups that were skipped or killed — a much larger sample than real trades (skips outnumber posts roughly 15-20 to 1). Skipped setups aren't a random sample though — Chev skipped them for a reason — so treat this as leads worth reviewing, not proof.",
      shadow_combo_leaderboard: "The same hypothetical leaderboard as the Shadow Tag Leaderboard, one level more specific — grouped by the exact combination of tags present, not just one tag at a time. Combinations below the trust minimum are shown greyed out.",
      weekly_regret_trend: "Missed profits by week — what the trades we skipped would have earned, totalled per week across the system's full history. Falling is good: it means the system is getting better at recognizing good setups it used to pass on.",
      win_rate_by_grade: "Real win rate on closed trades, grouped by the quality grade (B / A / A+) Dexter assigned when the setup was first evaluated. Higher grades should, over enough trades, show a higher win rate — this is where you can check if that's actually true.",
      tag_leaderboard: "Real win rate on CLOSED trades, one row per confluence tag Chev cited when he posted the trade. This is actual money-on-the-line performance, not a hypothetical — rows need at least 3 closed trades to show.",
      combo_leaderboard: "The same real win-rate idea as the Tag Leaderboard, one level more specific — grouped by the exact set of tags Chev cited together, not one at a time. Sample sizes here are thin (most combinations have only 1-2 trades so far), so rows below the trust minimum are shown greyed out rather than hidden.",
      planned_rr_distribution: "A histogram of the Risk:Reward ratio Dexter actually calculated for each closed trade at the moment it was entered — the same cost-adjusted formula George enforces, not a rough estimate. Shows whether trades are clustering in a healthy R:R range or getting waved through too close to the minimum.",
      system_over_time: "Three charts tracking how the system's own rules have moved over time, with markers for every restart or settings change: how tight the required Risk:Reward has been (the enforced floor, Chev's actual proposals, and the EV-based advisory number); how the escalation bar relates to how much actually gets escalated and posted; and how far stops have actually been placed versus the real cost floor. This is for spotting whether a tuning change actually helped, not for judging any single trade.",
      indicator_scoreboard: "The Indicator Scoreboard lines up every confluence tool in one table: its hand-set Points, its real win rate on actual closed trades, a week-by-week trend of that win rate, and — where it exists — the same tool's hypothetical (shadow) win rate on setups nobody took. Sort any column by tapping its header; rows with fewer than 3 closed trades are dimmed, not hidden.",
    };

    // PHASE 27: same 16 keys as SECTION_TOOLTIPS, retold in-character. Never reproduces
    // dialogue or scenes from any show -- original text evoking each archetype from
    // Kev's own Chev Story.txt. Definition mode is the default; this only shows when
    // Story Time is toggled on.
    const SECTION_STORY = {
      scorecard: "Jax doesn't care what anyone predicted — only what actually happened, checked candle by candle against real cost and real slippage. Every closed trade on this account passed through his hands before it counted as a win or a loss. This is his honest ledger, all-time, which means an early rough patch can still drag on it long after things improved.",
      equity_curve: "This is the line Jax actually keeps — the real balance, after his own honest accounting of every close. It only moves when a trade actually closes and he's confirmed the real number. A flat stretch isn't nothing happening — it's Jax quietly waiting for the next one to resolve.",
      setup_volume_heatmap: "Dexter never stops scanning, but even he has busier hours than others. This is his own attendance record — which days and hours actually produce something worth a folder, whether or not Chev ever saw it. Some sessions just have more happening than others, and Dexter's the only one who'd know.",
      funnel: "This is Dexter's own accounting of one night's work — every setup he noticed, everything filtered out before Chev ever saw it, what actually reached Chev's desk, what he decided, and what survived George's checklist to actually open. Most nights end in mostly nothing, and that's exactly how it's supposed to work.",
      skip_reject_reasons: "Chev doesn't get to just say no — he has to say why, citing something real from the chart, or George has to say exactly which line item failed. This tally is every one of those reasons stacked up over the last day. The longer a bar, the more setups that exact reason is quietly killing right now.",
      shadow_outcomes: "Mike Ross never lets a skipped setup just disappear — he opens a shadow file on every single one and waits to see, hypothetically, whether it would have won. These numbers assume a clean fill and no real-world friction, so treat them as leads, never as proof. He remembers everything; he just never gets to act on any of it.",
      gate_scoreboard: "Mike Ross's shadow files, sorted by exactly which of George's checklist items actually stopped the trade. It's the clearest way to see whether one specific rule is doing real work — or quietly costing more than it's saving.",
      shadow_tag_leaderboard: "Mike Ross's hypothetical win rate, tag by tag, built entirely from setups nobody actually took. There's far more of this data than real trades — skips outnumber posts badly — but Chev skipped each one for a reason, so read this as a lead worth checking, not a verdict.",
      shadow_combo_leaderboard: "The same shadow ledger Mike Ross keeps, but grouped by the exact combination of tags a setup carried, not one at a time. Thin combinations get greyed out — even his memory needs enough repeats before he'll vouch for a number.",
      weekly_regret_trend: "Week by week, what Mike Ross's shadow files say the skipped setups would have earned, added up across the system's whole history. A falling line is the good outcome — it means fewer good setups are slipping past everyone into his shadow ledger instead of the real one.",
      win_rate_by_grade: "The letter grade here is Dexter's own call, made the moment he first saw the setup, before anyone else weighed in. This checks whether his A+ actually beats his A, and his A actually beats his B, once enough real trades have closed to know for sure.",
      tag_leaderboard: "Jax's honest, real-money ledger, broken down one confluence tag at a time — only counting the tag if Chev actually cited it on a trade that actually closed. Real fills, real costs, real outcome. Needs at least 3 closed trades before Jax will show a row at all.",
      combo_leaderboard: "The same honest ledger Jax keeps for single tags, but for the exact combination Chev cited together on one trade. Most combinations have barely one or two closes so far, so the thin ones sit greyed out until there's enough to trust.",
      planned_rr_distribution: "This is the actual Risk:Reward George measured on every closed trade the moment it opened — the real cost-adjusted number, not a rough guess. He's the one who'd tell you if trades keep landing right on his minimum instead of comfortably clear of it.",
      system_over_time: "George's own numbers don't stay fixed — his required Risk:Reward, his cost ceiling, the bar Dexter has to clear before calling anyone over — all of it can move, and this tracks every one of those moves over time, with a marker for every restart. It's here to check whether a tuning change actually helped, not to judge any one trade.",
      indicator_scoreboard: "Every tool in Dexter's kit, lined up in one table — his own hand-set Points, its real win rate from Jax's honest ledger, and where Mike Ross has enough shadow data, its hypothetical win rate too. Tap any column to sort it; anything Jax has fewer than 3 real closes on gets dimmed, not hidden.",
    };

    const CONCEPT_GLOSSARY = [
      { name: "Confluence Score", def: "A plain number Dexter computes for every setup by adding up points from whichever indicators lined up — support/resistance, Fibonacci levels, RSI divergence, EMAs, chart patterns, and more. Nothing subjective here; it's arithmetic. A setup must clear a minimum score before Dexter will even show it to Chev.", how: "Each matched indicator contributes its own fixed point value (see Confluence Tags below); the score is their sum. The minimum required score depends on the asset (crypto/forex/stock) and whether EXPLORATION_MODE is active.",
        story: "This is Dexter's own arithmetic, and it really is just addition — a point for a support test, a point for a confirmed divergence, on down the list. Nothing subjective ever enters into it. A setup has to clear his line before he'll even walk it over to Chev's desk." },
      { name: "Risk:Reward (R:R)", def: "The ratio between how much a trade risks (entry to stop-loss) and how much it targets (entry to take-profit). A 2:1 R:R means the target is twice as far as the stop — if it wins, the reward is twice the risk.", how: "Computed as the target distance divided by the stop distance, both measured from the entry price.",
        story: "Every setup in the building carries this ratio the moment its numbers are set, whether or not anyone's paying attention to it yet. It's the plainest measure in the whole operation — how far to the target, divided by how far to the stop. George will ask a much harsher version of this same question later." },
      { name: "Net R:R", def: "The same Risk:Reward ratio, but adjusted for the real cost of trading — fees and slippage on both the entry and exit. A setup can look like a good R:R on paper and still fail Net R:R once those costs are subtracted, especially on tight stops. This is the number George (the risk gauntlet) actually enforces, not the raw R:R.", how: "Net R:R = (reward% − round-trip cost%) ÷ (risk% + round-trip cost%). Costs reduce the effective reward and increase the effective risk at the same time.",
        story: "This is George's own number, and it's the harsher one — the same ratio, but with real fees and slippage subtracted first. A setup can look fine on Dexter's raw math and still fail George's version once the true cost of getting in and out is counted." },
      { name: "Shadow Outcomes", def: "Hypothetical results for setups Chev skipped or a gate rejected — what would have happened if the trade had been taken, using the same win/loss labelling method as real trades, with realistic costs modelled in. These numbers never touch the real P&L or real stats; they exist purely to show whether the system is skipping good setups or correctly avoiding bad ones.", how: "Computed by the Examiner (labeller.py) using the same triple-barrier method (does price hit target, stop, or time out first) applied to every setup Dexter evaluates, whether or not it was ever escalated to Chev.",
        story: "Mike Ross keeps this file on setups nobody actually took — what would have happened, hypothetically, using the exact same win/loss test as a real trade. None of it ever touches the real ledger. It exists purely so someone, eventually, can ask whether good setups are being let go for no good reason." },
      { name: "Setup Grades (B / A / A+)", def: "A quality tier Dexter assigns to a setup based on how far its confluence score clears the minimum bar, boosted for a trending regime or a peak trading session, and reduced for forex during a low-quality session. There is no \"C\" grade — the floor is B.", how: "Starts from the ratio of the setup's score to the minimum required score (2x+ = A+, 1.5x+ = A, otherwise B), then adjusted up for a trending 4H regime or a peak session, and down for forex in a low-quality session.",
        story: "Dexter hands out this grade the moment he first sees a setup — B is the floor, there's no grade below it. It gets a bump for a trending market or a peak session, and a small penalty for forex during a quiet one." },
      { name: "The Funnel Stages", def: "The path every setup takes from \"Dexter noticed it\" to \"a real trade opened,\" in the last 24 hours: setups seen → filtered out before Chev ever sees them (score too low, too far from the level, on cooldown, etc.) → escalated to Chev → Chev's verdict (posted / skipped) → George's gate and gauntlet checks → actually opened. Each stage shows where setups are lost.", how: "Pre-escalation filters come from the Examiner's shadow log; Chev's decisions and George's gate/gauntlet rejections come from the decision log; the opened count comes from the live trade list.",
        story: "This is Dexter's own accounting of one night, start to finish — everything he saw, everything filtered out before Chev ever looked at it, what reached Chev, what he decided, and what survived George's checklist to actually open. The early filtering numbers come straight from Mike Ross's shadow files, since he's the one watching setups even before Chev does." },
    ];

    // PHASE 21: order matters for the Sections list -- same order the sections actually
    // appear in the dashboard (Overview, then Why It Skips, then Performance).
    const SECTION_ORDER = [
      'scorecard', 'equity_curve', 'setup_volume_heatmap',
      'funnel', 'skip_reject_reasons', 'shadow_outcomes', 'gate_scoreboard',
      'shadow_tag_leaderboard', 'shadow_combo_leaderboard', 'weekly_regret_trend',
      'win_rate_by_grade', 'tag_leaderboard', 'combo_leaderboard',
      'planned_rr_distribution', 'system_over_time', 'indicator_scoreboard',
    ];

    // PHASE 27: light-touch Story Time voice for the 123-tag glossary. Every real
    // confluence tag belongs to either Dexter (indicators/levels he counts) or Elliot
    // (shape/pattern/structure -- his six-step geometry process), matching the actual
    // division of labor in Chev Story.txt. Templated by category rather than one
    // bespoke paragraph per tag -- that's the "light touch" the phase asked for.
    function _tagStoryText(name) {
      const n = (name || '').toLowerCase();
      if (/triangle|wedge/.test(n))
        return "Elliot fits his lines through the swings for this one himself — a squeeze or a slope, either way he's measuring it, not guessing at it.";
      if (/flag|pennant|channel/.test(n))
        return "Elliot calls this one after walking his six steps in order — swing, leg, state, geometry, auction, then finally a hypothesis.";
      if (/double top|double bottom|triple top|triple bottom|head & shoulders|rectangle/.test(n))
        return "Elliot only names this shape once the swing points actually line up to prove it — he's not allowed to just eyeball it.";
      if (/pattern breakout|pattern \(/.test(n))
        return "This is Elliot's own confidence number talking — how well the shape actually matched its textbook definition, not a vibe.";
      if (/market structure/.test(n))
        return "Elliot reads this one off who's actually in control right now — participation and direction, not a single candle.";
      if (/open interest|funding/.test(n))
        return "This one comes in from the derivatives corner, but it lands in Dexter's folder just like everything else.";
      if (/support|resistance/.test(n))
        return "Dexter's been counting this level's touches all night — the more times price has bounced off it, the more it earns his attention.";
      if (/fibonacci|golden pocket/.test(n))
        return "Dexter measures this one off the swing the same mechanical way every time — no feel, just the ratio.";
      if (/rsi|divergence/.test(n))
        return "Dexter's watching the RSI needle argue with price on this one — when they disagree, he takes notice.";
      if (/ema/.test(n))
        return "Dexter just watches whether price respects this moving average or plows straight through it.";
      if (/bollinger/.test(n))
        return "Dexter tracks this band the same way every cycle — how wide it is, and whether price is testing its edge.";
      if (/volume|vwap/.test(n))
        return "Dexter checks where the actual volume traded, not just where price is sitting right now.";
      if (/candle/.test(n))
        return "Dexter flags this one straight off the candle shape itself — quick to spot, quick to log.";
      if (/prior day|opening range|liquidity sweep/.test(n))
        return "Dexter marks this level mechanically, the same spot every session, no judgment involved.";
      return "Dexter logs this one the same way he logs everything — quietly, and without an opinion of his own.";
    }

    // PHASE 27: Definition (default) vs Story Time, remembered per device.
    let _learnMode = localStorage.getItem('chevLearnMode') === 'story' ? 'story' : 'definition';
    let _tagRegistryCache = null;
    // Which section entry (if any) is currently pinned highlighted from an (i) deep-link --
    // tracked separately from the DOM so it survives _renderLearnContent() rebuilding the
    // Sections list's HTML on every Definition/Story Time toggle.
    let _pinnedLearnKey = null;

    async function _renderLearnContent() {
      const isStory = _learnMode === 'story';
      document.getElementById('stratLearnSections').innerHTML = SECTION_ORDER.map(key => `
        <div class="learnEntry${key === _pinnedLearnKey ? ' learnPinned' : ''}" id="learnEntry-${esc(key)}">
          <div class="gDef">${esc(_applyCharacterNames(isStory ? (SECTION_STORY[key] || SECTION_TOOLTIPS[key]) : SECTION_TOOLTIPS[key]) || '')}</div>
        </div>`).join('');
      document.getElementById('stratConceptGlossary').innerHTML = CONCEPT_GLOSSARY.map(c => `
        <div class="learnEntry">
          <div class="gName">${esc(c.name)}</div>
          <div class="gDef">${esc(_applyCharacterNames(isStory ? (c.story || c.def) : c.def))}</div>
          <div class="gHow">${esc(_applyCharacterNames(c.how))}</div>
        </div>`).join('');
      if (!_tagRegistryCache) _tagRegistryCache = await _loadTagRegistry();
      const registry = _tagRegistryCache;
      const codes = Object.keys(registry).sort((a, b) => registry[a].name.localeCompare(registry[b].name));
      document.getElementById('stratTagGlossary').innerHTML = codes.map(code => {
        const t = registry[code];
        const ptsTxt = t.points != null ? `${t.points}pt` : 'unscored';
        const defText = isStory ? _tagStoryText(t.name) : t.definition;
        return `
          <div class="learnEntry" data-search="${esc((t.name + ' ' + code).toLowerCase())}">
            <div class="gName">${esc(t.name)}<span class="gCode">${esc(code)}</span><span class="gPoints">${ptsTxt}</span></div>
            <div class="gDef">${esc(_applyCharacterNames(defText))}</div>
            <div class="gHow">${esc(_applyCharacterNames(t.how))}</div>
          </div>`;
      }).join('');
      const q = document.getElementById('glossarySearch').value.trim().toLowerCase();
      if (q) {
        document.querySelectorAll('#stratTagGlossary .learnEntry').forEach(el => {
          el.style.display = (el.dataset.search || '').includes(q) ? '' : 'none';
        });
      }
    }

    function _setLearnMode(mode) {
      _learnMode = mode;
      localStorage.setItem('chevLearnMode', mode);
      document.getElementById('learnModeDef').classList.toggle('active', mode === 'definition');
      document.getElementById('learnModeStory').classList.toggle('active', mode === 'story');
      _renderLearnContent();
    }
    document.getElementById('learnModeDef').classList.toggle('active', _learnMode === 'definition');
    document.getElementById('learnModeStory').classList.toggle('active', _learnMode === 'story');
    document.getElementById('learnModeDef').addEventListener('click', () => _setLearnMode('definition'));
    document.getElementById('learnModeStory').addEventListener('click', () => _setLearnMode('story'));

    let _learnRendered = false;
    async function _ensureLearnRendered() {
      if (_learnRendered) return;
      _learnRendered = true;
      await _renderLearnContent();
    }
    _ensureLearnRendered();   // cheap and idempotent -- render once, well before any (i) is tapped

    document.getElementById('glossarySearch').addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      document.querySelectorAll('#stratTagGlossary .learnEntry').forEach(el => {
        el.style.display = (!q || (el.dataset.search || '').includes(q)) ? '' : 'none';
      });
    });

    // PHASE 21: (i) icons are always visible everywhere now and deep-link into the Learn
    // tab instead of popping a tooltip -- switch tab, scroll the matching entry into view
    // (scrollIntoView finds #stratBody's own scrolling ancestor automatically), and pin a
    // persistent highlight on it (PHASE 27 follow-up: no longer auto-fades, and survives a
    // Definition/Story Time toggle since _renderLearnContent re-applies it from
    // _pinnedLearnKey on every render) -- click the highlighted entry itself to clear it.
    document.getElementById('strategyPanel').addEventListener('click', (e) => {
      const icon = e.target.closest('.stratLearnIcon');
      if (icon) {
        const key = icon.dataset.learn;
        const entry = document.getElementById('learnEntry-' + key);
        if (!entry) return;
        _switchStratTab('learn');
        entry.scrollIntoView({ behavior: 'smooth', block: 'start' });
        document.querySelectorAll('#stratLearnSections .learnEntry.learnPinned').forEach(el => el.classList.remove('learnPinned'));
        entry.classList.add('learnPinned');
        _pinnedLearnKey = key;
        return;
      }
      const pinned = e.target.closest('#stratLearnSections .learnEntry.learnPinned');
      if (pinned) {
        pinned.classList.remove('learnPinned');
        _pinnedLearnKey = null;
      }
    });

    window._openStrategy = openStrategy;
    window._closeStrategy = closeStrategy;
  })();

  window.addEventListener('keydown',e=>{
    if ((e.key==='k'||e.key==='K')&&(e.ctrlKey||e.metaKey)) {
      e.preventDefault();
      window._openCmdPalette?.();
      return;
    }
    if (e.key==='Escape'){ window._closeCmdPalette?.(); deactivateTool();hideCtx(); }
    if ((e.key==='z'||e.key==='Z')&&(e.ctrlKey||e.metaKey)&&!activeTool) {
      e.preventDefault();
      if (undoStack.length) { drawings=undoStack.pop(); saveDrawings(); updateObjTree(); markDirty(); }
      return;
    }
    if ((e.key==='c'||e.key==='C')&&(e.ctrlKey||e.metaKey)&&!activeTool&&
        !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
      if (hoverIndex>=0&&drawings[hoverIndex]) {
        _copiedDrawing=JSON.parse(JSON.stringify(drawings[hoverIndex]));
      }
      return;
    }
    if ((e.key==='v'||e.key==='V')&&(e.ctrlKey||e.metaKey)&&!activeTool&&
        !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
      if (_copiedDrawing) {
        e.preventDefault();
        pushUndo();
        const clone=JSON.parse(JSON.stringify(_copiedDrawing));
        // Offset slightly so the paste is visible as a new drawing
        const dt=currentCandles.length>=2?(currentCandles[1].time-currentCandles[0].time)*3:180;
        if (clone.time)  clone.time  += dt;
        if (clone.time1) clone.time1 += dt;
        if (clone.time2) clone.time2 += dt;
        if (clone.time3) clone.time3 += dt;
        if (clone.points) clone.points=clone.points.map(p=>({...p,time:p.time+dt}));
        drawings.push(clone);
        hoverIndex=drawings.length-1;
        saveDrawings(); updateObjTree(); markDirty();
      }
      return;
    }
    if ((e.key==='Delete'||e.key==='Backspace')&&!activeTool&&
        !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){
      pushUndo();drawings.pop();saveDrawings();updateObjTree();
    }
    // Number key tab shortcuts (no modifier, no input focused)
    if (!e.ctrlKey && !e.metaKey && !e.altKey &&
        !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) {
      const tabKeys = { '1':'trades', '2':'engine', '3':'layers', '4':'chat' };
      const tabTarget = tabKeys[e.key];
      if (tabTarget) {
        e.preventDefault();
        const tab = document.querySelector(`#intelTabBar [data-tab="${tabTarget}"]`);
        if (tab) tab.click();
        return;
      }
      // `/` — focus quick symbol search in topbar
      if (e.key === '/') {
        e.preventDefault();
        const qs = document.getElementById('quickSearchInput');
        if (qs) { qs.focus(); qs.select(); }
        return;
      }
      // `R` — run Dexter engine (works whenever ENGINE tab is active)
      if (e.key === 'r' || e.key === 'R') {
        const enginePane = document.getElementById('enginePane');
        if (enginePane && enginePane.classList.contains('active')) {
          e.preventDefault();
          runDexterEngine();
          return;
        }
      }
      // `Escape` while search focused — blur it
      if (e.key === 'Escape') {
        const ws = document.getElementById('watchlistSearch');
        if (ws && document.activeElement === ws) { ws.blur(); return; }
      }
    }
  });

  /* ---- Mouse: preview + hover + magnet dot ---- */
  chartWrap.addEventListener('mousemove',e=>{
    markDirty();
    const raw=rawPos(e);
    if (activeTool) {
      if (activeTool==='pencil') {
        cursorPos={x:raw.x,y:raw.y};
        if (pencilDrawing) {
          const anc={time:xToTime(raw.x),price:yToPrice(raw.y)};
          if (anc.time!=null&&anc.price!=null) {
            const last=pencilPoints[pencilPoints.length-1];
            const lx=last?timeToX(last.time):null, ly=last?priceToY(last.price):null;
            if (!last||lx==null||Math.hypot(raw.x-lx,raw.y-ly)>3) pencilPoints.push(anc);
            previewShape={type:'pencil',points:pencilPoints,color:TOOL_COLORS.pencil,lineWidth:1.5};
          }
        }
        return;
      }
      const s=snapMagnet(raw.x,raw.y);
      magnetDot=s.hit?{x:s.x,y:s.y}:null;
      cursorPos={x:s.x,y:s.y};
      const anc={x:s.x,y:s.y,price:yToPrice(s.y),time:xToTime(s.x)};
      if (anc.price==null||anc.time==null) return;
      const col=TOOL_COLORS[activeTool];
      if (activeTool==='hline') {
        previewShape={type:'hline',price:anc.price,color:TOOL_COLORS.hline,lineWidth:1.5}; return;
      }
      if (activeTool==='channel'&&channelStep===1&&channelBase) {
        const off=anc.price-basePriceAt(channelBase,anc.time);
        previewShape={type:'channel',...channelBase,priceOffset:off,color:TOOL_COLORS.channel}; return;
      }
      if (activeTool==='triangle'&&clickCount===2&&triangleP2&&firstClickPos) {
        previewShape={type:'triangle',time1:firstClickPos.time,price1:firstClickPos.price,time2:triangleP2.time,price2:triangleP2.price,time3:anc.time,price3:anc.price,color:col};
      } else if (clickCount===1&&firstClickPos) {
        if      (activeTool==='ray')       previewShape={type:'ray',      time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='trendline') previewShape={type:'trendline',time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='rect')      previewShape={type:'rect',     time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='fib')       previewShape={type:'fib',      time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='triangle')  previewShape={type:'trendline',time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='vp') { const lastT=currentCandles.length?currentCandles[currentCandles.length-1].time:anc.time; previewShape={type:'vp',time1:firstClickPos.time,price1:firstClickPos.price,time2:Math.min(anc.time,lastT),price2:anc.price,color:col}; }
        else if (activeTool==='measure')   previewShape={type:'measure',  time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:col};
        else if (activeTool==='channel')   previewShape={type:'trendline',time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price,color:TOOL_COLORS.channel};
      }
      return;
    }
    magnetDot=null;
    if (dragMode) return;
    let idx=hitTest(raw.x,raw.y);
    if (idx<0&&hoverXPos&&hoverIndex>=0&&Math.hypot(raw.x-hoverXPos.cx,raw.y-hoverXPos.cy)<20) {
      idx=hoverIndex;
    }
    if (idx!==hoverIndex) { hoverIndex=idx; markDirty(); }
    if (idx>=0) {
      const ep=getEndpointHit(raw.x,raw.y,drawings[idx]);
      if (ep!==hoverEP) hoverEP=ep;
      setCap(true, ep!=null?'crosshair':'move');
    } else {
      hoverEP=null;
      setCap(false);
    }
  });

  /* ---- Click: commit drawing ---- */
  chartWrap.addEventListener('click',e=>{
    // Shift+click: activate measure and place first point in one action
    if (e.shiftKey&&!activeTool&&!dragMode) {
      activeTool='measure'; clickCount=0; firstClickPos=null; previewShape=null;
      drawToolBtns.forEach(b=>b.classList.toggle('active',b.dataset.tool==='measure'));
      setCap(true,'crosshair');
    }
    if (!activeTool||dragMode) return;
    hideCtx();
    const raw=rawPos(e);
    const s=snapMagnet(raw.x,raw.y);
    const anc={price:yToPrice(s.y),time:xToTime(s.x)};
    if (anc.price==null||anc.time==null) return;
    const mk=(t,extra)=>({type:t,...extra,color:TOOL_COLORS[t]||TOOL_COLORS.hline,lineWidth:1.5,visible:true});
    if (activeTool==='hline') {
      pushUndo();drawings.push(mk('hline',{price:anc.price}));
      saveDrawings();previewShape=null;updateObjTree();markDirty();return;
    }
    if (activeTool==='channel'&&channelStep===1) {
      const off=anc.price-basePriceAt(channelBase,anc.time);
      drawings.push(mk('channel',{...channelBase,priceOffset:off}));
      saveDrawings();previewShape=null;channelBase=null;channelStep=0;clickCount=0;firstClickPos=null;updateObjTree();markDirty();return;
    }
    if (clickCount===0){firstClickPos=anc;clickCount=1;return;}
    if (activeTool==='triangle') {
      if (clickCount===1) { triangleP2=anc; clickCount=2; return; }
      // clickCount===2 → commit triangle
      pushUndo();
      drawings.push({type:'triangle',time1:firstClickPos.time,price1:firstClickPos.price,time2:triangleP2.time,price2:triangleP2.price,time3:anc.time,price3:anc.price,color:TOOL_COLORS.triangle,lineWidth:1.5,visible:true});
      saveDrawings();previewShape=null;clickCount=0;firstClickPos=null;triangleP2=null;updateObjTree();markDirty();return;
    }
    if (activeTool==='channel') {
      channelBase={time1:firstClickPos.time,price1:firstClickPos.price,time2:anc.time,price2:anc.price};
      channelStep=1;clickCount=0;firstClickPos=null;previewShape=null;return;
    }
    let t2=anc.time,p2=anc.price;
    if (activeTool==='vp'&&currentCandles.length) t2=Math.min(t2,currentCandles[currentCandles.length-1].time);
    const base={time1:firstClickPos.time,price1:firstClickPos.price,time2:t2,price2:p2};
    pushUndo();drawings.push({type:activeTool,...base,color:TOOL_COLORS[activeTool],lineWidth:1.5,visible:true});
    saveDrawings();previewShape=null;clickCount=0;firstClickPos=null;updateObjTree();markDirty();
  });

  /* ---- Drag / move ---- */
  drawCanvas.addEventListener('mousedown',e=>{
    if (e.button!==0) return;
    if (activeTool==='pencil') {
      pencilDrawing=true; pencilPoints=[];
      const raw=rawPos(e);
      const anc={time:xToTime(raw.x),price:yToPrice(raw.y)};
      if (anc.time!=null&&anc.price!=null) pencilPoints.push(anc);
      e.stopPropagation(); return;
    }
    if (activeTool) return;
    const raw=rawPos(e);
    if (hoverXPos&&hoverIndex>=0&&Math.hypot(raw.x-hoverXPos.cx,raw.y-hoverXPos.cy)<14) {
      pushUndo();drawings.splice(hoverIndex,1);hoverIndex=-1;hoverXPos=null;saveDrawings();updateObjTree();
      e.stopPropagation();return;
    }
    const idx=hitTest(raw.x,raw.y); if (idx<0) return;
    dragEndpoint=getEndpointHit(raw.x,raw.y,drawings[idx]);
    if ((drawings[idx].type==='vp'||drawings[idx].type==='channel')&&dragEndpoint===null) return; // endpoints only
    if ((drawings[idx].type==='rect'||drawings[idx].type==='measure')&&dragEndpoint===null) return; // corners only — no body drag
    dragMode=true;dragIndex=idx;dragStartRaw=raw;dragOrigDraw=JSON.parse(JSON.stringify(drawings[idx]));
    e.stopPropagation();
  });
  window.addEventListener('mousemove',e=>{
    if (!dragMode) return;
    const raw=rawPos(e),d=drawings[dragIndex],orig=dragOrigDraw;
    if (dragEndpoint!==null) {
      // VP anchors: snap by time (X) only — dots are at y=18, far from candle prices
      if (d.type==='vp') {
        if (!currentCandles.length) { markDirty(); return; }
        const nearestCandle = currentCandles.reduce((best,c)=>{
          const cx=timeToX(c.time); if (cx==null) return best;
          return Math.abs(cx-raw.x)<Math.abs((timeToX(best.time)||Infinity)-raw.x)?c:best;
        }, currentCandles[0]);
        if (nearestCandle) {
          const sx=timeToX(nearestCandle.time);
          magnetDot=sx!=null?{x:sx,y:18}:null;
          if (dragEndpoint===1) d.time1=nearestCandle.time;
          else if (dragEndpoint===2) d.time2=nearestCandle.time;
        }
        markDirty(); return;
      }
      // Endpoint drag: full magnet snap to nearest OHLC
      const s=snapMagnet(raw.x,raw.y);
      magnetDot=s.hit?{x:s.x,y:s.y}:null;
      const np=yToPrice(s.y),nt=xToTime(s.x);
      if (dragEndpoint===1) {
        if (d.type==='hline'){if(np!=null)d.price=np;}
        else {
          if(np!=null)d.price1=np;
          if(nt!=null) d.time1=nt;
        }
      } else if (dragEndpoint===2) {
        if(np!=null)d.price2=np;
        if(nt!=null) d.time2=nt;
      } else if (dragEndpoint===3) {
        if (d.type==='triangle') {
          if(np!=null)d.price3=np;
          if(nt!=null)d.time3=nt;
        } else if (d.type==='rect'||d.type==='measure') {
          // Corner (time1, price2)
          if(np!=null)d.price2=np;
          if(nt!=null)d.time1=nt;
        } else {
          // Channel bottom midpoint — slide the parallel line up/down
          if(np!=null) d.priceOffset=np-basePriceAt(orig,(orig.time1+orig.time2)/2);
        }
      } else if (dragEndpoint===4) {
        if (d.type==='rect'||d.type==='measure') {
          // Corner (time2, price1)
          if(np!=null)d.price1=np;
          if(nt!=null)d.time2=nt;
        }
      }
    } else {
      magnetDot=null;
      const p0=yToPrice(dragStartRaw.y),p1=yToPrice(raw.y),dp=(p0!=null&&p1!=null)?p1-p0:0;
      const t0=xToTime(dragStartRaw.x),t1=xToTime(raw.x),dt=(t0!=null&&t1!=null)?t1-t0:0;
      if (d.type==='hline') d.price=orig.price+dp;
      else if (d.type==='pencil'&&d.points) {
        d.points=orig.points.map(pt=>({time:pt.time+dt,price:pt.price+dp}));
      } else if (d.type==='triangle') {
        d.price1=orig.price1+dp;d.price2=orig.price2+dp;d.price3=orig.price3+dp;
        d.time1=orig.time1+dt; d.time2=orig.time2+dt; d.time3=orig.time3+dt;
      }
      else{d.price1=orig.price1+dp;d.price2=orig.price2+dp;d.time1=orig.time1+dt;d.time2=orig.time2+dt;}
    }
  });
  window.addEventListener('mouseup',()=>{
    if (activeTool==='pencil'&&pencilDrawing) {
      pencilDrawing=false;
      if (pencilPoints.length>=2) {
        pushUndo();
        drawings.push({type:'pencil',points:[...pencilPoints],color:TOOL_COLORS.pencil,lineWidth:1.5,visible:true});
        saveDrawings();updateObjTree();
      }
      pencilPoints=[];previewShape=null;
      return;
    }
    if (!dragMode) return;
    pushUndo();saveDrawings();updateObjTree();
    dragMode=false;dragIndex=-1;dragStartRaw=null;dragOrigDraw=null;dragEndpoint=null;magnetDot=null;
  });

  /* ---- Right-click: context menu ---- */
  chartWrap.addEventListener('contextmenu',e=>{
    e.preventDefault();
    if (activeTool){deactivateTool();return;}   // right-click always exits tool mode
    const raw=rawPos(e), idx=hitTest(raw.x,raw.y);
    if (idx>=0) showCtx(idx,e.clientX,e.clientY); else hideCtx();
  });

  /* ---- Context menu ---- */
  const drawCtxEl=document.getElementById('drawCtx');
  function showCtx(idx,cx,cy) {
    ctxTarget=idx; const d=drawings[idx];
    document.getElementById('ctxTypeLabel').textContent=drawingLabel(d).toUpperCase();
    document.getElementById('ctxWidth').value=dv(d,'lineWidth',1.5);
    document.getElementById('ctxWidthVal').textContent=dv(d,'lineWidth',1.5)+'px';
    document.getElementById('ctxText').value=dv(d,'text','');
    document.getElementById('ctxFontSize').value=dv(d,'fontSize',9);
    document.getElementById('ctxFontVal').textContent=dv(d,'fontSize',9)+'px';
    document.getElementById('ctxFill').value=dv(d,'fillOpacity',0.12);
    document.getElementById('ctxFillVal').textContent=Math.round(dv(d,'fillOpacity',0.12)*100)+'%';
    document.getElementById('ctxFillRow').style.display=d.type==='rect'?'flex':'none';
    document.getElementById('ctxOpacity').value=dv(d,'opacity',1);
    document.getElementById('ctxOpacityVal').textContent=Math.round(dv(d,'opacity',1)*100)+'%';
    document.querySelectorAll('#ctxColors .swatch').forEach(sw=>sw.classList.toggle('active',sw.dataset.color===d.color));
    document.querySelectorAll('#ctxAligns button').forEach(b=>b.classList.toggle('active',b.dataset.align===dv(d,'textAlign','right')));
    const _defPos = (d.type==='rect'||d.type==='fib'||d.type==='channel'||d.type==='hline') ? 'top-left' : 'top-right';
    document.querySelectorAll('#ctxLabelPos button').forEach(b=>b.classList.toggle('active',b.dataset.pos===(d.labelPos||_defPos)));
    drawCtxEl.style.left=Math.min(cx,window.innerWidth-230)+'px';
    drawCtxEl.style.top =Math.min(cy,window.innerHeight-270)+'px';
    drawCtxEl.style.display='block';
    renderTplList();
  }
  function hideCtx(){drawCtxEl.style.display='none';ctxTarget=-1;}
  function ctxApply(k,v){if (ctxTarget>=0){drawings[ctxTarget][k]=v;saveDrawings();}}

  document.getElementById('ctxWidth').addEventListener('input',function(){
    document.getElementById('ctxWidthVal').textContent=this.value+'px'; ctxApply('lineWidth',parseFloat(this.value));
  });
  document.getElementById('ctxFill').addEventListener('input',function(){
    document.getElementById('ctxFillVal').textContent=Math.round(this.value*100)+'%'; ctxApply('fillOpacity',parseFloat(this.value));
  });
  document.getElementById('ctxOpacity').addEventListener('input',function(){
    document.getElementById('ctxOpacityVal').textContent=Math.round(this.value*100)+'%';
    ctxApply('opacity',parseFloat(this.value)); redrawAll();
  });
  document.getElementById('ctxText').addEventListener('input',function(){ctxApply('text',this.value);});
  document.getElementById('ctxFontSize').addEventListener('input',function(){
    document.getElementById('ctxFontVal').textContent=this.value+'px'; ctxApply('fontSize',parseInt(this.value));
  });
  document.getElementById('ctxDelete').addEventListener('click',()=>{
    if (ctxTarget>=0){drawings.splice(ctxTarget,1);saveDrawings();updateObjTree();}hideCtx();
  });
  document.querySelectorAll('#ctxColors .swatch').forEach(sw=>{
    sw.addEventListener('click',()=>{
      ctxApply('color',sw.dataset.color);
      document.querySelectorAll('#ctxColors .swatch').forEach(s=>s.classList.remove('active'));
      sw.classList.add('active');
    });
  });
  document.querySelectorAll('#ctxAligns button').forEach(btn=>{
    btn.addEventListener('click',()=>{
      ctxApply('textAlign',btn.dataset.align);
      document.querySelectorAll('#ctxAligns button').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
  document.querySelectorAll('#ctxLabelPos button').forEach(btn=>{
    btn.addEventListener('click',()=>{
      ctxApply('labelPos',btn.dataset.pos);
      document.querySelectorAll('#ctxLabelPos button').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      redrawAll();
    });
  });
  document.getElementById('ctxClose').addEventListener('click', hideCtx);

  // Draggable context menu
  (function(){
    const handle=document.getElementById('ctxDragHandle');
    const panel=drawCtxEl;
    let dragging=false,ox=0,oy=0;
    handle.addEventListener('mousedown',e=>{
      if(e.target.id==='ctxClose') return;
      dragging=true; ox=e.clientX-panel.offsetLeft; oy=e.clientY-panel.offsetTop;
      e.preventDefault();
    });
    document.addEventListener('mousemove',e=>{
      if(!dragging) return;
      const x=Math.max(0,Math.min(e.clientX-ox,window.innerWidth-panel.offsetWidth));
      const y=Math.max(0,Math.min(e.clientY-oy,window.innerHeight-panel.offsetHeight));
      panel.style.left=x+'px'; panel.style.top=y+'px';
    });
    document.addEventListener('mouseup',()=>{dragging=false;});
  })();

  /* ---- Drawing templates ---- */
  const TPL_KEY='chevDrawTemplates';
  function loadTemplates(){try{return JSON.parse(localStorage.getItem(TPL_KEY)||'[]');}catch(e){return[];}}
  function saveTemplates(t){localStorage.setItem(TPL_KEY,JSON.stringify(t));}
  function renderTplList(){
    const list=document.getElementById('ctxTplList'); if(!list) return;
    const tpls=loadTemplates();
    if(!tpls.length){list.innerHTML='<span style="color:#5d6068;font-size:9px;">No templates yet</span>';return;}
    list.innerHTML=tpls.map((t,i)=>`
      <span style="display:inline-flex;align-items:center;gap:2px;background:#1e222d;border:1px solid #2a2e39;border-radius:3px;padding:1px 5px;cursor:pointer;font-size:9px;color:#d1d4dc;" data-tpl="${i}">
        <span style="width:8px;height:8px;border-radius:50%;background:${t.color};display:inline-block;flex-shrink:0;"></span>
        ${t.name}
        <span class="tplDel" data-tpl="${i}" style="color:#5d6068;margin-left:2px;cursor:pointer;">✕</span>
      </span>`).join('');
    list.querySelectorAll('[data-tpl]').forEach(el=>{
      if(el.classList.contains('tplDel')) return;
      el.addEventListener('click',()=>{
        const tpl=loadTemplates()[+el.dataset.tpl]; if(!tpl||ctxTarget<0) return;
        Object.assign(drawings[ctxTarget],{
          color:tpl.color, lineWidth:tpl.lineWidth, text:tpl.text,
          textAlign:tpl.textAlign, fontSize:tpl.fontSize,
        });
        saveDrawings(); showCtx(ctxTarget, drawCtxEl.offsetLeft, drawCtxEl.offsetTop);
      });
    });
    list.querySelectorAll('.tplDel').forEach(el=>{
      el.addEventListener('click',e=>{
        e.stopPropagation();
        const tpls=loadTemplates(); tpls.splice(+el.dataset.tpl,1); saveTemplates(tpls); renderTplList();
      });
    });
  }
  document.getElementById('ctxTplSave').addEventListener('click',()=>{
    if(ctxTarget<0) return;
    const name=document.getElementById('ctxTplName').value.trim()||'Template';
    const d=drawings[ctxTarget];
    const tpl={name, color:d.color||'#9598a1', lineWidth:dv(d,'lineWidth',1.5),
      text:dv(d,'text',''), textAlign:dv(d,'textAlign','right'), fontSize:dv(d,'fontSize',9)};
    const tpls=loadTemplates(); tpls.push(tpl); saveTemplates(tpls);
    document.getElementById('ctxTplName').value=''; renderTplList();
  });

  /* ---- Object tree ---- */
  const objTreeEl=document.getElementById('objectTree');
  const objTreeToggleBtn=document.getElementById('objectTreeToggle');
  // 2026-07-05 fix: this button toggled the PANEL's .open class but never touched
  // its own — with no active-state CSS anywhere for it either, it looked identically
  // grey whether the Drawings panel was open or closed. Now syncs a class onto the
  // button itself so it can actually show which state it's in (see .iconBtn.active CSS).
  objTreeToggleBtn.addEventListener('click',()=>{
    objTreeEl.classList.toggle('open');
    objTreeToggleBtn.classList.toggle('active', objTreeEl.classList.contains('open'));
  });
  document.getElementById('objClose').addEventListener('click',()=>{
    objTreeEl.classList.remove('open');
    objTreeToggleBtn.classList.remove('active');
  });

  function drawingLabel(d) {
    const labels={hline:'H.Line',ray:'Ray',trendline:'Trendline',rect:'Rectangle',fib:'Fibonacci',channel:'Channel',triangle:'Triangle',vp:'Vol Profile',measure:'Measure',pencil:'Pencil'};
    const base=labels[d.type]||d.type;
    if (d.type==='hline'&&d.price) return base+' @ '+d.price.toFixed(4);
    if (d.text) return base+': '+d.text.slice(0,16);
    return base;
  }

  function updateObjTree() {
    const list=document.getElementById('objectList');
    if (!list) return;
    if (!drawings.length){list.innerHTML='<div style="padding:10px;color:#5d6068;font-size:11px;">No drawings yet.</div>';}
    else {
      list.innerHTML=drawings.map((d,i)=>`
        <div class="objRow" data-i="${i}">
          <span class="objColor" style="background:${d.color||'#fff'}"></span>
          <span class="objLabel">${drawingLabel(d)}</span>
          <button class="objEye" data-i="${i}">${d.visible===false?'🚫':'👁'}</button>
          <button class="objDel" data-i="${i}">✕</button>
        </div>`).join('');
      list.querySelectorAll('.objEye').forEach(btn=>btn.addEventListener('click',e=>{
        e.stopPropagation();const i=+btn.dataset.i;
        drawings[i].visible=drawings[i].visible===false;saveDrawings();redrawAll();updateObjTree();
      }));
      list.querySelectorAll('.objDel').forEach(btn=>btn.addEventListener('click',e=>{
        e.stopPropagation();drawings.splice(+btn.dataset.i,1);saveDrawings();redrawAll();updateObjTree();
      }));
    }
    // RSI section — only shown when RSI panel is active
    const rsiSection = document.getElementById('rsiObjSection');
    const rsiList    = document.getElementById('rsiObjectList');
    if (!rsiSection || !rsiList) return;
    const showRsi = _indicatorState.rsi && rsiDrawings.length > 0;
    rsiSection.style.display = showRsi ? '' : 'none';
    if (!showRsi) return;
    const _rsiTypeLabel = { trendline: 'Trend', hline: 'H-Line', ray: 'Ray' };
    rsiList.innerHTML = rsiDrawings.map((d, i) => `
      <div class="objRow" data-ri="${i}">
        <span class="objColor" style="background:${d.color||'#f23645'}"></span>
        <span class="objLabel">RSI ${_rsiTypeLabel[d.type] || 'Trend'}</span>
        <button class="rsiObjEye" data-ri="${i}">${d.visible===false?'🚫':'👁'}</button>
        <button class="rsiObjDel" data-ri="${i}">✕</button>
      </div>`).join('');
    rsiList.querySelectorAll('.rsiObjEye').forEach(btn => btn.addEventListener('click', e => {
      e.stopPropagation();
      const i = +btn.dataset.ri;
      rsiDrawings[i].visible = rsiDrawings[i].visible === false ? true : false;
      saveRsiDrawings(); rsiRedrawAll(); updateObjTree();
    }));
    rsiList.querySelectorAll('.rsiObjDel').forEach(btn => btn.addEventListener('click', e => {
      e.stopPropagation();
      rsiDrawings.splice(+btn.dataset.ri, 1);
      saveRsiDrawings(); rsiRedrawAll(); updateObjTree();
    }));
  }
  updateObjTree();

