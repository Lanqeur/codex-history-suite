# Changelog

## 0.4.0 - 2026-07-15

- Add canonical normalized transcript snapshots with stable source identities, source inventories, coverage generations, and image externalization into the artifact CAS.
- Add one-time baseline plus ordered `export-delta`/`apply-delta` transport, including strict library and generation checks, changed-source reconstruction, artifact/cache differences, idempotence, rollback, and tamper verification.
- Add `hydrate-baseline` to preserve curated v2.1.1 knowledge while attaching complete canonical sources without model calls.
- Add `compact-storage` and snapshot-offset trace reconstruction to remove duplicate canonical payloads without losing evidence drill-down.
- Preserve curated scopes and relations during hydrated incremental updates, add only genuinely new evidence-backed facts, and leave semantic refresh explicit and budgeted.
- Make candidate semantic indexing copy-on-write so a failed build cannot mutate the active Chroma authority.
- Add portable source and artifact closure validation, real cross-device delta tests, and bilingual baseline/delta operating guidance.

## 0.3.1 - 2026-07-15

- Add database-to-CAS artifact closure audits with optional full SHA-256 verification.
- Add explicit `none`, `referenced`, and `all` artifact export policies and record closure in bundle manifests.
- Verify bundled artifact payloads against the SQLite `artifact_files` authority, while allowing explicitly query-only bundles.
- Add verified legacy artifact-pack adoption by external reference, copy, hard link, or automatic materialization.
- Resolve registered artifact sources in read-only artifact queries and preserve closure through export/import and merge workflows.
- Add portable history coverage watermarks with represented activity bounds, source observation times, build identity, logical digest, and an explicit legacy confidence marker.

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
