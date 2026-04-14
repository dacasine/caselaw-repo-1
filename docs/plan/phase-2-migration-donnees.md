# Phase 2 — Migration des données (SQLite → Postgres Supabase)

> Sous-plan détaillé de la Phase 2 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Préalable : Phase 1 terminée (contrats de schéma gelés, migrations SQL Supabase appliquées, extensions `pgvector`, `pgvectorscale`, `pg_trgm`, `unaccent`, `pgcrypto`, `btree_gin` installées, rôles et RLS cadrés).
> Source de vérité en entrée : SQLite `~/.swiss-caselaw/*.db` (~62 GB cumulés).
> Source de vérité en sortie : cluster Postgres Supabase self-hosted (même VPC que le futur déploiement Edge Functions).
> Rapport fondateur : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`.

---

## 1. Objectifs et critères de succès (invariants chiffrés)

### 1.1 Finalité de la phase

La Phase 2 est la bascule de la couche de persistance. Toutes les autres phases (chunking SAC, embeddings Longformer, enrichissement PA-RAG, Edge Functions, retrieval hybride, GraphRAG, évaluation) supposent que les données applicatives vivent dans Postgres, que l'intégrité référentielle du graphe de citations est préservée bit à bit, et que la lecture MCP/REST peut basculer sans que Word add-in ni Claude Desktop ne s'en rendent compte.

Il ne s'agit pas d'une simple transposition : la Phase 2 gèle aussi le vocabulaire opérationnel (noms de colonnes, types, conventions de nullabilité, timezone, collation) sur lequel toute la Phase 3 s'appuiera. Un bug de casting `TEXT → text`, un fuseau horaire perdu sur `publication_date` ou une collation par défaut non déterministe rendent impossible la vérification d'idempotence ultérieure.

### 1.2 Critères de succès (Definition of Done synthétique, voir §11 pour la version détaillée)

1. **Parité volumétrique** : toutes les tables Postgres miroir affichent un `COUNT(*)` identique (tolérance 0 lignes) à la table SQLite source, après gel de la ligne haute.
2. **Intégrité du graphe** : `decision_citations` Postgres = 8.84 M ± 0.1 %, `decision_statutes` Postgres = 11.34 M ± 0.1 %, `citation_targets` Postgres a une distribution de `confidence_score` dont la moyenne et la médiane sont à ±0.005 de la source SQLite.
3. **Checksum par table** : pour chaque table, `MD5(string_agg(row_canonical_form, '\n' ORDER BY pk))` calculé côté source et côté cible sont identiques. Si divergence, une table de delta est produite et inspectée ligne à ligne.
4. **Échantillonnage aléatoire** : 10 000 `decision_id` tirés sans remise comparés champ-par-champ donnent 100 % de correspondance (modulo normalisations documentées : dates ISO, JSON canonicalisé, espaces blancs trimés).
5. **FTS/tsvector** : la requête golden set de 200 requêtes benchmarks (voir Phase 9) donne un recall@100 Postgres ≥ 98 % du recall@100 SQLite FTS5 sur le même corpus.
6. **Cutover réversible** : pendant les 14 jours de dual-write, un script `rollback.sh` permet de revenir à SQLite en < 30 minutes (feature flag MCP + arrêt du writer Postgres).
7. **Compat HuggingFace** : `export_parquet.py` lisant depuis Postgres produit un Parquet dont le schéma PyArrow est strictement égal à celui produit par la version SQLite (même ordre, mêmes nullabilités, mêmes types).
8. **Performance dégradée acceptable** : aucune latence MCP sur les requêtes lecture ne dépasse 2× la latence SQLite pendant la fenêtre de validation (avant optimisation Phase 4).
9. **Downtime applicatif maximal** : 0 h pour les lecteurs (bascule transparente via feature flag), < 15 min pour les writers (bascule atomique de la config scraper).

### 1.3 Anti-objectifs (explicitement hors périmètre Phase 2)

- Chunking SAC (Phase 3).
- Recalcul des embeddings BGE-M3 → Longformer (Phase 4).
- Enrichissement LLM des 4 piliers PA-RAG (Phase 5).
- Nouvelle API MCP en Edge Functions (Phase 6).
- Index ANN pgvectorscale DiskANN (Phase 4).

Pendant la Phase 2, la table `chunks` n'existe pas encore ; les embeddings actuels BGE-M3 restent dans sqlite-vec et **ne sont pas migrés**. Leur migration est la Phase 4, et elle aura lieu après recalcul Longformer — il n'est donc pas nécessaire de transférer les vecteurs 1024-dim, ce qui évite 60-80 GB de transfert inutile.

---

## 2. Inventaire source par source

### 2.1 `~/.swiss-caselaw/decisions.db` (~58 GB, 965 k+ décisions)

**Tables source** (schéma canonique dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/db_schema.py`) :

- `decisions` : table principale, 28 colonnes textuelles + 1 JSON, ~58 GB dont ~55 GB sur `full_text`.
- `decisions_fts` : table virtuelle FTS5 (`content=decisions`, tokenizer `unicode61 remove_diacritics 2`), synchronisée par triggers `decisions_ai/ad/au`.
- `coverage_targets`, `source_snapshots`, `source_discoveries`, `source_fetch_attempts`, `gap_queue` : tables de métadonnées de couverture des scrapers (voir `db_schema.py:COVERAGE_SCHEMA_SQL`).

**Cibles Postgres** :

- `public.decisions` : mêmes colonnes, types natifs (`text`, `date`, `timestamptz`, `jsonb` pour `json_data`). PK `decision_id` conservée. Colonne `content_hash` contrainte `NOT NULL` après nettoyage (à documenter : aujourd'hui nullable dans SQLite, environ 300 k lignes sans hash, à rétro-remplir pendant la passe 1).
- `public.decisions_tsv` : colonnes générées `tsvector` multilingues DE/FR/IT (3 colonnes, une par langue + une multilingue pondérée) — voir §2.6.
- `public.coverage_targets`, `public.source_snapshots`, `public.source_discoveries`, `public.source_fetch_attempts`, `public.gap_queue` : migration directe, tables petites (< 500 MB cumulé).

**Particularités** :

- `json_data` contient le payload scraper brut, tailles hétérogènes (0 à 2 MB). Choix d'en faire du `jsonb` natif pour exploitation ultérieure (GraphRAG Phase 8) mais avec vérification préalable : ~800 lignes ont du JSON invalide (caractères de contrôle non échappés dans certains scrapers anciens). Le nettoyage est fait à l'extraction, pas au chargement (échec bloquant sinon).
- `cited_decisions` est stocké en TEXT (JSON sérialisé comme string). On conserve ce format pour la Phase 2 afin de ne pas casser `export_parquet.py` ; la transformation en `jsonb` ou en liste relationnelle sera une opération Phase 3/5.
- `canonical_key` : clé de dédup applicative, peut être NULL (anciennes lignes), doit rester indexée avec un index B-tree partiel `WHERE canonical_key IS NOT NULL`.
- Les triggers FTS5 sont abandonnés côté Postgres : le tsvector est calculé en colonne générée stockée (voir §2.6), pas par trigger (simplicité et idempotence).

### 2.2 `~/.swiss-caselaw/reference_graph.db` (~3.5 GB)

**Tables source** (schéma dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`, lignes 33-94) :

- `decisions` (miroir réduit : `decision_id`, `docket_number`, `docket_norm`, `court`, `canton`, `language`, `decision_date`) — présent pour résolution locale, pas à migrer (on pointera vers `public.decisions` au besoin).
- `statutes` : ~85 k articles normalisés (`statute_id`, `law_code`, `article`, `paragraph`).
- `decision_statutes` : 11.34 M liens décision→statut avec `mention_count`.
- `decision_citations` : 8.84 M arêtes citation avec `target_ref`, `target_type` (`decision|bge|docket`), `mention_count`, `is_prior_instance`.
- `citation_targets` : résolution 1→N des `target_ref` non univoques, avec `match_type` (`docket_norm|bge_norm|bge_bare`) et `confidence_score` (calculé par `_citation_confidence` dans `build_reference_graph.py:154-206`).

**Cibles Postgres** :

- `public.statutes` : transposition directe, PK `statute_id`, index composite `(law_code, article)`.
- `public.decision_statutes` : PK `(decision_id, statute_id)`, FK vers `public.decisions` et `public.statutes`, index secondaire sur `statute_id`.
- `public.decision_citations` : PK `(source_decision_id, target_ref)`, index sur `target_ref` pour la traversée inverse.
- `public.citation_targets` : PK `(source_decision_id, target_ref, target_decision_id)`, FK composite sur `decision_citations`, colonne `confidence_score` en `numeric(5,4)` avec CHECK entre 0.05 et 0.99 (bornes établies par la fonction source ligne 206).

**Particularités critiques** :

- La `confidence_score` et le `match_type` (`resolution_method` dans la terminologie du plan-maître) sont le produit d'un calcul déterministe mais coûteux (jointure `decisions × decision_citations` avec `ROW_NUMBER` partitionné). Deux options :
  1. **Recalculer côté Postgres** en rejouant la CTE `_resolve_citation_targets` (lignes 209-429) : avantage, validation du portage CTE ; inconvénient, risque d'écart numérique si floating-point behavior diffère.
  2. **Transférer les valeurs telles quelles** depuis SQLite : avantage, reproductibilité stricte ; inconvénient, on ne valide pas que la CTE Postgres donne le même résultat.
- **Décision** : faire les deux. Transfert brut en première passe, recalcul en deuxième passe sur une table `citation_targets_recomputed`, puis diff. Si `|score_transferred - score_recomputed| < 1e-4` pour ≥ 99.95 % des 13-14 M lignes résolues, on garde les valeurs transférées et on supprime la table temporaire. Sinon, on investigue avant de figer.
- Ne pas perdre les prior-instance flags (`is_prior_instance`) : ils conditionnent la logique d'autorité dans la Phase 7.

### 2.3 `~/.swiss-caselaw/statutes.db` (~42 MB, ~5 500 lois fédérales)

**Tables source** : `laws` (métadonnées de loi), `articles` (contenu d'article), `versions` (historique), `translations` (DE/FR/IT/RM).

**Cibles Postgres** :

- `public.laws` : PK `law_id`, colonnes `sr_number`, `short_title`, `long_title`, `jurisdiction='federal'`, dates d'entrée en vigueur.
- `public.law_articles` : PK `article_id`, FK `law_id`, colonnes `article_number`, `paragraph`, `language`, `text`, `version_valid_from`, `version_valid_to`.
- `public.law_article_tsv` : tsvector multilingue pondéré (voir §2.6).

**Particularité** : le lien `decision_statutes.statute_id` (issu du graph) n'est pas strictement aligné avec `law_articles.article_id` (issu du scraper fedlex). La réconciliation est explicitement **hors scope Phase 2** — elle sera faite en Phase 5 lors de l'enrichissement (cross-link `citation → article de loi → contenu`). On conserve donc les deux espaces de noms `statute_id` et `article_id` distincts et reliés par un futur mapping `statute_article_map` créé en Phase 5.

### 2.4 `~/.swiss-caselaw/cantonal_laws.db` (~26 000 actes cantonaux)

**Tables source** : `cantonal_laws`, `cantonal_articles`, `cantons` (métadonnées lexfind).

**Cibles Postgres** : `public.cantonal_laws`, `public.cantonal_law_articles`. Intégrées au même espace `public.laws` via un champ discriminant `jurisdiction ∈ {federal, cantonal_AG, cantonal_ZH, ...}` ? Décision **reportée au gel de schéma Phase 1** : si la Phase 1 a unifié, on charge dans la table unifiée ; sinon, on garde deux tables distinctes et on prévoit la fusion comme dette technique Phase 5.

### 2.5 `~/.swiss-caselaw/materialien.db` (Botschaften + débats parlementaires)

**Tables source** : `materialien`, `materialien_chunks` (éventuels), `commentaries` (si présent — à confirmer au snapshot).

**Cibles Postgres** : `public.materialien`, `public.commentaries`. Volume faible (~2 GB estimé). Chargement en passe 4, pas de contrainte de performance.

**Particularité** : certains champs contiennent des URL signées (PDF parlement.ch) avec expiration. Ne pas versionner l'URL signée dans Postgres (c'est une vue, pas une donnée). On ne stocke que l'URL canonique non signée, et on regénère le signed URL à la demande côté service.

### 2.6 Reconstruction des tsvector multilingues

Le schéma SQLite utilise FTS5 avec tokenizer `unicode61 remove_diacritics 2` et une seule virtual table. Postgres utilise `tsvector` + `ts_rank_cd` + optionnellement ParadeDB BM25 (cf. plan-maître ligne 25).

Pour chaque décision, la Phase 2 calcule **trois colonnes générées stockées** :

- `tsv_de` : `setweight(to_tsvector('german', coalesce(title,'')), 'A') || setweight(to_tsvector('german', coalesce(regeste,'')), 'B') || setweight(to_tsvector('german', coalesce(full_text,'')), 'C')`.
- `tsv_fr` : idem avec `french`.
- `tsv_it` : idem avec `italian`.

La colonne retenue pour la recherche dépend de `decisions.language`. Postgres n'ayant pas de configuration `unicode61-like` en natif, on applique `unaccent` en amont (fonction wrapper `immutable_unaccent(text) → text` installée en Phase 1) pour se rapprocher du comportement `remove_diacritics 2`.

Pour les décisions multilingues (rare, ~2 % du corpus), on stocke les trois colonnes et la recherche applicative choisit à la volée. Un index GIN composite `(language, tsv_de, tsv_fr, tsv_it)` est partiel par langue pour optimiser. Alternativement, une colonne `tsv_primary` calculée conditionnellement est créée pour simplifier la majorité des requêtes.

La pondération `A > B > C` (title > regeste > full_text) reflète l'heuristique PA-RAG (cf. rapport fondateur §retrieval hybride) : un hit dans le titre ou le regeste est un signal plus fort qu'un hit dans le corps.

---

## 3. Architecture ETL

### 3.1 Choix d'orchestrateur

Trois options évaluées :

- **Airflow** : robuste, mature, mais lourd pour 4 passes séquentielles. Infra supplémentaire (scheduler + workers + DB Airflow). Rejeté.
- **n8n** : prétexte l'existant (mcp n8n déjà présent dans le contexte). Mais n8n excelle sur les workflows I/O événementiels, pas sur les ETL batch 58 GB. Rejeté.
- **Scripts Python séquentiels orchestrés par un driver simple** (`phase2_migrate.py`) avec checkpoints sur disque et idempotence par hash. **Retenu**.

Rationale : la migration est one-shot (avec reprise sur incident), 4 passes linéairement dépendantes, pas de parallélisme inter-passes hors parallélisme intra-passe par court ou par shard. Un script bien structuré avec `--resume` et un fichier d'état JSON est plus simple à opérer et à débugger qu'un DAG Airflow.

Le driver `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_migrate.py` (à créer en Phase 2) expose :

```
phase2_migrate.py --pass {1,2,3,4,all} [--resume] [--dry-run] [--shard N/K] [--limit N] [--verify-only]
```

État persisté dans `/Users/damienhottelier/.swiss-caselaw/phase2_state.json` : dernier `decision_id` traité par passe, checksums intermédiaires, horodatage.

### 3.2 Parallélisme intra-passe

- **Passe 1 (decisions + tsv)** : shardée par `court` (29 cours) + par tranche `rowid` modulo 8. Chaque worker consomme un shard, écrit dans une staging table `public.decisions_staging` via `COPY FROM STDIN` (binary format PyArrow → psycopg binary), puis un `INSERT ... ON CONFLICT` final depuis la staging vers `public.decisions` orchestré par le driver.
- **Passe 2 (statutes + articles)** : volume faible, séquentiel monothread suffit.
- **Passe 3 (graphe)** : shardée par plage de `source_decision_id` hash modulo 16. Worker pool Python × psycopg, `COPY FROM` sur staging `decision_citations_staging`, puis merge. La CTE de résolution `citation_targets` est exécutée en une seule transaction côté serveur (pas de parallélisation applicative, c'est le planner Postgres qui parallélise via `max_parallel_workers_per_gather`).
- **Passe 4 (materialien/commentaries)** : séquentiel, volume faible.

Parallélisme cible : 8 workers Python pour la passe 1 (limité par I/O disque SQLite en lecture, pas par CPU). On n'ouvre qu'une seule connexion SQLite read-only par worker avec `immutable=1` (confirmé par le commit récent `78216bf` du repo : «immutable=1 on all MCP worker DB connections + DELETE journal mode»).

### 3.3 Reprise sur incident

Chaque passe est idempotente (voir §4). Si un worker crashe, le driver :

1. Détecte la panne via timeout (heartbeat de 30 s dans `phase2_state.json`).
2. Marque le shard `failed`, libère le slot.
3. Relance le shard **depuis le début du shard** (pas depuis la dernière ligne) — c'est moins optimal mais infiniment plus simple et sûr que le resume au milieu d'un shard. Le `INSERT ... ON CONFLICT DO NOTHING` absorbe la re-ingestion.

Le driver maintient un log `/Users/damienhottelier/.swiss-caselaw/phase2.log` en JSON Lines pour audit post-mortem.

### 3.4 Connexion Postgres

- **Côté load** : connexion directe au port Postgres 5432 du cluster Supabase, **pas via PostgREST** (PostgREST ne supporte pas `COPY` efficacement et introduit de la latence HTTP). On utilise psycopg 3 avec `pool_min_size=1, pool_max_size=8` par processus.
- `statement_timeout=0` pendant la migration (les `CREATE INDEX` finaux peuvent durer > 1 h).
- `maintenance_work_mem=2GB`, `work_mem=256MB` temporairement via `SET LOCAL` dans chaque transaction de load.
- Désactivation temporaire des index non-essentiels avant la passe 1, recréation à la fin (classique pour un bulk load).

### 3.5 Format de transfert

- Staging : `COPY table FROM STDIN WITH (FORMAT binary)` via psycopg `cursor.copy()`. Plus rapide que `INSERT` en batch, compatible avec `tsvector` généré côté Postgres (pas côté client).
- Texte brut (`full_text`) transféré tel quel, pas de compression applicative (la compression TOAST de Postgres s'en charge).

---

## 4. Stratégie d'idempotence

### 4.1 Principe général

Chaque passe doit pouvoir être rejouée N fois sans effet de bord. Deux mécanismes combinés :

1. **Hash de source** : chaque ligne source calcule un `source_row_hash = sha256(concatenation_canonique(colonnes_significatives))`. Ce hash est stocké dans une colonne technique `_migration_hash text` (à supprimer en fin de Phase 2). Si le hash de la ligne source n'a pas changé entre deux runs, l'UPSERT est un no-op pur.
2. **Upsert par PK** : toutes les tables cibles ont une PK stable (voir §2). L'instruction de base est :

```
INSERT INTO public.decisions (...) VALUES (...)
ON CONFLICT (decision_id) DO UPDATE
SET ... WHERE decisions._migration_hash IS DISTINCT FROM EXCLUDED._migration_hash;
```

Le `WHERE ... IS DISTINCT FROM` évite les updates à vide (et les invalidations de cache TOAST).

### 4.2 Staging tables

Chaque passe utilise une staging table `public.<table>_staging_<passN>` :

- Chargée par `COPY` brut (très rapide).
- Pas d'index (sauf PK si utile).
- Merge dans la table cible via `INSERT ... SELECT ... ON CONFLICT`.
- Tronquée à la fin de la passe si merge OK.

Avantage : la staging est découpée de la cible, permettant :

- Validation intermédiaire (counts, checksums) avant merge.
- Rollback simple : `DROP TABLE <staging>` sans toucher la cible.
- Bulk load sans fragmenter la table cible (qui garde ses index).

### 4.3 Cas particulier : suppressions en source

Pendant la migration, des scrapers peuvent supprimer des lignes dans SQLite (rare mais possible : correction de dédup, purge de lignes corrompues). La Phase 2 ne propage **pas** les suppressions : on n'ingère que des INSERT/UPDATE. La réconciliation finale (juste avant bascule) inclut un diff `SELECT decision_id FROM sqlite EXCEPT SELECT decision_id FROM postgres` pour détecter les orphelins et les supprimer manuellement après revue.

### 4.4 Idempotence du graphe de citations

Le graphe est reconstruit, pas migré ligne à ligne, pour une raison subtile : si on ajoute une décision pendant le dual-write (par exemple un nouveau BGer du jour), son texte contient des citations qui référencent des décisions anciennes. Ces citations doivent être résolues contre le corpus complet Postgres, pas contre un snapshot figé.

Donc la passe 3 :

1. Migre bulk `decision_citations` et `decision_statutes` depuis SQLite (état figé du snapshot).
2. Re-résout `citation_targets` **côté Postgres** via une CTE équivalente à `_resolve_citation_targets` dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py:209-429`.
3. Pendant le dual-write, chaque nouvelle décision insérée déclenche (côté application ou via job de fond) une mise à jour incrémentale : extraction de ses citations + résolution limitée aux `target_ref` nouvellement introduits.

Invariant : `|decision_citations Postgres - decision_citations SQLite| / 8.84M < 0.001` après la passe 3, avant dual-write.

---

## 5. Stratégie de cutover

### 5.1 Vue d'ensemble

La bascule se fait en **4 étapes temporellement disjointes** sur 14 jours :

| Jour | Écriture scrapers | Lecture MCP/REST | Lecture HF Parquet |
|---|---|---|---|
| J-1 à J0 | SQLite only | SQLite only | SQLite |
| J0 (migration initiale) | SQLite only (gel 2h) | SQLite only | SQLite |
| J0+2h à J+14 | **SQLite + Postgres** (dual-write) | SQLite only (par défaut) → progressivement Postgres via feature flag | SQLite |
| J+14 (bascule lecture) | SQLite + Postgres | **Postgres only** | Postgres (export depuis Postgres) |
| J+14 à J+28 | SQLite + Postgres (sécurité) | Postgres only | Postgres |
| J+28 (arrêt dual-write) | Postgres only | Postgres only | Postgres |

### 5.2 Gel temporaire des scrapers (J0)

Avant le démarrage de la migration initiale, tous les scrapers sont arrêtés via flag `SCRAPER_ENABLED=false` dans leur config systemd/cron. Les 29 scrapers (inventaire dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scrapers/`) consultent ce flag au démarrage de chaque run. Fenêtre cible : 2h (les 4 passes idempotentes tournent en < 90 min sur le matériel cible, marge confortable).

Gel uniquement pour la passe 1 et le début de la passe 3 (snapshot cohérent). Les passes 2 et 4 peuvent tourner scrapers actifs (volume faible, pas de contention I/O SQLite en lecture).

### 5.3 Dual-write (J0+2h à J+28)

**Mécanique** : une couche d'abstraction `DecisionWriter` (dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/pipeline.py` ou un nouveau module `dual_writer.py`) expose `insert_decision(row)` qui appelle deux backends :

1. Backend SQLite (existant, `db_schema.INSERT_OR_IGNORE_SQL`).
2. Backend Postgres (nouveau, `INSERT ... ON CONFLICT` vers `public.decisions`).

Les deux écritures sont dans la même transaction logique applicative mais des transactions DB séparées (on ne peut pas faire une XA transaction SQLite+Postgres). Règle : **SQLite d'abord, Postgres ensuite**. Si Postgres échoue, log l'erreur, enqueue dans une table `dual_write_retry_queue` (côté Postgres), continue. On ne bloque jamais un scraper sur une panne Postgres pendant la phase de dual-write. Conséquence : Postgres peut temporairement être en retard sur SQLite, ce qui est accepté (un reconciler horaire résorbe le retard — voir §5.4).

Feature flag côté scraper : `DUAL_WRITE_POSTGRES=true`, défaut `false`, activé globalement après validation de la migration initiale.

### 5.4 Reconciler horaire

Un job `phase2_reconciler.py` (cron */30min pendant la fenêtre dual-write) :

1. Liste `decision_id` présents dans SQLite avec `scraped_at > last_reconcile_ts` et absents de Postgres.
2. Les copie dans Postgres via le même mécanisme de staging.
3. Met à jour `last_reconcile_ts`.

Ceci couvre : (a) les échecs intermittents Postgres du dual-write, (b) les scrapers qui n'ont pas encore été redémarrés avec le flag `DUAL_WRITE_POSTGRES=true`.

### 5.5 Bascule lecture MCP/REST (J+14)

La bascule lecture est le moment de vérité. Elle est contrôlée par un feature flag `MCP_READ_BACKEND ∈ {sqlite, postgres, shadow}`.

- `sqlite` : comportement historique, `mcp_server.py` lit SQLite.
- `postgres` : nouveau, `mcp_server.py` lit Postgres via un driver psycopg.
- `shadow` : lit SQLite (servi à l'utilisateur) **et** Postgres en parallèle, compare les résultats, loggue les divergences sans affecter la réponse utilisateur. Sert 7 jours avant la bascule.

La bascule `shadow → postgres` est atomique (changement de variable d'environnement + reload MCP). Rollback : re-set `sqlite` et reload. Downtime MCP stdio : ~5 s (temps de reload du process). Côté Word add-in et Claude Desktop, transparent (ils reconnectent automatiquement via le bridge stdio).

### 5.6 Arrêt du dual-write (J+28)

- Scrapers repassent en `DUAL_WRITE_POSTGRES=false` → écriture Postgres only.
- SQLite `decisions.db` et `reference_graph.db` passent en read-only (chmod 444 + wrapper `DecisionWriter` rejette les writes SQLite).
- Snapshot cold de SQLite conservé 90 jours minimum sur stockage froid (S3 Glacier ou équivalent) pour rollback catastrophique.

### 5.7 Rollback plan

Quatre scénarios de rollback, du plus bénin au plus grave :

1. **Bug MCP lecture Postgres** (jours J+14 à J+28) : flip `MCP_READ_BACKEND=sqlite`, reload. SQLite est toujours à jour (dual-write actif). Durée : 5 min.
2. **Corruption Postgres détectée tard** (après J+28) : restore depuis le dernier snapshot SQLite cold (< 90 jours), replay des nouveaux scrapes depuis leurs archives JSONL (`output/decisions/*.jsonl`) pour combler le gap. Durée : 2-6 h selon le gap.
3. **Divergence graphe citations** détectée pendant la validation : refus de bascule, investigation, relance de la passe 3. Pas de rollback nécessaire (la passe est idempotente).
4. **Panne Supabase prolongée** : MCP retombe en `sqlite` via health-check automatique (Phase 6 ajoutera ce health-check ; pour la Phase 2, c'est manuel).

---

## 6. Plan de validation et réconciliation

### 6.1 Checksums par table

Pour chaque table `T` avec PK `pk`, un script `phase2_checksum.py` calcule :

```
SELECT encode(
  digest(
    string_agg(
      concat_ws('|',
        col1::text, col2::text, ..., colN::text
      ),
      E'\n' ORDER BY pk
    ),
    'sha256'
  ),
  'hex'
) FROM T;
```

Côté SQLite, l'équivalent est construit en Python (SQLite n'a pas `digest` natif) : stream des rows ordonnées par PK, hash incrémental SHA-256.

Les colonnes dérivées (tsvector, `_migration_hash` technique) sont **exclues** du checksum. Les colonnes date/timestamp sont normalisées en ISO 8601 UTC avant hash.

### 6.2 Counts par table

Table | Source (SQLite) | Cible (Postgres) | Tolérance
---|---|---|---
`decisions` | ~965 000 | ==source | 0
`decision_citations` | ~8 840 000 | ==source | ±0.1%
`decision_statutes` | ~11 340 000 | ==source | ±0.1%
`citation_targets` | ~13 000 000 | ==source recalc | ±0.5% (recalcul possible)
`statutes` (graph) | ~85 000 | ==source | 0
`laws` (fedlex) | ~5 500 | ==source | 0
`law_articles` | ~180 000 (estimé) | ==source | 0
`cantonal_laws` | ~26 000 | ==source | 0
`materialien` | variable | ==source | 0
`coverage_targets` | ~30 | ==source | 0
`source_snapshots` | variable | ==source | 0

La tolérance ±0.1% sur le graphe est imposée par le recalcul éventuel de `citation_targets` (Postgres peut résoudre 1-2 candidats de plus ou de moins sur des cas ambigus). Si la tolérance est dépassée, refus de bascule (invariant du plan-maître, section 2 ligne 2).

### 6.3 Sampling aléatoire champ-par-champ

Un script `phase2_sample_diff.py` :

1. Tire 10 000 `decision_id` aléatoires uniformément.
2. Pour chaque, requête la ligne côté SQLite et côté Postgres.
3. Compare champ par champ après normalisation documentée :
   - Dates : ISO 8601, troncature à la seconde si `timestamptz`.
   - `json_data` : re-sérialisation canonique (clés triées, pas d'espaces superflus).
   - Strings : `.strip()` + normalisation Unicode NFC des deux côtés.
4. Produit un rapport `/Users/damienhottelier/.swiss-caselaw/phase2_diff_report.json` avec tous les champs divergents.

Critère : 100 % de match sur l'échantillon (après normalisation). Une seule divergence non documentée bloque la bascule.

Sampling additionnel biaisé : 500 `decision_id` parmi les plus cités (top citations count), 500 parmi les plus longs (top `text_length`), 500 parmi les plus récents (top `decision_date`). Garantit couverture des cas atypiques.

### 6.4 Validation du graphe de citations

Checks spécifiques :

- `COUNT(*) WHERE confidence_score >= 0.8` identique ±1% entre SQLite et Postgres.
- Distribution de `match_type` (`docket_norm|bge_norm|bge_bare`) identique en proportion ±0.5%.
- Top-100 `target_ref` les plus cités : liste identique (même ordre, mêmes comptes).
- Prior instance : `COUNT(*) WHERE is_prior_instance = 1` identique exact.
- Transitive closure test : 100 décisions ATF choisies, le graphe de leurs citations à profondeur 2 doit être isomorphe entre les deux sources (même ensemble de nœuds et d'arêtes).

### 6.5 Validation FTS

Golden set de 200 requêtes (préparé en Phase 1, stocké dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/evaluation/golden_queries.jsonl`) :

- Pour chaque requête, top-100 SQLite FTS5 (avec `rank` bm25).
- Pour chaque requête, top-100 Postgres `ts_rank_cd` ou ParadeDB BM25.
- Recall@100 par langue : |intersection| / 100.
- Seuil : recall@100 moyen ≥ 0.98. Pas de chute > 0.05 sur une sous-catégorie de cours.

Le plan-maître mentionne un benchmark côte à côte obligatoire (section Risques, ligne 64). La Phase 2 pose l'infrastructure ; la validation fine vs. ParadeDB est en Phase 7.

### 6.6 Diff tool

Un outil `phase2_diff.py` permettant de passer en revue toute divergence détectée :

```
phase2_diff.py --decision-id <id>  # diff SQL entre SQLite et Postgres
phase2_diff.py --table decisions --field json_data --limit 100
phase2_diff.py --citation-source <id>  # diff graphe depuis une décision source
```

---

## 7. Gestion des scrapers pendant la transition

### 7.1 Inventaire (29 scrapers)

Les 29 scrapers listés dans le plan-maître (ligne 20) se trouvent dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scrapers/`. Ils se répartissent en 4 familles :

- **Cours fédérales** : BGer, BVGer, BStGer, BPatGer.
- **Cours cantonales** : 26 scrapers cantonaux, dont certains consolidés via entscheidsuche.ch, d'autres directs (ex. `scrapers/lexfind_cantonal/`).
- **Régulateurs** : FINMA, COMCO, PFPDT, autres autorités.
- **Sources normatives** : fedlex (lois fédérales), lexfind cantonal (lois cantonales).

### 7.2 Modifications à apporter

Tous les scrapers utilisent aujourd'hui le helper central `pipeline.py` (à confirmer par grep sur `INSERT_SQL` dans le repo). La Phase 2 introduit :

1. **Wrapper `dual_writer.py`** qui expose la même signature que l'actuel writer SQLite, mais fait le double-write conditionnel selon `DUAL_WRITE_POSTGRES`.
2. **Patch `pipeline.py`** pour router les INSERT via `dual_writer` au lieu de la connexion SQLite directe.
3. **Aucune modification** des 29 scripts scrapers individuels : ils passent tous par `pipeline.py`.

Si certains scrapers écrivent en direct dans SQLite sans passer par `pipeline.py` (héritage), ils sont patchés un par un avec priorité BGer, BVGer, BStGer (les plus volumineux et les plus critiques pour le graphe).

### 7.3 Monitoring pendant le dual-write

Nouveau dashboard (Grafana ou simple endpoint `/admin/dual_write_status` sur `web_api/main.py`) affichant :

- Rows/min écrits côté SQLite vs Postgres.
- Lag SQLite-Postgres (différence de `COUNT(*)` rafraîchie toutes les 10 min).
- Taille de `dual_write_retry_queue`.
- Temps de la dernière réconciliation.

### 7.4 Réécriture du graphe incrémentale

Les scrapers BGer/BVGer/BStGer/cantonaux écrivent des décisions qui créent de nouvelles arêtes dans `decision_citations` et `decision_statutes`. Pendant le dual-write, ces arêtes sont écrites dans SQLite `reference_graph.db` (build nightly actuel) **et** dans Postgres directement.

Stratégie retenue : **découplage**. Les scrapers n'écrivent pas les arêtes en direct, c'est un job nightly `phase2_build_graph_postgres.py` qui :

1. Lit les décisions nouvelles depuis `public.decisions` (`WHERE scraped_at > last_build`).
2. Ré-applique les extracteurs (`search_stack/reference_extraction.py`) sur `title || regeste || full_text`.
3. Upsert dans `public.decision_citations`, `public.decision_statutes`, `public.statutes`.
4. Pour chaque nouveau `target_ref`, lance la résolution `citation_targets` limitée aux candidats concernés (CTE restreinte au `target_ref` en question).

Cette approche maintient la cohérence avec le build SQLite actuel (même code d'extraction) et permet de fermer la boucle.

---

## 8. Compat dataset HuggingFace

### 8.1 Contrainte invariante

Le dataset HF (`swiss-caselaw` sur HuggingFace) a des consommateurs externes (chercheurs, partenaires). Son schéma PyArrow défini dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/export_parquet.py:31-73` est **gelé**. Toute modification du schéma casse les pipelines downstream.

### 8.2 Adaptation de `export_parquet.py`

La fonction `export_from_db(db_path, output_dir)` (ligne 262) doit être dupliquée en `export_from_postgres(dsn, output_dir)`. Différences :

- Connexion psycopg au lieu de sqlite3.
- Requête `SELECT ... FROM public.decisions WHERE court = %s` avec curseur serveur (`cursor.execute(..., prepare=True)`) pour streaming sans charger 58 GB en mémoire.
- Conversion des types Postgres vers les types PyArrow attendus :
  - `timestamptz → ISO string` (comme aujourd'hui, cf. `normalize_row` ligne 76).
  - `jsonb → json string` pour `json_data` (non exposé dans le schéma HF actuel — voir §8.4).
  - `cited_decisions` reste une string JSON (pas une liste relationnelle).
- Ordre et nullabilités strictement conservés (assert `table.schema.equals(DECISION_SCHEMA)` avant write).

Le driver CLI de `export_parquet.py` gagne un flag `--backend {sqlite, postgres, auto}` (default `auto` : postgres si DSN disponible, sinon sqlite).

### 8.3 Champs manquants côté Postgres

Le schéma HF expose quelques champs absents du schéma SQLite actuel (`judges`, `clerks`, `collection`, `appeal_info`, `bge_reference`, `external_id`, `entscheidsuche_signatur`). Ces champs sont aujourd'hui dérivés de `json_data` (payload scraper) via `normalize_row` lignes 105-114. Deux options :

1. Les matérialiser en colonnes Postgres dédiées.
2. Les laisser dans `json_data::jsonb` et les extraire à la volée dans `export_from_postgres`.

**Décision** : option 2 pour la Phase 2 (pas d'impact sur le schéma SQLite, iso-comportement garanti). La matérialisation en colonnes sera un refactoring Phase 5 si besoin (amélioration recherche facettée).

### 8.4 Gel du schéma HF pendant la Phase 2

Point sensible : le repo a un cron `stats.json` mis à jour quotidiennement (cf. commit `c823164`). L'export Parquet n'est pas dans le même cron, mais il convient de suspendre les releases HF pendant la fenêtre J0 → J+14 pour éviter de publier un dataset généré depuis un Postgres non encore validé. À partir de J+14, l'export bascule définitivement sur Postgres.

### 8.5 Test de non-régression

Un script `phase2_validate_parquet.py` :

1. Exporte le Parquet depuis SQLite (version historique).
2. Exporte le Parquet depuis Postgres (version Phase 2).
3. Ouvre les deux avec PyArrow et compare : même nombre de row groups, même ordre des décisions par court, même schéma, même hash SHA-256 sur les colonnes non-dérivées.

Tolérance : 0 divergence. Si divergence, `export_from_postgres` est patché jusqu'à parité.

---

## 9. Performance et fenêtres de maintenance

### 9.1 Budget temps

- **Passe 1 (decisions + tsv)** : 58 GB de `full_text` à transférer + compresser (TOAST). Sur lien local (SSD NVMe source → NVMe cible), débit attendu 150-300 MB/s effectif en `COPY BINARY`. Estimation : 4-6 h pour le load brut, +1-2 h pour la construction des tsvector (colonne générée stockée calculée par Postgres en arrière-plan via `pg_catalog.pg_tsvector_to_text` et `to_tsvector`). Budget total passe 1 : **6-8 h**.
- **Passe 2 (statutes + articles)** : ~42 MB + ~2.5 MB cantonal = 45 MB. Quelques minutes.
- **Passe 3 (graphe)** : 3.5 GB, ~20 M rows cumulées. Estimation load : 1-2 h. Recalcul `citation_targets` côté Postgres (CTE récursive partitionnée) : 1-3 h selon indexation. Budget total passe 3 : **3-5 h**.
- **Passe 4 (materialien)** : ~2 GB. 30-60 min.

Budget cumulé : **10-15 h** de load + **2-4 h** de validation = **12-19 h** de maintenance active. Fenêtre visée : un weekend (vendredi soir 20h → dimanche matin 12h).

### 9.2 Fenêtre de maintenance

- **Downtime strict** (aucune écriture scraper) : **2 h** maximum (fenêtre de snapshot SQLite cohérent + démarrage passe 1). Les scrapers sont re-démarrés dès que la passe 1 est finie (les passes 2-4 tolèrent des scrapers actifs).
- **Downtime lecteurs** : **0 h**. MCP continue de servir depuis SQLite.

Les utilisateurs finaux (Claude Desktop, Word add-in) ne voient aucune interruption. Seuls les scrapers (automates sans utilisateur) sont suspendus 2 h.

### 9.3 Optimisations spécifiques

- Désactivation de `autovacuum` sur les tables cibles pendant la passe 1 (`ALTER TABLE ... SET (autovacuum_enabled = false)`). Réactivation + `VACUUM ANALYZE` manuel en fin de passe.
- Index secondaires créés **après** le bulk load (`CREATE INDEX CONCURRENTLY` en tâche de fond une fois la passe 1 validée). `CREATE INDEX` non concurrent pendant une fenêtre dédiée si on peut se permettre un write lock court (oui, puisque dual-write pas encore activé).
- `checkpoint_timeout=30min`, `max_wal_size=32GB` temporairement pendant le load pour limiter les checkpoints à chaud.
- `synchronous_commit=off` pendant la passe 1 (acceptable : en cas de crash, on rejoue la passe ; la durabilité n'est pas critique pendant un bulk load idempotent).
- Réactivation de toutes les options production après la passe 4.

### 9.4 Matériel cible

Le cluster Supabase self-hosted a besoin de :

- **Stockage** : au minimum 200 GB SSD (58 GB data + indexation + WAL + marge Phase 3-4).
- **RAM** : 32 GB minimum, 64 GB recommandé pour tsvector build rapide.
- **CPU** : 8 vCPU minimum (parallèle workers Postgres + workers psycopg côté client).

Si le cluster n'est pas dimensionné, la Phase 1 doit avoir résolu le provisioning. Sinon, blocker.

---

## 10. Risques et mitigations

### 10.1 Registre des risques

R1. **Divergence du graphe de citations après recalcul Postgres** (probabilité moyenne, impact élevé).
- Cause : subtilité de la CTE `_resolve_citation_targets` portée de SQLite à Postgres (ordre de tri ROW_NUMBER non déterministe sur égalités, arrondis).
- Mitigation : double-run (transfert brut + recalcul) avec comparaison automatique, garde-fou 0.1 %.

R2. **Corruption de `json_data` à l'ingestion** (probabilité basse, impact moyen).
- Cause : ~800 lignes avec JSON invalide historique.
- Mitigation : scan pré-migration, isolation des lignes corrompues dans `decisions_quarantine`, patch manuel au cas par cas.

R3. **Explosion de la taille des tsvector** (probabilité basse, impact moyen).
- Cause : `full_text` volumineux (ATF de 80 k chars), tsvector stocké peut tripler la taille.
- Mitigation : benchmark sur 10 000 décisions représentatives avant la passe 1 complète, décision éventuelle de ne stocker que `tsv_de/fr/it` sans `tsv_full_text` si trop coûteux.

R4. **Écart tsvector vs FTS5 sur recall** (probabilité moyenne, impact moyen).
- Cause : `unicode61 remove_diacritics 2` non reproduit exactement par `unaccent + lower + to_tsvector`.
- Mitigation : golden set 200 requêtes en benchmark côte à côte, seuil recall@100 ≥ 0.98, plan B ParadeDB BM25 si seuil non atteint.

R5. **Latence MCP post-bascule** (probabilité moyenne, impact élevé).
- Cause : Postgres sans ANN ni cache applicatif dépasse le temps SQLite local.
- Mitigation : phase `shadow` 7 jours avec mesure de latence côte à côte, bascule conditionnée à un SLA `p95 < 2× p95_sqlite` sur les tools critiques.

R6. **Dérive du dual-write** (probabilité moyenne, impact moyen).
- Cause : reconciler buggué, scraper qui oublie le flag, transaction interrompue.
- Mitigation : reconciler toutes les 30 min + dashboard de lag + alerte si lag > 1000 rows ou > 2 h.

R7. **Suppression accidentelle du snapshot SQLite** (probabilité très basse, impact catastrophique).
- Mitigation : snapshot cold dans S3 (ou équivalent stockage objet versionné), conservation minimum 90 jours, chiffré.

R8. **Schéma HF Parquet cassé** (probabilité basse, impact élevé — consommateurs externes).
- Mitigation : test de non-régression `phase2_validate_parquet.py`, gel de release HF pendant J0-J+14, communication externe via release notes.

R9. **Extensions Postgres manquantes ou versions incompatibles** (probabilité basse, impact élevé — bloque la phase).
- Mitigation : vérification avant kickoff Phase 2 via une checklist `phase2_preflight.py` qui énumère les extensions et leurs versions attendues.

R10. **Collation non déterministe sur PK texte** (probabilité basse, impact moyen).
- Cause : `decision_id` est du texte, collations locales différentes selon l'OS → ordre de tri différent entre deux runs, brise les checksums.
- Mitigation : création explicite des colonnes clé en `COLLATE "C"` (collation binaire déterministe). À cadrer en Phase 1, revérifié en Phase 2.

### 10.2 Plan B si invariants non atteints

- Si `decision_citations Postgres < 8.84M × 0.999` : refus de bascule, investigation (probablement des extractions perdues à l'ingestion ou un bug dans la CTE), relance de la passe 3 avec logs verbose.
- Si recall FTS@100 < 0.98 : basculer sur ParadeDB BM25 (déjà prévu par le plan-maître, section Stack cible ligne 25) et relancer le benchmark.
- Si latence MCP post-bascule > 2× SQLite pour > 10 % des requêtes : activation préventive de pg_bouncer transaction pooling + cache LRU applicatif dans MCP ; si toujours insuffisant, repli sur `sqlite` et report de la bascule après Phase 4 (index ANN qui décalerait la charge vers pgvectorscale).

---

## 11. Definition of Done

La Phase 2 est considérée terminée lorsque **tous** les critères ci-dessous sont validés, tracés et signés dans un rapport `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-2-dor-dod-report.md` produit en fin de phase.

### 11.1 Critères techniques

- [ ] Toutes les tables cibles Postgres créées via migrations versionnées (Phase 1), présentes, alignées sur le schéma gelé.
- [ ] Passes 1-4 exécutées avec succès, logs archivés dans `phase2.log`, état final `phase2_state.json` marqué `complete`.
- [ ] Checksums SHA-256 par table : 100 % de match entre SQLite et Postgres pour les tables non-graphe ; match avec tolérance documentée pour les tables graphe.
- [ ] Counts : respect des tolérances du tableau §6.2.
- [ ] Sampling 10 000 + 1 500 biaisé : 100 % de match champ-par-champ.
- [ ] `citation_targets` : 13 M ± 0.5 %, distribution `confidence_score` conforme (voir §6.4).
- [ ] tsvector multilingues construits, index GIN créés, recall@100 golden queries ≥ 0.98.
- [ ] `export_parquet.py` adapté, test de non-régression vert (schéma PyArrow identique, hash de colonnes non-dérivées identique).

### 11.2 Critères opérationnels

- [ ] Dual-write actif, lag observé < 1 000 rows et < 30 min pendant 7 jours consécutifs.
- [ ] Reconciler horaire tourne sans erreur depuis 7 jours.
- [ ] Mode `shadow` lecture MCP actif 7 jours, divergences loggées et analysées, taux de divergence < 0.1 %.
- [ ] Bascule `MCP_READ_BACKEND=postgres` effectuée, latence p95 dans le SLA (≤ 2× SQLite pour tous les tools MCP testés).
- [ ] Word add-in et Claude Desktop vérifiés manuellement : 20 requêtes types chacun, aucune régression fonctionnelle.
- [ ] Snapshot cold SQLite archivé (S3 ou équivalent), checksum publié, procédure de restore documentée dans `docs/runbooks/rollback-phase2.md`.
- [ ] 29 scrapers redémarrés avec `DUAL_WRITE_POSTGRES=false` (écriture Postgres only), monitoring dashboard vert depuis 3 jours.

### 11.3 Critères documentaires

- [ ] Rapport DoD signé (tech lead + ops lead).
- [ ] Runbook rollback écrit et testé une fois en staging.
- [ ] Diff tool `phase2_diff.py` disponible et documenté.
- [ ] Registre des risques à jour, risques R1-R10 résolus ou acceptés explicitement.
- [ ] ADR (Architecture Decision Record) consigné pour les arbitrages majeurs :
  - ADR-P2-01 : scripts Python vs Airflow vs n8n.
  - ADR-P2-02 : transfert brut + recalcul vs recalcul seul pour `citation_targets`.
  - ADR-P2-03 : `json_data` en `jsonb` natif vs texte.
  - ADR-P2-04 : gel `statute_id` vs `article_id` (réconciliation reportée Phase 5).
  - ADR-P2-05 : dual-write SQLite-first vs Postgres-first vs two-phase commit.

### 11.4 Critères de réversibilité

- [ ] La commande `rollback.sh` est testée une fois en staging, temps mesuré < 30 min.
- [ ] Pendant la fenêtre J+14 à J+28, le rollback applicatif (flip feature flag) est exécuté au moins une fois en exercice.

### 11.5 Passage à la Phase 3

La Phase 3 (Chunking SAC) commence **uniquement après** la validation des 4 blocs ci-dessus. Elle se connecte exclusivement à Postgres, n'écrit plus jamais dans SQLite, et construit la table `public.chunks` en aval.

Si un critère DoD n'est pas atteint, la Phase 2 reste ouverte et la Phase 3 est bloquée. Cette discipline est la seule garantie que les fondations sont saines avant d'engager 3 semaines de chunking LLM coûteux.

---

## Annexe A — Liste des scripts livrés en Phase 2

- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_migrate.py` (driver)
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_preflight.py` (vérifs extensions/permissions)
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_checksum.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_sample_diff.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_diff.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_reconciler.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_build_graph_postgres.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/phase2_validate_parquet.py`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/scripts/rollback.sh`
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/dual_writer.py` (wrapper écriture)
- Patchs : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/pipeline.py`, `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` (feature flag `MCP_READ_BACKEND`), `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py` (endpoint `/admin/dual_write_status`), `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/export_parquet.py` (backend postgres).

## Annexe B — Timeline synthétique

| Semaine | Activités |
|---|---|
| S1 | Preflight, scripts driver, staging schema, dry-run sur subset 10k décisions |
| S1-S2 | Passes 1-4 complètes, validation, index, checksums |
| S2 | Mise en place dual-write, reconciler, dashboard |
| S2-S3 | Dual-write observé 7 jours, mode `shadow` MCP activé |
| S3 | Bascule lecture MCP Postgres, monitoring serré |
| S3 (post) | 2 semaines de stabilisation, puis arrêt dual-write, archivage SQLite |

Durée cible : **2-3 semaines actives** + 2 semaines de stabilisation (cohérent avec le plan-maître, ligne 49).
