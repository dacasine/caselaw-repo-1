# Phase 9 — Évaluation et observabilité (continu)

> Sous-plan détaillé. Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Rapport source : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md` (§ 8 évaluation, § 9.3 human-in-the-loop).
> Durée : **continu** — activé dès la phase 2, intensifié aux phases 6 (bascule MCP) et 7 (rerank), puis permanent en production.
> Statut : cadrage, en attente de l'avocat référent pour annotation.

---

## 0. Position dans le plan global

La phase 9 n'est pas séquentielle mais **transversale**. Elle démarre en même temps que la phase 1 (définition des métriques et du protocole de benchmark), devient opérationnelle en phase 2 (tests golden sur migration SQLite→Postgres), s'intensifie en phase 6 (parité MCP) et phase 7 (validation du rerank d'autorité), puis s'installe en régime permanent en production avec observabilité live et boucle de feedback utilisateur.

Invariant fondateur : **aucune bascule de phase (3→4, 6→7, 7→8) ne se fait sans régression verte sur le benchmark custom**. La phase 9 est le garde-fou du plan.

Rappel des autres sous-plans (contexte de dépendance) :
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-1-*.md` — schéma Supabase, contrats figés.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-3-*.md` — chunking SAC, source des passages à annoter.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-5-*.md` — enrichissement PA-RAG, source des signaux d'autorité.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/phase-7-*.md` — rerank composite, consommateur principal des métriques.

---

## 1. Objectifs et critères de succès

### 1.1 Objectif général

Fournir un appareil de mesure **objectif, reproductible, spécifique au droit suisse** qui permette :
1. De prouver que PA-RAG apporte un gain mesurable vs baseline historique (BGE-M3 + 3 chunks + tri chronologique naïf).
2. De détecter en production toute régression de qualité (drift) avant qu'elle n'impacte les utilisateurs.
3. De fournir à l'avocat référent une vue consolidée sur la fiabilité du système (taux d'hallucination, autorité, validité temporelle).
4. D'aligner les décisions de bascule technique (activation rerank, activation GraphRAG, fine-tuning embeddings) sur des seuils numériques prédéfinis, pas sur une impression subjective.

### 1.2 Critères de succès quantitatifs — seuils de bascule

Les seuils sont fixés en cohérence avec le rapport PA-RAG § 8.1 et les ordres de grandeur observés dans LegalBench-RAG (2024), Legal RAG Bench (Isaacus, fév 2026) et LaborBench (Stanford). Ils sont **paliers** : un palier plus bas est toléré sur certains domaines rares, mais la moyenne globale doit franchir le seuil.

| Métrique | Baseline (naïf BGE-M3+FTS5) | Seuil bascule phase 7 | Seuil cible production | Seuil critique (rollback) |
|---|---|---|---|---|
| Precision@10 | ~0.35 | ≥ 0.55 | ≥ 0.70 | < 0.40 |
| Recall@50 | ~0.55 | ≥ 0.75 | ≥ 0.85 | < 0.60 |
| nDCG@10 | ~0.40 | ≥ 0.60 | ≥ 0.72 | < 0.45 |
| MRR | ~0.38 | ≥ 0.55 | ≥ 0.68 | < 0.40 |
| Authority Correctness | ~0.50 | ≥ 0.75 | ≥ 0.85 | < 0.60 |
| Citation Fidelity | ~0.70 | ≥ 0.92 | ≥ 0.97 | < 0.85 |
| Hallucination Rate | ~0.12 | ≤ 0.04 | ≤ 0.015 | > 0.06 |
| Negative Treatment Detection | non mesuré | ≥ 0.65 | ≥ 0.85 | < 0.55 |
| Temporal Validity | ~0.45 | ≥ 0.80 | ≥ 0.92 | < 0.65 |
| Span-level Precision@5 | ~0.22 | ≥ 0.45 | ≥ 0.60 | < 0.30 |

Remarque : la colonne « baseline » est à mesurer empiriquement en phase 2 sur le mode naïf (cf. § 5 A/B testing). Les valeurs indiquées sont des estimations à partir de la littérature pour dimensionner les seuils ; elles seront remplacées par les chiffres réels avant la bascule.

### 1.3 Critères qualitatifs

- Accord inter-annotateur (kappa de Cohen) sur le sous-ensemble double-annoté ≥ 0.70 (accord substantiel).
- Couverture thématique équilibrée : aucun domaine sous-représenté à < 15 requêtes sur 200.
- Couverture linguistique : DE ≥ 50 %, FR ≥ 35 %, IT ≥ 10 % (reflet approximatif de la distribution du corpus BGer).
- Tenue du dashboard : mise à jour quotidienne, disponibilité ≥ 99 % sur 30 jours glissants.
- Cadence feedback : revue hebdomadaire de 10 requêtes échantillonnées par l'avocat référent, taux de complétion ≥ 90 % sur le trimestre.

### 1.4 Critères d'échec (rollback automatique)

Un rollback vers la version N-1 des Edge Functions est déclenché si, sur une fenêtre glissante de 48 h en production :
- Hallucination Rate mesuré (sampling automatisé) > 0.06.
- Authority Correctness < 0.60 sur les requêtes contenant une ATF connue.
- Latence p95 d'une RPC core (`hybrid_search`, `get_decision_with_citations`) dépasse 2× la baseline mesurée en phase 6.
- Taux d'erreur 5xx sur Edge Functions > 2 %.

---

## 2. Construction du benchmark custom suisse

### 2.1 Pourquoi un benchmark custom

Rien d'équivalent n'existe publiquement pour le droit suisse. LegalBench-RAG (§ 6 infra) est anglophone et centré sur du droit contractuel US ; LaborBench Stanford ne couvre que le droit du travail US ; Legal RAG Bench (Isaacus) teste l'end-to-end mais sur corpus anglophone. Le rapport PA-RAG § 8.1 est explicite : **tout benchmark juridique doit être construit dans la juridiction cible**, car les notions d'autorité (ATF vs arrêt ordinaire), de validité temporelle (overruling) et de pertinence (considérant juridique vs état de fait) sont culturellement et institutionnellement spécifiques.

### 2.2 Taille et distribution

- **200 requêtes** au total — taille modeste mais suffisante pour détecter des écarts significatifs avec p < 0.01 sur des métriques binaires de proportion. Dimensionnement validé par calcul de puissance : pour détecter un écart de 10 points de Precision@10 (0.60 vs 0.70) avec α = 0.05, β = 0.20, un test de proportions apparié requiert ~150 observations ; 200 donne marge de sécurité pour les sous-analyses par domaine.
- **10 domaines × 20 requêtes** :
  1. **CO** — Code des obligations (contrats, responsabilité civile, société).
  2. **CC** — Code civil (droit des personnes, famille, successions, réels).
  3. **CP** — Code pénal (infractions, procédure pénale fédérale).
  4. **LP** — Poursuite et faillite.
  5. **LTF** — Procédure devant le Tribunal fédéral (recevabilité, griefs, pouvoir d'examen).
  6. **Droit administratif fédéral** — Loi sur la procédure administrative, droit public général.
  7. **Droit fiscal** — LIFD, LHID, LTVA, fiscalité cantonale.
  8. **Droit du travail** — CO art. 319 ss, LTr, CCT, assurances sociales adjacentes.
  9. **Marchés publics** — LMP/AIMP, droit de la concurrence adjacent.
  10. **Droit de la famille** — mariage, divorce, filiation, protection de l'adulte (inclut parts du CC mais requêtes orientées praticien).

Chaque domaine = 20 requêtes avec sous-quotas internes :
- 10 requêtes « praticien standard » (recherche de jurisprudence sur un point précis).
- 5 requêtes « autorité » (demande explicite ou implicite d'un ATF de principe).
- 3 requêtes « temporelle » (test de la détection d'overruling / jurisprudence récente).
- 2 requêtes « rare / cantonale » (edge cases pour couverture).

### 2.3 Équilibre linguistique

- **DE** : ≥ 100 requêtes (50 %).
- **FR** : ≥ 70 requêtes (35 %).
- **IT** : ≥ 20 requêtes (10 %).
- 10 requêtes restantes : libre, priorité FR pour refléter l'usage de l'outil par le cabinet pilote.

La question peut être formulée dans une langue et viser des décisions dans une autre (cas réaliste suisse). Le benchmark doit inclure au minimum 20 requêtes cross-lingual (question FR → décisions DE par exemple).

### 2.4 Format d'une requête annotée

Chaque entrée du benchmark est un enregistrement structuré contenant :
- **`query_id`** : identifiant stable `SWCH-{domain}-{lang}-{nnn}`.
- **`query_text`** : question en langage naturel d'un avocat suisse.
- **`query_intent`** : catégorie `standard | authority | temporal | rare`.
- **`domain`** : un des 10 domaines.
- **`language`** : `de | fr | it`.
- **`expected_decisions`** : liste ordonnée de `decision_id` attendus, avec rang de pertinence (1 = arrêt pivot, 2 = arrêt confirmatif, 3 = arrêt périphérique).
- **`expected_spans`** : pour chaque decision_id, un ou plusieurs spans (offsets caractères dans le texte brut, ou identifiants de considérant) attestant la réponse.
- **`expected_authority`** : `true/false` — l'ATF principale doit-elle apparaître dans le top 3 ?
- **`negative_set`** : liste de `decision_id` qui ne doivent **pas** apparaître en top 10 (ex. arrêts overruled connus, décisions de fait non pertinentes partageant le vocabulaire).
- **`temporal_cutoff`** : date avant laquelle les arrêts pertinents ne doivent pas être considérés (utile pour test de validité temporelle).
- **`rationale`** : 2-4 phrases écrites par l'avocat expliquant pourquoi cette réponse est la bonne — sert de grille à la revue secondaire et à l'entraînement d'un futur cross-encoder.
- **`created_at`**, **`annotator_id`**, **`reviewer_id`**, **`benchmark_version`**.

### 2.5 Protocole d'annotation

**Équipe** : 1 avocat référent (annotateur principal, senior), 1 avocat secondaire (double annotation partielle), 1 coordinateur (data engineer, gestion des formats, pas d'annotation juridique).

**Processus par requête** :
1. Rédaction de la question en langue naturelle, ancrée dans une situation praticien réaliste (reformulations de requêtes effectivement posées en cabinet, anonymisées).
2. Recherche manuelle dans le corpus actuel (fork OpenCaseLaw) pour identifier les arrêts pivots.
3. Vérification sur le site `bger.ch` ou `fedlex.ch` que la jurisprudence n'a pas évolué depuis.
4. Annotation des spans dans le texte de l'arrêt (sélection manuelle des considérants déterminants).
5. Renseignement du `negative_set` (arrêts connus pour vocabulaire trompeur ou overruling).
6. Rédaction du `rationale`.
7. Revue par l'avocat secondaire sur 20 % des requêtes (40 requêtes tirées au hasard, stratifié par domaine et langue).

**Accord inter-annotateur** :
- Mesure sur les 40 requêtes doublement annotées.
- Kappa de Cohen sur :
  - la liste `expected_decisions` (accord binaire sur chaque decision_id du top 5 fusionné) — cible κ ≥ 0.70.
  - la sélection de spans (intersection over union des offsets, seuil IoU ≥ 0.50 considéré comme accord) — cible κ ≥ 0.60.
- Les désaccords sont tranchés par discussion. Si un désaccord persiste, la requête est étiquetée `ambiguous` et exclue du scoring principal mais conservée pour analyse qualitative.

**Budget temps** : estimation 45-90 min par requête annotée + 20 min de revue secondaire. Total : ~180 h pour l'avocat principal + ~14 h pour l'avocat secondaire. Étalé sur 6-8 semaines en parallèle des phases 2-5.

### 2.6 Versioning et gouvernance du benchmark

Le benchmark est un artefact versionné (git, sous `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/`, structure à créer en phase 1) :
- Branche `main` = version figée utilisée pour la communication externe et les rapports à l'avocat référent.
- Branche `dev` = évolutions en cours (ajout de requêtes, corrections d'annotations).
- Tag `v{MAJOR}.{MINOR}` à chaque release. MAJOR = changement de protocole ou de distribution. MINOR = ajout/retrait de requêtes.
- Un CHANGELOG documente chaque changement, signé par l'avocat référent.

**Règle d'or anti-leakage** : les requêtes du benchmark **ne doivent jamais** servir d'input à :
- un prompt d'enrichissement LLM (phase 5) — risque que le modèle « apprenne » la réponse.
- un jeu de fine-tuning d'embedding ou de cross-encoder.
- un prompt système de Claude Desktop ou du Word add-in.

Une pre-commit hook (à définir en phase 1) vérifie qu'aucun fichier du repo en dehors de `bench/` ne contient de `query_id` du benchmark.

### 2.7 Jeu de développement vs jeu de test

Sur les 200 requêtes :
- **150 (dev set)** : utilisées pendant les phases 3-8 pour itérer sur le chunking, les prompts d'enrichissement, la formule de rerank.
- **50 (test set, bloqué)** : scellées, évaluées **une seule fois par version** du pipeline, pour produire les chiffres officiels. Le test set n'est jamais inspecté manuellement après un échec — seul le dev set sert au debug.

Le test set est régénéré (rotation de 10-20 requêtes fraîches) tous les 6 mois pour éviter l'overfitting.

---

## 3. Métriques de retrieval classiques

### 3.1 Définitions opérationnelles (notation compacte)

Notations :
- **q** : requête. **R(q, k)** : liste ordonnée des k documents retournés par le système.
- **G(q)** : ensemble gold-standard des documents pertinents pour q.
- **rel(d, q) ∈ {0, 1, 2, 3}** : pertinence gradée (3 = pivot, 2 = confirmatif, 1 = périphérique, 0 = non pertinent).

Formules :
- **Precision@k(q)** = |R(q, k) ∩ G(q)| / k.
- **Recall@k(q)** = |R(q, k) ∩ G(q)| / |G(q)|.
- **MRR(q)** = 1 / rang du premier document pertinent ; 0 si aucun dans R(q, k_max).
- **DCG@k(q)** = Σ_{i=1..k} (2^rel(d_i, q) − 1) / log2(i + 1).
- **nDCG@k(q)** = DCG@k(q) / IDCG@k(q), où IDCG est le DCG du classement idéal.

Moyennes : macro-moyenne par domaine (20 requêtes → 1 score → moyenne sur 10 domaines) ET micro-moyenne globale (toutes requêtes équipondérées). Les deux sont rapportées.

### 3.2 Seuils acceptables par domaine

Certains domaines sont intrinsèquement plus difficiles (droit fiscal : terminologie cantonale très variable ; marchés publics : concepts techniques récents peu couverts). Seuils ajustés :

| Domaine | Precision@10 cible | Recall@50 cible | nDCG@10 cible |
|---|---|---|---|
| CO | 0.72 | 0.87 | 0.75 |
| CC | 0.72 | 0.87 | 0.75 |
| CP | 0.70 | 0.85 | 0.72 |
| LP | 0.68 | 0.82 | 0.70 |
| LTF | 0.75 | 0.90 | 0.78 |
| Admin fédéral | 0.65 | 0.80 | 0.68 |
| Fiscal | 0.62 | 0.78 | 0.65 |
| Travail | 0.70 | 0.85 | 0.72 |
| Marchés publics | 0.60 | 0.75 | 0.63 |
| Famille | 0.70 | 0.85 | 0.72 |
| **Moyenne cible** | **0.68** | **0.83** | **0.71** |

### 3.3 Intervalles de confiance et tests statistiques

- Tous les scores rapportés avec intervalle de confiance à 95 % via bootstrap (1 000 réechantillonnages sur les 200 requêtes).
- Comparaison A/B (PA-RAG vs baseline) : test de permutation apparié (10 000 permutations), seuil p < 0.01. Test signé des rangs de Wilcoxon en complément pour les scores continus (nDCG).
- Comparaison entre variantes (ex. λ_temporal ∈ {0.001, 0.002, 0.005}) : correction de Bonferroni si plus de 3 variantes testées simultanément.

---

## 4. Métriques spécifiques juridique

Ces métriques constituent la **vraie valeur ajoutée** du benchmark PA-RAG. Elles sont inspirées du rapport § 8.1 et complètent (voire remplacent) les métriques classiques quand le jugement de pertinence ne suffit pas.

### 4.1 Authority Correctness

**Définition** : pour chaque requête q dont `expected_authority = true`, le système doit retourner l'ATF pivot (celle marquée rang 1 dans `expected_decisions`) parmi les 3 premiers résultats.

**Formule** : `AC = (1/|Q_auth|) × Σ_{q ∈ Q_auth} 1[ATF_pivot(q) ∈ R(q, 3)]`.

**Interprétation** : mesure la capacité du système à privilégier l'autorité institutionnelle sur la seule similarité lexicale/sémantique. C'est la métrique qui valide l'activation du rerank d'autorité (phase 7) : sans rerank, AC plafonne à ~0.50 ; avec rerank (pondération PageRank temporel + bonus ATF + fraîcheur), cible ≥ 0.85.

**Cas limites** :
- Plusieurs ATF pivots légitimes (ex. ATF 140 III 134 et ATF 142 III 364 co-pivots) : succès si l'une des deux est top 3.
- ATF pivot overruled et nouvelle ATF existe : le succès exige la nouvelle ATF (pas l'ancienne), conformément à la politique « validité temporelle par défaut ».

### 4.2 Citation Fidelity

**Définition** : pour chaque citation de jurisprudence ou de disposition légale produite par le LLM en aval du retrieval, vérifier que la référence existe et que le passage cité correspond effectivement au contenu du document pointé.

**Mesure en deux temps** :
1. **Existence** : la référence (ex. « ATF 148 IV 123 consid. 3.2 ») résout-elle vers un decision_id réel dans la base ? Recours à `citations.db` + parseur de citations.
2. **Alignement sémantique** : le passage cité par le LLM est-il sémantiquement équivalent au considérant cité ? Mesure par similarité d'embedding ≥ 0.80 entre le texte généré et le texte du considérant réel.

**Formule agrégée** : `CF = (1/N_cit) × Σ 1[existe ET aligné]` où N_cit est le nombre total de citations produites sur l'ensemble du benchmark.

**Seuil** : ≥ 0.97 en production. Toute citation qui échoue à l'étape 1 compte comme hallucination (§ 4.3).

**Implémentation** : l'évaluation de CF nécessite un pipeline de génération LLM + extraction de citations ; ce pipeline est défini en phase 5 (enrichissement) et réutilisé ici.

### 4.3 Hallucination Rate

**Définition** : proportion de citations générées par le LLM qui pointent vers des références **inexistantes** (ATF inventée, article de loi inexistant, numéro de considérant hors bornes).

**Formule** : `HR = N_cit_inexistantes / N_cit_totales`.

**Procédure d'évaluation** :
- Génération de réponse complète (LLM + tool calls) pour les 200 requêtes du benchmark.
- Extraction automatique des citations (regex + parseur dédié, à implémenter en phase 5).
- Vérification de chaque citation contre la base Postgres.
- Agrégation, par domaine et global.

**Seuil critique** : HR > 0.06 déclenche rollback (§ 1.4). Cible production ≤ 0.015. Rapport PA-RAG § 8.1 mentionne 3-10 % comme baseline GPT-4 sans RAG — PA-RAG doit faire au moins 3× mieux.

### 4.4 Negative Treatment Detection

**Définition** : capacité du système à **exclure** (ou à fortement déclasser) les arrêts overruled du top 10 quand la question porte sur le droit actuel.

**Procédure** :
- Chaque requête du `negative_set` contient au moins un arrêt connu overruled (annotation de phase 5 : champ `validity_status = 'overruled'`).
- Métrique : `NTD = 1 − (nb arrêts overruled du negative_set apparaissant en top 10 / nb total d'arrêts overruled dans negative_set agrégé)`.

**Seuil** : ≥ 0.85 en production. La métrique sert de preuve que le filtre `validity_status` (phase 5/7) fonctionne et que la pondération par fraîcheur est efficace.

**Variante** : `NTD_soft` tolère les overruled en top 10 s'ils sont accompagnés d'un drapeau explicite dans la sortie LLM (« Note : ATF X a été renversée par ATF Y »). Mesuré séparément.

### 4.5 Temporal Validity

**Définition** : sur un ensemble dédié de paires `(overruled, overruling)` connues, le système doit prioriser l'arrêt `overruling` dès lors que la question ne contient pas de date cutoff antérieure à l'overruling.

**Corpus** : 30-50 paires d'overruling documentées par l'avocat référent, dont les exemples classiques mentionnés dans le rapport PA-RAG :
- ATF 150 II 105 vs ATF 137 II 313 (évolution jurisprudentielle en droit public).
- Autres paires à collecter : jurisprudence Rothenthurm, revirements récents en droit de la famille, évolutions LTF sur la recevabilité.

**Formule** : `TV = (1/|P|) × Σ_{(old, new) ∈ P} 1[rang(new) < rang(old) dans R(q, 10)]`.

**Seuil** : ≥ 0.92 en production. Mesure directe de la qualité de la signal de fraîcheur temporelle du rerank (phase 7).

### 4.6 Span-level Precision

**Définition** : inspirée de LegalBench-RAG (2024) — il ne suffit pas de retourner le bon document, il faut retourner le bon **passage**. Un arrêt de 80 000 caractères contient souvent un seul considérant pertinent.

**Mesure** : pour chaque chunk retourné en top k, calculer l'intersection over union (IoU) des offsets du chunk avec les `expected_spans`. Considérer un chunk comme « span-correct » si IoU ≥ 0.50.

**Formule** : `SP@k = (1/|Q|) × Σ_q (nb chunks span-correct parmi top k / min(k, nb chunks pertinents))`.

**Seuil** : SP@5 ≥ 0.60 en production. La métrique valide la qualité du chunking SAC (phase 3) : un mauvais chunking (chunks trop gros ou mal alignés sur les considérants) détruit SP@k même si Precision@k(document) reste haut.

### 4.7 Métriques secondaires d'observation

Non utilisées pour bascule mais tracées :
- **Language-correct rate** : proportion de réponses dans la langue de la question (évite l'écueil GPT qui répond en DE à une question FR).
- **Coverage by canton** : sur le sous-ensemble de requêtes cantonales, proportion de cantons effectivement représentés dans les top 10 agrégés.
- **Average citation count per response** : santé quantitative (trop peu = pauvre ; trop = padding).

---

## 5. Protocole A/B testing PA-RAG vs baseline

### 5.1 Mode baseline (rag_naive)

Un mode `rag_naive` est activable via feature flag sur l'Edge Function `hybrid_search` (phase 6) :
- Embeddings BGE-M3 (1024-dim) tels qu'actuellement présents dans `search_stack/build_vectors.py`.
- Top-k purement sémantique, k=3 chunks, 500 chars chacun (configuration historique du repo).
- Aucun rerank d'autorité, aucune pondération temporelle, aucun filtre validity.
- Tri chronologique décroissant en second critère.

Ce mode reproduit fidèlement le comportement du `mcp_server.py` historique **à la date de la bascule**. Il constitue la référence objective à battre.

### 5.2 Mode PA-RAG (rag_parag)

Configuration complète :
- Chunking SAC Longformer (phase 3).
- Embeddings `joelito/legal-swiss-longformer-base` 768-dim.
- Retrieval hybride BM25 + ANN + RRF (phase 7).
- Cross-encoder juridique (phase 7).
- Authority rerank composite (PageRank temporel + fraîcheur + signal de cour + flags négatifs), phase 7.
- GraphRAG expansion si activé (phase 8).

### 5.3 Design expérimental

Pour chacune des 200 requêtes :
1. Exécuter en mode `rag_naive`, stocker top-50 + chunks.
2. Exécuter en mode `rag_parag` (mêmes paramètres d'infra, mêmes index).
3. Calculer toutes les métriques § 3-4 pour les deux runs.
4. Comparaison par test de permutation apparié (chaque requête est sa propre paire).

**Gain attendu** : inspiré des chiffres Harvard cités dans le rapport PA-RAG (PageRank 0.213 vs 0.026, p<0.001 — ordre de grandeur ~8× sur une métrique de centrality/pertinence), cible minimale :
- nDCG@10 : +15 points absolus (0.40 → 0.55 minimum, idéalement 0.40 → 0.72).
- Authority Correctness : +30 points absolus.
- Temporal Validity : +40 points absolus.
- Hallucination Rate : division par 3 minimum.

Ces cibles sont les **conditions de validation de la bascule en production**. Si le gain n'est pas là, on ne bascule pas.

### 5.4 A/B testing en production (shadow mode)

Après bascule, conservation de la capacité à exécuter les deux modes en parallèle (shadow mode) :
- 5 % du trafic échantillonné exécute les deux modes.
- Comparaison automatique sur proxy-métriques (latence, taux de clic, feedback utilisateur).
- Tableau de bord mensuel de dérive du gain.

### 5.5 Tests de sensibilité

Variantes à tester sur le dev set (150 requêtes) pour calibrer :
- λ du time-decayed PageRank : grid {0.0005, 0.001, 0.002, 0.005, 0.01}.
- Pondérations composites du rerank (α autorité, β fraîcheur, γ similarité) : grid 3D sparse.
- k top-k rerank (20, 50, 100).
- Activation ou non du cross-encoder (coût latence vs gain qualité).

Chaque variante = 1 run complet du dev set, coût ~€10 d'inférence LLM si évaluation end-to-end, ~2 min de calcul retrieval pur.

---

## 6. Intégration de benchmarks externes

### 6.1 LegalBench-RAG (2024)

**Description** : 6 858 paires query-answer, 79 M+ caractères, span-level, 4 sous-ensembles (ContractNLI, CUAD, MAUD, Privacy-QA). Anglophone, centré sur le droit contractuel et de la confidentialité US.

**Adaptabilité au droit suisse** :
- **Directe** : inexploitable — terminologie, citations, institutions différentes.
- **Par transfert de méthodologie** : oui — le principe span-level, le format de paires, la granularité des annotations sont transposés au benchmark custom (§ 2-4).
- **Par traduction** : expérimentale — traduire un sous-ensemble (ex. 200 requêtes ContractNLI) en allemand juridique suisse et les évaluer en parallèle pour comparer les ordres de grandeur. Utile comme sanity check de performance absolue, sans valeur juridique.

**Décision** : on adopte la **méthodologie** (span-level, format structuré, distinction dev/test) sans réutiliser les données. Pas de traduction prévue en priorité ; éventuelle exploration exploratoire si temps disponible.

### 6.2 Legal RAG Bench (Isaacus, février 2026)

**Description** : benchmark end-to-end publié en février 2026 par Isaacus, focus sur le pipeline complet (retrieval + génération), corpus anglophone multi-juridictionnel. Révèle que **la qualité du retrieval domine la qualité du raisonnement LLM** — un retriever faible plombe le meilleur LLM, alors qu'un retriever fort rattrape un LLM moyen.

**Enseignement directement actionnable** :
- Investir en priorité dans le retrieval (chunking SAC, embeddings adaptés, rerank composite) plutôt que dans le choix du LLM downstream.
- Mesurer les métriques de retrieval (§ 3-4) **avant** les métriques end-to-end.
- Dans l'A/B testing, fixer le LLM (GLM-5.x Reasoning) et faire varier uniquement le pipeline de retrieval.

**Portabilité** : pas de données réutilisables mais approche d'évaluation end-to-end reproductible. On ajoute au benchmark custom une couche de métriques end-to-end (voir § 6.4).

### 6.3 LaborBench (Stanford)

**Description** : benchmark US de droit du travail, rapporte jusqu'à 92 % de précision avec un pipeline RAG avancé.

**Pertinence CH** : **faible**. Le droit du travail US (at-will, common law, NLRA) n'a pas d'équivalent suisse direct. Cité ici pour référence d'ordre de grandeur — si LaborBench atteint 92 % sur un domaine large, un système PA-RAG sur droit suisse devrait viser > 85 % sur ses métriques cibles (Authority Correctness, Citation Fidelity).

**Décision** : non intégré au benchmark, mentionné dans le rapport final comme point de comparaison externe.

### 6.4 Couche end-to-end (inspirée de Legal RAG Bench)

En complément des métriques de retrieval, on définit 2 métriques end-to-end :
- **Answer Faithfulness** : la réponse finale (LLM + contexte retrieval) est-elle conforme aux documents retournés ? Mesure par LLM-judge (GLM-5.x en mode évaluation, prompt structuré, score 0-5) + échantillonnage manuel par l'avocat référent.
- **Answer Usefulness** : l'avocat référent juge la réponse utile (0-5) sur un sous-ensemble de 50 requêtes (sélection aléatoire du test set).

Mesuré trimestriellement, pas hebdomadairement (coût humain élevé).

---

## 7. Observabilité production

### 7.1 Stack

- **Supabase logs** : source brute pour les Edge Functions (logs natifs). Consultation via l'API ou le dashboard Supabase.
- **Prometheus exporters** : à déployer à côté du pool Postgres Supabase self-hosted (exporter `postgres_exporter` officiel) et au niveau des Edge Functions (middleware Deno exposant `/metrics`).
- **Grafana** : dashboards consolidés, déploiement self-hosted ou Grafana Cloud. Sources : Prometheus + logs Supabase via Loki (optionnel).
- **Alertmanager** : routage des alertes (email + Slack avocat référent + Slack ops).
- **Sentry** ou équivalent : capture des exceptions dans les Edge Functions et dans le Word add-in.

### 7.2 Tableaux de bord minimaux

**Dashboard 1 — Santé technique** :
- Latence p50/p95/p99 par RPC (hybrid_search, get_decision_with_citations, expand_citations, etc.).
- Taux d'erreur 4xx / 5xx par Edge Function.
- QPS par Edge Function.
- Taille et latence de la file Postgres (connexions actives, attente lock, temps moyen requête).
- Santé de l'index pgvectorscale (taille, temps de reconstruction, last_vacuum).

**Dashboard 2 — Qualité retrieval (proxy temps réel)** :
- Score composite moyen des top 10 retournés (proxy d'autorité).
- Distribution des `validity_status` des arrêts retournés (cible : overruled < 3 % du total retourné).
- Taux de hit du rerank d'autorité : proportion de requêtes où le top 1 a été promu par le rerank (indicateur que le rerank travaille).
- Distribution des cours (BGer ATF, BGer ordinaire, tribunaux cantonaux, régulateurs) dans les résultats.
- Taux de hit du cross-encoder.

**Dashboard 3 — Feedback utilisateur** :
- Thumbs up/down par jour et par domaine.
- Taux de flagging (réponses marquées douteuses).
- Distribution des commentaires libres (analyse sémantique automatique hebdomadaire).

**Dashboard 4 — Qualité benchmark (régression)** :
- Scores des 10 métriques principales, sur dev set et test set, au fil des versions.
- Alertes sur régression > seuil sur l'une des métriques clés.
- Drift des scores par domaine (10 domaines × 10 métriques = matrice 100 cellules, heatmap).

### 7.3 Alertes

Règles d'alerte Alertmanager :
- **P0 (page immédiate, astreinte)** :
  - Taux d'erreur 5xx > 2 % sur 5 min.
  - Latence p95 d'une RPC core > 5 s sur 5 min.
  - Indisponibilité Postgres > 30 s.
- **P1 (email, traitement < 4 h)** :
  - Hallucination Rate (sampling auto) > 0.04 sur 24 h.
  - Authority Correctness < 0.75 sur 24 h.
  - Taux de thumbs down > 20 % sur 24 h.
- **P2 (email digest quotidien)** :
  - Drift d'une métrique > 2 écarts-types par rapport à la moyenne glissante 30 jours.
  - Taux de flagging utilisateur > 5 %.
  - Remplissage disque Postgres > 75 %.

### 7.4 Sampling automatisé de qualité en production

Toutes les 6 h, 10 requêtes représentatives du benchmark (stratification par domaine et langue) sont exécutées en production, résultats comparés au gold standard, métriques poussées vers Prometheus. Ceci fournit un signal **continu** de qualité, complément du benchmark offline.

Contrainte anti-leakage : ces 10 requêtes sont prises uniquement dans le dev set, jamais dans le test set.

### 7.5 Logs structurés

Chaque Edge Function produit un log JSON structuré incluant :
- `request_id`, `query_text` (hashé si RGPD/secret professionnel exige), `language_detected`, `domain_detected`.
- `retrieval_config_version`, `rerank_enabled`, `graphrag_enabled`.
- `latency_ms` (par étape : embed, bm25, ann, rrf, rerank, cross-encoder).
- `top_k_results` (decision_id seulement, pas le texte).
- `error_details` si applicable.

Logs indexés dans Loki ou équivalent, rétention 30 jours (logs détaillés) + 1 an (agrégats).

---

## 8. Tracking de drift

### 8.1 Signaux de drift à surveiller

- **Baisse de nDCG@10** sur le sampling automatisé : >1 écart-type sur 7 jours glissants.
- **Hausse du Hallucination Rate** : > 0.03 sur 7 jours glissants.
- **Drift de distribution de validity_status** : proportion d'arrêts retournés avec `validity_status = 'unknown'` en hausse > 10 % → signe que l'enrichissement LLM (phase 5) se dégrade ou que de nouvelles décisions entrent sans enrichissement.
- **Drift linguistique** : proportion de réponses en langue autre que celle de la question > 5 % (régression LLM ou embeddings).
- **Drift lexical** : apparition de nouveaux termes juridiques (néologismes, nouvelles lois) non couverts par le fine-tuning embedding — détection par outlier score sur les embeddings de requêtes récentes.
- **Drift de latence** : hausse progressive de p95 sans changement de code (indicateur de dégradation d'index pgvector).

### 8.2 Fréquence de ré-évaluation

- **Quotidien** : sampling automatisé de 10 requêtes (§ 7.4).
- **Hebdomadaire** : 10 requêtes revues manuellement par l'avocat référent (§ 9.2).
- **Mensuel** : run complet du dev set (150 requêtes) avec tous les scores, rapport auto généré en PDF.
- **Trimestriel** : run du test set (50 requêtes, scellé), plus couche end-to-end (Answer Faithfulness, Answer Usefulness), revue formelle.
- **Semestriel** : rotation partielle du test set (10-20 requêtes remplacées), revue de seuils, revue du protocole.

### 8.3 Protocole de réponse au drift

- Drift détecté par alerte P2 → investigation par data engineer dans la semaine.
- Drift confirmé → ticket de remédiation, priorité selon ampleur.
- Drift critique (franchit seuil P1) → réunion avec avocat référent, décision de rollback ou de remédiation urgente.
- Post-mortem formel pour tout rollback production.

---

## 9. Human-in-the-loop

### 9.1 Philosophie

Le rapport PA-RAG § 9.3 est explicite : l'outil est **un assistant, pas un substitut**. L'avocat doit toujours pouvoir :
- Vérifier chaque citation (clic vers l'arrêt complet).
- Signaler une réponse incorrecte ou incomplète.
- Refuser d'utiliser une réponse générée sans sanction de l'outil.

Cette posture éthique doit se refléter dans le produit ET dans le benchmark.

### 9.2 Boucle de feedback utilisateur

**Mécanismes intégrés au Word add-in et au MCP/Claude Desktop** :
- **Thumbs up / thumbs down** sur chaque réponse, avec commentaire libre optionnel.
- **Flagging** : bouton dédié « signaler une erreur » qui ouvre un mini-formulaire structuré :
  - Type d'erreur : citation inexistante | citation erronée | arrêt overruled non signalé | passage incorrect | hors sujet | autre.
  - Description libre.
  - Référence attendue (si l'utilisateur la connaît).
- **Correction proposée** : pour les utilisateurs avocats avancés (feature flag), possibilité d'annoter directement le résultat et de le proposer comme requête candidate au benchmark.

**Stockage** : table `feedback` dans Supabase, avec `request_id` corrélé aux logs d'Edge Function, lien vers `query_text`, `response_text`, `retrieved_decisions`.

### 9.3 Revue hebdomadaire

L'avocat référent :
- Reçoit chaque lundi matin un échantillon de 10 requêtes de la semaine précédente (sélection stratifiée : 3 flaggées, 3 thumbs down, 4 aléatoires).
- Annote chaque requête selon la grille du benchmark (§ 2.4).
- Délai cible : traité dans les 48 h.

Output : `weekly_review_{iso_week}.json`, intégré à un tableau de bord dédié.

### 9.4 Dataset de correction

Les requêtes flaggées + annotées par l'avocat référent constituent un **dataset de correction** versionné séparément du benchmark principal. Usage :
- Identification de patterns d'erreurs récurrents (ex. confusion entre art. 41 CO et art. 41 LP).
- Adaptation des prompts d'enrichissement (phase 5) ou du rerank (phase 7).
- **Pas** de fine-tuning direct sur ces données (risque d'overfitting sur cas rares, fuite vers le test set si non soigneusement séparé).

### 9.5 Garde-fous contre la sur-confiance utilisateur

- Affichage systématique d'un bandeau « Vérifiez chaque citation avant usage professionnel ».
- Indication du `validity_status` sur chaque arrêt retourné (OK / overruled / unknown).
- Mention du score de confiance global (dérivé du score composite) sur les réponses borderline.
- Lien direct vers le texte intégral de l'arrêt pour chaque citation produite par le LLM.

---

## 10. Cadence d'évaluation

### 10.1 Par phase du plan global

| Phase | Évaluation déclenchée | Métriques clés | Seuil gate |
|---|---|---|---|
| Phase 1 (contrats) | Dry-run du format benchmark sur 20 requêtes pilotes | Accord inter-annotateur, faisabilité | κ ≥ 0.60 |
| Phase 2 (migration) | Tests golden : parité SQLite vs Postgres sur 200 requêtes | Requête-à-requête diff = 0 sur top 10 | 100 % parité |
| Phase 3 (chunking SAC) | Dev set, Precision@10, Span-level Precision | SP@5 > baseline | SP@5 ≥ 0.40 |
| Phase 4 (embeddings Longformer) | Dev set, nDCG@10, Recall@50 | Gain vs BGE-M3 | nDCG@10 ≥ 0.55 |
| Phase 5 (enrichissement) | Dev set + sous-ensemble authority/temporal | Authority Correctness, Temporal Validity | AC ≥ 0.70, TV ≥ 0.75 |
| Phase 6 (MCP Edge Functions) | Tests golden : parité MCP stdio vs Edge Functions sur 200 requêtes | Parité fonctionnelle, latence | 100 % parité, p95 < 2× baseline |
| Phase 7 (rerank) | Test set (scellé) | Toutes métriques, test permutation | Tous seuils § 1.2 |
| Phase 8 (GraphRAG) | Dev set, sous-ensemble citation-aware | Gain sur requêtes « chaîne jurisprudentielle » | +5 pts nDCG@10 sur sous-ensemble |
| Phase 9 (permanent) | Mensuel complet, trimestriel test set, continu sampling | Toutes | Seuils production |

### 10.2 Régression à chaque déploiement

Avant tout déploiement en production :
1. Exécution automatique du dev set (150 requêtes) via pipeline CI.
2. Comparaison avec run précédent.
3. Blocage si régression > 2 points absolus sur nDCG@10 ou > 1 point sur Authority Correctness, sans approbation explicite.
4. Rapport PDF généré et archivé dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/runs/{timestamp}/`.

### 10.3 Évaluation mensuelle en production

Premier lundi ouvré de chaque mois :
- Run complet du dev set.
- Run du sampling cumulé du mois (180 requêtes auto-samplées).
- Revue par l'avocat référent du rapport auto-généré.
- Décision : statu quo / ajustement / investigation.
- Compte-rendu archivé.

---

## 11. Gouvernance

### 11.1 Rôles

- **Avocat référent (senior)** : propriétaire du benchmark. Décide du contenu des requêtes, valide les annotations, signe le CHANGELOG. Arbitre final sur les seuils qualitatifs. Responsable de la revue hebdomadaire.
- **Avocat secondaire** : double annotation, revue d'ambiguïtés, remplacement en cas d'indisponibilité.
- **Data engineer lead** : propriétaire du pipeline d'évaluation, des dashboards, de l'alerting. Implémente les métriques et CI.
- **ML engineer lead** : propriétaire des modèles (embeddings, cross-encoder, prompts LLM). Consomme les métriques, itère sur les variantes, propose les seuils techniques.
- **Tech lead / architecte** : arbitre en cas de conflit entre coût d'infra et gain de métrique. Valide les bascules de phase.

### 11.2 Décisions et arbitrages

| Décision | Proposée par | Validée par |
|---|---|---|
| Ajout d'une requête au benchmark | Avocat référent | Avocat référent |
| Retrait d'une requête du benchmark | Avocat référent ou data eng | Avocat référent |
| Seuil d'une métrique qualitative (AC, CF, HR, NTD, TV) | ML eng + avocat référent | Avocat référent |
| Seuil d'une métrique classique (P@k, R@k, nDCG) | ML eng | Tech lead |
| Bascule de phase (6→7, 7→8, etc.) | Tech lead | Tech lead + avocat référent |
| Rollback production | On-call data eng | Tech lead (confirmation post-hoc) |
| Rotation du test set | Avocat référent | Avocat référent |
| Modification du protocole d'annotation | Avocat référent + data eng | Tech lead + avocat référent |

### 11.3 Rôle de l'avocat référent

Pivot de toute la phase 9. Sans engagement ferme de l'avocat référent sur la durée (minimum 6 mois de revue hebdomadaire), la phase 9 ne peut pas fonctionner. Contrat de service explicite à établir en phase 1, avec :
- Temps alloué : 2 h/semaine (revue) + 20 h cumulées sur 6-8 semaines pour annotation initiale.
- Rémunération ou intéressement au projet si applicable.
- Back-up identifié (avocat secondaire).

### 11.4 Audit externe

Tous les 12 mois, audit externe du benchmark et du pipeline d'évaluation par un tiers juriste-technique (à identifier). Objectif : indépendance de la mesure, détection de biais internes.

---

## 12. Risques et mitigations

### 12.1 Benchmark trop petit

**Risque** : 200 requêtes = taille modeste. Un écart de performance peut être statistiquement significatif mais pratiquement non représentatif de tous les cas d'usage réels (un cabinet traite des milliers de types de requêtes).

**Mitigations** :
- Stratification thématique stricte (10 domaines, sous-quotas internes).
- Complément par sampling automatisé en production (logs agrégés).
- Revue hebdomadaire d'échantillons réels élargit de facto le benchmark.
- Rotation semestrielle du test set (renouvellement partiel) pour éviter l'obsolescence.
- Mention explicite de la limite dans tout rapport externe.

### 12.2 Biais d'annotateur

**Risque** : un seul avocat référent = une seule vision du droit. Certains domaines (droit fiscal cantonal, marchés publics) requièrent des spécialistes différents.

**Mitigations** :
- Double annotation sur 20 % + mesure de kappa.
- Consultation ad hoc de confrères spécialistes pour les 2 domaines les plus exotiques (fiscal, marchés publics).
- Marquage `ambiguous` des cas litigieux, exclusion du scoring principal.
- Documentation systématique du `rationale` → traçabilité des choix.
- Audit externe annuel.

### 12.3 Métriques mal alignées avec la valeur réelle

**Risque** : on optimise des chiffres (nDCG, Precision) qui ne reflètent pas ce que l'avocat utilisateur valorise réellement (gain de temps, confiance, absence de honte professionnelle).

**Mitigations** :
- Métriques juridiques spécifiques (Authority Correctness, Citation Fidelity, Hallucination Rate) sont conçues pour cela.
- Couche end-to-end (Answer Faithfulness, Answer Usefulness) mesurée par l'avocat lui-même.
- Feedback utilisateur (thumbs, flagging) traçable et agrégé.
- Revue trimestrielle explicite de l'alignement : « ces chiffres correspondent-ils à votre ressenti ? ».
- Priorisation P0 des seuils qualitatifs sur les seuils classiques en cas de conflit.

### 12.4 Leakage train/test

**Risque** : les requêtes du benchmark fuitent dans les prompts LLM (enrichissement phase 5), les jeux de fine-tuning d'embedding, ou les prompts système du produit. Conséquence : sur-estimation de la performance réelle.

**Mitigations** :
- Séparation stricte dev / test (150 / 50).
- Pre-commit hook vérifiant qu'aucun `query_id` n'apparaît en dehors de `bench/`.
- Hash-tracking des requêtes : aucun embedding LLM stocké en production ne doit correspondre à un hash de requête du test set.
- Audit périodique des datasets de fine-tuning.
- Rotation semestrielle du test set.

### 12.5 Drift non détecté

**Risque** : un biais lent s'installe (p.ex. embeddings qui se dégradent sur nouveaux arrêts post-2026) sans franchir de seuil d'alerte.

**Mitigations** :
- Monitoring continu (sampling auto 6 h).
- Alertes P2 sur dérive statistique (pas seulement sur seuils absolus).
- Revue mensuelle obligatoire avec œil humain sur les tendances.
- Fine-tuning ou reprise de l'embedding planifié tous les 12 mois, indépendamment des seuils.

### 12.6 Coût humain excessif

**Risque** : le protocole de revue hebdomadaire pèse sur l'avocat référent, conduit à désengagement, dégradation de la qualité des revues.

**Mitigations** :
- Automatisation maximale : le système prépare le dossier de revue, l'avocat n'a qu'à valider/infirmer.
- Budget temps cappé à 2 h/semaine.
- Back-up (avocat secondaire) prévu.
- Si taux de complétion < 70 % sur 4 semaines consécutives → alerte gouvernance, réévaluation du dispositif.

### 12.7 Faux positifs des métriques automatisées

**Risque** : détection d'hallucination automatique confondant une citation valable mais au format inhabituel avec une hallucination (ex. ATF cité en allemand dans une réponse FR).

**Mitigations** :
- Parseur de citations multi-lingues, testé sur un sous-ensemble de 500 citations connues avant mise en production.
- Tolérance explicite pour les variantes de format.
- Revue hebdomadaire des faux positifs signalés par le parseur.
- Calibration continue du seuil de similarité d'alignement (§ 4.2).

### 12.8 Dépendance à un prestataire externe (LLM-judge)

**Risque** : si Answer Faithfulness dépend d'un LLM externe (GLM-5.x), une dégradation ou indisponibilité du prestataire compromet la métrique.

**Mitigations** :
- LLM-judge exécuté en batch trimestriel (pas temps réel), tolère indisponibilité courte.
- Cache des jugements pour stabilité longitudinale.
- Back-up : Qwen3-Thinking ou autre modèle indépendant en cas de coupure.
- Sampling manuel de contrôle par l'avocat référent sur 20 % des jugements LLM.

---

## 13. Definition of Done

La phase 9 est considérée comme opérationnelle (et la bascule en production phase 7/8 autorisée) quand **tous** les points suivants sont verts :

### 13.1 Benchmark custom
- [ ] 200 requêtes annotées, distribution thématique et linguistique respectée.
- [ ] 40 requêtes double-annotées, kappa ≥ 0.70.
- [ ] Dev set (150) / test set (50) séparés, test set scellé.
- [ ] CHANGELOG signé par l'avocat référent.
- [ ] Pre-commit hook anti-leakage en place.
- [ ] Artefact versionné dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/`.

### 13.2 Pipeline d'évaluation
- [ ] Implémentation de chacune des 10 métriques (§ 3-4) avec tests unitaires.
- [ ] Pipeline CI exécutant le dev set à chaque PR touchant le retrieval.
- [ ] Rapport PDF auto-généré par run.
- [ ] Archivage des runs dans `bench/runs/{timestamp}/`.
- [ ] Intervalles de confiance bootstrap + tests de permutation implémentés.

### 13.3 A/B testing
- [ ] Mode `rag_naive` activable par feature flag.
- [ ] Run comparatif PA-RAG vs naïf sur test set, rapport archivé.
- [ ] Gain statistiquement significatif (p < 0.01) sur nDCG@10, AC, CF, HR, TV.
- [ ] Shadow mode 5 % en production opérationnel.

### 13.4 Observabilité
- [ ] 4 dashboards Grafana déployés et peuplés.
- [ ] Alertes P0/P1/P2 configurées et testées.
- [ ] Prometheus exporters déployés sur Postgres et Edge Functions.
- [ ] Sampling automatisé (10 req/6 h) opérationnel.
- [ ] Logs structurés JSON en production, rétention 30 j.
- [ ] Sentry ou équivalent branché sur Edge Functions et Word add-in.

### 13.5 Human-in-the-loop
- [ ] Thumbs up/down et flagging intégrés dans Word add-in et Claude Desktop.
- [ ] Table `feedback` opérationnelle dans Supabase.
- [ ] Processus de revue hebdomadaire opérationnel, premières 4 semaines exécutées.
- [ ] Dataset de correction versionné.
- [ ] Contrat d'engagement de l'avocat référent signé.

### 13.6 Gouvernance
- [ ] Matrice de décisions (§ 11.2) validée par les 5 rôles.
- [ ] Back-up (avocat secondaire) identifié.
- [ ] Plan d'audit externe annuel documenté.

### 13.7 Documentation
- [ ] Ce document (`phase-9-evaluation.md`) à jour.
- [ ] README dans `bench/` expliquant le format et l'usage.
- [ ] Runbook d'alertes (réponse opérationnelle) documenté.
- [ ] Rapport de référence v1.0 publié et signé par l'avocat référent.

---

## 14. Artefacts produits par la phase 9

- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/queries/` — 200 requêtes annotées, format JSON structuré, un fichier par domaine-langue.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/gold_spans/` — spans annotés par decision_id.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/runs/` — archive des runs datée, un dossier par timestamp.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/metrics/` — implémentations des 10 métriques (code en phase 9.2, hors scope de ce plan).
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/CHANGELOG.md` — historique versionné.
- `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/README.md` — mode d'emploi.
- Dashboards Grafana exportés en JSON, stockés dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/ops/grafana/`.
- Runbook alertes : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/ops/runbook-alerts.md`.
- Rapport mensuel et trimestriel : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/bench/reports/`.

---

## 15. Dépendances critiques vers les autres phases

- **Phase 1** : besoin du schéma figé des contrats (decision_id, chunk_id, span offsets) pour démarrer l'annotation.
- **Phase 2** : besoin des tests golden migration (200 requêtes exécutables sur les deux backends).
- **Phase 3** : chunking SAC impacte directement Span-level Precision — mesurer dès disponibilité.
- **Phase 4** : embeddings Longformer → nouvelle baseline de nDCG@10.
- **Phase 5** : enrichissement fournit les flags `validity_status`, `authority_score`, nécessaires pour AC / NTD / TV.
- **Phase 6** : feature flag `rag_naive` à implémenter côté Edge Function, sinon A/B testing impossible.
- **Phase 7** : cible principale de la validation quantitative — la bascule du rerank dépend des seuils de la phase 9.
- **Phase 8** : mesure marginale du gain GraphRAG, isolé sur sous-ensemble de requêtes citation-aware.

---

## 16. Coûts estimés (ordre de grandeur)

- Annotation initiale : ~180 h avocat senior + 14 h avocat secondaire. Coût externalisé ~25-40 k CHF selon tarif horaire.
- Revue hebdomadaire : 2 h × 52 sem × tarif horaire = ~10-15 k CHF/an.
- Infrastructure observabilité : Grafana Cloud free tier + Prometheus self-hosted ≈ 0-200 CHF/mois.
- LLM-judge (Answer Faithfulness) : ~10 CHF par run de 50 requêtes, 4/an = 40 CHF/an.
- Runs mensuels complets (dev set, 150 requêtes end-to-end) : ~15 CHF/mois × 12 = 180 CHF/an.
- Audit externe annuel : 3-8 k CHF selon scope.

**Total année 1** : ~40-60 k CHF (dominé par le temps avocat). **Années suivantes** : ~15-25 k CHF/an.

---

## 17. Sortie (handover vers opérations)

À la fin de la phase 9 (qui est « continue » mais atteint un régime stable après ~3 mois post-bascule phase 7), le dispositif d'évaluation et d'observabilité est transféré en mode opération permanente :
- Runbook documenté.
- Rôles et astreintes définis.
- Cadence mensuelle/trimestrielle automatisée.
- Budget récurrent alloué.
- Revue annuelle du protocole inscrite au calendrier gouvernance.

Le projet PA-RAG ne « termine » pas la phase 9 — il y entre et y reste aussi longtemps que le produit vit.
