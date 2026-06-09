# Integration Notes for kids-ai web

## 差し込み位置

`/api/chat` と `/api/chat/stream` の流れは以下を推奨。

```text
子どもの入力
↓
quiet hours / budget / frozen 判定
↓
memory load
↓
Claude / relay / API
↓
assistant_text 生成
↓
output_response_guard_inline()
↓
子どもへ送出
↓
input_memory_gate_async() を post-response で実行
↓
monitor_alert_level()
↓
usage.jsonl / safety_log_YYYY-MM-DD.jsonl に記録
```

## 重要な分離

### A. input_memory_gate_async

- 子どもの発言 → memory 保存判定
- post-response async OK
- Haiku judge など LLM judge 使用可
- 多少遅くても UX 影響は小さい

### B. output_response_guard_inline

- あい応答 → 子どもへ送出前フィルタ
- inline 必須
- 目標 `<200ms`
- LLM judge 禁止
- ルールベース強制

## L0.5 通知レベル

| level | meaning | action |
|---|---|---|
| normal | 通常会話 | L1日次サマリーのみ |
| watch | 気になる兆候 | L1で強調 |
| alert | 体調・友達トラブル等 | Discord即時通知 |
| critical | 危険話題 | Discord即時通知 + frozen推奨 |

## Per-mode routing

| mode | route recommendation | reason |
|---|---|---|
| chat | relay default / API fallback | 雑談は多少遅くても許容 |
| story | relay default / API fallback | 長文生成はコスト圧縮効果が大きい |
| explain | API direct first | 学習は安定性・速度優先 |
| programming | API direct first | 発明モードはステップ制御の安定性優先 |
| vision | API direct first | 画像系はrelayより直APIが安定 |
| abacus | server-side generation | API不要 |
