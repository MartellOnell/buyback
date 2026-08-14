from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buyback.models import Base, Person, ReceiptItem
from buyback.services.persons import add_receipt_item


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_get_or_create_person(session: AsyncSession):
    phone = "+79000000001"
    item = await add_receipt_item(session, phone, "молоко, 90р", datetime(2026, 1, 15, tzinfo=UTC))
    assert item.person.phone_number == phone

    item2 = await add_receipt_item(session, phone, "хлеб, 50р", datetime(2026, 2, 10, tzinfo=UTC))
    assert item2.person_id == item.person_id


async def test_max_items_cap(session: AsyncSession):
    phone = "+79000000003"
    for i in range(35):
        await add_receipt_item(session, phone, f"товар {i}", datetime(2026, 1, min(i + 1, 28), tzinfo=UTC))

    from sqlalchemy import select

    person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone))).scalar_one()
    # после 35 добавлений должно остаться ровно 30 (самые свежие)
    result = await session.execute(select(ReceiptItem).where(ReceiptItem.person_id == person_id))
    items = result.scalars().all()
    assert len(items) == 30
    dates = [i.receipt_datetime for i in items]
    assert dates[0] >= datetime(2026, 1, 6)


async def test_oldest_deleted(session: AsyncSession):
    phone = "+79000000004"
    await add_receipt_item(session, phone, "тов1", datetime(2025, 1, 1, tzinfo=UTC))
    await add_receipt_item(session, phone, "тов2", datetime(2025, 2, 1, tzinfo=UTC))
    await add_receipt_item(session, phone, "тов3", datetime(2025, 3, 1, tzinfo=UTC))

    # дозабиваем до 30 и ещё одну сверху — старая вылетит
    for i in range(28):
        await add_receipt_item(session, phone, f"доб {i}", datetime(2025, 4, i + 1, tzinfo=UTC))

    from sqlalchemy import select

    person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone))).scalar_one()
    result = await session.execute(select(ReceiptItem).where(ReceiptItem.person_id == person_id))
    items = result.scalars().all()
    assert len(items) == 30
    dates = [i.receipt_datetime for i in items]
    assert datetime(2025, 1, 1, tzinfo=UTC) not in dates  # самая старая ушла
