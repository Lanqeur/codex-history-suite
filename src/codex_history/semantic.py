from __future__ import annotations

import json
import os
import random
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import ProfileConfig
from .util import canonical_json, utc_now


COLLECTION_NAME = "codex_history"


@dataclass(frozen=True)
class EmbeddingSettings:
    endpoint: str
    api_key_env: str
    model: str
    dimensions: int
    input_price_cny: float = 0.0
    env_file: str = ""

    @classmethod
    def from_config(cls, config: ProfileConfig) -> "EmbeddingSettings":
        return cls(
            endpoint=config.embedding_endpoint,
            api_key_env=config.embedding_api_key_env,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            input_price_cny=config.embedding_input_price_cny,
            env_file=config.embedding_env_file,
        )

    @classmethod
    def from_environment(cls) -> "EmbeddingSettings":
        return cls(
            endpoint=os.environ.get(
                "CODEX_HISTORY_EMBEDDING_ENDPOINT",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            api_key_env=os.environ.get("CODEX_HISTORY_EMBEDDING_API_KEY_ENV", "DASHSCOPE_API_KEY"),
            model=os.environ.get("CODEX_HISTORY_EMBEDDING_MODEL", "text-embedding-v4"),
            dimensions=int(os.environ.get("CODEX_HISTORY_EMBEDDING_DIMENSIONS", "512")),
            input_price_cny=float(os.environ.get("CODEX_HISTORY_EMBEDDING_INPUT_PRICE_CNY", "0")),
            env_file=os.environ.get("CODEX_HISTORY_EMBEDDING_ENV_FILE", ""),
        )


def _environment_value(name: str, env_file: str) -> str:
    value = os.environ.get(name, "")
    if value or not env_file:
        return value
    path = Path(env_file).expanduser()
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, item = line.split("=", 1)
        if key.strip() == name:
            return item.strip().strip("'\"")
    return ""


class EmbeddingClient:
    def __init__(self, settings: EmbeddingSettings, *, timeout: int = 120) -> None:
        self.settings = settings
        self.api_key = _environment_value(settings.api_key_env, settings.env_file)
        endpoint = settings.endpoint.rstrip("/")
        self.url = endpoint if endpoint.endswith("/embeddings") else endpoint + "/embeddings"
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError(f"Embedding API key is missing from ${settings.api_key_env}")

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        payload = json.dumps(
            {
                "model": self.settings.model,
                "input": texts,
                "dimensions": self.settings.dimensions,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Embedding API HTTP {error.code}: {detail}") from error
        rows = sorted(body.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embedding count mismatch: expected {len(texts)}, received {len(vectors)}"
            )
        if any(len(vector) != self.settings.dimensions for vector in vectors):
            raise RuntimeError("Embedding API returned an unexpected vector dimension")
        usage = body.get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        return vectors, tokens


def embed_with_retry(
    client: EmbeddingClient,
    texts: list[str],
    attempts: int = 3,
) -> tuple[list[list[float]], int, int]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            vectors, tokens = client.embed(texts)
            return vectors, tokens, attempt
        except Exception as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))) + random.random() * 0.2)
    assert last_error is not None
    raise last_error


def chroma_collection(
    path: Path,
    settings: EmbeddingSettings,
    *,
    create: bool,
    collection_name: str = COLLECTION_NAME,
):
    try:
        import chromadb
    except ImportError as error:
        raise RuntimeError(
            "ChromaDB is unavailable; install codex-history-suite[semantic]"
        ) from error
    path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(path))
    metadata = {
        "hnsw:space": "cosine",
        "model": settings.model,
        "dimensions": settings.dimensions,
        "authority": "semantic-candidates-only",
    }
    if create:
        names = {item.name for item in client.list_collections()}
        selected = collection_name
        if collection_name not in names and collection_name == COLLECTION_NAME:
            legacy = "codex_history_v21"
            if legacy in names:
                selected = legacy
        collection = (
            client.get_collection(selected)
            if selected in names
            else client.create_collection(selected, metadata=metadata)
        )
    else:
        collection = client.get_collection(collection_name)
    return client, collection


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def refresh_embeddings(
    config: ProfileConfig,
    connection: sqlite3.Connection,
    *,
    max_cost_cny: float | None,
    batch_size: int = 8,
    chroma_path: Path | None = None,
) -> dict[str, Any]:
    settings = EmbeddingSettings.from_config(config)
    client = EmbeddingClient(settings)
    _chroma_client, collection = chroma_collection(
        chroma_path or config.root / "semantic/chroma", settings, create=True
    )
    documents = [
        dict(row)
        for row in connection.execute(
            "SELECT document_id,document_text FROM semantic_documents ORDER BY document_id"
        )
    ]
    existing = set(collection.get(include=[]).get("ids") or []) if collection.count() else set()
    requested_ids = {str(row["document_id"]) for row in documents}
    stale = sorted(existing - requested_ids)
    pending = [row for row in documents if row["document_id"] not in existing]
    estimated_tokens = sum(max(1, len(str(row["document_text"])) // 2) for row in pending)
    estimated_cost = estimated_tokens / 1_000_000 * settings.input_price_cny
    if max_cost_cny is not None and estimated_cost > max_cost_cny:
        raise RuntimeError(
            f"Embedding estimate {estimated_cost:.6f} CNY exceeds remaining limit {max_cost_cny:.6f} CNY"
        )
    if stale:
        for chunk in _batches(stale, 500):
            collection.delete(ids=chunk)
    run_id = f"embedding-{uuid.uuid4().hex[:16]}"
    connection.execute(
        "INSERT INTO embedding_runs(run_id,model,dimensions,status,started_at,requested_documents,estimated_cost_cny,max_cost_cny,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            settings.model,
            settings.dimensions,
            "running",
            utc_now(),
            len(pending),
            estimated_cost,
            max_cost_cny,
            canonical_json({"collection": collection.name}),
        ),
    )
    total_tokens = 0
    embedded = 0
    for batch_number, batch in enumerate(_batches(pending, batch_size), 1):
        ids = [str(row["document_id"]) for row in batch]
        texts = [str(row["document_text"]) for row in batch]
        connection.execute(
            "INSERT INTO embedding_batches(run_id,batch_number,document_ids_json,status) VALUES(?,?,?,'running')",
            (run_id, batch_number, canonical_json(ids)),
        )
        vectors, tokens, attempts = embed_with_retry(client, texts)
        collection.add(ids=ids, documents=texts, embeddings=vectors)
        batch_cost = tokens / 1_000_000 * settings.input_price_cny
        connection.execute(
            "UPDATE embedding_batches SET status='complete',attempt_count=?,input_tokens=?,cost_cny=?,completed_at=? WHERE run_id=? AND batch_number=?",
            (attempts, tokens, batch_cost, utc_now(), run_id, batch_number),
        )
        total_tokens += tokens
        embedded += len(batch)
        actual_cost = total_tokens / 1_000_000 * settings.input_price_cny
        if max_cost_cny is not None and actual_cost > max_cost_cny:
            raise RuntimeError("Embedding actual cost exceeded the configured limit")
    actual_cost = total_tokens / 1_000_000 * settings.input_price_cny
    connection.execute(
        "UPDATE embedding_runs SET status='complete',completed_at=?,embedded_documents=?,input_tokens=?,actual_cost_cny=? WHERE run_id=?",
        (utc_now(), embedded, total_tokens, actual_cost, run_id),
    )
    return {
        "enabled": True,
        "run_id": run_id,
        "model": settings.model,
        "dimensions": settings.dimensions,
        "collection": collection.name,
        "documents": len(documents),
        "embedded": embedded,
        "reused": len(documents) - embedded,
        "deleted_stale": len(stale),
        "input_tokens": total_tokens,
        "actual_cost_cny": round(actual_cost, 6),
    }


def query_embedding(text: str) -> list[float]:
    settings = EmbeddingSettings.from_environment()
    vectors, _tokens, _attempts = embed_with_retry(EmbeddingClient(settings), [text])
    return vectors[0]
