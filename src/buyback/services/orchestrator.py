import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buyback.db import async_session_factory
from buyback.models import Person, ReceiptItem, SendStatusEnum, Status
from buyback.services.ai import generate_offer
from buyback.services.channel import send_message

logger = logging.getLogger(__name__)


async def select_persons(session: AsyncSession) -> list[Person]:
    cutoff = datetime.now(tz=UTC) - timedelta(days=30)
    subq = select(Status.person_id).where(Status.sending_datetime > cutoff).scalar_subquery()
    stmt = select(Person).where(~Person.id.in_(subq))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def process_person(session: AsyncSession, person: Person) -> None:
    items_stmt = (
        select(ReceiptItem)
        .where(ReceiptItem.person_id == person.id)
        .order_by(ReceiptItem.receipt_datetime.desc())
        .limit(30)
    )
    items_result = await session.execute(items_stmt)
    items = list(items_result.scalars().all())

    if not items:
        logger.info("orchestrator_no_items", extra={"person_id": str(person.id)})
        return

    items_data = [item.data for item in items]
    try:
        offer_text = await generate_offer(items_data, session)
    except Exception:
        logger.exception("ai_generation_failed", extra={"person_id": str(person.id)})
        return

    try:
        message_id = await send_message(offer_text)
    except Exception:
        logger.exception("message_send_failed", extra={"person_id": str(person.id)})
        return

    status = Status(
        person_id=person.id,
        sending_data=offer_text,
        send_status=SendStatusEnum.IN_PROCESS,
        sending_datetime=datetime.now(tz=UTC),
        message_id=message_id,
    )
    session.add(status)
    await session.commit()
    logger.info("orchestrator_person_processed", extra={"person_id": str(person.id)})


async def run_cycle(person_id: str | None = None) -> None:
    logger.info("orchestrator_cycle_start", extra={"person_id": person_id})
    async with async_session_factory() as session:
        if person_id is not None:
            person = await session.get(Person, person_id)
            persons = [person] if person is not None else []
        else:
            persons = await select_persons(session)

        logger.info("orchestrator_candidates", extra={"count": len(persons)})

        for person in persons:
            await process_person(session, person)

    logger.info("orchestrator_cycle_done")
