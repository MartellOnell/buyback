import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buyback.models import Base, Person, Product, Status
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


def _build_nvidia_responses(queries: list[str], found: list[str], final_offer: str) -> list[MagicMock]:
    tool_resp = MagicMock()
    tool_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_e2e",
                            "type": "function",
                            "function": {
                                "name": "search_products",
                                "arguments": json.dumps({"queries": queries}, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ],
    }
    tool_resp.raise_for_status = MagicMock()

    final_resp = MagicMock()
    final_resp.json.return_value = {"choices": [{"message": {"content": final_offer}}]}
    final_resp.raise_for_status = MagicMock()

    return [tool_resp, final_resp]


async def test_e2e_run_cycle_single_person(session: AsyncSession):
    """
    E2E: одна персона → run_cycle → AI вызывает search_products (реальная БД) → Telegram → Status.

    Мокаются только внешние HTTP-вызовы: LLM API и Telegram.
    """
    phone = "+79000000100"
    for i in range(3):
        await add_receipt_item(session, phone, f"цемент М500, мешок {i}", datetime(2026, 6, i + 1, tzinfo=UTC))

    session.add(Product(name="Цемент М500"))
    session.add(Product(name="Песок строительный"))
    await session.commit()

    nvidia_responses = _build_nvidia_responses(
        queries=["Цемент", "Песок"],
        found=["Цемент М500", "Песок строительный"],
        final_offer="Загляните за цементом М500 и песком — всё для стройки!",
    )

    send_mock = AsyncMock(return_value="msg-e2e-001")

    with (
        patch("buyback.services.ai.httpx.AsyncClient") as client_cls,
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=nvidia_responses)
        client_cls.return_value = fake_client

        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    from sqlalchemy import select

    person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone))).scalar_one()
    status_result = await session.execute(select(Status).where(Status.person_id == person_id))
    status = status_result.scalar_one()

    assert status.sending_data == "Загляните за цементом М500 и песком — всё для стройки!"
    assert status.send_status == "IN_PROCESS"
    assert status.message_id == "msg-e2e-001"

    send_mock.assert_called_once_with("Загляните за цементом М500 и песком — всё для стройки!")

    post_calls = fake_client.post.call_args_list
    assert len(post_calls) == 2

    first_payload = post_calls[0].kwargs["json"]
    assert "tools" in first_payload
    assert first_payload["tools"][0]["function"]["name"] == "search_products"

    second_payload = post_calls[1].kwargs["json"]
    tool_msg = second_payload["messages"][3]["content"]
    assert "Цемент М500" in tool_msg
    assert "Песок строительный" in tool_msg

    user_msg = second_payload["messages"][1]["content"]
    assert "цемент М500, мешок 0" in user_msg


async def test_e2e_run_cycle_multiple_persons(session: AsyncSession):
    """
    E2E: три персоны — run_cycle обрабатывает всех без недавних статусов.
    """
    phones = ["+79000000101", "+79000000102", "+79000000103"]
    for phone in phones:
        await add_receipt_item(session, phone, "гвозди 100мм", datetime(2026, 7, 1, tzinfo=UTC))

    session.add(Product(name="Гвозди 100мм"))
    await session.commit()

    nvidia_responses = _build_nvidia_responses(
        queries=["гвозди"],
        found=["Гвозди 100мм"],
        final_offer="Гвозди 100мм уже ждут вас в магазине!",
    )

    send_mock = AsyncMock(return_value="msg-multi")

    with (
        patch("buyback.services.ai.httpx.AsyncClient") as client_cls,
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=nvidia_responses * 3)
        client_cls.return_value = fake_client

        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    assert send_mock.call_count == 3
    for _phone in phones:
        send_mock.assert_any_call("Гвозди 100мм уже ждут вас в магазине!")


async def test_e2e_run_cycle_skips_recent_status(session: AsyncSession):
    """
    E2E: одна персона с недавним статусом (пропускается), другая без — обрабатывается.
    """
    from sqlalchemy import select

    from buyback.models import SendStatusEnum

    phone_skip = "+79000000104"
    phone_process = "+79000000105"

    await add_receipt_item(session, phone_skip, "краска", datetime(2026, 8, 1, tzinfo=UTC))
    await add_receipt_item(session, phone_process, "кисть", datetime(2026, 8, 1, tzinfo=UTC))

    skip_person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone_skip))).scalar_one()
    session.add(
        Status(
            person_id=skip_person_id,
            sending_data="старое",
            send_status=SendStatusEnum.SEND,
            sending_datetime=datetime(2026, 8, 10, tzinfo=UTC),
            message_id="old-msg",
        )
    )
    await session.commit()

    nvidia_responses = _build_nvidia_responses(
        queries=["кисть"],
        found=[],
        final_offer="Кисти в наличии — заходите!",
    )

    send_mock = AsyncMock(return_value="msg-skip")

    with (
        patch("buyback.services.ai.httpx.AsyncClient") as client_cls,
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=nvidia_responses)
        client_cls.return_value = fake_client

        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    send_mock.assert_called_once_with("Кисти в наличии — заходите!")

    process_person_id = (
        await session.execute(select(Person.id).where(Person.phone_number == phone_process))
    ).scalar_one()
    status = (await session.execute(select(Status).where(Status.person_id == process_person_id))).scalar_one()
    assert status.sending_data == "Кисти в наличии — заходите!"


async def test_e2e_run_cycle_search_products_empty_db(session: AsyncSession):
    """
    E2E: в БД нет товаров — search_products возвращает пустой список,
    AI получает сообщение «Товары не найдены» и генерирует ответ без конкретных товаров.
    """
    phone = "+79000000106"
    await add_receipt_item(session, phone, "шурупы 50мм", datetime(2026, 8, 1, tzinfo=UTC))

    nvidia_responses = _build_nvidia_responses(
        queries=["шурупы"],
        found=[],
        final_offer="Загляните в наш магазин за новинками!",
    )

    send_mock = AsyncMock(return_value="msg-empty")

    with (
        patch("buyback.services.ai.httpx.AsyncClient") as client_cls,
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.post = AsyncMock(side_effect=nvidia_responses)
        client_cls.return_value = fake_client

        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    second_payload = fake_client.post.call_args_list[1].kwargs["json"]
    tool_msg = second_payload["messages"][3]["content"]
    assert "Товары не найдены" in tool_msg

    send_mock.assert_called_once_with("Загляните в наш магазин за новинками!")


async def test_e2e_run_cycle_no_persons(session: AsyncSession):
    """
    E2E: в БД нет персон — run_cycle не падает и не делает вызовов.
    """
    send_mock = AsyncMock()

    with (
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    send_mock.assert_not_called()
