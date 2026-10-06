"""Run the hosting site: python -m hosting (behind Caddy on 127.0.0.1)."""
import asyncio
import logging

import uvicorn

from . import db
from .app import create_app
from .bot import run_platform_bot  # Task 11
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs carry bot tokens


async def main() -> None:
    await db.init(settings.db_path)
    settings.tenants_root.mkdir(parents=True, exist_ok=True)
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=settings.listen_port,
                                           log_config=None, access_log=False))
    bot = asyncio.create_task(run_platform_bot())
    try:
        await server.serve()
    finally:
        bot.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
