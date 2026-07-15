---
name: build-codex-history
description: Initialize, discover, cost-plan, build, incrementally update, audit, repair, back up, or migrate a local Codex History knowledge base. Use when the user explicitly wants to create or maintain the evidence-first database from Codex CLI, IDE, or desktop transcripts on Windows, WSL, macOS, or Linux. This skill performs stateful operations and model calls only after a dry-run and an explicit cost limit.
---

# Build Codex History

Use the plugin's shared CLI as the only orchestration engine. Do not recreate the pipeline as ad hoc shell steps and never modify source transcripts.

Resolve `../../scripts/codex_history.py` relative to this Skill directory. Run it with Python 3.11 or newer (`python3` on POSIX; `py -3` or `python` on Windows). Add `--json` when interpreting results programmatically.

## Lifecycle

1. Run `doctor --json`. Explain failed checks and storage warnings before proceeding.
2. If uninitialized, run `init`. Use discovered Codex homes only when unambiguous; otherwise pass one or more explicit `--source` paths.
3. Run `discover --json`, then `plan --mode full --json` for an initial build or `update --dry-run --json` for later changes.
4. Report source/change counts, total and new/reprocessed bytes, requested and effective summarization modes, fallback reason, expected/upper model tokens, cached input assumptions, expected/upper CNY cost, and low/expected/upper managed storage. When `auto` falls back because the API key is absent, also report the potential model cost and explain that model summaries are the recommended quality path.
5. Never start paid work without a user-approved `--max-cost-cny` value. Use the conservative `estimated_cost_cny` from the plan as the minimum safe limit unless the user intentionally changes configuration and reruns the plan.
6. Run `build --max-cost-cny N --json` or `update --max-cost-cny N --json`. Keep promotion enabled unless the user explicitly requests a staging-only build.
7. Confirm every state-machine stage completed and the build audit passed. Report the completed build's actual `usage` and `storage`, and compare actual cost with the plan. A failed build must leave the prior `active.json` untouched.
8. After the first incremental update, after pipeline changes, or when correctness is disputed, run `audit --equivalence --json`. Treat any table digest difference as a release failure.

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
```

Use `--home` and `--profile` before the command for non-default installations.

## Migration And Recovery

Use `migrate --from-db PATH` for an existing v2.1/v2.1.1 SQLite authority. Migration uses SQLite backup, adds portable metadata and lifecycle tables, rebuilds FTS, audits, and only then promotes it. It does not silently discard legacy evidence or knowledge. The imported authority is query-compatible, not a canonical incremental baseline; run one full build before the first `update`, keeping the migrated build for comparison and rollback.

Use `status` to inspect failed stages. Use `repair` only after reading [references/recovery.md](references/recovery.md). Never delete the last passing build or clear a lock while a process is alive.

Read [references/configuration.md](references/configuration.md) when configuring model providers, embeddings, WSL storage, or multiple profiles. Read [references/lifecycle.md](references/lifecycle.md) when diagnosing change classification, checkpoints, promotion, or equivalence.

`auto` may fall back only when required model configuration or the API key is absent. A provider error, malformed response, exhausted budget, or interrupted request must fail the staging build rather than silently replacing model output with extractive output.
