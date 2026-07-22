# Recovery

## Failed Build

Run `status --json` and inspect the failed stage in `runs/<build-id>/run.json`. The previously promoted build remains authoritative. Snapshot chunks and model-cache entries written before failure are immutable and safe to reuse.

Do not manually promote a failed database. Correct the dependency, configuration, source mutation, model response, or budget problem, then run:

```bash
python3 scripts/codex_history.py repair --resume-latest --max-cost-cny N --json
```

Recovery starts a fresh candidate database transaction while reusing immutable transcript snapshot chunks, artifact CAS, and every successful paid model-response cache entry from the failed attempt. This deliberately avoids resuming half-committed relational state. The result records `resumed_from_build_id` and the reused layers. Successful promotion applies normal retention; failed run JSON remains available even if its obsolete database directory is later reclaimed.

## Retrieval Pollution

Run `repair --audit-pollution --json` when recent incremental records crowd out older knowledge, History-query output appears in high tiers, or imported and canonical Overviews are both current. Review the recursive Asset/ledger counts, duplicate Overview scopes, pending model fact blocks, total reducer/writer/embedding estimate, and disk preflight. Then run:

```bash
python3 scripts/codex_history.py repair --repair-pollution --max-cost-cny N --json
```

The repair is copy-on-write. It preserves canonical events, raw Evidence, and artifacts; removes only identified derived pollution and its dependent claim/relation rows; fences History retrieval events; rebuilds affected model layers; recreates FTS from authority; and normalizes Overview validity. An FTS delete-trigger mismatch is repaired only in the candidate by suspending the triggers during the bulk rewrite and rebuilding all FTS tables before audit. Never delete or edit source transcripts to solve derived-layer pollution.

## Lock File

The profile lock prevents concurrent build, update, migration, and equivalence operations. Check that no process is alive before using `repair --clear-stale-lock`. A lock is not stale merely because a large transcript is taking time to parse.

## Damaged Active Build

Run `audit`. If SQLite integrity or foreign keys fail, select the latest earlier passing build by restoring its database and `active.json` from backup. Do not repair source transcripts in place.

## Disk Preflight

`plan --json` reports `resource_preflight.free_bytes`, `candidate_bytes`, `headroom_bytes`, and `required_free_bytes`. A failing preflight is a hard stop before copying SQLite or Chroma. Remove obsolete exports/candidates, lower intentional retention, or move the profile; do not bypass it by deleting the active or only rollback build.

Full equivalence creates a separate clean authority and therefore requires `audit --equivalence --confirm-full-reference`. The flag confirms the operation, not a waiver of disk preflight.

## Migration

`migrate --from-db` uses SQLite's backup API and preserves existing Knowledge, Evidence, FTS, assets, claims, and relation rows. By default it hashes currently discoverable transcripts so `plan` can report drift without rebuilding imported knowledge. Use `--skip-source-adoption` only when the source files are unavailable. A migrated legacy authority is query-compatible but is deliberately not an incremental baseline: run one full build before `update`. The migrated build remains available for rollback and comparison.

Pass `--from-chroma PATH` to copy a compatible legacy Chroma runtime. The query engine recognizes both the portable `codex_history` collection and the v2.1 `codex_history_v21` collection. Enable embeddings in `config.toml` after copying it.
