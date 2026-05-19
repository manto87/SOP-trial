"""
NL Parser Tests
===============
Tests the `applyNLCommandRegex` logic by running a Python port of the
JavaScript function. Every previously-failing command gets a named test case
so regressions are caught immediately.

Run standalone:  python tests/test_nl_parser.py
Via runner:      python tests/test_runner.py
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tests.helpers import TestSuite
except ImportError:
    from helpers import TestSuite


# ── Shared constants (mirror JS constants in index.html) ─────────────────────

MONTHS = [
    "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
]

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

Q_MAP = {
    "q1": ["01", "02", "03"], "q2": ["04", "05", "06"],
    "q3": ["07", "08", "09"], "q4": ["10", "11", "12"],
}

PRODUCTS = [
    {"product_id": "FG001", "name": "Smart Home Hub Pro"},
    {"product_id": "FG002", "name": "Security Camera 4K"},
    {"product_id": "FG003", "name": "Smart Thermostat"},
    {"product_id": "FG004", "name": "Smart Doorbell"},
    {"product_id": "FG005", "name": "Smart Lock Premium"},
    {"product_id": "FG006", "name": "Energy Monitor"},
    {"product_id": "FG007", "name": "Smart Speaker"},
    {"product_id": "FG008", "name": "Home Gateway Pro"},
    {"product_id": "FG009", "name": "Motion Sensor Pack"},
    {"product_id": "FG010", "name": "Smart Plug 4-Pack"},
]

# Baseline stat forecast: 500 units per product×channel×month
STAT_FORECAST: dict = {}
for _p in PRODUCTS:
    for _ch in ["B2B", "B2C"]:
        for _m in MONTHS:
            STAT_FORECAST[f"{_p['product_id']}_{_ch}_{_m}"] = 500


# ── Python port of applyNLCommandRegex ───────────────────────────────────────

def clean_month_token(token: str) -> str:
    """Strip trailing year from token: 'Mar'25' → 'Mar', 'March 2025' → 'March'."""
    return re.sub(r"'?\s*\d{2,4}$", "", token).strip()


def resolve_months(token):
    if not token:
        return MONTHS[:]
    # "all" (standalone) → all months
    if re.match(r"^all$", token.strip(), re.I):
        return MONTHS[:]
    if re.search(r",|\band\b", token, re.I):
        parts = re.split(r"\s*,\s*|\s+and\s+", token.lower())
        result = []
        for p in parts:
            r = resolve_months(clean_month_token(p.strip()))
            if r:
                for m in r:
                    if m not in result:
                        result.append(m)
        return sorted(result) if result else None
    t = clean_month_token(token.lower().strip())
    if t in Q_MAP:
        return [m for m in MONTHS if m[5:7] in Q_MAP[t]]
    mm = MONTH_MAP.get(t)
    if mm:
        return [m for m in MONTHS if m[5:7] == mm]
    return None


def resolve_products(token):
    if not token:
        return [p["product_id"] for p in PRODUCTS]
    t = token.strip()
    # "all", "all products", "all items", "all FG", etc. → all products
    if re.match(r"^all\b", t, re.I) or t == '':
        return [p["product_id"] for p in PRODUCTS]
    up = t.upper()
    if any(p["product_id"] == up for p in PRODUCTS):
        return [up]
    partial = [p for p in PRODUCTS if p["name"] and t.lower() in p["name"].lower()]
    return [p["product_id"] for p in partial] if partial else None


def extract_channel(s):
    m = re.search(r"\b(B2B|B2C)\b", s, re.I)
    if m:
        rest = s[: m.start()] + s[m.end():]
        rest = re.sub(r"\b(in|for|channel)\b", "", rest, flags=re.I)
        return [m.group(1).upper()], re.sub(r"\s{2,}", " ", rest).strip()
    return ["B2B", "B2C"], s


def extract_product_suffix(s):
    m = re.search(r"\s+(?:for|from)\s+(\S+)\s*$", s, re.I)
    if m:
        prods = resolve_products(m.group(1))
        if prods:
            return prods, s[: s.rfind(m.group(0))].strip()
    return None, s


def extract_month(s):
    m = re.search(r"\b(?:in|for)\s+([\w',\s-]+?)(?:\s+(?:by|to)|$)", s, re.I)
    if m:
        tok = m.group(1).strip()
        if resolve_months(tok):
            cleaned = s[: m.start()] + s[m.end():]
            return tok, re.sub(r"\s{2,}", " ", cleaned).strip()
    tokens = s.strip().split()
    # Pass 1: real month names and quarters (higher priority than "all")
    for i, tok in enumerate(tokens):
        t = clean_month_token(tok)
        tl = t.lower()
        if MONTH_MAP.get(tl) or Q_MAP.get(tl):
            rest = " ".join(t2 for j, t2 in enumerate(tokens) if j != i).strip()
            return tok, rest
    # Pass 2: standalone "all" = all months (fallback, only if no real month found)
    for i, tok in enumerate(tokens):
        if tok.lower() == "all":
            rest = " ".join(t2 for j, t2 in enumerate(tokens) if j != i).strip()
            return "all", rest
    return None, s


def apply_nl_command(raw: str, manual_overrides: dict) -> tuple:
    """
    Parse and apply NL command.
    Returns (applied_count: int, message: str).
    Modifies `manual_overrides` in-place.
    """
    # Pre-process: strip noise words, normalise apostrophes
    txt = re.sub(r"\b(demand|forecast|sales|units|unit|volume|the|my|its|total|months|products?|items?|goods?|skus?|finished)\b", " ", raw, flags=re.I)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    txt = re.sub(r"[''`]", "'", txt)

    # ── Clear overrides ───────────────────────────────────────────────────────
    if re.search(r"clear(\s+all)?\s+(overrides?|forecast)", raw, re.I):
        count = len(manual_overrides)
        manual_overrides.clear()
        return 0, f"✓ Cleared {count} override(s)."

    applied = 0

    # ── Pattern A: "set/increase/… TARGET to VALUE" (absolute) ───────────────
    m = re.match(
        r"^(?:set|increase|raise|decrease|reduce|lower)\s+(.+?)\s+to\s+"
        r"(\d+(?:\.\d+)?)\s*(?:for\s+(\S+))?\s*$",
        txt, re.I
    )
    if m:
        target_str = m.group(1).strip()
        val = round(float(m.group(2)))
        trail_prod = resolve_products(m.group(3)) if m.group(3) else None

        channels2, target_str2 = extract_channel(target_str)
        month_tok2, target_str3 = extract_month(target_str2)

        products2 = trail_prod or resolve_products(target_str3)
        months2 = resolve_months(month_tok2) if month_tok2 else MONTHS[:]

        if not products2:
            return 0, f"⚠ Product not found: '{target_str3}'"
        if not months2:
            return 0, f"⚠ Month not recognised: '{month_tok2}'"

        for pid in products2:
            for ch in channels2:
                for mon in months2:
                    manual_overrides[f"{pid}_{ch}_{mon}"] = val
                    applied += 1
        return applied, f"✓ Set to {val} — applied to {applied} cell(s)."

    # ── Pattern B: "increase/decrease TARGET by X%" (relative) ───────────────
    m = re.match(
        r"^(increase|raise|decrease|reduce|lower)\s+(.+?)\s+by\s+"
        r"(\d+(?:\.\d+)?)\s*%(?:\s+for\s+(\S+))?\s*$",
        txt, re.I
    )
    if m:
        direction = 1 if re.match(r"increase|raise", m.group(1), re.I) else -1
        factor = 1 + direction * float(m.group(3)) / 100
        body = m.group(2).strip()
        trail_prod = resolve_products(m.group(4)) if m.group(4) else None

        channels, body2 = extract_channel(body)

        # "MONTH for PRODUCT" word order: "Mar'25 for FG001"
        for_m = re.match(r"^([\w']+)\s+for\s+(.+)$", body2, re.I)
        month_tok = None
        if for_m and resolve_months(for_m.group(1)):
            month_tok = for_m.group(1)
            body2 = for_m.group(2).strip()

        prod_from_suffix, body3 = extract_product_suffix(body2)
        work_body = body3

        month_tok2, body4 = extract_month(work_body)
        if not month_tok and month_tok2:
            month_tok = month_tok2

        # Strip orphan prepositions left after noise-word removal ("for FG001 in" → "FG001")
        clean_body = re.sub(r"\b(in|for)\b", " ", body4 or work_body, flags=re.I)
        clean_body = re.sub(r"\s{2,}", " ", clean_body).strip()

        products = trail_prod or prod_from_suffix or resolve_products(clean_body)
        months = resolve_months(month_tok) if month_tok else MONTHS[:]

        if not products:
            return 0, f"⚠ Product not found: '{clean_body}'"
        if not months:
            return 0, f"⚠ Month not recognised: '{month_tok}'"

        for pid in products:
            for ch in channels:
                for mon in months:
                    key = f"{pid}_{ch}_{mon}"
                    base = manual_overrides.get(key, STAT_FORECAST.get(key, 0))
                    manual_overrides[key] = max(0, round(base * factor))
                    applied += 1

        sign = "+" if direction > 0 else "-"
        return applied, f"✓ Applied {sign}{m.group(3)}% to {applied} cell(s)."

    return 0, "⚠ Not recognised."


# ── Test cases ────────────────────────────────────────────────────────────────

def run():
    t = TestSuite("NL Parser")

    # Each entry: (command, expected_applied, check_key, check_value)
    # check_value=None skips value assertion (used for "clear" or unknown expected value)

    cases = [
        # ── Regression cases (previously broken) ─────────────────────────────
        (
            "Increase Mar'25 demand by 20% for FG001",
            # PREVIOUSLY FAILED: smart apostrophe + year + noise word + "for PRODUCT"
            2, "FG001_B2B_2025-03", round(500 * 1.2),
        ),
        (
            "increase demand for FG001 in March by 20%",
            # PREVIOUSLY FAILED: noise word removal leaves orphan "for" prefix
            2, "FG001_B2B_2025-03", round(500 * 1.2),
        ),
        (
            "increase Mar'25 by 20% for FG001",
            # "MONTH for PRODUCT" word order
            2, "FG001_B2B_2025-03", round(500 * 1.2),
        ),
        (
            "increase FG001 in channel B2B in all months by 20%",
            # PREVIOUSLY FAILED: "months" not stripped, "all" not recognised as month token
            12, "FG001_B2B_2025-03", round(500 * 1.2),
        ),

        # ── Standard relative patterns ────────────────────────────────────────
        (
            "increase all in November by 10%",
            20, "FG003_B2C_2025-11", round(500 * 1.1),
        ),
        (
            "raise all B2B in Q4 by 10%",
            30, "FG005_B2B_2025-10", round(500 * 1.1),
        ),
        (
            "reduce FG003 by 5%",
            24, "FG003_B2B_2025-03", round(500 * 0.95),
        ),
        (
            "decrease FG009 B2C in September by 15%",
            1, "FG009_B2C_2025-09", round(500 * 0.85),
        ),
        (
            "increase FG002 in Q2 by 25%",
            6, "FG002_B2B_2025-04", round(500 * 1.25),
        ),
        (
            "increase all by 3%",
            240, "FG010_B2C_2026-02", round(500 * 1.03),
        ),
        (
            "raise FG007 november by 30%",
            # Month token without "in" prefix
            2, "FG007_B2B_2025-11", round(500 * 1.3),
        ),
        (
            "increase all in Q1 by 5%",
            60, "FG001_B2C_2025-03", round(500 * 1.05),
        ),

        # ── Absolute "set to" patterns ────────────────────────────────────────
        (
            "set FG001 B2B March to 500",
            1, "FG001_B2B_2025-03", 500,
        ),
        (
            "set all B2C in January to 1000",
            10, "FG001_B2C_2026-01", 1000,
        ),
        (
            "set FG001 B2B February to 800",
            # February = last horizon month (2026-02)
            1, "FG001_B2B_2026-02", 800,
        ),

        # ── "all months" variants ─────────────────────────────────────────────
        (
            "increase FG001 in channel B2B in all months by 20%",
            12, "FG001_B2B_2025-03", round(500 * 1.2),
        ),
        (
            "increase FG001 B2C all by 15%",
            12, "FG001_B2C_2025-03", round(500 * 1.15),
        ),

        # ── Partial product name ──────────────────────────────────────────────
        (
            "reduce thermostat by 10%",
            24, "FG003_B2B_2025-03", round(500 * 0.9),
        ),
        (
            "increase speaker in October by 20%",
            2, "FG007_B2B_2025-10", round(500 * 1.2),
        ),

        # ── Quarter expansion ─────────────────────────────────────────────────
        (
            "decrease all in Q3 by 8%",
            # Q3 = Jul+Aug+Sep → 3 months × 10 products × 2 channels = 60
            60, "FG001_B2C_2025-07", round(500 * 0.92),
        ),

        # ── Month name variants ───────────────────────────────────────────────
        (
            "increase FG004 in sep by 12%",
            2, "FG004_B2B_2025-09", round(500 * 1.12),
        ),
        (
            "raise FG006 in december by 5%",
            2, "FG006_B2B_2025-12", round(500 * 1.05),
        ),

        # ── Clear ─────────────────────────────────────────────────────────────
        (
            "clear all overrides",
            0, None, None,
        ),
        (
            "clear overrides",
            0, None, None,
        ),

        # ── "all products / all items / all FG" patterns (noise word gap) ───────
        (
            "increase all products in November by 10%",
            # "products" stripped → "all" → all 10 products × 2 channels = 20
            20, "FG001_B2B_2025-11", round(500 * 1.1),
        ),
        (
            "increase all items by 5%",
            # "items" stripped → "all" → all 240 cells
            240, "FG003_B2C_2026-02", round(500 * 1.05),
        ),
        (
            "raise all FG in Q4 by 10%",
            # "FG" not a product name/id → previously failed; now resolveProducts("all FG")
            # hits ^all\b → all products. 10 × 2 × 3 = 60
            60, "FG002_B2B_2025-10", round(500 * 1.1),
        ),
        (
            "decrease all finished goods in March by 5%",
            # "finished" + "goods" both stripped → "all" → 20 cells
            20, "FG005_B2C_2025-03", round(500 * 0.95),
        ),

        # ── Unrecognised → graceful error ─────────────────────────────────────
        (
            "gibberish command here",
            0, None, None,
        ),
        (
            "increase",
            0, None, None,
        ),
    ]

    t.section("Regression cases (previously failing commands)")
    regression_cmds = [
        "Increase Mar'25 demand by 20% for FG001",
        "increase demand for FG001 in March by 20%",
        "increase Mar'25 by 20% for FG001",
        "increase all products in November by 10%",
        "raise all FG in Q4 by 10%",
        "decrease all finished goods in March by 5%",
    ]

    t.section("All test cases")
    for cmd, exp_applied, exp_key, exp_val in cases:
        overrides = {}
        applied, msg = apply_nl_command(cmd, overrides)

        ok_applied = applied == exp_applied
        ok_val = True
        if exp_key and exp_val is not None:
            ok_val = overrides.get(exp_key) == exp_val

        label = f"{cmd!r:.55s}"
        detail = ""
        if not ok_applied:
            detail = f"applied: got {applied}, expected {exp_applied}"
        elif not ok_val:
            detail = f"{exp_key}: got {overrides.get(exp_key)}, expected {exp_val}"

        t.check(label, ok_applied and ok_val, detail)

    # ── Compound: chain of commands mutates state correctly ───────────────────
    t.section("State mutation chain")
    overrides = {}

    apply_nl_command("set FG001 B2B March to 1000", overrides)
    t.check("set creates override", overrides.get("FG001_B2B_2025-03") == 1000)

    apply_nl_command("increase FG001 B2B March by 10%", overrides)
    t.check("relative increase uses existing override as base",
            overrides.get("FG001_B2B_2025-03") == round(1000 * 1.1))

    apply_nl_command("clear all overrides", overrides)
    t.check("clear empties all overrides", len(overrides) == 0)

    apply_nl_command("increase all in November by 20%", overrides)
    t.check("after clear, relative increase uses stat FC base",
            overrides.get("FG001_B2B_2025-11") == round(500 * 1.2))

    # ── cleanMonthToken edge cases ────────────────────────────────────────────
    t.section("cleanMonthToken edge cases")
    cases_ct = [
        ("Mar'25",   "Mar"),
        ("March'25", "March"),
        ("jan25",    "jan"),
        ("Nov 2025", "Nov"),
        ("dec",      "dec"),
        ("Q4",       "Q4"),
    ]
    for raw, expected in cases_ct:
        result = clean_month_token(raw)
        t.check(f"cleanMonthToken({raw!r}) → {expected!r}", result == expected,
                f"got {result!r}")

    # ── resolveMonths edge cases ──────────────────────────────────────────────
    t.section("resolveMonths edge cases")
    rm_cases = [
        ("march",     ["2025-03"]),
        ("mar",       ["2025-03"]),
        ("Mar'25",    ["2025-03"]),
        # Q1 = calendar months Jan/Feb/Mar (01,02,03)
        # In this horizon (2025-03→2026-02): Mar-25 + Jan-26 + Feb-26
        ("q1",        ["2025-03", "2026-01", "2026-02"]),
        ("Q4",        ["2025-10", "2025-11", "2025-12"]),
        ("november",  ["2025-11"]),
        ("feb",       ["2026-02"]),          # only horizon month with Feb
        ("january",   ["2026-01"]),
        ("july, august and september", ["2025-07", "2025-08", "2025-09"]),
        ("all",       MONTHS),               # "all" → all 12
        ("All",       MONTHS),               # case-insensitive
        ("",          MONTHS),               # empty → all 12
        (None,        MONTHS),               # None → all 12
    ]
    for tok, expected in rm_cases:
        result = resolve_months(tok)
        t.check(f"resolveMonths({tok!r})", result == expected,
                f"got {result}, expected {expected}")

    t.summary()
    return t.results()


if __name__ == "__main__":
    passed, failed, errors = run()
    for e in errors:
        print(f"\n  DETAIL: {e}")
    sys.exit(0 if failed == 0 else 1)
