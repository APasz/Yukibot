This is a complete rewrite started by my lifelong companion NaiTechie a.k.a AiviA, and completed by myself, APasz, upon her passing.
Yukibot started out as a way for her to connect with her most cherished people. It became the catalyst for me teaching her the ways of Python and Discord bots

She posted V1 to Github here, https://github.com/Naitechie/Yukibot

## Development

This project now uses [`uv`](https://docs.astral.sh/uv/) for environment and dependency management.

### Setup

```bash
cp env.example .env
uv sync
```

`uv` will create `.venv` automatically. The project requires Python `3.14+`.

### Run

```bash
uv run python main.py
```

### Bot Profiles

`BOT_PROFILE` controls which command groups and services are enabled for a deployment.

- `yuki` is the default full bot profile.
- `erin` enables game-control command groups, game chat relay, and `/ops logs`/`/ops restart`.
- `portal` runs the NiceGUI web portal without starting a Discord gateway bot. It is intended as groundwork for a future standalone dashboard deployment and should be pointed at Yuki's authority endpoint.

### Data Authority

Yuki is always the data authority. It reads and writes `users.json` and `discord_names.json` locally, and can expose authenticated snapshots for sister bots when `DATA_AUTHORITY_TOKEN` is set.

Sister bots also push a typed bot-metadata snapshot to Yuki through the same authority API. The current snapshot includes per-bot OAuth overrides, and the payload is structured to grow into other sections later such as apps, mods, or feature flags.

### Public Address

`PUBLIC_BASE_URL` is the public base users type into a browser or game client to reach the server.

- Use a bare host like `wakusei.apasz.com`, or a full URL like `https://wakusei.apasz.com`.
- Public URLs should use `https` outside `INDEV`; plain `http` is intended only for local development.
- Do not include `/uploads` or any other path.
- Yukibot derives upload links from this base by appending `/uploads/`.
- `config.PUBLIC_IP` is the raw public IP resolved from `https://api.ipify.org`.
- `config.PUBLIC_ADDR` is the host used for game join addresses and defaults to `config.PUBLIC_IP` when `PUBLIC_BASE_URL` is unset.
- The mod web UI uses `MOD_WEB_PUBLIC_BASE_URL` when set, otherwise it reuses the public host from `PUBLIC_BASE_URL` and serves on `MOD_WEB_PORT` (default `3180`).
- `NODE_API_PORT` optionally starts a dedicated node API server for the current bot. When unset, `/api/node` is only served by the mod web process.
- `NODE_API_BIND_HOST` overrides the dedicated node API bind host and defaults to the mod web bind host.
- `NODE_API_PUBLIC_BASE_URL` controls the published public base used for node API links and registry metadata when a dedicated node API server is enabled. If unset, it defaults to the mod web public base.
- `MOD_WEB_BIND_HOST` controls the interface NiceGUI binds to and defaults to `0.0.0.0`.
- `NODE_NAME` identifies the local host in node API tokens and defaults to the active bot profile name.
- `NODE_API_TOKEN_SECRET` signs direct node API links. If unset, Yukibot falls back to `DATA_AUTHORITY_TOKEN`; if both are unset, node API auth is disabled.
- `MOD_WEB_DISCORD_CLIENT_ID` and `MOD_WEB_DISCORD_CLIENT_SECRET` enable Discord login for the mod web UI.
- The Discord OAuth redirect defaults to `{MOD_WEB_PUBLIC_BASE_URL}/auth/discord/callback`; override it with `MOD_WEB_AUTH_REDIRECT_URL` only when Discord is configured with a different public callback.
- Mod web browser sessions authenticate as Discord user IDs and authorize through `users.json` / `Access_Control`; `visitor` access can use chat-only web relay routes, while `user` and above can use the broader mod web tools.
- `BYPASS_WEB_AUTH=true` skips Discord web auth only when `INDEV` is also set. It is intended for local development and is ignored outside `INDEV`.
- When Yuki is used as the web portal, sibling nodes do not need Discord OAuth credentials; Yuki reads their node API with short-lived tokens signed by the shared `NODE_API_TOKEN_SECRET` / `DATA_AUTHORITY_TOKEN`, and remote downloads redirect to the owning node with a scoped short-lived token.
- Mods with a `download_block_reason` in their mod DB entry are listed in the web UI but excluded from downloads; 7D2D built-in mods are marked this way automatically.
- For local multi-node testing, set `INDEV=true`, `DATA_AUTHORITY_TOKEN`, and `REMOTE_NODES=true`. Yukibot loads child node definitions from `REMOTE_NODES_FILE` (default `remote_nodes.json`), starts each configured node, and stops them during shutdown. Each node may provide either `bot_token` directly or `bot_token_env` to read a token from the environment.

All other bot profiles act as clients.

The `portal` profile also acts as a remote client.

- `BOT_TOKEN` is not required for `portal`.
- Set `DATA_AUTHORITY_HOST` to Yuki's public authority host.
- `PUBLIC_BASE_URL` / `MOD_WEB_PUBLIC_BASE_URL` should point at the portal host, not Yuki.
- The portal home page lists remote nodes from the shared bot registry; app and chat pages should be reached through `/mod-web/nodes/{node_name}/...`.
- `NODE_API_PORT` is usually not needed for `portal`.

- `DATA_AUTHORITY_HOST` is the client-facing base host for the authority API. Prefer a full URL when you know the scheme, for example `https://wakusei.apasz.com`.
- If `DATA_AUTHORITY_HOST` is a bare host and `PUBLIC_BASE_URL` is explicitly set, the authority client inherits that scheme. Otherwise bare hosts default to `https`.
- If `DATA_AUTHORITY_HOST` is omitted in remote mode, an explicit `PUBLIC_BASE_URL` is used instead. The autogenerated fallback `PUBLIC_BASE_URL` is not used for remote authority discovery.
- `DATA_AUTHORITY_PORT` is an optional override for the public authority port.
- `DATA_AUTHORITY_CACHE_DIR` defaults to `.cache/authority`.
- `DATA_AUTHORITY_TIMEOUT_SECONDS` defaults to `2`.
- Remote authority clients require `https` by default. In `INDEV`, Yukibot permits `http` authority endpoints so locally spawned sibling nodes can talk to the loopback authority server.

On Yuki, the public authority endpoint also falls back to `PUBLIC_BASE_URL`, but the server bind address is configured separately.

- `DATA_AUTHORITY_BIND_HOST` and `DATA_AUTHORITY_BIND_PORT` control where the local authority server listens. They default to `127.0.0.1:8081`; the public authority endpoint is separate metadata used by clients.
- If you are fronting Yuki with nginx, point clients at the public URL and proxy `/authority/*` to the local authority server.

### Bot Configuration

- `OAuth` stores the current bot's local guild/user OAuth override URLs.
  Missing keys mean the install type is not supported. Present keys with `null` values mean the URL should be autogenerated.
- `KnownBots` stores structured metadata snapshots reported to Yuki by sister bots.

### Type Checking

```bash
uv run basedpyright
```
