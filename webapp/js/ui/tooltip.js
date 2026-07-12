/* ============================================================
   PHASE 3 Task 3: delayed plain-word tooltips.
   Any element carrying data-tip="..." gets a small custom tooltip after a
   2000ms hover delay, positioned above the element (or below if there's no
   room), gone instantly on mouseleave/click. Replaces native title="" (slow,
   browser-styled, and several of ours read like documentation instead of a
   one-line label).
   One delegated listener pair on document — works for every current
   data-tip element and any added later without needing to wire each one up
   individually.
   ============================================================ */
(function () {
  const DELAY_MS = 2000;
  let timer = null;
  let tipEl = null;
  let activeTarget = null;

  function ensureTipEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement('div');
    tipEl.id = 'chevTooltip';
    document.body.appendChild(tipEl);
    return tipEl;
  }

  function showTip(target) {
    const text = target.getAttribute('data-tip');
    if (!text) return;
    const el = ensureTipEl();
    el.textContent = text;
    el.classList.add('show');
    const targetRect = target.getBoundingClientRect();
    // Measure after the text is set and the tooltip is visible/positioned, so its
    // size reflects the current label rather than a stale one from a prior show.
    const tw = el.offsetWidth, th = el.offsetHeight;
    let left = targetRect.left + targetRect.width / 2 - tw / 2;
    left = Math.max(4, Math.min(left, window.innerWidth - tw - 4));
    let top = targetRect.top - th - 6;
    if (top < 4) top = targetRect.bottom + 6; // no room above — flip below
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove('show');
    activeTarget = null;
  }

  function clearPending() {
    clearTimeout(timer);
    timer = null;
  }

  document.addEventListener('mouseover', (e) => {
    const target = e.target.closest('[data-tip]');
    if (!target || target === activeTarget) return;
    if (target.contains(e.relatedTarget)) return; // moved between children of the same target
    clearPending();
    hideTip();
    activeTarget = target;
    timer = setTimeout(() => showTip(target), DELAY_MS);
  }, true);

  document.addEventListener('mouseout', (e) => {
    const target = e.target.closest('[data-tip]');
    if (!target) return;
    if (target.contains(e.relatedTarget)) return; // still inside the same target
    clearPending();
    hideTip();
  }, true);

  document.addEventListener('mousedown', () => { clearPending(); hideTip(); });
  window.addEventListener('blur', () => { clearPending(); hideTip(); });
})();
