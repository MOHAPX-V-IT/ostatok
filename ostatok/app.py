import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__
from .db import ROOT, ready
from .models import (ActionIn, ExpireIn, KeyIn, ProductIn, ReceiptIn, ReservationIn, WarehouseIn,
                     Page, ProductOut, WarehouseOut, BalanceOut, ReceiptOut, ReservationOut,
                     MovementOut, KeyOut, IssuedKeyOut, MeOut, ProblemOut)
from .service import Inventory, Problem, normalized, problem_body

log = logging.getLogger("ostatok")
header_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(database_url: str | None = None, *, sweeper: bool = True):
    inventory = Inventory(database_url)
    try:
        interval = int(os.environ.get("EXPIRY_INTERVAL_SECONDS", "5"))
    except ValueError:
        raise RuntimeError("EXPIRY_INTERVAL_SECONDS must be an integer from 1 to 60") from None
    if not 1 <= interval <= 60:
        raise RuntimeError("EXPIRY_INTERVAL_SECONDS must be an integer from 1 to 60")

    async def expiry_loop():
        while True:
            try:
                await asyncio.to_thread(inventory.sweep, 100)
            except (psycopg.Error, RuntimeError):
                log.warning("Expiry sweep postponed: storage unavailable")
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(_app):
        if not ready(database_url):
            raise RuntimeError("Database migrations are not current; run python -m ostatok.cli migrate")
        task = asyncio.create_task(expiry_loop()) if sweeper else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Остаток", description="Складские остатки и атомарные временные резервы. reader/operator/admin через X-API-Key.", version=__version__, lifespan=lifespan,
                  responses={code: {"model": ProblemOut, "description": "Problem JSON"} for code in (400, 401, 403, 404, 409, 413, 422, 503)})
    app.state.inventory = inventory

    def failure(request, status, code, detail, extra=None):
        body = {**problem_body(status, code, detail), "request_id": getattr(request.state, "request_id", str(uuid4()))}
        if extra:
            body.update(extra)
        return JSONResponse(body, status_code=status, media_type="application/problem+json", headers={"X-Request-ID": body["request_id"]})

    @app.middleware("http")
    async def request_boundary(request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", incoming) else str(uuid4())
        if request.method in ("POST", "PUT", "PATCH"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 1_048_576:
                    return failure(request, 413, "payload_too_large", "Размер запроса превышает 1 MiB")
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Problem)
    async def domain_error(request, error):
        return failure(request, error.status, error.code, error.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        errors = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"], "kind": e["type"]} for e in error.errors()]
        return failure(request, 422, "validation_error", "Исправьте параметры запроса", {"errors": errors})

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def unique_error(request, _error):
        return failure(request, 409, "unique_conflict", "SKU или код склада уже занят")

    @app.exception_handler(psycopg.errors.CheckViolation)
    async def constraint_error(request, _error):
        return failure(request, 409, "inventory_limit", "Операция нарушает допустимые пределы остатков")

    @app.exception_handler(psycopg.Error)
    async def database_error(request, _error):
        return failure(request, 503, "database_unavailable", "PostgreSQL недоступен или занят; повторите запрос с прежним ключом")

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return failure(request, error.status_code, "not_found" if error.status_code == 404 else "http_error", str(error.detail))

    @app.exception_handler(Exception)
    async def unexpected_error(request, _error):
        log.error("Unhandled error request_id=%s kind=%s", getattr(request.state, "request_id", "unknown"), type(_error).__name__)
        return failure(request, 500, "internal_error", "Операция не подтверждена; сохраните request_id для диагностики")

    def authenticate(secret: Annotated[str | None, Security(header_key)]):
        return inventory.authenticate(secret)

    def require(role):
        def check(actor: Annotated[dict, Depends(authenticate)]):
            if {"reader": 1, "operator": 2, "admin": 3}[actor["role"]] < {"reader": 1, "operator": 2, "admin": 3}[role]:
                raise Problem(403, "forbidden", f"Операция требует роли {role}")
            return actor
        return check

    # Concrete dependency objects avoid forward-reference resolution of local aliases.
    reader, operator, admin = require("reader"), require("operator"), require("admin")

    def mutation_result(request, result):
        status, body = result
        if status >= 400:
            return failure(request, status, body["code"], body["detail"])
        return JSONResponse(normalized(body), status_code=status)

    @app.get("/health", tags=["Service"])
    def health(request: Request):
        try:
            current = ready(database_url)
        except (psycopg.Error, RuntimeError):
            current = False
        if not current:
            return failure(request, 503, "database_unavailable", "PostgreSQL или миграции не готовы")
        return {"status": "ok", "version": __version__, "database": "postgresql"}

    @app.get("/", include_in_schema=False)
    def console(request: Request):
        file = ROOT / "console" / "index.html"
        if not file.exists():
            return failure(request, 503, "console_not_ready", "Консоль ещё не установлена; API доступен на /docs")
        return FileResponse(file)

    @app.get("/api/me", tags=["Access"], response_model=MeOut)
    def me(actor=Depends(reader)):
        return normalized({"key_id": actor["id"], "client_id": actor["client_id"], "name": actor["name"], "role": actor["role"]})

    @app.get("/api/products", tags=["Catalogue"], response_model=Page[ProductOut])
    def products(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), q: str | None = Query(None, max_length=100), actor=Depends(reader)):
        return normalized(inventory.listing("products", actor, limit, offset, {"q": q}))

    @app.post("/api/products", status_code=201, tags=["Catalogue"], response_model=ProductOut)
    def create_product(body: ProductIn, actor=Depends(operator)):
        return normalized(inventory.catalogue_create("products", body.model_dump(mode="json")))

    @app.get("/api/warehouses", tags=["Catalogue"], response_model=Page[WarehouseOut])
    def warehouses(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(reader)):
        return normalized(inventory.listing("warehouses", actor, limit, offset, {}))

    @app.post("/api/warehouses", status_code=201, tags=["Catalogue"], response_model=WarehouseOut)
    def create_warehouse(body: WarehouseIn, actor=Depends(operator)):
        return normalized(inventory.catalogue_create("warehouses", body.model_dump(mode="json")))

    @app.get("/api/balances", tags=["Inventory"], response_model=Page[BalanceOut])
    def balances(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), product_id: UUID | None = None, warehouse_id: UUID | None = None, actor=Depends(reader)):
        return normalized(inventory.listing("balances", actor, limit, offset, {"product_id": product_id, "warehouse_id": warehouse_id}))

    @app.post("/api/receipts", status_code=201, tags=["Inventory"], response_model=ReceiptOut)
    def receipt(request: Request, body: ReceiptIn, actor=Depends(operator), key: str | None = Header(None, alias="Idempotency-Key")):
        return mutation_result(request, inventory.receipt(actor, key, body.model_dump(mode="json")))

    @app.post("/api/reservations", status_code=201, tags=["Reservations"], response_model=ReservationOut)
    def reserve(request: Request, body: ReservationIn, actor=Depends(operator), key: str | None = Header(None, alias="Idempotency-Key")):
        return mutation_result(request, inventory.reserve(actor, key, body.model_dump(mode="json")))

    @app.get("/api/reservations", tags=["Reservations"], response_model=Page[ReservationOut])
    def reservations(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), status: Literal["active", "confirmed", "cancelled", "expired"] | None = None, order_ref: str | None = Query(None, max_length=160), actor=Depends(reader)):
        return normalized(inventory.listing("reservations", actor, limit, offset, {"status": status, "order_ref": order_ref}))

    @app.get("/api/reservations/{rid}", tags=["Reservations"], response_model=ReservationOut)
    def reservation(rid: UUID, actor=Depends(reader)):
        return normalized(inventory.get_reservation(actor, rid))

    @app.post("/api/reservations/{rid}/confirm", tags=["Reservations"], response_model=ReservationOut)
    def confirm(request: Request, rid: UUID, body: ActionIn | None = None, actor=Depends(operator), key: str | None = Header(None, alias="Idempotency-Key")):
        return mutation_result(request, inventory.finish(actor, key, rid, "confirm", (body or ActionIn()).model_dump(mode="json")))

    @app.post("/api/reservations/{rid}/cancel", tags=["Reservations"], response_model=ReservationOut)
    def cancel(request: Request, rid: UUID, body: ActionIn | None = None, actor=Depends(operator), key: str | None = Header(None, alias="Idempotency-Key")):
        return mutation_result(request, inventory.finish(actor, key, rid, "cancel", (body or ActionIn()).model_dump(mode="json")))

    @app.get("/api/movements", tags=["Inventory"], response_model=Page[MovementOut])
    def movements(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), product_id: UUID | None = None, warehouse_id: UUID | None = None, reservation_id: UUID | None = None, actor=Depends(reader)):
        return normalized(inventory.listing("movements", actor, limit, offset, {"product_id": product_id, "warehouse_id": warehouse_id, "reservation_id": reservation_id}))

    @app.post("/api/maintenance/expire", tags=["Service"])
    def expire(body: ExpireIn | None = None, actor=Depends(admin)):
        return {"expired": inventory.sweep((body or ExpireIn()).limit)}

    @app.get("/api/keys", tags=["Access"], response_model=Page[KeyOut])
    def keys(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000), actor=Depends(admin)):
        return normalized(inventory.listing("keys", actor, limit, offset, {}))

    @app.post("/api/keys", status_code=201, tags=["Access"], response_model=IssuedKeyOut)
    def create_key(body: KeyIn, actor=Depends(admin)):
        return inventory.create_key(body.model_dump(mode="json"))

    @app.delete("/api/keys/{kid}", status_code=204, tags=["Access"])
    def revoke_key(kid: UUID, actor=Depends(admin)):
        inventory.revoke_key(kid)
        return Response(status_code=204)

    app.mount("/console", StaticFiles(directory=ROOT / "console", check_dir=False), name="console")
    return app


app = create_app()
