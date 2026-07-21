---
name: codex-history
description: Retrieve, compare, trace, and export the user's local Codex conversation history from one or multiple Codex History Suite profiles. Use when work depends on prior decisions, unresolved tasks, failures and recoveries, verified capabilities, collaboration preferences, branch-family differences, exact historical messages or tool calls, evidence IDs, overview claims, reviewable conversation ranges, or files preserved in the artifact CAS. This Skill is read-only.
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
7. Use `conversation` when the user or another agent needs to inspect, combine, or share a bounded sequence of original messages and tool evidence. Start with `--list`, then constrain the export by thread/scope, turn range, or event time.
8. Use `artifacts` for content-addressed historical files and images.

## Commands

```bash
python3 ../../scripts/codex_history.py search '章节写手 SSE 正文流' --limit 10
python3 ../../scripts/codex_history.py context '会话恢复 transcript 图片 OOM' --all --deep-limit 6
python3 ../../scripts/codex_history.py search 'WSL 会话' --until 2026-06-30 --time-match contained
python3 ../../scripts/codex_history.py search '部署 -支付' --recent 30d
python3 ../../scripts/codex_history.py claims SCOPE_OR_OVERVIEW_ID
python3 ../../scripts/codex_history.py trace RECORD_OR_EVIDENCE_ID
python3 ../../scripts/codex_history.py artifacts test_agent_workflow_contracts.py
python3 ../../scripts/codex_history.py conversation '会话标题关键词' --list
python3 ../../scripts/codex_history.py conversation THREAD_ID --turn-range 4:12 --include-raw --embed-attachments -o evidence.html
python3 ../../scripts/codex_history.py conversation --scope FAMILY_ID --since 2026-06-01 --until 2026-06-30 -o family-evidence.html
python3 ../../scripts/codex_history.py stats
```

Add `--json` for programmatic filtering. If no active build exists, stop and direct the user to explicitly invoke `$build-codex-history`; do not initialize or update from this read-only Skill.

When the user wants to continue an old thread inside native Codex rather than inspect an offline export, direct the stateful operation to `$restore-codex-session`. This read-only Skill must not create or rewrite Codex sessions.

Conversation export defaults to visible user/assistant messages plus tool and goal events. It suppresses duplicate Codex event representations and injected environment/plugin context. Add `--include-internal` only when internal context is material and `--include-raw` for the complete normalized source event. Attachment metadata is always included when an event has an artifact URI or Event-linked `artifact_observation`; use `--embed-images` for an image-only portable viewer or `--embed-attachments` for captured images and documents. Default binary limits are 25 MiB per file and 100 MiB total. Prefer a bounded export over an entire large thread. The offline viewer safely renders user/assistant Markdown, GFM tables, fenced Mermaid diagrams, images, bounded text previews, and attachment cards while preserving a literal source mode; tool, goal, internal, and raw events stay literal. PDFs can open in-browser and other embedded documents can be downloaded. A human can filter, select, reorder, and export an evidence sequence as HTML, Markdown, or JSON.

For cross-device questions, start with the read-only federated command. Omit `--from` to search every enabled library, or repeat it to constrain the authorities:

```bash
python3 ../../scripts/codex_history.py library list --json
python3 ../../scripts/codex_history.py library search '发布 决策' --from laptop-default --from desktop-default --deep --json
```

Treat each `library_matches` item as an independent provenance link. `duplicate_count` means the same normalized knowledge content and status was found in multiple libraries; it does not prove that two broader scopes or projects are identical. Importing, merging, or synchronizing belongs to `$build-codex-history`, not this read-only Skill.

## Evidence Discipline

Similarity is retrieval, not truth. Distinguish `verified`, `executed`, `reported_outcome`, `planned`, `failed`, `blocked`, `uncertain`, and `mixed`. An assistant message can document a claim without independently verifying it. Prefer exact tool output and Evidence occurrences for high-impact conclusions.

SQLite controls scope, status, time, versions, claims, relations, and provenance. Chroma only contributes candidates. Even a verified historical row can be stale relative to the live repository, issue tracker, runtime, or deployment; inspect current state for present-tense assertions.

Read [references/schema.md](references/schema.md) when interpreting fields, time semantics, artifact URIs, relations, or trace output.
