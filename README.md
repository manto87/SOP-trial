# ElectroTech S&OP — Setup & Run Guide

A locally-run Sales & Operations Planning prototype for the fictional electronics manufacturer ElectroTech Industries. Built with Python + Flask + SQLite. No cloud infrastructure required.

---

## What's included

```
electrotech_db_v5.sqlite    ← The test company (22 MB, 63 tables, full master data)
.env                        ← Your Anthropic API key goes here
sop_app/
├── app.py                  ← Flask backend (37 API endpoints)
├── planning_engine.py      ← 4-pass constrained planning engine
├── templates/index.html    ← Single-page frontend (all tabs, charts, maps)
├── SPECIFICATION.md        ← Full functional specification
├── TECH_STACK.md           ← Tech stack reference
└── tests/                  ← Test suite
```

---

## Prerequisites

- **Python 3.10 or later** — check with `python3 --version`
- **pip** — comes with Python
- **Internet connection** at startup (to load Chart.js, D3, and Leaflet from CDN)
- **Anthropic API key** — free tier works; needed only for AI chat features

---

## Setup (5 minutes)

### Step 1 — Install Python dependencies

Open a terminal in the project root folder and run:

```bash
pip install flask anthropic
```

That's it. No other packages needed — everything else is either Python standard library or loaded from CDN in the browser.

If you prefer to keep dependencies isolated:

```bash
python3 -m venv venv
source venv/bin/activate        # On Windows: venv\Scripts\activate
pip install flask anthropic
```

### Step 2 — Add your Anthropic API key

Open the `.env` file in the project root and replace the placeholder with your key:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

Get a key at [console.anthropic.com](https://console.anthropic.com) (free account works).

**The app runs without a key** — all tabs and planning features work. Only the AI chat assistant, the analytics Q&A, and the forecast AI will show an error if the key is missing.

### Step 3 — Start the app

```bash
python3 sop_app/app.py
```

You should see:

```
 * Running on http://127.0.0.1:5050
 * Debug mode: on
```

### Step 4 — Open in your browser

Go to: **http://127.0.0.1:5050**

The app loads in your default browser. All eight tabs are immediately available.

---

## Using Claude Code

If you have Claude Code installed, you can also start the app by asking Claude:

> "Please start the ElectroTech S&OP app"

Claude will find `app.py` and run it. You can then ask Claude to:
- Explain how any part of the code works
- Add new features or modify existing ones
- Run queries against the database
- Debug any issues

---

## Running the planning engine

The app loads the last planning run from the database on startup. To generate a fresh plan:

1. Go to the **Supply Planning** tab
2. Click **Run Plan**
3. Wait ~2 seconds — the 4-pass engine runs and results update across all tabs

The plan uses the current demand forecast (statistical baseline + any manual overrides you have saved).

---

## Key things to try

| Tab | Suggested action |
|-----|-----------------|
| Demand Planning | Type `"increase all B2C in November by 10%"` in the command box → click Apply |
| Demand Planning | Click any forecast cell and enter a manual override → Save Plan |
| Supply Planning | Click Run Plan → watch utilisation heatmap update |
| BOM Navigator | Select FG001, expand the tree, switch to Build Peg mode, enter 500 units |
| Capacity Solutions | Toggle "Group by Resource" → expand SMT Line 1 → compare 4 scenarios |
| Exec S&OP | Review the scenario comparison table and recommended actions |
| Suppliers & Risk | Zoom the map to see Asia → Europe maritime routes |
| Analytics | Type "Which product has the highest revenue at risk?" |

---

## Stopping the app

Press `Ctrl+C` in the terminal window where the app is running.

---

## Resetting the data

The database (`electrotech_db_v5.sqlite`) is the single source of truth. If you want to reset any changes you've made to forecasts or planning data:

- **Reset forecast overrides only:** Go to Demand Planning → click "Reset to Stat FC"
- **Full reset:** Replace `electrotech_db_v5.sqlite` with the original copy from the zip

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError: flask` | Run `pip install flask anthropic` |
| `Address already in use` (port 5050) | Another process is using port 5050. Kill it or change the port in the last line of `app.py`: `app.run(debug=True, port=5051)` |
| Map doesn't load | Check your internet connection — Leaflet tiles load from CartoDB CDN |
| Charts are blank | Check browser console for CDN load errors; internet required |
| AI chat returns error | Check that your `ANTHROPIC_API_KEY` in `.env` is valid |
| Planning engine slow | Normal on first run — subsequent runs are faster due to SQLite caching |

---

## Project structure — where things live

| File | What's in it |
|------|-------------|
| `electrotech_db_v5.sqlite` | All data: products, BOM, resources, demand history, planning results, suppliers, customers |
| `sop_app/app.py` | All 37 API endpoints; Flask app startup |
| `sop_app/planning_engine.py` | 4-pass MRP algorithm: FG → SA → Component → convergence |
| `sop_app/templates/index.html` | Entire frontend: all tabs, charts, tables, maps in one file |
| `.env` | Environment variables (API key) |

---

## Questions or feedback

This app was built as part of a Substack series on AI-assisted supply chain development. If you have questions, want to share what you built with it, or just want to compare notes — get in touch.

*Happy planning.*
