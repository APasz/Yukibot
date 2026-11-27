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

### Platform compatibility

Native Windows startup remains blocked in `config.py` while compatibility work continues. Bot and Portal support is assessed separately from managed games. Existing adapters default to Linux/x86_64; Factorio also supports Linux/ARM64.

Adapters declare a non-empty `frozenset[AppPlatform]` through `App.supported_platforms`. Each immutable platform pairs an `AppOperatingSystem` (`LINUX`, `WINDOWS`, `MACOS`) with an `AppArchitecture` (`X86_64`, `ARM64`). Explicit pairs prevent support for Linux/ARM64 and Windows/x86_64 from accidentally enabling Windows/ARM64. These are code declarations, not instance configuration overrides. For example, an adapter verified on Linux/x86_64 and Windows/x86_64 would declare:

```python
from apps._platform import AppArchitecture, AppOperatingSystem, AppPlatform, LINUX_X86_64

supported_platforms = frozenset({
    LINUX_X86_64,
    AppPlatform(AppOperatingSystem.WINDOWS, AppArchitecture.X86_64),
})
```

Checks use the game node's operating system and architecture. `AMD64`, `x64`, and `x86_64` normalise to `X86_64`; `aarch64` and `arm64` normalise to `ARM64`. Unknown systems and architectures fail explicitly, and emulation is not inferred. Unsupported scopes and installation recipes are omitted from discovery. Startup skips their existing instances with a notice and preserves their saved configuration, including enabled flags. Direct creation, loading, and launch attempts are also rejected before adapter construction or process launch.

Factorio derives its supported platforms, native download target, and executable path from one build table. Linux/ARM64 uses the official `linux-arm64` headless release and `bin/arm64/factorio`; Linux/x86_64 retains `linux64` and `bin/x64/factorio`. Server startup, fresh-world generation, updates, and verification use that same selection. Updates download the exact version reported by release metadata and check for the expected binary in staging before changing installed files. Native ARM64 execution still needs a host smoke test; the package layout has been checked against the official archive.

Frontend builds use `uv` to select Python, POSIX ownership imports are deferred until a game needs them, and dev-cluster PID checks use `psutil.pid_exists`. Archive imports, exports, and Modrinth download metadata share Windows-safe name validation. Generated frontend contracts use consistent LF line endings.

The locked Python 3.14 dependency plan resolves for Windows x64 (`uv sync --locked --dry-run --python-platform x86_64-pc-windows-msvc --python 3.14 --no-python-downloads`). This checks the installation plan without installing or running Windows packages.

Before removing the startup blocker, the remaining work includes:

- Replacing POSIX FIFO use in `cmd_music.py` for generated audio.
- Handling Windows Portal shutdown signals and dev-cluster process-tree termination.
- Validating private cache access through Windows permissions rather than POSIX mode bits.
- Providing platform-specific host reboot and updater behaviour. The updater currently assumes OpenSSH control sockets, optional `sshpass`, and POSIX remote commands; text sent to remote shells also needs explicit LF handling.
- Running native Windows dependency installation, startup/shutdown, authentication/cache, file-operation, and frontend checks. Linux tests and Windows-targeted type analysis do not establish runtime compatibility.

### Run

```bash
uv run python main.py
```

### Network diagnostics frontend

`/mod-web/network` is the first standalone Svelte + TypeScript page. Open it from the user utilities menu. It uses the existing Discord session and ACL, alongside the NiceGUI pages, and does not create a NiceGUI client or Socket.IO connection. Python still hosts the application through the current server during this first migration step.

Build the browser assets with Node.js 22 and pnpm:

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
```

The build regenerates TypeScript API types and the browser-draft validation schema from `web_api`'s Python contracts. `rupdater.py` installs locked frontend dependencies and builds locally before any deployment. Releases build before committing so their revision includes the generated assets; regular and `--all-tracked` updates also transfer the complete compiled frontend. Build failures stop before committing or contacting a remote. `--check` and `--dry` use existing assets without building. Production serves the static assets and does not need Node.js or pnpm. Missing builds produce a clear 503 page without preventing the rest of the dashboard from starting.

For frontend development, sign in to the local Portal first, then run `pnpm --dir frontend dev` and open `http://127.0.0.1:5173/mod-web/frontend/`. Set `YUKIBOT_BACKEND_URL` when the local Portal is not on `http://127.0.0.1:3180`.

Sessions record direct browser-to-endpoint HTTP probes, event markers, and host CPU/memory snapshots with separate Portal-to-node observations. Browser scheduling delays and failed requests are separate outcomes. Recordings continue locally through temporary outages; the latest draft is saved in IndexedDB under the signed-in user's ID. Background browser throttling can reduce sampling coverage.

The separate speed test runs directly between the browser and the selected Portal/node. Portal is omitted from the speed-test selector when a game node uses the same configured public hostname; latency probes still measure each service. The time selector allows 5 seconds (default), 10 seconds, or 20 seconds per direction, following a 3-second idle baseline: about 13, 23, or 43 seconds total. The data selector caps each direction at 32 MiB (default), 64 MiB, 128 MiB, 256 MiB, 512 MiB, or 1 GiB. Each direction stops at whichever cap is reached first. Suggested connection speeds recalculate for the selected duration, leaving roughly a quarter of the data cap as headroom. Rates reflect observed HTTP throughput, with uploads counted only after the node acknowledges them. Results preserve both selected limits, include median idle/loaded latency, and can be exported. Short tests and data caps can understate capacity on fast or distant links. Hiding the tab or cancelling stops the test and retains partial results.

Normal recording does not run a speed test. The off-by-default **Include speed test at start** checkbox runs one test using the speed utility's selected destination and limits. Tests started during recording are saved with it and shaded on the timeline as artificial load. Standalone tests do not start a recording. Portal transfers use `/api/network/bandwidth/transfers` so they reach Portal even when `/api/node/*` routes to a separate game node. Nodes need the updated bandwidth routes; transfer-only credentials last for the selected test duration plus 10 seconds of grace, with a minimum lifetime of 30 seconds, a 60-second per-user/node cooldown, and two concurrent tests per node. Upload bodies are discarded and downloads use a bounded in-memory payload.

The game process section lists currently running apps across available nodes. Choose an app before recording to include its process-tree CPU usage, RSS memory, disk I/O rates, threads, uptime, and available player counts. The choice stays fixed for that session and is preserved in drafts, saved reports, and JSON exports. Inspecting the connection timeline also selects the corresponding process observation. CPU uses one logical CPU as 100%, so a multithreaded app can exceed 100%; RSS sums the process tree and can double-count shared pages. CPU and I/O rates need two observations of the same process tree and reset after restarts or tree changes. Missing or inaccessible measurements remain unavailable. Nodes need the updated `/api/node/apps/{app_name}/process` endpoint to supply process telemetry; older reports remain readable.

Players can save and export their own sessions. Admins can also read and compare other players' reports. Saved reports are client-reported evidence, retained in the private `.cache/network_diagnostics` directory for seven days, with limits of 20 per user, 200 total, and a 64 MiB cache. The API caps each submitted report at 2 MiB. Keep this cache persistent across Portal deployments to retain reports.

These HTTP probes do not measure the game's packet loss, trace ISP hops, or measure game simulation health. The first version uses configured nodes as destinations; it does not include an independent external reference endpoint. Portal and Yuki on Wakusei share a physical host and must not be treated as independent network references.

```bash
pnpm --dir frontend test
BOT_PROFILE=yuki uv run pytest checks/network_bandwidth_check.py checks/network_diagnostics_check.py checks/network_diagnostics_adapter_check.py checks/networking_check.py -q
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
- `MOD_WEB_RECONNECT_TIMEOUT_SECONDS` retains a disconnected dashboard for reconnection (default: 30 seconds). Returning online attempts to restore the existing page; users can still reload explicitly.
- `NODE_NAME` identifies the local host in node API tokens and defaults to the active bot profile name.
- `NODE_API_TOKEN_SECRET` signs Node API bearer tokens. It must be an independently generated secret and must not match `DATA_AUTHORITY_TOKEN`; requests fail closed when it is absent outside explicitly unauthenticated development mode.
- `NODE_API_UPLOAD_MAX_BYTES` caps non-mod Node API request bodies (default: 1 GiB). ZIP save uploads additionally enforce file-count, individual-file, extracted-size, and compression-ratio limits.
- `NODE_API_MOD_UPLOAD_MAX_BYTES` caps the combined files in a mod upload (default: 8 GiB). Requests allow a further 1 MiB for multipart headers. `NODE_API_MOD_EXTRACTED_MAX_BYTES` caps expanded 7D2D mod archives across the entire batch (default: 16 GiB); individual archive members are limited to 4 GiB, with file-count checks and a 1000:1 compression-ratio limit to accommodate sparse prefab data.
- 7D2D uploads accept ZIP, 7z, and single-volume RAR archives, containing a single mod or multiple mod folders, including an optional `Mods` folder and outer wrapper. Single new mods install automatically; bundles show a component preview before installation. Existing mods are identified by folder name or native mod identity and can only be skipped; built-in game and managed overhaul components are protected. Selected new components install together, with rollback of files and metadata on failure or cancellation.
- RAR also works through the shared extractor used by Discord and path-based mod installation. Compressed RAR files need a backend on the game node; install `unrar` for full RAR format support and extraction in one pass, including solid archives. RAR imports reject encryption, links, and split volumes, and enforce path and extraction-size limits. Other games retain their native mod formats (such as Minecraft JARs and Factorio/BeamMP ZIPs).
- Large request bodies spool in `DIR_TMP`; choose a filesystem with sufficient space rather than a small RAM-backed temporary directory. 7D2D archives extract directly from that spool into `.yukibot/mod-uploads` beside the app's `Mods` folder. Staging stays outside mod discovery, requires the same filesystem as `Mods`, and is moved into place without copying asset files again. Cancelled and expired previews are removed; previews expire after 30 minutes, and background cleanup also removes abandoned staging directories after a restart. Only one mod request is received or prepared at a time per node.
- Reverse proxies must allow the mod request size, including multipart overhead (8,590,983,168 bytes with the default settings), and stream uploads to the node so authentication happens before a long transfer. For Caddy, apply a matching `request_body` allowance to `/api/node/apps/*/mods/upload` and `/api/node/apps/*/mods/upload/inspect`; retain the normal limit elsewhere. For nginx, set an adequate `client_max_body_size` and disable `proxy_request_buffering` for these routes. Avoid an absolute transfer timeout; idle and upstream response timeouts must accommodate the connection and archive preparation.
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
- Updater service restarts restore the managed apps that were running on each node. Add `--no-auto-restart` to leave those apps stopped, for example `rupdater.py --restart --no-auto-restart`. Capturing running apps requires the node API to be available; a failed capture stops before restarting that node.
- `rupdater.py` reads remote targets from ignored `rupdater.targets.json`; copy `rupdater.targets.example.json` to create it. Use `--targets-file path/to/targets.json` for another profile. Set `password` to `null` to authenticate through your SSH agent or configured SSH key.
- The About page shows deployment metadata written by release deployments. `MOD_WEB_BUILD_SHA` remains an optional fallback when that metadata has not yet been created.
- Normal sign-ins use a browser-session cookie with a 16-hour server expiry. “Remember me” uses a persistent cookie with a 30-day absolute expiry.
- Mod web browser sessions authenticate as Discord user IDs and authorise through `users.json` / `Access_Control`; `visitor` access can use chat-only web relay routes, while `user` and above can use the broader mod web tools.
- `BYPASS_WEB_AUTH=true` skips Discord web auth only when `INDEV` is also set. It is intended for local development and is ignored outside `INDEV`.
- When the standalone portal hosts the web UI, sibling nodes do not need Discord OAuth credentials; the portal reads their Node API with short-lived `NODE_API_TOKEN_SECRET` bearer tokens. Remote downloads are streamed through the portal, so those bearer tokens never enter browser URLs. Download bars show archive preparation, then bytes sent by the portal when the file size is known; unknown sizes remain indeterminate. Each transfer has its own labelled status; completion and failure remain visible briefly alongside active transfers. “Transfer sent” confirms the portal finished sending, rather than confirming the browser saved the file. The browser retains its normal download handling.
- Portal forwards HTTP range requests for download resume. Nodes retain generated downloads for up to 24 hours within a 64-file / 32 GiB cache per service; eviction or a service restart can require a fresh download. Each retry checks current access, and an `If-Range` validator prevents combining different file revisions. Save uploads authenticate before receiving the body, allowing an accepted transfer to outlast its short-lived token.
- Console and chart updates pause on hidden surfaces and wait for browser acknowledgements before sending the latest pending update. Connection logs include reconnect duration, transport, optional browser-reported network estimates, transfer throughput and gaps between upload chunks. Update-volume summaries are logged at most once per minute per live surface, plus a final summary when it closes. These are application observations rather than a bandwidth test.
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
- The Portal app-installer coordinates one active install across all app nodes. Its in-memory lease is rebuilt from active Operations records after Portal starts or reconnects. Direct node-local `/api/node/app-installer/jobs` requests are lower-level APIs and can bypass this Portal-wide orchestration; node-local operation resource locks still protect folders and instance IDs.
- App-install job IDs are durable node operation IDs. Their sanitised status and log history live in `.yukibot/operations-<node>.sqlite3`; an operation still active after a node restart is retained as `interrupted` rather than retried automatically.
- Registered operations are also available through `/api/node/operations`. Listing uses the operation's read scope and omits log bodies; detail and cancellation dispatch through the registered operation kind so its executor retains control of safe cleanup.

### SteamCMD update targets

Fresh 7 Days to Die instances offer **Vanilla** and **Undead Legacy** installation profiles. Undead Legacy currently pairs game 2.6 (`v2.6`) with UL 2.7.40. It downloads both pinned official release archives, or accepts an absolute path to a complete ZIP already on the selected node. Existing instances cannot be converted. The regular Steam updater is disabled for this profile while overhaul updating is deferred.

7 Days to Die exposes **Map Rendering** in Settings (`EnableMapRendering` in `serverconfig.xml`). Enabling it also enables **Webdashboard** (`WebDashboardEnabled`): the shipped renderer initialises when the game's web server starts. The settings UI shows a toast explaining the automatic change before saving. Webdashboard stays enabled while Map Rendering is enabled; disabling Map Rendering leaves Webdashboard enabled. Restart the game server after changing these settings, then join and explore to generate tiles. The **Map** tab is always available for 7D2D and explains whether rendering is disabled, a restart is required, or explored tiles have not appeared yet. It discovers usable tiles automatically. It reads the game's shared explored map directly through the authenticated node API, including Undead Legacy's save layout.

Unrendered areas remain blank. The map includes pan, zoom, periodic tile refresh, shared dashboard annotations, and named online-player markers. Click a player or their name to centre the map without changing zoom. The coordinate readout shows the cursor position, or the map centre on touch devices, using north/south and east/west coordinates. Player positions reuse the existing five-second console poll and expire after fifteen seconds without an update; disconnected players disappear on the next successful poll. Positions are hidden when the game or console connection stops, and belong only to the running save.

An open map detects save changes and selects that save's tiles and annotations. Existing rendered tiles remain viewable while the game is stopped, provided Map Rendering remains enabled. The map does not expose the full generated world. The page checks readiness and save selection every fifteen seconds.

The map provides **Fit terrain** and **Go to coordinates** (positive north/east, negative south/west), and remembers the centre and zoom separately for each save in browser storage. Drawing shows contextual **Finish**, **Undo point**, and **Cancel** controls; Escape cancels a draft. Failed saves retain the draft and label for retry.

7D2D terrain uses a shared, fifteen-second tile index, restricted to the current viewport. Unexplored tiles make no PNG requests, and only changed tile revisions are downloaded. PNGs and indexes use authenticated private conditional requests; slow, incomplete, or failed replacements keep the last good image. Player, annotation, metadata, and terrain updates run independently, with annotations checked every fifteen seconds. Refreshes reuse existing player markers and unchanged annotations. World selection and Refresh keep the latest request when an update is already running. Leaving the app page cancels pending map requests and background work.

The shared 7D2D/Squaremap client lives in `frontend/src/map` and uses generated Python map contracts. Nodes advertise tile-index support; older nodes retain periodic tile loading until upgraded. The normal frontend build produces `frontend/dist/map.js` and copies `frontend/public/map.css`; production still needs no Node.js. Run its DOM and failure regression tests with `pnpm --dir frontend test src/map`.

The UL profile keeps game, mod, and user data inside the app folder, disables EAC, and includes `start_undead_legacy.sh` for standalone launches after copying the folder. Failed or cancelled installations remove their unfinished app folder; reusable downloads remain in the node's `opt/app-install-downloads` cache for 72 hours after their last use, with hourly expiry cleanup.

Managed UL client packs support Windows and Proton. Extract the ZIP into a matching Windows game installation, beside `7DaysToDie.exe`: it includes the complete managed overhaul and selected add-ons under `Mods/`, the pinned BepInEx/Doorstop runtime at the game root, an optional `Start Undead Legacy.cmd` launcher with EAC disabled, and `README-Undead-Legacy.txt` with installation and Proton instructions. Proton users launch through Steam with `WINEDLLOVERRIDES="winhttp.dll=n,b" %command%` and EAC disabled in the game launcher. Native Linux clients are deferred.

The client runtime uses the official release's configuration defaults and verifies loader binaries against pinned sizes and SHA-256 digests. Server BepInEx settings, logs, caches, saves, and dedicated-server launchers are excluded. Client overrides are placed relative to the game root; Windows-incompatible names and conflicting archive paths are rejected. UL remains protected in mod management, while its files, version, loader, launcher, and instructions participate in client-pack publication hashing. Recorded asset digests are verified during that same hashing pass. Existing UL packs need republishing after this layout change.

SteamCMD-backed apps fetch their publisher-provided version and beta branches from Steam app metadata, with each app's result cached for 12 hours. SteamCMD installs the current build on that branch—it cannot pin an arbitrary historical build ID unless the publisher exposes that build as a beta branch. Password-protected or custom branches can be declared in the instance's `steam_update.branches` configuration with `beta_password`.

### Satisfactory first claim

New Satisfactory servers are claimed from the game client. Use the admin password entered during installation when claiming the server; Yukibot waits for that claim and enables API management automatically afterwards.

### ETS2 and ATS first setup

Euro Truck Simulator 2 and American Truck Simulator dedicated servers install and update through SteamCMD. New installs enable Steam updates automatically; existing instances can enable them from **Properties → Update Configuration**. Each Yukibot instance receives an isolated data home and its selected connection port; the query port is the next port number. Permit both TCP and UDP for that pair (the default is 27015/27016).

SCS requires a package pair exported by a matching game client. Enable the developer console, load a map, and run `export_server_packages`; then use the instance's **Saves** tab (headed **Server Packages**) to upload both `server_packages.sii` and `server_packages.dat`. Yukibot blocks startup until both non-empty files are present.

For a persistent Steam server identity, create a Steam Game Server Login Token from **Properties → Steam Game Server Login Token**. Tokens must use the game App ID, not the dedicated-server App ID: ETS2 is `227300` (server `1948160`), and ATS is `270880` (server `2239530`).

The **Mods** tab lists mods from `server_packages.sii`, including Workshop links and whether players need each mod. Names may be truncated by the game export. This list is read-only; change your in-game mod setup, export again, then replace both package files in **Saves**. Optional mods are shown as optional only when **Settings → Optional Mods** (`mods_optioning`) is enabled.

### Type Checking

```bash
uv run basedpyright
```
