import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buyback.models import Person, ReceiptItem

logger = logging.getLogger(__name__)

MAX_ITEMS = 30


async def get_or_create_person(session: AsyncSession, phone_number: str) -> Person:
    result = await session.execute(select(Person).where(Person.phone_number == phone_number))
    person = result.scalar_one_or_none()
    if person is None:
        person = Person(phone_number=phone_number)
        session.add(person)
        await session.flush()
        logger.info("created_person", extra={"phone_number": phone_number, "person_id": str(person.id)})
    return person


async def add_receipt_item(
    session: AsyncSession, phone_number: str, data: str, receipt_datetime: datetime
) -> ReceiptItem:
    person = await get_or_create_person(session, phone_number)

    count_q = select(ReceiptItem).where(ReceiptItem.person_id == person.id)
    items = (await session.execute(count_q.order_by(ReceiptItem.receipt_datetime.asc()))).scalars().all()
    items_count = len(items)

    if items_count >= MAX_ITEMS:
        oldest = items[0]
        await session.delete(oldest)
        await session.flush()

    item = ReceiptItem(person_id=person.id, data=data, receipt_datetime=receipt_datetime)
    session.add(item)
    await session.commit()
    await session.refresh(item, ["person"])
    logger.info("receipt_item_added", extra={"phone": phone_number, "item_id": str(item.id)})
    return item
