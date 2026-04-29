# hermes-onepilot-platform

A plugin for the [Hermes](https://github.com/NousResearch/hermes-agent) agent framework that bridges Onepilot app messages to a Hermes gateway and posts assistant replies back so the Onepilot app can deliver them as push notifications, even when the app is closed.

## What it does

When a Hermes profile has the `onepilot` plugin enabled:

- Subscribes to the inbound channel for this user's messages.
- On a user message, calls Hermes' OpenAI-compatible API server (running in the same gateway process at `http://127.0.0.1:<API_SERVER_PORT>/v1/chat/completions`) to produce a reply.
- Posts the reply to the backend message endpoint, which inserts the row and delivers a push notification to the user's devices.

The plugin runs inside the Hermes gateway's asyncio event loop. There is no separate process: when the gateway is up, the plugin is up; when the gateway crashes and restarts, so does the plugin.

## Design principles

These rules govern every change to this plugin. The app ships through App Store review (1–2 week turnaround) but the plugin ships through `plugin_manifest` (instant). When the boundary between them blurs, every framework rename or schema tweak becomes a stuck App Store release. Don't blur the boundary.

**1. Plugins are the heavy lifters.** Anything that touches Hermes — `hermes` CLI flags, profile/config paths, plugin install, account write-back, cron-channel registration — lives **here**, not in iOS. If Hermes renames a flag or moves a private symbol (see `UPSTREAM.md`), this plugin absorbs it and ships v+1 via `plugin_manifest`. New iOS adapter code calling `hermes <verb>` directly is a regression — route it through the wrapper API in `wrapper_api.py` and let the plugin shell out internally.

**2. The app renders what we control.** iOS speaks to two surfaces only: this plugin's `/onepilot/v1/*` HTTP wrapper and the chat WebSocket. Both shapes are owned by us, both are versioned, both have contract tests. UI gates on `AgentFrameworkCapability`, never on `frameworkType == .hermes`. New features land as new wrapper endpoints — not as new `hermes` invocations from Swift. If the app needs to render something the plugin doesn't expose yet, add the endpoint here first.

**3. Security first.** A leaked agent key (`oak_*`) must read zero rows outside its bound `(user_id, agent_profile_id)`, and writes must be attributed only to that pair — server-side, not from request body. Every wrapper endpoint takes `Authorization: Bearer <agentKey>`, binds on `127.0.0.1` only, and never accepts user-supplied `userId`/`agentProfileId` overrides. New edge-function calls require a `SCOPE.md` documenting the authz scope. New imports (`subprocess`, `eval`, `exec`, dynamic `__import__`, write-mode `open` outside the plugin dir) need explicit justification — the in-process plugin model means anything we import has full gateway access. Monkey-patching framework internals (e.g. `cron.scheduler._deliver_result`) is a last resort; prefer a public API and file the upstream request in `UPSTREAM.md`. Residual risks are tracked in the parent repo's `SECURITY_AUDIT.md`.

**4. This repo is public — write for strangers, not insiders.** The plugin source ships to GitHub and runs on every user's host. Treat every comment, log line, error string, and identifier as user-readable.

- No backend architecture leaks: don't reference internal vendor names, project IDs, internal table names beyond what an endpoint already exposes, dashboard URLs, deploy hostnames, or service-internal tooling. Generic terms (`backend`, `auth provider`, `realtime channel`) over branded ones. (Vendor-name leaks are CI-gated — see `ci/plugin/hermes-platform/snapshot-diff.sh`.)
- No verbose internal commentary. Comments explain **why** a non-obvious thing is the way it is, not what the code does. No multi-paragraph docstrings, no walls of context that only make sense to someone on the team. If a reader needs three paragraphs to understand a function, the code is wrong, not the comments.
- No JIRA / Linear / PR / incident references in code. They rot, and they leak our process. Put that context in the commit message, where it belongs.
- No hardcoded internal URLs or staging hostnames. All endpoints come from `config["backendUrl"]` / `config["streamUrl"]` at runtime.
- Log lines are user-facing too — they end up in the gateway log and the user's terminal. No stack-trace dumps with internal paths, no PII, no full bearer tokens (prefix-only is fine for diagnostics).
- Error messages exposed via the wrapper API are bounded (`[:200]`) for the same reason — bound the leak surface.

**Practical contract:**

- Bump `pyproject.toml` + `plugin.yaml` versions + refresh the bundled Swift snapshot (`HermesOnepilotPlugin.swift`) in the same PR.
- New `/onepilot/v1/*` endpoint → contract test in `test/test_wrapper_api.py` + matching method in `OnepilotPluginClient.swift` in the app repo.
- Bootstrap commands (the very first `hermes profile create`, first plugin write, first `gateway run`) are exempt — by definition the wrapper API doesn't exist yet.

## Layout

```
plugin.yaml      # Hermes plugin manifest
__init__.py      # register(ctx) entry point + asyncio bridge
config.json      # runtime config (written by the deploy step, not committed)
```

## Install

The Onepilot iOS app installs this plugin automatically when a user picks "Onepilot Chat" as the channel for their Hermes agent. The deploy step:

1. Writes `plugin.yaml` and `__init__.py` to `<HERMES_HOME>/plugins/onepilot/`.
2. Mints a per-agent API key and writes `config.json` (mode 600) with the backend URLs and credentials the plugin needs.
3. Adds `onepilot` to `plugins.enabled` in the profile's `config.yaml`.
4. Sets `platforms.api_server.enabled: true` (the plugin self-fetches the local API server).
5. Restarts the gateway so the plugin is discovered.

## Auth model

A durable per-agent API key (server-bound to one `(user_id, agent_profile_id)` pair) lives in `config.json`. The plugin exchanges it for short-lived stream JWTs on demand. A leaked key cannot post into another user's inbox or read another agent's history — server-side checks reject any request whose body claims a different user or agent than the key is bound to.

The Onepilot app can rotate the key at any time (a fresh mint atomically revokes the prior hash) or revoke it outright.

## Reliability

- Plugin lives in-process with the Hermes gateway: no extra PID to supervise.
- Inbound subscription auto-reconnects with exponential backoff.
- Stream JWTs are renewed proactively before expiry.
- Long agent runs do not time out: the plugin's caller and the LLM caller are the same process.
- A foreground app reply (when the user has the Onepilot app open and a server-side path already produced a reply) is detected and de-duplicated so the user never sees two assistant turns for one prompt.

## Config

The deploy step writes `config.json` next to `__init__.py`. Fields:

| Field | Meaning |
|---|---|
| `enabled` | Master switch. Plugin idles cleanly if `false`. |
| `backendUrl` | Base URL for HTTP endpoints (`/functions/v1/...`). |
| `streamUrl` | Base URL for the inbound channel websocket. |
| `publishableKey` | Channel-server `apikey` query parameter. |
| `agentKey` | Durable per-agent credential (`oak_…`). |
| `userId` | Onepilot user this agent serves. |
| `agentProfileId` | Onepilot agent profile this plugin belongs to. |
| `sessionKey` | Default session routing key. |

## Cutting a new version

Six steps. Skipping the sha256 re-fetch (#5) is the known footgun — GitHub re-uploads the asset on publish, so the digest you see while it's a draft does **not** match the published artifact.

1. **Bump** the version in `pyproject.toml` AND `plugin.yaml` (both must agree — Hermes reads `plugin.yaml`, the wrapper-api `/health` endpoint reads `pyproject.toml`).
2. **Commit + tag** in this repo: `git commit -am "Release vX.Y.Z" && git tag vX.Y.Z && git push --follow-tags`. Update the submodule pointer in `onepilotapp/` and push there too — the release CI in `onepilotapp` builds the tarball from that pointer.
3. **Watch the release CI** (`gh run watch -R sofiane8910/onepilotapp`) cut a draft GitHub Release with the tarball attached as an asset.
4. **Publish the draft**: `gh release edit "hermes/onepilot-platform@vX.Y.Z" -R sofiane8910/onepilotapp --draft=false --latest=false`. Until this runs, the asset URL returns HTTP 404 and every iOS install fails fast.
5. **Re-fetch the canonical sha256** from the published asset (GitHub may have repacked):
   ```sh
   curl -sL "https://github.com/sofiane8910/onepilotapp/releases/download/hermes/onepilot-platform%40vX.Y.Z/onepilot-platform-vX.Y.Z.tgz" \
     | shasum -a 256 | awk '{print $1}'
   ```
6. **Bump the manifest** in Supabase (one row, three columns):
   ```sql
   UPDATE public.plugin_manifest
   SET version = 'vX.Y.Z',
       tarball_url = 'https://github.com/sofiane8910/onepilotapp/releases/download/hermes/onepilot-platform%40vX.Y.Z/onepilot-platform-vX.Y.Z.tgz',
       sha256 = '<sha from step 5>'
   WHERE channel = 'hermes-stable';
   ```
   Apply via `mcp__supabase_onepilot__execute_sql` (or the Supabase dashboard) — `service_role` is required because RLS denies DML to `anon`/`authenticated`. Existing agents pick up the new release on their next `ensureSyncSetup`; iOS doesn't need rebuilding.

## Rollback

If a release misbehaves, revert the `plugin_manifest` row to a known-good `version` + `tarball_url` + `sha256`. Existing agents stay on their installed version (the install script is a no-op when `plugin.yaml`'s `version:` already matches), but reinstalls and new deploys pick up the rollback.

## License

MIT
