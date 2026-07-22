# Configuration

## Contents

- Storage and profiles
- Source discovery
- Referenced files and Git checkpoints
- Summarization
- Semantic retrieval
- Optional runtime
- Cost controls

## Storage And Profiles

The default data home is `%LOCALAPPDATA%\codex-history` on Windows, `~/Library/Application Support/codex-history` on macOS, and `$XDG_DATA_HOME/codex-history` or `~/.local/share/codex-history` on Linux and WSL. `CODEX_HISTORY_HOME` overrides it. Use `--home` and `--profile` for explicit selection.

Each profile owns immutable chunked snapshots, artifact CAS, builds, model cache, reports, and `active.json`. On WSL, keep active SQLite, Chroma, and caches on the Linux filesystem. Mounted Windows drives are appropriate for exported backups.

Before a candidate build, the engine compares real free space with the active SQLite/semantic copy plus configured headroom. Successful promotion automatically retains the active build and one rollback build by default:

```toml
[profiles.default.runtime]
python = ""
retained_builds = 2
min_free_bytes = 536870912
peak_headroom_ratio = 0.15
```

`retained_builds` includes the active build. Increase it only when the extra complete SQLite copies are intentional. Shared content-addressed snapshots, artifact CAS, model cache, and exported packages are not deleted by build retention.

## Source Discovery

`source_roots` normally contains one or more Codex homes. The adapter scans `sessions/**/*.jsonl`, optional `archived_sessions/*.jsonl`, `session_index.jsonl`, and compatible state databases. WSL discovery considers both Linux and Windows Codex homes, but explicit source roots win.

Transcript storage is treated as a versioned, private input format. Unknown JSON records remain in canonical raw evidence even when they do not become searchable knowledge.

## Referenced Files And Git Checkpoints

Absolute-path capture is opt-in. Use a narrow document/archive allowlist because
unfiltered transcript paths commonly point back into Codex exports, caches,
databases, generated queues, and temporary workspaces.

```toml
[profiles.default.artifacts]
capture_existing_paths = true
max_file_bytes = 536870912
allowed_extensions = [".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"]
excluded_roots = ["/mnt/d/CodexTranscriptArchive"]
exclude_temporary = true
capture_git_repositories = true
git_allow_network = false
git_capture_dirty_worktree = true
git_max_bytes = 1073741824
git_command_timeout_seconds = 600
```

`artifact-plan` is mandatory before the first capture or a policy expansion. It
does not write files. It reports parsed paths, exclusions, existing files,
content hashes already available in any registered CAS, new bytes, repository
modes, and pending observations. `capture-artifacts` creates a copied candidate
SQLite database, captures only the approved plan, records occurrence time
separately from capture time, audits database-to-CAS closure, and atomically
promotes. It never calls summary or embedding models.

Complete clones use `git bundle --all`. With `git_allow_network = false`, partial
clones use a HEAD archive and `GIT_NO_LAZY_FETCH=1`; this prevents a checkpoint
from unexpectedly downloading old blobs. Set network access to true only after
reviewing repository size. Dirty repositories add a deterministic worktree
archive containing tracked and non-ignored untracked files. Ignored files and
`.git` are not copied into that archive.

Profile storage, source transcript roots, registered external artifact roots,
and the platform temporary directory are excluded automatically. Set
`exclude_temporary = false` only when temporary files are intentional evidence.
Add archive/export roots to `excluded_roots` to prevent self-ingestion loops.

## Summarization

New profiles use `summarization.mode = "auto"`. Auto mode uses a low-cost reducer for evidence-to-ledger consolidation and a separate high-quality writer for thread/family overviews. Both must be configured. If either key or endpoint is absent, ingestion still preserves deterministic core/fact evidence, but the build is explicitly marked `pending_model_consolidation`; this is an emergency offline fallback, not a completed knowledge layer. A later `update` processes that backlog even when no transcript changed. Use `"extractive"` to force this offline staging behavior or `"openai-compatible"` to require both models and fail on incomplete configuration.

The generated quality/cost preset is:

```toml
[profiles.default.summarization]
mode = "auto"
provider = "dashscope"
model = "deepseek-v4-flash"
endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
env_file = ""
thinking_enabled = false
input_price_cny_per_million = 1.0
cached_input_price_cny_per_million = 0.2
output_price_cny_per_million = 2.0

[profiles.default.summarization.writer]
provider = "dashscope"
model = "qwen3.7-max"
endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
env_file = ""
thinking_enabled = false
input_price_cny_per_million = 6.0
cached_input_price_cny_per_million = 1.2
output_price_cny_per_million = 18.0
```

Prices are user-owned planning inputs, not a live billing feed. Verify the provider's current region, deployment, cache, tier, and batch pricing before a paid build. DeepSeek handles the token-heavy reducer stage; Qwen handles the much smaller final overview stage. `thinking_enabled = false` avoids reasoning-token cost on this structured workload.

Every reducer response must account for every supplied Record ID, either in an evidence-linked ledger item or an explicit no-new-fact list. One repair call is allowed; repeated omissions fail the candidate build. Writer claims and assets must cite ledger Record IDs. Provider failures, malformed responses, and exhausted budgets never trigger a silent fallback.

New ledger generations are appended rather than rewriting old ledgers. Existing overviews are archived in `knowledge_versions` before replacement. Model responses are cached by stable scope/evidence input rather than local build ID, so source-side cache entries can travel in a delta and prevent duplicate calls on another device.

## Planning Estimates

`plan` and `update --dry-run` calculate both the effective build and the model-enabled alternative. The report separates reducer and writer tokens/prices, pending fact-block backlog, provider cached input, embedding tokens, expected cost, conservative no-cache cost, and managed disk usage.

```toml
[profiles.default.estimation]
bytes_per_token = 3.0
summary_input_ratio = 0.30
summary_output_ratio = 0.08
embedding_input_ratio = 0.15
cached_input_ratio = 0.0
sqlite_to_source_ratio = 0.18
artifact_to_source_ratio = 0.08
semantic_to_source_ratio = 0.05
```

The first plan uses these transparent defaults. Once an active database exists, the estimator calibrates summary-density, SQLite, artifact, and semantic ratios from that profile. `cached_input_ratio` is an expected provider-prefix cache ratio between 0 and 1; keep it at zero for a conservative first build. Exact Codex History model-response cache hits cost zero and are reported separately from provider cache hits.

The storage range covers current-state content-addressed transcript snapshots, the active SQLite build, artifact CAS, optional Chroma data, and model responses. It excludes the original transcripts and old retained builds. `resource_preflight` separately estimates transient peak space for the candidate copy and configured safety margin. Capturing existing absolute-path files can exceed the range because those files are not bounded by transcript size.

## Semantic Retrieval

Set `embedding.enabled = true` to use ChromaDB. Install the `semantic` package extra and configure endpoint, API-key environment variable, optional `env_file`, model, dimensions, and input price. The tested preset is DashScope `text-embedding-v4` at 512 dimensions; generated profiles use an editable CNY 0.5/million input-token planning price. `embedding_input_ratio` estimates unique semantic-document tokens from new model-relevant transcript bytes and is deliberately separate from summary density. Semantic candidate text is capped at 3,900 characters through deterministic head/middle/tail projection and batches are limited to eight documents, so provider limits cannot truncate authoritative SQLite/FTS/Evidence text.

SQLite FTS remains authoritative and available without Chroma. Deleting or rebuilding Chroma cannot delete historical evidence.

## Optional Runtime

`profiles.<name>.runtime.python` may point to a Python 3.11+ executable that has
the `semantic` extra installed. The bundled CLI automatically re-executes in
that interpreter when embeddings are enabled. This keeps the base plugin
zero-dependency while avoiding machine-specific virtual-environment commands in
the Skills. Leave it empty when the current interpreter already has ChromaDB.

The same runtime section controls `retained_builds`, `min_free_bytes`, and `peak_headroom_ratio` as shown under Storage And Profiles.

## Cost Controls

Always run `plan` or `update --dry-run`. Paid commands require a user-selected `--max-cost-cny`. The engine checks the conservative upper estimate before the run and reserves budget before each uncached model batch. Exact response-cache hits cost zero; provider cached-input tokens use `cached_input_price_cny_per_million`. Every successful, failed, and cache-hit attempt is appended to `usage/api-usage.jsonl`; plans return its cumulative `usage_ledger`, so retries cannot disappear from cost accounting.
