import traceback
import hikari
import config

# Tiny bot to scrub any lingering commands from server

bot = hikari.GatewayBot(token=config.env_req("BOT_TOKEN"))


@bot.listen()
async def ping(event: hikari.GuildAvailableEvent) -> None:
    try:
        appli = await bot.rest.fetch_application()
        cmds = await event.app.rest.fetch_application_commands(appli, event.guild.id)
        print(cmds)
        await event.app.rest.set_application_commands(appli, [], event.guild.id)
    except Exception as xcp:
        print(f"{xcp}\n{traceback.format_exc()}")
    print("Done")


bot.run()
