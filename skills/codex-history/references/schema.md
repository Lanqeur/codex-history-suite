# Query Schema

## Progressive Tiers

- `asset`: durable decisions, unresolved work, failures, capabilities, and preferences.
- `overview`: thread or branch-family narrative with claim spans.
- `ledger`: imported high-level ledgers from legacy databases.
- `fact_block`: one evidence-linked turn packet.
- `core`: exact user, assistant, tool-call, and tool-output text.

## Provenance

`knowledge.record_id` links through `knowledge_evidence` to global content-addressed Evidence. `evidence_occurrences` identifies thread, turn, source line, event timestamp, and byte interval. Portable builds resolve occurrences to `canonical_events.raw_json`; migrated databases may resolve legacy gzip evidence queues instead.

`codex-history-source://SOURCE_ID#line=N` is a portable source locator. `codex-history-artifact://sha256/DIGEST` identifies a CAS object independently of its original absolute path.

## Conversation Exports

`conversation` reconstructs normalized source events from snapshot chunk byte
offsets; it does not rebuild message text from FTS or summarized knowledge rows.
The `codex-history-conversation-export-v1` payload records the source ID, line,
byte interval, event ID, content SHA-256, thread, turn, timestamp, and source
event type for each exported item.

Codex transcripts can represent one visible user or assistant message twice as
both a `response_item` and an `event_msg`. The exporter prefers the visible
`event_msg`, merges artifact references found only in its matching response
item, and reports suppressed duplicates. Injected environment, permission,
Skill, app, and plugin contexts are excluded unless `--include-internal` is set.
Tool calls, tool outputs, and goal states are independent evidence and remain in
source order by default.

`--include-raw` embeds the complete normalized JSON event. Inline source images
have already been replaced by content-addressed artifact URIs; `--embed-images`
resolves available image objects and adds data URLs to make the export
self-contained. These switches can substantially increase output size.

The offline HTML uses pinned local Marked and DOMPurify builds to render
sanitized user/assistant Markdown and GFM tables. A pinned Mermaid runtime is
embedded only when the selected messages contain a fenced `mermaid` block, and
runs with `securityLevel: strict`. The viewer always offers a literal source
mode; tool, goal, internal-context, and raw canonical events are never passed
through the Markdown renderer.

## Time

- `occurred_start_at/end_at`: source-event time.
- `observed_at`: when direct evidence was observed.
- `asserted_at`: when the knowledge row was produced.
- `valid_from/to`: validity interval when known.
- `indexed_at`: index build time.

`overlaps` includes records crossing a window. `contained` requires the full occurrence interval inside it. `--as-of` selects the version valid at the requested time.

## Truth State

`verified` requires direct test or tool evidence. `executed` means an action happened without proving the intended outcome. `reported_outcome` is an assistant report. `planned`, `failed`, `blocked`, `uncertain`, and `mixed` preserve weaker or conflicting states.

Portable fresh builds auto-create only high-confidence `validates` relations for exact event transitions: a verified tool output with the same `call_id`, or a later completed state for the same goal objective. Automatic `contradicts`, `invalidates`, and `reopens` are disabled because they require claim-level valid-time adjudication. Migrated databases may contain review-only legacy relation types. Relations and model-generated claims remain navigation aids until traced; similarity scores never override Evidence or current live state.
