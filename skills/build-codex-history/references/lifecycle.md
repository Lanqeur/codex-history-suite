# Lifecycle And Invariants

## State Machine

1. `discover`: enumerate read-only sources and classify changes.
2. `snapshot`: write fixed-size content-addressed chunks and an immutable manifest.
3. `ingest`: parse canonical events, turns, evidence, knowledge, claims, and artifact mappings.
4. `lineage`: rebuild deterministic thread-family components.
5. `summarize`: reduce new fact blocks into append-only ledgers, then update affected thread/family overviews with a separate writer.
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

Only affected threads are reparsed. Curated family membership is preserved; deterministic families are rebuilt when needed. New fact blocks receive ingest provenance and remain pending until every Record ID passes model coverage validation. Old ledgers remain immutable, old overviews are versioned, and only affected thread/family overviews advance. Content-addressed Evidence, model responses, artifacts, and embeddings are reused. Snapshot reads are bounded to the discovery-time byte size, so a live transcript that grows during the run becomes a clean append on the next update instead of creating an inconsistent fingerprint.

When `auto` lacks model configuration, ingest and promotion may still stage searchable deterministic evidence, but `active.json` and SQLite metadata report `pending_model_consolidation`. With no later source changes, `update` returns that status until both models are available; once configured, the same command processes the pending backlog.

## Artifact-Only Builds

`library capture-artifacts` uses the same eight checkpointed stages but preserves
all transcript, knowledge, relation, and semantic rows. `discover` records the
reviewed path/Git plan, `ingest` adds content-addressed files, repository
checkpoints, and Event/Evidence observations, `summarize` records zero calls,
`index` rebuilds only artifact FTS, and `audit` verifies SQLite plus CAS closure
before promotion. A failure leaves the prior active build unchanged.

Occurrence time is the transcript event timestamp. Capture time is when the
currently accessible file or repository state was observed. A historical path
backfill must never claim that newly captured bytes are proven to be the exact
bytes that existed at the earlier occurrence time.

## Release Invariant

`audit --equivalence` performs a clean extractive full build from the same current sources and requires exact equality for canonical sources, parsed events/turns, Evidence occurrences, deterministic core/fact records, and artifacts. Model ledgers, Overview text, claims, semantic documents, and other generation-specific derived layers are reported separately as `derived_layer_differences`; they are not expected to be byte-identical between staged incremental consolidation and a one-shot rebuild.

A mismatch is not advisory. Inspect the per-table row count and SHA-256 differences before accepting an incremental implementation change.
