"""Ensure MongoDB indexes (safe to re-run). Usage: python -m scripts.init_indexes"""
from __future__ import annotations

import asyncio
import logging

from app.observability.logging_setup import configure_logging


async def main() -> None:
    configure_logging()
    from app.db.mongo import init_indexes, close_client

    await init_indexes()
    close_client()
    logging.getLogger(__name__).info("indexes ok")


if __name__ == "__main__":
    asyncio.run(main())
