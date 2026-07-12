/* ============================================================
   PHASE 6 Task 2: keyboard shortcuts panel. Opened via the topbar
   #shortcutsBtn or the ? key (see drawing.js's keydown handler, which
   already guards every non-modifier shortcut against firing inside an
   input). Closed via the X button, clicking outside the panel, or Esc
   (also wired in drawing.js's existing Escape block, alongside every
   other Esc-closeable overlay).
   ============================================================ */
(function () {
  const overlay = document.getElementById('shortcutsOverlay');
  const btn = document.getElementById('shortcutsBtn');
  const closeBtn = document.getElementById('shortcutsCloseBtn');
  if (!overlay || !btn) return;

  function open() { overlay.classList.add('open'); }
  function close() { overlay.classList.remove('open'); }

  btn.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

  window._openShortcuts = open;
  window._closeShortcuts = close;
})();
