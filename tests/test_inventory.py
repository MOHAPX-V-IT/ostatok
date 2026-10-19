import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg

from ostatok.cli import seed
from ostatok.db import connect, migrate, ready
from .conftest import catalogue, headers, reserve


def balances(state):
    response = state["http"].get("/api/balances", headers=headers(state))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_twenty_simultaneous_reserves_do_not_oversell(state):
    item = {**catalogue(state, (7,))[0], "quantity": 1}
    barrier = threading.Barrier(20)
    def attempt(i):
        barrier.wait(timeout=10)
        return reserve(state, [item], key=f"parallel-{i}", order=f"ORDER-{i}")
    with ThreadPoolExecutor(max_workers=20) as executor:
        responses = list(executor.map(attempt, range(20)))
    codes = [r.status_code for r in responses]
    assert codes.count(201) == 7, [(r.status_code, r.text) for r in responses]
    assert codes.count(409) == 13
    assert all(r.json()["code"] == "insufficient_stock" for r in responses if r.status_code == 409)
    stock = balances(state)[0]
    assert (stock["available"], stock["reserved"], stock["on_hand"]) == (0, 7, 7)
    with connect(state["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM reservations").fetchone()["n"] == 7
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='reserve'").fetchone()["n"] == 7


def test_bulk_rollback_and_opposite_item_order(state):
    items = catalogue(state, (2, 2))
    failed = reserve(state, [{**items[0], "quantity": 1}, {**items[1], "quantity": 3}], key="rollback")
    assert failed.status_code == 409
    assert all((r["available"], r["reserved"]) == (2, 0) for r in balances(state))
    with connect(state["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM reservations").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='reserve'").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM idempotency WHERE key='rollback'").fetchone()["n"] == 0
    barrier = threading.Barrier(2)
    def attempt(reverse):
        rows = [{**i, "quantity": 1} for i in items]
        barrier.wait(timeout=5)
        return reserve(state, rows[::-1] if reverse else rows)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [False, True]))
    assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
    assert all((r["available"], r["reserved"]) == (0, 2) for r in balances(state))


def test_idempotency_client_endpoint_scope_and_concurrent_replay(state):
    item = {**catalogue(state, (10,))[0], "quantity": 2}
    body = {"items": [item], "ttl_seconds": 900, "order_ref": "ONE-ORDER"}
    barrier = threading.Barrier(8)
    def call(_):
        barrier.wait(timeout=5)
        return state["http"].post("/api/reservations", headers=headers(state, key="one-key"), json=body)
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(call, range(8)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    rid = responses[0].json()["id"]
    conflict = state["http"].post("/api/reservations", headers=headers(state, key="one-key"), json={**body, "ttl_seconds": 901})
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    # Same client, a different API key and differently ordered JSON fields replay the result.
    sibling = state["http"].post("/api/keys", headers=headers(state, "admin"), json={"name": "Same client", "role": "operator", "client_id": state["operator"]["client_id"]}).json()
    replay = state["http"].post("/api/reservations", headers={"X-API-Key": sibling["secret"], "Idempotency-Key": "one-key"}, json=dict(reversed(list(body.items()))))
    assert replay.status_code == 201 and replay.json()["id"] == rid
    # Same idempotency text on a distinct endpoint is allowed.
    confirmed = state["http"].post(f"/api/reservations/{rid}/confirm", headers=headers(state, key="one-key"), json={})
    assert confirmed.status_code == 200
    # A different client owns a separate namespace.
    other = state["http"].post("/api/keys", headers=headers(state, "admin"), json={"name": "Other client", "role": "operator"}).json()
    another = state["http"].post("/api/reservations", headers={"X-API-Key": other["secret"], "Idempotency-Key": "one-key"}, json=body)
    assert another.status_code == 201 and another.json()["id"] != rid
    assert (balances(state)[0]["available"], balances(state)[0]["reserved"]) == (6, 2)


def test_confirm_cancel_and_journal_are_consistent(state):
    item = {**catalogue(state, (5,))[0], "quantity": 3}
    created = reserve(state, [item])
    assert created.status_code == 201
    rid = created.json()["id"]
    endpoint = f"/api/reservations/{rid}/confirm"
    confirmed = state["http"].post(endpoint, headers=headers(state, key="confirm"), json={"note": "Shipment"})
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
    replay = state["http"].post(endpoint, headers=headers(state, key="confirm"), json={"note": "Shipment"})
    assert replay.status_code == 200 and replay.json() == confirmed.json()
    changed = state["http"].post(endpoint, headers=headers(state, key="confirm"), json={"note": "Different body"})
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    repeat = state["http"].post(endpoint, headers=headers(state, key="confirm-again"), json={})
    assert repeat.status_code == 200
    conflict = state["http"].post(f"/api/reservations/{rid}/cancel", headers=headers(state, key="cancel-bad"), json={})
    assert conflict.status_code == 409
    stock = balances(state)[0]
    assert (stock["available"], stock["reserved"], stock["on_hand"]) == (2, 0, 2)
    second = reserve(state, [{**item, "quantity": 2}]).json()
    endpoint = f"/api/reservations/{second['id']}/cancel"
    first_cancel = state["http"].post(endpoint, headers=headers(state, key="cancel"), json={"note": "Buyer cancelled"})
    second_cancel = state["http"].post(endpoint, headers=headers(state, key="cancel-again"), json={})
    assert first_cancel.status_code == second_cancel.status_code == 200
    changed = state["http"].post(endpoint, headers=headers(state, key="cancel"), json={"note": "Different body"})
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    assert second_cancel.json()["status"] == "cancelled"
    with connect(state["url"]) as conn:
        rows = conn.execute("SELECT sum(delta_available) AS a,sum(delta_reserved) AS r FROM movements").fetchone()
        assert (rows["a"], rows["r"]) == (2, 0)
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='confirm'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='cancel'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM reservations").fetchone()["n"] == 2


def test_expiry_sweeper_confirm_race_releases_once(state):
    item = {**catalogue(state, (5,))[0], "quantity": 2}
    reservation = reserve(state, [item], ttl=1).json()
    rid = reservation["id"]
    with connect(state["url"]) as conn:
        conn.execute("UPDATE reservations SET expires_at=created_at+interval '0.001 seconds' WHERE id=%s", (rid,))
    time.sleep(.02)
    barrier = threading.Barrier(2)
    def confirm():
        barrier.wait(timeout=5)
        return state["http"].post(f"/api/reservations/{rid}/confirm", headers=headers(state, key="late-confirm"), json={})
    def sweep():
        barrier.wait(timeout=5)
        return state["http"].post("/api/maintenance/expire", headers=headers(state, "admin"), json={})
    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = executor.submit(confirm), executor.submit(sweep)
        confirmed, swept = a.result(), b.result()
    assert confirmed.status_code == 409 and confirmed.json()["code"] == "reservation_expired"
    assert swept.status_code == 200
    assert (balances(state)[0]["available"], balances(state)[0]["reserved"]) == (5, 0)
    with connect(state["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='expire'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM movements WHERE kind='confirm'").fetchone()["n"] == 0
    replay = state["http"].post(f"/api/reservations/{rid}/confirm", headers=headers(state, key="late-confirm"), json={})
    assert replay.status_code == 409


def test_confirm_rechecks_expiry_after_waiting_for_stock_lock(state):
    item = {**catalogue(state, (5,))[0], "quantity": 1}
    rid = reserve(state, [item], ttl=1).json()["id"]
    lock = connect(state["url"])
    lock.execute("SELECT * FROM balances FOR UPDATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(state["http"].post, f"/api/reservations/{rid}/confirm", headers=headers(state, key="blocked-confirm"), json={})
            time.sleep(1.15)
            lock.commit()
            response = pending.result()
        assert response.status_code == 409 and response.json()["code"] == "reservation_expired"
        assert (balances(state)[0]["available"], balances(state)[0]["reserved"]) == (5, 0)
    finally:
        lock.close()


def test_auth_validation_pagination_and_problem_json(state):
    http = state["http"]
    assert http.get("/health").status_code == 200
    assert http.get("/openapi.json").status_code == 200
    assert http.get("/docs").status_code == 200
    denied = http.get("/api/products", headers={"X-Request-ID": "integration-request"})
    assert denied.status_code == 401
    assert denied.headers["content-type"].startswith("application/problem+json")
    assert denied.json()["request_id"] == "integration-request"
    assert denied.headers["X-Request-ID"] == "integration-request"
    assert http.post("/api/products", headers=headers(state, "reader"), json={"sku": "NO", "name": "Forbidden"}).status_code == 403
    assert http.get("/api/keys", headers=headers(state, "reader")).status_code == 403
    items = catalogue(state, (3, 4))
    page = http.get("/api/products?limit=1&offset=1", headers=headers(state, "reader")).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert http.get("/api/products?limit=101", headers=headers(state)).status_code == 422
    assert reserve(state, [items[0]], ttl=86401).status_code == 422
    assert reserve(state, [items[0], items[0]]).status_code == 422
    assert reserve(state, [{**items[0], "quantity": 1.5}]).status_code == 422
    assert http.post("/api/reservations", headers=headers(state), json={"order_ref": "NO-KEY", "items": [items[0]]}).status_code == 400
    rid = reserve(state, [{**items[0], "quantity": 1}]).json()["id"]
    other = http.post("/api/keys", headers=headers(state, "admin"), json={"name": "Foreign reader", "role": "reader"}).json()
    assert http.get(f"/api/reservations/{rid}", headers={"X-API-Key": other["secret"]}).status_code == 403
    listed = http.get("/api/keys", headers=headers(state, "admin")).text
    assert "secret_hash" not in listed and state["operator"]["secret"] not in listed
    revoke = http.delete(f"/api/keys/{state['reader']['id']}", headers=headers(state, "admin"))
    assert revoke.status_code == 204
    assert http.get("/api/products", headers=headers(state, "reader")).status_code == 401
    assert http.delete(f"/api/keys/{state['admin']['id']}", headers=headers(state, "admin")).status_code == 409


def test_migrations_empty_and_existing_and_seed_is_idempotent(state):
    assert state["initial_migrations"] == ["001_inventory.sql", "002_query_indexes.sql"]
    assert ready(state["url"])
    seeded = seed(state["url"])
    first = balances(state)
    assert sorted(r["available"] for r in first) == [5, 20]
    seed(state["url"])
    assert balances(state) == first
    assert migrate(state["url"]) == []
    assert balances(state) == first
    # Reconstruct V1: only V2 indexes/ledger are removed, business data remains.
    with connect(state["url"]) as conn:
        for index in ("reservations_expiry_idx", "reservations_client_idx", "movements_inventory_idx", "movements_reservation_idx", "api_keys_active_client_idx"):
            conn.execute(f"DROP INDEX {index}")
        conn.execute("DELETE FROM schema_migrations WHERE version='002_query_indexes.sql'")
    assert migrate(state["url"]) == ["002_query_indexes.sql"]
    assert balances(state) == first
    item = {**seeded["products"][0], "quantity": 2}
    item.pop("reference")
    assert reserve(state, [item]).status_code == 201
    seed(state["url"])
    assert sorted((r["available"], r["reserved"]) for r in balances(state)) == [(5, 0), (18, 2)]
    with connect(state["url"]) as conn:
        row = conn.execute("SELECT version,checksum FROM schema_migrations ORDER BY version LIMIT 1").fetchone()
        conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=%s", (row["version"],))
    try:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            migrate(state["url"])
    finally:
        with connect(state["url"]) as conn:
            conn.execute("UPDATE schema_migrations SET checksum=%s WHERE version=%s", (row["checksum"], row["version"]))


def test_database_guards_and_http_body_boundary(state):
    catalogue(state, (3,))
    for column in ("available", "reserved"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect(state["url"]) as conn:
                # Identifiers are fixed test constants, not caller input.
                conn.execute(f"UPDATE balances SET {column}=-1")
    assert (balances(state)[0]["available"], balances(state)[0]["reserved"]) == (3, 0)
    with connect(state["url"]) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()["rolsuper"] is False
    oversized = state["http"].post("/api/products", headers={**headers(state), "Content-Type": "application/json"}, content=b"x" * 1_048_577)
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "payload_too_large"
    invalid = state["http"].post("/api/products", headers=headers(state), json={"sku": "X", "name": "X", "unexpected": state["operator"]["secret"]})
    assert invalid.status_code == 422
    assert state["operator"]["secret"] not in invalid.text
