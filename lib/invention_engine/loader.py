"""kids-ai 発明レシピエンジン: スキーマ/データのロード層

スキーマ正本: ~/knowledge-index/projects/kids-ai-programming-mode/engine/schemas/
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path.home() / "knowledge-index" / "projects" / "kids-ai-programming-mode" / "engine" / "schemas"

DEFAULT_CATALOG_PATH = SCHEMA_DIR / "parts_catalog_v03.json"
DEFAULT_INVENTORY_PATH = SCHEMA_DIR / "inventory_sample.json"
DEFAULT_TEMPLATES_PATH = SCHEMA_DIR / "recipe_templates_v03.json"

CATALOG_SCHEMA_PATH = SCHEMA_DIR / "parts_catalog.schema.json"
INVENTORY_SCHEMA_PATH = SCHEMA_DIR / "inventory.schema.json"
RECIPE_SCHEMA_PATH = SCHEMA_DIR / "recipe_dsl.schema.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing JSON: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalog(path: str | Path | None = None) -> dict[str, Any]:
    """parts_catalog をロードする。jsonschema による構造検証を必ず通す。"""
    p = Path(path) if path else DEFAULT_CATALOG_PATH
    data = _read_json(p)
    _validate_against(p, data, CATALOG_SCHEMA_PATH)
    return data


def load_inventory(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_INVENTORY_PATH
    data = _read_json(p)
    _validate_against(p, data, INVENTORY_SCHEMA_PATH)
    return data


def load_templates(path: str | Path | None = None) -> dict[str, Any]:
    """recipe_templates は専用スキーマを持たないため、構造的最低限のみチェック。"""
    p = Path(path) if path else DEFAULT_TEMPLATES_PATH
    data = _read_json(p)
    if "templates" not in data or not isinstance(data["templates"], list):
        raise ValueError(f"templates file missing 'templates' list: {p}")
    for t in data["templates"]:
        for key in ("template_id", "title_kids", "required_parts"):
            if key not in t:
                raise ValueError(f"template missing key '{key}': {t.get('template_id', t)}")
    return data


def load_recipe(path: str | Path) -> dict[str, Any]:
    """recipe(DSL) をロード。jsonschema 構造検証を通す。意味検証は validator.validate_semantic で別途。"""
    p = Path(path)
    data = _read_json(p)
    _validate_against(p, data, RECIPE_SCHEMA_PATH)
    return data


def _validate_against(data_path: Path, data: dict[str, Any], schema_path: Path) -> None:
    """jsonschema で data を schema に照らす。エラー時は読みやすく整形した RuntimeError を投げる。"""
    try:
        import jsonschema
    except ImportError as e:
        raise RuntimeError("jsonschema が必要: pip install jsonschema") from e

    schema = _read_json(schema_path)
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise jsonschema.ValidationError(
            f"[{data_path.name}] schema={schema_path.name}: {e.message}\n"
            f"  path: {'.'.join(str(p) for p in e.absolute_path) or '(root)'}\n"
            f"  validator: {e.validator}={e.validator_value}"
        ) from e
