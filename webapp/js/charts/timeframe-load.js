/* ============================================================
   Timeframe buttons + initial load kickoff.
   (extracted verbatim from index.html; do not reformat indentation)
   ============================================================ */
  /* ============================================================
     TIMEFRAME BUTTONS + INITIAL LOAD
     ============================================================ */
  const tfButtons = Array.from(document.querySelectorAll('[data-tf]'));
  tfButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      currentTf = btn.dataset.tf;
      tfButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadChart(currentSymbol, currentTf, currentType).then(startPolling).then(() => {
        if (activeIdeaRow) {
          const idea = currentIdeas.find(i => i.row === activeIdeaRow);
          if (idea) { drawChevToolsForIdea(idea); _refreshTradeOverlayLines(); }
        }
      });
    });
  });

  loadChart(currentSymbol, currentTf, currentType).then(startPolling);

