"""ElectroTech S&OP Application - Flask Backend"""

import sqlite3
import json
import math
import os
import uuid
import io
from datetime import datetime, date
from collections import defaultdict
from flask import Flask, jsonify, request, render_template
import planning_engine

app = Flask(__name__)

# Load .env file from project root if present (simple key=value parser, no dependency needed)
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k_clean, _v_clean = _k.strip(), _v.strip().strip("\"'")
            if not os.environ.get(_k_clean):  # override empty/missing, but not a real existing value
                os.environ[_k_clean] = _v_clean

# Resolve DB path relative to this file so it works regardless of working directory
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "electrotech_db_v5.sqlite")
TODAY = "2025-03"
HORIZON_MONTHS = [
    "2025-03","2025-04","2025-05","2025-06","2025-07","2025-08",
    "2025-09","2025-10","2025-11","2025-12","2026-01","2026-02"
]

def get_db():
    conn = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")  # 30s busy wait before "locked" error
    return conn

# ── helpers ──────────────────────────────────────────────────────────────────

def rows_to_list(rows):
    return [dict(r) for r in rows]

def compute_statistical_forecast():
    """
    For each product+channel, aggregate weekly demand_history to monthly,
    then project 12 months forward using seasonal naive + linear trend.
    History cutoff: 2025-02 (last complete month before today 2025-03).
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT product_id, channel,
               strftime('%Y-%m', week_start) AS month,
               SUM(quantity) AS qty
        FROM demand_history
        WHERE week_start < '2025-03-01'
        GROUP BY product_id, channel, month
        ORDER BY product_id, channel, month
    """).fetchall()
    conn.close()

    # Build history dict: {(prod, channel): {month_str: qty}}
    history = defaultdict(dict)
    for r in rows:
        history[(r["product_id"], r["channel"])][r["month"]] = r["qty"]

    forecast = {}  # {(prod, channel): {month: qty}}

    for key, hist in history.items():
        months_sorted = sorted(hist.keys())
        if len(months_sorted) < 12:
            continue

        # Build monthly series aligned to calendar months
        monthly = []
        for m in months_sorted:
            monthly.append(hist[m])

        n = len(monthly)
        # Simple linear trend
        mean_y = sum(range(n)) / n
        mean_x = sum(monthly) / n
        num = sum((i - mean_y) * (monthly[i] - mean_x) for i in range(n))
        den = sum((i - mean_y) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0
        intercept = mean_x - slope * mean_y

        # Seasonal index: average ratio per month-of-year vs trend
        seasonal = defaultdict(list)
        for i, m in enumerate(months_sorted):
            trend_val = intercept + slope * i
            if trend_val > 0:
                mo = int(m.split("-")[1])
                seasonal[mo].append(monthly[i] / trend_val)

        seas_idx = {mo: (sum(v) / len(v)) for mo, v in seasonal.items()}

        # Project forward
        proj = {}
        for j, fmon in enumerate(HORIZON_MONTHS):
            t = n + j
            trend_val = intercept + slope * t
            mo = int(fmon.split("-")[1])
            si = seas_idx.get(mo, 1.0)
            proj[fmon] = max(0, round(trend_val * si))

        forecast[key] = proj

    return forecast


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products")
def api_products():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products ORDER BY product_id").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/resources")
def api_resources():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, p.name as plant_name
        FROM production_resources r
        JOIN plants p ON p.plant_id = r.plant_id
        ORDER BY r.plant_id, r.resource_id
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# ── DEMAND PLANNING ───────────────────────────────────────────────────────────

@app.route("/api/forecast/statistical")
def api_statistical_forecast():
    """Returns statistical forecast for next 12 months per product/channel."""
    forecast = compute_statistical_forecast()
    result = []
    conn = get_db()
    products = {r["product_id"]: dict(r) for r in conn.execute("SELECT * FROM products").fetchall()}
    conn.close()

    for (prod, channel), monthly in forecast.items():
        p = products.get(prod, {})
        result.append({
            "product_id": prod,
            "product_name": p.get("name", prod),
            "product_family": p.get("product_family", ""),
            "channel": channel,
            "monthly": monthly,
            "total_12m": sum(monthly.values())
        })

    result.sort(key=lambda x: (x["product_id"], x["channel"]))
    return jsonify(result)


@app.route("/api/forecast/history")
def api_forecast_history():
    """
    Last 12 complete months of actual demand.
    Uses statistical_forecast_history actuals (horizon_type='historical_backtest')
    which are validated monthly totals — avoids partial-week distortion at period end.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT product_id, channel,
               substr(forecast_month, 1, 7) AS month,
               SUM(actual_qty) AS qty
        FROM statistical_forecast_history
        WHERE horizon_type = 'historical_backtest'
        GROUP BY product_id, channel, month
        ORDER BY product_id, channel, month
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/forecast/reset", methods=["POST"])
def api_forecast_reset():
    """Delete all manual forecast overrides for the planning horizon, reverting to statistical FC."""
    conn = get_db()
    result = conn.execute("""
        DELETE FROM manual_forecast_history
        WHERE forecast_month >= '2025-03-01'
    """)
    deleted = result.rowcount
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "deleted_rows": deleted})


@app.route("/api/forecast/manual", methods=["GET"])
def api_manual_forecast_get():
    """Get latest manual forecast overrides for the planning horizon."""
    conn = get_db()
    rows = conn.execute("""
        SELECT m.forecast_month, m.product_id, m.channel,
               m.statistical_forecast_qty, m.manual_forecast_qty,
               m.adjustment_pct, m.note, m.planner_id, m.created_at
        FROM manual_forecast_history m
        WHERE m.forecast_month >= '2025-03-01'
        AND m.manual_forecast_id IN (
            SELECT manual_forecast_id FROM manual_forecast_history m2
            WHERE m2.forecast_month = m.forecast_month
              AND m2.product_id = m.product_id
              AND m2.channel = m.channel
            ORDER BY m2.created_at DESC LIMIT 1
        )
        ORDER BY m.forecast_month, m.product_id, m.channel
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/forecast/manual", methods=["POST"])
def api_manual_forecast_save():
    """Save a manual forecast override."""
    data = request.json
    conn = get_db()

    stat_qty = data.get("statistical_forecast_qty", 0)
    manual_qty = data.get("manual_forecast_qty", 0)
    adj_pct = ((manual_qty - stat_qty) / stat_qty * 100) if stat_qty else 0
    mid = f"MFH_{data['product_id']}_{data['channel']}_{data['forecast_month'].replace('-','')[:7]}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    conn.execute("""
        INSERT INTO manual_forecast_history
        (manual_forecast_id, stat_forecast_id, forecast_month, product_id, channel,
         planner_id, actual_qty, statistical_forecast_qty, manual_forecast_qty,
         adjustment_pct, abs_error, ape, note, created_at)
        VALUES (?,?,?,?,?,?,0,?,?,?,0,NULL,?,?)
    """, (
        mid,
        data.get("stat_forecast_id", "STAT_FUTURE"),
        data["forecast_month"] + "-01",
        data["product_id"],
        data["channel"],
        data.get("planner_id", "DPL001"),
        stat_qty,
        manual_qty,
        round(adj_pct, 2),
        data.get("note", ""),
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "id": mid})


def _run_seasonal_trend_forecast(monthly_series, months_sorted, n_ahead):
    """
    Run the same seasonal naive + linear trend model used for the main forecast.
    Returns a list of n_ahead forecast values.
    """
    n = len(monthly_series)
    if n < 12:
        return [sum(monthly_series) / n] * n_ahead

    mean_i = sum(range(n)) / n
    mean_y = sum(monthly_series) / n
    num = sum((i - mean_i) * (monthly_series[i] - mean_y) for i in range(n))
    den = sum((i - mean_i) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0
    intercept = mean_y - slope * mean_i

    seasonal = defaultdict(list)
    for i, m in enumerate(months_sorted):
        trend_val = intercept + slope * i
        if trend_val > 0:
            mo = int(m.split("-")[1])
            seasonal[mo].append(monthly_series[i] / trend_val)
    seas_idx = {mo: sum(v) / len(v) for mo, v in seasonal.items()}

    preds = []
    for j in range(n_ahead):
        t = n + j
        trend_val = intercept + slope * t
        # month-of-year for the j-th forecast step after the last training month
        last_mo = int(months_sorted[-1].split("-")[1])
        mo = ((last_mo - 1 + j + 1) % 12) + 1
        si = seas_idx.get(mo, 1.0)
        preds.append(max(0, trend_val * si))
    return preds


@app.route("/api/forecast/accuracy")
def api_forecast_accuracy():
    """
    Rolling backtest accuracy: train on demand_history up to 2024-02,
    forecast 2024-03 – 2025-02 (12 months), compare vs actual demand_history.
    This gives genuinely differentiated accuracy per product rather than
    relying on the synthetically uniform statistical_forecast_history APE values.
    """
    conn = get_db()

    # Aggregate weekly demand to monthly
    rows = conn.execute("""
        SELECT product_id, channel,
               strftime('%Y-%m', week_start) AS month,
               SUM(quantity) AS qty
        FROM demand_history
        GROUP BY product_id, channel, month
        ORDER BY product_id, channel, month
    """).fetchall()

    products = {r["product_id"]: dict(r)
                for r in conn.execute("SELECT * FROM products").fetchall()}
    conn.close()

    # Training cutoff and holdout window
    TRAIN_CUTOFF  = "2024-02"   # last training month (inclusive)
    HOLDOUT_START = "2024-03"   # first holdout month
    HOLDOUT_END   = "2025-02"   # last holdout month

    # Build per-product/channel monthly series
    history = defaultdict(dict)
    for r in rows:
        history[(r["product_id"], r["channel"])][r["month"]] = r["qty"]

    # Aggregate to product level (sum across channels) for accuracy metric
    prod_apes   = defaultdict(list)   # product_id → list of APE per holdout month
    prod_biases = defaultdict(list)   # product_id → list of bias per holdout month
    prod_rev    = defaultdict(float)  # product_id → 12M holdout revenue (actuals × price)

    for (prod, channel), hist in history.items():
        months_sorted = sorted(hist.keys())
        train_months  = [m for m in months_sorted if m <= TRAIN_CUTOFF]
        holdout_months = [m for m in months_sorted if HOLDOUT_START <= m <= HOLDOUT_END]

        if len(train_months) < 12 or not holdout_months:
            continue

        train_series = [hist[m] for m in train_months]
        n_ahead = len(holdout_months)
        forecasts = _run_seasonal_trend_forecast(train_series, train_months, n_ahead)

        price = float(products.get(prod, {}).get("unit_price") or 0)

        for i, m in enumerate(holdout_months):
            actual   = hist[m]
            forecast = forecasts[i]
            if actual > 0:
                ape  = abs(actual - forecast) / actual
                bias = (forecast - actual) / actual
                prod_apes[prod].append(ape)
                prod_biases[prod].append(bias)
            prod_rev[prod] += actual * price

    result = []
    for prod_id, apes in prod_apes.items():
        p = products.get(prod_id, {})
        mean_ape      = sum(apes) / len(apes)
        mean_accuracy = max(0.0, 1.0 - mean_ape)
        mean_bias     = sum(prod_biases[prod_id]) / len(prod_biases[prod_id]) if prod_biases[prod_id] else 0
        result.append({
            "product_id":     prod_id,
            "product_name":   p.get("name", prod_id),
            "product_family": p.get("product_family", ""),
            "unit_price":     p.get("unit_price", 0),
            "mean_ape":       round(mean_ape, 4),
            "mean_accuracy":  round(mean_accuracy, 4),
            "mean_bias":      round(mean_bias, 4),
            "months":         len(apes),
            "revenue_12m":    round(prod_rev[prod_id]),
            "backtest_period": f"{HOLDOUT_START} – {HOLDOUT_END}",
        })

    result.sort(key=lambda x: x["mean_accuracy"], reverse=True)
    return jsonify(result)


# ── SUPPLY PLANNING ───────────────────────────────────────────────────────────

def _latest_run_id(conn, fallback="baseline_20260218_071626"):
    """Return the most-recently created run_id from planning results, or the fallback.
    Prefer user-triggered runs (run_YYYYMMDD_HHMMSS) over seed/baseline runs."""
    # Try to find the latest user run (name starts with 'run_')
    row = conn.execute(
        "SELECT run_id FROM planning_fg_constrained_results WHERE run_id LIKE 'run_%' GROUP BY run_id ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["run_id"]
    # Fall back to latest by run_id (covers baseline_ style names too)
    row = conn.execute(
        "SELECT run_id FROM planning_fg_constrained_results GROUP BY run_id ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return row["run_id"] if row else fallback


@app.route("/api/supply/plan")
def api_supply_plan():
    """
    Returns unconstrained and constrained supply plan.
    Accepts ?run_id= query param; defaults to latest run in the DB.
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    # FG constrained results
    fg = conn.execute("""
        SELECT product_id, month, demand_to_produce_qty,
               constrained_production_qty, backlog_qty, prebuild_inventory_qty
        FROM planning_fg_constrained_results
        WHERE run_id = ?
        ORDER BY product_id, month
    """, (run_id,)).fetchall()

    # Resource utilization — join with backlog-implied extra load for unconstrained view
    res = conn.execute("""
        SELECT r.resource_id, r.resource_name, r.plant_id, r.month,
               r.capacity_min, r.load_min, r.overload_min, r.utilization_pct,
               COALESCE(bl.extra_min, 0) as backlog_load_min,
               r.load_min + COALESCE(bl.extra_min, 0) as unconstrained_load_min
        FROM planning_resource_month_results r
        LEFT JOIN (
            SELECT rp.resource_id, rp.month,
                   SUM(fg.backlog_qty * rp.allocated_min / rp.allocated_qty) as extra_min
            FROM planning_resource_product_results rp
            JOIN planning_fg_constrained_results fg
              ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
            WHERE rp.run_id = ? AND rp.allocated_qty > 0 AND fg.backlog_qty > 0
            GROUP BY rp.resource_id, rp.month
        ) bl ON bl.resource_id = r.resource_id AND bl.month = r.month
        WHERE r.run_id = ?
        ORDER BY r.plant_id, r.resource_id, r.month
    """, (run_id, run_id)).fetchall()

    # Inventory
    inv = conn.execute("""
        SELECT item_id, item_type, location_id,
               SUM(quantity_on_hand) as qty
        FROM inventory
        WHERE item_type = 'finished_good'
        GROUP BY item_id
    """).fetchall()

    # Backlog revenue at risk
    brev = conn.execute("""
        SELECT SUM(p.backlog_qty * pr.unit_price) as revenue_at_risk
        FROM planning_fg_constrained_results p
        JOIN products pr ON pr.product_id = p.product_id
        WHERE p.run_id = ?
    """, (run_id,)).fetchone()

    conn.close()
    return jsonify({
        "fg_plan": rows_to_list(fg),
        "resources": rows_to_list(res),
        "inventory": rows_to_list(inv),
        "backlog_revenue": round(brev["revenue_at_risk"] or 0)
    })


@app.route("/api/supply/pullforward")
def api_pullforward():
    """
    Identify overloaded months and suggest pull-forward strategy:
    move production from overloaded months to earlier slack months.
    Returns: per-resource, per-month analysis with pull-forward recommendations.
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    res_data = conn.execute("""
        SELECT r.resource_id, r.resource_name, r.plant_id, r.month,
               r.capacity_min, r.load_min, r.utilization_pct,
               COALESCE(bl.extra_min, 0) as backlog_load_min,
               r.load_min + COALESCE(bl.extra_min, 0) as unconstrained_load_min
        FROM planning_resource_month_results r
        LEFT JOIN (
            SELECT rp.resource_id, rp.month,
                   SUM(fg.backlog_qty * rp.allocated_min / rp.allocated_qty) as extra_min
            FROM planning_resource_product_results rp
            JOIN planning_fg_constrained_results fg
              ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
            WHERE rp.run_id = ? AND rp.allocated_qty > 0 AND fg.backlog_qty > 0
            GROUP BY rp.resource_id, rp.month
        ) bl ON bl.resource_id = r.resource_id AND bl.month = r.month
        WHERE r.run_id = ?
        ORDER BY r.resource_id, r.month
    """, (run_id, run_id)).fetchall()

    conn.close()

    # Build per-resource timeline and compute pull-forward
    resources = defaultdict(list)
    for r in res_data:
        resources[r["resource_id"]].append(dict(r))

    recommendations = []
    for res_id, months in resources.items():
        months_in_horizon = [m for m in months if m["month"] in HORIZON_MONTHS]
        if not months_in_horizon:
            continue

        # Overloaded = months where unconstrained demand exceeds capacity (backlog implied)
        overloaded = [(i, m) for i, m in enumerate(months_in_horizon)
                      if m["unconstrained_load_min"] > m["capacity_min"] * 0.95]
        if not overloaded:
            continue

        for i, ov_month in overloaded:
            slack_months = []
            for j in range(max(0, i-4), i):
                slack = months_in_horizon[j]
                available = slack["capacity_min"] - slack["load_min"]
                if available > 0:
                    slack_months.append({
                        "month": slack["month"],
                        "available_min": round(available),
                        "current_util_pct": round(slack["utilization_pct"], 1)
                    })

            # True overload = backlog-implied extra resource minutes needed
            overload_qty = round(ov_month["backlog_load_min"])
            real_util = round(ov_month["unconstrained_load_min"] / ov_month["capacity_min"] * 100, 1) if ov_month["capacity_min"] > 0 else 100
            total_slack = sum(s["available_min"] for s in slack_months)
            recommendations.append({
                "resource_id": res_id,
                "resource_name": ov_month["resource_name"],
                "plant_id": ov_month["plant_id"],
                "overloaded_month": ov_month["month"],
                "capacity_min": round(ov_month["capacity_min"]),
                "load_min": round(ov_month["load_min"]),
                "overload_min": round(overload_qty),
                "remaining_gap_min": max(0, round(overload_qty - total_slack)),
                "utilization_pct": real_util,
                "pull_forward_options": slack_months,
                "resolvable": len(slack_months) > 0 and sum(s["available_min"] for s in slack_months) >= overload_qty * 0.8
            })

    return jsonify(recommendations)


@app.route("/api/supply/exceptions")
def api_exceptions():
    """Unresolvable supply exceptions (backlog + chronic overloads)."""
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)
    backlog = conn.execute("""
        SELECT p.product_id, pr.name as product_name, p.month,
               p.demand_to_produce_qty, p.constrained_production_qty,
               p.backlog_qty
        FROM planning_fg_constrained_results p
        JOIN products pr ON pr.product_id = p.product_id
        WHERE p.run_id = ? AND p.backlog_qty > 0
        ORDER BY p.backlog_qty DESC
    """, (run_id,)).fetchall()
    conn.close()
    return jsonify(rows_to_list(backlog))


# ── BOM NAVIGATOR ─────────────────────────────────────────────────────────────

@app.route("/api/bom/<product_id>")
def api_bom(product_id):
    """Return full BOM tree for a product with inventory and plan data."""
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    # Get all BOM rows
    bom_rows = conn.execute("""
        SELECT b.parent_id, b.child_id, b.quantity, b.bom_level,
               COALESCE(p.name, m.name) as child_name,
               CASE WHEN p.product_id IS NOT NULL THEN 'product'
                    ELSE COALESCE(m.material_type, 'component') END as child_type,
               COALESCE(m.make_buy, 'make') as make_buy,
               COALESCE(m.unit_cost, p.unit_price, 0) as unit_cost,
               COALESCE(m.moq, p.moq) as moq,
               m.lead_time_days as material_lead_time_days
        FROM bill_of_materials b
        LEFT JOIN products p ON p.product_id = b.child_id
        LEFT JOIN materials m ON m.material_id = b.child_id
        ORDER BY b.bom_level, b.parent_id
    """).fetchall()

    # Get inventory for BOM items
    inv = conn.execute("""
        SELECT item_id, SUM(quantity_on_hand) as qty_on_hand
        FROM inventory GROUP BY item_id
    """).fetchall()
    inv_map = {r["item_id"]: r["qty_on_hand"] for r in inv}

    # Get plan data for this product's components
    plan = conn.execute("""
        SELECT item_id, month, gross_demand_qty, planned_replenishment_qty,
               ending_inventory_qty, safety_stock_qty
        FROM planning_item_month_results
        WHERE run_id = ? AND month IN ({})
        ORDER BY item_id, month
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), [run_id] + HORIZON_MONTHS).fetchall()
    plan_map = defaultdict(dict)
    for r in plan:
        plan_map[r["item_id"]][r["month"]] = {
            "demand": round(r["gross_demand_qty"]),
            "replenishment": round(r["planned_replenishment_qty"]),
            "end_inv": round(r["ending_inventory_qty"]),
            "safety_stock": round(r["safety_stock_qty"])
        }

    # Build tree structure (recursive)
    bom_dict = defaultdict(list)
    for r in bom_rows:
        bom_dict[r["parent_id"]].append(dict(r))

    def build_tree(node_id, qty_multiplier=1):
        children = bom_dict.get(node_id, [])
        result = []
        for child in children:
            effective_qty = child["quantity"] * qty_multiplier
            item_id = child["child_id"]
            node = {
                "id": item_id,
                "name": child["child_name"] or item_id,
                "type": child["child_type"],
                "make_buy": child["make_buy"],
                "qty_per": child["quantity"],
                "effective_qty": round(effective_qty, 2),
                "bom_level": child["bom_level"],
                "unit_cost": child["unit_cost"],
                "moq": child["moq"],
                "material_lead_time_days": child["material_lead_time_days"],
                "inventory": inv_map.get(item_id, 0),
                "plan": plan_map.get(item_id, {}),
                "children": build_tree(item_id, effective_qty)
            }
            # Add supplier info for bought components
            if child["make_buy"] == "buy":
                sup = supplier_map.get(item_id, {})
                pri = sup.get(1, {})
                bak = sup.get(2, {})
                node["primary_supplier_name"] = pri.get("supplier_name")
                node["primary_supplier_country"] = pri.get("supplier_country")
                node["supplier_lead_time_days"] = pri.get("supplier_lead_time_days")
                node["supplier_reliability"] = pri.get("reliability_score")
                node["supplier_moq"] = pri.get("supplier_moq") or child.get("moq")
                node["backup_supplier_name"] = bak.get("supplier_name")
                node["has_substitute"] = sub_map.get(item_id, 0) > 0
                node["is_single_source"] = len(sup) <= 1
            result.append(node)
        return result

    # Get supplier info for bought components (primary supplier, backup)
    supplier_rows = conn.execute("""
        SELECT msm.material_id, msm.supplier_priority,
               s.supplier_id, s.name as supplier_name, s.country as supplier_country,
               COALESCE(msm.lead_time_days, s.lead_time_days) as supplier_lead_time_days,
               s.reliability_score, msm.moq as supplier_moq
        FROM material_supplier_mapping msm
        JOIN suppliers s ON s.supplier_id = msm.supplier_id
        WHERE msm.supplier_priority IN (1, 2)
        ORDER BY msm.material_id, msm.supplier_priority
    """).fetchall()
    supplier_map = {}
    for sr in supplier_rows:
        mid = sr["material_id"]
        if mid not in supplier_map:
            supplier_map[mid] = {}
        supplier_map[mid][sr["supplier_priority"]] = dict(sr)

    # Get substitutions lookup
    sub_rows = conn.execute("""
        SELECT material_id, COUNT(*) as sub_count
        FROM material_substitutions
        WHERE approval_status IN ('approved', 'approved_conditional')
        GROUP BY material_id
    """).fetchall()
    sub_map = {r["material_id"]: r["sub_count"] for r in sub_rows}

    # Get root product info
    prod = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    conn.close()

    if not prod:
        return jsonify({"error": "Product not found"}), 404

    tree = {
        "id": product_id,
        "name": prod["name"],
        "type": "finished_good",
        "make_buy": "make",
        "qty_per": 1,
        "effective_qty": 1,
        "bom_level": 0,
        "unit_cost": prod["unit_price"],
        "inventory": inv_map.get(product_id, 0),
        "plan": plan_map.get(product_id, {}),
        "children": build_tree(product_id, 1)
    }
    return jsonify(tree)


# ── CAPACITY SOLUTIONS ────────────────────────────────────────────────────────

@app.route("/api/capacity/solutions")
def api_capacity_solutions():
    """
    For each overloaded resource/month, compute cost of:
    - Saturday shift    (adds ~3,840 min/month, +50% premium)
    - 3rd night shift   (adds ~9,600 min/month Mon-Fri, +25% premium)
    - Combined Sat+Night
    - Holiday work      (work on mandatory bank holidays, +100% public-holiday premium)
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    overloaded = conn.execute("""
        SELECT r.resource_id, r.resource_name, r.plant_id, r.month,
               r.capacity_min, r.load_min, r.overload_min, r.utilization_pct,
               pr.resource_type, pr.weekly_capacity,
               COALESCE(bl.extra_min, 0) AS backlog_load_min,
               r.load_min + COALESCE(bl.extra_min, 0) AS unconstrained_load_min
        FROM planning_resource_month_results r
        JOIN production_resources pr ON pr.resource_id = r.resource_id
        LEFT JOIN (
            SELECT rp.resource_id, rp.month,
                   SUM(fg.backlog_qty * rp.allocated_min / rp.allocated_qty) AS extra_min
            FROM planning_resource_product_results rp
            JOIN planning_fg_constrained_results fg
              ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
            WHERE rp.run_id = ? AND rp.allocated_qty > 0 AND fg.backlog_qty > 0
            GROUP BY rp.resource_id, rp.month
        ) bl ON bl.resource_id = r.resource_id AND bl.month = r.month
        WHERE r.run_id = ?
        ORDER BY r.plant_id, r.resource_id, r.month
    """, (run_id, run_id)).fetchall()

    # Revenue at risk per resource/month
    rev_risk = conn.execute("""
        SELECT rp.resource_id, rp.month,
               SUM(fg.backlog_qty * p.unit_price) as revenue_at_risk
        FROM planning_resource_product_results rp
        JOIN planning_fg_constrained_results fg
          ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
        JOIN products p ON p.product_id = rp.product_id
        WHERE rp.run_id = ? AND fg.backlog_qty > 0
        GROUP BY rp.resource_id, rp.month
    """, (run_id,)).fetchall()
    rev_map = {(r["resource_id"], r["month"]): r["revenue_at_risk"] for r in rev_risk}

    # Cost rates from DB — keyed by (resource_type, shift_model)
    cost_rows = conn.execute("SELECT * FROM production_cost_rates").fetchall()
    cost_map = {(r["resource_type"], r["shift_model"]): r["cost_per_min_eur"] for r in cost_rows}

    # Bank holidays per resource (via calendar assignments)
    hol_rows = conn.execute("""
        SELECT rca.resource_id, ch.holiday_date, ch.holiday_name
        FROM resource_calendar_assignments rca
        JOIN calendar_holidays ch ON ch.calendar_id = rca.calendar_id
        WHERE ch.is_working_day_override = 0
    """).fetchall()
    from collections import defaultdict as _dd
    from datetime import date as _date
    holidays_by_res = _dd(list)
    for row in hol_rows:
        holidays_by_res[row["resource_id"]].append(
            (_date.fromisoformat(row["holiday_date"]), row["holiday_name"])
        )

    conn.close()

    def _holiday_cap_for_month(resource_id, month_str, weekly_cap_hours):
        """Return (added_min, holiday_count, holiday_names) for holidays in this month."""
        y, mo = int(month_str[:4]), int(month_str[5:7])
        ms = _date(y, mo, 1)
        import calendar as _cal
        me = _date(y, mo, _cal.monthrange(y, mo)[1])
        daily_cap_min = (weekly_cap_hours * 60) / 5.0
        hits = [(hd, nm) for hd, nm in holidays_by_res.get(resource_id, [])
                if ms <= hd <= me and hd.weekday() < 5]
        total_min = round(len(hits) * daily_cap_min)
        names = [nm for _, nm in hits]
        return total_min, len(hits), names

    solutions = []
    for r in overloaded:
        rtype = r["resource_type"]
        overload     = round(r["backlog_load_min"])
        current_cap  = r["capacity_min"]
        weekly_cap   = r["weekly_capacity"]

        sat_cost_per_min     = cost_map.get((rtype, "saturday"), cost_map.get(("production", "saturday"), 0.60))
        night_cost_per_min   = cost_map.get((rtype, "night"),    cost_map.get(("production", "night"),    0.50))
        holiday_cost_per_min = cost_map.get((rtype, "sunday"),   cost_map.get(("production", "sunday"),   0.80))

        # Saturday shift: ~4 Saturdays × 8h × 2 shifts = 3,840 min/month
        sat_cap      = 3840
        # 3rd night shift Mon-Fri: 8h × 5 days × 4 weeks = 9,600 min/month
        night_cap    = 9600
        combined_cap = sat_cap + night_cap

        # Holiday work: actual bank holidays in this month on working days
        hol_cap, hol_count, hol_names = _holiday_cap_for_month(
            r["resource_id"], r["month"], weekly_cap
        )

        solutions.append({
            "resource_id":            r["resource_id"],
            "resource_name":          r["resource_name"],
            "plant_id":               r["plant_id"],
            "month":                  r["month"],
            "current_capacity_min":   round(current_cap),
            "current_load_min":       round(r["load_min"]),
            "overload_min":           overload,
            "unconstrained_load_min": round(r["unconstrained_load_min"]),
            "utilization_pct":        round(r["unconstrained_load_min"] / current_cap * 100, 1) if current_cap > 0 else 100,
            "revenue_at_risk_eur":    round(rev_map.get((r["resource_id"], r["month"]), 0)),
            "saturday_shift": {
                "added_capacity_min":  sat_cap,
                "cost_eur":            round(sat_cap * sat_cost_per_min),
                "resolves_overload":   overload <= 0 or sat_cap >= overload,
                "recovery_ratio":      min(1.0, sat_cap / overload) if overload > 0 else 1.0,
                "new_utilization_pct": round(r["unconstrained_load_min"] / (current_cap + sat_cap) * 100, 1) if (current_cap + sat_cap) > 0 else 100,
            },
            "third_shift": {
                "added_capacity_min":  night_cap,
                "cost_eur":            round(night_cap * night_cost_per_min),
                "resolves_overload":   overload <= 0 or night_cap >= overload,
                "recovery_ratio":      min(1.0, night_cap / overload) if overload > 0 else 1.0,
                "new_utilization_pct": round(r["unconstrained_load_min"] / (current_cap + night_cap) * 100, 1) if (current_cap + night_cap) > 0 else 100,
            },
            "combined": {
                "added_capacity_min":  combined_cap,
                "cost_eur":            round(sat_cap * sat_cost_per_min + night_cap * night_cost_per_min),
                "resolves_overload":   overload <= 0 or combined_cap >= overload,
                "recovery_ratio":      min(1.0, combined_cap / overload) if overload > 0 else 1.0,
                "new_utilization_pct": round(r["unconstrained_load_min"] / (current_cap + combined_cap) * 100, 1) if (current_cap + combined_cap) > 0 else 100,
            },
            "holiday_work": {
                "added_capacity_min":  hol_cap,
                "cost_eur":            round(hol_cap * holiday_cost_per_min),
                "holiday_count":       hol_count,
                "holiday_names":       hol_names,
                "resolves_overload":   overload <= 0 or hol_cap >= overload,
                "recovery_ratio":      min(1.0, hol_cap / overload) if overload > 0 else 1.0,
                "new_utilization_pct": round(r["unconstrained_load_min"] / (current_cap + hol_cap) * 100, 1) if (current_cap + hol_cap) > 0 else 100,
                "note":                f"+100% legal holiday premium (Feiertagszuschlag)",
            },
        })

    return jsonify(solutions)


# ── BOTTLENECK CHAIN ──────────────────────────────────────────────────────────

@app.route("/api/capacity/bottleneck_chain")
def api_bottleneck_chain():
    """
    Theory of Constraints bottleneck chain:
    - Nodes: overloaded resources (aggregated across all months)
    - Edges: resources linked by shared products (product loads both resources)
    - Product loads: min_per_unit for cascade calculation
    - Backlog: unresolved backlog per product
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    # Overloaded resources (aggregate across months)
    overloaded = conn.execute("""
        SELECT r.resource_id, r.resource_name, r.plant_id,
               SUM(r.capacity_min) AS capacity_min,
               SUM(r.load_min) AS load_min,
               SUM(COALESCE(bl.extra_min, 0)) AS overload_min,
               ROUND(SUM(r.load_min + COALESCE(bl.extra_min,0)) * 100.0 / SUM(r.capacity_min), 1) AS utilization_pct,
               SUM(COALESCE(rev.revenue_at_risk,0)) AS revenue_at_risk
        FROM planning_resource_month_results r
        LEFT JOIN (
            SELECT rp.resource_id, rp.month,
                   SUM(fg.backlog_qty * rp.allocated_min / rp.allocated_qty) AS extra_min
            FROM planning_resource_product_results rp
            JOIN planning_fg_constrained_results fg
              ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
            WHERE rp.run_id = ? AND rp.allocated_qty > 0 AND fg.backlog_qty > 0
            GROUP BY rp.resource_id, rp.month
        ) bl ON bl.resource_id = r.resource_id AND bl.month = r.month
        LEFT JOIN (
            SELECT rp.resource_id, rp.month,
                   SUM(fg.backlog_qty * p.unit_price) AS revenue_at_risk
            FROM planning_resource_product_results rp
            JOIN planning_fg_constrained_results fg
              ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
            JOIN products p ON p.product_id = rp.product_id
            WHERE rp.run_id = ? AND fg.backlog_qty > 0
            GROUP BY rp.resource_id, rp.month
        ) rev ON rev.resource_id = r.resource_id AND rev.month = r.month
        WHERE r.run_id = ? AND r.utilization_pct >= 90
        GROUP BY r.resource_id, r.resource_name, r.plant_id
        ORDER BY utilization_pct DESC
    """, (run_id, run_id, run_id)).fetchall()

    if not overloaded:
        conn.close()
        return jsonify({"nodes": [], "edges": [], "product_loads": [], "backlog": []})

    overloaded_ids = [r["resource_id"] for r in overloaded]

    # Cost rates from DB — keyed by (resource_type, shift_model)
    cost_rows = conn.execute("SELECT * FROM production_cost_rates").fetchall()
    btl_cost_map = {(r["resource_type"], r["shift_model"]): r["cost_per_min_eur"] for r in cost_rows}
    sat_added   = 3840
    night_added = 9600
    comb_added  = sat_added + night_added

    # Build nodes
    rtype_rows = conn.execute(
        "SELECT resource_id, resource_type FROM production_resources"
    ).fetchall()
    rtype_map = {r["resource_id"]: r["resource_type"] for r in rtype_rows}

    nodes = []
    for r in overloaded:
        rtype = rtype_map.get(r["resource_id"], "production")
        sat_cpm   = btl_cost_map.get((rtype, "saturday"), btl_cost_map.get(("production", "saturday"), 3.0))
        night_cpm = btl_cost_map.get((rtype, "night"),    btl_cost_map.get(("production", "night"),    2.5))
        overload  = round(r["overload_min"])
        cap       = round(r["capacity_min"])
        sat_cost   = round(sat_added * sat_cpm)
        night_cost = round(night_added * night_cpm)
        comb_cost  = sat_cost + night_cost
        nodes.append({
            "resource_id":      r["resource_id"],
            "resource_name":    r["resource_name"],
            "plant_id":         r["plant_id"],
            "capacity_min":     cap,
            "load_min":         round(r["load_min"]),
            "overload_min":     overload,
            "utilization_pct":  r["utilization_pct"],
            "revenue_at_risk":  round(r["revenue_at_risk"]),
            "saturday_cost":    sat_cost,
            "night_cost":       night_cost,
            "combined_cost":    comb_cost,
            "saturday_added":   sat_added,
            "night_added":      night_added,
            "combined_added":   comb_added,
            "saturday_resolves": overload <= 0 or sat_added >= overload,
            "night_resolves":    overload <= 0 or night_added >= overload,
            "combined_resolves": overload <= 0 or comb_added >= overload,
        })

    # Product loads per overloaded resource (min_per_unit for cascade)
    placeholders = ",".join(["?" for _ in overloaded_ids])
    prod_loads = conn.execute(f"""
        SELECT rp.resource_id, rp.product_id,
               SUM(rp.allocated_min) AS total_min,
               CASE WHEN SUM(rp.allocated_qty) > 0
                    THEN ROUND(SUM(rp.allocated_min)*1.0/SUM(rp.allocated_qty), 2)
                    ELSE 0 END AS min_per_unit
        FROM planning_resource_product_results rp
        WHERE rp.run_id = ? AND rp.resource_id IN ({placeholders})
        GROUP BY rp.resource_id, rp.product_id
    """, [run_id] + overloaded_ids).fetchall()

    # Products on ≥2 overloaded resources → edges
    prod_to_resources = {}
    for row in prod_loads:
        prod_to_resources.setdefault(row["product_id"], []).append(row["resource_id"])

    # Resource routing order via operations + eligibility (column: sequence_no)
    routing_rows = conn.execute(f"""
        SELECT por.product_id, ore.resource_id, MIN(por.sequence_no) AS seq
        FROM product_operation_routings por
        JOIN operation_resource_eligibility ore ON ore.operation_id = por.operation_id AND ore.is_primary = 1
        WHERE ore.resource_id IN ({placeholders})
        GROUP BY por.product_id, ore.resource_id
    """, overloaded_ids).fetchall()
    routing_map = {(r["product_id"], r["resource_id"]): r["seq"] for r in routing_rows}

    # Build edges between overloaded resource pairs sharing products
    product_name_rows = conn.execute("SELECT product_id, name FROM products").fetchall()
    prod_name_map = {r["product_id"]: r["name"] for r in product_name_rows}

    min_per_unit_map = {(r["resource_id"], r["product_id"]): r["min_per_unit"] for r in prod_loads}

    edge_map = {}
    for pid, res_list in prod_to_resources.items():
        if len(res_list) < 2:
            continue
        # Sort resources by routing sequence to determine direction
        sorted_res = sorted(res_list, key=lambda r: routing_map.get((pid, r), 999))
        for i in range(len(sorted_res) - 1):
            src, tgt = sorted_res[i], sorted_res[i+1]
            key = (src, tgt)
            if key not in edge_map:
                edge_map[key] = {"source": src, "target": tgt, "products": [], "total_min": 0}
            edge_map[key]["products"].append({
                "product_id": pid,
                "name": prod_name_map.get(pid, pid),
                "min_per_unit_src": min_per_unit_map.get((src, pid), 0),
                "min_per_unit_tgt": min_per_unit_map.get((tgt, pid), 0),
            })
            edge_map[key]["total_min"] += min_per_unit_map.get((src, pid), 0)

    # Backlog per product
    backlog_rows = conn.execute("""
        SELECT fg.product_id, SUM(fg.backlog_qty) AS backlog_qty
        FROM planning_fg_constrained_results fg
        WHERE fg.run_id = ?
        GROUP BY fg.product_id
        HAVING SUM(fg.backlog_qty) > 0
    """, (run_id,)).fetchall()

    conn.close()
    return jsonify({
        "nodes":        nodes,
        "edges":        list(edge_map.values()),
        "product_loads": rows_to_list(prod_loads),
        "backlog":      rows_to_list(backlog_rows),
    })


# ── RESOURCE × PRODUCT LOAD (for pull-forward stacked chart) ─────────────────

@app.route("/api/supply/resource_product_load")
def api_resource_product_load():
    """Per-resource, per-product, per-month production load + prebuild flag."""
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    rows = conn.execute("""
        SELECT rp.resource_id, r.resource_name, r.plant_id, rp.product_id, rp.month,
               rp.allocated_min, rp.allocated_qty,
               r.capacity_min,
               pr.name AS product_name,
               COALESCE(fg.prebuild_inventory_qty, 0) AS prebuild_qty
        FROM planning_resource_product_results rp
        JOIN planning_resource_month_results r
          ON r.run_id = rp.run_id AND r.resource_id = rp.resource_id AND r.month = rp.month
        JOIN products pr ON pr.product_id = rp.product_id
        LEFT JOIN planning_fg_constrained_results fg
          ON fg.run_id = rp.run_id AND fg.product_id = rp.product_id AND fg.month = rp.month
        WHERE rp.run_id = ? AND rp.month IN ({})
        ORDER BY rp.resource_id, rp.month, rp.allocated_min DESC
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), [run_id] + HORIZON_MONTHS).fetchall()

    conn.close()
    return jsonify(rows_to_list(rows))


# ── SUPPLIERS & RISK ──────────────────────────────────────────────────────────

@app.route("/api/suppliers/overview")
def api_suppliers_overview():
    conn = get_db()
    rows = conn.execute("""
        SELECT s.supplier_id, s.name, s.country, s.region, s.city, s.lead_time_days,
               s.reliability_score, s.category,
               r.supplier_segment, r.po_count,
               r.avg_deviation_vs_confirmed_days, r.avg_deviation_vs_requested_days,
               r.on_time_rate, r.late_rate, r.early_rate,
               COUNT(DISTINCT m.material_id) as component_count
        FROM suppliers s
        LEFT JOIN supplier_inbound_reliability r ON r.supplier_id = s.supplier_id
        LEFT JOIN material_supplier_mapping m ON m.supplier_id = s.supplier_id
        GROUP BY s.supplier_id
        ORDER BY s.reliability_score DESC
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/suppliers/deliveries")
def api_suppliers_deliveries():
    conn = get_db()
    rows = conn.execute("""
        SELECT d.*, m.name as material_name, s.name as supplier_name
        FROM inbound_deliveries d
        LEFT JOIN materials m ON m.material_id = d.item_id
        LEFT JOIN suppliers s ON s.supplier_id = d.supplier_id
        ORDER BY d.requested_delivery_date DESC
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/suppliers/capacity")
def api_suppliers_capacity():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.capacity_row_id, c.supplier_id, c.material_id, c.month,
               c.allocated_capacity_qty, c.committed_qty, c.flex_up_pct, c.flex_down_pct,
               c.capacity_type,
               CASE WHEN c.allocated_capacity_qty > 0
                    THEN c.committed_qty * 100.0 / c.allocated_capacity_qty
                    ELSE 0 END as utilization_pct,
               s.name as supplier_name,
               mat.name as material_name
        FROM supplier_component_capacity c
        LEFT JOIN suppliers s ON s.supplier_id = c.supplier_id
        LEFT JOIN materials mat ON mat.material_id = c.material_id
        WHERE c.allocated_capacity_qty > 0
          AND c.month IN ({})
        ORDER BY s.name, mat.name, c.month
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), HORIZON_MONTHS).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/suppliers/substitutions")
def api_suppliers_substitutions():
    conn = get_db()
    rows = conn.execute("""
        SELECT ms.*,
               m1.name as original_material_name,
               m2.name as substitute_material_name
        FROM material_substitutions ms
        LEFT JOIN materials m1 ON m1.material_id = ms.material_id
        LEFT JOIN materials m2 ON m2.material_id = ms.substitute_material_id
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/suppliers/risk")
def api_suppliers_risk():
    conn = get_db()
    # Get all bought components with supplier details
    rows = conn.execute("""
        SELECT m.material_id, m.name as material_name,
               COUNT(DISTINCT msm.supplier_id) as supplier_count,
               s.supplier_id as primary_supplier_id,
               s.name as primary_supplier_name,
               s.lead_time_days as primary_lead_time,
               s.reliability_score as primary_reliability
        FROM materials m
        LEFT JOIN material_supplier_mapping msm ON msm.material_id = m.material_id
        LEFT JOIN material_supplier_mapping msm_pri
               ON msm_pri.material_id = m.material_id AND msm_pri.supplier_priority = 1
        LEFT JOIN suppliers s ON s.supplier_id = msm_pri.supplier_id
        WHERE m.make_buy = 'buy'
        GROUP BY m.material_id
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        flags = []
        supplier_count = r["supplier_count"] or 0
        lead_time = r["primary_lead_time"] or 0
        reliability = r["primary_reliability"] or 1.0

        is_single_source = supplier_count <= 1

        if is_single_source:
            flags.append("Single source")
        if lead_time > 21:
            flags.append(f"Long lead time ({lead_time}d)")
        if reliability < 0.95:
            flags.append(f"Low reliability ({round(reliability*100)}%)")

        if len(flags) >= 2 or (is_single_source and len(flags) >= 2):
            risk_level = "high"
        elif is_single_source and len(flags) == 1:
            risk_level = "high"
        elif len(flags) == 1:
            risk_level = "medium"
        else:
            risk_level = "low"

        result.append({
            "material_id": r["material_id"],
            "material_name": r["material_name"],
            "supplier_count": supplier_count,
            "primary_supplier_id": r["primary_supplier_id"],
            "primary_supplier_name": r["primary_supplier_name"],
            "primary_lead_time": lead_time,
            "primary_reliability": reliability,
            "is_single_source": is_single_source,
            "risk_flags": flags,
            "risk_level": risk_level
        })

    # Sort: high first, then medium, then low
    order = {"high": 0, "medium": 1, "low": 2}
    result.sort(key=lambda x: order[x["risk_level"]])
    return jsonify(result)


# ── SUPPLIER WORLD MAP ────────────────────────────────────────────────────────

# Hardcoded coordinates for known cities
_SUPPLIER_COORDS = {
    "SUP001": (50.1109,  8.6821),   # Frankfurt
    "SUP002": (59.3346, 18.0632),   # Stockholm
    "SUP003": (45.7640,  4.8357),   # Lyon
    "SUP004": (50.0755, 14.4378),   # Prague
    "SUP005": (22.5431, 114.0579),  # Shenzhen
    "SUP006": (25.0330, 121.5654),  # Taipei
    "SUP007": (37.5665, 126.9780),  # Seoul
    "SUP008": (10.8231, 106.6297),  # Ho Chi Minh City
    "SUP009": (34.6937, 135.5022),  # Osaka
    "SUP010": (23.0207, 113.7518),  # Dongguan
}
_PLANT_COORDS = {
    "PLT001": (49.0069,  8.4037, "ElectroTech Karlsruhe"),
    "PLT002": (45.7640,  4.8357, "ElectroTech Lyon"),
    "PLT003": (52.5200, 13.4050, "ElectroTech Berlin"),
}


@app.route("/api/suppliers/map")
def api_suppliers_map():
    """World-map data: supplier nodes, plant nodes, and supplier→plant flow arcs."""
    conn = get_db()

    suppliers = {r["supplier_id"]: dict(r)
                 for r in conn.execute("SELECT * FROM suppliers").fetchall()}

    # Annual supply volume per supplier (capacity units) for node sizing
    cap = {r["supplier_id"]: r["total"]
           for r in conn.execute("""
               SELECT supplier_id, SUM(allocated_capacity_qty) as total
               FROM supplier_component_capacity GROUP BY supplier_id
           """).fetchall()}

    # Supplier → plant flows from inbound_deliveries
    flow_rows = conn.execute("""
        SELECT d.supplier_id, d.destination_id AS plant_id,
               COUNT(*)              AS delivery_count,
               SUM(d.ordered_quantity) AS total_qty,
               SUM(d.ordered_quantity * m.unit_cost) AS goods_value
        FROM inbound_deliveries d
        JOIN materials m ON m.material_id = d.item_id
        GROUP BY d.supplier_id, d.destination_id
    """).fetchall()

    # Lane-level freight cost: cost_per_unit from lane_definitions
    lane_rows = conn.execute("""
        SELECT source_id AS supplier_id, destination_id AS plant_id,
               transport_mode, transport_cost_per_unit, lead_time_days
        FROM lane_definitions
        WHERE source_type = 'supplier' AND destination_type = 'plant'
    """).fetchall()
    lane_map = {(r["supplier_id"], r["plant_id"]): dict(r) for r in lane_rows}

    # 12-month planned freight from supplier_component_capacity × lane cost
    planned_rows = conn.execute("""
        SELECT sc.supplier_id, d.destination_id AS plant_id,
               SUM(sc.committed_qty) AS planned_committed_qty,
               SUM(sc.allocated_capacity_qty) AS planned_capacity_qty
        FROM supplier_component_capacity sc
        JOIN (SELECT DISTINCT supplier_id, destination_id FROM inbound_deliveries) d
          ON d.supplier_id = sc.supplier_id
        GROUP BY sc.supplier_id, d.destination_id
    """).fetchall()
    planned_map = {(r["supplier_id"], r["plant_id"]): dict(r) for r in planned_rows}

    # Freight rate table for % of value fallback
    freq_rows = conn.execute("SELECT * FROM material_freight_rates").fetchall()
    freq_map = {(r["supplier_region"], r["transport_mode"]): r["freight_pct_of_value"]
                for r in freq_rows}

    conn.close()

    # Build supplier list
    result_suppliers = []
    for sid, s in suppliers.items():
        lat, lng = _SUPPLIER_COORDS.get(sid, (None, None))
        result_suppliers.append({
            "supplier_id":      sid,
            "name":             s["name"],
            "city":             s["city"],
            "country":          s["country"],
            "category":         s["category"],
            "reliability_score": s["reliability_score"],
            "lead_time_days":   s["lead_time_days"],
            "lat": lat, "lng": lng,
            "total_capacity":   cap.get(sid, 0),
        })

    # Build plant list
    result_plants = [
        {"plant_id": pid, "name": coords[2], "lat": coords[0], "lng": coords[1]}
        for pid, coords in _PLANT_COORDS.items()
    ]

    # Build flows with freight cost
    result_flows = []
    for f in flow_rows:
        sid, pid = f["supplier_id"], f["plant_id"]
        if sid not in _SUPPLIER_COORDS or pid not in _PLANT_COORDS:
            continue

        lane = lane_map.get((sid, pid), {})
        mode = lane.get("transport_mode", "truck")
        cost_per_unit = lane.get("transport_cost_per_unit", 0)
        total_qty = f["total_qty"] or 0
        goods_value = f["goods_value"] or 0

        # Freight as % of goods value (from rate table: EU truck=1.8%, Asia sea=4.8%)
        region = "Asia" if suppliers.get(sid, {}).get("region") == "Asia" else "EU"
        freq_pct = freq_map.get((region, mode), freq_map.get(("Asia", "sea") if region == "Asia" else ("EU", "truck"), 0))
        freight_pct_of_value = round(freq_pct * 100, 1)

        # Actual freight cost = % of goods value (correct; per-unit rate is per-shipment proxy)
        actual_freight_eur = round(goods_value * freq_pct)

        # 12-month planned freight: use allocated capacity qty × avg unit cost × freq_pct
        planned = planned_map.get((sid, pid), {})
        planned_qty = planned.get("planned_committed_qty") or planned.get("planned_capacity_qty") or 0
        # avg unit cost from this flow's goods_value/total_qty
        avg_unit_cost = (goods_value / total_qty) if total_qty > 0 else 0
        planned_freight_eur = round(planned_qty * avg_unit_cost * freq_pct)

        result_flows.append({
            "supplier_id":         sid,
            "plant_id":            pid,
            "delivery_count":      f["delivery_count"],
            "total_qty":           total_qty,
            "goods_value_eur":     round(goods_value),
            "transport_mode":      mode,
            "cost_per_unit_eur":   cost_per_unit,
            "actual_freight_eur":  actual_freight_eur,
            "freight_pct_of_value": freight_pct_of_value,
            "planned_freight_eur": planned_freight_eur,
            "capacity_weight":     cap.get(sid, 1),
        })

    # Summary totals per supplier (across all plants)
    sup_freight = {}
    for f in result_flows:
        sid = f["supplier_id"]
        sup_freight[sid] = sup_freight.get(sid, 0) + f["actual_freight_eur"]

    return jsonify({
        "suppliers": result_suppliers,
        "plants":    result_plants,
        "flows":     result_flows,
        "supplier_freight_totals": sup_freight,
    })


# ── SANKEY FLOWS ──────────────────────────────────────────────────────────────

@app.route("/api/sankey")
def api_sankey():
    """
    Returns node/link data for two Sankey views:
      revenue   – Products → Channels → Fulfilled / Backlog  (€ revenue)
      production– Plants → Resources → Products              (€ production value)
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    products = {r["product_id"]: dict(r)
                for r in conn.execute("SELECT * FROM products").fetchall()}

    # Demand by product × channel — use the same computed stat forecast
    stat_fc = compute_statistical_forecast()   # {(product_id, channel): {month: qty}}
    fc_by_prod_ch = {
        (prod, ch): sum(monthly.values())
        for (prod, ch), monthly in stat_fc.items()
    }

    # Constrained supply + backlog by product (12-month horizon)
    supply_rows = conn.execute("""
        SELECT product_id,
               SUM(constrained_production_qty) AS supply_qty,
               SUM(backlog_qty)                AS backlog_qty
        FROM planning_fg_constrained_results
        WHERE run_id = ?
        GROUP BY product_id
    """, (run_id,)).fetchall()
    supply_map = {r["product_id"]: dict(r) for r in supply_rows}

    # Resource → Product allocations (for production view)
    rp_rows = conn.execute("""
        SELECT rp.resource_id, rp.resource_name, rp.product_id,
               SUM(rp.allocated_qty) AS qty
        FROM planning_resource_product_results rp
        WHERE rp.run_id = ?
        GROUP BY rp.resource_id, rp.product_id
    """, (run_id,)).fetchall()

    # Plant assignments for resources
    res_plant = {r["resource_id"]: r["plant_id"]
                 for r in conn.execute("""
                     SELECT DISTINCT resource_id, plant_id
                     FROM planning_resource_month_results WHERE run_id = ?
                 """, (run_id,)).fetchall()}

    plant_names = {r["plant_id"]: r["name"]
                   for r in conn.execute("SELECT plant_id, name FROM plants").fetchall()}

    conn.close()

    # ── Revenue Sankey ────────────────────────────────────────────────────────
    # Build channel demand per product from computed forecast
    prod_ch = {}
    for (prod, ch), demand_qty in fc_by_prod_ch.items():
        prod_ch.setdefault(prod, {})[ch] = demand_qty

    rev_nodes, rev_links = [], []
    node_set = {}

    def rev_node(nid, name, group, extra=None):
        if nid not in node_set:
            node_set[nid] = len(rev_nodes)
            rev_nodes.append({"id": nid, "name": name, "group": group, **(extra or {})})
        return node_set[nid]

    # Product family colour buckets
    fam_colours = {
        "Smart Home":     "#4f8ef7",
        "Security":       "#7c3aed",
        "Industrial IoT": "#f59e0b",
        "Industrial":     "#ef4444",
        "Networking":     "#10b981",
        "Consumer":       "#06b6d4",
    }

    ch_fulfilled_rev = {}
    ch_backlog_rev   = {}

    for pid, ch_dict in prod_ch.items():
        p     = products.get(pid, {})
        price = float(p.get("unit_price") or 0)
        fam   = p.get("product_family", "Other")
        rev_node(pid, p.get("name", pid), "product",
                 {"family": fam, "colour": fam_colours.get(fam, "#94a3b8")})

        total_demand = sum(ch_dict.values())
        sm = supply_map.get(pid, {"supply_qty": 0, "backlog_qty": 0})
        supply_qty   = float(sm["supply_qty"])
        backlog_qty  = float(sm["backlog_qty"])

        for ch, demand_qty in ch_dict.items():
            revenue = round(demand_qty * price)
            if revenue <= 0:
                continue
            rev_node(ch, f"{ch} Channel", "channel")
            rev_links.append({
                "source": pid, "target": ch, "value": revenue,
                "label": f"€{revenue:,.0f}"
            })
            share = demand_qty / total_demand if total_demand else 0
            ch_fulfilled_rev[ch] = ch_fulfilled_rev.get(ch, 0) + min(supply_qty * share, demand_qty) * price
            ch_backlog_rev[ch]   = ch_backlog_rev.get(ch, 0) + backlog_qty * share * price

    for ch in sorted(ch_fulfilled_rev):
        rev_node("fulfilled", "✓ Fulfilled", "fulfilled")
        rev_node("backlog",   "⚠ Backlog / At Risk", "backlog")
        if ch_fulfilled_rev[ch] > 0:
            rev_links.append({"source": ch, "target": "fulfilled",
                               "value": round(ch_fulfilled_rev[ch])})
        if ch_backlog_rev.get(ch, 0) > 0:
            rev_links.append({"source": ch, "target": "backlog",
                               "value": round(ch_backlog_rev[ch])})

    # ── Production Sankey ─────────────────────────────────────────────────────
    prod_nodes, prod_links = [], []
    pnode_set = {}

    def prod_node(nid, name, group, extra=None):
        if nid not in pnode_set:
            pnode_set[nid] = len(prod_nodes)
            prod_nodes.append({"id": nid, "name": name, "group": group, **(extra or {})})
        return pnode_set[nid]

    plant_colours = {"PLT001": "#4f8ef7", "PLT002": "#10b981", "PLT003": "#f59e0b"}

    for r in rp_rows:
        pid   = r["product_id"]
        rid   = r["resource_id"]
        plt   = res_plant.get(rid, "PLT001")
        p     = products.get(pid, {})
        price = float(p.get("unit_price") or 0)
        value = round(float(r["qty"]) * price)
        if value <= 0:
            continue
        prod_node(plt, plant_names.get(plt, plt), "plant",
                  {"colour": plant_colours.get(plt, "#94a3b8")})
        prod_node(rid, r["resource_name"], "resource",
                  {"plant": plt, "colour": plant_colours.get(plt, "#94a3b8") + "99"})
        prod_node(pid, p.get("name", pid), "product",
                  {"family": p.get("product_family", "Other")})

        # Plant → Resource (aggregate all products through this resource)
        plt_res_key = f"{plt}→{rid}"
        existing = next((l for l in prod_links
                         if l["source"] == plt and l["target"] == rid), None)
        if existing:
            existing["value"] += value
        else:
            prod_links.append({"source": plt, "target": rid, "value": value})

        # Resource → Product
        existing2 = next((l for l in prod_links
                          if l["source"] == rid and l["target"] == pid), None)
        if existing2:
            existing2["value"] += value
        else:
            prod_links.append({"source": rid, "target": pid, "value": value})

    return jsonify({
        "revenue":    {"nodes": rev_nodes,  "links": rev_links},
        "production": {"nodes": prod_nodes, "links": prod_links}
    })


# ── EXEC SUMMARY ──────────────────────────────────────────────────────────────

# ── BUDGET ───────────────────────────────────────────────────────────────────

def seed_budget(conn):
    """
    Create and populate the budget table (idempotent).
    Budget year 2025 = 2024 actual revenue × 1.10, distributed using 2024 monthly
    seasonal shares, at product × channel granularity.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget (
            budget_id          TEXT PRIMARY KEY,
            budget_year        INT  NOT NULL,
            budget_month       TEXT NOT NULL,
            product_id         TEXT NOT NULL,
            channel            TEXT NOT NULL,
            budget_qty         REAL NOT NULL DEFAULT 0,
            budget_revenue_eur REAL NOT NULL DEFAULT 0,
            created_at         TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    if conn.execute("SELECT COUNT(*) FROM budget WHERE budget_year = 2025").fetchone()[0] > 0:
        return   # already seeded

    # 2024 monthly demand by product/channel (week_start covers full 2024)
    rows_2024 = conn.execute("""
        SELECT strftime('%m', week_start) AS month_num,
               product_id, channel,
               SUM(quantity) AS qty
        FROM demand_history
        WHERE week_start >= '2024-01-01' AND week_start < '2025-01-01'
        GROUP BY month_num, product_id, channel
    """).fetchall()

    prices = {r["product_id"]: r["unit_price"]
              for r in conn.execute("SELECT product_id, unit_price FROM products")}

    # Build revenue map: (product_id, channel, month_num 1-12) -> revenue
    rev_map = {}
    for r in rows_2024:
        key = (r["product_id"], r["channel"], int(r["month_num"]))
        rev_map[key] = rev_map.get(key, 0.0) + r["qty"] * prices.get(r["product_id"], 0)

    # Annual 2024 revenue per product/channel
    annual_2024 = {}
    for (pid, ch, _), rev in rev_map.items():
        annual_2024[(pid, ch)] = annual_2024.get((pid, ch), 0.0) + rev

    inserts = []
    for (pid, ch), ann_rev in annual_2024.items():
        ann_budget_2025 = ann_rev * 1.10
        # seasonal shares from 2024
        month_revs = {m: rev_map.get((pid, ch, m), 0.0) for m in range(1, 13)}
        total = sum(month_revs.values()) or 1.0
        for m in range(1, 13):
            share       = month_revs[m] / total
            brev        = ann_budget_2025 * share
            bqty        = brev / prices[pid] if prices.get(pid) else 0.0
            budget_month = f"2025-{m:02d}"
            inserts.append((
                f"BDG_2025_{pid}_{ch}_{m:02d}",
                2025, budget_month, pid, ch, round(bqty, 2), round(brev, 2)
            ))

    conn.executemany("""
        INSERT OR IGNORE INTO budget
            (budget_id, budget_year, budget_month, product_id, channel, budget_qty, budget_revenue_eur)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, inserts)
    conn.commit()


@app.route("/api/budget")
def api_budget():
    """Monthly + disaggregated budget for 2025."""
    conn = get_db()
    seed_budget(conn)

    monthly = conn.execute("""
        SELECT budget_month,
               ROUND(SUM(budget_revenue_eur), 0) AS budget_revenue_eur,
               ROUND(SUM(budget_qty), 0)         AS budget_qty
        FROM budget
        WHERE budget_year = 2025
        GROUP BY budget_month
        ORDER BY budget_month
    """).fetchall()

    by_product = conn.execute("""
        SELECT b.budget_month, b.product_id, p.name AS product_name,
               b.channel,
               ROUND(b.budget_qty, 0)         AS budget_qty,
               ROUND(b.budget_revenue_eur, 0) AS budget_revenue_eur
        FROM budget b
        JOIN products p ON p.product_id = b.product_id
        WHERE b.budget_year = 2025
        ORDER BY b.budget_month, b.product_id, b.channel
    """).fetchall()

    conn.close()
    return jsonify({
        "year":       2025,
        "monthly":    rows_to_list(monthly),
        "by_product": rows_to_list(by_product),
    })


@app.route("/api/exec/summary")
def api_exec_summary():
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    # Ensure budget table exists and is seeded
    seed_budget(conn)

    # Total demand by month — include revenue (demand_qty × unit_price)
    demand = conn.execute("""
        SELECT f.month,
               SUM(f.demand_to_produce_qty)                       AS total_demand,
               SUM(f.constrained_production_qty)                  AS total_supply,
               SUM(f.backlog_qty)                                  AS total_backlog,
               SUM(f.demand_to_produce_qty * p.unit_price)        AS forecast_revenue_eur
        FROM planning_fg_constrained_results f
        JOIN products p ON p.product_id = f.product_id
        WHERE f.run_id = ? AND f.month IN ({})
        GROUP BY f.month ORDER BY f.month
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), [run_id] + HORIZON_MONTHS).fetchall()

    # Resource summary - count overloaded months
    res_summary = conn.execute("""
        SELECT resource_id, resource_name, plant_id,
               SUM(CASE WHEN utilization_pct >= 100 THEN 1 ELSE 0 END) as overloaded_months,
               MAX(utilization_pct) as peak_util,
               AVG(utilization_pct) as avg_util
        FROM planning_resource_month_results
        WHERE run_id = ? AND month IN ({})
        GROUP BY resource_id, resource_name, plant_id
        ORDER BY peak_util DESC
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), [run_id] + HORIZON_MONTHS).fetchall()

    # Top backlog products
    top_backlog = conn.execute("""
        SELECT p.product_id, pr.name, SUM(p.backlog_qty) as total_backlog,
               COUNT(*) as backlog_months
        FROM planning_fg_constrained_results p
        JOIN products pr ON pr.product_id = p.product_id
        WHERE p.run_id = ? AND p.backlog_qty > 0
        GROUP BY p.product_id ORDER BY total_backlog DESC LIMIT 5
    """, (run_id,)).fetchall()

    # Revenue at risk (backlog * unit price)
    revenue_risk = conn.execute("""
        SELECT SUM(p.backlog_qty * pr.unit_price) as revenue_at_risk
        FROM planning_fg_constrained_results p
        JOIN products pr ON pr.product_id = p.product_id
        WHERE p.run_id = ?
    """, (run_id,)).fetchone()

    # Total 12m demand value
    total_demand_val = conn.execute("""
        SELECT SUM(p.demand_to_produce_qty * pr.unit_price) as total_demand_value
        FROM planning_fg_constrained_results p
        JOIN products pr ON pr.product_id = p.product_id
        WHERE p.run_id = ?
    """, (run_id,)).fetchone()

    # Inventory value
    inv_value = conn.execute("""
        SELECT SUM(i.quantity_on_hand * pr.unit_price) as inv_value
        FROM inventory i
        JOIN products pr ON pr.product_id = i.item_id
        WHERE i.item_type = 'finished_good'
    """).fetchone()

    # Prebuild inventory carrying cost
    # COGS per unit = unit_price − gross_margin (gross_margin ≈ backlog_penalty_unit from cost_parameters)
    # Carrying cost = SUM(prebuild_qty × COGS) × annual_rate/12 × avg_holding_months
    # pull_forward window = 6 months → average holding period = 3 months
    CARRYING_RATE   = 0.20   # 20 % annual inventory holding cost
    AVG_HOLD_MONTHS = 3.0    # 6-month pull window ÷ 2
    prebuild_rows = conn.execute("""
        SELECT f.product_id,
               SUM(f.prebuild_inventory_qty)                         AS prebuild_qty,
               p.unit_price,
               COALESCE(cp.unit_cost, p.unit_price * 0.35)          AS gross_margin_per_unit
        FROM planning_fg_constrained_results f
        JOIN products p  ON p.product_id  = f.product_id
        LEFT JOIN cost_parameters cp
               ON cp.scope_id   = f.product_id
              AND cp.cost_type  = 'backlog_penalty_unit'
        WHERE f.run_id = ? AND f.prebuild_inventory_qty > 0
        GROUP BY f.product_id, p.unit_price, cp.unit_cost
    """, (run_id,)).fetchall()

    prebuild_carry = 0.0
    prebuild_units = 0.0
    for row in prebuild_rows:
        cogs = max(0.0, row["unit_price"] - row["gross_margin_per_unit"])
        prebuild_carry += row["prebuild_qty"] * cogs * (CARRYING_RATE / 12) * AVG_HOLD_MONTHS
        prebuild_units += row["prebuild_qty"]

    # Items that cannot rebuild safety stock (ending_inventory < safety_stock_target)
    shortage_items = conn.execute("""
        SELECT pim.item_id,
               COALESCE(m.name, p.name) AS item_name,
               pim.item_type,
               COUNT(CASE WHEN pim.ending_inventory_qty < pim.safety_stock_qty - 1.0 THEN 1 END) AS below_ss_months,
               COUNT(CASE WHEN pim.ending_inventory_qty <= 0 THEN 1 END) AS stockout_months,
               MIN(pim.ending_inventory_qty) AS min_inventory,
               MAX(pim.safety_stock_qty) AS safety_stock_target
        FROM planning_item_month_results pim
        LEFT JOIN materials m ON m.material_id = pim.item_id
        LEFT JOIN products p ON p.product_id = pim.item_id
        WHERE pim.run_id = ? AND pim.month IN ({})
        GROUP BY pim.item_id
        HAVING below_ss_months > 0 AND safety_stock_target > 1.0
        ORDER BY pim.item_type, below_ss_months DESC
        LIMIT 15
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), [run_id] + HORIZON_MONTHS).fetchall()

    # Budget for the planning horizon months (some may fall outside 2025)
    budget_rows = conn.execute("""
        SELECT budget_month, ROUND(SUM(budget_revenue_eur), 0) AS budget_revenue_eur
        FROM budget
        WHERE budget_year = 2025 AND budget_month IN ({})
        GROUP BY budget_month ORDER BY budget_month
    """.format(",".join(["?" for _ in HORIZON_MONTHS])), HORIZON_MONTHS).fetchall()
    budget_map = {r["budget_month"]: r["budget_revenue_eur"] for r in budget_rows}

    # Total budget and forecast for attainment KPI — only months with a budget entry
    total_budget_horizon = sum(budget_map.values())
    total_fc_horizon     = sum(
        (r["forecast_revenue_eur"] or 0) for r in demand if r["month"] in budget_map
    )

    conn.close()

    return jsonify({
        "monthly_plan": rows_to_list(demand),
        "resource_summary": rows_to_list(res_summary),
        "top_backlog_products": rows_to_list(top_backlog),
        "revenue_at_risk": round(revenue_risk["revenue_at_risk"] or 0),
        "total_demand_value": round(total_demand_val["total_demand_value"] or 0),
        "inventory_value": round(inv_value["inv_value"] or 0),
        "shortage_items": rows_to_list(shortage_items),
        "horizon": HORIZON_MONTHS,
        "prebuild_carrying_cost_eur": round(prebuild_carry),
        "prebuild_units": round(prebuild_units),
        "budget_monthly": budget_map,          # {YYYY-MM: budget_revenue_eur}
        "total_budget_eur": round(total_budget_horizon),
        "budget_attainment_pct": round(total_fc_horizon / total_budget_horizon * 100, 1)
                                 if total_budget_horizon > 0 else None,
    })


# ── NATURAL LANGUAGE FORECAST COMMAND (Claude API) ───────────────────────────

@app.route("/api/forecast/nl_command", methods=["POST"])
def api_forecast_nl_command():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set on server"}), 503

    body = request.get_json(force=True)
    text        = body.get("text", "").strip()
    products    = body.get("products", [])    # [{product_id, name}]
    months      = body.get("months", [])      # ["2025-03", ...]
    month_labels = body.get("month_labels", [])

    if not text:
        return jsonify({"error": "No command text provided"}), 400

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        product_list = ", ".join(
            f'{p["product_id"]} ({p["name"]}, {p.get("forecast_12m_units", 0):.0f} units/yr)'
            for p in sorted(products[:20], key=lambda x: x.get("forecast_12m_units", 0), reverse=True)
        )
        month_list   = ", ".join(months)

        system_prompt = """You are a demand planning assistant. Parse natural language forecast adjustment commands and return structured JSON only — no explanation, no markdown.

Available products: """ + product_list + """
Available months (YYYY-MM): """ + month_list + """
Available channels: B2B, B2C

Return exactly one of these JSON shapes:

Percentage adjustment:
{"action":"adjust","products":["FG001"],"channels":["B2B","B2C"],"months":["2025-11"],"factor":1.10,"description":"..."}

Absolute set:
{"action":"set","products":["FG001"],"channels":["B2B"],"months":["2025-03"],"value":500,"description":"..."}

Clear overrides:
{"action":"clear","products":["all"],"channels":["B2B","B2C"],"months":["all"],"description":"..."}

Rules:
- "all products" or "all" → products: ["all"]
- "all months" or no month specified → months: ["all"]
- Q1=2025-01..03, Q2=04..06, Q3=07..09, Q4=10..12 — expand to matching months from the available list
- Month names map to YYYY-MM from the available months list. Multiple months joined by commas or "and" (e.g. "july, august and september") must all be expanded.
- Factor: increase 10% → 1.10, decrease 5% → 0.95
- "increase/set/raise PRODUCT in MONTHS to VALUE" means set an ABSOLUTE value (action: "set", not "adjust"). "to 150" is never a percentage.
- Products are listed above with their 12-month forecast volume. Use this to resolve ranking terms: "largest N products" = top N by forecast_12m_units, "smallest", "top 3", etc. Return the resolved product IDs explicitly.
- If the command is ambiguous or invalid, return: {"action":"error","description":"<reason>"}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system_prompt,
            messages=[{"role": "user", "content": text}]
        )

        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        return jsonify(parsed)

    except json.JSONDecodeError:
        return jsonify({"action": "error", "description": "Claude returned unparseable response: " + raw[:120]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask/forecast", methods=["POST"])
def api_ask_forecast():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY not set on server"}), 503

    body    = request.get_json(force=True)
    text    = body.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "message": "No command provided"}), 400

    conn = get_db()

    # ── build context ─────────────────────────────────────────────────────────
    products = rows_to_list(conn.execute(
        "SELECT product_id, name FROM products ORDER BY product_id").fetchall())
    product_lines = "\n".join(
        f"  {p['product_id']} – {p['name']}" for p in products)

    months_in_db = rows_to_list(conn.execute(
        "SELECT DISTINCT forecast_month FROM manual_forecast_history ORDER BY forecast_month").fetchall())
    all_months = sorted({r["forecast_month"][:7] for r in months_in_db} |
                        set(HORIZON_MONTHS))
    month_lines = ", ".join(sorted(all_months))

    # Sample a few rows so Claude understands the schema concretely
    sample = rows_to_list(conn.execute(
        "SELECT * FROM manual_forecast_history LIMIT 3").fetchall())
    sample_str = "\n".join(str(dict(r)) for r in sample)

    system_prompt = f"""You are a supply-chain demand planning assistant embedded in an S&OP application.
Your job: convert the planner's natural-language forecast command into a single valid SQLite SQL statement that modifies the table `manual_forecast_history`. Return ONLY the raw SQL — no explanation, no markdown, no code fences.

=== TABLE SCHEMA ===
CREATE TABLE manual_forecast_history (
  manual_forecast_id TEXT PRIMARY KEY,
  stat_forecast_id   TEXT NOT NULL,
  forecast_month     TEXT NOT NULL,   -- format YYYY-MM-01
  product_id         TEXT NOT NULL,
  channel            TEXT NOT NULL,   -- 'B2B' or 'B2C'
  planner_id         TEXT NOT NULL,
  actual_qty         INTEGER NOT NULL,
  statistical_forecast_qty INTEGER NOT NULL,
  manual_forecast_qty      INTEGER NOT NULL,
  adjustment_pct     REAL NOT NULL,
  abs_error          INTEGER NOT NULL,
  ape                REAL,
  note               TEXT,
  created_at         TEXT NOT NULL
);

=== SAMPLE ROWS ===
{sample_str}

=== AVAILABLE PRODUCTS ===
{product_lines}

=== PLANNING HORIZON (the months planners are adjusting — prefer these for unqualified month names) ===
{", ".join(sorted(HORIZON_MONTHS))}

=== ALL MONTHS IN DB (YYYY-MM-01 format) ===
{month_lines}

=== RULES ===
- forecast_month is stored as YYYY-MM-01 (e.g. 2025-07-01 for July 2025)
- When the planner says a month name, map it to the nearest matching YYYY-MM-01
- Quarters: Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec — expand to all matching months
- "all products" means all product_ids listed above
- "all channels" or no channel specified means both B2B and B2C
- "increase X by Y%" → new manual_forecast_qty = ROUND(manual_forecast_qty * (1 + Y/100))
- "set X to N" or "increase X to N" → manual_forecast_qty = N  (absolute value, not percentage)
- "decrease X by Y%" → new manual_forecast_qty = ROUND(manual_forecast_qty * (1 - Y/100))
- Always update adjustment_pct = CASE WHEN statistical_forecast_qty > 0 THEN ROUND((CAST(manual_forecast_qty AS REAL)/statistical_forecast_qty - 1)*100, 1) ELSE 0.0 END in the same statement
- Only emit UPDATE statements — never DELETE, DROP, INSERT, CREATE or ALTER
- If the command cannot be mapped to a safe UPDATE, return exactly: ERROR: <brief reason>
"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": text}]
        )
        sql = msg.content[0].text.strip()
        # Strip accidental code fences
        if sql.startswith("```"):
            sql = "\n".join(sql.split("\n")[1:])
            sql = sql.rsplit("```", 1)[0].strip()
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"Claude error: {e}"}), 500

    # ── safety gate ───────────────────────────────────────────────────────────
    import re as _re
    sql_upper = sql.upper().lstrip()
    if sql_upper.startswith("ERROR:"):
        conn.close()
        return jsonify({"ok": False, "message": sql[6:].strip()}), 200

    ALLOWED_TABLES = {"MANUAL_FORECAST_HISTORY"}
    FORBIDDEN_KEYWORDS = {"DELETE", "DROP", "CREATE", "ALTER", "INSERT",
                          "ATTACH", "DETACH", "PRAGMA", "VACUUM"}

    if not sql_upper.startswith("UPDATE"):
        conn.close()
        return jsonify({"ok": False, "message": "Only UPDATE statements are allowed here."}), 200

    for kw in FORBIDDEN_KEYWORDS:
        if kw in sql_upper.split():
            conn.close()
            return jsonify({"ok": False, "message": f"Forbidden keyword in generated SQL: {kw}"}), 200

    table_mentioned = any(t in sql_upper for t in ALLOWED_TABLES)
    if not table_mentioned:
        conn.close()
        return jsonify({"ok": False, "message": "SQL must target manual_forecast_history."}), 200

    # ── pre-seed any missing planning-horizon rows so UPDATE finds them ────────
    def _extract_targets(s):
        pids = (_re.findall(r"product_id\s*=\s*'([^']+)'", s, _re.I) +
                _re.findall(r"'([^']+)'", " ".join(_re.findall(r"product_id\s+IN\s*\(([^)]+)\)", s, _re.I))))
        chs  = ([x.upper() for x in _re.findall(r"channel\s*=\s*'([^']+)'", s, _re.I)] +
                [x.strip().strip("'").upper() for grp in _re.findall(r"channel\s+IN\s*\(([^)]+)\)", s, _re.I)
                 for x in grp.split(",")])
        fms  = (_re.findall(r"forecast_month\s*=\s*'([^']+)'", s, _re.I) +
                _re.findall(r"'([^']+)'", " ".join(_re.findall(r"forecast_month\s+IN\s*\(([^)]+)\)", s, _re.I))))
        return (list(set(pids)), list(set(chs)) or ["B2B","B2C"], list(set(fms)))

    target_pids, target_chs, target_fms = _extract_targets(sql)
    if target_pids and target_fms:
        stat_data = compute_statistical_forecast()  # {(pid, ch): {month: qty}}
        for pid in target_pids:
            for ch in target_chs:
                for fm in target_fms:
                    exists = conn.execute(
                        "SELECT 1 FROM manual_forecast_history WHERE product_id=? AND channel=? AND forecast_month=?",
                        (pid, ch, fm)).fetchone()
                    if not exists:
                        month_key = fm[:7]
                        stat_q = max(1, int(stat_data.get((pid, ch), {}).get(month_key, 0)))
                        mid = f"MFH_{pid}_{ch}_{fm.replace('-','')}_BASE"
                        conn.execute("""
                            INSERT OR IGNORE INTO manual_forecast_history
                            (manual_forecast_id, stat_forecast_id, forecast_month, product_id, channel,
                             planner_id, actual_qty, statistical_forecast_qty, manual_forecast_qty,
                             adjustment_pct, abs_error, ape, note, created_at)
                            VALUES (?,?,?,?,?,?,0,?,?,0,0,NULL,'',?)
                        """, (mid, "STAT_HORIZON", fm, pid, ch, "DPL001",
                              stat_q, stat_q, datetime.now().isoformat()))
        conn.commit()

    # ── execute ───────────────────────────────────────────────────────────────
    try:
        cur = conn.execute(sql)
        rows_affected = cur.rowcount
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"SQL execution error: {e}"}), 200

    # ── build confirmation message via Claude ─────────────────────────────────
    try:
        confirm_msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system="You are a concise assistant. Write one short confirmation sentence (max 15 words) describing what was just done.",
            messages=[{"role": "user", "content":
                f'The planner said: "{text}". The SQL ran and updated {rows_affected} rows. Confirm.'}]
        )
        confirmation = confirm_msg.content[0].text.strip()
    except Exception:
        confirmation = f"Done — {rows_affected} row(s) updated."

    conn.close()
    return jsonify({"ok": True, "message": confirmation, "rows_affected": rows_affected, "sql": sql})


@app.route("/api/ask/chat", methods=["POST"])
def api_ask_chat():
    """Unified chat endpoint: classifies user intent then routes to forecast or analytics."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY not set on server"}), 503

    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "message": "No message provided"}), 400

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        classify = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system='Classify the intent: reply with exactly one word — "write" if the user wants to modify/change/set/increase/decrease/clear the demand forecast, or "read" if the user is asking a question or wants to see/list/show/summarize data. No other words.',
            messages=[{"role": "user", "content": text}]
        )
        intent = classify.content[0].text.strip().lower()
    except Exception as e:
        return jsonify({"ok": False, "message": f"Classification error: {e}"}), 500

    if "write" in intent:
        return api_ask_forecast()
    else:
        return api_ask_analytics()


@app.route("/api/ask/analytics", methods=["POST"])
def api_ask_analytics():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"ok": False, "message": "ANTHROPIC_API_KEY not set on server"}), 503

    body = request.get_json(force=True)
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "message": "No question provided"}), 400

    # ── system prompt with schema overview ───────────────────────────────────
    system_prompt = """You are a supply-chain analytics assistant embedded in an S&OP application.
Your job: convert the user's natural-language question into a single valid SQLite SELECT query.
Return ONLY the raw SQL — no explanation, no markdown, no code fences.
Always add LIMIT 100 unless the user asks for aggregated totals.

=== DATABASE SCHEMA ===

-- Master data
products(product_id TEXT PK, name TEXT, product_family TEXT, bu_b2b INT, bu_b2c INT, demand_class TEXT, unit_price REAL, lead_time_days INT)
materials(material_id TEXT PK, name TEXT, material_type TEXT, level INT, lead_time_days INT, unit_cost REAL, make_buy TEXT)
bill_of_materials(parent_id TEXT, child_id TEXT, quantity INT, bom_level INT)
customers(customer_id TEXT PK, name TEXT, channel TEXT, country TEXT, segment TEXT, revenue_share REAL)
suppliers(supplier_id TEXT PK, name TEXT, country TEXT, region TEXT, reliability_score REAL, category TEXT)
plants(plant_id TEXT PK, name TEXT, country TEXT, city TEXT, specialization TEXT)
production_resources(resource_id TEXT PK, name TEXT, plant_id TEXT, resource_type TEXT, weekly_capacity INT, capacity_unit TEXT)

-- Demand & orders
demand_history(week_start TEXT, product_id TEXT, channel TEXT, quantity INT)
  -- weekly actual sales; week_start = YYYY-MM-DD; channel = 'B2B' or 'B2C'
customer_orders(order_id TEXT PK, customer_id TEXT, product_id TEXT, quantity INT, unit_price REAL,
  order_value_eur REAL,  -- ← revenue column; do NOT use "revenue" (no such column)
  order_date TEXT, requested_delivery_date TEXT, actual_delivery_date TEXT,
  delivered_quantity INT, status TEXT, priority TEXT,
  on_time_flag INT, in_full_flag INT, otif_flag INT)
open_orders(order_id TEXT PK, customer_id TEXT, product_id TEXT, quantity INT, order_date TEXT, requested_delivery_date TEXT, status TEXT, priority TEXT)
expected_receipts(receipt_id TEXT PK, item_id TEXT, supplier_id TEXT, destination_id TEXT, quantity INT, expected_date TEXT, status TEXT)

-- Forecast accuracy  ← KEY TABLES FOR ACCURACY QUESTIONS
statistical_forecast_history(
  stat_forecast_id TEXT PK,  -- FK referenced by manual_forecast_history.stat_forecast_id
  forecast_month TEXT,       -- YYYY-MM-01; covers 2023-08 to 2025-01 (historical only)
  product_id TEXT,
  channel TEXT,              -- 'B2B' or 'B2C'
  actual_qty INT,            -- real demand that occurred
  statistical_forecast_qty INT,
  bias_qty INT,
  abs_error INT,
  ape REAL                   -- absolute percentage error = abs_error/actual_qty; pre-computed
)
manual_forecast_history(
  manual_forecast_id TEXT PK,
  stat_forecast_id TEXT,     -- FK → statistical_forecast_history.stat_forecast_id (for historical rows)
  forecast_month TEXT,       -- YYYY-MM-01
  product_id TEXT,
  channel TEXT,
  statistical_forecast_qty INT,
  manual_forecast_qty INT,
  actual_qty INT,            -- 0 for planning horizon (2025-03 onward); >0 only for historical rows
  adjustment_pct REAL,
  note TEXT,
  created_at TEXT
)
planner_forecast_accuracy VIEW  -- grouped by PLANNER only, not product
  (planner_id, planner_name, channel, stat_mape REAL, manual_mape REAL, avg_adjustment_pct REAL, improved_rows INT)

-- Planning results
planning_runs(run_id TEXT PK, run_ts TEXT, horizon_start TEXT, horizon_end TEXT)
  -- always filter to latest run: WHERE run_id = (SELECT run_id FROM planning_runs ORDER BY run_ts DESC LIMIT 1)
planning_fg_constrained_results(run_id TEXT, product_id TEXT, month TEXT,
  demand_to_produce_qty REAL, constrained_production_qty REAL,
  backlog_qty REAL,           -- ← units that CANNOT be produced due to capacity; "backlog" in S&OP = THIS, not customer_orders.status
  prebuild_inventory_qty REAL)
  -- revenue at risk from backlog = backlog_qty * products.unit_price (join on product_id)
  -- customer_orders.status values are: 'delivered', 'cancelled', 'partial' — NO 'backlog' status exists there
planning_resource_month_results(run_id TEXT, resource_id TEXT, resource_name TEXT, plant_id TEXT, month TEXT,
  capacity_min REAL, load_min REAL, overload_min REAL, utilization_pct REAL)
planning_resource_product_results(run_id TEXT, resource_id TEXT, resource_name TEXT, product_id TEXT, month TEXT,
  allocated_qty REAL, allocated_min REAL)

-- Other
inventory(item_id TEXT, item_type TEXT, location_id TEXT, quantity_on_hand INT, last_updated TEXT)
inbound_deliveries(inbound_delivery_id TEXT PK, item_id TEXT, supplier_id TEXT, ordered_quantity INT,
  actual_delivery_date TEXT, planned_lead_time_days INT, deviation_vs_confirmed_days INT)
disruption_events(event_id TEXT PK, event_type TEXT, affected_supplier_id TEXT, start_date TEXT, end_date TEXT, severity TEXT, description TEXT)
production_quality_results(result_id TEXT PK, product_id TEXT, plant_id TEXT, week_start TEXT,
  total_units_produced INT, defect_units INT, defect_rate_pct REAL)
inventory_policies(inventory_policy_id TEXT PK, item_id TEXT, location_id TEXT,
  policy_type TEXT, safety_stock_method TEXT,
  reorder_point_qty REAL,  -- the safety stock / reorder point quantity
  reorder_qty REAL,        -- order quantity when triggered
  min_cover_days REAL,     -- minimum days of stock cover (safety stock expressed as days)
  max_cover_days REAL,     -- maximum days of stock cover target
  review_cycle_days REAL, valid_from TEXT, valid_to TEXT)
  -- item_id is either a product (FG001…) or component (COMP001…); location_id is a plant (PLT001…) or warehouse
  -- covers 144 rows: 10 FG products + ~47 components, each across 3 plants
knowledge_elements(knowledge_id TEXT PK, knowledge_category TEXT, observation_text TEXT, confidence TEXT, impact_level TEXT)
cost_parameters(parameter_id TEXT PK, parameter_name TEXT, parameter_value REAL, unit TEXT)

=== QUERY PATTERNS (use these as templates) ===

-- Product forecast accuracy (worst/best MAPE by product):
SELECT s.product_id, p.name, ROUND(AVG(s.ape)*100, 1) AS mape_pct
FROM statistical_forecast_history s JOIN products p ON p.product_id = s.product_id
GROUP BY s.product_id, p.name ORDER BY AVG(s.ape) DESC LIMIT 10;

-- Manual vs statistical accuracy by product:
SELECT s.product_id, p.name,
  ROUND(AVG(s.ape)*100,1) AS stat_mape_pct,
  ROUND(AVG(ABS(m.manual_forecast_qty - s.actual_qty)*1.0/NULLIF(s.actual_qty,0))*100,1) AS manual_mape_pct
FROM statistical_forecast_history s
JOIN manual_forecast_history m ON m.stat_forecast_id = s.stat_forecast_id
JOIN products p ON p.product_id = s.product_id
GROUP BY s.product_id, p.name;

-- Top selling products by revenue:
SELECT p.name, SUM(o.order_value_eur) AS total_revenue_eur
FROM customer_orders o JOIN products p ON p.product_id = o.product_id
GROUP BY p.product_id, p.name ORDER BY total_revenue_eur DESC LIMIT 10;

-- Safety stock settings (all products and components, one row per item, averaged across plants):
SELECT ip.item_id, COALESCE(p.name, m.name) AS name,
  CASE WHEN p.product_id IS NOT NULL THEN 'Finished Good' ELSE m.material_type END AS item_type,
  ROUND(AVG(ip.reorder_point_qty),0) AS safety_stock_qty,
  ROUND(AVG(ip.min_cover_days),0) AS min_cover_days,
  ROUND(AVG(ip.max_cover_days),0) AS max_cover_days,
  ip.policy_type, ip.safety_stock_method
FROM inventory_policies ip
LEFT JOIN products p ON p.product_id = ip.item_id
LEFT JOIN materials m ON m.material_id = ip.item_id
GROUP BY ip.item_id ORDER BY item_type, ip.item_id LIMIT 100;

-- Current backlog units + revenue at risk by product (latest planning run):
SELECT r.product_id, p.name, SUM(r.backlog_qty) AS total_backlog_units,
  ROUND(SUM(r.backlog_qty * p.unit_price), 2) AS backlog_revenue_eur
FROM planning_fg_constrained_results r
JOIN products p ON p.product_id = r.product_id
WHERE r.run_id = (SELECT run_id FROM planning_runs ORDER BY run_ts DESC LIMIT 1)
GROUP BY r.product_id, p.name ORDER BY backlog_revenue_eur DESC;
-- NOTE: "backlog" always means planning_fg_constrained_results.backlog_qty — never customer_orders.status

=== RULES ===
- Only emit SELECT statements — never UPDATE, DELETE, DROP, INSERT, CREATE, ALTER, ATTACH, or PRAGMA
- If the question cannot be answered with the schema, return exactly: ERROR: <brief reason>
- For forecast accuracy: ALWAYS query statistical_forecast_history (has pre-computed ape column); join to manual_forecast_history via stat_forecast_id
- For revenue: use customer_orders.order_value_eur — never use a column named "revenue"
- channel values are always uppercase: 'B2B' or 'B2C'
- product_id values are like 'FG001', 'FG002', etc. — always uppercase
- Always alias columns clearly; always add LIMIT 100 unless computing aggregates
"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": text}]
        )
        sql = msg.content[0].text.strip()
        if sql.startswith("```"):
            sql = "\n".join(sql.split("\n")[1:])
            sql = sql.rsplit("```", 1)[0].strip()
    except Exception as e:
        return jsonify({"ok": False, "message": f"Claude error: {e}"}), 500

    # ── safety gate ───────────────────────────────────────────────────────────
    import re as _re
    sql_upper = sql.upper().lstrip()
    if sql_upper.startswith("ERROR:"):
        return jsonify({"ok": False, "message": sql[6:].strip()}), 200

    FORBIDDEN = {"UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "INSERT",
                 "ATTACH", "DETACH", "PRAGMA", "VACUUM"}
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        return jsonify({"ok": False, "message": "Only SELECT queries are allowed in Analytics."}), 200

    for kw in FORBIDDEN:
        if kw in sql_upper.split():
            return jsonify({"ok": False, "message": f"Forbidden keyword: {kw}"}), 200

    # ── execute ───────────────────────────────────────────────────────────────
    conn = get_db()
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        raw_rows = cur.fetchall()
        rows = [dict(zip(columns, r)) for r in raw_rows]
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"SQL error: {e}", "sql": sql}), 200
    finally:
        conn.close()

    # ── NL interpretation ─────────────────────────────────────────────────────
    try:
        summary_rows = rows[:5]
        interpret_msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system="""You are a supply-chain analyst embedded in an S&OP tool. Given a question and SQL query results, write a clear, direct 1-4 sentence answer in plain English.
Rules:
- Be specific: mention actual numbers, product names, dates from the results
- If a column has numeric values, calculate totals/averages yourself if helpful (e.g. sum up rows to give a grand total)
- Express monetary values in € with thousands separators (e.g. €1,234,567)
- If the result is a single NULL/empty value, explain what that means in business terms and suggest what to look for instead — do NOT just say "data not available"
- No markdown, no bullet points, just concise prose""",
            messages=[{"role": "user", "content":
                f'Question: "{text}"\nRows returned: {len(rows)}\nFirst 5 rows: {json.dumps(summary_rows, default=str)}\nAnswer directly.'}]
        )
        interpretation = interpret_msg.content[0].text.strip()
    except Exception:
        interpretation = f"Query returned {len(rows)} row(s)."

    return jsonify({
        "ok": True,
        "sql": sql,
        "interpretation": interpretation,
        "columns": columns,
        "rows": rows[:100],
        "row_count": len(rows)
    })


@app.route("/api/knowledge")
def api_knowledge():
    conn = get_db()

    elements = rows_to_list(conn.execute("""
        SELECT ke.*,
               p.planner_name
        FROM knowledge_elements ke
        LEFT JOIN demand_planners p ON p.planner_id = ke.source_planner_id
        ORDER BY ke.knowledge_category, ke.knowledge_id
    """).fetchall())

    implications = rows_to_list(conn.execute("""
        SELECT * FROM knowledge_implications ORDER BY knowledge_id, implication_type
    """).fetchall())

    episodes = rows_to_list(conn.execute("""
        SELECT * FROM knowledge_episodes ORDER BY occurred_date
    """).fetchall())

    topics = rows_to_list(conn.execute("""
        SELECT * FROM knowledge_topics ORDER BY topic_code
    """).fetchall())

    # Group implications by knowledge_id
    imp_map = {}
    for i in implications:
        imp_map.setdefault(i["knowledge_id"], []).append(i)

    for e in elements:
        e["implications"] = imp_map.get(e["knowledge_id"], [])

    conn.close()
    return jsonify({"elements": elements, "episodes": episodes, "topics": topics})


# ── BOM SHORTAGE IMPACT ───────────────────────────────────────────────────────

@app.route("/api/bom/shortage_impact")
def api_bom_shortage_impact():
    """
    Bottom-up pegging: for every component with shortage_qty > 0, trace UP
    through the BOM to SA → FG and calculate impacted FG units + revenue at risk.

    Returns:
      summary  : aggregate KPIs
      impacts  : list of components, each with upstream_chain (SA→FG) and
                 per-month shortage details
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)

    # 1. Check table exists
    tbl_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='planning_material_availability'"
    ).fetchone()
    if not tbl_exists:
        conn.close()
        return jsonify({"summary": {}, "impacts": [], "run_id": run_id})

    # 2. Fetch component shortages (grouped by material_id for summary, keep months separate)
    shortage_rows = conn.execute("""
        SELECT pma.material_id, pma.month, pma.shortage_qty, pma.on_hand_qty,
               pma.gross_requirement_qty, pma.receipts_qty, pma.latest_order_date,
               m.name AS material_name, m.lead_time_days, m.unit_cost,
               m.material_type
        FROM planning_material_availability pma
        LEFT JOIN materials m ON m.material_id = pma.material_id
        WHERE pma.run_id = ? AND pma.shortage_qty > 0
        ORDER BY pma.material_id, pma.month
    """, (run_id,)).fetchall()

    if not shortage_rows:
        conn.close()
        return jsonify({"summary": {"components_short": 0, "fgs_impacted": 0,
                                     "total_revenue_at_risk": 0, "months_affected": 0},
                        "impacts": [], "run_id": run_id})

    # 3. Build reverse BOM: child_id → list of {parent_id, qty_per, bom_level}
    bom_rows = conn.execute(
        "SELECT parent_id, child_id, quantity, bom_level FROM bill_of_materials"
    ).fetchall()
    reverse_bom = defaultdict(list)          # child → [parent entries]
    forward_bom  = defaultdict(list)          # parent → [child entries]
    for r in bom_rows:
        reverse_bom[r["child_id"]].append({"parent_id": r["parent_id"],
                                            "qty_per": r["quantity"],
                                            "bom_level": r["bom_level"]})
        forward_bom[r["parent_id"]].append({"child_id": r["child_id"],
                                             "qty_per": r["quantity"],
                                             "bom_level": r["bom_level"]})

    # 4. Materials and products master
    materials = {r["material_id"]: dict(r) for r in conn.execute("SELECT * FROM materials").fetchall()}
    products  = {r["product_id"]:  dict(r) for r in conn.execute("SELECT * FROM products").fetchall()}

    # 5. SA names (materials with material_type = 'subassembly')
    sa_ids = {mid for mid, m in materials.items() if m["material_type"] == "subassembly"}
    fg_ids = set(products.keys())

    # 6. Helper: trace a component up to SAs, then up to FGs
    def trace_upstream(comp_id):
        """Return [{sa_id, qty_comp_per_sa, affected_fgs:[{fg_id, qty_sa_per_fg}]}]"""
        chains = []
        # Level 1: comp → SA
        for sa_entry in reverse_bom.get(comp_id, []):
            sa_id = sa_entry["parent_id"]
            if sa_id not in sa_ids:
                continue
            qty_comp_per_sa = sa_entry["qty_per"]
            affected_fgs = []
            # Level 2: SA → FG
            for fg_entry in reverse_bom.get(sa_id, []):
                fg_id = fg_entry["parent_id"]
                if fg_id not in fg_ids:
                    continue
                affected_fgs.append({
                    "product_id": fg_id,
                    "product_name": products[fg_id].get("name", fg_id),
                    "unit_price": products[fg_id].get("unit_price", 0),
                    "qty_sa_per_fg": fg_entry["qty_per"],
                })
            if affected_fgs:
                chains.append({
                    "sa_id": sa_id,
                    "sa_name": materials.get(sa_id, {}).get("name", sa_id),
                    "qty_comp_per_sa": qty_comp_per_sa,
                    "affected_fgs": affected_fgs,
                })
        return chains

    # 7. Group shortage rows by component
    from itertools import groupby
    comp_shortages = defaultdict(list)
    comp_meta = {}
    # Use the model's planning date (TODAY = "2025-03"), not system time.
    # This keeps urgency_days meaningful relative to when the plan was generated.
    planning_today = date.fromisoformat(TODAY + "-01")
    for r in shortage_rows:
        mid = r["material_id"]
        comp_shortages[mid].append(dict(r))
        comp_meta[mid] = r  # last row wins for meta (same across months)

    # 8. Build impact structure per component.
    #
    # Revenue de-duplication: the same FG unit loss must not be counted once per
    # blocking component. We track fg_month_blocked_max[(fg_id, month)] = the
    # largest single-component block across all components for that FG+month, then
    # sum that for the global total. Per-component revenue figures use that
    # component's own block size (useful for ranking) but the global summary uses
    # the de-duplicated max.
    impacts = []
    all_impacted_fgs = set()
    # fg_month_blocked_max: {(fg_id, month): max_units_blocked_by_any_one_component}
    fg_month_blocked_max = defaultdict(float)

    for comp_id, months in comp_shortages.items():
        meta = comp_meta[comp_id]
        chains = trace_upstream(comp_id)

        # Per month: compute FG units blocked by this component's shortage
        monthly_fg_impact = defaultdict(lambda: defaultdict(float))  # {month: {fg_id: units}}
        total_shortage = sum(r["shortage_qty"] for r in months)

        for month_row in months:
            shortage = month_row["shortage_qty"]
            ym = month_row["month"]
            for chain in chains:
                qty_comp_per_sa = chain["qty_comp_per_sa"] or 1
                constrained_sa = shortage / qty_comp_per_sa
                for fg in chain["affected_fgs"]:
                    qty_sa_per_fg = fg["qty_sa_per_fg"] or 1
                    fg_impact = constrained_sa / qty_sa_per_fg
                    monthly_fg_impact[ym][fg["product_id"]] += fg_impact
                    # Update global max: take largest single-component block
                    key = (fg["product_id"], ym)
                    fg_month_blocked_max[key] = max(fg_month_blocked_max[key], fg_impact)

        # Aggregate per FG across months (for this component)
        fg_totals = defaultdict(float)
        for month_impacts in monthly_fg_impact.values():
            for fg_id, units in month_impacts.items():
                fg_totals[fg_id] += units
                all_impacted_fgs.add(fg_id)

        # Build upstream_chain with revenue (per-component figures, useful for ranking)
        upstream = []
        for chain in chains:
            fgs_out = []
            for fg in chain["affected_fgs"]:
                fg_id = fg["product_id"]
                total_units = fg_totals.get(fg_id, 0)
                rev = total_units * fg["unit_price"]
                by_month = {m: round(monthly_fg_impact[m].get(fg_id, 0), 1)
                            for m in sorted(monthly_fg_impact)}
                fgs_out.append({
                    **fg,
                    "total_impacted_units": round(total_units, 1),
                    "revenue_at_risk": round(rev, 0),
                    "impacted_by_month": by_month,
                })
            upstream.append({**chain, "affected_fgs": fgs_out})

        comp_rev = round(sum(fg["revenue_at_risk"] for c in upstream for fg in c["affected_fgs"]), 0)

        # Urgency: days from planning date to earliest order-by date
        order_dates = [r["latest_order_date"] for r in months if r["latest_order_date"]]
        earliest_order = min(order_dates) if order_dates else None
        urgency_days = None
        if earliest_order:
            urgency_days = (date.fromisoformat(earliest_order[:10]) - planning_today).days

        impacts.append({
            "material_id": comp_id,
            "material_name": meta["material_name"] or comp_id,
            "lead_time_days": meta["lead_time_days"],
            "unit_cost": meta["unit_cost"],
            "material_type": meta["material_type"],
            "total_shortage_qty": round(total_shortage, 1),
            "earliest_order_date": earliest_order,
            "urgency_days": urgency_days,
            "shortages_by_month": [
                {"month": r["month"], "shortage_qty": round(r["shortage_qty"], 1),
                 "on_hand_qty": round(r["on_hand_qty"], 1),
                 "gross_requirement_qty": round(r["gross_requirement_qty"], 1),
                 "latest_order_date": r["latest_order_date"]}
                for r in months
            ],
            "upstream_chain": upstream,
            "total_revenue_at_risk": comp_rev,
        })

    # Sort by revenue at risk descending
    impacts.sort(key=lambda x: x["total_revenue_at_risk"], reverse=True)

    # Global revenue: sum max-blocked units per (FG, month) × unit_price.
    # This avoids counting a FG unit loss once per blocking component.
    total_revenue_at_risk = sum(
        units * products[fg_id]["unit_price"]
        for (fg_id, ym), units in fg_month_blocked_max.items()
        if fg_id in products
    )

    conn.close()
    return jsonify({
        "run_id": run_id,
        "summary": {
            "components_short": len(impacts),
            "fgs_impacted": len(all_impacted_fgs),
            "total_revenue_at_risk": round(total_revenue_at_risk, 0),
            "months_affected": len({r["month"] for rows in comp_shortages.values() for r in rows}),
        },
        "impacts": impacts,
    })


# ── PLANNING ENGINE ───────────────────────────────────────────────────────────

@app.route("/api/plan/run", methods=["POST"])
def api_plan_run():
    """
    Trigger a full multi-level constrained planning run.

    Optional JSON body:
      {
        "run_id": "my_run_2025",          // default: auto-generated
        "demand_source": "statistical",   // "statistical" | "manual" | "consensus"
        "horizon_months": ["2025-03",...] // default: HORIZON_MONTHS
      }
    """
    body = request.get_json(silent=True) or {}
    run_id = body.get("run_id") or ("run_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S"))
    demand_source = body.get("demand_source", "statistical")
    horizon = HORIZON_MONTHS  # always use server-side list; ignore client integer

    conn = get_db()
    try:
        # Build fg_build_input from the requested demand source
        if demand_source == "manual":
            # Use most-recent manual forecast per product+channel+month, fall back to statistical
            stat = compute_statistical_forecast()
            manual_rows = conn.execute("""
                SELECT product_id, channel,
                       substr(forecast_month, 1, 7) AS month,
                       manual_forecast_qty
                FROM manual_forecast_history
                WHERE substr(forecast_month, 1, 7) IN ({})
                ORDER BY created_at DESC
            """.format(",".join(["?" for _ in horizon])), horizon).fetchall()
            manual_map = {}
            for r in manual_rows:
                key = (r["product_id"], r["channel"], r["month"])
                if key not in manual_map:  # keep most recent (ORDER BY created_at DESC)
                    manual_map[key] = r["manual_forecast_qty"]

            fg_build_input = {}
            for (pid, channel), monthly in stat.items():
                for ym, qty in monthly.items():
                    manual_qty = manual_map.get((pid, channel, ym))
                    fg_build_input.setdefault(pid, {})[ym] = fg_build_input.get(pid, {}).get(ym, 0) + (manual_qty if manual_qty is not None else qty)
        else:
            # Statistical forecast — sum across channels
            stat = compute_statistical_forecast()
            fg_build_input = {}
            for (pid, channel), monthly in stat.items():
                for ym, qty in monthly.items():
                    fg_build_input.setdefault(pid, {})[ym] = fg_build_input.get(pid, {}).get(ym, 0) + qty

        results = planning_engine.run_plan(
            conn=conn,
            fg_build_input=fg_build_input,
            horizon_months=horizon,
            today_str=TODAY + "-01",
            pull_forward_window_fg=6,
            pull_forward_window_sa=3,
        )

        planning_engine.persist_results(conn, run_id, results)
        conn.commit()

        # Summarise planned orders for run response
        po_list = results.get("planned_orders", [])
        past_due_count = sum(1 for p in po_list if p.get("is_past_due"))
        return jsonify({
            "run_id": run_id,
            "status": "ok",
            "summary": results["summary"],
            "rows_written": {
                "fg_results": len(results["fg_results"]),
                "item_results": len(results["item_results"]),
                "resource_results": len(results["resource_results"]),
                "resource_product": len(results["resource_product"]),
                "material_avail": len(results["material_avail"]),
                "planned_orders": len(po_list),
            },
            "mrp_planned_orders_summary": {
                "total_pos": len(po_list),
                "past_due_pos": past_due_count,
                "on_time_pos": len(po_list) - past_due_count,
                "total_order_qty": round(sum(p["order_qty"] for p in po_list)),
            },
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/material/availability")
def api_material_availability():
    """
    Component-level availability and shortage data from the latest planning run.
    Returns rows with shortage > 0 (or all if ?all=1).
    """
    conn = get_db()
    run_id = request.args.get("run_id", "baseline_20260218_071626")
    show_all = request.args.get("all", "0") == "1"

    # Check if planning_material_availability table exists
    tbl_exists = conn.execute("""
        SELECT name FROM sqlite_master WHERE type='table' AND name='planning_material_availability'
    """).fetchone()

    if not tbl_exists:
        conn.close()
        return jsonify([])

    shortage_filter = "" if show_all else "AND pma.shortage_qty > 0"
    rows = conn.execute(f"""
        SELECT pma.*,
               m.name AS material_name,
               m.lead_time_days,
               m.unit_cost,
               m.material_type
        FROM planning_material_availability pma
        LEFT JOIN materials m ON m.material_id = pma.material_id
        WHERE pma.run_id = ? {shortage_filter}
        ORDER BY pma.shortage_qty DESC, pma.month
    """, (run_id,)).fetchall()

    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/material/planned_orders")
def api_material_planned_orders():
    """
    MRP-generated planned purchase orders from the latest planning run.
    Groups by material and provides full order schedule with supplier info.

    Query params:
      run_id   : specific run (defaults to latest)
      material : filter by material_id
      past_due : '1' to show only past-due orders
    """
    conn = get_db()
    run_id = request.args.get("run_id") or _latest_run_id(conn)
    mat_filter = request.args.get("material", "")
    past_due_only = request.args.get("past_due", "0") == "1"

    tbl_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='planning_planned_orders'"
    ).fetchone()
    if not tbl_exists:
        conn.close()
        return jsonify({"orders": [], "summary": {}, "run_id": run_id})

    where_clauses = ["ppo.run_id = ?"]
    params = [run_id]
    if mat_filter:
        where_clauses.append("ppo.material_id = ?")
        params.append(mat_filter)
    if past_due_only:
        where_clauses.append("ppo.is_past_due = 1")

    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(f"""
        SELECT ppo.*,
               m.name AS material_name,
               m.unit_cost,
               m.material_type,
               s.supplier_name
        FROM planning_planned_orders ppo
        LEFT JOIN materials m ON m.material_id = ppo.material_id
        LEFT JOIN suppliers s ON s.supplier_id = ppo.supplier_id
        WHERE {where_sql}
        ORDER BY ppo.is_past_due DESC, ppo.order_date, ppo.material_id
    """, params).fetchall()

    orders = rows_to_list(rows)

    # Summary stats
    past_due = [o for o in orders if o.get("is_past_due")]
    total_value = sum(
        (o.get("order_qty") or 0) * (o.get("unit_cost") or 0) for o in orders
    )
    conn.close()

    return jsonify({
        "run_id": run_id,
        "orders": orders,
        "summary": {
            "total_orders": len(orders),
            "past_due_orders": len(past_due),
            "on_time_orders": len(orders) - len(past_due),
            "total_order_qty": round(sum(o.get("order_qty", 0) for o in orders)),
            "total_order_value_eur": round(total_value),
            "unique_materials": len({o["material_id"] for o in orders}),
            "unique_suppliers": len({o.get("supplier_id") for o in orders if o.get("supplier_id")}),
        }
    })


@app.route("/api/plan/runs")
def api_plan_runs():
    """List available plan run_ids (from planning_fg_constrained_results)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT run_id, COUNT(*) as row_count, MIN(month) as from_month, MAX(month) as to_month
        FROM planning_fg_constrained_results
        GROUP BY run_id
        ORDER BY run_id DESC
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/config")
def api_config():
    """Return server-side capability flags (no secrets exposed)."""
    return jsonify({
        "has_ai": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


# ── Pre-built analytics queries (work without AI) ────────────────────────────

_BUILTIN_QUERIES = {
    "highest_forecast_adjustment": {
        "title": "Products with Highest Forecast Adjustment",
        "interpretation": "Products ranked by average absolute % adjustment made by planners vs. statistical forecast.",
        "sql": """
SELECT m.product_id, p.name,
  ROUND(AVG(ABS(m.adjustment_pct)), 1)                        AS avg_abs_adjustment_pct,
  COUNT(*)                                                     AS adjustments,
  ROUND(SUM(m.manual_forecast_qty - m.statistical_forecast_qty)) AS total_qty_adjusted
FROM manual_forecast_history m
JOIN products p ON p.product_id = m.product_id
WHERE m.adjustment_pct IS NOT NULL
GROUP BY m.product_id, p.name
ORDER BY avg_abs_adjustment_pct DESC
LIMIT 10
""",
    },
    "high_utilization_resources": {
        "title": "Resources with Utilisation Above 90 %",
        "interpretation": "Capacity resources from the latest planning run with utilisation > 90%, ordered by highest utilisation first.",
        "sql": """
SELECT r.resource_id, r.resource_name, r.plant_id, r.month,
  ROUND(r.utilization_pct, 1)    AS utilization_pct,
  ROUND(r.overload_min / 60.0, 1) AS overload_hours
FROM planning_resource_month_results r
WHERE r.utilization_pct > 90
  AND r.run_id = (SELECT run_id FROM planning_runs ORDER BY run_ts DESC LIMIT 1)
ORDER BY r.utilization_pct DESC
LIMIT 50
""",
    },
    "b2b_demand_2024": {
        "title": "Total B2B Demand per Product in 2024",
        "interpretation": "Aggregated weekly B2B demand history for calendar year 2024, converted to estimated revenue.",
        "sql": """
SELECT p.product_id, p.name,
  SUM(d.quantity)                          AS total_b2b_qty,
  ROUND(SUM(d.quantity) * p.unit_price)    AS revenue_eur
FROM demand_history d
JOIN products p ON p.product_id = d.product_id
WHERE d.channel = 'B2B' AND d.week_start LIKE '2024-%'
GROUP BY p.product_id, p.name
ORDER BY total_b2b_qty DESC
LIMIT 20
""",
    },
    "top_customers": {
        "title": "Top 5 Customers by Order Volume",
        "interpretation": "Top 5 customers ranked by total revenue from customer_orders.",
        "sql": """
SELECT c.customer_id, c.name, c.channel, c.country,
  COUNT(o.order_id)              AS order_count,
  SUM(o.quantity)                AS total_qty,
  ROUND(SUM(o.order_value_eur))  AS total_revenue_eur
FROM customer_orders o
JOIN customers c ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.name, c.channel, c.country
ORDER BY total_revenue_eur DESC
LIMIT 5
""",
    },
    "component_shortages": {
        "title": "Component Shortage Status (Latest Run)",
        "interpretation": "Materials where MRP has identified a net shortage in the latest planning run.",
        "sql": """
SELECT a.material_id, mat.name, a.month,
  ROUND(a.gross_demand_qty)       AS gross_demand,
  ROUND(a.inventory_available)    AS on_hand,
  ROUND(a.shortage_qty)           AS shortage_qty,
  a.planned_po_order_date         AS po_order_date
FROM planning_material_availability a
JOIN materials mat ON mat.material_id = a.material_id
WHERE a.shortage_qty > 0
  AND a.run_id = (SELECT run_id FROM planning_runs ORDER BY run_ts DESC LIMIT 1)
ORDER BY a.shortage_qty DESC
LIMIT 50
""",
    },
    "supplier_reliability": {
        "title": "Supplier Reliability Overview",
        "interpretation": "Suppliers ranked by reliability score with on-time delivery metrics.",
        "sql": """
SELECT s.supplier_id, s.name, s.country, s.region,
  ROUND(s.reliability_score, 2)                       AS reliability_score,
  ROUND(r.late_rate * 100, 1)                         AS late_rate_pct,
  ROUND(r.avg_deviation_vs_confirmed_days, 1)         AS avg_delay_days,
  r.supplier_segment
FROM suppliers s
LEFT JOIN supplier_inbound_reliability r ON r.supplier_id = s.supplier_id
ORDER BY s.reliability_score DESC
LIMIT 20
""",
    },
}


@app.route("/api/analytics/builtin/<query_key>")
def api_analytics_builtin(query_key):
    """Execute a pre-built analytics query — works without ANTHROPIC_API_KEY."""
    entry = _BUILTIN_QUERIES.get(query_key)
    if not entry:
        return jsonify({"ok": False, "message": f"Unknown query key: {query_key}"}), 404

    try:
        conn = get_db()
        rows_raw = conn.execute(entry["sql"]).fetchall()
        conn.close()
        columns = list(rows_raw[0].keys()) if rows_raw else []
        rows = [list(r) for r in rows_raw]
        return jsonify({
            "ok": True,
            "title": entry["title"],
            "interpretation": entry["interpretation"],
            "sql": entry["sql"].strip(),
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ── E2E RISK ANALYSIS ─────────────────────────────────────────────────────────

@app.route("/api/risk/component_exposure", methods=["POST"])
def api_risk_component_exposure():
    """
    Accept Excel upload (customer | product_id | month | quantity | revenue?),
    explode BOM recursively, compute per-component concentration risk:
    single-customer, single-product, single-supplier.
    """
    try:
        import openpyxl
    except ImportError:
        return jsonify({"ok": False, "message": "openpyxl not installed — run: pip install openpyxl"}), 500

    if "file" not in request.files:
        return jsonify({"ok": False, "message": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "message": "Empty filename"}), 400

    # ── parse Excel ───────────────────────────────────────────────────────────
    try:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = iter(ws.values)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Cannot read Excel file: {e}"}), 400

    raw_header = list(next(rows_iter, []))
    if not raw_header:
        return jsonify({"ok": False, "message": "Excel file appears empty or has no header row"}), 400
    header = [str(c).strip().lower() if c is not None else "" for c in raw_header]

    def find_col(candidates):
        for c in candidates:
            for i, h in enumerate(header):
                if c in h:
                    return i
        return None

    col_customer = find_col(["customer", "cust", "client", "cliente", "buyer"])
    col_product  = find_col(["product_id", "product", "item_id", "item", "sku", "prodott", "fg", "article"])
    col_month    = find_col(["month", "mese", "period", "date", "data", "anno", "year_month"])
    col_qty      = find_col(["qty", "quant", "volume", "unit", "pieces", "pcs"])
    col_revenue  = find_col(["revenue", "ricav", "sales", "amount", "value", "fattur", "eur", "price"])

    missing = [name for name, col in [("customer", col_customer), ("product_id", col_product),
                                       ("month", col_month), ("quantity", col_qty)] if col is None]
    if missing:
        return jsonify({
            "ok": False,
            "message": f"Could not detect columns: {', '.join(missing)}. "
                       f"Headers found: {', '.join(header or ['(none)'])}. "
                       f"Expected: customer, product_id, month (YYYY-MM), quantity, revenue (optional)."
        }), 400

    sales_rows = []
    parse_errors = 0
    for row in rows_iter:
        try:
            def _cell(idx):
                return row[idx] if idx is not None and idx < len(row) else None
            customer   = str(_cell(col_customer)).strip() if _cell(col_customer) is not None else None
            product_id = str(_cell(col_product)).strip()  if _cell(col_product)  is not None else None
            month_raw  = _cell(col_month)
            qty_raw    = _cell(col_qty)
            rev_raw    = _cell(col_revenue) if col_revenue is not None else None

            if customer is None or product_id is None or month_raw is None:
                continue
            # Normalise month to YYYY-MM
            if hasattr(month_raw, "strftime"):
                month = month_raw.strftime("%Y-%m")
            else:
                month = str(month_raw).strip()[:7]  # take first 7 chars of "2024-01-01" etc.

            qty     = float(qty_raw)    if qty_raw    is not None else 0.0
            revenue = float(rev_raw)    if rev_raw    is not None else 0.0

            if customer and product_id and month:
                sales_rows.append((customer, product_id, month, qty, revenue))
        except (TypeError, ValueError, IndexError):
            parse_errors += 1

    if not sales_rows:
        return jsonify({"ok": False, "message": "No valid data rows found in file"}), 400

    # ── load master data from DB ──────────────────────────────────────────────
    conn = get_db()

    bom_rows = conn.execute("""
        SELECT b.parent_id, b.child_id, b.quantity,
               COALESCE(m.make_buy, 'make') as make_buy,
               COALESCE(p.name, m.name) as name,
               CASE WHEN p.product_id IS NOT NULL THEN 'fg'
                    ELSE COALESCE(m.material_type, 'component') END as item_type,
               COALESCE(m.lead_time_days, p.lead_time_days) as lead_time_days,
               COALESCE(m.unit_cost, p.unit_price, 0) as unit_cost
        FROM bill_of_materials b
        LEFT JOIN products p ON p.product_id = b.child_id
        LEFT JOIN materials m ON m.material_id = b.child_id
    """).fetchall()

    bom_dict = defaultdict(list)
    for r in bom_rows:
        bom_dict[r["parent_id"]].append({
            "child_id":        r["child_id"],
            "qty":             r["quantity"],
            "make_buy":        r["make_buy"],
            "name":            r["name"],
            "item_type":       r["item_type"],
            "lead_time_days":  r["lead_time_days"],
            "unit_cost":       r["unit_cost"],
        })

    sup_count_rows = conn.execute("""
        SELECT material_id, COUNT(DISTINCT supplier_id) as n
        FROM material_supplier_mapping GROUP BY material_id
    """).fetchall()
    supplier_count = {r["material_id"]: r["n"] for r in sup_count_rows}

    sup_name_rows = conn.execute("""
        SELECT msm.material_id, s.name as supplier_name
        FROM material_supplier_mapping msm
        JOIN suppliers s ON s.supplier_id = msm.supplier_id
        ORDER BY msm.material_id, msm.supplier_priority
    """).fetchall()
    supplier_names = defaultdict(list)
    for r in sup_name_rows:
        supplier_names[r["material_id"]].append(r["supplier_name"])

    inv_rows = conn.execute("""
        SELECT item_id, SUM(quantity_on_hand) as qty FROM inventory GROUP BY item_id
    """).fetchall()
    inventory = {r["item_id"]: r["qty"] for r in inv_rows}

    conn.close()

    # ── recursive BOM explosion ───────────────────────────────────────────────
    def explode_bom(item_id, multiplier=1.0, visited=None):
        if visited is None:
            visited = set()
        if item_id in visited:
            return {}
        visited = visited | {item_id}
        result = {}
        for child in bom_dict.get(item_id, []):
            cid      = child["child_id"]
            eff_qty  = child["qty"] * multiplier
            if cid in result:
                result[cid]["effective_qty"] += eff_qty
            else:
                result[cid] = {**child, "effective_qty": eff_qty}
            # Merge descendants
            for dcid, ddata in explode_bom(cid, eff_qty, visited).items():
                if dcid in result:
                    result[dcid]["effective_qty"] += ddata["effective_qty"]
                else:
                    result[dcid] = ddata
        return result

    bom_cache = {}
    for fg_id in {row[1] for row in sales_rows}:
        bom_cache[fg_id] = explode_bom(fg_id)

    # ── aggregate per component ───────────────────────────────────────────────
    comp_customers   = defaultdict(set)
    comp_fg_products = defaultdict(set)
    comp_revenue     = defaultdict(float)
    comp_qty         = defaultdict(float)
    comp_meta        = {}

    for customer, product_id, month, qty, revenue in sales_rows:
        for comp_id, comp_data in bom_cache.get(product_id, {}).items():
            comp_customers[comp_id].add(customer)
            comp_fg_products[comp_id].add(product_id)
            comp_revenue[comp_id]   += revenue
            comp_qty[comp_id]       += qty * comp_data["effective_qty"]
            if comp_id not in comp_meta:
                comp_meta[comp_id] = {k: comp_data[k] for k in
                                      ("name", "make_buy", "item_type", "lead_time_days", "unit_cost")}

    if not comp_customers:
        unique_products = sorted({row[1] for row in sales_rows})
        return jsonify({
            "ok": False,
            "message": f"No BOM matches found for products: {', '.join(unique_products)}. "
                       f"Verify product_id values match DB codes (e.g. FG001–FG010)."
        }), 400

    # ── build risk table ──────────────────────────────────────────────────────
    results = []
    for comp_id in comp_customers:
        meta   = comp_meta[comp_id]
        n_cust = len(comp_customers[comp_id])
        n_prod = len(comp_fg_products[comp_id])
        n_supp = supplier_count.get(comp_id, 0)

        flag_single_customer = n_cust == 1
        flag_single_product  = n_prod == 1
        flag_single_supplier = (meta["make_buy"] == "buy") and (n_supp <= 1)
        risk_score = int(flag_single_customer) + int(flag_single_product) + int(flag_single_supplier)

        results.append({
            "component_id":         comp_id,
            "name":                 meta["name"] or comp_id,
            "item_type":            meta["item_type"],
            "make_buy":             meta["make_buy"],
            "lead_time_days":       meta["lead_time_days"],
            "unit_cost":            meta["unit_cost"],
            "inventory_on_hand":    inventory.get(comp_id, 0),
            "n_customers":          n_cust,
            "customers":            sorted(comp_customers[comp_id]),
            "n_fg_products":        n_prod,
            "fg_products":          sorted(comp_fg_products[comp_id]),
            "n_suppliers":          n_supp,
            "suppliers":            supplier_names.get(comp_id, []),
            "revenue_at_risk":      round(comp_revenue[comp_id], 0),
            "total_qty_consumed":   round(comp_qty[comp_id], 1),
            "flag_single_customer": flag_single_customer,
            "flag_single_product":  flag_single_product,
            "flag_single_supplier": flag_single_supplier,
            "risk_score":           risk_score,
        })

    results.sort(key=lambda x: (-x["risk_score"], -x["revenue_at_risk"]))

    # ── KPI summary ───────────────────────────────────────────────────────────
    months_in_file = sorted({row[2] for row in sales_rows})
    kpis = {
        "total_components_analysed": len(results),
        "high_risk_components":      sum(1 for r in results if r["risk_score"] == 3),
        "medium_risk_components":    sum(1 for r in results if r["risk_score"] == 2),
        "low_risk_components":       sum(1 for r in results if r["risk_score"] == 1),
        "safe_components":           sum(1 for r in results if r["risk_score"] == 0),
        "revenue_at_risk_eur":       round(sum(r["revenue_at_risk"] for r in results if r["risk_score"] >= 2), 0),
        "unique_customers":          len({row[0] for row in sales_rows}),
        "unique_fg_products":        len({row[1] for row in sales_rows}),
        "period_from":               months_in_file[0]  if months_in_file else "",
        "period_to":                 months_in_file[-1] if months_in_file else "",
        "rows_parsed":               len(sales_rows),
        "parse_errors":              parse_errors,
    }

    return jsonify({"ok": True, "kpis": kpis, "components": results})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
