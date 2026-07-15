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

New profiles use `summarization.mode = "auto"`. Auto mode calls the configured OpenAI-compatible model when its endpoint, model, API-key environment variable, and key are available. If any of those are absent, it reports the missing item and falls back to deterministic `extractive` summaries. Use `"extractive"` to force offline operation or `"openai-compatible"` to require a model and fail on incomplete configuration.

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
```

Prices are user-owned planning inputs, not a live billing feed. Verify the provider's current region, deployment, cache, tier, and batch pricing before a paid build. The DeepSeek V4 Flash defaults above reflect the recommended economical mainland-China DashScope preset at the time of release. `thinking_enabled = false` avoids paying for reasoning tokens on this structured extraction workload.

The model must return cited Record IDs; unsupported claims remain unlinked. Model/provider failures do not trigger a silent fallback. Only missing configuration in `auto` mode does.

Model responses are cached by stage version, model, prompt hash, and input hash. A clean full rebuild reuses identical cached outputs.

## Planning Estimates

`plan` and `update --dry-run` calculate both the effective build and the model-enabled alternative. This means a new user can estimate model cost before setting an API key. The report separates expected and upper input/output tokens, provider cached input, embedding tokens, expected cost, conservative no-cache cost, and managed disk usage.

```toml
[profiles.default.estimation]
bytes_per_token = 3.0
summary_input_ratio = 0.30
summary_output_ratio = 0.08
cached_input_ratio = 0.0
sqlite_to_source_ratio = 0.18
artifact_to_source_ratio = 0.08
semantic_to_source_ratio = 0.05
```

The first plan uses these transparent defaults. Once an active database exists, the estimator calibrates summary-density, SQLite, artifact, and semantic ratios from that profile. `cached_input_ratio` is an expected provider-prefix cache ratio between 0 and 1; keep it at zero for a conservative first build. Exact Codex History model-response cache hits cost zero and are reported separately from provider cache hits.

The storage range covers current-state content-addressed transcript snapshots, the active SQLite build, artifact CAS, optional Chroma data, and model responses. It excludes the original transcripts and old retained builds. Capturing existing absolute-path files can exceed the range because those files are not bounded by transcript size.

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

Always run `plan` or `update --dry-run`. Paid commands require a user-selected `--max-cost-cny`. The engine checks the conservative upper estimate before the run and reserves budget before each uncached model batch. Exact response-cache hits cost zero; provider cached-input tokens use `cached_input_price_cny_per_million`.
