#!/usr/bin/env bash
# Runs once, on first initialisation of the Postgres volume.
# Creates one database + one owning role per service (database-per-service).
set -euo pipefail

create_service_db() {
  local db="$1" user="$2" password="$3"
  echo "Creating database '${db}' owned by role '${user}'"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
       -v db="$db" -v user="$user" -v password="$password" <<-'EOSQL'
	CREATE ROLE :"user" LOGIN PASSWORD :'password';
	CREATE DATABASE :"db" OWNER :"user";
	REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
EOSQL
}

create_service_db "$COMMERCE_DB_NAME"    "$COMMERCE_DB_USER"    "$COMMERCE_DB_PASSWORD"
create_service_db "$CHECKOUT_DB_NAME"    "$CHECKOUT_DB_USER"    "$CHECKOUT_DB_PASSWORD"
create_service_db "$FULFILLMENT_DB_NAME" "$FULFILLMENT_DB_USER" "$FULFILLMENT_DB_PASSWORD"
create_service_db "$AGENT_DB_NAME"       "$AGENT_DB_USER"       "$AGENT_DB_PASSWORD"

# Catalog search extensions (superuser required for pgvector).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$COMMERCE_DB_NAME" <<-'EOSQL'
	CREATE EXTENSION IF NOT EXISTS vector;
	CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOSQL
