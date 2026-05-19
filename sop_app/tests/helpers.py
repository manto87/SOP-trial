"""
Shared test helpers — HTTP client, assertion utilities, pretty output.
All test modules import from here.
"""

import re
import sys
import json
import urllib.request
import urllib.error
from typing import Any, List, Optional, Tuple

BASE_URL = "http://localhost:5050"

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get(path: str, timeout: int = 15) -> Any:
    url = BASE_URL + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise AssertionError(f"GET {path} → HTTP {e.code}: {body}")


def post(path: str, data: Any = None, timeout: int = 20, retries: int = 3) -> Any:
    import time
    body = json.dumps(data or {}).encode()
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(
            BASE_URL + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
            is_lock = (e.code == 500 and (
                "database is locked" in body_text
                or "OperationalError" in body_text
                or "SQLITE_BUSY" in body_text
            ))
            if is_lock and attempt < retries - 1:
                time.sleep(8)   # wait for plan run / DB lock to release
                last_err = body_text
                continue
            raise AssertionError(f"POST {path} → HTTP {e.code}: {body_text[:200]}")
    raise AssertionError(f"POST {path} → DB still locked after {retries} attempts")


# ── Format validators ─────────────────────────────────────────────────────────

def is_yyyy_mm(s: str) -> bool:
    """Exact YYYY-MM format — 7 chars, no day component."""
    return bool(re.match(r"^\d{4}-\d{2}$", str(s)))


def bad_months(rows: list, field: str = "month") -> List[str]:
    """Return any month values that are NOT in YYYY-MM format."""
    return [r.get(field, "") for r in rows if not is_yyyy_mm(r.get(field, ""))]


# ── Assertion + reporter ──────────────────────────────────────────────────────

class TestSuite:
    """Collects pass/fail results for a named module."""

    def __init__(self, name: str, verbose: bool = True):
        self.name = name
        self.verbose = verbose
        self._passed: List[str] = []
        self._failed: List[Tuple[str, str]] = []

    # ── core assertion ────────────────────────────────────────────────────────

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self._passed.append(name)
            if self.verbose:
                print(f"    \033[92m✓\033[0m  {name}")
        else:
            self._failed.append((name, detail))
            msg = f"    \033[91m✗\033[0m  {name}"
            if detail:
                msg += f"  →  {detail}"
            if self.verbose:
                print(msg)

    # ── convenience assertions ────────────────────────────────────────────────

    def eq(self, name: str, actual, expected, fmt: str = ""):
        detail = fmt or f"expected {expected!r}, got {actual!r}"
        self.check(name, actual == expected, detail if actual != expected else "")

    def gt(self, name: str, actual, threshold, unit: str = ""):
        u = f" {unit}" if unit else ""
        self.check(name, actual > threshold, f"got {actual}{u}, threshold > {threshold}{u}")

    def gte(self, name: str, actual, threshold, unit: str = ""):
        u = f" {unit}" if unit else ""
        self.check(name, actual >= threshold, f"got {actual}{u}, threshold >= {threshold}{u}")

    def contains(self, name: str, container, item):
        self.check(name, item in container, f"{item!r} not in {type(container).__name__}")

    def has_key(self, name: str, d: dict, key: str):
        self.check(name, key in d, f"missing key '{key}' in {list(d.keys())[:6]}")

    def all_yyyy_mm(self, name: str, rows: list, field: str = "month"):
        bad = bad_months(rows, field)
        self.check(name, len(bad) == 0,
                   f"bad format: {bad[:3]}" if bad else "")

    def section(self, title: str):
        if self.verbose:
            print(f"\n  \033[1m{title}\033[0m")

    # ── results ──────────────────────────────────────────────────────────────

    def results(self) -> Tuple[int, int, List[str]]:
        errors = [f"{name}{' — '+det if det else ''}"
                  for name, det in self._failed]
        return len(self._passed), len(self._failed), errors

    def summary(self):
        p, f, _ = self.results()
        total = p + f
        if f == 0:
            print(f"\n  \033[92m✓ All {total} checks passed\033[0m")
        else:
            print(f"\n  \033[91m✗ {f}/{total} checks failed\033[0m")
