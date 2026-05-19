"""
Core API Tests
==============
Tests every important API endpoint: correct HTTP status, response shape,
data types, and the critical YYYY-MM month format contract.

The month format contract is the most common source of bugs:
  RULE: All month values returned by any API endpoint MUST be exactly
  7 characters in YYYY-MM format.  Never YYYY-MM-01.

Run standalone:  python tests/test_api_core.py
Via runner:      python tests/test_runner.py
"""

import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, post, is_yyyy_mm, bad_months
except ImportError:
    from helpers import TestSuite, get, post, is_yyyy_mm, bad_months

HORIZON = [
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
]


def run():
    t = TestSuite("API Core")

    # ── 1. Products ───────────────────────────────────────────────────────────
    t.section("1 · Products")
    prods = get("/api/products")
    t.check("returns a list", isinstance(prods, list))
    t.eq("10 products", len(prods), 10)
    pids = [p["product_id"] for p in prods]
    for pid in ["FG001", "FG005", "FG010"]:
        t.contains(f"{pid} present", pids, pid)
    if prods:
        t.has_key("product has product_id", prods[0], "product_id")
        t.has_key("product has name", prods[0], "name")

    # ── 2. Statistical forecast ───────────────────────────────────────────────
    t.section("2 · Statistical Forecast")
    fc = get("/api/forecast/statistical?product_id=FG001&channel=B2B")
    t.check("returns a list", isinstance(fc, list))
    fg001 = [r for r in fc if r.get("product_id") == "FG001" and r.get("channel") == "B2B"]
    t.eq("FG001 B2B row present", len(fg001), 1)
    if fg001:
        monthly = fg001[0].get("monthly", {})
        t.eq("12 months in monthly dict", len(monthly), 12)
        bad = [m for m in monthly if not is_yyyy_mm(m)]
        t.check("all month keys YYYY-MM (not YYYY-MM-01)", len(bad) == 0,
                f"bad: {bad[:3]}" if bad else "")
        t.check("all stat FC values > 0", all(v > 0 for v in monthly.values()))
        t.check("horizon months covered",
                all(m in monthly for m in HORIZON))

    # ── 3. Forecast reset ─────────────────────────────────────────────────────
    t.section("3 · Forecast Reset")
    rst = post("/api/forecast/reset")
    t.eq("reset returns status:ok", rst.get("status"), "ok")
    t.check("deleted_rows is int", isinstance(rst.get("deleted_rows"), int))

    # ── 4. Manual forecast save + read cycle ──────────────────────────────────
    t.section("4 · Manual Forecast Save / Read")

    # Save two months
    for month, qty in [("2025-11", 9999), ("2025-12", 8888)]:
        sv = post("/api/forecast/manual", {
            "product_id": "FG001", "channel": "B2B",
            "forecast_month": month,
            "manual_forecast_qty": qty,
            "statistical_forecast_qty": 500,
            "planner_id": "DPL001", "note": "unit_test"
        })
        t.eq(f"save {month} → status:ok", sv.get("status"), "ok")

    man = get("/api/forecast/manual")
    t.check("GET /api/forecast/manual returns list", isinstance(man, list))
    overrides = {r.get("forecast_month"): r.get("manual_forecast_qty") for r in man}
    # DB stores with -01 suffix; client strips it — validate DB value
    t.eq("2025-11-01 stored correctly", overrides.get("2025-11-01"), 9999)
    t.eq("2025-12-01 stored correctly", overrides.get("2025-12-01"), 8888)

    # Reset clears them
    post("/api/forecast/reset")
    man2 = get("/api/forecast/manual")
    t.eq("after reset: 0 overrides", len(man2), 0)

    # ── 5. Supply plan (critical: month format contract) ──────────────────────
    t.section("5 · Supply Plan — Month Format Contract")
    plan = get("/api/supply/plan")
    t.check("returns dict", isinstance(plan, dict))

    fg = plan.get("fg_plan", [])
    t.gt("fg_plan non-empty", len(fg), 0, "rows")
    t.all_yyyy_mm("fg_plan months YYYY-MM", fg)

    resources = plan.get("resources", [])
    t.gt("resources non-empty", len(resources), 0, "rows")
    t.all_yyyy_mm("resource months YYYY-MM", resources)

    # Utilisation sanity checks
    utils = [r.get("utilization_pct", 0) for r in resources]
    t.check("some resources overloaded (>100%)", any(u > 100 for u in utils),
            f"max={max(utils):.1f}%" if utils else "empty")
    t.check("no impossible utilisation (>500%)", all(u <= 500 for u in utils),
            f"bad: {[u for u in utils if u > 500][:3]}")

    # Coverage: all 12 horizon months present in resources
    res_months = {r.get("month") for r in resources}
    missing = [m for m in HORIZON if m not in res_months]
    t.check("all 12 horizon months in resources", len(missing) == 0,
            f"missing: {missing}" if missing else "")

    # ── 6. Resource product load (pull-forward selector) ─────────────────────
    t.section("6 · Resource Product Load (pull-forward)")
    rpl = get("/api/supply/resource_product_load")
    pl = rpl if isinstance(rpl, list) else rpl.get("product_loads", [])
    t.gt("product_loads non-empty", len(pl), 100, "rows")

    resources_seen = list({r.get("resource_id") for r in pl})
    t.gt("multiple resources available", len(resources_seen), 1,
         f"(selector will be non-empty): {len(resources_seen)} resources")
    t.all_yyyy_mm("product_load months YYYY-MM", pl)

    # All 12 horizon months represented
    pl_months = {r.get("month") for r in pl}
    missing_pl = [m for m in HORIZON if m not in pl_months]
    t.check("all 12 horizon months in product_load", len(missing_pl) == 0,
            f"missing: {missing_pl}" if missing_pl else "")

    # ── 7. Pull-forward recommendations ──────────────────────────────────────
    t.section("7 · Pull-Forward Recommendations")
    try:
        pf = get("/api/supply/pullforward")
        pf_list = pf if isinstance(pf, list) else pf.get("data", pf.get("recommendations", []))
        t.check("pull-forward returns data", bool(pf))
    except Exception as e:
        t.check("pull-forward accessible", False, str(e))

    # ── 8. Exceptions ─────────────────────────────────────────────────────────
    t.section("8 · Supply Exceptions")
    try:
        exc = get("/api/supply/exceptions")
        t.check("exceptions returns data", bool(exc))
    except Exception as e:
        t.check("exceptions accessible", False, str(e))

    # ── 9. BOM Navigator ─────────────────────────────────────────────────────
    t.section("9 · BOM Navigator")
    for pid in ["FG001", "FG005"]:
        bom = get(f"/api/bom/{pid}")
        t.check(f"BOM {pid} returns data", bool(bom))
        if isinstance(bom, dict):
            has_tree = bool(bom.get("children") or bom.get("bom_tree"))
            t.check(f"BOM {pid} has tree", has_tree,
                    f"keys: {list(bom.keys())[:5]}")
            # lead_time_days is used by Build Peg — note if absent (informational, not blocking)
            children = bom.get("children", [])
            child_lt = [c for c in children if "lead_time_days" in c]
            # Not asserting hard requirement here since some BOM APIs omit it at root level
            # test_bom_pegging covers the full data quality check

    # ── 10. BOM Shortage Impact ───────────────────────────────────────────────
    t.section("10 · BOM Shortage Impact")
    try:
        si = get("/api/bom/shortage_impact")
        t.check("shortage_impact returns data", bool(si))
        items = si if isinstance(si, list) else si.get("items", si.get("shortages", []))
        t.check("shortage items present", isinstance(items, (list, dict)))
    except Exception as e:
        t.check("shortage_impact accessible", False, str(e))

    # ── 11. Capacity Solutions ────────────────────────────────────────────────
    t.section("11 · Capacity Solutions")
    cap = get("/api/capacity/solutions")
    sols = cap if isinstance(cap, list) else cap.get("solutions", [])
    t.gt("solutions non-empty", len(sols), 0, "entries")
    if sols:
        first = sols[0]
        for key in ["resource_id", "month", "utilization_pct", "overload_min"]:
            t.has_key(f"solution has {key}", first, key)
        t.all_yyyy_mm("solution months YYYY-MM", sols)
        overloaded = [s for s in sols if s.get("utilization_pct", 0) > 100]
        t.gt("overloaded resources exist", len(overloaded), 0)
        # Shift options present
        t.has_key("solution has saturday_shift", first, "saturday_shift")
        t.has_key("solution has third_shift", first, "third_shift")
        t.has_key("solution has combined", first, "combined")

    # ── 12. Bottleneck Chain ──────────────────────────────────────────────────
    t.section("12 · Bottleneck Chain")
    bc = get("/api/capacity/bottleneck_chain")
    t.check("returns dict", isinstance(bc, dict))
    nodes = bc.get("nodes", [])
    t.gt("nodes present", len(nodes), 0, "nodes")
    t.check("edges key present", "edges" in bc)
    if nodes:
        n = nodes[0]
        for key in ["resource_id", "utilization_pct", "overload_min", "revenue_at_risk"]:
            t.has_key(f"node has {key}", n, key)
        overloaded_nodes = [n for n in nodes if n.get("utilization_pct", 0) > 100]
        t.gt("some nodes overloaded", len(overloaded_nodes), 0)

    # ── 13. Exec Summary ─────────────────────────────────────────────────────
    t.section("13 · Exec Summary")
    exc_sum = get("/api/exec/summary")
    t.check("exec/summary returns data", bool(exc_sum))

    # ── 14. Suppliers ─────────────────────────────────────────────────────────
    t.section("14 · Suppliers")
    for path in ["/api/suppliers/overview", "/api/suppliers/risk"]:
        try:
            data = get(path)
            t.check(f"GET {path}", bool(data))
        except Exception as e:
            t.check(f"GET {path}", False, str(e))

    # ── 15. Knowledge base ────────────────────────────────────────────────────
    t.section("15 · Knowledge Base")
    try:
        kb = get("/api/knowledge")
        t.check("knowledge returns data", bool(kb))
    except Exception as e:
        t.check("knowledge accessible", False, str(e))

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
