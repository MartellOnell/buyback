import logging
from datetime import UTC, datetime

from sqlalchemy import select

from buyback.db import async_session_factory
from buyback.models import SendStatusEnum, Status
from buyback.services.channel import check_success_status

logger = logging.getLogger(__name__)


async def poll_statuses() -> None:
    async with async_session_factory() as session:
        result = await session.execute(select(Status).where(Status.send_status == SendStatusEnum.IN_PROCESS))
        statuses = list(result.scalars().all())

        if not statuses:
            return

        logger.info("status_poller_tick", extra={"pending_count": len(statuses)})

        for status in statuses:
            if not status.message_id:
                continue
            try:
                ok = await check_success_status(status.message_id)
            except Exception:
                logger.exception("status_check_failed", extra={"status_id": str(status.id)})
                continue

            if ok:
                status.send_status = SendStatusEnum.SEND
                status.sending_datetime = datetime.now(tz=UTC)
                logger.info("status_marked_send", extra={"status_id": str(status.id)})

        await session.commit()
