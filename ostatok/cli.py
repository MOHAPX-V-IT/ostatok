"""Local administration. Credentials are never read from command line flags."""
import argparse
import json
import os
import sys
import time
from uuid import UUID

import psycopg

from .db import connect, migrate
from .service import Inventory, Problem, bootstrap, normalized

SEED_CLIENT = UUID("f5571131-2d4c-4647-a50d-000000000001")


def seed(url=None):
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(784512336)")
        conn.execute("INSERT INTO clients(id,name) VALUES (%s,'Synthetic demo seed') ON CONFLICT DO NOTHING", (SEED_CLIENT,))
        warehouse = conn.execute("INSERT INTO warehouses(code,name) VALUES ('DEMO-WH','Демонстрационный склад') ON CONFLICT(code) DO NOTHING RETURNING id").fetchone()
        if not warehouse:
            warehouse = conn.execute("SELECT id,name FROM warehouses WHERE code='DEMO-WH'").fetchone()
            if warehouse["name"] != "Демонстрационный склад":
                raise Problem(409, "seed_conflict", "DEMO-WH уже занят другими данными; seed отменён")
        products = []
        for sku, name, quantity in [("DEMO-NOTE-A5", "Блокнот A5 / демо", 20), ("DEMO-PEN-BL", "Ручка синяя / демо", 5)]:
            product = conn.execute("INSERT INTO products(sku,name) VALUES (%s,%s) ON CONFLICT(sku) DO NOTHING RETURNING id", (sku, name)).fetchone()
            if not product:
                product = conn.execute("SELECT id,name FROM products WHERE sku=%s", (sku,)).fetchone()
                if product["name"] != name:
                    raise Problem(409, "seed_conflict", f"{sku} уже занят другими данными; seed отменён")
            products.append({"product_id": product["id"], "warehouse_id": warehouse["id"], "quantity": quantity, "reference": f"synthetic-seed:{sku}"})
    inventory = Inventory(url)
    for body in products:
        inventory.receipt({"client_id": SEED_CLIENT, "role": "admin"}, body["reference"], normalized(body))
    return {"warehouse_id": str(warehouse["id"]), "products": normalized(products), "note": "Synthetic seed: repeated run does not replenish consumed stock"}


def main():
    # Preserve Russian messages when a Windows terminal or launcher redirects output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Остаток: миграции, начальный ключ и освобождение TTL")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Применить SQL-миграции с проверкой checksum")
    boot = commands.add_parser("bootstrap", help="Создать первого admin, показать секрет один раз")
    boot.add_argument("--name", default="local-admin")
    commands.add_parser("seed", help="Идемпотентные синтетические товары и приход")
    sweep = commands.add_parser("sweep", help="Освободить истёкшие резервы")
    sweep.add_argument("--limit", type=int, default=100)
    sweep.add_argument("--watch", action="store_true")
    sweep.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("Задайте DATABASE_URL для настоящего PostgreSQL")
    try:
        if args.command == "migrate":
            result = {"applied": migrate()}
        elif args.command == "bootstrap":
            if not 1 <= len(args.name.strip()) <= 120:
                parser.error("--name: 1..120 символов")
            result = bootstrap(None, args.name.strip())
        elif args.command == "seed":
            result = seed()
        else:
            if not 1 <= args.limit <= 1000 or not 1 <= args.interval <= 60:
                parser.error("limit: 1..1000; interval: 1..60 секунд")
            while True:
                result = {"expired": Inventory().sweep(args.limit)}
                print(json.dumps(result), flush=True)
                if not args.watch:
                    return 0
                time.sleep(args.interval)
        print(json.dumps(normalized(result), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 0
    except Problem as error:
        print(f"Ошибка [{error.code}]: {error.detail}", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(f"Ошибка конфигурации/миграций: {error}", file=sys.stderr)
        return 2
    except psycopg.Error:
        print("Ошибка PostgreSQL. Проверьте подключение, права роли и текущие миграции. Секреты не выводятся.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
