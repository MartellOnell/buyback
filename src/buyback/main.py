import asyncio
import logging
import random
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from buyback.api.admin import router as admin_router
from buyback.api.deps import AuthDep
from buyback.api.receipts import router as receipts_router
from buyback.config import settings
from buyback.db import async_session_factory, init_db
from buyback.services.orchestrator import run_cycle
from buyback.services.persons import add_receipt_item
from buyback.services.status_poller import poll_statuses

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    logger.info("db_initialized")

    scheduler = AsyncIOScheduler()

    scheduler.add_job(run_cycle, "interval", days=settings.ORCHESTRATOR_INTERVAL_DAYS, id="orchestrator")
    scheduler.add_job(poll_statuses, "interval", minutes=settings.STATUS_POLLER_INTERVAL_MINUTES, id="status_poller")
    # Запускаем оркестратор один раз сразу при старте (для тестирования)
    scheduler.add_job(run_cycle, "date", id="orchestrator_startup")
    scheduler.start()
    logger.info("scheduler_started")

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(
    title="buyback",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Сервис холодных продаж на основе ИИ: принимает POS-транзакции, "
        "генерирует персонализированные предложения через LLM (LiteLLM proxy) и "
        "отправляет их клиентам в Telegram."
    ),
    openapi_tags=[
        {"name": "receipts", "description": "Приём POS-транзакций"},
        {"name": "operations", "description": "Операционные ручки (healthy, управление сервисом)"},
    ],
)
app.include_router(receipts_router)
app.include_router(admin_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://localhost:3000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["operations"], summary="Health-check")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/trigger-orchestrator",
    status_code=202,
    tags=["operations"],
    summary="Запустить цикл оркестратора",
)
async def trigger_orchestrator(background_tasks: BackgroundTasks, api_key: AuthDep) -> dict[str, str]:
    background_tasks.add_task(run_cycle)
    return {"status": "accepted", "message": "orchestrator cycle started"}


DEMO_PRODUCTS = [
    "Бензопила Husqvarna 135, 40см",
    "Цемент М500, мешок 50кг",
    "Перфоратор DeWalt D25133K",
    "Шуруповёрт Makita DF333D",
    "Лобзик Bosch PST 700 E",
    "Болгарка Makita GA5030",
    "Лазерный уровень Bosch GLL 3-80",
    "Шпатлёвка финишная Sheetrock, 20кг",
    "Саморезы по дереву 3,5х35, 200шт",
    "Краска интерьерная Tikkurila Euro 2, 9л",
]


@app.get(
    "/api/v1/demo",
    response_class=HTMLResponse,
    tags=["operations"],
    summary="Демо: случайный чек + случайный номер + запуск оркестратора",
)
async def demo_receipt_and_trigger() -> HTMLResponse:
    phone = f"+79{uuid.uuid4().int % 10**9:09d}"
    product = random.choice(DEMO_PRODUCTS)
    async with async_session_factory() as session:
        item = await add_receipt_item(session, phone, product, datetime.now(tz=UTC))
        person_id = str(item.person_id)

    async def _trigger():
        await run_cycle(person_id=person_id)

    asyncio.create_task(_trigger())

    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:40px'>"
        "<h2>Чек отправлен, оркестратор запущен</h2>"
        f"<p>Товар: <b>{product}</b></p>"
        f"<p>Номер: <b>{phone}</b></p>"
        "<p>Проверьте Telegram-бота.</p></body></html>"
    )
