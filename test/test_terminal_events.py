"""Terminal-event contract tests for `_handle_user_message`.

Every dispatch path that gets past the no-session-id gate MUST emit
exactly one terminal broadcast (`done` | `error` | `cancelled`) so the
iOS Lottie spinner clears without falling back to the 2-minute hard
backstop. This file exercises each early-return path plus the
post-`done` row-write failure case.

The plugin's network helpers (`_broadcast`, `_load_history`,
`_post_assistant_row`, `_progress_clear`, `_progress_upsert`,
`_stream_completion`) are monkey-patched to no-op or to record invocations
so the test runs offline.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))


class TerminalEventTests(unittest.TestCase):
    def setUp(self) -> None:
        # Load `__init__.py` directly — the plugin directory uses dashes
        # (`onepilot-hermes-chat`) which Python's normal import machinery
        # rejects, so a spec-based loader bypasses that.
        import importlib.util

        if "onepilot_hermes_chat" in sys.modules:
            del sys.modules["onepilot_hermes_chat"]
        spec = importlib.util.spec_from_file_location(
            "onepilot_hermes_chat", PLUGIN_DIR / "__init__.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.plugin = module

        self.broadcasts: list[tuple[str, dict[str, Any]]] = []

        async def fake_broadcast(config, session_id, event, payload):
            self.broadcasts.append((event, dict(payload)))

        async def fake_progress_clear(config, session_id):
            pass

        async def fake_progress_upsert(*a, **kw):
            pass

        # Override globals on the loaded module.
        self.plugin._broadcast = fake_broadcast
        self.plugin._progress_clear = fake_progress_clear
        self.plugin._progress_upsert = fake_progress_upsert

        self.config = {
            "backendUrl": "http://127.0.0.1:0",
            "publishableKey": "pk_test",
            "userId": "11111111-1111-1111-1111-111111111111",
            "agentProfileId": "22222222-2222-2222-2222-222222222222",
        }
        self.row = {
            "session_id": "33333333-3333-3333-3333-333333333333",
            "user_id": self.config["userId"],
            "agent_profile_id": self.config["agentProfileId"],
            "created_at": "2026-05-01T12:00:00Z",
            "content": [{"type": "text", "text": "hi"}],
        }

    # -- helpers --------------------------------------------------------

    def _terminal_events(self) -> list[str]:
        return [e for (e, _) in self.broadcasts if e in ("done", "error", "cancelled")]

    def _run(self, coro):
        # Each test gets a fresh loop. `asyncio.get_event_loop()` is
        # deprecated in 3.12+ when there's no running loop, so we make one.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # -- tests ----------------------------------------------------------

    def test_history_fetch_failure_emits_error(self):
        async def boom(config, session_id):
            raise RuntimeError("network down")

        self.plugin._load_history = boom

        self._run(self.plugin._handle_user_message(self.config, self.row))
        self.assertEqual(self._terminal_events(), ["error"])
        self.assertEqual(self.broadcasts[0][1].get("reason"), "history_fetch_failed")

    def test_foreground_already_answered_emits_cancelled(self):
        async def history(config, session_id):
            return [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answered already"}],
                    "created_at": "2026-05-01T12:00:01Z",  # > row.created_at
                },
            ]

        self.plugin._load_history = history

        self._run(self.plugin._handle_user_message(self.config, self.row))
        self.assertEqual(self._terminal_events(), ["cancelled"])
        self.assertEqual(self.broadcasts[0][1].get("reason"), "foreground_completed")

    def test_text_missing_emits_error(self):
        async def history(config, session_id):
            return []

        self.plugin._load_history = history

        no_text_row = dict(self.row)
        no_text_row["content"] = []  # nothing extractable

        self._run(self.plugin._handle_user_message(self.config, no_text_row))
        self.assertEqual(self._terminal_events(), ["error"])
        self.assertEqual(self.broadcasts[0][1].get("reason"), "text_missing")

    def test_post_done_row_failure_emits_followup_error(self):
        async def history(config, session_id):
            return []

        async def stream(client, url, headers, messages, on_progress):
            return ("the answer", None)

        async def failing_post(config, row, text, kind="text"):
            raise RuntimeError("supabase 503")

        self.plugin._load_history = history
        self.plugin._stream_completion = stream
        self.plugin._post_assistant_row = failing_post

        self._run(self.plugin._handle_user_message(self.config, self.row))

        # Both `done` and a follow-up `error` must reach iOS so the
        # extended deadline doesn't strand the spinner waiting for an
        # INSERT that will never arrive.
        events = [e for (e, _) in self.broadcasts]
        self.assertIn("done", events)
        self.assertIn("error", events)
        # `done` precedes the follow-up error.
        self.assertLess(events.index("done"), events.index("error"))
        self.assertEqual(self.broadcasts[events.index("error")][1].get("reason"),
                         "row_post_failed_post_done")

    def test_happy_path_emits_done(self):
        async def history(config, session_id):
            return []

        async def stream(client, url, headers, messages, on_progress):
            return ("ok", None)

        async def post(config, row, text, kind="text"):
            pass

        self.plugin._load_history = history
        self.plugin._stream_completion = stream
        self.plugin._post_assistant_row = post

        self._run(self.plugin._handle_user_message(self.config, self.row))
        self.assertEqual(self._terminal_events(), ["done"])

    def test_superseded_by_retry_emits_cancelled(self):
        # Foreground/cancelled-by-newer-user-row guard: if the user retried
        # (or rapid-typed) while this dispatch was queued, bail without
        # producing a duplicate assistant reply.
        async def history(config, session_id):
            return [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                    "created_at": "2026-05-01T12:00:00Z",  # the row we're processing
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "actually, this"}],
                    "created_at": "2026-05-01T12:00:05Z",  # newer retry
                },
            ]

        self.plugin._load_history = history

        self._run(self.plugin._handle_user_message(self.config, self.row))
        self.assertEqual(self._terminal_events(), ["cancelled"])
        self.assertEqual(self.broadcasts[0][1].get("reason"), "superseded_by_retry")

    def test_completion_failure_emits_error(self):
        async def history(config, session_id):
            return []

        async def stream(client, url, headers, messages, on_progress):
            return (None, "model returned 500")

        async def post(config, row, text, kind="text"):
            pass

        self.plugin._load_history = history
        self.plugin._stream_completion = stream
        self.plugin._post_assistant_row = post

        self._run(self.plugin._handle_user_message(self.config, self.row))
        self.assertEqual(self._terminal_events(), ["error"])
        # Locate the terminal error broadcast (the `started` event also
        # fires before the completion call, so index 0 isn't the error).
        err = next(p for (e, p) in self.broadcasts if e == "error")
        self.assertEqual(err.get("reason"), "completion_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
