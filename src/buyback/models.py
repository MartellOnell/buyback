from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SendStatusEnum(StrEnum):
    IN_PROCESS = "IN_PROCESS"
    SEND = "SEND"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(512), unique=True, index=True)


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    receipt_items: Mapped[list[ReceiptItem]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="ReceiptItem.receipt_datetime"
    )
    statuses: Mapped[list[Status]] = relationship(back_populates="person", cascade="all, delete-orphan")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    receipt_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[str] = mapped_column(Text)

    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    person: Mapped[Person] = relationship(back_populates="receipt_items")


class Status(Base):
    __tablename__ = "statuses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    sending_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    sending_data: Mapped[str] = mapped_column(Text)
    send_status: Mapped[SendStatusEnum] = mapped_column(
        String(32), default=SendStatusEnum.IN_PROCESS, server_default=SendStatusEnum.IN_PROCESS.value
    )
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    person: Mapped[Person] = relationship(back_populates="statuses")
