# Security

## Data Boundary

Codex History Suite processes local Codex transcripts. A default extractive and lexical build stays on the local machine. Enabling model summaries or embeddings sends selected text to the provider configured by the user.

The project repository and release archives must never include transcripts, generated knowledge databases, artifact CAS contents, model caches, `.env` files, API keys, access tokens, or machine-specific configuration.

## Reporting A Vulnerability

Do not include credentials, private transcripts, or personal data in a public issue. Open a minimal GitHub issue asking for a private contact channel, or use GitHub's private vulnerability reporting feature when it is enabled for the repository.

## Supported Baseline

The current release requires Python 3.11 or newer and SQLite FTS5. Source transcripts are opened read-only, staging builds are isolated, and `active.json` is promoted only after integrity and provenance audits pass.
