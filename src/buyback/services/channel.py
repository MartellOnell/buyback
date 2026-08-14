import logging

from buyback.services import telegram

logger = logging.getLogger(__name__)


async def send_message(text: str) -> str:
    return await telegram.send_message(text)


async def check_success_status(message_id: str) -> bool:
    return await telegram.check_success_status(message_id)
