"""
kids-ai 親モニタCH 送信モジュール
- L0：会話1ターンの即時転送（log_chat）
- L1：日次サマリー投下（post_summary） は daily_summary.py 側で生成→post_text
- MONITOR_CHANNEL_ID 未設定なら no-op（CH作成前でも server を壊さない）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

# bot token は既存 helper と同じファイルから読む
_BOT_ENV_PATH = Path(os.environ.get("KIDS_AI_DISCORD_ENV", str(Path.home() / ".config/kids-ai/discord.env")))
_KIDS_ENV_PATH = Path.home() / "kids-ai" / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


def _bot_token() -> str | None:
    env = _load_env_file(_BOT_ENV_PATH)
    return env.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")


def _monitor_channel_id() -> str | None:
    env = _load_env_file(_KIDS_ENV_PATH)
    return env.get("MONITOR_CHANNEL_ID") or os.environ.get("MONITOR_CHANNEL_ID")


def _chunks(s: str, limit: int = 1900):
    buf: list[str] = []
    cur = 0
    for line in s.splitlines(keepends=True):
        if cur + len(line) > limit and buf:
            yield "".join(buf)
            buf, cur = [], 0
        buf.append(line)
        cur += len(line)
    if buf:
        yield "".join(buf)


def post_text(text: str, chat_id: str | None = None) -> bool:
    """Post text to monitor channel (or specified chat_id). Returns False silently
    if channel/token unset — caller never needs to handle missing config."""
    token = _bot_token()
    channel = chat_id or _monitor_channel_id()
    if not token or not channel or not text.strip():
        return False

    api = f"https://discord.com/api/v10/channels/{channel}/messages"
    ok = True
    for body in _chunks(text):
        req = urllib.request.Request(
            api,
            data=json.dumps({"content": body}).encode("utf-8"),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (kids-ai-monitor, 1.0)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status not in (200, 201):
                    print(f"[monitor] HTTP {r.status}", file=sys.stderr)
                    ok = False
        except Exception as e:
            print(f"[monitor] post failed: {e}", file=sys.stderr)
            ok = False
    return ok


# ---------------------------------------------------------------------------
# L0：会話1ターン即時転送
# ---------------------------------------------------------------------------

def log_chat(
    user_id: str,
    user_msg: str,
    reply: str,
    memory_added: list[dict] | None = None,
    cost_jpy: float | None = None,  # 受け取るが表示はしない（2026-05-12保護者指示）
) -> bool:
    """Send a single chat turn to the monitor channel.
    No-op if MONITOR_CHANNEL_ID is unset (returns False)."""
    name_map = {"child1": "こども1", "child2": "こども2"}
    who = name_map.get(user_id, user_id)

    parts: list[str] = []
    parts.append(f"💬 **{who}** との会話")
    parts.append(f"👧 {user_msg}".strip())
    parts.append(f"🐤 {reply}".strip())

    if memory_added:
        added_lines = []
        for e in memory_added:
            if e is None:
                continue
            added_lines.append(f"  • [{e['category']}] {e['item']}")
        if added_lines:
            parts.append("📝 メモリ追加:")
            parts.extend(added_lines)

    # コスト表示は親モニタには出さない（保護者指示）
    text = "\n".join(parts)
    return post_text(text)


def post_wiring_check_request(
    user_id: str,
    recipe_title: str | None = None,
    photo_bytes: bytes | None = None,
    photo_mime: str = "image/jpeg",
    note: str = "",
) -> bool:
    """配線写真を親モニタCHに送信。出張中の親が遠隔チェックできる動線。
    photo_bytes が無ければテキスト通知のみ。返信は親が Discord で別途。"""
    token = _bot_token()
    channel = _monitor_channel_id()
    if not token or not channel:
        return False
    name = {"child1": "こども1", "child2": "こども2"}.get(user_id, user_id)
    title = recipe_title or "(レシピ名なし)"
    content = (
        f"🔧 **{name}** が配線チェックを依頼\n"
        f"📋 レシピ: {title}\n"
        + (f"💬 {note}\n" if note else "")
        + "👨‍🔧 写真を確認して、Discord で返信してください(現状は手動連絡、自動転送は未実装)"
    )
    api = f"https://discord.com/api/v10/channels/{channel}/messages"
    try:
        if photo_bytes:
            import uuid as _uuid
            boundary = "----wireCheck" + _uuid.uuid4().hex
            payload_json = json.dumps({"content": content}).encode("utf-8")
            body = b""
            crlf = b"\r\n"
            body += f"--{boundary}".encode() + crlf
            body += b'Content-Disposition: form-data; name="payload_json"' + crlf
            body += b"Content-Type: application/json" + crlf + crlf
            body += payload_json + crlf
            fname = f"wiring_{user_id}.jpg"
            body += f"--{boundary}".encode() + crlf
            body += f'Content-Disposition: form-data; name="files[0]"; filename="{fname}"'.encode() + crlf
            body += f"Content-Type: {photo_mime}".encode() + crlf + crlf
            body += photo_bytes + crlf
            body += f"--{boundary}--".encode() + crlf
            req = urllib.request.Request(
                api,
                data=body,
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "DiscordBot (kids-ai-monitor, 1.0)",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status in (200, 201)
        else:
            return post_text(content)
    except Exception as exc:
        print(f"[monitor.wiring_check] failed: {exc}", file=sys.stderr)
        return False
