---
name: restore-codex-session
description: Restore one original historical Codex thread from a Codex History Suite profile as a new native Codex session that can be resumed on the current device. Use when the user wants to reopen, import, recover, fork, or continue an exact old conversation from a local or transferred knowledge base instead of starting from a summary. This Skill creates a new session but never modifies the source knowledge base or an existing target thread.
---

# Restore Codex Session

Restore a canonical transcript snapshot through the target Codex app-server as an independent native fork. Resolve `../../scripts/codex_history.py` relative to this Skill directory and run it with Python 3.11 or newer. The operation makes no model or embedding calls.

## Workflow

1. Identify one exact source thread. Use `conversation QUERY --list --json` when the user supplied a title, topic, or ambiguous fragment.
2. Run `restore THREAD_ID --dry-run --json` first. Review the source title and activity range, materialized size, image report, target `CODEX_HOME`, Codex version, restored working directory, and warnings.
3. Choose a valid current working directory with `--cwd` when the historical path is absent or belongs to another device. Historical absolute paths remain inside the transcript as provenance; the restored thread's active working directory is the new path.
4. If the dry-run selected the intended thread and has no unaccepted size or path warning, run the same command without `--dry-run`.
5. Confirm `status=complete`, a native thread ID, rollout path, verified turn count, and restore manifest. Give the user the returned `codex://threads/ID` link and `codex resume ID` command.

## Commands

```bash
python3 ../../scripts/codex_history.py conversation '会话标题关键词' --list --json
python3 ../../scripts/codex_history.py restore THREAD_ID --dry-run --json
python3 ../../scripts/codex_history.py restore THREAD_ID --cwd /current/project --json

# Restore from an imported device profile into a specific Codex installation.
python3 ../../scripts/codex_history.py --profile laptop-default restore THREAD_ID \
  --codex-home ~/.codex --codex-bin /path/to/codex --cwd /current/project --json
```

On Windows use `py -3` and native Windows paths. Run the command in the same operating environment as the target Codex installation: Windows Python for a Windows Codex home, WSL Python for a WSL Codex home, and likewise on macOS or Linux.

## Transcript And Image Policy

The materialized canonical JSONL preserves original timestamps, user and assistant messages, tool calls and outputs, Goal events, compact records, and other source events. The target Codex app-server then owns the native fork: it writes current session metadata and can omit rolled-back, aborted, or unsupported records from the active native view. The restore manifest compares source and native line counts, hashes, and turn counts; the knowledge base remains the authority for records that are not active in the resumed thread.

The default `--image-mode deduplicated` restores the first occurrence of each captured image and replaces repeated occurrences with a SHA-256 placeholder, preventing the inline-base64 multiplication that can make long sessions unopenable. Defaults allow 2 MiB per decoded image, 25 MiB combined images, and a 256 MiB materialized transcript.

Use `--image-mode none` for a text-only native thread. Use `--image-mode all` only after reviewing its larger dry-run. `stored` preserves Codex History artifact URIs, which native Codex cannot render without the knowledge base. Missing or oversized images become traceable text placeholders; they remain available through `$codex-history` when their CAS payload is present.

## Safety Boundaries

- Never append restored events to an already-running thread and never rewrite an existing rollout. A restore always creates a new native fork with a new Codex thread ID.
- Never edit the source knowledge base, canonical snapshots, or source transcripts.
- Do not skip dry-run for a transcript above 128 MiB, a fallback working directory, missing artifacts, or an unexpected target Codex home.
- A bundle exported with `--artifacts none` can restore text and tool history but cannot re-inline image payloads.
- Native path-based fork support is version-sensitive. If the target Codex app-server rejects it, report the exact Codex version and error; do not hand-edit `state_*.sqlite` as a fallback.
- The audit manifest under `<CODEX_HOME>/codex-history-restores/` records the source thread, normalized transcript hash, target settings, and resulting native thread ID.

If the profile lacks canonical snapshots, stop and direct the user to `$build-codex-history` to run the appropriate baseline hydration or rebuild. Retrieval and HTML evidence export remain responsibilities of `$codex-history`; profile import, update, and repair remain responsibilities of `$build-codex-history`.
