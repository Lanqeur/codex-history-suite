# Recovery

## Failed Build

Run `status --json` and inspect the failed stage in `runs/<build-id>/run.json`. The previously promoted build remains authoritative. Snapshot chunks and model-cache entries written before failure are immutable and safe to reuse.

Do not manually promote a failed database. Correct the dependency, configuration, source mutation, model response, or budget problem and start another update. Failed staging builds are retained as evidence until deliberately cleaned up.

## Lock File

The profile lock prevents concurrent build, update, migration, and equivalence operations. Check that no process is alive before using `repair --clear-stale-lock`. A lock is not stale merely because a large transcript is taking time to parse.

## Damaged Active Build

Run `audit`. If SQLite integrity or foreign keys fail, select the latest earlier passing build by restoring its database and `active.json` from backup. Do not repair source transcripts in place.

## Migration

`migrate --from-db` uses SQLite's backup API and preserves existing Knowledge, Evidence, FTS, assets, claims, and relation rows. By default it hashes currently discoverable transcripts so `plan` can report drift without rebuilding imported knowledge. Use `--skip-source-adoption` only when the source files are unavailable. A migrated legacy authority is query-compatible but is deliberately not an incremental baseline: run one full build before `update`. The migrated build remains available for rollback and comparison.

Pass `--from-chroma PATH` to copy a compatible legacy Chroma runtime. The query engine recognizes both the portable `codex_history` collection and the v2.1 `codex_history_v21` collection. Enable embeddings in `config.toml` after copying it.
