# Configuration

## Contents

- Storage and profiles
- Source discovery
- Summarization
- Semantic retrieval
- Optional runtime
- Cost controls

## Storage And Profiles

The default data home is `%LOCALAPPDATA%\codex-history` on Windows, `~/Library/Application Support/codex-history` on macOS, and `$XDG_DATA_HOME/codex-history` or `~/.local/share/codex-history` on Linux and WSL. `CODEX_HISTORY_HOME` overrides it. Use `--home` and `--profile` for explicit selection.

Each profile owns immutable chunked snapshots, artifact CAS, builds, model cache, reports, and `active.json`. On WSL, keep active SQLite, Chroma, and caches on the Linux filesystem. Mounted Windows drives are appropriate for exported backups.

## Source Discovery

`source_roots` normally contains one or more Codex homes. The adapter scans `sessions/**/*.jsonl`, optional `archived_sessions/*.jsonl`, `session_index.jsonl`, and compatible state databases. WSL discovery considers both Linux and Windows Codex homes, but explicit source roots win.

Transcript storage is treated as a versioned, private input format. Unknown JSON records remain in canonical raw evidence even when they do not become searchable knowledge.

## Summarization

`summarization.mode = "extractive"` is offline and costs zero. Set it to `"openai-compatible"` to enable evidence-linked map/reduce summaries. Configure endpoint, model, API-key environment variable, optional `env_file`, and input/output prices. The model must return cited Record IDs; unsupported claims remain unlinked.

Model responses are cached by stage version, model, prompt hash, and input hash. A clean full rebuild reuses identical cached outputs.

## Semantic Retrieval

Set `embedding.enabled = true` to use ChromaDB. Install the `semantic` package extra and configure endpoint, API-key environment variable, optional `env_file`, model, dimensions, and input price. The tested preset is DashScope `text-embedding-v4` at 512 dimensions.

SQLite FTS remains authoritative and available without Chroma. Deleting or rebuilding Chroma cannot delete historical evidence.

## Optional Runtime

`profiles.<name>.runtime.python` may point to a Python 3.11+ executable that has
the `semantic` extra installed. The bundled CLI automatically re-executes in
that interpreter when embeddings are enabled. This keeps the base plugin
zero-dependency while avoiding machine-specific virtual-environment commands in
the Skills. Leave it empty when the current interpreter already has ChromaDB.

## Cost Controls

Always run `plan` or `update --dry-run`. Paid commands require a user-selected `--max-cost-cny`. The engine checks an upper estimate before the run and reserves budget before each uncached model batch. Cache hits cost zero.
