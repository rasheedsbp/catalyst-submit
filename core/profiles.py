"""Journal profile registry + fuzzy name matching.

A profile is a JSON file in profiles/. Adding a new journal = adding one file,
no code change.
"""
from __future__ import annotations

import json
import difflib
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


class Profile(dict):
    @property
    def id(self) -> str:
        return self["id"]

    @property
    def name(self) -> str:
        return self["name"]


def load_all() -> list[Profile]:
    out = []
    for p in sorted(PROFILE_DIR.glob("*.json")):
        with open(p) as f:
            d = json.load(f)
        d["_file"] = str(p)
        out.append(Profile(d))
    return out


def resolve(query: str, cutoff: float = 0.55) -> tuple[Profile | None, list[tuple[str, float]]]:
    """Return (best profile or None, ranked candidate list)."""
    profiles = load_all()
    q = _norm(query)
    scored: list[tuple[Profile, float]] = []
    for prof in profiles:
        names = [prof["name"]] + prof.get("aliases", [])
        best = max(difflib.SequenceMatcher(None, q, _norm(n)).ratio() for n in names)
        # substring bonus: "access" -> "IEEE Access"
        if any(q and q in _norm(n) for n in names):
            best = max(best, 0.9)
        scored.append((prof, best))
    scored.sort(key=lambda t: -t[1])
    ranked = [(p["name"], round(s, 2)) for p, s in scored[:6]]
    if scored and scored[0][1] >= cutoff:
        return scored[0][0], ranked
    return None, ranked


def get(profile_id: str) -> Profile:
    for p in load_all():
        if p["id"] == profile_id:
            return p
    raise KeyError(profile_id)
