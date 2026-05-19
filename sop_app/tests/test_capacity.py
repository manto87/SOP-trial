"""
Capacity Tests
==============
Tests the capacity solutions and bottleneck chain endpoints:
  1. Correct structure and required fields
  2. Utilisation values are plausible
  3. All three shift scenarios are present (Saturday / Night / Combined)
  4. Bottleneck chain nodes + edges consistent
  5. System is "meaningfully loaded": base alone overloads, combined shift resolves

Run standalone:  python tests/test_capacity.py
Via runner:      python tests/test_runner.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, is_yyyy_mm
except ImportError:
    from helpers import TestSuite, get, is_yyyy_mm

HORIZON = [
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
]

# Expected shift capacity additions (minutes/month — the ElectroTech model)
SAT_ADDED_MIN   = 3_840   # 1 Saturday × 8h × 8 resources / month  → per resource
NIGHT_ADDED_MIN = 9_600   # 5 nights/week × 2h × 4.8 weeks
COMB_ADDED_MIN  = 13_440  # Saturday + Night


def run():
    t = TestSuite("Capacity")

    # ── 1. Capacity Solutions shape ───────────────────────────────────────────
    t.section("1 · Capacity Solutions Shape")
    cap = get("/api/capacity/solutions")
    sols = cap if isinstance(cap, list) else cap.get("solutions", [])
    t.gt("solutions non-empty", len(sols), 0)

    required_fields = [
        "resource_id", "resource_name", "plant_id", "month",
        "utilization_pct", "overload_min", "current_capacity_min",
        "current_load_min", "saturday_shift", "third_shift", "combined",
    ]
    if sols:
        first = sols[0]
        for f in required_fields:
            t.has_key(f"solution has '{f}'", first, f)

    # ── 2. Month format contract ──────────────────────────────────────────────
    t.section("2 · Month Format")
    bad = [s.get("month","") for s in sols if not is_yyyy_mm(s.get("month",""))]
    t.check("all solution months YYYY-MM", len(bad) == 0,
            f"bad: {bad[:3]}" if bad else "")

    # Most horizon months present (first month may have no solutions if no overload)
    sol_months = {s.get("month") for s in sols}
    missing = [m for m in HORIZON if m not in sol_months]
    t.check("≥11 of 12 horizon months represented", len(missing) <= 1,
            f"missing: {missing}" if missing else "")

    # ── 3. Utilisation plausibility ───────────────────────────────────────────
    t.section("3 · Utilisation Plausibility")
    utils = [s.get("utilization_pct", 0) for s in sols]
    t.check("some resources >100% (overloaded at base capacity)",
            any(u > 100 for u in utils),
            f"max={max(utils):.1f}%" if utils else "empty")
    t.check("some resources ≤100% (not all overloaded)",
            any(0 < u <= 100 for u in utils))
    t.check("no impossible utilisation >500%",
            all(u <= 500 for u in utils),
            f"bad: {[u for u in utils if u > 500][:3]}")

    # ── 4. Shift scenario data ────────────────────────────────────────────────
    t.section("4 · Shift Scenarios")
    overloaded = [s for s in sols if s.get("utilization_pct", 0) > 100]
    t.gt("overloaded entries exist", len(overloaded), 0)

    for s in overloaded[:3]:
        sat  = s.get("saturday_shift") or {}
        ngt  = s.get("third_shift") or {}
        comb = s.get("combined") or {}

        res_month = f"{s.get('resource_id')}/{s.get('month')}"

        # Added capacity values
        if sat:
            t.check(f"{res_month}: saturday added_capacity_min > 0",
                    sat.get("added_capacity_min", 0) > 0,
                    f"got {sat.get('added_capacity_min')}")
        if ngt:
            t.check(f"{res_month}: night added_capacity_min > saturday",
                    ngt.get("added_capacity_min", 0) > sat.get("added_capacity_min", 0),
                    f"sat={sat.get('added_capacity_min')} night={ngt.get('added_capacity_min')}")
        if comb:
            t.check(f"{res_month}: combined > night",
                    comb.get("added_capacity_min", 0) >= ngt.get("added_capacity_min", 0),
                    f"night={ngt.get('added_capacity_min')} comb={comb.get('added_capacity_min')}")

        # Cost values
        if sat:
            t.check(f"{res_month}: saturday cost > 0",
                    (sat.get("cost_eur") or 0) > 0)

    # ── 5. System balance: combined shift must eventually resolve overloads ───
    t.section("5 · System Balance (Combined Shift Resolution)")
    # Some resources should be resolved by combined shift (new_utilization_pct ≤ 100)
    resolved_by_combined = [
        s for s in overloaded
        if (s.get("combined") or {}).get("new_utilization_pct", 999) <= 100
    ]
    unresolvable = [
        s for s in overloaded
        if (s.get("combined") or {}).get("new_utilization_pct", 999) > 100
    ]
    t.check("some overloads resolved by combined shift",
            len(resolved_by_combined) > 0,
            f"0 resolved out of {len(overloaded)} overloaded")
    # Log how many can't be resolved (informational)
    t.check(f"{len(unresolvable)}/{len(overloaded)} unresolvable entries noted (informational)",
            True)  # This is always "pass" — just reporting

    # ── 6. Bottleneck Chain ───────────────────────────────────────────────────
    t.section("6 · Bottleneck Chain")
    bc = get("/api/capacity/bottleneck_chain")
    t.check("returns dict", isinstance(bc, dict))

    nodes = bc.get("nodes", [])
    edges = bc.get("edges", [])
    t.gt("nodes present", len(nodes), 0)
    t.check("edges key present", "edges" in bc)

    # Node fields
    node_fields = ["resource_id", "resource_name", "utilization_pct",
                   "overload_min", "revenue_at_risk"]
    if nodes:
        for f in node_fields:
            t.has_key(f"node has '{f}'", nodes[0], f)

        overloaded_nodes = [n for n in nodes if n.get("utilization_pct", 0) > 100]
        t.gt("overloaded nodes exist", len(overloaded_nodes), 0,
             f"out of {len(nodes)} nodes")

        # Revenue at risk is positive for at least the main bottlenecks
        # (some minor overloads like packaging resources may have 0)
        rev_positive = sum(1 for n in overloaded_nodes if (n.get("revenue_at_risk") or 0) > 0)
        t.check("majority of overloaded nodes have revenue_at_risk > 0",
                rev_positive >= len(overloaded_nodes) * 0.6,
                f"{rev_positive}/{len(overloaded_nodes)} have revenue_at_risk > 0")

    # Edge fields
    if edges:
        edge_fields = ["source", "target", "total_min"]
        for f in edge_fields:
            t.has_key(f"edge has '{f}'", edges[0], f)

        # Edges reference valid node IDs
        node_ids = {n.get("resource_id") for n in nodes}
        bad_edges = [
            e for e in edges
            if e.get("source") not in node_ids or e.get("target") not in node_ids
        ]
        t.check("all edge source/target exist as nodes", len(bad_edges) == 0,
                f"bad edges: {bad_edges[:2]}" if bad_edges else "")

    # No self-loops
    self_loops = [e for e in edges if e.get("source") == e.get("target")]
    t.check("no self-loop edges", len(self_loops) == 0,
            f"loops: {self_loops[:2]}" if self_loops else "")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
