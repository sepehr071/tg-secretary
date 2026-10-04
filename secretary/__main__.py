import asyncio
import logging
import signal

from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    BusinessMessagesDeletedHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import commands, handlers, memory
from .config import settings
from .dashboard.app import create_app, make_server, run_server
from .db import close_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs every request URL at INFO, and Telegram API URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("secretary")


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error (update=%s)", update, exc_info=ctx.error)


async def main() -> None:
    await init_db()

    # Concurrent updates: the reply pipeline sleeps for the race-guard delay, so
    # serial processing would hide the owner's own messages (and commands) until
    # the delay ended. Bursts in one chat are collapsed in handlers._chat_gen.
    app = (
        Application.builder()
        .token(settings.tg_bot_token)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(BusinessConnectionHandler(handlers.on_business_connection))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
            handlers.on_business_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE & filters.VOICE,
            handlers.on_business_voice,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE & ~filters.TEXT & ~filters.VOICE,
            handlers.on_business_non_text,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_BUSINESS_MESSAGE,
            handlers.on_business_edited,
        )
    )
    app.add_handler(BusinessMessagesDeletedHandler(handlers.on_business_deleted))
    app.add_handler(CommandHandler("start", handlers.on_start))
    commands.register(app)
    app.add_error_handler(_on_error)

    await app.initialize()
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling(
        allowed_updates=[
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
            "message",
            "callback_query",
        ],
        drop_pending_updates=False,
    )

    log.info("Secretary bot online. Model=%s. Ctrl+C to stop.", settings.openrouter_model)

    worker_task = asyncio.create_task(memory.extract_worker())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop.set()

    # SIGINT/SIGTERM may not be available on Windows event loop policy in some setups.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    dash_server = None
    dash_task = None
    if settings.dashboard_enabled:
        dash_server = make_server(create_app(app.bot, _request_stop), settings.dashboard_port,
                                  settings.dashboard_host)
        dash_task = asyncio.create_task(run_server(dash_server))
        log.info("Dashboard on %s:%d — send /dashboard to the bot for a login link",
                 settings.dashboard_host, settings.dashboard_port)
        if settings.dashboard_host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("Dashboard is publicly bound; over plain http the session can be sniffed. "
                        "Prefer an https reverse proxy or the ssh tunnel.")

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down…")
        if dash_server is not None and dash_task is not None:
            dash_server.should_exit = True
            await dash_task
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
