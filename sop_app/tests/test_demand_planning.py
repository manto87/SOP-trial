"""
Demand Planning Tests
=====================
Tests the complete demand planning workflow:
  1. Stat forecast → manual override → save → reload cycle
  2. Forecast reset clears all overrides
  3. NL command endpoint (server-side route)
  4. Forecast accuracy endpoint
  5. Forecast history endpoint

Run standalone:  python tests/test_demand_planning.py
Via runner:      python tests/test_runner.py
"""

import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, post, is_yyyy_mm
except ImportError:
    from helpers import TestSuite, get, post, is_yyyy_mm

HORIZON = [
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
]


def run():
    t = TestSuite("Demand Planning")

    # ── 1. Stat forecast shape ────────────────────────────────────────────────
    t.section("1 · Statistical Forecast Shape")
    fc_all = get("/api/forecast/statistical")
    t.check("returns list", isinstance(fc_all, list))
    # Should have at least 10 rows (one per product; not all combos may have history)
    t.gte("≥10 stat forecast rows (at least one per product)",
          len(fc_all), 10, "rows")

    for row in fc_all[:3]:
        t.has_key("row has product_id", row, "product_id")
        t.has_key("row has channel", row, "channel")
        t.has_key("row has monthly dict", row, "monthly")
        monthly = row.get("monthly", {})
        t.eq("monthly has 12 entries", len(monthly), 12)

    # Channel values
    channels = {r.get("channel") for r in fc_all}
    t.check("B2B channel present", "B2B" in channels)
    t.check("B2C channel present", "B2C" in channels)

    # Month keys are YYYY-MM
    all_month_keys = []
    for row in fc_all:
        all_month_keys.extend(row.get("monthly", {}).keys())
    bad = [m for m in all_month_keys if not is_yyyy_mm(m)]
    t.check("all stat FC month keys YYYY-MM", len(bad) == 0,
            f"bad: {set(bad)}" if bad else "")

    # ── 2. Product/channel coverage ───────────────────────────────────────────
    # Note: filtering is done client-side; the API always returns all rows
    t.section("2 · Coverage")
    product_ids = {r.get("product_id") for r in fc_all}
    t.check("FG001 present in forecast", "FG001" in product_ids)
    t.check("FG010 present in forecast", "FG010" in product_ids)
    channels_present = {r.get("channel") for r in fc_all}
    t.check("B2B channel present", "B2B" in channels_present)
    t.check("B2C channel present", "B2C" in channels_present)

    # ── 3. Forecast reset ─────────────────────────────────────────────────────
    t.section("3 · Forecast Reset")
    # First ensure we have some overrides to clear
    for month in ["2025-05", "2025-06"]:
        post("/api/forecast/manual", {
            "product_id": "FG002", "channel": "B2C",
            "forecast_month": month,
            "manual_forecast_qty": 12345,
            "statistical_forecast_qty": 500,
            "planner_id": "DPL001", "note": "reset_test"
        })

    pre = get("/api/forecast/manual")
    test_entries = [r for r in pre
                    if r.get("product_id") == "FG002" and r.get("channel") == "B2C"]
    t.gte("test overrides saved before reset", len(test_entries), 2)

    rst = post("/api/forecast/reset")
    t.eq("reset status:ok", rst.get("status"), "ok")
    t.gte("deleted_rows ≥ 2", rst.get("deleted_rows", 0), 2)

    post_reset = get("/api/forecast/manual")
    t.eq("after reset: 0 overrides remain", len(post_reset), 0)

    # ── 4. Override save + read back ──────────────────────────────────────────
    t.section("4 · Override Save and Read-Back")
    test_cases = [
        ("FG001", "B2B", "2025-03", 1111),
        ("FG001", "B2B", "2025-11", 2222),
        ("FG003", "B2C", "2025-07", 3333),
    ]
    for prod, ch, month, qty in test_cases:
        sv = post("/api/forecast/manual", {
            "product_id": prod, "channel": ch,
            "forecast_month": month,
            "manual_forecast_qty": qty,
            "statistical_forecast_qty": 500,
            "planner_id": "DPL001", "note": "read_back_test"
        })
        t.eq(f"save {prod}/{ch}/{month} → ok", sv.get("status"), "ok")

    saved = get("/api/forecast/manual")
    saved_map = {
        (r.get("product_id"), r.get("channel"), r.get("forecast_month")): r.get("manual_forecast_qty")
        for r in saved
    }
    for prod, ch, month, qty in test_cases:
        db_month = month + "-01"  # DB stores with -01 day
        t.eq(f"read back {prod}/{ch}/{month}",
             saved_map.get((prod, ch, db_month)), qty,
             f"key ({prod},{ch},{db_month}) → got {saved_map.get((prod,ch,db_month))}")

    # Latest override wins (update same cell)
    post("/api/forecast/manual", {
        "product_id": "FG001", "channel": "B2B", "forecast_month": "2025-03",
        "manual_forecast_qty": 9999, "statistical_forecast_qty": 500,
        "planner_id": "DPL001", "note": "update_test"
    })
    updated = get("/api/forecast/manual")
    updated_map = {
        (r.get("product_id"), r.get("channel"), r.get("forecast_month")): r.get("manual_forecast_qty")
        for r in updated
    }
    t.eq("update: latest value wins (9999 not 1111)",
         updated_map.get(("FG001", "B2B", "2025-03-01")), 9999)

    # Clean up
    post("/api/forecast/reset")

    # ── 5. Forecast history ───────────────────────────────────────────────────
    t.section("5 · Forecast History")
    try:
        hist = get("/api/forecast/history")
        t.check("history returns list", isinstance(hist, list))
        t.gt("history has records", len(hist), 0, "records")
        if hist:
            t.has_key("history row has product_id", hist[0], "product_id")
            t.has_key("history row has month", hist[0], "month")
            t.has_key("history row has qty", hist[0], "qty")
    except Exception as e:
        t.check("forecast history accessible", False, str(e))

    # ── 6. Forecast accuracy ──────────────────────────────────────────────────
    t.section("6 · Forecast Accuracy")
    try:
        acc = get("/api/forecast/accuracy")
        t.check("accuracy returns data", bool(acc))
    except Exception as e:
        t.check("forecast accuracy accessible", False, str(e))

    # ── 7. NL command server endpoint ─────────────────────────────────────────
    # Note: returns 503 when ANTHROPIC_API_KEY not set (expected in dev/test env)
    t.section("7 · NL Command Endpoint")
    try:
        nl = post("/api/forecast/nl_command", {"command": "increase FG001 in March by 10%"})
        t.check("nl_command returns dict", isinstance(nl, dict))
    except AssertionError as e:
        err = str(e)
        # 503 = no API key configured (expected in test env) — not a real failure
        if "HTTP 503" in err or "API key" in err.lower() or "not set" in err.lower():
            t.check("nl_command: 503 expected (no API key in test env)", True)
        else:
            t.check("nl_command accessible", False, err[:100])

    # ── 8. Stat forecast values are positive and reasonable ───────────────────
    t.section("8 · Stat Forecast Value Sanity")
    fc_all2 = get("/api/forecast/statistical")
    all_values = []
    for row in fc_all2:
        all_values.extend(row.get("monthly", {}).values())

    t.check("all stat FC values ≥ 0", all(v >= 0 for v in all_values))
    t.check("all stat FC values < 100,000 (no runaway forecast)",
            all(v < 100_000 for v in all_values),
            f"max={max(all_values)}" if all_values else "empty")
    t.check("mean stat FC value > 50 (not degenerate)",
            (sum(all_values) / len(all_values)) > 50 if all_values else False,
            f"mean={sum(all_values)/len(all_values):.0f}" if all_values else "empty")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
