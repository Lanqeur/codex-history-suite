# Lifecycle And Invariants

## State Machine

1. `discover`: enumerate read-only sources and classify changes.
2. `snapshot`: write fixed-size content-addressed chunks and an immutable manifest.
3. `ingest`: parse canonical events, turns, evidence, knowledge, claims, and artifact mappings.
4. `lineage`: rebuild deterministic thread-family components.
5. `summarize`: optionally produce evidence-linked model assets using shared cache.
6. `index`: rebuild FTS and refresh only missing Chroma document hashes.
7. `audit`: run SQLite integrity, foreign keys, evidence coverage, FTS coverage, and logical digests.
8. `promote`: atomically replace `active.json` only after the audit passes.

Every stage is recorded in both `runs/<build-id>/run.json` and `stage_checkpoints`. Staging databases are never queried by the consumer Skill unless explicitly promoted.

## Change Classification

- `added`: new source path.
- `unchanged`: size and mtime match the active source state.
- `appended`: the previous complete content hash exactly matches the new file prefix.
- `rewritten`: content is not append-only.
- `deleted`: an active source is no longer discovered.

Only affected threads are reparsed. Family scopes containing affected threads are regenerated. Content-addressed Evidence, model responses, artifacts, and embeddings are reused. Snapshot reads are bounded to the discovery-time byte size, so a live transcript that grows during the run becomes a clean append on the next update instead of creating an inconsistent fingerprint.

## Release Invariant

`audit --equivalence` performs a clean full build from the same current sources and compares stable rows across source state, events, turns, scopes, Evidence, Knowledge, claims, artifacts, semantic documents, and aliases. Build timestamps, checkpoints, cache accounting, and historical versions are deliberately excluded.

A mismatch is not advisory. Inspect the per-table row count and SHA-256 differences before accepting an incremental implementation change.
