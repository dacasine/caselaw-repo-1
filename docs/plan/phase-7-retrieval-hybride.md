# Phase 7 — Retrieval hybride + reranking + score d'autorité composite

> Sous-plan de la phase 7 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Durée cible : 2 semaines (après Phase 6 — MCP Edge Functions opérationnel).
> Dépendances amont : Phases 3 (chunks SAC), 4 (embeddings Longformer + pgvectorscale), 5 (PageRank temporel, validity_status, court_level, atf_published, ratio/obiter).
> Dépendance aval : Phase 9 (benchmark 200 requêtes pour calibration des poids).
> Source conceptuelle : rapport PA-RAG `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`, sections 2.5, 4, 6.

Cette phase transforme le moteur de recherche existant (FTS5 SQLite + ANN sqlite-vec sur chunks trop courts) en un pipeline PA-RAG à trois étages, livré via Supabase/Postgres, exposé par les Edge Functions de la Phase 6 et les routes REST `web_api/main.py`. L'objectif est que chaque tool MCP concerné — `search_decisions`, `find_leading_cases`, `get_doctrine`, `draft_mock_decision`, `cite_check` — puisse solliciter, au choix, le pipeline historique (comportement compatible) ou le pipeline PA-RAG composite (nouveau comportement, opt-in puis défaut).

---

## 1. Objectifs et critères de succès

### 1.1 Objectifs fonctionnels

1. Implémenter un retrieval hybride BM25 + ANN sur la table `chunks` produite en Phase 3, avec fusion RRF.
2. Brancher un cross-encoder de reranking sur les top-100 candidats fusionnés, produisant un top-20 à haute précision.
3. Calculer pour chaque candidat un score composite `s_i = w_text·TextSim + w_cit·PageRank_temporel + w_court·Authority + w_temp·Temporal·Validity` réordonnant le top-20 en top-k final (typiquement k ∈ {5, 10}).
4. Exposer l'activation via un flag `authority_rerank=true` sur `search_decisions` (par défaut `false` en phase 7.x pour non-régression), avec paramètres `weights={text,cit,court,temp}` optionnels par appel.
5. Offrir un tool miroir `search_decisions_parag` pour les clients désirant le comportement PA-RAG par défaut sans toucher à `search_decisions`.
6. Conserver la parité avec les 23 tools MCP et les ~30 routes REST (invariant 1 du plan-maître).

### 1.2 Critères de succès mesurables

La phase est « verte » si, sur le benchmark Phase 9 (200 requêtes annotées, multilingue DE/FR/IT, mix ATF/cantons/1re instance) :

- **nDCG@10** ≥ 0.72 (baseline FTS5+ANN actuelle mesurée ≈ 0.58, cible calquée sur l'étude Harvard Law — rapport § 2.5).
- **Precision@10** ≥ 0.65 sur les requêtes « dogmatiques » (identification de leading case attendu).
- **Authority Correctness** (fraction du top-5 constituée de décisions ATF ou de 2e instance cantonale pertinente) ≥ 0.75 ; sous la formule composite avec poids par défaut 0.25/0.25/0.25/0.25, l'étude Harvard rapporte un PageRank moyen des résultats de 0.213 contre 0.026 pour un RAG naïf (p < 0.001) — on cible un écart du même ordre sur notre corpus.
- **Negative Treatment Detection** : zéro arrêt `validity_status = 'overruled'` dans le top-5 par défaut (via multiplicateur de validité à 0.1, cf. § 7).
- **Latence p50** ≤ 900 ms, **p95** ≤ 1800 ms pour un appel `search_decisions` avec `authority_rerank=true`, top-10 renvoyé, filtres durs appliqués. Budget détaillé : BM25+ANN ≤ 120 ms, RRF ≤ 5 ms, cross-encoder (top-100 → top-20) ≤ 600 ms sur GPU dédiée ou 1200 ms sur CPU avec MiniLM, composite ≤ 10 ms.
- **Parité** : les 23 tools MCP et les 30 routes REST continuent de passer leurs tests golden (jeu de requêtes figé Phase 1), l'activation PA-RAG est désactivable par flag sans redéploiement.

### 1.3 Critères de succès non fonctionnels

- Observabilité : chaque appel journalise les scores bruts et normalisés par phase (BM25 rank, ANN rank, RRF score, cross-encoder score, chacun des quatre termes du composite, score final), identifiant de requête, poids effectifs, filtres appliqués.
- Reproductibilité : deux appels identiques (même requête, mêmes filtres, même version de modèle) renvoient le même ordre. Toute non-déterminisme cross-encoder (dropout) est désactivé en inférence.
- Tolérance aux pannes : si le cross-encoder échoue (timeout, OOM GPU), fallback sur le score RRF pur + composite ; si PageRank est absent (décision trop récente, non encore recalculée), substitution par médiane de la cohorte court+décennie.

---

## 2. Architecture trois phases — vue d'ensemble

Le pipeline reproduit la décomposition recall/precision/authority de la section 4 du rapport, adaptée au stack Supabase du plan-maître.

```
Requête utilisateur + filtres durs
          │
          ▼
┌──────────────────────────────────────────────────┐
│ Phase A — Recall (top-100 parallèle + RRF)       │
│ ┌────────────────┐     ┌──────────────────────┐  │
│ │ BM25 chunks    │     │ ANN embed(query)     │  │
│ │ (ParadeDB ou   │     │ pgvectorscale        │  │
│ │  tsvector)     │     │ StreamingDiskANN     │  │
│ └────────┬───────┘     └──────────┬───────────┘  │
│          └────────── RRF k=60 ────┘              │
│          → Top 100 chunks fusionnés              │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ Phase B — Precision (cross-encoder → top 20)     │
│ cross-encoder/ms-marco-MiniLM-L-6-v2             │
│ fine-tuné corpus suisse DE/FR/IT                 │
└──────────────────┬───────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────┐
│ Phase C — Authority rerank (composite)           │
│ s = w_t·TextSim + w_c·PageRank_tmp +             │
│     w_j·Authority + w_τ·Temporal·Validity        │
└──────────────────┬───────────────────────────────┘
                   ▼
        Top-k final (défaut k=10)
```

### 2.1 Pourquoi trois phases distinctes

- **Phase A (recall)** : un cross-encoder seul sur tout le corpus est infaisable (965 k décisions × ~10 chunks = ~10 M paires par requête). BM25 et ANN sont des filtres pré-sélecteurs peu coûteux, complémentaires : BM25 capte les chaînes exactes (`art. 285 LP`, `ATF 148 III 95`) que les embeddings dilueraient ; ANN capte la paraphrase, la requête en langage naturel, la transposition DE↔FR↔IT via embeddings multilingues.
- **Phase B (precision)** : un cross-encoder traite la paire (query, chunk) conjointement dans un seul forward, modélisant des interactions token-à-token impossibles pour un bi-encoder. Le gain typique est +10 à +15 points de nDCG sur le top-20 (cf. étude KTH sur IR juridique, rapport réf. 29).
- **Phase C (authority)** : la similarité textuelle, même parfaite, ignore la structure normative. L'étude Harvard Law (corpus Fair Use) démontre empiriquement qu'un RAG naïf choisit des décisions textuellement proches mais faiblement autoritaires (PageRank 0.026 vs 0.213 pour le RAG structuré, rapport § 2.5).

### 2.2 Trade-offs de la suppression d'une phase

- **Sans Phase A** : impossible (coût prohibitif). Fallback seulement si le cross-encoder reçoit un filtre dur produisant déjà moins de 200 candidats.
- **Sans Phase B** : gain de latence ~600 ms, perte nDCG estimée à 8-12 points, fort impact sur requêtes paraphrastiques. Mode dégradé documenté, activable par `skip_cross_encoder=true`.
- **Sans Phase C** : équivalent au RAG classique. Mode par défaut jusqu'à bascule, utile pour comparaisons benchmark.
- **Sans Phase A ni B (composite seul sur un candidate set fourni)** : mode interne pour `cite_check` quand l'appelant passe déjà une liste de decision_id ; on applique alors uniquement la Phase C pour classer par autorité.

### 2.3 Granularité : chunks partout, agrégation tard

Le retrieval opère sur `chunks` (granularité considérant, cf. Phase 3). L'agrégation au niveau décision n'intervient qu'après le cross-encoder (cf. § 10). Cette décision répond à la recommandation LegalBench-RAG (rapport § 3.1) : la précision span-level est la métrique dominante pour les citations vérifiables. Un reranking d'abord agrégé perdrait l'information considérant et compliquerait la justification des citations dans la réponse LLM.

---

## 3. Phase A — BM25 : choix techno et tuning

### 3.1 Trois options envisagées

| Option | Description | Avantages | Inconvénients |
|---|---|---|---|
| **ParadeDB** | Extension Postgres dédiée BM25, construite sur Tantivy | BM25 « vrai », analyseurs multilingues, MATCH syntaxe, scoring Okapi BM25 standard | Ajoute une extension, licence AGPLv3 (à valider pour Supabase self-hosted), stabilité < pg_trgm |
| **tsvector + ts_rank_cd** | Standard Postgres natif | Zéro dépendance, déjà présent, RUM index possible pour perf | Ranking n'est pas BM25 « pur » mais une variante cover density, moins efficace sur corpus très hétérogènes en longueur |
| **Elasticsearch externe** | Cluster ES dédié, indexation miroir | BM25 de référence, analyseurs DE/FR/IT matures, highlighting, percolator | Dual-write à maintenir, coûts infra, latence réseau, contradiction avec l'invariant « Postgres-native » du plan-maître |

### 3.2 Recommandation : ParadeDB prioritaire, tsvector fallback

Le plan-maître (§ Stack cible) mentionne explicitement « ParadeDB BM25 ou tsvector fallback ». Nous suivons cet ordre :

- **Tentative 1** : ParadeDB sur la table `chunks` avec un index BM25 pondérant les champs `title` (poids ×3), `regeste` (×2), `chunk_text` (×1), `headnote` (×2 si présent). Ce multi-field ranking reproduit la logique Elasticsearch et exploite les champs SAC produits en Phase 3.
- **Tentative 2** (si ParadeDB bloquant — licence, stabilité, build image Docker) : tsvector avec `setweight`/`ts_rank_cd`, vecteurs tsvector séparés par champ combinés à l'interrogation. Perte attendue : 3-5 points de nDCG, acceptable comme mode de repli.
- **Rejet Elasticsearch** : risque opérationnel (dual-write) et rupture de l'invariant architectural ; gardé comme option pour Phase 10+ si les benchmarks montrent une insuffisance structurelle.

### 3.3 Multilinguisme DE / FR / IT

Le corpus suisse est trilingue (plus quelques EN pour la doctrine). Deux stratégies :

- **Index multilingue unique** : un seul index BM25, analyseur `swiss_multilang` (stop-words fusionnés DE+FR+IT, stemming désactivé ou minimal pour éviter les faux amis : « Urteil »/« urteilen », « jugement »/« juger »). Inconvénient : le stemming germanique agressif casse les décompositions, et les stopwords cross-langues peuvent gommer des termes utiles (« der » = article DE mais aussi partie d'un nom FR).
- **Index par langue + routage** : détection de langue du chunk (champ `language` déjà produit en Phase 3), trois index BM25. La requête est routée selon `filter.language` si fourni, sinon dupliquée sur les trois index et fusionnée par RRF inter-langue avant la fusion BM25↔ANN.

**Choix** : un index par langue, avec stemming Snowball DE/FR/IT, stopwords juridiques augmentés (art., al., ch., ss., ATF, SJ, RVJ…). La fusion RRF trilingue est produite avant RRF global : c'est une RRF hiérarchique (trois listes de langues → une liste BM25 ; puis BM25 + ANN → liste finale).

### 3.4 Pondération de champs

La Phase 3 produit pour chaque chunk les champs : `title` (intitulé de la décision), `regeste`/`headnote` (résumé officiel ou généré par SAC), `chunk_text` (texte du considérant), `chunk_summary` (résumé LLM SAC), `keywords_extracted`. Poids recommandés (à calibrer en Phase 9) :

- `title` : 3.0 — contient les références légales clés et l'objet de l'arrêt
- `regeste` : 2.5 — le Tribunal fédéral y condense le principe retenu
- `chunk_summary` : 1.8 — SAC augmente le signal contextuel
- `keywords_extracted` : 1.5 — termes juridiques canoniques
- `chunk_text` : 1.0 — le texte brut, référence

Ces poids sont stockés dans la table de configuration `retrieval_config` (nouvelle, créée par migration Phase 7), versionnée pour rollback.

### 3.5 Requête BM25

La requête BM25 est construite à partir de la requête utilisateur par :

1. Détection d'entités juridiques (regex : numéros ATF `ATF \d+ [IVX]+ \d+`, articles `art\. \d+[a-z]*( al\. \d+)?( \w+)?`, références de loi `LP|CO|CC|LTF|CPC|CPP|CP|LDIP|…`). Ces entités sont extraites et injectées comme clauses `+term` (obligatoires, boost ×2) dans la requête BM25.
2. Expansion synonymique juridique (table `legal_synonyms` construite en Phase 5 : `locataire↔Mieter`, `usufruit↔Nutzniessung`, etc.), utilisée seulement si la requête ne produit pas assez de hits BM25 (< 20) — pour éviter de noyer le signal.
3. Reste de la requête passé comme clause souple (OR).

Paramètres BM25 : `k1 = 1.2`, `b = 0.75` (valeurs Okapi standard). Un calibrage par grid search est prévu en Phase 9 sur un subset du benchmark (§ 8).

### 3.6 Top-N BM25

On retient **top 100 chunks** par langue interrogée, soit jusqu'à 300 pour une requête non filtrée, avant RRF. L'overhead est négligeable (fetch d'identifiants + scores), la déduplication par `chunk_id` intervient à la fusion.

---

## 4. Phase A — ANN : pgvectorscale et stratégie de filtrage

### 4.1 Choix de l'index

La Phase 4 aura livré un index **pgvectorscale StreamingDiskANN** sur `chunks.embedding` (768-dim, Legal Swiss Longformer). Les paramètres d'index (`num_neighbors`, `search_list_size`, `max_alpha`) sont calibrés Phase 4 pour top-50 < 50 ms sur le corpus complet (~10 M vecteurs). La Phase 7 réutilise cet index et se concentre sur la stratégie de filtrage et de recherche.

### 4.2 Paramètres de recherche

- `query_search_list_size` dynamique : 80 par défaut pour top-100 ; 120 si filtres durs réducteurs pré-ANN (pour compenser l'impact sur le recall) ; 200 en mode « deep recall » demandé par `find_leading_cases`.
- `rescore = 50` : récupération des candidats approximatifs puis re-scoring exact par produit scalaire, utile car l'index est en quantization.
- Normalisation des embeddings à l'indexation et à la requête (L2) ; similarité cosinus effective.

### 4.3 Pre-filter vs post-filter

Trois stratégies possibles pour appliquer les filtres durs SQL avec l'ANN :

1. **Post-filter** : on récupère top-500 ANN, on applique `WHERE jurisdiction = ...` ensuite. Risque : si le filtre est sélectif, on n'a plus assez de candidats (ex. top-500 sur tout le corpus, mais filtre canton Valais exclut 99 % → on retombe à 5 résultats).
2. **Pre-filter strict** : le filtre est injecté dans l'itération ANN (supporté par pgvectorscale via `label_filter` ou par un index partiel). Garantit le nombre de résultats mais peut dégrader la qualité (le parcours ANN explore moins efficacement si l'ensemble filtré est épars).
3. **Iterative filtering** (notre choix) : pre-filter si le filtre est large (> 10 % du corpus estimé), post-filter sinon avec relance automatique top-2000 si trop peu de hits post-filtrage. Le seuil de basculement est stocké dans `retrieval_config.prefilter_selectivity_threshold = 0.10`.

### 4.4 Filtres durs faibles → peu de candidats

Cas limite : filtre `canton = GL, validity_status != 'overruled', court_level >= 4, chunk_type = 'ratio'` + période courte ⇒ moins de 50 chunks dans tout le corpus. On procède alors en :

- Passer au recall « exhaustif » : seq-scan filtré (coût négligeable sur < 1 000 chunks) avec score cosinus brut, puis BM25 sur le même sous-ensemble.
- Skip de la Phase B cross-encoder si le sous-ensemble est < 20 candidats et que le composite suffit.
- Retour d'un champ `degraded_mode=true` dans la réponse avec explication pour l'utilisateur, afin qu'il sache que l'élargissement des filtres peut améliorer les résultats.

### 4.5 Top-N ANN

**Top 100 chunks** récupérés par l'ANN, mêmes critères que BM25 pour la fusion équilibrée.

---

## 5. Phase A — Reciprocal Rank Fusion

### 5.1 Formule

Pour chaque chunk `d` apparaissant dans les listes BM25 (`R_bm25`) ou ANN (`R_ann`) :

`RRF(d) = Σ 1 / (k + rank(d, R))` pour R ∈ {R_bm25, R_ann}, `rank(d, R)` étant 1-based, ∞ si `d ∉ R`.

### 5.2 Choix de k

`k = 60` est la valeur de référence de l'article fondateur de Cormack et al. (2009) et celle du rapport (§ 4.1). Elle offre un bon équilibre : un k très bas (10) donne trop de poids aux rangs de tête et pénalise les hits de rang 50-100 qui peuvent pourtant être pertinents ; un k très haut (200) lisse excessivement. Calibrage final en Phase 9 via grid search sur {20, 40, 60, 80, 100}.

### 5.3 Alternatives considérées et rejetées

- **Weighted sum normalisée** (`α·score_bm25_norm + (1-α)·score_ann_norm`) : nécessite une normalisation robuste des scores bruts, ce que BM25 et ANN rendent difficile (plages et distributions différentes, dépendant de la requête). RRF évite cette normalisation en n'utilisant que les rangs.
- **CombSUM / CombMNZ** (Fox & Shaw) : requièrent aussi normalisation, sensibles aux outliers.
- **Learned fusion** (modèle supervisé) : surpuissant pour un MVP, reporté Phase 10+. Nécessite labels de pertinence en quantité.

### 5.4 Pondération asymétrique BM25 vs ANN

Extension de RRF avec poids : `RRF(d) = w_B·1/(k+rank_B) + w_A·1/(k+rank_A)`. Par défaut `w_B = w_A = 1`. On envisage `w_B = 1.3, w_A = 0.7` pour les requêtes contenant ≥ 1 référence juridique détectée (numéro ATF, article) — cf. § 3.5 — car BM25 est alors strictement supérieur. Détection heuristique, calibrage Phase 9.

### 5.5 Déduplication

Un même chunk peut apparaître dans BM25 et ANN (cas nominal). Une même décision peut produire plusieurs chunks dans le top 100 fusionné (ex. ATF long avec 12 considérants, 4 qui matchent).

- **Dédup chunk** : naturel via `chunk_id`, aucun traitement supplémentaire.
- **Dédup décision** : *non appliqué* en sortie de Phase A. On conserve les multiples chunks d'une même décision pour que le cross-encoder puisse choisir le meilleur. L'agrégation se fait plus tard (cf. § 10).
- **Cap par décision** : limite douce à 5 chunks par `decision_id` dans le top-100 fusionné pour éviter qu'une décision volumineuse n'occupe tout le budget cross-encoder. Les chunks au-delà sont stockés dans une liste secondaire et réinjectés si le top-20 cross-encoder reste dominé par d'autres chunks de la même décision (dédup tardif).

---

## 6. Phase B — Cross-encoder

### 6.1 Choix du modèle de base

Options évaluées :

- `cross-encoder/ms-marco-MiniLM-L-6-v2` — 22M params, latence CPU ~80 ms/paire, qualité de base MS-MARCO ; recommandé par le rapport (§ 4.1, 9.1).
- `cross-encoder/ms-marco-MiniLM-L-12-v2` — 33M params, +3 points nDCG, latence ×1.8.
- `BAAI/bge-reranker-v2-m3` — multilingue natif, 568M params, top qualité mais latence 4-6× supérieure.
- `jinaai/jina-reranker-v2-base-multilingual` — 278M, multilingue, bon compromis qualité/latence.

**Choix** : `cross-encoder/ms-marco-MiniLM-L-6-v2` fine-tuné sur corpus suisse (détail § 6.3), avec `bge-reranker-v2-m3` comme option haut-de-gamme activable par `reranker="bge"` pour requêtes critiques (`draft_mock_decision`, audits). Le MiniLM fine-tuné atteint ~85 % de la qualité du BGE pour 5 % du coût compute, ce qui est le bon trade-off pour le trafic courant.

### 6.2 Quantification et inférence

- Export ONNX, quantification int8 via ONNX Runtime. Gain latence CPU ×2-3, perte qualité < 0.5 point nDCG.
- Servi dans un worker dédié (Edge Function avec runtime CPU renforcé, ou service Python FastAPI séparé si Edge Function ne suffit pas — à trancher Phase 6). Batch dynamique (§ 6.5).
- GPU optionnelle (NVIDIA T4 suffit) pour p95 cible ; sans GPU, cible p95 atteinte par batch + quantification.

### 6.3 Corpus de fine-tuning suisse

Fine-tuning sur paires (query, passage) construites à partir :

1. **Paires ATF-regeste** : chaque regeste d'ATF devient une requête, le considérant « ratio » correspondant est positif ; négatifs = regestes d'autres ATF de la même matière (hard negatives) + chunks aléatoires (easy negatives). Cible ~50 k paires positives × 4 négatifs.
2. **Paires « question plaidoirie → considérant »** synthétiques, générées par LLM (synthetic.new, GLM-5.x Reasoning comme en Phase 5) à partir des 10 k ATF les plus cités : pour chaque considérant ratio, demander au LLM de formuler 3 questions naturelles auxquelles ce considérant répond.
3. **Paires issues du benchmark d'évaluation** : split train/test rigoureux ; jamais entraîner sur les 200 requêtes du benchmark Phase 9.

Loss : `MarginMSE` ou `CrossEntropy` sur triplets (distillation d'un enseignant plus gros, ex. `bge-reranker-v2-m3`, sur les mêmes paires, recommandé par la littérature sentence-transformers).

Le modèle est versionné (`retrieval_config.cross_encoder_version`), rollback trivial.

### 6.4 Multilinguisme

Le MiniLM de base est principalement EN. Le fine-tuning multilingue est essentiel. Deux voies :

- Distillation trilingue : enseignant multilingue (`bge-reranker-v2-m3`), étudiant MiniLM initialisé à partir d'un checkpoint multilingue (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) puis entraîné sur les paires suisses DE/FR/IT.
- Cross-lingual : requête FR, passage DE → le reranker doit encore scorer haut. Garantie par inclusion explicite de 15 % de paires cross-lingual dans le training set.

### 6.5 Latence vs qualité, batch size

- **Latence cible** : top-100 → top-20 en ≤ 600 ms sur GPU ou ≤ 1 200 ms sur CPU quantifié.
- **Batch** : 16 paires par batch (tuning empirique, dépend de la longueur max). Longueur max tronquée à 512 tokens (requête ~32 + chunk ~480). Les chunks SAC font ~400-512 tokens, compatible.
- **Early exit** : si les 10 premières paires ont toutes un score < seuil τ_low, abandon des 90 suivantes et retour des 10 meilleurs du RRF. Gain ~80 % du temps sur les requêtes très hors-domaine. τ_low calibré Phase 9.
- **Caching** : cache LRU (query_hash, chunk_id) → score cross-encoder, TTL 24 h. Hit rate attendu ~30 % pour trafic répété (tools comme `cite_check` revisitant les mêmes paires).

### 6.6 Top-20 en sortie

On garde 20 candidats pour la Phase C. 20 est un compromis : assez pour que la rerank autorité puisse promouvoir un ATF en tête même s'il était en 15ᵉ position textuelle ; pas trop pour maintenir la latence et la lisibilité de l'observabilité.

---

## 7. Phase C — Score composite d'autorité

### 7.1 Formule détaillée

Pour chaque candidat `i` du top-20 issu du cross-encoder :

`s_i = w_text · TextSim_i + w_cit · PageRank_temporel_i + w_court · Authority_i + w_temp · Temporal_i · Validity_i`

avec `w_text + w_cit + w_court + w_temp = 1`.

Chaque composant :

- `TextSim_i` : score cross-encoder normalisé min-max sur le top-20 (§ 7.3).
- `PageRank_temporel_i` : valeur stockée dans `decisions.pagerank_temporal` (Phase 5), normalisée min-max sur le top-20. C'est un PageRank time-decayed (facteur `e^{-λ·Δt}` sur les arêtes entrantes, λ calibré Phase 5) qui compense le biais de récence signalé par le rapport (§ 2.2).
- `Authority_i` : score statique de cour, défini comme :
  - TF publié ATF : 1.00
  - TF non publié : 0.85
  - TAF / TPF : 0.70
  - Tribunal cantonal 2e instance : 0.55
  - Tribunal cantonal 1re instance : 0.35
  - Autorité administrative / régulateur : 0.25
  - Étranger (persuasif) : 0.15
  Ces valeurs sont stockées dans `court_authority_scores` (migration Phase 7). Elles reflètent la hiérarchie décrite au rapport § 2.1.
- `Temporal_i` : facteur de fraîcheur, `Temporal_i = e^{-μ·(now - date)}` avec `μ = ln(2)/10` (demi-vie 10 ans, calibrage Phase 9). Compense le vieillissement doctrinal sans être brutal.
- `Validity_i` : **multiplicatif** et non additif, appliqué sur `Temporal_i` :
  - `valid` (défaut) : 1.0
  - `criticized` ou `distinguished` : 0.5
  - `partially_overruled` : 0.3
  - `overruled` ou `reversed` : 0.1
  - `unknown` : 0.9 (léger abattement, incertitude)

### 7.2 Validity multiplicatif vs additif

Justification du choix multiplicatif : un arrêt renversé doit voir son score effondré, pas simplement atténué. Avec additif (`+w_val · Validity`), un overruled avec très haut PageRank et Authority remonterait encore. Avec multiplicatif sur le terme temporel, l'effet est robuste : `0.1 · Temporal` annihile pratiquement la composante temporelle, et l'arrêt ne peut plus être top-5 sauf domination totale sur les trois autres axes. Ce design suit la recommandation PA-MA-RAG (rapport § 6, signal `N(c)` du Conflict Checker).

Alternative étudiée : pénalité additive forte (`s - 0.5` si overruled). Rejetée car asymétrique et non convexe (sort du simplex des poids).

### 7.3 Normalisation min-max : sur top-20 ou sur population

Deux niveaux possibles :

- **Sur population entière** (tous chunks du corpus) : stable entre requêtes, interprétable globalement (un PageRank normalisé de 0.9 signifie « top 10 % mondial »). Inconvénient : pour une requête dans un domaine niche, le top-20 peut vivre dans une bande étroite (ex. tous entre 0.02 et 0.08) et le composite perd en granularité.
- **Sur top-20** (relatif à la requête) : maximise le contraste pour l'utilisateur, mais un top-20 où tous sont faiblement autoritaires produit quand même un « meilleur » à 1.0.

**Choix hybride** : normalisation min-max **sur top-20** pour `TextSim` (contraste) ; **sur population, bornée au 99e percentile** pour `PageRank_temporel` et `Authority` (stabilité cross-requête) ; `Temporal_i` est déjà dans [0, 1] par construction ; `Validity_i` est catégoriel dans [0.1, 1.0].

Ce choix évite qu'une requête de niche fasse remonter un arrêt faiblement autoritaire comme « gagnant relatif ».

### 7.4 Rationale des poids par défaut

Le plan-maître et le rapport (§ 2.5) fixent `w_text = w_cit = w_court = w_temp = 0.25` comme baseline Harvard Law. Elle a le mérite d'être :

- **Neutre** (aucun axe privilégié ex ante) ;
- **Validée empiriquement** (p < 0.001 vs RAG naïf sur PageRank moyen) ;
- **Interprétable** (chaque axe vaut autant qu'un autre, explication simple à un juriste).

Elle est sous-optimale pour des sous-domaines (cf. § 8). L'API expose `weights` paramétrable par tool call.

### 7.5 Paramétrage par tool call

Signature PA-RAG (ajoutée à `search_decisions`, `search_decisions_parag`, `find_leading_cases`, `draft_mock_decision`) :

- `authority_rerank: bool` (défaut `false` en 7.x, `true` en 7.y après validation Phase 9)
- `weights: { text: float, cit: float, court: float, temp: float }` (défaut `{0.25, 0.25, 0.25, 0.25}`, somme vérifiée == 1.0 à ±0.01, renormalisée sinon)
- `include_overruled: bool` (défaut `false` — filtre dur complémentaire, voir § 9)
- `reranker: "minilm" | "bge" | "none"` (défaut `"minilm"`)
- `top_k: int` (défaut 10, max 50)

Les poids sont journalisés dans la trace de requête (cf. § 13).

---

## 8. Calibration des poids

### 8.1 Protocole général

1. Figer le benchmark Phase 9 : 200 requêtes annotées, chaque requête accompagnée de 5-10 décisions « ground truth » (leading case attendu, précédents proches, décisions à exclure).
2. Grid search sur `(w_text, w_cit, w_court, w_temp)` dans le simplex, pas de 0.05, total 1.0 → ~5 456 combinaisons. Pour chaque, calculer nDCG@10, Precision@10, Authority Correctness sur le benchmark.
3. Retenir la combinaison Pareto-optimale (front nDCG vs Authority Correctness). Cible : nDCG ne chute pas de plus de 2 points sous la combinaison max-nDCG, et Authority Correctness est maximisé.
4. Cross-valider par split 80/20 pour éviter l'overfit.

### 8.2 Poids adaptatifs par domaine juridique

Un seul quadruplet est probablement sous-optimal. Trois axes de variation à tester :

- **Par type de requête** :
  - « Trouver un leading case » → `w_cit` et `w_court` renforcés (0.35 / 0.35 / 0.2 / 0.1)
  - « Actualité d'une règle » → `w_temp` renforcé (0.2 / 0.2 / 0.2 / 0.4)
  - « Question technique sur un article précis » → `w_text` renforcé (0.4 / 0.2 / 0.25 / 0.15)
- **Par matière** : droit public / droit privé / droit pénal / droit fiscal → un quadruplet par matière, classifieur de requête simple (LLM ou règles + taxonomie) qui sélectionne.
- **Par juridiction cible** : recherche fédérale vs cantonale — une recherche cantonale privilégie `w_court` atténué (les 1re instance cantonales y sont pertinentes).

L'implémentation stocke ces profils dans `retrieval_weight_profiles`, le tool reçoit `weight_profile="leading_case_public_law"` (raccourci) ou `weights={...}` (explicite).

### 8.3 Calibration continue

Un job hebdomadaire re-calibre les poids à partir des feedbacks utilisateurs collectés (clics, ajouts au dossier, signalements « ce résultat est hors-sujet »). Tracking réservé Phase 9 (observabilité), implémentation v2.

---

## 9. Filtres durs

### 9.1 Liste exhaustive

| Filtre | Sémantique | Application |
|---|---|---|
| `jurisdiction` | Fédéral / code canton ISO (VD, VS, GE, ZH…) | Pre-filter ANN + clause SQL BM25 |
| `canton` | Alias de `jurisdiction` limité aux cantons | idem |
| `court_id` | Identifiant d'une cour spécifique | idem |
| `court_level_min` / `court_level_max` | Niveau hiérarchique (1 à 5) | idem |
| `chamber` | Chambre/cour au sein du TF | idem |
| `date_from` / `date_to` | Plage de `decision_date` | idem |
| `language` | DE / FR / IT (EN doctrine) | idem |
| `chunk_type` | `ratio` / `motivation` / `facts` / `dispositif` / `obiter` | clause SQL sur `chunks.chunk_type` |
| `exclude_obiter` | Raccourci de `chunk_type in (ratio, motivation, facts, dispositif)` | idem |
| `validity_status` | `!= overruled` par défaut si `include_overruled=false` | clause SQL |
| `atf_published` | Bool : restreindre aux arrêts publiés au recueil | clause SQL |
| `legal_area` | Taxonomie (droit public, civil, pénal, fiscal…) | clause SQL sur `decisions.legal_areas` |
| `article_cited` | Liste d'articles que la décision doit citer | join sur `decision_article_citations` |
| `sort_of_case` | `irrecevabilité / rejet / admission / admission_partielle` (schéma plan-maître) | clause SQL |
| `min_pagerank` | Seuil minimal sur `pagerank_temporal` | clause SQL |

### 9.2 Ordre d'application

1. Filtres indexables → pushdown Postgres avant ANN/BM25.
2. `exclude_obiter` et `chunk_type` → sur la table `chunks` directement.
3. `include_overruled=false` → par défaut, retire les `validity_status = 'overruled'` du candidate set. Un flag explicite `include_overruled=true` lève ce filtre, car un cite_check peut vouloir montrer qu'un arrêt est overruled.

### 9.3 Impact sur le recall et mitigation

Un filtre trop strict peut laisser < 50 candidats. Mitigation :

- Détection précoce : après pre-filter, si `count_chunks < 200`, bascule en mode « exhaustif » (§ 4.4) et skip Phase B si `< 20`.
- Signalement : champ `applied_filters_reduced_corpus_to: N` dans la réponse, avec suggestion d'élargissement (« retirez le filtre `canton=GL` pour 12 × plus de résultats »).
- Feedback UX : le Word add-in et le front REST afficheront ce signal (cosmétique, hors scope Phase 7 côté UI).

### 9.4 Filtre « autorité minimale » implicite

Par défaut, aucun filtre d'autorité minimale n'est imposé (toute décision est recherchable). Le score composite se charge de prioriser. Option `min_court_level=3` pour exclure les 1re instance, utile pour les requêtes à portée fédérale.

---

## 10. Chunks vs documents : stratégie d'agrégation

### 10.1 Retrieval au chunk, rerank tardif au document

Le pipeline opère sur chunks jusqu'à la sortie du cross-encoder. Deux stratégies d'agrégation testées :

- **Top-chunk par décision** : pour chaque décision apparaissant dans le top-20 chunks, conserver seulement le meilleur chunk. L'autorité composite s'applique sur la paire (chunk, decision).
- **Score décision = max(chunks)** ou `= logsumexp(chunks)` : agrégation explicite.

**Choix** : `top-chunk par décision` pour le classement, mais **tous les chunks passants** retournés dans la réponse, avec le score du meilleur comme score de tri et les autres listés sous `supporting_chunks` (utile pour `draft_mock_decision` qui a besoin de plusieurs passages d'un même ATF).

### 10.2 Taille du top final

- Top-k de décisions (distinct) : par défaut 10, max 50.
- Nombre de chunks total retournés : par défaut 30 (10 décisions × 3 chunks max chacune), max 100.

### 10.3 Citations et spans

Le rapport (§ 3.1) insiste sur la précision span-level pour les citations vérifiables. On retourne pour chaque chunk son offset (`char_start`, `char_end`) dans la décision source, ce qui permet au Word add-in et à `cite_check` de surligner le passage.

---

## 11. Intégration dans les tools MCP et routes REST

### 11.1 Tools bénéficiaires

| Tool | Comportement actuel | Changement Phase 7 |
|---|---|---|
| `search_decisions` | FTS5+ANN, top 20 | Ajout flags `authority_rerank`, `weights`, `include_overruled`, `reranker`, `top_k`. Défaut inchangé en 7.x (pas de breaking change). |
| `search_decisions_parag` | N/A | Nouveau tool, PA-RAG activé par défaut, mêmes paramètres que `search_decisions` mais `authority_rerank=true` par défaut. |
| `find_leading_cases` | Cherche ATF publié par matière | Basculé sur PA-RAG avec `weight_profile="leading_case"`, top-k élargi (20), `atf_published=true`. |
| `get_doctrine` | Récupère articles doctrine | PA-RAG optionnel sur la partie jurisprudence citée ; `w_cit` renforcé. |
| `draft_mock_decision` | Génère un projet d'arrêt | Retrieval préalable en PA-RAG `authority_rerank=true` sur matière, utilisant `supporting_chunks` pour le drafting. |
| `cite_check` | Vérifie validité des citations | Phase C seule : prend la liste des decision_id à vérifier, calcule `Validity`, signale overruled/criticized, renvoie l'ordre par composite. |
| `find_similar_decisions` | kNN sur embedding | PA-RAG activable ; la similarité sert seulement de requête initiale. |
| `get_decision_context` | Résumé d'une décision | N/A (pas de retrieval). |
| 15 autres tools (fetch_*, list_*) | CRUD | Inchangés. |

### 11.2 Routes REST

Miroir intégral des tools (invariant parité). Les routes `/search`, `/search_parag`, `/leading_cases`, `/doctrine`, `/draft_decision`, `/cite_check` reçoivent les mêmes paramètres que les tools MCP correspondants. Couche partagée : un module `retrieval_parag.py` (ou son équivalent Edge Function TS si la Phase 6 a fini de migrer) expose la fonction unique appelée par MCP et REST.

### 11.3 Flag d'activation global

Variable d'environnement `PA_RAG_DEFAULT_ON=true|false` pour piloter la bascule sans redéploiement. En 7.x : `false` (opt-in). En 7.y, après verdict Phase 9 : `true` (défaut activé, flag `authority_rerank=false` reste utilisable pour benchmark comparatif).

### 11.4 Compatibilité Word add-in et Claude Desktop

Le Word add-in (`tools/word-addin/`) appelle les tools MCP par stdio via bridge (Phase 6). La signature élargie est rétro-compatible (nouveaux champs optionnels). Claude Desktop idem. Les tests golden Phase 1 tournent en CI : échec si un champ obligatoire ou un code d'erreur change.

---

## 12. Considération PA-MA-RAG (multi-agents) — v2

### 12.1 Quand basculer

Le rapport (§ 6) décrit sept agents : Issue Framing, Authority Planning, Retrieval, Precedent Ranking, Conflict Checker, Drafting, Verification. La Phase 7 livre une implémentation **monolithique** (un pipeline unique, éventuellement paramétré par tool). Le basculement vers une architecture multi-agents se justifie lorsque :

- les requêtes complexes (questions ouvertes, plaidoiries) deviennent majoritaires ;
- les conflits de jurisprudence (divergence cantonale, revirement de jurisprudence) doivent être traités explicitement et pas juste filtrés ;
- l'orchestration LLM dépasse la complexité gérable dans un seul prompt (Phase 9 montre des régressions sur requêtes multi-issues).

### 12.2 Ordre d'ajout recommandé

1. **Conflict Checker Agent** (première extension de valeur maximale) : détecte les contradictions dans le top-k (ex. ATF 148 III 95 suivi par ATF 137 II 313 sur la même question), applique la règle `binding > persuasive, higher > lower, later > earlier`, renvoie un champ `conflicts` à l'utilisateur avec résolution proposée. Utilise les champs `overrules`, `cited_by`, `validity_status` déjà produits en Phase 5.
2. **Issue Framing Agent** : décompose la requête en sous-questions juridiques ; chaque sous-question lance son propre retrieval. Pertinent pour `draft_mock_decision`.
3. **Verification Agent** : re-vérifie que chaque proposition du LLM drafting est appuyée par un passage dans le top-k. Intégration naturelle avec `cite_check`.
4. **Authority Planning Agent** : sélection adaptative du `weight_profile` (cf. § 8.2), déléguée à un LLM léger.
5. **Precedent Ranking Agent** : simple wrapper sur la Phase C du présent plan.
6. **Retrieval Agent** : wrapper sur Phases A+B.
7. **Drafting Agent** : LLM final, déjà en place pour `draft_mock_decision`.

### 12.3 Intégration incrémentale

La séparation se fera sans casser l'API tool-level : chaque agent devient un appel interne orchestré derrière le même tool. Observabilité étendue pour tracer les étapes.

---

## 13. Observabilité

### 13.1 Journalisation par requête

Chaque invocation (MCP ou REST) produit un enregistrement dans la table `retrieval_traces` :

- `trace_id`, `tool_name`, `authority_rerank`, `weights`, `filters`, `user_id` (si auth)
- Phase A : `bm25_top_ids[100]`, `bm25_scores[100]`, `ann_top_ids[100]`, `ann_scores[100]`, `rrf_top_ids[100]`, `rrf_scores[100]`, latences individuelles
- Phase B : `ce_top_ids[20]`, `ce_scores[20]`, `reranker_version`, latence
- Phase C : pour chaque id du top-20 : `text_sim_norm`, `pagerank_norm`, `authority_norm`, `temporal_raw`, `validity_multiplier`, `composite_score`. Ordre final.
- Indicateurs dérivés : `degraded_mode`, `early_exit`, `cache_hits`, `nb_candidates_post_filter`.

Rétention 30 jours glissants (RGPD, pseudo-anonymisation des requêtes contenant des noms propres).

### 13.2 Distribution des poids effectifs

Dashboard (implémentation Phase 9, spec Phase 7) : histogramme de `weights` par tool, par jour. Détecte les usages non par défaut et permet d'ajuster les poids défaut si un profil domine.

### 13.3 Tracking des décisions filtrées

Pour chaque trace, journaliser combien de candidats ont été écartés par quel filtre (`validity_status`, `court_level_min`, etc.). Permet d'identifier les filtres trop agressifs dans l'UX du Word add-in.

### 13.4 Échantillonnage et alerting

- Traces complètes pour 10 % du trafic + 100 % des requêtes où `degraded_mode=true` ou `early_exit=true`.
- Alerte Grafana : p95 latence > 2500 ms pendant 5 min → page on-call.
- Alerte qualité : nDCG@10 quotidien (rejoué sur les 50 requêtes du set de smoke) < 0.68 pendant 24 h → ticket automatique.

### 13.5 Explainability utilisateur

Un champ optionnel `explain=true` sur la requête retourne la décomposition du score composite pour chaque résultat, affichable en tooltip par le Word add-in. Utile pour les juristes qui veulent comprendre pourquoi un arrêt remonte.

---

## 14. Risques et mitigations

### 14.1 Cross-encoder trop lent

- **Risque** : p95 dépasse 2 s, UX dégradée.
- **Mitigations** : quantification int8, batch dynamique, early exit, cache LRU, GPU dédiée si nécessaire, fallback sur RRF+composite sans cross-encoder (`reranker="none"`), modèle plus petit (MiniLM L-4).

### 14.2 Biais d'autorité qui enterre des décisions récentes importantes

- **Risque** : un ATF 2025 de principe (renversant une jurisprudence) a encore peu de citations → faible PageRank, relégué derrière un ATF 2005 cité 300 fois.
- **Mitigations** :
  - PageRank **temporel** (facteur de décroissance λ sur les arêtes entrantes) déjà calculé en Phase 5 ;
  - Boost `atf_published=true` intégré dans `Authority_i` (1.00 vs 0.85) ;
  - Temporal score avec demi-vie 10 ans — les arrêts récents ne sont pas pénalisés ;
  - Détection heuristique : si une décision récente `OVERRULES` une décision du top-k, elle est promue d'un cran au minimum (rule-based override, v2) ;
  - Monitoring Phase 9 : sur une liste d'ATF récents connus, vérifier qu'ils remontent dans le top-10 de requêtes ciblées.

### 14.3 Poids mal calibrés

- **Risque** : calibration Phase 9 sur benchmark non représentatif.
- **Mitigations** :
  - Benchmark multi-domaines (7 matières minimum : public, civil, pénal, fiscal, social, LP, administratif).
  - Poids adaptatifs par profil (§ 8.2).
  - A/B test en prod à partir de 7.y : 10 % du trafic sur poids expérimentaux, métriques de satisfaction côté add-in (clic long/court, signalement).

### 14.4 Derives du cross-encoder (drift linguistique)

- **Risque** : la distribution des requêtes change (nouveau contentieux émerge, ex. crypto), le fine-tuning vieillit.
- **Mitigations** : re-fine-tuning trimestriel, surveillance de la distribution des longueurs/types de requête, flag `reranker_version` journalisé.

### 14.5 Validity_status manquant ou erroné

- **Risque** : Phase 5 peut classer à tort « valid » un arrêt récemment renversé, non encore réindexé.
- **Mitigations** : pipeline de mise à jour Phase 5 réactif (webhook sur nouvelles décisions), champ `validity_last_checked_at`, `Validity = 0.9` si staleness > 6 mois pour ATF critiques, re-scoring nocturne.

### 14.6 Saturation ParadeDB / tsvector

- **Risque** : un corpus en croissance dégrade BM25 sur les requêtes longues.
- **Mitigations** : partitionnement par année, index BM25 partiel sur les décennies récentes (recherche commune) + index archive, vacuum/analyse planifiés.

### 14.7 Perte d'equité cantonale

- **Risque** : le score `Authority` favorisant les hautes juridictions enterre les spécificités cantonales (droit cantonal pur).
- **Mitigations** : `weight_profile="cantonal"` avec `w_court` abaissé à 0.1 et `jurisdiction_match` additionnel (bonus si la décision vient du canton demandé). Exposé dans `search_decisions_parag`.

### 14.8 Biais linguistique

- **Risque** : requête IT sous-sert par rapport à DE/FR (corpus IT plus petit).
- **Mitigations** : cross-lingual retrieval obligatoire (Phase 6.4) ; le composite donne une chance aux passages DE/FR pertinents même si la requête est IT. Monitoring nDCG par langue en Phase 9.

### 14.9 Conflit licence ParadeDB

- **Risque** : AGPLv3 incompatible avec distribution souhaitée.
- **Mitigations** : fallback tsvector prêt (§ 3.2), décision arbitrée avant la fin de la première semaine de la phase.

---

## 15. Definition of Done

La Phase 7 est terminée et peut être déclarée « en production » si **toutes** les conditions suivantes sont réunies.

### 15.1 Implémentation

- [ ] Module de retrieval PA-RAG (chemin à confirmer Phase 6, p.ex. Edge Function `supabase/functions/search_parag/` + wrapper MCP), appelé par les tools `search_decisions`, `search_decisions_parag`, `find_leading_cases`, `get_doctrine`, `draft_mock_decision`, `cite_check`, `find_similar_decisions`.
- [ ] Index BM25 (ParadeDB ou tsvector fallback) sur la table `chunks` avec pondération par champ.
- [ ] Index pgvectorscale StreamingDiskANN confirmé opérationnel (livraison Phase 4) et utilisé avec paramètres calibrés Phase 7.
- [ ] RRF k=60 implémenté, paramétrable.
- [ ] Cross-encoder MiniLM fine-tuné suisse déployé (inférence ONNX int8), route de rerank opérationnelle, cache LRU en place.
- [ ] Phase C composite opérationnelle avec les quatre composants et les poids par défaut `0.25/0.25/0.25/0.25`.
- [ ] Tables de config créées : `retrieval_config`, `court_authority_scores`, `retrieval_weight_profiles`, `retrieval_traces`.
- [ ] Fine-tuning cross-encoder documenté et reproducible (script + corpus + version modèle).

### 15.2 Tests

- [ ] Tests unitaires : BM25, ANN, RRF, normalisation min-max, formule composite, multiplicateur Validity, validation des poids (somme=1), gestion filtres durs, pre-filter/post-filter switch.
- [ ] Tests d'intégration : parité `search_decisions` sans flag (zéro régression) ; nouveau `search_decisions_parag` passe un set de 20 requêtes de smoke.
- [ ] Tests golden des 23 tools MCP : verts.
- [ ] Tests des 30 routes REST : verts.
- [ ] Test de charge : 200 req/s pendant 10 min, p95 < 1800 ms, zéro 5xx.

### 15.3 Benchmark Phase 9 (partiel, en continu)

- [ ] nDCG@10 ≥ 0.72, Precision@10 ≥ 0.65, Authority Correctness ≥ 0.75 sur benchmark 200 requêtes.
- [ ] Zéro `overruled` dans top-5 par défaut.
- [ ] Aucun cas critique de « leading case connu non retrouvé » dans le top-10 pour un set de 30 requêtes de référence.
- [ ] PageRank moyen des résultats top-5 ≥ 0.18 (proxy de la baseline Harvard 0.213).

### 15.4 Observabilité

- [ ] Journalisation `retrieval_traces` opérationnelle avec rétention 30 j.
- [ ] Dashboard Grafana « PA-RAG » avec latences par phase, distribution des poids effectifs, taux `degraded_mode`, taux `early_exit`, p95 par tool.
- [ ] Alertes latence et qualité câblées (§ 13.4).
- [ ] Flag `explain=true` fonctionnel, réponse enrichie de la décomposition.

### 15.5 Documentation

- [ ] README du module de retrieval dans le repo.
- [ ] Guide de calibration des poids (à consolider avec Phase 9).
- [ ] Changelog `CHANGELOG.md` mettant à jour les tools avec les nouvelles signatures.
- [ ] Note aux consommateurs externes (Word add-in, Claude Desktop) sur les nouveaux paramètres optionnels et leur rétro-compatibilité.

### 15.6 Bascule

- [ ] `PA_RAG_DEFAULT_ON=false` en 7.x (opt-in), trafic effectif ≥ 20 % sur `search_decisions_parag` pendant 1 semaine sans régression.
- [ ] Décision de bascule `PA_RAG_DEFAULT_ON=true` (7.y) après revue benchmark Phase 9 et revue comité technique.
- [ ] Plan de rollback documenté : flip du flag, aucune migration à inverser, TTL cache 24 h → purge rapide.

### 15.7 Préparation PA-MA-RAG v2

- [ ] Spec courte de l'agent Conflict Checker (2-3 pages) prête à être priorisée Phase 10+.
- [ ] Points d'extension identifiés dans le code (interface `RetrievalAgent`, `RerankAgent`, `AuthorityAgent`) pour faciliter la décomposition ultérieure.

---

## Annexe A — Récapitulatif des paramètres par défaut

| Paramètre | Valeur | Source / justification |
|---|---|---|
| Top-N BM25 | 100 par langue | Rapport § 4.1 |
| Top-N ANN | 100 | Rapport § 4.1 |
| RRF k | 60 | Cormack et al. 2009, rapport § 4.1 |
| Top cross-encoder in/out | 100 → 20 | Trade-off latence/qualité |
| Top-k final | 10 (max 50) | UX |
| Poids composite | 0.25 / 0.25 / 0.25 / 0.25 | Baseline Harvard, rapport § 2.5 |
| Temporal demi-vie μ | `ln(2)/10` (10 ans) | Calibrage Phase 9 |
| Validity overruled | ×0.1 | Rapport § 2.3, analogue KeyCite/Shepard's |
| Validity criticized | ×0.5 | Rapport § 2.3 |
| Validity valid | ×1.0 | — |
| Authority TF ATF publié | 1.00 | Rapport § 2.1 + § 7.2 |
| Authority cantonal 2e inst. | 0.55 | Pyramide suisse |
| Cross-encoder | `ms-marco-MiniLM-L-6-v2` fine-tuné CH | Rapport § 9.1 |
| Reranker haut-de-gamme | `bge-reranker-v2-m3` | option premium |
| BM25 k1, b | 1.2, 0.75 | Okapi standard |
| Seuil pre/post filter | 10 % du corpus | Heuristique |
| PA_RAG_DEFAULT_ON | `false` (7.x) puis `true` (7.y) | Bascule progressive |

## Annexe B — Tables de configuration créées (migrations Phase 7)

- `retrieval_config` : clés/valeurs de tuning (k1, b, RRF k, λ, μ, poids défaut, version cross-encoder).
- `court_authority_scores` : mapping `court_id` → score [0,1].
- `retrieval_weight_profiles` : profils (`leading_case`, `doctrine`, `recent_law`, `cantonal`, `criminal`, …) → quadruplet de poids.
- `retrieval_traces` : journal par requête (cf. § 13).
- `retrieval_feedback` : réservé Phase 9 (clics, signalements) pour calibration continue.

## Annexe C — Inventaire des fichiers impactés (prévision)

- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py` (sera archivé à la bascule Phase 6, mais patch de transition ajoutant flags `authority_rerank` aux tools concernés si la bascule Phase 6 est incomplète).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py` : routes REST recevant les nouveaux paramètres.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/` : module de retrieval actuel, à remplacer ou encapsuler par le nouveau pipeline PA-RAG.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/` : inchangé (rétro-compat). Éventuellement un toggle UI `explain` et un tooltip de décomposition ajoutés en Phase 7.y.
- Supabase Edge Functions (chemin à confirmer Phase 6) : `search_parag`, `rerank`, `authority_score`.
- Migrations SQL : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/supabase/migrations/` (chemin cible du plan-maître).

## Annexe D — Dépendances externes et prérequis de phases antérieures

- Phase 1 — contrats figés des 23 tools et 30 routes.
- Phase 2 — miroir Postgres opérationnel, parité avec SQLite.
- Phase 3 — table `chunks` peuplée avec champs `chunk_text`, `chunk_summary`, `keywords_extracted`, `chunk_type`, `char_start`, `char_end`, `language`, `decision_id`.
- Phase 4 — index pgvectorscale StreamingDiskANN, embeddings Longformer 768-dim.
- Phase 5 — `decisions.pagerank_temporal`, `validity_status`, `overrules`, `court_level`, `atf_published`, `legal_areas`, `ratio_decidendi`.
- Phase 6 — MCP Edge Functions prêtes à héberger les nouveaux endpoints ; bridge stdio pour compat clients.

## Annexe E — Rétro-compatibilité et contrats

- Aucune suppression de champ dans la réponse des tools existants.
- Nouveaux champs (`composite_score`, `text_sim_norm`, `pagerank_norm`, `authority_norm`, `temporal_raw`, `validity_multiplier`, `supporting_chunks`, `applied_filters_reduced_corpus_to`, `degraded_mode`) : tous optionnels, présents seulement si `authority_rerank=true` ou `explain=true`.
- Codes d'erreur inchangés ; nouveau code `RETRIEVAL_DEGRADED` (non bloquant, informationnel).
- Versioning d'API : pas de bump majeur (pas de breaking change). Une note dans le `CHANGELOG` suffit.

---

Fin du sous-plan Phase 7.
