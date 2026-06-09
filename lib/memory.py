"""
kids-ai メモリ層
- jsonl ストア（~/kids-ai/memory/{user_id}/memory.jsonl）
- add / extract / search / list / build_snippet を提供
- server.py からも CLI（kids-mem）からも import される
"""
from __future__ import annotations

import os
import json
import datetime
from pathlib import Path
from typing import Optional

import config as _config

VALID_USERS = tuple(_config.child_ids())
VALID_CATEGORIES = ("like", "dislike", "event", "person", "experience")

# Per-child memory lives under <repo>/memory/<child_id>/ by default (git-ignored),
# overridable with KIDS_AI_MEMORY_DIR.
BASE = Path(os.environ.get(
    "KIDS_AI_MEMORY_DIR",
    str(Path(__file__).resolve().parent.parent / "memory"),
))


def _store_path(user_id: str) -> Path:
    if user_id not in VALID_USERS:
        raise ValueError(f"unknown user_id: {user_id}")
    d = BASE / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.jsonl"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def add(
    user_id: str,
    category: str,
    item: str,
    source_msg: str = "",
    confidence: float = 1.0,
    dedupe: bool = True,
) -> Optional[dict]:
    """Append a memory entry. If dedupe=True (default), skip when (category, item)
    already exists in the most recent 100 entries — returns None in that case.
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    item_n = _norm(item)
    if not item_n:
        return None

    if dedupe:
        existing = _load_all(user_id)[-100:]
        for e in existing:
            if e.get("category") == category and _norm(e.get("item", "")) == item_n:
                return None  # already remembered

    entry = {
        "ts": _now_iso(),
        "category": category,
        "item": item.strip(),
        "source_msg": source_msg.strip(),
        "confidence": round(float(confidence), 3),
    }
    path = _store_path(user_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _load_all(user_id: str) -> list[dict]:
    path = _store_path(user_id)
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def list_recent(
    user_id: str, limit: int = 20, category: Optional[str] = None
) -> list[dict]:
    entries = _load_all(user_id)
    if category:
        entries = [e for e in entries if e.get("category") == category]
    return entries[-limit:]


def search(user_id: str, query: str, top_k: int = 5) -> list[dict]:
    """Keyword/substring match with recency boost.
    Only entries with at least one positive match are returned —
    recency alone does NOT pull in unrelated entries.
    """
    entries = _load_all(user_id)
    if not entries:
        return []
    q = query.lower().strip()
    if not q:
        return entries[-top_k:]

    scored: list[tuple[float, int, dict]] = []
    for i, e in enumerate(entries):
        text = f"{e.get('item','')} {e.get('source_msg','')}".lower()
        match = 0.0
        if q in text:
            match += 1.0
        for tok in q.split():
            if tok and tok in text:
                match += 0.5
        if match <= 0:
            continue  # no real match → exclude regardless of recency
        recency = (i / max(len(entries), 1)) * 0.3
        scored.append((match + recency, i, e))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [e for _, _, e in scored[:top_k]]


def build_snippet(
    user_id: str,
    recent_n: int = 7,
    query: Optional[str] = None,
    query_top_k: int = 3,
) -> str:
    """Build a system-prompt snippet listing recent + relevant memory entries."""
    recent = list_recent(user_id, limit=recent_n)
    related: list[dict] = []
    if query:
        related = search(user_id, query, top_k=query_top_k)
        # dedupe by (ts, item)
        seen = {(e["ts"], e["item"]) for e in recent}
        related = [e for e in related if (e["ts"], e["item"]) not in seen]

    if not recent and not related:
        return ""

    lines = ["---", "", "## 最近の話題（メモリから）"]
    for e in recent:
        date = e["ts"][:10]
        lines.append(f"- [{date} {e['category']}] {e['item']}")
    if related:
        lines.append("")
        lines.append("### この話題に関連しそうな過去メモリ")
        for e in related:
            date = e["ts"][:10]
            lines.append(f"- [{date} {e['category']}] {e['item']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# haiku 自動抽出（最小実装、Anthropic API 直叩き）
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """あなたは子供の発言からメモリ項目を抽出するシステムです。
発言から、以下5カテゴリに該当する項目を抽出してJSON配列で返してください：
- like: 好きなもの・キャラ・本・食べ物・場所
- dislike: 嫌い・苦手なもの
- event: 起きた出来事
- person: 人物（友達・先生・家族・ペット）
- experience: 体験・行ったこと・達成したこと

絶対に抽出してはいけないもの：
- 学校名・住所・電話番号などの個人特定情報
- 本名フルネーム（あだ名・下の名前はOK）

返却フォーマット（JSONのみ、説明文なし）：
[
  {"category": "like", "item": "短い要約", "confidence": 0.0-1.0}
]

抽出すべき項目がない場合は [] を返してください。
"""


_SKIP_PATTERNS = (
    "うん", "はい", "そう", "そうだね", "わかった", "わかる", "ありがとう",
    "おはよう", "おやすみ", "またね", "バイバイ", "やだ", "いやだ", "もう",
    "ええ", "うー", "あー", "んー", "へえ", "ふーん", "そっか",
)


def _should_extract(msg: str) -> bool:
    """安いheuristicで抽出に値する発言か判定（haikuコスト節約）。"""
    s = (msg or "").strip()
    if len(s) < 8:
        return False
    # 全角/半角の句読点・記号を除いた実質文字数
    core = "".join(c for c in s if c not in "。、！？!?.　 \t,．")
    if len(core) < 6:
        return False
    # 短い挨拶・相槌のみで構成された発言
    if s in _SKIP_PATTERNS:
        return False
    if all(any(p in s for p in _SKIP_PATTERNS) for _ in [None]) and len(s) < 12:
        return False
    return True


def extract_from_message(user_id: str, msg: str) -> list[dict]:
    """子供発言1ターンを haiku に投げて自動抽出 → add して返す。
    短い発言・挨拶のみのものは haiku 呼ばずスキップ（コスト節約）。"""
    if not _should_extract(msg):
        return []
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed")

    client = Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": msg}],
    )
    raw = response.content[0].text.strip() if response.content else "[]"
    # tolerate code-block wrapping
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []

    added: list[dict] = []
    for it in items:
        cat = it.get("category")
        item = it.get("item")
        conf = it.get("confidence", 0.7)
        if cat in VALID_CATEGORIES and item:
            added.append(add(user_id, cat, item, source_msg=msg, confidence=conf))
    return added
