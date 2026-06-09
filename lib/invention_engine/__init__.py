"""kids-ai 発明レシピ生成エンジン (v0.3, flat execution model)

設計正本: ~/knowledge-index/projects/kids-ai-programming-mode/engine/schemas/

公開API:
    load_catalog(path=None)       -> dict
    load_inventory(path=None)     -> dict
    load_templates(path=None)     -> dict
    load_recipe(path)             -> dict
    validate_structure(recipe)    -> None | raises jsonschema.ValidationError
    validate_semantic(recipe, catalog, inventory=None) -> ValidationResult
    suggest_from_inventory(inventory, catalog, templates, child_id, interests=None) -> list

スキーマ正本: jsonschema Draft-07
"""
from .loader import (
    load_catalog,
    load_inventory,
    load_templates,
    load_recipe,
    SCHEMA_DIR,
)
from .validator import (
    validate_structure,
    validate_semantic,
    ValidationResult,
    MAX_RULES,
    MAX_ACTIONS_PER_RULE,
    MAX_DELAY_MS,
    USB_BUDGET_MA,
    CHILD_SAFE_GPIO,
)
from .suggest import suggest_from_inventory

__all__ = [
    "load_catalog",
    "load_inventory",
    "load_templates",
    "load_recipe",
    "SCHEMA_DIR",
    "validate_structure",
    "validate_semantic",
    "ValidationResult",
    "suggest_from_inventory",
    "MAX_RULES",
    "MAX_ACTIONS_PER_RULE",
    "MAX_DELAY_MS",
    "USB_BUDGET_MA",
    "CHILD_SAFE_GPIO",
]
