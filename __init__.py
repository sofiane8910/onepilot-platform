"""Onepilot channel plugin for Hermes — chat I/O + cron delivery."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes_plugins.onepilot")

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.json"

HISTORY_LIMIT = 20

_WIRE_TOPIC_PREFIX = "realtime:"
_WIRE_EVENT_CHANGES = "postgres_changes"

_CRON_PATCHED = False


class _TerminalAuthError(Exception):
    pass


def register(ctx) -> None:
    if not CONFIG_PATH.exists():
        logger.warning("[onepilot] %s missing — plugin idle", CONFIG_PATH)
        return

    try:
        config = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        logger.error("[onepilot] config.json parse failed: %s", exc)
        return

    required = {
        "backendUrl", "streamUrl", "publishableKey", "agentKey",
        "userId", "agentProfileId", "sessionKey",
    }
    missing = required - set(config.keys())
    if missing:
        logger.error("[onepilot] config.json missing keys: %s — plugin idle", sorted(missing))
        return

    if config.get("enabled") is False:
        logger.info("[onepilot] plugin disabled in config")
        return

    _install_cron_channel(config)

    import threading

    def _thread_main() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run(config))
        except Exception as exc:
            logger.error("[onepilot] plugin thread crashed: %s", exc)
        finally:
            loop.close()

    threading.Thread(
        target=_thread_main,
        name="onepilot-plugin",
        daemon=True,
    ).start()

    user_short = str(config["userId"])[:8]
    agent_short = str(config["agentProfileId"])[:8]
    logger.info("[onepilot] plugin scheduled (user=%s agent=%s)", user_short, agent_short)


def _install_cron_channel(config: dict[str, Any]) -> None:
    """Register `onepilot` as a cron delivery channel by patching cron.scheduler."""
    global _CRON_PATCHED
    if _CRON_PATCHED:
        return
    try:
        import cron.scheduler as _sched
    except Exception as exc:
        logger.warning(
            "[onepilot] cron.scheduler import failed (%s); cron channel disabled", exc
        )
        return

    if not hasattr(_sched, "_KNOWN_DELIVERY_PLATFORMS") or not hasattr(
        _sched, "_deliver_result"
    ):
        logger.warning(
            "[onepilot] hermes cron internals changed; cron channel disabled"
        )
        return

    try:
        _sched._KNOWN_DELIVERY_PLATFORMS = _sched._KNOWN_DELIVERY_PLATFORMS | frozenset(
            {"onepilot"}
        )
    except Exception as exc:
        logger.warning(
            "[onepilot] failed to extend _KNOWN_DELIVERY_PLATFORMS (%s); cron channel disabled",
            exc,
        )
        return

    _orig_deliver = _sched._deliver_result

    def _patched(job, content, adapters=None, loop=None):
        deliver = str(job.get("deliver", "") or "")
        platform_names = [
            part.strip().split(":", 1)[0]
            for part in deliver.split(",")
            if part.strip()
        ]
        if "onepilot" not in platform_names:
            return _orig_deliver(job, content, adapters=adapters, loop=loop)

        if content and content.lstrip().startswith("[SILENT]"):
            return None

        try:
            err = _onepilot_deliver_sync(job, content, config)
        except Exception as exc:
            err = f"onepilot delivery failed: {exc}"
        if err:
            logger.warning("[onepilot] cron %s delivery failed: %s", job.get("id", "?"), err)
        return err

    _sched._deliver_result = _patched
    _CRON_PATCHED = True
    logger.info("[onepilot] cron delivery channel installed")


def _onepilot_deliver_sync(job: dict, content: str, config: dict[str, Any]) -> Optional[str]:
    import concurrent.futures

    coro = _onepilot_deliver(job, content, config)
    try:
        return asyncio.run(coro)
    except RuntimeError:
        coro.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _onepilot_deliver(job, content, config))
            return future.result(timeout=120)


async def _onepilot_deliver(
    job: dict, content: str, config: dict[str, Any]
) -> Optional[str]:
    import httpx

    deliver = str(job.get("deliver", "") or "")
    session_key = config["sessionKey"]
    for part in deliver.split(","):
        part = part.strip()
        if part.startswith("onepilot:"):
            tail = part.split(":", 1)[1].strip()
            if tail:
                session_key = tail
            break

    job_name = str(job.get("name") or job.get("id") or "").strip()
    text = content or ""
    if job_name:
        text = f"**{job_name}**\n\n{text}"

    body = {
        "userId": str(config["userId"]).lower(),
        "agentProfileId": str(config["agentProfileId"]).lower(),
        "sessionKey": session_key,
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "timestamp": int(time.time() * 1000),
    }
    url = f"{config['backendUrl']}/functions/v1/agent-message-ingest"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['agentKey']}",
    }
    body_str = json.dumps(body)

    delays = [0.25, 0.75]
    for attempt in range(len(delays) + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(url, headers=headers, content=body_str)
            if r.status_code == 200:
                logger.info(
                    "[onepilot] cron '%s' delivered (%d chars)",
                    job.get("id", "?"),
                    len(text),
                )
                return None
            if r.status_code < 500 or attempt == len(delays):
                return f"ingest {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            if attempt == len(delays):
                return f"ingest network error: {exc}"
        await asyncio.sleep(delays[attempt])
    return "ingest exhausted retries"


async def _run(config: dict[str, Any]) -> None:
    backoff = 1.0
    while True:
        try:
            await _connect_and_subscribe(config)
            backoff = 1.0
        except _TerminalAuthError as exc:
            logger.error("[onepilot] terminal auth: %s — channel idle", exc)
            return
        except Exception as exc:
            logger.warning("[onepilot] subscription error: %s — reconnect in %.0fs", exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


async def _fetch_stream_token(config: dict[str, Any]) -> tuple[str, float]:
    import httpx
    url = f"{config['backendUrl']}/functions/v1/agent-stream-token"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {config['agentKey']}"},
        )
    body_preview = r.text[:200] if r.text else ""
    if r.status_code == 401 and "revoked" in body_preview.lower():
        raise _TerminalAuthError(f"key revoked: {body_preview}")
    if 400 <= r.status_code < 500 and r.status_code != 429:
        raise _TerminalAuthError(f"auth fetch {r.status_code}: {body_preview}")
    r.raise_for_status()
    j = r.json()
    if not j.get("token") or not j.get("expires_at"):
        raise RuntimeError("stream token response missing token/expires_at")
    return j["token"], float(j["expires_at"]) * 1000.0


async def _connect_and_subscribe(config: dict[str, Any]) -> None:
    import websockets  # type: ignore[import-not-found]

    token, exp_ms = await _fetch_stream_token(config)

    stream_url = config["streamUrl"]
    if stream_url.startswith("https://"):
        ws_base = "wss://" + stream_url[len("https://"):]
    elif stream_url.startswith("http://"):
        ws_base = "ws://" + stream_url[len("http://"):]
    else:
        ws_base = stream_url

    schema = "public"
    table = "messages"
    user_id_lc = str(config["userId"]).lower()
    agent_id_lc = str(config["agentProfileId"]).lower()
    row_filter = f"user_id=eq.{user_id_lc}"
    socket_url = (
        f"{ws_base}/realtime/v1/websocket"
        f"?apikey={config['publishableKey']}&vsn=1.0.0"
    )

    ref_counter = [1]

    def next_ref() -> str:
        v = str(ref_counter[0])
        ref_counter[0] += 1
        return v

    async with websockets.connect(socket_url) as ws:
        topic = f"{_WIRE_TOPIC_PREFIX}{schema}:{table}"
        join_ref = next_ref()
        join_payload = {
            "config": {
                "broadcast": {"self": False},
                "presence": {"key": ""},
                _WIRE_EVENT_CHANGES: [
                    {
                        "event": "INSERT",
                        "schema": schema,
                        "table": table,
                        "filter": row_filter,
                    }
                ],
            },
            "access_token": token,
        }
        await ws.send(json.dumps({
            "topic": topic,
            "event": "phx_join",
            "payload": join_payload,
            "ref": join_ref,
            "join_ref": join_ref,
        }))

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send(json.dumps({
                        "topic": "phoenix",
                        "event": "heartbeat",
                        "payload": {},
                        "ref": next_ref(),
                    }))
                except Exception:
                    return

        async def renew_token() -> None:
            nonlocal exp_ms
            while True:
                wait_s = max(30.0, (exp_ms - time.time() * 1000) / 1000.0 - 60.0)
                await asyncio.sleep(wait_s)
                try:
                    new_token, new_exp = await _fetch_stream_token(config)
                except _TerminalAuthError:
                    raise
                except Exception as exc:
                    logger.warning("[onepilot] token renew failed: %s — closing socket", exc)
                    await ws.close()
                    return
                exp_ms = new_exp
                try:
                    await ws.send(json.dumps({
                        "topic": topic,
                        "event": "access_token",
                        "payload": {"access_token": new_token},
                        "ref": next_ref(),
                    }))
                except Exception:
                    return

        hb_task = asyncio.create_task(heartbeat())
        renew_task = asyncio.create_task(renew_token())
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                event = frame.get("event")
                if event == _WIRE_EVENT_CHANGES:
                    payload = (frame.get("payload") or {}).get("data") or {}
                    if payload.get("type") != "INSERT":
                        continue
                    record = payload.get("record") or {}
                    if str(record.get("agent_profile_id", "")).lower() != agent_id_lc:
                        continue
                    if record.get("role") != "user":
                        continue
                    if str(record.get("source") or "").lower() == "webhook":
                        continue
                    asyncio.create_task(_handle_user_message(config, record))
                elif event == "phx_reply":
                    pl = frame.get("payload") or {}
                    if pl.get("status") == "error":
                        logger.warning("[onepilot] phx_reply error: %s", str(pl)[:200])
                elif event == "system":
                    pl = frame.get("payload") or {}
                    if pl.get("status") == "error":
                        msg = str(pl.get("message") or "").lower()
                        logger.warning("[onepilot] system error: %s", str(pl)[:200])
                        if "token" in msg and "expir" in msg:
                            try:
                                new_token, new_exp = await _fetch_stream_token(config)
                                exp_ms = new_exp
                                await ws.send(json.dumps({
                                    "topic": topic,
                                    "event": "access_token",
                                    "payload": {"access_token": new_token},
                                    "ref": next_ref(),
                                }))
                            except _TerminalAuthError:
                                raise
                            except Exception:
                                await ws.close()
        finally:
            hb_task.cancel()
            renew_task.cancel()


def _channel_for_session(session_id: str) -> str:
    """Realtime channel name iOS subscribes to. Mirrors RealtimeMessageListener
    in the iOS client: `messages_<first 8 chars of UUID, UPPERCASE>`. Swift's
    `UUID.uuidString` is uppercase and Supabase Realtime topic matching is
    case-sensitive, so lowercasing here would route every broadcast to a
    topic nobody is listening on (postgres INSERTs still arrive via a
    different code path, which is why the final reply lands but live
    progress events disappeared)."""
    s = str(session_id).replace("-", "").upper()
    return f"messages_{s[:8]}"


async def _broadcast(config: dict[str, Any], session_id: str, event: str, payload: dict[str, Any]) -> None:
    """Best-effort fire-and-forget broadcast of a live progress event to the
    iOS client via Supabase Realtime's HTTP broadcast API. Failures are
    non-fatal: the canonical chat reply is still delivered through the
    existing ingest path. Short timeout so a broken realtime endpoint
    cannot stall the agent loop."""
    import httpx
    url = f"{config['backendUrl']}/realtime/v1/api/broadcast"
    body = {
        "messages": [
            {
                "topic": _channel_for_session(session_id),
                "event": event,
                "payload": payload,
                "private": False,
            }
        ]
    }
    headers = {
        "Content-Type": "application/json",
        "apikey": config["publishableKey"],
        "Authorization": f"Bearer {config['publishableKey']}",
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(url, headers=headers, json=body)
    except Exception as exc:
        logger.debug("[onepilot] broadcast %s failed: %s", event, exc)


async def _progress_upsert(
    config: dict[str, Any],
    session_id: str,
    user_id: str,
    agent_profile_id: str,
    *,
    reasoning_text: Optional[str] = None,
    partial_response: Optional[str] = None,
    status_label: Optional[str] = None,
    is_active: bool = True,
    set_started_at: bool = False,
) -> None:
    """Persist live agent state to `agent_session_progress` so iOS can rebuild
    the full in-flight UI (Lottie spinner, status pill, partial response,
    reasoning trail) on bootstrap / app-foreground / conversation-reopen.
    Realtime broadcasts are fire-and-forget; this single row is the durable
    catch-up snapshot. Best-effort — a 404 (migration not applied yet) or
    transient network error is logged at debug and ignored. Framework-
    agnostic: any agent plugin writes the same shape."""
    import httpx
    from datetime import datetime, timezone
    url = f"{config['backendUrl']}/rest/v1/agent_session_progress"
    now_iso = datetime.now(timezone.utc).isoformat()
    body: dict[str, Any] = {
        "session_id": str(session_id).lower(),
        "user_id": str(user_id).lower(),
        "agent_profile_id": str(agent_profile_id).lower(),
        "is_active": is_active,
        "last_activity_at": now_iso,
        "updated_at": now_iso,
    }
    if reasoning_text is not None:
        body["reasoning_text"] = reasoning_text
    if partial_response is not None:
        body["partial_response"] = partial_response
    if status_label is not None:
        body["status_label"] = status_label
    if set_started_at:
        body["started_at"] = now_iso
    headers = {
        "Content-Type": "application/json",
        "apikey": config["publishableKey"],
        "Authorization": f"Bearer {config['publishableKey']}",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                logger.debug("[onepilot] progress upsert %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        logger.debug("[onepilot] progress upsert failed: %s", exc)


async def _progress_finalize(
    config: dict[str, Any],
    session_id: str,
    user_id: str,
    agent_profile_id: str,
) -> None:
    """Mark the live progress row finished (`is_active=false`) and clear
    transient fields. The row stays around for a short grace window so a
    client returning right after `done` can still see the final partial
    state; the pg_cron sweep deletes finalised rows after 5 min. Best-
    effort — a missing row is fine."""
    await _progress_upsert(
        config,
        session_id,
        user_id,
        agent_profile_id,
        partial_response="",
        status_label="",
        is_active=False,
    )


async def _post_assistant_row(config: dict[str, Any], row: dict[str, Any], text: str, kind: str = "text") -> None:
    """Post an assistant row via the canonical ingest path. Used both for
    real replies and for surfacing plugin-level errors as visible chat
    messages so the iOS client never sits on a silent timeout."""
    import httpx
    ingest_body = {
        "userId": str(config["userId"]).lower(),
        "agentProfileId": str(config["agentProfileId"]).lower(),
        "sessionKey": row.get("session_key") or config["sessionKey"],
        "role": "assistant",
        "content": [{"type": kind, "text": text}],
        "timestamp": int(time.time() * 1000),
    }
    ingest_url = f"{config['backendUrl']}/functions/v1/agent-message-ingest"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config['agentKey']}",
    }
    body_str = json.dumps(ingest_body)
    delays = [0.25, 0.75]
    for attempt in range(len(delays) + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(ingest_url, headers=headers, content=body_str)
            if r.status_code < 500 or attempt == len(delays):
                if r.status_code != 200:
                    logger.warning("[onepilot] ingest %d: %s", r.status_code, r.text[:200])
                else:
                    logger.info("[onepilot] %s delivered (%d chars)", kind, len(text))
                return
            logger.warning("[onepilot] ingest %d (attempt %d) — retrying", r.status_code, attempt + 1)
        except Exception as exc:
            if attempt == len(delays):
                logger.warning("[onepilot] ingest network error: %s", exc)
                return
            logger.warning("[onepilot] ingest network error (attempt %d): %s — retrying", attempt + 1, exc)
        await asyncio.sleep(delays[attempt])


async def _stream_completion(
    client: "httpx.AsyncClient",
    completion_url: str,
    headers: dict[str, str],
    messages: list[dict[str, str]],
    on_progress,
) -> tuple[Optional[str], Optional[str]]:
    """Stream /v1/chat/completions, fire `on_progress(event, payload)` for
    each tool-progress event, accumulate text deltas, return
    (reply_text, error). On HTTP/SSE error, returns (None, error_string).
    The api_server emits OpenAI-shape SSE plus a custom
    `event: hermes.tool.progress` frame whenever the agent calls a tool —
    that's the live "is the agent thinking" signal we forward to iOS."""
    accumulated: list[str] = []
    pending_event: Optional[str] = None
    pending_data_lines: list[str] = []
    # <think>…</think> blocks streamed across multiple SSE chunks need
    # boundary-aware extraction. We track whether we're inside a think block
    # so a tag that splits across two deltas still gets captured as reasoning
    # and stripped from the visible reply.
    in_think_block = False
    # Log every distinct delta-key shape we see in the stream so we can tell
    # which field the model is actually using (reasoning_content vs reasoning
    # vs reasoning_details vs <think> in content). One-shot-per-shape keeps
    # the log volume bounded — the first chunk is usually just `['role']` and
    # is uninformative; the interesting one is whichever shape carries the
    # actual reasoning text in subsequent chunks.
    seen_delta_shapes: set[tuple[str, ...]] = set()

    async def _flush() -> None:
        nonlocal pending_event, pending_data_lines
        if not pending_data_lines:
            pending_event = None
            return
        raw = "\n".join(pending_data_lines).strip()
        pending_data_lines = []
        evt = pending_event
        pending_event = None
        if not raw or raw == "[DONE]":
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        if evt == "hermes.tool.progress":
            try:
                await on_progress("tool_progress", obj if isinstance(obj, dict) else {"raw": raw})
            except Exception as exc:
                logger.debug("[onepilot] on_progress raised: %s", exc)
            return
        # Standard chat-completion chunk. We accept reasoning under any of:
        #   delta.reasoning_content   (DeepSeek-R1, GLM-thinking, Z.AI)
        #   delta.reasoning           (some OpenRouter providers, MiniMax M2)
        #   delta.reasoning_details[] (OpenRouter unified shape, items have .text / .content / .summary)
        #   <think>…</think> inside delta.content (Anthropic-style, MiniMax)
        # The think-tag path also strips the tags + their contents from the
        # visible reply so the user only sees the final answer.
        nonlocal in_think_block, seen_delta_shapes
        try:
            choices = obj.get("choices") or []
            if not choices:
                return
            delta = (choices[0] or {}).get("delta") or {}
            try:
                shape = tuple(sorted(delta.keys()))
                if shape and shape not in seen_delta_shapes:
                    seen_delta_shapes.add(shape)
                    sample = {k: type(delta[k]).__name__ for k in shape}
                    logger.info("[onepilot] new delta shape keys=%s types=%s", list(shape), sample)
            except Exception:
                pass

            async def _emit_reasoning(piece: str) -> None:
                if not piece:
                    return
                try:
                    await on_progress("reasoning_delta", {"text": piece})
                except Exception:
                    pass

            # 1. Explicit reasoning fields (provider-emitted, never in content).
            for field in ("reasoning_content", "reasoning"):
                val = delta.get(field)
                if isinstance(val, str) and val:
                    await _emit_reasoning(val)

            # 2. OpenRouter-style reasoning_details: list of dicts with text/content/summary.
            details = delta.get("reasoning_details")
            if isinstance(details, list):
                for item in details:
                    if not isinstance(item, dict):
                        continue
                    for key in ("text", "content", "thinking"):
                        v = item.get(key)
                        if isinstance(v, str) and v:
                            await _emit_reasoning(v)
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        for s in summary:
                            if isinstance(s, dict):
                                t = s.get("text")
                                if isinstance(t, str) and t:
                                    await _emit_reasoning(t)
                            elif isinstance(s, str):
                                await _emit_reasoning(s)

            # 3. Inline <think>…</think> blocks inside delta.content. Anthropic-shape
            # and MiniMax both do this. We fire reasoning_delta for the inside of
            # the tag and strip the entire block (including tags) from the visible
            # reply. State carries across chunks because a tag may straddle deltas.
            content = delta.get("content")
            if isinstance(content, str) and content:
                visible_parts: list[str] = []
                buf = content
                while buf:
                    if in_think_block:
                        end = buf.find("</think>")
                        if end == -1:
                            await _emit_reasoning(buf)
                            buf = ""
                        else:
                            await _emit_reasoning(buf[:end])
                            buf = buf[end + len("</think>"):]
                            in_think_block = False
                    else:
                        start = buf.find("<think>")
                        if start == -1:
                            visible_parts.append(buf)
                            buf = ""
                        else:
                            if start > 0:
                                visible_parts.append(buf[:start])
                            buf = buf[start + len("<think>"):]
                            in_think_block = True
                visible = "".join(visible_parts)
                if visible:
                    accumulated.append(visible)
                    # Forward visible content as it streams so the plugin
                    # can persist partial assistant text into
                    # `agent_session_progress`. iOS uses this to rebuild
                    # the partial-response bubble after a disconnect — the
                    # final assistant INSERT still lands via the canonical
                    # `done` path; this is purely the in-flight snapshot.
                    try:
                        await on_progress("assistant_delta", {"text": visible})
                    except Exception:
                        pass
        except Exception:
            return

    try:
        async with client.stream(
            "POST",
            completion_url,
            headers={**headers, "Accept": "text/event-stream"},
            json={"model": "hermes-agent", "messages": messages, "stream": True},
        ) as response:
            if response.status_code != 200:
                err_body = (await response.aread()).decode("utf-8", errors="replace")[:300]
                return None, f"api_server returned {response.status_code}: {err_body}"
            async for line in response.aiter_lines():
                if line == "":
                    await _flush()
                    continue
                if line.startswith(":"):
                    continue  # SSE comment / keepalive
                if line.startswith("event:"):
                    pending_event = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    pending_data_lines.append(line[len("data:"):].lstrip())
                    continue
            await _flush()
    except Exception as exc:
        return None, f"api_server stream failed: {exc}"

    text = "".join(accumulated).strip()
    if not text:
        return None, "api_server returned no assistant text"
    return text, None


async def _handle_user_message(config: dict[str, Any], row: dict[str, Any]) -> None:
    import httpx

    session_id = row.get("session_id")
    if not session_id:
        # No session id = nothing to broadcast back to. The dispatch was
        # malformed at the source (iOS always inserts with session_id), so
        # the iOS-side stalled-state fallback handles this.
        return

    # Terminal-event contract: every dispatch that gets past the no-session
    # gate guarantees exactly one of `done` / `error` / `cancelled` reaches
    # iOS. The Lottie spinner clears on whichever lands first; the 2-min
    # iOS-side hard timeout exists only as a last-resort backstop. Without
    # this, an early return below would leave the spinner stuck for the
    # full 2 min — the originating bug this contract closes.
    _terminal_emitted = {"v": False}

    async def _emit_terminal(event_kind: str, payload: dict[str, Any]) -> None:
        if _terminal_emitted["v"]:
            return
        _terminal_emitted["v"] = True
        await _broadcast(config, session_id, event_kind, payload)

    try:
        try:
            history = await _load_history(config, session_id)
        except Exception as exc:
            logger.warning("[onepilot] history fetch failed: %s", exc)
            await _emit_terminal("error", {"message": f"history fetch failed: {exc}", "reason": "history_fetch_failed"})
            return

        # Skip if a foreground client (the open Onepilot app) already answered.
        user_created = row.get("created_at") or ""
        for h in history:
            if (
                h.get("role") == "assistant"
                and isinstance(h.get("created_at"), str)
                and isinstance(user_created, str)
                and h["created_at"] > user_created
            ):
                # Legitimate skip — not an error. iOS treats `cancelled` as
                # terminal and clears the spinner without surfacing a banner.
                await _emit_terminal("cancelled", {"reason": "foreground_completed"})
                return

        # Skip if the user sent a NEWER message after the one we're about to
        # process. The common path: user retried (or rapid-typed) while this
        # dispatch was still in the Realtime queue. Without this guard the
        # plugin would produce two assistant replies (the slow original + the
        # fresh retry) and iOS would render both. Symmetric to the assistant
        # check above.
        for h in history:
            if (
                h.get("role") == "user"
                and isinstance(h.get("created_at"), str)
                and isinstance(user_created, str)
                and h["created_at"] > user_created
            ):
                await _emit_terminal("cancelled", {"reason": "superseded_by_retry"})
                return

        messages = _normalize_history(history)
        if not messages:
            text = _extract_text(row.get("content"))
            if text:
                messages = [{"role": "user", "content": text}]
            else:
                await _emit_terminal("error", {"message": "user message had no extractable text", "reason": "text_missing"})
                return

        api_port = int(os.environ.get("API_SERVER_PORT", "8642"))
        completion_url = f"http://127.0.0.1:{api_port}/v1/chat/completions"
        # Defense-in-depth: when the gateway is configured with API_SERVER_KEY
        # the api_server platform rejects unauthenticated /v1/* calls. The
        # plugin runs in-process so it sees the same env. Without this header
        # the plugin's loopback POST would 401 after Onepilot deploy generates
        # a key.
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("API_SERVER_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Tell the iOS client we accepted the message and dispatched the agent.
        # This is the first heartbeat — it lets the UI reset its no-reply timer
        # and surface a "thinking" state with concrete provenance ("the plugin
        # is alive and calling the model"), separate from optimistic local UI.
        await _broadcast(config, session_id, "started", {})

        # Live state accumulators. The reasoning trail, partial assistant
        # response, and current status label are upserted into
        # `agent_session_progress` so iOS can rebuild the full in-flight UI
        # (Lottie, status pill, partial response, trail) when the user
        # reopens the chat / app mid-thought. Realtime broadcasts remain the
        # fast path; this row is the durable catch-up snapshot.
        _trail = {"text": ""}
        _partial = {"text": ""}
        _status = {"text": "Thinking…"}
        user_id_str = str(config.get("userId") or row.get("user_id") or "")
        agent_id_str = str(config.get("agentProfileId") or row.get("agent_profile_id") or "")
        # Initial row: is_active=true, started_at=now. Subsequent upserts
        # bump last_activity_at and (selectively) the field that changed.
        if user_id_str and agent_id_str:
            await _progress_upsert(
                config, session_id, user_id_str, agent_id_str,
                reasoning_text="",
                partial_response="",
                status_label=_status["text"],
                is_active=True,
                set_started_at=True,
            )

        # Wall-clock of the most recent progress event. Drives the thinking
        # heartbeat below: when the model is in pure chain-of-thought (no
        # exposed `reasoning_*` deltas, no tool calls — common for
        # reasoning models like o1 / DeepSeek-R1 / GLM-Z routed through
        # the api_server), nothing reaches iOS for tens of seconds and the
        # spinner falsely flips to stalled. The heartbeat keeps
        # `lastActivityAt` rolling on the iOS side so the UI reflects real
        # liveness.
        import time as _time
        last_progress_at = {"v": _time.monotonic()}

        async def _on_progress(event_kind: str, payload: dict[str, Any]) -> None:
            last_progress_at["v"] = _time.monotonic()
            # `assistant_delta` is an internal-to-plugin event that carries
            # mid-stream visible text. We don't broadcast it (the canonical
            # assistant INSERT after `done` is the wire-level shape iOS
            # expects); we just persist it into `partial_response`.
            if event_kind != "assistant_delta":
                await _broadcast(config, session_id, event_kind, payload)

            line = ""
            new_status: Optional[str] = None
            new_partial: Optional[str] = None
            if event_kind == "reasoning_delta":
                t = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(t, str):
                    line = t
            elif event_kind == "tool_progress" and isinstance(payload, dict):
                emoji = payload.get("emoji") or "→"
                label = payload.get("label") or payload.get("tool") or "tool"
                line = f"{emoji} {label}\n"
                tool = payload.get("tool")
                if isinstance(tool, str) and tool:
                    new_status = f"Running {tool}…"
                elif isinstance(label, str) and label:
                    new_status = label
                else:
                    new_status = "Working…"
            elif event_kind == "assistant_delta":
                t = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(t, str) and t:
                    _partial["text"] += t
                    new_partial = _partial["text"]

            trail_changed = bool(line) and not _trail["text"].endswith(line)
            if trail_changed:
                _trail["text"] += line
            status_changed = new_status is not None and new_status != _status["text"]
            if status_changed:
                _status["text"] = new_status  # type: ignore[assignment]

            if not (trail_changed or status_changed or new_partial is not None):
                return
            if user_id_str and agent_id_str:
                await _progress_upsert(
                    config, session_id, user_id_str, agent_id_str,
                    reasoning_text=_trail["text"] if trail_changed else None,
                    partial_response=new_partial,
                    status_label=_status["text"] if status_changed else None,
                    is_active=True,
                )

        # Side-task: emit a "thinking" heartbeat every ~12s when no progress
        # has flowed through `_on_progress`. Reasoning models can spend a
        # full minute in pure CoT before producing the first visible token;
        # without this, iOS sees only the initial `started` and flips to
        # stalled-state copy at 25s. The heartbeat is shaped as a regular
        # `tool_progress` so the existing iOS handler renders it as a
        # "thinking" trail line instead of needing a new event type.
        import asyncio as _asyncio

        async def _heartbeat_loop() -> None:
            while True:
                await _asyncio.sleep(4)
                # Only emit if it's actually been quiet for >12s. Real
                # progress events bump `last_progress_at` so a chatty model
                # never sees a heartbeat.
                if _time.monotonic() - last_progress_at["v"] >= 12.0:
                    try:
                        await _on_progress("tool_progress", {
                            "tool": "thinking",
                            "label": "Thinking…",
                            "emoji": "🧠",
                            "kind": "heartbeat",
                        })
                    except Exception:
                        # Heartbeat failures are non-fatal — the canonical
                        # 2-min iOS backstop still catches truly dead runs.
                        pass

        reply: Optional[str] = None
        error: Optional[str] = None
        heartbeat_task = _asyncio.create_task(_heartbeat_loop())
        try:
            # No read timeout — agent runs can take many minutes. Streaming keeps
            # the loopback connection live so any idle timeout in the gateway
            # never trips, and we forward progress to iOS as it arrives.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
            ) as client:
                reply, error = await _stream_completion(
                    client, completion_url, headers, messages, _on_progress
                )
        except Exception as exc:
            error = f"api_server call failed: {exc}"
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (BaseException,):
                # Includes asyncio.CancelledError (which is a BaseException
                # subclass on 3.8+); swallow so the outer try/except/finally
                # contract still runs.
                pass

        if error:
            logger.warning("[onepilot] %s", error)
            # Surface the failure as a visible assistant row so the user sees
            # WHY their request didn't produce a reply, instead of staring at a
            # silent spinner until the iOS reply-timeout fires.
            await _emit_terminal("error", {"message": error, "reason": "completion_failed"})
            try:
                await _post_assistant_row(config, row, f"⚠️ {error}")
            except Exception as post_exc:
                logger.warning("[onepilot] error-row post failed: %s", post_exc)
            if user_id_str and agent_id_str:
                await _progress_finalize(config, session_id, user_id_str, agent_id_str)
            return
        assert reply is not None  # narrowed by error == None

        # Success path. The `done` broadcast is the last heartbeat before the
        # canonical assistant INSERT lands. If `_post_assistant_row` raises
        # AFTER `done`, iOS would extend its deadline on `done` and then wait
        # for a row that never arrives — back to the stuck spinner. Wrap the
        # row post so we can downgrade to a follow-up `error` instead.
        await _emit_terminal("done", {})
        try:
            await _post_assistant_row(config, row, reply)
        except Exception as post_exc:
            logger.warning("[onepilot] assistant row post failed after done: %s", post_exc)
            # `_terminal_emitted` is already True; bypass the helper and emit
            # a follow-up error so iOS hears that the row will never land.
            await _broadcast(config, session_id, "error", {
                "message": f"assistant row post failed: {post_exc}",
                "reason": "row_post_failed_post_done",
            })
        if user_id_str and agent_id_str:
            await _progress_finalize(config, session_id, user_id_str, agent_id_str)
    except Exception as exc:
        # Anything we didn't anticipate — make sure iOS hears something.
        logger.exception("[onepilot] handler crashed: %s", exc)
        try:
            await _broadcast(config, session_id, "error", {
                "message": f"plugin crash: {exc}",
                "reason": "plugin_crash",
            })
        except Exception:
            pass
    finally:
        # Defense in depth: if no terminal event was emitted along any code
        # path above (e.g. a bare `return` slipped past the contract in a
        # future edit), still tell iOS the run ended.
        if not _terminal_emitted["v"]:
            try:
                await _broadcast(config, session_id, "error", {
                    "message": "plugin returned without terminal event",
                    "reason": "missing_terminal",
                })
            except Exception:
                pass


async def _load_history(config: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    import httpx

    url = (
        f"{config['backendUrl']}/functions/v1/agent-message-history"
        f"?session_id={session_id}&limit={HISTORY_LIMIT}"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {config['agentKey']}"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"history {r.status_code}: {r.text[:200]}")
    j = r.json()
    msgs = j.get("messages") or []
    return msgs if isinstance(msgs, list) else []


def _normalize_history(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in reversed(rows):
        text = _extract_text(row.get("content"))
        if text:
            out.append({"role": row.get("role", "user"), "content": text})
    return out


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except Exception:
            return content
        return _extract_text(parsed)
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                ptype = p.get("type")
                if (ptype == "text" or ptype is None) and isinstance(p.get("text"), str):
                    return p["text"]
        return ""
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _extract_assistant_text(completion: dict[str, Any]) -> str:
    choices = completion.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict):
                ptype = p.get("type")
                if (ptype == "text" or ptype is None) and isinstance(p.get("text"), str):
                    return p["text"]
    return ""
