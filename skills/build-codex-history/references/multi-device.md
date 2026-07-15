# Multi-Device Libraries

## Safety Model

- A source profile is never edited by import, federated search, merge, or sync.
- Import installs a separate profile. A newer bundle from the same stable `library_id` replaces only that imported generation and moves the previous generation to `backups/imports`.
- Merge writes a generated profile. Its transcript source set can be rebuilt repeatedly without changing either parent.
- Paid summary or embedding work still requires a reviewed plan and explicit `--max-cost-cny`.

## Bundle Contract

`library export` creates a ZIP with `bundle.json`, an SQLite backup, active transcript snapshot chunks and manifests, the complete artifact CAS, and optional semantic/model-cache files. The manifest records SHA-256, size, role, source device, profile lineage, active build, logical digest, capabilities, non-secret provider settings, and historical source roots.

`library verify` rejects a missing file, size mismatch, SHA-256 mismatch, unsupported schema, absolute archive path, or path containing `..`. Import performs the same verification before installing data.

Immutable files enter `<home>/shared/blobs/<sha-prefix>/<sha256>`. The imported profile receives a hard link when supported and a verified copy otherwise. SQLite is copied independently because it receives local schema/path metadata.

## Naming And Lineage

Run `library device --name NAME` once per installation. Without an explicit import name, the profile becomes `<device-slug>-<source-profile>` and receives `-2`, `-3`, and so on only for unrelated name collisions.

`library_id` identifies a logical library across exports. `bundle_id` identifies one audited build generation. Reimporting the same bundle is a no-op. Importing a different bundle with the same library ID updates the existing imported profile and preserves its prior directory under `backups/imports`.

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

Run merge without `--build` first. It returns source counts, conflict methods, a stable source digest, and a build plan. After approval, repeat with `--build --max-cost-cny N`. Repeating the same merge yields the same source digest and an incremental `no_changes` result.

## Offline Two-Way Convergence

```bash
python3 scripts/codex_history.py library sync shared-history.zip \
  --from laptop-default --from desktop-default \
  --as shared-history --max-cost-cny 30 --json
```

Import `shared-history.zip` on both devices. Both now contain the same merged lineage alongside their untouched local profile. Continue collecting new transcripts in each local profile. At the next sync, export both updated local profiles, import them on the hub, rerun `library sync`, and import the new convergence bundle on both ends.

This is symmetric offline convergence, not live file replication. Do not write directly into another machine's active SQLite or synchronize profile directories with a generic cloud drive while a build is running.

## Recovery

- A failed verification installs nothing.
- A failed staged import leaves the source bundle unchanged; inspect `.import-*` only if a process was killed outside normal exception handling.
- A lineage update preserves the previous imported profile under `backups/imports`.
- Source profiles and their `active.json` files are never rollback targets for a merge failure.
