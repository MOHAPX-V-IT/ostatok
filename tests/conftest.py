import os
import socket
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn
from psycopg.conninfo import conninfo_to_dict

from ostatok.app import create_app
from ostatok.db import connect, migrate
from ostatok.service import bootstrap


@pytest.fixture(scope="session")
def test_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL required: real PostgreSQL only; no fallback")
    info = conninfo_to_dict(url)
    app_info = conninfo_to_dict(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else {}
    if not info.get("dbname", "").endswith("_test") or info.get("dbname") == app_info.get("dbname"):
        pytest.fail("Refusing destructive fixtures: use a dedicated database ending in _test")
    with connect(url) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    first = migrate(url)
    return {"url": url, "initial_migrations": first}


@pytest.fixture(scope="session")
def live_server(test_database):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(test_database["url"], sweeper=False), log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started, "Real HTTP test server did not start"
    yield base
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
    assert not thread.is_alive()


@pytest.fixture
def state(test_database, live_server):
    url = test_database["url"]
    with connect(url) as conn:
        conn.execute("TRUNCATE movements,idempotency,reservation_items,reservations,receipts,balances,products,warehouses,api_keys,clients RESTART IDENTITY CASCADE")
    admin = bootstrap(url, "Integration admin")
    client = httpx.Client(base_url=live_server, timeout=25, limits=httpx.Limits(max_connections=40))
    admin_headers = {"X-API-Key": admin["secret"]}
    response = client.post("/api/keys", headers=admin_headers, json={"name": "Operator", "role": "operator"})
    assert response.status_code == 201, response.text
    operator = response.json()
    response = client.post("/api/keys", headers=admin_headers, json={"name": "Reader", "role": "reader", "client_id": operator["client_id"]})
    assert response.status_code == 201, response.text
    reader = response.json()
    yield {"url": url, "http": client, "admin": admin, "operator": operator, "reader": reader, "initial_migrations": test_database["initial_migrations"]}
    client.close()


def headers(state, role="operator", key=None):
    result = {"X-API-Key": state[role]["secret"]}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def catalogue(state, quantities=(10,)):
    client = state["http"]
    w = client.post("/api/warehouses", headers=headers(state), json={"code": "TEST-WH", "name": "Integration warehouse"})
    assert w.status_code == 201, w.text
    warehouse = w.json()["id"]
    items = []
    for i, qty in enumerate(quantities):
        p = client.post("/api/products", headers=headers(state), json={"sku": f"TEST-{i}", "name": f"Integration product {i}"})
        assert p.status_code == 201, p.text
        item = {"product_id": p.json()["id"], "warehouse_id": warehouse, "quantity": qty}
        receipt = client.post("/api/receipts", headers=headers(state, key=str(uuid4())), json={**item, "reference": "TEST-RECEIPT"})
        assert receipt.status_code == 201, receipt.text
        items.append(item)
    return items


def reserve(state, items, key=None, ttl=900, order="TEST-ORDER"):
    return state["http"].post("/api/reservations", headers=headers(state, key=key or str(uuid4())), json={"order_ref": order, "ttl_seconds": ttl, "items": items})
