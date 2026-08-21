import traceback
from pathlib import Path

import hikari

import config

# Tiny bot to scrub any lingering commands from server

TOKEN_FILE: Path = Path(__file__).with_name("token.reset")
TOKEN_ENVIRONMENT_VARIABLES: frozenset[str] = frozenset({"BOT_TOKEN", "YUKI_BOT_TOKEN", "ERIN_BOT_TOKEN"})


def load_reset_token(token_file: Path = TOKEN_FILE) -> str:
    """Read the reset bot token or its .env variable name from ``token.reset``."""
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"{token_file} must contain a bot token or a supported environment variable name")
    if token in TOKEN_ENVIRONMENT_VARIABLES:
        return config.env_req(token)
    return token


def create_bot() -> hikari.GatewayBot:
    bot = hikari.GatewayBot(token=load_reset_token())

    @bot.listen()
    async def clear(event: hikari.GuildAvailableEvent) -> None:
        try:
            appli = await bot.rest.fetch_application()
            cmds = await event.app.rest.fetch_application_commands(appli, event.guild.id)
            print(cmds)
            await event.app.rest.set_application_commands(appli, [], event.guild.id)
        except Exception as xcp:
            print(f"{xcp}\n{traceback.format_exc()}")
        print("Done")

    return bot


def main() -> None:
    create_bot().run()


if __name__ == "__main__":
    main()
