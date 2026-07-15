# Codex History Suite

[English](README.md) | [简体中文](README.zh-CN.md)

Codex History Suite turns local Codex transcripts into a portable, evidence-first knowledge base. One core engine powers two Codex Skills:

- `build-codex-history`: initialize, discover, plan, build, incrementally update, audit, migrate, repair, and back up.
- `codex-history`: read-only progressive search, context assembly, claim inspection, evidence trace, comparison, and artifact lookup.

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
python3 scripts/codex_history.py build --max-cost-cny 0 --json
python3 scripts/codex_history.py search 'project decision' --json
```

On Windows, use `py -3 scripts\codex_history.py`. Python 3.11 or newer and SQLite FTS5 are required.

To install the CLI:

```bash
python3 -m pip install .
codex-history doctor
```

Install `.[semantic]` to enable ChromaDB. Configure a model provider in `config.toml` before switching summarization from the default zero-cost `extractive` mode to `openai-compatible`.

When semantic dependencies live in a dedicated virtual environment, set
`profiles.<name>.runtime.python` to that environment's Python executable. The
bundled CLI switches to it automatically for embedding-enabled profiles; leave
the value empty for the current interpreter or lexical-only operation.

## State Machine

```text
discover -> snapshot -> ingest -> lineage -> summarize -> index -> audit -> promote
```

Every stage is checkpointed in the staging SQLite database and in `runs/<build-id>/run.json`. The prior active build remains available after any failure.

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
