"""
あい（子供版）バックエンド+フロント統合サーバー
- FastAPI で /api/chat + 静的ファイル配信
- Haiku 4.5 (Anthropic API)
- 使用量ログ ~/kids-ai/logs/usage_YYYY-MM-DD.jsonl
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from anthropic import Anthropic
from openai import OpenAI as OpenAIClient

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

# kids-ai/lib を import path に追加（memory / monitor モジュール用）
sys.path.insert(0, str(BASE / "lib"))
import memory as kids_memory  # noqa: E402
import monitor as kids_monitor  # noqa: E402
import discord_relay as kids_relay  # noqa: E402
import safety_gate as kids_safety  # noqa: E402
import furigana as kids_furigana  # noqa: E402
import config as kids_config  # noqa: E402

# Child roster + assistant name come from config/children.json (git-ignored)
# with a fallback to config/children.example.json. Nothing personal is hard-coded.
CHILD_IDS = tuple(kids_config.child_ids())
CHILD_NAMES = kids_config.child_names()
FURIGANA_IDS = tuple(kids_config.furigana_ids())
ASSISTANT_NAME = kids_config.assistant_name()
DEFAULT_CHILD_ID = kids_config.default_child_id()

# 安全ゲート 1週間 log-only 試運転モード (Phase 1, 2026-05-27 開始)
# - 入力側 memory_gate: 既存抽出は維持、決定だけ並行ログ
# - 出力側 output_guard: 元テキストはそのまま送出、判定だけログ
# - logs/safety_YYYY-MM-DD.jsonl に追記、false +/-を観察してから本番有効化
SAFETY_LOG_ONLY = os.environ.get("KIDS_AI_SAFETY_ENFORCE", "0").strip() != "1"


def safety_log(record: dict) -> None:
    try:
        today = datetime.date.today().isoformat()
        with open(LOG_DIR / f"safety_{today}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[safety_log] write error: {exc}", file=sys.stderr)

# サブスク経路（Discord listener → Max枠で完結、Anthropic API 不要）。
# 1 で全 chat/chat_stream リクエストを listener 経路に差し向ける。
# vision は当面 Anthropic 直叩き継続（マルチモーダルrelayは別タスク）。
RELAY_ENABLED = os.environ.get("KIDS_AI_RELAY_ENABLED", "0").strip() == "1"

PROMPT_PATH = BASE / "prompts" / "chat-kids-v1.md"
EXPLAIN_PROMPT_PATH = BASE / "prompts" / "yasashiku-setsumei-v1.md"
STORY_PROMPT_PATH = BASE / "prompts" / "story-mode-v2.md"
PROGRAMMING_PROMPT_PATH = BASE / "prompts" / "programming-mode-v1.md"
PROFILE_DIR = BASE / "prompts"
PWA_DIR = BASE / "pwa"
LOG_DIR = BASE / "logs"
FROZEN_DIR = BASE / "frozen"
LOG_DIR.mkdir(exist_ok=True)
FROZEN_DIR.mkdir(exist_ok=True)

BREAK_THRESHOLD_SEC = 2 * 60 * 60  # 連続2時間で軽く注意（21:00以降は別途quiet hours発動）


def is_frozen(user_id: str) -> bool:
    return (FROZEN_DIR / f"{user_id}.flag").exists()


# 子供AIの夜間シャットアウト（JST、モード別）
# chat: 21:00 - 翌06:00
# explain: 22:00 - 翌06:00（学習用に1時間長く使える、2026-05-16保護者指示）
# サーバ側で時刻判定するためクライアント時計改竄では抜けられない
JST = ZoneInfo("Asia/Tokyo")
QUIET_START_HOUR = 21
QUIET_START_MINUTE = 0
QUIET_EXPLAIN_START_HOUR = 22
QUIET_EXPLAIN_START_MINUTE = 0
QUIET_END_HOUR = 6
QUIET_REPLY = (
    "もう ねる じかんだよ。"
    "きょうも たくさん おはなし してくれて ありがとう。"
    "あしたの あさ また はなそうね。"
    "おやすみなさい。"
)


# あいモード1日あたり利用上限（2026-05-16 保護者指示）
# - 1日2時間 = 120分。21:00夜間シャットアウトに加えて、日中の遊びすぎも止める
# - 「分」は usage_*.jsonl の ts から HH:MM 単位の distinct count で近似
#   （同一分に何ターン話しても1分カウント＝実滞在時間に近い）
# - 子別／モード別カウント。explainモードは別予算（無制限）
CHAT_DAILY_BUDGET_MIN = 120
CHAT_BUDGET_REPLY = (
    "きょうは あいと いっぱい おはなしできたね！"
    "また あした、げんきに あおうね。"
    "(まなぶモードは つかえるよ)"
)


def chat_minutes_used_today(user_id: str) -> int:
    """今日（JSTローカル）の chat系（chat + story）モード利用分数を返す。
    usage_YYYY-MM-DD.jsonl から user_id+mode=(chat|story) の行を集め、
    ts の HH:MM 単位 distinct 数を返す（同一分の複数ターンは1分扱い）。
    story はお話モードでお遊び枠なので chat 予算と共有（2026-05-17）。"""
    today = datetime.date.today().isoformat()
    path = LOG_DIR / f"usage_{today}.jsonl"
    if not path.exists():
        return 0
    minutes: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("user_id") != user_id:
                    continue
                if (e.get("mode") or "chat") not in ("chat", "story", "programming"):
                    continue
                ts = e.get("ts") or ""
                # "2026-05-16T20:41:12" → "2026-05-16T20:41"
                if len(ts) >= 16:
                    minutes.add(ts[:16])
    except Exception as exc:
        print(f"[budget] read error: {exc}", file=sys.stderr)
    return len(minutes)


QUIET_OVERRIDE_FLAG = BASE / "quiet_override.json"


def _quiet_override_active() -> bool:
    """親確認用の一時解除フラグ。`quiet_override.json` 内 `until_epoch` が現在より未来なら True。
    （ファイル消すか until_epoch を過ぎたら自動失効）"""
    if not QUIET_OVERRIDE_FLAG.exists():
        return False
    try:
        with open(QUIET_OVERRIDE_FLAG, encoding="utf-8") as f:
            data = json.load(f)
        until = float(data.get("until_epoch") or 0)
        return time.time() < until
    except Exception:
        return False


def is_quiet_hours(mode: str = "chat") -> bool:
    if _quiet_override_active():
        return False
    now = datetime.datetime.now(JST)
    h, m = now.hour, now.minute
    # 朝6:00以前は両モードとも休眠
    if h < QUIET_END_HOUR:
        return True
    if mode == "explain":
        if h > QUIET_EXPLAIN_START_HOUR:
            return True
        if h == QUIET_EXPLAIN_START_HOUR and m >= QUIET_EXPLAIN_START_MINUTE:
            return True
        return False
    # chat（デフォルト）: 21:00以降
    if h > QUIET_START_HOUR:
        return True
    if h == QUIET_START_HOUR and m >= QUIET_START_MINUTE:
        return True
    return False

def _read_prompt(path: Path) -> str:
    """Read a prompt file and inject the configured assistant name."""
    return path.read_text(encoding="utf-8").replace("{{assistant_name}}", ASSISTANT_NAME)


BASE_SYSTEM_PROMPT = _read_prompt(PROMPT_PATH)
EXPLAIN_SYSTEM_PROMPT = _read_prompt(EXPLAIN_PROMPT_PATH)
STORY_SYSTEM_PROMPT = _read_prompt(STORY_PROMPT_PATH)
PROGRAMMING_SYSTEM_PROMPT = _read_prompt(PROGRAMMING_PROMPT_PATH)


VALID_MODES = ("chat", "explain", "story", "programming")


# storyモード応答冒頭の常套相槌フィルタ。LLM癖でターン毎に「あそっか／なるほど」等が
# 連発される問題（2026-05-17 こども2お話モード報告）への後処理対策。
# 章マーカー「【第◯章】」が来るより前に出る頭フィラーだけを最大1回除去する。
STORY_LEAD_MAX = 40
STORY_LEAD_FILLER_RE = re.compile(
    r"^[\s　「『]*"
    r"(?:"
    r"あ[、\s]?そっか[ー～]?|あそっかー?|"
    r"そっか[ー～]?|そうか[ー～]?|そうだね[え]?|そうなんだ[ね]?|"
    r"なるほど[ー～！\!]?|"
    r"うーん[…\s、]*|うんうん[、\s]*|"
    r"あれ[\?？]+|"
    r"どうなるかな[ー～\?？]*|"
    r"わかった[よね]?|わかったよ[ー～]?|"
    r"いいねぇ?|いいじゃん[ー～]?|"
    r"すごい[ねえ]*"
    r")"
    r"[、。！？\s　…」』]*"
)


def build_system_prompt(
    user_id: str,
    latest_user_msg: str = "",
    session_duration_sec: int = 0,
    mode: str = "chat",
) -> str:
    """Build per-user system prompt for given mode.
    mode="chat": Socratic personalized chat with break-time nudges + memory.
    mode="explain": Plain kid-friendly explainer; no memory, no break nudges, kanji rules stay.
    """
    mode = mode if mode in VALID_MODES else "chat"
    if mode == "explain":
        parts = [EXPLAIN_SYSTEM_PROMPT]
    elif mode == "story":
        parts = [STORY_SYSTEM_PROMPT]
    elif mode == "programming":
        parts = [PROGRAMMING_SYSTEM_PROMPT]
    else:
        parts = [BASE_SYSTEM_PROMPT]

    profile_path = PROFILE_DIR / f"{user_id}_profile.md"
    if profile_path.exists():
        parts.append(_read_prompt(profile_path))

    # explainモードのみ：3Dモデルカタログを動的に system prompt に注入
    if mode == "explain":
        topics = _load_model3d_catalog().get("topics", {})
        topic_lines = "\n".join(f"- `{k}`: {v.get('label', k)}" for k, v in topics.items())
        rich_hint = (
            "## リッチブロック記法（必要な時だけ使う）\n"
            "テキスト本文の中に以下のフェンスブロックを混ぜると、クライアントが自動でレンダリングする。\n"
            "全部任意、回答の本文が主。図や数式が**説明を助けるとき**だけ添える。\n\n"
            "### KaTeX数式\n"
            "- インライン：`$x + 2 = 5$` のように `$...$` で囲む\n"
            "- ディスプレイ：`$$\\frac{1}{2}$$` のように `$$...$$` で囲む（独立ブロック）\n"
            "- 算数・面積・分数・速さ・割合の式に使う\n\n"
            "### Leaflet地図 (```geomap)\n"
            "場所・地理の話題で、ピンを立てて見せたい時。\n"
            "```geomap\n{\"lat\": 35.1, \"lon\": 138.86, \"zoom\": 11, \"label\": \"東京\"}\n```\n"
            "・lat/lon は世界中OK、zoomは5〜15の範囲（広い→狭い）\n\n"
            "### 現在地表示 (```geomap_here)\n"
            "「今どこ？」「ここどこ？」「いまいる場所」など現在地を聞かれた時に使う。\n"
            "クライアントが端末のGPS/Wi-Fi位置情報を取得して地図表示する（Fire HDはGPS無しなのでだいたいの場所になる旨を1文添えてあげると親切）。\n"
            "```geomap_here\n{\"zoom\": 14, \"label\": \"今いる場所\"}\n```\n"
            "・lat/lonはクライアント側で補完するのでAIは書かない\n"
            "・許可が拒否されたらクライアントがエラー表示するのでAIは事前心配不要\n\n"
            "### 3Dモデルビューア (```model3d)\n"
            "話題が以下のキャタログに合致する時だけ使う。topic名は完全一致が必要。\n"
            f"{topic_lines}\n"
            "```model3d\n{\"topic\": \"fox\", \"alt\": \"キツネ\"}\n```\n"
            "・カタログにない話題は使わない（無理に当てるとエラー表示が出る）\n\n"
            "### 既存のMermaid図はそのまま使えるので、フローチャート・マインドマップ・円グラフはそちらを継続。"
        )
        parts.append(rich_hint)

    if mode == "chat":
        if user_id in kids_memory.VALID_USERS:
            snippet = kids_memory.build_snippet(
                user_id, recent_n=7, query=latest_user_msg or None, query_top_k=3,
            )
            if snippet:
                parts.append(snippet)
        if session_duration_sec >= BREAK_THRESHOLD_SEC:
            minutes = session_duration_sec // 60
            break_hint = (
                "## 軽い注意ヒント（連続利用2時間超）\n"
                f"この子はもう **約{minutes}分** あいと話しています。\n"
                "21時までは普通に相手していいけど、2時間超えたら**軽く注意**を1文添えて。\n"
                "・「ちょっと休憩しよっか」「目が疲れるよ」くらいの軽さ、強制も罪悪感も与えない\n"
                "・毎回違う言い回しで、子の様子・話題・時間帯に合わせて自然に\n"
                "・決まり文句や「外で〇〇」「明日にしよう」みたいなテンプレは避ける\n"
                "・今のターンの内容自体は普通に答えていい、注意は最後の1文だけ"
            )
            parts.append(break_hint)
    return "\n\n---\n\n".join(parts)

# モード別モデル割り当て（2026-05-16）
# chat: ごっこ遊び主体なのでHaikuで十分（保護者判断）
# explain: 解説の正確性重視、Sonnet据置
# vision : 種名同定は推論力必要、Sonnet据置
MODEL_CHAT = "claude-haiku-4-5"
MODEL_EXPLAIN = "claude-sonnet-4-6"
MODEL_VISION  = "claude-sonnet-4-6"
MODEL = MODEL_CHAT  # 後方互換：旧コード参照用、健康エンドポイント等

MAX_TOKENS = 300  # 子供向け短文応答、レイテンシ短縮優先（2026-05-13）
MAX_TOKENS_STORY = 600  # 物語モード：3〜5行の章＋選択肢3個で300字程度必要

# モデル別価格（$/MTok）2026-05時点
PRICING = {
    "claude-haiku-4-5":   (1.0, 5.0),
    "claude-sonnet-4-6":  (3.0, 15.0),
}
# 後方互換用（旧名称参照箇所が残ってる場合に備え）
HAIKU_INPUT_PRICE, HAIKU_OUTPUT_PRICE = PRICING["claude-haiku-4-5"]
USD_TO_JPY = 150


def model_for(mode: str) -> str:
    if mode == "explain":
        return MODEL_EXPLAIN
    if mode == "programming":
        # レシピJSON生成は構造精度が必要、Sonnet 4.6
        return MODEL_EXPLAIN
    # story モードもごっこ遊びの延長、Haikuで十分（コスト抑制）
    return MODEL_CHAT


MAX_TOKENS_PROGRAMMING = 1200  # レシピJSON(数百行)+説明文+確認、余裕めに


def max_tokens_for(mode: str) -> int:
    if mode == "story":
        return MAX_TOKENS_STORY
    if mode == "programming":
        return MAX_TOKENS_PROGRAMMING
    return MAX_TOKENS


def price_for(model_name: str) -> tuple[float, float]:
    return PRICING.get(model_name, PRICING["claude-haiku-4-5"])

client = Anthropic()
openai_client = OpenAIClient()
app = FastAPI(title="あい子供版")


# ---------------------------------------------------------------------------
# 天気tool（Open-Meteo・完全無料・APIキー不要）
# 子供が「天気は？」と聞いたときSonnetがこれを呼ぶ
# ---------------------------------------------------------------------------
import urllib.parse as _urlparse
import urllib.request as _urlreq

_WEATHER_CODE_JA = {
    0: "快晴", 1: "ほぼ快晴", 2: "薄曇り", 3: "曇り",
    45: "霧", 48: "霧",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨",
    66: "凍る雨", 67: "強い凍る雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "細かい雪",
    80: "弱いにわか雨", 81: "にわか雨", 82: "強いにわか雨",
    85: "弱いにわか雪", 86: "強いにわか雪",
    95: "雷雨", 96: "雷雨と雹", 99: "強い雷雨と雹",
}
_DEFAULT_LOCATION = os.environ.get("KIDS_AI_DEFAULT_LOCATION", "東京")


def get_weather_data(location: str | None = None) -> dict:
    loc = (location or _DEFAULT_LOCATION).strip() or _DEFAULT_LOCATION
    try:
        q = _urlparse.quote(loc)
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=ja&countryCode=JP"
        with _urlreq.urlopen(geo_url, timeout=8) as r:
            g = json.load(r)
        results = g.get("results") or []
        if not results:
            return {"error": f"「{loc}」の場所が見つかりませんでした"}
        r0 = results[0]
        lat = r0["latitude"]; lon = r0["longitude"]
        name = r0.get("name", loc)
        fc_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,weather_code,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max"
            f"&timezone=Asia/Tokyo&forecast_days=2"
        )
        with _urlreq.urlopen(fc_url, timeout=8) as r:
            w = json.load(r)
        cur = w.get("current") or {}
        daily = w.get("daily") or {}
        def _day(key, i):
            arr = daily.get(key) or []
            return arr[i] if i < len(arr) else None
        return {
            "location": name,
            "current": {
                "temp_c": cur.get("temperature_2m"),
                "weather": _WEATHER_CODE_JA.get(cur.get("weather_code"), "不明"),
                "wind_kmh": cur.get("wind_speed_10m"),
            },
            "today": {
                "max_temp_c": _day("temperature_2m_max", 0),
                "min_temp_c": _day("temperature_2m_min", 0),
                "weather": _WEATHER_CODE_JA.get(_day("weather_code", 0), "不明"),
                "precip_prob_percent": _day("precipitation_probability_max", 0),
            },
            "tomorrow": {
                "max_temp_c": _day("temperature_2m_max", 1),
                "min_temp_c": _day("temperature_2m_min", 1),
                "weather": _WEATHER_CODE_JA.get(_day("weather_code", 1), "不明"),
                "precip_prob_percent": _day("precipitation_probability_max", 1),
            },
        }
    except Exception as e:
        return {"error": f"天気を取得できませんでした: {type(e).__name__}: {e}"}


WEATHER_TOOL = {
    "name": "get_weather",
    "description": (
        "今日と明日の天気・気温・降水確率を取得する。"
        "子供が「天気は？」「雨降る？」「暑い？」「明日は？」など天気を聞いたときに使う。"
        "場所の指定がなければ設定済みのデフォルト地点（環境変数 KIDS_AI_DEFAULT_LOCATION）の天気を返す。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "場所の名前。例: 東京、横浜、大阪。指定なしならデフォルト地点。",
            }
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# OpenAI TTS（nova固定、あい音声ペルソナ）
# ---------------------------------------------------------------------------

class TtsRequest(BaseModel):
    text: str
    user_id: str | None = None


@app.post("/api/tts")
def tts(req: TtsRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text empty")
    if len(text) > 2000:
        text = text[:2000]
    try:
        # memory:reference_openai_tts_model_access.md
        # このプロジェクトAPIキーは tts-1 アクセス権なし、tts-1-hd で動く
        speech = openai_client.audio.speech.create(
            model="tts-1-hd",
            voice="nova",
            input=text,
            response_format="mp3",
        )
        audio_bytes = speech.read() if hasattr(speech, "read") else speech.content
    except Exception as e:
        print(f"[tts] error: {e}", file=sys.stderr)
        raise HTTPException(502, f"tts upstream error: {e}")
    return Response(content=audio_bytes, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# OpenAI STT（音声→テキスト、Fire HD等GPS非搭載端末向け）
# memory:feedback_voice_kb_stt_provider.md → gpt-4o-mini-transcribe 固定
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Vision（カメラ撮影→画像認識→子供向け説明）
# multipart/form-data: image(必須), user_id, prompt(任意の質問)
# ストリーミング応答（/api/chat/stream と同じNDJSON形式）
# ---------------------------------------------------------------------------

VISION_MAX_BYTES = 8 * 1024 * 1024  # 8MB（Claude API画像上限は5MBだがbase64膨張前提で多めに通す→後でクライアント側でリサイズ）
VISION_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Children with furigana=true get the simple all-hiragana prompt; others get the standard one.
VISION_DEFAULT_PROMPT_SIMPLE = "この しゃしんの なかに あるものを やさしく おしえて。なまえと、どこが おもしろいか みじかく ね。"
VISION_DEFAULT_PROMPT_STD = "この写真に写ってるものを教えて。名前と、興味を持つきっかけになりそうなポイントを短くね。"
VISION_DEFAULT_PROMPT_UNKNOWN = "この写真に写っているものを子供にもわかるように短く説明して。"


def _default_vision_prompt(user_id: str) -> str:
    if user_id in FURIGANA_IDS:
        return VISION_DEFAULT_PROMPT_SIMPLE
    if user_id in CHILD_IDS:
        return VISION_DEFAULT_PROMPT_STD
    return VISION_DEFAULT_PROMPT_UNKNOWN


@app.post("/api/vision/stream")
async def vision_stream(
    image: UploadFile = File(...),
    user_id: str = Form("unknown"),
    prompt: str = Form(""),
    session_start_ts: float | None = Form(None),
    mode: str = Form("chat"),
):
    user_id = (user_id or "unknown").strip().lower()
    if user_id not in CHILD_IDS + ("unknown",):
        user_id = "unknown"
    mode = mode if mode in VALID_MODES else "chat"

    media_type = (image.content_type or "").lower()
    if media_type not in VISION_ALLOWED_MIME:
        raise HTTPException(415, f"unsupported image type: {media_type}")

    try:
        img_bytes = await image.read()
    except Exception as e:
        raise HTTPException(400, f"image read error: {e}")
    if not img_bytes:
        raise HTTPException(400, "image empty")
    if len(img_bytes) > VISION_MAX_BYTES:
        raise HTTPException(413, "image too large (>8MB)")

    import base64
    img_b64 = base64.standard_b64encode(img_bytes).decode("ascii")

    user_text = (prompt or "").strip() or _default_vision_prompt(user_id)

    frozen_flag = user_id in CHILD_IDS and is_frozen(user_id)
    quiet_flag = user_id in CHILD_IDS and is_quiet_hours(mode=mode)
    # chat 日次予算チェック（vision_stream も対象）
    budget_flag = (
        user_id in CHILD_IDS
        and mode == "chat"
        and not quiet_flag
        and chat_minutes_used_today(user_id) >= CHAT_DAILY_BUDGET_MIN
    )

    session_duration_sec = 0
    if session_start_ts:
        session_duration_sec = max(0, int(time.time() - session_start_ts))

    system_prompt = build_system_prompt(
        user_id,
        latest_user_msg=user_text,
        session_duration_sec=session_duration_sec,
        mode=mode,
    )

    # Vision用の追加ヒント（モード別：chat=観察ヒント／explain=直接同定）
    if mode == "explain":
        vision_hint = (
            "## 写真への応答ヒント（解説モード）\n"
            "子供がカメラで撮った写真を見せてくれた。図鑑的に直接答えるのがゴール。\n"
            "- 写ってるもの（生き物・植物・石・道具など）の**名前を具体的に**答える\n"
            "  例：『亀』ではなく『ニホンイシガメ』『クサガメ』まで踏み込む\n"
            "  自信ないなら『たぶん〇〇、似てるのに△△もいる』と候補を出す\n"
            "- 名前の次に**特徴・見分け方・面白いポイント**を2〜3文\n"
            "- ソクラテス式の問い返しは不要、直球で答えて学習に使ってもらう\n"
            "- 安全上の懸念（顔・個人情報・危ない物）が写ってたら親への共有を促す\n"
            "- 長文NG、全体5〜8文程度"
        )
    else:
        vision_hint = (
            "## 写真への応答ヒント\n"
            "子供がカメラで撮った写真を見せてくれた。\n"
            "- 写ってるものを子供の語彙で短く説明する（小3こども1/小5こども2のプロファイル参照）\n"
            "- いきなり全部答えず、子供が気付くきっかけ（色・形・大きさ）を1つ添える\n"
            "- 安全上の懸念（顔・個人情報・危ない物）が写ってたら優しく親への共有を促す\n"
            "- 2〜3文、長文NG"
        )
    system_prompt = system_prompt + "\n\n---\n\n" + vision_hint

    history_for_log_text = user_text  # メモリ抽出は画像メインなのでスキップ、ログ用に保持

    def event_gen():
        if frozen_flag:
            yield json.dumps({"type": "frozen"}, ensure_ascii=False) + "\n"
            return

        if quiet_flag or budget_flag:
            msg = QUIET_REPLY if quiet_flag else CHAT_BUDGET_REPLY
            buf = ""
            for ch in msg:
                buf += ch
                if ch == "。":
                    yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
                    buf = ""
                    time.sleep(0.15)
            if buf:
                yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
            yield json.dumps({
                "type": "done",
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
                "quiet": bool(quiet_flag),
                "budget_exhausted": bool(budget_flag),
            }, ensure_ascii=False) + "\n"
            return

        t0 = time.time()
        full_text = ""
        in_tok = 0
        out_tok = 0
        active_model = MODEL_VISION
        relay_used = False

        if RELAY_ENABLED:
            # Discord listener (Sonnet 4.6, Max枠) で vision 解析。API課金ゼロ。
            import uuid as _uuid
            req_id = _uuid.uuid4().hex[:8]
            # 履歴は user_text のみ簡易構築（vision_streamは単発質問なので過去履歴は使わない）
            history_for_relay = [{"role": "user", "content": user_text}]
            try:
                reply_text = kids_relay.post_vision_and_wait(
                    req_id=req_id,
                    system_prompt=system_prompt,
                    history=history_for_relay,
                    mode=mode,
                    user_id=user_id,
                    user_text=user_text,
                    image_bytes=img_bytes,
                    image_mime=media_type,
                )
            except Exception as e:
                print(f"[vision_stream] relay error: {e}", file=sys.stderr)
                yield json.dumps({"type": "error", "message": f"relay: {type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
                return
            if reply_text is None:
                yield json.dumps({"type": "error", "message": "listener relay timeout"}, ensure_ascii=False) + "\n"
                return
            full_text = reply_text
            relay_used = True
            # fake-token streaming（/api/chat/stream の relay と同流儀）
            buf = ""
            for ch in reply_text:
                buf += ch
                if ch in ("。", "！", "？", "\n"):
                    yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
                    buf = ""
                    time.sleep(0.08)
            if buf:
                yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
        else:
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                    {"type": "text", "text": user_text},
                ],
            }]
            try:
                with client.messages.stream(
                    model=active_model,
                    max_tokens=max_tokens_for(mode),
                    system=system_prompt,
                    messages=messages,
                ) as stream:
                    for delta in stream.text_stream:
                        if delta:
                            full_text += delta
                            yield json.dumps({"type": "token", "text": delta}, ensure_ascii=False) + "\n"
                    final_msg = stream.get_final_message()
                    in_tok = final_msg.usage.input_tokens
                    out_tok = final_msg.usage.output_tokens
            except Exception as e:
                print(f"[vision_stream] upstream error: {e}", file=sys.stderr)
                yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
                return

        duration = time.time() - t0
        in_price, out_price = price_for(active_model)
        cost_usd = (in_tok * in_price + out_tok * out_price) / 1_000_000
        cost_jpy = cost_usd * USD_TO_JPY

        log_entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user_id": user_id,
            "duration_s": round(duration, 2),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_jpy": round(cost_jpy, 4),
            "model": active_model,
            "image_bytes": len(img_bytes),
            "user_msg_len": len(history_for_log_text),
            "reply_len": len(full_text),
            "vision": True,
            "mode": mode,
            "stream": True,
            "relay": relay_used,
        }
        today = datetime.date.today().isoformat()
        try:
            with open(LOG_DIR / f"usage_{today}.jsonl", "a") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[vision_stream] usage log error: {e}", file=sys.stderr)

        # 写真ターンの親モニタ即時通知は chat モードでは抑制（日次サマリーに集約）
        # explain モード（学習用）は写真の文脈が学習進捗に直結するので継続通知
        # 2026-05-16 保護者指示
        if user_id in kids_memory.VALID_USERS and mode != "chat":
            try:
                kids_monitor.log_chat(
                    user_id=user_id,
                    user_msg=f"[📷 写真] {history_for_log_text}",
                    reply=full_text,
                    memory_added=[],
                    cost_jpy=cost_jpy,
                )
            except Exception as e:
                print(f"[vision_stream] monitor.log_chat error: {e}", file=sys.stderr)

        yield json.dumps({
            "type": "done",
            "usage": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_jpy": round(cost_jpy, 4),
            },
        }, ensure_ascii=False) + "\n"

    return StreamingResponse(event_gen(), media_type="application/x-ndjson")


@app.post("/api/stt")
async def stt(audio: UploadFile = File(...), user_id: str = Form("unknown")):
    try:
        audio_bytes = await audio.read()
    except Exception as e:
        raise HTTPException(400, f"audio read error: {e}")
    if not audio_bytes:
        raise HTTPException(400, "audio empty")
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "audio too large (>25MB)")

    # ファイル拡張子はクライアントの MediaRecorder 出力に依存
    filename = audio.filename or "audio.webm"
    try:
        result = openai_client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=(filename, audio_bytes),
            language="ja",
        )
        text = (result.text or "").strip()
    except Exception as e:
        print(f"[stt] error: {e}", file=sys.stderr)
        raise HTTPException(502, f"stt upstream error: {e}")
    return {"text": text}


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    user_id: str | None = "unknown"
    session_start_ts: float | None = None  # クライアント側保持の連続会話開始時刻(秒)
    mode: str | None = "chat"  # "chat" or "explain"


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.messages:
        raise HTTPException(400, "messages empty")

    history = [{"role": m.role, "content": m.content} for m in req.messages[-40:]]
    user_id = (req.user_id or "unknown").strip().lower()
    if user_id not in CHILD_IDS + ("unknown",):
        user_id = "unknown"

    # 凍結チェック（L3トリガー時にflag立っていれば応答停止）
    if user_id in CHILD_IDS and is_frozen(user_id):
        return {
            "reply": "あい今お休み中なの。明日また話そうね。",
            "frozen": True,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
        }

    # 夜間シャットアウト（chat=21:00 / explain=22:00 - 翌06:00 JST）
    if user_id in CHILD_IDS and is_quiet_hours(mode=(req.mode or "chat")):
        return {
            "reply": QUIET_REPLY,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
        }

    # chat 日次予算チェック（1日120分）。explainは対象外。story / programming は chat 予算と共有。
    _req_mode_now = req.mode or "chat"
    if user_id in CHILD_IDS and _req_mode_now in ("chat", "story", "programming"):
        if chat_minutes_used_today(user_id) >= CHAT_DAILY_BUDGET_MIN:
            return {
                "reply": CHAT_BUDGET_REPLY,
                "budget_exhausted": True,
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
            }

    latest_user_msg = ""
    for m in reversed(history):
        if m["role"] == "user":
            latest_user_msg = m["content"]
            break

    # 連続会話時間（クライアント送信のsession_start_tsから算出）
    session_duration_sec = 0
    if req.session_start_ts:
        session_duration_sec = max(0, int(time.time() - req.session_start_ts))

    system_prompt = build_system_prompt(
        user_id,
        latest_user_msg=latest_user_msg,
        session_duration_sec=session_duration_sec,
        mode=(req.mode or "chat"),
    )

    # 子供の最新発言からメモリ自動抽出（chat対話のみ、explainは学習用なのでスキップ）
    memory_added: list[dict] = []
    if (req.mode or "chat") == "chat" and user_id in kids_memory.VALID_USERS and latest_user_msg:
        try:
            memory_added = kids_memory.extract_from_message(user_id, latest_user_msg)
        except Exception as e:
            print(f"[memory.extract] error: {e}", file=sys.stderr)

    t0 = time.time()
    active_model = model_for(req.mode or "chat")
    # programming モードは relay の「120字以内・前置きNG」ルールがレシピJSON生成を壊すので
    # 必ず Anthropic API 直叩き。コスト面は Sonnet 4.6 で 1ターン数円レベル想定。
    _use_relay = RELAY_ENABLED and (req.mode or "chat") != "programming"
    if _use_relay:
        # Discord listener 経由（Max枠）。API 課金ゼロ。
        import uuid as _uuid
        req_id = _uuid.uuid4().hex[:8]
        try:
            reply_text = kids_relay.post_and_wait(
                req_id=req_id,
                system_prompt=system_prompt,
                history=history,
                mode=(req.mode or "chat"),
                user_id=user_id,
            )
        except Exception as e:
            raise HTTPException(502, f"relay error: {type(e).__name__}: {e}")
        if reply_text is None:
            raise HTTPException(504, "listener relay timeout")
        text = reply_text
        in_tok = 0
        out_tok = 0
    else:
        try:
            response = client.messages.create(
                model=active_model,
                max_tokens=max_tokens_for(req.mode or "chat"),
                system=system_prompt,
                messages=history,
            )
        except Exception as e:
            raise HTTPException(502, f"upstream error: {type(e).__name__}: {e}")
        text = response.content[0].text if response.content else ""
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens

    # 出力側 safety guard (inline / rule-based / <200ms)
    # log_only=True で1週間試運転中: 判定だけ記録、本文は元のまま送出する
    try:
        guard = kids_safety.output_response_guard_inline(
            child_id=user_id if user_id in CHILD_IDS else DEFAULT_CHILD_ID,  # unknown は型エラー回避
            mode=(req.mode or "chat"),
            assistant_text=text,
        )
        safety_log({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "gate": "output",
            "user_id": user_id,
            "mode": req.mode or "chat",
            "alert_level": guard.alert_level,
            "reason": guard.reason,
            "elapsed_ms": round(guard.elapsed_ms, 2),
            "frozen_suggested": guard.frozen_suggested,
            "rewritten": guard.text != text,
            "log_only": SAFETY_LOG_ONLY,
        })
        if not SAFETY_LOG_ONLY and guard.text != text:
            text = guard.text
    except Exception as exc:
        print(f"[safety_gate.output] error: {exc}", file=sys.stderr)

    # 入力側 memory gate (rule-based prefilter のみ、log-only)
    # LLM judge は Phase 2 で有効化、現在は決定をログだけ
    if user_id in kids_memory.VALID_USERS and latest_user_msg:
        try:
            mem_decision = kids_safety.quick_memory_prefilter(
                child_id=user_id,
                mode=(req.mode or "chat"),
                user_text=latest_user_msg,
                log_only=SAFETY_LOG_ONLY,
            )
            safety_log({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "gate": "input_memory",
                "user_id": user_id,
                "mode": req.mode or "chat",
                "action": mem_decision.action,
                "memory_type": mem_decision.memory_type,
                "sensitivity": mem_decision.sensitivity,
                "use_for_learning": mem_decision.use_for_learning,
                "alert_level": mem_decision.alert_level,
                "reason": mem_decision.reason,
                "frozen_suggested": mem_decision.frozen_suggested,
                "log_only": SAFETY_LOG_ONLY,
            })
        except Exception as exc:
            print(f"[safety_gate.input] error: {exc}", file=sys.stderr)

    duration = time.time() - t0
    in_price, out_price = price_for(active_model)
    cost_usd = (in_tok * in_price + out_tok * out_price) / 1_000_000
    cost_jpy = cost_usd * USD_TO_JPY

    log_entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "user_id": user_id,
        "duration_s": round(duration, 2),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_jpy": round(cost_jpy, 4),
        "model": active_model,
        "mode": req.mode or "chat",
        "user_msg_len": len(history[-1]["content"]) if history else 0,
        "reply_len": len(text),
    }
    today = datetime.date.today().isoformat()
    with open(LOG_DIR / f"usage_{today}.jsonl", "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # L0(Lv2)：メモリ追加イベント時のみ親モニタCHへ転送
    # chat/story モードは即時通知抑制（22:00日次サマリーに集約、2026-05-16/17 保護者指示）
    # explain モードはメモリ追加が学習進捗の指標なので即時通知継続
    _req_mode = req.mode or "chat"
    if user_id in kids_memory.VALID_USERS and memory_added and _req_mode not in ("chat", "story", "programming"):
        try:
            kids_monitor.log_chat(
                user_id=user_id,
                user_msg=latest_user_msg,
                reply=text,
                memory_added=memory_added,
                cost_jpy=cost_jpy,
            )
        except Exception as e:
            print(f"[monitor.log_chat] error: {e}", file=sys.stderr)

    # こども1向け: display_html で <ruby>ふりがな</ruby> 付与(TTSは元 reply のまま)
    display_html = None
    if user_id in FURIGANA_IDS:
        try:
            display_html = kids_furigana.add_furigana_html(
                text, child_id=user_id, mode=(req.mode or "chat"),
            )
        except Exception as e:
            print(f"[furigana] error: {e}", file=sys.stderr)
    return {
        "reply": text,
        "display_html": display_html,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_jpy": round(cost_jpy, 4),
        },
    }


# ---------------------------------------------------------------------------
# ストリーミング応答エンドポイント
# Anthropic SDK の messages.stream で逐次トークン送信、PWA 側で文単位 TTS。
# レスポンス形式: ndjson、各行が {"type": ...} の JSON。
#   {"type":"token","text":"..."}  - 部分テキスト
#   {"type":"done","usage":{...}}  - 完了
#   {"type":"frozen"}              - 凍結中
#   {"type":"error","message":"..."}
# ---------------------------------------------------------------------------

@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    if not req.messages:
        raise HTTPException(400, "messages empty")

    history = [{"role": m.role, "content": m.content} for m in req.messages[-40:]]
    user_id = (req.user_id or "unknown").strip().lower()
    if user_id not in CHILD_IDS + ("unknown",):
        user_id = "unknown"

    frozen_flag = user_id in CHILD_IDS and is_frozen(user_id)
    quiet_flag = user_id in CHILD_IDS and is_quiet_hours(mode=(req.mode or "chat"))
    # chat 日次予算チェック（1日120分）。story / programming は chat 予算と共有。
    _req_mode_now = req.mode or "chat"
    budget_flag = (
        user_id in CHILD_IDS
        and _req_mode_now in ("chat", "story", "programming")
        and not quiet_flag
        and chat_minutes_used_today(user_id) >= CHAT_DAILY_BUDGET_MIN
    )

    latest_user_msg = ""
    for m in reversed(history):
        if m["role"] == "user":
            latest_user_msg = m["content"]
            break

    session_duration_sec = 0
    if req.session_start_ts:
        session_duration_sec = max(0, int(time.time() - req.session_start_ts))

    system_prompt = build_system_prompt(
        user_id,
        latest_user_msg=latest_user_msg,
        session_duration_sec=session_duration_sec,
        mode=(req.mode or "chat"),
    )

    memory_added: list[dict] = []
    if (req.mode or "chat") == "chat" and user_id in kids_memory.VALID_USERS and latest_user_msg and not frozen_flag and not quiet_flag:
        try:
            memory_added = kids_memory.extract_from_message(user_id, latest_user_msg)
        except Exception as e:
            print(f"[memory.extract] error: {e}", file=sys.stderr)

    def event_gen():
        if frozen_flag:
            yield json.dumps({"type": "frozen"}, ensure_ascii=False) + "\n"
            return

        # 夜間シャットアウト：固定の優しいメッセージを文単位でstream（既存TTSフロー互換）
        if quiet_flag or budget_flag:
            msg = QUIET_REPLY if quiet_flag else CHAT_BUDGET_REPLY
            # 「。」区切りで段階送出。クライアント側TTSは「。」検知で発話キューに積む
            buf = ""
            for ch in msg:
                buf += ch
                if ch == "。":
                    yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
                    buf = ""
                    time.sleep(0.15)
            if buf:
                yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
            yield json.dumps({
                "type": "done",
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
                "quiet": True,
            }, ensure_ascii=False) + "\n"
            return

        t0 = time.time()
        full_text = ""
        in_tok = 0
        out_tok = 0
        active_model = model_for(req.mode or "chat")
        cur_messages = list(history)
        MAX_TOOL_LOOPS = 3  # 暴走防止

        # Discord listener relay 経路（Max枠で完結、Anthropic API 不要）
        # 受信した full text を「。！？\n」で擬似トークン化→既存PWA TTSフロー互換
        # programming モードは relay の長さ制限とフォーマット制約で recipe-json が壊れるので直叩き
        _stream_use_relay = RELAY_ENABLED and (req.mode or "chat") != "programming"
        if _stream_use_relay:
            import uuid as _uuid
            req_id = _uuid.uuid4().hex[:8]
            try:
                reply_text = kids_relay.post_and_wait(
                    req_id=req_id,
                    system_prompt=system_prompt,
                    history=cur_messages,
                    mode=(req.mode or "chat"),
                    user_id=user_id,
                )
            except Exception as e:
                print(f"[chat_stream] relay error: {e}", file=sys.stderr)
                yield json.dumps({"type": "error", "message": f"relay: {type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
                return
            if reply_text is None:
                yield json.dumps({"type": "error", "message": "listener relay timeout"}, ensure_ascii=False) + "\n"
                return

            # storyモードのみ：冒頭フィラー除去（既存ロジック流用）
            if (req.mode or "chat") == "story":
                reply_text = STORY_LEAD_FILLER_RE.sub("", reply_text, count=1).lstrip()

            full_text = reply_text
            # 文単位で送出（句点/改行で区切る。既存夜間ショートも同じ流儀）
            buf = ""
            for ch in reply_text:
                buf += ch
                if ch in ("。", "！", "？", "\n"):
                    yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"
                    buf = ""
                    time.sleep(0.08)
            if buf:
                yield json.dumps({"type": "token", "text": buf}, ensure_ascii=False) + "\n"

            duration = time.time() - t0
            log_entry = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "user_id": user_id,
                "duration_s": round(duration, 2),
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_jpy": 0.0,
                "model": active_model,
                "mode": req.mode or "chat",
                "user_msg_len": len(history[-1]["content"]) if history else 0,
                "reply_len": len(full_text),
                "stream": True,
                "relay": True,
            }
            today = datetime.date.today().isoformat()
            try:
                with open(LOG_DIR / f"usage_{today}.jsonl", "a") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[chat_stream] usage log error: {e}", file=sys.stderr)

            _req_mode = req.mode or "chat"
            if user_id in kids_memory.VALID_USERS and memory_added and _req_mode not in ("chat", "story"):
                try:
                    kids_monitor.log_chat(
                        user_id=user_id,
                        user_msg=latest_user_msg,
                        reply=full_text,
                        memory_added=memory_added,
                        cost_jpy=0.0,
                    )
                except Exception as e:
                    print(f"[monitor.log_chat] error: {e}", file=sys.stderr)

            if _req_mode == "story" and user_id in kids_memory.VALID_USERS:
                _name = CHILD_NAMES.get(user_id, user_id)
                try:
                    if len(history) <= 1:
                        kids_monitor.post_text(f"📖 **{_name}** がお話モード開始 (relay)\n👧 {latest_user_msg[:120]}")
                    if "[END]" in full_text:
                        kids_monitor.post_text(
                            f"📖✨ **{_name}** のお話エンディング到達 (relay)\n```\n{full_text[:600]}\n```"
                        )
                except Exception as e:
                    print(f"[monitor.story] error: {e}", file=sys.stderr)

            yield json.dumps({
                "type": "done",
                "usage": {"input_tokens": 0, "output_tokens": 0, "cost_jpy": 0.0},
                "relay": True,
            }, ensure_ascii=False) + "\n"
            return

        # storyモードのみ、応答冒頭の常套相槌（あそっか/なるほど/そっか/うーん…/あれ？等）を
        # プロンプト指示でも止まらない場合に備えてサーバ側でも除去する後処理フィルタ。
        # 各ターン応答の頭40文字 or 「。」or 「【第」のいずれかが先に来た時点で
        # 1回だけマッチ判定→除去→残りを通常送出。tool_use loop は1ループ目だけ作用。
        is_story_mode = (req.mode or "chat") == "story"
        try:
            for loop_i in range(MAX_TOOL_LOOPS):
                lead_buf = ""
                lead_done = not (is_story_mode and loop_i == 0)
                with client.messages.stream(
                    model=active_model,
                    max_tokens=max_tokens_for(req.mode or "chat"),
                    system=system_prompt,
                    messages=cur_messages,
                    tools=[WEATHER_TOOL],
                ) as stream:
                    for delta in stream.text_stream:
                        if not delta:
                            continue
                        full_text += delta
                        if not lead_done:
                            lead_buf += delta
                            if (
                                "【第" in lead_buf
                                or "。" in lead_buf
                                or "\n" in lead_buf
                                or len(lead_buf) >= STORY_LEAD_MAX
                            ):
                                cleaned = STORY_LEAD_FILLER_RE.sub("", lead_buf, count=1)
                                if cleaned:
                                    yield json.dumps({"type": "token", "text": cleaned}, ensure_ascii=False) + "\n"
                                lead_buf = ""
                                lead_done = True
                            continue
                        yield json.dumps({"type": "token", "text": delta}, ensure_ascii=False) + "\n"
                    # ストリーム終了時の取りこぼし flush
                    if not lead_done and lead_buf:
                        cleaned = STORY_LEAD_FILLER_RE.sub("", lead_buf, count=1)
                        if cleaned:
                            yield json.dumps({"type": "token", "text": cleaned}, ensure_ascii=False) + "\n"
                        lead_buf = ""
                        lead_done = True
                    final_msg = stream.get_final_message()
                    in_tok += final_msg.usage.input_tokens
                    out_tok += final_msg.usage.output_tokens
                tool_uses = [b for b in final_msg.content if getattr(b, "type", None) == "tool_use"]
                if not tool_uses:
                    break
                # assistant message を会話履歴に追加（text + tool_use 全部）
                assistant_content = []
                for b in final_msg.content:
                    bt = getattr(b, "type", None)
                    if bt == "text":
                        assistant_content.append({"type": "text", "text": b.text})
                    elif bt == "tool_use":
                        assistant_content.append({
                            "type": "tool_use", "id": b.id, "name": b.name, "input": b.input,
                        })
                cur_messages.append({"role": "assistant", "content": assistant_content})
                # tool 実行
                tool_results = []
                for tu in tool_uses:
                    if tu.name == "get_weather":
                        loc = (tu.input or {}).get("location")
                        result = get_weather_data(loc)
                    else:
                        result = {"error": f"unknown tool: {tu.name}"}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                cur_messages.append({"role": "user", "content": tool_results})
        except Exception as e:
            print(f"[chat_stream] upstream error: {e}", file=sys.stderr)
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
            return

        duration = time.time() - t0
        in_price, out_price = price_for(active_model)
        cost_usd = (in_tok * in_price + out_tok * out_price) / 1_000_000
        cost_jpy = cost_usd * USD_TO_JPY

        log_entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user_id": user_id,
            "duration_s": round(duration, 2),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_jpy": round(cost_jpy, 4),
            "model": active_model,
            "mode": req.mode or "chat",
            "user_msg_len": len(history[-1]["content"]) if history else 0,
            "reply_len": len(full_text),
            "stream": True,
        }
        today = datetime.date.today().isoformat()
        try:
            with open(LOG_DIR / f"usage_{today}.jsonl", "a") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[chat_stream] usage log error: {e}", file=sys.stderr)

        # 安全ゲート log-only（streaming path、本文は既に送出済なので enforce 不可、観察のみ）
        try:
            guard_s = kids_safety.output_response_guard_inline(
                child_id=user_id if user_id in CHILD_IDS else DEFAULT_CHILD_ID,
                mode=(req.mode or "chat"),
                assistant_text=full_text,
            )
            safety_log({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "gate": "output",
                "user_id": user_id,
                "mode": req.mode or "chat",
                "alert_level": guard_s.alert_level,
                "reason": guard_s.reason,
                "elapsed_ms": round(guard_s.elapsed_ms, 2),
                "frozen_suggested": guard_s.frozen_suggested,
                "rewritten": guard_s.text != full_text,
                "log_only": True,  # stream は常に observe only
                "stream": True,
            })
        except Exception as exc:
            print(f"[safety_gate.output_stream] error: {exc}", file=sys.stderr)
        if user_id in kids_memory.VALID_USERS and latest_user_msg:
            try:
                mem_dec = kids_safety.quick_memory_prefilter(
                    child_id=user_id,
                    mode=(req.mode or "chat"),
                    user_text=latest_user_msg,
                    log_only=SAFETY_LOG_ONLY,
                )
                safety_log({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "gate": "input_memory",
                    "user_id": user_id,
                    "mode": req.mode or "chat",
                    "action": mem_dec.action,
                    "memory_type": mem_dec.memory_type,
                    "sensitivity": mem_dec.sensitivity,
                    "use_for_learning": mem_dec.use_for_learning,
                    "alert_level": mem_dec.alert_level,
                    "reason": mem_dec.reason,
                    "frozen_suggested": mem_dec.frozen_suggested,
                    "log_only": SAFETY_LOG_ONLY,
                    "stream": True,
                })
            except Exception as exc:
                print(f"[safety_gate.input_stream] error: {exc}", file=sys.stderr)

        # chat/story モードは即時通知抑制（22:00日次サマリーに集約）
        _req_mode = req.mode or "chat"
        if user_id in kids_memory.VALID_USERS and memory_added and _req_mode not in ("chat", "story"):
            try:
                kids_monitor.log_chat(
                    user_id=user_id,
                    user_msg=latest_user_msg,
                    reply=full_text,
                    memory_added=memory_added,
                    cost_jpy=cost_jpy,
                )
            except Exception as e:
                print(f"[monitor.log_chat] error: {e}", file=sys.stderr)

        # story モード：開始/エンディングだけ即時通知（中間章は22:00サマリー集約）
        if _req_mode == "story" and user_id in kids_memory.VALID_USERS:
            _name = CHILD_NAMES.get(user_id, user_id)
            try:
                # 初回ターン（履歴1件＝今のuser発言のみ）
                if len(history) <= 1:
                    kids_monitor.post_text(f"📖 **{_name}** がお話モード開始\n👧 {latest_user_msg[:120]}")
                # エンディング検知
                if "[END]" in full_text:
                    kids_monitor.post_text(
                        f"📖✨ **{_name}** のお話エンディング到達\n```\n{full_text[:600]}\n```"
                    )
            except Exception as e:
                print(f"[monitor.story] error: {e}", file=sys.stderr)

        # こども1向け display_html (ふりがな付き) を done に同梱
        display_html = None
        if user_id in FURIGANA_IDS:
            try:
                display_html = kids_furigana.add_furigana_html(
                    full_text, child_id=user_id, mode=(req.mode or "chat"),
                )
            except Exception as e:
                print(f"[furigana.stream] error: {e}", file=sys.stderr)
        yield json.dumps({
            "type": "done",
            "usage": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_jpy": round(cost_jpy, 4),
            },
            "display_html": display_html,
        }, ensure_ascii=False) + "\n"

    return StreamingResponse(event_gen(), media_type="application/x-ndjson")


# index.html / マニフェストはリロード毎に必ず最新取得させる（修正即反映のため）
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
def root():
    return FileResponse(PWA_DIR / "index.html", headers=NO_CACHE_HEADERS)


@app.get("/api/children")
def api_children():
    """Expose the configured roster so the client can render without hard-coded names."""
    return {
        "assistant_name": ASSISTANT_NAME,
        "children": [
            {
                "id": c["id"],
                "display_name": c.get("display_name", c["id"]),
                "grade": c.get("grade"),
                "furigana": bool(c.get("furigana")),
                "theme": c.get("theme", {}),
            }
            for c in kids_config.children()
        ],
    }


@app.get("/child1")
def root_child1():
    return FileResponse(PWA_DIR / "index.html", headers=NO_CACHE_HEADERS)


@app.get("/child2")
def root_child2():
    return FileResponse(PWA_DIR / "index.html", headers=NO_CACHE_HEADERS)


@app.get("/manifest.json")
def manifest():
    return FileResponse(PWA_DIR / "manifest.json", headers=NO_CACHE_HEADERS)


@app.get("/manifest-child1.json")
def manifest_child1():
    return FileResponse(PWA_DIR / "manifest-child1.json", headers=NO_CACHE_HEADERS)


@app.get("/manifest-child2.json")
def manifest_child2():
    return FileResponse(PWA_DIR / "manifest-child2.json", headers=NO_CACHE_HEADERS)


@app.get("/icon-{filename}")
def icon(filename: str):
    """子別アイコン（icon-192-child1.png 等）も含めて配信。"""
    safe = filename.replace("/", "").replace("..", "")
    path = PWA_DIR / f"icon-{safe}"
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "icon not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# 会話履歴の永続化（端末/ブラウザを跨いで保持）
# クライアントの localStorage はブラウザ依存（Silk→Fully Kiosk切替で消える）
# サーバ側にも保存しておき、起動時にサーバ優先で復元する。
# ---------------------------------------------------------------------------

HISTORY_DIR = BASE / "conversations"
HISTORY_DIR.mkdir(exist_ok=True)
HISTORY_MAX_ITEMS_SERVER = 500  # サーバ側は端末側より多めに保持


class HistorySaveRequest(BaseModel):
    user_id: str
    items: list[dict]  # [{"role":"user"/"assistant","content":"..."}, ...]
    mode: str | None = "chat"  # "chat" or "explain"
    cleared_ack_epoch: float | None = 0.0  # クライアントが認識している cleared_at（再汚染防止用）


def _history_path(user_id: str, mode: str) -> Path:
    """chat → {user_id}.json (legacy filename), explain → {user_id}_explain.json,
    story → {user_id}_story.json, programming → {user_id}_programming.json"""
    if mode == "explain":
        return HISTORY_DIR / f"{user_id}_explain.json"
    if mode == "story":
        return HISTORY_DIR / f"{user_id}_story.json"
    if mode == "programming":
        return HISTORY_DIR / f"{user_id}_programming.json"
    return HISTORY_DIR / f"{user_id}.json"


# 「私のさくひん集」=発明レシピ永続化（jsonl append）
RECIPES_DIR = BASE / "recipes"
RECIPES_DIR.mkdir(exist_ok=True)


def _recipes_path(user_id: str) -> Path:
    return RECIPES_DIR / f"{user_id}_recipes.jsonl"


class RecipeSaveRequest(BaseModel):
    user_id: str
    mode: str | None = "programming"
    kind: str | None = "recipe"
    recipe_id: str | None = None
    title: str | None = None
    recipe: dict
    saved_at: str | None = None


@app.post("/api/programming/recipe/save")
def programming_recipe_save(req: RecipeSaveRequest):
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    if not req.recipe or not isinstance(req.recipe, dict):
        raise HTTPException(400, "recipe required")
    path = _recipes_path(user_id)
    entry = {
        "user_id": user_id,
        "saved_at": req.saved_at or datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "recipe_id": req.recipe_id or req.recipe.get("recipe_id"),
        "title": req.title or req.recipe.get("title"),
        "recipe": req.recipe,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True, "recipe_id": entry["recipe_id"]}


# ===========================================================================
# 発明モード ⇄ Pico MQTT ブリッジ (2026-05-30)
#   レシピを実機 Pico へ送る / 止める / 状態を返す。
#   依存追加なしで mosquitto_pub / mosquitto_sub を subprocess 経由で叩く。
#   broker は Pi 自身 (127.0.0.1:1883)。Pico は kids/{user}/recipe を購読し、
#   kids/{user}/status に READY / RUNNING:<id> / EMERGENCY_STOPPED 等を publish。
# ===========================================================================
MQTT_HOST = os.environ.get("KIDS_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("KIDS_MQTT_PORT", "1883"))

# Pico 必須フィールド（micropython_runtime_v0_1.validate_recipe と一致）
_PICO_REQUIRED_FIELDS = ("recipe_id", "title", "triggers", "rules", "actions", "stop", "safety")

# user_id -> {"status": str, "ts": epoch}
_pico_status: dict[str, dict] = {}
_pico_status_lock = threading.Lock()


def _mqtt_publish(topic: str, payload: str | None, retain: bool = False) -> None:
    """1メッセージ publish。payload=None で空メッセージ。retain=True で保持。

    retain の意図: Pico runtime v0.1 は idle 待機中に WDT を feed しないため
    約10秒周期で再起動しており、非retained だと再起動の瞬間に publish した
    recipe が取りこぼされる。retain しておけば Pico が再接続して subscribe した
    瞬間に必ず最新 recipe を受け取れる。stop 時に空 retain で確実にクリアする。
    """
    args = ["mosquitto_pub", "-h", MQTT_HOST, "-p", str(MQTT_PORT), "-t", topic]
    if retain:
        args.append("-r")
    if payload is None:
        args.append("-n")
        subprocess.run(args, check=True, timeout=5)
    else:
        # 任意長・任意文字を安全に渡すため stdin 経由 (-s)
        args.append("-s")
        subprocess.run(args, input=payload.encode("utf-8"), check=True, timeout=5)


def _pico_status_listener() -> None:
    """kids/+/status を購読し、子別の最新ステータスを保持する常駐スレッド。"""
    while True:
        try:
            proc = subprocess.Popen(
                ["mosquitto_sub", "-h", MQTT_HOST, "-p", str(MQTT_PORT),
                 "-t", "kids/+/status", "-v"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            for line in proc.stdout:  # "kids/child1/status READY"
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 1)
                topic = parts[0]
                msg = parts[1] if len(parts) > 1 else ""
                seg = topic.split("/")
                if len(seg) >= 3 and seg[0] == "kids" and seg[2] == "status":
                    uid = seg[1]
                    with _pico_status_lock:
                        _pico_status[uid] = {"status": msg, "ts": time.time()}
                    print(f"[pico_status] {uid}: {msg}", file=sys.stderr)
        except Exception as exc:
            print(f"[pico_status_listener] err: {exc}", file=sys.stderr)
        time.sleep(3)  # mosquitto_sub が落ちたら再接続


# 常駐スレッド起動（mosquitto_sub が無い環境でも import は失敗させない）
try:
    threading.Thread(target=_pico_status_listener, daemon=True).start()
except Exception as _exc:  # pragma: no cover
    print(f"[pico_status_listener] start failed: {_exc}", file=sys.stderr)


class RecipeRunRequest(BaseModel):
    user_id: str
    recipe: dict


@app.post("/api/programming/recipe/run")
def programming_recipe_run(req: RecipeRunRequest):
    """完成レシピを実機 Pico へ送信し、RUNNING 確認まで待って返す。"""
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    if not req.recipe or not isinstance(req.recipe, dict):
        raise HTTPException(400, "recipe required")
    missing = [k for k in _PICO_REQUIRED_FIELDS if k not in req.recipe]
    if missing:
        raise HTTPException(400, "recipe missing fields: " + ",".join(missing))

    payload = json.dumps(req.recipe, ensure_ascii=False)
    topic = f"kids/{user_id}/recipe"
    sent_at = time.time()
    try:
        # retain=True: Pico が再起動周期で切断していても、再接続時に確実に受信
        _mqtt_publish(topic, payload, retain=True)
    except FileNotFoundError:
        raise HTTPException(503, "mosquitto_pub not installed")
    except Exception as exc:
        raise HTTPException(502, f"mqtt publish failed: {exc}")

    # Pico からの RUNNING / INVALID 応答を待つ。Pico は約10秒周期で再起動する
    # ことがあるため、再接続→RUNNING を拾えるよう最大 12 秒待つ。
    confirmed = False
    pico_status = None
    deadline = sent_at + 12.0
    while time.time() < deadline:
        with _pico_status_lock:
            st = _pico_status.get(user_id)
        if st and st["ts"] >= sent_at:
            pico_status = st["status"]
            if pico_status.startswith("RUNNING") or pico_status.startswith("AWAITING"):
                confirmed = True
                break
            if pico_status.startswith("INVALID_RECIPE") or pico_status.startswith("BAD_JSON"):
                break
        time.sleep(0.2)

    return {
        "ok": True,
        "topic": topic,
        "recipe_id": req.recipe.get("recipe_id"),
        "confirmed": confirmed,
        "pico_status": pico_status,
    }


class PicoStopRequest(BaseModel):
    user_id: str


@app.post("/api/programming/stop")
def programming_stop(req: PicoStopRequest):
    """🛑 ぜんぶ止める: kids/{user}/stop へ空メッセージを publish。"""
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    try:
        _mqtt_publish(f"kids/{user_id}/stop", None)
        # 保持中の recipe をクリア（空 retain）。これをしないと Pico が再起動の
        # たびに retained recipe を再実行してしまい、stop が永続しない。
        _mqtt_publish(f"kids/{user_id}/recipe", None, retain=True)
    except FileNotFoundError:
        raise HTTPException(503, "mosquitto_pub not installed")
    except Exception as exc:
        raise HTTPException(502, f"mqtt publish failed: {exc}")
    return {"ok": True}


@app.get("/api/programming/pico/status")
def programming_pico_status(user_id: str):
    """Pico の最新ステータス + 鮮度を返す。PWA のアトリエ状態ランプ用。

    注意: Pico runtime v0.1 は定期ハートビートを送らない（READY/RUNNING 等の
    状態変化時のみ publish）。よって age_s が古くても電源が入っていれば生きている。
    connected は「最近応答があった」程度の目安で、断定はしない。
    """
    user_id = (user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    with _pico_status_lock:
        st = _pico_status.get(user_id)
    if not st:
        return {"seen": False, "connected": False, "status": None, "age_s": None}
    age = time.time() - st["ts"]
    status = st["status"]
    running = status.startswith("RUNNING") or status.startswith("AWAITING")
    # READY/RUNNING を 120 秒以内に受信していれば「つながってる」目安
    fresh = age < 120
    connected = fresh and (status == "READY" or running)
    return {
        "seen": True,
        "connected": connected,
        "running": running,
        "status": status,
        "age_s": round(age, 1),
    }


class WiringRenderRequest(BaseModel):
    user_id: str
    text: str
    mode: str | None = "wiring"


@app.post("/api/programming/wiring/render")
def programming_wiring_render(req: WiringRenderRequest):
    """配線ガイドの固定文字列を サーバ側でふりがな処理して返す。
    クライアント側で kidWiringBody/kidWiringHint/intro/finish テキストをこれに通す。"""
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    if user_id not in FURIGANA_IDS:
        return {"display_html": req.text}  # child2等はそのまま
    try:
        html_out = kids_furigana.add_furigana_html(req.text or "", child_id=user_id, mode="wiring")
    except Exception as e:
        print(f"[wiring.render] error: {e}", file=sys.stderr)
        return {"display_html": req.text}
    return {"display_html": html_out}


@app.post("/api/programming/wiring/photo-check")
async def programming_wiring_photo_check(
    user_id: str = Form(...),
    recipe_id: str | None = Form(None),
    recipe_json: str | None = Form(None),
    photo: UploadFile = File(...),
):
    """配線写真を Sonnet 4.6 で**項目別判定**して返す。
    - 「ぜんぶOK」ではなく item ごとに verdict + confidence + note
    - AI判定の限界を子供に隠さず、ada人確認推奨フラグも返す"""
    user_id = (user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    if photo.content_type not in VISION_ALLOWED_MIME:
        raise HTTPException(400, f"unsupported mime: {photo.content_type}")
    photo_bytes = await photo.read()
    if len(photo_bytes) > VISION_MAX_BYTES:
        raise HTTPException(413, "photo too large")

    # レシピから使用部品リスト抽出
    recipe = {}
    try:
        if recipe_json:
            recipe = json.loads(recipe_json)
    except Exception:
        recipe = {}
    devices = set()
    for t in (recipe.get("triggers") or []):
        if t.get("source"): devices.add(t["source"])
    for a in (recipe.get("actions") or []):
        if a.get("device"): devices.add(a["device"])
    device_list = "、".join(sorted(devices)) or "(レシピ情報なし)"

    system = (
        "あなたは子供向け電子工作の配線チェッカー。Pico 2 W + Freenove starter kit 想定。\n"
        "写真をみて、項目ごとに **だいじょうぶそう/きをつけて/おとな確認推奨** を判定して JSON で返す。\n"
        "AI判定の限界を子供に隠さない: 抵抗値・極性・5V系・配線色は confidence=low or medium にして adult_required=true。\n"
        "「ぜんぶOK」は禁止。1項目ずつ verdict をつける。\n\n"
        f"このレシピで使う部品: {device_list}"
    )
    user_prompt = (
        "写真の配線を以下の項目別に判定して JSON だけ返してください:\n"
        "1. 線の本数 (期待数と合うか)\n"
        "2. 差す場所 (ピンを間違えてないか)\n"
        "3. LEDの極性 (長い足が+側か)\n"
        "4. 抵抗の値 (220Ω が入ってるか/カラーコード判定)\n"
        "5. 5V系/GND (混線/逆挿しがないか)\n"
        "6. その他気になる点\n\n"
        "返す JSON フォーマット (厳守):\n"
        "{\n"
        '  "items": [\n'
        '    {"item": "線の本数", "verdict": "ok|warn|adult", "confidence": "high|medium|low", "note": "短い説明(20字程度)"}\n'
        "  ],\n"
        '  "overall": "全体感の一言コメント (子供向け、「だいじょうぶそう」「ここはおとうさんに見てもらってね」)",\n'
        '  "needs_adult_review": true\n'
        "}\n\n"
        "余計な前置きや説明は禁止、JSON だけ。"
    )

    import base64 as _b64
    img_b64 = _b64.b64encode(photo_bytes).decode("ascii")
    img_mime = photo.content_type or "image/jpeg"

    try:
        resp = client.messages.create(
            model=MODEL_VISION,
            max_tokens=900,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": img_mime, "data": img_b64}},
                    {"type": "text", "text": user_prompt},
                ],
            }],
        )
    except Exception as e:
        raise HTTPException(502, f"vision error: {type(e).__name__}: {e}")
    txt = resp.content[0].text if resp.content else ""
    # JSONだけ抽出 (前後の余計な文字に頑健に)
    j = None
    try:
        j = json.loads(txt)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", txt)
        if m:
            try:
                j = json.loads(m.group(0))
            except Exception:
                j = None
    if not j:
        return {"ok": False, "raw": txt[:500]}
    return {"ok": True, "result": j}


@app.post("/api/programming/wiring/notify-parent")
async def programming_wiring_notify_parent(
    user_id: str = Form(...),
    recipe_title: str | None = Form(None),
    note: str | None = Form(None),
    photo: UploadFile | None = File(None),
):
    """配線確認のため親モニタCHに通知。出張中の親が遠隔チェックできる動線。"""
    user_id = (user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    photo_bytes = None
    photo_mime = "image/jpeg"
    if photo is not None:
        photo_bytes = await photo.read()
        if len(photo_bytes) > 8 * 1024 * 1024:
            raise HTTPException(413, "photo too large (>8MB)")
        photo_mime = photo.content_type or "image/jpeg"
    ok = kids_monitor.post_wiring_check_request(
        user_id=user_id,
        recipe_title=recipe_title,
        photo_bytes=photo_bytes,
        photo_mime=photo_mime,
        note=note or "",
    )
    return {"ok": ok}


@app.get("/api/programming/recipes/list")
def programming_recipes_list(user_id: str):
    user_id = (user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    path = _recipes_path(user_id)
    if not path.exists():
        return {"items": [], "count": 0}
    items: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"[recipes_list] read error: {exc}", file=sys.stderr)
    return {"items": items, "count": len(items)}


@app.post("/api/history/save")
def history_save(req: HistorySaveRequest):
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    mode = (req.mode or "chat").strip().lower()
    if mode not in VALID_MODES:
        mode = "chat"
    items = req.items or []
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    if not items:
        return {"ok": False, "skipped": "empty items", "preserved": True}
    path_existing = _history_path(user_id, mode)
    server_cleared_at_epoch = 0.0
    if path_existing.exists():
        try:
            with open(path_existing, encoding="utf-8") as f:
                existing = json.load(f)
            old_count = len(existing.get("items", []))
            server_cleared_at_epoch = float(existing.get("cleared_at_epoch") or 0.0)
            if old_count > 10 and len(items) < old_count / 2:
                print(f"[history_save] WARN: {user_id}/{mode} shrinking {old_count}→{len(items)}", file=sys.stderr)
        except Exception:
            pass
    # 親が明示的にクリアした後、クライアントがclear_ackを送らない限り保存を拒否（再汚染防止）
    if server_cleared_at_epoch > 0:
        client_ack_epoch = float(getattr(req, "cleared_ack_epoch", 0.0) or 0.0)
        if client_ack_epoch < server_cleared_at_epoch:
            print(f"[history_save] REJECT: {user_id}/{mode} client ack {client_ack_epoch} < cleared {server_cleared_at_epoch}", file=sys.stderr)
            return {"ok": False, "rejected": "cleared_by_parent", "cleared_at_epoch": server_cleared_at_epoch}
    items = items[-HISTORY_MAX_ITEMS_SERVER:]
    safe = []
    for m in items:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            entry: dict = {"role": role, "content": content}
            ts_epoch = m.get("ts")
            if isinstance(ts_epoch, (int, float)) and ts_epoch > 0:
                entry["ts"] = float(ts_epoch)
            mid = m.get("id")
            if isinstance(mid, str) and mid:
                entry["id"] = mid[:64]
            if m.get("starred"):
                entry["starred"] = True
            if m.get("edited"):
                entry["edited"] = True
            safe.append(entry)
    payload = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(safe),
        "items": safe,
        "mode": mode,
    }
    path = path_existing
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return {"ok": True, "count": len(safe), "mode": mode}


@app.get("/api/history/load")
def history_load(user_id: str, mode: str = "chat"):
    user_id = (user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    mode = (mode or "chat").strip().lower()
    if mode not in VALID_MODES:
        mode = "chat"
    path = _history_path(user_id, mode)
    if not path.exists():
        return {"items": [], "ts": None, "count": 0, "mode": mode}
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"[history_load] read error: {e}", file=sys.stderr)
        return {"items": [], "ts": None, "count": 0, "error": str(e), "mode": mode}
    return {
        "items": payload.get("items", []),
        "ts": payload.get("ts"),
        "count": payload.get("count", len(payload.get("items", []))),
        "mode": mode,
        "cleared_at": payload.get("cleared_at"),
        "cleared_at_epoch": payload.get("cleared_at_epoch") or 0.0,
    }


class HistoryClearRequest(BaseModel):
    user_id: str
    mode: str | None = "chat"


@app.post("/api/history/clear")
def history_clear(req: HistoryClearRequest):
    """子が🔄ボタンで「やりなおす」した時にサーバ履歴を空にする。
    cleared_at_epoch を立てて、debounce 中の旧履歴 save が走っても再汚染しないようにする。
    親 clear（手動で json 書換）と同じ機構を流用。"""
    user_id = (req.user_id or "").strip().lower()
    if user_id not in kids_memory.VALID_USERS:
        raise HTTPException(400, "invalid user_id")
    mode = (req.mode or "chat").strip().lower()
    if mode not in VALID_MODES:
        mode = "chat"
    cleared_at = datetime.datetime.now().isoformat(timespec="seconds")
    cleared_at_epoch = datetime.datetime.now().timestamp()
    payload = {
        "ts": cleared_at,
        "count": 0,
        "items": [],
        "mode": mode,
        "cleared_at": cleared_at,
        "cleared_at_epoch": cleared_at_epoch,
    }
    path = _history_path(user_id, mode)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return {"ok": True, "mode": mode, "cleared_at_epoch": cleared_at_epoch}


@app.get("/api/frozen")
def api_frozen(user_id: str):
    user_id = (user_id or "").strip().lower()
    if user_id not in CHILD_IDS:
        return {"frozen": False, "user_id": user_id}
    return {"frozen": is_frozen(user_id), "user_id": user_id}


@app.get("/api/budget")
def api_budget(user_id: str, mode: str = "chat"):
    """Daily chat budget remaining (minutes). Used by PWA to show countdown.
    explainモードは予算管理しないので unlimited 扱い。"""
    user_id = (user_id or "").strip().lower()
    if user_id not in CHILD_IDS or mode != "chat":
        return {
            "user_id": user_id,
            "mode": mode,
            "budget_min": None,
            "used_min": 0,
            "remaining_min": None,
            "exhausted": False,
        }
    used = chat_minutes_used_today(user_id)
    remaining = max(0, CHAT_DAILY_BUDGET_MIN - used)
    return {
        "user_id": user_id,
        "mode": "chat",
        "budget_min": CHAT_DAILY_BUDGET_MIN,
        "used_min": used,
        "remaining_min": remaining,
        "exhausted": remaining == 0,
    }


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


# 3Dモデルカタログ（topic → glb URL マップ）。クライアントが起動時に1回取得。
_MODEL3D_CATALOG_PATH = BASE / "lib" / "model3d_catalog.json"
_MODEL3D_CATALOG_CACHE: dict | None = None


def _load_model3d_catalog() -> dict:
    global _MODEL3D_CATALOG_CACHE
    if _MODEL3D_CATALOG_CACHE is not None:
        return _MODEL3D_CATALOG_CACHE
    try:
        _MODEL3D_CATALOG_CACHE = json.loads(_MODEL3D_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[model3d] catalog load failed: {e}", file=sys.stderr)
        _MODEL3D_CATALOG_CACHE = {"topics": {}}
    return _MODEL3D_CATALOG_CACHE


@app.get("/api/model3d/catalog")
def api_model3d_catalog():
    return _load_model3d_catalog()


# ---------------------------------------------------------------------------
# あんざん（暗算）モード - そろばん検定準拠の加減算問題ジェネレータ
# LLM非使用、純Python乱数生成。こども1（小3・そろばん3級・暗算スタート段階）向け
# 級設定はテキスト「10級〜7級」に合わせた暫定値、塾教材に合わせて要調整可
# ---------------------------------------------------------------------------
import random as _abacus_random

ABACUS_LEVELS = {
    10: {"digits": 2, "terms": 2, "ops": ["+"],         "flash_ms": 1500, "label": "10級 (2けた2口・たすだけ)"},
    9:  {"digits": 2, "terms": 3, "ops": ["+", "-"],   "flash_ms": 1300, "label": "9級 (2けた3口・たす/ひく)"},
    8:  {"digits": 2, "terms": 4, "ops": ["+", "-"],   "flash_ms": 1200, "label": "8級 (2けた4口・たす/ひく)"},
    7:  {"digits": 3, "terms": 3, "ops": ["+", "-"],   "flash_ms": 1300, "label": "7級 (3けた3口・たす/ひく)"},
}


def _gen_abacus_problem(grade: int) -> dict:
    cfg = ABACUS_LEVELS.get(grade, ABACUS_LEVELS[10])
    digits = cfg["digits"]
    terms = cfg["terms"]
    ops = cfg["ops"]
    low = 10 ** (digits - 1)
    high = 10 ** digits - 1

    seq = []
    running = _abacus_random.randint(low, high)
    seq.append({"op": "+", "val": running})
    for _ in range(terms - 1):
        op = _abacus_random.choice(ops)
        if op == "-":
            max_sub = min(high, max(low, running - 1))
            if max_sub < low:
                op = "+"
        if op == "+":
            val = _abacus_random.randint(low, high)
            running += val
        else:
            val = _abacus_random.randint(low, max_sub)
            running -= val
        seq.append({"op": op, "val": val})
    return {
        "grade": grade,
        "label": cfg["label"],
        "flash_ms": cfg["flash_ms"],
        "seq": seq,
        "answer": running,
    }


class AbacusGenReq(BaseModel):
    grade: int = 10


@app.post("/api/abacus/generate")
def api_abacus_generate(req: AbacusGenReq):
    grade = req.grade if req.grade in ABACUS_LEVELS else 10
    return _gen_abacus_problem(grade)


@app.get("/api/abacus/levels")
def api_abacus_levels():
    return {
        "levels": [
            {"grade": g, "label": cfg["label"], "flash_ms": cfg["flash_ms"]}
            for g, cfg in sorted(ABACUS_LEVELS.items(), reverse=True)
        ]
    }


# ---------------------------------------------------------------------------
# クライアント自動リロード用バージョン情報
# クライアントが30秒毎にpollし、index.htmlの更新を検知して自動reload
# ---------------------------------------------------------------------------

@app.get("/api/version")
def api_version():
    try:
        mtime = (PWA_DIR / "index.html").stat().st_mtime
        # 整数化＋短文字列化（ハッシュより小さい・人間にも追える）
        v = str(int(mtime))
    except Exception:
        v = "0"
    return Response(
        content=json.dumps({"v": v}),
        media_type="application/json",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/api/usage")
def usage_summary():
    today = datetime.date.today().isoformat()
    log_path = LOG_DIR / f"usage_{today}.jsonl"
    if not log_path.exists():
        return {"date": today, "calls": 0, "total_jpy": 0}
    total_jpy = 0.0
    calls = 0
    with open(log_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
                total_jpy += entry.get("cost_jpy", 0)
                calls += 1
            except Exception:
                pass
    return {"date": today, "calls": calls, "total_jpy": round(total_jpy, 2)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
