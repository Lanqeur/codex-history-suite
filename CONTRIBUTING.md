# Contributing

Contributions that improve transcript adapters, platform portability, retrieval quality, evidence traceability, or incremental equivalence are welcome.

## Development

```bash
python -m pip install .[dev]
python -m pytest
```

Install `.[semantic]` to run the Chroma smoke test:

```bash
python tests/semantic_smoke.py
```

Every change to ingestion or derived knowledge must preserve the release invariant: multiple incremental updates over a source corpus and a clean full rebuild of the same corpus must produce equivalent logical records and provenance.

Never commit real Codex transcripts, generated databases, CAS objects, API keys, `.env` files, or machine-specific configuration. Tests must use synthetic fixtures.
