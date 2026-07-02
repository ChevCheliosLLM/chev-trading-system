# Chev Trading System — Collaboration Guide
### Kev & Alejandro | Mission: Make Chev the best trader the world has ever seen

---

## The System in Plain English

Think of it like a small hedge fund with four people working around the clock:

**Dexter** is the *research analyst*. He never sleeps. Every 5 minutes he scans the watchlist, runs all the maths on every chart, and writes a structured report. He doesn't have opinions — he only measures. "The price moved 4 ATR, there are two confluence zones, volume is expanding." Pure facts.

**engines.py** is Dexter's *calculator* — the actual maths behind his reports. Swing detection, leg strength, auction theory, pattern recognition. Dexter feeds in price data; engines.py outputs structured insight.

**Chev** is the *senior trader*. He reads Dexter's reports and decides: is this worth a trade? He has opinions, personality, strategy, and context. When Dexter says "3 confluences on BTC 1H," Chev decides yes or no.

**Jane** is the *second analyst*. Alejandro built her. She has her own market view, communicates via Telegram, and her signals reach Chev. Think of her as a specialist who catches things Dexter misses.

**The webapp** (webapp/index.html) is the *trading floor dashboard*. This is the screen Kev looks at to understand what all of them are doing and why. Charts, live trades, Chev's ideas, engine readouts — all in one place.

**Google Sheets** is the *trade journal* — the permanent record of every decision and outcome.

---

## Who Owns What

This is the most important thing to agree on. When two people edit the same file at the same time, Git can get confused. The rule is: **talk before you touch a shared file.**

| File / Area | Owner | Notes |
|---|---|---|
| `webapp/index.html` | **Kev** | The entire dashboard UI |
| `engines.py` | **Shared — coordinate first** | The core analysis maths |
| `patterns.py` | **Shared — coordinate first** | Pattern recognition |
| `dexter.py` | **Shared — coordinate first** | Main bot logic. Most conflicts happen here |
| `telegram_listener.py` | **Alejandro** | Jane's Telegram bridge |
| Jane's code (wherever it lives) | **Alejandro** | Alejandro's domain |
| `chart_drawer.py` | **Kev** | Open WebUI tool for Chev |
| `launcher.py` | **Kev** | Windows launcher |
| Chev's system prompt (Open WebUI) | **Both** | Agree on changes before editing |
| Google Sheets structure | **Both** | Agree on new columns before adding |

---

## How Git Works — The Simple Version

Git is like Google Docs version history, but for code. Every time you save a "checkpoint" (called a commit), Git remembers exactly what the files looked like. If something breaks, you can go back.

**The daily workflow:**

```
Morning: git pull          ← get Alejandro's latest changes
Work on your stuff
Evening: git add .
         git commit -m "what you did"
         git push          ← share your changes with Alejandro
```

**If you're about to touch a shared file (dexter.py, engines.py):**

1. Message Alejandro first: "I'm going to edit dexter.py, you good?"
2. Make your changes
3. Commit and push straight away so he can pull and continue

**The golden rule: pull before you start, push when you finish.**

---

## Running the System After an Update

**If Alejandro pushes a change to dexter.py:**
1. Kev opens VS Code terminal: `git pull`
2. Kev restarts Dexter (close the terminal running it, run `python dexter.py` again)
3. Done. The new code is live.

**If Kev pushes a change to webapp/index.html:**
1. Alejandro runs: `git pull`
2. Hard refresh the browser (Ctrl+Shift+R)
3. Done.

**If Alejandro wants to see the webapp:**
The webapp runs on Kev's machine at `http://localhost:5000`. To share it remotely, Kev runs Cloudflare Tunnel (the launcher has a button for it) — this gives a public URL Alejandro can open in his browser and see the live terminal.

**Alejandro's local setup:**
To run Dexter on his own machine for testing:
1. Clone the repo: `git clone [repo URL]`
2. Install Python dependencies: `pip install requests pandas numpy gspread google-auth flask`
3. Get a copy of `google_credentials.json` from Kev (never commit this file)
4. Run: `python dexter.py`

---

## The Task Board

### Priority 1 — Foundation (Do This Week)

| Task | Owner | What It Is |
|---|---|---|
| GitHub repo live + both have access | **Kev** | Done when both can pull/push |
| Alejandro clones repo + runs dexter.py locally | **Alejandro** | He can test his changes without breaking Kev's live system |
| Write down "the strategy" in plain English | **Both** | What makes Chev trade? What makes him skip? One page, no code |
| Alejandro reads engines.py comments | **Alejandro** | Understand what Dexter measures before improving it |

---

### Priority 2 — Website (Kev's Zone)

These are the visible improvements to the dashboard. Kev leads these, Alejandro can contribute ideas.

| Task | Status | What It Is |
|---|---|---|
| RSI Divergence drawn on chart | 🔄 In progress | When Chev spots an RSI divergence as confluence, draw the yellow trendlines on the price chart AND the RSI panel |
| Pattern overlays drawn on chart | ❌ Not started | When Dexter/Chev detects a triangle, channel, or wedge, draw those trendlines directly on the chart so you can see them |
| Fix the Fibonacci anchor | ❌ Not started | Fib should anchor to the FIRST leg of the move, not just any recent swing. Right now it sometimes picks the wrong starting point |
| On Radar pill system (SR, FIB, VP, RSI Div) | ✅ Built | The pills in the idea cards now show/hide each confluence on the chart |
| Toolbar always visible at any window size | ✅ Fixed | Two-row toolbar — row 1: identity + timeframe, row 2: all tools |
| Volume Profile opacity + range fix | ✅ Fixed | VP now anchors to start of move via server-side detection |
| Make Chev's patterns visible | ❌ Not started | When the engine detects a BULL_FLAG or COMPRESSION, outline it on the chart with trendlines so Kev can see what Dexter sees |
| Show Jane's signals on the dashboard | ❌ Not started | A section in the "Trades" panel showing Jane's current calls alongside Dexter's |
| Chev/Jane disagreement flag | ❌ Not started | Red/yellow badge when Chev and Jane have opposite views on the same pair |
| UI polish pass | 🔄 Ongoing | Spacing, fonts, responsiveness — always improving |

---

### Priority 3 — Dexter & Engines (Alejandro's Zone + Both)

These are improvements to what Dexter measures and how Chev uses it.

| Task | Owner | What It Is |
|---|---|---|
| **Backtesting framework** | **Alejandro** | Run the full engine stack against 12 months of historical data. See if the strategy would have made money. Without this, we're flying blind. |
| **Audit Chev's prompt** | **Both** | Read exactly what Dexter tells Chev before every trade. Is it accurate? Is it missing anything? Is it confusing? Fix it. |
| **Improve confluence scoring** | **Alejandro** | Right now Dexter counts confluences (3+ = escalate to Chev). But are all confluences equal? An SR level with 10 touches is stronger than one with 2. Make the scoring smarter. |
| **Multi-timeframe gate** | **Alejandro** | Chev should only trade when both 1H and 4H agree on direction. Build this filter into Dexter so he only escalates when both timeframes point the same way. |
| **Fix the Fib anchor in engines.py** | **Alejandro** | `_find_last_impulse()` and `_impulse_anchors()` should identify the FIRST leg of the current move, not just the most recent swing. This affects Fib levels and VP range. |
| **Jane/Chev debate mechanism** | **Both** | Before posting a trade, Dexter sends the setup to both Chev and Jane independently. If they agree → post it. If they disagree → flag for review. This is the single highest-impact improvement available. |
| **RSI Divergence in confluence JSON** | **Alejandro** | Make sure Dexter always writes RSI_DIV_T1 and RSI_DIV_T2 to the Google Sheet when an RSI divergence is a confluence. Kev will use these to draw the trendlines. |
| **Document what each column in Google Sheets means** | **Both** | Col 0–19 are defined but not everyone knows what they are. Write it down once so both can use the data confidently. |
| **Stop Loss quality audit** | **Alejandro** | Look at the last 20 losses. Did price hit SL because the SL was too tight, or because the trade was wrong? If it's always "too tight," the ATR-based SL needs widening. |
| **Jane performance tracking** | **Alejandro** | Jane is live but her win rate isn't tracked the same way as Chev's. Add Jane's trades to the performance log so we can compare them. |

---

### Priority 4 — The Big Picture (Both Together)

These are the longer-horizon ideas. Don't start these until Priority 2 and 3 are mostly done.

| Idea | What It Is |
|---|---|
| **Backtesting results review** | After Alejandro builds backtesting, both sit down and go through the results. Where does the strategy make money? Where does it lose? This shapes everything else. |
| **Strategy A/B test** | Run two slightly different versions of the strategy rules for 30 days and compare win rates. The winner becomes the standard. |
| **Draw-then-ask** | Kev draws something on the chart (a trendline, a zone), then asks Chev: "what do you think about this?" Chev responds with his view. Turns the terminal into a proper study partner. |
| **Chev memory between sessions** | Right now Chev forgets every conversation. What if he kept a short "market context" note — "BTC has been in a compression for 3 days, watching for breakout" — that persists? |
| **Portfolio-level risk control** | Never more than 3 trades open at once. Never risk more than 6% of total account across all open positions. This is the #1 discipline upgrade. |
| **Weekly review meeting** | Every week, both of you look at the last 7 days of trades together. Why did wins win? Why did losses lose? Was it Chev's fault or Dexter's? This is how elite traders improve. |

---

## The Weekly Rhythm

| Day | What Happens |
|---|---|
| **Monday** | `git pull`. Both start fresh. Check what broke over the weekend. |
| **Wednesday** | Quick sync: what did each person build this week? Any shared files being edited? |
| **Friday** | Both push their week's work. Short trade review: last 5 trades — win/loss and why. |

---

## Things NOT to Do (Hard Rules)

1. **Never commit `google_credentials.json`** — this gives full access to the Google Sheets. It should only ever be shared directly (WhatsApp/email), never through GitHub.
2. **Never push directly to `main` when you're mid-experiment** — create a branch (`git checkout -b my-experiment`), test it, then merge.
3. **Never change the Google Sheets column layout without telling the other person first** — everything reads columns by index number. Move a column and the whole system reads the wrong data.
4. **Never touch dexter.py while the other person is actively running a live session** — coordinate first.

---

## Quick Reference — Git Commands

```bash
# Get latest code from GitHub
git pull

# See what files you've changed
git status

# Save your work (checkpoint)
git add .
git commit -m "describe what you changed"

# Share your changes
git push

# Create a safe branch for experiments
git checkout -b my-feature-name

# Merge your experiment back
git checkout main
git merge my-feature-name
```

---

## The Mission

Chev, Dexter, and Jane are tools. The strategy is the thing that wins or loses.

The goal is not to have the most complex code. The goal is to have Chev making high-probability decisions — ones where the structure is right, the risk is defined, and the entry is confirmed by multiple independent signals (Dexter's measurement, Jane's view, chart structure, and Chev's interpretation all pointing the same way).

When that alignment happens, that's an A+ trade. Everything we build is in service of finding those moments more reliably and executing them more cleanly.

---

*Questions? Kev and Alejandro can reach Claude Code (the AI pair programmer) in VS Code.*
