#!/bin/bash
# First start only: create the application role and database with the extensions the schema needs.
set -e
psql -v ON_ERROR_STOP=1 -U postgres <<SQL
CREATE ROLE inventory LOGIN PASSWORD '${INVENTORY_DB_PASSWORD}';
CREATE DATABASE inventory OWNER inventory;
\c inventory
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
