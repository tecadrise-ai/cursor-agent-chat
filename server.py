"""
Standalone test server: Cursor CLI chat with --resume session id.
Each named session uses ./chats/<slug>/ as workspace; session.json + _app_state.json persist UI.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _default_config() -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": 8765, "log_level": "info"},
        "app": {"title": "Agent Chat CLI Test"},
        "cors": {"allow_origins": ["*"]},
        "paths": {
            "static_dir": "static",
            "chats_dir": "chats",
            "session_file": "session.json",
            "app_state_file": "_app_state.json",
            "agents_md_file": "AGENTS.md",
        },
        "defaults": {
            "cli_prefix": "agent -p --trust --yolo --approve-mcps --model auto",
            "agents_md_default": "You are a helpfull virtual assistant.",
            "schedule": "interval:15m",
        },
        "cli_trailing": {"instructions_argv": ["read", "instructions", "from", "AGENTS.md"]},
        "agent": {"create_chat_argv": ["agent", "create-chat"]},
        "scheduler": {"misfire_grace_time": 43200},
        "timeouts": {"agent_seconds": 3600, "create_chat_seconds": 120},
    }


def _load_config() -> dict:
    base = _default_config()
    if not CONFIG_PATH.is_file():
        return base
    try:
        raw = CONFIG_PATH.read_bytes().decode("utf-8")
        if sys.version_info >= (3, 11):
            import tomllib

            user = tomllib.loads(raw)
        else:
            import tomli

            user = tomli.loads(raw)
    except Exception as e:
        print(f"Warning: could not load {CONFIG_PATH}: {e} — using defaults", file=sys.stderr)
        return base
    return _deep_merge(base, user)


def _coerce_str_list(val, fallback: list[str]) -> list[str]:
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
            return list(fallback)
        if parts:
            return parts
    return list(fallback)


def _coerce_cors_origins(val) -> list[str]:
    if isinstance(val, str) and val.strip():
        parts = [x.strip() for x in val.replace(";", ",").split(",") if x.strip()]
        return parts if parts else ["*"]
    if isinstance(val, list) and val:
        return [str(x).strip() for x in val if str(x).strip()]
    return ["*"]


_FB_INSTR = ["read", "instructions", "from", "AGENTS.md"]
_FB_CREATE = ["agent", "create-chat"]

_CFG = _load_config()
_SERVER = _CFG["server"]
_APP = _CFG["app"]
_CORS = _CFG["cors"]
_PATHS = _CFG["paths"]
_DEFAULTS = _CFG["defaults"]
_CLI_TRAIL = _CFG["cli_trailing"]
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
DEFAULT_AGENTS_MD = str(_DEFAULTS["agents_md_default"])
DEFAULT_CLI = str(_DEFAULTS["cli_prefix"])
DEFAULT_SCHEDULE = str(_DEFAULTS["schedule"])
AGENTS_MD_ARGV = tuple(_coerce_str_list(_CLI_TRAIL.get("instructions_argv"), _FB_INSTR))
CREATE_CHAT_ARGV = _coerce_str_list(_AGENT_CMD.get("create_chat_argv"), _FB_CREATE)
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


scheduler = BackgroundScheduler()
_scheduler_slug_lock = threading.Lock()
_scheduler_running_slugs: set[str] = set()
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
    meta_path = CHATS / APP_STATE_NAME
    meta = _read_json(meta_path)
    if not meta or not isinstance(meta.get("tabs"), list):
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tabs = meta["tabs"]
    for i, t in enumerate(tabs):
        if not isinstance(t, dict) or t.get("slug") != slug:
            continue
        msgs = list(t.get("messages") or [])
        stamp_user = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        msgs.append({"role": "user", "text": user_text, "ts": stamp_user})
        stamp_agent = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        msgs.append({"role": "agent", "text": agent_text, "err": is_err, "ts": stamp_agent})
        t["messages"] = msgs
        tabs[i] = t
        pdir = root / slug
        if pdir.is_dir():
            sess = _read_json(pdir / SESSION_NAME) or {}
            sess["messages"] = msgs
            sess["updated_at"] = now
            _write_json_atomic(pdir / SESSION_NAME, sess)
        break
    meta["tabs"] = tabs
    meta["saved_at"] = now
    _write_json_atomic(meta_path, meta)


def execute_scheduled_chat(slug: str) -> None:
    with _scheduler_slug_lock:
        if slug in _scheduler_running_slugs:
            return
        _scheduler_running_slugs.add(slug)
    try:
        meta_path = CHATS / APP_STATE_NAME
        meta = _read_json(meta_path)
        if not meta:
            return
        tab = None
        for t in meta.get("tabs") or []:
            if isinstance(t, dict) and t.get("slug") == slug:
                tab = t
                break
        if not tab or not tab.get("schedulerEnabled"):
            return
        prompt = (tab.get("schedulerPrompt") or "").strip()
        if not prompt:
            return
        chat_id = (tab.get("chatId") or "").strip()
        cli_prefix = tab.get("cliPrefix") or DEFAULT_CLI
        if not chat_id:
            return
        try:
            ws = _workspace_for_slug(slug)
        except ValueError:
            return
        if not ws.is_dir() or not (ws / SESSION_NAME).is_file():
            return
        tail = _cli_trailing_argv_chat(ws, prompt)
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
    meta = _read_json(CHATS / APP_STATE_NAME)
    if not meta or not isinstance(meta.get("tabs"), list):
        return
    _sync_scheduler_from_tabs_payload(meta["tabs"])


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


def _build_agent_argv(
    cli_prefix: str,
    chat_id: str,
    workspace: Path,
    trailing: list[str],
) -> list[str]:
    parts = _split_cli_prefix(cli_prefix)
    return parts + [
        "--resume",
        chat_id.strip(),
        "--workspace",
        str(workspace),
    ] + trailing


def _argv_to_preview(argv: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def _cli_trailing_argv_chat(ws: Path, user_message: str) -> list[str]:
    u = user_message.strip()
    if not (ws / AGENTS_MD_NAME).is_file():
        return [u]
    return list(AGENTS_MD_ARGV) + [u]


def _cli_trailing_argv_preview(ws: Path, user_message: str) -> list[str]:
    u = (user_message or "").strip()
    if not (ws / AGENTS_MD_NAME).is_file():
        return [u if u else "<message>"]
    if not u:
        return list(AGENTS_MD_ARGV)
    return list(AGENTS_MD_ARGV) + [u]


def _run_agent(argv: list[str], *, cwd: str | None, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    if sys.platform == "win32":
        line = subprocess.list2cmdline(argv)
        return subprocess.run(
            line,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
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
                "cliPrefix": data.get("cli_prefix") or DEFAULT_CLI,
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
    cliPrefix: str = DEFAULT_CLI
    cmdPreview: str = "\u2014"
    messages: list[dict] = Field(default_factory=list)
    schedulerEnabled: bool = False
    schedule: str = DEFAULT_SCHEDULE
    schedulerPrompt: str = ""


class PersistBody(BaseModel):
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


class PreviewCmdBody(BaseModel):
    cli_prefix: str = Field(default=DEFAULT_CLI)
    chat_id: str
    slug: str
    message: str = ""


class AgentsMdBody(BaseModel):
    slug: str
    content: str = ""


class OpenWorkspaceBody(BaseModel):
    slug: str


class DeleteSessionBody(BaseModel):
    slug: str


@app.get("/")
def root():
    index = STATIC / "index.html"
    if not index.is_file():
        raise HTTPException(404, "static/index.html missing")
    return FileResponse(index)


@app.get("/api/state")
def api_get_state():
    _chats_dir()
    meta_path = CHATS / APP_STATE_NAME
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
            tabs.append(
                {
                    "id": t.get("id") or "",
                    "slug": slug,
                    "title": t.get("title") or "",
                    "chatId": t.get("chatId") or "",
                    "cliPrefix": t.get("cliPrefix") or DEFAULT_CLI,
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
        return {"tabs": disk, "active_tab_id": disk[0]["id"]}
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
                session = {
                    "slug": slug,
                    "tab_id": tab.id,
                    "title": tab.title,
                    "chat_id": tab.chatId,
                    "cli_prefix": tab.cliPrefix,
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
        _write_json_atomic(CHATS / APP_STATE_NAME, meta)
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
    session = {
        "slug": slug,
        "tab_id": tab_id,
        "title": title,
        "chat_id": chat_id,
        "cli_prefix": DEFAULT_CLI,
        "cmd_preview": "\u2014",
        "messages": [],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scheduler_enabled": False,
        "schedule": DEFAULT_SCHEDULE,
        "scheduler_prompt": "",
    }
    _write_json_atomic(ws / SESSION_NAME, session)
    (ws / AGENTS_MD_NAME).write_text(DEFAULT_AGENTS_MD, encoding="utf-8")
    if old_slug and old_slug != slug:
        old_dir = _chats_dir() / old_slug
        if old_dir.is_dir():
            shutil.rmtree(old_dir, ignore_errors=True)
    return {
        "chat_id": chat_id,
        "slug": slug,
        "tab_id": tab_id,
        "title": title,
        "workspace_abs": str(ws.resolve()),
        "workspace_display": f"chats/{slug}",
    }


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
        tail = _cli_trailing_argv_preview(ws, body.message or "")
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

    tail = _cli_trailing_argv_chat(ws, msg)
    try:
        args = _build_agent_argv(req.cli_prefix, req.chat_id, ws, tail)
        preview = _argv_to_preview(args)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        r = _run_agent(args, cwd=str(ws), timeout=TIMEOUT_AGENT_S)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"agent timed out ({TIMEOUT_AGENT_S}s)") from None

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    return {
        "returncode": r.returncode,
        "stdout": out,
        "stderr": err,
        "preview_cmd": preview,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL)
