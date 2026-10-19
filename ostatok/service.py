"""Inventory invariants live in transactions, never in an in-memory stock cache."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from uuid import UUID

from psycopg.types.json import Jsonb

from .db import connect


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


TITLES = {
    "unauthorized": "Требуется API-ключ", "forbidden": "Недостаточно прав",
    "not_found": "Объект не найден", "insufficient_stock": "Недостаточно доступного остатка",
    "idempotency_conflict": "Ключ уже использован с другим запросом",
    "reservation_expired": "Срок резерва истёк", "state_conflict": "Недопустимое состояние",
    "validation_error": "Проверьте параметры запроса", "database_unavailable": "Хранилище временно недоступно",
    "unique_conflict": "Такой идентификатор уже существует", "inventory_limit": "Превышен предел остатка",
    "invalid_idempotency_key": "Требуется ключ идемпотентности", "last_admin": "Нельзя отозвать последнего администратора",
}


def problem_body(status, code, detail):
    return {"type": f"urn:ostatok:{code}", "title": TITLES.get(code, "Ошибка запроса"), "status": status, "code": code, "detail": detail}


def serial(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(type(value).__name__)


def normalized(value):
    return json.loads(json.dumps(value, default=serial))


def key_hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


KEY_FIELDS = "id,client_id,name,role,created_at,revoked_at"


def issue_key(conn, name, role, client_id=None):
    if client_id is None:
        client_id = conn.execute("INSERT INTO clients(name) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
    elif not conn.execute("SELECT id FROM clients WHERE id=%s", (client_id,)).fetchone():
        raise Problem(404, "not_found", "Указанный клиент не существует")
    secret = "ost_" + secrets.token_urlsafe(32)
    row = conn.execute(f"INSERT INTO api_keys(client_id,name,role,secret_hash) VALUES (%s,%s,%s,%s) RETURNING {KEY_FIELDS}", (client_id, name, role, key_hash(secret))).fetchone()
    return normalized({**row, "secret": secret})


def bootstrap(url, name):
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(784512335)")
        if conn.execute("SELECT 1 FROM api_keys WHERE role='admin' AND revoked_at IS NULL LIMIT 1").fetchone():
            raise Problem(409, "state_conflict", "Активный admin уже существует; используйте управление ключами")
        return issue_key(conn, name, "admin")


class Inventory:
    def __init__(self, url=None):
        self.url = url

    def authenticate(self, secret):
        if not secret or not secret.startswith("ost_") or len(secret) > 128:
            raise Problem(401, "unauthorized", "Передайте действующий X-API-Key")
        with connect(self.url) as conn:
            key = conn.execute(f"SELECT {KEY_FIELDS} FROM api_keys WHERE secret_hash=%s AND revoked_at IS NULL", (key_hash(secret),)).fetchone()
        if not key:
            raise Problem(401, "unauthorized", "API-ключ не найден или отозван")
        return key

    def mutate(self, actor, endpoint, idem_key, body, operation):
        if not idem_key or not 1 <= len(idem_key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in idem_key):
            raise Problem(400, "invalid_idempotency_key", "Idempotency-Key: 1–128 печатных ASCII-символов без пробелов")
        fingerprint = hashlib.sha256(json.dumps(body, default=serial, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with connect(self.url) as conn:
            conn.execute("INSERT INTO idempotency(client_id,endpoint,key,request_hash) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (actor["client_id"], endpoint, idem_key, fingerprint))
            entry = conn.execute("SELECT request_hash,response,response_status FROM idempotency WHERE client_id=%s AND endpoint=%s AND key=%s FOR UPDATE", (actor["client_id"], endpoint, idem_key)).fetchone()
            if entry["request_hash"] != fingerprint:
                raise Problem(409, "idempotency_conflict", "Для нового содержимого используйте новый Idempotency-Key")
            if entry["response"] is not None:
                return entry["response_status"], entry["response"]
            status, result = operation(conn)
            result = normalized(result)
            conn.execute("UPDATE idempotency SET response=%s,response_status=%s WHERE client_id=%s AND endpoint=%s AND key=%s", (Jsonb(result), status, actor["client_id"], endpoint, idem_key))
            return status, result

    @staticmethod
    def _catalogue_exists(conn, item):
        if not conn.execute("SELECT 1 FROM products WHERE id=%s", (item["product_id"],)).fetchone() or not conn.execute("SELECT 1 FROM warehouses WHERE id=%s", (item["warehouse_id"],)).fetchone():
            raise Problem(404, "not_found", "Товар или склад не найден")

    @staticmethod
    def _lock_balances(conn, items):
        result = {}
        for item in sorted(items, key=lambda i: (str(i["warehouse_id"]), str(i["product_id"]))):
            pair = (str(item["warehouse_id"]), str(item["product_id"]))
            result[pair] = conn.execute("SELECT available,reserved FROM balances WHERE warehouse_id=%s AND product_id=%s FOR UPDATE", (item["warehouse_id"], item["product_id"])).fetchone()
        return result

    @staticmethod
    def _movement(conn, kind, actor_id, item, da, dr, reference, reservation_id=None, receipt_id=None, note=""):
        conn.execute("INSERT INTO movements(kind,client_id,product_id,warehouse_id,quantity,delta_available,delta_reserved,reference,reservation_id,receipt_id,note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (kind, actor_id, item["product_id"], item["warehouse_id"], item["quantity"], da, dr, reference, reservation_id, receipt_id, note))

    def receipt(self, actor, key, body):
        def operation(conn):
            self._catalogue_exists(conn, body)
            conn.execute("INSERT INTO balances(warehouse_id,product_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (body["warehouse_id"], body["product_id"]))
            self._lock_balances(conn, [body])
            row = conn.execute("INSERT INTO receipts(client_id,product_id,warehouse_id,quantity,reference) VALUES (%s,%s,%s,%s,%s) RETURNING id,product_id,warehouse_id,quantity,reference,created_at", (actor["client_id"], body["product_id"], body["warehouse_id"], body["quantity"], body["reference"])).fetchone()
            conn.execute("UPDATE balances SET available=available+%s WHERE warehouse_id=%s AND product_id=%s", (body["quantity"], body["warehouse_id"], body["product_id"]))
            self._movement(conn, "receipt", actor["client_id"], body, body["quantity"], 0, body["reference"], receipt_id=row["id"])
            return 201, row
        return self.mutate(actor, "POST /api/receipts", key, body, operation)

    def reserve(self, actor, key, body):
        def operation(conn):
            for item in body["items"]:
                self._catalogue_exists(conn, item)
            locked = self._lock_balances(conn, body["items"])
            for item in body["items"]:
                current = locked[(str(item["warehouse_id"]), str(item["product_id"]))]
                if not current or current["available"] < item["quantity"]:
                    available = current["available"] if current else 0
                    raise Problem(409, "insufficient_stock", f"Товар {item['product_id']}, склад {item['warehouse_id']}: доступно {available}, запрошено {item['quantity']}")
            # One DB clock value ensures the TTL is measured from successful lock acquisition.
            row = conn.execute("INSERT INTO reservations(client_id,order_ref,created_at,expires_at) SELECT %s,%s,t,t+(%s * interval '1 second') FROM (SELECT clock_timestamp() AS t) v RETURNING id", (actor["client_id"], body["order_ref"], body["ttl_seconds"])).fetchone()
            rid = row["id"]
            for item in body["items"]:
                conn.execute("INSERT INTO reservation_items(reservation_id,product_id,warehouse_id,quantity) VALUES (%s,%s,%s,%s)", (rid, item["product_id"], item["warehouse_id"], item["quantity"]))
                conn.execute("UPDATE balances SET available=available-%s,reserved=reserved+%s WHERE warehouse_id=%s AND product_id=%s", (item["quantity"], item["quantity"], item["warehouse_id"], item["product_id"]))
                self._movement(conn, "reserve", actor["client_id"], item, -item["quantity"], item["quantity"], body["order_ref"], reservation_id=rid)
            return 201, self._reservation(conn, rid)
        return self.mutate(actor, "POST /api/reservations", key, body, operation)

    @staticmethod
    def _reservation(conn, rid):
        row = conn.execute("SELECT * FROM reservations WHERE id=%s", (rid,)).fetchone()
        if not row:
            raise Problem(404, "not_found", "Резерв не найден")
        row["items"] = conn.execute("SELECT i.product_id,i.warehouse_id,i.quantity,p.sku,p.name AS product_name,w.code AS warehouse_code FROM reservation_items i JOIN products p ON p.id=i.product_id JOIN warehouses w ON w.id=i.warehouse_id WHERE reservation_id=%s ORDER BY i.warehouse_id,i.product_id", (rid,)).fetchall()
        return row

    @staticmethod
    def _authorize_reservation(actor, row):
        if actor["role"] != "admin" and row["client_id"] != actor["client_id"]:
            raise Problem(403, "forbidden", "Резерв принадлежит другому клиенту")

    def _close(self, conn, reservation, kind, actor_id, note=""):
        for item in reservation["items"]:
            quantity = item["quantity"]
            da = 0 if kind == "confirm" else quantity
            conn.execute("UPDATE balances SET available=available+%s,reserved=reserved-%s WHERE warehouse_id=%s AND product_id=%s", (da, quantity, item["warehouse_id"], item["product_id"]))
            self._movement(conn, kind, actor_id, item, da, -quantity, reservation["order_ref"], reservation_id=reservation["id"], note=note)
        state = {"confirm": "confirmed", "cancel": "cancelled", "expire": "expired"}[kind]
        conn.execute("UPDATE reservations SET status=%s,closed_at=clock_timestamp() WHERE id=%s", (state, reservation["id"]))
        return self._reservation(conn, reservation["id"])

    def finish(self, actor, key, rid, action, body):
        def operation(conn):
            row = conn.execute("SELECT * FROM reservations WHERE id=%s FOR UPDATE", (rid,)).fetchone()
            if not row:
                raise Problem(404, "not_found", "Резерв не найден")
            self._authorize_reservation(actor, row)
            reservation = self._reservation(conn, rid)
            if row["status"] != "active":
                if action == "confirm" and row["status"] == "confirmed" or action == "cancel" and row["status"] in ("cancelled", "expired"):
                    return 200, reservation
                code = "reservation_expired" if row["status"] == "expired" else "state_conflict"
                return 409, problem_body(409, code, f"Текущее состояние: {row['status']}")
            self._lock_balances(conn, reservation["items"])
            expired = conn.execute("SELECT clock_timestamp() >= %s AS expired", (row["expires_at"],)).fetchone()["expired"]
            if expired:
                result = self._close(conn, reservation, "expire", actor["client_id"], "TTL истёк")
                if action == "confirm":
                    return 409, problem_body(409, "reservation_expired", "Резерв истёк; остатки возвращены в доступные")
                return 200, result
            return 200, self._close(conn, reservation, action, actor["client_id"], body["note"])
        return self.mutate(actor, f"POST /api/reservations/{rid}/{action}", key, body, operation)

    def sweep(self, limit=100):
        expired = 0
        for _ in range(limit):
            with connect(self.url) as conn:
                row = conn.execute("SELECT id FROM reservations WHERE status='active' AND expires_at<=clock_timestamp() ORDER BY expires_at,id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
                if not row:
                    break
                reservation = self._reservation(conn, row["id"])
                self._lock_balances(conn, reservation["items"])
                self._close(conn, reservation, "expire", reservation["client_id"], "TTL истёк; sweeper")
                expired += 1
        return expired

    def catalogue_create(self, table, body):
        # Table and column identifiers are internal constants, never request values.
        field = "sku" if table == "products" else "code"
        with connect(self.url) as conn:
            return conn.execute(f"INSERT INTO {table}({field},name) VALUES (%s,%s) RETURNING *", (body[field], body["name"])).fetchone()

    def get_reservation(self, actor, rid):
        with connect(self.url) as conn:
            row = self._reservation(conn, rid)
            self._authorize_reservation(actor, row)
            return row

    def listing(self, kind, actor, limit, offset, filters):
        clauses, values = [], []
        if kind in ("products", "warehouses", "keys"):
            fields = f"{KEY_FIELDS}" if kind == "keys" else "*"
            source = "api_keys" if kind == "keys" else kind
            query = f"SELECT {fields} FROM {source}"
            if kind == "products" and filters.get("q"):
                clauses.append("(sku ILIKE %s OR name ILIKE %s)")
                term = "%" + filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                values.extend([term, term])
            order = "created_at DESC,id"
        elif kind == "balances":
            query = "SELECT b.*,b.available+b.reserved AS on_hand,p.sku,p.name AS product_name,w.code AS warehouse_code,w.name AS warehouse_name FROM balances b JOIN products p ON p.id=b.product_id JOIN warehouses w ON w.id=b.warehouse_id"
            for field in ("product_id", "warehouse_id"):
                if filters.get(field):
                    clauses.append(f"b.{field}=%s"); values.append(filters[field])
            order = "w.code,p.sku"
        elif kind == "movements":
            query = "SELECT m.*,p.sku,w.code AS warehouse_code FROM movements m JOIN products p ON p.id=m.product_id JOIN warehouses w ON w.id=m.warehouse_id"
            for field in ("product_id", "warehouse_id", "reservation_id"):
                if filters.get(field):
                    clauses.append(f"m.{field}=%s"); values.append(filters[field])
            order = "m.id DESC"
        else:
            query = "SELECT * FROM reservations"
            if actor["role"] != "admin":
                clauses.append("client_id=%s"); values.append(actor["client_id"])
            if filters.get("status"):
                clauses.append("status=%s"); values.append(filters["status"])
            if filters.get("order_ref"):
                clauses.append("position(%s in order_ref)>0"); values.append(filters["order_ref"])
            order = "created_at DESC,id"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = conn.execute(f"SELECT count(*) AS n FROM ({query}) counted", values).fetchone()["n"]
            rows = conn.execute(query + f" ORDER BY {order} LIMIT %s OFFSET %s", [*values, limit, offset]).fetchall()
            if kind == "reservations":
                rows = [self._reservation(conn, row["id"]) for row in rows]
            return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def create_key(self, body):
        with connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(784512335)")
            return issue_key(conn, body["name"], body["role"], body.get("client_id"))

    def revoke_key(self, kid):
        with connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(784512335)")
            row = conn.execute("SELECT role,revoked_at FROM api_keys WHERE id=%s FOR UPDATE", (kid,)).fetchone()
            if not row:
                raise Problem(404, "not_found", "Ключ не найден")
            if row["revoked_at"]:
                return
            if row["role"] == "admin" and conn.execute("SELECT count(*) AS n FROM api_keys WHERE role='admin' AND revoked_at IS NULL").fetchone()["n"] <= 1:
                raise Problem(409, "last_admin", "Сначала создайте другой admin-ключ")
            conn.execute("UPDATE api_keys SET revoked_at=clock_timestamp() WHERE id=%s", (kid,))
