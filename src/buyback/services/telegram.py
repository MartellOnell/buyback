import logging
import uuid

import httpx

from buyback.config import settings

logger = logging.getLogger(__name__)

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send_message(text: str) -> str:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        raise RuntimeError("telegram_not_configured")
    url = SEND_URL.format(token=settings.TELEGRAM_BOT_TOKEN)
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.post(url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": text})
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram_send_failed: {data}")
    raw_id = str(data.get("result", {}).get("message_id") or uuid.uuid4())
    logger.info("telegram_sent", extra={"chat_id": settings.TELEGRAM_CHAT_ID, "message_id": raw_id})
    return raw_id


async def check_success_status(_message_id: str) -> bool:
    return True
