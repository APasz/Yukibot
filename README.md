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
Set `BOT_PROFILE` in `.env` before running anything. Startup now fails if it is missing.

### Run

```bash
uv run python main.py
```

### Bot Profiles

`BOT_PROFILE` is required and controls which command groups and services are enabled for a deployment.

- `yuki` is the full bot profile.
- `erin` enables game-control command groups, game chat relay, and `/ops logs`/`/ops restart`.
- `portal` runs the NiceGUI web portal without starting a Discord gateway bot. It is the standalone dashboard deployment and should be pointed at Yuki's authority endpoint.

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
- `NODE_API_TOKEN_SECRET` signs Node API bearer tokens. It must be an independently generated secret and must not match `DATA_AUTHORITY_TOKEN`; requests fail closed when it is absent outside explicitly unauthenticated development mode.
- `NODE_API_UPLOAD_MAX_BYTES` caps each Node API upload (default: 1 GiB). ZIP save uploads additionally enforce file-count, individual-file, extracted-size, and compression-ratio limits.
- Unauthenticated node API access is allowed only in `INDEV` or when `ALLOW_UNAUTH_NODE_API=true` is explicitly set.
- `MOD_WEB_DISCORD_CLIENT_ID` and `MOD_WEB_DISCORD_CLIENT_SECRET` enable Discord login for the mod web UI.
- The Discord OAuth redirect defaults to `{MOD_WEB_PUBLIC_BASE_URL}/auth/discord/callback`; override it with `MOD_WEB_AUTH_REDIRECT_URL` only when Discord is configured with a different public callback.
- `MOD_WEB_SESSION_CACHE_DIR` defaults to `.cache/mod_web_sessions`; Portal persists browser sessions and pending OAuth state there so Portal restarts do not force users to sign in again. The cache directory must be private to the service account; group- or world-writable directories are rejected.
- `/mod-web/mirrors` is available only on the standalone Portal profile to `user` access and above. It can publish validated snapshots from public GitHub/GitLab repositories or uploaded ZIP archives; owners manage their own mirrors and `admin` access can manage all of them.
- `MIRROR_STORAGE_DIR` defaults to `mirror_data` on Portal only. It holds the mirror catalogue, immutable upload sources, and the currently published snapshots, so it must be persistent storage on the Portal host. Yuki and Erin neither create this storage nor expose mirror routes.
- ComputerCraft clients fetch a public Portal mirror at `/mirror/v1/projects/<mirror-id>/manifest.json` and `/mirror/v1/projects/<mirror-id>/files/<path>?revision=<manifest-revision>`. Manifest responses revalidate on every check; revision-qualified files are immutable and Portal retains the latest four snapshots for a safe update retry.
- Each published mirror has a **ComputerCraft setup** action with a recommended `wget run` bootstrap command. When its snapshot has a top-level `startup.lua`, it creates a Yukibot-managed root `startup.lua` only when the computer has no existing startup file; on every boot it attempts to update, then starts the mirrored app even if Yukibot is unavailable. The dialog provides a shared dispatcher snippet for integrating with an existing startup.lua instead. The bootstrapper records its managed files under `/.yukibot_mirrors`, only replaces files it installed, and retries once if a snapshot changes during an update.
- Portal checks branch-tracking Git mirrors daily. Initial checks are deterministically staggered across the day, and the Portal scheduler handles one due mirror every 15 seconds. It resolves the branch first and only downloads an archive when the published revision has changed.
- `rupdater.py --release --restart` commits non-ignored local changes after prompting for a required release message, deploys the resulting local commit, and writes its SHA, optional local Git tag, target, and deployment time to `.yukibot/deployment.json` on each remote. After smoke-testing, push that exact commit and any tag to GitHub.
- `rupdater.py` reads remote targets from ignored `rupdater.targets.json`; copy `rupdater.targets.example.json` to create it. Use `--targets-file path/to/targets.json` for another profile. Set `password` to `null` to authenticate through your SSH agent or configured SSH key.
- The About page shows deployment metadata written by release deployments. `MOD_WEB_BUILD_SHA` remains an optional fallback when that metadata has not yet been created.
- Normal sign-ins use a browser-session cookie with a 16-hour server expiry. “Remember me” uses a persistent cookie with a 30-day absolute expiry.
- Mod web browser sessions authenticate as Discord user IDs and authorise through `users.json` / `Access_Control`; `visitor` access can use chat-only web relay routes, while `user` and above can use the broader mod web tools.
- `BYPASS_WEB_AUTH=true` skips Discord web auth only when `INDEV` is also set. It is intended for local development and is ignored outside `INDEV`.
- When the standalone portal hosts the web UI, sibling nodes do not need Discord OAuth credentials; the portal reads their Node API with short-lived `NODE_API_TOKEN_SECRET` bearer tokens. Remote downloads are streamed through the portal, so those bearer tokens never enter browser URLs.
- Mods with a `download_block_reason` in their mod DB entry are listed in the web UI but excluded from downloads; 7D2D built-in mods are marked this way automatically.
- Downloadable mods may define `client_pack.policy` as `required` (the default), `optional`, or `alternative`. Optional mods start selected in the client-pack dialog.
- Alternative mods must share a non-empty `client_pack.choice_group`; every group requires at least two mods and exactly one entry with `client_pack.default_choice` set to `true`.
- For local multi-node testing, use `uv run python dev_cluster.py`. It launches Yuki, Erin, and Portal as separate processes with local loopback ports and gives you `start` / `stop` / `restart` controls in one terminal. Add `--debug` to enable project debug logging for every member.

All other bot profiles act as clients.

The `portal` profile also acts as a remote client.

- `BOT_TOKEN` is not required for `portal`.
- `ERIN_BOT_TOKEN` is required by `dev_cluster.py`; `YUKI_BOT_TOKEN` is optional and otherwise falls back to `BOT_TOKEN`.
- Set `DATA_AUTHORITY_HOST` to Yuki's public authority host.
- `PUBLIC_BASE_URL` / `MOD_WEB_PUBLIC_BASE_URL` should point at the portal host, not Yuki.
- The portal home page lists remote nodes from the shared bot registry; app and chat pages should be reached through `/mod-web/nodes/{node_name}/...`.
- `NODE_API_PORT` is usually not needed for `portal`.
- Portal accesses its own node API through loopback automatically. This works when Portal and Yuki share a public URL or host: no DNS or reverse-proxy change is needed for Portal's own maintenance page.

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
- `app_installer.allowed_scopes` limits SteamCMD app installs on that node.
  Omit it or use `null` to allow every supported recipe; use `[]` to disable installs; otherwise list allowed scopes, such as `["satisfactory"]`. The node API enforces this policy.

### SteamCMD update targets

SteamCMD-backed apps can update to their default release or a listed publisher-provided version or beta branch. SteamCMD installs the current build on that branch—it cannot pin an arbitrary historical build ID unless the publisher exposes that build as a beta branch. Password-protected or custom branches can be declared in the instance's `steam_update.branches` configuration with `beta_password`.

### Satisfactory first claim

New Satisfactory servers are claimed from the game client. Use the admin password entered during installation when claiming the server; Yukibot waits for that claim and enables API management automatically afterwards.

### Type Checking

```bash
uv run basedpyright
```
