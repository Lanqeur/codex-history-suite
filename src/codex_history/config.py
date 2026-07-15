from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .util import atomic_write_text


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
                if child.is_dir() and not child.name.lower() in {"all users", "default", "public"}:
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
    summary_mode: str = "extractive"
    summary_provider: str = ""
    summary_model: str = ""
    summary_endpoint: str = ""
    summary_api_key_env: str = ""
    summary_env_file: str = ""
    summary_input_price_cny: float = 0.0
    summary_output_price_cny: float = 0.0
    embedding_enabled: bool = False
    embedding_provider: str = "dashscope"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 512
    embedding_endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key_env: str = "DASHSCOPE_API_KEY"
    embedding_env_file: str = ""
    embedding_input_price_cny: float = 0.0
    artifact_capture_paths: bool = False
    artifact_max_file_bytes: int = 100 * 1024 * 1024
    runtime_python: str = ""

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
mode = "extractive"
provider = ""
model = ""
endpoint = ""
api_key_env = ""
env_file = ""
input_price_cny_per_million = 0.0
output_price_cny_per_million = 0.0

[profiles.{profile}.embedding]
enabled = false
provider = "dashscope"
model = "text-embedding-v4"
dimensions = 512
endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
env_file = ""
input_price_cny_per_million = 0.0

[profiles.{profile}.artifacts]
capture_existing_paths = false
max_file_bytes = 104857600

[profiles.{profile}.runtime]
python = ""
'''
    atomic_write_text(path, text)
    return path


def load_config(home: Path | None = None, profile: str | None = None) -> ProfileConfig:
    home = (home or default_data_home()).expanduser().resolve()
    path = config_path(home)
    if not path.exists():
        raise FileNotFoundError(f"Codex History is not initialized: {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"Unsupported config schema: {raw.get('schema_version')}")
    profile = profile or raw.get("active_profile", "default")
    profiles = raw.get("profiles", {})
    if profile not in profiles:
        raise KeyError(f"Unknown profile: {profile}")
    item = profiles[profile]
    summary = item.get("summarization", {})
    embedding = item.get("embedding", {})
    artifacts = item.get("artifacts", {})
    runtime = item.get("runtime", {})
    roots = tuple(Path(value).expanduser() for value in item.get("source_roots", []))
    return ProfileConfig(
        home=home,
        name=profile,
        source_roots=roots,
        include_archived=bool(item.get("include_archived", True)),
        snapshot_chunk_bytes=int(item.get("snapshot_chunk_bytes", 4 * 1024 * 1024)),
        summary_mode=str(summary.get("mode", "extractive")),
        summary_provider=str(summary.get("provider", "")),
        summary_model=str(summary.get("model", "")),
        summary_endpoint=str(summary.get("endpoint", "")),
        summary_api_key_env=str(summary.get("api_key_env", "")),
        summary_env_file=str(summary.get("env_file", "")),
        summary_input_price_cny=float(summary.get("input_price_cny_per_million", 0.0)),
        summary_output_price_cny=float(summary.get("output_price_cny_per_million", 0.0)),
        embedding_enabled=bool(embedding.get("enabled", False)),
        embedding_provider=str(embedding.get("provider", "dashscope")),
        embedding_model=str(embedding.get("model", "text-embedding-v4")),
        embedding_dimensions=int(embedding.get("dimensions", 512)),
        embedding_endpoint=str(
            embedding.get("endpoint", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        ),
        embedding_api_key_env=str(embedding.get("api_key_env", "DASHSCOPE_API_KEY")),
        embedding_env_file=str(embedding.get("env_file", "")),
        embedding_input_price_cny=float(embedding.get("input_price_cny_per_million", 0.0)),
        artifact_capture_paths=bool(artifacts.get("capture_existing_paths", False)),
        artifact_max_file_bytes=int(artifacts.get("max_file_bytes", 100 * 1024 * 1024)),
        runtime_python=str(runtime.get("python", "")),
    )


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
