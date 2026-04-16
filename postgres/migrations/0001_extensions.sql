-- Postgres extensions required by the PA-RAG stack.
-- Run once on a fresh database. Idempotent.

CREATE EXTENSION IF NOT EXISTS vector;          -- pgvector (base)
CREATE EXTENSION IF NOT EXISTS vectorscale;     -- pgvectorscale (StreamingDiskANN)
CREATE EXTENSION IF NOT EXISTS pg_trgm;         -- trigram for fuzzy text
CREATE EXTENSION IF NOT EXISTS btree_gin;       -- composite GIN indexes
CREATE EXTENSION IF NOT EXISTS unaccent;        -- accent-insensitive search

-- Supabase provides these by default if using their image:
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
