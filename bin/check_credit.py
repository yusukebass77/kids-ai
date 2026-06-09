#!/usr/bin/env python3
"""kids-ai API残高チェッカー（一時運用、Pro移行後は撤去予定）

- kids-ai/logs/usage_*.jsonl の cost_jpy を BASELINE_DATE 以降で集計
- BASELINE_USD - 使用累計 = 残額推定
- 閾値以下で #あい管理 CH へ Discord アラート投下
- 注意：kids-ai 以外の Anthropic API 利用分はカウントしない（過大評価ぎみの推定）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOG_DIR = BASE / "logs"
sys.path.insert(0, str(BASE / "lib"))
import monitor as kids_monitor  # noqa: E402

# 基準点：2026-05-13 のテスト終了時点、コンソールで残$19.80 を目視
BASELINE_DATE = "2026-05-13"
BASELINE_USD = 19.80
USD_TO_JPY = 150.0
ALERT_THRESHOLD_USD = 5.0  # 余裕含み（他API利用分の不可視部分を考慮）
SYSTEM_CHANNEL = "1498268429021483058"  # #あい管理


def estimate_remaining() -> tuple[float, float]:
    total_jpy = 0.0
    if not LOG_DIR.is_dir():
        return BASELINE_USD, 0.0
    for f in sorted(LOG_DIR.glob("usage_*.jsonl")):
        date_str = f.stem.replace("usage_", "")
        if date_str < BASELINE_DATE:
            continue
        try:
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    total_jpy += float(entry.get("cost_jpy", 0.0))
                except Exception:
                    continue
        except Exception:
            continue
    spent_usd = total_jpy / USD_TO_JPY
    return BASELINE_USD - spent_usd, spent_usd


def main() -> int:
    remaining, spent = estimate_remaining()
    print(
        f"baseline: ${BASELINE_USD:.2f} ({BASELINE_DATE})  "
        f"spent: ${spent:.2f}  remaining(est): ${remaining:.2f}",
        flush=True,
    )
    if remaining < ALERT_THRESHOLD_USD:
        msg = (
            "⚠️ **Anthropic API残高アラート（kids-ai）**\n"
            f"残額推定: **${remaining:.2f}**（閾値 ${ALERT_THRESHOLD_USD:.0f} 切り）\n"
            f"基準: ${BASELINE_USD:.2f}（{BASELINE_DATE}）/ 消費累計: ${spent:.2f}\n"
            "※ kids-ai 以外の API 利用分は含まず、実残はもう少し少ない可能性あり\n"
            "→ **Pro移行 or 残高チャージ判断のタイミング**\n"
            "公式確認: https://console.anthropic.com/settings/billing"
        )
        ok = kids_monitor.post_text(msg, chat_id=SYSTEM_CHANNEL)
        print(f"alert posted: {ok}", flush=True)
    else:
        print("no alert needed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
