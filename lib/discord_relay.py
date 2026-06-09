"""kids-ai → Discord listener relay (Max枠で完結、Anthropic API不要).

呼び出し側 (server.py) が relay 経路で動かしたいときに `post_and_wait()` を叩く。
内部で webhook POST → 数秒ごとに channel poll → `[KIDS-{user_id}-{req_id}]` 付きの
listener reply を拾って本文を返す。

- ENV:
  - DISCORD_BOT_TOKEN              … 既存 ~/.claude/channels/discord/.env から読む
  - KIDS_AI_RELAY_CHAT_ID          … relay 用 channel id（例: 新規 #kids-ai-bridge）
  - KIDS_AI_RELAY_WEBHOOK_URL      … 上記 channel に作った Webhook URL（別identity post 必須）
  - KIDS_AI_RELAY_TIMEOUT_SEC      … 既定 90（listener 重い時に備えて余裕めに）
  - KIDS_AI_RELAY_POLL_SEC         … 既定 2

NO ANTHROPIC API CALL: 本モジュールは Discord HTTP しか叩かない。
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import time
import urllib.request
import uuid as _uuid
from pathlib import Path
from typing import Any

DISCORD_API = "https://discord.com/api/v10"

TOKEN_FILE = os.environ.get("KIDS_AI_DISCORD_ENV", str(Path.home() / ".config/kids-ai/discord.env"))

# 失敗時は早めに気付くため、env未設定だと post_and_wait が RuntimeError を投げる
CHAT_ID_ENV = "KIDS_AI_RELAY_CHAT_ID"
WEBHOOK_ENV = "KIDS_AI_RELAY_WEBHOOK_URL"


def _load_bot_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(f"missing {TOKEN_FILE}")
    for line in open(TOKEN_FILE):
        s = line.strip()
        if s.startswith("DISCORD_BOT_TOKEN="):
            return s.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("DISCORD_BOT_TOKEN not in .env")


def _config() -> tuple[str, str, str, int, int]:
    chat_id = os.environ.get(CHAT_ID_ENV, "").strip()
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if not chat_id:
        raise RuntimeError(f"{CHAT_ID_ENV} not set")
    if not webhook:
        raise RuntimeError(f"{WEBHOOK_ENV} not set (listener self-ignores bot posts; webhook必須)")
    timeout = int(os.environ.get("KIDS_AI_RELAY_TIMEOUT_SEC", "90"))
    poll = int(os.environ.get("KIDS_AI_RELAY_POLL_SEC", "2"))
    token = _load_bot_token()
    return chat_id, webhook, token, timeout, poll


def _build_multipart(payload: dict, file_field: str, filename: str, file_bytes: bytes,
                     mimetype: str = "text/markdown") -> tuple[bytes, str]:
    return _build_multipart_multi(payload, [(file_field, filename, file_bytes, mimetype)])


def _build_multipart_multi(payload: dict,
                           files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """payload + 任意数のファイルパートを multipart/form-data に組み立てる。

    files: [(field_name, filename, bytes, mimetype), ...]
    """
    boundary = "----kidsRelay" + _uuid.uuid4().hex
    crlf = b"\r\n"
    buf = io.BytesIO()
    buf.write(f"--{boundary}".encode()); buf.write(crlf)
    buf.write(b'Content-Disposition: form-data; name="payload_json"'); buf.write(crlf)
    buf.write(b"Content-Type: application/json"); buf.write(crlf)
    buf.write(crlf)
    buf.write(json.dumps(payload, ensure_ascii=False).encode("utf-8")); buf.write(crlf)
    for field, filename, file_bytes, mimetype in files:
        buf.write(f"--{boundary}".encode()); buf.write(crlf)
        buf.write(
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"'.encode("utf-8")
        ); buf.write(crlf)
        buf.write(f"Content-Type: {mimetype}".encode()); buf.write(crlf)
        buf.write(crlf)
        buf.write(file_bytes); buf.write(crlf)
    buf.write(f"--{boundary}--".encode()); buf.write(crlf)
    return buf.getvalue(), boundary


def _webhook_post(webhook: str, content: str, attachment_name: str | None,
                  attachment_bytes: bytes | None) -> dict:
    files = None
    if attachment_name and attachment_bytes:
        files = [("files[0]", attachment_name, attachment_bytes, "text/markdown")]
    return _webhook_post_multi(webhook, content, files)


def _webhook_post_multi(webhook: str, content: str,
                        files: list[tuple[str, str, bytes, str]] | None) -> dict:
    """webhook POST。files が None なら application/json、ありなら multipart で複数ファイル添付可。"""
    url = f"{webhook}?wait=true"
    if files:
        payload = {"content": content}
        body, boundary = _build_multipart_multi(payload, files)
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "DiscordWebhook (kids-ai-relay, 1.0)",
            },
            method="POST",
        )
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordWebhook (kids-ai-relay, 1.0)",
            },
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch_after(chat_id: str, token: str, after_id: str, limit: int = 50) -> list[dict]:
    url = f"{DISCORD_API}/channels/{chat_id}/messages?after={after_id}&limit={limit}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (kids-ai-relay, 1.0)",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _format_history(history: list[dict]) -> str:
    """[{role, content}] を listener が読みやすい markdown に整形（最大40件）。"""
    lines: list[str] = []
    for m in history[-40:]:
        role = m.get("role", "?")
        content = m.get("content", "")
        label = {"user": "👧 子供", "assistant": "🐸 あい"}.get(role, role)
        lines.append(f"### {label}\n{content}\n")
    return "\n".join(lines) if lines else "(履歴なし)"


def _build_context_md(system_prompt: str, history: list[dict], mode: str, user_id: str) -> bytes:
    body = (
        f"# kids-ai context (user={user_id}, mode={mode})\n\n"
        f"## SYSTEM PROMPT（この人格・安全ルール・学年漢字制限を完全遵守）\n\n"
        f"{system_prompt}\n\n"
        f"---\n\n"
        f"## 直近の会話履歴（最新40件まで）\n\n"
        f"{_format_history(history)}\n"
    )
    return body.encode("utf-8")


def post_and_wait(
    req_id: str,
    system_prompt: str,
    history: list[dict],
    mode: str,
    user_id: str,
) -> str | None:
    """Discord listener へ投げて返答を待つ。timeout 時 None。

    返却は listener reply の本文のみ（marker行 `[KIDS-...]` は除去済）。
    """
    chat_id, webhook, token, timeout, poll = _config()

    if not history:
        return None
    latest_user_msg = ""
    for m in reversed(history):
        if m.get("role") == "user":
            latest_user_msg = m.get("content", "")
            break
    if not latest_user_msg:
        return None

    marker = f"[KIDS-{user_id}-{req_id}]"
    name_label = {"child1": "こども1 (小3)", "child2": "こども2 (小5)"}.get(user_id, user_id)
    snippet = latest_user_msg if len(latest_user_msg) <= 1200 else latest_user_msg[:1200] + "…"
    content = (
        f"{marker} 🧒 kids-ai relay\n"
        f"対象: {name_label}\n"
        f"モード: **{mode}**\n\n"
        f"質問:\n> {snippet.replace(chr(10), chr(10) + '> ')}\n\n"
        f"📎 添付 `kids-ai-{req_id}.md` に system prompt + 直近履歴あり\n\n"
        f"⚙️ 返信ルール（厳守）\n"
        f"1. 添付の system prompt を**完全遵守**（人格・安全ルール・学年漢字制限）\n"
        f"2. 返信の1行目に `{marker}` マーカーを必ず付与\n"
        f"3. 2行目以降が本文。PWA が**そのまま TTS で読み上げる**ので、メタ説明・前置き・括弧書きの注釈は一切入れない\n"
        f"4. 既存履歴の流れを汲んで自然に応答、応答は概ね 120字以内（短文重視）\n"
    )

    attachment_bytes = _build_context_md(system_prompt, history, mode, user_id)
    attachment_name = f"kids-ai-{req_id}.md"
    posted = _webhook_post(webhook, content, attachment_name, attachment_bytes)
    posted_msg_id = posted["id"]
    posted_author_id = posted.get("author", {}).get("id")
    started = time.time()

    return _wait_for_reply(chat_id, token, posted_msg_id, posted_author_id, marker,
                            started, timeout, poll)


def _wait_for_reply(chat_id: str, token: str, posted_msg_id: str,
                    posted_author_id: str | None, marker: str,
                    started: float, timeout: int, poll: int) -> str | None:
    """listener reply を marker で待つ共通ループ。"""
    while True:
        elapsed = time.time() - started
        if elapsed > timeout:
            return None
        try:
            msgs = _fetch_after(chat_id, token, posted_msg_id, limit=50)
        except Exception:
            time.sleep(poll)
            continue
        for m in reversed(msgs):  # oldest-first
            text = (m.get("content") or "")
            if marker not in text:
                continue
            # webhook の自己エコーは無視
            author = m.get("author", {}) or {}
            if author.get("id") and author.get("id") == posted_author_id:
                continue
            # 本文抽出: marker を含む行を削除、残りを join
            lines = [ln for ln in text.splitlines() if marker not in ln]
            body = "\n".join(lines).strip()
            if not body:
                continue
            return body
        time.sleep(poll)


def post_vision_and_wait(
    req_id: str,
    system_prompt: str,
    history: list[dict],
    mode: str,
    user_id: str,
    user_text: str,
    image_bytes: bytes,
    image_mime: str,
) -> str | None:
    """画像付き Discord listener relay。listener が画像を vision 解析して返答。

    返却は listener reply の本文のみ（marker行 `[KIDS-...]` は除去済）。timeout 時 None。
    """
    chat_id, webhook, token, timeout, poll = _config()

    marker = f"[KIDS-{user_id}-{req_id}]"
    name_label = {"child1": "こども1 (小3)", "child2": "こども2 (小5)"}.get(user_id, user_id)
    snippet = user_text if len(user_text) <= 1200 else user_text[:1200] + "…"

    # 拡張子推定
    ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    ext = ext_map.get(image_mime.lower(), "jpg")
    image_filename = f"kids-ai-{req_id}-image.{ext}"
    context_filename = f"kids-ai-{req_id}.md"

    content = (
        f"{marker} 🧒📷 kids-ai vision relay\n"
        f"対象: {name_label}\n"
        f"モード: **{mode}**\n\n"
        f"子供のメッセージ:\n> {snippet.replace(chr(10), chr(10) + '> ')}\n\n"
        f"📎 添付: `{image_filename}` (写真本体) と `{context_filename}` (system prompt + 履歴)\n\n"
        f"⚙️ 返信ルール（厳守）\n"
        f"1. **必ず `download_attachment` で画像を取得し、`Read` で内容を確認**してから応答する\n"
        f"2. 添付の system prompt を**完全遵守**（人格・安全ルール・学年漢字制限・写真応答ヒント）\n"
        f"3. 返信の1行目に `{marker}` マーカーを必ず付与\n"
        f"4. 2行目以降が本文。PWA が**そのまま TTS で読み上げる**ので、メタ説明・前置き・括弧書きの注釈は一切入れない\n"
        f"5. 写真の内容に基づいて応答、概ね 120字以内（短文重視、explainモードは5〜8文OK）\n"
    )

    context_bytes = _build_context_md(system_prompt, history, mode, user_id)
    files = [
        ("files[0]", image_filename, image_bytes, image_mime),
        ("files[1]", context_filename, context_bytes, "text/markdown"),
    ]
    posted = _webhook_post_multi(webhook, content, files)
    posted_msg_id = posted["id"]
    posted_author_id = posted.get("author", {}).get("id")
    started = time.time()

    return _wait_for_reply(chat_id, token, posted_msg_id, posted_author_id, marker,
                            started, timeout, poll)
