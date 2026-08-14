import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import HTTPStatusError, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buyback.models import Base, Person, Product, Status
from buyback.services.ai import PROMPT_TEMPLATE, SYSTEM_PROMPT, generate_offer, search_products_in_db
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


def _fake_nvidia_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
    }
    resp.raise_for_status = MagicMock()
    return resp


def _fake_httpx_client(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=resp)
    return client


# --- unit-тесты generate_offer без session (старый путь без tool calling) ---


async def test_generate_offer_prompt_formatting():
    items = ["молоко, 90р", "хлеб, 50р", "яблоки, 120р"]
    expected_items_text = "- молоко, 90р\n- хлеб, 50р\n- яблоки, 120р"
    expected_prompt = PROMPT_TEMPLATE.format(count=3, items_text=expected_items_text)

    fake_resp = _fake_nvidia_response("Купите свежие яблоки со скидкой 10%!")
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        result = await generate_offer(items)

    call_args = fake_client.post.call_args
    payload = call_args.kwargs["json"]
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"] == SYSTEM_PROMPT
    assert payload["messages"][1]["role"] == "user"
    assert payload["messages"][1]["content"] == expected_prompt
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["temperature"] == 0.3
    assert payload["top_p"] == 0.95
    assert payload["max_tokens"] == 2048
    assert payload["stream"] is False
    assert "tools" not in payload
    assert result == "Купите свежие яблоки со скидкой 10%!"


async def test_generate_offer_response_parsing():
    items = ["товар 1"]
    fake_resp = _fake_nvidia_response("  Текст с пробелами по краям  ")
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        result = await generate_offer(items)

    assert result == "Текст с пробелами по краям"


async def test_generate_offer_empty_list():
    items: list[str] = []
    fake_resp = _fake_nvidia_response("Нет истории покупок")
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        result = await generate_offer(items)

    expected_prompt = PROMPT_TEMPLATE.format(count=0, items_text="")
    call_args = fake_client.post.call_args
    assert call_args.kwargs["json"]["messages"][1]["content"] == expected_prompt
    assert result == "Нет истории покупок"


async def test_generate_offer_http_error():
    items = ["товар 1"]
    request = Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    bad_response = Response(401, request=request)
    bad_response._content = b'{"error": "unauthorized"}'

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=HTTPStatusError("Unauthorized", request=request, response=bad_response))

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client), pytest.raises(HTTPStatusError):
        await generate_offer(items)


async def test_generate_offer_malformed_response():
    items = ["товар 1"]
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"unexpected": "structure"}
    fake_resp.raise_for_status = MagicMock()

    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client), pytest.raises(KeyError):
        await generate_offer(items)


async def test_generate_offer_headers_and_auth():
    items = ["товар 1"]
    fake_resp = _fake_nvidia_response("ok")
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        await generate_offer(items)

    call_kwargs = fake_client.post.call_args.kwargs
    assert "Authorization" in call_kwargs["headers"]
    assert call_kwargs["headers"]["Accept"] == "application/json"


async def test_generate_offer_long_sms_content():
    items = ["тест"]
    long_text = "А" * 300
    fake_resp = _fake_nvidia_response(long_text)
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        result = await generate_offer(items)

    assert result == long_text


# --- тесты retry при 429 ---


async def test_generate_offer_retry_429_succeeds():
    """После нескольких 429 вызов успешно завершается."""
    items = ["тест"]

    request = Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    rate_limit_resp = Response(429, request=request, headers={"Retry-After": "0.01"})
    success_resp = MagicMock()
    success_resp.json.return_value = {"choices": [{"message": {"content": "Успех после ретраев"}}]}
    success_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(
        side_effect=[
            HTTPStatusError("429", request=request, response=rate_limit_resp),
            HTTPStatusError("429", request=request, response=rate_limit_resp),
            success_resp,
        ]
    )

    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.asyncio.sleep", AsyncMock()),
    ):
        result = await generate_offer(items)

    assert result == "Успех после ретраев"
    assert fake_client.post.call_count == 3


async def test_generate_offer_retry_429_exhausted():
    """429 на каждом вызове — после исчерпания ретраев исключение пробрасывается."""
    items = ["тест"]

    request = Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    rate_limit_resp = Response(429, request=request)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=HTTPStatusError("429", request=request, response=rate_limit_resp))

    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.asyncio.sleep", AsyncMock()),
        pytest.raises(HTTPStatusError),
    ):
        await generate_offer(items)

    assert fake_client.post.call_count == 1 + 5  # initial + NVIDIA_MAX_RETRIES = 6


async def test_generate_offer_no_retry_on_non_429():
    """Не-429 ошибки не ретраятся."""
    items = ["тест"]

    request = Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    bad_response = Response(500, request=request)

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=HTTPStatusError("500", request=request, response=bad_response))

    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.asyncio.sleep", AsyncMock()),
        pytest.raises(HTTPStatusError),
    ):
        await generate_offer(items)

    assert fake_client.post.call_count == 1


async def test_generate_offer_retry_uses_retry_after_header():
    """Retry-After из заголовка используется как задержка."""
    items = ["тест"]

    request = Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    rate_limit_resp = Response(429, request=request, headers={"Retry-After": "3.5"})
    success_resp = MagicMock()
    success_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    success_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(
        side_effect=[
            HTTPStatusError("429", request=request, response=rate_limit_resp),
            success_resp,
        ]
    )

    sleep_mock = AsyncMock()
    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.asyncio.sleep", sleep_mock),
    ):
        await generate_offer(items)

    sleep_mock.assert_called_once_with(3.5)


def _fake_tool_call_response(tool_name: str, tool_args: dict, tool_call_id: str = "call_001") -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ],
    }
    resp.raise_for_status = MagicMock()
    return resp


async def test_generate_offer_with_session_sends_tools():
    items = ["молоко"]
    fake_resp = _fake_nvidia_response("Предложение без tool call")
    fake_client = _fake_httpx_client(fake_resp)

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        await generate_offer(items, session=MagicMock())

    payload = fake_client.post.call_args.kwargs["json"]
    assert "tools" in payload
    assert len(payload["tools"]) == 1
    assert payload["tools"][0]["function"]["name"] == "search_products"
    assert payload["tool_choice"] == "auto"


async def test_generate_offer_tool_calling_full_flow():
    """Модель вызывает search_products, получает результат, генерирует финальный ответ."""
    items = ["молоко, 90р", "хлеб, 50р"]

    tool_resp = _fake_tool_call_response("search_products", {"queries": ["молоко", "хлеб"]})
    final_resp = _fake_nvidia_response("Купите Молоко 3.2% и Хлеб Бородинский со скидкой!")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=[tool_resp, final_resp])

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=MagicMock())

    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.search_products_in_db", AsyncMock(return_value=["Молоко 3.2%", "Хлеб Бородинский"])),
    ):
        result = await generate_offer(items, session=mock_session)

    assert result == "Купите Молоко 3.2% и Хлеб Бородинский со скидкой!"
    assert fake_client.post.call_count == 2

    first_call = fake_client.post.call_args_list[0].kwargs["json"]
    assert first_call["tools"][0]["function"]["name"] == "search_products"

    second_call = fake_client.post.call_args_list[1].kwargs["json"]
    messages = second_call["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "search_products"
    assert messages[3]["role"] == "tool"
    assert "Молоко 3.2%" in messages[3]["content"]


async def test_generate_offer_tool_calling_no_products_found(session: AsyncSession):
    """Когда search_products возвращает пустой список."""
    items = ["редкий_товар"]

    tool_resp = _fake_tool_call_response("search_products", {"queries": ["редкий_товар"]})
    final_resp = _fake_nvidia_response("К сожалению, подходящих товаров не найдено.")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=[tool_resp, final_resp])

    with patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client):
        result = await generate_offer(items, session=session)

    second_call = fake_client.post.call_args_list[1].kwargs["json"]
    tool_message = second_call["messages"][3]["content"]
    assert "Товары не найдены" in tool_message
    assert result == "К сожалению, подходящих товаров не найдено."


async def test_generate_offer_tool_calling_multiple_queries(session: AsyncSession):
    """Модель передаёт несколько поисковых запросов."""
    items = ["молоко, хлеб, сыр"]

    tool_resp = _fake_tool_call_response("search_products", {"queries": ["молоко", "хлеб", "сыр"]})
    final_resp = _fake_nvidia_response("Попробуйте наш новый Сыр Гауда!")

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=[tool_resp, final_resp])

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=MagicMock())

    with (
        patch("buyback.services.ai.httpx.AsyncClient", return_value=fake_client),
        patch("buyback.services.ai.search_products_in_db", AsyncMock(return_value=["Сыр Гауда"])),
    ):
        await generate_offer(items, session=mock_session)

    call_args = fake_client.post.call_args_list[1].kwargs["json"]
    tool_message = call_args["messages"][3]["content"]
    assert "Сыр Гауда" in tool_message


async def test_search_products_in_db_finds_matches(session: AsyncSession):
    """Интеграционный тест: search_products_in_db с реальной БД."""
    products = ["Молоко 3.2%", "Хлеб Бородинский", "Сыр Гауда", "Яблоки Голден"]
    for name in products:
        session.add(Product(name=name))
    await session.commit()

    result = await search_products_in_db(session, ["Молоко", "Сыр", "шоколад"])
    assert "Молоко 3.2%" in result
    assert "Сыр Гауда" in result
    assert len([r for r in result if "шоколад" in r.lower()]) == 0
    assert "Хлеб Бородинский" not in result


async def test_search_products_in_db_empty_db(session: AsyncSession):
    result = await search_products_in_db(session, ["молоко"])
    assert result == []


async def test_search_products_in_db_deduplicates(session: AsyncSession):
    session.add(Product(name="Молоко"))
    await session.commit()

    result = await search_products_in_db(session, ["Молоко", "Молоко", "Молоко"])
    assert result == ["Молоко"]


# --- интеграционные тесты оркестратора ---


async def test_orchestrator_full_cycle_integration(session: AsyncSession):
    phone = "+79000000005"
    for i in range(5):
        await add_receipt_item(session, phone, f"покупка {i}", datetime(2026, 1, i + 1, tzinfo=UTC))

    fake_offer = "Персональное предложение: скидка 20% на любимые товары!"

    with (
        patch(
            "buyback.services.orchestrator.generate_offer",
            AsyncMock(return_value=fake_offer),
        ),
        patch(
            "buyback.services.orchestrator.send_message",
            AsyncMock(return_value="msg-12345"),
        ),
        patch(
            "buyback.services.orchestrator.async_session_factory",
            return_value=session,
        ),
    ):
        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    from sqlalchemy import select

    person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone))).scalar_one()
    status_result = await session.execute(select(Status).where(Status.person_id == person_id))
    status = status_result.scalar_one()

    assert status.sending_data == fake_offer
    assert status.send_status == "IN_PROCESS"
    assert status.message_id == "msg-12345"


async def test_orchestrator_skips_person_with_recent_status(session: AsyncSession):
    from sqlalchemy import select

    from buyback.models import Person, SendStatusEnum, Status

    phone = "+79000000006"
    await add_receipt_item(session, phone, "покупка", datetime(2026, 8, 1, tzinfo=UTC))
    person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone))).scalar_one()

    recent_status = Status(
        person_id=person_id,
        sending_data="старое предложение",
        send_status=SendStatusEnum.SEND,
        sending_datetime=datetime(2026, 8, 10, tzinfo=UTC),
        message_id="msg-99999",
    )
    session.add(recent_status)
    await session.commit()

    mock_generate = AsyncMock()

    with (
        patch("buyback.services.orchestrator.generate_offer", mock_generate),
        patch(
            "buyback.services.orchestrator.send_message",
            AsyncMock(return_value="msg-dummy"),
        ),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    mock_generate.assert_not_called()


async def test_orchestrator_handles_ai_failure(session: AsyncSession):
    phone_good = "+79000000007"
    phone_bad = "+79000000008"

    await add_receipt_item(session, phone_good, "покупка good", datetime(2026, 8, 1, tzinfo=UTC))
    await add_receipt_item(session, phone_bad, "покупка bad", datetime(2026, 8, 1, tzinfo=UTC))

    call_order = []

    async def generate_mock(items, _session=None):
        call_order.append(items)
        if "bad" in items[0]:
            raise RuntimeError("AI service unavailable")
        return "Хорошее предложение"

    async def send_mock(text):
        return "msg-sent"

    with (
        patch("buyback.services.orchestrator.generate_offer", generate_mock),
        patch("buyback.services.orchestrator.send_message", send_mock),
        patch("buyback.services.orchestrator.async_session_factory", return_value=session),
    ):
        from buyback.services.orchestrator import run_cycle

        await run_cycle()

    from sqlalchemy import select

    good_person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone_good))).scalar_one()
    bad_person_id = (await session.execute(select(Person.id).where(Person.phone_number == phone_bad))).scalar_one()

    good_status = (await session.execute(select(Status).where(Status.person_id == good_person_id))).scalar_one_or_none()
    bad_status = (await session.execute(select(Status).where(Status.person_id == bad_person_id))).scalar_one_or_none()

    assert good_status is not None
    assert good_status.sending_data == "Хорошее предложение"
    assert bad_status is None
