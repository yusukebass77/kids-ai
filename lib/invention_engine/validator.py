"""kids-ai 発明レシピエンジン: 検証層

- validate_structure: jsonschema による recipe_dsl.schema.json 構造検証
- validate_semantic: 意味検証（catalog参照／在庫／GPIO数／I2C衝突／消費電流／安全性）

設計正本: ~/knowledge-index/projects/kids-ai-programming-mode/engine/schemas/validator_pseudocode.py
本実装は疑似コードを Python に落とし込んだもの。安全に関わる判定は全部ここ（プログラム）でやる。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# ============================================================
# 定数（validation_policy と一致させること）
# ============================================================
MAX_RULES = 5
MAX_ACTIONS_PER_RULE = 5
MAX_DELAY_MS = 10000
USB_BUDGET_MA = 500
CHILD_SAFE_GPIO = 10


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    needs_adult: bool = False
    max_safe_level: int = 1
    current_typ_ma: float = 0.0
    current_peak_ma: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "alternatives": list(self.alternatives),
            "needs_adult": self.needs_adult,
            "max_safe_level": self.max_safe_level,
            "current_typ_ma": self.current_typ_ma,
            "current_peak_ma": self.current_peak_ma,
        }


def validate_structure(recipe: dict[str, Any]) -> None:
    """jsonschema による DSL 構造検証。エラー時は jsonschema.ValidationError を投げる。"""
    from .loader import RECIPE_SCHEMA_PATH, _read_json

    import jsonschema

    schema = _read_json(RECIPE_SCHEMA_PATH)
    jsonschema.validate(instance=recipe, schema=schema)


def _peak_ma(part: dict[str, Any]) -> float:
    return part.get(
        "current_draw_ma_peak",
        part.get("current_draw_ma_max", part.get("current_draw_ma_typ", 0)),
    )


def _sum_gpio_pins(parts: list[dict[str, Any]]) -> int:
    """各部品の pins_required を合計。i2c の sda/scl も別 GPIO として数える。
    値が文字列（"220ohm_required"等）の場合は GPIO 消費にカウントしない。
    gnd や resistor 系のキーも GPIO ではないので除外する。
    """
    NON_GPIO_KEYS = {"gnd", "vcc", "gnd_or_vcc", "resistor"}
    total = 0
    for p in parts:
        pins = p.get("pins_required") or {}
        if p.get("interface") == "i2c":
            # i2c は sda/scl の 2 ピン固定（共有可能だが安全側で1部品あたり0.5換算でなく2加算しない方針）
            # → kids-ai 設計では「共有バス1本」として全 i2c 合算で 2 を別途加算する方針もあるが、
            #   ここでは最初の i2c 部品に 2、それ以降は 0 とする近似ロジックを後段で実施。
            continue
        for key, val in pins.items():
            if key in NON_GPIO_KEYS:
                continue
            if isinstance(val, int):
                total += val
            # 文字列は GPIO ではないのでスキップ
    # i2c のバス用 GPIO（sda/scl）は最大 1 セット = 2 ピンを共有として加算
    if any(p.get("interface") == "i2c" for p in parts):
        total += 2
    return total


def validate_semantic(
    recipe: dict[str, Any],
    catalog: dict[str, Any],
    inventory: dict[str, Any] | None = None,
) -> ValidationResult:
    """疑似コードに忠実な意味検証実装。"""
    res = ValidationResult(ok=True)
    catalog_index = {p["part_id"]: p for p in catalog["parts"]}

    # --------------------------------------------------------
    # STEP 1: コンポーネント検証
    # --------------------------------------------------------
    instance_map: dict[str, dict[str, Any]] = {}
    for comp in recipe["components"]:
        pid = comp["part_id"]
        iid = comp["instance_id"]
        if pid not in catalog_index:
            res.errors.append(f"未登録の部品: {pid}。カタログにある部品しか使えない。")
            continue
        part = catalog_index[pid]
        if iid in instance_map:
            res.errors.append(f"instance_id の重複: {iid}")
        instance_map[iid] = part
        if part.get("phase_locked", False):
            res.errors.append(
                f"{part['name_kids']} は今はまだ使えない（Picoと大人チェックのあと）。"
            )

    count_by_pid = Counter(comp["part_id"] for comp in recipe["components"])
    for pid, n in count_by_pid.items():
        if pid in catalog_index:
            cap = catalog_index[pid].get("max_count", 1)
            if n > cap:
                res.errors.append(f"{pid} を {n} 個使っているが上限は {cap} 個。")

    if inventory is not None:
        stock = inventory["stock"]
        for pid, n in count_by_pid.items():
            have = stock.get(pid, 0)
            if n > have:
                res.errors.append(f"{pid} は家に {have} 個しかない（レシピは {n} 個要求）。")

    # --------------------------------------------------------
    # STEP 2: ルール / DSL 検証
    # --------------------------------------------------------
    rules = recipe.get("rules", [])
    if len(rules) > MAX_RULES:
        res.errors.append(f"ルールが多すぎる（{len(rules)} > {MAX_RULES}）。")

    for rule in rules:
        rid = rule["rule_id"]
        w = rule["when"]
        sensor = instance_map.get(w["sensor"])
        if sensor is None:
            res.errors.append(f"{rid}: 存在しないセンサー {w['sensor']}")
        else:
            if sensor.get("role") != "input":
                res.errors.append(f"{rid}: {w['sensor']} は入力部品ではない。")
            events = sensor.get("role_events", {})
            if w["event"] not in events:
                res.errors.append(
                    f"{rid}: {sensor['part_id']} にイベント {w['event']} は無い。"
                )
            else:
                ev_def = events[w["event"]]
                param = ev_def.get("param")
                if param and param in w:
                    val = w[param]
                    lo, hi = ev_def.get("min"), ev_def.get("max")
                    if lo is not None and val < lo:
                        res.errors.append(
                            f"{rid}: {param}={val} は小さすぎる（最小 {lo}）。"
                        )
                    if hi is not None and val > hi:
                        res.errors.append(
                            f"{rid}: {param}={val} は大きすぎる（最大 {hi}）。"
                        )

        then = rule["then"]
        has_par = "parallel" in then
        has_seq = "sequence" in then
        if has_par == has_seq:
            res.errors.append(
                f"{rid}: then は parallel か sequence のどちらか一方だけにする（ネスト・併記は不可）。"
            )
            continue
        steps = then["parallel"] if has_par else then["sequence"]

        if len(steps) > MAX_ACTIONS_PER_RULE:
            res.errors.append(f"{rid}: アクションが多すぎる。")

        for step in steps:
            if "parallel" in step or "sequence" in step:
                res.errors.append(f"{rid}: ネストは禁止（案A・フラットのみ）。")

            act = instance_map.get(step["actuator"])
            if act is None:
                res.errors.append(f"{rid}: 存在しない出力 {step['actuator']}")
                continue
            if act.get("role") != "output":
                res.errors.append(f"{rid}: {step['actuator']} は出力部品ではない。")
            if step["action"] not in act.get("actions", []):
                res.errors.append(
                    f"{rid}: {act['part_id']} にアクション {step['action']} は無い。"
                )

            if has_par and "delay_ms" in step:
                res.warnings.append(f"{rid}: parallel 内の delay_ms は無視される。")
            if "delay_ms" in step and step["delay_ms"] > MAX_DELAY_MS:
                res.errors.append(f"{rid}: delay が長すぎる。")
            if "duration_ms" in step and step["duration_ms"] > MAX_DELAY_MS:
                res.errors.append(f"{rid}: duration が長すぎる。")

    # --------------------------------------------------------
    # STEP 3: 組み合わせ可能性
    # --------------------------------------------------------
    parts = list(instance_map.values())

    gpio_needed = _sum_gpio_pins(parts)
    if gpio_needed > CHILD_SAFE_GPIO:
        res.errors.append(f"つなぐ線が多すぎる（必要 {gpio_needed} > 上限 {CHILD_SAFE_GPIO}）。")

    i2c_parts = [p for p in parts if p.get("interface") == "i2c"]
    addr_seen: dict[str, str] = {}
    for p in i2c_parts:
        addr = p.get("i2c_address")
        if addr is None:
            res.errors.append(f"{p['part_id']}: i2c なのに i2c_address 未定義。")
            continue
        if addr in addr_seen:
            other = p.get("i2c_address_alt", []) or []
            free = [a for a in other if a not in addr_seen]
            if free:
                res.warnings.append(
                    f"{p['part_id']} と {addr_seen[addr]} が同じ {addr}。"
                    f"{free[0]} へ再割当が必要 → 大人チェックに回す。"
                )
                addr_seen[free[0]] = p["part_id"]
            else:
                res.errors.append(
                    f"{p['part_id']} と {addr_seen[addr]} の I2Cアドレスが衝突。回避不可。"
                )
        else:
            addr_seen[addr] = p["part_id"]

    total_typ = sum(p.get("current_draw_ma_typ", 0) for p in parts)
    total_peak = sum(_peak_ma(p) for p in parts)
    res.current_typ_ma = total_typ
    res.current_peak_ma = total_peak
    if total_peak > USB_BUDGET_MA:
        res.errors.append(
            f"電気が足りない（ピーク {total_peak}mA > USB {USB_BUDGET_MA}mA）。"
            f"外部電源か、部品を減らす必要がある。"
        )
        res.alternatives = compute_alternatives(
            parts, USB_BUDGET_MA, catalog_index, inventory=inventory
        )
    elif total_typ > USB_BUDGET_MA * 0.8:
        res.warnings.append("電気の使用が多め。大人チェックを推奨。")

    for p in parts:
        if p.get("level_shift_required", False):
            res.warnings.append(f"{p['name_kids']} はレベルシフタが要る → 大人チェック。")

    # --------------------------------------------------------
    # STEP 4: 安全 / 大人チェック判定
    # --------------------------------------------------------
    needs_adult = any(p.get("adult_required", False) for p in parts)
    max_safe_level = max((p.get("safe_level", 1) for p in parts), default=1)
    res.needs_adult = needs_adult
    res.max_safe_level = max_safe_level

    if needs_adult or max_safe_level >= 4:
        res.warnings.append("この発明は大人チェックが必要。")

    gate = recipe["send_to_pico_gate"]
    if max_safe_level >= 3 and not gate.get("requires_photo_check", False):
        res.errors.append("このレシピは写真チェック必須にすべき。")
    if needs_adult and not gate.get("requires_adult_approval", False):
        res.errors.append("このレシピは大人承認必須にすべき。")

    res.ok = len(res.errors) == 0
    return res


def compute_alternatives(
    parts: list[dict[str, Any]],
    budget_ma: float,
    catalog_index: dict[str, dict[str, Any]],
    inventory: dict[str, Any] | None = None,
    max_results: int = 3,
    max_drop: int = 3,
) -> list[dict[str, Any]]:
    """budget_ma 以内に収まる代替構成を最大 max_results 件返す。

    制約（保護者指示 2026-05-28）:
    - 代替候補はプログラム側で計算する（LLMに任意の部品を考えさせない）
    - 出力部品（role=="output"）を 1..max_drop 個外す組み合わせのみ探索
    - 入力部品（センサー類）は外さない（発明の本質を保つ）
    - 入力1個も無い構成は除外（センサー反応で動くのが発明の前提）
    - 候補が無ければ空リスト

    並び順スコア（優先度高→低）:
    1. 危険度が下がる: 元の max_safe_level からの低下幅（大きいほど良）
    2. 使う部品が少ない: 残り部品数の少なさ
    3. 元の発明アイデアに近い: 残り部品数（多いほど良 = ②と逆向き、複合重み）
       → 実運用は「外した個数 == 1 が最善」「外した個数 == 2 が次善」とする
    4. 手持ち部品で作れる: inventory ありなら全 kept が在庫充足

    重複検出: kept の part_id ソート済タプルで重複排除。
    """
    outputs = [p for p in parts if p.get("role") == "output"]
    inputs = [p for p in parts if p.get("role") == "input"]
    controllers = [p for p in parts if p.get("role") == "controller"]
    if not outputs:
        return []

    original_safe_level = max((p.get("safe_level", 1) for p in parts), default=1)

    from itertools import combinations

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    # 外す出力数を 1, 2, ..., min(max_drop, len(outputs)-1) で増やしていく
    for drop_n in range(1, min(max_drop, len(outputs)) + 1):
        # 出力を全部外すと発明にならないので、最低1出力は残す（len(outputs)-1 で打ち止め）
        if drop_n >= len(outputs):
            break
        for drop_combo in combinations(outputs, drop_n):
            dropped_ids = {p["part_id"] for p in drop_combo}
            kept_outputs = [p for p in outputs if p["part_id"] not in dropped_ids]
            if not kept_outputs:
                continue
            kept = inputs + kept_outputs + controllers
            peak = sum(_peak_ma(p) for p in kept)
            if peak > budget_ma:
                continue

            key = tuple(sorted(p["part_id"] for p in kept))
            if key in seen:
                continue
            seen.add(key)

            kept_safe = max((p.get("safe_level", 1) for p in kept), default=1)
            safety_drop = original_safe_level - kept_safe

            inventory_ok = True
            if inventory is not None:
                stock = inventory.get("stock", {})
                from collections import Counter
                kept_counts = Counter(p["part_id"] for p in kept)
                for pid, n in kept_counts.items():
                    if stock.get(pid, 0) < n:
                        inventory_ok = False
                        break

            candidates.append({
                "keep": [p["part_id"] for p in kept],
                "dropped": sorted(dropped_ids),
                "peak_ma": peak,
                "max_safe_level": kept_safe,
                "safety_drop": safety_drop,
                "parts_count": len(kept),
                "inventory_ok": inventory_ok,
            })

    # 並び順スコア:
    #   - inventory_ok=True を上に（4位の制約）
    #   - safety_drop 大きい順（1位: 危険度低下）
    #   - dropped 個数少ない順（3位: 元のアイデアに近い → 外した数が少ない方が忠実）
    #   - parts_count 少ない順（2位: 部品少ない方が良い、ただし1出力以上保証は既に担保済）
    candidates.sort(
        key=lambda c: (
            0 if c["inventory_ok"] else 1,
            -c["safety_drop"],
            len(c["dropped"]),
            c["parts_count"],
        )
    )
    return candidates[:max_results]
