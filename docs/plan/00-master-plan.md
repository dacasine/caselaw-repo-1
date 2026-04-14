# Plan de migration — OpenCaseLaw fork → PA-RAG sur Supabase

> Plan-maître. Sous-plans détaillés dans `phase-1-*.md` … `phase-9-*.md`.
> Rapport source : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`.

## Contexte repo (état actuel)

- MCP monolithique : `mcp_server.py` (538 KB, 11 500 lignes, 23 tools)
- REST : FastAPI `web_api/main.py`, port 8910, ~30 routes
- SQLite :
  - `~/.swiss-caselaw/decisions.db` (~58 GB, FTS5, 965 k+ décisions, schéma dans `db_schema.py`)
  - `~/.swiss-caselaw/reference_graph.db` (~3.5 GB, 8.84 M edges citation + 11.34 M liens décision→statut)
  - `~/.swiss-caselaw/statutes.db` (~42 MB, fedlex ~5 500 lois)
  - `~/.swiss-caselaw/cantonal_laws.db` (~26 000 actes cantonaux)
  - `~/.swiss-caselaw/materialien.db` (Botschaften + débats)
- Chunking actuel : `search_stack/chunker.py` (3 chunks × 500 chars = 1 500 chars/arrêt — insuffisant pour des ATF de 10-80 k chars)
- Embeddings actuels : BGE-M3 (1024-dim) via sqlite-vec (`search_stack/build_vectors.py`)
- Citation graph : `search_stack/build_reference_graph.py`
- Dataset HF : `export_parquet.py` (1 Parquet par cour)
- Scrapers : 29 (fedlex, lexfind_cantonal, bger, cantonaux, régulateurs, etc.)
- Word add-in : TypeScript/Office.js dans `tools/word-addin/`

## Stack cible

- **DB** : Supabase self-hosted (pgvector, pgvectorscale, pg_trgm, ParadeDB BM25 ou tsvector fallback)
- **Chunking** : SAC (Summary-Augmented Chunking) par considérant, 400-512 tokens, overlap 15 %
- **Embeddings** : `joelito/legal-swiss-longformer-base` (768-dim, long-context)
- **MCP** : 23 Edge Functions Deno/TS + bridge MCP stdio pour compat clients
- **LLM enrichissement** : synthetic.new (GLM-5.x Reasoning, Qwen3-Thinking)
- **Retrieval** : hybride BM25 + ANN (pgvectorscale StreamingDiskANN) + RRF + cross-encoder + authority rerank (formule composite PA-RAG)

## Invariants non négociables

1. Parité fonctionnelle MCP (23 tools) et REST (30 routes) à chaque bascule
2. Les 8.84 M edges de citation préservés (checksums pré/post)
3. Word add-in et Claude Desktop fonctionnent sans modification côté client
4. Dataset HuggingFace Parquet schéma inchangé (consommateurs externes)
5. Aucune suppression cascade des citations pendant la migration

## Sort de l'affaire (schéma de classification cible)

- **Recours / appel** : `irrecevabilité | rejet | admission | admission_partielle`
- **Première instance** : `admission | admission_partielle | rejet`

## Décision 2026-04-14 — v1 sur infra actuelle, Supabase reporté en v2

On conserve SQLite + sqlite-vec + `mcp_server.py` Python + FastAPI. PA-RAG construit par-dessus. Migration Supabase différée à un fork v2 une fois la valeur v1 prouvée.

**Phases v1 (dans l'ordre)** :

| # | Phase | Durée | Livrable |
|---|---|---|---|
| 3 | Chunking SAC (Summary-Augmented) | 3 sem | Table `chunks` SQLite, couverture > 95 % |
| 4 | Embeddings Longformer (sqlite-vec) | 1 sem | Index sqlite-vec 768-dim, top-50 < 100 ms |
| 5 | Enrichissement PA-RAG (4 piliers + sort) | 4 sem | ATF + TF enrichis, PageRank temporel |
| 7 | Retrieval hybride + authority rerank | 2 sem | Score composite dans `mcp_server.py` |
| 8 | GraphRAG léger (CTE sur reference_graph.db) | 2 sem | find_appeal_chain, find_leading_cases ++ |
| 9 | Évaluation + observabilité | continu | Benchmark 200 requêtes, comparaison vs baseline |

**Effort v1** : ~10-12 semaines dev full-time.

**Phases v2 différées (référence pour fork futur)** :

| # | Phase | Statut |
|---|---|---|
| 1 | Cadrage + schéma Supabase (parité SQLite) | Sous-plan rédigé, à exécuter en v2 |
| 2 | Migration des données (SQLite → Postgres) | Sous-plan rédigé, à exécuter en v2 |
| 6 | MCP Edge Functions (Deno/TS) | Sous-plan en cours, à exécuter en v2 |

Les contrats MCP/REST gelés et les tests golden (Phase 1 v2) restent utiles dès maintenant — on peut les monter en v1 sans exécuter le reste de Phase 1.

## Risques majeurs

1. Coût LLM ~14 M appels pour SAC complet → priorisation ATF + cache hash
2. Perte de précision FTS5 → tsvector/BM25 à la bascule (benchmark côte à côte obligatoire)
3. λ du time-decayed PageRank à calibrer par grid search
4. Intégrité graphe de citations pendant dual-write
5. Latence Edge Functions vs MCP stdio local (mitigation : bridge en place, cache agressif)
