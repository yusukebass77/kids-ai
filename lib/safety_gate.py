# lib/safety_gate.py
# kids-ai web safety gate draft
# 2026-05-27
#
# Purpose:
# - Input side: child utterance -> memory save decision (async, LLM judge allowed)
# - Output side: assistant response -> inline guard before sending to child (<200ms, rule-based only)
# - L0.5 monitor helper
# - Passive memory decay helper

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal, Optional, Callable, Awaitable
import json
import re
import time
from datetime import datetime, timezone

ChildId = Literal["child2", "child1"]
# kids-ai/server.py VALID_MODES と一致させる。abacus はサーバ側生成のため対話モードに含めない。
# vision は別エンドポイントで稼働、unknown は profile 未設定子用のフォールバック。
Mode = Literal["chat", "explain", "story", "programming", "vision", "unknown"]

MemoryAction = Literal["save", "redact_and_save", "discard"]
Sensitivity = Literal["low", "medium", "high", "critical"]
AlertLevel = Literal["normal", "watch", "alert", "critical"]


@dataclass
class MemoryGateDecision:
    child_id: ChildId
    mode: Mode
    action: MemoryAction
    memory_type: str
    content: str
    sensitivity: Sensitivity
    use_for_learning: bool
    reason: str
    alert_level: AlertLevel = "normal"
    frozen_suggested: bool = False
    log_only: bool = True


@dataclass
class OutputGuardResult:
    child_id: ChildId
    mode: Mode
    allowed: bool
    text: str
    alert_level: AlertLevel
    frozen_suggested: bool
    reason: str
    elapsed_ms: float


# -----------------------------------------------------------------------------
# Common rule patterns
# -----------------------------------------------------------------------------

NEGATIVE_SELF_PATTERNS = [
    r"私はバカ",
    r"わたしはバカ",
    r"自分はバカ",
    r"私はだめ",
    r"わたしはだめ",
    r"自分はだめ",
    r"太ってる",
    r"きらわれてる",
    r"嫌われてる",
    r"生きて.*意味",
]

HEALTH_PATTERNS = [
    r"頭が痛い",
    r"お腹が痛い",
    r"おなかが痛い",
    r"気持ち悪い",
    r"吐きそう",
    r"血が出た",
    r"けがした",
    r"怪我した",
    r"息が苦しい",
]

CONFLICT_PATTERNS = [
    r"いじめ",
    r"仲間はずれ",
    r"無視された",
    r"悪口",
    r"たたかれた",
    r"叩かれた",
    r"先生.*こわい",
    r"先生.*怖い",
    r"学校.*行きたくない",
]

UNSAFE_PATTERNS = [
    r"死にたい",
    r"消えたい",
    r"ころしたい",
    r"殺したい",
    r"自分を傷",
    r"家出",
    r"火をつけ",
    r"包丁",
]

# NOTE:
# PII detection is intentionally conservative. Tune during one-week log-only trial.
PII_PATTERNS = [
    r"([一-龥ぁ-んァ-ン]{2,5})ちゃん",
    r"([一-龥ぁ-んァ-ン]{2,5})くん",
    r"([一-龥ぁ-んァ-ン]{2,8})小学校",
    r"([一-龥ぁ-んァ-ン]{2,8})駅",
]

# 家族関係語は「○○ちゃん/くん」匿名化から除外
FAMILY_RELATION_EXEMPT = {
    "おかあ", "おとう", "おばあ", "おじい",
    "おにい", "おねえ", "じいちゃ", "ばあちゃ",
    "とうちゃ", "かあちゃ", "にいちゃ", "ねえちゃ",
    "あか", "おじ", "おば",
}

OUTPUT_FORBIDDEN_PATTERNS = [
    # Avoid reinforcing negative self-beliefs or exposing PII from memory.
    r"前に.*バカ",
    r"前に.*だめ",
    r"あなたは.*太って",
    r"あなたは.*嫌われ",
    r"前に.*嫌われている",
    r"住所は",
    r"学校は.*小学校",
    r"最寄り駅",
]


# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _redact_friend_suffix(match: re.Match) -> str:
    """`〜ちゃん/くん` の prefix が家族関係語ならそのまま残す、それ以外は友達A置換。"""
    prefix = match.group(1)
    if prefix in FAMILY_RELATION_EXEMPT:
        return match.group(0)
    return "友達A"


def _redact_pii(text: str) -> str:
    """Redact likely friend names, schools, and stations while keeping context.
    Family relation words (おかあちゃん/おばあちゃん等) are exempted."""
    redacted = text
    redacted = re.sub(r"([一-龥ぁ-んァ-ン]{2,5})ちゃん", _redact_friend_suffix, redacted)
    redacted = re.sub(r"([一-龥ぁ-んァ-ン]{2,5})くん", _redact_friend_suffix, redacted)
    redacted = re.sub(r"([一-龥ぁ-んァ-ン]{2,8})小学校", "学校A", redacted)
    redacted = re.sub(r"([一-龥ぁ-んァ-ン]{2,8})駅", "駅A", redacted)
    return redacted


def _classify_alert_level(text: str) -> tuple[AlertLevel, bool, str]:
    if _match_any(text, UNSAFE_PATTERNS):
        return "critical", True, "unsafe_topic"

    if _match_any(text, HEALTH_PATTERNS):
        return "alert", False, "health"

    if _match_any(text, CONFLICT_PATTERNS):
        return "alert", False, "conflict"

    if _match_any(text, NEGATIVE_SELF_PATTERNS):
        return "watch", False, "negative_self_belief"

    return "normal", False, "normal"


# -----------------------------------------------------------------------------
# A. Input side safety gate
# child utterance -> memory decision
# async / LLM judge allowed / post-response
# -----------------------------------------------------------------------------

def quick_memory_prefilter(
    *,
    child_id: ChildId,
    mode: Mode,
    user_text: str,
    log_only: bool = True,
) -> MemoryGateDecision:
    """
    Rule-based prefilter before optional LLM judge.
    Strongly sensitive utterances are discarded or redacted here.
    """

    alert_level, frozen_suggested, reason = _classify_alert_level(user_text)

    if reason in {"unsafe_topic", "negative_self_belief", "health", "conflict"}:
        return MemoryGateDecision(
            child_id=child_id,
            mode=mode,
            action="discard",
            memory_type=reason,
            content="",
            sensitivity="critical" if reason == "unsafe_topic" else "high",
            use_for_learning=False,
            reason=f"prefilter:{reason}",
            alert_level=alert_level,
            frozen_suggested=frozen_suggested,
            log_only=log_only,
        )

    if _match_any(user_text, PII_PATTERNS):
        redacted = _redact_pii(user_text)
        # 家族関係語(おかあちゃん等)で PII regex がヒットしても redact 後と変わらない
        # → false-positive。social_context ではなく family_context として save 扱い
        if redacted == user_text:
            return MemoryGateDecision(
                child_id=child_id,
                mode=mode,
                action="save",
                memory_type="family_context",
                content=user_text,
                sensitivity="low",
                use_for_learning=True,
                reason="prefilter:family_relation_pass",
                alert_level="normal",
                frozen_suggested=False,
                log_only=log_only,
            )
        return MemoryGateDecision(
            child_id=child_id,
            mode=mode,
            action="redact_and_save",
            memory_type="social_context",
            content=redacted,
            sensitivity="medium",
            use_for_learning=False,
            reason="prefilter:pii_redacted",
            alert_level="normal",
            frozen_suggested=False,
            log_only=log_only,
        )

    return MemoryGateDecision(
        child_id=child_id,
        mode=mode,
        action="save",
        memory_type="unknown",
        content=user_text,
        sensitivity="low",
        use_for_learning=False,
        reason="prefilter:pass_to_judge",
        alert_level="normal",
        frozen_suggested=False,
        log_only=log_only,
    )


def build_memory_judge_prompt(child_id: ChildId, mode: Mode, user_text: str) -> str:
    return f"""
You are a child-safety memory classifier for a private family AI.

Child: {child_id}
Mode: {mode}

Classify whether this user message should be saved as long-term memory.

Return strict JSON only:
{{
  "action": "save" | "redact_and_save" | "discard",
  "memory_type": "interest | preference | skill_progress | creative_work | family_context | friend_context | location_context | schedule | health | negative_self_belief | conflict | unsafe_topic | other",
  "content": "safe memory text in Japanese, redacted if needed",
  "sensitivity": "low | medium | high | critical",
  "use_for_learning": true | false,
  "reason": "short reason"
}}

Rules:
- Never save negative self-beliefs.
- Never save health details.
- Never save address, school name, station name, family schedule, or full names.
- Redact friend names to 友達A.
- Redact school/station to 学校A/駅A.
- Save interests, preferences, creative works, and learning progress when safe.
- use_for_learning must be true only for low-sensitivity interests, preferences, creative works, or learning progress.

Message:
{user_text}
""".strip()


async def input_memory_gate_async(
    *,
    child_id: ChildId,
    mode: Mode,
    user_text: str,
    llm_judge: Optional[Callable[[str], Awaitable[str]]] = None,
    log_only: bool = True,
) -> MemoryGateDecision:
    """
    Input-side safety gate.
    Intended to run after response has already been sent, so latency is acceptable.
    """

    pre = quick_memory_prefilter(
        child_id=child_id,
        mode=mode,
        user_text=user_text,
        log_only=log_only,
    )

    # Forced discard/redact by deterministic rules.
    if pre.reason != "prefilter:pass_to_judge":
        return pre

    # If no LLM judge is supplied, return safe default.
    if llm_judge is None:
        return pre

    prompt = build_memory_judge_prompt(child_id, mode, user_text)

    try:
        raw = await llm_judge(prompt)
        data = json.loads(raw)

        action = data.get("action", "discard")
        memory_type = data.get("memory_type", "other")
        content = data.get("content", "")
        sensitivity = data.get("sensitivity", "high")
        use_for_learning = bool(data.get("use_for_learning", False))
        reason = data.get("reason", "llm_judge")

        # Final hardening.
        if sensitivity in {"high", "critical"}:
            use_for_learning = False

        if memory_type in {
            "health",
            "negative_self_belief",
            "conflict",
            "unsafe_topic",
            "schedule",
            "location_context",
        }:
            if action == "save":
                action = "discard"
            use_for_learning = False

        safe_content = _redact_pii(content)

        return MemoryGateDecision(
            child_id=child_id,
            mode=mode,
            action=action,
            memory_type=memory_type,
            content=safe_content if action != "discard" else "",
            sensitivity=sensitivity,
            use_for_learning=use_for_learning,
            reason=f"llm:{reason}",
            alert_level="normal",
            frozen_suggested=False,
            log_only=log_only,
        )

    except Exception as exc:
        return MemoryGateDecision(
            child_id=child_id,
            mode=mode,
            action="discard",
            memory_type="judge_error",
            content="",
            sensitivity="high",
            use_for_learning=False,
            reason=f"judge_error:{type(exc).__name__}",
            alert_level="watch",
            frozen_suggested=False,
            log_only=log_only,
        )


# -----------------------------------------------------------------------------
# B. Output side response guard
# assistant response -> before sending to child
# inline / <200ms / rule-based only / no LLM judge
# -----------------------------------------------------------------------------

def output_response_guard_inline(
    *,
    child_id: ChildId,
    mode: Mode,
    assistant_text: str,
) -> OutputGuardResult:
    """
    Must run synchronously before sending assistant_text to the child.
    No LLM calls. Target: <200ms.
    """

    started = time.perf_counter()
    text = assistant_text
    alert_level: AlertLevel = "normal"
    frozen_suggested = False
    reason = "ok"
    allowed = True

    if _match_any(text, OUTPUT_FORBIDDEN_PATTERNS):
        text = (
            "ごめんね、言い方をかえるね。"
            "だいじなことだから、ひとりでかかえないで、"
            "お父さんかお母さんにも話してみよう。"
        )
        alert_level = "watch"
        reason = "blocked_reinforcement_or_pii"

    if _match_any(text, UNSAFE_PATTERNS):
        text = (
            "その話はとても大事だから、あいだけで答えないよ。"
            "近くの大人にすぐ話してね。"
        )
        alert_level = "critical"
        frozen_suggested = True
        reason = "blocked_unsafe_output"

    elapsed_ms = (time.perf_counter() - started) * 1000

    if elapsed_ms > 200:
        if alert_level == "normal":
            alert_level = "watch"
        reason = f"{reason}:slow_guard"

    return OutputGuardResult(
        child_id=child_id,
        mode=mode,
        allowed=allowed,
        text=text,
        alert_level=alert_level,
        frozen_suggested=frozen_suggested,
        reason=reason,
        elapsed_ms=elapsed_ms,
    )


# -----------------------------------------------------------------------------
# L0.5 monitor helper
# -----------------------------------------------------------------------------

def monitor_alert_level(
    *,
    child_id: ChildId,
    mode: Mode,
    user_text: str,
    assistant_text: str = "",
) -> dict:
    combined = f"{user_text}\n{assistant_text}"
    alert_level, frozen_suggested, reason = _classify_alert_level(combined)

    return {
        "child_id": child_id,
        "mode": mode,
        "alert_level": alert_level,
        "frozen_suggested": frozen_suggested,
        "reason": reason,
        "ts": _now_iso(),
    }


# -----------------------------------------------------------------------------
# Passive memory decay
# -----------------------------------------------------------------------------

def should_mark_stale(
    *,
    last_referenced_days: int,
    sensitivity: Sensitivity,
) -> bool:
    """
    Do not actively ask: 'Do you still like this?'
    Just move old memories out of topic candidates.
    """

    if sensitivity in {"high", "critical"}:
        return True

    if last_referenced_days >= 180:
        return True

    return False


def to_jsonl(obj) -> str:
    return json.dumps(asdict(obj), ensure_ascii=False)
