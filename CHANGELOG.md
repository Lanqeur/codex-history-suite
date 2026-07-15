# Changelog

## 0.3.0 - 2026-07-15

- Add stable device and library identities plus a catalog overlay for imported and generated profiles.
- Export and verify complete portable bundles containing SQLite, transcript chunks, artifact CAS, semantic indexes, and model-response caches.
- Import bundles with automatic profile naming, lineage-aware updates, preserved prior generations, display-time path remapping, and a shared content-addressed blob store.
- Add federated multi-profile search with exact knowledge deduplication and per-library provenance.
- Add non-destructive transcript merge and offline two-way convergence bundles with exact, longest-prefix, and deterministic event-union conflict handling.
- Add end-to-end tamper, Zip Slip, physical deduplication, idempotence, and convergence tests.

## 0.2.0 - 2026-07-15

- Make new profiles model-first with an explicit extractive fallback when configuration or an API key is missing.
- Add a recommended non-thinking DashScope DeepSeek V4 Flash preset with user-editable input, cached-input, and output pricing.
- Estimate model, embedding, cache, and storage costs from local transcript scale while excluding inline base64 payloads from model-token estimates.
- Report actual API usage, cost, response-cache hits, and managed disk usage after each completed build.
- Require an explicit cost ceiling before any planned paid build and reject invalid pricing or estimation configuration.

## 0.1.0 - 2026-07-15

- Initial portable, evidence-first full and incremental Codex History pipeline.
