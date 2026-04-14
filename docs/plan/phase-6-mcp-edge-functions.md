# Phase 6 — MCP Edge Functions Supabase + REST API + Word add-in

> Sous-plan détaillé de la Phase 6 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Durée cible : 3 semaines.
> Livrable : bascule production du MCP monolithique Python `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` (538 KB, ~13 000 lignes, 23 tools) vers 23 Edge Functions Deno/TypeScript hébergées par Supabase, avec préservation stricte de la compatibilité des clients (Claude Desktop via MCP stdio, Word add-in Office.js via REST, navigateur via `web_api/main.py`).
> Dépendances amont : Phases 1 à 5 terminées (schéma Supabase gelé, données migrées, chunks SAC produits, embeddings Longformer indexés, enrichissement PA-RAG appliqué sur ATF + TF).
> Dépendance aval : Phase 7 (retrieval hybride + authority rerank) consomme ces Edge Functions comme couche d'exécution.

---

## 1. Objectifs et critères de succès

### 1.1 Objectifs fonctionnels

1. **Substitution sans régression** : chaque tool MCP et chaque route REST existante conserve sa signature (nom, paramètres, types de retour, ordre des champs documenté) pour préserver les clients tiers connus (Claude Desktop, VS Code MCP clients, Word add-in, scripts internes).
2. **Déplacement de la logique métier vers Supabase Edge Functions (Deno runtime)** : le couplage fort actuel au fichier SQLite `~/.swiss-caselaw/decisions.db` disparaît au profit de connexions Postgres via le connection pooler Supavisor, avec pool partagé par le module `_shared/db.ts`.
3. **Préserver la compatibilité stdio** via un bridge local « MCP ↔ HTTPS » qui traduit les appels du protocole MCP vers des `POST` HTTPS authentifiés vers Supabase Edge Functions, sans toucher aux clients.
4. **Éliminer le monolithe** : `mcp_server.py` passe en mode archivé et en gel après 6 semaines de cohabitation sans régression. Jusqu'à suppression effective il reste exécutable localement pour debug uniquement.
5. **Découper le risque** : aucune bascule tout-ou-rien. Canary par tool, feature flag côté bridge, rollback unitaire en cas de régression.

### 1.2 Critères d'acceptation techniques

| Critère | Cible | Mesure |
|---|---|---|
| Parité golden (Phase 1 test vectors) | 100 % | Script de diff JSON entre réponse MCP Python et Edge Function pour les 23 tools sur les 200 requêtes golden |
| Latence p95 par tool | ≤ latence actuelle + 20 % max pour les 15 tools en lecture simple, ≤ 40 % pour les 8 tools composites (search_decisions, draft_mock_decision, get_doctrine, analyze_legal_trend…) | Traces Supabase + métriques p50/p95/p99 par fonction |
| Cold-start Edge Function | < 700 ms p95 sur les 5 tools critiques (search_decisions, get_decision, find_citations, get_statistics, get_law) | Test de cold-start dédié (kill du worker) |
| Compatibilité Claude Desktop | 100 % des 23 tools invocables | Pack de tests MCP stdio golden |
| Compatibilité Word add-in | 100 % des 6 flows add-in (recherche, citation, vérification, brief, règle, export) | Suite Playwright existante dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/tests` |
| REST API externe | 100 % des 30 routes opérantes | Re-run des tests FastAPI existants |
| Couverture de logs structurés | 100 % des invocations | Supabase Logs + tag `tool=<name>` |
| Erreurs 5xx en prod durant canary | < 0,5 % sur 7 jours glissants par tool | Alerting Supabase |

### 1.3 Hors périmètre (explicite)

- Pas de refonte de la logique de retrieval : c'est la Phase 7.
- Pas de refonte du GraphRAG : c'est la Phase 8.
- Pas de refonte du Word add-in fonctionnellement ; uniquement validation de compat et ajustement de l'URL base.
- Pas de changement du protocole MCP exposé (les outils gardent leurs noms exacts).
- Pas de migration du provider LLM (OpenAI/Anthropic/Gemini/Ollama restent configurables via `/settings/keys`).

---

## 2. Analyse du monolithe actuel

### 2.1 Métriques brutes

- Fichier : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py`
- Taille : 538 KB, 13 068 lignes (mesure actuelle, supérieure au chiffre 11 500 du plan-maître — le plan-maître sera mis à jour en Phase 1).
- Nombre de fonctions privées et utilitaires recensées : ~180 (voir mapping section 4).
- Points d'entrée protocolaires : `@server.list_tools()` (ligne 11410) et `@server.call_tool()` (ligne 11429). Un dispatcher central `call_tool` route les 23 tools vers des fonctions `async def _tool_<name>`.

### 2.2 Patterns répétés identifiés

1. **Connexions SQLite multi-fichiers** : helpers `get_db()`, `_get_graph_conn()`, `_get_vec_conn()`, `_get_statutes_conn()`, `_get_cantonal_conn()`, `_get_ok_conn()`, `_get_materialien_conn()`, `_get_anwaltsrecht_conn()`. Chacun ouvre un `sqlite3.Connection` vers `~/.swiss-caselaw/*.db` en mode `immutable=1`. À remplacer par un unique client Postgres pointé sur les tables correspondantes (Phase 1/2 a déjà préparé le schéma unifié).
2. **Cache in-process** : `_cache_get`, `_cache_set`, `_cache_clear` — dictionnaire local + TTL. À remplacer par un cache Edge par-fonction (map en mémoire du worker) + optionnel Supabase KV ou Upstash externe pour cache partagé de second niveau (hors périmètre Phase 6, cache local suffit).
3. **FTS5 SQLite** : `_search_fts5_inner`, `_sanitize_fts5`, `_build_query_strategies`, `_build_nl_or_query`, `_build_nl_and_query`, `_build_anchor_pair_strategies`, `_build_language_focus_query` — toute la combinatoire de construction de requête FTS5 est propre à SQLite. Remplacement par ParadeDB BM25 ou `tsvector` en Phase 7 ; en Phase 6 on conserve un pont de fallback (voir 2.4).
4. **Normalisation textuelle** : `_normalize_token_for_fts`, `_normalize_token_for_match`, `_collapse_umlaut_variants`, `_normalize_docket`, `_normalize_statute_law_code`. Logique de normalisation allemand/français/italien à porter fidèlement en TS (testable unitairement).
5. **Docket parsing et variants** : `_parse_docket_family`, `_build_docket_variants`, `_extract_docket_serial`, `_collapse_spaced_docket`, `_looks_like_docket_query`. Fonctions pures, portage direct.
6. **Cross-encoder rerank** : `_get_cross_encoder`, `_apply_cross_encoder_boosts`, `_apply_llm_rerank`, `_rerank_rows`. Appels à un modèle local (sentence-transformers) ou distant (LLM provider). En Deno on appelle via HTTPS un endpoint modèle (soit self-hosted, soit synthetic.new, soit OpenAI/Anthropic en passthrough).
7. **Graph queries** : `_find_outgoing_citations`, `_find_incoming_citations`, `_find_appeal_chain`, `_walk_chain`, `_search_statute_graph`, `_search_graph_decisions_for_statutes`, `_count_citations`. À ré-exprimer en SQL Postgres (CTE récursifs préparés en Phase 8) ; pour Phase 6, équivalents en SQL direct ou vues matérialisées.
8. **Enrichissement LLM** : `_expand_query_with_llm`, `_apply_llm_rerank`, `_extract_legal_query_from_facts`, `_retrieve_case_law_for_facts`. Tous passent par un provider abstrait. En Edge Function, utiliser un module `_shared/llm.ts` qui lit `/settings/keys` (via table Postgres `user_settings` ou variables d'environnement Edge).
9. **Métriques** : `_record_tool_call`, `_record_query`, `_init_metrics_db`, `_flush_metrics_to_disk`, `_get_lifetime_metrics`, `_record_zero_result`. Persistance actuelle dans SQLite local. À remplacer par insertion dans table Postgres `tool_metrics` + export vers Supabase Logs (tracing structuré natif).

### 2.3 Couplage à l'environnement local

Le monolithe suppose :

- Filesystem POSIX avec 5 fichiers SQLite lisibles en `immutable=1`.
- Modèle sentence-transformers cacheable en RAM (BGE-M3, ~2 Go).
- Accès internet sortant pour les providers LLM (OpenAI/Anthropic/Gemini) ou Ollama local.
- Process long-lived (state caché en mémoire, `_cache`, métriques, modèle préchargé).

Edge Functions sont **éphémères, stateless, sans filesystem persistant et sans GPU**. Conséquences :

- Modèle local d'embedding et cross-encoder impossible in-function. Solution : Phase 4 a déjà externalisé l'embedding vers pgvectorscale (index DiskANN) + MVP d'un micro-service « encoder » sur Fly.io ou synthetic.new pour générer l'embedding de requête au besoin.
- Pas de cache inter-invocations fiable : chaque worker a son propre heap, pas de garantie de persistance. Le cache devient best-effort. Pour des résultats stables, on s'appuie sur les index Postgres et la mémoïsation applicative (cache de requête par hash, TTL court).
- Pas de SQLite. Tout le contenu est déjà en Postgres depuis la Phase 2 ; il ne reste qu'à réécrire les `SELECT` en SQL Postgres (syntaxe quasi identique, sauf FTS5 → tsvector/ParadeDB).

### 2.4 Extractabilité des 23 tools

Classement par difficulté de portage (ordre proposé de migration canary, du plus simple au plus complexe) :

1. **Triviaux, lecture directe d'une table** : `list_courts`, `get_statistics`, `get_decision`, `get_law`, `check_update_status`, `update_database` (no-op).
2. **Lecture + jointure** : `find_citations`, `find_appeal_chain`, `get_case_brief` (concaténation de champs), `get_commentary`, `get_materialien`, `get_legislation`, `browse_legislation_changes`.
3. **Recherche simple** : `search_laws`, `search_commentaries`, `search_materialien`, `search_legislation` — équivalents `ILIKE` + `tsvector`.
4. **Recherche complexe avec rerank** : `search_decisions` (le plus gros), `find_leading_cases`, `analyze_legal_trend`.
5. **Composés LLM** : `draft_mock_decision`, `get_doctrine`, `generate_exam_question`. Dépendent du provider, d'un premier retrieval, d'un prompt composite, et d'une agrégation.

Cet ordre guide la stratégie canary (section 11).

---

## 3. Architecture cible Edge Functions

### 3.1 Arborescence

Répertoire racine des fonctions : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/supabase/functions/` (à créer). Conventions Supabase CLI :

```
supabase/
  functions/
    _shared/
      db.ts                  # pool Postgres (Supavisor), helpers SQL paramétrés
      parag.ts               # normalisation, docket parsing, umlaut collapse
      auth.ts                # vérif JWT, service role, rate limiting
      types.ts               # types générés + alias métier
      llm.ts                 # abstraction provider (OpenAI, Anthropic, Gemini, Ollama, synthetic.new)
      logger.ts              # logs structurés (JSON lines) + trace id
      metrics.ts             # helpers d'insertion dans tool_metrics
      fts.ts                 # wrappers tsvector / ParadeDB BM25
      rerank.ts              # appel cross-encoder externe + fallback heuristique
      graph.ts               # CTE de citation / appeal chain (préfigure Phase 8)
      cache.ts               # mémoire locale LRU + hash de requête
      errors.ts              # taxonomie d'erreurs + mapping HTTP
    search_decisions/index.ts
    get_decision/index.ts
    list_courts/index.ts
    get_statistics/index.ts
    find_citations/index.ts
    find_appeal_chain/index.ts
    find_leading_cases/index.ts
    analyze_legal_trend/index.ts
    draft_mock_decision/index.ts
    get_case_brief/index.ts
    get_doctrine/index.ts
    generate_exam_question/index.ts
    get_law/index.ts
    search_laws/index.ts
    get_commentary/index.ts
    search_commentaries/index.ts
    get_materialien/index.ts
    search_materialien/index.ts
    search_legislation/index.ts
    get_legislation/index.ts
    browse_legislation_changes/index.ts
    update_database/index.ts
    check_update_status/index.ts
```

Une fonction = un dossier = un `index.ts` exporté en handler Deno.serve standard. Le module `_shared/` est importé par chemin relatif. Le déploiement est piloté par `supabase functions deploy <name>`.

### 3.2 Conventions internes

- **Une fonction = un tool**. Jamais deux tools dans la même fonction ; cela permet canary, rollback, métriques et versioning indépendants.
- **Idempotence** : toutes les fonctions de lecture sont idempotentes et supportent la réémission ; `draft_mock_decision` et `generate_exam_question` déposent un enregistrement dans `generation_log` pour rejouabilité avec cache de sortie par hash d'entrée.
- **Entrée** : JSON body strictement validé par un schéma Zod défini dans `_shared/types.ts`. Validation 400 immédiate si dérive du contrat.
- **Sortie** : JSON dont le schéma est gelé par tool et versionné dans `_shared/types.ts`. Champs additifs uniquement ; ajout de champ permis, rename ou suppression interdits sans bump de version protocolaire (`X-Tool-Version` header).
- **Types Postgres générés** : `supabase gen types typescript` produit `_shared/database.generated.ts` depuis le schéma Phase 1. Les tools typent leurs accès via ces types.
- **Pas d'état global mutable partagé entre invocations**. Caches explicitement annotés « best-effort ».
- **Langue des logs** : anglais technique. Messages utilisateur localisés par i18n du client (hors scope fonction).

### 3.3 Module `_shared/` — responsabilités

| Module | Rôle | Source Python équivalente |
|---|---|---|
| `db.ts` | Ouverture pool Postgres via `postgres` deno driver, helpers `query<T>()`, `queryOne<T>()`, `queryMany<T>()`, transactions | `get_db`, `_get_graph_conn`, etc. (unification) |
| `parag.ts` | Parsing docket, normalisation texte multilingue, collapse umlaut, dédup par canonical key | `_normalize_docket`, `_collapse_umlaut_variants`, `_make_canonical_key`, `_parse_docket_family`, `_build_docket_variants` |
| `auth.ts` | Vérification JWT, extraction user id, check de role `service_role` pour tools sensibles, rate limit bucket par user/tool | (absent, ajouté) |
| `types.ts` | Types d'entrée/sortie par tool + types métier partagés (`DecisionRow`, `CitationEdge`, `StatuteRef`) | `db_schema.py` |
| `llm.ts` | Façade providers. Méthodes `complete()`, `embed()`, `rerank()`. Choix du provider par requête via header ou config utilisateur | `_expand_query_with_llm`, `_apply_llm_rerank`, `_extract_legal_query_from_facts` |
| `logger.ts` | `log.info/warn/error` JSON lines avec `tool`, `request_id`, `user_id`, `duration_ms`, `db_time_ms`, `llm_time_ms` | `_record_tool_call`, `_record_query`, `_log_search_trace` |
| `metrics.ts` | Insertion asynchrone dans table `tool_metrics` (buffered, flush à la fin du handler) | `_flush_metrics_to_disk`, `_get_lifetime_metrics` |
| `fts.ts` | Construction requête ParadeDB BM25 ou tsvector selon env flag `FTS_BACKEND`. Stratégies `NL_OR`, `NL_AND`, `ANCHOR_PAIR`, `LANGUAGE_FOCUS`. | `_build_query_strategies` et dérivés |
| `rerank.ts` | Appel cross-encoder (endpoint HTTP externe sur Fly.io ou synthetic.new) + fallback sur heuristique léger (term coverage + recency + authority placeholder) | `_apply_cross_encoder_boosts`, `_rerank_rows` |
| `graph.ts` | Requêtes CTE citations (`graph_decision_cites`) et chaîne d'appels (`find_appeal_chain`) | `_find_outgoing_citations`, `_find_incoming_citations`, `_walk_chain` |
| `cache.ts` | LRU local (max ~256 entrées) avec TTL 60 s par défaut, clé = hash SHA-256 des args normalisés | `_cache_get`, `_cache_set`, `_cache_clear` |
| `errors.ts` | Exceptions : `BadInputError` → 400, `NotFound` → 404, `Upstream` → 502, `RateLimited` → 429, etc. | (dispersé) |

### 3.4 Génération de types depuis le schéma

Pipeline de build :

1. `supabase gen types typescript --local > _shared/database.generated.ts`.
2. Diff automatisé en CI : si le schéma SQL Phase 1 évolue, le fichier généré bouge et un check bloque les merges sans regen.
3. Les types métier (`DecisionRow`, `Chunk`, `CitationEdge`) sont des alias depuis `database.generated.ts` + extensions dans `_shared/types.ts` (fields dérivés uniquement).

### 3.5 Connexion Postgres

- Via le pool Supavisor (transaction mode) en production pour absorber le fan-out fonctions sans épuiser les connexions Postgres.
- Chaîne de connexion lue depuis les secrets Edge `SUPABASE_DB_URL` (injectée par la plateforme).
- RLS activée sur les tables utilisateur (`user_settings`, `sessions`, `chat_messages`). Tools lisant des données publiques (décisions, lois, commentaires) utilisent le rôle `anon` ou un rôle applicatif dédié `mcp_reader` en lecture seule. Les tools d'écriture (sessions, metrics) utilisent un rôle `mcp_writer` avec INSERT/UPDATE limités.

### 3.6 Runtime et versions

- Deno 1.46+ pilotée par Supabase Edge Runtime.
- TypeScript strict, `noImplicitAny`, `exactOptionalPropertyTypes`.
- Deps tierces minimales : `postgres` (driver), `zod` (validation), `jose` (JWT), `oak` inutile (Deno.serve suffit).
- Aucune dépendance binaire native (exclut `onnxruntime`, `sentence-transformers`, `tiktoken` natif). Les calculs lourds (embeddings, cross-encoder) sont externalisés.

---

## 4. Mapping détaillé des 23 tools

Chaque sous-section décrit l'entrée gelée (en se basant sur `mcp_server.py`), le retour, la dépendance DB, la dépendance LLM et l'éventuelle exposition aux gains PA-RAG. Les signatures exactes seront extraites en Phase 1 dans `contracts/tools.json` et sont ici résumées.

### 4.1 `search_decisions`

- **Entrées** (gelées Phase 1) : `query: string`, `limit?: int`, `offset?: int`, `court?: string[]`, `language?: 'fr'|'de'|'it'`, `year_from?: int`, `year_to?: int`, `legal_area?: string[]`, `decision_type?: string[]`, `rerank?: boolean`, `include_snippets?: boolean`, `strategy_hint?: string`.
- **Sortie** : `{ total, results: DecisionRow[], strategies_tried, query_expansions, rerank_applied, latency }`.
- **Dépendances DB** : tables `decisions`, `chunks`, `chunk_embeddings` (pgvector), `statute_refs`, `citation_edges`, `fts_index` (tsvector ou ParadeDB). Vues matérialisées `mv_leading_scores` (préparée Phase 5).
- **Dépendance LLM** : optionnelle, `_expand_query_with_llm` → `_shared/llm.ts.complete()` pour 1–3 reformulations ; rerank via cross-encoder externe.
- **Enrichissement PA-RAG** : oui. Consomme `authority_score`, `time_decay`, `sort_de_laffaire`, `citations_in`, `citations_out`. La formule composite est appliquée en Phase 7 ; en Phase 6 on réplique la logique existante, pas plus.
- **Ordre de canary** : n°4 (après 3 tools triviaux passés sans incident).

### 4.2 `get_decision`

- Entrée : `decision_id: string` (canonique `court/docket/date` ou alias).
- Sortie : objet `DecisionRow` complet (métadonnées + texte + passages + statutes + parties + sort).
- DB : `decisions`, `passages`, `statute_refs`, `parties`. Résolution d'alias via `_resolve_decision_id` → SQL indexé.
- LLM : aucun.
- PA-RAG : expose `authority_score`, `sort_de_laffaire`, `leading_rank` si Phase 5 appliquée.
- Canary : n°2.

### 4.3 `list_courts`

- Entrée : `language?: string`.
- Sortie : `{ courts: [{ code, display_name, level, jurisdiction, decisions_count }] }`.
- DB : `courts` (table de référence) ou agrégation `decisions` GROUP BY.
- LLM : aucun. Canary : n°1.

### 4.4 `get_statistics`

- Entrée : `scope?: 'global'|'court'|'period'`, filtres optionnels.
- Sortie : counts agrégés par cour / année / langue / area.
- DB : vues matérialisées `mv_stats_*` (à créer Phase 2 si absentes).
- LLM : aucun. Canary : n°3.

### 4.5 `find_citations`

- Entrée : `decision_id`, `direction: 'in'|'out'|'both'`, `depth: 1..3`, `limit`.
- Sortie : `{ incoming, outgoing, pagerank_rank }`.
- DB : table `citation_edges` (8,84 M edges). CTE récursif avec limite de profondeur.
- LLM : aucun. Canary : n°5.

### 4.6 `find_appeal_chain`

- Entrée : `decision_id`.
- Sortie : chaîne amont/aval (première instance → TF → ATF) avec décisions liées.
- DB : `appeal_edges` (sous-ensemble de `citation_edges` marqué `type='appeal'`).
- LLM : aucun. Canary : n°6.

### 4.7 `find_leading_cases`

- Entrée : `query: string`, `legal_area?`, `court?`, `limit`.
- Sortie : top-N décisions ATF/leading par score d'autorité.
- DB : `decisions` + `mv_leading_scores` + `citation_edges` pour contextuels. Requête hybride BM25 + ANN.
- LLM : optionnel (expansion requête).
- PA-RAG : **fort**. Utilise `leading_rank`, `time_decayed_pagerank`, `authority_score`.
- Canary : n°10 (après stabilisation de `search_decisions`).

### 4.8 `analyze_legal_trend`

- Entrée : `topic: string`, `time_window: {from, to}`, `granularity: 'year'|'quarter'`.
- Sortie : série temporelle de décisions + jurisprudence évolutive.
- DB : `decisions`, `citation_edges`, agrégations par bucket temporel.
- LLM : synthèse textuelle de la tendance (optionnelle, param `summarize: boolean`).
- Canary : n°12.

### 4.9 `draft_mock_decision`

- Entrée : `facts: string`, `legal_area?`, `court?`, `length?`.
- Sortie : texte de décision fictive + `sources: DecisionRow[]` + `statutes_cited`.
- DB : pipeline multi-étape (`_retrieve_case_law_for_facts`, `_collect_statute_requests`, `_search_graph_decisions_for_statutes`).
- LLM : **obligatoire**. Prompt composite avec contexte RAG. Respect du provider configuré via `/settings/keys`.
- PA-RAG : **fort**. Doit exploiter le retrieval hybride pondéré par autorité.
- Canary : dernier (n°23). Feature-flagged, fallback MCP stdio Python pendant 2 semaines minimum.

### 4.10 `get_case_brief`

- Entrée : `decision_id`, `style?: 'short'|'detailed'`, `language?`.
- Sortie : `{ facts, question, holding, reasoning, sort }`.
- DB : `decisions` + `chunks` (sélection des chunks SAC les plus structurants).
- LLM : obligatoire pour synthèse si non précalculée. Cache de sortie par `decision_id + style + language` fort TTL (7 jours).
- Canary : n°15.

### 4.11 `get_doctrine`

- Entrée : `query`, `legal_area?`, `jurisdiction?`.
- Sortie : synthèse doctrinale multi-sources (commentaires, matérialiens, décisions).
- DB : `commentaries`, `materialien`, `decisions`.
- LLM : **obligatoire**.
- PA-RAG : **oui**. Intégration de l'authority score sur décisions citées.
- Canary : n°20.

### 4.12 `generate_exam_question`

- Entrée : `topic`, `difficulty`, `format: 'case'|'mcq'|'essay'`.
- Sortie : question + corrigé + sources.
- DB : retrieval de décisions pédagogiques (filtre `is_teaching_case`).
- LLM : obligatoire.
- Canary : n°22.

### 4.13 `get_law`

- Entrée : `law_code: string`, `article?`, `as_of?: date`.
- Sortie : texte d'article + versions temporelles.
- DB : `statutes`, `statute_versions`.
- LLM : aucun. Canary : n°7.

### 4.14 `search_laws`

- Entrée : `query`, filters.
- Sortie : articles pertinents.
- DB : `statutes` + tsvector.
- LLM : aucun (optionnel expansion). Canary : n°9.

### 4.15 `get_commentary`

- Entrée : `commentary_id` ou `article + commentary_source`.
- Sortie : texte du commentaire.
- DB : `commentaries`.
- LLM : aucun. Canary : n°11.

### 4.16 `search_commentaries`

- Entrée : `query`, `law_code?`, `author?`.
- Sortie : liste.
- DB : tsvector sur `commentaries`.
- Canary : n°13.

### 4.17 `get_materialien`

- Entrée : `law_code`, `article?`.
- Sortie : Botschaft + débats parlementaires pertinents.
- DB : `materialien`.
- Canary : n°14.

### 4.18 `search_materialien`

- Entrée : `query`, filters.
- Sortie : liste.
- DB : tsvector sur `materialien`.
- Canary : n°16.

### 4.19 `search_legislation`

- Entrée : `query`, `jurisdiction: 'federal'|'cantonal'|'all'`, `date_range?`.
- Sortie : législation fédérale/cantonale.
- DB : `statutes`, `cantonal_laws` unifiées dans une vue `legislation`.
- Canary : n°17.

### 4.20 `get_legislation`

- Entrée : `law_uri` ou `law_code`.
- Sortie : métadonnées loi + structure articles.
- DB : `statutes` + `statute_articles`.
- Canary : n°8.

### 4.21 `browse_legislation_changes`

- Entrée : `since_date`, filters.
- Sortie : diff des versions législatives.
- DB : `statute_versions` + vue `legislation_changelog`.
- Canary : n°18.

### 4.22 `update_database` — **NO-OP client, état serveur**

- Sémantique actuelle (Python) : déclenche un scrape local.
- Sémantique cible : ne lance rien côté client. Retourne `{ status: 'server-managed', last_ingestion_at: <timestamp>, next_scheduled_at: <timestamp>, note: 'Ingestion gérée par les scrapers serveur, cf. pg_cron' }`.
- DB : lecture de `ingestion_runs` (table cron log Phase 2).
- LLM : aucun. Canary : n°19 (simple).

### 4.23 `check_update_status`

- Sémantique cible : identique à `update_database` (alias légitime dans le monolithe), renvoie l'état du dernier cron et progression.
- Canary : n°21.

### 4.24 Récapitulatif

| Tool | DB | LLM | PA-RAG | Canary |
|---|---|---|---|---|
| list_courts | oui | non | non | 1 |
| get_decision | oui | non | passif | 2 |
| get_statistics | oui | non | non | 3 |
| search_decisions | oui | optionnel | **fort** | 4 |
| find_citations | oui | non | non | 5 |
| find_appeal_chain | oui | non | non | 6 |
| get_law | oui | non | non | 7 |
| get_legislation | oui | non | non | 8 |
| search_laws | oui | opt | non | 9 |
| find_leading_cases | oui | opt | **fort** | 10 |
| get_commentary | oui | non | non | 11 |
| analyze_legal_trend | oui | opt | moyen | 12 |
| search_commentaries | oui | non | non | 13 |
| get_materialien | oui | non | non | 14 |
| get_case_brief | oui | **oui** | passif | 15 |
| search_materialien | oui | non | non | 16 |
| search_legislation | oui | non | non | 17 |
| browse_legislation_changes | oui | non | non | 18 |
| update_database | oui | non | non | 19 |
| get_doctrine | oui | **oui** | **fort** | 20 |
| check_update_status | oui | non | non | 21 |
| generate_exam_question | oui | **oui** | moyen | 22 |
| draft_mock_decision | oui | **oui** | **fort** | 23 |

---

## 5. Bridge MCP stdio ↔ HTTPS

### 5.1 Rôle

Les clients MCP existants (Claude Desktop en premier lieu) communiquent en **stdio JSON-RPC** avec un serveur local. Supabase Edge Functions exposent du **HTTPS**. Pour éviter toute modification côté client, un petit process local — le **bridge** — lit sur stdin, reçoit les messages MCP (`initialize`, `tools/list`, `tools/call`, `notifications/*`), et pour chaque `tools/call` effectue un `POST` HTTPS vers l'Edge Function correspondante.

Ce process remplit le rôle de `mcp_server.py` vis-à-vis du client, sans contenir la logique métier. Il devient un adaptateur ~300 lignes maximum.

### 5.2 Emplacement et langage

- Répertoire : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_bridge/` (à créer).
- Implémentation recommandée : Python (stdlib asyncio + httpx) pour maximiser la compat de déploiement existante (l'utilisateur a déjà un venv Python prêt à lancer `mcp_server.py`). Alternative : Deno standalone pour homogénéité, mais ajoute un prérequis d'install pour les utilisateurs.
- Bundle : un seul `bridge.py` exécutable, démarré par `claude_desktop_config.json` avec la même commande `python bridge.py`.

### 5.3 Conception

- **Chargement** : au démarrage, le bridge fait un `GET /functions/v1/list_tools` (endpoint dédié ou fichier statique JSON servi par Supabase Storage) pour récupérer la liste des 23 tools et leur schéma d'entrée Zod → converti en JSON Schema MCP.
- **Dispatch** : sur `tools/call { name, arguments }`, le bridge mappe `name` → `POST https://<project>.functions.supabase.co/<name>` avec `arguments` dans le body, `Authorization: Bearer <anon-key ou user-jwt>`.
- **Streaming** : pour `chat` et éventuellement `draft_mock_decision`, accepter un retour `text/event-stream` (SSE) et relayer en MCP `notifications/progress`.
- **Cache négatif** : si une fonction est désactivée (feature flag), le bridge tombe en fallback sur `mcp_server.py` via invocation locale (option compilation-time) ou renvoie erreur contrôlée.
- **Feature flag par tool** : fichier local `~/.swiss-caselaw/bridge.toml`
  - `[tools] search_decisions = "edge"` ou `"local"` ou `"dual-compare"`.
  - Mode `dual-compare` : exécute l'Edge et le local en parallèle, loggue le diff, renvoie le local. Sert la comparaison golden en prod masquée.
- **Health** : `tools/list` renvoie la liste depuis l'Edge ; si Edge injoignable, fallback sur liste statique bundlée.

### 5.4 Authentification

- Clé par défaut : Supabase `anon` key pour les tools publics (lecture pure).
- Pour les tools LLM payants (`draft_mock_decision`, `get_doctrine`, `generate_exam_question`, `get_case_brief`, `analyze_legal_trend`) : JWT utilisateur obtenu via un flow OAuth léger (Supabase Auth) ou clé API personnelle générée par l'utilisateur dans `/settings/keys`.
- Le bridge stocke la clé dans `~/.swiss-caselaw/bridge.toml` chiffré via OS keychain (macOS Keychain, libsecret sur Linux, DPAPI sur Windows) ou en clair si keychain indisponible avec avertissement.
- Les clés provider LLM (OpenAI, Anthropic, Gemini, Ollama) restent locales dans le bridge si le user les a fournies. Elles sont **transmises** à l'Edge Function via header chiffré `X-Provider-Key-<provider>` au besoin. Alternative : stockage côté serveur via `/settings/keys` → table `user_settings` (chiffrée au repos avec pgsodium). La décision doit être prise au kickoff Phase 6 ; recommandation : **côté serveur** pour la simplicité d'administration et cohérence entre clients web et Word add-in.

### 5.5 Latence attendue

Overhead du bridge :

- Serialisation MCP stdio → parse JSON : ~0,5 ms.
- TLS handshake réutilisé (connection pooling httpx) : ~0 ms après premier appel.
- Round trip HTTPS local → Supabase région proche : **60–120 ms** RTT typique (CH→EU Supabase).
- Cold start Edge Function : 200–700 ms au premier appel par worker.
- Budget total overhead : **< 150 ms** en chaud, **< 900 ms** en cold start, vs un accès SQLite local actuel ~5–30 ms.

Conséquences :

- Pour les tools triviaux (list_courts, get_statistics) la latence va **augmenter significativement en relatif** (~10 ms → ~100 ms). Acceptable car toujours perçu instantané par l'humain.
- Pour les tools composites (search_decisions avec rerank LLM, déjà ~2–8 s aujourd'hui) l'overhead est négligeable (< 2 %).
- Mitigation : connexion HTTP/2 keep-alive persistante, warm-up script qui ping les 5 tools chauds toutes les 4 minutes pour maintenir les workers chauds.

---

## 6. Gestion des tools « no-op » (`update_database`, `check_update_status`)

### 6.1 Sémantique actuelle

Dans `mcp_server.py`, `update_database` déclenche un scraping local (processus long, écriture dans les SQLite locaux). Ce comportement est **incompatible** avec l'architecture Supabase : les données sont centralisées, l'ingestion est pilotée par `pg_cron` et les scrapers hébergés (Phase 2/5).

### 6.2 Sémantique cible (post-migration)

- `update_database(force?: bool)` :
  - Ne lance **aucune** opération locale.
  - Effectue un `SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 1` + un `SELECT next_run_at FROM cron_schedule WHERE job='ingestion'`.
  - Retourne :
    ```
    {
      "status": "server-managed",
      "last_ingestion_at": "2026-04-13T02:00:00Z",
      "last_ingestion_duration_s": 842,
      "last_ingestion_rows_added": 1243,
      "next_scheduled_at": "2026-04-14T02:00:00Z",
      "cron_status": "healthy",
      "note": "L'ingestion est gérée côté serveur par les scrapers automatisés. Aucune action locale requise."
    }
    ```
  - Param `force` ignoré avec un avertissement dans le champ `note` si `true`.
- `check_update_status()` : identique à `update_database()` sans `force`. Conservation pour compat uniquement.

### 6.3 Messages utilisateur

Le champ `note` est affiché textuellement par certains clients (Claude Desktop, Word add-in). Texte localisé en 3 langues :

- fr : « L'ingestion est gérée côté serveur par les scrapers automatisés. Aucune action locale requise. Prochaine mise à jour planifiée : {next_scheduled_at}. »
- de : équivalent allemand.
- it : équivalent italien.

La langue est choisie via `Accept-Language` du header de la requête.

### 6.4 Migration douce

Pendant 4 semaines après bascule, la documentation externe (README, docs Word add-in) est mise à jour pour expliquer la nouvelle sémantique. Aucune erreur n'est renvoyée si un client appelle `update_database(force=true)`.

---

## 7. Refactor REST API

### 7.1 Décision d'architecture : garder FastAPI ou l'éliminer ?

Deux options :

**Option A — Garder `web_api/main.py` comme mince proxy**. FastAPI écoute toujours sur le port 8910, mais chaque route délègue à l'Edge Function correspondante (via httpx). Avantages :

- Zéro modification côté Word add-in et navigateur (URL stable `http://localhost:8910`).
- Conservation du streaming SSE `/chat` façon actuelle.
- Gestion des clés provider locale (déjà implémentée dans `_provider_status`, `/settings/keys`).
- Possibilité d'ajouter des routes hybrides (par ex. enrichissement d'un résultat Edge avec une logique locale).

**Option B — Éliminer FastAPI, Word add-in parle directement à Supabase**. Avantages :

- Moins de composants, pas de process local à maintenir.
- Inconvénients : chaque client doit gérer l'auth, les CORS, la config des clés provider, et la migration est plus disruptive (déploiement Word add-in obligatoire).

**Recommandation** : **Option A** pour la Phase 6, avec plan de sunset en Phase 9+ si l'option B devient souhaitable. Raison : le plan-maître impose compatibilité sans modification client.

### 7.2 Refactor minimal de `web_api/main.py`

Fichier : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py` (710 lignes actuelles). Changements :

1. **Suppression du couplage direct MCP stdio**. La version actuelle appelle le MCP via un subprocess ou appelle directement les helpers Python (à confirmer selon l'implémentation). Après refactor, chaque route devient un `await httpx_client.post(<edge-url>/<tool>, json=payload)` avec ré-emballage du retour dans la shape attendue par les consommateurs REST.
2. **Conservation des routes** :
   - `/health`, `/settings/keys` (GET, POST, DELETE), `/settings/ollama/status`, `/settings/ollama/url`, `/statute` (get_statute_text), `/decision/{id}`, `/search`, `/sessions`, `/chat` (POST stream SSE), `/chat/messages`, `/chat/sessions/{id}`.
   - Les 30 routes (liste exhaustive à consolider dans `contracts/routes.json` Phase 1) gardent leur URL path, method, query/body schema et response schema.
3. **Streaming `/chat`** : la route continue de consommer un provider LLM (OpenAI/Anthropic/Gemini/Ollama) mais le **retrieval** (les tools MCP appelés durant la conversation pour injecter le contexte RAG) passe par HTTPS vers les Edge Functions. Le pattern `_resolve_cited_decisions` devient un appel httpx à `get_decision`.
4. **Client httpx unique** : `AsyncClient` partagé, keep-alive, timeout 30 s sauf pour `/chat` (pas de timeout). HTTP/2 activé.
5. **Cache** : possibilité de conserver un cache LRU local côté FastAPI pour décharger les Edge Functions (par ex. `list_courts`, `get_statistics`, TTL 5 min).

### 7.3 Authentification et clés providers

- `/settings/keys` continue de stocker les clés provider dans `.env` local (comme aujourd'hui) **ou** dans Supabase `user_settings` (recommandé en Phase 6.5).
- Transition suggérée : dual-write 2 semaines, lecture prioritaire Supabase si l'utilisateur est connecté, fallback `.env` sinon.
- Pas d'authentification obligatoire pour l'accès local (compatibilité avec l'usage desktop solo). Pour l'accès hébergé ultérieur, JWT Supabase.

### 7.4 Streaming `/chat`

- Server-Sent Events (SSE) identique à l'actuel format (voir `_sse`, `ChatChunk` dans `main.py`).
- Si un tool MCP est invoqué par l'orchestrateur LLM en cours de streaming, FastAPI le relaie via HTTPS et réinjecte le résultat dans le flux.
- Latence ajoutée par appel HTTPS : ~80 ms par tool utilisé dans une conversation. Acceptable (1 à 3 tools par tour en moyenne).

### 7.5 Headers propagés

- `X-Request-Id` : généré par FastAPI s'il n'existe pas, relayé à l'Edge. Permet corrélation des logs.
- `X-User-Id` : si auth présente.
- `X-Tool-Version` : renseigné par l'Edge Function en réponse pour traçage.
- `Accept-Language` : relayé pour les messages localisés (voir no-ops section 6).

### 7.6 Tests

- Les tests FastAPI existants sont rejoués à chaque release.
- Un nouveau suite `tests/integration/test_rest_edge_parity.py` compare la réponse REST actuelle (branche `main`) et la réponse REST post-bascule pour 100 requêtes couvrant les 30 routes, sur la même base de données.

---

## 8. Compatibilité Word add-in

### 8.1 Architecture actuelle

- TypeScript/Office.js, 6 fichiers dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/js/` : `api.js`, `app.js`, `citation.js`, `i18n.js`, `verify.js`, `word-api.js`.
- Appels HTTP via `fetch()` (vu dans `api.js` lignes 80 et 175, `verify.js` ligne 65 pour l'API Anthropic directe).
- Config URL dans manifest `manifest.xml` + variables d'environnement injectées au build.

### 8.2 Impact Phase 6

- **Aucune modification fonctionnelle attendue** si l'Option A (FastAPI proxy) est retenue : Word add-in continue d'appeler `http://localhost:8910`.
- Si l'utilisateur déploie l'add-in vers un backend hébergé, l'URL de base devient `https://<project>.functions.supabase.co` ou un domaine custom `https://api.casine.ch`. Déjà configurable via `.env` et manifest.
- Le flux `verify.js` qui tape directement `api.anthropic.com` reste inchangé : il valide la clé Anthropic de l'utilisateur depuis son navigateur Word. Pas de dépendance à notre backend.

### 8.3 Tests end-to-end

Suite Playwright dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/tests` (à étendre) :

1. **Recherche** : ouvrir l'add-in, saisir requête, vérifier que les 20 premiers résultats matchent la shape attendue.
2. **Insertion citation** : sélectionner un résultat, insérer la citation dans le document Word, vérifier format.
3. **Vérification** : vérifier clé Anthropic, vérifier hallucination d'une citation.
4. **Brief** : ouvrir un brief sur une décision, insérer dans le document.
5. **Export** : exporter liste de citations.
6. **i18n** : rotation fr/de/it et validation des libellés.

### 8.4 TLS

- Backend hébergé (hors mode Option A local) doit exposer TLS 1.3 avec certificat valide. Supabase fournit nativement `*.functions.supabase.co`.
- Pour mode local (Option A), Word add-in tolère `http://localhost:8910` si chargé via `https://localhost:<dev-port>` pour la ressource add-in elle-même (configuration `ManifestMixedContent` Office).

### 8.5 Rétro-compatibilité des shapes de réponse

Gel strict : aucun champ renommé, aucun champ supprimé. Le test de parité (section 11) compare byte-à-byte les réponses après normalisation JSON canonique.

---

## 9. Authentification et rate limiting

### 9.1 Modèle d'auth

- **Public anon** : accès en lecture aux tools non-LLM (list_courts, get_decision, find_citations, etc.). JWT anon Supabase dans header `Authorization`.
- **Utilisateur authentifié** : requis pour tools LLM coûteux et pour `/chat`. JWT utilisateur via Supabase Auth (email/OTP ou OAuth).
- **Service role** : réservé aux Edge Functions internes qui font du batch (ex. génération nocturne de briefs). Jamais exposé au client.

### 9.2 Rôles Postgres

- `mcp_reader` : `SELECT` sur `decisions`, `chunks`, `citation_edges`, `statutes`, etc.
- `mcp_writer` : + `INSERT/UPDATE` sur `sessions`, `chat_messages`, `tool_metrics`, `generation_log`.
- `mcp_admin` : + DDL + accès `pg_cron`.

### 9.3 RLS

- Tables publiques (lecture seule) : `decisions`, `statutes`, `commentaries`, `materialien`, `citation_edges`, `chunks` → RLS policy `USING (true)` pour `anon`, `authenticated`.
- Tables utilisateur : `user_settings`, `sessions`, `chat_messages`, `generation_log` → RLS policy `USING (user_id = auth.uid())`.
- Tables opérationnelles : `tool_metrics`, `ingestion_runs` → pas d'accès client (lecture via fonction).

### 9.4 Rate limiting

Politique par tool et par utilisateur, appliquée dans `_shared/auth.ts` avec compteur Postgres (table `rate_limit_counters`) ou bucket glissant simple :

| Catégorie tools | Limite anon | Limite user | Limite LLM user |
|---|---|---|---|
| Lecture triviale (list_courts, get_decision…) | 60/min | 300/min | — |
| Recherche (search_*, find_*) | 20/min | 120/min | — |
| LLM composites (draft_mock_decision, get_doctrine, generate_exam_question, get_case_brief, analyze_legal_trend.summarize) | 0 (refusé anon) | refusé | 10/heure + 50/jour |

Dépassement : `429 Too Many Requests` avec `Retry-After`. Les tools `update_database` / `check_update_status` : 10/heure.

### 9.5 Quota par clé provider

Si les clés LLM sont gérées côté serveur (recommandé), un compteur `tokens_consumed_today` par utilisateur et par provider est tenu, limite soft configurée par l'admin.

---

## 10. Observabilité

### 10.1 Logs structurés

- Format : JSON lines.
- Champs obligatoires : `ts`, `level`, `tool`, `request_id`, `user_id?`, `duration_ms`, `db_time_ms`, `llm_time_ms`, `rows_returned`, `cache_hit`, `error?`.
- Émis vers stdout de l'Edge Function → Supabase Logs → export vers OpenObserve / Grafana Loki.

### 10.2 Tracing

- Propagation `traceparent` (W3C Trace Context) depuis le client (si supporté) ou générée par le bridge/REST.
- Span par tool, sous-spans par appel DB et par appel LLM.
- Export OpenTelemetry vers un collecteur self-hosted (Tempo / Jaeger).

### 10.3 Métriques

Table Postgres `tool_metrics` (agrégée quotidiennement) :

- `tool`, `version`, `date`, `invocations`, `errors`, `p50_ms`, `p95_ms`, `p99_ms`, `cache_hit_rate`, `db_time_avg_ms`, `llm_time_avg_ms`, `tokens_consumed`.

Vue matérialisée `mv_tool_health_daily` pour dashboards Grafana.

Dashboards minimum (Phase 9 consolidera) :

- **Parité canary** : taux d'écart Edge vs legacy par tool.
- **Latence** : p50/p95/p99 par tool, 7 jours.
- **Erreurs** : stacked area par classe d'erreur (`BadInput`, `Upstream`, `Internal`).
- **Cold-start** : histogramme cold-start par fonction.
- **Quota LLM** : tokens consommés par jour par provider.

### 10.4 Alerting

- p95 > 1,5 × baseline pendant 10 min → warning Slack.
- p99 > 3 × baseline pendant 5 min → critical PagerDuty.
- Taux d'erreur > 2 % sur 15 min → critical.
- Dérive parité canary > 1 % sur 100 requêtes consécutives → bloquant (auto-rollback à legacy).

---

## 11. Stratégie de bascule production

### 11.1 Pipeline canary par tool

1. **Déploiement Edge Function** en production Supabase, accessible mais non référencée par le bridge/REST.
2. **Phase dual-compare** (1 à 3 jours par tool) : 10 % du trafic envoyé en miroir à l'Edge, réponse comparée à la legacy byte-à-byte (normalisation JSON). Dérive loggée. Réponse utilisateur servie par la legacy.
3. **Phase canary 10 %** : 10 % du trafic réel servi par l'Edge, 90 % par la legacy. Surveillance latence et erreurs.
4. **Phase canary 50 %** : ramp-up.
5. **Phase full 100 %** : bascule complète.
6. **Fallback automatique** : si taux d'erreur > 2 % ou dérive > 1 %, le flag repasse à legacy automatiquement.

Ordre des tools : voir section 4.24 (colonne Canary). Les 3 tools triviaux (list_courts, get_statistics, get_decision) passent en premier pour valider la plomberie bridge+REST+Edge, puis montée en complexité.

### 11.2 Feature flag

- Fichier côté bridge : `~/.swiss-caselaw/bridge.toml`.
- Fichier côté FastAPI : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/config.toml`.
- Admin flag global : table Supabase `feature_flags` avec `tool`, `mode` ∈ `{legacy, edge, dual-compare, canary-N}`, `rollout_pct`, `updated_at`.
- Recharge des flags toutes les 30 s par le bridge et le REST.

### 11.3 Rollback par tool

- Flip du flag `mode=legacy` pour un tool précis, les autres restent sur Edge.
- Temps de rollback cible : < 60 s.
- Zéro impact sur les 22 autres tools.

### 11.4 Budget de bascule

| Semaine | Activités |
|---|---|
| S1 | Infra : `_shared/`, déploiement skeleton des 23 fonctions, tests unitaires, branchement Supavisor, Zod schemas, CI. Bridge MVP. REST proxy MVP. |
| S2 | Portage logique 13 tools simples (1–13 du canary). Dual-compare puis canary. Tests golden. |
| S3 | Portage 10 tools composites (14–23), dont LLM. Canary progressif. Observabilité finalisée. Revue de parité. |

Buffer d'une semaine au-delà des 3 en cas de dérive parité sur `search_decisions` ou `draft_mock_decision`.

### 11.5 Critères de go/no-go par tool

- Go si : 100 % parité sur golden pour ce tool, p95 ≤ seuil, 7 jours de dual-compare sans dérive.
- No-go si : dérive > 0,1 % ou régression client constatée.

---

## 12. Archivage de `mcp_server.py`

### 12.1 Critères avant archivage

Tous doivent être remplis pendant 21 jours consécutifs après bascule à 100 % sur les 23 tools :

1. Aucune invocation legacy côté bridge (flag `legacy` non activé pour aucun tool).
2. Zéro dérive parité détectée en dual-compare aléatoire (1 % du trafic reste en dual-compare permanent).
3. Latence p95 dans les cibles.
4. Tests golden ré-exécutés hebdomadairement : 100 % pass.
5. Dataset HuggingFace inchangé (schéma Parquet identique) — test en Phase 9.
6. Word add-in tests Playwright : 100 % pass.
7. Claude Desktop e2e (scénario manuel scripté) : pass.

### 12.2 Processus d'archivage

- J+21 : déplacement de `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` vers `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/archive/mcp_server.py.v1`.
- Commit tag `mcp-legacy-final-v1`.
- Mise à jour du README principal : section « Legacy MCP » pointant vers l'archive et expliquant qu'elle n'est plus maintenue.
- `mcp_server.py` reste **exécutable** pendant 90 jours supplémentaires depuis l'archive (sans SQLite requis si migration données accomplie, sinon nécessite les SQLite locaux). Après 90 jours, suppression définitive sauf demande explicite.

### 12.3 Dépendances Python à nettoyer

- `requirements.txt` : retrait de `mcp`, `sentence-transformers`, `onnxruntime`, `sqlite-vec` (si uniquement utilisé par le legacy).
- Conservation de `fastapi`, `httpx`, `uvicorn`, `pydantic` (utilisés par le proxy REST).

### 12.4 Période de grâce

- 6 semaines pendant lesquelles un tool peut être remis temporairement en legacy via le feature flag (pour debug ou régression imprévue).
- Au-delà : archivage irréversible.

---

## 13. Risques et mitigations

### 13.1 Cold-start Edge Functions

- **Risque** : premier appel à une fonction peu sollicitée (ex. `generate_exam_question`) peut dépasser 1,5 s.
- **Mitigation** : script de warm-up toutes les 4 min ciblant les 10 tools les plus utilisés. Pour les tools rares, tolérer le cold-start (l'utilisateur humain est déjà en attente de réponse LLM).

### 13.2 Compatibilité Deno vs Python natif

- **Risque** : certaines libs Python (sentence-transformers, spacy, sqlite3 features avancées) n'ont pas d'équivalent Deno.
- **Mitigation** :
  - Embeddings → pgvectorscale côté DB (déjà Phase 4), calcul de l'embedding de requête délégué à un micro-service dédié.
  - Cross-encoder → service externe HTTP (synthetic.new ou Fly.io hébergeant un modèle ONNX).
  - Parsing NLP léger (tokenization, umlaut, stopwords) → portage pur TS, testé unitairement.
  - FTS5 → tsvector ou ParadeDB BM25 (bascule Phase 7 ; en Phase 6 on peut utiliser un `websearch_to_tsquery` basique comme pont).

### 13.3 Perte de features SQLite spécifiques

- **Risque** : `sqlite-vec`, `FTS5` tokenizers custom (`trigram`, `unicode61`), stratégies MATCH ésotériques.
- **Mitigation** : tableau de mapping exhaustif en Phase 1 (`contracts/fts_mapping.md`). Pour les cas non reproductibles fidèlement en Postgres, accepter une **parité fonctionnelle approximative** documentée, et enrichir en Phase 7 avec ParadeDB.

### 13.4 Dérive JSON response shape

- **Risque** : ordre des clés, précision des floats, formats de date divergents.
- **Mitigation** : normalisation canonique (tri clés, round floats à 6 décimales, dates ISO 8601 Z) avant comparaison. Test automatisé avec tolérance configurable.

### 13.5 Connexions Postgres épuisées

- **Risque** : explosion des workers Edge → saturation du pool Postgres.
- **Mitigation** : Supavisor en mode transaction pooling. Limite dure `max_connections = 500` côté Postgres, soft limit 300 pour les fonctions. Monitoring dédié.

### 13.6 Coût LLM runaway

- **Risque** : tools LLM exposés publiquement → facture anthropic/openai inattendue.
- **Mitigation** : auth obligatoire pour tools LLM, quota par user, alerting coût quotidien, kill-switch manuel par tool.

### 13.7 Latence accumulée dans `/chat`

- **Risque** : une conversation qui invoque 5 tools MCP voit 5 × 100 ms = 500 ms ajoutés par rapport à la version stdio locale.
- **Mitigation** : parallélisation des appels tools quand possible, cache agressif sur les 3 tools les plus appelés (`get_decision`, `find_citations`, `get_law`).

### 13.8 Secrets et clés provider

- **Risque** : fuite de clé OpenAI/Anthropic dans logs ou headers.
- **Mitigation** : redaction automatique par `_shared/logger.ts` (regex sur patterns `sk-…`, `claude-…`), tests anti-fuite, pgsodium pour chiffrement au repos.

### 13.9 Régression silencieuse Word add-in

- **Risque** : le changement de backend casse un flow add-in inattendu.
- **Mitigation** : suite Playwright exécutée nightly + au moment de chaque canary, blocage auto si échec.

### 13.10 Dépendance à Supabase

- **Risque** : incident Supabase = indisponibilité totale de tous les tools.
- **Mitigation** : bridge peut basculer tous les tools en `legacy` via flag global en < 60 s, à condition que `mcp_server.py` archive reste exécutable localement. Plan de continuité documenté.

---

## 14. Definition of Done

La Phase 6 est considérée terminée lorsque l'ensemble des critères suivants est validé, signé par le lead technique et le lead produit.

### 14.1 Code et déploiement

- [ ] 23 Edge Functions déployées en production Supabase sous `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/supabase/functions/` avec convention 1 dossier / 1 tool.
- [ ] Module `_shared/` couvrant les 12 responsabilités listées section 3.3.
- [ ] Types TS générés depuis le schéma Postgres (Phase 1) et versionnés.
- [ ] Bridge MCP stdio → HTTPS fonctionnel dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_bridge/`.
- [ ] `web_api/main.py` refactoré comme proxy vers les Edge Functions, 30 routes opérantes.
- [ ] Feature flags fonctionnels côté bridge, REST et table Supabase.

### 14.2 Parité fonctionnelle

- [ ] 200 requêtes golden Phase 1 passent à 100 % sur les 23 tools via Edge.
- [ ] Dérive JSON < 0,1 % sur dual-compare 7 jours par tool.
- [ ] Tests FastAPI existants : 100 % pass.
- [ ] Tests Playwright Word add-in : 100 % pass.
- [ ] Test manuel Claude Desktop : OK sur scénario scripté (10 flows).

### 14.3 Performance

- [ ] p95 par tool ≤ seuil défini section 1.2.
- [ ] Cold-start p95 des 5 tools critiques < 700 ms.
- [ ] Warm-up cron opérationnel.

### 14.4 Sécurité

- [ ] RLS activée sur tables utilisateur.
- [ ] Rôles Postgres `mcp_reader`, `mcp_writer`, `mcp_admin` créés et appliqués.
- [ ] Rate limiting vérifié par tests de charge.
- [ ] Redaction secrets validée par audit logs.
- [ ] Clés provider chiffrées (pgsodium ou keychain local selon choix).

### 14.5 Observabilité

- [ ] Logs structurés JSON sur 100 % des invocations.
- [ ] Dashboards Grafana pour parité, latence, erreurs, cold-start, quota LLM.
- [ ] Alerting Slack + PagerDuty configuré.
- [ ] Table `tool_metrics` peuplée et vue `mv_tool_health_daily` rafraîchie quotidiennement.

### 14.6 Migration et archivage

- [ ] Feature flag par tool documenté et testé en rollback.
- [ ] 21 jours consécutifs à 100 % Edge sans dérive sur tous les tools.
- [ ] `mcp_server.py` déplacé vers `archive/` avec tag `mcp-legacy-final-v1`.
- [ ] README mis à jour.
- [ ] Dépendances Python nettoyées.

### 14.7 Documentation

- [ ] `docs/plan/phase-6-mcp-edge-functions.md` (ce document) finalisé et approuvé.
- [ ] `docs/architecture/edge-functions.md` à créer : vue d'ensemble technique.
- [ ] `docs/ops/runbook-edge.md` : procédures incident, rollback, cold-start, warm-up.
- [ ] `docs/contracts/tools.json` : schémas Zod exportés en JSON Schema pour consommation externe.
- [ ] `docs/contracts/routes.json` : inventaire des 30 routes REST + schémas.
- [ ] Changelog public pour `update_database` / `check_update_status` (nouvelle sémantique no-op).

### 14.8 Gouvernance

- [ ] Revue de sécurité (threat model) passée.
- [ ] Revue de coût (estimation Supabase + LLM) validée.
- [ ] Backup et DR : procédure testée sur base de staging.
- [ ] Tableau de bord exécutif : adoption, coût par tool, qualité perçue.

---

## Annexes

### A. Liste exhaustive des 23 tools (rappel du plan-maître)

1. search_decisions
2. get_decision
3. list_courts
4. get_statistics
5. find_citations
6. find_appeal_chain
7. find_leading_cases
8. analyze_legal_trend
9. draft_mock_decision
10. get_case_brief
11. get_doctrine
12. generate_exam_question
13. get_law
14. search_laws
15. get_commentary
16. search_commentaries
17. get_materialien
18. search_materialien
19. search_legislation
20. get_legislation
21. browse_legislation_changes
22. update_database (no-op client)
23. check_update_status (no-op client)

### B. Références fichiers

- Monolithe : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` (13 068 lignes, 538 KB).
- REST : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py` (710 lignes).
- Word add-in : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/` (manifest, js/, css/, tests/).
- Schéma DB : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/db_schema.py` (Phase 1 en sera la source de vérité Postgres).
- Chunker : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py`.
- Embeddings : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py`.
- Graph : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`.
- Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
- Rapport PA-RAG : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`.

### C. Répertoires nouveaux à créer en Phase 6

- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/supabase/functions/` (23 sous-dossiers + `_shared/`).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_bridge/` (bridge stdio→HTTPS).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/archive/` (destination du monolithe archivé).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/architecture/` (edge-functions.md).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/ops/` (runbook-edge.md).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/contracts/` (tools.json, routes.json, fts_mapping.md).

### D. Interactions avec les autres phases

- **Phase 1** fige les contrats (entrées/sorties tools et routes) que la Phase 6 respecte strictement.
- **Phase 2** fournit le miroir Postgres que les Edge Functions interrogent.
- **Phase 3** fournit `chunks` (SAC) consommés par `search_decisions` et `get_case_brief`.
- **Phase 4** fournit l'index pgvectorscale consommé par `search_decisions`, `find_leading_cases`, `get_doctrine`.
- **Phase 5** fournit `authority_score`, `leading_rank`, `sort_de_laffaire` consommés par les 4 tools PA-RAG-forts.
- **Phase 7** remplace la couche FTS basique par retrieval hybride + authority rerank : **modifie l'intérieur** des Edge Functions `search_decisions`, `find_leading_cases`, `get_doctrine`, `analyze_legal_trend`, mais pas leurs signatures (Phase 6 a fixé le contrat).
- **Phase 8** branche GraphRAG (CTE récursifs) dans `find_citations`, `find_appeal_chain`, `find_leading_cases`, `get_doctrine`.
- **Phase 9** valide benchmark 200 requêtes + dashboards dont ceux définis en section 10 de ce plan.
