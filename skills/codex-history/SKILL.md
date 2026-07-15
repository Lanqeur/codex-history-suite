---
name: codex-history
description: Retrieve, compare, and trace the user's local Codex conversation history from the active Codex History Suite profile. Use when work depends on prior decisions, unresolved tasks, failures and recoveries, verified capabilities, collaboration preferences, branch-family differences, exact historical tool calls, evidence IDs, overview claims, or files preserved in the artifact CAS. This Skill is read-only.
---

# Codex History

Use progressive disclosure over the active local SQLite/Chroma knowledge base. Resolve `../../scripts/codex_history.py` relative to this Skill directory and run it with Python 3.11 or newer. The CLI locates the active database from the shared cross-platform profile; do not hardcode database, virtualenv, archive, or Chroma paths.

## Retrieval Workflow

1. Start with `context` or `search` using two to five distinctive terms. Prefer project names, feature names, error text, paths, and domain vocabulary.
2. Stay in high tiers initially. Add `--deep` only when implementation details or tool traces matter.
3. Use `--all`, quoted phrases, `-term`, `--exclude`, `--scope`, `--asset`, and `--status` to reduce noise.
4. Use `--time-match contained` for work fully within a window, `overlaps` for cross-period research, and `--as-of` for historical validity.
5. Run `claims SCOPE_OR_OVERVIEW_ID` to inspect sentence-level support.
6. Run `trace RECORD_OR_EVIDENCE_ID` before relying on consequential completion, failure, decision, or tool-output claims. Use `--raw` only when summarized evidence is insufficient.
7. Use `artifacts` for content-addressed historical files and images.

## Commands

```bash
python3 ../../scripts/codex_history.py search '章节写手 SSE 正文流' --limit 10
python3 ../../scripts/codex_history.py context '会话恢复 transcript 图片 OOM' --all --deep-limit 6
python3 ../../scripts/codex_history.py search 'WSL 会话' --until 2026-06-30 --time-match contained
python3 ../../scripts/codex_history.py search '部署 -支付' --recent 30d
python3 ../../scripts/codex_history.py claims SCOPE_OR_OVERVIEW_ID
python3 ../../scripts/codex_history.py trace RECORD_OR_EVIDENCE_ID
python3 ../../scripts/codex_history.py artifacts test_agent_workflow_contracts.py
python3 ../../scripts/codex_history.py stats
```

Add `--json` for programmatic filtering. If no active build exists, stop and direct the user to explicitly invoke `$build-codex-history`; do not initialize or update from this read-only Skill.

## Evidence Discipline

Similarity is retrieval, not truth. Distinguish `verified`, `executed`, `reported_outcome`, `planned`, `failed`, `blocked`, `uncertain`, and `mixed`. An assistant message can document a claim without independently verifying it. Prefer exact tool output and Evidence occurrences for high-impact conclusions.

SQLite controls scope, status, time, versions, claims, relations, and provenance. Chroma only contributes candidates. Even a verified historical row can be stale relative to the live repository, issue tracker, runtime, or deployment; inspect current state for present-tense assertions.

Read [references/schema.md](references/schema.md) when interpreting fields, time semantics, artifact URIs, relations, or trace output.

