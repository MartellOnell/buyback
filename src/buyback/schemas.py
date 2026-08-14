from datetime import datetime

from pydantic import BaseModel, Field


class ReceiptCreate(BaseModel):
    phone_number: str = Field(
        description="Номер телефона клиента в международном формате",
        examples=["+79991234567"],
    )
    receipt_data: str = Field(
        description="Позиции из чека, разделённые ;",
        examples=["Перфоратор DeWalt D25133K; Сверло по бетону 6х110; Уровень лазерный"],
    )
    receipt_datetime: datetime = Field(
        description="Дата и время покупки (UTC)",
        examples=["2026-01-15T12:00:00Z"],
    )


class ReceiptResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str = Field(description="UUID позиции чека")
    receipt_datetime: datetime = Field(description="Дата и время покупки")
    data: str = Field(description="Позиции из чека")
    person_id: str = Field(description="UUID клиента (Person)")
