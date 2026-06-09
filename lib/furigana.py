"""kids-ai furigana post-processor (for children with furigana enabled in config).

サーバ側で <ruby>漢字<rt>かんじ</rt></ruby> HTMLを生成。TTSには元テキストを渡す(2重読み防止)。
- pykakasi で読み生成、難読/固有語は CUSTOM_READINGS で固定
- モード別強度(wiring=最強, programming/explain=強, chat/story=中)
- furigana を有効にするかは config の各 child の "furigana" フラグで決まる
"""
from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Literal

from pykakasi import kakasi

ChildId = str  # child id as defined in config/children.json
Mode = Literal["chat", "explain", "story", "abacus", "vision", "programming", "wiring"]

_kks = kakasi()

KANJI_RE = re.compile(r"[一-龥々]+")
URL_RE = re.compile(r"https?://\S+")
RUBY_EXISTING_RE = re.compile(r"<ruby>[\s\S]*?</ruby>")

CUSTOM_READINGS = {
    # 名詞
    "発明": "はつめい",
    "配線": "はいせん",
    "電源": "でんげん",
    "確認": "かくにん",
    "写真": "しゃしん",
    "抵抗": "ていこう",
    "距離": "きょり",
    "接続": "せつぞく",
    "危険": "きけん",
    "実験": "じっけん",
    "部品": "ぶひん",
    "送信": "そうしん",
    "設定": "せってい",
    "反応": "はんのう",
    "通電": "つうでん",
    "電気": "でんき",
    "方向": "ほうこう",
    "成功": "せいこう",
    "失敗": "しっぱい",
    "算数": "さんすう",
    "問題": "もんだい",
    "答え": "こたえ",
    "石": "いし",
    "色": "いろ",
    "線": "せん",
    "安全": "あんぜん",
    "大人": "おとな",
    "お父さん": "おとうさん",
    "お母さん": "おかあさん",
    "赤外線": "せきがいせん",
    "極性": "きょくせい",
    "通信": "つうしん",
    "音": "おと",
    "光": "ひかり",
    "電池": "でんち",
    "本数": "ほんすう",
    "場所": "ばしょ",
    "判定": "はんてい",
    "回路": "かいろ",
    "足": "あし",
    "色違い": "いろちがい",
    "極": "きょく",
    # 動詞活用（pykakasi誤読対策）
    "通る": "とおる",
    "通った": "とおった",
    "通って": "とおって",
    "通っ": "とおっ",
    "抜く": "ぬく",
    "抜いて": "ぬいて",
    "抜い": "ぬい",
    "抜": "ぬ",
    "止まる": "とまる",
    "止める": "とめる",
    "止ま": "とま",
    "止め": "とめ",
    "止": "と",
    "光る": "ひかる",
    "光らせる": "ひからせる",
    "光っ": "ひかっ",
    "近づく": "ちかづく",
    "近づい": "ちかづい",
    "近づける": "ちかづける",
    "近": "ちか",
    "離れる": "はなれる",
    "離れ": "はなれ",
    "離": "はな",
    "繋ぐ": "つなぐ",
    "繋い": "つない",
    "繋": "つな",
    "変わる": "かわる",
    "変わ": "かわ",
    "変": "か",
    "見る": "みる",
    "見て": "みて",
    "見": "み",
    "作る": "つくる",
    "作ろう": "つくろう",
    "作っ": "つくっ",
    "作": "つく",
    "回す": "まわす",
    "鳴る": "なる",
    "鳴っ": "なっ",
    "鳴": "な",
    "押す": "おす",
    "押し": "おし",
    "押": "お",
    "差す": "さす",
    "差し": "さし",
    "間違える": "まちがえる",
    "間違え": "まちがえ",
    "間違": "まちが",
    "助け": "たすけ",
    "進む": "すすむ",
    "進": "すす",
    "戻る": "もどる",
    "戻": "もど",
    "始める": "はじめる",
    "始め": "はじめ",
    "終わる": "おわる",
    "終わり": "おわり",
    "終わ": "おわ",
}

MODE_STRENGTH = {
    "chat": "medium",
    "explain": "strong",
    "story": "medium",
    "abacus": "medium",
    "vision": "medium",
    "programming": "strong",
    "wiring": "strongest",
}


@lru_cache(maxsize=4096)
def reading_for(word: str) -> str:
    if word in CUSTOM_READINGS:
        return CUSTOM_READINGS[word]
    try:
        result = _kks.convert(word)
        hira = "".join(item.get("hira", item.get("kana", item.get("orig", ""))) for item in result)
    except Exception:
        return ""
    if not hira or hira == word:
        return ""
    return hira


def _should_add_ruby(word: str, *, child_id: ChildId, mode: Mode) -> bool:
    if child_id != "child1":
        return False
    if word in CUSTOM_READINGS:
        return True
    strength = MODE_STRENGTH.get(mode, "medium")
    if strength == "strongest":
        return True
    if strength == "strong":
        return len(word) >= 1
    if strength == "medium":
        return len(word) >= 2
    return len(word) >= 3


KATAKANA_ONLY_RE = re.compile(r"^[ァ-ヴーｦ-ﾟ]+$")
HAS_KANJI_RE = re.compile(r"[一-龥々]")


def add_furigana_html(text: str, *, child_id: ChildId, mode: Mode) -> str:
    """元テキストを <ruby>HTML化。escape済みなので innerHTML 安全。
    pykakasi の token単位 (orig+hira) を使って文脈考慮の読み生成。
    カタカナ語にはルビを付けない (ドラゴンゲート等)。"""
    if not text or child_id != "child1":
        return html.escape(text or "")

    # 既存の <ruby> ブロックを保持
    placeholders: dict[str, str] = {}
    def hold_ruby(m: re.Match) -> str:
        key = f"\x01RUBY_{len(placeholders)}\x01"
        placeholders[key] = m.group(0)
        return key
    work = RUBY_EXISTING_RE.sub(hold_ruby, text)

    # URLを退避
    urls: dict[str, str] = {}
    def hold_url(m: re.Match) -> str:
        key = f"\x02URL_{len(urls)}\x02"
        urls[key] = m.group(0)
        return key
    work = URL_RE.sub(hold_url, work)

    # pykakasi で全文をトークン化(文脈ありで読み生成)
    try:
        tokens = _kks.convert(work)
    except Exception:
        tokens = [{"orig": work, "hira": ""}]

    out_parts: list[str] = []
    for tok in tokens:
        orig = tok.get("orig", "")
        if not orig:
            continue
        # placeholder/URLはそのまま戻す
        if orig in placeholders:
            out_parts.append(placeholders[orig])
            continue
        if orig in urls:
            out_parts.append(html.escape(urls[orig]))
            continue
        # カタカナのみは ruby 不要
        if KATAKANA_ONLY_RE.match(orig):
            out_parts.append(html.escape(orig))
            continue
        # 漢字を含むか
        if not HAS_KANJI_RE.search(orig):
            out_parts.append(html.escape(orig))
            continue
        # 強度判定
        if not _should_add_ruby(orig, child_id=child_id, mode=mode):
            out_parts.append(html.escape(orig))
            continue
        # 読み取得 (CUSTOMが優先、pykakasi がフォールバック)
        if orig in CUSTOM_READINGS:
            yomi = CUSTOM_READINGS[orig]
        else:
            yomi = tok.get("hira") or ""
            if not yomi or yomi == orig:
                yomi = reading_for(orig)
        if not yomi:
            out_parts.append(html.escape(orig))
            continue
        out_parts.append(f"<ruby>{html.escape(orig)}<rt>{html.escape(yomi)}</rt></ruby>")

    out = "".join(out_parts)

    # markdown **bold** → <strong>
    out = re.sub(r"\*\*([^*\n][\s\S]*?)\*\*", lambda m: "<strong>" + m.group(1) + "</strong>", out)

    return out


def strip_ruby_for_tts(text: str) -> str:
    """TTS用に <ruby>漢字<rt>かんじ</rt></ruby> から rt を除去して 漢字 だけ残す。"""
    return re.sub(r"<ruby>([\s\S]*?)<rt>[\s\S]*?</rt>([\s\S]*?)</ruby>", r"\1\2", text)
