/* ============================================================
   Radar multi-symbol Dexter scan + Chev's hypothetical entry.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     RADAR — multi-symbol Dexter scan
     ============================================================ */
  const _radarResults  = {};
  let   _radarScanning = false;

  function _getAllWatchlistSymbols() {
    const seen = new Set();
    const out  = [];
    Object.values(groups).forEach(list => {
      list.forEach(item => {
        if (!seen.has(item.symbol)) { seen.add(item.symbol); out.push(item); }
      });
    });
    return out;
  }

  function _getStarredWatchlistSymbols() {
    return _getAllWatchlistSymbols().filter(item => item.starred);
  }

  function _radarCardHTML(item, result) {
    const sym  = item.symbol;
    const id   = cssId(sym);
    const hasResult = !!result;
    const state   = result?.state || result?.market_state;
    const topHyp  = (result?.hypotheses || [])[0];
    const regime  = state?.regime || '';
    const isBull  = regime.includes('BULL');
    const isBear  = regime.includes('BEAR');
    const biasCls = !hasResult ? 'idle' : isBull ? 'bull' : isBear ? 'bear' : 'range';
    const biasLbl = !hasResult ? 'NOT SCANNED' : isBull ? '▲ BULL' : isBear ? '▼ BEAR' : '◆ RANGE';
    const conf    = topHyp ? Math.round((topHyp.confidence || 0) * 100) : null;
    const confCls = conf == null ? '' : conf >= 65 ? 'high' : conf >= 40 ? 'mid' : 'low';
    const hypName = topHyp ? (topHyp.name || topHyp.label || '').replace(/_/g, ' ') : (hasResult ? 'No data' : "Click to ask Chev where he'd enter");
    const logo    = item.type === 'crypto' ? `<img class="logo" src="${iconUrl(item.icon || item.symbol.replace('USDT','').toLowerCase())}" alt="" onerror="this.style.display='none'" style="width:16px;height:16px;border-radius:50%;flex-shrink:0">` : '';
    return `<div class="radarCard ${biasCls}" data-sym="${sym}" data-type="${item.type||'crypto'}">
      <div class="radarCardTop" title="Click to ask Chev where he'd enter this, even if he'd skip it live">
        ${logo}
        <span class="radarSym">${sym.replace('USDT','')}</span>
        <span class="radarBias ${biasCls}">${biasLbl}</span>
        ${conf != null ? `<span class="radarConf ${confCls}">${conf}%</span>` : ''}
      </div>
      <div class="radarHypName">${hypName}</div>
      ${hasResult ? `<div class="radarConfBar"><div class="radarConfFill ${biasCls}" style="width:${conf||0}%"></div></div>` : ''}
      <div class="radarHypoSlot" id="hypoSlot-${id}"></div>
    </div>`;
  }

  function _renderRadar() {
    const grid = document.getElementById('radarGrid');
    if (!grid) return;
    const items = _getStarredWatchlistSymbols();
    if (!items.length) {
      grid.innerHTML = '<div id="radarEmpty">★ Star pairs in the watchlist to track them here.</div>';
      return;
    }
    const tf = document.getElementById('radarTf')?.value || '1h';
    grid.innerHTML = items.map(item => _radarCardHTML(item, _radarResults[item.symbol])).join('');
    grid.querySelectorAll('.radarCard[data-sym]').forEach(card => {
      const sym  = card.dataset.sym;
      const type = card.dataset.type || 'crypto';
      // The whole pill (card) is the button — not just the symbol text.
      card.addEventListener('click', (e) => {
        if (e.target.closest('.radarHypoSlot')) return; // let tag pills / re-ask / answer-card handle their own clicks
        _askChevHypothetical(sym, type, tf, card);
      });
      const cached = _loadHypoCache(sym, tf);
      if (cached) _renderHypoSlot(sym, cached.data, cached.ts);
    });
  }

  async function _radarScanAll() {
    if (_radarScanning) return;
    _radarScanning = true;
    const btn      = document.getElementById('radarScanBtn');
    const progress = document.getElementById('radarProgress');
    const tf       = document.getElementById('radarTf')?.value || '1h';
    const items    = _getStarredWatchlistSymbols();
    if (!items.length) { if (progress) progress.textContent = 'No starred symbols — star some in the watchlist first'; _radarScanning = false; return; }
    if (btn) { btn.disabled = true; btn.innerHTML = '<img src="emoji/time.png" alt="" style="width:13px;height:13px;margin-right:5px;opacity:0.85;vertical-align:middle">Scanning...'; }
    // Show skeleton cards
    const grid = document.getElementById('radarGrid');
    if (grid) grid.innerHTML = items.map(item => `<div class="radarCard scanning" style="min-height:64px"></div>`).join('');
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (progress) progress.textContent = `${i + 1} / ${items.length} — ${item.symbol}`;
      try {
        const r = await _apiFetch(`/api/analysis/engine?symbol=${encodeURIComponent(item.symbol)}&tf=${tf}`);
        if (r.ok) {
          const data = await r.json();
          if (!data.error) {
            _radarResults[item.symbol] = data;
            _saveEngineCache(item.symbol, tf, data);
          }
        }
      } catch(e) { /* skip symbol on error */ }
      _renderRadar();
      // Small delay to avoid flooding Dexter
      await new Promise(res => setTimeout(res, 400));
    }
    if (progress) progress.textContent = `Done — ${items.length} symbols`;
    if (btn) { btn.disabled = false; btn.innerHTML = '<img src="emoji/tools.png" alt="" style="width:13px;height:13px;margin-right:5px;opacity:0.85;vertical-align:middle">Scan All'; }
    _radarScanning = false;
    showNotification('Radar complete', `${items.length} symbols analyzed on ${tf.toUpperCase()}`, 'success', 'not-bad-2.png', 5000);
  }

  /* ============================================================
     RADAR — Chev's hypothetical entry ("where would you enter
     this, even if you'd skip it live"). Real one-off LLM call,
     cached client-side only — never posted/logged/traded.
     ============================================================ */
  function _hypoIdeaObj(sym, data) {
    return {
      row: 'hypo-' + sym,
      pair: sym,
      tf: data.tf,
      direction: data.direction,
      entry: data.entry, sl: data.sl, tp: data.tp,
      tags: data.tags,
      confluence_prices: data.confluence_prices || {},
      reason: data.reasoning,
    };
  }

  function _hypoCardHTML(sym, data, ts) {
    const isLong  = (data.direction || '').toLowerCase() === 'long';
    const dirCls  = isLong ? 'long' : 'short';
    const dirArrow = isLong ? '⬆' : '⬇';
    const row     = 'hypo-' + sym;
    const _ps     = _getPillState(row);
    const tags    = (data.tags || '').split(',').map(t => t.trim()).filter(Boolean);
    const tagChips = tags.map(t => {
      const stats = _tagStatsCache && _tagStatsCache[t];
      const trusted = stats && stats.n >= 3;
      const col  = trusted ? winRateColor(stats.win_rate * 100) : null;
      const style = col ? ` style="border-color:${col};color:${col}"` : '';
      const title = stats ? ` title="${Math.round(stats.win_rate*100)}% real win rate (n=${stats.n})"` : '';
      return `<button class="ideaPill${_ps[t] ? ' active' : ''}" data-tag="${t}" data-row="${row}"${style}${title}>${esc(t)}</button>`;
    }).join('');
    const rrChip = data.planned_rr != null
      ? `<span class="ideaRRChip"><img src="emoji/ruler.png" alt="" style="width:9px;height:9px;margin-right:2px;vertical-align:middle;opacity:0.7">R:R ${data.planned_rr.toFixed(1)}</span>`
      : '';
    const wouldTakeChip = data.would_take === true
      ? `<span class="ideaLevChip" style="background:rgba(8,153,129,0.15);color:var(--green)">WOULD TAKE</span>`
      : data.would_take === false
      ? `<span class="ideaLevChip" style="background:rgba(242,54,69,0.15);color:var(--red)">WOULD SKIP</span>` : '';
    const ago = Math.max(0, Math.round((Date.now() - ts) / 60000));
    const agoStr = ago < 1 ? 'just now' : ago < 60 ? ago + 'm ago' : Math.round(ago / 60) + 'h ago';
    const noteHtml = data.reasoning ? `<div class="ideaNote">${String(data.reasoning).replace(/</g, '&lt;')}</div>` : '';
    const tagLabel = tagChips ? `<div class="radarHypoTagLabel">Tools Chev confirmed (didn't clear the confluence threshold):</div>` : '';
    return `<div class="ideaCard ${dirCls}" data-row="${row}">
      <div class="ideaCardTop">
        <span class="ideaDirBadge ${dirCls}">${dirArrow} ${(data.direction || '').toUpperCase()}</span>
        <span class="ideaPairName">Hypothetical</span>
        ${rrChip}${wouldTakeChip}
      </div>
      ${tagLabel}
      ${tagChips ? `<div class="ideaTagRow">${tagChips}</div>` : ''}
      <div class="ideaPriceRow">
        <button class="ideaPill ideaPillE${_ps['_E'] ? ' active' : ''}" data-tag="_E" data-row="${row}">E ${data.entry}</button>
        <button class="ideaPill ideaPillSL${_ps['_SL'] ? ' active' : ''}" data-tag="_SL" data-row="${row}">SL ${data.sl}</button>
        <button class="ideaPill ideaPillTP${_ps['_TP'] ? ' active' : ''}" data-tag="_TP" data-row="${row}">TP ${data.tp}</button>
      </div>
      ${noteHtml}
      <div class="radarHypoMeta">Asked ${agoStr} · <span class="radarHypoReask">re-ask</span></div>
    </div>`;
  }

  function _renderHypoSlot(sym, data, ts) {
    const slot = document.getElementById('hypoSlot-' + cssId(sym));
    if (!slot) return;
    slot.innerHTML = _hypoCardHTML(sym, data, ts);
    const card = slot.querySelector('.ideaCard');
    const row  = card.dataset.row;
    card.addEventListener('click', (e) => {
      if (e.target.closest('.ideaPill') || e.target.closest('.radarHypoReask')) return;
      selectIdea(_hypoIdeaObj(sym, data));
    });
    slot.querySelectorAll('.ideaPill').forEach(pill => {
      pill.addEventListener('click', (e) => {
        e.stopPropagation();
        const tag  = pill.dataset.tag;
        const isOn = !pill.classList.contains('active');
        pill.classList.toggle('active', isOn);
        _setPillState(row, tag, isOn);
        if (String(activeIdeaRow) === row) _applyIdeaPill(_hypoIdeaObj(sym, data), tag, isOn);
      });
    });
    const reaskEl = slot.querySelector('.radarHypoReask');
    if (reaskEl) {
      reaskEl.addEventListener('click', (e) => {
        e.stopPropagation();
        const card2 = slot.closest('.radarCard');
        const type  = card2?.dataset.type || 'crypto';
        const tf    = document.getElementById('radarTf')?.value || data.tf || '1h';
        if (card2) _askChevHypothetical(sym, type, tf, card2, true);
      });
    }
  }

  // Per-symbol anti-spam guard: once Chev's asked, ignore repeat clicks for 30s —
  // regardless of how fast the reply actually comes back — so an eager click
  // doesn't fire the same "contacting Chev" request over and over.
  const _hypoCooldownUntil = {};

  async function _askChevHypothetical(sym, type, tf, card, forceRefresh) {
    const now = Date.now();
    if (!forceRefresh && now < (_hypoCooldownUntil[sym] || 0)) return; // still cooling down
    const slot = card.querySelector('.radarHypoSlot') || document.getElementById('hypoSlot-' + cssId(sym));

    if (!forceRefresh) {
      const cached = _loadHypoCache(sym, tf);
      if (cached) { _renderHypoSlot(sym, cached.data, cached.ts); return; }
    }

    _hypoCooldownUntil[sym] = now + 30000;
    card.classList.add('cooling');
    setTimeout(() => { card.classList.remove('cooling'); }, 30000);

    // Live elapsed-time ticker — Chev's calls are globally rate-limited (shared with
    // the live scanner, min ~65s between ANY call to protect the Gemini quota), so an
    // on-demand ask can sit queued for a while with zero server-side activity. Without
    // this the UI looks frozen; with it, the wait is at least visibly still alive.
    const startedAt = Date.now();
    const tick = () => {
      if (!slot) return;
      const secs = Math.round((Date.now() - startedAt) / 1000);
      const note = secs > 60
        ? ' — Chev may be mid-conversation with the live scanner (calls are queued, not parallel), this can take a couple minutes'
        : '';
      slot.innerHTML = `<div class="radarHypoLoading">💬 Contacting Chev… (${secs}s)${note}</div>`;
    };
    tick();
    const tickTimer = setInterval(tick, 1000);
    // Client-side safety net: Dexter now bounds its own internal waits (candle fetch 30s,
    // Chev's reply 200s), so a well-formed response always arrives well under 4 minutes.
    // If this fires instead, the request never reached Flask at all (dead connection/tunnel).
    const abortCtrl = new AbortController();
    const abortTimer = setTimeout(() => abortCtrl.abort(), 240000);

    try {
      await _ensureTagStats();
      const r = await _apiFetch('/api/analysis/hypothetical', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, tf }),
        signal: abortCtrl.signal,
      });
      let data;
      try {
        data = await r.json();
      } catch (parseErr) {
        console.error('[Hypothetical] response was not JSON', r.status, parseErr);
        clearInterval(tickTimer); clearTimeout(abortTimer);
        if (slot) slot.innerHTML = `<div class="radarHypoError">Dexter returned an unreadable response (HTTP ${r.status}) — check Dexter's console</div>`;
        return;
      }
      if (!r.ok || data.error) {
        console.warn('[Hypothetical] error response', r.status, data);
        clearInterval(tickTimer); clearTimeout(abortTimer);
        if (slot) slot.innerHTML = `<div class="radarHypoError">${esc(data.error || `HTTP ${r.status}`)}</div>`;
        return;
      }
      data.tf = tf;
      _saveHypoCache(sym, tf, data);
      clearInterval(tickTimer); clearTimeout(abortTimer);
      // "Chev answered" IS this: the loading line is replaced by the full trade card below.
      _renderHypoSlot(sym, data, Date.now());
      // Auto-draw entry/SL/TP lines + red/green zones immediately — no extra click needed
      selectIdea(_hypoIdeaObj(sym, data));
    } catch (e) {
      clearInterval(tickTimer); clearTimeout(abortTimer);
      const timedOut = e.name === 'AbortError';
      console.error('[Hypothetical] fetch failed', timedOut ? '(client-side 4min abort — request never completed)' : e);
      if (slot) slot.innerHTML = timedOut
        ? `<div class="radarHypoError">Timed out after 4 minutes — the request never got a response. Check Dexter's console for errors, or that it's still running.</div>`
        : `<div class="radarHypoError">Dexter unreachable — ${esc(e.message || 'network error')}</div>`;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    const radarBtn = document.getElementById('radarScanBtn');
    if (radarBtn) radarBtn.addEventListener('click', _radarScanAll);
  });

