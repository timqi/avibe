"""Read-only snapshot of currently-running agent instances.

This aggregator runs INSIDE the controller process (reachable only from the
controller's asyncio loop via ``core/internal_server.py``) because every
liveness source it reads is controller in-memory state:

- Claude: ``controller.claude_sessions`` (composite_key -> SDK client),
  ``claude_active_sessions`` (active turn set), ``session_last_activity``.
- Codex: ``CodexAgent._session_mgr`` / ``_turn_registry`` / ``_transports``
  (one transport/pid per working dir, shared by many sessions).
- OpenCode: ``OpenCodeAgent._active_requests`` (no OS subprocess / pid).
- Orphans: the persisted Claude process registry (``claude_processes.json``)
  for ``owner == "session"`` processes that no longer have a live SDK client.

Durations are computed HERE (the controller owns the ``time.monotonic()``
baselines; raw monotonic values are meaningless in the UI process).

Display metadata (platform / scope / title / workdir) is enriched from the
SQLite ``agent_sessions`` + ``scopes`` tables, joined by
``session_anchor == base_session_id``. ``agent_sessions.agent_status`` is NOT
trusted as a liveness source: it is only written for workbench sessions, so IM
truth comes solely from the in-memory registries above.

The snapshot is strictly read-only; it never mutates sessions, transports, the
process registry, or eviction state.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.controller import Controller

logger = logging.getLogger(__name__)


def _safe_call(fn, default):
    """Call ``fn`` tolerating ``RuntimeError`` from concurrent mutation.

    Like ``_safe_items`` but for callables that iterate live registries
    internally (e.g. ``CodexSessionManager.all_base_sessions`` unions three
    lock-free dicts that the controller loop may mutate while we run in a worker
    thread). Retries a few times, then returns ``default``.
    """
    for _ in range(3):
        try:
            return fn()
        except RuntimeError:
            continue
    return default


def _safe_items(mapping: Any) -> list:
    """``list(mapping.items())`` tolerant of concurrent mutation.

    The snapshot runs in a worker thread (``asyncio.to_thread``) while the
    controller loop may still mutate these registries, so a plain
    ``list(d.items())`` can raise ``RuntimeError: dictionary changed size during
    iteration``. Reuses ``_safe_call``'s retry-then-default behavior.
    """
    if not hasattr(mapping, "items"):
        return []
    return _safe_call(lambda: list(mapping.items()), [])


def _base_from_composite(composite_key: str) -> str:
    """Recover ``base_session_id`` from a Claude composite key.

    Composite keys are ``f"{base_session_id}:{working_path}"``; subagent bases
    themselves contain a colon (``{platform}_{thread}:{agent_name}``), so a
    naive ``split(":")[0]`` is wrong. ``working_path`` is an absolute path with
    no colon, so the base is everything before the LAST colon.
    """
    base, sep, _workdir = composite_key.rpartition(":")
    return base if sep else composite_key


def _split_scope_id(scope_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse ``<platform>::<scope_type>::<native_id>`` (see make_scope_id) into
    ``(platform, scope_type)``; either may be ``None`` when absent."""
    if not scope_id:
        return None, None
    parts = scope_id.split("::")
    platform = parts[0] or None
    scope_type = parts[1] if len(parts) >= 2 else None
    return platform, scope_type


def _make_row(
    *,
    backend: str,
    state: str,
    base_session_id: Optional[str] = None,
    composite_key: Optional[str] = None,
    workdir: Optional[str] = None,
    pid: Optional[int] = None,
    pid_shared: bool = False,
    native_session_id: Optional[str] = None,
    model: Optional[str] = None,
    elapsed_seconds: Optional[float] = None,
) -> dict[str, Any]:
    """Build one normalized running-agent row.

    ``state`` is one of ``active`` (turn in flight), ``idle`` (connected but no
    active turn), ``orphan`` (process in registry with no live session).
    ``elapsed_seconds`` is seconds since the last recorded activity (for active
    rows it reads as "busy for", for idle rows as "idle for"); ``None`` when the
    baseline is unknown.
    """
    return {
        "backend": backend,
        "state": state,
        "base_session_id": base_session_id,
        "composite_key": composite_key,
        "workdir": workdir,
        "pid": pid,
        "pid_shared": pid_shared,
        "native_session_id": native_session_id,
        "model": model,
        "elapsed_seconds": (round(elapsed_seconds, 1) if isinstance(elapsed_seconds, (int, float)) else None),
        # Filled by _enrich_from_db when a matching agent_sessions row exists.
        "session_id": None,
        "title": None,
        "platform": None,
        "scope_type": None,
        "scope_display_name": None,
        "trigger_source": None,
        "agent_name": None,
        "openable_in_chat": False,
    }


def _collect_claude(
    controller: "Controller", now: float, seen_native: set[str], seen_pids: set[int]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from modules.agents.claude_process_reaper import get_claude_client_pid
    except Exception:  # noqa: BLE001
        get_claude_client_pid = lambda _client: None  # type: ignore[assignment]

    sessions = getattr(controller, "claude_sessions", {}) or {}
    active = getattr(controller, "claude_active_sessions", set()) or set()
    last_activity = getattr(controller, "session_last_activity", {}) or {}

    for composite_key, client in _safe_items(sessions):
        # composite_key is ``{base}:{abs_workdir}``; split once for both halves.
        ck_base, ck_sep, ck_workdir = composite_key.rpartition(":")
        base = getattr(client, "_vibe_runtime_base_session_id", None) or (ck_base if ck_sep else composite_key)
        native = getattr(client, "_vibe_native_session_id", None)
        model = getattr(client, "_vibe_current_model", None)
        pid = get_claude_client_pid(client)
        is_active = composite_key in active
        la = last_activity.get(composite_key)
        elapsed = (now - la) if isinstance(la, (int, float)) else None
        rows.append(
            _make_row(
                backend="claude",
                state="active" if is_active else "idle",
                base_session_id=base,
                composite_key=composite_key,
                workdir=(ck_workdir or None) if ck_sep else None,
                pid=pid,
                native_session_id=native,
                model=model,
                elapsed_seconds=elapsed,
            )
        )
        if native:
            seen_native.add(str(native))
        if isinstance(pid, int):
            seen_pids.add(pid)
    return rows


def _collect_codex(controller: "Controller") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    agent = _get_agent(controller, "codex")
    if agent is None:
        return rows
    session_mgr = getattr(agent, "_session_mgr", None)
    turn_registry = getattr(agent, "_turn_registry", None)
    transports = getattr(agent, "_transports", {}) or {}
    if session_mgr is None:
        return rows

    # Count sessions per cwd so the UI can flag a pid shared across sessions.
    # ``all_base_sessions`` unions three lock-free dicts internally, so guard it
    # against concurrent mutation (we run in a worker thread, §to_thread).
    base_ids = list(_safe_call(session_mgr.all_base_sessions, []))
    # Resolve each base's cwd once, then count sessions per cwd (so the UI can
    # flag a pid shared across sessions) — avoids a second get_cwd pass.
    cwd_by_base: dict[str, Optional[str]] = {base: session_mgr.get_cwd(base) for base in base_ids}
    cwd_session_count: dict[str, int] = {}
    for cwd in cwd_by_base.values():
        if cwd:
            cwd_session_count[cwd] = cwd_session_count.get(cwd, 0) + 1

    for base in base_ids:
        cwd = cwd_by_base.get(base)
        active_turn = turn_registry.get_active_turn(base) if turn_registry is not None else None
        transport = transports.get(cwd) if cwd else None
        pid = getattr(transport, "pid", None) if transport is not None else None
        # No per-session elapsed for codex: ``_transport_last_activity`` is keyed
        # by cwd (shared across every session on that transport) and is touched on
        # every streaming event, so it reflects neither this session's turn
        # duration nor its idle time. Report ``None`` rather than a misleading
        # value; the row still shows backend / state / pid_shared.
        rows.append(
            _make_row(
                backend="codex",
                state="active" if active_turn else "idle",
                base_session_id=base,
                workdir=cwd,
                pid=pid,
                pid_shared=bool(cwd and cwd_session_count.get(cwd, 0) > 1),
                elapsed_seconds=None,
            )
        )
    return rows


def _collect_opencode(controller: "Controller") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    agent = _get_agent(controller, "opencode")
    if agent is None:
        return rows
    # OpenCode has no owned subprocess (HTTP server + poll loop), so no pid.
    # ``_active_requests`` only holds IN-FLIGHT turn tasks (popped in a finally
    # once the turn settles), so OpenCode only ever surfaces as ``active`` here —
    # connected-but-idle OpenCode sessions are not represented (D2 is honored for
    # claude/codex; OpenCode has no idle-session registry to enumerate).
    active_requests = getattr(agent, "_active_requests", {}) or {}
    for base, task in _safe_items(active_requests):
        if bool(getattr(task, "done", lambda: False)()):
            continue  # finishing — being popped; not a live turn
        rows.append(
            _make_row(
                backend="opencode",
                state="active",
                base_session_id=base,
            )
        )
    return rows


def _collect_orphans(seen_native: set[str], seen_pids: set[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from modules.agents.claude_process_reaper import (
            AVIBE_CLAUDE_SESSION_OWNER,
            _load_owned_process_registry,
            _process_ages,
            _process_start_time,
        )
    except Exception:  # noqa: BLE001
        return rows
    try:
        registry = _load_owned_process_registry()
    except Exception:  # noqa: BLE001
        logger.debug("running_agents: failed to read process registry", exc_info=True)
        return rows

    # Filter to session-owned registry rows not already backed by a live client.
    # The registry accumulates stale rows (the reaper prunes lazily), so an entry
    # alone proves nothing — we must verify the process is genuinely alive. A
    # missing/non-numeric ``started_at`` means we cannot prove identity, so we
    # skip it rather than risk a false orphan from pid reuse.
    candidates: list[tuple[Any, int, Optional[str], float]] = []
    for record in registry:
        if getattr(record, "owner", None) != AVIBE_CLAUDE_SESSION_OWNER:
            continue
        native = getattr(record, "native_session_id", None)
        if native and str(native) in seen_native:
            continue  # still owned by a live session — not an orphan
        record_pid = getattr(record, "pid", None)
        if not isinstance(record_pid, int) or record_pid in seen_pids:
            continue
        recorded = getattr(record, "started_at", None)
        if not isinstance(recorded, (int, float)):
            continue  # identity unprovable — don't risk a false orphan
        candidates.append((record, record_pid, native, float(recorded)))

    if not candidates:
        return rows

    # ONE batched ``ps`` to drop dead/stale pids (the common case: e.g. 88 stale
    # dead entries collapse to zero here with a single subprocess call). Only the
    # few survivors get the precise per-pid start-time identity check below, so
    # we never fan out N blocking ``ps`` calls on the controller loop.
    ages = _process_ages({pid for _, pid, _, _ in candidates})
    for record, record_pid, native, recorded in candidates:
        if record_pid not in ages:
            continue  # not alive — stale registry entry, not a leak
        current_started_at = _process_start_time(record_pid)
        if current_started_at is None:
            continue
        if abs(current_started_at - recorded) >= 1.0:
            continue  # pid was reused by a different process
        rows.append(
            _make_row(
                backend="claude",
                state="orphan",
                pid=record_pid,
                native_session_id=native,
                elapsed_seconds=ages.get(record_pid),
            )
        )
    return rows


def _get_agent(controller: "Controller", name: str):
    service = getattr(controller, "agent_service", None)
    if service is None:
        return None
    agents = getattr(service, "agents", {}) or {}
    return agents.get(name)


def _enrich_from_db(rows: list[dict[str, Any]]) -> None:
    """Best-effort: fill platform/scope/title/session_id from agent_sessions.

    Joins by ``session_anchor == base_session_id``. Failure is non-fatal: rows
    keep their in-memory truth and just lack display metadata.
    """
    anchors = sorted({r["base_session_id"] for r in rows if r.get("base_session_id")})
    if not anchors:
        return
    try:
        from sqlalchemy import select

        from storage.db import create_sqlite_engine
        from storage.models import agent_sessions, scopes
    except Exception:  # noqa: BLE001
        return

    meta_by_anchor: dict[str, dict[str, Any]] = {}
    try:
        # Runs controller-side (this aggregator is only reached from the
        # controller's internal server), so ``create_sqlite_engine()`` resolves
        # the same DB the controller writes. Outer-join ``scopes`` so platform /
        # scope_type / display_name come from the canonical columns rather than
        # only string-splitting ``scope_id``.
        engine = create_sqlite_engine()
        with engine.connect() as conn:
            result = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.session_anchor,
                    agent_sessions.c.scope_id,
                    agent_sessions.c.title,
                    agent_sessions.c.workdir,
                    agent_sessions.c.agent_name,
                    agent_sessions.c.last_active_at,
                    scopes.c.platform.label("scope_platform"),
                    scopes.c.scope_type.label("scope_scope_type"),
                    scopes.c.display_name.label("scope_display_name"),
                    scopes.c.native_type.label("scope_native_type"),
                )
                .select_from(agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id))
                .where(agent_sessions.c.session_anchor.in_(anchors))
            ).mappings()
            for row in result:
                anchor = row["session_anchor"]
                # Prefer the most-recently-active row when an anchor has several.
                existing = meta_by_anchor.get(anchor)
                if existing is None or (row["last_active_at"] or "") > (existing.get("last_active_at") or ""):
                    meta_by_anchor[anchor] = dict(row)
    except Exception:  # noqa: BLE001
        logger.debug("running_agents: db enrichment failed", exc_info=True)
        return

    for r in rows:
        meta = meta_by_anchor.get(r.get("base_session_id"))
        if meta:
            _apply_session_meta(r, meta)


def _apply_session_meta(r: dict[str, Any], meta: dict[str, Any]) -> None:
    """Fold one ``agent_sessions``⟕``scopes`` row into a running-agent row.

    Pure (no I/O) so it can be unit-tested without a DB.
    """
    scope_id = meta.get("scope_id")
    # Canonical scopes columns win; fall back to splitting scope_id when the
    # scope row is missing (FK is nullable / SET NULL).
    split_platform, split_scope_type = _split_scope_id(scope_id)
    platform = meta.get("scope_platform") or split_platform
    # Private agent runs (``vibe agent run`` / one agent invoking another) are
    # NOT IM sessions: ``reserve_private_agent_session`` stamps the scope with a
    # PLACEHOLDER platform (the configured primary platform — slack/discord/…,
    # only "slack" when config is unreadable) but marks it
    # ``native_type="private_agent_run"``. So keying off ``native_type`` (not the
    # platform) catches these uniformly on every install. Without this guard the
    # row would be mislabeled as a real IM session. Treat it as an agent-initiated
    # internal run: no IM platform, ``trigger_source="agent"``, not chat-openable.
    is_private_run = meta.get("scope_native_type") == "private_agent_run"
    r["session_id"] = meta.get("id")
    r["title"] = meta.get("title")
    r["platform"] = None if is_private_run else platform
    r["scope_type"] = meta.get("scope_scope_type") or split_scope_type
    r["scope_display_name"] = meta.get("scope_display_name")
    r["agent_name"] = meta.get("agent_name")
    if not r.get("workdir"):
        r["workdir"] = meta.get("workdir")
    # Trigger source: agent-initiated for private agent runs; otherwise human.
    # (scheduled/watch/webhook/callback are only asserted when a harness run
    # reliably links them, which is not modeled here.)
    r["trigger_source"] = "agent" if is_private_run else "human"
    # Private agent runs are internal (``no_delivery``) — not user chat sessions —
    # so they are not openable. Any other persisted session IS openable in the
    # workbench Chat by its id, IM sessions included (mirrors how the Inbox /
    # sidebar navigate to ``/chat/<session_id>``).
    r["openable_in_chat"] = bool(meta.get("id")) and not is_private_run


async def _end_orphan_pid(pid: int) -> dict[str, Any]:
    """SIGTERM→SIGKILL a leaked Claude process, but ONLY after re-verifying it is
    an avibe-owned, still-alive, identity-matching registry entry — so a bogus or
    reused pid from the client can never make us kill an unrelated process."""
    try:
        from modules.agents.claude_process_reaper import (
            AVIBE_CLAUDE_SESSION_OWNER,
            _load_owned_process_registry,
            _process_start_time,
            _reap_pid_set,
        )
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "reaper_unavailable"}
    record = None
    for r in _load_owned_process_registry():
        if getattr(r, "pid", None) == pid and getattr(r, "owner", None) == AVIBE_CLAUDE_SESSION_OWNER:
            record = r
            break
    if record is None:
        return {"ok": False, "error": "not_an_owned_process", "pid": pid}
    current = _process_start_time(pid)
    if current is None:
        return {"ok": False, "error": "process_not_alive", "pid": pid}
    recorded = getattr(record, "started_at", None)
    # FAIL CLOSED: refuse to kill unless identity is provable. A record with a
    # missing/non-numeric ``started_at`` (e.g. the registration-time ``ps`` failed)
    # cannot be distinguished from a reused pid, so we never SIGKILL it — matching
    # the read path's conservatism (``_collect_orphans`` skips such records too).
    if not isinstance(recorded, (int, float)):
        return {"ok": False, "error": "identity_unprovable", "pid": pid}
    if abs(current - recorded) >= 1.0:
        return {"ok": False, "error": "pid_reused", "pid": pid}
    # NOTE: a microscopic TOCTOU remains between this check and the signal (the pid
    # could exit + be reused in the gap); it's inherent to pid-based signaling and
    # negligible vs. the registration→kill window the start-time match already covers.
    killed = await _reap_pid_set({pid}, terminate_timeout=2.0, logger=logger)
    return {"ok": killed > 0, "action": "killed_process", "pid": pid}


def _find_claude_composite_for_base(session_handler: Any, base_session_id: Optional[str]) -> Optional[str]:
    if not base_session_id:
        return None
    sessions = getattr(session_handler, "claude_sessions", {}) or {}
    for composite_key, client in _safe_items(sessions):
        client_base = getattr(client, "_vibe_runtime_base_session_id", None) or _base_from_composite(composite_key)
        if client_base == base_session_id:
            return composite_key
    return None


async def _end_claude(controller: "Controller", composite_key: Optional[str], base_session_id: Optional[str]) -> dict[str, Any]:
    session_handler = getattr(controller, "session_handler", None)
    if session_handler is None:
        return {"ok": False, "error": "session_handler_unavailable"}
    ck = composite_key or _find_claude_composite_for_base(session_handler, base_session_id)
    sessions = getattr(session_handler, "claude_sessions", {}) or {}
    client = sessions.get(ck) if ck else None
    if client is None:
        return {"ok": False, "error": "session_not_live"}
    # Interrupt any in-flight turn first (best-effort), then disconnect + free the
    # SDK client / subprocess via the same path idle-eviction uses.
    try:
        if hasattr(client, "interrupt"):
            await client.interrupt()
    except Exception:  # noqa: BLE001
        logger.debug("end: claude interrupt failed for %s", ck, exc_info=True)
    try:
        await session_handler.cleanup_session(ck)
    except Exception as exc:  # noqa: BLE001
        logger.warning("end: claude cleanup_session failed for %s: %s", ck, exc)
        return {"ok": False, "error": "cleanup_failed", "detail": str(exc)}
    return {"ok": True, "action": "ended", "backend": "claude"}


async def _end_codex(controller: "Controller", base_session_id: Optional[str]) -> dict[str, Any]:
    if not base_session_id:
        return {"ok": False, "error": "base_session_id_required"}
    agent = _get_agent(controller, "codex")
    if agent is None:
        return {"ok": False, "error": "codex_unavailable"}
    session_mgr = getattr(agent, "_session_mgr", None)
    turn_registry = getattr(agent, "_turn_registry", None)
    transports = getattr(agent, "_transports", {}) or {}
    if session_mgr is None or turn_registry is None:
        return {"ok": False, "error": "codex_registries_unavailable"}
    cwd = session_mgr.get_cwd(base_session_id)
    thread_id = session_mgr.get_thread_id(base_session_id)
    turn_id = turn_registry.get_active_turn(base_session_id)
    transport = transports.get(cwd) if cwd else None
    # Interrupt the active turn (the shared app-server transport stays up for
    # other sessions on the same cwd); then clear THIS session's thread/turn state.
    interrupted = False
    if turn_id and thread_id and transport is not None:
        try:
            await transport.send_request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            interrupted = True
        except Exception:  # noqa: BLE001
            logger.debug("end: codex turn/interrupt failed for %s", base_session_id, exc_info=True)
    try:
        session_mgr.invalidate_thread(base_session_id)
        turn_registry.clear_session(base_session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("end: codex clear failed for %s: %s", base_session_id, exc)
        return {"ok": False, "error": "clear_failed", "detail": str(exc)}
    # ``interrupted`` is False when there was no active turn to stop (idle/stale):
    # the session state is still cleared, but the caller can tell nothing was
    # actively interrupted.
    return {"ok": True, "action": "ended", "backend": "codex", "interrupted": interrupted}


async def _end_opencode(controller: "Controller", base_session_id: Optional[str]) -> dict[str, Any]:
    if not base_session_id:
        return {"ok": False, "error": "base_session_id_required"}
    agent = _get_agent(controller, "opencode")
    if agent is None:
        return {"ok": False, "error": "opencode_unavailable"}
    active_requests = getattr(agent, "_active_requests", {}) or {}
    task = active_requests.get(base_session_id)
    # Best-effort remote abort (so the OpenCode server stops the run too), then
    # cancel the local polling task.
    session_mgr = getattr(agent, "_session_manager", None)
    try:
        get_req = getattr(session_mgr, "get_request_session", None)
        req_info = get_req(base_session_id) if callable(get_req) else None
        if req_info:
            server = await agent._get_server()
            await server.abort_session(req_info[0], req_info[1])
    except Exception:  # noqa: BLE001
        logger.debug("end: opencode remote abort failed for %s", base_session_id, exc_info=True)
    if task is not None and not task.done():
        task.cancel()
    return {"ok": True, "action": "ended", "backend": "opencode"}


async def end_running_agent(
    controller: "Controller",
    *,
    backend: Optional[str] = None,
    state: Optional[str] = None,
    composite_key: Optional[str] = None,
    base_session_id: Optional[str] = None,
    pid: Optional[int] = None,
) -> dict[str, Any]:
    """Terminate a running agent's LIVE runtime, dispatched by backend + state.

    - orphan → SIGTERM/SIGKILL the leaked (verified, avibe-owned) process.
    - claude → interrupt the turn + disconnect the SDK client (frees subprocess).
    - codex  → interrupt the turn + clear the session's thread/turn state (the
      shared app-server stays up for other sessions on the same cwd).
    - opencode → abort the remote run + cancel the local polling task.

    Runs on the controller event loop (mutates loop-owned registries / awaits
    backend coroutines). There is deliberately NO self-protection: ending the
    current session / avibe's own runtime is allowed by design.
    """
    if state == "orphan":
        if not isinstance(pid, int):
            return {"ok": False, "error": "pid_required_for_orphan"}
        return await _end_orphan_pid(pid)
    if backend == "claude":
        return await _end_claude(controller, composite_key, base_session_id)
    if backend == "codex":
        return await _end_codex(controller, base_session_id)
    if backend == "opencode":
        return await _end_opencode(controller, base_session_id)
    # Fallback: a pid-only target with no backend is treated as an orphan kill.
    if isinstance(pid, int):
        return await _end_orphan_pid(pid)
    return {"ok": False, "error": "unknown_target"}


def snapshot_running_agents(controller: "Controller") -> dict[str, Any]:
    """Return a read-only snapshot of all currently-running agent instances.

    Shape::

        {
          "ok": True,
          "agents": [ { backend, state, base_session_id, platform, scope_type,
                        scope_display_name, title, workdir, pid, pid_shared,
                        native_session_id, model, elapsed_seconds, trigger_source,
                        session_id, agent_name, openable_in_chat }, ... ],
          "counts": { "total", "active", "idle", "orphan",
                      "by_backend": {claude, codex, opencode} },
        }

    One row per live SESSION (F1): a Codex pid shared by several sessions yields
    one row per session, each flagged ``pid_shared``.
    """
    # ``now`` is MONOTONIC: only valid against the controller's monotonic
    # activity baselines (claude/codex). The orphan path deliberately uses a
    # separate wall-clock baseline (``time.time()`` vs ``ps`` start time) — do
    # NOT feed this ``now`` into orphan elapsed math.
    now = time.monotonic()
    seen_native: set[str] = set()
    seen_pids: set[int] = set()
    rows: list[dict[str, Any]] = []
    rows.extend(_collect_claude(controller, now, seen_native, seen_pids))
    rows.extend(_collect_codex(controller))
    rows.extend(_collect_opencode(controller))
    rows.extend(_collect_orphans(seen_native, seen_pids))

    _enrich_from_db(rows)

    # Single pass over rows for both the state tallies and the per-backend counts.
    states: dict[str, int] = {"active": 0, "idle": 0, "orphan": 0}
    by_backend: dict[str, int] = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
        by_backend[r["backend"]] = by_backend.get(r["backend"], 0) + 1

    return {
        "ok": True,
        "agents": rows,
        "counts": {
            "total": len(rows),
            "active": states["active"],
            "idle": states["idle"],
            "orphan": states["orphan"],
            "by_backend": by_backend,
        },
    }
