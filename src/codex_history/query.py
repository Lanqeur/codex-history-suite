#!/usr/bin/env python3
"""Query a Codex History Suite database with lexical, semantic, and temporal retrieval."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB = Path(
    os.environ.get(
        "CODEX_HISTORY_DB",
        "codex_history.sqlite3",
    )
)
SEMANTIC_COLLECTION = "codex_history"
SEMANTIC_MODEL = os.environ.get("CODEX_HISTORY_EMBEDDING_MODEL", "text-embedding-v4")
SEMANTIC_DIMENSIONS = int(os.environ.get("CODEX_HISTORY_EMBEDDING_DIMENSIONS", "512"))
DEFAULT_CHROMA = Path(
    os.environ.get(
        "CODEX_HISTORY_CHROMA",
        "chroma",
    )
)
SNAPSHOT_ROOT = Path(os.environ.get("CODEX_HISTORY_SNAPSHOTS", "snapshots"))
ASSET_TYPES = ("decisions", "unresolved", "failures", "capabilities", "preferences")
HIGH_TIERS = ("asset", "overview", "ledger")
ALL_TIERS = (*HIGH_TIERS, "fact_block", "core")
TIER_ORDER = {tier: index for index, tier in enumerate(ALL_TIERS)}
STATUS_STRENGTH = {
    "verified": 6,
    "executed": 5,
    "mixed": 4,
    "planned": 3,
    "blocked": 2,
    "failed": 2,
    "uncertain": 1,
}
QUERY_TOKEN_RE = re.compile(r'-?"[^"]+"|-?\S+')
_QUERY_VECTOR_CACHE: dict[str, list[float]] = {}
_SEMANTIC_COLLECTION_CACHE: dict[str, Any] = {}
_SEMANTIC_WARNING_SHOWN = False


def _environment_path_mappings() -> list[dict[str, str]]:
    try:
        value = json.loads(os.environ.get("CODEX_HISTORY_PATH_MAPPINGS", "[]"))
    except json.JSONDecodeError:
        return []
    return [
        {
            "original_prefix": str(item.get("original_prefix") or ""),
            "local_prefix": str(item.get("local_prefix") or ""),
        }
        for item in value
        if isinstance(item, dict)
        and item.get("original_prefix")
        and item.get("local_prefix")
    ]


PATH_MAPPINGS = _environment_path_mappings()


def _environment_artifact_roots() -> list[Path]:
    try:
        value = json.loads(os.environ.get("CODEX_HISTORY_ARTIFACT_ROOTS", "[]"))
    except json.JSONDecodeError:
        return []
    return [Path(str(item)).expanduser() for item in value if str(item)]


ARTIFACT_ROOTS = _environment_artifact_roots()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def parse_json(value: str) -> Any:
    return json.loads(value) if value else None


def remap_path(value: str) -> tuple[str, str | None]:
    if not value:
        return value, None
    normalized = value.replace("\\", "/")
    best: tuple[int, str, str] | None = None
    for mapping in PATH_MAPPINGS:
        original = mapping["original_prefix"]
        local = mapping["local_prefix"]
        candidate = original.replace("\\", "/")
        case_insensitive = bool(re.match(r"^[A-Za-z]:/", candidate))
        source_value = normalized.casefold() if case_insensitive else normalized
        source_prefix = candidate.casefold() if case_insensitive else candidate
        if source_value == source_prefix or source_value.startswith(source_prefix.rstrip("/") + "/"):
            match = (len(candidate), candidate, local)
            if best is None or match[0] >= best[0]:
                best = match
    if best is None:
        return value, None
    suffix = normalized[len(best[1]) :].lstrip("/")
    mapped = str(Path(best[2]) / Path(suffix)) if suffix else best[2]
    return mapped, value


def remap_path_fields(value: Any) -> Any:
    path_keys = {
        "path",
        "source_path",
        "source_open_path",
        "queue_path",
        "manifest_path",
        "overview_path",
        "ledger_path",
    }
    if isinstance(value, list):
        return [remap_path_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, item in list(result.items()):
        if key in path_keys and isinstance(item, str):
            mapped, original = remap_path(item)
            result[key] = mapped
            if original is not None:
                result[f"{key}_original"] = original
        elif isinstance(item, (dict, list)):
            result[key] = remap_path_fields(item)
    return result


def resolve_artifact_path(cas_relative_path: str) -> str | None:
    path = Path(cas_relative_path.replace("\\", "/"))
    parts = path.parts[1:] if path.parts and path.parts[0] == "cas" else path.parts
    if not parts or path.is_absolute() or ".." in parts:
        return None
    relative = Path(*parts)
    for root in ARTIFACT_ROOTS:
        candidate = root / relative
        if candidate.is_file():
            return str(candidate)
    return None


def connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"Codex History database not found: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-131072")
    connection.execute("PRAGMA mmap_size=268435456")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key,value FROM metadata")
    }


def query_terms(query: str) -> list[str]:
    terms = []
    for raw in QUERY_TOKEN_RE.findall(query):
        term = raw.lstrip("-").strip("\"'，,。；;：:！？!?()[]{}")
        if term:
            terms.append(term)
    return list(dict.fromkeys(terms))


def fts_expression(query: str) -> str | None:
    terms = [term for term in query_terms(query) if len(term) >= 3]
    if not terms:
        return None
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def parse_query(query: str, exclusions: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    positive: list[str] = []
    negative: list[str] = [value.strip() for value in exclusions if value.strip()]
    for raw in QUERY_TOKEN_RE.findall(query):
        is_negative = raw.startswith("-")
        value = raw[1:] if is_negative else raw
        value = value.strip().strip("\"").strip()
        if not value:
            continue
        (negative if is_negative else positive).append(value)
    return list(dict.fromkeys(positive)), list(dict.fromkeys(negative))


def alias_groups(connection: sqlite3.Connection, terms: list[str]) -> list[list[str]]:
    rows = connection.execute("SELECT alias,canonical FROM aliases").fetchall()
    graph: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    for row in rows:
        alias, canonical = str(row["alias"]), str(row["canonical"])
        akey, ckey = alias.casefold(), canonical.casefold()
        graph[akey].update((akey, ckey))
        graph[ckey].update((akey, ckey))
        display[akey], display[ckey] = alias, canonical
    result = []
    for term in terms:
        key = term.casefold()
        variants = [term]
        if key in graph:
            variants.extend(display[value] for value in sorted(graph[key]))
        result.append(list(dict.fromkeys(variants)))
    return result


def parse_time(value: str, *, end_of_day: bool = False) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        suffix = "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"
        return value + suffix
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def recent_since(value: str) -> str:
    match = re.fullmatch(r"(\d+)([hdwmy])", value.strip().lower())
    if not match:
        raise SystemExit("--recent must look like 12h, 30d, 8w, 6m, or 1y")
    count, unit = int(match.group(1)), match.group(2)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}.get(unit)
    delta = timedelta(hours=count) if unit == "h" else timedelta(days=count * days)
    return (datetime.now(timezone.utc) - delta).isoformat()


def timestamp_rank(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def field_matches(
    row: dict[str, Any], groups: list[list[str]], negative: list[str]
) -> tuple[list[str], list[str], bool]:
    body = "\n".join(
        str(row.get(key) or "")
        for key in ("text", "theme", "category", "asset_type", "tier")
    ).casefold()
    title = str(row.get("scope_title") or "").casefold()
    if any(term.casefold() in body or term.casefold() in title for term in negative):
        return [], [], True
    body_matches: list[str] = []
    title_matches: list[str] = []
    for group in groups:
        if any(variant.casefold() in body for variant in group):
            body_matches.append(group[0])
        elif any(variant.casefold() in title for variant in group):
            title_matches.append(group[0])
    return body_matches, title_matches, False


def fts_group_expression(groups: list[list[str]], mode: str, *, trigram: bool) -> str | None:
    expressions = []
    for group in groups:
        variants = [value for value in group if not trigram or len(value) >= 3]
        if not variants:
            continue
        quoted = [f'"{value.replace(chr(34), chr(34) * 2)}"' for value in variants]
        expressions.append("(" + " OR ".join(quoted) + ")")
    if not expressions:
        return None
    return (" AND " if mode == "all" else " OR ").join(expressions)


def decode_knowledge(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["evidence_refs"] = parse_json(value.pop("evidence_refs_json")) or []
    value["metadata"] = parse_json(value.pop("metadata_json")) or {}
    value.pop("rank", None)
    return remap_path_fields(value)


def sql_filters(
    *,
    scopes: list[str],
    exclude_scopes: list[str],
    exclude_record_ids: list[str],
    tiers: list[str],
    asset_type: str,
    status_group: str,
    since: str,
    until: str,
    time_field: str,
    time_match: str,
    as_of: str,
    exclude_scope_range: bool,
    include_history: bool,
) -> tuple[list[str], list[Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if as_of:
        point = parse_time(as_of, end_of_day=re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of.strip()) is not None)
        filters.extend(
            [
                "COALESCE(k.asserted_at,k.indexed_at,k.occurred_end_at,k.occurred_start_at)<=?",
                "(k.valid_from IS NULL OR k.valid_from<=?)",
                "(k.valid_to IS NULL OR k.valid_to>?)",
            ]
        )
        params.extend([point, point, point])
    elif not include_history:
        filters.append("k.valid_to IS NULL")
    if scopes:
        filters.append(f"k.scope_id IN ({','.join('?' for _ in scopes)})")
        params.extend(scopes)
    if exclude_scopes:
        filters.append(f"k.scope_id NOT IN ({','.join('?' for _ in exclude_scopes)})")
        params.extend(exclude_scopes)
    if exclude_record_ids:
        filters.append(
            f"k.record_id NOT IN ({','.join('?' for _ in exclude_record_ids)})"
        )
        params.extend(exclude_record_ids)
    if tiers:
        filters.append(f"k.tier IN ({','.join('?' for _ in tiers)})")
        params.extend(tiers)
    if asset_type:
        filters.append("k.asset_type=?")
        params.append(asset_type)
    if status_group:
        filters.append("k.status_group=?")
        params.append(status_group)
    if exclude_scope_range:
        filters.append("COALESCE(k.temporal_confidence,'')!='scope_range'")

    time_columns = {
        "occurred": ("k.occurred_start_at", "k.occurred_end_at"),
        "asserted": ("k.asserted_at", "k.asserted_at"),
        "indexed": ("k.indexed_at", "k.indexed_at"),
    }
    start_column, end_column = time_columns[time_field]
    if since:
        operator_column = start_column if time_match == "contained" else f"COALESCE({end_column},{start_column})"
        filters.append(f"{operator_column}>=?")
        params.append(since)
    if until:
        operator_column = end_column if time_match == "contained" else f"COALESCE({start_column},{end_column})"
        filters.append(f"{operator_column}<=?")
        params.append(until)
    return filters, params


def semantic_row_scores(
    connection: sqlite3.Connection,
    query: str,
    filters: list[str],
    params: list[Any],
    limit: int,
    *,
    required: bool,
) -> tuple[dict[int, float], str | None, list[str]]:
    global _SEMANTIC_WARNING_SHOWN
    try:
        from .semantic import EmbeddingSettings, chroma_collection, query_embedding

        expansion_rows = connection.execute(
            "SELECT trigger_text,expansion_text FROM semantic_query_expansions "
            "ORDER BY length(trigger_text) DESC,trigger_text"
        ).fetchall()
        hints = [
            str(row["expansion_text"])
            for row in expansion_rows
            if str(row["trigger_text"]).casefold() in query.casefold()
        ]
        semantic_query = " ".join([query.strip(), *hints]).strip()
        cache_key = hashlib.sha256(
            "\x1f".join(
                (
                    semantic_query,
                    SEMANTIC_MODEL,
                    str(SEMANTIC_DIMENSIONS),
                    os.environ.get("CODEX_HISTORY_EMBEDDING_ENDPOINT", ""),
                )
            ).encode("utf-8")
        ).hexdigest()
        vector = _QUERY_VECTOR_CACHE.get(cache_key)
        if vector is None:
            vector = query_embedding(semantic_query)
            _QUERY_VECTOR_CACHE[cache_key] = vector
        archive_chroma = Path(connection.execute("PRAGMA database_list").fetchone()[2]).parent / "chroma"
        chroma_path = DEFAULT_CHROMA if DEFAULT_CHROMA.is_dir() else archive_chroma
        cache_path = str(chroma_path.resolve())
        collection = _SEMANTIC_COLLECTION_CACHE.get(cache_path)
        if collection is None:
            last_error: Exception | None = None
            for collection_name in ("codex_history", "codex_history_v21"):
                try:
                    _client, collection = chroma_collection(
                        chroma_path,
                        EmbeddingSettings.from_environment(),
                        create=False,
                        collection_name=collection_name,
                    )
                    break
                except Exception as error:
                    last_error = error
            else:
                assert last_error is not None
                raise last_error
            _SEMANTIC_COLLECTION_CACHE[cache_path] = collection
        result = collection.query(
            query_embeddings=[vector],
            n_results=min(collection.count(), max(limit * 100, 600)),
            include=["distances"],
        )
        document_ids = list(result["ids"][0])
        similarities = {
            document_id: 1.0 - float(distance)
            for document_id, distance in zip(document_ids, result["distances"][0])
        }
        scores: dict[int, float] = {}
        for offset in range(0, len(document_ids), 600):
            chunk = document_ids[offset : offset + 600]
            where = " AND ".join(
                [f"sdr.document_id IN ({','.join('?' for _ in chunk)})", *filters]
            )
            rows = connection.execute(
                "SELECT k.rowid AS rowid,sdr.document_id FROM semantic_document_records sdr "
                "INDEXED BY sqlite_autoindex_semantic_document_records_1 "
                "CROSS JOIN knowledge k ON k.record_id=sdr.record_id WHERE " + where,
                [*chunk, *params],
            )
            for row in rows:
                scores[int(row["rowid"])] = max(
                    scores.get(int(row["rowid"]), -1.0),
                    similarities[str(row["document_id"])],
                )
        return scores, None, hints
    except Exception as error:
        message = f"semantic retrieval unavailable: {error}"
        if required:
            raise SystemExit(message) from error
        if not _SEMANTIC_WARNING_SHOWN:
            print(f"Warning: {message}; using lexical retrieval only.", file=sys.stderr)
            _SEMANTIC_WARNING_SHOWN = True
        return {}, message, []


def lexical_row_candidates(
    connection: sqlite3.Connection,
    groups: list[list[str]],
    query_mode: str,
    filters: list[str],
    params: list[Any],
    tiers: list[str],
    limit: int,
) -> tuple[dict[int, float], dict[int, set[str]]]:
    ranks: dict[int, float] = {}
    engines: dict[int, set[str]] = defaultdict(set)
    candidate_limit = max(limit * 24, 160)
    fts_specs = (
        ("knowledge_body_fts", True, "body-trigram", 8.0),
        ("knowledge_body_terms_fts", False, "body-unicode61", 8.0),
        ("knowledge_title_fts", True, "title-trigram", 1.0),
        ("knowledge_title_terms_fts", False, "title-unicode61", 1.0),
    )
    tier_groups = tiers or [""]
    for selected_tier in tier_groups:
        tier_filters, tier_params = list(filters), list(params)
        if selected_tier:
            tier_filters.append("k.tier=?")
            tier_params.append(selected_tier)
        for table, trigram, engine, weight in fts_specs:
            expression = fts_group_expression(groups, query_mode, trigram=trigram)
            if not expression:
                continue
            where = " AND ".join([f"{table} MATCH ?", *tier_filters])
            try:
                rows = connection.execute(
                    f"SELECT k.rowid AS rowid,bm25({table}) AS rank FROM {table} "
                    f"JOIN knowledge k ON k.rowid={table}.rowid WHERE {where} "
                    "ORDER BY rank LIMIT ?",
                    [expression, *tier_params, candidate_limit],
                )
            except sqlite3.OperationalError:
                continue
            for row in rows:
                rowid = int(row["rowid"])
                weighted = float(row["rank"]) / weight
                ranks[rowid] = min(ranks.get(rowid, weighted), weighted)
                engines[rowid].add(engine)
    return ranks, engines


def search_records(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    scopes: Iterable[str] = (),
    exclude_scopes: Iterable[str] = (),
    exclude_threads: Iterable[str] = (),
    tiers: Iterable[str] = HIGH_TIERS,
    asset_type: str = "",
    status_group: str = "",
    query_mode: str = "any",
    exclusions: Iterable[str] = (),
    since: str = "",
    until: str = "",
    recent: str = "",
    time_field: str = "occurred",
    time_match: str = "overlaps",
    as_of: str = "",
    exclude_scope_range: bool = False,
    include_history: bool = False,
    retrieval: str = "hybrid",
) -> list[dict[str, Any]]:
    scope_values = list(dict.fromkeys(scopes))
    excluded_scope_values = list(dict.fromkeys(exclude_scopes))
    excluded_thread_values = list(dict.fromkeys(exclude_threads))
    tier_values = list(dict.fromkeys(tiers))
    positive, negative = parse_query(query, exclusions)
    groups = alias_groups(connection, positive)
    if recent:
        since = recent_since(recent)
    elif since:
        since = parse_time(since)
    if until:
        until = parse_time(until, end_of_day=True)

    excluded_record_ids: list[str] = []
    if excluded_thread_values:
        excluded_record_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT rx.record_id FROM record_evidence_occurrences rx "
                "JOIN evidence_occurrences ex ON ex.occurrence_id=rx.occurrence_id "
                f"WHERE ex.thread_id IN ({','.join('?' for _ in excluded_thread_values)})",
                excluded_thread_values,
            )
        ]

    if as_of and (since or until or recent):
        raise SystemExit("--as-of cannot be combined with --since, --until, or --recent")
    filters, params = sql_filters(
        scopes=scope_values,
        exclude_scopes=excluded_scope_values,
        exclude_record_ids=excluded_record_ids,
        tiers=tier_values,
        asset_type=asset_type,
        status_group=status_group,
        since=since,
        until=until,
        time_field=time_field,
        time_match=time_match,
        as_of=as_of,
        exclude_scope_range=exclude_scope_range,
        include_history=include_history,
    )

    rank_by_rowid: dict[int, float] = {}
    engines_by_rowid: dict[int, set[str]] = defaultdict(set)
    if retrieval in ("lexical", "hybrid"):
        rank_by_rowid, engines_by_rowid = lexical_row_candidates(
            connection, groups, query_mode, filters, params, tier_values, limit
        )

    semantic_scores: dict[int, float] = {}
    semantic_error: str | None = None
    semantic_hints: list[str] = []
    if retrieval in ("semantic", "hybrid") and query.strip():
        semantic_scores, semantic_error, semantic_hints = semantic_row_scores(
            connection,
            query,
            filters,
            params,
            limit,
            required=retrieval == "semantic",
        )

    candidate_rowids = set(rank_by_rowid) | set(semantic_scores)
    like_terms = list(dict.fromkeys(value for group in groups for value in group))
    if retrieval in ("lexical", "hybrid") and like_terms:
        body_expression = " OR ".join(
            "(k.text LIKE ? OR k.theme LIKE ? OR k.category LIKE ? OR k.asset_type LIKE ?)"
            for _ in like_terms
        )
        title_expression = " OR ".join("k.scope_title LIKE ?" for _ in like_terms)
        for selected_tier in tier_values or [""]:
            tier_filters, tier_params = list(filters), list(params)
            if selected_tier:
                tier_filters.append("k.tier=?")
                tier_params.append(selected_tier)
            where = " AND ".join([*tier_filters, f"(({body_expression}) OR ({title_expression}))"])
            like_params: list[Any] = []
            for term in like_terms:
                like_params.extend([f"%{term}%"] * 4)
            like_params.extend(f"%{term}%" for term in like_terms)
            for row in connection.execute(
                f"SELECT k.rowid FROM knowledge k WHERE {where} LIMIT ?",
                [*tier_params, *like_params, max(limit * 24, 160)],
            ):
                rowid = int(row["rowid"])
                candidate_rowids.add(rowid)
                engines_by_rowid[rowid].add("substring")

    if not candidate_rowids:
        return []
    rows: list[sqlite3.Row] = []
    rowid_values = sorted(candidate_rowids)
    base_where = " AND ".join(filters) if filters else "1=1"
    for offset in range(0, len(rowid_values), 600):
        chunk = rowid_values[offset : offset + 600]
        rows.extend(
            connection.execute(
                f"SELECT k.rowid AS _rowid,k.* FROM knowledge k WHERE {base_where} "
                f"AND k.rowid IN ({','.join('?' for _ in chunk)})",
                [*params, *chunk],
            ).fetchall()
        )

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        decoded = decode_knowledge(row)
        rowid = int(row["_rowid"])
        decoded.pop("_rowid", None)
        body_matched, title_matched, excluded = field_matches(decoded, groups, negative)
        if excluded:
            continue
        matched = [*body_matched, *title_matched]
        semantic_score = semantic_scores.get(rowid)
        if query_mode == "all":
            accepted = len(matched) == len(groups) and bool(body_matched)
        elif retrieval == "semantic":
            accepted = semantic_score is not None
        else:
            accepted = bool(matched) or semantic_score is not None
        if not accepted:
            continue
        key = (decoded["scope_id"], re.sub(r"\s+", " ", decoded["text"]).strip())
        if key in seen:
            continue
        seen.add(key)
        decoded["matched_terms"] = matched
        decoded["body_matched_terms"] = body_matched
        decoded["scope_title_matched_terms"] = title_matched
        decoded["title_only_match"] = bool(title_matched and not body_matched)
        decoded["match_fields"] = [
            *( ["body"] if body_matched else [] ),
            *( ["scope_title"] if title_matched else [] ),
            *( ["semantic"] if semantic_score is not None else [] ),
        ]
        decoded["search_engines"] = sorted(
            engines_by_rowid.get(rowid) or ({"semantic"} if semantic_score is not None else {"substring"})
        )
        decoded["lexical_rank"] = rank_by_rowid.get(rowid, 0.0)
        decoded["semantic_score"] = semantic_score
        decoded["semantic_expansions"] = semantic_hints
        tier_bonus = {
            "asset": 0.35,
            "overview": 0.30,
            "ledger": 0.20,
            "fact_block": 0.10,
            "core": 0.0,
        }.get(decoded["tier"], 0.0)
        decoded["retrieval_score"] = (
            len(body_matched) * 1.4
            + len(title_matched) * 0.15
            + (semantic_score or 0.0)
            + tier_bonus
            + STATUS_STRENGTH.get(decoded["status_group"], 0) * 0.01
        )
        if semantic_error:
            decoded["semantic_warning"] = semantic_error
        results.append(decoded)
    results.sort(
        key=lambda row: (
            row["title_only_match"],
            -row["retrieval_score"],
            -len(row["body_matched_terms"]),
            -len(row["matched_terms"]),
            -(row.get("semantic_score") or -1.0),
            TIER_ORDER.get(row["tier"], 99),
            row["lexical_rank"],
            -STATUS_STRENGTH.get(row["status_group"], 0),
            -int(row["evidence_count"]),
            -timestamp_rank(row.get("occurred_end_at")),
            row["record_id"],
        )
    )
    return results[:limit]


def print_records(rows: list[dict[str, Any]], *, text_limit: int = 700) -> None:
    if not rows:
        print("No matching history records.")
        return
    for index, row in enumerate(rows, 1):
        asset = f":{row['asset_type']}" if row.get("asset_type") else ""
        labels = [f"{row['tier']}{asset}", row["status_group"], row["category"]]
        if row.get("theme"):
            labels.append(row["theme"])
        print(
            f"{index}. [{row['record_id']}] {row['scope_id']} "
            f"{' | '.join(labels)}"
        )
        print(f"   {truncate(row['text'], text_limit)}")
        refs = row.get("evidence_refs") or []
        print(
            f"   Evidence ({len(refs)}): "
            + (", ".join(refs[:12]) if refs else "none")
        )
        if row.get("occurred_start_at") or row.get("occurred_end_at"):
            print(
                f"   Time: {row.get('occurred_start_at') or '?'} -> "
                f"{row.get('occurred_end_at') or '?'} "
                f"({row.get('temporal_confidence') or 'unknown'})"
            )
        if row.get("matched_terms"):
            print(
                f"   Match: {', '.join(row['matched_terms'])} | "
                f"fields={','.join(row.get('match_fields') or [])} | "
                f"engines={','.join(row.get('search_engines') or [])}"
            )
        if row.get("semantic_score") is not None:
            print(
                f"   Semantic: {row['semantic_score']:.4f} "
                f"({SEMANTIC_MODEL}/{SEMANTIC_DIMENSIONS}d) | "
                f"retrieval_score={row['retrieval_score']:.4f}"
            )
            if row.get("semantic_expansions"):
                print(f"   Semantic expansion: {' | '.join(row['semantic_expansions'])}")
        print(f"   Source: {row['source_path']}#{row['source_locator']}")


def retrieval_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "query_mode": getattr(args, "query_mode", "any"),
        "exclude_scopes": getattr(args, "exclude_scope", ()),
        "exclude_threads": getattr(args, "exclude_thread", ()),
        "exclusions": getattr(args, "exclude", ()),
        "since": getattr(args, "since", ""),
        "until": getattr(args, "until", ""),
        "recent": getattr(args, "recent", ""),
        "time_field": getattr(args, "time_field", "occurred"),
        "time_match": getattr(args, "time_match", "overlaps"),
        "as_of": getattr(args, "as_of", ""),
        "exclude_scope_range": getattr(args, "exclude_scope_range", False),
        "include_history": getattr(args, "history", False),
        "retrieval": getattr(args, "retrieval", "hybrid"),
    }


def command_search(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    tiers = args.tier or (ALL_TIERS if args.deep else HIGH_TIERS)
    rows = search_records(
        connection,
        args.query,
        limit=args.limit,
        scopes=args.scope,
        tiers=tiers,
        asset_type=args.asset,
        status_group=args.status,
        **retrieval_options(args),
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_records(rows, text_limit=args.text_limit)
    return 0


def command_asset(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    if args.query:
        rows = search_records(
            connection,
            args.query,
            limit=args.limit,
            scopes=args.scope,
            tiers=("asset",),
            asset_type=args.asset_type,
            status_group=args.status,
            **retrieval_options(args),
        )
    else:
        filters = ["asset_type=?"]
        params: list[Any] = [args.asset_type]
        if not args.history:
            filters.append("valid_to IS NULL")
        if args.scope:
            filters.append(f"scope_id IN ({','.join('?' for _ in args.scope)})")
            params.extend(args.scope)
        if args.status:
            filters.append("status_group=?")
            params.append(args.status)
        params.append(args.limit)
        rows = [
            decode_knowledge(row)
            for row in connection.execute(
                f"SELECT * FROM knowledge WHERE {' AND '.join(filters)} "
                "ORDER BY scope_id,status_group,evidence_count DESC,record_id LIMIT ?",
                params,
            )
        ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_records(rows, text_limit=args.text_limit)
    return 0


def command_context(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    high = search_records(
        connection,
        args.query,
        limit=args.limit,
        scopes=args.scope,
        tiers=HIGH_TIERS,
        asset_type=args.asset,
        **retrieval_options(args),
    )
    deep: list[dict[str, Any]] = []
    if args.deep_limit:
        deep = search_records(
            connection,
            args.query,
            limit=args.deep_limit,
            scopes=args.scope,
            tiers=("fact_block", "core"),
            **retrieval_options(args),
        )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in [*high, *deep]:
        key = (row["scope_id"], row["text"])
        if key not in seen:
            seen.add(key)
            rows.append(row)

    if args.json:
        print(json.dumps({"query": args.query, "records": rows}, ensure_ascii=False, indent=2))
        return 0
    print("# Codex History Context")
    print(f"Query: {args.query}")
    print(f"Records: {len(rows)}")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["scope_id"]].append(row)
    for scope_id, scope_rows in grouped.items():
        print(f"\n## {scope_id} | {scope_rows[0]['scope_title']}")
        for row in scope_rows:
            asset = f"/{row['asset_type']}" if row.get("asset_type") else ""
            print(
                f"- [{row['record_id']}] {row['tier']}{asset}; "
                f"status={row['status_group']}; evidence={len(row['evidence_refs'])}"
            )
            print(f"  {truncate(row['text'], args.text_limit)}")
            if row["evidence_refs"]:
                print(f"  refs: {', '.join(row['evidence_refs'][:12])}")
    return 0


def evidence_row(connection: sqlite3.Connection, identifier: str) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT * FROM evidence WHERE evidence_id LIKE ? ORDER BY evidence_id LIMIT 2",
        (identifier + "%",),
    ).fetchall()
    if len(rows) != 1:
        return None
    value = dict(rows[0])
    for key in ("scope_ids_json", "thread_ids_json", "applies_to_json"):
        value[key.removesuffix("_json")] = parse_json(value.pop(key)) or []
    return value


def raw_evidence_sources(
    evidence: dict[str, Any], repack_root: Path
) -> list[dict[str, Any]]:
    queue_dir = repack_root / "queues"
    candidates = [queue_dir / f"{scope_id}.tasks.jsonl.gz" for scope_id in evidence["scope_ids"]]
    if evidence["assignment"] != "exclusive":
        candidates.append(queue_dir / "global-shared.tasks.jsonl.gz")
    candidates = [path for path in dict.fromkeys(candidates) if path.is_file()]

    def scan(paths: Iterable[Path]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for path in paths:
            with gzip.open(path, "rt", encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if evidence["evidence_id"] not in line:
                        continue
                    task = json.loads(line)
                    for item_index, item in enumerate(task.get("evidence") or []):
                        if item.get("evidence_id") == evidence["evidence_id"]:
                            found.append(
                                {
                                    "queue_path": str(path),
                                    "line_number": line_number,
                                    "task_id": task.get("task_id"),
                                    "evidence_index": item_index,
                                    "policy": item.get("policy"),
                                    "applies_to": item.get("applies_to") or [],
                                    "content": item.get("content") or {},
                                }
                            )
        return found

    sources = scan(candidates)
    if sources:
        return sources
    remaining = [path for path in sorted(queue_dir.glob("*.tasks.jsonl.gz")) if path not in candidates]
    return scan(remaining)


def incremental_evidence_sources(
    connection: sqlite3.Connection, evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    prefix = "incremental-promotion-"
    source_task = str(evidence.get("source_task_id") or "")
    if not source_task.startswith(prefix):
        return []
    run_id = source_task.removeprefix(prefix)
    row = connection.execute(
        "SELECT manifest_path FROM sync_runs WHERE sync_run_id=?", (run_id,)
    ).fetchone()
    if row is None or not row["manifest_path"]:
        return []
    path = Path(row["manifest_path"]).parent / "evidence" / "items.jsonl.gz"
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if evidence["evidence_id"] not in line:
                continue
            item = json.loads(line)
            if item.get("short_id") != evidence["evidence_id"]:
                continue
            return [
                {
                    "queue_path": str(path),
                    "line_number": line_number,
                    "task_id": source_task,
                    "evidence_index": 0,
                    "policy": item.get("grade"),
                    "applies_to": evidence.get("applies_to") or [],
                    "content": item.get("payload") or {},
                    "source_kind": "incremental_item_cas",
                }
            ]
    return []


def summarize_evidence_source(source: dict[str, Any], text_limit: int) -> str:
    content = source["content"]
    kind = content.get("kind") or "unknown"
    if kind == "message":
        body = f"{content.get('role', '')}: {content.get('text', '')}"
    elif kind == "tool_trace":
        body = compact_json(
            {
                "tool_name": content.get("tool_name"),
                "purpose": content.get("purpose"),
                "input_summary": content.get("input_summary"),
                "result_summary": content.get("result_summary"),
            }
        )
    else:
        body = compact_json(content)
    return f"{kind}: {truncate(body, text_limit)}"


def portable_evidence_sources(
    connection: sqlite3.Connection, evidence: dict[str, Any]
) -> list[dict[str, Any]]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_events'"
    ).fetchone():
        return []
    result: list[dict[str, Any]] = []
    occurrences = connection.execute(
        "SELECT metadata_json FROM evidence_occurrences WHERE evidence_id=? ORDER BY thread_id,turn_seq,position",
        (evidence["evidence_id"],),
    ).fetchall()
    for occurrence in occurrences:
        event_id = (parse_json(occurrence["metadata_json"]) or {}).get("event_id")
        if not event_id:
            continue
        row = connection.execute(
            "SELECT ce.*,sf.source_path FROM canonical_events ce "
            "JOIN source_files sf ON sf.source_id=ce.source_id WHERE ce.event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            continue
        raw_text = str(row["raw_json"] or "")
        if not raw_text:
            start = int(row["byte_start"])
            end = int(row["byte_end"])
            cursor = 0
            pieces: list[bytes] = []
            for chunk in connection.execute(
                "SELECT size_bytes,cas_relative_path FROM source_chunks "
                "WHERE source_id=? ORDER BY chunk_index",
                (row["source_id"],),
            ):
                chunk_size = int(chunk["size_bytes"])
                chunk_end = cursor + chunk_size
                if end <= cursor:
                    break
                if start < chunk_end and end > cursor:
                    path = SNAPSHOT_ROOT / str(chunk["cas_relative_path"])
                    if path.is_file():
                        with path.open("rb") as handle:
                            local_start = max(0, start - cursor)
                            local_end = min(chunk_size, end - cursor)
                            handle.seek(local_start)
                            pieces.append(handle.read(local_end - local_start))
                cursor = chunk_end
            raw_text = b"".join(pieces).rstrip(b"\r\n").decode(
                "utf-8", errors="replace"
            )
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            raw = {"raw_json": raw_text}
        result.append(
            {
                "queue_path": row["source_path"],
                "line_number": row["line_no"],
                "task_id": row["turn_id"] or row["thread_id"],
                "evidence_index": 0,
                "policy": "portable_canonical_event",
                "applies_to": evidence.get("applies_to") or [],
                "content": {
                    "kind": row["role"] or row["payload_type"] or row["event_type"],
                    "text": row["text"],
                    "tool_name": row["tool_name"],
                    "raw": raw,
                },
                "source_kind": "portable_chunked_snapshot",
            }
        )
    return result


def linked_records(connection: sqlite3.Connection, evidence_id: str) -> list[dict[str, Any]]:
    return [
        decode_knowledge(row)
        for row in connection.execute(
            """
            SELECT k.* FROM knowledge_evidence ke
            JOIN knowledge k ON k.record_id=ke.record_id
            WHERE ke.evidence_id=?
            ORDER BY CASE k.tier
                WHEN 'asset' THEN 0 WHEN 'overview' THEN 1 WHEN 'ledger' THEN 2
                WHEN 'fact_block' THEN 3 ELSE 4 END,
                k.scope_id,k.record_id
            """,
            (evidence_id,),
        )
    ]


def evidence_occurrences(
    connection: sqlite3.Connection, evidence_id: str
) -> list[dict[str, Any]]:
    result = []
    for row in connection.execute(
        """
        SELECT eo.*,t.title AS thread_title
        FROM evidence_occurrences eo
        JOIN threads t ON t.thread_id=eo.thread_id
        WHERE eo.evidence_id=?
        ORDER BY eo.occurred_start_at,eo.thread_id,eo.turn_seq,eo.position
        """,
        (evidence_id,),
    ):
        value = dict(row)
        value["metadata"] = parse_json(value.pop("metadata_json")) or {}
        result.append(value)
    return result


def overview_claims(connection: sqlite3.Connection, record_id: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM overview_claims WHERE overview_record_id=? ORDER BY ordinal",
        (record_id,),
    ):
        value = dict(row)
        value["metadata"] = parse_json(value.pop("metadata_json")) or {}
        value["supporting_records"] = [
            dict(link)
            for link in connection.execute(
                "SELECT ocr.record_id,ocr.match_method,ocr.score,ocr.rank,k.tier,"
                "k.status_group,k.evidence_count FROM overview_claim_records ocr "
                "JOIN knowledge k ON k.record_id=ocr.record_id "
                "WHERE ocr.claim_id=? ORDER BY ocr.rank",
                (value["claim_id"],),
            )
        ]
        claims.append(value)
    return claims


def record_relations(connection: sqlite3.Connection, record_id: str) -> list[dict[str, Any]]:
    return [
        {
            **dict(row),
            "evidence_refs": parse_json(row["evidence_refs_json"]) or [],
            "metadata": parse_json(row["metadata_json"]) or {},
        }
        for row in connection.execute(
            "SELECT *,CASE WHEN source_record_id=? THEN 'outgoing' ELSE 'incoming' END direction "
            "FROM knowledge_relations WHERE source_record_id=? OR target_record_id=? "
            "ORDER BY relation_type,source_record_id,target_record_id",
            (record_id, record_id, record_id),
        )
    ]


def command_trace(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    record_rows = connection.execute(
        "SELECT * FROM knowledge WHERE record_id LIKE ? ORDER BY record_id LIMIT 2",
        (args.identifier + "%",),
    ).fetchall()
    records = [decode_knowledge(row) for row in record_rows]
    evidence_items: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    if len(records) == 1:
        claims = overview_claims(connection, records[0]["record_id"])
        relations = record_relations(connection, records[0]["record_id"])
        for ref in records[0]["evidence_refs"]:
            item = evidence_row(connection, ref)
            if item:
                evidence_items.append(item)
    elif len(records) > 1:
        raise SystemExit("Record prefix is ambiguous; provide more characters.")
    else:
        item = evidence_row(connection, args.identifier)
        if not item:
            raise SystemExit(f"No unique record or evidence matched: {args.identifier}")
        evidence_items = [item]
        records = linked_records(connection, item["evidence_id"])

    database_metadata = metadata(connection)
    repack_root = Path(database_metadata["repack_root"]) if database_metadata.get("repack_root") else None
    raw_sources: dict[str, list[dict[str, Any]]] = {}
    occurrences: dict[str, list[dict[str, Any]]] = {}
    for item in evidence_items:
        sources = portable_evidence_sources(connection, item)
        if not sources and repack_root is not None:
            sources = raw_evidence_sources(item, repack_root)
        if not sources:
            sources = incremental_evidence_sources(connection, item)
        raw_sources[item["evidence_id"]] = sources
        occurrences[item["evidence_id"]] = evidence_occurrences(
            connection, item["evidence_id"]
        )
    raw_sources = remap_path_fields(raw_sources)

    if args.json:
        print(
            json.dumps(
                {
                    "records": records,
                    "overview_claims": claims,
                    "relations": relations,
                    "evidence": evidence_items,
                    "occurrences": occurrences,
                    "raw_sources": raw_sources,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if records:
        print("# Linked knowledge")
        print_records(records[: args.limit], text_limit=args.text_limit)
    if claims:
        print("\n# Overview claims")
        for claim in claims:
            links = ", ".join(
                f"{link['record_id']} ({link['score']:.3f})"
                for link in claim["supporting_records"]
            ) or "none"
            print(
                f"- [{claim['claim_id']}] {claim['status']} "
                f"span={claim['start_char']}:{claim['end_char']} | {claim['claim_text']}"
            )
            print(f"  supports: {links}")
    if relations:
        print("\n# Fact evolution")
        for relation in relations:
            print(
                f"- {relation['direction']} {relation['relation_type']}: "
                f"{relation['source_record_id']} -> {relation['target_record_id']} "
                f"({relation['confidence']})"
            )
    print("\n# Evidence trace")
    for item in evidence_items[: args.limit]:
        print(
            f"- {item['evidence_id']} | {item['assignment']} | "
            f"chars={item['evidence_chars']} | scopes={','.join(item['scope_ids'])}"
        )
        print(
            f"  source_task={item['source_task_id']} | "
            f"applies_to={','.join(item['applies_to'])}"
        )
        for occurrence in occurrences[item["evidence_id"]][: args.occurrence_limit]:
            print(
                f"  occurrence: {occurrence['thread_id']} "
                f"({occurrence['thread_title']}) turn={occurrence['turn_seq']} "
                f"position={occurrence['position']} "
                f"time={occurrence['occurred_start_at']}..{occurrence['occurred_end_at']}"
            )
        sources = raw_sources[item["evidence_id"]]
        if not sources:
            print("  raw source: not found in repacked queues")
        for source in sources[: args.source_limit]:
            print(
                f"  raw source: {source['queue_path']}:{source['line_number']} "
                f"task={source['task_id']} evidence[{source['evidence_index']}]"
            )
            if args.raw:
                print(json.dumps(source["content"], ensure_ascii=False, indent=2))
            else:
                print(f"  {summarize_evidence_source(source, args.text_limit)}")
    return 0


def command_claims(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT record_id,scope_id,scope_title,text FROM knowledge "
        "WHERE tier='overview' AND valid_to IS NULL "
        "AND (record_id LIKE ? OR scope_id=?) ORDER BY scope_id",
        (args.identifier + "%", args.identifier),
    ).fetchall()
    result = [
        {"overview": dict(row), "claims": overview_claims(connection, row["record_id"])}
        for row in rows
    ]
    if not result:
        raise SystemExit(f"No current overview matched: {args.identifier}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for item in result:
        overview = item["overview"]
        print(f"# {overview['scope_id']} | {overview['record_id']}")
        for claim in item["claims"]:
            links = ", ".join(
                f"{link['record_id']} ({link['score']:.3f})"
                for link in claim["supporting_records"]
            ) or "none"
            print(f"- [{claim['claim_id']}] {claim['status']}: {claim['claim_text']}")
            print(f"  supports: {links}")
    return 0


def command_compare(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    tiers = ALL_TIERS if args.deep else HIGH_TIERS
    left = search_records(
        connection, args.query, limit=args.limit, scopes=(args.left,), tiers=tiers,
        **retrieval_options(args),
    )
    right = search_records(
        connection, args.query, limit=args.limit, scopes=(args.right,), tiers=tiers,
        **retrieval_options(args),
    )
    left_text = {row["text"] for row in left}
    right_text = {row["text"] for row in right}
    result = {
        "query": args.query,
        "left_scope": args.left,
        "right_scope": args.right,
        "shared_exact_texts": sorted(left_text & right_text),
        "left": left,
        "right": right,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"# Scope comparison: {args.left} vs {args.right}")
    print(f"Query: {args.query}")
    print(f"Exact shared results: {len(result['shared_exact_texts'])}")
    print(f"\n## {args.left}")
    print_records(left, text_limit=args.text_limit)
    print(f"\n## {args.right}")
    print_records(right, text_limit=args.text_limit)
    return 0


def command_artifacts(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    expression = fts_expression(args.query)
    if expression:
        rows = connection.execute(
            """
            SELECT ap.*,af.size_human,af.mime_type,af.tiers,af.keep_reasons,
                   bm25(artifact_fts,8.0,4.0,2.0,2.0) AS rank
            FROM artifact_fts
            JOIN artifact_paths ap ON ap.rowid=artifact_fts.rowid
            JOIN artifact_files af ON af.sha256=ap.sha256
            WHERE artifact_fts MATCH ?
            ORDER BY rank,ap.path
            LIMIT ?
            """,
            (expression, args.limit),
        ).fetchall()
    else:
        terms = query_terms(args.query)
        where = " OR ".join("ap.path LIKE ?" for _ in terms) or "1=1"
        rows = connection.execute(
            f"""
            SELECT ap.*,af.size_human,af.mime_type,af.tiers,af.keep_reasons
            FROM artifact_paths ap JOIN artifact_files af ON af.sha256=ap.sha256
            WHERE {where} ORDER BY ap.path LIMIT ?
            """,
            [*(f"%{term}%" for term in terms), args.limit],
        ).fetchall()
    artifacts = [remap_path_fields(dict(row)) for row in rows]
    for row in artifacts:
        row.pop("rank", None)
        local_path = resolve_artifact_path(str(row.get("cas_relative_path") or ""))
        row["artifact_available"] = local_path is not None
        row["local_cas_path"] = local_path

    ledger_filters = ["(ref LIKE ? OR role LIKE ?)"]
    ledger_params: list[Any] = [f"%{args.query}%", f"%{args.query}%"]
    if args.scope:
        ledger_filters.append(f"scope_id IN ({','.join('?' for _ in args.scope)})")
        ledger_params.extend(args.scope)
    ledger_params.append(args.limit)
    ledger_rows = [
        remap_path_fields(dict(row))
        for row in connection.execute(
            f"SELECT * FROM ledger_artifacts WHERE {' AND '.join(ledger_filters)} "
            "ORDER BY scope_id,ref LIMIT ?",
            ledger_params,
        )
    ]
    for row in ledger_rows:
        row["evidence_refs"] = parse_json(row.pop("evidence_refs_json")) or []

    if args.json:
        print(json.dumps({"files": artifacts, "ledger_refs": ledger_rows}, ensure_ascii=False, indent=2))
        return 0
    print("# Artifact files")
    if not artifacts:
        print("No matching artifact files.")
    for index, row in enumerate(artifacts, 1):
        print(f"{index}. {row['path']} ({row['size_human']}, {row['mime_type']})")
        print(f"   {row['artifact_uri']}")
        print(f"   CAS: {row['cas_relative_path']} | sha256={row['sha256']}")
        print(f"   Local: {row['local_cas_path'] or 'unavailable'}")
    print("\n# Ledger artifact references")
    if not ledger_rows:
        print("No matching ledger artifact references.")
    for row in ledger_rows:
        print(f"- {row['scope_id']} | {row['ref']} | {row['role']}")
        print(f"  Evidence: {', '.join(row['evidence_refs']) or 'none'}")
    return 0


def command_conversation(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    from .conversation import (
        build_conversation_export,
        list_threads,
        parse_turn_range,
        write_conversation_export,
    )

    selectors = [*args.selectors, *args.thread]
    if args.list:
        rows = list_threads(connection, selectors, limit=args.limit)
        if args.json:
            print(json.dumps({"threads": rows}, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(
                    f"{row['thread_id']} | {row['last_activity_at'] or 'unknown'} | "
                    f"turns={row['turn_count']} | {row['title']}"
                )
        return 0
    if args.output is None:
        raise SystemExit("conversation export requires --output PATH")
    output_format = args.format or ("json" if args.output.suffix.lower() == ".json" else "html")
    try:
        payload = build_conversation_export(
            connection,
            SNAPSHOT_ROOT,
            selectors=selectors,
            scope_selectors=args.scope,
            turn_range=parse_turn_range(args.turn_range),
            since=args.since,
            until=args.until,
            include_tools=not args.no_tools,
            include_goals=not args.no_goals,
            include_internal=args.include_internal,
            include_raw=args.include_raw,
            embed_images=args.embed_images,
            artifact_roots=ARTIFACT_ROOTS,
            title=args.title,
        )
        report = write_conversation_export(
            payload,
            args.output,
            output_format=output_format,
            force=args.force,
        )
    except (FileExistsError, FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Conversation export: {report['output']}")
        print(
            f"Format: {report['format']} | threads={report['threads']} | "
            f"messages={report['messages']} | size={report['size_bytes']} bytes"
        )
        print(f"Roles: {compact_json(report['roles'])}")
    return 0


def command_stats(args: argparse.Namespace, connection: sqlite3.Connection) -> int:
    data = {
        "metadata": metadata(connection),
        "database": str(args.db),
        "database_size_bytes": args.db.stat().st_size,
        "counts": {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "scopes",
                "threads",
                "evidence",
                "evidence_occurrences",
                "knowledge",
                "knowledge_evidence",
                "record_evidence_occurrences",
                "knowledge_relations",
                "semantic_documents",
                "semantic_document_records",
                "semantic_query_expansions",
                "overview_claims",
                "overview_claim_records",
                "relation_candidates",
                "embedding_runs",
                "embedding_batches",
                "aliases",
                "ledger_artifacts",
                "artifact_files",
                "artifact_paths",
                "sync_runs",
                "source_checkpoints",
                "pending_turns",
                "incremental_events",
            )
        },
        "tiers": dict(
            connection.execute(
                "SELECT tier,count(*) FROM knowledge GROUP BY tier ORDER BY tier"
            ).fetchall()
        ),
        "assets": dict(
            connection.execute(
                "SELECT asset_type,count(*) FROM knowledge WHERE asset_type<>'' "
                "GROUP BY asset_type ORDER BY asset_type"
            ).fetchall()
        ),
        "status_groups": dict(
            connection.execute(
                "SELECT status_group,count(*) FROM knowledge "
                "GROUP BY status_group ORDER BY status_group"
            ).fetchall()
        ),
        "temporal_confidence": dict(
            connection.execute(
                "SELECT temporal_confidence,count(*) FROM knowledge "
                "GROUP BY temporal_confidence ORDER BY temporal_confidence"
            ).fetchall()
        ),
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(f"Database: {data['database']}")
        print(f"Size: {data['database_size_bytes'] / 1024 / 1024:.1f} MiB")
        for section in ("counts", "tiers", "assets", "status_groups"):
            print(f"{section}: {compact_json(data[section])}")
    return 0


def add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    parser.add_argument("--text-limit", type=int, default=700)


def add_retrieval_options(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--any", dest="query_mode", action="store_const", const="any",
        help="match any positive term (default)",
    )
    mode.add_argument(
        "--all", dest="query_mode", action="store_const", const="all",
        help="require every positive term or alias group",
    )
    parser.set_defaults(query_mode="any")
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="exclude records containing this term; repeatable",
    )
    parser.add_argument(
        "--exclude-scope", action="append", default=[],
        help="exclude a scope from candidate retrieval; repeatable",
    )
    parser.add_argument(
        "--exclude-thread", action="append", default=[],
        help="exclude records derived from a transcript thread; repeatable",
    )
    parser.add_argument("--since", default="", help="inclusive ISO date/time lower bound")
    parser.add_argument("--until", default="", help="inclusive ISO date/time upper bound")
    parser.add_argument(
        "--recent", default="", help="relative lower bound such as 12h, 30d, 8w, 6m, 1y",
    )
    parser.add_argument(
        "--time-field", choices=("occurred", "asserted", "indexed"),
        default="occurred", help="timestamp dimension used by time filters",
    )
    parser.add_argument(
        "--time-match", choices=("overlaps", "contained"), default="overlaps",
        help="match records overlapping the window or fully contained in it",
    )
    parser.add_argument(
        "--as-of", default="",
        help="return the knowledge version valid at this ISO date/time",
    )
    parser.add_argument(
        "--exclude-scope-range", action="store_true",
        help="exclude records whose occurrence time is only inherited from a scope range",
    )
    parser.add_argument(
        "--retrieval", choices=("lexical", "hybrid", "semantic"),
        default=os.environ.get("CODEX_HISTORY_RETRIEVAL", "hybrid"),
        help="retrieval channel; hybrid is the default",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="include superseded records; current records are the default",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="search historical knowledge")
    search.add_argument("query")
    search.add_argument("--scope", action="append", default=[])
    search.add_argument("--tier", action="append", choices=ALL_TIERS)
    search.add_argument("--asset", choices=ASSET_TYPES, default="")
    search.add_argument("--status", default="")
    search.add_argument("--deep", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    add_retrieval_options(search)
    add_output_options(search)
    search.set_defaults(handler=command_search)

    asset = subparsers.add_parser("asset", help="list or search one durable asset")
    asset.add_argument("asset_type", choices=ASSET_TYPES)
    asset.add_argument("query", nargs="?", default="")
    asset.add_argument("--scope", action="append", default=[])
    asset.add_argument("--status", default="")
    asset.add_argument("--limit", type=int, default=20)
    add_retrieval_options(asset)
    add_output_options(asset)
    asset.set_defaults(handler=command_asset)

    unresolved = subparsers.add_parser("unresolved", help="query unresolved assets")
    unresolved.add_argument("query", nargs="?", default="")
    unresolved.add_argument("--scope", action="append", default=[])
    unresolved.add_argument("--status", default="")
    unresolved.add_argument("--limit", type=int, default=20)
    add_retrieval_options(unresolved)
    add_output_options(unresolved)
    unresolved.set_defaults(handler=command_asset, asset_type="unresolved")

    context = subparsers.add_parser("context", help="build a progressive context packet")
    context.add_argument("query")
    context.add_argument("--scope", action="append", default=[])
    context.add_argument("--asset", choices=ASSET_TYPES, default="")
    context.add_argument("--limit", type=int, default=12)
    context.add_argument("--deep-limit", type=int, default=6)
    add_retrieval_options(context)
    add_output_options(context)
    context.set_defaults(handler=command_context)

    trace = subparsers.add_parser("trace", help="trace a fact or evidence ID to raw evidence")
    trace.add_argument("identifier")
    trace.add_argument("--limit", type=int, default=12)
    trace.add_argument("--source-limit", type=int, default=3)
    trace.add_argument("--occurrence-limit", type=int, default=12)
    trace.add_argument("--raw", action="store_true")
    add_output_options(trace)
    trace.set_defaults(handler=command_trace)

    claims = subparsers.add_parser(
        "claims", help="show sentence-level evidence links for an overview"
    )
    claims.add_argument("identifier", help="overview record prefix or scope ID")
    add_output_options(claims)
    claims.set_defaults(handler=command_claims)

    compare = subparsers.add_parser("compare", help="compare search results for two scopes")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("query")
    compare.add_argument("--deep", action="store_true")
    compare.add_argument("--limit", type=int, default=8)
    add_retrieval_options(compare)
    add_output_options(compare)
    compare.set_defaults(handler=command_compare)

    artifacts = subparsers.add_parser("artifacts", help="search CAS files and ledger refs")
    artifacts.add_argument("query")
    artifacts.add_argument("--scope", action="append", default=[])
    artifacts.add_argument("--limit", type=int, default=20)
    add_output_options(artifacts)
    artifacts.set_defaults(handler=command_artifacts)

    conversation = subparsers.add_parser(
        "conversation",
        help="list or export original conversation ranges as portable evidence",
    )
    conversation.add_argument(
        "selectors",
        nargs="*",
        help="thread ID, exact title, or title substring; multiple matches are combined",
    )
    conversation.add_argument(
        "--thread", action="append", default=[],
        help="additional thread ID or title selector; repeatable",
    )
    conversation.add_argument(
        "--scope", action="append", default=[],
        help="include every thread in this scope ID or title; repeatable",
    )
    conversation.add_argument("--list", action="store_true", help="list matching threads")
    conversation.add_argument("--limit", type=int, default=100, help="maximum list results")
    conversation.add_argument("-o", "--output", type=Path, default=None)
    conversation.add_argument("--format", choices=("html", "json"), default="")
    conversation.add_argument(
        "--turn-range", default="",
        help="inclusive 1-based range such as 4:12, 8, :20, or 30:",
    )
    conversation.add_argument("--since", default="", help="inclusive event timestamp lower bound")
    conversation.add_argument("--until", default="", help="inclusive event timestamp upper bound")
    conversation.add_argument("--no-tools", action="store_true", help="omit tool calls and outputs")
    conversation.add_argument("--no-goals", action="store_true", help="omit goal state events")
    conversation.add_argument(
        "--include-internal", action="store_true",
        help="include injected environment and plugin context messages",
    )
    conversation.add_argument(
        "--include-raw", action="store_true",
        help="embed complete normalized JSON events for source inspection",
    )
    conversation.add_argument(
        "--embed-images", action="store_true",
        help="embed referenced image artifacts into the portable HTML/JSON",
    )
    conversation.add_argument("--title", default="Codex conversation evidence")
    conversation.add_argument("--force", action="store_true", help="replace an existing output")
    conversation.add_argument("--json", action="store_true", help="emit a JSON operation report")
    conversation.set_defaults(handler=command_conversation)

    stats = subparsers.add_parser("stats", help="show index health and counts")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(handler=command_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    connection = connect(args.db)
    try:
        return args.handler(args, connection)
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
