from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .util import atomic_write_text, read_json


CONFIG_SCHEMA_VERSION = 1


def default_data_home(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform = platform or sys.platform
    env = env or os.environ
    home = home or Path.home()
    override = env.get("CODEX_HISTORY_HOME")
    if override:
        return Path(override).expanduser()
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        return Path(base) / "codex-history" if base else home / "AppData/Local/codex-history"
    if platform == "darwin":
        return home / "Library/Application Support/codex-history"
    xdg = env.get("XDG_DATA_HOME")
    return Path(xdg) / "codex-history" if xdg else home / ".local/share/codex-history"


def default_codex_homes(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    platform = platform or sys.platform
    env = env or os.environ
    home = home or Path.home()
    candidates: list[Path] = []
    explicit = env.get("CODEX_HOME")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(home / ".codex")
    if platform.startswith("linux") and env.get("WSL_DISTRO_NAME"):
        users = Path("/mnt/c/Users")
        windows_user = env.get("WIN_USERNAME") or env.get("USERNAME")
        if windows_user:
            candidates.append(users / windows_user / ".codex")
        elif users.is_dir():
            for child in sorted(users.iterdir()):
                if child.is_dir() and child.name.lower() not in {"all users", "default", "public"}:
                    candidates.append(child / ".codex")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


@dataclass(frozen=True)
class ProfileConfig:
    home: Path
    name: str
    source_roots: tuple[Path, ...]
    include_archived: bool = True
    snapshot_chunk_bytes: int = 4 * 1024 * 1024
    summary_mode: str = "auto"
    summary_provider: str = "dashscope"
    summary_model: str = "deepseek-v4-flash"
    summary_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    summary_api_key_env: str = "DASHSCOPE_API_KEY"
    summary_env_file: str = ""
    summary_thinking_enabled: bool | None = False
    summary_input_price_cny: float = 1.0
    summary_cached_input_price_cny: float = 0.2
    summary_output_price_cny: float = 2.0
    writer_provider: str = "dashscope"
    writer_model: str = "qwen3.7-max"
    writer_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    writer_api_key_env: str = "DASHSCOPE_API_KEY"
    writer_env_file: str = ""
    writer_thinking_enabled: bool | None = False
    writer_input_price_cny: float = 6.0
    writer_cached_input_price_cny: float = 1.2
    writer_output_price_cny: float = 18.0
    estimate_bytes_per_token: float = 3.0
    estimate_summary_input_ratio: float = 0.30
    estimate_summary_output_ratio: float = 0.08
    estimate_embedding_input_ratio: float = 0.15
    estimate_cached_input_ratio: float = 0.0
    estimate_sqlite_to_source_ratio: float = 0.18
    estimate_artifact_to_source_ratio: float = 0.08
    estimate_semantic_to_source_ratio: float = 0.05
    embedding_enabled: bool = False
    embedding_provider: str = "dashscope"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 512
    embedding_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key_env: str = "DASHSCOPE_API_KEY"
    embedding_env_file: str = ""
    embedding_input_price_cny: float = 0.5
    artifact_capture_paths: bool = False
    artifact_max_file_bytes: int = 100 * 1024 * 1024
    runtime_python: str = ""
    path_mappings: tuple[tuple[str, str], ...] = ()

    @property
    def root(self) -> Path:
        return self.home / "profiles" / self.name

    @property
    def builds_dir(self) -> Path:
        return self.root / "builds"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def cas_dir(self) -> Path:
        return self.root / "cas"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def active_path(self) -> Path:
        return self.root / "active.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".update.lock"


def config_path(home: Path) -> Path:
    return home / "config.toml"


def catalog_path(home: Path) -> Path:
    return home / "catalog.json"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_initial_config(
    home: Path,
    *,
    profile: str,
    source_roots: Sequence[Path],
    force: bool = False,
) -> Path:
    path = config_path(home)
    if path.exists() and not force:
        raise FileExistsError(f"Configuration already exists: {path}")
    sources = ", ".join(_toml_string(str(item.expanduser().resolve())) for item in source_roots)
    text = f'''schema_version = {CONFIG_SCHEMA_VERSION}
active_profile = {_toml_string(profile)}

[profiles.{profile}]
source_roots = [{sources}]
include_archived = true
snapshot_chunk_bytes = 4194304

[profiles.{profile}.summarization]
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

[profiles.{profile}.summarization.writer]
provider = "dashscope"
model = "qwen3.7-max"
endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
env_file = ""
thinking_enabled = false
input_price_cny_per_million = 6.0
cached_input_price_cny_per_million = 1.2
output_price_cny_per_million = 18.0

[profiles.{profile}.estimation]
bytes_per_token = 3.0
summary_input_ratio = 0.30
summary_output_ratio = 0.08
embedding_input_ratio = 0.15
cached_input_ratio = 0.0
sqlite_to_source_ratio = 0.18
artifact_to_source_ratio = 0.08
semantic_to_source_ratio = 0.05

[profiles.{profile}.embedding]
enabled = false
provider = "dashscope"
model = "text-embedding-v4"
dimensions = 512
endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
env_file = ""
input_price_cny_per_million = 0.5

[profiles.{profile}.artifacts]
capture_existing_paths = false
max_file_bytes = 104857600

[profiles.{profile}.runtime]
python = ""
'''
    atomic_write_text(path, text)
    return path


def _profile_from_item(home: Path, profile: str, item: Mapping[str, Any]) -> ProfileConfig:
    summary = item.get("summarization", {})
    writer = summary.get("writer", {})
    estimation = item.get("estimation", {})
    embedding = item.get("embedding", {})
    artifacts = item.get("artifacts", {})
    runtime = item.get("runtime", {})
    mappings = item.get("path_mappings", [])
    roots = tuple(Path(value).expanduser() for value in item.get("source_roots", []))
    parsed_mappings: list[tuple[str, str]] = []
    for mapping in mappings:
        if isinstance(mapping, dict):
            original = str(mapping.get("original_prefix") or "")
            local = str(mapping.get("local_prefix") or "")
        elif isinstance(mapping, (list, tuple)) and len(mapping) == 2:
            original, local = str(mapping[0]), str(mapping[1])
        else:
            continue
        if original and local:
            parsed_mappings.append((original, local))
    config = ProfileConfig(
        home=home,
        name=profile,
        source_roots=roots,
        include_archived=bool(item.get("include_archived", True)),
        snapshot_chunk_bytes=int(item.get("snapshot_chunk_bytes", 4 * 1024 * 1024)),
        summary_mode=str(summary.get("mode", "auto")),
        summary_provider=str(summary.get("provider", "dashscope")),
        summary_model=str(summary.get("model", "deepseek-v4-flash")),
        summary_endpoint=str(
            summary.get("endpoint", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        ),
        summary_api_key_env=str(summary.get("api_key_env", "DASHSCOPE_API_KEY")),
        summary_env_file=str(summary.get("env_file", "")),
        summary_thinking_enabled=(
            bool(summary["thinking_enabled"]) if "thinking_enabled" in summary else None
        ),
        summary_input_price_cny=float(summary.get("input_price_cny_per_million", 1.0)),
        summary_cached_input_price_cny=float(
            summary.get("cached_input_price_cny_per_million", 0.2)
        ),
        summary_output_price_cny=float(summary.get("output_price_cny_per_million", 2.0)),
        writer_provider=str(writer.get("provider", "dashscope")),
        writer_model=str(writer.get("model", "qwen3.7-max")),
        writer_endpoint=str(
            writer.get("endpoint", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        ),
        writer_api_key_env=str(writer.get("api_key_env", "DASHSCOPE_API_KEY")),
        writer_env_file=str(writer.get("env_file", "")),
        writer_thinking_enabled=(
            bool(writer["thinking_enabled"]) if "thinking_enabled" in writer else False
        ),
        writer_input_price_cny=float(writer.get("input_price_cny_per_million", 6.0)),
        writer_cached_input_price_cny=float(
            writer.get("cached_input_price_cny_per_million", 1.2)
        ),
        writer_output_price_cny=float(writer.get("output_price_cny_per_million", 18.0)),
        estimate_bytes_per_token=float(estimation.get("bytes_per_token", 3.0)),
        estimate_summary_input_ratio=float(estimation.get("summary_input_ratio", 0.30)),
        estimate_summary_output_ratio=float(estimation.get("summary_output_ratio", 0.08)),
        estimate_embedding_input_ratio=float(
            estimation.get("embedding_input_ratio", 0.15)
        ),
        estimate_cached_input_ratio=float(estimation.get("cached_input_ratio", 0.0)),
        estimate_sqlite_to_source_ratio=float(
            estimation.get("sqlite_to_source_ratio", 0.18)
        ),
        estimate_artifact_to_source_ratio=float(
            estimation.get("artifact_to_source_ratio", 0.08)
        ),
        estimate_semantic_to_source_ratio=float(
            estimation.get("semantic_to_source_ratio", 0.05)
        ),
        embedding_enabled=bool(embedding.get("enabled", False)),
        embedding_provider=str(embedding.get("provider", "dashscope")),
        embedding_model=str(embedding.get("model", "text-embedding-v4")),
        embedding_dimensions=int(embedding.get("dimensions", 512)),
        embedding_endpoint=str(
            embedding.get("endpoint", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        ),
        embedding_api_key_env=str(embedding.get("api_key_env", "DASHSCOPE_API_KEY")),
        embedding_env_file=str(embedding.get("env_file", "")),
        embedding_input_price_cny=float(embedding.get("input_price_cny_per_million", 0.5)),
        artifact_capture_paths=bool(artifacts.get("capture_existing_paths", False)),
        artifact_max_file_bytes=int(artifacts.get("max_file_bytes", 100 * 1024 * 1024)),
        runtime_python=str(runtime.get("python", "")),
        path_mappings=tuple(parsed_mappings),
    )
    _validate_profile(config)
    return config


def profile_names(home: Path | None = None) -> list[str]:
    home = (home or default_data_home()).expanduser().resolve()
    names: set[str] = set()
    path = config_path(home)
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        names.update(str(name) for name in raw.get("profiles", {}))
    catalog = read_json(catalog_path(home), {}) or {}
    names.update(
        str(name)
        for name, item in catalog.get("profiles", {}).items()
        if item.get("enabled", True)
    )
    return sorted(names)


def load_config(home: Path | None = None, profile: str | None = None) -> ProfileConfig:
    home = (home or default_data_home()).expanduser().resolve()
    path = config_path(home)
    raw: dict[str, Any] = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise ValueError(f"Unsupported config schema: {raw.get('schema_version')}")
    catalog = read_json(catalog_path(home), {}) or {}
    if not raw and not catalog:
        raise FileNotFoundError(f"Codex History is not initialized: {path}")
    profile = profile or raw.get("active_profile")
    if not profile:
        enabled = [
            name
            for name, item in catalog.get("profiles", {}).items()
            if item.get("enabled", True)
        ]
        profile = sorted(enabled)[0] if enabled else "default"
    profiles = raw.get("profiles", {})
    if profile in profiles:
        return _profile_from_item(home, profile, profiles[profile])
    catalog_item = catalog.get("profiles", {}).get(profile)
    if not catalog_item or not catalog_item.get("enabled", True):
        raise KeyError(f"Unknown profile: {profile}")
    return _profile_from_item(home, profile, catalog_item.get("config", {}))


def _validate_profile(config: ProfileConfig) -> None:
    errors: list[str] = []
    if config.summary_mode.strip().lower() not in {
        "auto",
        "extractive",
        "openai-compatible",
    }:
        errors.append("summarization.mode must be auto, extractive, or openai-compatible")
    for label, value in (
        ("summarization.input_price_cny_per_million", config.summary_input_price_cny),
        (
            "summarization.cached_input_price_cny_per_million",
            config.summary_cached_input_price_cny,
        ),
        ("summarization.output_price_cny_per_million", config.summary_output_price_cny),
        ("summarization.writer.input_price_cny_per_million", config.writer_input_price_cny),
        (
            "summarization.writer.cached_input_price_cny_per_million",
            config.writer_cached_input_price_cny,
        ),
        ("summarization.writer.output_price_cny_per_million", config.writer_output_price_cny),
        ("embedding.input_price_cny_per_million", config.embedding_input_price_cny),
        ("estimation.summary_input_ratio", config.estimate_summary_input_ratio),
        ("estimation.summary_output_ratio", config.estimate_summary_output_ratio),
        ("estimation.embedding_input_ratio", config.estimate_embedding_input_ratio),
        ("estimation.sqlite_to_source_ratio", config.estimate_sqlite_to_source_ratio),
        ("estimation.artifact_to_source_ratio", config.estimate_artifact_to_source_ratio),
        ("estimation.semantic_to_source_ratio", config.estimate_semantic_to_source_ratio),
    ):
        if value < 0:
            errors.append(f"{label} must be non-negative")
    if config.estimate_bytes_per_token <= 0:
        errors.append("estimation.bytes_per_token must be greater than zero")
    if not 0 <= config.estimate_cached_input_ratio <= 1:
        errors.append("estimation.cached_input_ratio must be between 0 and 1")
    if config.snapshot_chunk_bytes <= 0:
        errors.append("snapshot_chunk_bytes must be greater than zero")
    if config.embedding_dimensions <= 0:
        errors.append("embedding.dimensions must be greater than zero")
    if config.artifact_max_file_bytes <= 0:
        errors.append("artifacts.max_file_bytes must be greater than zero")
    if errors:
        raise ValueError("Invalid Codex History profile: " + "; ".join(errors))


def configured_secret(name: str, env_file: str = "") -> str:
    """Read a configured secret without ever returning it in reports."""
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value or not env_file:
        return value
    path = Path(env_file).expanduser()
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip().strip("'\"")
    return ""


def resolve_summarization(config: ProfileConfig) -> dict[str, object]:
    requested = config.summary_mode.strip().lower()
    if requested not in {"auto", "extractive", "openai-compatible"}:
        raise ValueError(f"Unsupported summarization mode: {config.summary_mode}")
    missing = [
        label
        for label, value in (
            ("endpoint", config.summary_endpoint),
            ("model", config.summary_model),
            ("api_key_env", config.summary_api_key_env),
        )
        if not value
    ]
    key_available = bool(
        configured_secret(config.summary_api_key_env, config.summary_env_file)
    )
    if not missing and not key_available:
        missing.append(f"${config.summary_api_key_env}")
    writer_missing = [
        label
        for label, value in (
            ("writer.endpoint", config.writer_endpoint),
            ("writer.model", config.writer_model),
            ("writer.api_key_env", config.writer_api_key_env),
        )
        if not value
    ]
    writer_key_available = bool(
        configured_secret(config.writer_api_key_env, config.writer_env_file)
    )
    if not writer_missing and not writer_key_available:
        writer_missing.append(f"${config.writer_api_key_env}")
    missing.extend(writer_missing)

    if requested == "extractive":
        effective = "extractive"
        fallback_reason = "extractive mode was explicitly selected"
    elif not missing:
        effective = "openai-compatible"
        fallback_reason = ""
    elif requested == "auto":
        effective = "extractive"
        fallback_reason = "missing " + ", ".join(missing)
    else:
        effective = "unavailable"
        fallback_reason = "missing " + ", ".join(missing)
    return {
        "requested_mode": requested,
        "effective_mode": effective,
        "provider": config.summary_provider,
        "model": config.summary_model,
        "writer_provider": config.writer_provider,
        "writer_model": config.writer_model,
        "api_key_env": config.summary_api_key_env,
        "api_key_available": key_available,
        "writer_api_key_env": config.writer_api_key_env,
        "writer_api_key_available": writer_key_available,
        "fallback": requested == "auto" and effective == "extractive",
        "fallback_reason": fallback_reason,
    }


def ensure_profile_dirs(config: ProfileConfig) -> None:
    for path in (
        config.root,
        config.builds_dir,
        config.snapshots_dir,
        config.cas_dir,
        config.runs_dir,
        config.cache_dir,
        config.reports_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
