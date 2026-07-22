from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from .config import ProfileConfig
from .util import utc_now


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def resource_preflight(
    config: ProfileConfig,
    *,
    database: Path | None,
    estimate: Mapping[str, Any],
    operation: str,
) -> dict[str, Any]:
    """Estimate transient peak storage before creating a build candidate."""
    config.root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(config.root)
    active_database_bytes = database.stat().st_size if database and database.is_file() else 0
    semantic_bytes = _directory_bytes(config.root / "semantic/chroma")
    storage = dict(estimate.get("storage") or {})
    components = dict(storage.get("components_expected_bytes") or {})
    projected_database = int(components.get("sqlite_active_build") or 0)
    projected_semantic = int(components.get("semantic_index") or 0)
    source = dict(estimate.get("source") or {})
    shared_growth_bytes = int(source.get("new_or_reprocessed_bytes") or 0) + int(
        storage.get("inline_artifact_decoded_upper_bytes") or 0
    )
    if active_database_bytes:
        candidate_bytes = active_database_bytes + semantic_bytes
    else:
        candidate_bytes = projected_database + projected_semantic
    headroom_bytes = max(
        int(config.runtime_min_free_bytes),
        int(candidate_bytes * config.runtime_peak_headroom_ratio),
    )
    required_free_bytes = candidate_bytes + shared_growth_bytes + headroom_bytes
    return {
        "operation": operation,
        "filesystem": str(config.root),
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "active_database_bytes": active_database_bytes,
        "active_semantic_bytes": semantic_bytes,
        "candidate_bytes": candidate_bytes,
        "shared_growth_bytes": shared_growth_bytes,
        "headroom_bytes": headroom_bytes,
        "required_free_bytes": required_free_bytes,
        "passed": int(usage.free) >= required_free_bytes,
        "note": (
            "Peak estimate covers the candidate SQLite/semantic copy and configured safety "
            "headroom; shared snapshot and artifact CAS growth remains content-addressed."
        ),
    }


def require_resource_preflight(report: Mapping[str, Any]) -> None:
    if report.get("passed"):
        return
    raise RuntimeError(
        "Insufficient free disk space for the build candidate: "
        f"{int(report.get('free_bytes') or 0)} bytes free, "
        f"{int(report.get('required_free_bytes') or 0)} bytes required. "
        "Run `codex-history plan --json`, remove obsolete builds, or move the profile."
    )


def prune_profile_builds(config: ProfileConfig, *, active_build_id: str) -> dict[str, Any]:
    """Keep the active build and the configured number of newest complete builds."""
    candidates: list[tuple[int, Path]] = []
    for path in config.builds_dir.iterdir() if config.builds_dir.is_dir() else ():
        if not path.is_dir() or path.name == active_build_id:
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    keep_inactive = max(0, config.runtime_retained_builds - 1)
    removed: list[str] = []
    reclaimed = 0
    for _mtime, path in candidates[keep_inactive:]:
        reclaimed += _directory_bytes(path)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path.name)
    return {
        "retained_builds": config.runtime_retained_builds,
        "removed_build_ids": removed,
        "removed_build_count": len(removed),
        "reclaimed_bytes": reclaimed,
    }


def append_usage_event(config: ProfileConfig, event: Mapping[str, Any]) -> None:
    """Append one provider attempt to a durable JSONL invoice ledger."""
    config.usage_dir.mkdir(parents=True, exist_ok=True)
    payload = {"recorded_at": utc_now(), "profile": config.name, **dict(event)}
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path = config.usage_dir / "api-usage.jsonl"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def usage_summary(config: ProfileConfig) -> dict[str, Any]:
    path = config.usage_dir / "api-usage.jsonl"
    totals: dict[str, Any] = {
        "events": 0,
        "successful_attempts": 0,
        "failed_attempts": 0,
        "cache_hits": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "cost_cny": 0.0,
    }
    if not path.is_file():
        return {"path": str(path), **totals}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            totals["events"] += 1
            status = str(row.get("status") or "")
            if status == "complete":
                totals["successful_attempts"] += 1
            elif status == "failed":
                totals["failed_attempts"] += 1
            if row.get("cache_hit"):
                totals["cache_hits"] += 1
            totals["input_tokens"] += int(row.get("input_tokens") or 0)
            totals["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
            totals["output_tokens"] += int(row.get("output_tokens") or 0)
            totals["cost_cny"] += float(row.get("cost_cny") or 0.0)
    totals["cost_cny"] = round(float(totals["cost_cny"]), 6)
    return {"path": str(path), **totals}
