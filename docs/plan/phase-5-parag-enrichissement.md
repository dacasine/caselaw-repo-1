# Phase 5 — Enrichissement PA-RAG : 4 piliers + sort de l'affaire

> Sous-plan détaillé de la Phase 5 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Source théorique : rapport PA-RAG `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`, sections 2 (4 piliers), 5 (PageRank dual), 7 (spécificités suisses).
> Dépendances amont : Phase 1 (schéma Supabase gelé, tables `decision_metadata_parag`, `decision_authority`), Phase 2 (miroir Postgres des 965 k décisions et des 8,84 M arêtes de citation), Phase 3 (chunking SAC — mutualisation LLM critique).
> Dépendances aval : Phase 7 (retrieval hybride — consomme les 4 scores pour le rerank), Phase 8 (GraphRAG — exploite `validity_status` et PageRank), Phase 9 (dashboards d'évaluation PA-RAG).
> Durée prévue : 4 semaines calendaires pour le premier palier ATF + TF publiés ; enrichissement long-tail cantonal étalé sur 6 mois supplémentaires en tâche de fond.

---

## 1. Objectifs et critères de succès

### 1.1 Objectif stratégique

La Phase 5 transforme le corpus Postgres brut (sortie de la Phase 2) en un corpus **précédent-aware** au sens du rapport PA-RAG. Elle matérialise, dans des colonnes SQL interrogeables en <5 ms par décision, les quatre signaux qui distinguent un RAG juridique crédible d'un RAG généraliste appliqué à des arrêts :

1. **Authority Score** — hiérarchie institutionnelle des juridictions (statique).
2. **Citation Centrality** — PageRank temporel sur le graphe de citations (recalculé périodiquement).
3. **Validity Status (Temporal Score)** — état de traitement ultérieur : overrulé, confirmé, nuancé, distingué (dynamique, hérité du graphe).
4. **Jurisdictional Score** — pertinence territoriale/matérielle (calculé à la requête, pas stocké).

En complément, la phase extrait le **sort de l'affaire** (admission, rejet, irrecevabilité, admission partielle) et segmente le raisonnement en **ratio decidendi** / **obiter dicta**, deux primitives sans lesquelles la formule de score composite (§ 2.5 du rapport) reste purement syntaxique.

### 1.2 Critères de succès mesurables

| Indicateur | Cible palier 1 (fin S+4) | Cible palier 2 (fin S+26) |
|---|---|---|
| Couverture Authority Score | 100 % des 965 k décisions | 100 % |
| Couverture `atf_published` | 100 % des BGE (~18 000) | 100 % + validation croisée Recueil Officiel |
| PageRank temporel calculé | 100 % du graphe (8,84 M edges) | Recalcul hebdomadaire automatisé |
| Validity status extrait | 100 % des ATF, 50 % des TF non publiés | 100 % TF, 20 % cantonaux |
| Sort de l'affaire extrait | 100 % TF + TAF + TPF | 100 % cours cantonales supérieures |
| Ratio/obiter annotés | 100 % ATF (~18 k), 30 % TF non publiés | 80 % TF, 10 % cantonaux |
| Discordance LLM vs regex sur sort | < 3 % | < 2 % |
| Cohérence PageRank (stabilité top-1000 entre runs hebdomadaires) | Jaccard ≥ 0,92 | ≥ 0,95 |
| Coût LLM cumulé (palier 1) | ≤ 25 kUSD (tarif synthetic.new) | ≤ 110 kUSD total |
| Latence de lecture d'un bundle enrichi (authority + pagerank + validity) | p95 < 10 ms | p95 < 5 ms |

### 1.3 Définitions de succès qualitatives

- Le rerank de la Phase 7 peut appeler, pour chaque *decision_id* candidat, un **unique** SELECT sur `decision_authority` retournant les quatre scores bruts + le flag `atf_published`. Aucun calcul lourd côté retrieval.
- Les 6 modes de traitement ultérieur (overrules | reversed | criticized | distinguished | affirmed | followed) produisent un drapeau `validity_status` à trois niveaux (rouge/jaune/vert) trivialement affichable dans le Word add-in (cf. `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/`).
- Un juriste peut, sur un échantillon aléatoire de 50 décisions, confirmer à ≥ 90 % la classification du sort et la distinction ratio/obiter (sampling manuel documenté dans § 11).

### 1.4 Non-objectifs explicites

- Pas de détection de l'autorité de la chose jugée *matérielle* entre parties (res judicata) — hors périmètre du PA-RAG.
- Pas de résumé narratif long : la SAC (Phase 3) produit déjà un résumé par considérant ; la Phase 5 ne re-résume pas.
- Pas de traduction multilingue des ratios : la langue de la décision est conservée (DE/FR/IT/RM). L'harmonisation sémantique multilingue relève des embeddings Longformer (Phase 4).

---

## 2. Pilier 1 — Authority Score

### 2.1 Principe et place dans la formule PA-RAG

L'Authority Score est la composante **la plus stable** des quatre piliers. Il ne bouge qu'en cas de réforme institutionnelle (rare ; p. ex. création d'un nouveau tribunal cantonal spécialisé). C'est aussi celui qui offre le meilleur ratio signal/coût : un simple mapping hiérarchique déjà largement encodé dans la colonne `court` du schéma actuel (cf. lignes 34-46 de `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`).

Le score est un entier court (1 à 6), stocké dans `decision_authority.court_level_score`. La formule composite PA-RAG (§ 2.5 du rapport) le normalise ensuite en [0, 1] par division par la borne supérieure.

### 2.2 Mapping hiérarchique détaillé

| Niveau numérique | Typologie | Exemples concrets dans le corpus |
|---|---|---|
| **6** (bonus atf_published) | Arrêts de principe du TF publiés au Recueil Officiel | BGE 147 I 268, ATF 149 II 1 |
| **5** | Tribunal fédéral (toutes sections), Tribunal fédéral des assurances (pré-2007) | `court = 'bger'`, `court = 'bge'` sans publication |
| **4** | Tribunal administratif fédéral (TAF/BVGer), Tribunal pénal fédéral (TPF/BStGer), Tribunal fédéral des brevets (TFB) | `court IN ('bvger', 'bstger', 'bpatger')` |
| **3** | Cours supérieures cantonales : Tribunal cantonal (civil/pénal/admin), Cour d'appel, Obergericht, Kantonsgericht, Verwaltungsgericht, Sozialversicherungsgericht, Cour des assurances sociales | Détection via combinaison `canton IS NOT NULL` ET `court` matchant une liste blanche (≈ 42 libellés distincts recensés dans l'actuel `decisions.court`) |
| **2** | Juridictions cantonales de première instance : tribunaux d'arrondissement, tribunaux de district, chambres patrimoniales, autorités de conciliation statuant sur proposition de jugement | Complément de la liste blanche niveau 3 |
| **1** | Autorités administratives fédérales (OFJ, SEM, FINMA, COMCO, IPI) et cantonales, commissions de recours pré-contentieuses, préfectures statuant en matière administrative | Corpus `regulators` (cf. 29 scrapers) |

### 2.3 Cas limites documentés

1. **Tribunaux spécialisés intercantonaux** : Chambre des avocats intercantonale, Autorité indépendante d'examen des plaintes en matière de radio-télévision (AIEP). Assignés niveau 3 par convention, justifié par leur compétence nationale en matière sectorielle.
2. **Justice intercantonale** (concordats romands — Cour de justice de Genève vs Chambre civile vaudoise) : pas de bonus inter-cantonal ; le pilier jurisdictional (§ 5) s'en charge à la requête.
3. **Arrêts du TF statuant comme juridiction unique** (p. ex. art. 1 LTF — recours contre actes des autorités fédérales) : traités comme niveau 5/6 standard, pas de bonus supplémentaire.
4. **Décisions d'autorités admin fédérales *quasi-juridictionnelles*** (décisions formelles FINMA, sanctions COMCO) : niveau 1 par défaut, **escaladé à 2** si le document présente la structure d'une décision formelle (dispositif, motifs, voies de droit) — détecté par heuristique structurelle sur la présence des sections attendues.
5. **Anciennes sections du TF absorbées** (Cour des assurances sociales à Lucerne pré-2007, IIIe Cour de droit social) : re-mappées vers `bger` niveau 5 au moment du calcul, sans toucher à la colonne `court` d'origine.
6. **Décisions de cassation** (Tribunal de cassation pénale VD pré-2011, Cour de cassation civile GE pré-2011) : niveau 3, malgré leur positionnement institutionnel supérieur pré-unification procédurale.
7. **Arrêts non signés / ordonnances présidentielles** (refus de l'effet suspensif) : même niveau que la formation ordinaire, pas de décote. Justification : ils produisent des effets juridiques équivalents pour le justiciable.

### 2.4 Signal `atf_published`

Le rapport (§ 7, ligne 229) insiste : la publication au Recueil Officiel **est** un signal indépendant fort, car résultant d'une sélection délibérée par le TF. L'implémentation retient trois voies parallèles de détection, avec union logique :

1. **Pattern canonique** : regex sur `docket_number` et `title` matchant `\bATF\s+\d{1,3}\s+[IVX]{1,4}\s+\d{1,4}\b` ou son équivalent allemand `BGE`. Capture ~98 % des cas.
2. **Jointure sur `court`** : les rows où `court IN ('bge', 'bge_historical')` ont systématiquement `atf_published = true`. Capture les historiques pré-numérisation.
3. **Champ déjà présent** (si fourni par le scraper bger — à confirmer dans le mapping de la Phase 2) : surcharge prioritaire.

En cas de discordance entre les trois sources, on privilégie la colonne du scraper, puis la jointure `court`, puis le regex. Les conflits sont loggés pour audit.

### 2.5 Stockage et recalcul

- Colonne `decision_authority.court_level_score SMALLINT NOT NULL` (schéma gelé en Phase 1).
- Colonne `decision_authority.atf_published BOOLEAN NOT NULL DEFAULT false`.
- Recalcul déclenché uniquement par :
  - création/mise à jour d'une règle de mapping (`mapping_version` tracé en métadonnée) ;
  - ingestion d'une nouvelle décision (trigger on INSERT dans `decisions`).
- Pas de recalcul périodique massif nécessaire → coût opérationnel marginal.

### 2.6 Tests de non-régression

- Échantillon de référence : 500 décisions tirées au hasard et annotées manuellement une fois (stocké dans un fixture versionné). L'exécution du mapping doit retourner exactement les niveaux attendus, sous peine de bloquer le déploiement.
- Comparaison croisée avec les stats attendues : ≈ 18 000 rows niveau 6 (BGE), ≈ 120 000 niveau 5 (TF non publiés), ≈ 45 000 niveau 4 (TAF+TPF+TFB), ≈ 380 000 niveau 3 (cantonales supérieures), etc. Tout écart > 5 % déclenche un audit.

---

## 3. Pilier 2 — Citation Centrality (PageRank temporel)

### 3.1 Principe et motivation

Le rapport (§ 2.2 lignes 53-56 et § 5.3 lignes 188-195) établit que PageRank capture la **qualité** des citations (être cité par un ATF pèse plus qu'être cité par un arrêt cantonal de première instance), là où l'in-degree brut est trivialement manipulable et biaisé par la productivité de certaines cours. Nous appliquons PageRank **temporellement pondéré** pour neutraliser le biais de récence identifié en § 9 du rapport (ligne 310).

### 3.2 Graphe d'entrée

Source : les 8,84 M arêtes de citation stockées dans la table `decision_citations` + sa résolution `citation_targets` (cf. `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py` lignes 68-93 et 209-429). Après migration Phase 2, équivalent Postgres avec :

- Nœuds : `decision_id` des 965 k décisions.
- Arêtes dirigées : citing → cited, déduites de `citation_targets` avec `confidence_score ≥ 0.5` pour limiter le bruit des faux positifs de résolution. Le seuil est paramètre (`min_confidence`), calibré sur l'échantillon d'évaluation.
- Poids d'arête : `w_ij = confidence_score × exp(-λ · (now - citing_date) / 365)` où `citing_date` = date de la décision citante (source, pas cible — la logique est que l'**acte de citer** vieillit, pas la décision citée).

### 3.3 Paramètre λ et calibration

- Valeur initiale : **λ = 0.099** (demi-vie ≈ 7 ans, cohérent avec le turnover jurisprudentiel suisse observé empiriquement : un arrêt reste typiquement cité massivement pendant 5-10 ans avant décrue).
- Grid search sur `{0.05, 0.07, 0.10, 0.14, 0.20}` équivalents à demi-vies {14, 10, 7, 5, 3,5 ans}.
- Fonction objectif : corrélation de Spearman entre le classement PageRank et le classement de référence humain sur un gold set de 100 arrêts phares (panel de 3 juristes, sélection a priori parmi les "grands arrêts" enseignés). Le λ optimal est celui maximisant ρ tout en maintenant un écart-type du top-1000 stable (éviter l'instabilité).
- Rechecker le λ tous les 12 mois ou après toute refonte majeure du corpus.

### 3.4 Algorithme de calcul

- Implémentation **hors Postgres** (graphe trop large pour du CTE récursif efficace) : extraction des arêtes vers un worker Python `networkx` ou `igraph` (préférence `igraph` pour les perfs sur graphes denses), calcul PageRank itératif (damping 0.85, tolérance 1e-6, max 100 itérations), réinjection des scores dans `decision_authority.pagerank_score REAL NOT NULL DEFAULT 0`.
- Format de sortie : score flottant dans [0, 1] **après normalisation par le max** (pas par la somme — le max donne une interprétation plus intuitive "part de la citation la plus autoritaire du corpus").
- Durée attendue : 20-40 min sur un nœud 32 GB RAM pour 8,84 M edges (benchmarks igraph standard).

### 3.5 PageRank dual

Suivant le rapport (§ 5.3 lignes 188-195), on calcule **deux** PageRanks indépendants :

1. **PR_cites** — graphe inter-décisions (CITES) : le graphe principal décrit ci-dessus. Mesure l'autorité **jurisprudentielle** ("cet arrêt est cité par des arrêts qui sont cités par d'autres arrêts importants").
2. **PR_appeals** — graphe inter-juridictions (APPEALS_TO) : graphe agrégé au niveau cour (≈ 200 nœuds, pas 965 k), où une arête A→B pondérée par le nombre de recours de A vers B. Mesure la **centralité institutionnelle** de chaque cour. Résultat projeté sur chaque décision individuelle via son `court`.

Ces deux scores sont stockés dans deux colonnes distinctes (`pagerank_cites_score`, `pagerank_appeals_score`) pour que la formule composite Phase 7 puisse pondérer séparément. La Phase 7 décide de la combinaison (somme pondérée, produit, max).

### 3.6 Biais de récence et mitigations

Le time-decay déplace mécaniquement les arrêts antérieurs à 1980 vers des scores très faibles, même si ce sont des arrêts historiquement structurants (p. ex. ATF 76 I 350 sur la liberté du commerce et de l'industrie). Deux mitigations :

1. **Plancher d'autorité** : tout arrêt `atf_published` reçoit un `pagerank_cites_score` minimal = médiane globale, garantissant qu'un grand arrêt ancien ne tombe jamais sous le radar du rerank.
2. **Variante non décayée** stockée en parallèle (`pagerank_cites_score_raw`) pour les requêtes explicitement "doctrine historique" du Word add-in. Coût de stockage marginal (8 B/row × 965 k = 8 MB).

### 3.7 Recalcul périodique

- **Fréquence** : hebdomadaire, nuit du dimanche au lundi (peu de charge utilisateur).
- **Déclencheur** : `pg_cron` job → appel d'une Edge Function qui orchestre :
  1. Export des arêtes fraîches vers le worker.
  2. Calcul PageRank dual.
  3. UPSERT en batch dans `decision_authority`.
  4. Incrément `pagerank_version` (audit trail).
- **Idempotence** : le job écrit dans une table staging puis swap atomique (rename). Aucun état intermédiaire visible en prod.
- **Monitoring** : métrique Jaccard du top-1000 entre runs successifs. Si Jaccard < 0,9, alerte manuelle (signe d'un changement massif du corpus ou d'un bug).

### 3.8 Coût ressources

- CPU : 1 run hebdo × 40 min = ~3 h CPU/mois.
- Stockage : 3 colonnes REAL × 965 k rows ≈ 24 MB.
- Trafic réseau interne : export/import de 8,84 M edges en binaire compact ≈ 200 MB.

---

## 4. Pilier 3 — Validity Status (traitement négatif et positif)

### 4.1 Taxonomie des 6 modes

Le pilier le plus **riche** et le plus coûteux. Inspiré directement de KeyCite/Shepard's (rapport § 2.3 ligne 68) mais adapté à la jurisprudence suisse, qui connaît plus rarement le *explicit overruling* à l'américaine et pratique davantage le *silent overruling* et la nuance progressive.

| Mode | Gravité | Description | Signal affiché |
|---|---|---|---|
| `overrules` | rouge | Revirement explicite de jurisprudence par une décision ultérieure de même rang ou supérieur | Drapeau rouge vif |
| `reversed` | rouge | Arrêt annulé en instance supérieure sur recours (le dispositif est cassé) | Drapeau rouge + icône d'appel |
| `criticized` | jaune | Arrêt maintenu mais critiqué explicitement (p. ex. "cette jurisprudence doit être nuancée") sans être renversé | Drapeau jaune |
| `distinguished` | jaune clair | Arrêt écarté dans un cas d'espèce pour défaut de similitude factuelle, sans remise en cause du principe | Drapeau jaune pâle |
| `affirmed` | vert | Arrêt confirmé explicitement par une instance supérieure ou ultérieure | Case verte |
| `followed` | vert | Arrêt suivi comme précédent par une autre cour (citation positive neutre — le cas par défaut) | Gris neutre (pas d'affichage actif) |

Le drapeau synthétique final (`validity_status` ∈ {rouge, jaune, vert, inconnu}) est dérivé par règle de priorité : `overrules > reversed > criticized > distinguished > affirmed > followed > défaut`. Un arrêt peut accumuler plusieurs annotations (p. ex. confirmé sur un point, critiqué sur un autre) ; on stocke le détail dans une table enfant et on agrège pour l'affichage.

### 4.2 Détection : LLM + signaux lexicaux

Pipeline à deux étages :

**Étage 1 — filtre lexical** sur les contextes de citation (±200 chars autour de chaque citation déjà résolue dans `citation_targets`). Dictionnaire multilingue :

- DE : *revidiert*, *ändert die Rechtsprechung*, *bestätigt*, *präzisiert*, *nuanciert*, *abweichend*, *aufgehoben*.
- FR : *revient sur sa jurisprudence*, *abandonne*, *confirme*, *précise*, *nuance*, *distingue*, *critique*, *écarte*.
- IT : *abbandona*, *conferma*, *precisa*, *si scosta da*, *critica*, *annulla*.

Seul les contextes matchant au moins un marqueur sont envoyés à l'étage 2 (économie LLM massive : ~3-5 % des contextes candidats).

**Étage 2 — classification LLM** sur les candidats. Un seul appel par (décision_citante, décision_citée, contexte) retourne un JSON typé : `{mode: one of 6, confidence: float, evidence_quote: string}`. Modèle cible : GLM-5.x Reasoning (synthetic.new), cf. plan-maître ligne 29. Budget de tokens : ~500 in / ~80 out par appel.

### 4.3 Propagation bidirectionnelle

Un verdict de traitement négatif affecte **deux** tables :

1. `decision_metadata_parag.validity_status` de la décision **citée** (la cible du traitement). C'est cette colonne que le retrieval consulte pour filtrer/dégrader.
2. `decision_citations_enriched.treatment_mode` pour **l'arête citante → citée** (table enfant de `decision_citations`). Permet de reconstituer l'historique : "cet arrêt a été critiqué par X en 2019 puis overrulé par Y en 2023".

Propagation transitive : si Y overrule X, et que X avait elle-même overrulé W, alors W **redevient** potentiellement applicable. On ne matérialise pas cette résurrection automatiquement — elle relève d'un jugement humain. Mais on signale la chaîne dans l'UI du Word add-in.

### 4.4 Edge cases

1. **Renversement partiel** : ATF qui revient sur un considérant précis tout en maintenant le reste. Stocké comme `overrules` avec un champ `scope` textuel (considérant renversé). Le `validity_status` global passe à **jaune** (pas rouge), car l'arrêt reste largement valide.
2. **Confirmation implicite** : un TF qui cite sans commentaire un arrêt ancien. Classé `followed` par défaut — pas de mise à jour du validity status (reste vert).
3. **Critique doctrinale intégrée** : le TF cite une décision cantonale et relève qu'elle "ne saurait être suivie". Classé `criticized` pour la cantonale.
4. **Chaînes longues** (A cite B cite C overrule X) : seule la chaîne directe est matérialisée ; les inférences transitives sont calculées à la requête via CTE récursif (Phase 8 GraphRAG).
5. **Revirement sans citation** (silent overruling) : **non détectable** par le pipeline actuel (pas de citation → pas de contexte). Mitigation : sampling humain sur les ATF récents, comparaison sémantique (embeddings Phase 4) entre le nouvel arrêt et sa jurisprudence antérieure sur la même norme — flagger les cas de forte divergence pour revue. Ce sous-chantier est listé en risque au § 12.
6. **Arrêts en langue rhéto-romane** : corpus marginal (<0,1 %). Traitement par règle : passer au LLM multilingue sans filtre lexical préalable, accepter un coût unitaire plus élevé.
7. **Décisions d'autorités administratives** : validity status possible uniquement si le TF/TAF a statué ensuite. Par défaut `inconnu`, jamais vert.

### 4.5 Gold set de validation

- Constituer un gold set de **200** paires (citant, cité) annotées à la main par 2 juristes, avec arbitrage en cas de désaccord. Mesurer F1 macro-pondéré sur les 6 modes. Seuil d'acceptation : F1 ≥ 0,80 global, avec F1 ≥ 0,70 pour chaque classe individuelle (pas d'effondrement sur une classe minoritaire comme `distinguished`).
- Re-mesurer F1 trimestriellement sur des échantillons frais pour détecter le drift (voir § 11).

---

## 5. Pilier 4 — Jurisdictional Score

### 5.1 Philosophie : score non stocké, calculé à la requête

Contrairement aux trois premiers piliers, la pertinence juridictionnelle dépend de **la requête** (qui cherche ? dans quel contexte ?) et pas uniquement de la décision. Un arrêt vaudois sur le droit du bail est très pertinent pour un juriste vaudois, moins pour un genevois, et purement persuasif pour un zurichois. Le Jurisdictional Score est donc **calculé à la volée** dans le rerank (Phase 7), en consultant une matrice statique `canton × matière`.

### 5.2 Matrice canton × matière

Dimensions :

- **Axe canton** : 26 cantons + `fédéral` + `intercantonal` = 28 valeurs.
- **Axe matière** : taxonomie dérivée des codes légaux primaires cités — 14 grandes catégories (droit civil, droit pénal, droit administratif, droit fiscal cantonal, droit des poursuites, droit du travail, droit du bail, droit de la famille, droit des assurances sociales, droit international privé, procédure civile, procédure pénale, procédure administrative, droit constitutionnel).

Pour chaque cellule (canton_requête, canton_décision, matière), on assigne un **facteur de pertinence** ∈ [0, 1] :

- **1,0** : décision fédérale sur n'importe quelle matière (liant erga omnes).
- **1,0** : décision du canton de la requête sur matière cantonale (droit fiscal GE pour requête GE).
- **0,7** : décision d'un canton tiers sur matière cantonale harmonisée (p. ex. droit procédural pré-2011) → persuasif fort.
- **0,5** : décision d'un canton tiers sur matière purement cantonale non harmonisée (droit fiscal ZH pour requête GE) → persuasif faible.
- **0,3** : décision d'une cour spécialisée étrangère au litige (p. ex. TFB pour une question civile classique).
- **0,0** : matière exclusivement fédérale citée par une autorité purement admin cantonale non compétente (cas rare).

La matrice est un fichier versionné (`/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/jurisdictional-matrix.yaml` à créer en Phase 5, Write-une-fois, édité à la main par un juriste référent).

### 5.3 Règles dérivées

1. **Droit fédéral toujours pertinent** : si la matière de la décision est fédérale (détectée par les statutes cités : CO, CC, CP, LP, LTF, LEtr, etc.), facteur = 1,0 indépendamment du canton.
2. **Droit cantonal pertinent dans son canton** : facteur = 1,0 pour matché, dégradé sinon selon matrice.
3. **Droit comparé (France / Allemagne / Autriche)** : ces décisions n'existent pas dans notre corpus (scrapers suisses exclusifs), mais en prévision d'un enrichissement futur (p. ex. Cour de cassation FR citée dans arrêts suisses), on prévoit un espace de noms `foreign_jurisdiction` avec facteur plafond 0,4 (persuasif uniquement).
4. **Arrêts CEDH / CJUE** : facteur 0,9 (quasi-fédéral) pour toutes matières touchant aux droits fondamentaux ou à l'ALCP. Identifiés par pattern `(CEDH|CJUE|EGMR|EuGH) \d+`.

### 5.4 Détection de matière

Heuristique en cascade :

1. Si la décision cite ≥ 3 articles d'un même code fédéral, assigner cette matière (majorité pondérée par fréquence).
2. Sinon, si `court` ∈ {TPF, TAF section pénale} → matière par défaut selon la cour.
3. Sinon, fallback LLM (mutualisé avec l'appel § 6-8) classifiant la matière dans la taxonomie de 14 classes.

Résultat stocké dans `decision_metadata_parag.primary_matter TEXT`, réutilisable hors scoring (filtres UI, statistiques).

### 5.5 Matière et multi-matière

Un arrêt peut toucher plusieurs matières (p. ex. ATF mêlant droit civil et procédure civile). Stockage en tableau : `primary_matter`, `secondary_matters TEXT[]`. Le Jurisdictional Score à la requête prend le max sur toutes les matières présentes.

---

## 6. Sort de l'affaire

### 6.1 Schéma complet

Repris du plan-maître (§ "Sort de l'affaire") :

- **Recours / appel** : `irrecevabilité | rejet | admission | admission_partielle`.
- **Première instance** : `admission | admission_partielle | rejet`.

Stocké dans `decision_metadata_parag.outcome TEXT NOT NULL CHECK (outcome IN (…))` + colonne auxiliaire `outcome_procedural_stance TEXT CHECK (IN ('recours', 'premiere_instance', 'autre'))`.

### 6.2 Détection du stade procédural (recours vs 1re instance)

Cascade de règles :

1. **Par la cour** : BGer, TAF, TPF, TFB, cours cantonales supérieures siégeant sur recours → `recours`. Tribunaux d'arrondissement, de district, autorités de conciliation → `premiere_instance`. Chambres de recours administratives cantonales → `recours` même si "chambre" dans le nom.
2. **Par la structure du document** : présence d'une section "Recours" dans les faits, mention "statuant sur le recours contre…" dans l'en-tête → `recours`.
3. **Par le LLM** en dernier recours (ambigu).

Le stade procédural pilote le **schéma** applicable pour l'outcome (`irrecevabilité` disponible uniquement pour `recours`).

### 6.3 Classification par LLM

Mutualisée avec l'appel de chunking SAC de la Phase 3 (voir § 8). Le LLM reçoit le **dispositif** (extrait par regex robuste — en-tête "Par ces motifs, le Tribunal fédéral prononce :" ou équivalents DE/IT — puis pris les 30 lignes suivantes), et retourne :

```
{
  "outcome": "admission_partielle",
  "outcome_confidence": 0.93,
  "outcome_evidence": "Le recours est partiellement admis ; l'arrêt attaqué est annulé en tant qu'il…"
}
```

### 6.4 Vérification croisée regex / LLM

Regex de contrôle indépendants :

- `\b(admis|admet|gutgeheissen|accolto)\b` → hint `admission`.
- `\b(partiellement|teilweise|parzialmente)\b` dans la même phrase → `admission_partielle`.
- `\b(rejeté|rejette|abgewiesen|respinto)\b` → `rejet`.
- `\b(irrecevable|nicht eingetreten|inammissibile)\b` → `irrecevabilité`.

Règle de flag :

- Si LLM et regex concordent → stockage direct, `outcome_check = 'ok'`.
- Si discordance → `outcome_check = 'flagged'`, revue manuelle, le LLM l'emporte provisoirement.
- Taux de discordance cible **<3 %** sur palier 1 (ATF + TF). Au-delà, audit du prompt LLM.

### 6.5 Cas limites

1. **Admission partielle complexe** : recours admis sur les frais, rejeté sur le fond. Dispositif à 5-6 chiffres. LLM doit traiter l'ensemble ; la granularité "qu'est-ce qui est admis" reste au niveau du texte libre du dispositif (pas d'extraction structurée fine en Phase 5).
2. **Renvoi à l'instance précédente** : admission parce que l'instance inférieure doit re-statuer, sans se prononcer sur le fond. Classé `admission_partielle` par convention (reflète l'issue procédurale du pourvoi).
3. **Radiation** (retrait du recours, transaction) : nouvelle valeur `radiation` à ajouter au schéma CHECK — **à valider** en revue de Phase 1 si pas déjà présente. Affecte ~2 % des TF.
4. **Dispositif manquant** (OCR défaillant, scraping incomplet) : `outcome = NULL`, `outcome_check = 'unavailable'`. Monitoré.
5. **Décisions incidentes** (art. 93 LTF) : dispositif sur la recevabilité uniquement. Classées `irrecevabilité` si irrecevable, `admission` si le TF entre en matière sur l'incident (rare).

---

## 7. Ratio decidendi vs obiter dicta

### 7.1 Principes juridiques

Cf. rapport § 2 lignes 114-118. La distinction, bien qu'héritée de la common law, est pratiquée implicitement par le TF quand il rédige les considérants : le raisonnement décisionnel y alterne avec des remarques incidentes ("au demeurant", "il y a lieu de relever…", "à titre superfétatoire"). Marquer ratio vs obiter permet à la Phase 7 de pondérer les chunks différemment dans le rerank : un chunk identifié comme ratio pèse davantage qu'un chunk obiter, toutes choses égales par ailleurs.

### 7.2 Critères d'extraction

Un considérant (ou plutôt **un passage** au sein d'un considérant) est classé `ratio` si :

1. il est **causalement nécessaire** au dispositif (sans lui, la solution changerait) ;
2. il tranche une **question de droit** énoncée par les parties ou relevée d'office ;
3. il est formulé en termes **généralisables** ("il découle de l'art. X CO que…") et pas purement factuels.

À défaut → `obiter`. Cas fréquent d'obiter : "la Cour peut laisser ouverte la question de savoir si…", "au surplus, il convient de relever…", "les autres griefs soulevés ne méritent pas d'examen".

### 7.3 Granularité

Décision stratégique : **granularité par considérant entier**, pas par phrase.

- Motivation : la SAC (Phase 3) produit déjà un chunk par considérant, aligné avec le découpage naturel du TF ("1.", "2.1", "2.2", "3.", etc.). Annoter au niveau du considérant donne donc une annotation par chunk, directement exploitable par le retrieval sans jointure fine.
- Conséquence : quelques considérants mixtes (ratio ET obiter) sont classés par leur caractère **dominant**. Minorité (<10 % empiriquement) ; acceptable.
- Granularité fine "par phrase" envisagée en Phase 8 (GraphRAG), pas en Phase 5.

Stockage : `chunks.reasoning_role TEXT CHECK (IN ('ratio', 'obiter', 'fait', 'procedure', 'dispositif', 'inconnu'))` — 6 rôles plutôt que 2, pour distinguer aussi les considérants purement factuels (état de fait), procéduraux (recevabilité), et le dispositif lui-même.

### 7.4 Décisions courtes

ATF de 1 500 chars ou moins (fréquent pour les non-entrées en matière — art. 108 LTF) : souvent **100 % procédure + dispositif**, aucun ratio au sens fort. Traitement : le LLM a la permission de retourner `ratio_decidendi: null` explicitement, et toute la décision est taggée `procedure` + `dispositif`. Pas de signal d'alerte.

### 7.5 Décisions longues et multi-questions

ATF de 40 000+ chars traitant plusieurs questions de droit : plusieurs passages ratio, chacun rattaché à une `legal_question`. Le LLM retourne une liste :

```
{
  "legal_questions": [
    {"question": "Champ d'application de l'art. 120 CO", "ratio_chunks": ["c.3.1", "c.3.2"], "outcome_on_question": "applicable"},
    {"question": "Calcul de l'indemnité pour tort moral", "ratio_chunks": ["c.5"], "outcome_on_question": "réduite"}
  ]
}
```

Stocké dans `decision_metadata_parag.legal_questions JSONB`.

### 7.6 Contrôle qualité

- Échantillon aléatoire de 50 ATF annotés manuellement (2 juristes, Cohen's κ attendu ≥ 0,70 pour valider la fiabilité inter-annotateurs humains).
- Mesure F1 sur classification `ratio` vs `obiter` uniquement (autres rôles détectés structurellement). Cible : F1 ≥ 0,75 palier 1, ≥ 0,85 palier 2.

---

## 8. Mutualisation avec la Phase 3 (SAC)

### 8.1 Justification économique

Le plan-maître (risque n° 1, ligne 63) identifie la facture LLM comme premier risque de la migration : ~14 M appels pour SAC complet. La Phase 5 ajoute potentiellement autant d'appels (1 par décision pour outcome, 1 par décision pour ratio/obiter, 1 par citation pour validity status, 1 par décision pour matière). Soit ~30 M appels si non mutualisé, contre ~15 M si mutualisé correctement.

### 8.2 Architecture de prompt unifié

La Phase 3 produit déjà un appel LLM **par décision** pour générer les résumés de considérants (SAC). On **étend** ce prompt pour qu'il retourne, en une seule passe, un JSON enrichi :

```
{
  "chunk_summaries": [
    {"chunk_id": "c.1", "reasoning_role": "fait", "summary": "…"},
    {"chunk_id": "c.2", "reasoning_role": "procedure", "summary": "…"},
    {"chunk_id": "c.3", "reasoning_role": "ratio", "summary": "…"},
    …
  ],
  "outcome": "…",
  "outcome_procedural_stance": "recours",
  "outcome_evidence": "…",
  "legal_questions": […],
  "primary_matter": "droit_civil",
  "secondary_matters": ["procedure_civile"],
  "ratio_decidendi_synthesis": "…",
  "obiter_dicta": ["…", "…"]
}
```

Le coût incrémental (ratio/obiter/outcome/matière) est estimé à **+15-20 %** de tokens in/out par rapport au SAC seul — négligeable vs le coût d'un deuxième appel complet.

### 8.3 Traitement séparé : validity status

La détection de traitement négatif reste **dissociée** du prompt unifié, car elle opère sur des **paires** (citant, cité) et non sur une décision seule. Volume : ~440 k citations avec marqueurs lexicaux après filtre étage 1 (5 % de 8,84 M). Budget LLM dédié : ~80 kUSD sur 6 mois.

### 8.4 Cache et idempotence

- Hash SHA-256 du texte normalisé de la décision → clé de cache. Toute réexécution avec même hash renvoie le résultat cached sans appel LLM. Couverture cible > 98 % sur re-runs.
- Versioning du prompt : incrément de `prompt_version` invalide le cache pour les lignes concernées (re-calcul à la prochaine passe).

### 8.5 Ordonnancement des passes

Ordre obligatoire :

1. **Passe A** (Phase 3 étendue) : prompt unifié → SAC + outcome + ratio/obiter + matière. Sur toutes les décisions à enrichir selon priorisation (§ 9).
2. **Passe B** (Phase 5 dédiée) : validity status sur les paires de citations, **après** résolution du graphe (dépend de `citation_targets`).
3. **Passe C** (Phase 5 dédiée) : PageRank. Indépendante du LLM. Peut tourner en parallèle des passes A/B.

---

## 9. Priorisation et rollout

### 9.1 Stratégie pyramidale

Le corpus compte ~965 k décisions mais la valeur informationnelle est concentrée : les ~18 k ATF génèrent >60 % des citations entrantes. On enrichit donc par ordre décroissant d'utilité marginale, pas uniformément.

| Palier | Cible | Couverture enrichissement complet | Échéance |
|---|---|---|---|
| **P0** | ATF (BGE) | 100 % (outcome + ratio/obiter + validity + matière + PageRank) | S+3 |
| **P1** | TF non publiés récents (2015-aujourd'hui) | 100 % (outcome + PageRank) + 50 % ratio/obiter | S+4 |
| **P2** | TAF + TPF + TFB (toutes années) | 100 % outcome + 50 % ratio/obiter | S+8 |
| **P3** | Cours cantonales supérieures 2020+ | 100 % outcome + 20 % ratio/obiter | S+16 |
| **P4** | Cantonal 2010-2019 | 50 % outcome + 10 % ratio/obiter | S+26 |
| **P5** | Cantonal antérieur + 1re instance | Opportuniste — triggered par signal (citation entrante forte) | long-tail continu |

PageRank est **toujours calculé globalement** (graphe complet hebdomadaire), indépendant du palier. Authority Score également (100 % dès J+1).

### 9.2 Critères de promotion vers palier supérieur

Un cantonal peut être "promu" vers un traitement enrichi (P3 vers P1-équivalent) si :

- il reçoit ≥ 10 citations entrantes depuis des décisions fédérales, OU
- son PageRank est dans le top 1 % de sa catégorie, OU
- il est demandé explicitement par un utilisateur via l'UI Word add-in (signal manuel, file d'attente).

### 9.3 Rollout technique

- Deployments progressifs via *feature flags* sur `decision_metadata_parag.enriched_version`.
- Phase 7 (retrieval) lit le flag : si `enriched_version IS NULL`, la décision n'entre pas dans le rerank authority (repli sur scoring basique). Garantit que la montée en charge est transparente pour les utilisateurs.
- Shadow eval : chaque palier déclenche une ré-évaluation du benchmark 200 requêtes (Phase 9) pour mesurer le gain marginal. Si le gain < 2 % nDCG@10, on arrête temporairement la progression et on investigue.

---

## 10. Stockage : tables concernées (contenu conceptuel)

Rappel : le schéma lui-même est gelé en Phase 1. Ici on précise uniquement **ce que chaque colonne contient** après la Phase 5.

### 10.1 `decision_authority` (une ligne par décision)

- `decision_id` PK.
- `court_level_score` : 1-6 (mapping § 2.2).
- `atf_published` : booléen (§ 2.4).
- `pagerank_cites_score` : [0, 1] normalisé, time-decayed (§ 3).
- `pagerank_cites_score_raw` : même sans time-decay (§ 3.6).
- `pagerank_appeals_score` : projection du PageRank inter-juridictions (§ 3.5).
- `pagerank_version` : int incrémenté à chaque recalcul hebdo.
- `authority_computed_at` : timestamp.

### 10.2 `decision_metadata_parag` (une ligne par décision)

- `decision_id` PK.
- `outcome` : enum § 6.1.
- `outcome_procedural_stance` : enum § 6.2.
- `outcome_confidence` : [0, 1].
- `outcome_check` : `ok` | `flagged` | `unavailable`.
- `validity_status` : `rouge` | `jaune` | `vert` | `inconnu` (§ 4.1).
- `validity_reasons` : JSONB — liste des (source_decision_id, mode, evidence_quote) ayant produit le status.
- `primary_matter` : enum 14 classes (§ 5.4).
- `secondary_matters` : TEXT[].
- `ratio_decidendi_synthesis` : TEXT — synthèse agrégée (2-4 phrases).
- `obiter_dicta` : JSONB[] — liste de passages identifiés.
- `legal_questions` : JSONB (§ 7.5).
- `enriched_version` : int (feature flag § 9.3).
- `prompt_version` : string, versionne le prompt LLM utilisé.
- `enriched_at` : timestamp.

### 10.3 `chunks` (une ligne par considérant-chunk, produit Phase 3)

Colonnes ajoutées ou remplies en Phase 5 :

- `reasoning_role` : enum 6 valeurs (§ 7.3).
- `ratio_weight` : REAL — poids suggéré pour le rerank Phase 7 (1,0 si `ratio`, 0,5 si `obiter`, 0,3 si `fait`, etc. — table statique).

### 10.4 `decision_citations_enriched` (table enfant, nouvelle)

- PK (`source_decision_id`, `target_decision_id`).
- `treatment_mode` : enum 6 modes (§ 4.1).
- `treatment_confidence` : [0, 1].
- `treatment_evidence_quote` : TEXT.
- `treatment_scope` : TEXT nullable (considérant concerné si renversement partiel).
- `detected_at` : timestamp.
- `llm_model_version` : string.

### 10.5 Matrice `jurisdictional_relevance` (table de configuration, 28 × 28 × 14 ≈ 11 000 cellules)

- PK (`canton_requete`, `canton_decision`, `matiere`).
- `relevance_factor` : [0, 1].
- Éditée manuellement ; versionnée via migrations.

---

## 11. Observabilité

### 11.1 Métriques de pipeline

Dashboards Supabase/Grafana à construire en parallèle de la Phase 5 (consolidés en Phase 9) :

- **Couverture par pilier** : % de décisions avec `court_level_score NOT NULL`, `pagerank_cites_score > 0`, `validity_status != 'inconnu'`, `outcome NOT NULL`, par palier (P0-P5) et par cour.
- **Distribution validity_status** : répartition rouge/jaune/vert/inconnu, évolution mensuelle. Un drift (p. ex. soudaine explosion du rouge) signale un bug du détecteur.
- **Recalcul PageRank** : temps d'exécution, Jaccard top-1000 vs run précédent, delta moyen de score sur le top 10 000.
- **Discordance LLM vs regex sur outcome** : % de `outcome_check = 'flagged'`, avec drill-down par cour et par période.
- **Coût LLM cumulé** : tokens in/out/USD par passe (A/B), par palier, par jour.
- **Latence lecture `decision_authority`** : p50/p95/p99 sur les SELECT du rerank Phase 7.

### 11.2 Alertes

- Jaccard PageRank hebdo < 0,9 → alerte ops + revue manuelle.
- Taux de flag outcome > 5 % sur une semaine → alerte qualité.
- Cache hit LLM < 95 % → alerte coût (suspicion d'invalidation inopinée).
- `pagerank_version` et `prompt_version` exposés en métadonnées de réponse API pour traçabilité client.

### 11.3 Sampling manuel

- **Audit mensuel** : 30 décisions tirées aléatoirement, chaque mois, annotées par un juriste. Comparaison à l'enrichissement automatique. Mesure F1 + κ. Résultats conservés en `audit_runs`.
- **Feedback utilisateur** : le Word add-in affiche un bouton "signaler une classification incorrecte" → feed une file `parag_feedback` révisée hebdomadairement par l'équipe juridique.
- Ces deux sources alimentent la **courbe de drift** du § 12.

### 11.4 Journalisation

- Chaque appel LLM est logué : `decision_id`, `passe`, `prompt_version`, `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `cached` (bool). Rétention 180 j.
- Logs PageRank : nombre de nœuds, arêtes, itérations, convergence, λ utilisé. Rétention illimitée (volume marginal).

---

## 12. Risques et mitigations

### 12.1 Hallucination LLM sur l'extraction ratio/obiter

**Risque** : le LLM invente un "ratio decidendi" plausible mais absent du texte (surtout sur arrêts courts ou atypiques).
**Mitigations** :
- Contraindre le prompt à retourner des **extraits textuels exacts** (spans) en plus de la synthèse, pour vérification regex (le span doit exister verbatim dans le texte source). Flag si le span n'existe pas → `ratio_check = 'spurious'`.
- Sampling manuel § 11.3 avec révocation systématique des spans hallucinés.
- Modèle à basse température (0-0,2) sur la passe A.

### 12.2 Drift du détecteur de traitement négatif

**Risque** : la distribution des modes (overrules/criticized/...) dérive dans le temps parce que le modèle LLM évolue côté fournisseur, ou le style de rédaction du TF change.
**Mitigations** :
- Re-score du gold set (200 paires, § 4.5) trimestriellement. Si F1 recule > 5 points, geler le modèle et investiguer.
- Ancrer `llm_model_version` dans `decision_citations_enriched`. Autoriser un modèle unique par trimestre pour stabiliser les comparaisons.
- Double-annotation humaine sur 20 paires par mois pour calibrage continu.

### 12.3 Biais historique amplifié par PageRank

**Risque** : PageRank favorise les arrêts déjà fortement cités → boucle de rétroaction qui enterre les arrêts récents pertinents.
**Mitigations** :
- Time-decay (§ 3.2) atténue mécaniquement.
- Monitoring du rapport d'âge moyen du top 1000 : si > 15 ans en moyenne, alerte.
- Pilier Temporal Score (non formalisé en Phase 5 mais prévu dans Phase 7) pondère à la baisse les arrêts anciens non publiés aux ATF, compensant le biais.

### 12.4 Faux positifs de résolution de citation

**Risque** : `citation_targets.confidence_score < 0.6` génère du bruit dans le graphe → PageRank et validity status biaisés.
**Mitigations** :
- Seuil minimum 0,5 (cf. § 3.2).
- Échantillonnage manuel des citations `confidence ∈ [0.5, 0.6]` pour estimer la précision réelle.
- Publication des stats de confiance dans les dashboards.

### 12.5 Coût LLM explosif sur cantonal

**Risque** : enrichir 500 k cantonaux à coût plein explose le budget (plusieurs centaines kUSD).
**Mitigations** :
- Palier P3-P5 explicitement dégradé (50 %, 20 %, opportuniste).
- Déclenchement par signal (§ 9.2) plutôt que couverture uniforme.
- Modèle moins cher (GLM-5.x Air plutôt que Reasoning) autorisé sur P4-P5 avec validation sur gold set.

### 12.6 Biais linguistique

**Risque** : le LLM est plus fiable en DE/FR qu'en IT (volume d'entraînement), encore moins en RM.
**Mitigations** :
- F1 mesuré séparément par langue sur le gold set. Seuil ≥ 0,75 par langue.
- Si IT < 0,75 : élargir le gold set IT à 50 paires, re-prompter avec few-shot spécifique.
- RM traité par exception (volume marginal).

### 12.7 Instabilité du schéma `outcome`

**Risque** : décisions de radiation, suspension, renvoi pur → catégories non couvertes.
**Mitigations** :
- Audit trimestriel des rows avec `outcome = NULL` ou `outcome_check = 'flagged'`.
- Évolution du CHECK constraint via migration si une nouvelle catégorie émerge > 1 % du volume.

### 12.8 Dépendance externe (synthetic.new)

**Risque** : indisponibilité, changement de pricing, deprecation des modèles cibles.
**Mitigations** :
- Abstraction derrière une interface `LLMProvider` pour permettre bascule vers un provider alternatif (OpenRouter, Mistral La Plateforme, self-hosted Qwen) sans réécriture.
- Cache disque local agressif (§ 8.4) permet re-runs sans re-facturation en cas de coupure.

### 12.9 Mises à jour rétroactives du graphe

**Risque** : un nouvel ATF de 2026 peut overrule un arrêt de 2018 → il faut mettre à jour le `validity_status` de 2018 **rétroactivement**.
**Mitigations** :
- Passe B (validity status) re-tourne sur les citations nouvellement ingérées.
- Trigger sur INSERT dans `decision_citations` pour ré-évaluer les cibles si marqueurs lexicaux présents.
- Versioning `validity_version` dans `decision_metadata_parag` pour audit.

---

## 13. Definition of Done

La Phase 5 est considérée terminée — et autorise la bascule vers la Phase 6 (MCP Edge Functions) — lorsque **tous** les points suivants sont vrais et documentés :

### 13.1 Couverture

- [ ] 100 % des 965 k décisions ont un `court_level_score` non nul.
- [ ] 100 % des BGE ont `atf_published = true` (vérifié par pattern + jointure `court`).
- [ ] 100 % des décisions ont `pagerank_cites_score` et `pagerank_appeals_score`.
- [ ] 100 % des ATF ont `outcome`, `ratio_decidendi_synthesis`, `obiter_dicta`, `legal_questions`, `primary_matter` renseignés.
- [ ] 100 % des TF non publiés 2015+ ont `outcome` renseigné ; 50 % ont ratio/obiter.
- [ ] 100 % des TAF + TPF ont `outcome` renseigné.
- [ ] 20 % des cantonaux 2020+ ont `outcome` renseigné.
- [ ] 100 % des chunks issus de la Phase 3 ont `reasoning_role` non `inconnu`.

### 13.2 Qualité

- [ ] F1 global du détecteur validity status ≥ 0,80 sur le gold set 200 paires, avec F1 par classe ≥ 0,70.
- [ ] F1 ratio/obiter ≥ 0,75 sur l'échantillon 50 ATF.
- [ ] Taux de discordance LLM/regex outcome < 3 %.
- [ ] Taux de hallucination ratio (span non retrouvé) < 2 %.
- [ ] Cohen's κ inter-annotateurs humains ≥ 0,70 sur le gold set.
- [ ] Corrélation Spearman PageRank vs jugement humain sur 100 grands arrêts ≥ 0,60.

### 13.3 Opérabilité

- [ ] Job `pg_cron` hebdomadaire de recalcul PageRank déployé et exécuté avec succès deux semaines d'affilée.
- [ ] Dashboards couverture + distribution validity + coût LLM + latence lecture opérationnels.
- [ ] Alertes configurées (Jaccard, flag rate, cache hit, latence).
- [ ] Rollback documenté : procédure pour revenir à un `enriched_version` antérieur en cas de régression.
- [ ] Cache LLM hit rate ≥ 95 % sur re-runs.

### 13.4 Traçabilité

- [ ] Toutes les tables enrichies ont `prompt_version`, `llm_model_version`, `pagerank_version` selon applicable.
- [ ] Logs d'appels LLM rétention 180 j opérationnels.
- [ ] Audit trimestriel documenté (template Markdown versionné).
- [ ] Gold set 200 paires + 50 ATF + 100 grands arrêts stockés dans un fixture versionné dans `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/fixtures/phase-5/` (structure à créer en première itération).

### 13.5 Validation juridique

- [ ] Sign-off explicite d'un juriste référent sur la matrice `jurisdictional_relevance`.
- [ ] Sign-off explicite sur la taxonomie des 6 modes de traitement.
- [ ] Sign-off explicite sur le mapping hiérarchique (§ 2.2) incluant les cas limites.
- [ ] Revue des 30 premières décisions enrichies du palier P0 par un juriste, sans correction majeure (< 3 erreurs par lot de 30).

### 13.6 Coût

- [ ] Coût cumulé palier 1 ≤ 25 kUSD (conforme budget § 1.2).
- [ ] Projection coût paliers 2-5 ≤ 110 kUSD total documentée et approuvée.

### 13.7 Intégration aval

- [ ] Phase 7 peut consommer les 4 scores via une unique requête SELECT < 10 ms p95.
- [ ] Word add-in affiche `validity_status` (drapeau) et `outcome` sans modification côté client au-delà de la lecture des nouveaux champs.
- [ ] Dataset HuggingFace Parquet schéma inchangé (invariant 4 du plan-maître) — les nouvelles colonnes sont optionnelles et ajoutées sans rupture.

---

## Annexe A — Dépendances et artefacts référencés

- Plan-maître : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`
- Rapport PA-RAG : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md`
- Graphe de citations actuel (SQLite source) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_reference_graph.py`
- Extraction citations/statutes : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/reference_extraction.py`
- Chunker actuel (remplacé par SAC en Phase 3) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py`
- Embeddings (dépendance Phase 4) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/build_vectors.py`
- Word add-in (consommateur des champs outcome + validity_status) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/tools/word-addin/`
- MCP monolithique (sera remplacé en Phase 6) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/mcp_server.py`
- REST FastAPI (consommera les nouveaux scores en Phase 7) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/web_api/main.py`
- Matrice jurisdictional (à créer) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/jurisdictional-matrix.yaml`
- Fixtures gold sets (à créer) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/fixtures/phase-5/`

## Annexe B — Glossaire rapide

- **ATF / BGE** : Arrêts du Tribunal fédéral publiés au Recueil Officiel (Recueil officiel des arrêts du Tribunal fédéral). Sélection éditoriale délibérée.
- **SAC** : Summary-Augmented Chunking (Phase 3) — chunking par considérant avec résumé LLM.
- **PageRank dual** : double calcul, l'un sur le graphe inter-décisions, l'autre sur le graphe inter-cours.
- **Validity status** : équivalent KeyCite/Shepard's adapté au droit suisse.
- **Ratio decidendi** : raisonnement central, nécessaire au dispositif, à valeur normative.
- **Obiter dicta** : remarques incidentes, persuasives mais non contraignantes.
- **Authority Score** : score hiérarchique institutionnel 1-6.
- **Jurisdictional Score** : score de pertinence territoriale/matérielle, calculé à la requête.
