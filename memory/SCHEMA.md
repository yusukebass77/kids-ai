# kids-ai メモリ層スキーマ

子の会話から抽出した「好きなもの・出来事・人・体験」を蓄積する jsonl ストア。
あい（子供版）が次の会話で自然に参照するための基盤。

## ディレクトリ構成

```
~/kids-ai/memory/
├── SCHEMA.md          ← このファイル
├── child1/
│   └── memory.jsonl   ← こども1の蓄積メモリ（1行=1エントリ）
└── child2/
    └── memory.jsonl   ← こども2の蓄積メモリ
```

## エントリスキーマ（jsonl 1行 = 1エントリ）

```json
{
  "ts": "2026-05-12T16:00:00+09:00",
  "category": "like",
  "item": "ハリポタのハーマイオニー",
  "source_msg": "ハーマイオニーがかっこいい",
  "confidence": 0.9
}
```

### フィールド定義

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `ts` | string (ISO8601) | ◯ | 記録時刻、JSTオフセット付き |
| `category` | string enum | ◯ | `like` / `dislike` / `event` / `person` / `experience` のいずれか |
| `item` | string | ◯ | 短い要約（例「ハリポタのハーマイオニー」「3年生の遠足」） |
| `source_msg` | string | ◯ | 抽出元の子供発言（原文、トリミング可） |
| `confidence` | float 0.0〜1.0 | ◯ | 抽出信頼度（手動追加は1.0、haiku自動抽出は判定値） |

### カテゴリ定義

- **like**：好きなもの・キャラ・本・遊び・食べ物・場所
- **dislike**：嫌い・苦手なもの・避けたいもの
- **event**：起きた出来事（学校での出来事、家での出来事、習い事の進捗等）
- **person**：人物（友達、先生、家族、ペット、本のキャラとして実在の人物的に語られる場合）
- **experience**：体験（行った場所、やったこと、達成したこと）

## CLI 操作

```
kids-mem add <user_id> <category> <item> [--source MSG] [--confidence C]
kids-mem extract <user_id> --msg "子供の発言"        # haiku経由で自動抽出
kids-mem search <user_id> <query> [--top K]
kids-mem list <user_id> [--limit N] [--category CAT]
kids-mem snippet <user_id> [--recent N] [--query Q]  # システムプロンプト注入用
```

## システムプロンプト注入の形

`server.py` の `build_system_prompt(user_id)` で末尾に追加：

```
---

## 最近の話題（メモリから）
- [2026-05-12 like] ハリポタのハーマイオニー
- [2026-05-11 event] 3年生の遠足で珍しい石を拾った
- [2026-05-10 person] お友達のさくらちゃん
- ...
```

あい側の参照ルールは `chat-kids-v1.md` の「最近の話題メモリ」セクション参照。

## プライバシー・運用ルール

- 親モニタCHにはメモリ追加イベントも流す（透明性）
- 子供本人がメモリ内容を確認できる仕組みは Phase 2 以降で検討
- 個人特定情報（学校名・住所・本名フルネーム）の自動抽出は禁止
- haiku 抽出時のシステムプロンプトに該当NG項目を明記
