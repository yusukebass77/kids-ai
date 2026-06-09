"""kids-ai configuration loader.

Reads config/children.json (your real, git-ignored config). Falls back to
config/children.example.json so the project runs out-of-the-box for a demo.

Exposes the child roster and assistant name to the rest of the app so that no
personal data (names, grades, etc.) is ever hard-coded in source.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE / "config"


@lru_cache(maxsize=1)
def _load() -> dict:
    real = CONFIG_DIR / "children.json"
    example = CONFIG_DIR / "children.example.json"
    path = real if real.exists() else example
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def assistant_name() -> str:
    return _load().get("assistant_name", "あい")


def timezone() -> str:
    return _load().get("timezone", "Asia/Tokyo")


def children() -> list[dict]:
    return _load().get("children", [])


def child_ids() -> tuple[str, ...]:
    return tuple(c["id"] for c in children())


def child_names() -> dict[str, str]:
    return {c["id"]: c.get("display_name", c["id"]) for c in children()}


def furigana_ids() -> tuple[str, ...]:
    return tuple(c["id"] for c in children() if c.get("furigana"))


def grade(child_id: str) -> int | None:
    for c in children():
        if c["id"] == child_id:
            return c.get("grade")
    return None


def default_child_id() -> str:
    ids = child_ids()
    return ids[0] if ids else "child1"
