# Memory Safety Schema for kids-ai web

## 基本方針

- `child_id` は必須。`child2` と `child1` のメモリ混線を防ぐ。
- `use_for_learning=true` は `sensitivity=low` の安全な興味・好み・創作・学習進捗だけに限定する。
- 友達名・学校名・駅名・住所・家族予定・体調・自己否定・強いトラブルは、原則として長期メモリに保存しない。
- メモリ衰退は passive に行う。本人に「今も好き？」と確認せず、参照されない記憶を自然に話題候補から外す。

## memory_type × sensitivity

| memory_type | examples | sensitivity | use_for_learning | default action |
|---|---|---:|---:|---|
| interest | 石、物語、英語、そろばん、工作 | low | true | save |
| preference | 好きな色、好きなキャラ、遊び方 | low | true | save |
| skill_progress | 7級の引き算が苦手、英単語に興味 | low | true | save |
| creative_work | 作ったキャラ、物語世界、発明アイデア | low | true | save |
| family_context | 家族との一般的な出来事 | medium | false | redact_and_save |
| friend_context | 友達との会話、遊び | medium | false | redact_and_save |
| location_context | 学校名、駅名、住所、習い事場所 | high | false | discard or redact_and_save |
| schedule | 家族予定、外出予定、留守情報 | high | false | discard |
| health | 痛み、体調不良、ケガ、病気 | high | false | discard + monitor |
| negative_self_belief | 私はバカ、太ってる、嫌われてる | high | false | discard + monitor |
| conflict | いじめ、強い友達トラブル、先生トラブル | high | false | discard + monitor |
| unsafe_topic | 自傷、暴力、性的、危険行動 | critical | false | discard + immediate alert |

## action

```json
{
  "action": "save | redact_and_save | discard",
  "memory_type": "interest | preference | skill_progress | creative_work | family_context | friend_context | location_context | schedule | health | negative_self_belief | conflict | unsafe_topic | other",
  "content": "safe memory text in Japanese, redacted if needed",
  "sensitivity": "low | medium | high | critical",
  "use_for_learning": true,
  "reason": "short reason"
}
```

## Redaction examples

- 「さくらちゃんと遊んだ」→「友達Aと遊んだ」
- 「横浜小学校で話した」→「学校Aで話した」
- 「菊名駅で見た」→「駅Aで見た」

## Do not save

- 自己否定発話
- 体調の詳細
- 住所・学校名・駅名
- 家族予定・留守情報
- 強い友達トラブル・先生トラブル
- 年齢に合わない性的・暴力的話題
