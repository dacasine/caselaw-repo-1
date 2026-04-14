# Phase 1 — Cadrage + schéma Supabase (parité SQLite)

> Sous-plan détaillé. Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Durée visée : 3 semaines. Livrables : contrats d'interface gelés (OpenAPI + JSON schema MCP), suite de tests golden (200 requêtes), jeu de migrations SQL Postgres avec parité 1-pour-1 des cinq SQLite actuels et squelette des tables PA-RAG futures.

---

## 1. Objectifs et critères de succès mesurables

### 1.1 Objectifs

1. **Geler les contrats avant toute migration** — produire la source de vérité du comportement actuel (OpenAPI 3.1 pour les ~30 routes REST FastAPI, JSON schema pour les 23 tools MCP) afin qu'aucune phase ultérieure (2 à 9) ne puisse régresser sans que le diff ne soit détecté.
2. **Construire un harnais de tests "boîte noire"** — 200 requêtes reproductibles couvrant les 23 tools et les principales routes REST, avec snapshots JSON canonisés, rejouable après chaque phase.
3. **Concevoir le schéma Postgres cible** — parité sémantique exacte avec les 5 SQLite (`decisions.db`, `reference_graph.db`, `statutes.db`, `cantonal_laws.db`, `materialien.db`) tout en anticipant les tables PA-RAG (sans peupler) : `decision_metadata_parag`, `decision_authority`, `chunks`, `chunk_embeddings`, `chunk_summaries`.
4. **Arbitrer les extensions Postgres** — choix documenté entre ParadeDB (BM25 natif) et tsvector/GIN, pgroonga en optionnel pour le CJK/DE composé, pgvector + pgvectorscale obligatoires, pg_trgm pour fuzzy, apache_age en optionnel (GraphRAG natif).
5. **Stratégie de partitionnement** — partitionner `decisions` par année de décision (déclarative `RANGE`) et `chunks` par hash du `decision_id` (déclarative `HASH`, 16 partitions) pour absorber 965 k décisions + ~30 M chunks futurs.
6. **Définir les invariants d'intégrité** — identifiants canoniques (format ATF `BGE_141_III_123`, format BGer `1C_123/2024`), FK `ON DELETE RESTRICT` sur toute arête du graphe de citations, contraintes `CHECK` sur les énumérations (`outcome`, `decision_type`, `language`).
7. **Préparer la stratégie RLS** — politiques par défaut en lecture publique (données déjà publiques), écriture restreinte à un rôle `service_role_ingest`, séparation rôle `anon` / `authenticated` / `service_role`.

### 1.2 Critères de succès mesurables

| Critère | Cible | Mesure |
|---|---|---|
| Fichier OpenAPI généré | 100 % des routes FastAPI couvertes | Diff contre `web_api/main.py` nul (script d'introspection) |
| Fichier MCP schema | 23/23 tools documentés (nom, params, types, descriptions) | Comparaison contre registry MCP en runtime |
| Tests golden | 200 requêtes, 23 tools couverts, toutes routes publiques | Rejeu en CI < 5 min, 100 % green sur la baseline SQLite |
| Migrations SQL | N fichiers numérotés, idempotents, rollback testé | Application sur DB vierge + rollback = DB vierge |
| Extensions validées | Liste figée + version minimale documentée | Smoke test d'installation sur Supabase self-hosted |
| Parité colonne | 0 colonne perdue des 5 SQLite | Script de mapping exhaustif vérifié |
| Taille projetée | Estimation disque ±15 % du réel observé phase 2 | Calcul basé sur row-count × avg-row-size PG vs SQLite |
| RLS spec | Policies documentées par table (4 rôles) | Matrice role × table × verbe complète |

### 1.3 Hors périmètre phase 1 (explicite)

- Aucune donnée n'est migrée (phase 2).
- Aucune Edge Function n'est écrite (phase 6).
- Aucun embedding n'est calculé (phase 4).
- Aucun chunk n'est produit (phase 3).
- Le schéma PA-RAG est **déclaré** (DDL) mais pas peuplé ni indexé ANN.

---

## 2. Inventaire précis de ce qui doit être migré

### 2.1 Vue d'ensemble des 5 SQLite sources

| Fichier SQLite | Taille | Volumétrie | Rôle | Schéma canonique |
|---|---|---|---|---|
| `~/.swiss-caselaw/decisions.db` | ~58 GB | 965 k+ décisions | Corpus principal + FTS5 | `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/db_schema.py` |
| `~/.swiss-caselaw/reference_graph.db` | ~3.5 GB | 8.84 M edges citation + 11.34 M liens décision→statut | Graphe de citations | Inférer depuis `search_stack/build_reference_graph.py` |
| `~/.swiss-caselaw/statutes.db` | ~42 MB | ~5 500 lois fédérales (fedlex) | Textes légaux fédéraux | Inférer depuis scrapers `fedlex_*` |
| `~/.swiss-caselaw/cantonal_laws.db` | ~26 MB | ~26 000 actes cantonaux | Textes légaux cantonaux | Inférer depuis `lexfind_cantonal_*` |
| `~/.swiss-caselaw/materialien.db` | variable | Botschaften, débats parlementaires | Travaux préparatoires | À introspect via `PRAGMA table_info` |

Une des premières livraisons de la phase est le **rapport d'introspection** de chaque SQLite (script dédié, exécution locale, sortie markdown + JSON) produisant pour chaque base : liste des tables, colonnes avec types, indexes, triggers, vues, contraintes, FK internes, row counts, tailles par table.

### 2.2 `decisions.db` — mapping sémantique vers Postgres

La table `decisions` du SQLite actuel (cf. `db_schema.py` lignes 10-39) comporte 27 colonnes. Mapping proposé :

- Table Postgres cible : `public.decisions`, **partitionnée RANGE sur `decision_date`** (partitions annuelles : `decisions_y<YYYY>`, plus une partition `decisions_undated` pour les lignes sans date et `decisions_default` pour le futur).
- Clé primaire composite Postgres : `(decision_date, decision_id)` (la règle de partitionnement impose que la colonne de partitionnement soit dans la PK). Contrainte `UNIQUE (decision_id)` garantie globalement via un index unique sur toutes les partitions (ou via une contrainte d'exclusion applicative si l'unicité globale pose problème).
- Colonnes :
  - `decision_id TEXT` → `text` en PG, `NOT NULL`. Validation `CHECK` via regex séparant les formats ATF (`BGE_\d+_[IVX]+_\d+`), BGer (`\d[A-Z]_\d+/\d{4}`), cantonal (préfixe canton + numéro).
  - `court TEXT NOT NULL` → `text NOT NULL`, indexé (B-tree), ajout d'une contrainte `CHECK` énumérée (enum applicative via table `courts` référentielle).
  - `canton TEXT NOT NULL` → `text NOT NULL`, FK sur table `cantons` (référentielle, 27 lignes : 26 cantons + `CH` pour le fédéral).
  - `chamber TEXT` → `text NULL`.
  - `docket_number TEXT NOT NULL` / `docket_number_2 TEXT` → idem PG, index B-tree sur le premier.
  - `decision_date TEXT` / `publication_date TEXT` → **conversion en `date`** (les valeurs SQLite sont déjà au format `YYYY-MM-DD` d'après l'usage existant). Lignes sans date : allouées à `decisions_undated`.
  - `language TEXT NOT NULL` → `text NOT NULL CHECK (language IN ('de','fr','it','rm','en'))`, indexé.
  - `title TEXT`, `legal_area TEXT`, `regeste TEXT`, `abstract_de/fr/it TEXT`, `full_text TEXT` → `text`.
  - `decision_type TEXT` → `text` + `CHECK` énumérée (`judgment`, `order`, `decree`, `opinion`, `other`).
  - `outcome TEXT` → `text` + `CHECK` selon le plan-maître (`irrecevabilité | rejet | admission | admission_partielle` pour recours ; `admission | admission_partielle | rejet` pour première instance). Gestion d'une valeur `unknown` pour le legacy non encore classifié.
  - `source_url TEXT`, `pdf_url TEXT` → `text`.
  - `cited_decisions TEXT` → conservé **tel quel** en `text` (payload JSON stringifié existant), mais **les arêtes exploitables vivent dans `decision_edges`** (cf. `reference_graph.db`). Cette colonne reste une copie de compatibilité.
  - `scraped_at TEXT` → `timestamptz`.
  - `source TEXT`, `source_id TEXT`, `source_spider TEXT` → `text`.
  - `content_hash TEXT` → `text`, indexé B-tree (dédoublonnage).
  - `json_data TEXT` → **`jsonb`** en PG (gain substantiel : requêtes JSON, index GIN, compression TOAST).
  - `canonical_key TEXT` → `text`, indexé B-tree.

Au total : **27 colonnes préservées**, dont 1 conversion date, 2 conversions timestamptz, 1 conversion jsonb, les 23 autres restent `text`.

### 2.3 `decisions.db` — indexes et FTS5 → Postgres

Les 8 indexes B-tree SQLite (`idx_decisions_court|canton|date|language|docket|chamber|type|canonical`) sont reproduits à l'identique en Postgres (B-tree locaux par partition + index global `UNIQUE (decision_id)`).

Le `VIRTUAL TABLE decisions_fts USING fts5(... tokenize='unicode61 remove_diacritics 2')` couplé aux triggers `AFTER INSERT|DELETE|UPDATE` est remplacé par **l'une des deux stratégies retenues en section 3** :

- **Option A (recommandée prioritaire)** : colonne matérialisée `fts_tsv tsvector` alimentée par trigger ou `GENERATED ALWAYS AS` composé de `setweight(to_tsvector(<config>, coalesce(title,'')),'A') || setweight(to_tsvector(<config>, coalesce(regeste,'')),'B') || setweight(to_tsvector(<config>, coalesce(full_text,'')),'C')`, avec une `<config>` dépendant de `language` (dispatch via `case` ou via colonne fonctionnelle). Index GIN sur `fts_tsv`.
- **Option B (ParadeDB)** : index BM25 natif `pg_search` sur `title | regeste | full_text` avec tokenizer `icu` multilingue. Pas de colonne matérialisée ; le BM25 est stocké dans un index externe géré par l'extension.

Dans les deux cas, les triggers SQLite disparaissent (remplacés par la sémantique de l'option choisie — `GENERATED` ou l'index BM25 géré par ParadeDB).

### 2.4 Tables de coverage (`db_schema.py` lignes 95-183)

Mapping 1-pour-1 sans partitionnement (faibles volumes) :

- `coverage_targets` → conservée (8 colonnes, PK `source_key`). Conversions : `active INTEGER` → `boolean`, `created_at/updated_at TEXT DEFAULT (datetime('now'))` → `timestamptz DEFAULT now()`.
- `source_snapshots` → id `bigserial`, `expected_ids_json TEXT` → `jsonb`. Index unique composite préservé.
- `source_discoveries` → id `bigserial`, `stub_json TEXT` → `jsonb`, `status` → `text CHECK` énumérée (`discovered|fetched|failed|skipped`).
- `source_fetch_attempts` → id `bigserial`, `status` énuméré, `error_type` énuméré (`network|parse|auth|quota|other`).
- `gap_queue` → id `bigserial`, contrainte `UNIQUE(source_key, decision_year, decision_id)` préservée, `status` énuméré (`open|retry|resolved|abandoned`).

Tous les `CREATE INDEX IF NOT EXISTS` listés dans le schéma canonique sont reproduits à l'identique.

### 2.5 `reference_graph.db` — graphe de citations (8.84 M + 11.34 M edges)

Script source : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py` (à introspecter). Tables attendues (à confirmer par introspection) :

- Table `decision_edges` (attendu ~8.84 M lignes). Colonnes présumées : `src_decision_id TEXT`, `dst_decision_id TEXT`, `edge_type TEXT` (citation positive / négative / neutre), `context TEXT` (extrait du paragraphe citant), `weight REAL`, éventuellement `position INT`.
  - Mapping PG : `src text NOT NULL`, `dst text NOT NULL`, `edge_type text NOT NULL CHECK (edge_type IN (...))`, `context text`, `weight double precision`, `position int`. PK composite `(src, dst, edge_type, position)` ou surrogate `bigserial`.
  - **FK critiques** : `src` et `dst` référencent `decisions(decision_id)` avec `ON DELETE RESTRICT` (invariant non-négociable du plan-maître : « Aucune suppression cascade des citations »).
  - Complication : la FK vers une table partitionnée Postgres requiert que la colonne cible soit dans un index unique global — garanti par le `UNIQUE (decision_id)` évoqué en 2.2.
  - Indexes : B-tree sur `src`, sur `dst`, composite `(dst, edge_type)` pour calculer l'autorité entrante (PageRank, in-degree, base pour `decision_authority`).
  - Partitionnement envisagé : `HASH (src)` 16 partitions pour paralléliser les jointures en calcul de PageRank.
- Table `decision_statute_edges` (attendu ~11.34 M). Colonnes présumées : `decision_id TEXT`, `statute_ref TEXT` (ex. `SR 220 art. 41`), `canonical_statute_id TEXT`, `confidence REAL`, `context TEXT`, `position INT`.
  - FK `decision_id` → `decisions`. FK `canonical_statute_id` → `statutes.article_id` (table ci-dessous), `ON DELETE RESTRICT`.
  - Index B-tree `(decision_id)`, `(canonical_statute_id)`.

Si l'introspection révèle des tables secondaires (ex. table de statistiques d'autorité cachée, vues matérialisées, liste de `known_statutes`), elles sont recréées à l'identique en PG.

### 2.6 `statutes.db` (~42 MB, ~5 500 lois fédérales)

Tables attendues (à introspect) : probablement `statutes` (métadonnées loi : SR number, titre, langues), `articles` (un enregistrement par article individuel, avec `statute_id`, `article_number`, `text_de`, `text_fr`, `text_it`, `version_date`).

Mapping :

- `statutes` : `statute_id text PRIMARY KEY` (SR number), `title_de/fr/it text`, `entry_into_force date`, `last_amended date`, métadonnées Fedlex.
- `articles` : `article_id text PRIMARY KEY` (ex. `SR-220-art-41`), FK `statute_id` vers `statutes`, colonnes multilingues `text_de/fr/it`, `version_date`.
- Colonne FTS équivalente : `fts_tsv tsvector` par article (cf. stratégie FTS 2.3).
- Note : `statutes.db` est un cas d'usage **pas partitionné** (petite taille).

### 2.7 `cantonal_laws.db` (~26 000 actes cantonaux)

Structure symétrique à `statutes` mais avec dimension `canton` :

- `cantonal_acts(act_id text PK, canton text NOT NULL, title text, language text, ...)`, FK `canton` vers `cantons`.
- `cantonal_articles(article_id text PK, act_id text FK, article_number text, text text, ...)`.
- Index composite `(canton, act_id)`, `(canton, title)`.
- Pas de partitionnement (volume modeste).

### 2.8 `materialien.db` (Botschaften + débats)

Structure à introspect. Modèle probable :

- `materialien(mat_id text PK, mat_type text CHECK IN ('botschaft','debat','rapport_comm','feuille_federale',...), related_statute_id text, title text, date date, full_text text, source_url text)`.
- FK optionnelle `related_statute_id` → `statutes.statute_id` (ON DELETE SET NULL — ici on tolère la nullité, ce n'est pas une arête de citation).
- Colonne FTS.

### 2.9 Tables référentielles nouvelles (non dans SQLite — gain de normalisation)

Ces tables n'existent pas encore dans les SQLite mais sont introduites dès la phase 1 pour supporter les contraintes d'intégrité :

- `cantons(code text PK, name_de text, name_fr text, name_it text)` : 27 lignes seed (26 cantons + `CH`).
- `courts(court_id text PK, canton text FK, name_de text, name_fr text, name_it text, court_level text CHECK IN ('federal','cantonal_supreme','cantonal_lower','administrative','specialized'))` : seed à partir d'un inventaire unique des valeurs observées dans `decisions.court`.
- `legal_areas(area_id text PK, label_de text, label_fr text, label_it text)` : seed à partir de `decisions.legal_area` distinct.

Ces tables sont **populées en fin de phase 2** mais déclarées en phase 1 pour que les FK des migrations soient cohérentes dès le DDL initial.

### 2.10 Squelettes PA-RAG (déclarés, non peuplés)

Anticipés par le plan-maître (phases 3-5). DDL créé en phase 1, aucune donnée insérée :

- `chunks(chunk_id uuid PK, decision_id text FK, considerant_id text, chunk_order int, char_start int, char_end int, token_count int, content text, language text, summary text, created_at timestamptz)`. **Partitionnée HASH sur `decision_id`, 16 partitions**. Index `(decision_id, chunk_order)`, GIN sur `content` via `fts_tsv` généré.
- `chunk_embeddings(chunk_id uuid PK FK → chunks, embedding vector(768), model text NOT NULL, model_version text, created_at timestamptz)`. Index DiskANN (phase 4, pas phase 1).
- `chunk_summaries(chunk_id uuid PK FK, summary_de text, summary_fr text, summary_it text, llm_model text, llm_version text, prompt_hash text)`.
- `decision_metadata_parag(decision_id text PK FK, sort_affaire text CHECK, decision_stage text CHECK IN ('first_instance','appeal','cassation','constitutional'), procedural_posture text, enriched_at timestamptz, llm_model text)`.
- `decision_authority(decision_id text PK FK, pagerank_raw double precision, pagerank_time_decayed double precision, in_degree int, out_degree int, authority_score double precision, computed_at timestamptz, lambda double precision)`.
- `retrieval_eval_runs`, `retrieval_eval_queries`, `retrieval_eval_results` : triplet pour la phase 9, déclaré en phase 1 pour que la suite de tests golden puisse écrire ses résultats dès le départ.

### 2.11 Récapitulatif mapping

| SQLite source | Tables PG cibles | Transformations clés |
|---|---|---|
| `decisions.db` | `decisions` (partitionnée), `coverage_targets`, `source_snapshots`, `source_discoveries`, `source_fetch_attempts`, `gap_queue` | FTS5 → tsvector OU ParadeDB ; TEXT dates → `date`/`timestamptz` ; `json_data` TEXT → `jsonb` ; triggers supprimés au profit de `GENERATED` ou d'index BM25 |
| `reference_graph.db` | `decision_edges` (partitionnée HASH), `decision_statute_edges` | FK `ON DELETE RESTRICT` ajoutées (invariant) ; index optimisés pour PageRank |
| `statutes.db` | `statutes`, `statute_articles` | Colonnes multilingues ; FTS par article |
| `cantonal_laws.db` | `cantonal_acts`, `cantonal_articles` | Dim `canton` FK ; FTS |
| `materialien.db` | `materialien` | FK optionnelle vers `statutes` |
| (nouveau) | `cantons`, `courts`, `legal_areas` | Référentiels seedés phase 2 |
| (nouveau PA-RAG) | `chunks` (partitionnée HASH), `chunk_embeddings`, `chunk_summaries`, `decision_metadata_parag`, `decision_authority`, `retrieval_eval_*` | DDL seul, peuplement phases 3-5 et 9 |

---

## 3. Décisions d'architecture (trade-offs explicites)

### 3.1 ParadeDB (`pg_search` BM25) vs tsvector/GIN

| Axe | `tsvector` + GIN (Option A) | ParadeDB `pg_search` (Option B) |
|---|---|---|
| Fidélité au scoring FTS5 actuel | Moyenne — `ts_rank_cd` est proche mais pas identique à BM25 SQLite | Élevée — BM25 natif, paramètres `k1`, `b` réglables |
| Support multilingue DE/FR/IT | Bon via configs `german`, `french`, `italian` ; dispatch par `language` | Bon via tokenizer ICU ; gestion composés DE moins fine sans pgroonga |
| Maturité écosystème | Extension core, ≥ PG 12, battle-tested | Jeune (extension tierce), bouge vite, stabilité en production à valider |
| Intégration Supabase self-hosted | Native (aucune action) | Requiert build custom image ou installation manuelle |
| Coût opérationnel | Nul | Maintenance extension, upgrades |
| Performance sur 965 k × full_text | GIN adéquat, taille index importante (~15-25 % du corpus) | BM25 natif optimisé, potentiellement plus rapide sur top-k |
| Compat phase 7 (hybride BM25 + ANN + RRF) | Nécessite calcul BM25 applicatif ou conversion rank | BM25 direct, RRF plus propre |

**Décision retenue** : **tsvector + GIN comme baseline de phase 1**, avec `pg_search` activé en parallèle sur un sous-corpus (1 % échantillon) pour benchmark côte à côte. La bascule BM25-natif est une décision à prendre en **phase 7**, pas phase 1. Raison : phase 1 doit minimiser les dépendances exotiques pour que les tests golden mesurent la parité, pas l'évolution du scoring.

Conséquence : la phase 1 crée les colonnes `fts_tsv` et les index GIN, et **ajoute en préparation** les DDL `pg_search` commentés ou sous feature flag.

### 3.2 pgroonga (optionnel)

Pertinence théorique : meilleur support des mots composés allemands (ex. `Schadenersatzanspruch` décomposé) et des tokenizers N-gram utiles pour CJK — non applicable ici. Les décisions CH sont en DE/FR/IT, pas en CJK.

**Décision** : **pas d'adoption en phase 1**. Réévaluer en phase 7 si la qualité du rappel sur requêtes DE composées est insuffisante. Coût de rétrogradation : nul (aucun DDL n'en dépend).

### 3.3 pgvector + pgvectorscale (StreamingDiskANN)

Non discutable : requis par phase 4. **Décision phase 1** : installer les deux extensions, créer la colonne `embedding vector(768)` dans `chunk_embeddings`, mais **ne pas créer l'index DiskANN** (coût disque + temps de build inutiles tant que la table est vide).

Version minimale : `pgvector` ≥ 0.7.0 (support `halfvec` optionnel pour phase 4), `pgvectorscale` ≥ 0.3.0 (StreamingDiskANN).

### 3.4 pg_trgm

Utilisé pour fuzzy matching sur `docket_number`, `title`, résolution d'alias de court names.

**Décision** : activé, indexes GIN trigram sur `decisions.title`, `decisions.docket_number`, `statutes.title_de/fr/it`.

### 3.5 apache_age (GraphRAG natif)

Phase 8 prévoit « GraphRAG léger (Postgres-native) » avec CTE récursifs et vues matérialisées — **pas apache_age**.

**Décision** : **pas d'adoption**. Les 8.84 M edges sont parfaitement requêtables via récursion SQL sur `decision_edges` avec index approprié. `apache_age` ajouterait une surface de maintenance et une duplication du graphe.

### 3.6 Partitionnement

**`decisions`** — RANGE par année de `decision_date`. Raisons :
- Les requêtes utilisateurs filtrent très souvent par fenêtre temporelle (cinq dernières années, « depuis 2010 »).
- Les partitions anciennes deviennent read-only (archivage, compression `ALTER TABLE ... SET (parallel_workers=4)`).
- Facilite la rétention différentielle (ex. dump exhaustif par année).
- Conséquence sur PK : `(decision_date, decision_id)` plutôt que `(decision_id)` seul. Mitigation : index unique global sur `decision_id` recréé hors partitionnement via `CREATE UNIQUE INDEX ... ON ONLY` + `ATTACH`, ou via `UNIQUE NULLS NOT DISTINCT` sur chaque partition + contrainte applicative.
- Partitions prévues : `decisions_undated` (sans date), `decisions_y1874` à `decisions_y<année_courante>`, `decisions_default` (filet). ~150 partitions, acceptable pour PG 15+.

**`chunks`** — HASH sur `decision_id`, 16 partitions. Raisons :
- 965 k × ~30 chunks ≈ 29 M chunks attendus ; HASH équilibre la charge d'écriture et les lectures aléatoires.
- Les jointures `chunks ⋈ decisions` bénéficient du partition-wise join si les deux sont partitionnées par `decision_id` — mais `decisions` est RANGE sur date, donc pas de partition-wise join gratuit. Accepté : le filtre principal sur `chunks` est `WHERE decision_id = ?` (résolution en une partition via hash).

**`decision_edges`** — HASH sur `src`, 16 partitions. Raisons identiques à `chunks`. PageRank requiert des scans complets, parallélisables par partition.

**Pas de partitionnement** : `statutes`, `cantonal_*`, `materialien`, `chunk_embeddings` (un seul index ANN global est plus performant qu'un index par partition en pgvectorscale), tables référentielles.

### 3.7 RLS (Row Level Security)

Matrice par défaut :

| Table | `anon` | `authenticated` | `service_role_ingest` | `service_role_enrich` |
|---|---|---|---|---|
| `decisions` (toutes partitions) | SELECT | SELECT | SELECT, INSERT, UPDATE | SELECT |
| `decision_edges` | SELECT | SELECT | INSERT, UPDATE (no DELETE) | SELECT |
| `decision_statute_edges` | SELECT | SELECT | INSERT, UPDATE | SELECT |
| `statutes`, `*_articles`, `cantonal_*`, `materialien` | SELECT | SELECT | INSERT, UPDATE | — |
| `chunks`, `chunk_*`, `decision_metadata_parag`, `decision_authority` | SELECT | SELECT | — | INSERT, UPDATE |
| `coverage_*`, `source_*`, `gap_queue` | — | — | INSERT, UPDATE, SELECT | — |
| `retrieval_eval_*` | — | SELECT | — | INSERT, UPDATE, SELECT |

Policies codifiées dès phase 1 (migrations), même si les rôles applicatifs ne sont activés qu'en phase 6.

### 3.8 Identifiants canoniques et FK

- Format `decision_id` validé par `CHECK` regex multi-alternatives (ATF | BGer | cantonal).
- `ON DELETE RESTRICT` systématique sur toute FK entrante dans `decisions`, `statutes`, `chunks` (invariant plan-maître).
- `ON DELETE CASCADE` autorisé uniquement pour : `decisions → decisions_fts` (si extracted col), `chunks → chunk_embeddings`, `chunks → chunk_summaries` (un chunk qui disparaît implique que son embedding/summary deviennent orphelins sans valeur).
- `ON DELETE SET NULL` pour la FK optionnelle `materialien.related_statute_id`.

### 3.9 Indexes FTS multilingues DE/FR/IT

Stratégie retenue : **colonne générée conditionnelle par langue**.

- Création d'une colonne `fts_tsv tsvector GENERATED ALWAYS AS (case language when 'de' then to_tsvector('german', coalesce(title,'') || ' ' || coalesce(regeste,'') || ' ' || coalesce(full_text,'')) when 'fr' then to_tsvector('french', ...) when 'it' then to_tsvector('italian', ...) else to_tsvector('simple', ...) end) STORED`.
- Index GIN sur `fts_tsv` par partition (propagation automatique via partitionnement déclaratif).
- Pour les requêtes cross-langue, l'application envoie `plainto_tsquery(<config>, <input>)` avec dispatch côté middleware.
- Les abstracts `abstract_de/fr/it` sont indexés dans une colonne séparée `fts_abstracts_tsv` mélangeant les trois avec `setweight` pour booster les résultats aux abstracts présents.

### 3.10 Type des identifiants UUID vs text

- `chunk_id` : `uuid` (v5 déterministe, namespace = decision_id, name = position, pour reproductibilité phase 3).
- `decision_id`, `article_id`, `statute_id`, `mat_id`, `act_id` : `text` (formats métier existants, invariant).

---

## 4. Stratégie de gel des contrats

### 4.1 Extraire l'OpenAPI depuis FastAPI

FastAPI expose nativement `/openapi.json`. Stratégie :

1. **Introspection runtime** — démarrer `web_api/main.py` en mode test, GET `/openapi.json`, snapshot versionné `contracts/openapi/openapi-v1-frozen.json`.
2. **Enrichissement** — certaines routes peuvent avoir des réponses non typées (`-> dict`). Ajouter des `response_model` pydantic dans FastAPI **avant snapshot** si nécessaire, pour que l'OpenAPI soit exploitable comme contrat. Cette revue est un livrable de la phase 1.
3. **Freeze** — le fichier `openapi-v1-frozen.json` est commité ; tout changement en phase 6 produit un diff semver'é (`openapi-v2-edge-functions.json`), revue manuelle obligatoire.
4. **Fixtures de réponse** — pour chaque route, capturer 2-5 réponses réelles en JSON canonisé (tri des clés, normalisation des dates dynamiques via placeholders). Ces fixtures alimentent les tests golden (section 5).
5. **Diff CI** — script Python qui, à chaque phase, relance l'introspection et compare le nouveau `openapi.json` au frozen. Règles : ajout de route OK (warn), retrait de route NOK (fail), changement de signature NOK (fail sauf si semver bump explicite).

Livrable : répertoire `contracts/openapi/` contenant le JSON frozen, un fichier `CHANGELOG.md`, et un script `validate_openapi_parity.py`.

### 4.2 Capturer les signatures MCP (23 tools)

MCP stdio n'a pas de standard OpenAPI, mais le SDK expose `list_tools()` retournant nom, description, JSON schema de chaque tool.

1. **Introspection** — lancer `mcp_server.py` en mode `list_tools_only` (à ajouter si absent), sérialiser le registry en `contracts/mcp/mcp-tools-v1-frozen.json`.
2. **Format attendu** — tableau de 23 entrées `{name, description, inputSchema (JSON Schema draft-7), outputSchema (si typé)}`.
3. **Enrichissement** — pour chaque tool, documenter manuellement les effets de bord (lecture seule, mutations, appels réseau externes), la criticité, la latence typique observée.
4. **Fixtures** — pour chaque tool, capturer 3-5 invocations réelles avec entrées et sorties JSON canonisées. Ces fixtures sont les inputs de la suite golden.
5. **Diff CI** — identique OpenAPI : script Python, retrait de tool interdit, changement de schema interdit sans bump de version.

Livrable : `contracts/mcp/mcp-tools-v1-frozen.json`, `contracts/mcp/fixtures/<tool_name>/<case_id>.{in,out}.json`, `validate_mcp_parity.py`.

### 4.3 Gouvernance du gel

- Tag git `contracts-v1-frozen` posé à la fin de la phase 1.
- Toute PR modifiant `web_api/main.py` ou `mcp_server.py` doit soit ne rien changer au contrat, soit fournir un diff semver explicite + justification + mise à jour des fixtures.
- Le script CI est bloquant à partir de la phase 2.

---

## 5. Stratégie de tests golden

### 5.1 Objectif

Construire un harnais **boîte noire** qui, sans connaître l'implémentation, rejoue 200 requêtes contre le système et compare la réponse à un snapshot de référence. Ce harnais sert de filet de sécurité pour les phases 2 à 9 : chaque bascule est validée en rejouant la suite.

### 5.2 Choix des 200 requêtes — allocation

| Catégorie | Nombre | Couverture |
|---|---|---|
| Tools MCP — lecture simple (get by id, metadata, list) | 60 | 10 tools × 6 cas |
| Tools MCP — recherche FTS | 40 | Tool `search`, variantes langue, opérateurs, tranches de date |
| Tools MCP — graphe (citations entrantes/sortantes, chemins) | 25 | 5 tools × 5 cas |
| Tools MCP — statuts/cantonal/materialien | 25 | Lookup article, recherche croisée |
| Tools MCP — outils utilitaires (coverage, stats, health) | 20 | Cas OK + edge cases |
| REST `/search` et endpoints publics | 15 | Recherche, pagination, filtres |
| REST endpoints de métadonnées (courts, cantons, stats) | 10 | Listes, agrégats |
| Edge cases (requêtes vides, caractères spéciaux, unicode DE/FR/IT, décision inconnue, date future, pagination extrême) | 5 | Robustesse |
| **Total** | **200** | **23/23 tools + 30 routes touchées** |

### 5.3 Principes de sélection

- **Diversité linguistique** : chaque catégorie contient au moins 2 cas DE, 2 FR, 2 IT, et un mixte.
- **Diversité juridictionnelle** : ATF, BGer, TAF, TPF + 3 cantons représentatifs (ZH, GE, TI pour couvrir 3 langues).
- **Diversité temporelle** : décisions de <1950, 1950-1999, 2000-2019, 2020+.
- **Ancrage réel** : pas de cas synthétiques. Toutes les requêtes proviennent soit de logs applicatifs existants, soit d'une sélection manuelle par un juriste, soit des cas de test déjà présents dans `tests/` du repo.
- **Stabilité** : les requêtes ne référencent que des décisions publiées et des lois stables (pas de décisions scraped récemment qui pourraient disparaître).

### 5.4 Format du snapshot

- Fichier par cas : `tests/golden/<category>/<case_id>.yaml`.
- Contenu : `name`, `description`, `tool_or_route`, `request` (args ou query params), `expected_response` (JSON canonisé, clés triées, champs volatils remplacés par `<<TIMESTAMP>>`, `<<LATENCY_MS>>`).
- Normalisation : scores FTS arrondis à 4 décimales, ordre de tri déterministe secondaire sur `decision_id` en cas d'égalité de score, troncature de `full_text` à 500 chars dans le snapshot (le corps complet reste vérifié par hash).
- Hash de contrôle : chaque snapshot inclut un champ `expected_response_sha256` calculé sur la réponse brute normalisée avant troncature, pour vérifier que les champs tronqués n'ont pas dérivé.

### 5.5 Tolérances

Certains tests doivent tolérer des variations (ex. BM25 vs FTS5 produira des scores différents). Règles :
- **Strict** (par défaut) : réponse byte-identique post-normalisation.
- **Set-equals** : vérifier l'ensemble des `decision_id` retournés, ignorer l'ordre (pour top-k où les égalités sont fréquentes).
- **Top-k prefix** : vérifier que les `k` premiers résultats sont un sur-ensemble du top-`k*1.2` attendu (tolérance de re-ranking).
- **Score-tolerant** : scores ±5 % acceptés.

La catégorie de tolérance est déclarée dans chaque YAML (`tolerance: strict|set_equals|top_k_prefix|score_tolerant`). À la fin de la phase 1 : **toutes les 200 requêtes sont en `strict`** (baseline SQLite). Les tolérances s'assouplissent phase par phase si justifié par un ADR.

### 5.6 Harnais d'exécution

- Runner Python `pytest` + `pytest-xdist` pour parallélisme.
- Fixtures : démarre soit `mcp_server.py` (phase 1-2), soit les Edge Functions (phase 6+) ; le code de test ne connaît pas l'implémentation.
- Sortie : rapport JUnit + diff HTML pour chaque échec.
- CI : déclenché sur chaque PR, bloquant. Budget temps cible : < 5 min parallélisé.

### 5.7 Capture initiale

Procédure de capture baseline (exécutée une fois en fin de phase 1) :

1. Geler la version courante de `mcp_server.py` et `web_api/main.py` (tag git).
2. Exécuter les 200 requêtes contre le système actuel, sérialiser les réponses en mode `--update-snapshots`.
3. Revue manuelle par un juriste d'un échantillon de 20 cas (validation fonctionnelle).
4. Commit du répertoire `tests/golden/`.
5. Tag `golden-v1-baseline`.

---

## 6. Ordre de création des migrations SQL

### 6.1 Règles transverses

- **Numérotation** : `supabase/migrations/<YYYYMMDDHHMMSS>_<slug>.sql`. Ordre strictement croissant.
- **Idempotence** : chaque migration utilise `CREATE ... IF NOT EXISTS`, `ALTER ... IF NOT EXISTS` quand disponible, `DO $$ ... $$` pour les éléments non-idempotents nativement.
- **Rollback** : pour chaque `<slug>.sql`, un fichier `<slug>.down.sql` est produit, testé en CI (up → down → up sur base vierge, attendu : état final identique).
- **Granularité** : une migration = une unité logique (une table + ses index + ses policies ; ou un set de référentiels).
- **Pas de DML** en phase 1 (aucune donnée réelle insérée, seulement des seeds de référentiels). Les DML de migration de données sont phase 2.

### 6.2 Ordre logique

1. **Extensions** (`001_extensions`) — `CREATE EXTENSION` pour `pgcrypto`, `pg_trgm`, `btree_gin`, `vector`, `vectorscale`, `unaccent`. Test d'installation sur Supabase self-hosted.
2. **Rôles et schemas** (`002_roles_schemas`) — création schemas `public` (par défaut), `parag` (tables PA-RAG), `eval` (tables golden/retrieval eval). Rôles `service_role_ingest`, `service_role_enrich` (si absents).
3. **Tables référentielles** (`003_refs`) — `cantons`, `courts`, `legal_areas`. Seeds minimaux (cantons seulement, les 26 + `CH`).
4. **Schéma `decisions` partitionné** (`004_decisions`) — table parent, contraintes CHECK, index uniques globaux, fonction de création de partitions. Création des partitions annuelles via script (boucle `DO $$`).
5. **FTS decisions** (`005_decisions_fts`) — colonnes générées `fts_tsv`, `fts_abstracts_tsv`, index GIN (par partition via propagation déclarative).
6. **Tables coverage** (`006_coverage`) — `coverage_targets`, `source_snapshots`, `source_discoveries`, `source_fetch_attempts`, `gap_queue`.
7. **Graphe citations** (`007_reference_graph`) — `decision_edges` (partitionnée HASH 16), `decision_statute_edges`. FK `ON DELETE RESTRICT`.
8. **Statuts fédéraux** (`008_statutes`) — `statutes`, `statute_articles` + FTS.
9. **Lois cantonales** (`009_cantonal_laws`) — `cantonal_acts`, `cantonal_articles` + FTS.
10. **Matériaux** (`010_materialien`) — `materialien` + FTS.
11. **Squelettes PA-RAG** (`011_parag_skeleton`) — `parag.chunks` (partitionnée HASH 16), `parag.chunk_embeddings`, `parag.chunk_summaries`, `parag.decision_metadata_parag`, `parag.decision_authority`. Pas d'index ANN (phase 4).
12. **Tables eval** (`012_eval_tables`) — `eval.retrieval_eval_runs`, `eval.retrieval_eval_queries`, `eval.retrieval_eval_results`.
13. **Policies RLS** (`013_rls_policies`) — `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` + `CREATE POLICY` pour chaque table, selon la matrice 3.7.
14. **Vues et fonctions utilitaires** (`014_views_functions`) — vues de compatibilité si utiles pour la couche REST (ex. `v_decisions_public` masquant `json_data`), fonctions de normalisation de `decision_id`.
15. **Contraintes différées et validations finales** (`015_final_constraints`) — index de vérification, `ANALYZE`, commentaires `COMMENT ON` pour documentation auto.

Chaque migration est accompagnée de son `.down.sql` et d'un test unitaire SQL (fichier `.test.sql` exécuté via `psql --set ON_ERROR_STOP=on`).

### 6.3 Rollback — stratégie spécifique

- Ordre de rollback : inverse de l'application.
- `DROP TABLE ... CASCADE` **interdit** dans les rollbacks de tables référencées par FK sans suppression préalable des tables dépendantes. L'ordre inverse garantit l'absence de cascade involontaire.
- Extensions : `DROP EXTENSION IF EXISTS ... CASCADE` seulement dans le rollback de `001_extensions`, précédé d'une vérification qu'aucune colonne `vector` ne reste (sinon fail).
- Test CI : `up-all ; down-all ; verify(empty schema) ; up-all ; verify(final schema)` sur chaque push modifiant `supabase/migrations/`.

---

## 7. Dépendances externes et prérequis infra

### 7.1 Versions

- **PostgreSQL** : 16.x (support `pg_search`, `pgvectorscale`, partition-wise joins améliorés, `JSON_TABLE`). Minimum 15.x accepté si 16 indisponible sur l'image Supabase self-hosted.
- **Supabase** : version self-hosted la plus récente stable (à verrouiller en début de phase 1). Image Docker officielle ou build custom si `pg_search`/`vectorscale` non embarqués.
- **pgvector** : ≥ 0.7.0.
- **pgvectorscale** : ≥ 0.3.0.
- **pg_search (ParadeDB)** : ≥ 0.10 si retenu en parallèle benchmark.
- **pg_trgm**, **unaccent**, **btree_gin**, **pgcrypto** : inclus core.
- **SQLite** côté source : 3.40+ (compat FTS5 confirmée).
- **Python** outillage : 3.11+, `pytest` 8+, `httpx`, `mcp` SDK.
- **Node/TS** : 20+ (utilisé phase 6, préparé phase 1 pour scripts de validation OpenAPI via `@redocly/openapi-core`).

### 7.2 Ressources infra — dimensionnement

- **RAM DB** : minimum 32 GB (phase 1 suffit avec 16 GB, mais dimensionner pour phase 2 où les 58 GB sont chargés). Recommandé 64 GB pour phase 4 (DiskANN in-memory partiel).
- **Disque DB** :
  - Corpus decisions SQLite 58 GB → Postgres estimé **85-110 GB** (facteur 1.5-1.9 dû à MVCC, TOAST, index GIN FTS, index B-tree multiples). Prévoir 150 GB pour marge phase 1-2.
  - Graphe citations 3.5 GB SQLite → ~7-10 GB PG avec indexes.
  - Statuts/cantonal/materialien : ~150 MB PG.
  - **Chunks + embeddings (phase 3-4)** : 29 M chunks × (1 KB text + 768 × 4 B embedding) ≈ 30 GB texte + 90 GB vecteurs + 40-60 GB index DiskANN → **prévoir 200 GB supplémentaires** pour phases 3-4, mais **non provisionné en phase 1**.
  - **Budget disque phase 1-2** : 200 GB. **Budget total à terme (phase 9)** : ~500 GB.
- **CPU** : 8 vCPU minimum pour ingestion parallèle phase 2 ; phase 1 dev suffisante à 4 vCPU.
- **IOPS** : SSD NVMe requis (l'ingestion phase 2 lit 58 GB et écrit 100+ GB).

### 7.3 Environnements

- **Dev local** : Docker Compose avec Supabase self-hosted + volumes bind-mount.
- **Staging** : clone complet du setup, utilisé pour la capture des fixtures golden et le benchmark.
- **Prod** : non touché en phase 1.

### 7.4 Outils annexes

- `pg_dump` / `pg_restore` 16.x pour les rollbacks.
- `sqlite-utils` CLI pour introspection des 5 SQLite.
- Script d'introspection maison (Python) produisant le rapport markdown de schéma actuel.
- `openapi-diff` (Redocly ou outil équivalent) pour le diff CI.

---

## 8. Risques spécifiques à cette phase et mitigations

| # | Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Introspection incomplète des 5 SQLite (tables oubliées, triggers cachés, vues non listées) | Moyenne | Élevé (régression silencieuse phase 2) | Script d'introspection exhaustif (`PRAGMA` complet + `sqlite_master` + `sqlite_stat1`) + revue manuelle croisée par deux personnes |
| R2 | Format `decision_id` non-homogène dans le corpus existant (anciennes entrées avec IDs non canoniques) | Élevée | Moyen (CHECK regex bloque la migration phase 2) | Audit préalable : requête d'énumération des formats observés, élargissement du regex ou table de remappage documentée |
| R3 | Dates non-ISO dans `decision_date` / `publication_date` (formats `DD.MM.YYYY`, nulls stringifiés `""`) | Élevée | Moyen | Audit préalable, fonction de coercition idempotente testée, colonne `decision_date_raw` préservée en archive |
| R4 | Partitionnement RANGE sur date impose date dans la PK, brise ORM existants attendus sur `decision_id` seul | Moyenne | Moyen | Index unique global `UNIQUE (decision_id)` + vues de compat masquant la PK composite ; documentation claire aux consommateurs |
| R5 | FK `ON DELETE RESTRICT` vers table partitionnée : PG impose certaines contraintes (index unique global requis) | Moyenne | Élevé | Décidé dès le DDL : `UNIQUE (decision_id)` indexé globalement ; tests de création sur DB vierge |
| R6 | Extension `pg_search` ou `pgvectorscale` absente de l'image Supabase self-hosted | Moyenne | Moyen | Validation dès la semaine 1 ; plan B : image Docker custom documentée |
| R7 | OpenAPI FastAPI incomplet (routes sans `response_model`) | Élevée | Moyen | Revue systématique + ajout des response_models avant snapshot, tracée dans un ADR |
| R8 | 200 requêtes insuffisantes pour couvrir les régressions phase 6 (Edge Functions) | Moyenne | Élevé | Dès phase 1 : mesurer la couverture sur `mcp_server.py` (quels codepaths sont exercés) ; ajouter requêtes si < 70 % |
| R9 | Snapshots golden trop fragiles (casse au moindre changement d'ordre) | Élevée | Faible | Normalisation rigoureuse + tolérances graduées ; recapture sur règle explicite |
| R10 | Coût disque sous-estimé (indexes GIN FTS multilingues plus gros que prévu) | Moyenne | Moyen | Benchmark sur échantillon 1 % en semaine 2, extrapolation, ajustement du sizing |
| R11 | Les triggers FTS5 de SQLite masquent une logique applicative (ex. transforms avant insertion) | Faible | Moyen | Relecture des triggers en section `db_schema.py` + diff comportemental via tests golden |
| R12 | Enum `outcome` comporte des valeurs legacy non documentées (`unknown`, ``, `NULL`) | Élevée | Faible | Audit énumération complet + valeur `unknown` explicitement autorisée |
| R13 | La partition `decisions_undated` devient volumineuse (décisions sans date fiable) et déséquilibre les plans | Moyenne | Faible | Campagne de normalisation des dates en phase 2 ; stats dédiées |
| R14 | Fuite de secret dans un snapshot golden (URL interne, token) | Faible | Élevé | Passage de chaque snapshot par un scanner de secrets avant commit |
| R15 | Divergence entre `mcp_server.py` et `web_api/main.py` sur un même tool/route (même fonctionnalité, signatures différentes) | Élevée | Moyen | Tests golden couvrent les deux surfaces séparément, un ADR documente les écarts tolérés |
| R16 | `pg_search` activé en parallèle perturbe les plans d'exécution des tests golden | Faible | Moyen | Extension installée mais aucun index créé tant que benchmark non lancé |

---

## 9. Definition of Done

La phase 1 est **terminée** quand **toutes** les conditions suivantes sont vérifiées :

### 9.1 Contrats

- [ ] Fichier `contracts/openapi/openapi-v1-frozen.json` commité, couvrant 100 % des routes exposées par `web_api/main.py`.
- [ ] Fichier `contracts/mcp/mcp-tools-v1-frozen.json` commité, couvrant les 23 tools de `mcp_server.py`.
- [ ] Script `validate_openapi_parity.py` en place, exécuté en CI, passe.
- [ ] Script `validate_mcp_parity.py` en place, exécuté en CI, passe.
- [ ] Tag git `contracts-v1-frozen` posé.

### 9.2 Tests golden

- [ ] 200 cas YAML commités sous `tests/golden/`, répartis selon la matrice section 5.2.
- [ ] Baseline capturée contre l'environnement SQLite actuel, tous les cas en vert, tolérance `strict`.
- [ ] Harnais `pytest` exécutable en < 5 min en parallèle.
- [ ] Revue manuelle de 20 cas signée par un relecteur juriste (PR commentée).
- [ ] Tag git `golden-v1-baseline` posé.

### 9.3 Migrations SQL

- [ ] 15 fichiers de migration numérotés et commités, chacun avec son `.down.sql`.
- [ ] Test CI `up → down → up` passe sur DB vierge.
- [ ] DDL pour tables PA-RAG (`parag.chunks`, `parag.chunk_embeddings`, `parag.chunk_summaries`, `parag.decision_metadata_parag`, `parag.decision_authority`) créé mais aucun peuplement.
- [ ] Partitions `decisions` créées (années 1874 à année courante + `undated` + `default`).
- [ ] Partitions HASH sur `chunks` et `decision_edges` créées (16 chacune).
- [ ] Colonnes `fts_tsv` générées et index GIN créés sur `decisions`, `statutes`, `statute_articles`, `cantonal_acts`, `cantonal_articles`, `materialien`.
- [ ] RLS activée sur toutes les tables, matrice de policies codifiée.
- [ ] Seeds `cantons` (27 lignes) insérés ; `courts` et `legal_areas` vides mais table créée.

### 9.4 Documentation et ADR

- [ ] Rapport d'introspection des 5 SQLite commité sous `docs/plan/phase-1-introspection-sqlite.md`.
- [ ] ADR-001 : choix tsvector vs ParadeDB (avec plan de benchmark phase 7).
- [ ] ADR-002 : choix partitionnement RANGE annuel sur `decisions`.
- [ ] ADR-003 : pas d'apache_age, pas de pgroonga en phase 1.
- [ ] ADR-004 : stratégie FTS multilingue (colonne `GENERATED` par langue).
- [ ] ADR-005 : format `decision_id` canonique et règles de validation.
- [ ] ADR-006 : partitionnement HASH 16 sur `chunks` et `decision_edges`.
- [ ] ADR-007 : politique RLS par défaut + matrice rôles.
- [ ] Diagramme ERD généré (outil au choix) commité en PNG + source.

### 9.5 Infra et CI

- [ ] Stack Supabase self-hosted lancée localement via Docker Compose, migrations appliquées avec succès.
- [ ] Extensions `pgcrypto`, `pg_trgm`, `btree_gin`, `unaccent`, `vector`, `vectorscale` installées et validées.
- [ ] CI GitHub Actions (ou équivalent) exécute : lint SQL, test up/down migrations, validation OpenAPI parity, validation MCP parity, tests golden.
- [ ] Temps CI total < 10 min.

### 9.6 Sizing et sign-off

- [ ] Estimation disque phase 2 documentée (benchmark sur échantillon 1 % effectué).
- [ ] Prérequis infra phase 2 validés avec l'ops (RAM, disque, IOPS, Postgres version).
- [ ] Sign-off des 8 autres sous-plans phase 2-9 sur les contrats gelés (aucun sous-plan ne demande une modif tardive des contrats sans ADR).

### 9.7 Critères d'arrêt

Si l'une des conditions suivantes est rencontrée en fin de phase 1, **la phase 2 ne démarre pas** :

- Plus de 5 tools MCP ou 5 routes REST sans fixture golden exploitable.
- Plus de 10 % des `decision_id` actuels ne valident pas le regex canonique et aucun plan de remédiation n'est documenté.
- Extension manquante bloquante sans plan B validé.
- Benchmark disque démontre un dépassement > 50 % du budget provisionné.

---

## Annexes — références

- Schéma SQLite canonique : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/db_schema.py`
- Construction du graphe : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`
- Chunker actuel (à remplacer en phase 3) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py`
- Vectorisation actuelle (à remplacer en phase 4) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py`
- MCP serveur monolithique : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py`
- REST FastAPI : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py`
- Export Parquet (contrat externe à préserver) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/export_parquet.py`
- Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`
- Rapport PA-RAG : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`
