# Changelog

## 0.8.0 - 2026-07-21

- Bind captured absolute-path documents back to their exact conversation events through artifact observations, while retaining inline image URI recovery.
- Add portable attachment cards with inline raster images, bounded text previews, in-browser PDF opening, and original-file downloads for Office, archive, and other document types.
- Add `--embed-attachments`, per-file and aggregate size limits, explicit missing/skipped states, and attachment-aware search and evidence exports.
- Store embedded binaries once per SHA-256 in conversation export v2 so repeated references do not duplicate base64 payloads.

## 0.7.0 - 2026-07-21

- Render user and assistant Markdown with offline GFM table support while retaining a one-click source view.
- Render fenced Mermaid diagrams offline in strict security mode, embedding the pinned runtime only when a selected range contains Mermaid.
- Sanitize rendered historical content with DOMPurify and keep tool, goal, internal-context, and raw evidence in literal source form.
- Bundle pinned Marked, DOMPurify, and Mermaid browser builds plus their licenses in plugin and Python package distributions.

## 0.6.1 - 2026-07-21

- Make the conversation evidence viewer and its selected-evidence HTML exports use a high-contrast light theme by default.
- Add a persistent light/dark theme toggle without changing exported evidence or requiring a knowledge-base rebuild.

## 0.6.0 - 2026-07-21

- Add exact conversation-range reconstruction from canonical snapshot byte offsets, with Codex dual-write deduplication and per-event provenance.
- Add thread/title/scope, turn-range, and timestamp selection with optional tool, goal, internal-context, raw-event, and embedded-image controls.
- Add a self-contained Codex-style offline HTML evidence viewer with search, role and time filters, progressive rendering, evidence selection, drag ordering, and HTML/Markdown/JSON export.
- Document original-conversation verification workflows in the read-only Skill and bilingual user guides.

## 0.5.2 - 2026-07-17

- Add policy-gated absolute-path artifact discovery with WSL/Windows alias normalization, extension and size limits, automatic self-ingestion exclusions, and SHA-256 deduplication.
- Add zero-model `library artifact-plan` and `library capture-artifacts` workflows with Event/Evidence-linked observations, copied SQLite candidates, closure audits, and atomic promotion.
- Capture normal Git repositories as verified `bundle --all` checkpoints, partial clones as network-free HEAD archives, and dirty tracked plus non-ignored-untracked worktrees as deterministic snapshots.
- Add artifact observations and repository checkpoints to the portable schema, logical audits, FTS, export manifests, and content-addressed storage.
- Make deltas transport complete artifact mapping metadata and converge artifact-only generations even when the transcript source generation is unchanged.
- Document safe capture policy, Git checkpoint behavior, artifact-only lifecycle, and cross-device incremental semantics in English and Chinese.

## 0.5.0 - 2026-07-16

- Replace hydrated incremental summary bypasses with generation-based model consolidation: DeepSeek reduces new fact blocks into append-only ledgers and Qwen updates only affected thread/family overviews.
- Treat extractive ingestion as an explicit `pending_model_consolidation` state and allow later model backfill even when source transcripts have not changed.
- Require exact reducer Record-ID coverage, evidence-linked writer claims, one bounded repair attempt, and atomic rollback on malformed or incomplete model output.
- Detect content changes inside an existing turn, preserve old ledgers, archive replaced overviews/assets in `knowledge_versions`, and refresh evidence rollups plus conservative relations.
- Restore copy-on-write incremental Chroma refresh, bound semantic candidate projections and batch sizes to provider limits, and add realistic embedding token/pricing estimates.
- Separate reducer, writer, embedding, and provider-cache costs; keep stable model-cache keys portable across generation deltas.
- Redefine equivalence around canonical source/evidence authority while reporting generation-specific model-layer differences separately.
- Add backlog recovery, coverage-gate rollback, semantic projection, and two-model pipeline tests plus updated bilingual operating guidance.

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
