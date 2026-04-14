# Phase 8 — GraphRAG léger (Postgres-native, sans Neo4j)

> Sous-plan détaillé du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md` (ligne 56).
> Durée cible : **2 semaines**.
> Rapport source : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md` (sections 5 et 5.2).
> Code existant de référence : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`.

Cette phase s'exécute **après** la Phase 7 (retrieval hybride + authority rerank) et **avant** la Phase 9 (évaluation + observabilité). Elle consomme le graphe de citations déjà matérialisé lors de la Phase 2 (migration SQLite → Postgres des tables `decisions`, `decision_citations`, `citation_targets`, `decision_statutes`, `statutes`) et l'enrichissement PA-RAG de la Phase 5 (PageRank temporel, validity_status, classification du sort).

---

## 1. Objectifs et critères de succès

### 1.1 Objectifs fonctionnels

L'objectif principal de la Phase 8 est de **transformer le graphe de citations passif** (8.84 millions d'edges, 11.34 millions de liens décision→statut, aujourd'hui interrogé saut par saut via des jointures ad-hoc) **en un véritable moteur GraphRAG** capable de :

1. Remonter des **chaînes d'instance (Instanzenzug)** : tribunal de première instance → cour d'appel cantonale → Tribunal fédéral → éventuelle CourEDH. Typiquement 2 à 4 sauts.
2. Identifier les **leading cases d'un domaine** par agrégation de centralité (PageRank temporel déjà calculé en Phase 5) filtrée par thématique (`legal_topics`), par branche du droit et par validité (non-overruled).
3. Dégager des **tendances jurisprudentielles** en parcourant les chaînes de citation sur fenêtres temporelles glissantes et en détectant les ruptures (un arrêt cité 50 fois/an qui s'effondre brutalement → signal d'overruling implicite).
4. Détecter des **cycles** dans le graphe de citation (arrêts qui se citent mutuellement, boucles de renvoi après cassation) sans explosion combinatoire.
5. Optionnellement propager `validity_status` le long des arêtes `OVERRULES` : si A overrule B et que C suit explicitement la ratio de B, lever un flag de re-vérification humaine sur C.

### 1.2 Critères de succès quantitatifs

| Critère | Cible | Méthode de mesure |
|---|---|---|
| Latence p50 traversée 3 sauts depuis un ATF quelconque | < 200 ms | Requêtes `find_appeal_chain` et `find_leading_cases` sur 1 000 arrêts tirés au hasard |
| Latence p99 traversée 3 sauts | < 800 ms | Idem, percentile 99 |
| Latence détection cycles (profondeur ≤ 4) sur sous-graphe de 50 k arrêts | < 1,5 s | Benchmark dédié |
| Couverture Instanzenzug sur BGer 2015-2025 | ≥ 80 % | Cross-check avec `is_prior_instance` déjà présent dans `decision_citations` |
| Coût storage vues matérialisées vs gain latence | ratio ≤ 1 GB / 100 ms gagné sur p50 | Benchmark avant/après REFRESH |
| Exactitude agrégation leading cases (top-20 par domaine) | ≥ 95 % accord inter-annotateur vs baseline manuelle | Evaluation Phase 9 |

### 1.3 Non-objectifs

- **Pas de traversée > 5 sauts** : au-delà, la pertinence juridique s'effondre (un arrêt n'est pas "proche" de son petit-cousin cité transitivement).
- **Pas de détection automatisée d'overruling** (reste Phase 5, via signaux textuels + LLM).
- **Pas de visualisation graphe** dans cette phase (prévu pour un livrable UI ultérieur, hors plan).
- **Pas de GDS (Graph Data Science) type Louvain, community detection, node2vec** : inscrits comme extensions v2 si apache_age est activé.

---

## 2. Décision architecturale : Postgres vs Neo4j vs apache_age

### 2.1 Rappel du contexte

L'ensemble du stack cible (Phase 1) est **Supabase self-hosted** : Postgres 15+ avec `pgvector`, `pgvectorscale` (StreamingDiskANN), `pg_trgm`, ParadeDB BM25 ou `tsvector` fallback. Introduire une base de graphe séparée signifierait :

- Une seconde source de vérité à synchroniser (dual-write, cohérence éventuelle).
- Une seconde stack ops (backup, monitoring, upgrades, HA).
- Un second langage de requête (Cypher) à maîtriser par l'équipe.
- Un coût licence (Neo4j Enterprise) ou des limitations opérationnelles (Community edition : pas de clustering, pas de backups en ligne).

### 2.2 Trois options comparées

#### Option A — Postgres pur (CTE récursifs + vues matérialisées) **[retenue]**

- **Expressivité** : SQL récursif couvre 90 % des cas GraphRAG légers (traversée bornée en profondeur, agrégations le long de chemins, détection de cycles via `VISITED` set). Insuffisant pour : plus courts chemins pondérés (Dijkstra/A*), centralité graphe-globale (déjà calculée hors-ligne en Phase 5), communautés.
- **Performance** : sur 8.84 M edges avec indexes bidirectionnels, 3 sauts restent sous la seconde si le fan-out est contrôlé (`LIMIT` par niveau, filtres sur `confidence_score` et `court`). Au-delà de 4 sauts sans garde-fou, explosion combinatoire garantie.
- **Ops** : zéro surcoût, tout est dans le même cluster Supabase. Backups, PITR, monitoring unifiés.
- **Coût** : nul (hors storage des vues matérialisées estimé à 2-5 GB).

#### Option B — Neo4j séparé

- **Expressivité** : Cypher natif, GDS mature (PageRank, Louvain, Node2Vec), visualisation Bloom.
- **Performance** : excellentes traversées profondes (> 5 sauts), index natifs sur relations.
- **Ops** : +++ complexe. Dual-write cohérent avec Postgres exige CDC (Debezium) ou batch ETL nocturne. Les 8.84 M edges tiennent confortablement en Community edition, mais sans HA/clustering.
- **Coût** : Enterprise ≈ 36 k USD/an par cluster ; Community gratuit mais ops fragile.
- **Verdict** : overkill pour les use cases Phase 8, à reconsidérer seulement si un use case GDS (community detection des "écoles" jurisprudentielles, embeddings graphe pour retrieval) justifie le coût.

#### Option C — Extension `apache_age` sur Postgres (Cypher dans Postgres)

- **Expressivité** : Cypher, mais pas toutes les fonctions GDS. Maturité moyenne (v1.5 stable depuis 2023).
- **Performance** : bénéficie du stockage Postgres, mais couche Cypher ajoute un overhead parsing/planification. Moins optimisé que Neo4j natif sur traversées profondes.
- **Ops** : une extension de plus à maintenir. Compat `pgvectorscale` à vérifier (pas de conflit connu en 2025, mais peu de retours de production cumulant les deux).
- **Coût** : nul.
- **Verdict** : option de repli si les CTE récursifs deviennent trop verbeux ou si on veut exposer du Cypher à des utilisateurs externes. **Non activée en v1**, documentée comme escape hatch.

### 2.3 Décision

**Option A retenue** pour la v1 de la Phase 8. Justifications :

1. **Principe KISS** : on ne sort de Postgres que si un use case est bloqué, ce qui n'est pas le cas.
2. **Parité invariant** (plan-maître ligne 36) : les 8.84 M edges existent déjà dans SQLite `reference_graph.db` et seront migrés 1:1 en Phase 2. Aucune transformation de schéma n'est nécessaire pour les CTE.
3. **Réversibilité** : si un besoin graphe avancé émerge (GDS), on peut activer `apache_age` sans migrer les données (il lit les tables Postgres existantes via wrappers).
4. **Coût d'opportunité** : les 2 semaines budgetées pour la Phase 8 suffisent largement en Option A ; Option B ou C mobiliserait 4-6 semaines (setup + dual-write + tests parité).

### 2.4 Trigger de réévaluation

On re-considère Neo4j ou `apache_age` **si et seulement si** au moins un des seuils suivants est franchi en production :

- Latence p99 d'une traversée 3 sauts > 2 s malgré tuning (indexes, partitioning, vues matérialisées).
- Un use case produit exige community detection ou node embeddings sur le graphe entier (> 100 k nœuds).
- Les CTE récursifs dépassent 150 lignes SQL pour une requête et deviennent non-maintenables.

---

## 3. Schéma graphe conceptuel adapté au droit suisse

### 3.1 Entités (nœuds)

1. **Decision** — toute décision de toute juridiction (BGer, BGE historique, BVGer, BStGer, cantons, régulateurs). Clé : `decision_id`. Attributs utiles pour traversées : `court`, `canton`, `decision_date`, `language`, `sort` (classification Phase 5), `validity_status`, `pagerank_temporal` (Phase 5), `docket_norm`.
2. **Court** — juridiction. Attributs : `code` (bger, bvger, bstger, bge, ta_ge, ta_vd, etc.), `level` (fédéral / cantonal / régulateur), `instance_order` (entier 1=première instance, 2=appel, 3=TF), `language_scope`.
3. **LegalProvision** — article de loi, fédéral (fedlex) ou cantonal. Clé composite `law_code + article + paragraph`. Attributs : `source` (fedlex / lexfind_cantonal), `in_force_from`, `in_force_to`, `replaced_by` (FK auto-référentielle pour historicité).
4. **LegalCode** — texte législatif entier (CO, CC, LDIP, LPC, Cst., LCR, LEI, etc.). Grouper les `LegalProvision`.
5. **LegalConcept** — notion juridique (bonne foi, lien de causalité adéquate, pouvoir d'appréciation, fardeau de la preuve, etc.). **Absent du schéma actuel**, à construire en Phase 5 (taxonomie) et à remplir en Phase 8.
6. **DoctrineSource** *(optionnel, v2)* — article de doctrine, commentaire bâlois/zurichois, manuel. Hors périmètre v1.

### 3.2 Relations (arêtes)

| Arête | Sémantique | Multiplicité | Source de données |
|---|---|---|---|
| `Decision -[DECIDED_BY]-> Court` | Rendu par | N:1 | Colonne `decisions.court` |
| `Court -[APPEALS_TO]-> Court` | Recours possible vers | N:1 ou N:N | Table statique à créer (10-20 lignes) |
| `Decision -[CITES]-> Decision` | Cite | N:N, pondéré par `mention_count` + `confidence_score` | Table `decision_citations` + `citation_targets` |
| `Decision -[OVERRULES]-> Decision` | Renverse explicitement | N:N, rare | Dérivée d'`enrichissement PA-RAG Phase 5 (signaux textuels + LLM) |
| `Decision -[FOLLOWS]-> Decision` | Suit la ratio de | N:N | Dérivée Phase 5 (sous-type de `CITES` avec annotation positive) |
| `Decision -[DISTINGUISHES]-> Decision` | Distingue | N:N | Dérivée Phase 5 (sous-type de `CITES` avec annotation contrastive) |
| `Decision -[IS_PRIOR_INSTANCE_OF]-> Decision` | Est l'instance antérieure de | 1:1 ou 1:N | Flag `is_prior_instance` déjà présent dans `decision_citations` |
| `Decision -[INTERPRETS]-> LegalProvision` | Interprète l'article | N:N | Table `decision_statutes` |
| `Decision -[CONCERNS]-> LegalConcept` | Porte sur la notion | N:N | **À construire** Phase 5 (LLM topic extraction) |
| `LegalProvision -[PART_OF]-> LegalCode` | Fait partie de | N:1 | Table `statutes` + `fedlex` metadata |
| `LegalProvision -[AMENDS]-> LegalProvision` | Modifie | N:1 | fedlex historique (hors v1) |
| `LegalConcept -[BROADER_THAN]-> LegalConcept` | Hyperonymie | N:N, DAG | **À construire** Phase 5 (taxonomie éditoriale + LLM) |
| `LegalConcept -[RELATED_TO]-> LegalConcept` | Co-occurrence | N:N | Dérivée statistique Phase 5 |

### 3.3 Particularités suisses reflétées

- **Trilinguisme** : une même `LegalProvision` peut être citée en DE (`Art. 8 BV`), FR (`art. 8 Cst.`), IT (`art. 8 Cost.`). La normalisation `statute_id` actuelle (`search_stack/reference_extraction.py`) agrège déjà les trois. Le schéma Postgres héritera de cette normalisation.
- **BGE vs BGer** : une même décision existe en deux identités (arrêt brut BGer ``4A_...`` et sa publication ATF ``147 III 65`` le cas échéant). Le schéma actuel les traite comme deux nœuds distincts reliés logiquement par `docket_number`. Phase 8 ajoutera une relation explicite `Decision -[PUBLISHED_AS]-> Decision` pour éviter les doubles comptages dans les agrégations.
- **Instanzenzug cantonal → fédéral** : la chaîne typique est `TPI cantonal → Tribunal cantonal → BGer`. La table `APPEALS_TO` doit coder ces chemins par canton.
- **Régulateurs (COMCO, FINMA, WEKO, etc.)** : certains n'ont pas de recours direct au BGer mais passent par le BVGer. À coder dans `APPEALS_TO`.

---

## 4. Mapping sur tables Postgres existantes

### 4.1 Déjà disponible (issu de Phase 2)

| Table Postgres cible | Source SQLite | Volume | Couvre l'arête |
|---|---|---|---|
| `decisions` | `decisions.db::decisions` | 965 k+ lignes | `Decision` (nœud), `DECIDED_BY` (via colonne `court`) |
| `decision_citations` | `reference_graph.db::decision_citations` | 8.84 M | `CITES` (brut, target_ref non résolu) |
| `citation_targets` | `reference_graph.db::citation_targets` | ~6-7 M (70-80 % résolution) | `CITES` (résolu, avec `confidence_score`) |
| `decision_statutes` | `reference_graph.db::decision_statutes` | 11.34 M | `INTERPRETS` |
| `statutes` | `reference_graph.db::statutes` | ~100 k | `LegalProvision` (nœud) |
| `fedlex_laws` | `statutes.db` | ~5 500 | `LegalCode` (nœud fédéral) |
| `cantonal_laws` | `cantonal_laws.db` | ~26 000 | `LegalCode` (nœud cantonal) |

### 4.2 À créer en Phase 8

| Table | Rôle | Volumétrie estimée | Source |
|---|---|---|---|
| `courts` | Catalogue des juridictions avec `instance_order`, `level`, `parent_court_id`, `canton` | 50-80 lignes | Seed manuel + consolidation avec scrapers existants |
| `appeals_to` | Arête `APPEALS_TO` entre cours (DAG) | 80-150 lignes | Seed manuel, validé par un praticien |
| `legal_concepts` | Nœud `LegalConcept` (taxonomie) | 500-2 000 nœuds | Produit Phase 5 (topic extraction LLM + curation) |
| `decision_concepts` | Arête `CONCERNS` | 5-10 M | Produit Phase 5 |
| `concept_hierarchy` | Arête `BROADER_THAN` (DAG) | 500-3 000 | Produit Phase 5 |
| `decision_overrules` | Arête `OVERRULES` explicite | 1 000-5 000 | Produit Phase 5 (signaux + LLM) |
| `decision_citation_annotations` | Sous-type de `CITES` : `FOLLOWS` / `DISTINGUISHES` / `MENTIONS` / `CRITICIZES` | ~2-3 M (annotation partielle) | Produit Phase 5 |
| `decision_publications` | Arête `PUBLISHED_AS` (BGer ↔ BGE) | ~30 k | Dérivée des `docket_number` BGE |

### 4.3 Absent et laissé hors v1

- **Temporalité fine des `LegalProvision`** (versions d'un article dans le temps). Nécessiterait un snapshot historique fedlex. Out-of-scope Phase 8.
- **Arête `AMENDS` entre articles** (loi modificatrice → loi modifiée). Nécessiterait parsing fedlex profond.
- **Arête `CITES` de doctrine** (décisions citant doctrine). Suppose l'ingestion doctrine, hors scope v1.

### 4.4 Contrainte d'intégrité invariante

Le plan-maître (ligne 37) impose la **préservation des 8.84 M edges** avec checksums pré/post migration et **aucune suppression cascade** pendant la migration. Les tables créées en Phase 8 sont **additives** et ne touchent pas aux edges existantes.

---

## 5. Vues matérialisées

### 5.1 Principes directeurs

- **Matérialiser** ce qui est lent à calculer à la volée ET stable sur 24 h.
- **Ne pas matérialiser** ce qui change trop fréquemment ou dont le paramétrage est ouvert (ex : top-K leading cases par *n'importe quel* concept).
- **Indexer** les vues matérialisées autant que les tables sources (sinon on ne gagne rien).
- **REFRESH CONCURRENTLY** obligatoire en production (sinon lock AccessExclusive de plusieurs minutes).

### 5.2 Vues prévues

1. **`v_citation_graph`** — vue matérialisée canonique du graphe de citations résolues. Union de `citation_targets` enrichie par les attributs des nœuds source/cible (court source, court cible, date source, date cible, `confidence_score`, `is_prior_instance`, `mention_count`). C'est la table "chaude" sur laquelle tous les CTE récursifs s'appuient. Deux colonnes supplémentaires dérivées : `time_gap_days` (différence `source_date - target_date`), `is_forward_citation` (1 si source postérieure à cible, 0 sinon ; utile pour filtrer les anomalies temporelles). Volume estimé : ≈ 7 M lignes, ≈ 1,5 GB avec indexes.

2. **`v_instanzenzug`** — vue matérialisée des chaînes d'instance complètes, pré-calculées en partant du BGer vers le bas. Pour chaque arrêt BGer, colonnes : `bger_decision_id`, `appeal_cantonal_decision_id`, `first_instance_decision_id`, `chain_length`, `canton`. Alimenté par un CTE one-shot exploitant `is_prior_instance=1` et la topologie `APPEALS_TO`. Volume estimé : ≈ 300 k-500 k lignes. Refresh : hebdomadaire (peu de nouveaux arrêts BGer/jour).

3. **`v_leading_cases_by_topic`** — top-100 leading cases par `legal_topic` (au sens Phase 5), combinant `pagerank_temporal` décroissant, `in_degree` pondérée par `confidence_score`, filtrée `validity_status = 'valid'` et `court IN ('bge','bger','bvger')`. Volume : ≈ 50-200 topics × 100 = 5-20 k lignes. Refresh : quotidien (dépend du PageRank Phase 5 refresh nocturne).

4. **`v_statute_decision_counts`** — pour chaque `statute_id`, nombre de décisions l'interprétant, réparti par cour et par décennie. Alimente `find_decisions_by_statute` et l'équivalent REST. Volume : ≈ 100 k × 10 décennies × 5 cours = ~500 k lignes compactes. Refresh : quotidien.

5. **`v_citation_cycles_candidates`** — pré-calcul des paires `(A, B)` où `A` cite `B` ET `B` cite `A` (cycles de longueur 2, trivial) ; et, en étape 2, des triplets où un cycle de longueur 3 existe. Permet de détecter rapidement les cycles connus sans re-parcourir le graphe. Volume estimé : quelques milliers de paires, quelques centaines de triplets. Refresh : hebdomadaire.

6. **`v_concept_co_occurrence`** — co-occurrence de `LegalConcept` dans une même décision, pondérée. Alimente les requêtes "concepts proches de X". Volume : ≈ 50 k-200 k lignes selon taille taxonomie. Refresh : hebdomadaire.

### 5.3 Stratégie de refresh

- **Quotidien (nuit, 02:00-04:00 UTC)** : `v_citation_graph` (incrémentiel si possible), `v_leading_cases_by_topic`, `v_statute_decision_counts`.
- **Hebdomadaire (dimanche nuit)** : `v_instanzenzug`, `v_citation_cycles_candidates`, `v_concept_co_occurrence`.
- **À la demande** : lors d'un backfill majeur (nouveaux scrapers, re-enrichissement PA-RAG), tout est refresh dans l'ordre topologique.
- **CONCURRENTLY** systématique. Prérequis : chaque vue a une contrainte UNIQUE.

### 5.4 Coût de maintenance

Sur la base de 58 GB de données source (plan-maître ligne 12), les vues matérialisées Phase 8 ajoutent **estimation 3-5 GB** de stockage, dominés par `v_citation_graph` (≈ 1,5 GB) et `v_statute_decision_counts` (≈ 500 MB). Coût CPU refresh : 5-15 min/nuit sur un worker dédié. Impact write locks sur tables sources : nul grâce à `CONCURRENTLY`.

---

## 6. CTE récursifs : patterns de requête

### 6.1 Pattern 1 — Instanzenzug remontant (find_appeal_chain)

**Entrée** : un `decision_id` de n'importe quelle cour.
**Sortie** : la chaîne ordonnée `{décision initiale → appel → TF → CourEDH}` sous forme de liste de `decision_id` avec métadonnées (cour, date, sort).
**Pattern conceptuel** :

- Point d'entrée : le nœud donné.
- À chaque itération, joindre `decision_citations` (ou `citation_targets`) filtrée sur `is_prior_instance = 1` dans un sens, et symétriquement chercher les arrêts dont le nœud courant est `is_prior_instance`.
- Garde-fou : profondeur max **5** (couvre TPI → TCant → BGer → CourEDH avec marge).
- Garde-fou : `VISITED` set accumulé dans un array `decision_id[]` pour éviter les cycles.
- Garde-fou : branchement ≤ 3 par niveau (un arrêt a rarement plus de 3 instances antérieures ou postérieures "légitimes").
- Terminaison : quand plus aucun voisin `is_prior_instance=1` n'est trouvé, OU profondeur atteinte.

**Bénéfice vs implementation naïve** : aujourd'hui, `find_appeal_chain` dans `mcp_server.py` fait 3-4 requêtes séquentielles Python-side avec jointures en boucle. Un CTE unique Postgres fait l'équivalent en un plan optimisé, estimation 5-10× plus rapide.

### 6.2 Pattern 2 — Chaînes de citation en avant (influence d'un arrêt)

**Entrée** : un `decision_id`, une profondeur `k ∈ {1, 2, 3}`.
**Sortie** : ensemble des arrêts `D` tels qu'il existe un chemin de citation de longueur `≤ k` depuis l'arrêt donné vers `D`, avec annotation `path_length` et `path_confidence` (produit des `confidence_score` le long du chemin).
**Pattern conceptuel** :

- Même squelette récursif.
- Filtre `target.decision_date >= source.decision_date` (un arrêt est cité **par** des arrêts postérieurs).
- Agrégation : à chaque niveau, garder le chemin de plus fort produit de confiance vers chaque nœud atteint (sinon explosion exponentielle si un nœud très cité est atteint par 1 000 chemins différents).
- `LIMIT` par niveau : 200 arrêts au niveau 1, 500 au niveau 2, 1 000 au niveau 3 (tunables).
- `HAVING path_confidence >= 0.3` final pour élaguer les chemins faibles.

### 6.3 Pattern 3 — Chaînes de citation en arrière (ancêtres d'un arrêt)

Symétrique du pattern 2 mais `target.decision_date <= source.decision_date`. Utile pour répondre à "sur quels précédents cet arrêt s'appuie-t-il implicitement (cités par les arrêts qu'il cite) ?".

### 6.4 Pattern 4 — Leading cases par domaine (find_leading_cases)

**Entrée** : un `legal_topic` (ou un `LegalConcept`), une fenêtre temporelle optionnelle, une langue optionnelle.
**Sortie** : top-K arrêts triés par score composite.
**Pattern conceptuel** :

- Pas de récursion nécessaire si `v_leading_cases_by_topic` est matérialisée : simple lookup + filtres.
- Si besoin de recalcul dynamique (fenêtre temporelle custom) : jointure `decisions ⋈ decision_concepts ⋈ citation_targets (agrégation in-degree)` avec tri composite `pagerank_temporal × log(1 + in_degree) × recency_factor`.
- Garde-fou : exclusion des arrêts avec `validity_status IN ('overruled', 'obsolete')`.

### 6.5 Pattern 5 — Analyze_legal_trend

**Entrée** : un `legal_topic`, une fenêtre `[start_date, end_date]`, un pas (trimestre / année).
**Sortie** : série temporelle `{bucket_date → count_citations, count_decisions, mean_pagerank}`.
**Pattern conceptuel** :

- Window function Postgres (`date_trunc('quarter', …)`) + agrégations sur `v_citation_graph` filtrée par concept.
- Détection de ruptures : comparer moyenne glissante 8 trimestres vs 2 derniers. Seuil relatif (> 40 % de baisse / hausse) → flag.
- Pas de récursion nécessaire.

### 6.6 Pattern 6 — Détection de cycles

**Entrée** : un `decision_id` ou un sous-graphe (ex : un canton, un domaine).
**Sortie** : liste des cycles trouvés, longueur ≤ 4.
**Pattern conceptuel** :

- Recursive CTE avec `path decision_id[]` et condition `new_node = ANY(path)` pour détecter fermeture.
- Limiter profondeur à **4** strictement (cycles plus longs quasi-inexistants en pratique juridique et explosent en coût).
- Filtrer cycles triviaux : si `A cite B` et `B cite A` mais l'un est `is_prior_instance`, ce n'est pas un cycle pathologique (c'est normal : arrêt d'appel référence son antérieur qui référence le dossier…). Exclure via annotation `is_prior_instance = 0` des deux côtés.
- `v_citation_cycles_candidates` pré-calcule les cycles de longueur 2 et 3 pour servir de point d'entrée rapide.

### 6.7 Pattern 7 — Propagation `OVERRULES`

**Entrée** : un `decision_id` A avec `OVERRULES -> B`.
**Sortie** : ensemble des arrêts C qui suivent B (`FOLLOWS`) et qu'il faut marquer à re-vérifier.
**Pattern conceptuel** :

- Un seul saut récursif : `A -[OVERRULES]-> B`, puis `C -[FOLLOWS]-> B` avec `C.decision_date < A.decision_date`.
- Pour chaque C trouvé, créer une entrée dans `validity_review_queue` avec raison `"ancestor overruled"` et `trigger_decision_id = A`.
- **Garde-fou critique** : ne pas propager en cascade transitive. Si l'analyste valide C comme "reste valable malgré overruling de B", pas de propagation plus loin. Voir section 10.

### 6.8 Garde-fous généraux contre l'explosion combinatoire

Dans tous les patterns récursifs :

1. **Profondeur bornée** codée en dur dans la CTE (pas de paramètre utilisateur > 5).
2. **VISITED set** dans un array accumulé, exclusion sur `NOT = ANY(visited)`.
3. **LIMIT par niveau** : empêche qu'un nœud à très haut degré (ATF fondateur cité 20 000 fois) n'explose la traversée.
4. **Statement timeout** Postgres à 5 s par défaut pour les CTE récursifs, 15 s pour les runs administratifs (refresh vues).
5. **Budget d'effort** côté API : chaque endpoint impose un `max_edges_visited` retourné dans la réponse ; si atteint, le résultat est marqué `partial=true`.

---

## 7. Indexation

### 7.1 Indexes sur tables sources

- `decision_citations` et `citation_targets` :
  - Index couvrant `(source_decision_id, target_decision_id, confidence_score, is_prior_instance, mention_count)` — support traversée avant.
  - Index couvrant `(target_decision_id, source_decision_id, confidence_score, is_prior_instance)` — support traversée arrière.
  - Index partiel sur `WHERE is_prior_instance = 1` — sert les Instanzenzug, très sélectif (< 3 % des edges).
  - Index partiel sur `WHERE confidence_score >= 0.7` — pour traversées "haute fiabilité uniquement".

- `decisions` :
  - Index B-tree sur `(court, decision_date DESC)` — filtres fenêtres temporelles par cour.
  - Index B-tree sur `(canton, decision_date DESC)` — équivalent cantonal.
  - Index partiel sur `WHERE validity_status = 'valid'` — grande majorité, mais sert à accélérer les filtres d'exclusion overruled.
  - BRIN sur `decision_date` seul — pour les balayages de grandes plages (analyse de tendances). BRIN est adapté car les dates sont fortement corrélées à l'ordre d'insertion.

- `decision_statutes` :
  - Index couvrant `(statute_id, decision_id)` et inverse `(decision_id, statute_id)`.
  - Index partiel pour statuts "chauds" (ex : `WHERE statute_id IN (top-100 articles cités)`) si profiling montre un bénéfice.

- `decision_concepts` (nouveau, Phase 5) :
  - Index couvrant `(concept_id, decision_id, relevance_score DESC)` pour top-K arrêts par concept.

### 7.2 Indexes sur vues matérialisées

Chaque vue matérialisée reçoit au minimum :

- Un index UNIQUE sur la clé naturelle (nécessaire à `REFRESH CONCURRENTLY`).
- Des index de support pour les requêtes attendues (ex : `v_leading_cases_by_topic` indexée sur `(topic, rank)`).

### 7.3 Revue périodique

`pg_stat_user_indexes` et `pg_stat_statements` sont audités **chaque mois** pendant la phase 9 (observabilité). Indexes jamais utilisés après 90 jours → drop. Indexes trop lourds (> 500 MB) sans usage proportionnel → reconsidérer.

---

## 8. Use cases détaillés par tool MCP

Référence : les 23 tools MCP actuellement dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` (plan-maître ligne 8) dont le portage Edge Functions est traité en Phase 6. La Phase 8 apporte des améliorations ciblées sur les tools ci-dessous.

### 8.1 `find_citations` (bidirectionnel, 1-saut)

- **État actuel** : jointure simple sur `citation_targets`, aucune récursion nécessaire.
- **Apport Phase 8** : rien de structurel. Gains marginaux : filtre `confidence_score`, annotation `FOLLOWS/DISTINGUISHES` si disponible, flag si l'arrêt cible est `overruled`.
- **Requête conceptuelle** : lookup direct sur `v_citation_graph` avec deux branches (source→cible et cible→source) et colonnes enrichies.

### 8.2 `find_appeal_chain`

- **État actuel** : logique Python-side, 3-4 requêtes séquentielles, approximatif sur les chaînes cantonales.
- **Apport Phase 8** : CTE récursif unique (pattern §6.1) ou lookup direct sur `v_instanzenzug`.
- **Bénéfice** : latence divisée par 5-10, couverture cantonale améliorée grâce à `appeals_to` explicite.
- **Requête conceptuelle** : si le nœud est BGer, lookup direct `v_instanzenzug`. Sinon, CTE remontant qui cherche le BGer ancêtre puis dévale via `v_instanzenzug`.

### 8.3 `find_leading_cases`

- **État actuel** : inexistant ou approximation sur `pagerank_temporal` seul.
- **Apport Phase 8** : premier vrai support via `v_leading_cases_by_topic` + pattern §6.4 pour les cas dynamiques.
- **Bénéfice** : nouveau tool, sans équivalent aujourd'hui.
- **Requête conceptuelle** : jointure `decisions ⋈ decision_concepts` filtrée par concept ou topic, ordonnée par score composite (défini en Phase 5 : `0.5 × pagerank + 0.3 × normalized_in_degree + 0.2 × recency_boost`).

### 8.4 `analyze_legal_trend`

- **État actuel** : agrégation simple par année, pas d'analyse de rupture.
- **Apport Phase 8** : pattern §6.5, détection rupture via moyenne glissante, corrélation avec événements législatifs (publication d'une révision fedlex dans la fenêtre).
- **Bénéfice** : passage de "stat descriptive" à "signal analytique".

### 8.5 `find_cycles` (nouveau tool)

- **État actuel** : n'existe pas.
- **Apport Phase 8** : nouveau tool MCP, pattern §6.6.
- **Usage** : qualité du graphe (détecter anomalies d'extraction), recherche juridique avancée (dialogue jurisprudentiel avec allers-retours).
- **Intérêt produit** : faible en v1, mais utile pour debug graphe.

### 8.6 `flag_validity_review` (nouveau pipeline interne)

- **État actuel** : n'existe pas.
- **Apport Phase 8** : job batch nocturne qui applique le pattern §6.7 à chaque nouvel `OVERRULES` détecté en Phase 5, alimente une queue `validity_review_queue` consommée par un reviewer humain (ou LLM avec score de confiance).
- **Bénéfice** : fiabilité juridique améliorée sans effort manuel permanent.

### 8.7 Synthèse gains par tool

| Tool | Latence avant | Latence après (cible) | Exactitude |
|---|---|---|---|
| `find_citations` | ~80 ms | ~40 ms | Meilleure (annotations) |
| `find_appeal_chain` | ~500 ms-2 s | < 200 ms | > 80 % couverture |
| `find_leading_cases` | inexistant | < 100 ms sur vue | Nouveau |
| `analyze_legal_trend` | ~400 ms | < 300 ms | Détection ruptures nouvelle |
| `find_cycles` | inexistant | < 1,5 s | Nouveau |

---

## 9. Extension optionnelle `apache_age`

### 9.1 Quand l'activer

- **Seuils déclencheurs** listés en §2.4.
- Ou si, à l'usage, on identifie des requêtes Cypher beaucoup plus lisibles que leurs équivalents CTE. Exemples typiques : chemins de longueur variable avec annotations multiples (`MATCH (a)-[:CITES*1..3 {confidence > 0.7}]->(b)` est plus court qu'un CTE récursif).
- Ou si un use case produit exige d'exposer un endpoint de requête ad-hoc (Cypher) à des utilisateurs avancés.

### 9.2 Coût ops d'activation

- Installation de l'extension côté Supabase self-hosted (privileged). Compatible Postgres 15, avec une compile C spécifique.
- Création d'un graph "view" dérivé des tables existantes via `ag_catalog.create_graph` et chargement via `load_labels_from_file` ou triggers de sync depuis `decision_citations`.
- **Double représentation** : `apache_age` stocke les nœuds/arêtes dans son propre schéma, distinct des tables Postgres relationnelles. Sync possible via triggers, mais introduit une dualité que la Phase 8 v1 cherche précisément à éviter.
- Overhead mémoire : +500 MB-1 GB runtime pour le parser Cypher + cache graphe.

### 9.3 Compatibilité avec `pgvectorscale`

- Aucun conflit documenté entre `apache_age` 1.5 et `pgvectorscale` 0.3+. Les deux extensions s'installent dans des schémas distincts (`ag_catalog` vs `public`) et n'interagissent pas.
- Risque résiduel : upgrades Postgres majeurs pourraient nécessiter d'attendre la compatibilité `apache_age` avant d'upgrader. Documenter dans runbook ops.

### 9.4 Plan de migration si activation

1. Dev/preview environnement : installer `apache_age`, créer le graphe, porter 2-3 requêtes emblématiques en Cypher.
2. Benchmark côte à côte : CTE vs Cypher, latence et lisibilité.
3. Si gain > 30 % latence ou > 50 % lisibilité (jugement qualitatif) → activation prod derrière feature flag.
4. Cohabitation : les endpoints lisent l'un ou l'autre selon un flag ; les CTE restent pour fallback 6 mois.

---

## 10. Propagation de `validity_status` via graphe (optionnel avancé)

### 10.1 Motivation

Un arrêt A overrule un arrêt B. Tous les arrêts C qui suivaient B (`FOLLOWS`) deviennent potentiellement fragilisés. Propager automatiquement `validity_status = 'overruled'` à C serait incorrect (C peut tenir par d'autres motifs), mais **flagger C pour re-vérification** est utile.

### 10.2 Règles de propagation

1. **Trigger** : création d'une ligne dans `decision_overrules` (détecté par Phase 5).
2. **Propagation niveau 1** : tous les `C` avec annotation `FOLLOWS` vers `B` sont ajoutés à `validity_review_queue` avec raison `"ancestor_overruled"`, gravité `medium`.
3. **Propagation niveau 2 (optionnelle)** : les `D` avec `FOLLOWS` vers un `C` lui-même en `validity_review_queue` reçoivent une propagation de gravité `low`. **Cap à 2 niveaux** strictement.
4. **Exclusions** :
   - Pas de propagation si `C.decision_date > A.decision_date` (C a été rendu après l'overruling, il est censé en tenir compte).
   - Pas de propagation si `C` cite également un autre arrêt équivalent à B comme autorité principale (pluralité d'ancêtres → robustesse).
   - Pas de propagation si `C.court` est une instance cantonale et `B` est BGer (les cantons suivent BGer par déférence, mais l'overruling BGer est déjà suivi naturellement par les cantons post-A).

### 10.3 Garde-fous anti-cascade abusive

- **Pas de propagation récursive automatique** : une propagation niveau 2 ne re-déclenche pas une propagation niveau 3.
- **Taux plancher d'auto-flag** : si un overruling déclencherait > 500 flags, le job s'arrête et alerte un humain (signe d'overruling très impactant qui mérite revue éditoriale).
- **Revue humaine obligatoire** pour passer `validity_status` d'un arrêt à `'overruled'`. Le graphe ne fait que **proposer**, jamais décider.
- **Audit trail** : chaque entrée dans `validity_review_queue` conserve `trigger_decision_id`, `propagation_level`, `created_at`, `status` (pending/reviewed/dismissed).

### 10.4 Out-of-scope v1

La règle `DISTINGUISHES` (C distingue B mais ne le renverse pas) n'est pas exploitée en v1. Un arrêt "distingué" reste valide, mais son scope est rétréci. Modélisation fine reportée.

---

## 11. Observabilité

### 11.1 Métriques à exposer (dashboard Phase 9)

- **Latence CTE récursifs** : p50, p95, p99 par pattern (§6.1 à §6.7). Via `pg_stat_statements` filtré par queryid.
- **Profondeur moyenne atteinte** vs profondeur max configurée. Indique si les garde-fous mordent ou s'ils sont inutilement larges.
- **Taille moyenne des résultats** par pattern. Si `find_appeal_chain` renvoie en moyenne 2 nœuds mais p95 à 15, chercher les anomalies (cycles cachés).
- **Taux de `partial=true`** sur les réponses API (garde-fou `max_edges_visited` atteint). Cible < 2 %.
- **Coût refresh vues matérialisées** : durée, lignes ajoutées/modifiées, espace récupéré par VACUUM post-refresh.
- **Cache hit rate** sur endpoints graph (Phase 6) : invalidation liée au refresh nocturne des vues.

### 11.2 Requêtes lentes

- Seuil : toute requête CTE > 1 s est loggée dans `slow_graph_queries` avec plan d'exécution (`EXPLAIN (ANALYZE, BUFFERS)`).
- Revue hebdo par l'équipe infra.
- Corrélation avec mises à jour des statistiques Postgres (`ANALYZE` après bulk ingest) : si le planner choisit un mauvais plan, re-stats.

### 11.3 Alertes

- Alerte si latence p99 d'un pattern dépasse 2× sa baseline sur 1 h.
- Alerte si refresh d'une vue matérialisée échoue 2 fois d'affilée.
- Alerte si `validity_review_queue` dépasse 1 000 entrées pending (saturation reviewer humain).

### 11.4 Cohérence graphe

Check nocturne :

- Nombre d'edges `citation_targets` vs checksum du build SQLite d'origine (invariant plan-maître).
- Pourcentage de `target_ref` non résolus en `target_decision_id` (doit rester stable ; une baisse soudaine = scraper qui renvoie des dockets malformés).
- Distribution des `confidence_score` (histogramme doit être stable à ±5 % d'une semaine sur l'autre).

---

## 12. Risques et mitigations

### 12.1 CTE explosion sur cycles

- **Risque** : un graphe dense avec cycles mal détectés → CTE qui n'arrête plus ou retourne millions de lignes.
- **Mitigation** : profondeur bornée en dur, `VISITED` set systématique, `LIMIT` par niveau, `statement_timeout` à 5 s, `max_edges_visited` dans l'API.

### 12.2 Maintenance vues matérialisées à l'échelle

- **Risque** : avec 58 GB+ de données sources et une croissance ~15 %/an, les refresh se rallongent, fenêtres nocturnes saturées.
- **Mitigation** : refresh incrémentaux là où possible (via triggers sur `updated_at`), partitionnement `v_citation_graph` par décennie de `decision_date`, refresh parallélisable par partition.

### 12.3 Complexité future forcée (migration Neo4j)

- **Risque** : si un use case exige GDS avancé, la migration Postgres → Neo4j pourrait coûter 2-3 mois de dev.
- **Mitigation** :
  - Mod模éliser les entités/relations de manière **graphe-agnostique** (FK explicites, pas de JSONB pour les relations).
  - Conserver une couche d'abstraction "graph service" dans les Edge Functions (Phase 6) : les tools MCP appellent `graph.findAppealChain(id)` plutôt que du SQL direct. Un changement de backend graphe ne casse pas les tools.
  - Documenter précisément le schéma graphe conceptuel (§3) comme contrat inter-phase, indépendant de l'implémentation.

### 12.4 Couverture `is_prior_instance` incomplète

- **Risque** : les instances cantonales citant sans flag `is_prior_instance` → Instanzenzug cassé.
- **Mitigation** : backfill Phase 5 (LLM qui lit l'arrêt et identifie les instances antérieures), audit manuel sur échantillon, bootstrap `v_instanzenzug` depuis les arrêts BGer (où la résolution est la plus fiable) puis propagation descendante.

### 12.5 Bloat et fragmentation des tables sources

- **Risque** : après plusieurs millions de UPDATE (re-enrichissement, re-confidence), les tables sont fragmentées, queries ralentissent.
- **Mitigation** : `VACUUM (ANALYZE)` hebdomadaire, `pg_repack` trimestriel sur `decision_citations` et `citation_targets`.

### 12.6 Drift entre graphe et retrieval

- **Risque** : un arrêt marqué `overruled` dans le graphe continue de remonter top-1 du retrieval parce que `authority_rerank` (Phase 7) n'a pas encore été re-calculé.
- **Mitigation** : le rerank lit `validity_status` en temps réel via jointure, pas via score pré-calculé figé. Documenté comme invariant Phase 7.

### 12.7 Confidentialité et RLS

- **Risque** : si à terme certaines décisions sont restreintes (anonymisation partielle, décisions non publiques), les CTE récursifs pourraient "leaker" des IDs via la traversée.
- **Mitigation** : appliquer les RLS (row-level security) de Supabase sur les tables sources ; les vues matérialisées héritent du filtrage au refresh (donc ne pas matérialiser de données restreintes dans une vue accessible librement). En v1, toutes les décisions sont publiques : risque théorique.

---

## 13. Definition of Done

La Phase 8 est considérée terminée lorsque **tous** les critères suivants sont satisfaits :

### 13.1 Livrables techniques

- [ ] Table `courts` peuplée (50-80 juridictions suisses), seed versionné dans `/supabase/seed/courts.sql` (répertoire à créer en Phase 1).
- [ ] Table `appeals_to` peuplée et validée par un praticien suisse (DAG, sans cycle).
- [ ] Tables `legal_concepts`, `decision_concepts`, `concept_hierarchy`, `decision_overrules`, `decision_citation_annotations`, `decision_publications` créées, contraintes et indexes en place (peuplement progressif par Phase 5, la Phase 8 assure le schéma et les indexes).
- [ ] Vue matérialisée `v_citation_graph` créée, refresh concurrent validé, volume mesuré, indexes en place.
- [ ] Vue `v_instanzenzug` créée et peuplée, couverture BGer 2015-2025 ≥ 80 %.
- [ ] Vue `v_leading_cases_by_topic` créée, top-20 par topic audité manuellement sur 10 topics représentatifs.
- [ ] Vue `v_statute_decision_counts` créée.
- [ ] Vue `v_citation_cycles_candidates` créée.
- [ ] Vue `v_concept_co_occurrence` créée (dépend de la taxonomie Phase 5 ; peut être vide si Phase 5 pas terminée, mais schéma prêt).

### 13.2 Fonctions / endpoints

- [ ] Les 7 patterns de requête (§6.1 à §6.7) implémentés comme fonctions SQL ou vues, avec tests unitaires couvrant les cas limites (profondeur max, cycles, branchement fort).
- [ ] Tool MCP `find_appeal_chain` migré sur `v_instanzenzug` + CTE, tests de parité fonctionnelle avec l'ancien comportement Python (sur 500 arrêts golden).
- [ ] Tool MCP `find_leading_cases` créé, intégré au MCP et au REST (Phase 6).
- [ ] Tool MCP `find_cycles` créé en mode preview (feature flag).
- [ ] Tool `analyze_legal_trend` étendu avec détection rupture.
- [ ] Pipeline `flag_validity_review` (job batch + table `validity_review_queue`) opérationnel, avec run de dry-run documenté.

### 13.3 Indexes et perf

- [ ] Indexes couvrants bidirectionnels créés sur `decision_citations`, `citation_targets`, `decision_statutes`, `decision_concepts`.
- [ ] Indexes partiels pour `is_prior_instance=1`, `confidence_score >= 0.7`, `validity_status = 'valid'`.
- [ ] BRIN sur `decisions.decision_date`.
- [ ] Benchmark de latence sur 1 000 arrêts golden : p50 < 200 ms, p99 < 800 ms pour 3 sauts confirmés.

### 13.4 Observabilité

- [ ] Queries lentes (> 1 s) loguées dans `slow_graph_queries` avec plan.
- [ ] Dashboard Phase 9 reçoit les métriques latence / profondeur / taille résultats / `partial=true`.
- [ ] Alertes configurées (latence p99, refresh échoué, queue saturée).
- [ ] Runbook ops documenté : refresh manuel d'une vue, diagnostic CTE lent, propagation overrule review.

### 13.5 Documentation

- [ ] Schéma graphe conceptuel documenté dans ce fichier et référencé depuis `00-master-plan.md`.
- [ ] Patterns de requête documentés avec exemples attendus et anti-patterns.
- [ ] ADR (Architecture Decision Record) capturé sur la décision Postgres vs Neo4j, versionné dans `/docs/adr/` (répertoire à créer si absent).
- [ ] Procédure d'activation future de `apache_age` documentée (runbook étape par étape).
- [ ] Procédure de propagation `validity_status` documentée (règles, exclusions, rôles humains).

### 13.6 Non-régression

- [ ] Les 8.84 M edges de `decision_citations` sont intégralement préservés (checksum comparé au build SQLite d'origine, invariant plan-maître ligne 37).
- [ ] Le dataset HuggingFace Parquet (invariant ligne 39) reste inchangé en schéma.
- [ ] Les 23 tools MCP et 30 routes REST restent fonctionnels (parité invariant ligne 35). Tools améliorés gardent leur signature publique ; nouveaux tools sont additifs.
- [ ] Client Word add-in et Claude Desktop (invariant ligne 38) fonctionnent sans modification.

### 13.7 Validation humaine

- [ ] Revue par un praticien juridique suisse sur :
  - 20 chaînes `find_appeal_chain` tirées au hasard (accord ≥ 95 %).
  - 10 listes `find_leading_cases` sur 10 domaines (accord qualitatif ≥ 90 %).
  - 5 propagations `validity_review_queue` issues de la dry-run (pertinence ≥ 80 %).

### 13.8 Sortie de phase

- [ ] Retrospective écrite : ce qui a marché, ce qui a débordé, ce qui est reporté.
- [ ] Handoff Phase 9 (évaluation + observabilité) : la Phase 9 récupère des endpoints graph stables sur lesquels construire les benchmarks 200 requêtes.

---

*Fin du sous-plan Phase 8. Les sous-plans amont (Phase 5 — enrichissement PA-RAG, Phase 7 — retrieval hybride) et aval (Phase 9 — évaluation) sont consommés/produits par cette phase selon les interfaces décrites ci-dessus.*
