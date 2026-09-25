"""Chaos helper: runs INSIDE fulfillment-worker. Counts fulfilment jobs per order id in $CHAOS_ORDER_IDS."""

import asyncio
import json
import os

from sqlalchemy import func, select

from commerce_common.db import create_engine, create_session_factory
from fulfillment_svc.models import FulfillmentJob
from fulfillment_svc.settings import FulfillmentSettings


async def main() -> None:
    engine = create_engine(FulfillmentSettings().database_url, pool_size=1, max_overflow=0)
    counts = {}
    async with create_session_factory(engine)() as session:
        for order_id in json.loads(os.environ["CHAOS_ORDER_IDS"]):
            counts[order_id] = await session.scalar(
                select(func.count()).select_from(FulfillmentJob).where(FulfillmentJob.order_id == order_id)
            )
    await engine.dispose()
    print("CHAOS_RESULT " + json.dumps(counts))


asyncio.run(main())
