/* ============================================================
   Watchlist sparklines + proximity alerts.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
   /* ============================================================
      SPARKLINES — live mini price charts in watchlist
      NOTE: the shared mutable globals _sparkBuffers, _alertFired,
      PROXIMITY_PCT and SPARK_MAX are declared in js/config/state.js
      (loaded first) so they are available to ui/watchlist.js at boot,
      which references _sparkBuffers during its initial refresh.
      ============================================================ */

   function _pushSpark(symbol, price) {
    if (!_sparkBuffers[symbol]) _sparkBuffers[symbol] = [];
    const buf = _sparkBuffers[symbol];
    buf.push(price);
    if (buf.length > SPARK_MAX) buf.shift();
    _drawSparkline(symbol);
  }

  function _drawSparkline(symbol) {
    const canvas = document.getElementById('sp-' + cssId(symbol));
    if (!canvas || !canvas.getContext) return;
    const buf = _sparkBuffers[symbol];
    if (!buf || buf.length < 2) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    const min = Math.min(...buf), max = Math.max(...buf);
    const range = max - min || 1;
    const isUp  = buf[buf.length - 1] >= buf[0];
    const lineColor = isUp ? '#089981' : '#f23645';
    const fillColor = isUp ? 'rgba(8,153,129,0.12)' : 'rgba(242,54,69,0.12)';
    const pts = buf.map((p, i) => ({
      x: (i / (buf.length - 1)) * (W - 2) + 1,
      y: H - 2 - ((p - min) / range) * (H - 6)
    }));
    // Filled area
    ctx.beginPath();
    ctx.moveTo(pts[0].x, H);
    pts.forEach(pt => ctx.lineTo(pt.x, pt.y));
    ctx.lineTo(pts[pts.length - 1].x, H);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();
    // Line
    ctx.beginPath();
    pts.forEach((pt, i) => i === 0 ? ctx.moveTo(pt.x, pt.y) : ctx.lineTo(pt.x, pt.y));
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap  = 'round';
    ctx.stroke();
    // Endpoint dot
    const last = pts[pts.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 2, 0, Math.PI * 2);
    ctx.fillStyle = lineColor;
    ctx.fill();
  }

  /* ============================================================
     PROXIMITY ALERTS — fires when price enters Chev's entry zone
     (_alertFired / PROXIMITY_PCT declared in js/config/state.js)
     ============================================================ */

  function _checkProximityAlert(symbol, price) {
    if (!currentIdeas || !currentIdeas.length) return;
    const ideas = currentIdeas.filter(i => {
      const s = (i.pair || i.symbol || i.ticker || '').toUpperCase();
      return s === symbol.toUpperCase();
    });
    ideas.forEach(idea => {
      const entry = parseFloat(idea.entry);
      if (!entry) return;
      const key = symbol + '_' + idea.row;
      const pct = Math.abs(price - entry) / entry;
      if (pct <= PROXIMITY_PCT) {
        const now = Date.now();
        // Re-fire only after 5 min silence per key
        if (!_alertFired[key] || (now - _alertFired[key]) > 300000) {
          _alertFired[key] = now;
          const dir = (idea.direction || '').toUpperCase() === 'LONG' ? '▲' : '▼';
          const side = (idea.direction || '').toUpperCase() === 'LONG' ? 'long' : 'short';
          showNotification(
            `${dir} ${symbol} entering zone`,
            `${side} entry at ${entry.toLocaleString()} — Chev's setup is live`,
            'warning', 'fire.png', 9000
          );
          // Flash the idea card
          const card = chevIdeasEl?.querySelector(`.ideaCard[data-row="${idea.row}"]`);
          if (card) {
            card.classList.add('zone-pulse', 'zone-alert');
            setTimeout(() => { card.classList.remove('zone-pulse'); }, 2500);
          }
        }
      } else if (pct > PROXIMITY_PCT * 4) {
        // Reset when price moves far away so alert can fire again
        delete _alertFired[key];
        const card = chevIdeasEl?.querySelector(`.ideaCard[data-row="${idea.row}"]`);
        if (card) card.classList.remove('zone-alert');
      }
    });
  }

