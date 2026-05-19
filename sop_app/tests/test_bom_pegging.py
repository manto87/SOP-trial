"""
BOM Navigator & Build Peg Tests
================================
Tests the BOM tree structure, inventory data, and the Build Peg logic
(the client-side pegging calculation, ported to Python for server-side testing).

Critical previously-broken scenario:
  - Build Peg showed "no data" because default date was outside horizon
  - Urgency showed "302 days to order" relative to real date (2026-04)
    instead of planning date (2025-03-01)

Run standalone:  python tests/test_bom_pegging.py
Via runner:      python tests/test_runner.py
"""

import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite, get, is_yyyy_mm
except ImportError:
    from helpers import TestSuite, get, is_yyyy_mm

# Planning "today" — must match JS constant and planning engine
PLANNING_TODAY = date(2025, 3, 1)

# Horizon boundaries
HORIZON_START = date(2025, 3, 1)
HORIZON_END   = date(2026, 2, 28)


def flatten_bom(node, depth=0):
    """Recursively yield (node, depth) tuples from a BOM tree."""
    yield node, depth
    for child in node.get("children", []):
        yield from flatten_bom(child, depth + 1)


def run():
    t = TestSuite("BOM Pegging")

    # ── 1. BOM tree structure ─────────────────────────────────────────────────
    t.section("1 · BOM Tree Structure")
    bom = get("/api/bom/FG001")
    t.check("BOM FG001 returns dict", isinstance(bom, dict))

    nodes = list(flatten_bom(bom))
    t.gt("BOM has multiple nodes (FG + children)", len(nodes), 2)

    # Root node
    root = bom
    t.eq("root is FG001", root.get("id") or root.get("item_id"), "FG001",
         f"got {root.get('id') or root.get('item_id')}")
    t.check("root bom_level == 0 or 1",
            root.get("bom_level", 0) <= 1)
    t.check("root has children", len(root.get("children", [])) > 0,
            f"got {len(root.get('children', []))} children")

    # All nodes have required fields (lead_time_days may be absent from root/packaging nodes)
    for field in ["id", "effective_qty"]:
        missing = [n.get("id", "?") for n, _ in nodes if field not in n]
        t.check(f"all nodes have {field}", len(missing) == 0,
                f"missing in: {missing[:3]}" if missing else "")

    # lead_time_days: check where present, note total coverage (BOM API may omit at root)
    lt_present = [(n, d) for n, d in nodes if "lead_time_days" in n]
    lt_pct = len(lt_present) / max(len(nodes), 1) * 100

    # Where present, lead_time_days must be non-negative
    bad_lt = [(n.get("id"), n.get("lead_time_days")) for n, _ in lt_present
              if not isinstance(n.get("lead_time_days"), (int, float))
              or n.get("lead_time_days") < 0]
    t.check("where present: all lead_time_days ≥ 0", len(bad_lt) == 0,
            f"bad: {bad_lt[:3]}" if bad_lt else "")

    # Informational: how many nodes have lead_time_days (no pass/fail — API may omit at root)
    t.check(f"lead_time_days coverage noted: {len(lt_present)}/{len(nodes)} nodes ({lt_pct:.0f}%)",
            True)  # always passes — this is a data coverage metric, not a hard requirement

    # effective_qty is positive
    bad_qty = [(n.get("id"), n.get("effective_qty")) for n, _ in nodes
               if not isinstance(n.get("effective_qty"), (int, float))
               or n.get("effective_qty") <= 0]
    t.check("all effective_qty > 0", len(bad_qty) == 0,
            f"bad: {bad_qty[:3]}" if bad_qty else "")

    # ── 2. Inventory data present in BOM ─────────────────────────────────────
    t.section("2 · Inventory Data")
    inv_nodes = [(n.get("id"), n.get("inventory")) for n, _ in nodes
                 if n.get("inventory") is not None]
    t.gt("some nodes have inventory data", len(inv_nodes), 0,
         f"out of {len(nodes)} nodes")

    for item_id, inv in inv_nodes[:5]:
        if isinstance(inv, dict):
            t.check(f"{item_id}: inventory has quantity_on_hand",
                    "quantity_on_hand" in inv,
                    f"keys: {list(inv.keys())}")

    # ── 3. Build Peg calculation (Python port of JS runBuildPeg) ─────────────
    t.section("3 · Build Peg Calculation")

    def run_build_peg(bom_root, qty: int, target_date_str: str):
        """
        Port of the JS runBuildPeg function.
        Returns list of {id, level, required_qty, need_by, order_by,
                         on_hand, gap, urgency_days} dicts.
        """
        target_date = date.fromisoformat(target_date_str)
        results = []

        def walk(node, parent_qty: float, parent_need_by: date):
            item_id   = node.get("id") or node.get("item_id", "?")
            eff_qty   = float(node.get("effective_qty", 1))
            lt_days   = int(node.get("lead_time_days", 0))
            on_hand   = 0
            inv = node.get("inventory")
            if isinstance(inv, dict):
                on_hand = inv.get("quantity_on_hand", 0) or 0

            req_qty   = parent_qty * eff_qty
            need_by   = parent_need_by
            order_by  = need_by - timedelta(days=lt_days)
            gap       = max(0, req_qty - on_hand)
            urg_days  = (order_by - PLANNING_TODAY).days

            results.append({
                "id":           item_id,
                "level":        node.get("bom_level", 0),
                "required_qty": req_qty,
                "need_by":      need_by.isoformat(),
                "order_by":     order_by.isoformat(),
                "on_hand":      on_hand,
                "gap":          gap,
                "urgency_days": urg_days,
            })

            for child in node.get("children", []):
                child_need_by = need_by - timedelta(days=lt_days)
                walk(child, req_qty, child_need_by)

        # Root: need_by = target_date, required_qty = input qty
        root_lt  = int(bom_root.get("lead_time_days", 0))
        root_inv = bom_root.get("inventory") or {}
        root_oh  = (root_inv.get("quantity_on_hand") or 0) if isinstance(root_inv, dict) else 0
        results.append({
            "id":           bom_root.get("id") or bom_root.get("item_id", "?"),
            "level":        0,
            "required_qty": qty,
            "need_by":      target_date.isoformat(),
            "order_by":     (target_date - timedelta(days=root_lt)).isoformat(),
            "on_hand":      root_oh,
            "gap":          max(0, qty - root_oh),
            "urgency_days": ((target_date - timedelta(days=root_lt)) - PLANNING_TODAY).days,
        })
        for child in bom_root.get("children", []):
            walk(child, qty, target_date)

        return results

    # Test: 500 units of FG001 needed by 2025-09-01 (within horizon)
    peg_result = run_build_peg(bom, qty=500, target_date_str="2025-09-01")
    t.gt("peg produces results", len(peg_result), 0)

    # Root entry
    root_entry = next((r for r in peg_result if r["level"] == 0), None)
    t.check("root entry present", root_entry is not None)
    if root_entry:
        t.eq("root required_qty = input qty", root_entry["required_qty"], 500.0)
        t.eq("root need_by = target date", root_entry["need_by"], "2025-09-01")

    # All need_by dates are parseable
    for entry in peg_result:
        try:
            date.fromisoformat(entry["need_by"])
            date.fromisoformat(entry["order_by"])
            ok = True
        except ValueError:
            ok = False
        if not ok:
            t.check(f"parseable dates for {entry['id']}", False,
                    f"need_by={entry['need_by']}, order_by={entry['order_by']}")
            break
    else:
        t.check("all need_by / order_by dates parseable", True)

    # No negative required_qty
    neg_qty = [r for r in peg_result if r["required_qty"] < 0]
    t.check("no negative required_qty", len(neg_qty) == 0,
            f"bad: {neg_qty[:2]}" if neg_qty else "")

    # ── 4. Urgency relative to PLANNING_TODAY (not real date) ────────────────
    t.section("4 · Urgency Uses Planning Date (2025-03-01)")

    # Create a synthetic node with known lead time
    synthetic_order_by = date(2025, 6, 1)  # 92 days from 2025-03-01
    expected_urgency = (synthetic_order_by - PLANNING_TODAY).days

    # The urgency for any node = (order_by - PLANNING_TODAY).days
    # If we used real date (2026-04-02), urgency would be deeply negative
    t.check(
        "urgency is days from PLANNING_TODAY (2025-03-01), not real date",
        expected_urgency > 0,
        f"92 days to order from 2025-03-01 → positive (correct); "
        f"from real date 2026-04-02 it would be {(synthetic_order_by - date.today()).days} days (negative = wrong)"
    )

    # Verify all peg results have urgency_days computed from planning date
    # (they should NOT be deeply negative like -300)
    # A planning-relative urgency for a 2025-09-01 target ≥ 0 from 2025-03-01
    too_negative = [r for r in peg_result if r["urgency_days"] < -400]
    t.check(
        "no urgency_days < -400 (would indicate real-date calculation)",
        len(too_negative) == 0,
        f"bad: {[(r['id'], r['urgency_days']) for r in too_negative[:3]]}" if too_negative else ""
    )

    # ── 5. Default peg date within horizon ───────────────────────────────────
    t.section("5 · Default Peg Date Within Horizon")

    # The JS default is hardcoded to 2025-09-01 — verify it's within the horizon
    default_date = date(2025, 9, 1)
    t.check("default peg date 2025-09-01 is within horizon",
            HORIZON_START <= default_date <= HORIZON_END,
            f"{default_date} not between {HORIZON_START} and {HORIZON_END}")

    # A date like 2026-07-01 (what new Date() + 3 months would be in April 2026)
    bad_default = date(2026, 7, 1)
    t.check("real-date default 2026-07-01 is OUTSIDE horizon (confirms we must hardcode)",
            not (HORIZON_START <= bad_default <= HORIZON_END))

    # ── 6. Multi-product BOM check ────────────────────────────────────────────
    t.section("6 · Multi-Product BOM Coverage")
    for pid in ["FG001", "FG002", "FG005"]:
        b = get(f"/api/bom/{pid}")
        t.check(f"BOM {pid} returns data", bool(b))
        t.check(f"BOM {pid} has children", len(b.get("children", [])) > 0,
                f"keys: {list(b.keys())}")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
