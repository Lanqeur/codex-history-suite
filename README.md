# Codex History Suite

[English](README.md) | [简体中文](README.zh-CN.md)

Codex History Suite turns local Codex transcripts into a portable, evidence-first knowledge base. One core engine powers three Codex Skills:

- `build-codex-history`: initialize, discover, plan, build, incrementally update, audit, migrate, repair, and back up.
- `codex-history`: read-only progressive and federated search, context assembly, claim inspection, evidence trace, conversation-range export, comparison, and artifact lookup.
- `restore-codex-session`: restore one canonical historical thread as a new native Codex session that can be resumed and continued.

The builder never edits source transcripts. It snapshots them into fixed-size content-addressed chunks, externalizes inline images into an artifact CAS, preserves canonical raw events, derives turns and Evidence, builds SQLite FTS and optional Chroma embeddings, audits the staging database, and atomically promotes `active.json` only after success.

For a more detailed Chinese installation and first-use guide, see [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md).

## Install The Plugin

Add this repository as a Codex marketplace, then install the plugin:

```bash
codex plugin marketplace add Lanqeur/codex-history-suite
codex plugin add codex-history-suite@codex-history-suite
```

Restart the ChatGPT desktop app and start a new Codex thread so the three bundled Skills are loaded.

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

## Original Conversation Evidence Viewer

The knowledge layers are navigation aids, not a replacement for the original
record. Version 0.9 can reconstruct selected conversations directly from the
canonical snapshot and package them into a self-contained offline HTML viewer:

```bash
# Find thread IDs by title or title fragment.
python3 scripts/codex_history.py conversation 'payment callback' --list

# Export human turns 4 through 12 with visible messages, tool calls, and goals.
python3 scripts/codex_history.py conversation THREAD_ID --turn-range 4:12 \
  --include-raw --embed-attachments -o payment-evidence.html

# Combine every thread in a scope, constrained by event time.
python3 scripts/codex_history.py conversation --scope FAMILY_ID \
  --since 2026-06-01 --until 2026-06-30 -o family-evidence.html
```

The viewer works without a server or network connection. It renders sanitized
user and assistant Markdown, including GFM tables and fenced Mermaid diagrams,
with a one-click literal source view. It also supports thread, text, role, and
time filtering; incremental rendering for long exports; exact event
provenance; evidence selection and drag ordering; and exporting the chosen
sequence as HTML, Markdown, or JSON. By default it omits injected
environment/plugin context and leaves images as content-addressed references.
Attachment metadata linked to each exact event remains visible without copying
binary content. Use `--embed-images` for a lighter image-only package, or
`--embed-attachments` to include images and captured documents. Images render
inline, text files have a bounded preview, PDFs can open in the browser, and
Office/archive files can be downloaded with their filename, MIME type, size,
original path, and SHA-256 intact. Embedded objects are content-addressed once,
with defaults of 25 MiB per file and 100 MiB total; adjust those limits with
`--max-attachment-mb` and `--max-embedded-mb`. No model call or knowledge-base
rebuild is required, but a path-only reference can be packaged only after the
file has been captured into the artifact CAS.

## Continue A Historical Thread In Native Codex

The HTML viewer is for review. When you want to continue the original work,
version 0.9 can materialize one canonical transcript and ask the target Codex
app-server to create an independent native fork:

```bash
# Locate one exact source thread, including a thread in an imported profile.
python3 scripts/codex_history.py conversation 'payment callback' --list --json

# Inspect size, images, working directory, Codex home, and warnings without writing.
python3 scripts/codex_history.py --profile laptop-default restore THREAD_ID \
  --cwd /current/project --dry-run --json

# Create and verify the new native session.
python3 scripts/codex_history.py --profile laptop-default restore THREAD_ID \
  --cwd /current/project --json
```

The result includes a new native thread ID, `codex resume THREAD_ID`, a
`codex://threads/THREAD_ID` desktop link, and an audit manifest under the target
`CODEX_HOME`. The source knowledge base, source transcript, and existing Codex
threads are never modified. Original timestamps, messages, tool calls and
outputs, Goal events, and compact records are preserved in the materialized
source. Codex owns the native fork and may rewrite session metadata or leave
rolled-back, aborted, and unsupported records outside its active view. The
audit manifest records source/native hashes, line counts, and turn counts; the
knowledge base remains the complete evidence authority.

The default image policy restores each captured image once and replaces later
duplicate base64 occurrences with a SHA-256 placeholder. This preserves the
evidence link without recreating transcript inflation. Dry-run and restore make
no model or embedding calls. Run the command in the same operating environment
as the target Codex installation, such as Windows Python for Windows Codex or
WSL Python for WSL Codex. A query-only bundle exported with `--artifacts none`
can restore text and tool history, but unavailable images become traceable
placeholders.

New profiles use a model-first two-stage preset: non-thinking `deepseek-v4-flash` reduces the token-heavy new evidence into append-only ledgers, then non-thinking `qwen3.7-max` updates the much smaller thread/family overviews. When `DASHSCOPE_API_KEY` is unavailable, deterministic evidence is still ingested, but the library is explicitly marked `pending_model_consolidation`. This is a searchable emergency fallback, not a finished summary layer; a later model-enabled `update` completes the backlog even when no transcript changed.

Existing profiles are never rewritten during a plugin upgrade. To adopt the new behavior, copy the summarization and estimation sections from [the configuration reference](skills/build-codex-history/references/configuration.md) into the existing `config.toml`, then rerun `plan`.

```bash
export DASHSCOPE_API_KEY='your-key'  # PowerShell: $env:DASHSCOPE_API_KEY='your-key'
python3 scripts/codex_history.py plan --mode full --json
```

The generated prices are editable planning inputs. The preset records CNY 1/2 per million input/output tokens for `deepseek-v4-flash`, CNY 6/1.2/18 for Qwen writer uncached-input/cached-input/output, and CNY 0.5 per million input tokens for `text-embedding-v4`. Always verify the [current Model Studio pricing](https://help.aliyun.com/zh/model-studio/model-pricing) for the selected region and deployment.

`plan` and `update --dry-run` report transcript bytes, new/reprocessed bytes, pending fact blocks, separate reducer/writer tokens and prices, expected cached input, embedding tokens, expected and conservative CNY cost, and a low/expected/upper disk estimate. Inline data-URI base64 is excluded from model-token estimates while remaining in snapshot storage. A key is not required just to budget a build.

Completed builds return an actual `usage` summary for model input, provider-cached input, output, embedding tokens, response-cache hits, and CNY cost, plus `storage` totals for the active core components and the whole retained profile.

## Referenced Files And Git Checkpoints

Version 0.5.2 can preserve ordinary files named by absolute transcript paths and
checkpoint referenced Git repositories without mixing source trees into the
transcript database. Enable the reviewed artifact policy, inspect the zero-write
plan, then run a zero-model artifact-only build:

```bash
python3 scripts/codex_history.py --profile default library artifact-plan \
  --since 2026-07-10T00:00:00Z --json
python3 scripts/codex_history.py --profile default library capture-artifacts \
  --since 2026-07-10T00:00:00Z --json
```

The plan canonicalizes WSL/Windows path aliases, excludes the profile, Codex
source homes, registered artifact roots, configured roots, and temporary
storage, applies an extension allowlist and size limits, then deduplicates by
SHA-256. A capture build copies the active SQLite authority, adds versioned
Event/Evidence artifact observations, rebuilds artifact FTS, audits closure, and
atomically promotes with zero summary or embedding calls.

Normal Git clones use `git bundle --all`. Partial clones default to a network-free
HEAD archive rather than silently downloading missing history; enabling network
completion is explicit. Dirty repositories retain the history artifact plus a
deterministic tracked-and-untracked worktree snapshot. Unchanged repository
fingerprints reuse the existing checkpoint.

## Multiple Devices

Version 0.4 adds canonical baselines plus generation-checked deltas. Give each machine a stable identity, create and transfer one complete baseline, then move only new content-addressed transcript chunks, artifacts, and model-cache entries:

```bash
python3 scripts/codex_history.py library device --name 'Work laptop' --json
python3 scripts/codex_history.py --profile default coverage --json
python3 scripts/codex_history.py --profile default library artifact-audit --verify-hashes --json
python3 scripts/codex_history.py --profile default library export ~/work-laptop.zip \
  --artifacts referenced --json
python3 scripts/codex_history.py library import ~/work-laptop.zip --json

# After later local conversations and a successful incremental update:
python3 scripts/codex_history.py --profile default update --max-cost-cny 5 --json
python3 scripts/codex_history.py --profile default library export-delta ~/work-laptop-001.zip \
  --base ~/work-laptop.zip --artifacts referenced --json

# On the receiving machine; the baseline is not transferred again:
python3 scripts/codex_history.py library apply-delta ~/work-laptop-001.zip \
  --max-cost-cny 5 --json
python3 scripts/codex_history.py library list --json
python3 scripts/codex_history.py library search 'release decision' --deep --json
```

The next delta can use the previous delta as `--base`. Each delta contains the complete target source inventory and artifact mappings but packages only blobs absent from its base generation. Stable evidence-based model cache keys are included by default, so the receiver can reproduce source-side consolidation without duplicate model charges. `apply-delta` requires the exact `library_id` and a compatible source generation, and rejects missing, out-of-order, cross-library, or tampered deltas before promotion. An artifact-only delta remains actionable when its transcript generation is unchanged; it merges mappings and verifies CAS closure with zero model calls.

Imported profiles are named from the source device and profile, with collision suffixes added automatically. A stable `library_id` recognizes later generations of the same library; a newer import updates that profile while preserving the prior generation under `backups/imports`. Every bundle entry is verified with SHA-256, unsafe archive paths are rejected, and immutable transcript chunks, artifacts, semantic files, and model-cache entries share a global content-addressed blob store through hard links when the filesystem permits it.

Artifact export is explicit. `--artifacts none` creates a smaller query-only bundle whose SQLite keeps artifact metadata but intentionally omits file payloads. `referenced` is the default portable mode and includes every artifact indexed by the active database. `all` also includes unreferenced files retained in local or registered external CAS roots. Referenced and all exports fail when database-to-CAS closure is missing, the size disagrees, or SHA-256 verification fails. The bundle manifest records both the selected policy and computed closure.

Every new bundle also records `history_coverage`: the earliest and latest represented conversation activity, source scan and snapshot watermarks, build completion, thread/source/event counts, logical digest, and a stable knowledge-version ID. `latest_activity_at` means “the latest timestamp actually represented,” while `source_scan_started_at` means “when local sources were observed”; neither field alone proves that every possible transcript in the interval exists. Inspect the same watermark at any time with `coverage --json` or in `status --json` and `library list --json`.

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

`library sync` performs the merge/build and exports one convergence baseline. Import that same baseline on both devices; subsequent generations of that merged lineage can also travel as deltas. Repeated imports, delta application, and merges are idempotent by library lineage, source generation, and content digest. Absolute paths remain in provenance; automatic exact-file/root mappings and optional `--path-map 'OLD=NEW'` mappings expose usable local paths at query time without rewriting historical evidence.

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

Paid builds require an explicit `--max-cost-cny` after reviewing the dry-run. Every reducer input Record ID must be represented in a ledger fact or an explicit no-new-fact list; one failed repair, provider error, malformed writer response, or exhausted budget fails the candidate build and leaves the prior active library untouched. Exact model-response cache hits cost zero.

## Incremental Invariant

`codex-history audit --equivalence` creates a clean extractive reference build and requires exact equality for canonical sources, parsed events/turns, Evidence occurrences, deterministic core/fact records, and artifacts. Model ledgers, overviews, claims, and semantic documents are generation-specific; their differences are reported separately instead of being misclassified as source-authority failures.

Fresh builds generate only conservative, evidence-exact fact relations. Verified tool outputs can validate the matching call, and completed goals can validate the same earlier objective. Ambiguous contradiction, invalidation, and reopening labels are never inferred automatically.

## Legacy Migration

`migrate --from-db` preserves and audits an existing v2.1/v2.1.1 SQLite authority; `--from-chroma` can copy its semantic index. Use `--from-artifacts ARTIFACT_PACK --artifact-mode reference` to verify and register a large external CAS without duplicating it, or select `copy`, `hardlink`, or `auto` to materialize files under the profile. The migrated build is immediately queryable but is not yet a canonical incremental baseline. Run `hydrate-baseline` to attach normalized source snapshots while preserving curated Overview, ledger, Evidence, relations, and the existing semantic index; then use `compact-storage` to remove duplicate raw payloads after trace-offset verification. Neither step calls a model. Promotion is atomic, so the imported build remains available for rollback and comparison.

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
python3 /path/to/skill-creator/scripts/quick_validate.py skills/restore-codex-session
python3 /path/to/plugin-creator/scripts/validate_plugin.py .
```
