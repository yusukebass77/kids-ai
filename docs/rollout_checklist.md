# Rollout Checklist

## Phase 0: 実装前確認

- [ ] `child_id` が `child2` / `child1` で必ず渡る
- [ ] memory 保存処理の入口が特定できている
- [ ] `/api/chat` と `/api/chat/stream` の送出直前に guard を入れられる
- [ ] safety log の出力先を決める

## Phase 1: 1週間 log-only

- [ ] `log_only=True` で開始
- [ ] memory action は実保存に反映しない
- [ ] output guard は結果をログに残す
- [ ] L0.5 alert も通知せずログだけ

確認項目:

- [ ] false positive: 普通の会話を危険判定しすぎていないか
- [ ] false negative: 体調・自己否定・友達トラブルを見落としていないか
- [ ] latency: output_response_guard_inline が常に 200ms 未満か
- [ ] memory leakage: child_id が混ざっていないか
- [ ] learning safety: use_for_learning=true が low sensitivity のみに限定されているか

## Phase 2: memory gate 有効化

- [ ] discard は保存しない
- [ ] redact_and_save は匿名化済み content のみ保存
- [ ] high / critical は use_for_learning=false を強制

## Phase 3: L0.5 通知有効化

- [ ] watch は L1 日次サマリーで強調
- [ ] alert は Discord 即時通知
- [ ] critical は Discord 即時通知 + frozen推奨
- [ ] auto-frozen はしない

## Phase 4: learning feedback 有効化

- [ ] use_for_learning=true の low memory のみ explain/programming に注入
- [ ] friend_context / family_context / health / conflict は学習例題に使わない
