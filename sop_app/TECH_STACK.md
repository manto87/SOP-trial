# ElectroTech S&OP — Tech Stack

## Overview

The application is a single-server, single-user web app. Everything runs locally on your machine — no cloud infrastructure, no external databases, no SaaS dependencies (except the Anthropic API for the AI assistant features).

---

## Backend

| Component | Technology | Version | Role |
|-----------|-----------|---------|------|
| Language | Python | 3.10+ | All server-side logic |
| Web framework | Flask | latest | HTTP routing, JSON API, HTML templating |
| Database | SQLite | built-in | All data storage (single `.sqlite` file) |
| DB interface | sqlite3 | built-in (stdlib) | No ORM — raw SQL queries |
| AI / LLM | Anthropic Python SDK | latest | NLP forecast commands, chat assistant, analytics Q&A |
| Planning engine | Custom (`planning_engine.py`) | — | 4-pass multi-level constrained MRP |

**Python standard library used:** `json`, `math`, `os`, `uuid`, `datetime`, `collections`, `logging`, `re`, `itertools`

**Third-party Python packages required (install via pip):**
```
flask
anthropic
```

No other pip packages are needed. Everything else is either Python stdlib or loaded from CDN in the browser.

---

## Frontend

The entire frontend is a **single HTML file** (`templates/index.html`, ~5,500 lines). It is a single-page application — all tabs, charts, and interactions are rendered client-side in JavaScript. No build step, no npm, no bundler.

| Library | Version | Source | Role |
|---------|---------|--------|------|
| Chart.js | 4.4.0 | jsDelivr CDN | All bar, line, area charts |
| chartjs-plugin-datalabels | 2.2.0 | jsDelivr CDN | Data labels on charts |
| D3.js | v7 | jsDelivr CDN | Sankey / Flow Analysis diagram |
| d3-sankey | 0.12.3 | jsDelivr CDN | Sankey layout algorithm |
| Leaflet.js | 1.9.4 | unpkg CDN | Supplier & Risk interactive map |
| CartoDB Dark tiles | — | cartocdn.com | Dark map tiles for Leaflet |

**Note:** The frontend requires an internet connection at startup to load CDN libraries. If you need fully offline operation, download the CDN files and update the `<script>` / `<link>` tags to local paths.

---

## Database

| Property | Value |
|----------|-------|
| Engine | SQLite 3 |
| File | `electrotech_db_v5.sqlite` |
| Location | One level above `sop_app/` folder |
| Size | ~22 MB |
| Tables | 63 |
| Access | Read/write from `app.py` via `sqlite3` stdlib |

The DB path is resolved relative to `app.py` at startup:
```python
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "electrotech_db_v5.sqlite")
```

---

## API

The Flask backend exposes **37 REST API endpoints**, all returning JSON. Key groups:

| Prefix | Endpoints | Description |
|--------|-----------|-------------|
| `/api/forecast/` | 7 | Statistical forecast, manual overrides, accuracy, history, NL command |
| `/api/supply/` | 5 | Constrained plan, pull-forward, exceptions, resource loads |
| `/api/capacity/` | 2 | Capacity solutions (shift scenarios), bottleneck chain |
| `/api/bom/` | 3 | BOM tree, shortage impact, pegging export |
| `/api/suppliers/` | 6 | Overview, deliveries, capacity, substitutions, risk, map |
| `/api/exec/` | 1 | Executive S&OP summary |
| `/api/ask/` | 3 | AI chat assistant (forecast, general, analytics) |
| `/api/plan/` | 2 | Run planning engine, list runs |
| `/api/` | 8 | Products, resources, inventory, budget, sankey, config, analytics |

---

## External Services

| Service | Purpose | Required? |
|---------|---------|-----------|
| Anthropic API | NL forecast commands, AI chat assistant, analytics Q&A | Optional — app works without it; AI features return an error if key is missing |
| CartoDB tile server | Dark map tiles for the supplier map | Optional — map renders without tiles (blank background) |
| jsDelivr / unpkg CDNs | Chart.js, D3, Leaflet libraries | Required at startup (or use local copies) |

---

## Development Environment

Tested on:
- macOS (Apple Silicon and Intel)
- Python 3.10, 3.11, 3.12

The app has no OS-specific dependencies and should run on Linux and Windows without modification.

---

## File Structure

```
test company applications/          ← project root
├── .env                            ← environment variables (ANTHROPIC_API_KEY)
├── electrotech_db_v5.sqlite        ← 22 MB database (the "test company")
├── sop_app/
│   ├── app.py                      ← Flask backend (2,883 lines, 37 routes)
│   ├── planning_engine.py          ← 4-pass planning engine
│   ├── templates/
│   │   └── index.html              ← Single-page frontend (5,486 lines)
│   ├── static/                     ← (empty — all assets from CDN)
│   └── tests/
│       ├── helpers.py
│       ├── test_api_core.py
│       ├── test_bom_pegging.py
│       ├── test_capacity.py
│       ├── test_demand_planning.py
│       ├── test_nl_parser.py
│       ├── test_plan_engine.py
│       ├── test_runner.py
│       └── test_smoke.py
```
