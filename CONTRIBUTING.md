# Contributing

Issues and PRs welcome. kids-ai is actively maintained.

## Ground rules
- **Never commit personal data.** Real names/details live in `config/children.json`
  and `prompts/<id>_profile.md`, which are git-ignored. Use generic placeholders
  (child1/child2) in code and examples.
- Secrets come from **environment variables** only.
- Changes to the **safety gate** or prompts should include before/after examples
  so reviewers can judge behavior.

## Areas that need help
- More STT/TTS backends and languages.
- Dynamic N-children routing in the PWA (currently two example children).
- A test fixture set for the safety gate and refine prompts.

## License
By contributing you agree your contributions are licensed under MIT.
