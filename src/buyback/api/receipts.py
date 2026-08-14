from fastapi import APIRouter

from buyback.api.deps import AuthDep
from buyback.db import async_session_factory
from buyback.schemas import ReceiptCreate, ReceiptResponse
from buyback.services.persons import add_receipt_item

router = APIRouter(prefix="/api/v1", tags=["receipts"])


@router.post(
    "/receipts",
    status_code=201,
    response_model=ReceiptResponse,
    tags=["receipts"],
    summary="Принять чек от POS",
    description=(
        "Принимает транзакцию от POS: создаёт клиента (если новый), добавляет позиции "
        "в чеки (максимум 30 — самая старая позиция удаляется)."
    ),
)
async def create_receipt(payload: ReceiptCreate, api_key: AuthDep) -> ReceiptResponse:
    async with async_session_factory() as session:
        item = await add_receipt_item(session, payload.phone_number, payload.receipt_data, payload.receipt_datetime)
    return ReceiptResponse(
        id=str(item.id),
        receipt_datetime=item.receipt_datetime,
        data=item.data,
        person_id=str(item.person_id),
    )
