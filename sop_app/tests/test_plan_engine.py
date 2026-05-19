"""
Planning Engine Tests
=====================
Tests the plan run cycle end-to-end:
  1. POST /api/plan/run → produces a run_id
  2. Supply plan immediately reflects the new run (not the baseline)
  3. Month format contract holds after a plan run
  4. Utilisation values are plausible (not all zero, not all >500%)
  5. run_id format and _latest_run_id logic

Run standalone:  python tests/test_plan_engine.py
Via runner:      python tests/test_runner.py
"""

import sys
import os
import re
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, post, is_yyyy_mm
except ImportError:
    from helpers import TestSuite, get, post, is_yyyy_mm

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "electrotech_db_v5.sqlite"
)

HORIZON = [
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
]


def run():
    t = TestSuite("Planning Engine")

    # ── 1. Trigger a plan run ─────────────────────────────────────────────────
    t.section("1 · Plan Run")
    result = post("/api/plan/run", {})
    t.check("run returns data", bool(result))
    run_id = result.get("run_id", "")
    t.check("run_id starts with 'run_'", run_id.startswith("run_"),
            f"got '{run_id}'")
    t.check("run_id format run_YYYYMMDD_HHMMSS",
            bool(re.match(r"^run_\d{8}_\d{6}$", run_id)),
            f"got '{run_id}'")

    # ── 2. Post-run supply plan: month format contract ────────────────────────
    t.section("2 · Post-Run Supply Plan")
    plan = get("/api/supply/plan")

    fg = plan.get("fg_plan", [])
    t.gt("fg_plan has rows", len(fg), 0)
    bad_fg = [r["month"] for r in fg if not is_yyyy_mm(r.get("month", ""))]
    t.check("fg_plan months YYYY-MM after run", len(bad_fg) == 0,
            f"bad: {bad_fg[:3]}" if bad_fg else "")

    res = plan.get("resources", [])
    t.gt("resources has rows", len(res), 0)
    bad_res = [r["month"] for r in res if not is_yyyy_mm(r.get("month", ""))]
    t.check("resource months YYYY-MM after run", len(bad_res) == 0,
            f"bad: {bad_res[:3]}" if bad_res else "")

    # ── 3. Utilisation plausibility ───────────────────────────────────────────
    t.section("3 · Utilisation Plausibility")
    utils = [r.get("utilization_pct", 0) for r in res]
    t.check("not all zero utilisation", any(u > 0 for u in utils))
    t.check("not all >500% (insane)", all(u <= 500 for u in utils),
            f"bad: {[u for u in utils if u > 500][:3]}")
    t.check("some resources >100% (meaningful overload)",
            any(u > 100 for u in utils),
            f"max={max(utils):.1f}%" if utils else "empty")
    t.check("some resources ≤100% (not all overloaded)",
            any(0 < u <= 100 for u in utils))

    # ── 4. Resource product load updated ─────────────────────────────────────
    t.section("4 · Resource Product Load Updated")
    rpl = get("/api/supply/resource_product_load")
    pl = rpl if isinstance(rpl, list) else rpl.get("product_loads", [])
    t.gt("product_loads non-empty after run", len(pl), 0)
    bad_pl = [r.get("month", "") for r in pl if not is_yyyy_mm(r.get("month", ""))]
    t.check("product_load months YYYY-MM after run", len(bad_pl) == 0,
            f"bad: {bad_pl[:3]}" if bad_pl else "")
    resources_seen = {r.get("resource_id") for r in pl}
    t.gt("multiple resources in product_load", len(resources_seen), 1)

    # ── 5. DB: run_id written correctly ──────────────────────────────────────
    t.section("5 · Database Integrity")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # run_id exists in DB
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM planning_fg_constrained_results WHERE run_id=?",
            (run_id,)
        ).fetchone()
        t.gt("run_id rows in planning_fg_constrained_results", row["n"], 0)

        # Month format in DB
        bad_db = conn.execute(
            """SELECT DISTINCT month FROM planning_fg_constrained_results
               WHERE run_id=? AND length(month) != 7""",
            (run_id,)
        ).fetchall()
        t.check("DB months are all 7 chars (YYYY-MM)", len(bad_db) == 0,
                f"bad DB months: {[r['month'] for r in bad_db[:3]]}" if bad_db else "")

        # Resource results in DB (table is planning_resource_month_results)
        res_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM planning_resource_month_results WHERE run_id=?",
            (run_id,)
        ).fetchone()
        t.gt("resource results written to DB", res_rows["n"], 0)

        # _latest_run_id picks user runs over baseline
        latest = conn.execute(
            "SELECT run_id FROM planning_fg_constrained_results "
            "WHERE run_id LIKE 'run_%' GROUP BY run_id ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        t.check("_latest_run_id picks user run (not baseline)",
                latest and latest["run_id"] == run_id,
                f"got {latest['run_id'] if latest else None}")

        conn.close()
    except Exception as e:
        t.check("DB checks passed", False, str(e))

    # ── 6. Coverage: all 12 horizon months produced ───────────────────────────
    t.section("6 · Horizon Coverage")
    res_months = {r.get("month") for r in res}
    missing = [m for m in HORIZON if m not in res_months]
    t.check("all 12 horizon months in resources", len(missing) == 0,
            f"missing: {missing}" if missing else "")

    fg_months = {r.get("month") for r in fg}
    fg_missing = [m for m in HORIZON if m not in fg_months]
    t.check("all 12 horizon months in fg_plan", len(fg_missing) == 0,
            f"missing: {fg_missing}" if fg_missing else "")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
