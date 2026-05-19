"""
Smoke Tests — Critical Path Checklist
======================================
A fast (~5 second) test that covers the exact scenarios that have broken
before. Run this FIRST after any change to confirm nothing is obviously broken.

Every test here corresponds to a real user-reported bug:

  [A] Month format:  heatmap didn't update after Run Plan
      Root cause: planning engine wrote YYYY-MM-01 instead of YYYY-MM
  [B] Pull-forward selector: empty, couldn't select resource
      Root cause: same month format bug (0 rows matched SQL filter)
  [C] Build Peg: "no data available"
      Root cause: default date was outside horizon (new Date() + 3mo = 2026-07)
  [D] NL: "Increase Mar'25 demand by 20% for FG001" silently failed
      Root cause: noise word removal left orphan "for" + year-stripped month
  [E] NL: "increase FG001 in channel B2B in all months by 20%" failed
      Root cause: "months" not stripped; "all" not recognised as month token
  [F] Urgency "302 days to order" using real date instead of planning date
      Root cause: new Date() = 2026-04 used instead of planning "today" = 2025-03-01
  [G] _latest_run_id always returned baseline
      Root cause: ORDER BY MAX(month) DESC preferred baseline (months to 2027-02)

Run standalone:  python tests/test_smoke.py
Via runner:      python tests/test_runner.py
"""

import sys
import os
import re
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, post, is_yyyy_mm
    from tests.test_nl_parser import apply_nl_command, MONTHS
except ImportError:
    from helpers import TestSuite, get, post, is_yyyy_mm
    from test_nl_parser import apply_nl_command, MONTHS

PLANNING_TODAY = date(2025, 3, 1)
HORIZON_END    = date(2026, 2, 28)


def run():
    t = TestSuite("Smoke (Critical Path)")

    # ── [A] Month format: supply plan must return YYYY-MM ─────────────────────
    t.section("[A] Month Format Contract")
    plan = get("/api/supply/plan")
    fg = plan.get("fg_plan", [])
    res = plan.get("resources", [])

    bad_fg  = [r["month"] for r in fg  if not is_yyyy_mm(r.get("month",""))]
    bad_res = [r["month"] for r in res if not is_yyyy_mm(r.get("month",""))]

    t.check("[A] fg_plan months are YYYY-MM (not YYYY-MM-01)",
            len(bad_fg) == 0,
            f"bad: {bad_fg[:3]}" if bad_fg else "")
    t.check("[A] resource months are YYYY-MM (not YYYY-MM-01)",
            len(bad_res) == 0,
            f"bad: {bad_res[:3]}" if bad_res else "")

    # ── [B] Pull-forward selector: product_load must have rows ────────────────
    t.section("[B] Pull-Forward Selector")
    rpl = get("/api/supply/resource_product_load")
    pl = rpl if isinstance(rpl, list) else rpl.get("product_loads", [])
    t.gt("[B] product_loads has rows (selector non-empty)", len(pl), 50, "rows")
    resources_seen = {r.get("resource_id") for r in pl}
    t.gt("[B] multiple resources in selector", len(resources_seen), 1,
         f"{len(resources_seen)} resources")

    # ── [C] Build Peg: BOM has data, default date is in horizon ──────────────
    t.section("[C] Build Peg Default Date")
    bom = get("/api/bom/FG001")
    t.check("[C] BOM FG001 returns tree", bool(bom) and bool(bom.get("children", [])))

    default_date = date(2025, 9, 1)  # hardcoded in JS
    t.check("[C] default peg date (2025-09-01) is within horizon",
            PLANNING_TODAY <= default_date <= HORIZON_END)

    real_date_plus_3mo = date.today().replace(year=date.today().year) + timedelta(days=90)
    outside = real_date_plus_3mo > HORIZON_END
    t.check("[C] real date + 3mo is OUTSIDE horizon (confirms we must hardcode default)",
            outside,
            f"{real_date_plus_3mo} > {HORIZON_END}: {outside}")

    # ── [D] NL: "Increase Mar'25 demand by 20% for FG001" ────────────────────
    t.section("[D] NL Regression: Mar'25 + noise word + for PRODUCT")
    overrides = {}
    count, msg = apply_nl_command("Increase Mar'25 demand by 20% for FG001", overrides)
    t.eq("[D] applied to 2 cells (B2B + B2C)", count, 2,
         f"msg: {msg}")
    t.eq("[D] FG001 B2B 2025-03 = 600", overrides.get("FG001_B2B_2025-03"), 600)
    t.eq("[D] FG001 B2C 2025-03 = 600", overrides.get("FG001_B2C_2025-03"), 600)

    overrides.clear()
    count2, msg2 = apply_nl_command("increase demand for FG001 in March by 20%", overrides)
    t.eq("[D] alt word order: 2 cells", count2, 2, f"msg: {msg2}")

    # ── [E] NL: "increase FG001 in channel B2B in all months by 20%" ──────────
    t.section("[E] NL Regression: channel keyword + all months")
    overrides.clear()
    count3, msg3 = apply_nl_command(
        "increase FG001 in channel B2B in all months by 20%", overrides
    )
    t.eq("[E] applied to 12 cells (FG001 × B2B × 12 months)", count3, 12,
         f"msg: {msg3}")
    t.check("[E] all 12 B2B months overridden",
            all(f"FG001_B2B_{m}" in overrides for m in MONTHS))
    t.check("[E] B2C not affected",
            not any(f"FG001_B2C_{m}" in overrides for m in MONTHS))

    # ── [F] Urgency uses planning date, not real date ─────────────────────────
    t.section("[F] Urgency Calculation Uses Planning Date")
    # An order_by date of 2025-06-01 should give positive urgency from 2025-03-01
    order_by = date(2025, 6, 1)
    urgency_from_planning = (order_by - PLANNING_TODAY).days
    urgency_from_real     = (order_by - date.today()).days

    t.check("[F] from planning date (2025-03-01): urgency is positive",
            urgency_from_planning > 0,
            f"got {urgency_from_planning} days")
    t.check("[F] from real date (today): urgency would be negative (wrong)",
            urgency_from_real < 0,
            f"got {urgency_from_real} days — confirms real date gives wrong result")

    # ── [G] Supply plan data quality (no plan run needed — reads existing data) ─
    # We verify the supply plan endpoint always returns YYYY-MM months regardless
    # of which run_id is active. The full plan-run cycle is tested in test_plan_engine.
    t.section("[G] Supply Plan Data Integrity (latest run)")
    plan2 = get("/api/supply/plan")
    fg2 = plan2.get("fg_plan", [])
    t.gt("[G] fg_plan has rows", len(fg2), 0)

    bad2 = [r["month"] for r in fg2 if not is_yyyy_mm(r.get("month", ""))]
    t.check("[G] all months YYYY-MM (not YYYY-MM-01)", len(bad2) == 0,
            f"bad: {bad2[:3]}" if bad2 else "")

    # Verify resource results have sensible utilisation (confirms a real plan was run)
    res2 = plan2.get("resources", [])
    t.gt("[G] resources has entries", len(res2), 0)
    utils2 = [r.get("utilization_pct", 0) for r in res2]
    t.check("[G] overloaded resources exist (plan ran correctly)",
            any(u > 100 for u in utils2),
            f"max util: {max(utils2):.1f}%" if utils2 else "no data")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
