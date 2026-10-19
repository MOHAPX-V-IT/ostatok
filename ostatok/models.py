from typing import Literal, Generic, TypeVar
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductIn(StrictModel):
    sku: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9А-Яа-яЁё._-]+$")
    name: str = Field(min_length=1, max_length=200)


class WarehouseIn(StrictModel):
    code: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9А-Яа-яЁё._-]+$")
    name: str = Field(min_length=1, max_length=200)


class Item(StrictModel):
    product_id: UUID
    warehouse_id: UUID
    quantity: int = Field(ge=1, le=1_000_000_000, strict=True)


class ReceiptIn(Item):
    reference: str = Field(min_length=1, max_length=160)


class ReservationIn(StrictModel):
    order_ref: str = Field(min_length=1, max_length=160)
    ttl_seconds: int = Field(default=900, ge=1, le=86_400, strict=True)
    items: list[Item] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_pairs(self):
        pairs = {(i.warehouse_id, i.product_id) for i in self.items}
        if len(pairs) != len(self.items):
            raise ValueError("Each product/warehouse pair must occur once")
        return self


class ActionIn(StrictModel):
    note: str = Field(default="", max_length=500)


class KeyIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    role: Literal["reader", "operator", "admin"]
    client_id: UUID | None = None


class ExpireIn(StrictModel):
    limit: int = Field(default=100, ge=1, le=1000, strict=True)


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ProductOut(BaseModel):
    id: UUID
    sku: str
    name: str
    created_at: datetime


class WarehouseOut(BaseModel):
    id: UUID
    code: str
    name: str
    created_at: datetime


class BalanceOut(BaseModel):
    product_id: UUID
    warehouse_id: UUID
    available: int
    reserved: int
    on_hand: int
    sku: str
    product_name: str
    warehouse_code: str
    warehouse_name: str


class ReceiptOut(ReceiptIn):
    id: UUID
    created_at: datetime


class ReservationItemOut(Item):
    sku: str
    product_name: str
    warehouse_code: str


class ReservationOut(BaseModel):
    id: UUID
    client_id: UUID
    order_ref: str
    status: Literal["active", "confirmed", "cancelled", "expired"]
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    items: list[ReservationItemOut]


class MovementOut(BaseModel):
    id: int
    kind: Literal["receipt", "reserve", "confirm", "cancel", "expire"]
    client_id: UUID
    product_id: UUID
    warehouse_id: UUID
    reservation_id: UUID | None
    receipt_id: UUID | None
    quantity: int
    delta_available: int
    delta_reserved: int
    reference: str
    note: str
    created_at: datetime
    sku: str
    warehouse_code: str


class KeyOut(BaseModel):
    id: UUID
    client_id: UUID
    name: str
    role: Literal["reader", "operator", "admin"]
    created_at: datetime
    revoked_at: datetime | None


class IssuedKeyOut(KeyOut):
    secret: str = Field(description="Shown once; only SHA-256 is persisted")


class MeOut(BaseModel):
    key_id: UUID
    client_id: UUID
    name: str
    role: Literal["reader", "operator", "admin"]


class ProblemOut(BaseModel):
    type: str
    title: str
    status: int
    code: str
    detail: str
    request_id: str
