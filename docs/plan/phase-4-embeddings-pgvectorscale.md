# Phase 4 — Embeddings Longformer + pgvectorscale

> Sous-plan détaillé de la Phase 4 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Pré-requis : Phase 3 terminée (table `chunks` peuplée, couverture > 95 %, chunking SAC stabilisé).
> Dépendances externes : Supabase self-hosted avec `pgvector >= 0.7`, extension `pgvectorscale >= 0.5`, runtime d'inférence GPU ou CPU AVX-512.
> Durée cible : **1 semaine calendaire** pour le plan d'infrastructure + index ; **4 à 8 semaines** additionnelles pour le ré-encodage complet des ~14 M chunks (exécution en tâche de fond, hors chemin critique fonctionnel).

---

## 1. Objectifs et critères de succès

### 1.1 Objectifs fonctionnels

La Phase 4 remplace le couple actuel **BGE-M3 (1024-dim) + sqlite-vec** (tel qu'implémenté dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py`) par :

- Un modèle d'embedding **spécialisé juridique suisse multilingue** : `joelito/legal-swiss-longformer-base` (768 dimensions, contexte natif 4 096 tokens, RoBERTa pré-entraîné sur 689 Go de textes légaux en 24 langues dont DE/FR/IT/RM).
- Un index ANN **pgvectorscale StreamingDiskANN** sur la table `chunk_embeddings(embedding)`, optimisé pour un corpus de ~14 M vecteurs 768-dim qui dépasse la RAM économique (~40 Go sur disque).
- Un index hiérarchique complémentaire **pgvector HNSW** sur `decisions.regeste_embedding` (~965 k vecteurs) pour le filtrage grossier doc-niveau en amont du rerank chunk-niveau.
- Un **service d'inférence** externalisé au serveur Postgres (conteneur dédié, file d'attente batch, export ONNX) afin de découpler le coût GPU/CPU du moteur transactionnel.

### 1.2 Critères de succès quantitatifs

| Métrique | Cible | Mesure |
|---|---|---|
| Latence top-50 ANN chunk-niveau (p50, corpus chaud) | < 50 ms | `EXPLAIN (ANALYZE, BUFFERS)` + bench côté client |
| Latence top-100 ANN chunk-niveau (p95, corpus chaud) | < 100 ms | Idem |
| Latence top-20 HNSW regeste (p95) | < 15 ms | Idem |
| Recall@10 vs exact brute-force (échantillon 1 k requêtes) | ≥ 0.97 | Eval offline |
| Dim réduite vs actuel | 768 (vs 1024) | Économie stockage ≈ 25 % |
| Couverture d'indexation des chunks Phase 3 | ≥ 99.5 % | `SELECT COUNT(*) FROM chunks WHERE embedding IS NULL` |
| Débit ré-encodage soutenu (GPU A10/L4 ONNX int8) | ≥ 800 chunks/s | Métriques worker |
| Ré-encodage prioritaire ATF (~20 k décisions, ~400 k chunks) | ≤ 48 h | Wall-clock pipeline |

### 1.3 Critères qualitatifs

- Pas de régression de nDCG@10 vs baseline BGE-M3 sur le benchmark 200 requêtes (Phase 9). En pratique, on vise **+5 à +8 points nDCG** grâce à la spécialisation juridique et au long-contexte qui capture les considérants entiers sans fragmentation agressive.
- Équivalence qualitative DE/FR/IT mesurée par sous-benchs trilingues (écart nDCG max entre langues ≤ 3 points).
- Aucune indisponibilité de la recherche sémantique pendant la bascule (double index temporaire BGE-M3 + Longformer, cf. § 9).

---

## 2. Choix du modèle : Longformer Swiss vs alternatives

### 2.1 Candidats évalués

| Modèle | Dim | Contexte | Corpus pré-entraînement | Multilingue | Note |
|---|---|---|---|---|---|
| **`joelito/legal-swiss-longformer-base`** | 768 | 4 096 tokens natifs | 689 Go MultiLegalPile (24 langues, dont CH DE/FR/IT) + SwissLegal corpora | Oui (DE/FR/IT/RM) | **Choix retenu** |
| `joelito/legal-swiss-roberta-base` | 768 | 512 tokens | Idem | Oui | Contexte court : fragmente les considérants longs |
| Swiss Legal BERT BFH (FHNW) | 768 | 512 tokens | Arrêts TF + doctrine CH | Faible (DE dominant) | Sous-représentation FR/IT, licence restrictive |
| `intfloat/multilingual-e5-large` | 1 024 | 512 tokens | Web multilingue généraliste | Oui (100+ langues) | Non-spécialisé juridique, perf inférieure sur terminologie |
| `BAAI/bge-m3` (actuel) | 1 024 | 8 192 tokens | Web multilingue | Oui (100+) | Baseline. Bon mais non-spécialisé, dense+sparse+multi-vec |
| `law-ai/InLegalBERT` | 768 | 512 tokens | Droit indien EN | Non | Hors scope linguistique |
| `nlpaueb/legal-bert-base-uncased` | 768 | 512 tokens | Droit EN/US | Non | Hors scope |

### 2.2 Trade-offs explicites

**Pour Longformer Swiss** :
- *Couverture linguistique* : DE/FR/IT natifs avec proportions calibrées sur le corpus CH (TF publie ~55 % DE, ~30 % FR, ~10 % IT). Évite le biais monolingue de Swiss Legal BERT BFH.
- *Long-contexte* : fenêtre native 4 096 tokens via sliding-window attention permet d'encoder un considérant entier sans troncature (médiane considérant ~1 500 tokens d'après l'échantillonnage Phase 3). BGE-M3 à 8 192 est plus large mais paie le coût en latence sans gain qualitatif ici puisque les chunks SAC ciblent 400-512 tokens.
- *Spécialisation juridique* : le rapport PA-RAG § 7.4 documente un gain moyen de **+12 à +17 points nDCG** pour les modèles pré-entraînés sur corpus légal vs généralistes, particulièrement sur requêtes techniques (articles, régestes, motifs).
- *Dimensionnalité 768* : ~25 % d'économie vs 1024 (BGE-M3/E5). Sur 14 M vecteurs : **~43 Go** (float32) vs ~57 Go, soit 14 Go d'I/O en moins pour chaque scan de l'index.

**Contre Longformer Swiss** :
- *Coût d'inférence* : sliding-window attention plus coûteuse qu'un BERT classique à longueur égale (≈ 1.3× GPU time). Mitigé par le fait que la majorité des chunks SAC tiennent sous 1 024 tokens (seuil où Longformer dégrade peu vs full-attention).
- *Maturité ONNX* : l'export de Longformer vers ONNX nécessite un opset ≥ 17 et un patch pour le sliding-window attention pattern (documenté dans `optimum`). Risque faible mais à valider en S1.
- *Pas de sparse natif* : contrairement à BGE-M3 (dense+sparse+colbert), Longformer ne produit que du dense. Le pilier lexical hybride BM25 sera donc porté par `ParadeDB pg_search` ou `tsvector` côté Postgres (cf. plan-maître § Stack cible), et non par un index sparse issu de l'embedder.

### 2.3 Décision

`joelito/legal-swiss-longformer-base` est retenu comme modèle de production pour la table `chunk_embeddings`. Le modèle BGE-M3 est **conservé en shadow** pendant la durée du ré-encodage complet (cf. § 9) pour permettre un rollback. La variante `legal-swiss-roberta-base` peut être utilisée en back-up pour les chunks courts (< 512 tokens) si un benchmark démontre qu'elle équivaut à Longformer pour ce sous-ensemble (optimisation potentielle, non-bloquante).

---

## 3. Architecture du service d'inférence

### 3.1 Topologie

Le service d'inférence est un **conteneur dédié**, indépendant du cluster Postgres/Supabase, exposant une API HTTP/gRPC interne :

```
[worker ré-encodage / MCP query]  -->  [inference-service]  -->  [model runtime (ONNX / PyTorch)]
                                              |
                                              v
                                       [batching queue]
```

Rationale :
- Isoler le profil de charge GPU/CPU intensif du serveur Postgres (qui a ses propres contraintes mémoire/IO).
- Permettre le scaling horizontal indépendant (autoscaler sur la profondeur de file).
- Découpler la version du modèle de la version du schéma DB (cf. § 8 versioning).

### 3.2 Runtime : ONNX par défaut

- **Export ONNX** : conversion de `joelito/legal-swiss-longformer-base` via `optimum-cli export onnx --model ... --task feature-extraction`. Opset 17, shapes dynamiques `(batch, seq_len)`.
- **Runtime** : `onnxruntime` avec `ExecutionProvider` CUDA (GPU) ou CPU (`CPUExecutionProvider` + `OMP_NUM_THREADS` tuné).
- **Quantization int8** : `onnxruntime.quantization.quantize_dynamic` sur les couches Linear. Le rapport PA-RAG § 7.4 note une dégradation typique < 1 point nDCG pour un gain latence ~3-4× sur CPU AVX-512. À valider sur bench interne avant activation en prod.
- **Fallback PyTorch** : conservé pour dev/debug et pour les chunks qui échouent à l'ONNX (edge cases de padding Longformer).

### 3.3 Batching et queue

- **Batch dynamique** : regroupement des requêtes entrantes par fenêtre temporelle courte (ex. 10-20 ms) avec taille max configurable (ex. 64 séquences). Critère : remplir le batch à ≥ 80 % du quota GPU memory avant flush.
- **Bucketing par longueur** : tri intra-batch par `seq_len` croissant pour minimiser le padding gaspillé (gain effectif 20-40 % sur corpus à distribution de longueur très étalée comme les considérants).
- **Queue persistante** : pour le ré-encodage massif, une queue Postgres (`LISTEN/NOTIFY` sur `embedding_jobs`) ou Redis Streams permet la reprise sur incident sans perte de travail.

### 3.4 Scaling : GPU vs CPU

| Profil | Hardware | Débit attendu (batch 32, seq 512) | Coût/1M chunks (indicatif) |
|---|---|---|---|
| GPU A10 (24 Go) | 1× A10, fp16 | ~1 500 chunks/s | ~0.40 EUR |
| GPU L4 (24 Go) | 1× L4, fp16 | ~1 100 chunks/s | ~0.30 EUR |
| CPU AVX-512 | 32 vCPU + ONNX int8 | ~200 chunks/s | ~0.80 EUR (rentable seulement si GPU indisponibles) |
| GPU T4 (16 Go) | 1× T4, fp16 | ~700 chunks/s | ~0.25 EUR |

Recommandation : **GPU L4 ou A10** pour le burst de ré-encodage initial, puis bascule CPU int8 en régime permanent (seules les nouvelles décisions scrapées, ~30 k/an, nécessitent une inférence en ligne, ce qui tient largement sur CPU).

### 3.5 Scaling horizontal

- Plusieurs instances `inference-service` derrière un load-balancer round-robin (ou un dispatcher MCP).
- Chaque worker tire sa charge de la queue ; le backpressure est géré par la profondeur de file.
- Cible : 4 à 8 instances en pic de ré-encodage, 1 instance en régime permanent.

### 3.6 Observabilité du service

- Métriques exposées (`/metrics` Prometheus-compatible) : QPS in/out, `batch_size` moyen, `seq_len` p50/p95, latence par étape (queue / padding / forward / post), utilisation GPU memory, erreurs par type.
- Traces OpenTelemetry sur chaque requête (span `encode`, tags `model_version`, `batch_size`, `seq_len`).

---

## 4. Stratégie d'indexation : pgvectorscale DiskANN vs pgvector HNSW vs IVFFlat

### 4.1 Contrainte de dimensionnement

- **14 M chunks × 768 dim × 4 octets = ~43 Go** de vecteurs bruts.
- RAM typique d'un nœud Supabase self-hosted : 32 à 128 Go. L'index ANN doit pouvoir dépasser la RAM sans effondrement de latence.
- Débit de mise à jour : ~30 k nouveaux chunks/jour en régime permanent (scrapers), par burst de quelques milliers.

### 4.2 Comparaison des trois options

| Critère | pgvectorscale StreamingDiskANN | pgvector HNSW | pgvector IVFFlat |
|---|---|---|---|
| Corpus > RAM | Oui (SSD-native) | Non (dégrade fortement) | Partiellement |
| Build time (14 M × 768) | ~4 h (SSD NVMe) | ~14 h | ~30 min |
| Recall@10 à latence cible | 0.97-0.99 | 0.97-0.99 | 0.90-0.95 (sans re-tune) |
| Latence p95 top-50 | 20-40 ms | 10-25 ms (si in-RAM) | 50-150 ms |
| Mise à jour incrémentale | Streaming insert O(log n) amorti | Idem, coût plus élevé | Reclustering périodique requis |
| Stockage disque | ~60 Go (graph + vecteurs) | ~55 Go (graph + vecteurs) | ~45 Go |
| Paramètres critiques | `num_neighbors`, `search_list_size` | `m`, `ef_construction`, `ef_search` | `lists`, `probes` |
| Maturité | Stable (Timescale, 2023+) | Très stable | Très stable |

### 4.3 Décision

- **Index principal chunk-niveau** : `pgvectorscale StreamingDiskANN` sur `chunk_embeddings(embedding vector(768))`. Justification : corpus > RAM, streaming inserts fréquents, recall élevé maintenu à budget latence serré.
- **Paramètres nominaux** :
  - `num_neighbors = 50` (degré du graphe ; trade-off qualité/stockage)
  - `search_list_size = 100` (taille de la beam search à la construction ; impacte qualité du graphe)
  - `max_alpha = 1.2` (paramètre de diversité, défaut DiskANN)
  - `storage_layout = memory_optimized` (SBQ quantization, défaut pgvectorscale, divise les lectures disque par 32 au prix de ~1 % de recall)
  - `query_search_list_size = 100` (runtime, ajustable par session pour trade-off rappel/latence)
  - `query_rescore = 50` (re-score en précision float32 des 50 meilleurs candidats)
- **Distance** : `vector_cosine_ops` (embeddings L2-normalisés à l'encodage — cf. § 8).

### 4.4 Index complémentaire doc-niveau : HNSW

Pour la table `decisions.regeste_embedding` (965 k vecteurs, tient en RAM) :

- **pgvector HNSW** avec `m = 16`, `ef_construction = 64`, `ef_search = 40`.
- Distance cosine (`vector_cosine_ops`).
- Justification : HNSW natif pgvector est plus rapide in-RAM que DiskANN, et 965 k × 768 × 4 = ~3 Go rentre confortablement dans le `shared_buffers`.
- Cas d'usage : cf. § 5 (filtrage grossier).

### 4.5 Pourquoi pas IVFFlat ?

- Recall insuffisant sans grid search onéreux sur `(lists, probes)`.
- Reclustering nécessaire après insertion massive → incompatible avec le rythme de scraping quotidien.
- IVFFlat reste utile comme **baseline pour les benchs de sanity-check** uniquement.

### 4.6 Paramètres réglables en runtime

- `SET pgvectorscale.query_search_list_size = 150;` en session pour doper le recall sur requêtes critiques (ex. analyses de jurisprudence longues).
- `SET hnsw.ef_search = 80;` idem pour l'index regeste.

---

## 5. Index hiérarchique (decision-niveau + chunk-niveau)

### 5.1 Rationale

Le rapport PA-RAG § 4.2 et § 7 recommande un **filtrage à deux niveaux** :
1. Pré-sélection rapide des décisions candidates via embedding doc-niveau (regeste ou titre enrichi).
2. Rerank fin au niveau des chunks pour identifier les passages pertinents.

Cette hiérarchie :
- Réduit l'espace de recherche chunk-niveau par **un facteur ~15** (965 k → ~100 k chunks candidats pour une requête typique).
- Autorise le filtrage métadonnées (cour, date, autorité, langue) avant le scan ANN coûteux.
- Améliore la qualité sémantique globale en privilégiant d'abord la pertinence du document entier.

### 5.2 Schéma cible

- `decisions.regeste_embedding vector(768)` : embedding du regeste officiel (ou du résumé SAC doc-niveau généré Phase 3 si regeste absent).
- `chunk_embeddings.embedding vector(768)` : embedding de chaque chunk SAC.
- FK `chunk_embeddings.decision_id -> decisions.decision_id` pour la jointure.

### 5.3 Patron de requête hybride (conceptuel)

1. Étape A (doc-niveau, HNSW) : `ORDER BY regeste_embedding <=> :q LIMIT 200` avec filtres `WHERE language = :lang AND court = :court AND decision_date BETWEEN ...`.
2. Étape B (chunk-niveau, DiskANN) : `WHERE decision_id IN (<200 ids de A>) ORDER BY embedding <=> :q LIMIT 50`.
3. Étape C (rerank PA-RAG Phase 7) : cross-encoder + authority rerank sur les 50 candidats.

### 5.4 Cas d'usage spécifiques

- **Recherche large ("trouve-moi tous les arrêts sur X")** : étape A suffit, skip étape B.
- **Recherche pointue ("passage précis sur Y dans un arrêt de 2023")** : étape B directement avec filtre `decision_date`, skip étape A (l'espace candidat reste gérable grâce aux filtres partitionnels).
- **Navigation guidée** : étape A produit une liste d'arrêts cliquables ; étape B se déclenche on-demand par arrêt sélectionné.

### 5.5 Filtres combinés

pgvectorscale supporte le **pre-filtering** via `iterative_search` (planifie l'index en fonction des prédicats). Pour préserver le recall quand les filtres éliminent > 90 % du corpus, activer `SET pgvectorscale.enable_iterative_search = on;` en session.

---

## 6. Plan de ré-encodage (~14 M chunks)

### 6.1 Priorisation (authority-first)

Le rapport PA-RAG § 4.2 souligne que les **ATF** (Arrêts du Tribunal fédéral publiés) pèsent de manière disproportionnée dans les requêtes utilisateur (estimation : ~70 % du trafic concerne ~2 % du corpus). Ordre de ré-encodage :

| Tranche | Volume (chunks estimés) | Priorité | Délai cible |
|---|---|---|---|
| 1. ATF (publication officielle) | ~400 k | P0 | 48 h |
| 2. Arrêts TF non-publiés (5A/5D/5P/…) post-2015 | ~2.5 M | P1 | 2 semaines |
| 3. Tribunaux fédéraux spécialisés (TAF, TFB, TPF, TMC) | ~1.5 M | P1 | 2 semaines |
| 4. Cantonaux post-2020 | ~3 M | P2 | 3 semaines |
| 5. Cantonaux 2010-2020 | ~4 M | P3 | 4 semaines |
| 6. Cantonaux < 2010 + divers | ~2.6 M | P4 | 6 semaines |

### 6.2 Parallélisme

- **Sharding déterministe** par `hash(chunk_id) % N` où N = nombre de workers GPU (typiquement 4-8 en pic).
- Chaque worker lit sa partition depuis une vue `chunks_to_encode_shard_{i}` matérialisée ou une requête paramétrée.
- Inspiration directe du mécanisme de `--shard-index` / `--num-shards` déjà présent dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py` (lignes 632-635), à porter en version Postgres-native.

### 6.3 Checkpointing

- Table `embedding_jobs(chunk_id, status, model_version, started_at, finished_at, error)` avec index sur `(status, model_version)`.
- Status : `pending | running | done | error | skipped`.
- Reprise sur crash : `WHERE status IN ('pending', 'error')` et TTL sur `running` (reset si > 30 min sans update).
- Idempotence : `ON CONFLICT (chunk_id, model_version) DO NOTHING` sur l'insert de l'embedding.

### 6.4 Pipeline bout-en-bout

```
[chunks]  --SELECT pending-->  [dispatcher]  --batch RPC-->  [inference-service × N]
                                                                    |
                                      <---embeddings---<
                                      |
                              [COPY / bulk insert]  -->  [chunk_embeddings]
                                                                    |
                                                      --NOTIFY-->  [maintenance: VACUUM, REINDEX]
```

### 6.5 Estimation coût/temps

Hypothèses : 4× GPU L4 (1 100 chunks/s chacun) = 4 400 chunks/s agrégé, disponibilité 85 %.

- Temps wall-clock pur : 14 000 000 / 4 400 / 0.85 = **~62 minutes… théoriques**. En pratique, overheads DB (inserts, commit batches, index maintenance) et facteur padding moyen : **multiplication par 4 à 6**, soit **~6 à 8 heures** pour l'inférence pure.
- Tranche P0 (400 k ATF) : ~20 min d'inférence + ~2 h d'indexation DiskANN partielle + QA → **< 48 h** incluant contrôles.
- Coût GPU cloud (L4 à 0.60 EUR/h × 4 × 10 h overlap) : **~25 EUR** pour la totalité. Négligeable vs coût LLM Phase 5.

### 6.6 Stratégie d'indexation pendant le ré-encodage

Option A (retenue) : **build incrémental**. L'index DiskANN est créé dès les premiers chunks ATF insérés (avec `CREATE INDEX CONCURRENTLY`), puis accepte les streaming inserts. Les premières requêtes sont opérationnelles dès la fin de P0.

Option B (écartée) : **bulk-then-index**. Encoder tout puis `CREATE INDEX`. Plus rapide pour l'index lui-même mais prive la prod de recherche sémantique pendant 6+ semaines. Inacceptable.

### 6.7 Commits et contention

- Inserts groupés par paquets de 1 000 à 5 000 lignes (COPY ou `INSERT ... VALUES (...), (...)`).
- `work_mem` session réglé à 256 Mo pour les workers (sans toucher le global).
- `maintenance_work_mem` à 2 Go pour les REINDEX / VACUUM post-bulk.
- Désactivation temporaire des triggers non-essentiels sur `chunk_embeddings` pendant le bulk-load.

---

## 7. Fine-tuning optionnel (paires ATF)

### 7.1 Rationale et gain attendu

Le rapport PA-RAG § 7.4 rapporte que le fine-tuning supervisé d'un modèle juridique pré-entraîné sur des paires (question → considérant pertinent) extraites des ATF apporte un **gain moyen de +17 points nDCG@10** sur retrieval juridique CH. Ce gain est suffisant pour être envisagé comme **jalon secondaire**, non-bloquant pour la bascule Phase 4.

### 7.2 Corpus d'entraînement

- Source primaire : les ATF (~20 k décisions publiées) avec régestes structurés (question juridique explicite + considérants référencés).
- Paires générées :
  - **Positive** : (regeste[i], considérant cité par regeste[i]).
  - **Hard negative** : considérants d'autres arrêts partageant ≥ 2 articles de loi cités mais dont le regeste diffère.
  - **Soft negative** : tirage aléatoire intra-corpus (diversité).
- Volume cible : ~80 k paires positives + 3× hard negatives + 5× soft negatives = ~720 k triplets.

### 7.3 Protocole

- **Objectif** : contrastive loss (MultipleNegativesRankingLoss) avec température 0.05.
- **Base model** : `joelito/legal-swiss-longformer-base` ou `joelito/legal-swiss-roberta-base` selon longueur moyenne des considérants cibles.
- **Split** : 80 % train, 10 % dev, 10 % test stratifié par langue et par chambre TF.
- **Hyperparamètres nominaux** : lr 2e-5, batch effective 128 (gradient accumulation), 3 epochs, warmup 10 %, weight decay 0.01.
- **Hardware** : 1× GPU A100 40 Go ou 2× L4. Durée estimée : 12 à 24 h selon configuration.
- **Eval** : nDCG@10, MRR, Recall@50 sur le split test, benchmarké contre le modèle de base non fine-tuné.

### 7.4 Coût vs gain

- Coût infra : **~100 EUR** (A100 cloud 24 h) + effort d'engineering (1 à 2 semaines d'un data scientist pour monter pipeline, nettoyer paires, évaluer).
- Gain si confirmé : +10 à +17 points nDCG@10 → impact utilisateur significatif.
- Décision : **GO conditionnel** après Phase 9 (évaluation). Si le modèle de base atteint déjà la cible business (nDCG ≥ X), reporter le fine-tuning à un cycle ultérieur. Sinon, l'activer comme livrable de Phase 4-bis.

### 7.5 Versioning du modèle fine-tuné

- Nommage : `legal-swiss-longformer-ch-caselaw-ft-v1.0-YYYYMMDD`.
- Stockage artefacts : registre modèles interne (HuggingFace privé ou S3/R2 versionné).
- Basculer en prod exige un ré-encodage complet (cf. § 8 et § 9 pour la mécanique de double index).

---

## 8. Normalisation des embeddings et versioning

### 8.1 Normalisation

- **L2-normalisation** appliquée au sortir de l'encodeur (`x / ||x||_2`), cohérent avec l'approche actuelle dans `build_vectors.py` ligne 296 (`normalize_embeddings=True`).
- Avec des vecteurs unitaires, la distance cosine équivaut à la distance L2 au facteur près : `||a - b||^2 = 2 - 2·cos(a, b)`. L'index est configuré en **cosine** (`vector_cosine_ops`) pour clarté sémantique et cohérence avec les patterns de requête documentés.
- Invariant : toute insertion via le pipeline garantit `||embedding||_2 ∈ [0.999, 1.001]` (tolérance float32). CHECK constraint optionnelle, ou validation applicative.

### 8.2 Versioning

- Colonne `model_version TEXT NOT NULL` ajoutée à `chunk_embeddings` et `decisions.regeste_embedding_model_version`.
- Valeurs typées par convention : `joelito/legal-swiss-longformer-base@v1.0`, `BAAI/bge-m3@v1.5`, `legal-swiss-longformer-ch-caselaw-ft-v1.0-20260601`.
- Index partiel : `CREATE INDEX ... WHERE model_version = 'current'` pour accélérer les bascules.
- Table `embedding_model_registry(model_version PK, dim, normalized, trained_on, deployed_at, status)` : une source de vérité pour savoir quel modèle est actif, déprécié, archivé.

### 8.3 Règle d'or

**Un index ANN = un seul `model_version`**. Mélanger des vecteurs issus de modèles différents dans le même index casse la métrique de distance. La bascule passe donc par deux index co-existants (cf. § 9).

---

## 9. Compatibilité rétrocompatible BGE-M3

### 9.1 Schéma transitionnel

Pendant la durée du ré-encodage (jusqu'à 8 semaines) :

- Table `chunk_embeddings` **conserve deux colonnes vecteur** :
  - `embedding vector(768)` (Longformer, nouveau)
  - `embedding_legacy vector(1024)` (BGE-M3, ancien — copié depuis la source sqlite-vec lors de la Phase 2)
- Deux index ANN actifs :
  - `idx_chunk_embeddings_diskann` sur `embedding` (pgvectorscale, Longformer)
  - `idx_chunk_embeddings_legacy_hnsw` sur `embedding_legacy` (pgvector HNSW, BGE-M3) — HNSW choisi car build plus rapide et bascule temporaire.
- Colonne `model_version` discrimine à la requête.

### 9.2 Politique de routage par requête

- Par défaut, requête dirigée vers l'index Longformer.
- Flag runtime `use_legacy_embedding = true` (header HTTP ou paramètre MCP) pour forcer l'index BGE-M3 — utile pour A/B testing et pour les chunks pas encore ré-encodés (détectés par jointure avec `embedding_jobs`).
- Fallback automatique : si le chunk ciblé n'a pas encore de Longformer embedding (`embedding IS NULL`), la logique de ranking utilise son BGE-M3 quand disponible, avec une pénalité calibrée sur le score composite (cf. Phase 7).

### 9.3 Critère de retrait de BGE-M3

- **Couverture Longformer ≥ 99.5 %** des chunks ET
- **A/B test sur 1 semaine** confirme Longformer ≥ BGE-M3 sur nDCG@10 prod ET
- **Benchmark Phase 9** validé.

Puis :
1. `DROP INDEX idx_chunk_embeddings_legacy_hnsw;`
2. `ALTER TABLE chunk_embeddings DROP COLUMN embedding_legacy;`
3. `VACUUM FULL chunk_embeddings;` (fenêtre de maintenance).

### 9.4 Rollback

Si Longformer sous-performe de manière catastrophique : bascule inverse en 1 minute (changement de flag routing). L'index legacy reste en place jusqu'au retrait explicite. Scénario de rollback documenté dans le runbook Phase 4.

---

## 10. Observabilité

### 10.1 Métriques temps d'encodage

- `embedding_encode_duration_seconds{model_version, seq_len_bucket, batch_size_bucket}` (histogramme)
- `embedding_encode_total{model_version, status=success|error}` (compteur)
- `embedding_queue_depth{shard}` (gauge)
- `embedding_batch_fill_ratio` (gauge ; cible > 0.8)

### 10.2 Métriques de distribution

- Distribution des normes `||embedding||_2` : doit rester concentrée autour de 1.0.
- Distribution des dimensions (moyenne, écart-type, skew) calculée quotidiennement sur un échantillon de 10 k vecteurs. Dérive > 10 % vs baseline → alerte.
- Distribution par langue : pour chaque langue (DE/FR/IT), mesure du centroïde et de l'écart intra-classe. Écart croissant entre langues signale un drift.

### 10.3 Métriques de drift qualité

- **nDCG@10 canary** : batch quotidien de 50 requêtes du benchmark Phase 9 lancé en prod, alerte si dégradation > 3 points vs la veille.
- **Hit-rate vs BGE-M3** : pendant la période de double index, taux de cas où Longformer et BGE-M3 s'accordent sur le top-1. Si ce taux s'effondre, signal d'investigation (sans que cela soit nécessairement un bug — ce peut être un gain).
- **Corrélation score ANN / score cross-encoder** (Phase 7) : baisse de corrélation → embedding de moins bonne qualité.

### 10.4 Métriques d'index

- Taille sur disque, fragmentation, ratio `vacuum_full` nécessaire.
- Profondeur graphe DiskANN (`pgvectorscale.disk_ann_stats()` si exposé) : écarts vs `num_neighbors` théorique.
- `shared_buffers` hit ratio sur les pages d'index.

### 10.5 Dashboards

Grafana (ou équivalent) avec 4 panneaux :
1. Pipeline ré-encodage : progress bar, ETA, débit par worker.
2. Latence recherche sémantique : p50/p95/p99 chunk et doc.
3. Qualité : nDCG canary, distribution de normes, drift dim.
4. Santé service d'inférence : GPU util, memory, queue depth.

---

## 11. Risques et mitigations

| # | Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Coût GPU ré-encodage dépasse budget | Moyenne | Moyen | Bucketing longueur, quantization int8 CPU pour tail P4, spot instances. Plafond de dépense configuré. |
| R2 | Longformer Swiss < BGE-M3 sur sous-ensembles (FR, cantonaux) | Faible-Moyen | Élevé | Double index pendant 8 sem, rollback 1-min, fine-tuning optionnel § 7 |
| R3 | Export ONNX de Longformer échoue (sliding-window attention) | Faible | Moyen | Plan B : PyTorch pur avec `torch.compile`, perf ~0.7× ONNX mais acceptable pour régime permanent |
| R4 | Drift qualité entre DE/FR/IT | Moyenne | Moyen | Eval trilingue obligatoire en Phase 9, stratification des corpus d'évaluation |
| R5 | Latence DiskANN dépasse 50 ms p95 sous charge | Faible | Élevé | Tune `query_search_list_size`, `query_rescore`, RAM plus élevée, partition par langue si besoin |
| R6 | Contention Postgres pendant bulk insert | Moyenne | Moyen | COPY batchés, `maintenance_work_mem`, fenêtre de maintenance prévue pour VACUUM |
| R7 | Bug dans le calcul de `model_version` → mélange vecteurs dans l'index | Faible | Critique | CHECK constraint, tests d'intégration golden, monitoring de dispersion intra-index |
| R8 | Chunks trop longs (> 4 096 tokens) tronqués silencieusement | Moyenne | Faible | Enforce chunking Phase 3 à 512 tokens max. Compteur de troncature émis par le service d'inférence, alerte si > 0.1 % |
| R9 | Incompatibilité pgvectorscale version Supabase self-hosted | Faible | Élevé | Pinner version stable, image Docker custom si besoin, test sur staging avant bascule |
| R10 | Perte de travail si crash du dispatcher ré-encodage | Moyenne | Faible | Checkpointing DB-native (§ 6.3), reprise idempotente |
| R11 | Fine-tuning apporte gain décevant (< +5 pts) | Moyenne | Faible | Feature optionnelle, pas de bloquant Phase 4. Décision post-Phase 9 |
| R12 | Coût stockage index > budget disque | Faible | Moyen | Compression SBQ (pgvectorscale `memory_optimized`), monitoring taille, provisioning SSD adéquat |

---

## 12. Definition of Done

La Phase 4 est considérée terminée lorsque **toutes** les conditions suivantes sont satisfaites :

### 12.1 Infrastructure

- [ ] Service d'inférence déployé en prod, avec au minimum 1 instance GPU et fallback CPU int8 fonctionnel.
- [ ] Export ONNX du modèle `joelito/legal-swiss-longformer-base` validé (parité numérique avec PyTorch à ε = 1e-4 sur 1 000 inputs de test).
- [ ] Batching dynamique avec bucketing de longueur opérationnel, taux de remplissage moyen ≥ 0.75.
- [ ] Dashboards Grafana provisionnés (4 panneaux § 10.5).
- [ ] Alertes Prometheus configurées (latence, queue depth, drift dim).

### 12.2 Schéma et indexation

- [ ] Table `chunk_embeddings` avec colonnes `embedding vector(768)`, `model_version TEXT`, `embedding_legacy vector(1024)` (transition).
- [ ] Colonne `decisions.regeste_embedding vector(768)` peuplée pour 100 % des décisions ayant un regeste.
- [ ] Index `idx_chunk_embeddings_diskann` créé avec paramètres `num_neighbors=50, search_list_size=100, storage_layout=memory_optimized`.
- [ ] Index `idx_decisions_regeste_hnsw` créé avec `m=16, ef_construction=64`.
- [ ] Contrainte ou validation garantissant `||embedding||_2 ≈ 1.0`.
- [ ] Table `embedding_model_registry` alimentée.

### 12.3 Ré-encodage

- [ ] Tranche P0 (ATF) : 100 % des chunks encodés en Longformer, délai < 48 h atteint.
- [ ] Tranches P1 et P2 : ≥ 95 % encodés.
- [ ] Pipeline de checkpointing testé sur un crash simulé (redémarrage worker, reprise sans perte).
- [ ] Sharding déterministe validé (pas de doublon, pas de trou).
- [ ] Ré-encodage complet ≥ 99.5 % atteint OU planning confirmé à ≤ 8 semaines avec suivi hebdomadaire.

### 12.4 Performance

- [ ] Latence top-50 ANN chunk p50 ≤ 50 ms sur bench standard (1 000 requêtes, corpus chaud).
- [ ] Latence top-100 ANN chunk p95 ≤ 100 ms.
- [ ] Latence top-20 HNSW regeste p95 ≤ 15 ms.
- [ ] Recall@10 vs brute-force ≥ 0.97 sur échantillon 1 k requêtes.

### 12.5 Qualité

- [ ] nDCG@10 Longformer ≥ nDCG@10 BGE-M3 sur le bench 200 requêtes (Phase 9 preview). Cible forte : +5 points.
- [ ] Écart DE/FR/IT sur nDCG@10 ≤ 3 points.
- [ ] Aucune régression sur requêtes de golden set (subset critique de 30 requêtes ATF).

### 12.6 Compatibilité

- [ ] Double index BGE-M3 / Longformer fonctionnel, flag runtime `use_legacy_embedding` opérationnel.
- [ ] Rollback documenté et testé en staging (bascule Longformer → BGE-M3 en < 5 min).
- [ ] Plan de retrait de l'index legacy publié (critères § 9.3).

### 12.7 Observabilité

- [ ] Toutes les métriques § 10.1 à § 10.4 exposées et alimentées.
- [ ] Canary quotidien nDCG@10 opérationnel avec alerte.
- [ ] Logs structurés (JSON) incluant `model_version`, `seq_len`, `batch_size`, `request_id`.

### 12.8 Documentation

- [ ] Runbook opérateur : procédure ré-encodage, bascule, rollback, incidents courants.
- [ ] Guide développeur : API du service d'inférence, codes d'erreur, quotas.
- [ ] Lien croisé avec `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md` mis à jour.
- [ ] Décision documentée pour le fine-tuning optionnel (GO / NO-GO / REPORT avec justification).

### 12.9 Jalon de bascule vers Phase 5

La Phase 5 (enrichissement PA-RAG) peut démarrer dès que :
- Tranche P0 + P1 ré-encodées (≥ 2.9 M chunks),
- Index DiskANN actif,
- Latence et recall mesurés conformes.

Le ré-encodage des tranches P3 et P4 peut se poursuivre en tâche de fond parallèlement à la Phase 5.

---

## Annexes

### A. Références croisées

- Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`
- Rapport PA-RAG : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md` (§ 4.2 enrichissement métadonnées, § 7.4 modèles d'embeddings)
- Implémentation actuelle BGE-M3 / sqlite-vec : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py`
- Chunker actuel (à moderniser en Phase 3) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py`
- Sous-plan Phase 3 (chunking SAC) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-3-chunking-sac.md` (dépendance amont)
- Sous-plan Phase 5 (enrichissement) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-5-enrichissement-pa-rag.md` (consommateur aval)
- Sous-plan Phase 7 (retrieval hybride) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-7-retrieval-hybride.md` (consommateur aval)
- Sous-plan Phase 9 (évaluation) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-9-evaluation.md` (bench de validation)

### B. Invariants respectés du plan-maître

- **Parité MCP/REST** : l'interface de recherche sémantique change de backend mais expose les mêmes signatures de tool (`search_semantic`, `search_hybrid`, etc.) et routes REST.
- **Citations préservées** : la Phase 4 ne touche pas au graphe de citations.
- **Word add-in / Claude Desktop** : aucun changement côté client. Les MCP tools absorbent la bascule embedding en interne.
- **Dataset HuggingFace Parquet** : schéma inchangé. Les embeddings ne sont pas exposés en Parquet public (trop volumineux, pas utiles aux consommateurs externes).

### C. Hors-scope explicite

- Génération de questions à partir de chunks (HyDE, query expansion) : relève de Phase 7.
- Cross-encoder reranking : Phase 7.
- Authority rerank (formule composite) : Phase 7.
- Enrichissement métadonnées (4 piliers PA-RAG, sort de l'affaire) : Phase 5.
- PageRank temporel sur citations : Phase 5.
- GraphRAG léger : Phase 8.
- MCP Edge Functions : Phase 6.

### D. Paramètres nominaux à réviser avant mise en prod

Tous les paramètres ci-dessous sont des **valeurs nominales de départ**. Ils doivent être confirmés ou ajustés après benchs sur données réelles (cf. § 10 observabilité et Phase 9 évaluation).

- DiskANN : `num_neighbors`, `search_list_size`, `max_alpha`, `storage_layout`, `query_search_list_size`, `query_rescore`.
- HNSW : `m`, `ef_construction`, `ef_search`.
- Inférence : `batch_size`, `batch_window_ms`, `sub_batch_size` par bucket de longueur.
- Quantization : int8 oui/non par profil (GPU/CPU).
- Normes : seuil d'alerte drift dim, seuil `batch_fill_ratio`.

---

_Fin du sous-plan Phase 4._
