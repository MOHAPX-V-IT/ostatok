# 0.1.0-rc.1

Локальный пререлиз API складских остатков и временных резервов на настоящем PostgreSQL.

- SKU, склады, приход, balances и постраничный журнал движений.
- Атомарный bulk reserve до 50 позиций, TTL 1 секунда — 24 часа, confirm/cancel, автоматический и CLI sweeper.
- Уникальные idempotency records по client/endpoint/key, сохранение ответов в одной транзакции с движениями.
- Хешированные reader/operator/admin API keys, bootstrap и защита последнего admin.
- SQL migrations с checksum, Problem JSON, request ID, OpenAPI/Swagger, синтетический повторяемый seed.
- Локальная консоль, Docker/Compose-конфигурация и GitHub Actions с PostgreSQL service.

Нет внешних коммерческих интеграций, дробных единиц, tenant-изоляции каталога, автоматического retention и подтверждённых эксплуатационных SLA. Docker и GitHub CI не считались выполненными локальными проверками.
