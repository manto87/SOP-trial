"""
ElectroTech S&OP — Multi-Level Constrained Planning Engine
===========================================================

Algorithm overview
------------------
Pass 0  : Load master data (BOM, resources, inventory, policies, receipts)
Pass 1  : FG MRP + capacity constraint  (existing logic reproduced here)
Pass 2  : SA MRP + capacity constraint  (NEW)
          ↳ SA backlog cascades back to reduce FG constrained build
Pass 3  : Component availability check  (NEW)
          ↳ Component shortage cascades back to reduce SA constrained build → FG
Pass 4  : Convergence loop (repeat passes 2-3 until stable, max 5 iterations)

Bucket granularity
------------------
FG demand / FG constrained build : monthly  (S&OP rhythm)
SA planning / SA capacity         : weekly   (SA lead times 2-5 days)
Component availability            : weekly   (component lead times 7-35 days)

All monetary values in EUR.
"""

import math
import logging
from datetime import datetime, date, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

# ── Calendar helpers ──────────────────────────────────────────────────────────

def weeks_in_horizon(horizon_months):
    """Return list of ISO week start dates (Monday) covering the horizon."""
    first = _month_start(horizon_months[0])
    # go back to Monday
    first -= timedelta(days=first.weekday())
    last = _month_end(horizon_months[-1])
    weeks = []
    d = first
    while d <= last:
        weeks.append(d.isoformat())
        d += timedelta(days=7)
    return weeks

def _month_start(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y, m, 1)

def _month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    if m == 12:
        return date(y + 1, 1, 1) - timedelta(days=1)
    return date(y, m + 1, 1) - timedelta(days=1)

def week_of(iso_date_str):
    """Return the Monday (week_start) for a given date string."""
    d = date.fromisoformat(iso_date_str[:10])
    return (d - timedelta(days=d.weekday())).isoformat()

def month_of(iso_date_str):
    return iso_date_str[:7]

def days_in_month(ym):
    return (_month_end(ym) - _month_start(ym)).days + 1

def weeks_for_month(ym, all_weeks):
    """All week_start strings whose Monday falls within month ym."""
    ms = _month_start(ym)
    me = _month_end(ym)
    return [w for w in all_weeks if ms <= date.fromisoformat(w) <= me]

def weekly_capacity_for_month(weekly_cap_hours, ym, all_weeks, holiday_dates=None):
    """Convert hours/week capacity to minutes for a given month, deducting bank holidays.

    holiday_dates : optional set/list of datetime.date objects for public holidays
                    that apply to this resource's plant calendar.  Any holiday that
                    falls Mon-Fri inside the month is treated as a lost working day.
    """
    ms = _month_start(ym)
    me = _month_end(ym)
    # Monday before or on ms
    w_start = ms - timedelta(days=ms.weekday())
    total_minutes = 0.0
    w = w_start
    while w <= me:
        w_end = w + timedelta(days=6)
        overlap_start = max(w, ms)
        overlap_end = min(w_end, me)
        fraction = (overlap_end - overlap_start).days / 7.0
        total_minutes += weekly_cap_hours * 60 * fraction
        w += timedelta(days=7)

    # Deduct mandatory bank holidays that fall on Mon-Fri (working days)
    if holiday_dates:
        daily_cap_min = (weekly_cap_hours * 60) / 5.0  # capacity per working day
        for hd in holiday_dates:
            if ms <= hd <= me and hd.weekday() < 5:   # 0=Mon … 4=Fri
                total_minutes -= daily_cap_min

    return max(0.0, total_minutes)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_plan(conn, fg_build_input, horizon_months, fg_service_level=0.95,
             pull_forward_window_fg=6, pull_forward_window_sa=3, today_str="2025-03-01"):
    """
    Run the full multi-level constrained plan.

    Parameters
    ----------
    conn            : sqlite3 connection (row_factory = sqlite3.Row)
    fg_build_input  : dict {product_id: {month: qty}}  — unconstrained FG gross demand
    horizon_months  : list of 'YYYY-MM' strings
    fg_service_level: safety stock coverage factor for FG (default 0.95)
    pull_forward_window_fg : months a FG production run can be pulled earlier
    pull_forward_window_sa : months an SA production run can be pulled earlier
    today_str       : ISO date of planning date

    Returns
    -------
    dict with keys:
      fg_results        : list of dicts (planning_fg_constrained_results rows)
      item_results      : list of dicts (planning_item_month_results rows, all levels)
      resource_results  : list of dicts (planning_resource_month_results, FG + SA resources)
      resource_product  : list of dicts (planning_resource_product_results)
      material_avail    : list of dicts (planning_material_availability rows)
      summary           : dict with high-level KPIs
    """
    today = date.fromisoformat(today_str)
    all_weeks = weeks_in_horizon(horizon_months)

    # ── Pass 0: Load master data ──────────────────────────────────────────────
    master = _load_master(conn, horizon_months)

    # ── Pass 1: FG capacity-constrained plan ─────────────────────────────────
    fg_constrained = _run_fg_pass(
        fg_build_input, master, horizon_months, all_weeks,
        fg_service_level, pull_forward_window_fg
    )

    # ── Iterative passes 2+3 ──────────────────────────────────────────────────
    # first_comp_result: the component availability picture based on the
    # FG+SA-capacity-constrained plan BEFORE any component feedback is applied.
    # This is the right basis for the Shortage Impact report — it shows which
    # components would be short if we tried to execute the capacity-constrained
    # FG/SA plan, and therefore which FGs will be delayed.
    # The subsequent iterations only refine the constrained build quantities.
    first_comp_result = None

    for iteration in range(5):
        prev_fg = {(r["product_id"], r["month"]): r["constrained_production_qty"]
                   for r in fg_constrained["fg_results"]}

        # Pass 2: SA capacity
        sa_result = _run_sa_pass(
            fg_constrained["fg_constrained_build"], master, horizon_months, all_weeks,
            pull_forward_window_sa
        )

        # Cascade SA shortfall back to FG
        fg_constrained = _apply_sa_feedback(
            fg_constrained, sa_result["sa_shortfall_by_fg"], master, horizon_months, all_weeks
        )

        # Pass 3: Component availability (against the SA-capacity-constrained build)
        comp_result = _run_component_pass(
            sa_result["sa_constrained_build"], master, horizon_months, all_weeks, today
        )

        # Save first-iteration component picture for the shortage impact report.
        # After feedback the SA build is reduced so shortages shrink — we want
        # to show what's short relative to the PLANNED (pre-feedback) schedule.
        if first_comp_result is None:
            first_comp_result = comp_result

        # Cascade component shortfall back to SA → FG
        if comp_result["any_shortage"]:
            sa_result = _apply_comp_feedback(sa_result, comp_result["sa_reduction"], master)
            fg_constrained = _apply_sa_feedback(
                fg_constrained, sa_result["sa_shortfall_by_fg"], master, horizon_months, all_weeks
            )

        # Check convergence
        new_fg = {(r["product_id"], r["month"]): r["constrained_production_qty"]
                  for r in fg_constrained["fg_results"]}
        if new_fg == prev_fg:
            logger.info(f"Planning converged after {iteration + 1} iteration(s)")
            break
    else:
        logger.warning("Planning did not fully converge after 5 iterations")

    if first_comp_result is None:
        first_comp_result = comp_result

    # ── Assemble output ───────────────────────────────────────────────────────
    return {
        "fg_results":       fg_constrained["fg_results"],
        "item_results":     fg_constrained["item_results"] + sa_result["item_results"] + first_comp_result["item_results"],
        "resource_results": fg_constrained["resource_results"] + sa_result["resource_results"],
        "resource_product": fg_constrained["resource_product"] + sa_result["resource_product"],
        # Use first-iteration shortages for the shortage impact report
        "material_avail":   first_comp_result["material_avail"],
        # Planned purchase orders generated by MRP (first iteration)
        "planned_orders":   first_comp_result.get("planned_orders", []),
        "summary":          _build_summary(fg_constrained, sa_result, first_comp_result, master),
    }


# ── Pass 0: Load master data ──────────────────────────────────────────────────

def _load_master(conn, horizon_months):
    m = {}

    # Products
    m["products"] = {r["product_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM products").fetchall()}

    # Materials
    m["materials"] = {r["material_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM materials").fetchall()}

    # BOM: {parent_id: [{child_id, quantity, bom_level}]}
    bom_raw = [dict(r) for r in conn.execute(
        "SELECT parent_id, child_id, quantity, bom_level FROM bill_of_materials").fetchall()]
    m["bom"] = defaultdict(list)
    for b in bom_raw:
        m["bom"][b["parent_id"]].append(b)

    # Resources: {resource_id: dict}
    m["resources"] = {r["resource_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM production_resources").fetchall()}

    # Resource requirements: {product_id: [{resource_id, time_per_unit_min}]}
    req_raw = [dict(r) for r in conn.execute(
        "SELECT product_id, resource_id, time_per_unit_min FROM product_resource_requirements").fetchall()]
    m["req"] = defaultdict(list)
    for r in req_raw:
        m["req"][r["product_id"]].append(r)

    # Inventory: {item_id: total_on_hand}  (sum across locations)
    inv_raw = [dict(r) for r in conn.execute(
        "SELECT item_id, SUM(quantity_on_hand) as qty FROM inventory GROUP BY item_id").fetchall()]
    m["inventory"] = {r["item_id"]: r["qty"] for r in inv_raw}

    # Inventory policies: {item_id: {min_cover_days, reorder_point_qty}}  (avg across locations)
    pol_raw = [dict(r) for r in conn.execute("""
        SELECT item_id, AVG(min_cover_days) as min_cover_days,
               AVG(reorder_point_qty) as reorder_point_qty
        FROM inventory_policies GROUP BY item_id""").fetchall()]
    m["policies"] = {r["item_id"]: r for r in pol_raw}

    # Expected receipts: {item_id: [(expected_date, quantity)]}
    rec_raw = [dict(r) for r in conn.execute(
        "SELECT item_id, expected_date, quantity FROM expected_receipts WHERE status != 'cancelled'").fetchall()]
    m["receipts"] = defaultdict(list)
    for r in rec_raw:
        m["receipts"][r["item_id"]].append((r["expected_date"], r["quantity"]))

    # Supplier mapping: {material_id: priority-1 supplier dict}
    # Columns: material_id, supplier_id, priority, unit_price, lead_time_days, moq
    sup_raw = [dict(r) for r in conn.execute("""
        SELECT material_id, supplier_id, supplier_priority, unit_price, lead_time_days, moq
        FROM material_supplier_mapping
        ORDER BY material_id, supplier_priority
    """).fetchall()]
    m["supplier_map"] = {}
    for row in sup_raw:
        mid = row["material_id"]
        # Keep the lowest-priority-number (i.e. supplier_priority=1 preferred) supplier
        if mid not in m["supplier_map"] or row["supplier_priority"] < m["supplier_map"][mid]["supplier_priority"]:
            m["supplier_map"][mid] = row

    # Supplier reliability: {supplier_id: {late_rate, avg_deviation_vs_confirmed_days, supplier_segment}}
    rel_raw = [dict(r) for r in conn.execute("""
        SELECT supplier_id, late_rate, avg_deviation_vs_confirmed_days, supplier_segment
        FROM supplier_inbound_reliability
    """).fetchall()]
    m["supplier_reliability"] = {r["supplier_id"]: r for r in rel_raw}

    # Monthly demand history (for weekly disaggregation weights)
    wk_raw = [dict(r) for r in conn.execute("""
        SELECT product_id, strftime('%Y-%m', week_start) as month,
               week_start, SUM(quantity) as qty
        FROM demand_history
        WHERE week_start >= '2024-03-01'
        GROUP BY product_id, month, week_start
        ORDER BY product_id, week_start""").fetchall()]
    # {product_id: {month: {week_start: qty}}}
    m["weekly_history"] = defaultdict(lambda: defaultdict(dict))
    for r in wk_raw:
        m["weekly_history"][r["product_id"]][r["month"]][r["week_start"]] = r["qty"]

    # Bank holidays per resource (via plant calendar assignment)
    hol_rows = conn.execute("""
        SELECT rca.resource_id, ch.holiday_date
        FROM resource_calendar_assignments rca
        JOIN calendar_holidays ch ON ch.calendar_id = rca.calendar_id
        WHERE ch.is_working_day_override = 0
    """).fetchall()
    holidays_by_resource = defaultdict(set)
    for row in hol_rows:
        holidays_by_resource[row["resource_id"]].add(date.fromisoformat(row["holiday_date"]))
    m["holidays_by_resource"] = dict(holidays_by_resource)

    # Horizon month capacities per resource (in minutes), holiday-adjusted
    m["res_cap_monthly"] = {}  # {resource_id: {month: minutes}}
    all_weeks = weeks_in_horizon(horizon_months)
    for res_id, res in m["resources"].items():
        wk_cap = res["weekly_capacity"]  # hours/week
        holidays = m["holidays_by_resource"].get(res_id) or None
        m["res_cap_monthly"][res_id] = {
            ym: weekly_capacity_for_month(wk_cap, ym, all_weeks, holidays)
            for ym in horizon_months
        }

    return m


# ── MRP Planned Order Generation ─────────────────────────────────────────────

def _generate_planned_orders(comp_gross, master, horizon_months, today):
    """
    MRP net-requirements pass: for each component × month where
    (gross_demand + safety_stock) > (on_hand + confirmed_receipts + prior_PO_receipts),
    generate a planned purchase order.

    Effective lead time = nominal_LT + avg_deviation (reliability buffer).
    Very-unreliable suppliers in Q4 (Oct-Dec) receive an extra 5-day buffer.

    Parameters
    ----------
    comp_gross   : {comp_id: {month: gross_qty}}
    master       : master data dict from _load_master
    horizon_months : list of 'YYYY-MM'
    today        : date object (planning date)

    Returns
    -------
    planned_receipts : {comp_id: {receipt_month: total_qty}}  — to add to supply
    planned_orders   : list of dicts for reporting / persistence
    """
    horizon_set = set(horizon_months)
    comp_ids = [mid for mid, mat in master["materials"].items()
                if mat["material_type"] in {"component", "packaging"}]

    planned_receipts = defaultdict(lambda: defaultdict(float))
    planned_orders = []

    for comp_id in comp_ids:
        mat = master["materials"].get(comp_id, {})

        # ── Supplier info ──────────────────────────────────────────────────────
        sup = master["supplier_map"].get(comp_id) or {}
        nominal_lt  = int(sup.get("lead_time_days") or mat.get("lead_time_days") or 14)
        moq         = max(1, int(sup.get("moq") or 1))
        supplier_id = sup.get("supplier_id") or ""

        rel = master["supplier_reliability"].get(supplier_id, {})
        avg_dev   = max(0.0, float(rel.get("avg_deviation_vs_confirmed_days") or 0))
        late_rate = float(rel.get("late_rate") or 0)
        segment   = rel.get("supplier_segment") or "standard"

        pol = master["policies"].get(comp_id, {})
        ss_days = float(pol.get("min_cover_days") or 14)

        # ── Confirmed PO receipts by month ─────────────────────────────────────
        confirmed_by_month = defaultdict(float)
        for exp_date, qty in master["receipts"].get(comp_id, []):
            ym = exp_date[:7]
            if ym in horizon_set:
                confirmed_by_month[ym] += qty

        inv = float(master["inventory"].get(comp_id, 0))

        for ym in horizon_months:
            gross     = comp_gross[comp_id].get(ym, 0.0)
            confirmed = confirmed_by_month.get(ym, 0.0)
            po_rcpt   = planned_receipts[comp_id].get(ym, 0.0)  # POs placed for prior months

            monthly_ss = (gross * ss_days / 30.0) if gross > 0 else 0.0
            available  = inv + confirmed + po_rcpt
            net_req    = gross + monthly_ss - available

            if net_req > 0 and gross > 0:
                # Round up to MOQ
                order_qty = math.ceil(net_req / moq) * moq

                # Effective lead time: nominal + reliability deviation buffer
                eff_lt = nominal_lt + int(math.ceil(avg_dev))

                # Extra Q4 buffer for very unreliable suppliers (e.g. Taiwan Q4 delays)
                ym_month = int(ym[5:7])
                if segment == "very_unreliable" and ym_month in (10, 11, 12) and late_rate > 0.6:
                    eff_lt += 5

                # Ideal order date to receive by start of need month
                ideal_order_date = _month_start(ym) - timedelta(days=eff_lt)
                actual_order_date = max(ideal_order_date, today)
                is_past_due = ideal_order_date < today

                receipt_date  = actual_order_date + timedelta(days=eff_lt)
                receipt_month = receipt_date.isoformat()[:7]

                planned_receipts[comp_id][receipt_month] += order_qty

                planned_orders.append({
                    "material_id":             comp_id,
                    "supplier_id":             supplier_id,
                    "need_month":              ym,
                    "order_date":              actual_order_date.isoformat(),
                    "receipt_date":            receipt_date.isoformat(),
                    "receipt_month":           receipt_month,
                    "order_qty":               round(order_qty),
                    "net_requirement_qty":     round(net_req, 1),
                    "moq":                     moq,
                    "effective_lead_time_days": eff_lt,
                    "supplier_segment":        segment,
                    "late_rate":               round(late_rate, 3),
                    "is_past_due":             1 if is_past_due else 0,
                })

            # ── Roll inventory forward ─────────────────────────────────────────
            # Include any PO that lands THIS month (including one just generated)
            total_rcpt = confirmed + planned_receipts[comp_id].get(ym, 0.0)
            total_avail = inv + total_rcpt
            usable = max(0.0, total_avail - monthly_ss)
            consumed = min(gross, usable)
            inv = max(0.0, total_avail - consumed)

    return dict(planned_receipts), planned_orders


# ── Pass 1: FG capacity-constrained plan ─────────────────────────────────────

def _run_fg_pass(fg_build_input, master, horizon_months, all_weeks, service_level, pull_window):
    """
    FG MRP → capacity constraint → pull forward → backlog.
    Returns dict with fg_constrained_build, fg_results, item_results,
    resource_results, resource_product.
    """
    # Safety stock: use inventory policy reorder_point_qty; fallback = 4 weeks demand
    def fg_ss(pid, avg_monthly_demand):
        pol = master["policies"].get(pid)
        if pol and pol["reorder_point_qty"]:
            return pol["reorder_point_qty"]
        return round(avg_monthly_demand * 0.5)

    # Opening inventory per FG
    fg_inv = {pid: master["inventory"].get(pid, 0) for pid in fg_build_input}

    # Net demand per FG × month
    planned = {}  # {pid: {month: net_demand}}
    item_rows = []
    for pid, month_demand in fg_build_input.items():
        inv = fg_inv.get(pid, 0)
        avg_d = sum(month_demand.values()) / max(len(month_demand), 1)
        ss = fg_ss(pid, avg_d)
        pid_rows = {}
        for ym in horizon_months:
            gross = month_demand.get(ym, 0)
            # Net = gross - opening inv + safety stock target
            net = max(0, gross - max(0, inv - ss))
            pid_rows[ym] = net
            item_rows.append({
                "item_id": pid, "item_type": "finished_good", "month": ym,
                "demand_signal_type": "forecast",
                "gross_demand_qty": gross, "net_demand_qty": net,
                "safety_stock_qty": ss, "receipts_qty": 0,
                "starting_inventory_qty": inv,
                "planned_replenishment_qty": net,
                "ending_inventory_qty": max(0, inv - gross + net),
                "constrained_fg_build_qty": net,  # updated later
            })
            # roll inventory forward
            inv = max(0, inv - gross + net)
        planned[pid] = pid_rows

    # Load resources month by month — FG resources only (not RES1xx)
    fg_resource_ids = [rid for rid in master["resources"] if not rid.startswith("RES1")]
    load = defaultdict(lambda: defaultdict(float))  # {res_id: {month: min}}
    prod_load = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # {res_id: {pid: {month: min}}}

    for pid, pid_months in planned.items():
        for req in master["req"].get(pid, []):
            rid = req["resource_id"]
            if rid not in fg_resource_ids:
                continue
            for ym, qty in pid_months.items():
                mins = qty * req["time_per_unit_min"]
                load[rid][ym] += mins
                prod_load[rid][pid][ym] += mins

    # Snapshot unconstrained demand load before the constraint loop modifies it in-place.
    # This is what gets reported as utilisation so the UI shows true demand pressure
    # (e.g. 300% when demand is 3× capacity) rather than always ≤100%.
    unconstrained_load = {rid: dict(load[rid]) for rid in load}

    # Capacity constraint + pull-forward
    constrained = {pid: dict(months) for pid, months in planned.items()}
    backlog = defaultdict(lambda: defaultdict(float))  # {pid: {month: backlog_qty}}

    # Pre-build time_per_unit lookup so we don't reference stale loop variables below
    tpu_lookup = {}  # {(pid, rid): time_per_unit_min}
    for pid in fg_build_input:
        for req_r in master["req"].get(pid, []):
            if req_r["resource_id"] in fg_resource_ids:
                tpu_lookup[(pid, req_r["resource_id"])] = req_r["time_per_unit_min"]

    for ym_idx, ym in enumerate(horizon_months):
        # Collect overloaded resources and their scale factors
        overloaded = {}  # rid → scale (cap / load)
        for rid in fg_resource_ids:
            cap = master["res_cap_monthly"].get(rid, {}).get(ym, 0)
            if cap > 0 and load[rid][ym] > cap:
                overloaded[rid] = cap / load[rid][ym]

        if not overloaded:
            continue

        # Per product: apply the single binding (most restrictive) scale factor once.
        # Iterating per resource and compounding scales would incorrectly collapse
        # quantities toward zero when many resources are overloaded simultaneously.
        for pid in list(constrained.keys()):
            # Minimum scale = most restrictive resource this product is loaded on
            min_scale = 1.0
            binding_rid = None
            for rid, scale in overloaded.items():
                if prod_load[rid][pid][ym] > 0 and scale < min_scale:
                    min_scale = scale
                    binding_rid = rid

            if binding_rid is None or min_scale >= 1.0:
                continue

            tpu = tpu_lookup.get((pid, binding_rid), 0)
            if tpu <= 0:
                continue

            original_qty = constrained[pid][ym]
            new_qty = original_qty * min_scale
            shortfall = original_qty - new_qty

            # Try pull forward using binding resource's available slack in earlier months
            pulled = 0.0
            for pf in range(1, pull_window + 1):
                pf_idx = ym_idx - pf
                if pf_idx < 0:
                    break
                pf_ym = horizon_months[pf_idx]
                pf_cap = master["res_cap_monthly"].get(binding_rid, {}).get(pf_ym, 0)
                pf_slack = pf_cap - load[binding_rid][pf_ym]
                if pf_slack <= 0:
                    continue
                pullable_qty = min(shortfall - pulled, pf_slack / tpu)
                if pullable_qty <= 0:
                    continue
                constrained[pid][pf_ym] = constrained[pid].get(pf_ym, 0) + pullable_qty
                load[binding_rid][pf_ym] += pullable_qty * tpu
                prod_load[binding_rid][pid][pf_ym] += pullable_qty * tpu
                pulled += pullable_qty

            actual_backlog = max(0, shortfall - pulled)
            constrained[pid][ym] = new_qty
            # Reduce load on ALL overloaded resources to reflect the reduced production
            for rid in overloaded:
                old_tpu = tpu_lookup.get((pid, rid), 0)
                if old_tpu > 0 and prod_load[rid][pid][ym] > 0:
                    reduction = (original_qty - new_qty) * old_tpu
                    load[rid][ym] = max(0, load[rid][ym] - reduction)
                    prod_load[rid][pid][ym] = max(0, prod_load[rid][pid][ym] - reduction)
            backlog[pid][ym] += actual_backlog

    # Build output rows
    fg_results = []
    for pid, pid_months in constrained.items():
        for ym in horizon_months:
            gross = fg_build_input[pid].get(ym, 0)
            constr = pid_months.get(ym, 0)
            blg = backlog[pid].get(ym, 0)
            fg_results.append({
                "product_id": pid,
                "month": ym,
                "demand_to_produce_qty": planned[pid].get(ym, 0),
                "constrained_production_qty": round(constr, 2),
                "backlog_qty": round(blg, 2),
                "prebuild_inventory_qty": max(0, round(constr - planned[pid].get(ym, 0), 2)),
            })

    # Update constrained_fg_build_qty in item_rows
    constr_map = {(r["product_id"], r["month"][:7]): r["constrained_production_qty"]
                  for r in fg_results}
    for row in item_rows:
        key = (row["item_id"], row["month"][:7])
        row["constrained_fg_build_qty"] = constr_map.get(key, row["constrained_fg_build_qty"])

    # Resource result rows (FG resources).
    # Use unconstrained_load for utilisation so the UI reflects the true demand
    # pressure before capacity scaling — e.g. 300 % when demand is 3× capacity.
    resource_results = []
    resource_product_rows = []
    for rid in fg_resource_ids:
        res = master["resources"][rid]
        for ym in horizon_months:
            cap = master["res_cap_monthly"].get(rid, {}).get(ym, 0)
            ld = unconstrained_load.get(rid, {}).get(ym, 0)
            overload = max(0, ld - cap)
            resource_results.append({
                "resource_id": rid,
                "resource_name": res["name"],
                "plant_id": res["plant_id"],
                "month": ym,
                "capacity_min": round(cap, 1),
                "load_min": round(ld, 1),
                "overload_min": round(overload, 1),
                "utilization_pct": round(ld / cap * 100, 1) if cap > 0 else 0,
            })
            for pid in fg_build_input:
                pl = prod_load[rid][pid][ym]
                if pl > 0:
                    qty = constrained[pid].get(ym, 0)
                    resource_product_rows.append({
                        "resource_id": rid, "resource_name": res["name"],
                        "product_id": pid, "month": ym,
                        "allocated_qty": round(qty, 2),
                        "allocated_min": round(pl, 1),
                    })

    return {
        "fg_constrained_build": constrained,  # {pid: {month: qty}}
        "fg_results": fg_results,
        "item_results": item_rows,
        "resource_results": resource_results,
        "resource_product": resource_product_rows,
        "backlog": backlog,
    }


# ── Pass 2: SA capacity-constrained plan ─────────────────────────────────────

def _run_sa_pass(fg_constrained_build, master, horizon_months, all_weeks, pull_window):
    """
    SA MRP: explode FG constrained build → SA demand → SA capacity constraint.
    Returns sa_constrained_build, resource_results, item_results, sa_shortfall_by_fg.
    """
    sa_resource_ids = [rid for rid in master["resources"] if rid.startswith("RES1")]

    # Collect all SA ids
    sa_ids = [mid for mid, mat in master["materials"].items()
              if mat["material_type"] == "subassembly"]

    # Gross SA demand from constrained FG build
    sa_gross = defaultdict(lambda: defaultdict(float))  # {sa_id: {month: qty}}
    # Track which FGs drive each SA (for feedback)
    sa_fg_contribution = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # {sa_id: {month: {fg_id: qty}}}

    for fg_id, fg_months in fg_constrained_build.items():
        for bom_child in master["bom"].get(fg_id, []):
            sa_id = bom_child["child_id"]
            if sa_id not in [m for m in sa_ids]:
                continue
            qty_per_fg = bom_child["quantity"]
            for ym, fg_qty in fg_months.items():
                sa_demand = fg_qty * qty_per_fg
                sa_gross[sa_id][ym] += sa_demand
                sa_fg_contribution[sa_id][ym][fg_id] += sa_demand

    # SA MRP netting
    sa_planned = {}  # {sa_id: {month: net_demand}}
    item_rows = []
    for sa_id in sa_ids:
        inv = master["inventory"].get(sa_id, 0)
        pol = master["policies"].get(sa_id, {})
        ss = pol.get("reorder_point_qty", 0) or 0
        pid_rows = {}
        for ym in horizon_months:
            gross = sa_gross[sa_id].get(ym, 0)
            net = max(0, gross - max(0, inv - ss))
            pid_rows[ym] = net
            item_rows.append({
                "item_id": sa_id, "item_type": "subassembly", "month": ym,
                "demand_signal_type": "derived",
                "gross_demand_qty": round(gross, 2), "net_demand_qty": round(net, 2),
                "safety_stock_qty": ss, "receipts_qty": 0,
                "starting_inventory_qty": round(inv, 2),
                "planned_replenishment_qty": round(net, 2),
                "ending_inventory_qty": round(max(0, inv - gross + net), 2),
                "constrained_fg_build_qty": 0,  # filled below
            })
            inv = max(0, inv - gross + net)
        sa_planned[sa_id] = pid_rows

    # Load SA resources
    load = defaultdict(lambda: defaultdict(float))
    prod_load = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))

    for sa_id, sa_months in sa_planned.items():
        for req in master["req"].get(sa_id, []):
            rid = req["resource_id"]
            if rid not in sa_resource_ids:
                continue
            for ym, qty in sa_months.items():
                mins = qty * req["time_per_unit_min"]
                load[rid][ym] += mins
                prod_load[rid][sa_id][ym] += mins

    # Snapshot unconstrained SA load before the constraint loop modifies it
    sa_unconstrained_load = {rid: dict(load[rid]) for rid in load}

    # Capacity constraint + pull-forward
    constrained = {sa_id: dict(months) for sa_id, months in sa_planned.items()}
    sa_backlog = defaultdict(lambda: defaultdict(float))

    # Pre-build time_per_unit lookup for SA resources
    sa_tpu_lookup = {}  # {(sa_id, rid): time_per_unit_min}
    for sa_id in sa_ids:
        for req_r in master["req"].get(sa_id, []):
            if req_r["resource_id"] in sa_resource_ids:
                sa_tpu_lookup[(sa_id, req_r["resource_id"])] = req_r["time_per_unit_min"]

    for ym_idx, ym in enumerate(horizon_months):
        # Find overloaded SA resources and their scale factors
        overloaded = {}  # rid → scale
        for rid in sa_resource_ids:
            cap = master["res_cap_monthly"].get(rid, {}).get(ym, 0)
            if cap > 0 and load[rid][ym] > cap:
                overloaded[rid] = cap / load[rid][ym]

        if not overloaded:
            continue

        # Per SA: apply binding (most restrictive) scale once
        for sa_id in sa_ids:
            min_scale = 1.0
            binding_rid = None
            for rid, scale in overloaded.items():
                if prod_load[rid][sa_id][ym] > 0 and scale < min_scale:
                    min_scale = scale
                    binding_rid = rid

            if binding_rid is None or min_scale >= 1.0:
                continue

            tpu = sa_tpu_lookup.get((sa_id, binding_rid), 0)
            if tpu <= 0:
                continue

            original_qty = constrained[sa_id][ym]
            new_qty = original_qty * min_scale
            shortfall = original_qty - new_qty

            # Pull forward using binding resource slack
            pulled_qty = 0.0
            for pf in range(1, pull_window + 1):
                pf_idx = ym_idx - pf
                if pf_idx < 0:
                    break
                pf_ym = horizon_months[pf_idx]
                pf_cap = master["res_cap_monthly"].get(binding_rid, {}).get(pf_ym, 0)
                pf_slack = (pf_cap - load[binding_rid][pf_ym]) / max(tpu, 0.001)
                pullable = min(shortfall - pulled_qty, pf_slack)
                if pullable <= 0:
                    continue
                constrained[sa_id][pf_ym] = constrained[sa_id].get(pf_ym, 0) + pullable
                load[binding_rid][pf_ym] += pullable * tpu
                prod_load[binding_rid][sa_id][pf_ym] += pullable * tpu
                pulled_qty += pullable

            actual_backlog = max(0, shortfall - pulled_qty)
            constrained[sa_id][ym] = new_qty
            # Reduce load on all overloaded resources
            for rid in overloaded:
                old_tpu = sa_tpu_lookup.get((sa_id, rid), 0)
                if old_tpu > 0 and prod_load[rid][sa_id][ym] > 0:
                    reduction = (original_qty - new_qty) * old_tpu
                    load[rid][ym] = max(0, load[rid][ym] - reduction)
                    prod_load[rid][sa_id][ym] = max(0, prod_load[rid][sa_id][ym] - reduction)
            sa_backlog[sa_id][ym] += actual_backlog

    # Update constrained build in item rows
    constr_map = {(sa_id, ym): constrained[sa_id].get(ym, 0)
                  for sa_id in sa_ids for ym in horizon_months}
    for row in item_rows:
        key = (row["item_id"], row["month"][:7])
        row["constrained_fg_build_qty"] = round(constr_map.get(key, 0), 2)

    # Compute SA shortfall by FG (for feedback to Pass 1)
    # {fg_id: {month: qty_reduction}}
    sa_shortfall_by_fg = defaultdict(lambda: defaultdict(float))
    for sa_id in sa_ids:
        for ym in horizon_months:
            blg = sa_backlog[sa_id].get(ym, 0)
            if blg <= 0:
                continue
            total_sa_demand = sa_gross[sa_id].get(ym, 0)
            if total_sa_demand <= 0:
                continue
            # Distribute backlog proportionally to FG contributors
            for fg_id, fg_sa_demand in sa_fg_contribution[sa_id][ym].items():
                share = fg_sa_demand / total_sa_demand
                # How many FG units does this SA backlog represent?
                bom_qty = next(
                    (b["quantity"] for b in master["bom"].get(fg_id, [])
                     if b["child_id"] == sa_id), 1.0)
                fg_reduction = (blg * share) / bom_qty
                sa_shortfall_by_fg[fg_id][ym] += fg_reduction

    # Resource result rows
    resource_results = []
    resource_product_rows = []
    for rid in sa_resource_ids:
        res = master["resources"][rid]
        for ym in horizon_months:
            cap = master["res_cap_monthly"].get(rid, {}).get(ym, 0)
            ld = sa_unconstrained_load.get(rid, {}).get(ym, 0)
            overload = max(0, ld - cap)
            resource_results.append({
                "resource_id": rid,
                "resource_name": res["name"],
                "plant_id": res["plant_id"],
                "month": ym,
                "capacity_min": round(cap, 1),
                "load_min": round(ld, 1),
                "overload_min": round(overload, 1),
                "utilization_pct": round(ld / cap * 100, 1) if cap > 0 else 0,
            })
            for sa_id in sa_ids:
                pl = prod_load[rid][sa_id][ym]
                if pl > 0:
                    resource_product_rows.append({
                        "resource_id": rid, "resource_name": res["name"],
                        "product_id": sa_id, "month": ym,
                        "allocated_qty": round(constrained[sa_id].get(ym, 0), 2),
                        "allocated_min": round(pl, 1),
                    })

    return {
        "sa_constrained_build": constrained,
        "sa_shortfall_by_fg": sa_shortfall_by_fg,
        "sa_backlog": sa_backlog,
        "item_results": item_rows,
        "resource_results": resource_results,
        "resource_product": resource_product_rows,
    }


def _apply_sa_feedback(fg_pass_result, sa_shortfall_by_fg, master, horizon_months, all_weeks):
    """Reduce FG constrained build by SA shortfall and update fg_results."""
    fg_cb = fg_pass_result["fg_constrained_build"]

    for fg_id, months in sa_shortfall_by_fg.items():
        if fg_id not in fg_cb:
            continue
        for ym, reduction in months.items():
            fg_cb[fg_id][ym] = max(0, fg_cb[fg_id].get(ym, 0) - reduction)

    # Rebuild fg_results with updated constrained build
    new_fg_results = []
    for r in fg_pass_result["fg_results"]:
        pid = r["product_id"]
        ym = r["month"][:7]
        new_constr = fg_cb.get(pid, {}).get(ym, r["constrained_production_qty"])
        orig_demand = r["demand_to_produce_qty"]
        new_backlog = max(0, orig_demand - new_constr)
        new_fg_results.append({**r,
            "constrained_production_qty": round(new_constr, 2),
            "backlog_qty": round(new_backlog, 2),
        })

    return {**fg_pass_result,
            "fg_constrained_build": fg_cb,
            "fg_results": new_fg_results}


# ── Pass 3: Component availability ───────────────────────────────────────────

def _run_component_pass(sa_constrained_build, master, horizon_months, all_weeks, today):
    """
    Explode SA constrained build → component gross demand.
    Generate MRP planned purchase orders to cover net requirements.
    Check remaining availability (after planned POs) against demand.
    Return shortage per component × month, and SA reduction needed.
    """
    # Collect component and packaging ids
    item_types = {"component", "packaging"}
    comp_ids = [mid for mid, mat in master["materials"].items()
                if mat["material_type"] in item_types]
    comp_ids_set = set(comp_ids)

    # Gross component demand from SA constrained build
    comp_gross = defaultdict(lambda: defaultdict(float))
    # Also track from SA level (SA → component)
    comp_sa_contribution = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))

    for sa_id, sa_months in sa_constrained_build.items():
        for bom_child in master["bom"].get(sa_id, []):
            comp_id = bom_child["child_id"]
            if comp_id not in comp_ids_set:
                continue
            qty_per_sa = bom_child["quantity"]
            for ym, sa_qty in sa_months.items():
                comp_demand = sa_qty * qty_per_sa
                comp_gross[comp_id][ym] += comp_demand
                comp_sa_contribution[comp_id][ym][sa_id] += comp_demand

    # ── MRP: generate planned purchase orders for net requirements ────────────
    planned_receipts_data, planned_orders = _generate_planned_orders(
        comp_gross, master, horizon_months, today
    )

    # Pass 3a: compute component availability per month (rolling inventory)
    # including confirmed receipts AND planned PO receipts
    # comp_scale[comp_id][ym] = fraction of demand that can be supplied (0..1)
    any_shortage = False
    material_avail = []
    item_results = []
    comp_scale = defaultdict(lambda: defaultdict(lambda: 1.0))  # {comp_id: {ym: scale}}

    # Index planned orders by (material_id, receipt_month) for per-row annotation
    po_by_mat_month = defaultdict(float)   # {(comp_id, ym): total planned order qty
    po_order_date_by_mat_month = {}        # {(comp_id, ym): earliest order date}
    for po in planned_orders:
        key = (po["material_id"], po["receipt_month"])
        po_by_mat_month[key] += po["order_qty"]
        existing = po_order_date_by_mat_month.get(key)
        if existing is None or po["order_date"] < existing:
            po_order_date_by_mat_month[key] = po["order_date"]

    for comp_id in comp_ids:
        mat = master["materials"].get(comp_id, {})
        lead_days = mat.get("lead_time_days", 14) or 14
        pol = master["policies"].get(comp_id, {})
        ss_days = pol.get("min_cover_days", 14) or 14

        on_hand = master["inventory"].get(comp_id, 0)

        confirmed_by_month = defaultdict(float)
        for exp_date, qty in master["receipts"].get(comp_id, []):
            ym = exp_date[:7]
            if ym in {m for m in horizon_months}:
                confirmed_by_month[ym] += qty

        planned_by_month = planned_receipts_data.get(comp_id, {})

        inv = float(on_hand)
        for ym in horizon_months:
            gross    = comp_gross[comp_id].get(ym, 0)
            receipts = confirmed_by_month.get(ym, 0) + planned_by_month.get(ym, 0)
            monthly_ss = (gross * ss_days / 30) if gross > 0 else 0

            available = inv + receipts
            usable = max(0.0, available - monthly_ss)  # what can actually be consumed
            shortage = max(0.0, gross - usable)
            net = max(0.0, gross - available + monthly_ss)

            if shortage > 0:
                any_shortage = True
                comp_scale[comp_id][ym] = usable / gross if gross > 0 else 0.0

            latest_order_date = (_month_start(ym) - timedelta(days=lead_days)).isoformat()

            po_key = (comp_id, ym)
            planned_po_qty        = po_by_mat_month.get(po_key, 0)
            planned_po_order_date = po_order_date_by_mat_month.get(po_key)

            material_avail.append({
                "material_id": comp_id,
                "month": ym,
                "gross_requirement_qty": round(gross, 1),
                "on_hand_qty": round(inv, 1),
                "receipts_qty": round(receipts, 1),
                "available_qty": round(available, 1),
                "safety_stock_qty": round(monthly_ss, 1),
                "shortage_qty": round(shortage, 1),
                "latest_order_date": latest_order_date,
                "constrained_supply_qty": round(usable if gross > 0 else 0, 1),
                "planned_po_qty": round(planned_po_qty, 1),
                "planned_po_order_date": planned_po_order_date,
            })

            item_results.append({
                "item_id": comp_id, "item_type": "component", "month": ym,
                "demand_signal_type": "derived",
                "gross_demand_qty": round(gross, 1), "net_demand_qty": round(net, 1),
                "safety_stock_qty": round(monthly_ss, 1), "receipts_qty": round(receipts, 1),
                "starting_inventory_qty": round(inv, 1),
                "planned_replenishment_qty": round(net, 1),
                "ending_inventory_qty": round(max(0, inv + receipts - min(gross, usable)), 1),
                "constrained_fg_build_qty": 0,
            })

            # Roll inventory forward: only consume what's available (not full gross if short)
            actual_consumption = min(gross, usable)
            inv = max(0.0, inv + receipts - actual_consumption)

    # Pass 3b: compute SA scale factors using MIN across all constraining components.
    # Summing per-component reductions would double-count when multiple components
    # are short for the same SA — instead find the most constraining component.
    sa_ids_set = set(sa_constrained_build.keys())
    sa_scale = defaultdict(lambda: defaultdict(lambda: 1.0))  # {sa_id: {ym: scale}}

    for comp_id, ym_scales in comp_scale.items():
        for ym, scale in ym_scales.items():
            for sa_id, sa_comp_demand in comp_sa_contribution[comp_id][ym].items():
                if sa_comp_demand > 0:
                    # This component can only supply `scale` of what's demanded by this SA
                    sa_scale[sa_id][ym] = min(sa_scale[sa_id][ym], scale)

    # Convert SA scale to SA reduction quantities
    sa_reduction = defaultdict(lambda: defaultdict(float))
    for sa_id in sa_ids_set:
        for ym in horizon_months:
            scale = sa_scale[sa_id].get(ym, 1.0)
            if scale < 1.0:
                original = sa_constrained_build[sa_id].get(ym, 0)
                sa_reduction[sa_id][ym] = original * (1.0 - scale)

    return {
        "any_shortage": any_shortage,
        "material_avail": material_avail,
        "item_results": item_results,
        "sa_reduction": sa_reduction,
        "planned_orders": planned_orders,
    }


def _apply_comp_feedback(sa_result, sa_reduction, master):
    """Reduce SA constrained build by component shortage and recompute sa_shortfall_by_fg."""
    sa_cb = sa_result["sa_constrained_build"]

    # sa_reduction is already in quantity terms (original_sa * (1 - min_comp_scale))
    # Cap each reduction at the current constrained build to avoid going negative.
    for sa_id, months in sa_reduction.items():
        if sa_id not in sa_cb:
            continue
        for ym, red in months.items():
            original = sa_cb[sa_id].get(ym, 0)
            sa_cb[sa_id][ym] = max(0.0, original - red)

    # Propagate SA reduction to FG.  Only look up FG parents (not SA parents) to avoid
    # incorrectly feeding back through multi-level SA→SA BOM relationships.
    products_set = set(master["products"].keys())
    new_shortfall = defaultdict(lambda: defaultdict(float))
    for sa_id, months in sa_reduction.items():
        for ym, red in months.items():
            if red <= 0:
                continue
            for fg_id, fg_bom in master["bom"].items():
                if fg_id not in products_set:
                    continue  # skip SA→SA links
                for child in fg_bom:
                    if child["child_id"] == sa_id:
                        fg_red = red / max(child["quantity"], 0.001)
                        new_shortfall[fg_id][ym] += fg_red

    return {**sa_result,
            "sa_constrained_build": sa_cb,
            "sa_shortfall_by_fg": new_shortfall}


# ── Summary ───────────────────────────────────────────────────────────────────

def _build_summary(fg_pass, sa_result, comp_result, master):
    total_fg_backlog = sum(r["backlog_qty"] for r in fg_pass["fg_results"])
    total_fg_backlog_eur = sum(
        r["backlog_qty"] * master["products"].get(r["product_id"], {}).get("unit_price", 0)
        for r in fg_pass["fg_results"])

    total_sa_backlog = sum(
        v for months in sa_result["sa_backlog"].values() for v in months.values())

    shortage_items = len({r["material_id"] for r in comp_result["material_avail"]
                          if r["shortage_qty"] > 0})
    total_shortage_eur = sum(
        r["shortage_qty"] * master["materials"].get(r["material_id"], {}).get("unit_cost", 0)
        for r in comp_result["material_avail"])

    overloaded_sa_res = len({r["resource_id"] for r in sa_result["resource_results"]
                              if r["overload_min"] > 0})

    return {
        "fg_backlog_units": round(total_fg_backlog, 0),
        "fg_backlog_eur": round(total_fg_backlog_eur, 0),
        "sa_backlog_units": round(total_sa_backlog, 0),
        "component_shortage_items": shortage_items,
        "component_shortage_eur": round(total_shortage_eur, 0),
        "sa_resources_overloaded": overloaded_sa_res,
    }


# ── DB persistence ────────────────────────────────────────────────────────────

def persist_results(conn, run_id, results):
    """Write all planning results to DB, replacing previous data for this run_id."""

    # planning_material_availability — create / migrate if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planning_material_availability (
            run_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            month TEXT NOT NULL,
            gross_requirement_qty REAL NOT NULL DEFAULT 0,
            on_hand_qty REAL NOT NULL DEFAULT 0,
            receipts_qty REAL NOT NULL DEFAULT 0,
            available_qty REAL NOT NULL DEFAULT 0,
            safety_stock_qty REAL NOT NULL DEFAULT 0,
            shortage_qty REAL NOT NULL DEFAULT 0,
            latest_order_date TEXT,
            constrained_supply_qty REAL NOT NULL DEFAULT 0,
            planned_po_qty REAL NOT NULL DEFAULT 0,
            planned_po_order_date TEXT,
            PRIMARY KEY (run_id, material_id, month)
        )
    """)
    # Add new columns to existing tables (idempotent — silently ignored if already present)
    for col_def in [
        ("planning_material_availability", "planned_po_qty REAL NOT NULL DEFAULT 0"),
        ("planning_material_availability", "planned_po_order_date TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {col_def[0]} ADD COLUMN {col_def[1]}")
        except Exception:
            pass  # column already exists

    # planning_planned_orders — create if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planning_planned_orders (
            run_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            supplier_id TEXT,
            need_month TEXT NOT NULL,
            order_date TEXT NOT NULL,
            receipt_date TEXT NOT NULL,
            receipt_month TEXT NOT NULL,
            order_qty REAL NOT NULL DEFAULT 0,
            net_requirement_qty REAL NOT NULL DEFAULT 0,
            moq REAL NOT NULL DEFAULT 1,
            effective_lead_time_days INTEGER NOT NULL DEFAULT 14,
            supplier_segment TEXT,
            late_rate REAL,
            is_past_due INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, material_id, need_month, order_date)
        )
    """)

    # Delete old results for this run
    for tbl in ["planning_fg_constrained_results", "planning_item_month_results",
                "planning_resource_month_results", "planning_resource_product_results",
                "planning_material_availability", "planning_planned_orders"]:
        conn.execute(f"DELETE FROM {tbl} WHERE run_id = ?", (run_id,))

    # Insert FG results
    conn.executemany("""
        INSERT OR REPLACE INTO planning_fg_constrained_results
        (run_id, product_id, month, demand_to_produce_qty, constrained_production_qty,
         backlog_qty, prebuild_inventory_qty)
        VALUES (:run_id, :product_id, :month, :demand_to_produce_qty,
                :constrained_production_qty, :backlog_qty, :prebuild_inventory_qty)
    """, [{**r, "run_id": run_id} for r in results["fg_results"]])

    # Insert item results
    conn.executemany("""
        INSERT OR REPLACE INTO planning_item_month_results
        (run_id, item_id, item_type, month, demand_signal_type,
         gross_demand_qty, net_demand_qty, safety_stock_qty, receipts_qty,
         starting_inventory_qty, planned_replenishment_qty,
         ending_inventory_qty, constrained_fg_build_qty)
        VALUES (:run_id, :item_id, :item_type, :month, :demand_signal_type,
                :gross_demand_qty, :net_demand_qty, :safety_stock_qty, :receipts_qty,
                :starting_inventory_qty, :planned_replenishment_qty,
                :ending_inventory_qty, :constrained_fg_build_qty)
    """, [{**r, "run_id": run_id} for r in results["item_results"]])

    # Resource month results
    conn.executemany("""
        INSERT OR REPLACE INTO planning_resource_month_results
        (run_id, resource_id, resource_name, plant_id, month,
         capacity_min, load_min, overload_min, utilization_pct)
        VALUES (:run_id, :resource_id, :resource_name, :plant_id, :month,
                :capacity_min, :load_min, :overload_min, :utilization_pct)
    """, [{**r, "run_id": run_id} for r in results["resource_results"]])

    # Resource product results
    conn.executemany("""
        INSERT OR REPLACE INTO planning_resource_product_results
        (run_id, resource_id, resource_name, product_id, month, allocated_qty, allocated_min)
        VALUES (:run_id, :resource_id, :resource_name, :product_id, :month,
                :allocated_qty, :allocated_min)
    """, [{**r, "run_id": run_id} for r in results["resource_product"]])

    # Material availability (with planned PO columns)
    conn.executemany("""
        INSERT OR REPLACE INTO planning_material_availability
        (run_id, material_id, month, gross_requirement_qty, on_hand_qty, receipts_qty,
         available_qty, safety_stock_qty, shortage_qty, latest_order_date,
         constrained_supply_qty, planned_po_qty, planned_po_order_date)
        VALUES (:run_id, :material_id, :month, :gross_requirement_qty, :on_hand_qty,
                :receipts_qty, :available_qty, :safety_stock_qty, :shortage_qty,
                :latest_order_date, :constrained_supply_qty,
                :planned_po_qty, :planned_po_order_date)
    """, [{**r, "run_id": run_id} for r in results["material_avail"]])

    # Planned purchase orders (MRP output)
    if results.get("planned_orders"):
        conn.executemany("""
            INSERT OR REPLACE INTO planning_planned_orders
            (run_id, material_id, supplier_id, need_month, order_date, receipt_date,
             receipt_month, order_qty, net_requirement_qty, moq, effective_lead_time_days,
             supplier_segment, late_rate, is_past_due)
            VALUES (:run_id, :material_id, :supplier_id, :need_month, :order_date,
                    :receipt_date, :receipt_month, :order_qty, :net_requirement_qty,
                    :moq, :effective_lead_time_days, :supplier_segment, :late_rate, :is_past_due)
        """, [{**r, "run_id": run_id} for r in results["planned_orders"]])

    conn.commit()
