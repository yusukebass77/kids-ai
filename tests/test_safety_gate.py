# tests/test_safety_gate.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lib.safety_gate import (
    quick_memory_prefilter,
    output_response_guard_inline,
    should_mark_stale,
    _redact_pii,
)


def test_negative_self_belief_discard():
    decision = quick_memory_prefilter(
        child_id="child2",
        mode="chat",
        user_text="私はバカかもしれない",
        log_only=True,
    )
    assert decision.action == "discard"
    assert decision.use_for_learning is False
    assert decision.sensitivity == "high"


def test_pii_redacted():
    decision = quick_memory_prefilter(
        child_id="child1",
        mode="chat",
        user_text="さくらちゃんと横浜小学校で遊んだ",
        log_only=True,
    )
    assert decision.action == "redact_and_save"
    assert "さくら" not in decision.content
    assert "横浜小学校" not in decision.content
    assert decision.use_for_learning is False


def test_health_discard_and_alert():
    decision = quick_memory_prefilter(
        child_id="child1",
        mode="chat",
        user_text="お腹が痛い",
        log_only=True,
    )
    assert decision.action == "discard"
    assert decision.alert_level == "alert"


def test_output_guard_blocks_negative_reinforcement():
    result = output_response_guard_inline(
        child_id="child2",
        mode="chat",
        assistant_text="前に自分はバカって言ってたよね",
    )
    assert result.elapsed_ms < 200
    assert result.alert_level in {"watch", "alert", "critical"}
    assert "バカって言ってた" not in result.text


def test_output_guard_fast_for_normal_text():
    result = output_response_guard_inline(
        child_id="child1",
        mode="programming",
        assistant_text="いいね。まずLEDを1つ光らせてみよう。",
    )
    assert result.allowed is True
    assert result.elapsed_ms < 200
    assert result.alert_level == "normal"


def test_passive_memory_decay():
    assert should_mark_stale(last_referenced_days=180, sensitivity="low") is True
    assert should_mark_stale(last_referenced_days=30, sensitivity="low") is False
    assert should_mark_stale(last_referenced_days=1, sensitivity="high") is True


def test_family_relation_not_redacted():
    """家族関係語(おかあちゃん/おばあちゃん等)はPII匿名化の対象外。
    友達Aに置換されたら子供が祖父母の話をできなくなる。"""
    assert _redact_pii("おかあちゃんと公園いった") == "おかあちゃんと公園いった"
    assert _redact_pii("おばあちゃんちで遊んだ") == "おばあちゃんちで遊んだ"
    assert _redact_pii("おじいちゃんが石くれた") == "おじいちゃんが石くれた"
    # 一般の友達名はちゃんと置換される
    assert "さくら" not in _redact_pii("さくらちゃんと遊んだ")


def test_family_relation_action_is_save():
    """家族関係語は redact_and_save ではなく save (low sensitivity, family_context)。
    PII regex は family も拾うが、redact 後と変わらないので family_context として保存。"""
    d = quick_memory_prefilter(
        child_id="child1", mode="chat", user_text="おかあちゃんと公園いった", log_only=True,
    )
    assert d.action == "save"
    assert d.memory_type == "family_context"
    assert d.sensitivity == "low"
    assert "おかあちゃん" in d.content


def test_child_id_isolation_in_decisions():
    """安全判定の決定オブジェクトに child_id が必ず正しく入る (姉妹混線防止のbase)。
    memory.py 側の load_memory(child_id) と組み合わせて姉妹間漏洩を防ぐ。"""
    d_child2 = quick_memory_prefilter(
        child_id="child2", mode="chat", user_text="ねこが好き", log_only=True,
    )
    d_child1 = quick_memory_prefilter(
        child_id="child1", mode="chat", user_text="石を集めるのが好き", log_only=True,
    )
    assert d_child2.child_id == "child2"
    assert d_child1.child_id == "child1"
    # 同じ user_text でも child_id がスワップされないこと
    d_child12 = quick_memory_prefilter(
        child_id="child1", mode="chat", user_text="ねこが好き", log_only=True,
    )
    assert d_child12.child_id == "child1" and d_child12.content == "ねこが好き"
