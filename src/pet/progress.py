"""Completion metrics for a reconstruction project."""

from __future__ import annotations

from collections import Counter

from .db import DONE_STATES

SCORE = {
    "open": 0,
    "draft": 25,
    "compiles": 55,
    "tested": 80,
    "assumption": 50,
    "blocked": 0,
    "verified": 100,
}

WEIGHT = {
    "open": 0.0,
    "draft": 0.2,
    "compiles": 0.4,
    "tested": 0.8,
    "verified": 1.0,
    "assumption": 0.6,
    "blocked": 0.0,
}


def score(unit) -> int:
    """0-100 indicator for the table: state base plus attached Evidence."""
    base = SCORE.get(unit["state"], 0)
    if base >= 100:
        return 100
    try:
        extra = min(10, 2 * len(__import__("json").loads(unit["evidence"] or "[]")))
    except ValueError:
        extra = 0
    return min(99, base + extra)


def summarize(units) -> dict:
    states = Counter(unit["state"] for unit in units)
    total = len(units)
    weight = sum(WEIGHT.get(unit["state"], 0.0) for unit in units)
    size = sum(unit["size"] or 1 for unit in units)
    weighted_size = sum((unit["size"] or 1) * WEIGHT.get(unit["state"], 0.0) for unit in units)
    verified = sum(1 for unit in units if unit["state"] in DONE_STATES)
    assumptions = states.get("assumption", 0)
    blocked = states.get("blocked", 0)
    return {
        "total": total,
        "verified": verified,
        "assumptions": assumptions,
        "blocked": blocked,
        "states": dict(states),
        "percent_units": 100.0 * weight / total if total else 0.0,
        "percent_bytes": 100.0 * weighted_size / size if size else 0.0,
        "evidence": sum(len(_json(unit["evidence"])) for unit in units),
    }


def _json(text: str) -> list:
    import json
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except ValueError:
        return []
