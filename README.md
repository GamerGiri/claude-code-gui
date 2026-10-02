# Claude Code GUI

A native desktop app for the **Claude Code CLI**. It speaks to Claude Code through
Anthropic's official [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)
(the stream-json protocol), so it is not a terminal wrapper: replies stream token by token, every tool
call renders as a card, and permission prompts are answered by Claude Code's *own*
permission engine.

Sessions, streaming, tools, permissions, skills, history, attachments — in one window.

```
+--------------------------------------------------------------+
| Claude Code   New session          Model  Mode  Effort  Stop |
| ------------- ----------------------------------------------- |
| + New session   [thinking]                                   |
|               Claude                                         |
| Sessions      Yeah, the naming committee had fun with that   |
|  . my repo    cost $0.31 - turns 1 - in 14 (+36071 cache)    |
|  x my repo      [ what model are you? ]                      |
|               Session ready - 32 tools - D:\projects\app     |
| Working dir   [ attach chips appear here ]                   |
|               [ Ask Claude to build, refactor, debug... ]    |
+--------------------------------------------------------------+
```

## Features

| | |
| --- | --- |
| **Streaming chat** | Token-level deltas, collapsed thinking blocks, markdown + fenced code |
| **Live tool cards** | Tool name, input JSON, running/done/failed state, tool results, subagent lineage |
| **Real permission prompts** | Allow once · Always allow `<rule>` (sends the CLI's own `updatedPermissions` back) · Deny with feedback Claude sees |
| **Sessions** | New, resume from history, delete; transcript replay on resume; last session restored on relaunch |
| **Skills & slash commands** | `/` autocomplete over built-ins, your `.claude/commands/*.md`, and installed skills |
| **Attachments** | Paste a screenshot, drag files in, or pick files; images go as real base64 blocks |
| **Context meter** | Live context-window breakdown (`get_context_usage`) |
| **Git rail** | `git status --porcelain` + `diff --stat`, and per-message file restore (`rewind_files`) |
| **Model / mode / effort** | Applied live via `set_model()` and `set_permission_mode()` |

## Requirements

* **Python 3.10+** (3.12/3.13 tested) — *not* the Microsoft Store / Python Manager `python` alias
* **Node.js 18+** with Claude Code installed: `npm install -g @anthropic-ai/claude-code`
* A signed-in CLI: `claude auth login`
* Windows 10+ with the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (present on current Windows)

Nothing is vendored here — no `.venv`, no `node_modules`, no build output. You create
your own environment in one step.

## Setup first (one time)

Run the setup script for your platform **before** launching. It creates a project-local
`.venv` and installs `pywebview`, `claude-agent-sdk` and `pillow` into it. Re-running is
safe; it reuses the existing venv.

### Windows

```bat
git clone <this-repo> && cd claude-code-gui
setup.bat
run.bat
```

Expected tail of `setup.bat`:

```
Using .venv\Scripts\python.exe
...
Successfully installed pywebview-6.2.1 claude-agent-sdk-0.2.163 pillow-12.3.0 ...
dependencies OK

Done. Launch the app with:  run.bat
```

`setup.bat` resolves a real interpreter (`py -3.13`, then `python`) and deliberately
avoids the Store alias, which is the usual source of `ModuleNotFoundError`.

### macOS / Linux

```bash
git clone <this-repo> && cd claude-code-gui
./setup.sh
./run.sh
```

`setup.sh` creates `.venv`, installs `requirements.txt`, and on Linux additionally tries
`pywebview[qt]` (`PyQt6-WebEngine`) for a browser engine. Screenshot paste is Windows-only;
everything else works cross-platform.

If you prefer to do it by hand:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python.exe
.venv/bin/python app.py
```

`run.bat` / `run.sh` prefer `.venv` when present and print a tip pointing at the setup
script when it is missing.

## Usage

1. Set the **working directory** — click `⋯` (native folder browser) or type a path and press Enter.
2. Pick **model**, **permission mode**, **effort**.
3. **+ New session**, or click a session in the sidebar to resume it.
4. Ask something. Tool calls appear as collapsible cards; permission requests open a modal.
5. **Context** opens the live context-window panel. The right rail shows git changes; each of your messages can restore files to that checkpoint.
6. Hover a session and click `×` to delete it (removes its transcript from `~/.claude/projects/`).

## Slash commands

Print/stream-json mode does not expand slash commands ([CLI issue #4184](https://github.com/anthropics/claude-code/issues/4184)), so the GUI implements them. Type `/` for autocomplete: **Tab** / **↑↓** to move, **Enter** to accept, **Esc** to dismiss.

| Kind | Behaviour | Examples |
| --- | --- | --- |
| `local` | handled in-app via SDK calls | `/model opus`, `/plan`, `/permissions acceptEdits`, `/context`, `/clear`, `/rewind`, `/skills`, `/help` |
| `expand` | reads `.claude/commands/<name>.md`, substitutes `$ARGUMENTS` / `$1…$9`, sends as a prompt | your custom commands |
| `skill` | invokes an installed skill by name | `/design-taste-frontend refactor the settings page` |
| `forward` | passed through to the CLI, best effort | `/compact`, `/init`, `/code-review`, `/doctor` |

Skills are discovered from `<project>/.claude/skills`, `~/.claude/skills`, and
`~/.claude/plugins/**/skills/*/SKILL.md`. Sessions start with `skills="all"` plus
`setting_sources=[user, project, local]`, so the CLI's own `Skill` tool is live. `/skills`
lists what was found; the sidebar shows the count.

## Chat history

The CLI writes transcripts to `~/.claude/projects/`; the GUI reads them back.

* Clicking a session resumes it (`resume=<id>`) **and** replays its stored transcript (`get_session_messages`).
* The live session id and directory persist in `~/.claude-gui/settings.json`, so relaunching restores your last project, replays the transcript, and the first prompt you send re-attaches to that session.
* **+ New session** starts fresh in the same directory.

## Attachments

Three routes, all shown as removable chips under the composer and as thumbnails in your sent bubble:

| Input | How it works |
| --- | --- |
| **Ctrl+V** an image / screenshot | JS `paste` handler; when WebView2 has no File (raw bitmaps), Python reads the Win32 clipboard (`CF_HDROP`, then `CF_DIBV5`/`CF_DIB`) and re-encodes to PNG with Pillow |
| **Drag files in** | HTML5 `drop` → base64 → `attach_blob` |
| **📎 Files** | Win32 multi-select dialog returning **real absolute paths** |

What reaches Claude Code:

* images → real base64 `image` content blocks (`.png/.jpg/.jpeg/.gif/.webp/.bmp`, ≤ 5 MB)
* text/code files → inlined as fenced blocks, visible to any model
* other binaries → referenced by absolute path, with a note for Claude to Read them

Dropped/pasted files arrive as blobs (WebView2 exposes no filesystem path for drops), so
use **📎 Files** when Claude must open a file by path. Pillow is optional: without it,
pasted screenshots are refused with an explicit message and everything else keeps working.

## Architecture

```
app.py  ── asyncio worker thread ──> ClaudeSDKClient (stream-json over stdio)
   │            can_use_tool callback ──> permission modal ──> PermissionResultAllow/Deny
   └── Hub (event queue) <── JS polls every 120 ms ── ui/app.js renders
```

| File | Role |
| --- | --- |
| `app.py` | Backend: session manager, event hub, permission bridge, discovery worker, JS API |
| `ui/index.html` | Layout: sidebar, transcript, composer, git rail, permission modal |
| `ui/styles.css` | Dark theme (clay accent), streaming and tool-card states |
| `ui/app.js` | Renderer + event pump: deltas, cards, permissions, slash menu, attachments |
| `setup.bat` / `setup.sh` | One-time setup: create `.venv`, install dependencies |
| `run.bat` / `run.sh` | Launchers; prefer `.venv` when present |

### Protocol mapping

| UI action | Claude Code capability |
| --- | --- |
| Send / follow-up | `ClaudeSDKClient.query()` (streaming input) |
| Stop | `interrupt()` |
| Model / mode | `set_model()`, `set_permission_mode()` |
| Permission modal | `can_use_tool` → `PermissionResultAllow/Deny` + `updatedPermissions` |
| Context panel | `get_context_usage()` |
| Restore files | `rewind_files(user_message_id)` (needs `enable_file_checkpointing`) |
| Session list / resume | `list_sessions()`, `resume=<session_id>` |
| Delete session | `delete_session()` — JSONL transcript + subagent dir |
| History replay | `get_session_messages()` |
| Attach image / file | user message with base64 `image` content blocks |

## Troubleshooting

| Symptom | Cause & fix |
| --- | --- |
| `ModuleNotFoundError: No module named 'webview'` | You launched the Microsoft Store `python` alias. Run `setup.bat`, then `run.bat`. The app prints this guidance itself when a dependency is missing. |
| Window title says *Not responding* | Long calls must not run on the UI thread. All discovery (`auth status`, session scan, git) runs in a background worker; every `js_api` call returns in ~1 ms. |
| Folder / Files button does nothing | Both pickers run on their own thread with COM initialized. pywebview calls `js_api` methods on a worker thread, and the Explorer-style dialogs (folder browser, `GetOpenFileNameW` with `OFN_EXPLORER`) are built on COM — without an apartment they fail instantly and **no window is ever created**. The app owns the thread, calls `CoInitializeEx`, and reports real failures as a toast. Typing a path + Enter always works. |
| Only thinking, no answer | Text blocks were dropped: SDK `TextBlock`/`ThinkingBlock` carry **no** `.type` attribute, so match on `isinstance`. A `result` safety net renders the final text if nothing streamed. |
| `Not logged in · Please run /login` | Run `claude auth login` in a terminal. Credentials are per-interpreter/per-user, not inherited by every launch. |
| Skills missing | Sessions need `skills="all"` **and** `setting_sources`; discovery scans project, user, and plugin skill dirs. Check `/skills`. |

## Notes & limits

* `bypassPermissions` is opt-in; **plan** mode keeps Claude read-only.
* Cost/usage figures are the CLI's client-side estimates.
* Effort is a launch-time option — changing it restarts the session.
* The app resolves the native CLI binary itself (npm `.cmd` shims can't be spawned by the SDK) and passes it as `cli_path`.
* Everything is local: no server, no telemetry. Settings live in `~/.claude-gui/`; attachments staged in `~/.claude-gui/attachments/`.

## Contributing

Issues and PRs welcome. Keep `js_api` methods non-blocking (queue slow work in the
worker), match SDK blocks with `isinstance`, and prefer the CLI's own mechanisms over
reimplementing them. Run `python -m py_compile app.py && node --check ui/app.js` before
a PR.

## License

MIT — see [LICENSE](LICENSE).
