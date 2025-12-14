import asyncio
import base64
import contextlib
import email.utils
import enum
import hashlib
import hmac
import json
import logging
import random
import regex as reg
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import aioftp
import aiohttp
import hikari
from pydantic import BaseModel, field_validator

import _resolator
import _utils
import config
from _discord import App_Bound, DC_Relay
from apps._app import AM_Receiver

log = logging.getLogger(__name__)

BASE = f"http://{config.env_req('SE_SERVER_IP')}:{config.env_opt('SE_SERVER_PORT')}"
# KEY_B64 = config.env_req("SE_SERVER_RAPI")  # from <RemoteSecurityKey> in SpaceEngineers-Dedicated.cfg

DISABLE = True
env = config.env_opt("SE_SERVER_DISABLE")
if env:
    env = env.lower()
    if env == "true":
        DISABLE = True

log.info("Space Engineers SETUP | DISABLE=%s", DISABLE)


def _trace():
    tc = aiohttp.TraceConfig()

    @tc.on_request_start
    async def _start(session, ctx, params):
        log.debug("HTTP %s %s", params.method, params.url)
        for k, v in (params.headers or {}).items():
            if k.lower() not in {"authorization"}:
                log.debug("  > %s: %s", k, v)

    @tc.on_request_end
    async def _end(session, ctx, params):
        log.debug("HTTP done %s %s", params.method, params.url)

    @tc.on_request_exception
    async def _exc(session, ctx, params):
        log.error("HTTP EXC %s %s", params.method, params.url)

    return tc


@dataclass
class FTPConfig:
    host: str
    port: int
    user: str
    password: str
    path_cfg: str  # absolute path to SpaceEngineers-Dedicated.cfg


class SEKeyManager:
    def __init__(self, initial_b64: str, ftp: FTPConfig | None = None) -> None:
        self._key_b64 = initial_b64
        self._key_bytes = base64.b64decode(initial_b64)
        self._ftp = ftp
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0  # monotonic seconds
        self._backoff = 5.0  # seconds
        self._max_backoff = 300.0

    def current_b64(self) -> str:
        return self._key_b64

    def current_bytes(self) -> bytes:
        return self._key_bytes

    async def refresh_from_ftp(self) -> bool:
        if not self._ftp:
            log.warning("SEKeyManager: FTP not configured, cannot refresh key")
            return False

        # basic jittered cooldown so we don't spam FTP after repeated 403s
        now = asyncio.get_running_loop().time()
        if now < self._cooldown_until:
            return False

        async with self._lock:
            # double-check after acquiring lock
            now = asyncio.get_running_loop().time()
            if now < self._cooldown_until:
                return False

            try:
                log.info("SEKeyManager: refreshing RemoteSecurityKey via FTP...")
                async with aioftp.Client.context(
                    self._ftp.host, self._ftp.port, self._ftp.user, self._ftp.password
                ) as client:
                    stream = await client.download_stream(self._ftp.path_cfg)
                    raw = await stream.read()
                    with contextlib.suppress(Exception):
                        await stream.finish()

                # Space Engineers config is XML; key is <RemoteSecurityKey>base64...</RemoteSecurityKey>
                root = ET.fromstring(raw)
                key_node = root.find(".//RemoteSecurityKey")
                if key_node is None or not key_node.text:
                    log.error("SEKeyManager: RemoteSecurityKey missing in cfg")
                    # apply backoff
                    self._cooldown_until = now + self._backoff
                    self._backoff = min(self._backoff * 2, self._max_backoff)
                    return False

                new_b64 = key_node.text.strip()
                new_bytes = base64.b64decode(new_b64)

                # success
                self._key_b64 = new_b64
                self._key_bytes = new_bytes
                self._backoff = 5.0
                self._cooldown_until = now + 2.0
                log.info("SEKeyManager: key refreshed")
                return True

            except Exception as e:
                log.exception("SEKeyManager: FTP refresh failed: %r", e)
                self._cooldown_until = now + self._backoff
                self._backoff = min(self._backoff * 2, self._max_backoff)
                return False


# at top-level setup
KEYMGR = SEKeyManager(
    initial_b64=config.env_req("SE_SERVER_RAPI"),
    ftp=FTPConfig(
        host=config.env_req("SE_FTP_HOST"),
        port=int(config.env_opt("SE_FTP_PORT") or 21),
        user=config.env_req("SE_FTP_USER"),
        password=config.env_req("SE_FTP_PASS"),
        path_cfg=config.env_req("SE_CFG_PATH"),  # e.g. "/home/se/Instance/SpaceEngineers-Dedicated.cfg"
    ),
)


_EMOJI_PREFIX_RE = reg.compile(
    r"^(?:"
    r"[\uE000-\uF8FF]"  # Private Use Area (fonts stick platform icons here)
    r"|[\U0001F300-\U0001FAFF]"  # Emoji blocks
    r"|[\u2600-\u27BF]"  # Misc symbols/dingbats
    r"|[\U0001F1E6-\U0001F1FF]"  # Regional indicator flags
    r"|[\u200D\uFE0F]"  # ZWJ / VS16
    r")+"
    r"\s*"
)


class Endpoint(enum.StrEnum):
    _vrr = "/vrageremote"
    _ver = "v1"
    _server = "server"
    _session = "session"
    ping = f"{_vrr}/{_ver}/{_server}/ping"
    api = f"{_vrr}/api"
    players = f"{_vrr}/{_ver}/{_session}/players"
    chat = f"{_vrr}/{_ver}/{_session}/chat"


def norm_name(string: str):
    return string.lower()


Allowed_Players = {norm_name(n) for n in []}


class SE_Base(BaseModel):
    SteamID: int
    DisplayName: str  # raw from SE (with platform glyphs)
    CleanName: str = ""  # derived

    model_config = {"extra": "ignore"}

    @field_validator("DisplayName", mode="after")
    @classmethod
    def _coerce_str(cls, v):
        return v if isinstance(v, str) else str(v)

    def model_post_init(self, __context):
        if not self.CleanName:
            object.__setattr__(self, "CleanName", _EMOJI_PREFIX_RE.sub("", self.DisplayName).strip())

    @property
    def is_real(self):
        print(f"SE_Base: {self.CleanName} | {self.DisplayName}")
        return (self.CleanName != self.DisplayName) and self.CleanName

    @property
    def user_allowed(self):
        return self.is_real or norm_name(self.CleanName) in Allowed_Players


class SE_Player(SE_Base):
    FactionName: str
    FactionTag: str
    PromoteLevel: int
    Ping: int


class SE_Message(SE_Base):
    Content: str
    Timestamp: int


class Ticks:
    count: int | None = None


def auth_headers(path: str) -> dict[str, str]:
    date = email.utils.formatdate(usegmt=True)
    nonce = str(random.randint(1, 2_147_483_646))
    msg = f"{path}\r\n{nonce}\r\n{date}\r\n".encode("utf-8")
    mac = hmac.new(KEYMGR.current_bytes(), msg, hashlib.sha1).digest()
    return {"Date": date, "Authorization": f"{nonce}:{base64.b64encode(mac).decode()}"}


async def se_get(session: aiohttp.ClientSession, path: str, q: str = "", **kw):
    url = BASE + path + q
    r = await session.get(url, headers=auth_headers(path + q), **kw)
    if r.status in (401, 403) and await KEYMGR.refresh_from_ftp():
        r.release()  # free connection
        r = await session.get(url, headers=auth_headers(path + q), **kw)
    return r


async def se_post(session: aiohttp.ClientSession, path: str, data: bytes | str, **kw):
    url = BASE + path
    base = auth_headers(path)
    hdrs = kw.pop("headers", None)
    if hdrs:
        base = base | hdrs
    # important: don't advertise JSON unless we truly send JSON
    return await session.post(url, headers=base, data=data, **kw)


async def ping(session: aiohttp.ClientSession) -> bool:
    path = Endpoint.ping.value
    r = await se_get(session, path)
    if r.status == 200:
        return True
    log.error("SE.ping: %s | %s | %s", r.status, r.reason, (await r.text()).strip())
    return False


async def info(session: aiohttp.ClientSession) -> str | None:
    path = Endpoint.api.value
    r = await se_get(session, path)
    if r.status == 200:
        return await r.text()
    log.error("SE.info: %s | %s | %s", r.status, r.reason, (await r.text()).strip())
    return None


async def player(session: aiohttp.ClientSession) -> list[SE_Player] | None:
    path = Endpoint.players.value
    r = await se_get(session, path)
    if r.status != 200:
        log.error("SE.player: %s | %s | %s", r.status, r.reason, (await r.text()).strip())
        return None
    data: dict = await r.json()
    return [SE_Player(**play) for play in data.get("data", {}).get("Players", [])]


async def get_chat(session: aiohttp.ClientSession, ticks: int | None = None) -> list[SE_Message] | None:
    path = Endpoint.chat.value
    q = f"?Date={ticks + 1}" if ticks is not None else ""
    r = await se_get(session, path, q)
    if r.status != 200:
        if r.status == 403:
            Ticks.count = 0
        log.error("SE.chat: %s | %s | %s", r.status, r.reason, (await r.text()).strip())
        return None

    data: dict = await r.json()
    messes = [SE_Message(**mess) for mess in data.get("data", {}).get("Messages", [])]
    return [m for m in messes if m.Content and m.user_allowed]


async def say(session: aiohttp.ClientSession, text: str) -> bool:
    path = Endpoint.chat.value
    url = BASE + path

    async def _try(payload: bytes | str, ctype: str):
        hdrs = auth_headers(path) | {"Content-Type": ctype, "Connection": "keep-alive"}
        r = await session.post(url, headers=hdrs, data=payload)
        if r.status in (401, 403) and await KEYMGR.refresh_from_ftp():
            r.release()
            hdrs = auth_headers(path) | {"Content-Type": ctype, "Connection": "keep-alive"}
            r = await session.post(url, headers=hdrs, data=payload)
        return r

    for chunk in _utils.Utilities.chunket(text, 200):
        # Variant A: plain text
        try_order = [
            (chunk.encode("utf-8"), "text/plain"),
            (json.dumps(chunk, ensure_ascii=False).encode("utf-8"), "application/json"),
            (json.dumps({"Message": chunk}, ensure_ascii=False).encode("utf-8"), "application/json"),
        ]
        sent = False
        for payload, ctype in try_order:
            try:
                r = await _try(payload, ctype)
            except aiohttp.ClientError as e:
                log.warning("SE.say %s failed: %r", ctype, e)
                continue
            body = (await r.text())[:300]
            if r.status in (200, 204):
                sent = True
                break
            log.error("SE.say %s -> %s %s | %s", ctype, r.status, r.reason, body.strip())
        if not sent:
            return False
        await asyncio.sleep(0.25)
    return True


class SpaceEngineers:
    def __init__(self, bot: hikari.GatewayBot, reso: _resolator.Resolutator) -> None:
        self.am_receiver = Receiver(self)
        self.session: aiohttp.ClientSession | None = None
        self.chat_task: asyncio.Task | None = None

        self.bot = bot
        self.reso = reso
        self.chat_channel = hikari.Snowflake(config.env_req("GAME_CHAT_CHANNEL"))
        DC_Relay._special_channels.setdefault(self.chat_channel, set())
        DC_Relay._special_channels[self.chat_channel].add(("SE", self.am_receiver.send))

        self.ticks_file = Path("se_ticks")
        self.misses = 0

        log.info(f"Space Engineers SETUP | {DISABLE=}")

    async def setup(self):
        if DISABLE:
            return
        Ticks.count = int(self.ticks_file.read_text().strip()) if self.ticks_file.exists() else None

        chan = await self.reso.channel(self.chat_channel)
        if not chan or not hasattr(chan, "send"):
            log.error("SE.setup: chan None")
        self.chan: hikari.TextableChannel = chan  # pyright: ignore[reportAttributeAccessIssue]
        await asyncio.sleep(1)

        if self.session:
            return
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=25),
            connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, ssl=False),
            headers={"Connection": "keep-alive"},
            trace_configs=[_trace()],
        )
        self.chat_task = asyncio.create_task(self.fetch_chat_loop())

    async def fetch_chat_loop(self):
        if DISABLE:
            return
        assert self.session is not None
        await asyncio.sleep(0.6)

        def jitter(delay: int | float):
            return min(delay * 1.2, 30.0) + random.uniform(0, 0.3)

        while True:
            try:
                msgs = await get_chat(self.session, Ticks.count)  # uses ?Date= when ticks is set
                if msgs:
                    self.misses = 0
                    # normal path
                    for m in msgs:
                        ts = m.Timestamp

                        if Ticks.count is not None and ts < Ticks.count:
                            # restart detected mid-stream
                            Ticks.count = ts
                            self.ticks_file.write_text(f"{Ticks.count}")
                        else:
                            Ticks.count = max(Ticks.count or 0, ts)
                            self.ticks_file.write_text(f"{Ticks.count}")
                        await self.chan.send(f"<{m.CleanName}> {m.Content}")

                    delay = 0.82 + random.uniform(0, 0.3)
                else:
                    self.misses += 1
                    if self.misses >= 5:
                        # probe: unfiltered fetch
                        recent = await get_chat(self.session, None)  # no Date param
                        if recent:
                            max_ts = max(x.Timestamp for x in recent)
                            if Ticks.count is None or max_ts >= Ticks.count:
                                # reboot confirmed
                                Ticks.count = max_ts
                                self.ticks_file.write_text(f"{Ticks.count}")
                        self.misses = 0
                    delay = jitter(2.5)
            except Exception:
                delay = jitter(15)
            await asyncio.sleep(delay)

    async def close(self):
        if DISABLE:
            return
        if self.chat_task:
            self.chat_task.cancel()
        if self.session:
            await self.session.close()
            self.session = None


class Receiver(AM_Receiver):
    def __init__(self, app: SpaceEngineers) -> None:
        super().__init__()
        self.app = app

    async def send(self, payload: App_Bound):
        if DISABLE:
            return
        if isinstance(payload.player, str) and payload.player.isnumeric():
            player = int(payload.player)
        elif isinstance(payload.player, int):
            player = payload.player
        else:
            player = None

        if self.app.session:
            player = await config.Name_Cache().best_known(player) if player else None
            await say(self.app.session, f"{player}: {payload.content}")


async def async_main():
    async with aiohttp.ClientSession() as s:
        print(await ping(s))
        # print(await info(s))
        # print(await get_chat(s))
        print(await player(s))


if __name__ == "__main__":
    asyncio.run(async_main())
