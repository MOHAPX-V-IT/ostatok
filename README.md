# Остаток

![Остаток — складской резерв без повторного списания](docs/preview.png)

API складских остатков и временных резервов под заказы. Принимает приход, резервирует несколько позиций целиком, подтверждает списание или возвращает товары в доступный остаток. История документов и движений сохраняется в PostgreSQL.

**0.1.0-rc.1 · Python / FastAPI / psycopg / PostgreSQL 18.** Это небольшой монолит для локальной работы и разбора транзакционных решений. SQLite fallback отсутствует.

## Что можно сделать

- Завести SKU и склады, принять приход, посмотреть доступное, зарезервированное и физическое количество.
- Одним запросом зарезервировать до 50 различных пар товар/склад с TTL от 1 секунды до 24 часов. При нехватке любой позиции весь запрос откатывается.
- Подтвердить или отменить резерв; истёкший резерв нельзя подтвердить. Автоматический sweeper и CLI освобождают его позиции.
- Повторить запрос после потери ответа без повторного движения. Idempotency-Key ограничен клиентом и конкретным endpoint; другой body под тем же зафиксированным ключом даёт 409.
- Разделить reader/operator/admin API-ключи, отозвать ключ, сохранить первого администратора от случайного удаления.
- Читать постраничные списки остатков, резервов и движений, работать через консоль или Swagger.

`on_hand = available + reserved`. Резерв переносит количество из available в reserved; confirm уменьшает reserved и физический остаток; cancel/expiry возвращают количество в available. Все значения — целые единицы.

## Запуск с существующим PostgreSQL

Нужны Python 3.12+ и настоящий PostgreSQL 18. Создайте отдельную БД, владельцем которой является выделенная non-superuser роль. `DATABASE_URL` должен ссылаться на неё. Тестам нужна **другая** БД с именем, заканчивающимся на `_test`.

Из корня репозитория, PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock

$env:DATABASE_URL = 'postgresql://USER:PASSWORD@127.0.0.1:55432/ostatok'
python -m ostatok.cli migrate
python -m ostatok.cli bootstrap --name local-admin
python -m ostatok.cli seed

python -m uvicorn ostatok.app:app --host 127.0.0.1 --port 4511 --limit-concurrency 100
```

Замените USER/PASSWORD своими значениями; специальные символы в URL требуют percent encoding. `.env.example` — образец параметров, локальный Python не загружает `.env` автоматически. Миграции запускаются явно: сервер проверяет их актуальность и не подменяет отсутствующую БД локальным хранилищем.

Bootstrap печатает JSON с новым `secret` **один раз**, а в БД записывает только SHA-256. Сохраните секрет в защищённом месте и введите его в консоль. Повторный bootstrap при существующем активном admin завершается ошибкой; новые ключи выпускаются через admin API. В репозитории нет стандартного токена или общей учётной записи.

- [Консоль](http://127.0.0.1:4511/) — локальный интерфейс реального API.
- [Swagger](http://127.0.0.1:4511/docs) — интерактивные схемы запросов/ответов; Authorize принимает X-API-Key.
- [OpenAPI JSON](http://127.0.0.1:4511/openapi.json), [health](http://127.0.0.1:4511/health).

Остановка — Ctrl+C. Данные остаются в PostgreSQL; приложение не хранит остатки в своей памяти или JSON-файле.

## Вариант Docker Compose

Локально Docker не использовался. Конфигурация подготовлена для машины с Docker Compose:

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_hex(24)); print(secrets.token_hex(24))"
```

Запишите два сгенерированных hex-пароля в `.env`: `POSTGRES_PASSWORD` для cluster admin и `APP_DB_PASSWORD` для выделенной app-role. Пустые значения не допускаются, `.env` исключён из Git. Hex избегает неоднозначных символов в connection URL.

```powershell
docker compose up -d db
docker compose build api
docker compose run --rm api python -m ostatok.cli migrate
docker compose run --rm api python -m ostatok.cli bootstrap --name local-admin
docker compose run --rm api python -m ostatok.cli seed
docker compose up -d api
```

PostgreSQL публикуется только на `127.0.0.1:55432`, API — на `127.0.0.1:4511`. Эти порты должны быть свободны; не запускайте одновременно этот вариант и серверы с теми же портами. API-контейнер работает как UID/GID 10001 и имеет healthcheck.

На новом volume init-script создаёт роль `ostatok_app` без superuser/createdb/createrole и базы `ostatok`, `ostatok_test`. API подключается этой ролью. PostgreSQL 18 использует volume `/var/lib/postgresql`; существующий volume не переинициализируется при смене переменных окружения. Не удаляйте его ради обычного перезапуска.

## Демо: резерв, повтор, подтверждение и TTL

Seed создаёт только синтетические `DEMO-NOTE-A5` (20 единиц), `DEMO-PEN-BL` (5) и склад `DEMO-WH`. Повторный seed не восстанавливает потраченные количества и не сбрасывает историю.

1. Запустите приложение, подключите консоль выпущенным admin-ключом. На новой демобазе найдите два SKU и их реальные остатки.
2. Создайте резерв заказа `DEMO-ORDER-001` на 2 блокнота и 1 ручку, TTL 900 секунд. Available уменьшится, reserved увеличится; физическое количество не изменится.
3. Повторите тот же API-запрос с тем же Idempotency-Key: получите тот же ID, без нового списания. Измените количество, сохранив ключ: получите Problem JSON 409.
4. Подтвердите резерв. Зарезервированные позиции уйдут в списание; повторный confirm не создаст движения второй раз. Посмотрите документ и журнал.
5. Создайте другой резерв с коротким TTL. После срока обновите список: sweeper переведёт его в expired и освободит количество. Поздний confirm вернёт 409.
6. Попробуйте bulk reserve, где одной позиции недостаточно. Другие позиции также останутся без изменений. Не удаляйте этот документ из истории: неуспешный запрос вообще не создаёт резерв.

Для API-клиента тело reserve выглядит так; подставьте UUID из GET каталога:

```json
{
  "order_ref": "DEMO-ORDER-001",
  "ttl_seconds": 900,
  "items": [
    {
      "product_id": "UUID-ТОВАРА",
      "warehouse_id": "UUID-СКЛАДА",
      "quantity": 2
    }
  ]
}
```

Передайте `X-API-Key: ost_...` и новый `Idempotency-Key` (1–128 печатных ASCII-символов без пробелов). Confirm/cancel принимают необязательный `{ "note": "Причина или отметка" }`, для них также нужен Idempotency-Key.

## Контракт и ошибки

| Группа                 | Endpoint                                                                    |
| ---------------------- | --------------------------------------------------------------------------- |
| Кто подключён          | `GET /api/me`                                                               |
| Каталог                | `GET/POST /api/products`, `GET/POST /api/warehouses`                        |
| Остатки и приход       | `GET /api/balances`, `POST /api/receipts`                                   |
| Резервы                | `GET/POST /api/reservations`, `GET /api/reservations/{id}`                  |
| Переходы               | `POST /api/reservations/{id}/confirm`, `POST /api/reservations/{id}/cancel` |
| История                | `GET /api/movements`                                                        |
| Ключи admin            | `GET/POST /api/keys`, `DELETE /api/keys/{id}`                               |
| Освободить TTL вручную | `POST /api/maintenance/expire`                                              |

Списки возвращают `{items,total,limit,offset}`. Limit — 1–100, по умолчанию 50; фильтры описаны в [контракте консоли](docs/contract.json) и Swagger. Даты — UTC ISO 8601. Ответ содержит X-Request-ID, ошибки — тот же request_id и `application/problem+json`.

| Статус    | Причина                                                                  |
| --------- | ------------------------------------------------------------------------ |
| 400       | Отсутствует/неверен Idempotency-Key                                      |
| 401 / 403 | Ключ отсутствует/отозван либо недостаточно роли/прав на резерв           |
| 404       | Товар, склад, резерв или ключ не найден                                  |
| 409       | Нехватка остатка, истечение TTL, недопустимый переход или конфликт ключа |
| 413 / 422 | Body больше 1 MiB либо ошибка структуры/границ                           |
| 503       | PostgreSQL или locks временно недоступны                                 |

Reader читает; operator также меняет каталог и запасы; admin управляет ключами. Reader/operator видят резервы своего client_id, admin — все. Каталог и журнал общие для доверенной команды, это не полная tenant-изоляция. Ключи можно намеренно выпустить для одного client_id.

Idempotency replay возвращает **исходный** сохранённый ответ. За текущим статусом уже закрытого резерва обращайтесь к GET. Ошибки валидации и нехватки остатка откатывают запись ключа; confirm, обнаруживший expiry, фиксирует освобождение и ответ 409 вместе. Порядок JSON-полей незначим, порядок элементов массива значим.

## Sweeper и эксплуатационные границы

В приложении sweeper работает каждые 5 секунд; интервал задаётся `EXPIRY_INTERVAL_SECONDS` (1–60). Для отдельного процесса:

```powershell
python -m ostatok.cli sweep --limit 100
python -m ostatok.cli sweep --watch --interval 5 --limit 100
```

При остановленном API резервы освобождает этот CLI. До прохода sweeper истёкший резерв может удерживать available, но подтвердить его нельзя. SKU/склады/резервы и movements не удаляются через API. Это сохраняет историю, однако retention пока нет; таблицы со временем растут.

Нет дробных единиц, перемещений между складами, возвратов отгрузки, внешней ERP/оплаты и эксплуатационных SLA. Временная точка решения о TTL определяется PostgreSQL после ожидания locks. Для внешнего доступа нужны TLS, управление сетью и секретами; UI и Swagger сами по себе не заменяют такой контур.

Миграции проверяют checksum применённых SQL; меняйте схему новым файлом. Для backup используйте штатные инструменты PostgreSQL и проверяйте восстановление. Скопировать каталог Python недостаточно: данные находятся в БД.

## Проверки

Тесты требуют отдельного PostgreSQL: имя TEST_DATABASE_URL должно оканчиваться на `_test` и отличаться от app URL. Fixtures пересоздают schema public **только в этой тестовой БД**. Без неё проверки завершаются ошибкой, а не переключаются на SQLite.

```powershell
$env:TEST_DATABASE_URL = 'postgresql://USER:PASSWORD@127.0.0.1:55432/ostatok_test'
python -m pip check
python -m compileall -q ostatok tests
python -m pytest -q
```

Проверяются реальные HTTP-запросы и независимые PostgreSQL-соединения: 20 конкурентных резервов без oversell, атомарность bulk и порядок locks, scoped replay/409, confirm/cancel, expiry race, ожидание locks через границу TTL, роли, pagination, миграции и повторяемый seed. Реальные результаты — в [VERIFICATION.md](VERIFICATION.md). Docker и GitHub CI не заявлены выполненными.

## Внутри репозитория

- [Архитектура, схема Mermaid, компромиссы и вопросы для интервью](ARCHITECTURE.md)
- [Контракт консоли](docs/contract.json), [проверки](VERIFICATION.md), [выпуск](RELEASE.md)
- [Транзакции и ledger](ostatok/service.py), [HTTP-граница](ostatok/app.py), [SQL-миграции](migrations/001_inventory.sql)
- [Locked зависимости](requirements.lock), [CI с PostgreSQL](.github/workflows/checks.yml), [Dockerfile](Dockerfile)

## Лицензия

[MIT](LICENSE).

## Реальный интерфейс

![Рабочая консоль Остаток](docs/interface.png)

Снимок сделан с работающим API и PostgreSQL на синтетических данных. [Проверенные сценарии и ограничения](VERIFICATION.md). [Происхождение визуальных решений](DESIGN_PROVENANCE.md).
