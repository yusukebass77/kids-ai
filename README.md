# kids-ai

**A self-hosted, safety-gated AI companion for your kids — configurable, private, runs on hardware you control.**

kids-ai is a small FastAPI server + installable web app (PWA) that gives each
child their own friendly AI assistant. It speaks their language level, keeps a
light per-child memory, runs everything through a **safety gate**, and is driven
entirely by a config file — **no names or personal details are hard-coded**.

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Self-hosted](https://img.shields.io/badge/self--hosted-privacy--first-success)

## Why

Generic chatbots aren't built for a 7-year-old: wrong reading level, no
guardrails, no sense of *this* child. kids-ai lets a parent stand up a private
assistant per child, tuned to their grade and personality, with safety checks on
both what goes in and what comes out — and your child's conversations never
leave your server.

## Features

- **Per-child personas** — name, grade, reading level, interests, and a free-form
  profile, all from config. Add a child by editing one JSON file.
- **Modes** — `chat` (Socratic companion w/ memory), `explain` (kid-friendly
  explainer with KaTeX / maps / 3D models), `story` (collaborative gamebook),
  `programming` (inventor assistant).
- **Safety gate** — input + output screening with a log-only trial mode before
  enforcement.
- **Furigana** — automatic `<ruby>` readings for younger children (per-child flag).
- **Light memory** — remembers a child's likes / events across sessions.
- **Daily time budget & quiet hours** — gentle nudges, parent override.
- **TTS** — spoken replies (OpenAI voices).
- **PWA** — installable on a tablet (e.g. Fire HD / any browser), per-child theme.
- **Optional Discord relay** — route chat through a Discord listener instead of
  the API.

## Configure (this is where your data lives)

Copy the example config and edit it. **`config/children.json` is git-ignored**,
so your kids' real names and details never get committed:

```bash
cp config/children.example.json config/children.json
```

```json
{
  "assistant_name": "あい",
  "children": [
    { "id": "child1", "display_name": "...", "grade": 3, "furigana": true,
      "theme": { "bg": "#ffc0cb", "accent": "#c44569" } }
  ]
}
```

Optionally add a per-child profile at `prompts/<id>_profile.md` (see
`prompts/profile.example.md`) to personalize tone and topics. Prompts use a
`{{assistant_name}}` placeholder that is filled from config at load time.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# keys via environment
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...           # TTS / STT (optional)
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://<host>:8000/child1` on the child's device and install the PWA.

### Configuration (environment)

| Var | Purpose |
|-----|---------|
| `ANTHROPIC_API_KEY` | chat / story / explain (Claude) |
| `OPENAI_API_KEY` | TTS + STT (optional) |
| `KIDS_AI_SAFETY_ENFORCE` | `1` to enforce the safety gate (default: log-only) |
| `KIDS_AI_RELAY_ENABLED` | `1` to route chat through the Discord relay |
| `KIDS_AI_MEMORY_DIR` | override the per-child memory location |

## Privacy

Personal data stays out of the repo by design — these are git-ignored:
`config/children.json`, `prompts/<id>_profile.md` edits, `memory/`,
`conversations/`, `logs/`. Keep your real config and profiles private.

## Status

Early but working. Roadmap and known issues are tracked in GitHub Issues —
contributions welcome (see `CONTRIBUTING.md`).

## License

MIT — see [`LICENSE`](LICENSE).
