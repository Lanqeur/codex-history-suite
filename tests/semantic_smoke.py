from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path

from codex_history.config import ensure_profile_dirs, load_config, write_initial_config
from codex_history.schema import connect, initialize
from codex_history.semantic import EmbeddingClient, refresh_embeddings
from codex_history.util import utc_now


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-history-semantic-") as temporary:
        root = Path(temporary)
        write_initial_config(root / "home", profile="default", source_roots=[root / "source"])
        config = replace(
            load_config(root / "home"),
            embedding_enabled=True,
            embedding_dimensions=4,
            embedding_api_key_env="SEMANTIC_SMOKE_KEY",
            embedding_input_price_cny=1.0,
        )
        os.environ["SEMANTIC_SMOKE_KEY"] = "test-only"
        ensure_profile_dirs(config)
        database = config.root / "semantic-smoke.sqlite3"
        connection = connect(database)
        initialize(connection)
        connection.executemany(
            "INSERT INTO semantic_documents(document_id,content_sha256,document_text,record_count,created_at) VALUES(?,?,?,?,?)",
            (
                ("doc-1", "sha-1", "交付收口与上线准备", 1, utc_now()),
                ("doc-2", "sha-2", "WSL transcript 图片膨胀", 1, utc_now()),
            ),
        )
        original = EmbeddingClient.embed

        def fake_embed(self, texts):
            return [[float(index + 1), 0.0, 0.0, 0.0] for index, _ in enumerate(texts)], len(texts) * 4

        EmbeddingClient.embed = fake_embed
        try:
            first = refresh_embeddings(config, connection, max_cost_cny=1.0)
            second = refresh_embeddings(config, connection, max_cost_cny=0.0)
        finally:
            EmbeddingClient.embed = original
            connection.close()
        assert first["embedded"] == 2
        assert second["embedded"] == 0
        assert second["reused"] == 2
    print("semantic smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

