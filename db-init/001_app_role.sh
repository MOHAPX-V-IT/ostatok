#!/usr/bin/env bash
set -euo pipefail
# The API never connects as the cluster superuser. This runs only on a fresh volume.
psql --username "$POSTGRES_USER" --dbname postgres --set=ON_ERROR_STOP=1 --set=app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE ostatok_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE ostatok OWNER ostatok_app;
CREATE DATABASE ostatok_test OWNER ostatok_app;
SQL
