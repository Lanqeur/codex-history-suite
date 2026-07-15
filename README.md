# Codex History Suite

[English](README.md) | [简体中文](README.zh-CN.md)

Codex History Suite turns local Codex transcripts into a portable, evidence-first knowledge base. One core engine powers two Codex Skills:

- `build-codex-history`: initialize, discover, plan, build, incrementally update, audit, migrate, repair, and back up.
- `codex-history`: read-only progressive and federated search, context assembly, claim inspection, evidence trace, comparison, and artifact lookup.

The builder never edits source transcripts. It snapshots them into fixed-size content-addressed chunks, externalizes inline images into an artifact CAS, preserves canonical raw events, derives turns and Evidence, builds SQLite FTS and optional Chroma embeddings, audits the staging database, and atomically promotes `active.json` only after success.

For a more detailed Chinese installation and first-use guide, see [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md).

## Install The Plugin

Add this repository as a Codex marketplace, then install the plugin:

```bash
codex plugin marketplace add Lanqeur/codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

Restart the ChatGPT desktop app and start a new Codex thread so the two bundled Skills are loaded.

## Quick Start

The plugin is self-contained and can run without installing a package:

```bash
python3 scripts/codex_history.py doctor --json
python3 scripts/codex_history.py init --source ~/.codex --json
python3 scripts/codex_history.py plan --mode full --json
python3 scripts/codex_history.py build --max-cost-cny 30 --json  # replace 30 with the reviewed upper limit
python3 scripts/codex_history.py search 'project decision' --json
```

On Windows, use `py -3 scripts\codex_history.py`. Python 3.11 or newer and SQLite FTS5 are required.

To install the CLI:

```bash
python3 -m pip install .
codex-history doctor
```

New profiles use model-first `auto` summarization. The generated preset recommends DashScope's non-thinking `deepseek-v4-flash`; when `DASHSCOPE_API_KEY` is unavailable, it reports the reason and falls back to deterministic `extractive` summaries. This keeps first use functional, but model summaries are strongly recommended for better cross-turn synthesis, durable assets, and evidence-linked overviews.

Existing profiles are never rewritten during a plugin upgrade. To adopt the new behavior, copy the summarization and estimation sections from [the configuration reference](skills/build-codex-history/references/configuration.md) into the existing `config.toml`, then rerun `plan`.

```bash
export DASHSCOPE_API_KEY='your-key'  # PowerShell: $env:DASHSCOPE_API_KEY='your-key'
python3 scripts/codex_history.py plan --mode full --json
```

The generated prices are editable planning inputs. As of 2026-07-15, Alibaba Cloud lists `deepseek-v4-flash` at CNY 1 per million input tokens and CNY 2 per million output tokens in the Chinese mainland deployment; always verify the [current Model Studio pricing](https://help.aliyun.com/zh/model-studio/model-pricing). Direct DeepSeek API users can use its OpenAI-compatible endpoint and enter converted CNY prices from the [official DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing).

`plan` and `update --dry-run` report transcript bytes, new/reprocessed bytes, expected and upper summary tokens, expected cached input, output and embedding tokens, expected and conservative CNY cost, and a low/expected/upper disk estimate split into snapshots, SQLite, artifact CAS, semantic index, and model-response cache. Inline data-URI base64 payloads are scanned and excluded from model-token estimates while remaining in snapshot storage estimates. The report still shows the potential model cost when `auto` has fallen back, so a key is not required just to budget a build.

Completed builds return an actual `usage` summary for model input, provider-cached input, output, embedding tokens, response-cache hits, and CNY cost, plus `storage` totals for the active core components and the whole retained profile.

## Multiple Devices

Version 0.3 adds a verified library bundle and non-destructive multi-device workflow. Give each machine a stable identity, export its active profile, and import both bundles on either machine or on a separate hub:

```bash
python3 scripts/codex_history.py library device --name 'Work laptop' --json
python3 scripts/codex_history.py --profile default library export ~/work-laptop.zip --json
python3 scripts/codex_history.py library import ~/work-laptop.zip --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search 'release decision' --deep --json
```

Imported profiles are named from the source device and profile, with collision suffixes added automatically. A stable `library_id` recognizes later generations of the same library; a newer import updates that profile while preserving the prior generation under `backups/imports`. Every bundle entry is verified with SHA-256, unsafe archive paths are rejected, and immutable transcript chunks, artifacts, semantic files, and model-cache entries share a global content-addressed blob store through hard links when the filesystem permits it.

Federated search queries independent SQLite/Chroma authorities and collapses exact knowledge duplicates while retaining every matching profile and Record ID. It is immediately useful and does not rebuild anything. A merge is different: it reconstructs transcript snapshots by stable thread ID, chooses exact or longest-prefix variants, performs a deterministic event union for divergent copies, and writes a new generated profile without changing either source:

```bash
python3 scripts/codex_history.py library merge \
  --from work-laptop-default --from desktop-default \
  --as personal-history --json
# Review the returned full plan before allowing model work:
python3 scripts/codex_history.py library merge \
  --from work-laptop-default --from desktop-default \
  --as personal-history --build --max-cost-cny 30 --json
```

`library sync` performs the merge/build and exports one convergence bundle. Import that same bundle on both devices. Repeated imports and merges are idempotent by library lineage and content digest. Absolute paths remain in provenance; automatic exact-file/root mappings and optional `--path-map 'OLD=NEW'` mappings expose usable local paths at query time without rewriting historical evidence.

See [the multi-device reference](skills/build-codex-history/references/multi-device.md) for the bundle format, conflict rules, offline two-way synchronization, and recovery procedure.

Install `.[semantic]` to enable ChromaDB. Model summarization and semantic retrieval are independent: the recommended model-first summaries work with lexical SQLite retrieval, while Chroma can be enabled separately.

When semantic dependencies live in a dedicated virtual environment, set
`profiles.<name>.runtime.python` to that environment's Python executable. The
bundled CLI switches to it automatically for embedding-enabled profiles; leave
the value empty for the current interpreter or lexical-only operation.

## State Machine

```text
discover -> snapshot -> ingest -> lineage -> summarize -> index -> audit -> promote
```

Every stage is checkpointed in the staging SQLite database and in `runs/<build-id>/run.json`. The prior active build remains available after any failure.

Paid builds require an explicit `--max-cost-cny` after reviewing the dry-run. Exact model-response cache hits cost zero; provider-side cached input is costed separately using the user-entered cached-input price and expected hit ratio. API failures never silently downgrade a paid model build to extractive mode.

## Incremental Invariant

`codex-history audit --equivalence` creates a clean full reference build from the same current sources and compares stable logical digests for sources, chunks, events, turns, scopes, Evidence, Knowledge, claims, artifacts, and semantic documents. Incremental updates are releasable only when this comparison passes.

Fresh builds generate only conservative, evidence-exact fact relations. Verified tool outputs can validate the matching call, and completed goals can validate the same earlier objective. Ambiguous contradiction, invalidation, and reopening labels are never inferred automatically.

## Legacy Migration

`migrate --from-db` preserves and audits an existing v2.1/v2.1.1 SQLite authority; `--from-chroma` can copy its semantic index. The migrated build is immediately queryable but intentionally read-only as a legacy baseline. Run one full build before the first incremental update. Promotion is atomic, so the imported build remains available for rollback and comparison.

## Cross-Platform Storage

- Windows: `%LOCALAPPDATA%\codex-history`
- macOS: `~/Library/Application Support/codex-history`
- Linux and WSL: `$XDG_DATA_HOME/codex-history` or `~/.local/share/codex-history`

Set `CODEX_HISTORY_HOME` or use `--home` to override. WSL users should keep the active SQLite/Chroma runtime on the Linux filesystem and use mounted Windows drives for exported backups.

## Development

```bash
PYTHONPATH=src python3 -m pytest
python3 /path/to/skill-creator/scripts/quick_validate.py skills/build-codex-history
python3 /path/to/skill-creator/scripts/quick_validate.py skills/codex-history
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```
