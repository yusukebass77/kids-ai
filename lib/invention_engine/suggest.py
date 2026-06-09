"""kids-ai 手持ち提案モード

設計正本: validator_pseudocode.py の suggest_from_inventory()
"""
from __future__ import annotations

from typing import Any


def suggest_from_inventory(
    inventory: dict[str, Any],
    catalog: dict[str, Any],
    templates: dict[str, Any],
    child_id: str,
    interests: list[str] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """在庫で作れるテンプレだけを通し、子ども最適化スコアで上位 limit 件返す。"""
    stock = inventory["stock"]
    catalog_index = {p["part_id"]: p for p in catalog["parts"]}
    available: list[tuple[int, dict[str, Any]]] = []

    for tpl in templates["templates"]:
        ok = True
        for need in tpl["required_parts"]:
            pid = need["part_id"]
            qty = need.get("qty", 1)
            if stock.get(pid, 0) < qty:
                ok = False
                break
            if catalog_index.get(pid, {}).get("phase_locked", False):
                ok = False
                break
        if not ok:
            continue

        score = 0
        if child_id in tpl.get("recommended_for", []):
            score += 2
        if interests:
            score += len(set(interests) & set(tpl.get("interest_tags", [])))
        available.append((score, tpl))

    available.sort(key=lambda x: x[0], reverse=True)
    return [tpl for _, tpl in available[:limit]]
