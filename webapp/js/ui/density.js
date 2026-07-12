/* ============================================================
   PHASE 4 Task 3: UI density control. #densityBtn opens #densityPopover
   (positioned the same way #emaDropdown does -- right-aligned under the
   button, via JS-set left/top on a position:fixed element); picking an
   option sets data-density on <html> + persists it to localStorage (read
   by the inline anti-FOUC snippet in <head> on next load), then re-runs
   the same chart fitContent/resize-sync nudge Task 1 uses for the sidebar
   toggle, since every density's larger/smaller tokens change #chartWrap's
   effective size just like the sidebar does.
   ============================================================ */
(function () {
  const btn = document.getElementById('densityBtn');
  const popover = document.getElementById('densityPopover');
  if (!btn || !popover) return;
  const opts = popover.querySelectorAll('.densityOpt');

  function syncActive() {
    const current = document.documentElement.getAttribute('data-density') || 'comfortable';
    opts.forEach(o => o.classList.toggle('active', o.dataset.density === current));
  }
  syncActive();

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (popover.classList.contains('open')) { popover.classList.remove('open'); return; }
    const rect = btn.getBoundingClientRect();
    popover.style.left = '';
    popover.style.right = (window.innerWidth - rect.right) + 'px';
    popover.style.top = (rect.bottom + 4) + 'px';
    popover.classList.add('open');
  });

  document.addEventListener('click', (e) => {
    if (!popover.contains(e.target) && e.target !== btn) popover.classList.remove('open');
  });

  opts.forEach(opt => {
    opt.addEventListener('click', () => {
      document.documentElement.setAttribute('data-density', opt.dataset.density);
      localStorage.setItem('chevDensity', opt.dataset.density);
      syncActive();
      popover.classList.remove('open');
      // No CSS transition to wait out here (unlike the sidebar toggle) -- the
      // attribute change is instant, so just wait one frame for layout to
      // settle before re-fitting the chart.
      requestAnimationFrame(() => {
        if (window._chevChart) window._chevChart.timeScale().fitContent();
        if (window.syncRsiCanvasSize) window.syncRsiCanvasSize();
        if (window._syncRsiAxisWidth) window._syncRsiAxisWidth();
      });
    });
  });
})();
