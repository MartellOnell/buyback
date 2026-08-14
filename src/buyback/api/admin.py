import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.inspection import inspect as sa_inspect

from buyback.db import async_session_factory
from buyback.models import Person, Product, ReceiptItem, Status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

RESOURCES: dict[str, type] = {
    "products": Product,
    "persons": Person,
    "receipt-items": ReceiptItem,
    "statuses": Status,
}


def _serialize(model: type, obj) -> dict:
    data: dict = {}
    for column in sa_inspect(model).columns:
        value = getattr(obj, column.key)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        data[column.key] = value
    return data


async def _get_resource(model: type, obj_id: str):
    async with async_session_factory() as session:
        try:
            obj = await session.get(model, UUID(obj_id))
        except ValueError:
            obj = None
        if obj is None:
            raise HTTPException(status_code=404, detail="Not found")
        return obj


def _apply_filters(stmt, model: type, params: dict):
    columns = {c.key: c for c in sa_inspect(model).columns}
    search_terms: list[str] = []

    for key, value in params.items():
        if value == "" or value is None:
            continue

        operator = "eq"
        field = key
        for suffix, op in (("_like", "like"), ("_gte", "gte"), ("_lte", "lte"), ("_ne", "ne")):
            if key.endswith(suffix):
                field = key[: -len(suffix)]
                operator = op
                break

        if field == "q":
            search_terms.append(str(value))
            continue
        if field not in columns:
            continue

        column = columns[field]
        if operator == "like":
            stmt = stmt.where(column.ilike(f"%{value}%"))
        elif operator == "gte":
            stmt = stmt.where(column >= value)
        elif operator == "lte":
            stmt = stmt.where(column <= value)
        elif operator == "ne":
            stmt = stmt.where(column != value)
        else:
            stmt = stmt.where(column == value)

    for term in search_terms:
        like = f"%{term}%"
        conditions = [column.ilike(like) for column in sa_inspect(model).columns]
        stmt = stmt.where(or_(*conditions))

    return stmt


@router.get("/{resource}")
async def list_resource(resource: str, request: Request) -> dict:
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown resource")

    query_params = dict(request.query_params)
    start_index = int(query_params.pop("startIndex", 0) or 0)
    page_size = int(query_params.pop("pageSize", 10) or 10)
    sort_field = query_params.pop("sortField", None)
    sort_direction = query_params.pop("sortDirection", "asc")

    columns = {c.key: c for c in sa_inspect(model).columns}

    stmt = _apply_filters(select(model), model, query_params)
    count_stmt = _apply_filters(select(func.count()).select_from(model), model, query_params)

    async with async_session_factory() as session:
        total = (await session.execute(count_stmt)).scalar_one()
        if sort_field and sort_field in columns:
            sort_column = columns[sort_field]
            stmt = stmt.order_by(sort_column.desc() if sort_direction == "desc" else sort_column.asc())
        stmt = stmt.offset(start_index).limit(page_size)
        rows = (await session.execute(stmt)).scalars().all()

    return {"data": {"items": [_serialize(model, r) for r in rows], "totalItems": total}}


@router.get("/{resource}/{obj_id}")
async def get_one(resource: str, obj_id: str) -> dict:
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown resource")
    obj = await _get_resource(model, obj_id)
    return {"data": _serialize(model, obj)}


@router.post("/{resource}", status_code=201)
async def create_one(resource: str, request: Request) -> dict:
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown resource")

    payload = await request.json()
    allowed = {c.key: c for c in sa_inspect(model).columns if c.key != "id"}

    obj = model()
    for key, value in payload.items():
        if key in allowed:
            setattr(obj, key, value)

    async with async_session_factory() as session:
        session.add(obj)
        await session.commit()
        await session.refresh(obj)

    return {"data": _serialize(model, obj)}


@router.put("/{resource}/{obj_id}")
async def update_one(resource: str, obj_id: str, request: Request) -> dict:
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown resource")

    obj = await _get_resource(model, obj_id)
    payload = await request.json()
    allowed = {c.key for c in sa_inspect(model).columns if c.key != "id"}

    async with async_session_factory() as session:
        obj = await session.get(model, obj.id)
        for key, value in payload.items():
            if key in allowed:
                setattr(obj, key, value)
        await session.commit()
        await session.refresh(obj)

    return {"data": _serialize(model, obj)}


@router.patch("/{resource}/{obj_id}")
async def patch_one(resource: str, obj_id: str, request: Request) -> dict:
    return await update_one(resource, obj_id, request)


@router.delete("/{resource}/{obj_id}")
async def delete_one(resource: str, obj_id: str) -> dict:
    model = RESOURCES.get(resource)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown resource")
    obj = await _get_resource(model, obj_id)
    async with async_session_factory() as session:
        obj = await session.get(model, obj.id)
        await session.delete(obj)
        await session.commit()
    return {"data": _serialize(model, obj)}
