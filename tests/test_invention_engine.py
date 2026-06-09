"""kids-ai 発明レシピエンジン v0.3 スモークテスト

期待値の根拠:
- README 検証済欄: ドラゴンアラームは意味バリデータで PASS
  (電流45mA < USB 500mA、needs_adult=True、max_safe_level=3、エラーなし)
- 構造検証は同梱 3 データセット (catalog/inventory/sample recipe) 全部通過
- 在庫サンプルで suggest_from_inventory が >=1 件返る

Usage:
    python3 ~/kids-ai/tests/test_invention_engine.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "lib"
sys.path.insert(0, str(LIB))

from invention_engine import (  # noqa: E402
    load_catalog,
    load_inventory,
    load_templates,
    load_recipe,
    validate_structure,
    validate_semantic,
    suggest_from_inventory,
    SCHEMA_DIR,
)


def test_load_catalog():
    cat = load_catalog()
    assert cat["schema_version"].startswith("0.3"), cat["schema_version"]
    parts = cat["parts"]
    assert len(parts) >= 8, f"parts count {len(parts)}"
    ids = {p["part_id"] for p in parts}
    for required in ("pico2w", "button", "single_led", "active_buzzer", "vl53l0x_distance"):
        assert required in ids, f"missing required part {required}"
    return cat


def test_load_inventory(cat):
    inv = load_inventory()
    assert inv["schema_version"].startswith("0.3")
    for pid in inv["stock"]:
        # 在庫に登録された part_id はカタログにあること（必須ではないがサンプルでは整合）
        cat_ids = {p["part_id"] for p in cat["parts"]}
        if pid not in cat_ids:
            print(f"  [warn] inventory has {pid} not in catalog (sample tolerance)")
    return inv


def test_load_templates():
    tpls = load_templates()
    assert len(tpls["templates"]) == 10, f"templates count {len(tpls['templates'])}"
    return tpls


def test_dragon_alarm(cat, inv):
    recipe_path = SCHEMA_DIR / "sample_recipe_dsl_v03.json"
    rcp = load_recipe(recipe_path)
    assert rcp["recipe_id"] == "dragon_alarm_screen_first"

    # 構造検証 (load_recipe 内部で通過済だが二重で念のため)
    validate_structure(rcp)

    # 意味検証
    result = validate_semantic(rcp, cat, inventory=inv)
    print(f"  dragon_alarm: ok={result.ok}, "
          f"current_typ={result.current_typ_ma}mA peak={result.current_peak_ma}mA, "
          f"needs_adult={result.needs_adult}, max_safe_level={result.max_safe_level}")
    print(f"  errors: {result.errors}")
    print(f"  warnings: {result.warnings}")

    assert result.ok, f"dragon_alarm should pass: errors={result.errors}"
    assert result.needs_adult is True, "dragon_alarm needs_adult should be True"
    assert result.max_safe_level == 3, f"max_safe_level expected 3, got {result.max_safe_level}"
    return result


def test_templates_all_structurally_resolvable(cat, tpls):
    """テンプレ自体は recipe DSL ではないが、required_parts がカタログに居ることを確認。"""
    cat_ids = {p["part_id"] for p in cat["parts"]}
    for tpl in tpls["templates"]:
        for need in tpl["required_parts"]:
            assert need["part_id"] in cat_ids, (
                f"template {tpl['template_id']} requires unknown part {need['part_id']}"
            )


def test_suggest_child1(cat, inv, tpls):
    suggestions = suggest_from_inventory(inv, cat, tpls, child_id="child1",
                                          interests=["adventure", "alarm"])
    print(f"  suggest(child1): {len(suggestions)} hits → "
          f"{[t['template_id'] for t in suggestions]}")
    assert len(suggestions) >= 1, "should return at least 1 template for child1"
    return suggestions


def test_negative_unknown_part(cat):
    """カタログ未登録の部品を入れるとエラーが立つ。"""
    bad_recipe = {
        "recipe_schema_version": "0.3-draft",
        "recipe_id": "bad_test",
        "title_kids": "テスト",
        "child_id": "child1",
        "mode": "original_invention",
        "origin": "original_invention",
        "dsl_version": "0.3",
        "components": [
            {"part_id": "ghost_part", "instance_id": "x"},
        ],
        "rules": [
            {
                "rule_id": "r1",
                "when": {"sensor": "x", "event": "press"},
                "then": {"parallel": [{"actuator": "x", "action": "on"}]},
            }
        ],
        "validation_policy": {
            "max_rules": 5, "max_actions_per_rule": 5, "max_delay_ms": 10000,
            "execution_model": "flat", "nesting_allowed": False,
            "no_unbounded_loop": True, "no_arbitrary_code": True,
            "program_must_validate_actions": True,
        },
        "send_to_pico_gate": {
            "requires_photo_check": False, "requires_adult_approval": False,
            "usb_state_required": "off", "power_state_required": "unchecked",
        },
    }
    validate_structure(bad_recipe)  # 構造は通る
    result = validate_semantic(bad_recipe, cat)
    assert not result.ok, "unknown part should fail semantic validation"
    assert any("未登録の部品" in e for e in result.errors), (
        f"expected 未登録の部品 error, got {result.errors}"
    )


def test_negative_nested_then(cat):
    """parallel と sequence 同時定義は構造検証で落ちる。"""
    import jsonschema
    bad = {
        "recipe_schema_version": "0.3-draft",
        "recipe_id": "bad_nest",
        "title_kids": "テスト",
        "child_id": "child1",
        "mode": "original_invention",
        "origin": "original_invention",
        "dsl_version": "0.3",
        "components": [{"part_id": "button", "instance_id": "b1"}],
        "rules": [
            {
                "rule_id": "r1",
                "when": {"sensor": "b1", "event": "press"},
                "then": {
                    "parallel": [{"actuator": "b1", "action": "press"}],
                    "sequence": [{"actuator": "b1", "action": "press"}],
                },
            }
        ],
        "validation_policy": {
            "max_rules": 5, "max_actions_per_rule": 5, "max_delay_ms": 10000,
            "execution_model": "flat", "nesting_allowed": False,
            "no_unbounded_loop": True, "no_arbitrary_code": True,
            "program_must_validate_actions": True,
        },
        "send_to_pico_gate": {
            "requires_photo_check": False, "requires_adult_approval": False,
            "usb_state_required": "off", "power_state_required": "unchecked",
        },
    }
    try:
        validate_structure(bad)
    except jsonschema.ValidationError:
        return
    raise AssertionError("parallel+sequence both present should fail structure validation")


def test_negative_missing_origin(cat):
    """origin 未指定の recipe は構造検証で落ちる（v0.3+ で origin required）。"""
    import jsonschema
    bad = {
        "recipe_schema_version": "0.3-draft",
        "recipe_id": "bad_no_origin",
        "title_kids": "テスト",
        "child_id": "child1",
        "mode": "original_invention",
        # origin が無い
        "components": [{"part_id": "button", "instance_id": "b1"}],
        "rules": [
            {
                "rule_id": "r1",
                "when": {"sensor": "b1", "event": "press"},
                "then": {"parallel": [{"actuator": "b1", "action": "press"}]},
            }
        ],
        "validation_policy": {
            "max_rules": 5, "max_actions_per_rule": 5, "max_delay_ms": 10000,
            "execution_model": "flat", "nesting_allowed": False,
            "no_unbounded_loop": True, "no_arbitrary_code": True,
            "program_must_validate_actions": True,
        },
        "send_to_pico_gate": {
            "requires_photo_check": False, "requires_adult_approval": False,
            "usb_state_required": "off", "power_state_required": "unchecked",
        },
    }
    try:
        validate_structure(bad)
    except jsonschema.ValidationError as e:
        assert "origin" in str(e), f"expected 'origin' in error message, got: {e}"
        return
    raise AssertionError("recipe without origin should fail structure validation")


def test_compute_alternatives_priorities():
    """compute_alternatives が保護者方針通りに動くかの単独テスト（v0.3+）。"""
    from invention_engine.validator import compute_alternatives

    # 仮の parts: peak 合計 700mA > USB 500mA
    # - pico2w: 80mA (controller, safe_level 3)
    # - sensor1: 5mA (input, safe_level 2)
    # - heavy_output: 600mA peak (output, safe_level 4)  ← 外せば safety_drop 大
    # - medium_output: 100mA peak (output, safe_level 3)
    # - light_output: 20mA peak (output, safe_level 2)
    parts = [
        {"part_id": "pico2w", "role": "controller", "safe_level": 3, "current_draw_ma_typ": 80},
        {"part_id": "sensor1", "role": "input", "safe_level": 2, "current_draw_ma_typ": 5},
        {"part_id": "heavy_output", "role": "output", "safe_level": 4, "current_draw_ma_peak": 600},
        {"part_id": "medium_output", "role": "output", "safe_level": 3, "current_draw_ma_peak": 100},
        {"part_id": "light_output", "role": "output", "safe_level": 2, "current_draw_ma_peak": 20},
    ]
    inventory = {"stock": {
        "pico2w": 1, "sensor1": 1,
        "heavy_output": 1, "medium_output": 1, "light_output": 1,
    }}

    alternatives = compute_alternatives(parts, budget_ma=500, catalog_index={}, inventory=inventory)
    print(f"  alternatives: {len(alternatives)}件返却")
    for a in alternatives:
        print(f"    keep={a['keep']} dropped={a['dropped']} "
              f"peak={a['peak_ma']}mA safe={a['max_safe_level']} "
              f"drop={a['safety_drop']} inv_ok={a['inventory_ok']}")

    # 最大3件
    assert len(alternatives) <= 3, f"max 3件のはずが {len(alternatives)} 件返った"
    assert len(alternatives) >= 1, "少なくとも1件は返るはず（heavy_output外せば峰内）"

    # 1位は危険度が下がる構成（heavy_output safe_level=4 を外した構成）
    top = alternatives[0]
    assert "heavy_output" in top["dropped"], (
        f"top候補は heavy_output を外すはず: dropped={top['dropped']}"
    )
    assert top["safety_drop"] >= 1, f"safety_drop >= 1 を期待: {top['safety_drop']}"
    assert top["peak_ma"] <= 500, f"peak <= 500 を期待: {top['peak_ma']}"


def main():
    print("== test_load_catalog ==")
    cat = test_load_catalog()
    print(f"  OK: {len(cat['parts'])} parts, schema v{cat['schema_version']}")

    print("== test_load_inventory ==")
    inv = test_load_inventory(cat)
    print(f"  OK: {len(inv['stock'])} items in stock")

    print("== test_load_templates ==")
    tpls = test_load_templates()
    print(f"  OK: {len(tpls['templates'])} templates")

    print("== test_dragon_alarm (sample recipe semantic) ==")
    test_dragon_alarm(cat, inv)
    print("  OK")

    print("== test_templates_all_structurally_resolvable ==")
    test_templates_all_structurally_resolvable(cat, tpls)
    print(f"  OK: all 10 templates reference catalog parts only")

    print("== test_suggest_child1 ==")
    test_suggest_child1(cat, inv, tpls)
    print("  OK")

    print("== test_negative_unknown_part ==")
    test_negative_unknown_part(cat)
    print("  OK (semantic rejected unknown part)")

    print("== test_negative_nested_then ==")
    test_negative_nested_then(cat)
    print("  OK (structure rejected parallel+sequence both)")

    print("== test_negative_missing_origin ==")
    test_negative_missing_origin(cat)
    print("  OK (structure rejected missing origin)")

    print("== test_compute_alternatives_priorities ==")
    test_compute_alternatives_priorities()
    print("  OK (alternatives sorted by safety/parts/inventory priorities)")

    print("\nall tests passed ✅")


if __name__ == "__main__":
    main()
