---
name: build-codex-history
description: Initialize, discover, cost-plan, build, incrementally update, audit, repair, migrate, export, import, deduplicate, merge, or synchronize local Codex History knowledge bases. Use when the user wants to create, maintain, move, or combine evidence-first databases from Codex CLI, IDE, or desktop transcripts on Windows, WSL, macOS, or Linux. This skill performs stateful operations and model calls only after a dry-run and an explicit cost limit.
---

# Build Codex History

Use the plugin's shared CLI as the only orchestration engine. Do not recreate the pipeline as ad hoc shell steps and never modify source transcripts.

Resolve `../../scripts/codex_history.py` relative to this Skill directory. Run it with Python 3.11 or newer (`python3` on POSIX; `py -3` or `python` on Windows). Add `--json` when interpreting results programmatically.

## Lifecycle

1. Run `doctor --json`. Explain failed checks and storage warnings before proceeding.
2. If uninitialized, run `init`. Use discovered Codex homes only when unambiguous; otherwise pass one or more explicit `--source` paths.
3. Run `discover --json`, then `plan --mode full --json` for an initial build or `update --dry-run --json` for later changes.
4. Report source/change counts, total and new/reprocessed bytes, pending model fact blocks, requested and effective summarization modes, reducer/writer models, fallback reason, expected/upper model tokens, cached input assumptions, expected/upper CNY cost, and low/expected/upper managed storage. When `auto` lacks either model key, explain that deterministic evidence can be staged but remains `pending_model_consolidation`, not a finished knowledge layer.
5. Never start paid work without a user-approved `--max-cost-cny` value. Use the conservative `estimated_cost_cny` from the plan as the minimum safe limit unless the user intentionally changes configuration and reruns the plan.
6. Run `build --max-cost-cny N --json` or `update --max-cost-cny N --json`. Keep promotion enabled unless the user explicitly requests a staging-only build.
7. Confirm every state-machine stage completed, the evidence coverage gate passed, and the build audit passed. Report the completed build's actual `usage`, `storage`, and `knowledge_completion_status`, and compare actual cost with the plan. A failed build must leave the prior `active.json` untouched.
8. After the first incremental update, after pipeline changes, or when correctness is disputed, run `audit --equivalence --json`. Treat authority-layer differences as a release failure; inspect generation-specific `derived_layer_differences` separately.

The fixed state machine is `discover -> snapshot -> ingest -> lineage -> summarize -> index -> audit -> promote`. Chunked snapshots and artifact CAS are immutable and content addressed. SQLite is authoritative; Chroma supplies semantic candidates only.

## Commands

```bash
python3 ../../scripts/codex_history.py doctor --json
python3 ../../scripts/codex_history.py init --source ~/.codex --json
python3 ../../scripts/codex_history.py discover --json
python3 ../../scripts/codex_history.py plan --mode full --json
python3 ../../scripts/codex_history.py build --max-cost-cny 30 --json
python3 ../../scripts/codex_history.py update --dry-run --json
python3 ../../scripts/codex_history.py update --max-cost-cny 5 --json
python3 ../../scripts/codex_history.py audit --equivalence --json
python3 ../../scripts/codex_history.py status --json
python3 ../../scripts/codex_history.py coverage --json
python3 ../../scripts/codex_history.py library artifact-plan --since TIMESTAMP --json
python3 ../../scripts/codex_history.py library capture-artifacts --since TIMESTAMP --json
```

Use `--home` and `--profile` before the command for non-default installations.

## Referenced Files And Repositories

Never enable broad absolute-path capture without first reviewing `library
artifact-plan`. Require an extension allowlist and exclude Codex History storage,
source transcript roots, registered artifact roots, archive/export roots, and
temporary output unless the user intentionally includes it. The planner must
deduplicate by content hash, not path spelling; WSL and Windows aliases can
identify the same file.

Use `library capture-artifacts` for a backfill that should not rerun
summarization. It copies the active authority, records Event/Evidence-linked
artifact observations with separate occurrence/capture timestamps, creates Git
checkpoints, audits closure, and only then promotes. Report zero model and
embedding calls explicitly.

Normal Git repositories use a complete bundle. Partial clones default to a
network-free HEAD archive; never enable history completion without explaining
that missing blobs can be downloaded. Dirty repositories require an additional
tracked plus non-ignored-untracked worktree snapshot. Reuse an existing
checkpoint when its refs and worktree fingerprints are unchanged.

## Migration And Recovery

Use `migrate --from-db PATH` for an existing v2.1/v2.1.1 SQLite authority. Add `--from-artifacts PATH --artifact-mode reference|copy|hardlink|auto` when the legacy database indexes an external artifact pack. Migration uses SQLite backup, adds portable metadata and lifecycle tables, rebuilds FTS, verifies artifact closure when requested, audits, and only then promotes it. It does not silently discard legacy evidence or knowledge. The imported authority is query-compatible, not yet a canonical incremental baseline. Run `hydrate-baseline` to attach normalized source snapshots while preserving curated knowledge and the existing semantic index, then optionally run `compact-storage` to remove duplicate canonical payloads. Both operations cost zero model tokens. Keep the migrated build for comparison and rollback until the hydrated baseline passes audit.

Use `status` to inspect failed stages. Use `repair` only after reading [references/recovery.md](references/recovery.md). Never delete the last passing build or clear a lock while a process is alive.

Read [references/configuration.md](references/configuration.md) when configuring model providers, embeddings, WSL storage, or multiple profiles. Read [references/lifecycle.md](references/lifecycle.md) when diagnosing change classification, checkpoints, promotion, or equivalence.

## Device Libraries

Use `library device`, `library export`, `library import`, and `library verify` for the one-time canonical device baseline. Run `coverage --json` and explain both the represented `latest_activity_at` and source observation watermark before migration; do not call bundle creation time a content cutoff. Run `library artifact-audit --verify-hashes` before an archival export. Select `library export --artifacts none` for an explicitly query-only bundle, `referenced` for the default database-closed portable bundle, or `all` to retain unreferenced CAS objects too. Never describe `none` or the old `backup` command as a complete artifact backup.

After the baseline is imported, use `library export-delta DEST --base PREVIOUS_TRANSFER` on the source and `library apply-delta DELTA` on the receiver. The previous transfer may be the baseline or the immediately preceding delta. Keep model cache deltas enabled unless the user explicitly accepts possible repeated model calls. Report base and target source generations, changed-source counts, payload size, and coverage watermarks. Never advise retransferring a full multi-gigabyte bundle for an ordinary append. Do not bypass a generation mismatch: missing, out-of-order, cross-lineage, and tampered deltas must fail before promotion.

Use `library search` when the user wants immediate cross-device retrieval without rebuilding. It preserves independent authorities and collapses exact duplicate knowledge while returning every source profile and Record ID.

Use `library merge` only after explaining that it creates or updates a separate generated profile. Run it without `--build` first and report the transcript merge methods plus the returned full/incremental cost plan. Add `--build --max-cost-cny N` only after explicit approval. Never modify either source profile.

Use `library sync DESTINATION --from A --from B` when the user wants offline two-way convergence. It produces one merged, audited baseline; the same baseline must be imported on both devices. Later generations of that merged lineage should use deltas. A newer full baseline with the same stable `library_id` can still replace an imported generation while preserving the previous one under `backups/imports`.

Read [references/multi-device.md](references/multi-device.md) before handling divergent transcript copies, path mappings, repeated synchronization, or import recovery.

`auto` may stage deterministic evidence only when required model configuration or an API key is absent, and must label the result `pending_model_consolidation`. A provider error, malformed response, incomplete Record-ID coverage, exhausted budget, or interrupted request must fail the staging build rather than silently replacing model output with extractive output. A later model-enabled `update` must process pending records even when source files are unchanged.
