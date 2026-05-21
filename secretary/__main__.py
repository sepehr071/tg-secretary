import asyncio
import logging
import signal

from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

try:
    from telegram.ext import BusinessMessagesDeletedHandler  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    BusinessMessagesDeletedHandler = None  # type: ignore[assignment]

from . import commands, handlers, memory
from .config import settings
from .db import close_db, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("secretary")


async def main() -> None:
    db_conn = await init_db()

    app = (
        Application.builder()
        .token(settings.tg_bot_token)
        .build()
    )
    app.bot_data["db_conn"] = db_conn

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
    if BusinessMessagesDeletedHandler is not None:
        app.add_handler(BusinessMessagesDeletedHandler(handlers.on_business_deleted))
    app.add_handler(CommandHandler("start", handlers.on_start))
    commands.register(app)

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

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down…")
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
