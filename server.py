"""
Standalone test server: Cursor CLI chat per workspace; optional --resume via cli_prefix template.
Each named session uses ./chats/<slug>/ as workspace; session.json + _app_state.json persist UI.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import shutil
import signal
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Missing required config file: {CONFIG_PATH}")
    try:
        raw = CONFIG_PATH.read_bytes().decode("utf-8")
        if sys.version_info >= (3, 11):
            import tomllib

            cfg = tomllib.loads(raw)
        else:
            import tomli

            cfg = tomli.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Could not load required config file {CONFIG_PATH}: {e}") from e
    if not isinstance(cfg, dict):
        raise RuntimeError(f"Config file {CONFIG_PATH} did not parse to a table")
    return cfg


def _coerce_str_list(val, field_name: str) -> list[str]:
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for x in val:
            s = str(x).strip()
            if s:
                out.append(s)
        if out:
            return out
    if isinstance(val, str) and val.strip():
        try:
            parts = shlex.split(val.strip(), posix=(sys.platform != "win32"))
        except ValueError:
            raise RuntimeError(f"Config field {field_name} contains invalid shell syntax") from None
        if parts:
            return parts
    raise RuntimeError(f"Config field {field_name} must be a non-empty string or list of strings")


def _coerce_cors_origins(val) -> list[str]:
    if isinstance(val, str) and val.strip():
        parts = [x.strip() for x in val.replace(";", ",").split(",") if x.strip()]
        return parts if parts else ["*"]
    if isinstance(val, list) and val:
        return [str(x).strip() for x in val if str(x).strip()]
    return ["*"]


_FB_INSTR = [
    "read",
    "AGENTS.md",
    "and",
    "HUMAN.md.",
    "If",
    "ATTACHMENTS.md",
    "exists,",
    "use",
    "the",
    "referenced",
    "files",
    "listed",
    "there.",
    "Follow",
    "HUMAN.md",
    "first",
    "as",
    "the",
    "current",
    "task.",
]
_CFG = _load_config()
_SERVER = _CFG["server"]
_APP = _CFG["app"]
_CORS = _CFG["cors"]
_PATHS = _CFG["paths"]
_DEFAULTS = _CFG["defaults"]
_AGENT_CMD = _CFG["agent"]
_SCHED = _CFG["scheduler"]
_TO = _CFG["timeouts"]

PORT = int(_SERVER["port"])
HOST = str(_SERVER["host"])
LOG_LEVEL = str(_SERVER["log_level"])
APP_TITLE = str(_APP["title"])
CORS_ALLOW_ORIGINS = _coerce_cors_origins(_CORS.get("allow_origins"))
STATIC = ROOT / str(_PATHS["static_dir"])
CHATS = ROOT / str(_PATHS["chats_dir"])
SESSION_NAME = str(_PATHS["session_file"])
APP_STATE_NAME = str(_PATHS["app_state_file"])
AGENTS_MD_NAME = str(_PATHS["agents_md_file"])
HUMAN_MD_NAME = str(_PATHS.get("human_md_file", "HUMAN.md"))
ATTACHMENTS_MD_NAME = str(_PATHS.get("attachments_md_file", "ATTACHMENTS.md"))
DEFAULT_AGENTS_MD = str(_DEFAULTS["agents_md_default"])
DEFAULT_CLI = str(_DEFAULTS["cli_prefix"])
DEFAULT_SCHEDULE = str(_DEFAULTS["schedule"])
CREATE_CHAT_ARGV = _coerce_str_list(_AGENT_CMD["create_chat_argv"], "agent.create_chat_argv")
SCHEDULER_MISFIRE_GRACE = int(_SCHED["misfire_grace_time"])
TIMEOUT_AGENT_S = int(_TO["agent_seconds"])
TIMEOUT_CREATE_CHAT_S = int(_TO["create_chat_seconds"])


def _chats_dir() -> Path:
    CHATS.mkdir(parents=True, exist_ok=True)
    return CHATS


def _slugify(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")[:80] or "session"
    if not re.match(r"^[a-z0-9]", s):
        s = "s-" + s
    return s


def _allocate_slug(base: str) -> str:
    root = _chats_dir()
    slug = base
    n = 2
    while (root / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _session_path(slug: str) -> Path:
    if not slug or slug.startswith("_") or ".." in slug or "/" in slug or "\\" in slug:
        raise ValueError("invalid slug")
    return _chats_dir() / slug / SESSION_NAME


def _workspace_for_slug(slug: str) -> Path:
    if not slug or slug.startswith("_") or ".." in slug or "/" in slug or "\\" in slug:
        raise ValueError("invalid slug")
    return (_chats_dir() / slug).resolve()


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _human_md_path(ws: Path) -> Path:
    return ws / HUMAN_MD_NAME


def _attachments_md_path(ws: Path) -> Path:
    return ws / ATTACHMENTS_MD_NAME


CURSOR_PROJECTS_BASE = Path.home() / ".cursor" / "projects"


def _cursor_project_name(ws: Path) -> str:
    s = str(ws.resolve())
    s = s.replace(":\\", "-").replace("\\", "-").replace("/", "-")
    return s.rstrip("-")


def _cursor_assets_dir(ws: Path) -> Path:
    name = _cursor_project_name(ws)
    p = CURSOR_PROJECTS_BASE / name / "assets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _attachment_prefix_for_content_type(content_type: str, filename: str) -> str:
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if ct.startswith("image/") or re.search(r"\.(png|jpe?g|gif|webp|bmp)$", name):
        return "screenshot"
    if ct == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    return "file"


def _attachment_suffix_for_upload(content_type: str, filename: str) -> str:
    raw_name = (filename or "").strip()
    suffix = Path(raw_name).suffix[:16]
    if suffix and re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
        return suffix.lower()
    ct = (content_type or "").lower()
    custom = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }
    if ct in custom:
        return custom[ct]
    guessed = mimetypes.guess_extension(ct) if ct else None
    if guessed and re.fullmatch(r"\.[A-Za-z0-9]+", guessed):
        return guessed.lower()
    return ".bin"


_READABLE_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
})
_READABLE_PDF_MIMES = frozenset({"application/pdf"})


def _attachment_access_hint(mime: str) -> str:
    m = (mime or "").lower()
    if m in _READABLE_IMAGE_MIMES:
        return "Use the **Read** tool on this file to view the image."
    if m in _READABLE_PDF_MIMES:
        return "Use the **Read** tool on this file to extract the PDF text."
    if m.startswith("text/"):
        return "Use the **Read** tool on this file to read the contents."
    return "Binary file, use the **Read** tool if the format is supported."


def _next_attachment_token(ws: Path, prefix: str) -> str:
    assets = _cursor_assets_dir(ws)
    rx = re.compile(rf"^{re.escape(prefix)}(\d+)(?:\.[^.]+)?$", re.I)
    n = 1
    for p in assets.iterdir():
        if not p.is_file():
            continue
        m = rx.match(p.name)
        if m:
            try:
                n = max(n, int(m.group(1)) + 1)
            except ValueError:
                pass
    return f"{prefix}{n}"


def _write_human_md(ws: Path, prompt: str, *, source: str) -> None:
    text = (prompt or "").strip()
    path = _human_md_path(ws)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    body = (
        "# HUMAN.md\n\n"
        "Current task input for this chat.\n\n"
        f"- Source: {source}\n"
        f"- Updated: {stamp}\n\n"
        "## Priority\n"
        "Treat this file as the leading instruction for the current run.\n"
        "If AGENTS.md conflicts with HUMAN.md, follow HUMAN.md.\n\n"
        "## Task\n\n"
        f"{text}\n"
    )
    path.write_text(body, encoding="utf-8")


def _prepare_run_context(ws: Path, prompt: str, *, source: str) -> None:
    _write_human_md(ws, prompt, source=source)
    _write_attachments_md(ws, prompt)


def _extract_attachment_refs(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"(?<!\w)@([A-Za-z][A-Za-z0-9_-]*)", text or ""):
        token = m.group(1)
        low = token.lower()
        if low not in seen:
            seen.add(low)
            out.append(token)
    return out


def _resolve_attachment_token(ws: Path, token: str) -> Path | None:
    assets = _cursor_assets_dir(ws)
    exact = [p for p in assets.iterdir() if p.is_file() and p.stem.lower() == token.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(f"Multiple assets files match @{token}")
    return None


def _write_attachments_md(ws: Path, prompt: str) -> None:
    path = _attachments_md_path(ws)
    refs = _extract_attachment_refs(prompt)
    if not refs:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    lines = [
        "# ATTACHMENTS.md",
        "",
        "Referenced files for this run only.",
        "",
        "## How to access",
        "",
        "Use your **Read** tool with the absolute file path shown below.",
        "The Read tool supports images (png, jpg, gif, webp) and PDFs natively.",
        "Do NOT use shell commands to open these files.",
        "",
        "## Files",
        "",
    ]
    any_resolved = False
    for token in refs:
        resolved = _resolve_attachment_token(ws, token)
        if resolved is None:
            lines.append(f"- `@{token}` -> MISSING (not uploaded)")
            continue
        any_resolved = True
        abs_path = str(resolved)
        mime = mimetypes.guess_type(str(resolved.name))[0] or "application/octet-stream"
        hint = _attachment_access_hint(mime)
        lines.extend(
            [
                f"- `@{token}` -> `{abs_path}`",
                f"  - type: `{mime}`",
                f"  - size_bytes: {resolved.stat().st_size}",
                f"  - access: {hint}",
            ]
        )
    if not any_resolved:
        lines.append("")
        lines.append("No referenced files could be resolved.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sanitize_client_id(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", s):
        return None
    return s


def _legacy_app_state_path() -> Path:
    return CHATS / APP_STATE_NAME


def _app_state_path(client_id: str | None) -> Path:
    cid = _sanitize_client_id(client_id)
    if not cid:
        return _legacy_app_state_path()
    stem, suffix = os.path.splitext(APP_STATE_NAME)
    return CHATS / f"{stem}.{cid}{suffix}"


def _iter_app_state_paths() -> list[Path]:
    root = _chats_dir()
    stem, suffix = os.path.splitext(APP_STATE_NAME)
    out: list[Path] = []
    legacy = _legacy_app_state_path()
    if legacy.is_file():
        out.append(legacy)
    for p in sorted(root.glob(f"{stem}.*{suffix}")):
        if p.is_file():
            out.append(p)
    return out


scheduler = BackgroundScheduler()
_scheduler_slug_lock = threading.Lock()
_scheduler_running_slugs: set[str] = set()

# Manual /api/chat runs one subprocess per slug; poll GET /api/chat/status until done; POST /api/chat/stop kills the tree.
_manual_agent_lock = threading.Lock()
_manual_agent_state: dict[str, dict] = {}
# (enabled, schedule_lower) last applied per slug — avoid remove+re-add on every persist (resets interval next_run).
_chat_sched_sig: dict[str, tuple[bool, str]] = {}


def _get_interval_schedule_parts(schedule_str: str) -> tuple[int, str]:
    s = (schedule_str or "").lower().strip()
    if s.startswith("interval:"):
        val_str = s.replace("interval:", "").strip()
    else:
        val_str = s
    unit = val_str[-1]
    val = int(val_str[:-1])
    return val, unit


def _interval_start_next_minute_boundary() -> datetime:
    now = datetime.now().astimezone()
    floor = now.replace(second=0, microsecond=0)
    return floor + timedelta(minutes=1)


def _interval_step_for_display(schedule_str: str) -> timedelta:
    try:
        val, unit = _get_interval_schedule_parts(schedule_str or "")
        if unit == "s":
            return timedelta(seconds=max(1, val))
        if unit == "m":
            return timedelta(minutes=max(1, val))
        if unit == "h":
            return timedelta(hours=max(1, val))
        if unit == "d":
            return timedelta(days=max(1, val))
    except (ValueError, IndexError):
        pass
    return timedelta(minutes=1)


def _normalize_next_run_past_minute(local: datetime, schedule_str: str) -> datetime:
    tz = local.tzinfo
    nowm = datetime.now(tz).replace(second=0, microsecond=0)
    nr = local.replace(second=0, microsecond=0)
    if nr > nowm:
        return nr
    step = _interval_step_for_display(schedule_str)
    cur = nr
    for _ in range(10000):
        if cur > nowm:
            return cur
        cur += step
    return nowm + timedelta(minutes=1)


def _append_scheduler_messages_and_save(slug: str, user_text: str, agent_text: str, is_err: bool) -> None:
    root = _chats_dir()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stamp_user = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    stamp_agent = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    appended = False
    for meta_path in _iter_app_state_paths():
        meta = _read_json(meta_path)
        if not meta or not isinstance(meta.get("tabs"), list):
            continue
        tabs = meta["tabs"]
        changed = False
        for i, t in enumerate(tabs):
            if not isinstance(t, dict) or t.get("slug") != slug:
                continue
            msgs = list(t.get("messages") or [])
            msgs.append({"role": "user", "text": user_text, "ts": stamp_user})
            msgs.append({"role": "agent", "text": agent_text, "err": is_err, "ts": stamp_agent})
            t["messages"] = msgs
            tabs[i] = t
            changed = True
            appended = True
            break
        if changed:
            meta["tabs"] = tabs
            meta["saved_at"] = now
            _write_json_atomic(meta_path, meta)

    pdir = root / slug
    if pdir.is_dir():
        sess = _read_json(pdir / SESSION_NAME) or {}
        msgs = list(sess.get("messages") or [])
        if not appended:
            msgs.append({"role": "user", "text": user_text, "ts": stamp_user})
            msgs.append({"role": "agent", "text": agent_text, "err": is_err, "ts": stamp_agent})
        else:
            msgs = None
            for meta_path in _iter_app_state_paths():
                meta = _read_json(meta_path)
                if not meta or not isinstance(meta.get("tabs"), list):
                    continue
                for t in meta["tabs"]:
                    if isinstance(t, dict) and t.get("slug") == slug:
                        msgs = list(t.get("messages") or [])
                        break
                if msgs is not None:
                    break
            if msgs is None:
                msgs = list(sess.get("messages") or [])
        sess["messages"] = msgs
        sess["updated_at"] = now
        _write_json_atomic(pdir / SESSION_NAME, sess)


def execute_scheduled_chat(slug: str) -> None:
    with _scheduler_slug_lock:
        if slug in _scheduler_running_slugs:
            return
        _scheduler_running_slugs.add(slug)
    try:
        try:
            ws = _workspace_for_slug(slug)
        except ValueError:
            return
        sess = _read_json(ws / SESSION_NAME)
        if not sess or not sess.get("scheduler_enabled"):
            return
        prompt = (sess.get("scheduler_prompt") or "").strip()
        if not prompt:
            return
        chat_id = (sess.get("chat_id") or "").strip()
        cli_prefix = sess.get("cli_prefix") or DEFAULT_CLI
        if not chat_id:
            return
        if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
            return
        try:
            _prepare_run_context(ws, prompt, source="scheduler")
        except (OSError, RuntimeError):
            _append_scheduler_messages_and_save(
                slug,
                prompt,
                "Could not prepare HUMAN.md or ATTACHMENTS.md for scheduled run.",
                True,
            )
            return
        tail = [] if _cli_prefix_uses_template(cli_prefix) else _cli_trailing_argv_chat(ws, prompt)
        try:
            args = _build_agent_argv(cli_prefix, chat_id, ws, tail)
        except ValueError:
            return
        try:
            r = _run_agent(args, cwd=str(ws), timeout=TIMEOUT_AGENT_S)
        except subprocess.TimeoutExpired:
            _append_scheduler_messages_and_save(
                slug,
                prompt,
                f"Scheduled run timed out ({TIMEOUT_AGENT_S}s).",
                True,
            )
            return
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        body = out or "(empty stdout)"
        if err:
            body += "\n\n--- stderr ---\n" + err
        is_err = r.returncode != 0
        if is_err:
            body = f"exit {r.returncode}\n\n" + body
        _append_scheduler_messages_and_save(slug, prompt, body, is_err)
    finally:
        with _scheduler_slug_lock:
            _scheduler_running_slugs.discard(slug)


def schedule_chat_job(slug: str, schedule_str: str, enabled: bool) -> None:
    job_id = f"chat_sched_{slug}"
    if not enabled:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        _chat_sched_sig.pop(slug, None)
        return

    sched_norm = (schedule_str or DEFAULT_SCHEDULE).strip()
    sig: tuple[bool, str] = (True, sched_norm.lower())
    if _chat_sched_sig.get(slug) == sig and scheduler.get_job(job_id):
        return

    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    s = sched_norm.lower()
    if not s:
        _chat_sched_sig.pop(slug, None)
        return
    common = {
        "args": [slug],
        "id": job_id,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": SCHEDULER_MISFIRE_GRACE,
        "replace_existing": True,
    }
    try:
        if s.startswith("interval:"):
            val, unit = _get_interval_schedule_parts(s)
            if unit == "s":
                scheduler.add_job(execute_scheduled_chat, "interval", seconds=val, **common)
            elif unit == "m":
                start = _interval_start_next_minute_boundary()
                scheduler.add_job(
                    execute_scheduled_chat,
                    IntervalTrigger(minutes=val, start_date=start, timezone=start.tzinfo),
                    **common,
                )
            elif unit == "h":
                scheduler.add_job(execute_scheduled_chat, "interval", hours=val, **common)
            elif unit == "d":
                scheduler.add_job(execute_scheduled_chat, "interval", days=val, **common)
            else:
                _chat_sched_sig.pop(slug, None)
                return
        elif s.startswith("cron:"):
            cron_expr = (schedule_str or "").replace("cron:", "").strip()
            scheduler.add_job(
                execute_scheduled_chat,
                CronTrigger.from_crontab(cron_expr),
                **common,
            )
        elif s.startswith("date:"):
            date_expr = (schedule_str or "").replace("date:", "").strip()
            scheduler.add_job(execute_scheduled_chat, "date", run_date=date_expr, **common)
        else:
            val, unit = _get_interval_schedule_parts(s)
            if unit == "s":
                scheduler.add_job(execute_scheduled_chat, "interval", seconds=val, **common)
            elif unit == "m":
                start = _interval_start_next_minute_boundary()
                scheduler.add_job(
                    execute_scheduled_chat,
                    IntervalTrigger(minutes=val, start_date=start, timezone=start.tzinfo),
                    **common,
                )
            elif unit == "h":
                scheduler.add_job(execute_scheduled_chat, "interval", hours=val, **common)
            elif unit == "d":
                scheduler.add_job(execute_scheduled_chat, "interval", days=val, **common)
            else:
                _chat_sched_sig.pop(slug, None)
                return
    except Exception as e:
        print(f"Invalid schedule for chat {slug}: {schedule_str!r} — {e}")
        _chat_sched_sig.pop(slug, None)
        return
    _chat_sched_sig[slug] = sig


def _sync_scheduler_from_tabs_payload(tabs: list[dict]) -> None:
    slugs_in_tabs: set[str] = set()
    for t in tabs:
        if not isinstance(t, dict):
            continue
        slug = t.get("slug")
        if not slug:
            continue
        slugs_in_tabs.add(str(slug))
        schedule_chat_job(
            str(slug),
            t.get("schedule") or DEFAULT_SCHEDULE,
            bool(t.get("schedulerEnabled")),
        )
    for job in list(scheduler.get_jobs()):
        jid = job.id or ""
        if jid.startswith("chat_sched_"):
            su = jid[len("chat_sched_") :]
            if su not in slugs_in_tabs:
                scheduler.remove_job(jid)
                _chat_sched_sig.pop(su, None)


def _load_schedulers_from_disk() -> None:
    _sync_scheduler_from_tabs_payload(_scan_disk_tabs())


def _format_scheduler_next_display(schedule_str: str, next_run: datetime | None) -> str | None:
    if next_run is None:
        return None
    try:
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        local = next_run.astimezone().replace(second=0, microsecond=0)
    except (OSError, OverflowError, ValueError):
        return None
    s = (schedule_str or "").strip().lower()
    if s.startswith("date:"):
        return local.strftime("%Y-%m-%d %H:%M")
    if s.startswith("cron:"):
        parts = (schedule_str or "").replace("cron:", "").strip().split()
        if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            return local.strftime("%H:%M")
        if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] != "*":
            return local.strftime("%a %H:%M")
        return local.strftime("%Y-%m-%d %H:%M")
    if s.startswith("interval:"):
        local = _normalize_next_run_past_minute(local, schedule_str)
        return local.strftime("%Y-%m-%d %H:%M")
    try:
        _get_interval_schedule_parts(schedule_str or "")
    except (ValueError, IndexError):
        return local.strftime("%Y-%m-%d %H:%M")
    local = _normalize_next_run_past_minute(local, schedule_str)
    return local.strftime("%Y-%m-%d %H:%M")


def _enrich_tabs_scheduler_next_run(tabs: list[dict]) -> None:
    for row in tabs:
        slug = row.get("slug")
        row["schedulerAgentBusy"] = bool(slug and str(slug) in _scheduler_running_slugs)
        if not slug or not row.get("schedulerEnabled"):
            row["schedulerNextRunDisplay"] = None
            continue
        job = scheduler.get_job(f"chat_sched_{str(slug)}")
        nr = job.next_run_time if job else None
        row["schedulerNextRunDisplay"] = _format_scheduler_next_display(
            str(row.get("schedule") or ""),
            nr,
        )


@asynccontextmanager
async def _lifespan(_: FastAPI):
    if not scheduler.running:
        scheduler.start()
    _chats_dir()
    _load_schedulers_from_disk()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title=APP_TITLE, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _split_cli_prefix(prefix: str) -> list[str]:
    s = (prefix or "").strip()
    if not s:
        raise ValueError("CLI prefix is empty")
    if sys.platform == "win32":
        return shlex.split(s, posix=False)
    return shlex.split(s)


def _cli_prefix_uses_template(prefix: str) -> bool:
    s = prefix or ""
    return "{chat_id}" in s or "{workspace}" in s


def _expand_cli_prefix_template(prefix: str, chat_id: str, workspace: Path) -> str:
    return (
        (prefix or "")
        .replace("{chat_id}", chat_id.strip())
        .replace("{workspace}", str(workspace))
    )


def _build_agent_argv(
    cli_prefix: str,
    chat_id: str,
    workspace: Path,
    trailing: list[str],
) -> list[str]:
    if _cli_prefix_uses_template(cli_prefix):
        expanded = _expand_cli_prefix_template(cli_prefix, chat_id, workspace)
        parts = shlex.split(expanded)
        return parts + trailing
    parts = _split_cli_prefix(cli_prefix)
    return parts + ["--resume", chat_id.strip(), "--workspace", str(workspace)] + trailing


def _argv_to_preview(argv: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def _cli_trailing_argv_chat(ws: Path, user_message: str) -> list[str]:
    _ = user_message
    if (ws / AGENTS_MD_NAME).is_file():
        return list(_FB_INSTR)
    return ["read", "HUMAN.md", "and", "follow", "it", "as", "the", "current", "task."]


def _cli_trailing_argv_preview(ws: Path, user_message: str) -> list[str]:
    _ = user_message
    return _cli_trailing_argv_chat(ws, "")


def _run_agent(argv: list[str], *, cwd: str | None, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    if sys.platform == "win32" and argv:
        exe = shutil.which(argv[0]) or argv[0]
        resolved = [exe, *argv[1:]]
        lower = str(exe).lower()
        if lower.endswith(".cmd") or lower.endswith(".bat"):
            line = subprocess.list2cmdline(resolved)
            return subprocess.run(
                ["cmd.exe", "/d", "/s", "/c", line],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                env=env,
            )
        argv = resolved
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        env=env,
    )


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _popen_agent(argv: list[str], *, cwd: str | None) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    popen_kw: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": cwd,
        "env": env,
    }
    if sys.platform == "win32" and argv:
        exe = shutil.which(argv[0]) or argv[0]
        resolved = [exe, *argv[1:]]
        lower = str(exe).lower()
        if lower.endswith(".cmd") or lower.endswith(".bat"):
            line = subprocess.list2cmdline(resolved)
            return subprocess.Popen(
                ["cmd.exe", "/d", "/s", "/c", line],
                **popen_kw,
            )
        argv = resolved
    return subprocess.Popen(argv, **popen_kw)


def _manual_agent_worker(slug: str, args: list[str], cwd: str, preview: str) -> None:
    proc: subprocess.Popen[str] | None = None
    rc = -1
    out = ""
    err = ""
    killed = False
    try:
        proc = _popen_agent(args, cwd=cwd)
        with _manual_agent_lock:
            st = _manual_agent_state.get(slug)
            if not st:
                _terminate_process_tree(proc.pid)
                _manual_agent_state.pop(slug, None)
                return
            st["proc"] = proc
        try:
            out, err = proc.communicate(timeout=TIMEOUT_AGENT_S)
            rc = proc.returncode if proc.returncode is not None else 0
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=30)
            except Exception:
                out, err = "", ""
            rc = proc.returncode if proc.returncode is not None else -1
            killed = True
    except Exception as e:
        rc = -1
        err = str(e)
    finally:
        with _manual_agent_lock:
            st = _manual_agent_state.get(slug)
            if st:
                st["proc"] = None
                if st.get("user_stopped"):
                    killed = True
                st["result"] = {
                    "returncode": rc,
                    "stdout": (out or "").strip(),
                    "stderr": (err or "").strip(),
                    "preview_cmd": preview,
                    "killed": killed,
                }


def _new_chat_id() -> str:
    r = _run_agent(CREATE_CHAT_ARGV, cwd=None, timeout=TIMEOUT_CREATE_CHAT_S)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "create-chat failed")
    out = (r.stdout or "").strip()
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        out,
        re.I,
    )
    if not m:
        raise RuntimeError(f"Could not parse chat id from: {out[:500]}")
    return m.group(0)


def _scan_disk_tabs() -> list[dict]:
    out: list[dict] = []
    root = _chats_dir()
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        data = _read_json(p / SESSION_NAME)
        if not data:
            continue
        slug = data.get("slug") or p.name
        out.append(
            {
                "id": data.get("tab_id") or slug,
                "slug": slug,
                "title": data.get("title") or slug,
                "chatId": data.get("chat_id") or "",
                "createdAt": data.get("created_at") or "",
                "cliPrefix": data.get("cli_prefix") or DEFAULT_CLI,
                "inputDraft": data.get("input_draft") or "",
                "cmdPreview": data.get("cmd_preview") or "\u2014",
                "messages": data.get("messages") or [],
                "schedulerEnabled": bool(data.get("scheduler_enabled")),
                "schedule": data.get("schedule") or DEFAULT_SCHEDULE,
                "schedulerPrompt": data.get("scheduler_prompt") or "",
            }
        )
    return out


def _default_state() -> dict:
    tid = "draft_" + uuid.uuid4().hex[:12]
    return {
        "tabs": [
            {
                "id": tid,
                "slug": None,
                "title": "Chat 1",
                "chatId": "",
                "cliPrefix": DEFAULT_CLI,
                "cmdPreview": "\u2014",
                "messages": [],
                "schedulerEnabled": False,
                "schedule": DEFAULT_SCHEDULE,
                "schedulerPrompt": "",
            }
        ],
        "active_tab_id": tid,
    }


class TabState(BaseModel):
    id: str = ""
    slug: str | None = None
    title: str = ""
    chatId: str = ""
    createdAt: str = ""
    cliPrefix: str = DEFAULT_CLI
    inputDraft: str = ""
    cmdPreview: str = "\u2014"
    messages: list[dict] = Field(default_factory=list)
    schedulerEnabled: bool = False
    schedule: str = DEFAULT_SCHEDULE
    schedulerPrompt: str = ""


class PersistBody(BaseModel):
    client_id: str = ""
    active_tab_id: str = ""
    tabs: list[TabState] = Field(default_factory=list)


class NewChatBody(BaseModel):
    session_name: str = Field(..., min_length=1)
    replace_slug: str | None = None


class ChatRequest(BaseModel):
    cli_prefix: str = Field(default=DEFAULT_CLI)
    chat_id: str
    slug: str
    message: str


class StopAgentBody(BaseModel):
    slug: str


class PreviewCmdBody(BaseModel):
    cli_prefix: str = Field(default=DEFAULT_CLI)
    chat_id: str
    slug: str
    message: str = ""


class AgentsMdBody(BaseModel):
    slug: str
    content: str = ""


class McpConfigBody(BaseModel):
    content: str = ""


class OpenWorkspaceBody(BaseModel):
    slug: str


class InboxUploadBody(BaseModel):
    slug: str
    filename: str = ""
    content_type: str = ""
    data_base64: str


class DeleteSessionBody(BaseModel):
    slug: str


class NewChatIdBody(BaseModel):
    slug: str


@app.get("/")
def root():
    index = STATIC / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


@app.get("/api/config")
def api_get_config():
    return {
        "default_cli_prefix": DEFAULT_CLI,
        "default_schedule": DEFAULT_SCHEDULE,
    }


@app.get("/api/state")
def api_get_state(client_id: str = ""):
    _chats_dir()
    meta_path = _app_state_path(client_id)
    meta = _read_json(meta_path)
    if meta and isinstance(meta.get("tabs"), list) and meta["tabs"]:
        tabs: list[dict] = []
        for t in meta["tabs"]:
            if not isinstance(t, dict):
                continue
            slug = t.get("slug")
            if slug:
                if not (CHATS / str(slug)).is_dir():
                    continue
            disk_chat_id = ""
            created_at = t.get("createdAt") or ""
            if not created_at and slug:
                disk = _read_json(CHATS / str(slug) / SESSION_NAME) or {}
                if isinstance(disk, dict):
                    disk_chat_id = disk.get("chat_id") or ""
                    created_at = disk.get("created_at") or ""
            elif slug:
                disk = _read_json(CHATS / str(slug) / SESSION_NAME) or {}
                if isinstance(disk, dict):
                    disk_chat_id = disk.get("chat_id") or ""
                    # Prefer disk timestamp when available so UI reflects latest
                    # chat_id rotation time for this specific tab.
                    created_at = disk.get("created_at") or created_at
            tabs.append(
                {
                    "id": t.get("id") or "",
                    "slug": slug,
                    "title": t.get("title") or "",
                    "chatId": disk_chat_id or t.get("chatId") or "",
                    "createdAt": created_at,
                    "cliPrefix": t.get("cliPrefix") or DEFAULT_CLI,
                    "inputDraft": t.get("inputDraft") if isinstance(t.get("inputDraft"), str) else "",
                    "cmdPreview": t.get("cmdPreview") if t.get("cmdPreview") else "\u2014",
                    "messages": t.get("messages") if isinstance(t.get("messages"), list) else [],
                    "schedulerEnabled": bool(t.get("schedulerEnabled")),
                    "schedule": t.get("schedule") or DEFAULT_SCHEDULE,
                    "schedulerPrompt": t.get("schedulerPrompt")
                    if isinstance(t.get("schedulerPrompt"), str)
                    else "",
                }
            )
        if tabs:
            _enrich_tabs_scheduler_next_run(tabs)
            active = meta.get("active_tab_id") or ""
            if not active or not any(x["id"] == active for x in tabs):
                active = tabs[0]["id"]
            return {"tabs": tabs, "active_tab_id": active}
    disk = _scan_disk_tabs()
    if disk:
        _enrich_tabs_scheduler_next_run(disk)
        active = disk[0]["id"]
        if _sanitize_client_id(client_id):
            _write_json_atomic(meta_path, {"version": 1, "active_tab_id": active, "tabs": disk})
        return {"tabs": disk, "active_tab_id": active}
    d = _default_state()
    _write_json_atomic(meta_path, {"version": 1, **d})
    return d


@app.post("/api/persist")
def api_persist(body: PersistBody):
    try:
        root = _chats_dir()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        serial_tabs: list[dict] = []
        for tab in body.tabs:
            d = tab.model_dump()
            serial_tabs.append(d)
            if tab.slug:
                slug = str(tab.slug)
                pdir = root / slug
                pdir.mkdir(parents=True, exist_ok=True)
                prev = _read_json(pdir / SESSION_NAME) or {}
                prev_created_at = prev.get("created_at") if isinstance(prev, dict) else ""
                session = {
                    "slug": slug,
                    "tab_id": tab.id,
                    "title": tab.title,
                    "chat_id": tab.chatId,
                    "created_at": tab.createdAt or prev_created_at or now,
                    "cli_prefix": tab.cliPrefix,
                    "input_draft": tab.inputDraft or "",
                    "cmd_preview": tab.cmdPreview,
                    "messages": tab.messages,
                    "updated_at": now,
                    "scheduler_enabled": tab.schedulerEnabled,
                    "schedule": tab.schedule or DEFAULT_SCHEDULE,
                    "scheduler_prompt": tab.schedulerPrompt or "",
                }
                _write_json_atomic(pdir / SESSION_NAME, session)
        meta = {
            "version": 1,
            "active_tab_id": body.active_tab_id,
            "tabs": serial_tabs,
            "saved_at": now,
        }
        _write_json_atomic(_app_state_path(body.client_id), meta)
        _sync_scheduler_from_tabs_payload(serial_tabs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"persist failed: {e!s}",
        ) from e
    return {"ok": True}


@app.post("/api/new-chat")
def api_new_chat(body: NewChatBody):
    old_slug = (body.replace_slug or "").strip()
    if old_slug:
        try:
            _session_path(old_slug)
        except ValueError:
            old_slug = ""
    base = _slugify(body.session_name)
    slug = _allocate_slug(base)
    ws = _chats_dir() / slug
    ws.mkdir(parents=True, exist_ok=False)
    try:
        chat_id = _new_chat_id()
    except Exception as e:
        try:
            ws.rmdir()
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=(str(e) or repr(e) or "new-chat failed")) from e
    tab_id = "tab_" + uuid.uuid4().hex[:16]
    title = body.session_name.strip() or slug
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    session = {
        "slug": slug,
        "tab_id": tab_id,
        "title": title,
        "chat_id": chat_id,
        "created_at": created_at,
        "cli_prefix": DEFAULT_CLI,
        "input_draft": "",
        "cmd_preview": "\u2014",
        "messages": [],
        "updated_at": created_at,
        "scheduler_enabled": False,
        "schedule": DEFAULT_SCHEDULE,
        "scheduler_prompt": "",
    }
    _write_json_atomic(ws / SESSION_NAME, session)
    (ws / AGENTS_MD_NAME).write_text(DEFAULT_AGENTS_MD, encoding="utf-8")
    _prepare_run_context(
        ws,
        "No human task yet. Wait for the next manual or scheduled instruction and then follow HUMAN.md first.",
        source="system",
    )
    if old_slug and old_slug != slug:
        old_dir = _chats_dir() / old_slug
        if old_dir.is_dir():
            shutil.rmtree(old_dir, ignore_errors=True)
    return {
        "chat_id": chat_id,
        "created_at": created_at,
        "slug": slug,
        "tab_id": tab_id,
        "title": title,
        "workspace_abs": str(ws.resolve()),
        "workspace_display": f"chats/{slug}",
    }


@app.post("/api/new-chat-id")
def api_new_chat_id(body: NewChatIdBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")

    sess = _read_json(ws / SESSION_NAME) or {}
    if not isinstance(sess, dict):
        raise HTTPException(500, "invalid session data")

    try:
        chat_id = _new_chat_id()
    except Exception as e:
        raise HTTPException(status_code=500, detail=(str(e) or repr(e) or "new-chat-id failed")) from e

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sess["chat_id"] = chat_id
    # Rotate display timestamp together with new chat_id.
    sess["created_at"] = now
    sess["updated_at"] = now
    _write_json_atomic(ws / SESSION_NAME, sess)
    return {"chat_id": chat_id, "created_at": sess.get("created_at") or ""}


@app.post("/api/preview-cmd")
def api_preview_cmd(body: PreviewCmdBody):
    slug = (body.slug or "").strip()
    try:
        ws = (_chats_dir() / slug).resolve()
    except Exception:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")

    cid = (body.chat_id or "").strip()
    if not cid:
        raise HTTPException(400, "chat_id is empty")

    try:
        tail = [] if _cli_prefix_uses_template(body.cli_prefix) else _cli_trailing_argv_preview(ws, body.message or "")
        args = _build_agent_argv(body.cli_prefix, cid, ws, tail)
        preview = _argv_to_preview(args)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return {"preview_cmd": preview}


@app.get("/api/agents-md")
def api_get_agents_md(slug: str):
    slug = (slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")
    path = ws / AGENTS_MD_NAME
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = DEFAULT_AGENTS_MD
    else:
        text = DEFAULT_AGENTS_MD
    return {"content": text}


@app.post("/api/agents-md")
def api_post_agents_md(body: AgentsMdBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")
    path = ws / AGENTS_MD_NAME
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"could not write AGENTS.md: {e}") from e
    return {"ok": True}


def _mcp_config_path() -> Path:
    # Project-level MCP config used by cursor-agent when chats/<slug>/ workspaces
    # walk up to the nearest .cursor/mcp.json. This is the "internal" config.
    return ROOT / ".cursor" / "mcp.json"


@app.get("/api/mcp-config")
def api_get_mcp_config():
    path = _mcp_config_path()
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise HTTPException(500, f"could not read mcp.json: {e}") from e
    else:
        text = '{\n  "mcpServers": {}\n}\n'
    return {"content": text, "path": str(path)}


@app.post("/api/mcp-config")
def api_post_mcp_config(body: McpConfigBody):
    text = body.content or ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e.msg} (line {e.lineno}, col {e.colno})") from e
    if not isinstance(parsed, dict):
        raise HTTPException(400, "mcp.json must be a JSON object at the top level")
    path = _mcp_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"could not write mcp.json: {e}") from e
    return {"ok": True, "path": str(path)}


@app.post("/api/open-workspace")
def api_open_workspace(body: OpenWorkspaceBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")
    path = str(ws.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", path], close_fds=True)
    except OSError as e:
        raise HTTPException(500, f"could not open folder: {e}") from e
    return {"ok": True}


@app.post("/api/open-assets")
def api_open_assets(body: OpenWorkspaceBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")
    assets = _cursor_assets_dir(ws)
    path = str(assets.resolve())
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", path], close_fds=True)
    except OSError as e:
        raise HTTPException(500, f"could not open assets folder: {e}") from e
    return {"ok": True}


@app.post("/api/inbox-upload")
def api_inbox_upload(body: InboxUploadBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")

    b64 = (body.data_base64 or "").strip()
    if not b64:
        raise HTTPException(400, "data_base64 is empty")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(400, "data_base64 is invalid") from None
    if not raw:
        raise HTTPException(400, "uploaded file is empty")

    prefix = _attachment_prefix_for_content_type(body.content_type, body.filename)
    token = _next_attachment_token(ws, prefix)
    suffix = _attachment_suffix_for_upload(body.content_type, body.filename)
    assets = _cursor_assets_dir(ws)
    path = assets / f"{token}{suffix}"
    try:
        path.write_bytes(raw)
    except OSError as e:
        raise HTTPException(500, f"could not save attachment: {e}") from e

    return {
        "ok": True,
        "token": token,
        "reference": f"@{token}",
        "filename": path.name,
        "absolute_path": str(path),
    }


@app.post("/api/delete-session")
def api_delete_session(body: DeleteSessionBody):
    slug = (body.slug or "").strip()
    try:
        ws = _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir():
        raise HTTPException(400, f"unknown session folder: {slug}")
    job_id = f"chat_sched_{slug}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    _chat_sched_sig.pop(slug, None)
    shutil.rmtree(ws, ignore_errors=True)
    return {"ok": True}


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    slug = (req.slug or "").strip()
    try:
        ws = (_chats_dir() / slug).resolve()
    except Exception:
        raise HTTPException(400, "invalid slug") from None
    if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
        raise HTTPException(400, f"unknown session slug: {slug}")

    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(400, "message is empty")

    try:
        _prepare_run_context(ws, msg, source="manual")
    except (OSError, RuntimeError) as e:
        raise HTTPException(500, f"could not prepare HUMAN.md or ATTACHMENTS.md: {e}") from e

    tail = [] if _cli_prefix_uses_template(req.cli_prefix) else _cli_trailing_argv_chat(ws, msg)
    try:
        args = _build_agent_argv(req.cli_prefix, req.chat_id, ws, tail)
        preview = _argv_to_preview(args)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    with _manual_agent_lock:
        exist = _manual_agent_state.get(slug)
        if exist and exist.get("proc") is not None and exist["proc"].poll() is None:
            raise HTTPException(409, "agent already running for this session") from None
        if exist and exist.get("result") is not None:
            _manual_agent_state.pop(slug, None)
        _manual_agent_state[slug] = {
            "proc": None,
            "result": None,
            "preview": preview,
            "user_stopped": False,
        }
    threading.Thread(
        target=_manual_agent_worker,
        args=(slug, args, str(ws), preview),
        daemon=True,
    ).start()
    return {"pending": True, "slug": slug, "preview_cmd": preview}


@app.get("/api/chat/status")
def api_chat_status(slug: str = Query(..., min_length=1)):
    slug = slug.strip()
    try:
        _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    with _manual_agent_lock:
        st = _manual_agent_state.get(slug)
        if not st:
            raise HTTPException(404, "no active manual run for this session") from None
        if st.get("result") is not None:
            res = dict(st["result"])
            _manual_agent_state.pop(slug, None)
            return {"running": False, **res}
        p = st.get("proc")
        if p is not None and p.poll() is None:
            return {"running": True, "preview_cmd": st.get("preview", "")}
        return {"running": True, "preview_cmd": st.get("preview", "")}


@app.post("/api/chat/stop")
def api_chat_stop(body: StopAgentBody):
    slug = (body.slug or "").strip()
    try:
        _workspace_for_slug(slug)
    except ValueError:
        raise HTTPException(400, "invalid slug") from None
    with _manual_agent_lock:
        st = _manual_agent_state.get(slug)
        if not st:
            return {"ok": True, "stopped": False}
        st["user_stopped"] = True
        p = st.get("proc")
        if p is not None and p.poll() is None:
            _terminate_process_tree(p.pid)
            return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL)
