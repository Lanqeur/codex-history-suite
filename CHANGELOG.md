# Changelog

## 0.2.0 - 2026-07-15

- Make new profiles model-first with an explicit extractive fallback when configuration or an API key is missing.
- Add a recommended non-thinking DashScope DeepSeek V4 Flash preset with user-editable input, cached-input, and output pricing.
- Estimate model, embedding, cache, and storage costs from local transcript scale while excluding inline base64 payloads from model-token estimates.
- Report actual API usage, cost, response-cache hits, and managed disk usage after each completed build.
- Require an explicit cost ceiling before any planned paid build and reject invalid pricing or estimation configuration.

## 0.1.0 - 2026-07-15

- Initial portable, evidence-first full and incremental Codex History pipeline.
