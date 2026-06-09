# Setup guide — server + tablet (PWA)

End-to-end: run the server, configure your child(ren), then install the app on a
tablet. Takes ~10 minutes.

## 1. Run the server

```bash
git clone https://github.com/yusukebass77/kids-ai.git
cd kids-ai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/children.example.json config/children.json   # edit with your kids

export ANTHROPIC_API_KEY=sk-ant-...      # required (chat / story / explain)
export OPENAI_API_KEY=sk-...             # optional (TTS / STT)
uvicorn server:app --host 0.0.0.0 --port 8000
```

Check it's up: open `http://<server-ip>:8000/health` in a browser — you should
get a JSON status. `<server-ip>` is the machine running the server (e.g.
`192.168.1.100`, or a hostname / Tailscale IP if you use one).

> The web app is **served by this server** — there's no separate "server URL" to
> type into the app. You just open the right URL on the tablet (next step).

## 2. Configure your child(ren)

Edit `config/children.json` (git-ignored, so your real data never gets committed):

```json
{
  "assistant_name": "あい",
  "children": [
    { "id": "child1", "display_name": "...", "grade": 3, "furigana": true,
      "theme": { "bg": "#ffc0cb", "accent": "#c44569" } }
  ]
}
```

Each child gets a route at `/<id>` (e.g. `/child1`). Optionally add a profile at
`prompts/<id>_profile.md` (see `prompts/profile.example.md`) to personalize tone
and topics. Restart the server after editing config.

## 3. Install on the tablet (PWA)

On the child's tablet, open the **child's own URL**:

```
http://<server-ip>:8000/child1
```

Then add it to the home screen:

- **Android (Chrome / Samsung Internet)** — menu (⋮) → **Add to Home screen** → confirm.
- **Amazon Fire HD (Silk)** — menu → **Add to Home screen** → name it → **Add**.
- **iPad (Safari)** — Share (□↑) → **Add to Home Screen** → **Add**.

Open the installed icon. The page loads the child's themed interface and connects
to the server automatically (same origin). Repeat with `/child2` etc. on each
child's device.

## 4. First use

- Talk or type to the assistant (chat mode by default).
- Mode buttons switch between **chat / explain / story / programming (invention)**.
- For a child with `furigana: true`, kanji above their grade get automatic ruby
  readings.

## Troubleshooting

- **Blank page / can't connect** — confirm the server is running and the tablet is
  on the same network (or can reach the server IP/host). Re-open `…/health`.
- **404 on `/child1`** — that `id` isn't in `config/children.json`, or the server
  wasn't restarted after editing config.
- **No replies** — check `ANTHROPIC_API_KEY` is set in the server's environment.
- **PWA won't install** — update the browser; on iPad only Safari supports install.

## Optional

- **Safety gate** runs in log-only mode by default; set `KIDS_AI_SAFETY_ENFORCE=1`
  to enforce once you've reviewed the logs.
- **HTTPS** — for install reliability and security, put the server behind HTTPS
  (reverse proxy or a tunnel) rather than plain `http://` over the internet.
