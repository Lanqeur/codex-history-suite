# Multi-Device Libraries

## Safety Model

- A source profile is never edited by import, federated search, merge, or sync.
- Import installs a separate profile. A newer bundle from the same stable `library_id` replaces only that imported generation and moves the previous generation to `backups/imports`.
- Merge writes a generated profile. Its transcript source set can be rebuilt repeatedly without changing either parent.
- Paid summary or embedding work still requires a reviewed plan and explicit `--max-cost-cny`.

## Bundle Contract

`library export` creates a ZIP with `bundle.json`, an SQLite backup, active transcript snapshot chunks and manifests, selected artifact files, and optional semantic/model-cache files. Choose one artifact policy:

- `none`: retain artifact metadata in SQLite but intentionally omit file payloads.
- `referenced`: include every CAS object indexed by the active database; this is the default.
- `all`: include referenced objects plus unreferenced files retained in local and registered external CAS roots.

For `referenced` and `all`, export verifies database-to-CAS size and SHA-256 closure before writing the ZIP. The manifest records the policy, indexed and packaged counts, closure digest, SHA-256, size, role, source device, profile lineage, active build, logical digest, capabilities, non-secret provider settings, and historical source roots.

The manifest's `history_coverage` separates content time from processing time. `earliest_activity_at` and `latest_activity_at` bound timestamps actually represented by the authority. `source_scan_started_at` and `source_snapshot_completed_at` describe source observation, while `authority_completed_at` describes processing. `knowledge_version_id` and `logical_digest` identify the exact authority generation. These are watermarks, not a proof that no transcript is missing between the bounds; legacy migrations are marked `legacy-migrated` and may rely on thread metadata rather than canonical events.

`library verify` rejects a missing file, size mismatch, SHA-256 mismatch, database-to-bundle artifact mismatch, unsupported schema, absolute archive path, or path containing `..`. A `none` bundle passes only because the omission is explicit in its manifest. Import performs the same verification before installing data.

Use `library adopt-artifacts PACK --mode reference` to register and verify a large existing CAS without duplicating its bytes. `copy` materializes an independent profile copy, `hardlink` requires one filesystem, and `auto` tries a hard link before copying. A reference remains an external runtime dependency, but a later `referenced` or `all` export dereferences and embeds the real files.

Immutable files enter `<home>/shared/blobs/<sha-prefix>/<sha256>`. The imported profile receives a hard link when supported and a verified copy otherwise. SQLite is copied independently because it receives local schema/path metadata.

## Canonical Baseline And Delta Contract

A library can receive deltas only when its baseline has a complete `source_inventory`. Each source entry records the stable source/thread identity, logical raw hash and size, normalized snapshot hash and size, ordered content-addressed chunks, inline artifacts, and observation watermarks. Normalization replaces inline image base64 with `codex-history-artifact://` references without changing the logical raw-source generation. Exact normalized event payloads remain recoverable from snapshot byte offsets, and image bytes remain in the artifact CAS.

Create and import the baseline once:

```bash
python3 scripts/codex_history.py --profile default library export DEVICE-baseline.zip \
  --artifacts referenced --json
python3 scripts/codex_history.py library import DEVICE-baseline.zip --json
```

After a local audited `update`, export only the difference from the immediately preceding transfer:

```bash
python3 scripts/codex_history.py --profile default library export-delta DEVICE-001.zip \
  --base DEVICE-baseline.zip --artifacts referenced --json
python3 scripts/codex_history.py library apply-delta DEVICE-001.zip \
  --max-cost-cny 5 --json

# The next generation uses the prior delta, not the original baseline.
python3 scripts/codex_history.py --profile default library export-delta DEVICE-002.zip \
  --base DEVICE-001.zip --artifacts referenced --json
```

`delta.json` contains the full base and target source inventories, complete artifact mapping metadata, and only the new transcript chunks, artifact CAS objects, and optional exact model-cache entries needed to reach the target. It records stable library lineage plus exact base/target source generations. Verification hashes every packaged file, proves that every changed source can be reconstructed from base plus delta chunks, and checks artifact metadata against the target inventory. For artifact mode `referenced` or `all`, every artifact present in the target inventory but absent from the base inventory must be packaged.

`apply-delta` is restricted to profile-managed imported sources. It requires the receiver's active source generation to equal either the declared base or target generation, stages immutable blobs, atomically reconstructs only changed transcript files, and runs the normal incremental audit/promotion pipeline. Artifact-only deltas whose source generation is unchanged use a zero-model copied-database merge, validate CAS closure, and promote atomically. Idempotence requires both source generation and artifact inventory/metadata to match; an equal source generation alone does not skip pending artifact mappings. On source reconstruction failure it restores source files. A skipped delta, unrelated library, divergent local generation, unsafe archive path, or content mismatch is a hard error. `--artifacts none` remains an intentional query-only transport and does not claim artifact convergence.

Model-cache deltas are enabled by default so the receiver can reproduce source-side reducer/writer results without duplicate model charges. Cache keys exclude device-local build IDs. Omitting caches may cause paid work during `apply-delta`; always review the target plan and pass an explicit `--max-cost-cny` when its configuration can call a model. Chroma refresh is copy-on-write and embeds only missing document hashes after successful consolidation.

## Naming And Lineage

Run `library device --name NAME` once per installation. Without an explicit import name, the profile becomes `<device-slug>-<source-profile>` and receives `-2`, `-3`, and so on only for unrelated name collisions.

`library_id` identifies a logical library across exports. `bundle_id` identifies one audited full baseline, `delta_id` identifies one base-to-target transition, and `source_generation_id` is the strict synchronization watermark. Reimporting the same bundle or reapplying the same delta is a no-op. Importing a different full bundle with the same library ID updates the existing imported profile and preserves its prior directory under `backups/imports`.

## Path Remapping

The database keeps original absolute paths as provenance. Import materializes transcript snapshots under the imported profile and records exact old-file to new-file mappings plus old-root to imported-root mappings. Add explicit mappings with:

```bash
python3 scripts/codex_history.py library import DEVICE.zip \
  --path-map 'C:\Users\name\project=/home/name/project' --json
```

Query output applies the longest matching prefix and adds a corresponding `*_original` field. The stored Evidence is not rewritten.

## Federated Search

`library search` opens each selected active database read-only. Each profile uses its own lexical or semantic index. Exact normalized knowledge text is grouped only when tier, asset type, and status also agree. The winning result carries `library_matches` for every profile/Record ID and a `duplicate_count`.

This is the preferred first step because it is immediate, cheap, reversible, and keeps authority boundaries visible.

## Transcript Merge

Merge reconstructs source bytes from `source_files` and content-addressed `source_chunks`, then groups variants by stable thread ID:

1. `exact`: identical transcript bytes are kept once.
2. `longest-prefix`: when every older copy is an exact prefix, the longest copy wins.
3. `event-union`: divergent copies are parsed as JSONL, exact canonical events are deduplicated, one session metadata row is retained, and the remaining events are ordered by timestamp with stable source/line tie-breakers.

Different thread IDs remain different threads even when they share a branch prefix. Downstream Evidence and CAS layers deduplicate repeated content without destroying branch identity.

A query-only legacy bundle without reconstructable transcript chunks remains valid for federated search, but merge rejects it instead of silently dropping that library's history.

Run merge without `--build` first. It returns source counts, conflict methods, a stable source digest, and a build plan. After approval, repeat with `--build --max-cost-cny N`. Repeating the same merge yields the same source digest and either `no_changes` after model completion or `pending_model_consolidation` when an offline fallback still has unfinished higher layers.

## Offline Two-Way Convergence

```bash
python3 scripts/codex_history.py library sync shared-history.zip \
  --from laptop-default --from desktop-default \
  --as shared-history --max-cost-cny 30 --json
```

Import `shared-history.zip` on both devices. Both now contain the same merged lineage alongside their untouched local profile. Continue collecting new transcripts in each local profile. Move each local library's ordinary updates to the hub with its delta chain. When a new merge generation is produced, transfer one convergence baseline if the generated lineage is new; after that lineage exists on both devices, its later append-only generations can also travel as deltas.

This is symmetric offline convergence, not live file replication. Do not write directly into another machine's active SQLite or synchronize profile directories with a generic cloud drive while a build is running.

## Native Session Restore

An imported baseline or applied delta with canonical snapshot chunks can restore one source thread as a new native Codex session. Use `$restore-codex-session` and run `restore THREAD_ID --dry-run` before the stateful operation. Restoration reads the selected profile, materializes exact normalized events in temporary storage, rehydrates captured images under bounded deduplication rules, then asks the target Codex app-server to fork that path into its own session store.

The receiver chooses a current `--cwd`; original paths remain provenance inside the historical transcript. The new session has a new native thread ID and never overwrites the imported profile, source snapshot, or an existing Codex thread. Bundles exported with `--artifacts none` retain the textual and tool trace but cannot restore image bytes. Legacy migrated authorities must first gain canonical snapshots through `hydrate-baseline` or a rebuild.

Run the restore CLI in the same operating environment as the target Codex installation. Treat app-server path import as version-sensitive and keep the generated manifest under the target `CODEX_HOME/codex-history-restores` for provenance.

## Recovery

- A failed verification installs nothing.
- A failed staged import leaves the source bundle unchanged; inspect `.import-*` only if a process was killed outside normal exception handling.
- A failed delta restores materialized source files and cannot replace the last passing `active.json`.
- Keep the baseline and ordered delta chain until a newer verified baseline is intentionally chosen as a checkpoint.
- A lineage update preserves the previous imported profile under `backups/imports`.
- Source profiles and their `active.json` files are never rollback targets for a merge failure.
