import asyncio
import hashlib
import os
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from webapp import create_app, init_db

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5486146974"))
MANAGER = os.getenv("MANAGER_USERNAME", "savrrage").strip().lstrip("@")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = (
    os.getenv("PUBLIC_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
).rstrip("/")


async def main():
    if not TOKEN or ":" not in TOKEN:
        raise RuntimeError("Добавьте BOT_TOKEN в Environment на Render")
    if not PUBLIC_URL.startswith("https://"):
        raise RuntimeError("Render не передал публичный HTTPS-адрес")

    init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    webapp_info = WebAppInfo(url=PUBLIC_URL)

    @dp.message(CommandStart())
    async def start(message: Message):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть AutoPrime",
                        web_app=webapp_info,
                    )
                ]
            ]
        )
        await message.answer(
            "🏁 <b>AutoPrime</b>\n"
            "Рули с характером. Каталог, заказ и статус — в одном приложении.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    app = create_app(TOKEN, ADMIN_ID, MANAGER)
    webhook_path = "/telegram/webhook"
    webhook_secret = hashlib.sha256(TOKEN.encode()).hexdigest()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=webhook_secret,
    ).register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    try:
        await bot.set_webhook(
            PUBLIC_URL + webhook_path,
            secret_token=webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="AutoPrime",
                web_app=webapp_info,
            )
        )
        me = await bot.get_me()
        print(f"ГОТОВО: @{me.username}")
        print(f"Магазин: {PUBLIC_URL}")
        print(f"Webhook: {PUBLIC_URL}{webhook_path}")

        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stopped.set)
            except NotImplementedError:
                pass
        await stopped.wait()
    finally:
        # Webhook специально не удаляется: после сна Render Telegram разбудит сервис.
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"ОШИБКА: {type(error).__name__}: {error}", file=sys.stderr)
        raise