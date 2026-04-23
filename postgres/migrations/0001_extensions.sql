-- Postgres extensions required by the PA-RAG stack.
-- Run once on a fresh database. Idempotent.

CREATE EXTENSION IF NOT EXISTS vector;          -- pgvector (base)
CREATE EXTENSION IF NOT EXISTS vectorscale;     -- pgvectorscale (StreamingDiskANN)
CREATE EXTENSION IF NOT EXISTS pg_trgm;         -- trigram for fuzzy text
CREATE EXTENSION IF NOT EXISTS btree_gin;       -- composite GIN indexes
CREATE EXTENSION IF NOT EXISTS unaccent;        -- accent-insensitive search

-- Immutable wrapper for unaccent() — required for GENERATED ALWAYS AS columns.
-- The built-in unaccent() is STABLE (depends on dictionary config) but our
-- dictionary never changes at runtime, so an IMMUTABLE shim is safe.
CREATE OR REPLACE FUNCTION immutable_unaccent(text)
RETURNS text AS $$
    SELECT public.unaccent('public.unaccent', $1)
$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE STRICT;

-- Supabase provides these by default if using their image:
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
