import asyncio
import json
import logging
import random

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from buyback.config import settings
from buyback.models import Product

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — маркетолог по холодным продажам в розничной сети строительных магазинов.\n\n"
    "Пиши сообщения так, как это делает опытный продавец-консультант, который "
    "помнит своего покупателя: тепло, живо и по-человечески, короткими фразами. "
    "Не будь канцелярским и сухим — никаких «отлично подойдёт к вашему оборудованию».\n\n"
    "Правила написания сообщения:\n"
    "- Только русский язык, не длиннее 300 символов.\n"
    "- Начни с личной связи с покупками клиента: «Вы уже брали…», «К вашим … отлично подойдут…» — "
    "покажи, что ты помнишь его покупки из истории ниже.\n"
    "- Выбери один конкретный товар-дополнение из результатов search_products, который логично "
    "сочетается с купленным. Отметь, что он есть в наличии: «есть в наличии», «такой есть у нас в магазине».\n"
    "- Заверши тёплым приглашением заглянуть в магазин («Загляните, подберём под вашу задачу»).\n"
    "- Не добавляй название магазина и слоган в конец сообщения.\n\n"
    "Правила честности (ЭТО КРИТИЧНО — защита от выдумок):\n"
    "- Называй товары, их характеристики, бренды и факт наличия ТОЛЬКО из результатов "
    "search_products и истории покупок. Никогда не придумывай товары, модели, бренды, "
    "характеристики и наличие.\n"
    "- Не выдумывай акции, скидки, цены и сроки: упоминай их только если они указаны "
    "в истории покупок или найдены в базе. Единственное исключение — общая скидка 10%, "
    "разрешённая этим промптом ниже.\n"
    "- ВАЖНО: если search_products вернул пустой список или товары не найдены — НИКОГДА не пиши "
    "клиенту об этом. Не говори «не нашлось товаров», «не могу предложить», «ничего не найдено». "
    "Вместо этого предложи общую выгоду: скидку 10% на следующую покупку в этой категории "
    "или на любой товар из магазина.\n"
    "- Перед тем как отправить сообщение, мысленно проверь его по чек-листу: (1) каждое "
    "название товара и бренд есть в данных, (2) нет придуманных цен/акций/сроков, "
    "(3) длина до 300 символов.\n\n"
    "Пример желаемого стиля (показывает тон и форму, но не копируй его дословно):\n"
    "«Вы уже брали анкеры М10 и саморезы — если крепите что-то к бетону или кирпичу, "
    "к ним отлично подойдут распорные дюбели 10х60, они есть в наличии. Загляните, "
    "подберём под вашу задачу.»\n"
)

PROMPT_TEMPLATE = (
    "История покупок клиента (последние {count} позиций, от новых к старым):\n"
    "{items_text}\n\n"
    "Перед написанием сообщения используй функцию search_products, чтобы проверить наличие "
    "товаров в базе магазина. Ищи по категориям и базовым словам (например «перфоратор», "
    "«сверло по бетону», «лазерный уровень», «саморезы»), а не по точным моделям и брендам.\n\n"
    "Используй в сообщении ТОЛЬКО товары из результата search_products: если товара нет "
    "в этом результате, не упоминай его.\n\n"
    "Напиши одно персонализированное сообщение. В ответ верни только текст сообщения — "
    "без кавычек, заголовков, приветствий, подписи и пояснений, одним абзацем."
)

PRODUCT_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_products",
        "description": (
            "Поиск товаров в базе данных магазина по ключевым словам. "
            "Передавай короткие запросы по категориям и базовым словам (например «перфоратор», "
            "«сверло по бетону», «шуруповёрт», «лазерный уровень», «саморезы»), а не полные "
            "модели и бренды. Возвращает названия товаров, которые действительно есть в наличии."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список поисковых запросов (короткие слова категорий)",
                }
            },
            "required": ["queries"],
        },
    },
}


async def search_products_in_db(session: AsyncSession, queries: list[str]) -> list[str]:
    results: list[str] = []
    for query in queries:
        stmt = select(Product.name).where(Product.name.ilike(f"%{query}%")).limit(10)
        rows = await session.execute(stmt)
        results.extend(row for (row,) in rows)
    return list(dict.fromkeys(results))


def _chat_template_kwargs() -> dict:
    return {}


async def _call_llm(client: httpx.AsyncClient, headers: dict[str, str], messages: list[dict], tools: bool) -> dict:
    payload: dict = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "top_p": 0.95,
        "max_tokens": 2048,
        "stream": False,
        **(_chat_template_kwargs()),
    }
    if tools:
        payload["tools"] = [PRODUCT_SEARCH_TOOL]
        payload["tool_choice"] = "auto"

    last_exc: Exception | None = None
    for attempt in range(settings.LLM_MAX_RETRIES + 1):
        try:
            resp = await client.post(settings.LLM_BASE_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                last_exc = exc

                logger.warning(
                    "llm_429_rate_limited",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": settings.LLM_MAX_RETRIES,
                        "retry_after_header": exc.response.headers.get("Retry-After"),
                        "delay": delay,
                        "response_body": exc.response.text[:500],
                    },
                )
            else:
                raise

        if attempt < settings.LLM_MAX_RETRIES:
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]


def _parse_retry_after(header: str | None) -> float | None:
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _backoff_delay(attempt: int) -> float:
    return settings.LLM_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)


def _extract_content(message: dict) -> str:
    content = (message.get("content") or "").strip()
    if content:
        return content
    reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    return reasoning


async def _finalize_without_tools(client: httpx.AsyncClient, headers: dict[str, str], messages: list[dict]) -> str:
    payload: dict = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "top_p": 0.95,
        "max_tokens": 2048,
        "stream": False,
        **(_chat_template_kwargs()),
    }
    try:
        resp = await client.post(settings.LLM_BASE_URL, headers=headers, json=payload)
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        return _extract_content(message)
    except Exception:
        logger.exception("llm_finalize_failed")
        return ""


async def generate_offer(items: list[str], session: AsyncSession | None = None) -> str:
    items_text = "\n".join(f"- {item}" for item in items)
    prompt = PROMPT_TEMPLATE.format(count=len(items), items_text=items_text)

    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Accept": "application/json",
    }

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    use_tools = session is not None

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        message = await _call_llm(client, headers, messages, use_tools)

        tool_calls = message.get("tool_calls")
        while tool_calls and session is not None:
            messages.append(message)
            for tool_call in tool_calls:
                func_name = tool_call["function"]["name"]
                try:
                    func_args = json.loads(tool_call["function"]["arguments"])
                except json.JSONDecodeError:
                    func_args = {}
                if func_name == "search_products":
                    queries = func_args.get("queries", [])
                    found = await search_products_in_db(session, queries)
                    result_text = json.dumps(
                        {"found_products": found} if found else {"found_products": [], "message": "Товары не найдены"},
                        ensure_ascii=False,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": result_text,
                        }
                    )
                    logger.info(f"tool_search_products queries={queries} found={len(found)}")

            message = await _call_llm(client, headers, messages, use_tools)
            tool_calls = message.get("tool_calls")

        content = _extract_content(message)
        if not content and session is not None:
            content = await _finalize_without_tools(client, headers, messages)

        logger.info("ai_offer_generated", extra={"length": len(content)})
        if not content:
            return "Скидка 10% на следующую покупку в нашем магазине — загляните за своими инструментами и материалами!"
        return content
