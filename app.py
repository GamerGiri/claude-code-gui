"""Claude Code GUI - a native desktop front-end for the Claude Code CLI.

Speaks to Claude Code through the official Agent SDK (stream-json protocol):
real token streaming, live tool events, and permission prompts that are
answered by the CLI's own permission engine (allow / deny / updatedPermissions).
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import dataclasses
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

# Dependencies are checked explicitly so a missing package produces an
# actionable message instead of a bare ModuleNotFoundError. This machine has
# several interpreters (Store stub, Python313, uv); run.bat uses .venv.
_DEPS_HELP = (
    "Claude Code GUI is missing a dependency.\n"
    "Run once:      setup.bat        (creates .venv and installs pywebview + claude-agent-sdk)\n"
    "Then launch:   run.bat\n"
    "Or into the interpreter you just used:\n"
    "               python -m pip install pywebview claude-agent-sdk"
)

try:
    import webview
except ImportError as exc:
    raise SystemExit(f"{_DEPS_HELP}\n\nimport failed: {exc}")

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        ServerToolUseBlock,
        StreamEvent,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
except ImportError as exc:
    raise SystemExit(f"{_DEPS_HELP}\n\nimport failed: {exc}")

# Pillow re-encodes clipboard bitmaps to PNG. Optional: without it, pasted
# screenshots are refused with a clear message and file/text attaching still works.
try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

# PermissionUpdate is top-level in 0.2.x; PermissionRuleValue only lives in
# claude_agent_sdk.types, so import defensively across SDK versions.
from claude_agent_sdk.types import PermissionRuleValue

try:
    from claude_agent_sdk import PermissionUpdate  # type: ignore
except ImportError:  # pragma: no cover
    from claude_agent_sdk.types import PermissionUpdate

try:  # present in 0.2.x; keep the app alive if a build lacks them
    from claude_agent_sdk import RateLimitEvent, TaskProgressMessage
except Exception:  # pragma: no cover
    RateLimitEvent = None
    TaskProgressMessage = None

APP_NAME = "Claude Code GUI"
APP_DIR = Path.home() / ".claude-gui"
SETTINGS_FILE = APP_DIR / "settings.json"
UI_DIR = Path(__file__).resolve().parent / "ui"

MODELS = ["default", "opus", "sonnet", "haiku", "fable"]
MODES = [
    ("default", "Ask for approval"),
    ("acceptEdits", "Auto-accept edits"),
    ("plan", "Plan mode (read-only)"),
    ("bypassPermissions", "Bypass permissions"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
PERM_MODES = {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}

# Slash commands are a property of the interactive CLI: print/stream-json mode does
# not expand them (CLI issue #4184). The GUI therefore implements them itself:
#   local   -> handled by the app (maps onto SDK client calls)
#   expand  -> read .claude/commands/<name>.md and send the expanded prompt
#   forward -> pass the text through to the CLI as-is, best effort
BUILTIN_COMMANDS = [
    ("model", "[alias]", "Switch model live", "local"),
    ("effort", "<level>", "Set reasoning effort (restarts session)", "local"),
    ("plan", "", "Plan mode: read-only", "local"),
    ("permissions", "[mode]", "Set permission mode", "local"),
    ("context", "", "Show context-window usage", "local"),
    ("usage", "", "Show context-window usage", "local"),
    ("clear", "", "Fresh conversation in this project", "local"),
    ("new", "", "Fresh conversation in this project", "local"),
    ("reset", "", "Fresh conversation in this project", "local"),
    ("resume", "", "Refresh the session list", "local"),
    ("sessions", "", "Refresh the session list", "local"),
    ("rewind", "", "Restore files to the last prompt", "local"),
    ("stop", "", "Interrupt the current turn", "local"),
    ("help", "", "List slash commands", "local"),
    ("skills", "", "List installed skills", "local"),
    ("compact", "[instructions]", "Summarize context (forwarded to CLI)", "forward"),
    ("init", "", "Generate CLAUDE.md (forwarded)", "forward"),
    ("review", "[target]", "Review a diff (forwarded)", "forward"),
    ("code-review", "[level]", "Review the diff (forwarded)", "forward"),
    ("security-review", "", "Security review (forwarded)", "forward"),
    ("doctor", "", "Setup diagnostics (forwarded)", "forward"),
    ("mcp", "", "MCP servers (forwarded)", "forward"),
    ("agents", "", "Subagents (forwarded)", "forward"),
    ("memory", "", "Edit CLAUDE.md memory (forwarded)", "forward"),
]

MAX_TEXT = 24_000          # cap tool-result payloads sent to the UI
HOME_DIR = Path.home()     # resolved from USERPROFILE when available

# ---- attachments -------------------------------------------------------- #
ATTACH_DIR = APP_DIR / "attachments"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".css", ".html", ".htm", ".xml", ".csv", ".tsv", ".sql", ".sh",
    ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".java", ".kt", ".swift", ".m", ".mm", ".vue", ".svelte", ".scss", ".less", ".env",
    ".lock", ".log", ".diff", ".patch", ".gitignore", ".dockerfile", ".makefile", ".pl",
}
MAX_IMAGE_BYTES = 5_000_000     # Anthropic image payload ceiling (base64 on disk)
MAX_INLINE_BYTES = 120_000      # text files inlined into the prompt
MAX_ATTACHMENTS = 8
PREVIEW_MAX_BYTES = 400_000     # images at most this size get a UI thumbnail
# System subtypes that are pure bookkeeping and arrive many times per turn.
NOISY_SYSTEM_SUBTYPES = {"thinking_tokens", "turn_duration", "local_command_output"}
TRUNC = "\n... [truncated by GUI] ..."


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clip(s: str, n: int = MAX_TEXT) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + TRUNC


def _native_from_shim(shim: Path) -> Optional[str]:
    """npm installs a .cmd shim that points at bin/claude.exe. The Agent SDK
    refuses to spawn .bat/.cmd files, so resolve the real binary when present."""
    pkg = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    for name in ("claude.exe", "claude"):
        p = pkg / name
        if p.exists():
            return str(p)
    return None


def find_claude() -> Optional[str]:
    """Locate the Claude Code executable; also make sure node is on PATH."""
    for d in (
        r"C:\Program Files\nodejs",
        os.path.expandvars(r"%APPDATA%\npm"),
        str(Path.home() / ".npm-global/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ):
        if Path(d).exists() and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = os.environ["PATH"] + os.pathsep + d

    candidates: list[Path] = []
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    for cand in (
        Path(os.path.expandvars(r"%APPDATA%")) / "npm" / "claude.cmd",
        Path(os.path.expandvars(r"%APPDATA%")) / "npm" / "claude",
        Path.home() / ".local/bin/claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        candidates.append(cand)
    candidates.extend(Path.cwd().glob("*/npm/claude.cmd"))  # sandboxed npm bins

    for c in candidates:
        if not c.exists():
            continue
        native = _native_from_shim(c) if c.suffix.lower() in (".cmd", ".bat") else None
        if native:
            return native
        if os.name == "nt" and c.suffix.lower() in (".cmd", ".bat"):
            continue  # SDK will not spawn batch scripts
        return str(c)
    for c in candidates:  # last resort: the shim itself
        if c.exists():
            return str(c)
    return None


def tool_result_text(block: ToolResultBlock) -> str:
    c = block.content
    if isinstance(c, str):
        return c
    parts = []
    for item in c or []:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append("[image]")
        else:
            t = getattr(item, "type", None)
            if t == "text":
                parts.append(getattr(item, "text", ""))
    return "\n".join(parts)


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        log(f"settings save failed: {exc}")


def auth_status() -> dict:
    exe = find_claude()
    if not exe:
        return {"loggedIn": False, "installed": False, "authMethod": "none"}
    try:
        out = subprocess.run(
            [exe, "auth", "status"], capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (out.stdout or "").strip()
        data = json.loads(raw[raw.find("{"):]) if "{" in raw else {}
    except Exception as exc:  # pragma: no cover
        return {"loggedIn": False, "installed": True, "error": str(exc)}
    data["installed"] = True
    return data


def _ui_blocks_from_raw(content: Any) -> list:
    """Convert raw transcript content (str or block dicts) into UI block dicts."""
    out = []
    if isinstance(content, str):
        if content.strip():
            out.append({"type": "text", "text": clip(content, 8000)})
        return out
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            out.append({"type": "text", "text": clip(b["text"], 8000)})
        elif t == "thinking" and b.get("thinking", "").strip():
            out.append({"type": "thinking", "text": clip(b["thinking"], 4000)})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.get("id", ""),
                        "name": b.get("name", "tool"),
                        "input": clip(json.dumps(b.get("input", {}), indent=2,
                                                 ensure_ascii=False), 4000)})
        elif t == "tool_result":
            c = b.get("content")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            out.append({"type": "tool_result", "toolUseId": b.get("tool_use_id", ""),
                        "text": clip(str(c or ""), 6000), "isError": bool(b.get("is_error"))})
    return out


def history_payload(session_id: str, cwd: Optional[str]) -> dict:
    """Replay a stored session's transcript so the chat window is not empty."""
    try:
        from claude_agent_sdk import get_session_messages
        msgs = get_session_messages(session_id, directory=cwd or None, limit=200)
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc), "items": []}
    items = []
    for m in msgs:
        raw = m.message if isinstance(m.message, dict) else {}
        blocks = _ui_blocks_from_raw(raw.get("content"))
        if blocks:
            items.append({"role": m.type, "uuid": m.uuid, "blocks": blocks})
    return {"sessionId": session_id, "items": items}


def _skill_frontmatter(text: str) -> dict:
    meta, _ = _frontmatter(text)
    return meta


def skills_payload(cwd: Optional[str]) -> list:
    """Discover installed skills: project, user (~/.claude/skills), and plugins.

    Home is resolved from USERPROFILE first: some sandboxes remap Path.home().
    """
    found: dict = {}
    global HOME_DIR
    HOME_DIR = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())

    def scan(d: Path, source: str) -> None:
        if not d.is_dir():
            return
        for sub in sorted(p for p in d.iterdir() if p.is_dir()):
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            try:
                meta = _skill_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # pragma: no cover
                meta = {}
            name = (meta.get("name") or sub.name).strip().lower()
            if not name or name in found:
                continue
            found[name] = {
                "name": name,
                "args": "[args]",
                "desc": (meta.get("description") or "Installed skill")[:160],
                "kind": "skill",
                "source": source,
            }

    if cwd:
        scan(Path(cwd) / ".claude" / "skills", "project")
    scan(HOME_DIR / ".claude" / "skills", "user")
    plug = HOME_DIR / ".claude" / "plugins"
    for md in sorted(plug.glob("*/plugins/*/skills/*/SKILL.md")):
        source = f"plugin:{md.parents[2].name}"
        scan_dir = md.parent
        try:
            meta = _skill_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
        except Exception:  # pragma: no cover
            meta = {}
        name = (meta.get("name") or scan_dir.name).strip().lower()
        if name and name not in found:
            found[name] = {"name": name, "args": "[args]",
                           "desc": (meta.get("description") or "Plugin skill")[:160],
                           "kind": "skill", "source": source}
    for md in sorted(plug.glob("*/external_plugins/*/skills/*/SKILL.md")):
        try:
            meta = _skill_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
        except Exception:  # pragma: no cover
            meta = {}
        name = (meta.get("name") or md.parent.name).strip().lower()
        if name and name not in found:
            found[name] = {"name": name, "args": "[args]",
                           "desc": (meta.get("description") or "Plugin skill")[:160],
                           "kind": "skill", "source": "plugin"}
    return sorted(found.values(), key=lambda x: x["name"])


def _clipboard_dib_bytes(fmt: int) -> bytes:
    """Raw CF_DIB / CF_DIBV5 payload from the clipboard. No threads, no pythonnet."""
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    k.GlobalSize.restype = ctypes.c_size_t
    k.GlobalSize.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(0):
        return b""
    try:
        h = u.GetClipboardData(fmt)
        if not h:
            return b""
        n = int(k.GlobalSize(h))
        ptr = k.GlobalLock(h)
        if not ptr or not n:
            return b""
        try:
            return ctypes.string_at(ptr, n)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def _dib_to_png(dib: bytes) -> Optional[bytes]:
    """Wrap a DIB in a BITMAPFILEHEADER so Pillow can decode it, then emit PNG.

    CF_DIBV5 carries a 124-byte header with bitfields; screenshots are effectively
    32-bit BGRA, so the header is normalized to a plain 40-byte BI_RGB header that
    Pillow decodes cleanly.
    """
    try:
        if len(dib) < 40:
            return None
        bi_size = int.from_bytes(dib[0:4], "little")
        if bi_size < 40 or bi_size > len(dib):
            bi_size = 40
        header = bytearray(dib[:bi_size])
        w_px = int.from_bytes(header[4:8], "little", signed=True)
        h_px = int.from_bytes(header[8:12], "little", signed=True)
        bpp = int.from_bytes(header[14:16], "little")
        if w_px < 2 or abs(h_px) < 2 or w_px > 8192 or abs(h_px) > 8192:
            return None
        if bpp not in (1, 4, 8, 24, 32):
            return None
        header[0:4] = struct.pack("<I", 40)          # normalize to BITMAPINFOHEADER
        header[16:20] = b"\x00\x00\x00\x00"          # biCompression = BI_RGB
        header[32:36] = b"\x00\x00\x00\x00"          # biClrUsed
        if bpp == 32:
            header[14:16] = struct.pack("<H", 24)    # drop the unused alpha byte
        n_colors = int.from_bytes(header[32:36], "little")
        pal_len = ((n_colors or (1 << bpp)) * 4) if bpp <= 8 else 0
        stride = ((abs(h_px) * bpp + 31) // 32) * 4
        need = pal_len + w_px * stride
        body = dib[bi_size:bi_size + need]
        if len(body) < need:                         # tolerate truncated payloads
            body += b"\x00" * (need - len(body))
        bmp = (b"BM" + struct.pack("<I", 14 + 40 + len(body)) + b"\x00\x00\x00\x00"
               + struct.pack("<I", 40) + bytes(header[:40]) + body)
        img = Image.open(io.BytesIO(bmp))
        img.load()
        out = io.BytesIO()
        img.convert("RGBA").save(out, "PNG")
        data = out.getvalue()
        return data if 0 < len(data) <= MAX_IMAGE_BYTES else None
    except Exception:
        return None


def _clipboard_image_files() -> list:
    """CF_HDROP entries on the clipboard (Snipping Tool / Explorer copy)."""
    u = ctypes.windll.user32
    shell = getattr(ctypes.windll, "shell32", None)
    paths: list = []
    CF_HDROP = 15
    try:
        if not shell or not u.IsClipboardFormatAvailable(CF_HDROP):
            return []
        if not u.OpenClipboard(0):
            return []
        try:
            u.GetClipboardData.restype = ctypes.c_void_p
            u.GetClipboardData.argtypes = [ctypes.c_uint]
            shell.DragQueryW.restype = ctypes.c_uint
            shell.DragQueryW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_wchar_p, ctypes.c_uint]
            h = u.GetClipboardData(CF_HDROP)
            if not h:
                return []
            count = int(shell.DragQueryW(h, 0xFFFFFFFF, None, 0))
            for i in range(min(count, MAX_ATTACHMENTS)):
                buf = ctypes.create_unicode_buffer(260)
                if int(shell.DragQueryW(h, i, buf, 260)):
                    paths.append(str(buf.value))
        finally:
            u.CloseClipboard()
    except Exception:
        return []
    return paths


def read_clipboard_image() -> dict:
    """Grab an image from the Windows clipboard (Ctrl+V of a screenshot/bitmap).

    WebView2 only yields a File for clipboard *files*, so raw bitmaps must be read
    from Win32 directly. CF_HDROP first (real paths), then CF_DIBV5/CF_DIB re-encoded
    by Pillow.
    """
    if os.name != "nt":
        return {"kind": "none", "error": "clipboard images require Windows"}

    for p in _clipboard_image_files():
        path = Path(p)
        if path.is_file() and path.suffix.lower() in IMG_EXTS:
            d = describe_attachment(str(path))
            if d.get("kind") == "image":
                return d

    u = ctypes.windll.user32
    CF_DIBV5, CF_DIB = 17, 8
    for fmt in (CF_DIBV5, CF_DIB):
        try:
            if not u.IsClipboardFormatAvailable(fmt):
                continue
        except Exception:
            continue
        dib = _clipboard_dib_bytes(fmt)
        if not dib:
            continue
        if Image is None:
            return {"kind": "none",
                    "error": "Pillow is not installed, so clipboard images are unavailable. "
                             "Fix: python -m pip install pillow"}
        png = _dib_to_png(dib)
        if not png:
            continue
        try:
            ATTACH_DIR.mkdir(parents=True, exist_ok=True)
            target = ATTACH_DIR / f"clipboard-{int(time.time()*1000)}.png"
            target.write_bytes(png)
        except Exception as exc:  # pragma: no cover
            return {"kind": "error", "error": str(exc)}
        d = describe_attachment(str(target))
        d["name"] = f"screenshot-{time.strftime('%H%M%S')}.png"
        if d.get("kind") == "image":
            return d
    return {"kind": "none"}


def build_prompt(text: str, attachments: Optional[list] = None) -> Any:
    """Turn text + attachments into the prompt payload the CLI expects.

    Images become real base64 image content blocks (the CLI accepts them on stdin
    in stream-json input mode). Text files are inlined as fenced blocks so any model
    can see them, and binary files are referenced by path for the Read tool.
    """
    items = [a for a in (attachments or []) if a]
    body = text.strip()
    if not items:
        return body

    images = [a for a in items if a.get("kind") == "image"]
    texts = [a for a in items if a.get("kind") == "text"]
    others = [a for a in items if a.get("kind") not in ("image", "text")]

    parts: list = []
    if body:
        parts.append({"type": "text", "text": body})

    notes = []
    for a in texts:
        lang = {".py": "python", ".js": "javascript", ".ts": "typescript", ".jsx": "jsx",
                ".tsx": "tsx", ".json": "json", ".md": "markdown", ".css": "css",
                ".html": "html", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
                ".sql": "sql", ".sh": "bash", ".ps1": "powershell"}.get(
                    Path(a["name"]).suffix.lower(), "")
        fence = "`" * 3
        notes.append(f"Attached file {a['path']} ({a.get('bytes', 0)} bytes):\n"
                     f"{fence}{lang}\n{a['text']}\n{fence}")
    for a in others:
        kind = "binary file" if a.get("kind") == "binary" else "unusable file"
        line = f"Attached {kind}: {a['path']} ({a.get('mime', '?')}, " \
               f"{a.get('bytes', 0)} bytes)."
        if a.get("kind") == "binary":
            line += " Read it with your tools if you need its contents."
        if a.get("error"):
            line += f" Note: {a['error']}"
        notes.append(line)

    label = "\n\n".join(notes)
    if images:
        # Text first, then image blocks; the CLI pairs them into one user turn.
        intro = (body + "\n\n" if body else "") + label if label else (body or "Attached files follow.")
        parts.append({"type": "text", "text": intro})
        for a in images:
            parts.append({"type": "image", "source": {
                "type": "base64", "media_type": a.get("mediaType") or "image/png",
                "data": a["data"],
            }})
    elif label:
        parts.append({"type": "text", "text": label})

    message = {
        "type": "user",
        "message": {"role": "user", "content": parts},
        "parent_tool_use_id": None,
    }
    return message


def looks_textual(path: Path) -> bool:
    """Extension first, then a cheap binary sniff for extension-less files."""
    if path.suffix.lower() in TEXT_EXTS:
        return True
    try:
        head = path.open("rb").read(2048)
    except Exception:  # pragma: no cover
        return False
    if b"\x00" in head:
        return False
    return sum(1 for c in head if 9 <= c <= 13 or 32 <= c <= 126) > len(head) * 0.85


def describe_attachment(path: str) -> dict:
    """Classify a file into an attachment the CLI can consume.

    images  -> base64 image content block
    text    -> inlined fenced code block (works with any model, no upload)
    binary  -> referenced by path only, so Claude can Read it with tools
    """
    try:
        p = Path(path)
        size = p.stat().st_size
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        ext = p.suffix.lower()
        base = {"name": p.name, "path": str(p), "mime": mime, "bytes": size}

        if ext in IMG_EXTS:
            if size > MAX_IMAGE_BYTES:
                return {**base, "kind": "error",
                        "error": f"{p.name} is {size/1e6:.1f} MB; images must be under "
                                 f"{MAX_IMAGE_BYTES/1e6:.0f} MB"}
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            return {**base, "kind": "image", "data": data,
                    "mediaType": mime if mime.startswith("image/") else f"image/{ext[1:]}"}

        if size <= MAX_INLINE_BYTES and looks_textual(p):
            text = p.read_text(encoding="utf-8", errors="replace")
            return {**base, "kind": "text", "text": text}

        return {**base, "kind": "binary"}
    except Exception as exc:  # pragma: no cover
        return {"name": str(path), "kind": "error", "path": str(path),
                "error": f"{type(exc).__name__}: {exc}"}


class _OFN(ctypes.Structure):
    """OPENFILENAMEW, laid out per the Windows SDK."""
    _fields_ = [
        ("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HINSTANCE),
        ("lpstrFilter", wt.LPCWSTR), ("lpstrCustomFilter", wt.LPWSTR),
        ("nMaxCustFilter", wt.DWORD), ("nFilterIndex", wt.DWORD),
        ("lpstrFile", wt.LPWSTR), ("nMaxFile", wt.DWORD),
        ("lpstrFileTitle", wt.LPWSTR), ("nMaxFileTitle", wt.DWORD),
        ("lpstrInitialDir", wt.LPCWSTR), ("lpstrTitle", wt.LPCWSTR),
        ("Flags", wt.DWORD), ("nFileOffset", wt.WORD), ("nFileExtension", wt.WORD),
        ("lpstrDefExt", wt.LPCWSTR), ("hCustData", wt.LPARAM),
        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wt.LPCWSTR),
    ]


def _parse_ofn_buffer(raw: str) -> list:
    """Decode the multi-select buffer: "dir\0name1\0name2\0\0", single = "path\0\0"."""
    parts = raw.split("\x00")
    if len(parts) >= 2 and parts[1] == "":
        return [parts[0]]
    base_dir = parts[0]
    return [str(Path(base_dir) / n) for n in parts[1:] if n]


def _run_open_dialog(flags: int, initial: Optional[str], title: str) -> dict:
    """GetOpenFileNameW on a dedicated STA thread with COM initialized.

    The Explorer-style dialog is built on IFileOpenDialog (COM). pywebview runs
    js_api calls on a worker thread that has no apartment, where the call fails
    immediately and no window is ever created - which looked like "the button does
    nothing". Owning the thread lets us initialize COM and report real errors.
    """
    out: dict = {"paths": [], "source": "cancelled"}

    def work() -> None:
        ole32 = ctypes.windll.ole32
        COINIT_APARTMENTTHREADED = 0x00000002
        try:
            # S_OK / S_FALSE are fine; RPC_E_CHANGED_MODE means an apartment exists
            ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        except Exception:  # pragma: no cover
            pass
        try:
            buf = ctypes.create_unicode_buffer(32768)
            ofn = _OFN()
            ofn.lStructSize = ctypes.sizeof(_OFN)
            ofn.hwndOwner = find_window_hwnd(APP_NAME)
            ofn.lpstrFilter = (
                "Images & text\0*.png;*.jpg;*.jpeg;*.gif;*.webp;*.txt;*.md;*.py;*.js;*.ts;"
                "*.jsx;*.tsx;*.json;*.css;*.html;*.csv;*.yaml;*.yml;*.toml;*.sql;*.sh;*.ps1;"
                "*.c;*.h;*.cpp;*.cs;*.go;*.rs;*.rb;*.java;*.vue;*.svelte\0All files\0*.*\0\0"
            )
            ofn.lpstrFile = ctypes.addressof(buf)
            ofn.nMaxFile = 32768
            ofn.lpstrInitialDir = initial or str(HOME_DIR)
            ofn.lpstrTitle = title
            ofn.Flags = flags

            ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
            err = ctypes.windll.comdlg32.CommDlgExtendedError()
            if not ok:
                # 0 means the user cancelled; anything else is a real failure
                out["source"] = "cancelled" if not err else f"error: CommDlgExtendedError=0x{err:04X}"
                return
            paths = [p for p in _parse_ofn_buffer(buf[:]) if Path(p).is_file()]
            out["paths"] = paths
            out["source"] = "win32"
        except Exception as exc:
            out["source"] = f"error: {type(exc).__name__}: {exc}"
        finally:
            try:
                ole32.CoUninitialize()
            except Exception:  # pragma: no cover
                pass

    t = threading.Thread(target=work, daemon=True, name="claude-gui-file-dialog")
    t.start()
    t.join(timeout=600)          # modal dialog blocks until the user answers
    if t.is_alive():
        return {"paths": [], "source": "error: dialog thread timed out"}
    return out


def pick_files_native(initial: Optional[str] = None,
                      title: str = "Attach files to this prompt") -> dict:
    """Win32 multi-select file dialog returning real absolute paths.

    Tries the modern Explorer dialog, then retries with the legacy (non-COM)
    common dialog if the COM-based one cannot be instantiated.
    """
    if os.name != "nt":
        return {"paths": [], "source": "unsupported"}

    ALLOWMULTISELECT, EXPLORER = 0x0002, 0x0040
    FILEMUSTEXIST, PATHMUSTEXIST, LONGNAMES, ENABLESIZING = 0x1000, 0x0800, 0x0008, 0x0200
    modern = ALLOWMULTISELECT | EXPLORER | FILEMUSTEXIST | PATHMUSTEXIST | LONGNAMES | ENABLESIZING
    legacy = ALLOWMULTISELECT | FILEMUSTEXIST | PATHMUSTEXIST | LONGNAMES   # no COM dialog

    res = _run_open_dialog(modern, initial, title)
    if not res["paths"] and str(res.get("source", "")).startswith("error"):
        res = _run_open_dialog(legacy, initial, title)
    return res


def find_window_hwnd(title: str) -> int:
    """HWND of our own window, so the picker opens modal to the app."""
    if os.name != "nt":
        return 0
    found = {"hwnd": 0}
    try:
        u = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, _lparam):
            n = u.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if title in (buf.value or "") and u.IsWindowVisible(hwnd):
                r = wt.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                if (r.right - r.left) > 400:      # the real window, not a helper
                    found["hwnd"] = int(hwnd)
            return True

        u.EnumWindows(proto(cb), 0)
    except Exception:  # pragma: no cover
        return 0
    return found["hwnd"]


def pick_folder_native(initial: Optional[str] = None,
                       title: str = "Choose a project folder") -> dict:
    """Shell folder browser on its own STA thread with a message pump.

    pywebview's WinForms dialog is created from the js_api worker thread and reads
    os.environ['HOMEPATH'] for its start directory; both are unreliable (the dialog
    can never appear, and a missing HOMEPATH raises), so use the shell browser.
    """
    result: dict = {"path": None, "source": "shell"}

    def work() -> None:
        try:
            import pythoncom
            import win32com.client as wc
            pythoncom.CoInitialize()
            owner = find_window_hwnd(APP_NAME)
            shell = wc.Dispatch("Shell.Application")
            # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX | BIF_VALIDATE
            opts = 0x0001 | 0x0040 | 0x0010 | 0x0020
            folder = shell.BrowseForFolder(owner, title, opts, initial or str(HOME_DIR))
            if folder is not None:
                self_folder = getattr(folder, "Self", None)
                path = getattr(self_folder, "Path", None) if self_folder else None
                if path:
                    result["path"] = str(path)
        except Exception as exc:
            result["source"] = f"error: {type(exc).__name__}: {exc}"
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=300)          # the dialog blocks until the user answers
    return result


def pick_folder_pywebview(initial: Optional[str] = None) -> dict:
    try:
        folder = getattr(webview, "FOLDER_DIALOG", 0)
        res = webview.create_file_dialog(folder, directory=initial or str(HOME_DIR))
        if isinstance(res, (list, tuple)):
            res = res[0] if res else None
        return {"path": res, "source": "pywebview"}
    except Exception as exc:  # pragma: no cover
        return {"path": None, "source": f"error: {type(exc).__name__}: {exc}"}


def layout_report(payload: str) -> None:
    """Persist the renderer's last layout measurement for diagnostics."""
    try:
        data = json.loads(payload)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "layout.json").write_text(json.dumps(data), encoding="utf-8")
        log(f"layout: composer={data.get('composer')} inView={data.get('inView')}")
    except Exception as exc:  # pragma: no cover
        log(f"layout report ignored: {exc}")


def _frontmatter(text: str) -> tuple[dict, str]:
    """Split a command file into (metadata, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"\'')
    return meta, text[end + 4:].lstrip("\n")


def command_dirs(cwd: Optional[str]) -> list:
    dirs = []
    if cwd:
        dirs.append(Path(cwd) / ".claude" / "commands")
    # HOME_DIR, not Path.home(): some sandboxes remap home to the cwd
    dirs.append(HOME_DIR / ".claude" / "commands")
    return dirs


def load_commands(cwd: Optional[str]) -> list:
    """Built-in commands plus the user's/project .claude/commands/*.md files."""
    out = {
        name: {"name": name, "args": args, "desc": desc, "kind": kind, "source": "built-in"}
        for (name, args, desc, kind) in BUILTIN_COMMANDS
    }
    for d in command_dirs(cwd):
        if not d.is_dir():
            continue
        source = "project" if cwd and d.parent.parent == Path(cwd).absolute() else "user"
        for f in sorted(d.glob("*.md")):
            name = f.stem.lower()
            try:
                meta, _ = _frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # pragma: no cover
                meta = {}
            out[name] = {
                "name": name,
                "args": "[args]",
                "desc": meta.get("description") or "Custom command",
                "kind": "expand",
                "source": source,
            }
    skills = skills_payload(cwd)
    for sk in skills:
        out[sk["name"]] = sk          # skills are invokable as /name
    cmds = sorted(out.values(), key=lambda x: (x["kind"] == "forward", x["name"]))
    log(f"commands: {len(cmds)} total, {len(skills)} skills discovered")
    return cmds


def expand_command(name: str, args: str, cwd: Optional[str]) -> dict:
    """Expand a custom slash command from .claude/commands/<name>.md."""
    name = (name or "").lstrip("/").lower()
    for d in command_dirs(cwd):
        f = d / f"{name}.md"
        if not f.is_file():
            continue
        _, body = _frontmatter(f.read_text(encoding="utf-8", errors="ignore"))
        body = body.replace("$ARGUMENTS", args or "")
        for i, tok in enumerate((args or "").split()[:9], start=1):
            body = body.replace(f"${i}", tok)
        return {"text": body.strip(), "name": name}
    return {"error": f"No such command: /{name} (checked project and user .claude/commands)"}


def git_snapshot(cwd: str) -> dict:
    git = shutil.which("git")
    if not git:
        return {"files": [], "stat": "", "branch": "", "error": "git not installed"}

    def run(args: list) -> str:
        try:
            r = subprocess.run(
                [git, *args], cwd=cwd, capture_output=True, text=True, timeout=8
            )
            return (r.stdout or "").strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    root = run(["rev-parse", "--show-toplevel"])
    porcelain = run(["status", "--porcelain"])
    files = []
    for line in porcelain.splitlines():
        if len(line) >= 4:
            files.append({"xy": line[:2].strip(), "path": line[3:].strip()})

    # A repo rooted at the user's home dir lists the entire profile as untracked.
    note = None
    home = str(Path.home()).rstrip("\\/").lower()
    if root and os.path.dirname(root).rstrip("\\/").lower() == home:
        note = "Git repo rooted at your home folder \u2014 open a project directory for a useful diff."
    elif len(files) > 60:
        note = f"{len(files)} changes in this repo \u2014 showing the first 60."

    return {
        "files": files[:60],
        "total": len(files),
        "stat": run(["diff", "--stat"])[:4000],
        "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]),
        "root": root,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# session manager (asyncio loop on a worker thread)
# --------------------------------------------------------------------------- #
class SessionManager:
    _serializable = False  # not exposed to JS

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.client: Optional[ClaudeSDKClient] = None
        self.cwd = str(Path.home())
        self.model = "default"
        self.mode = "default"
        self.effort = "medium"
        self.session_id: Optional[str] = None
        self.busy = False
        self.usage_busy = False
        self.pending: dict = {}     # permission request bookkeeping
        self.noisy: dict = {}       # counts of bookkeeping system messages
        self.last_user_text: Optional[str] = None
        self.on_session = None      # callback(session_id) -> persist for relaunch
        self.starting = False

    # ---- lifecycle ------------------------------------------------------- #
    def start_loop(self) -> None:
        """Create the loop synchronously, then run it on a worker thread.

        Creating it here (not inside the thread) removes the race where a fast
        first action found self.loop still None and crashed in submit().
        """
        if self.thread and self.thread.is_alive() and self.loop and not self.loop.is_closed():
            return
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="claude-gui-loop")
        self.thread.start()

    def _run_loop(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    def submit(self, coro) -> bool:
        """Queue a coroutine onto the worker loop; never blocks the caller."""
        if self.loop is None or self.loop.is_closed():
            try:
                coro.close()
            except Exception:
                pass
            log("submit ignored: event loop not running yet")
            return False
        asyncio.run_coroutine_threadsafe(coro, self.loop)
        return True

    # ---- session control ------------------------------------------------- #
    async def _options(self, resume: Optional[str]) -> ClaudeAgentOptions:
        # bypassPermissions auto-approves before the callback is consulted, so
        # registering it there only earns an SDK shadowing warning.
        gate = self._can_use_tool if self.mode != "bypassPermissions" else None
        return ClaudeAgentOptions(
            cwd=self.cwd,
            model=None if self.model == "default" else self.model,
            permission_mode=self.mode,
            effort=self.effort,
            include_partial_messages=True,
            enable_file_checkpointing=True,
            resume=resume,
            skills="all",              # let the CLI discover installed skills
            can_use_tool=gate,
            cli_path=find_claude(),
            env={"CLAUDE_CODE_ENTRYPOINT": "claude-gui"},
            setting_sources=["user", "project", "local"],
        )

    async def start(self, cwd: str, model: str, mode: str, effort: str,
                    resume: Optional[str] = None) -> None:
        if self.starting:
            return
        self.starting = True
        await self.stop(quiet=True)
        self.cwd = cwd
        self.model = model
        self.mode = mode
        self.effort = effort
        try:
            opts = await self._options(resume)
            self.client = ClaudeSDKClient(opts)
            await self.client.connect()
            self.hub.push("status", {"state": "ready", "cwd": cwd,
                                     "model": model, "mode": mode})
        except Exception as exc:
            self.client = None
            self.hub.push("status", {"state": "error", "message": str(exc)})
        finally:
            self.starting = False

    async def stop(self, quiet: bool = False) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        for pid in list(self.pending):
            self._resolve(pid, {"behavior": "deny", "message": "Session closed"})
        self.busy = False
        if not quiet:
            self.hub.push("status", {"state": "idle"})

    async def send(self, text: str, attachments: Optional[list] = None) -> None:
        if not self.client:
            self.hub.push("status", {"state": "error", "message": "No session. Start one first."})
            return
        self.busy = True
        self.last_user_text = text.strip()
        self.hub.push("busy", {"busy": True})
        try:
            prompt = build_prompt(text, attachments)
            if isinstance(prompt, str):
                await self.client.query(prompt)
            else:
                # query() accepts str or an async iterable of message dicts; a
                # single dict is not iterable, so yield it through a generator.
                async def _once(msg=prompt):
                    yield msg
                await self.client.query(_once())
            asyncio.ensure_future(self._receive(), loop=self.loop)
        except Exception as exc:
            self.hub.push("status", {"state": "error", "message": str(exc)})
            self.busy = False
            self.hub.push("busy", {"busy": False})

    async def _receive(self) -> None:
        assert self.client is not None
        try:
            async for msg in self.client.receive_response():
                await self._dispatch(msg)
        except Exception as exc:  # pragma: no cover
            self.hub.push("status", {"state": "error", "message": str(exc)})
        finally:
            self.busy = False
            self.hub.push("busy", {"busy": False})

    async def interrupt(self) -> None:
        if self.client:
            try:
                await self.client.interrupt()
                self.hub.push("notice", {"text": "Interrupted."})
            except Exception as exc:
                log(f"interrupt failed: {exc}")

    async def set_model(self, model: str) -> None:
        self.model = model
        if self.client:
            try:
                await self.client.set_model(None if model == "default" else model)
            except Exception:
                pass
        self.hub.push("meta", {"model": model})

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.client:
            try:
                await self.client.set_permission_mode(mode)
            except Exception:
                pass
        self.hub.push("meta", {"mode": mode})

    async def restart_fresh(self) -> None:
        """Start a new conversation in the same directory (like /clear)."""
        await self.start(self.cwd, self.model, self.mode, self.effort, None)

    async def context_usage(self) -> dict:
        if not self.client:
            return {}
        try:
            resp = await self.client.get_context_usage()
            cats = [
                {"name": c.get("name"), "tokens": c.get("tokens"),
                 "color": c.get("color")}
                for c in (resp.get("categories") or [])
            ]
            return {
                "totalTokens": resp.get("totalTokens", 0),
                "maxTokens": resp.get("maxTokens", 0),
                "percentage": round(float(resp.get("percentage", 0)), 1),
                "model": resp.get("model"),
                "categories": cats,
                "memoryFiles": (resp.get("memoryFiles") or [])[:12],
            }
        except Exception as exc:
            return {"error": str(exc)}

    async def push_context_usage(self) -> None:
        """Compute context usage on the loop and emit it as an event."""
        try:
            data = await self.context_usage()
        except Exception as exc:  # pragma: no cover
            data = {"error": str(exc)}
        self.usage_busy = False
        self.hub.push("usage", {"payload": data})

    async def rewind(self, message_id: str) -> None:
        if not self.client:
            return
        try:
            await self.client.rewind_files(message_id)
            self.hub.push("notice", {"text": "Files restored to that point."})
        except Exception as exc:
            self.hub.push("notice", {"text": f"Restore failed: {exc}"})

    # ---- permissions ----------------------------------------------------- #
    async def _can_use_tool(self, tool_name: str, input_data: dict, ctx) -> Any:
        pid = f"perm-{len(self.pending)+1}-{int(time.time()*1000)}"
        fut: asyncio.Future = self.loop.create_future()
        self.pending[pid] = {"fut": fut, "tool": tool_name}

        sugg = []
        for u in getattr(ctx, "suggestions", []) or []:
            d = dataclasses.asdict(u) if dataclasses.is_dataclass(u) else dict(u)
            d["rules"] = [
                r if isinstance(r, dict) else dataclasses.asdict(r)
                for r in (d.get("rules") or [])
            ]
            sugg.append(d)

        self.hub.push(
            "permission",
            {
                "id": pid,
                "tool": tool_name,
                "input": json.dumps(input_data, indent=2, ensure_ascii=False)[:6000],
                "title": getattr(ctx, "title", None),
                "displayName": getattr(ctx, "display_name", None),
                "description": getattr(ctx, "description", None),
                "blockedPath": getattr(ctx, "blocked_path", None),
                "reason": getattr(ctx, "decision_reason", None),
                "suggestions": sugg,
            },
        )
        decision = await fut
        self.pending.pop(pid, None)

        if decision.get("behavior") == "allow":
            upd = []
            for d in decision.get("updated_permissions") or []:
                utype = d.get("type") or "addRules"
                rules = [
                    PermissionRuleValue(
                        tool_name=r.get("tool_name") or tool_name,
                        rule_content=r.get("rule_content"),
                    )
                    for r in (d.get("rules") or [])
                ]
                if utype == "setMode":
                    mode = d.get("mode")
                    if mode not in PERM_MODES:
                        continue
                    upd.append(PermissionUpdate(type="setMode", mode=mode,
                                                destination="session"))
                elif utype in ("addDirectories", "removeDirectories"):
                    dirs = [str(x) for x in (d.get("directories") or [])]
                    if not dirs:
                        continue
                    upd.append(PermissionUpdate(type=utype, directories=dirs,
                                                destination="session"))
                else:
                    behavior = d.get("behavior")
                    if not rules or behavior not in ("allow", "deny", "ask"):
                        continue  # CLI rejects addRules without rules+behavior
                    upd.append(PermissionUpdate(
                        type=utype, rules=rules, behavior=behavior,
                        destination=d.get("destination") or "session",
                    ))
            return PermissionResultAllow(
                updated_input=decision.get("updated_input"),
                updated_permissions=upd or None,
            )
        return PermissionResultDeny(
            message=decision.get("message") or "User denied this action.",
            interrupt=bool(decision.get("interrupt", False)),
        )

    def resolve(self, pid: str, decision: dict) -> None:
        """Thread-safe resolution from the UI thread."""
        if pid not in self.pending or not self.loop:
            return
        self.loop.call_soon_threadsafe(self._resolve, pid, decision)

    def _resolve(self, pid: str, decision: dict) -> None:
        entry = self.pending.get(pid)
        if not entry:
            return
        fut: asyncio.Future = entry["fut"]
        if not fut.done():
            fut.set_result(decision)
        self.hub.push("permission_resolved", {"id": pid})

    # ---- message translation --------------------------------------------- #
    async def _dispatch(self, msg: Any) -> None:
        sid = getattr(msg, "session_id", None)
        if sid and sid != self.session_id:
            self.session_id = sid
            self.hub.push("meta", {"sessionId": sid})
            if self.on_session:
                try:
                    self.on_session(sid)
                except Exception:  # pragma: no cover
                    pass

        if isinstance(msg, StreamEvent):
            await self._stream_event(msg)
        elif isinstance(msg, AssistantMessage):
            self.hub.push(
                "assistant",
                {
                    "messageId": getattr(msg, "message_id", None),
                    "uuid": getattr(msg, "uuid", None),
                    "model": getattr(msg, "model", None),
                    "parentToolUseId": getattr(msg, "parent_tool_use_id", None),
                    "blocks": self._blocks(getattr(msg, "content", [])),
                },
            )
        elif isinstance(msg, UserMessage):
            await self._user_message(msg)
        elif isinstance(msg, ResultMessage):
            denials = []
            for d in (getattr(msg, "permission_denials", []) or []):
                dd = dataclasses.asdict(d) if dataclasses.is_dataclass(d) else dict(d)
                denials.append({
                    "tool": dd.get("tool_name"),
                    "input": clip(json.dumps(dd.get("tool_input", {}), ensure_ascii=False)),
                })
            self.hub.push(
                "result",
                {
                    "subtype": getattr(msg, "subtype", None),
                    "isError": bool(getattr(msg, "is_error", False)),
                    "result": clip(str(getattr(msg, "result", "") or "")),
                    "costUsd": getattr(msg, "total_cost_usd", 0) or 0,
                    "durationMs": getattr(msg, "duration_ms", 0),
                    "apiMs": getattr(msg, "duration_api_ms", 0),
                    "turns": getattr(msg, "num_turns", 0),
                    "usage": getattr(msg, "usage", None) or {},
                    "modelUsage": getattr(msg, "model_usage", None) or {},
                    "denials": denials,
                    "fallbackText": clip(str(getattr(msg, "result", "") or "")),
                    "sessionId": getattr(msg, "session_id", None),
                },
            )
        elif isinstance(msg, SystemMessage):
            sub = getattr(msg, "subtype", "")
            data = getattr(msg, "data", {}) or {}
            if sub == "init":
                self.hub.push(
                    "init",
                    {
                        "model": data.get("model"),
                        "cwd": data.get("cwd"),
                        "tools": data.get("tools", []),
                        "permissionMode": data.get("permissionMode"),
                        "sessionId": data.get("session_id"),
                    },
                )
            elif sub == "api_retry":
                self.hub.push(
                    "notice",
                    {"text": f"API retry {data.get('attempt')}/{data.get('max_retries')} - "
                             f"{data.get('error')} - {data.get('retry_delay_ms')}ms"},
                )
            elif sub in NOISY_SYSTEM_SUBTYPES:
                # Bookkeeping messages (one per thinking burst); keep them out of
                # the transcript and surface a compact counter instead.
                self.noisy[sub] = self.noisy.get(sub, 0) + 1
                self.hub.push("meta", {"quiet": True, "counters": dict(self.noisy)})
            elif sub:
                self.hub.push("system", {"subtype": sub, "data": clip(json.dumps(data))})
        elif RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
            info = getattr(msg, "rate_limit_info", None)
            self.hub.push(
                "notice",
                {"text": f"Rate limit: {getattr(info, 'status', '')} "
                         f"{getattr(info, 'type', '')}".strip()},
            )
        elif TaskProgressMessage is not None and isinstance(msg, TaskProgressMessage):
            self.hub.push(
                "task",
                {"taskId": getattr(msg, "task_id", None),
                 "description": getattr(msg, "description", None),
                 "tool": getattr(msg, "last_tool_name", None)},
            )

    async def _stream_event(self, ev: StreamEvent) -> None:
        e = ev.event or {}
        t = e.get("type")
        mid = str(e.get("message_id") or ev.uuid)
        parent = getattr(ev, "parent_tool_use_id", None)

        if t == "content_block_delta":
            d = e.get("delta") or {}
            kind = d.get("type")
            if kind == "text_delta":
                self.hub.push("delta", {"messageId": mid, "kind": "text",
                                        "text": d.get("text", ""), "parent": parent})
            elif kind == "thinking_delta":
                self.hub.push("delta", {"messageId": mid, "kind": "thinking",
                                        "text": d.get("thinking", ""), "parent": parent})
        elif t == "content_block_start":
            blk = e.get("content_block") or {}
            if blk.get("type") == "tool_use":
                self.hub.push(
                    "tool_start",
                    {"id": blk.get("id"), "name": blk.get("name"),
                     "parent": parent, "messageId": mid},
                )

    async def _user_message(self, msg: UserMessage) -> None:
        for b in self._blocks(getattr(msg, "content", None)):
            if b["type"] == "tool_result":
                self.hub.push(
                    "tool_result",
                    {"id": b["toolUseId"], "text": clip(tool_result_text(b["_obj"])),
                     "isError": bool(b.get("isError")),
                     "structured": getattr(msg, "tool_use_result", None)},
                )
            elif b["type"] == "text" and b.get("text"):
                self.hub.push(
                    "user_echo",
                    {"uuid": getattr(msg, "uuid", None),
                     "text": clip(b["text"], 4000),
                     "parent": getattr(msg, "parent_tool_use_id", None)},
                )

    def _blocks(self, content: Any) -> list:
        """Translate SDK content blocks into UI block dicts.

        Match on isinstance first: TextBlock/ThinkingBlock carry no `.type`
        attribute, so keying off `.type` silently drops the assistant's answer.
        """
        out = []
        for b in content or []:
            if isinstance(b, ToolUseBlock):
                out.append({"type": "tool_use", "id": b.id, "name": b.name,
                            "input": clip(json.dumps(b.input, indent=2, ensure_ascii=False), 6000)})
            elif isinstance(b, ToolResultBlock):
                out.append({"type": "tool_result", "toolUseId": b.tool_use_id,
                            "isError": bool(b.is_error), "_obj": b})
            elif isinstance(b, TextBlock):
                out.append({"type": "text", "text": b.text})
            elif isinstance(b, ThinkingBlock):
                out.append({"type": "thinking", "text": b.thinking})
            elif isinstance(b, ServerToolUseBlock):
                out.append({"type": "tool_use", "id": getattr(b, "id", ""),
                            "name": getattr(b, "name", "server"),
                            "input": clip(json.dumps(getattr(b, "input", {}), indent=2,
                                                     ensure_ascii=False), 6000)})
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    out.append({"type": "text", "text": b.get("text", "")})
                elif t == "thinking":
                    out.append({"type": "thinking", "text": b.get("thinking", "")})
                elif t == "tool_use":
                    out.append({"type": "tool_use", "id": b.get("id", ""),
                                "name": b.get("name", "tool"),
                                "input": clip(json.dumps(b.get("input", {}), indent=2,
                                                         ensure_ascii=False), 6000)})
                elif t == "tool_result":
                    out.append({"type": "tool_result", "toolUseId": b.get("tool_use_id", ""),
                                "isError": bool(b.get("is_error")), "_obj": b})
        return out


# --------------------------------------------------------------------------- #
# event hub (backend -> JS, drained by polling)
# --------------------------------------------------------------------------- #
class Hub:
    """Queue of protocol events; the UI drains it by polling."""

    _serializable = False  # not exposed to JS (see util.get_functions)

    def __init__(self) -> None:
        self._q = deque(maxlen=4000)
        self._lock = threading.Lock()

    def push(self, kind: str, payload: dict) -> None:
        with self._lock:
            self._q.append({"kind": kind, "ts": time.time(), **payload})

    def drain(self, limit: int = 120) -> list:
        with self._lock:
            n = min(limit, len(self._q))
            items = [self._q.popleft() for _ in range(n)]
            return items


# --------------------------------------------------------------------------- #
# background worker (keeps every js_api call non-blocking)
# --------------------------------------------------------------------------- #
class Worker:
    """Runs slow discovery work (subprocesses, disk scans) off the UI thread.

    pywebview invokes js_api methods on the UI thread, so anything that spawns
    a process or scans a directory must be deferred here; results arrive back
    as hub events. Requests are cached per key and de-duplicated while running,
    which is what keeps the window from appearing frozen.
    """

    _serializable = False  # keep pywebview from exposing internals to JS

    def __init__(self, hub: Hub, ttl: float = 12.0) -> None:
        self.hub = hub
        self.ttl = ttl
        self.cache: dict = {}
        self.inflight: set = set()
        self._lock = threading.Lock()
        self.q: "queue.Queue" = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def request(self, kind: str, key, fn, *args) -> bool:
        """Queue fn(*args) and deliver its result as event `kind`. Returns True
        when work was queued, False when served from cache."""
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            self.hub.push(kind, {"payload": hit[1], "cached": True})
            return False
        with self._lock:
            if key in self.inflight:
                return False
            self.inflight.add(key)
        self.q.put((kind, key, fn, args))
        return True

    def invalidate(self, key) -> None:
        self.cache.pop(key, None)

    def _loop(self) -> None:
        while True:
            kind, key, fn, args = self.q.get()
            try:
                res = fn(*args)
            except Exception as exc:  # pragma: no cover
                res = {"error": f"{type(exc).__name__}: {exc}"}
            self.cache[key] = (time.time(), res)
            with self._lock:
                self.inflight.discard(key)
            self.hub.push(kind, {"payload": res})


def list_sessions_payload(cwd: Optional[str]) -> list:
    from claude_agent_sdk import list_sessions
    infos = list_sessions(directory=cwd or None, limit=40)
    return [
        {
            "id": i.session_id,
            "title": i.custom_title or i.summary or (i.first_prompt or "")[:60],
            "cwd": i.cwd,
            "branch": i.git_branch,
            "modified": i.last_modified,
            "size": i.file_size,
        }
        for i in infos
    ]


# --------------------------------------------------------------------------- #
# JS API surface
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self, mgr: SessionManager, hub: Hub) -> None:
        self.mgr = mgr
        self.hub = hub
        self.window = None  # never a webview.Window: pywebview recursively walks
                            # every exposed attribute, and crawling the WinForms /
                            # WebView2 COM tree floods the UI thread at startup.
        self.settings = load_settings()
        self._settings_lock = threading.Lock()
        self.worker = Worker(hub)
        self.attachments: dict = {}    # attachment id -> payload (kept server-side)
        mgr.on_session = self.persist_session

    def persist_session(self, session_id: str) -> None:
        """Remember the live session so a relaunch can restore the transcript."""
        with self._settings_lock:
            self.settings["last_session"] = session_id
            self.settings["last_cwd"] = self.mgr.cwd
        save_settings(self.settings)

    # ---- plumbing -------------------------------------------------------- #
    def poll(self) -> str:
        return json.dumps(self.hub.drain())

    def ping(self) -> str:
        return "ok"

    # ---- session --------------------------------------------------------- #
    def start(self, cwd: str, model: str, mode: str, effort: str,
              resume: Optional[str] = None) -> str:
        self.mgr.start_loop()
        self.mgr.submit(self.mgr.start(cwd, model, mode, effort, resume))
        self.settings.update({"cwd": cwd, "model": model, "mode": mode, "effort": effort})
        save_settings(self.settings)
        return json.dumps({"ok": True})

    def send(self, text: str, attachments_json: str = "") -> str:
        items: list = []
        if attachments_json:
            try:
                for aid in json.loads(attachments_json):
                    got = self.attachments.get(aid)
                    if got:
                        items.append(got)
            except Exception as exc:  # pragma: no cover
                return json.dumps({"ok": False, "error": str(exc)})
        if not text.strip() and not items:
            return json.dumps({"ok": False})
        self.mgr.submit(self.mgr.send(text.strip(), items))
        for aid in [a for a in (json.loads(attachments_json) if attachments_json else [])]:
            self.attachments.pop(aid, None)
        return json.dumps({"ok": True})

    # ---- attachments ---------------------------------------------------- #
    def pick_files(self) -> str:
        res = pick_files_native(self.mgr.cwd)
        out: list = []
        for p in res.get("paths", [])[:MAX_ATTACHMENTS]:
            d = describe_attachment(p)
            out.append(self._register(d))
        return json.dumps({"items": out, "source": res.get("source")})

    def paste_clipboard_image(self) -> str:
        """Native clipboard read for screenshots/bitmap pastes."""
        d = read_clipboard_image()
        if d.get("kind") != "image":
            return json.dumps({"items": [], "kind": d.get("kind"),
                               "error": d.get("error")})
        return json.dumps({"items": [self._register(d)], "kind": "image"})

    def attach_blob(self, name: str, mime: str, b64: str) -> str:
        """Store a dropped/pasted blob (no filesystem path available in WebView2)."""
        try:
            ATTACH_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\-]", "_", (name or "pasted")[-80:])
            ext = Path(safe).suffix.lower()
            if not ext:
                ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                       "image/webp": ".webp"}.get(mime, ".bin")
                safe = f"{safe}{ext}"
            target = ATTACH_DIR / f"{int(time.time()*1000)}-{safe}"
            target.write_bytes(base64.b64decode(b64))
            return json.dumps(self._register(describe_attachment(str(target))))
        except Exception as exc:
            return json.dumps({"kind": "error", "name": name, "error": str(exc)})

    def _register(self, d: dict) -> dict:
        aid = f"att-{uuid.uuid4().hex[:10]}"
        self.attachments[aid] = d      # full payload stays server-side for send()
        # Thumbnails are capped so the JS bridge never carries a 5 MB base64 string.
        preview = None
        if d.get("kind") == "image" and d.get("bytes", 0) <= PREVIEW_MAX_BYTES:
            preview = d.get("data")
        return {"id": aid, "name": d.get("name"), "kind": d.get("kind"),
                "mime": d.get("mime"), "bytes": d.get("bytes", 0),
                "preview": preview, "error": d.get("error")}

    def interrupt(self) -> str:
        self.mgr.submit(self.mgr.interrupt())
        return json.dumps({"ok": True})

    def stop(self) -> str:
        self.mgr.submit(self.mgr.stop())
        return json.dumps({"ok": True})

    def set_model(self, model: str) -> str:
        self.mgr.submit(self.mgr.set_model(model))
        self.settings["model"] = model
        save_settings(self.settings)
        return json.dumps({"ok": True})

    def set_mode(self, mode: str) -> str:
        self.mgr.submit(self.mgr.set_mode(mode))
        self.settings["mode"] = mode
        save_settings(self.settings)
        return json.dumps({"ok": True})

    def resolve_permission(self, pid: str, decision_json: str) -> str:
        try:
            self.mgr.resolve(pid, json.loads(decision_json))
        except Exception as exc:  # pragma: no cover
            log(f"resolve failed: {exc}")
        return json.dumps({"ok": True})

    def context_usage(self) -> str:
        """Non-blocking: result arrives later as a 'usage' event."""
        if not self.mgr.loop or not self.mgr.client:
            self.hub.push("usage", {"payload": {"error": "no session"}})
            return json.dumps({"queued": False})
        if self.mgr.usage_busy:
            return json.dumps({"queued": False})
        self.mgr.usage_busy = True
        self.mgr.submit(self.mgr.push_context_usage())
        return json.dumps({"queued": True})

    def rewind(self, message_id: str) -> str:
        self.mgr.submit(self.mgr.rewind(message_id))
        return json.dumps({"ok": True})

    # ---- discovery (all deferred to the worker; nothing blocks the UI) --- #
    def sessions(self, cwd: Optional[str], refresh: bool = False) -> str:
        key = ("sessions", cwd or "")
        if refresh:
            self.worker.invalidate(key)
        self.worker.request("sessions", key, list_sessions_payload, cwd)
        return json.dumps({"queued": True})

    def auth_status(self, refresh: bool = False) -> str:
        if refresh:
            self.worker.invalidate("auth")
        self.worker.request("auth", "auth", auth_status)
        return json.dumps({"queued": True})

    def git(self, cwd: Optional[str], refresh: bool = False) -> str:
        target = cwd or self.mgr.cwd
        key = ("git", target)
        if refresh:
            self.worker.invalidate(key)
        self.worker.request("git", key, git_snapshot, target)
        return json.dumps({"queued": True})

    def pick_folder(self) -> str:
        initial = self.mgr.cwd or str(HOME_DIR)
        out = pick_folder_native(initial)
        if not out.get("path") and str(out.get("source", "")).startswith("error"):
            out = pick_folder_pywebview(initial)   # second attempt, explicit directory
        return json.dumps(out)

    def home(self) -> str:
        return json.dumps({"home": str(Path.home()), "claude": find_claude() or ""})

    def defaults(self) -> str:
        s = self.settings
        return json.dumps(
            {
                "cwd": s.get("cwd") or str(Path.cwd()),
                "model": s.get("model", "default"),
                "mode": s.get("mode", "default"),
                "effort": s.get("effort", "medium"),
                "models": MODELS,
                "modes": MODES,
                "efforts": EFFORTS,
                "lastSession": s.get("last_session"),
                "lastCwd": s.get("last_cwd"),
            }
        )

    def history(self, session_id: str, cwd: Optional[str]) -> str:
        self.worker.request("history", ("history", session_id), history_payload, session_id, cwd)
        return json.dumps({"queued": True})

    def delete_session(self, session_id: str, cwd: Optional[str]) -> str:
        """Hard-delete a stored session transcript (SDK delete_session)."""
        try:
            from claude_agent_sdk import delete_session as sdk_delete
            sdk_delete(session_id, directory=cwd or None)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if self.mgr.session_id == session_id:
            self.mgr.session_id = None
        with self._settings_lock:
            if self.settings.get("last_session") == session_id:
                self.settings.pop("last_session", None)
        save_settings(self.settings)
        # rebuild the sidebar from disk, bypassing the worker cache
        key = ("sessions", cwd or "")
        self.worker.invalidate(key)
        self.worker.request("sessions", key, list_sessions_payload, cwd)
        self.hub.push("notice", {"text": f"Deleted session {session_id[:8]}."})
        return json.dumps({"ok": True})

    def commands(self, cwd: Optional[str]) -> str:
        self.worker.request("commands", ("commands", cwd or ""), load_commands, cwd)
        return json.dumps({"queued": True})

    def expand(self, name: str, args: str, cwd: Optional[str]) -> str:
        try:
            return json.dumps(expand_command(name, args, cwd))
        except Exception as exc:  # pragma: no cover
            return json.dumps({"error": str(exc)})

    def restart(self) -> str:
        self.mgr.submit(self.mgr.restart_fresh())
        return json.dumps({"ok": True})

    def layout(self, payload: str) -> str:
        layout_report(payload)
        return json.dumps({"ok": True})

    def open_external(self, url: str) -> str:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        return json.dumps({"ok": True})


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def main() -> None:
    hub = Hub()
    mgr = SessionManager(hub)
    api = Api(mgr, hub)

    window = webview.create_window(
        title=APP_NAME,
        url=str(UI_DIR / "index.html"),
        js_api=api,
        width=1320,
        height=900,
        min_size=(960, 620),
        text_select=True,
    )

    def on_closed() -> None:  # pragma: no cover
        try:
            mgr.submit(mgr.stop(quiet=True))
        except Exception:
            pass

    try:
        window.events.closed += on_closed
    except Exception:
        pass

    exe = find_claude()
    log(f"claude executable: {exe or 'NOT FOUND'}")

    # Pre-warm discovery so the sidebar fills without touching the UI thread.
    api.worker.request("auth", "auth", auth_status)
    api.worker.request("sessions", ("sessions", ""), list_sessions_payload, None)
    api.worker.request("git", ("git", mgr.cwd), git_snapshot, mgr.cwd)

    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
