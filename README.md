# cancer-session-rescue

[English](README.md) | [简体中文](README.zh-CN.md)

**Diagnose and rescue "cancer sessions" — agent conversation logs that bloated to hundreds of MB and freeze the app every time you open them.**

Works with any harness that stores sessions as JSONL. Ships with adapters for **WorkBuddy** and **OpenAI Codex CLI/Desktop**, plus a heuristic `generic` adapter for everything else.

```
Before:  rollout-xxx.jsonl   355.6 MB   ← opening this file froze the app
After:     same file          34.0 MB   ← opens fine, conversation intact
```

## Why sessions become cancerous

Long agent sessions accumulate machine-produced bulk that gets fully reloaded into the model context every time you reopen the session:

| Toxin | Typical share | Effect |
|---|---|---|
| base64 screenshots | 60–90% in visual workflows | Explodes multimodal context |
| Verbose tool outputs (verification loops, long command echoes) | 10–90% | Slowly starves the context window |
| Repeated patch-fix-verify cycles | — | Compounds both above |

The actual human–agent conversation is usually **less than 5%** of the file.

## Install

```bash
pip install git+https://github.com/<you>/cancer-session-rescue.git
# or from a clone:
pip install -e .
```

Requires Python 3.8+. No third-party dependencies.

## Quick start

```bash
# 1. Triage: find the cancerous sessions
csr triage codex            # scans ~/.codex/sessions and ~/.codex/archived_sessions
csr triage workbuddy        # scans ~/.workbuddy/projects
csr triage generic --dir /path/to/any/jsonl/dir

# 2. Slim a single session (dry-run by default — nothing is written)
csr slim codex ~/.codex/sessions/2026/07/15/rollout-xxx.jsonl --sanitize

# 3. Apply for real (original file is renamed and kept)
csr slim codex rollout-xxx.jsonl --apply --sanitize

# 4. Or batch-slim everything above 2MB in one go
csr slim-dir codex --apply --sanitize
```

Exit-code-safe design: compressed output is **validated line-by-line as JSON before it replaces the original**. If validation fails, nothing changes. The original is always kept as `*.replaced-<timestamp>` (or `*.bak-*MB` with `--backup`).

## What it does

Two compression layers, applied to every record:

1. **Strip images** — every `data:image` / base64 blob becomes `[image stripped, was N KB]`
2. **Truncate tool traffic** — tool outputs above 400 chars are cut to 200 + a size note; tool call arguments above 600 chars are cut to 300. Message records are **never truncated** — the conversation stays fully readable.
3. **Strip reasoning text** (`--deep`, recommended) — model thinking text hides in *three* places, none of which are conversation content: `message.content[].type=="reasoning"` blocks, nested `providerData.reasoning`, and `rawContent` fields. Found in the field: 1.41MB + 1.19MB per session on WorkBuddy, 428 occurrences in one 15.7MB file.

Optional **sanitization** (`--sanitize`): rewrite `C:\Users\<name>\...` / `/Users/<name>/...` paths to `~` so a session file can be shared as a bug report without leaking your username. 910 rewrites on our 355MB test file, zero afterwards.

### CRLF safety (Layer 4)

On Windows, **a large share of session files use CRLF line endings** (25 of 39 large files on our test machine). Naively re-joining lines with `"\n".join(...)` produces mixed line endings and corrupted files — we learned this the hard way (first rewrite broke 1,903 lines). The engine now detects the original line-ending style per file and restores it on write, verified: CRLF in → CRLF out, 0 bare LF.

## Safety model

| Guarantee | How |
|---|---|
| Original never destroyed | rename-then-replace; originals kept as `.replaced-*` |
| No silent corruption | output parsed line-by-line before swap; abort on any bad line |
| Live sessions untouched | files modified within the last 2 minutes are skipped in batch mode |
| Conversation fidelity | message-type records are never truncated by the tool layer |
| Dry-run first | `slim` without `--apply` writes nothing |

## Harness adapters

| Harness | Status | Session location | Notes |
|---|---|---|---|
| **WorkBuddy** | battle-tested | `~/.workbuddy/projects/**/*.jsonl` | Includes `stop` subcommand for killing live session registries |
| **Codex CLI/Desktop** | battle-tested | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | Handles `response_item` / `payload.type` nesting, `custom_tool_call_output`, `compacted` records; ordinal order preserved so `codex resume` still works |
| **generic** | heuristic | any `.jsonl` | Classifies records by type-name keywords; truncates the longest string field |

Writing a new adapter is ~60 lines: implement `classify()`, `truncate_tool_record()`, and `privacy_rules()`. See `src/cancer_session_rescue/adapters.py`.

## Real-world results (2026-10, one Windows machine)

| Harness | Files slimmed | Before | After | Freed |
|---|---|---|---|---|
| WorkBuddy | 37 (11 cancer-grade ≥10MB) | 374 MB | 111 MB | 263 MB |
| Codex | 1 (worst single file) | 355.6 MB | 34.0 MB | −91% |

The 90.9MB WorkBuddy session contained 390 base64 images and ~90MB of Playwright verification output. The 355MB Codex rollout contained 667 images and 890 oversized tool calls. With v0.2.0 `--deep`, the 90.9MB session drops further to **4.4MB (96%)** — the reasoning strip alone removed 68 blocks the two-layer version couldn't touch.

## Field manual: the habits that prevent cancer sessions

1. **Don't let the agent screenshot repeatedly** — base64 is the biggest toxin. Have it run scripts and report text; you look at the browser yourself.
2. **Validate check scripts before running verify loops** — one session died in a loop checking a DOM element that didn't exist ("verification never passes" fake-stuck).
3. **Switch sessions when one gets slow** — carry over a summary, not the whole history.
4. **Read-only triage first** — never load a giant session to "just look at it"; use `csr triage` which streams and never loads content.

## FAQ

**Will Codex still resume the slimmed session?**
Yes — record order (`ordinal`) and all message/reasoning records are preserved. We slim the payload of tool traffic, not the ledger structure. Verified on 16,761-record rollouts.

**Can I run this on sessions while the app is open?**
Batch mode skips files modified in the last 2 minutes. For a single file, close the session first — the `stop` subcommand (WorkBuddy) releases the file handle by archiving the session's runtime registry entry.

**Does sanitization remove secrets too?**
It rewrites username paths by default. Add rules for tokens/emails by extending `privacy_rules()` in your adapter before publishing any file.

## License

MIT
