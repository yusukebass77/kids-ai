# kids-ai safety_gate draft

kids-ai web 向けの safety gate 試運転ドラフトです。

## Included files

```text
lib/safety_gate.py
  入力側 memory gate / 出力側 response guard / L0.5 monitor / passive decay

docs/SCHEMA_memory_safety.md
  memory_type × sensitivity 表。SCHEMA.md へ転記想定。

docs/integration_notes.md
  server.py への差し込み位置と mode routing 方針。

docs/rollout_checklist.md
  1週間 log-only 試運転から本番有効化までのチェックリスト。

tests/test_safety_gate.py
  最低限の pytest テスト。
```

## Main principle

`safety_gate` は必ず2分割する。

```text
A. input_memory_gate_async
   子どもの発言 → memory保存判定
   post-response async OK
   LLM judge 可

B. output_response_guard_inline
   あい応答 → 子どもへ送出前
   inline / <200ms
   ルールベースのみ
```

## Suggested first deployment

```text
1. lib/safety_gate.py を追加
2. server.py の送出直前に output_response_guard_inline() を入れる
3. memory.py の save_memory() 手前に input_memory_gate_async() を入れる
4. safety_log_YYYY-MM-DD.jsonl に log_only=True で1週間記録
5. false positive / false negative を確認してから本番反映
```
