# Phase 3 — Chunking SAC (Summary-Augmented Chunking)

> Sous-plan détaillé de la phase 3 du plan-maître `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/00-master-plan.md`.
> Rapport source : `/Users/damienhottelier/Downloads/RAG pour décisions juridiques — Architectures, pondération et implémentation.md` (sections 3, 3.1, 3.2, 5).
> Chunker actuel (baseline à remplacer) : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py`.
> Schéma cible (défini en phase 1) : table `chunks` dans la migration Supabase (cf. `docs/plan/phase-1-*.md`).
> Durée prévue : 3 semaines (cf. tableau des 9 phases, `00-master-plan.md`).

---

## 0. Résumé exécutif

Le chunker actuel (`search_stack/chunker.py`) produit **au maximum 3 chunks de 500 caractères** par décision, soit **1 500 caractères indexés** quel que soit l'arrêt. Or un ATF moyen fait 10 000 à 80 000 caractères, des arrêts TAF ou cantonaux atteignent 150 000 caractères. La couverture effective du texte indexé vectoriellement est donc **inférieure à 5 %** sur l'ensemble du corpus, et proche de **2 %** sur les ATF majeurs. Cette perte est masquée par la co-présence de FTS5 sur le texte complet, qui récupère par lexicalité ce que l'ANN rate — mais elle rend le retrieval vectoriel purement illusoire sur tout considérant situé au-delà des 500 premiers caractères d'une section.

La phase 3 remplace ce chunker par un pipeline **Summary-Augmented Chunking (SAC)** à trois étages :

1. **Parseur structurel multilingue** (DE/FR/IT) qui segmente chaque décision selon la structure canonique des décisions suisses : *en-tête → faits → considérants numérotés → dispositif*, en tolérant les variations syntaxiques du Tribunal fédéral (TF), du TAF, du TPF et des 26 cantons.
2. **Split récursif SAC** au niveau du considérant, avec fenêtre cible 400-512 tokens, overlap 15 % (~75 tokens) aux frontières de phrase, et **préfixation d'un *summary header*** (1-2 phrases générées par LLM) qui contextualise chaque chunk dans l'arrêt (parties, question juridique traitée, position dans le raisonnement).
3. **Classification `chunk_type`** (`faits | motivation | ratio | obiter | dispositif`) via le même appel LLM, permettant la pondération PA-RAG ultérieure (phase 5) : le *ratio decidendi* pèse plus que les *obiter dicta*.

Critère de succès central : **couverture ≥ 95 %** des caractères utiles du corpus indexés dans au moins un chunk, **tout en garantissant des spans exacts** (`span_start`, `span_end`) pour la citation span-level exigée par LegalBench-RAG. Budget LLM estimé ~14 M appels ; stratégies de réduction détaillées en §8.

---

## 1. Objectifs et critères de succès

### 1.1 Objectifs fonctionnels

- **O1 — Couverture** : au moins 95 % des caractères utiles (hors header/footer de greffe, hors tables des matières générées automatiquement, hors pages blanches PDF) de chaque décision sont couverts par au moins un chunk.
- **O2 — Granularité** : chaque considérant distinct (1., 1.1, 2., 2.1, 2.2, etc.) est représenté par au moins un chunk ; aucun considérant numéroté n'est fusionné avec un autre au sein d'un même chunk (sauf considérants < 50 tokens, regroupés par voisinage).
- **O3 — Taille** : chaque chunk a une longueur cible entre **256 et 512 tokens** (mesurés avec le tokenizer du modèle d'embedding cible `joelito/legal-swiss-longformer-base`, cf. phase 4). Tolérance : 128-768 tokens aux bords.
- **O4 — Contextualisation** : chaque chunk embarque en préfixe un *summary header* d'1-2 phrases qui situe le chunk dans l'arrêt (cf. §4).
- **O5 — Classification** : chaque chunk porte un `chunk_type` ∈ {`faits`, `motivation`, `ratio`, `obiter`, `dispositif`, `entete`, `autre`}.
- **O6 — Traçabilité** : `span_start` et `span_end` (offsets caractère dans le texte canonique de la décision) sont exacts au caractère près ; le contenu du chunk (sans le summary header) correspond **exactement** à `text[span_start:span_end]` modulo normalisation Unicode NFC documentée.
- **O7 — Idempotence** : re-exécuter le pipeline sur une décision dont le texte n'a pas changé ne produit aucune nouvelle écriture ni appel LLM (cache par hash).

### 1.2 Objectifs de qualité (évaluation — cf. §10)

- **Q1 — Frontières propres** : ≥ 90 % des chunks se terminent en fin de phrase (ponctuation forte ou balise structurelle).
- **Q2 — Distribution des tailles** : médiane entre 380 et 460 tokens, écart-type < 120 tokens hors bords de section.
- **Q3 — Accord inter-évaluateurs sur `chunk_type`** : ≥ 85 % d'accord humain-LLM sur un échantillon stratifié de 300 chunks (ATF, TF non publié, TAF, cantonal).
- **Q4 — Pertinence du summary header** : ≥ 90 % des headers sont jugés factuellement corrects et utiles sur un échantillon de 200 chunks (évaluation humaine binaire).
- **Q5 — Uplift retrieval** (évaluation extrinsèque, mesurée en phase 9) : recall@10 sur le benchmark de 200 requêtes ≥ +15 points vs baseline actuelle.

### 1.3 Objectifs économiques

- **E1 — Coût LLM** : ≤ 14 M appels LLM pour le corpus complet, avec profil de dépense détaillé en §8, dont **au moins 50 %** absorbables par cache (re-run, mises à jour incrémentales).
- **E2 — Débit** : débit stable ≥ 50 décisions/s en pipeline structurel pur (sans LLM), ≥ 5 décisions/s avec LLM activé sur 8 workers parallèles.
- **E3 — Budget mensuel soutenable** : possibilité d'activer le mode *LLM-off* (fallback) pour maintenir l'avancement du corpus long-tail cantonal sous contrainte budgétaire, avec rattrapage ultérieur possible sans ré-ingestion du texte brut.

### 1.4 Non-objectifs (hors périmètre de la phase 3)

- L'embedding lui-même (phase 4 — Longformer + pgvectorscale).
- L'extraction du *sort de l'affaire* (recours, première instance) — phase 5.
- Le calcul PageRank temporel, le score d'autorité composite — phase 5.
- La synchronisation graphe de citations — maintenue en place (phase 2) mais non modifiée ici.

---

## 2. Analyse critique du chunker existant

Fichier : `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py` (154 lignes).

### 2.1 Ce que fait le chunker actuel

Le chunker applique trois stratégies en cascade, avec arrêt à la première qui produit ≥ 2 chunks :

1. **Splitting par section** (`_split_by_sections`) : cherche les en-têtes *Sachverhalt/Faits/Fatti/Tatbestand*, *Erwägungen/Considérants/Considerandi/Begründung/Motivazione*, *Dispositiv/Dispositif/Dispositivo/Urteilsformel/Demnach erkennt*. Si au moins deux sections sont trouvées, il les retourne.
2. **Splitting par paragraphes** (`_split_by_paragraphs`) : split sur double saut de ligne, regroupe en `max_chunks` groupes équilibrés.
3. **Splitting positionnel** (`_split_positional`) : prend le début, le milieu et la fin du texte à offsets fixes.

Ensuite, **chaque chunk est tronqué à 500 caractères** (`c[:max_chunk_chars]`).

### 2.2 Défauts bloquants

1. **Plafond dur de 1 500 caractères indexés** par arrêt (3 × 500). Pour un ATF de 60 k caractères, **97,5 % du texte** n'est jamais embed­dé. La vector search sur ces décisions ne matche que le *début* de chaque grande section.
2. **Troncature aveugle à mi-mot** : `text[start:start+500]` coupe en plein milieu d'un mot, d'une citation d'article (« art. 285 LP ») ou d'une référence ATF (« ATF 150 II 1| »). Le tokenizer aval voit alors des fragments invalides.
3. **Pas de granularité considérant**. Un arrêt TF est structuré *1., 1.1, 1.2, 2., 2.1, 2.1.1, 2.2, 3.*. Le chunker traite *tout Erwägungen* comme un seul bloc, perdant la granularité nécessaire pour citer précisément un considérant (LegalBench-RAG).
4. **Pas de metadata** : le chunker retourne `list[str]`, pas de span, pas de type, pas d'identifiant de considérant. Impossible de pondérer *ratio* vs *obiter* en aval.
5. **Pas de contexte inter-chunk**. Un chunk pris isolément ne sait pas quelles étaient les parties, la question juridique, ni sa position dans le raisonnement — information critique pour l'embedding et la pertinence.
6. **Patterns d'en-tête fragiles** : les regex ne capturent pas « En fait », « En droit », « Par ces motifs », ni des variantes ponctuelles (*« Considérant en fait et en droit »*, *« Ritenuto in fatto »*, *« Ritenuto in diritto »*, fréquentes dans les arrêts TAF/TPF et les cantonaux). Le pattern `Motivation` matche aussi le mot courant, causant des faux positifs.
7. **Stratégie positionnelle** : quand ni sections ni paragraphes ne fonctionnent (typiquement OCR cantonal dégradé), le fallback prend trois snippets de 500 chars aléatoirement, ce qui produit un signal de retrieval quasi nul.
8. **Pas d'idempotence** ni de cache. À chaque réindexation, tout est recalculé.
9. **Pas de multilinguisme fin** : les trois langues sont jointes dans un seul regex ; pas de détection de langue par décision, pas de basculement de patterns (dans un arrêt cantonal FR, les titres DE sont des bruits de confusion).

### 2.3 Conséquence sur le retrieval actuel

Avec 1 024-dim BGE-M3 et 3 × 500 chars, la recherche vectorielle joue essentiellement sur *la première moitié du Sachverhalt* et *les 500 premiers caractères des considérants*. Tout développement d'un *ratio decidendi* situé au considérant 3.4 d'un ATF long est invisible à l'ANN. Le FTS5 masque partiellement ce défaut pour les requêtes à forte signature lexicale, mais pas pour les requêtes paraphrasées ou doctrinales.

**Conclusion** : réécriture complète requise. Aucun code du chunker actuel n'est préservé tel quel. Ses patterns regex servent de base de départ pour §3 mais sont refondus.

---

## 3. Conception du parseur structurel multilingue

Le parseur structurel est la **fondation** du chunking SAC. Sa qualité détermine directement la granularité des chunks, la précision des spans et donc la fiabilité de la citation span-level. Il fonctionne en deux couches : (a) **détection de langue** et **normalisation**, (b) **segmentation hiérarchique** en *en-tête → faits → considérants (avec arborescence numérique) → dispositif*.

### 3.1 Normalisation préalable

Avant tout parsing :

- Normalisation Unicode NFC (les arrêts TF mixent parfois NFC et NFD selon la source HTML/PDF).
- Normalisation des espaces : espaces insécables (U+00A0, U+202F) vers espace ordinaire **uniquement pour la recherche de patterns**, pas dans le texte canonique stocké. Les offsets `span_start`/`span_end` sont calculés **dans le texte canonique post-NFC** (avant toute substitution d'espaces).
- Normalisation des guillemets (« » → préservés ; " " → préservés), tirets (– — préservés).
- Détection et retrait des en-têtes/pieds de page récurrents OCR PDF (cantonaux) : lignes répétées sur chaque page (numéro de dossier, numéro de page). Heuristique : si une ligne apparaît ≥ 3 fois à intervalle régulier (toutes les ~3 000 chars), elle est marquée header répétitif et exclue des considérants (mais conservée dans le texte canonique, le parseur marque juste `is_repetitive=true` pour exclusion de chunking). Les spans restent calés sur le texte canonique.
- Détection de la **langue principale** de la décision :
  - Signal fort prioritaire : champ `language` déjà présent en DB (cf. `db_schema.py`) issu du scraping (bger.ch expose la langue dans l'URL, les portails cantonaux aussi pour TI).
  - Signal secondaire : fasttext `lid.176` sur les 2 000 premiers caractères du texte effectif.
  - En cas de conflit (rare : décisions trilingues TI ou décisions du TF citant longuement une source DE dans un arrêt FR), on conserve la langue DB et on journalise.

### 3.2 Segmentation hiérarchique — schéma général

Chaque décision est modélisée comme un arbre :

```
Decision
├── entete                (rubrum, parties, composition, objet)
├── faits                 (En fait / Sachverhalt / Fatti)
│   ├── A. ...
│   ├── B. ...
│   └── C. ...
├── considerants          (En droit / Erwägungen / Diritto)
│   ├── 1.
│   │   ├── 1.1
│   │   └── 1.2
│   │       └── 1.2.3
│   ├── 2.
│   └── 3.
└── dispositif            (Par ces motifs / Demnach erkennt / Il Tribunale federale pronuncia)
    ├── 1.
    └── 2.
```

Le parseur produit une liste ordonnée de **nœuds** avec :
- `node_id` (chemin hiérarchique, ex. `considerants.2.1`)
- `kind` (`entete`, `faits`, `faits_lettre`, `considerants_root`, `considerant`, `dispositif`, `dispositif_item`)
- `span_start`, `span_end`
- `language`
- `depth` (profondeur considérant, 0 pour racine section)
- `numbering_label` (brut, ex. `"1.2.3"` ou `"A."`)

Chaque nœud est potentiellement à son tour découpé en chunks SAC (§4), sauf `entete` qui produit un chunk unique non soumis à SAC LLM.

### 3.3 Patterns par langue

#### 3.3.1 Allemand (DE)

- **Faits** : `Sachverhalt`, `Tatbestand`, rare `In Sachen`, `Aus den Akten ergibt sich`.
- **Considérants** : `Erwägungen`, `In Erwägung`, `Das Bundesgericht zieht in Erwägung`, `Die Kammer zieht in Erwägung`, parfois simplement un considérant `1.` suivant immédiatement le rubrum.
- **Dispositif** : `Demnach erkennt das Bundesgericht`, `Demnach erkennt die Kammer`, `Demnach wird erkannt`, `Demnach verfügt`, `Das Bundesgericht erkennt`, `Das Gericht beschliesst`.

#### 3.3.2 Français (FR)

- **Faits** : `Faits`, `En fait`, `Vu les faits`, `Attendu que` (ancien style vaudois/neuchâtelois).
- **Considérants** : `Considérant(s)`, `En droit`, `Considérant en droit`, `Considérant ce qui suit`, `Le Tribunal fédéral considère`, `Droit`.
- **Dispositif** : `Par ces motifs`, `Par ces considérants`, `Le Tribunal fédéral prononce`, `Le Tribunal prononce`, `La Cour prononce`, `Pour ces motifs`.

#### 3.3.3 Italien (IT)

- **Faits** : `Fatti`, `In fatto`, `Ritenuto in fatto`, `Premesso in fatto`.
- **Considérants** : `Considerandi`, `In diritto`, `Ritenuto in diritto`, `Diritto`, `Motivazione`.
- **Dispositif** : `Il Tribunale federale pronuncia`, `Per questi motivi`, `Per questi considerandi`, `La Corte pronuncia`, `Il Tribunale pronuncia`.

#### 3.3.4 Règles de combinaison

- Les patterns sont **pondérés par langue détectée** (§3.1). Dans un arrêt FR, les patterns DE/IT reçoivent un poids de 0,2 (gardent valeur de secours si le texte est mixte ou mal typé en DB).
- Un en-tête est validé uniquement s'il est :
  - Sur une ligne isolée (précédée et suivie d'un saut de ligne ou en début/fin de texte) **ou** sur une ligne courte (< 80 caractères) **ou** immédiatement avant une numérotation `1.`, `A.`.
  - Éventuellement précédé d'une numérotation romaine ou alphabétique (`I.`, `II.`, `A.`, `B.`).
  - Pas entouré de guillemets ou de deux points précédents suggérant une citation intra-texte (*« selon les considérants »*).

### 3.4 Détection de la numérotation des considérants

La numérotation est la partie la plus délicate : le TF utilise `1.`, `1.1`, `1.1.1` (et parfois `1.1.1.1`). Les cantonaux utilisent aussi `1)`, `1-`, `a)`, `aa)`, `aaa)`. Le parseur fonctionne par **machine à états** lisant séquentiellement les lignes à l'intérieur de la section *considérants* :

- État **Attente_considerant** : cherche une ligne commençant par `^\s*(\d+)(\.\d+)*\.?\s` (numérique) ou `^\s*[a-z]+\)\s` (alphabétique cantonal). Reconnaissance tolérée de l'absence de point final.
- Transition sur détection : ouvre un nœud `considerant` avec `numbering_label` égal à la capture.
- État **Dans_considerant** : accumule jusqu'à rencontre d'un nouveau label **de profondeur ≤** (frontière), ou d'un en-tête de section ultérieure (*Dispositif*), ou de la fin du texte.
- Règle de profondeur : `1.1` ferme un `1.1` antérieur mais est enfant de `1.` ; `2.` ferme toute l'arborescence sous `1.` ; `1.1.2` est enfant de `1.1`. Le parseur maintient une pile de labels actifs et ferme les nœuds au pop.
- Tolérance OCR : labels aux ponctuations déformées (`1,` au lieu de `1.`, `l.` au lieu de `1.` quand l'OCR confond `1` et `l`) détectés avec règles prudentes + vérification contextuelle (label précédent + 1).
- En présence d'ambigüité (un `1.` en plein milieu d'une phrase : *« ... conformément à l'art. 1. »*), le parseur utilise l'heuristique : nouveau considérant ssi le label est **en tout début de ligne** (après au moins un `\n`), suivi d'au moins un espace puis d'une majuscule commençant une phrase.

### 3.5 Fallback sémantique pour décisions non structurées

Un sous-ensemble du corpus (estimation : 5 à 15 % des cantonaux, 1-2 % des arrêts TF anciens ou OCR dégradés) ne présente **aucun en-tête de section détectable** ni **aucune numérotation de considérants**. Pour ces cas :

- **Étage 1 — Heuristique paragraphe** : découpage par double saut de ligne, regroupement en paquets de 2-5 paragraphes jusqu'à atteindre 400-512 tokens. Aucun LLM invoqué. Toutes les parties marquées `kind=considerant` par défaut, `chunk_type=motivation` par défaut.
- **Étage 2 — Segmentation sémantique optionnelle** : si la décision est de priorité élevée (ATF, TF publié), un LLM léger segmente en *faits / motivation / dispositif* à partir du texte brut. Sinon, étage 1 suffit.
- **Marquage** : chunks issus du fallback portent `source_parser='fallback_paragraph'` ou `source_parser='fallback_semantic'` dans `chunks.meta`, ce qui permet un *re-run* ciblé ultérieurement quand un meilleur parseur ou un meilleur OCR est disponible.

### 3.6 Parseur — garanties

- **Déterministe** : sortie identique pour entrée identique. Aucune dépendance LLM à cet étage.
- **Sans perte** : l'union des spans des nœuds feuilles couvre à 100 % le texte canonique, modulo le marquage `is_repetitive` (header/footer OCR exclus). On vérifie cette invariant en test unitaire : `sum(end-start for leaf in leaves) + skipped == len(text)`.
- **Stable face aux variations** : un test golden de 500 décisions manuellement annotées (50 ATF × 3 langues + 250 cantonaux mixtes) maintient la régression.

---

## 4. Stratégie SAC — Summary-Augmented Chunking

Le SAC est la contribution centrale de la phase 3. Il transforme des nœuds structurels (considérants) en chunks embeddables tout en préservant le contexte global de l'arrêt.

### 4.1 Pourquoi préfixer un *summary header* avant embedding

Un considérant pris isolément perd l'information de contexte :
- Qui sont les parties ?
- Quel est l'objet du litige ?
- Quelle question juridique ce considérant tranche-t-il ?
- À quelle étape du raisonnement se situe-t-il (recevabilité, qualification, subsomption, conclusion) ?

Sans ces éléments, l'embedding d'un chunk de considérant 2.3 évoquant *« le principe de la bonne foi »* est indistinct entre 10 000 autres considérants traitant de bonne foi. En **préfixant 1-2 phrases de contexte** (ex : *« Recours en matière de marchés publics contre une décision du canton de Zurich ; le considérant examine l'application de l'art. 48 LMP au cas d'une offre anormalement basse. »*), le vecteur résultant se rapproche dans l'espace sémantique des requêtes qui **décrivent le problème juridique**, même si elles n'utilisent pas les termes exacts du considérant.

Cette technique est dérivée de :
- **Contextual retrieval** (Anthropic 2024) : préfixer un contexte court au chunk avant embedding augmente le recall de 35-50 %.
- **Late chunking** et **LLM-based chunking** (cf. rapport PA-RAG §3.1) : quand le chunking LLM-based est trop coûteux à l'échelle, la synthèse contextuelle préfixée est un compromis rentable.

**Règle d'inclusion du summary header** :
- Le header est concaténé au contenu au moment de l'embedding : `embedding_input = summary_header + "\n\n" + content`.
- Le header est **stocké séparément** (colonne `chunk.summary_header`) — il ne modifie **jamais** `content`, ni `span_start`, ni `span_end`.
- Pour la restitution à l'utilisateur (citation), seul le `content` est affiché, avec un lien vers `(decision_id, considerant_num, span)`.

### 4.2 Split récursif — algorithme conceptuel

Entrée : un nœud `considerant` (ou `faits`, ou `dispositif_item`) avec son texte, ses bornes `[start, end]`, sa langue.

Procédure :

1. Si `token_count(texte) ≤ 512`, produire un unique chunk ; passer à l'étape 5 (enrichissement summary).
2. Sinon, tenter un **split aux frontières de phrase** (tokenizer de phrases multilingue : règles Unicode + modèle léger pour les abréviations juridiques FR/DE/IT, ex. *« art. 285 LP »* ne termine pas une phrase). Viser des paquets de ~450 tokens, maximum 512. À chaque frontière de paquet, on s'assure que la dernière phrase **se termine réellement** (ponctuation forte ou fin de considérant).
3. **Overlap 15 %** (~75 tokens) : chaque chunk reprend les ~75 derniers tokens du précédent, **réalignés sur une frontière de phrase** (pas de coupure en milieu de phrase même pour l'overlap). L'overlap est appliqué **dans le texte canonique** : `span_start` du chunk N+1 recule jusqu'à englober ~75 tokens d'overlap avec le chunk N ; les offsets se chevauchent.
4. Si un considérant est **hiérarchique** (ex : un `1.` contient `1.1`, `1.2`) et que le chunker est invoqué sur le **nœud racine `1.`**, la règle est : **toujours chunker les enfants séparément**, ne chunker le racine que sur la portion *texte du racine avant le premier enfant* (préambule).
5. Pour chaque chunk produit à l'étape 2-3 :
   - Calculer `token_count` (tokenizer Longformer cible).
   - Assigner un `considerant_num` dérivé du `numbering_label` du nœud, suffixé d'un indice si le considérant est découpé en plusieurs chunks (ex. `2.3.a`, `2.3.b` pour les deux sous-chunks du considérant 2.3).
   - Calculer un `hash_content` (SHA-256 du `content` normalisé) pour idempotence.

### 4.3 Génération du summary header par LLM

Pour chaque chunk, un appel LLM produit :
- Le `summary_header` (1-2 phrases, ≤ 60 tokens, dans la langue du chunk).
- Le `chunk_type` (cf. §5).

**Inputs fournis au LLM** (description conceptuelle, aucun prompt complet en annexe conformément aux règles) :
- Métadonnées de la décision : cour, chambre, date, numéro, langue, éventuellement objet résumé du rubrum.
- Extrait du rubrum (≤ 500 tokens) : parties, objet du litige.
- Table des matières générée (liste des numéros de considérants et leur première ligne, ≤ 300 tokens) — **position dans le raisonnement**.
- Texte du chunk courant (≤ 512 tokens).
- Identifiant hiérarchique du chunk (ex. `considerants.2.3.a`).

**Outputs attendus** (format JSON strict, validé par schéma) :
- `summary_header` : string ≤ 60 tokens, rédigé comme une phrase descriptive en 3e personne, dans la langue du chunk.
- `chunk_type` : enum parmi les 7 valeurs (§5).
- `confidence` : float ∈ [0, 1], auto-estimation du LLM (utilisée pour fallback et QA).

**Règles de rédaction imposées au LLM** :
- Le header **ne doit pas** citer de numéros d'articles de loi absents du chunk.
- Le header **ne doit pas** conclure sur l'issue juridique si le chunk est un considérant intermédiaire (pas de *« conclut au rejet »* dans un chunk de subsomption).
- Le header **doit** mentionner l'objet général (marchés publics, responsabilité civile, droit des étrangers, etc.) et, si possible, la question juridique traitée par le chunk.

### 4.4 Cas particuliers

- **Chunk `entete`** : pas de SAC, chunk unique, pas de summary header (le header serait redondant), `chunk_type=entete`.
- **Chunk `dispositif_item`** : la plupart tiennent en < 100 tokens, pas de SAC. Un summary header est ajouté (*« Point du dispositif ordonnant X. »*) pour aligner l'embedding avec des requêtes du type *« décision qui admet partiellement »*.
- **Chunks très courts** (< 50 tokens) : si adjacents à un autre chunk du même `considerant_num`, fusion. Sinon, conservés seuls avec `chunk_type=motivation` (valeur par défaut).
- **Chunk sans LLM** (fallback budget, §8) : `summary_header=NULL`, `chunk_type='motivation'` par défaut, flag `needs_enrichment=true` pour reprise ultérieure.

---

## 5. Classification `chunk_type`

### 5.1 Schéma de classification

| `chunk_type` | Définition | Exemple | Poids PA-RAG (phase 5) |
|---|---|---|---|
| `entete` | Rubrum, composition, parties, objet | *« Cour de droit public ... Recourants A., B. contre ... »* | neutre (0,5) |
| `faits` | Exposé des faits de la cause | *« Le 3 mars 2022, la société X a déposé une offre... »* | 0,8 |
| `motivation` | Raisonnement général, exposé de la doctrine, rappel des règles | *« Selon l'art. 48 LMP, une offre anormalement basse... »* | 1,0 |
| `ratio` | Règle de droit appliquée au cas, porteuse de l'issue | *« Partant, la décision attaquée viole l'art. 29 Cst. en ce qu'elle... »* | 1,4 |
| `obiter` | Remarque incidente, non nécessaire à l'issue | *« Par surabondance, on relèvera que... »* | 0,7 |
| `dispositif` | Point du dispositif final | *« Le recours est admis. La décision est annulée. »* | 1,2 |
| `autre` | Incertain, OCR dégradé, métadonnées greffe | *« Berne, le 15 janvier 2024. »* | 0,3 |

### 5.2 Distinction `ratio` vs `motivation` vs `obiter`

C'est le point le plus délicat. Heuristiques inclues dans les instructions LLM :

- **`ratio`** : phrases qui appliquent la règle au cas d'espèce et qui **conditionnent l'issue** ; typiquement présentes dans les derniers considérants avant le dispositif, ou dans les considérants de subsomption ; marqueurs linguistiques : *« Partant, ... »*, *« Il s'ensuit que ... »*, *« Demzufolge »*, *« Ne segue che »*. La règle opératoire : si on retirait ce chunk, l'issue changerait.
- **`motivation`** : rappels de doctrine, exposé général des règles applicables, jurisprudence citée ; marqueurs : *« Selon la jurisprudence »*, *« De manière générale »*, citations d'articles sans lien immédiat au cas.
- **`obiter`** : formules explicites *« par surabondance »*, *« on pourrait ajouter »*, *« à titre superfétatoire »*, *« obiter »*, *« en passant »*, *« im Übrigen »*, *« übrigens »*, *« per inciso »*. Sans un de ces marqueurs explicites, ne jamais classer en `obiter` (préférer `motivation`).

### 5.3 Coût incrémental nul

La classification est **fusionnée dans le même appel LLM** que la génération du summary header (cf. §4.3). Pas d'appel supplémentaire, pas de coût supplémentaire. C'est l'un des leviers d'économie majeurs du pipeline.

### 5.4 Reclassification différée

La classification initiale peut être sous-optimale (voir §11 — risques). Un mécanisme de **reclassification différée** est prévu :

- En phase 5 (enrichissement PA-RAG), un second passage peut réévaluer `chunk_type` à la lumière du *sort de l'affaire* et des marqueurs de renversement de jurisprudence.
- En phase 9 (évaluation), un échantillon est humainement ré-étiqueté ; si le taux d'erreur > 15 %, un re-run ciblé est déclenché sur la population à risque (ex : tous les chunks `obiter` dont le `confidence` LLM < 0,7).

---

## 6. Choix du moteur LLM

### 6.1 Contraintes

- **Multilinguisme natif DE/FR/IT** (plus occasionnellement RM pour grisons) avec qualité équivalente aux trois langues.
- **Coût bas** : à 14 M appels, même 0,0005 USD/appel = 7 000 USD. Objectif < 0,0002 USD/appel ~= 2 800 USD total.
- **Latence raisonnable** : < 3 s par appel en p95.
- **Disponibilité batch / parallélisme élevé** : au moins 200 req/s soutenues.
- **JSON structuré fiable** (grammars / constrained decoding préférable).
- **Pas de data leakage** vers des tiers non contractuels (le corpus caselaw suisse est public, mais les synthèses générées sont notre propriété éditoriale).

### 6.2 Candidats

| Moteur | Forces | Faiblesses | Rôle |
|---|---|---|---|
| **synthetic.new GLM-5.x Reasoning** | Excellent en DE/FR/IT, reasoning chainé, JSON fiable, API batch, coût agressif | Fournisseur jeune, SLA à confirmer | **Principal** |
| **synthetic.new Qwen3-Thinking** | Très bon multilingue, fort en raisonnement juridique, moins cher encore | Moins éprouvé sur le suisse-allemand | **Secondaire / fallback** |
| Claude Sonnet 4.x | Qualité supérieure en rédaction FR, JSON strict excellent | Coût 3-10× supérieur | **Ciblage haute valeur** (ATF seuls, si QA révèle insuffisance) |
| Modèle local (vLLM + Qwen2.5-14B) | Aucun coût marginal, confidentialité | Investissement infra, débit limité sans GPU | **Option B** si budget sous tension |

### 6.3 Stratégie de cascade

1. **Défaut** : GLM-5.x Reasoning sur synthetic.new, mode batch, JSON schema imposé.
2. **Fallback applicatif** : en cas d'échec parsing JSON ou `confidence < 0,5` retourné, nouvel appel vers Qwen3-Thinking.
3. **Escalation ATF** : pour les chunks issus d'arrêts publiés au recueil ATF (flag `atf_published=true`), on active systématiquement un second appel Qwen3-Thinking et on compare les deux sorties ; en cas de divergence sur `chunk_type`, arbitrage par Claude Sonnet sur un sous-échantillon (≤ 1 % des cas).
4. **Circuit breaker** : si taux d'erreur JSON > 5 % sur fenêtre 10 min, bascule automatique vers Qwen3-Thinking et alerte opérateur.

### 6.4 Budget et latence ciblés

- Coût estimé à l'appel (GLM-5.x Reasoning, synthetic.new, ~1 000 tokens input / 100 output) : ~0,00012 USD.
- 14 M appels → ~1 700 USD si parcours complet, ~850 USD avec cache hit 50 %.
- Débit cible : 200 appels/s soutenus → 14 M / 200 = 70 000 s ≈ **20 heures de compute LLM cumulées**, soit ~3-4 jours calendaires avec parallélisme modéré et marges.

---

## 7. Stratégie de batch, priorisation, idempotence

### 7.1 Ordre de traitement (priorisation par autorité)

Conformément au principe *indexer d'abord ce qui pèse le plus dans le droit suisse* :

1. **Vague 1 — ATF (~25 k décisions)** : couvre tout arrêt publié au recueil officiel. Priorité absolue. Active la cascade LLM complète (GLM → QC Qwen sur échantillon 10 %).
2. **Vague 2 — TF non publiés (~940 k décisions)** : tous les arrêts du Tribunal fédéral depuis 2007 non publiés à l'ATF. LLM activé, QC réduit à 2 %.
3. **Vague 3 — TAF/TPF (~200 k)** : tribunaux fédéraux spéciaux. LLM activé.
4. **Vague 4 — Cantonaux 2e instance (~300 k)** : LLM activé.
5. **Vague 5 — Cantonaux 1ère instance et long-tail (reliquat)** : fallback sans LLM par défaut (§8), LLM activé sur demande ou par lot budgétaire mensuel.

Le budget LLM est alloué par vague ; le pipeline s'arrête automatiquement en fin de vague si le budget mensuel est dépassé.

### 7.2 File d'attente et orchestration

- **Queue persistante** dans Supabase (table `chunking_queue` créée en phase 1) : `decision_id`, `priority`, `attempts`, `status ∈ {pending, running, done, error, skipped}`, `last_error`, `worker_id`, `started_at`, `finished_at`.
- **Workers** : scripts Python (futurs) consommant la queue par batch de 50 décisions, parallélisme 8 workers par défaut, auto-tune selon rate-limits du fournisseur LLM.
- **Advisory lock Postgres** (`pg_try_advisory_lock(decision_id)`) pour garantir une unique exécution par décision, même en cas de crash.
- **Retry** : exponentiel, base 2 s, max 5 tentatives ; catégories d'erreur = transient (réseau, rate-limit → retry) vs permanent (JSON schema violation persistante → manual review).
- **Dead-letter** : après 5 échecs, la décision est rangée dans `chunking_dlq` avec trace complète, et une tâche humaine/LLM d'analyse est planifiée hors-ligne.

### 7.3 Idempotence par hash

- **Hash décision** : SHA-256 du texte canonique de la décision après normalisation NFC. Stocké dans `decisions.text_hash`.
- **Hash chunk** : SHA-256 du `content` normalisé. Stocké dans `chunks.content_hash`.
- **Règle d'idempotence** :
  - Si `decisions.text_hash` inchangé depuis le dernier run : **skip complet**, aucun appel LLM, aucune écriture.
  - Si `decisions.text_hash` changé : re-run complet de la décision, mais les chunks existants dont le `content_hash` est identique après re-parsing **ne sont pas ré-enrichis** (on copie l'ancien `summary_header` et `chunk_type`, ce qui économise l'appel LLM).
- **Cache auxiliaire** : table `chunk_enrichment_cache(content_hash, summary_header, chunk_type, model_id, created_at)` partagée entre décisions — si deux décisions contiennent un chunk textuellement identique (ex. formules greffe répétées), le second chunk hérite du premier.

### 7.4 Observabilité

- Dashboard minimal (phase 3) : nombre de décisions traitées par vague, taux d'erreur, coût LLM cumulé, débit, distribution `chunk_type`. Alimentation via métriques Prometheus émises par les workers.
- Logs structurés JSON dans une table `chunking_events(decision_id, event, payload, ts)` pour debug.

---

## 8. Budget LLM et fallback sans LLM

### 8.1 Estimation du volume d'appels

- 965 k décisions actuelles + marge de croissance → arrondi à 1 M pour planification.
- Longueur moyenne estimée : ATF ~40 k chars, TF non-pub ~18 k, cantonal moyen ~12 k. Moyenne pondérée ~15 k chars.
- Chunks par décision ~ `15 000 / (450 tokens × 4 chars/token × 0,85 après overlap)` ≈ **10 chunks/décision** en moyenne.
- Total chunks : **~10 M**. Avec overhead QA (double passage sur ATF 25 k × 10 = 250 k) → **~10,25 M**.
- Marge sécurité 35 % (ré-enrichissements ciblés, re-runs sur drift, chunks courts agrégés) → **~14 M appels LLM** (cohérent avec estimation du master plan).

### 8.2 Leviers d'économie

1. **Cache hash** (§7.3) : 30-60 % de hits attendus sur les re-runs, formules greffe, dispositifs standards.
2. **Fusion chunks courts** : réduit le nombre d'appels de ~8 % (chunks < 50 tokens fusionnés au voisin).
3. **Fallback sans LLM sur long-tail cantonal** (§8.4) : peut exclure temporairement 20-40 % des appels.
4. **Batching API** : groupage de 10-20 chunks par appel quand le fournisseur le permet → division du coût fixe par ~15.
5. **Summary header omis sur `entete` et `dispositif_item` simples** : ~5 % des chunks.

### 8.3 Profil budgétaire mensuel

- Mois 1 : Vagues 1 + 2 (ATF + TF non publiés) → ~9,5 M chunks × ratio appel ≈ 6-7 M appels effectifs → budget cible ~850 USD.
- Mois 2 : Vagues 3 + 4 (TAF/TPF + cantonaux 2e instance) → ~3 M appels → ~360 USD.
- Mois 3+ : Vague 5 en mode soutenable, budget 200 USD/mois jusqu'à complétion.

### 8.4 Fallback sans LLM

Activé pour le long-tail cantonal (vague 5) et en cas de pression budgétaire :

- Parseur structurel exécuté complètement (§3), y compris fallback sémantique (§3.5).
- Chunks produits avec `summary_header=NULL`, `chunk_type='motivation'` par défaut (sauf si §3 identifie *faits* ou *dispositif* structurellement, auquel cas le type structural prévaut).
- Flag `needs_enrichment=true`.
- Embedding en phase 4 fonctionne malgré l'absence de summary header (on embed `content` seul, perte de contextualisation acceptée sur le long-tail).
- Reprise ultérieure possible : un re-run ciblé appelle le LLM uniquement sur les chunks avec `needs_enrichment=true` et met à jour `summary_header` + `chunk_type` **sans re-embedding immédiat** — le ré-embedding est planifié en phase 4 incrémentale.

### 8.5 Invariants budgétaires

- **Ne jamais bloquer l'ingestion** : le pipeline doit produire des chunks exploitables même avec 0 appel LLM.
- **Ne jamais perdre l'information** : l'omission du summary header est toujours réversible ; la classification par défaut (`motivation`) est documentée comme *approximation* et ne doit pas servir de signal décisionnel en phase 5 tant que `needs_enrichment=true`.

---

## 9. Format et invariants de la table `chunks`

Le schéma de la table `chunks` est **conçu en phase 1** (migrations Supabase) et **peuplé en phase 3**. Cette section spécifie les invariants attendus du côté producteur (chunker) pour la validation en phase 2+ (golden tests).

### 9.1 Colonnes produites par la phase 3

- `id` (UUID v7, ordre temporel approximatif — utile pour la pagination).
- `decision_id` (FK vers `decisions.id`).
- `considerant_num` (string nullable, ex. `"1"`, `"1.2"`, `"1.2.3.a"` ; NULL pour `entete`).
- `chunk_index` (int, ordre séquentiel dans la décision, 0-based).
- `span_start`, `span_end` (int, offsets caractère dans le texte canonique NFC).
- `content` (text, exactement `decision.text_canonical[span_start:span_end]`).
- `content_hash` (SHA-256 hex).
- `token_count` (int, mesuré tokenizer Longformer).
- `language` (enum `de|fr|it|rm|other`).
- `chunk_type` (enum §5.1).
- `summary_header` (text nullable).
- `source_parser` (enum `structural|fallback_paragraph|fallback_semantic`).
- `source_model` (string nullable, ex. `glm-5.1-reasoning@synthetic.new`).
- `confidence` (float nullable ∈ [0,1]).
- `needs_enrichment` (bool, default false sauf fallback).
- `is_repetitive` (bool, default false ; true pour headers/footers répétitifs identifiés §3.1).
- `created_at`, `updated_at`.
- `meta` (JSONB, extensible, ex. `{"numbering_label":"1.2.3","depth":3,"parser_version":"3.1.0"}`).

### 9.2 Invariants stricts

- **Inv-1 — Span exact** : `chunks.content = text[span_start:span_end]` après normalisation NFC. Vérifié en test systématique sur échantillon 10 k chunks.
- **Inv-2 — Hash stable** : `content_hash = sha256(content)`. Re-calcul possible à tout moment.
- **Inv-3 — Ordre** : `chunks` triés par `chunk_index` couvrent la décision de bout en bout, modulo `is_repetitive=true` et avec overlaps explicites (les overlaps sont autorisés ; la couverture se calcule par union d'intervalles).
- **Inv-4 — Couverture ≥ 95 %** : `| union([start,end] pour chunks WHERE NOT is_repetitive) | / len(text) ≥ 0,95`. Métrique agrégée corpus-wide ; alerte si < 90 % par sous-corpus.
- **Inv-5 — Unicité** : pas de doublon `(decision_id, chunk_index)` ni `(decision_id, span_start, span_end)`.
- **Inv-6 — Cohérence type** : si `chunk_type='entete'` → `considerant_num IS NULL` et `chunk_index=0`.
- **Inv-7 — Token count** : 128 ≤ `token_count` ≤ 768 sauf exception motivée (`entete` court, `dispositif_item` très court).
- **Inv-8 — Source model présent ssi enrichi** : `source_model IS NOT NULL ⇔ summary_header IS NOT NULL`.

### 9.3 Traçabilité vers la source

- Chaque chunk est lié à `(decision_id, span_start, span_end)` permettant, au moment de la citation, de renvoyer l'utilisateur vers l'endroit exact du texte brut, avec surlignage span-level.
- La conservation du `decision.text_canonical` en DB (phase 2) est **nécessaire et non négociable** pour cette invariant.
- La table `chunks` ne doit **jamais** être la source de vérité du texte : elle est une **vue dérivée reproductible** à partir du texte canonique.

---

## 10. Évaluation de qualité du chunking

### 10.1 Métriques intrinsèques (calculables sans requête)

1. **Distribution des tailles** (tokens) : histogramme par cour, par langue. Cible médiane ~420, 80 % des chunks ∈ [256, 512].
2. **Taux de frontière propre** : % de chunks dont `content` se termine par `[.!?»\"]` suivi éventuellement d'un guillemet fermant ou d'un saut de ligne. Cible ≥ 90 %.
3. **Couverture** : cf. Inv-4.
4. **Granularité considérant** : ratio `nb_chunks / nb_considerants_détectés`. Cible 1,0-2,5 (la plupart des considérants tiennent en 1-2 chunks).
5. **Distribution `chunk_type`** : attendu corpus ATF ~ {faits 15 %, motivation 55 %, ratio 15 %, obiter 3 %, dispositif 5 %, entete 2 %, autre 5 %}. Déviation > 10 points sur un sous-corpus = alerte.
6. **Taux de fallback** : % de décisions traitées via `fallback_paragraph` ou `fallback_semantic`. Cible < 10 % global, < 3 % sur ATF/TF.
7. **Taux d'idempotence** : % de re-runs qui n'invoquent aucun LLM. Cible ≥ 50 % après la première passe complète.

### 10.2 Métriques humaines (échantillon)

- **300 chunks stratifiés** (50 par `chunk_type` × représentation équilibrée des 3 langues et de 5 niveaux de juridiction) :
  - Jugement binaire *header factuellement correct* (cible ≥ 90 %).
  - Jugement binaire *classification correcte* (cible ≥ 85 %).
  - Jugement *frontière sémantique acceptable* (cible ≥ 90 %).

### 10.3 Métriques extrinsèques (phase 9)

- Les gains réels du SAC se mesurent sur le benchmark de 200 requêtes (phase 9).
- Métriques cibles : **recall@10** +15 points vs baseline actuelle, **nDCG@10** +10 points, **span-level F1 (LegalBench-RAG)** +20 points.
- Ablation obligatoire : comparer SAC complet vs SAC sans summary header vs SAC sans classification pour quantifier l'apport de chaque composant.

### 10.4 Dashboard continu

Une fois opérationnel, le dashboard affiche :
- Couverture corpus agrégée et par cour.
- Distribution `chunk_type`.
- Coût LLM cumulé + restant budget.
- File d'attente `chunking_queue` (pending, running, dlq).
- Taux d'erreur et p95 latence LLM.

---

## 11. Risques et mitigations

### 11.1 Hallucinations de `summary_header`

**Risque** : le LLM invente des numéros d'articles, des noms de parties, des dates.

**Mitigations** :
- **Règle prompt** : interdiction de citer des articles absents du chunk et du rubrum fourni.
- **Validation post-hoc** : extraction regex des citations d'articles dans `summary_header` ; si une citation n'apparaît ni dans le chunk, ni dans le rubrum transmis → header rejeté, régénération.
- **Échantillonnage QA humain** : 50 headers/jour revus en phase pilote (première semaine), 10/semaine en régime.
- **Préfixage seulement**, jamais de réécriture du `content` : l'hallucination reste contenue dans un champ séparé et identifiable.

### 11.2 Dérive de classification `ratio` vs `obiter`

**Risque** : surclassement systématique en `ratio` (si prompt vague) ou sous-classement (modèle conservateur).

**Mitigations** :
- Marqueurs linguistiques **obligatoires** pour `obiter` (§5.2).
- `ratio` ne peut être attribué qu'à des chunks dont le considérant est dans le **dernier tiers** des considérants de la décision (heuristique) ; sinon, `motivation` par défaut, sauf si marqueurs explicites de subsomption au cas.
- Calibration initiale sur **100 ATF annotés manuellement** avant d'ouvrir le pipeline sur 1 M décisions.
- Mécanisme de reclassification différée (§5.4).

### 11.3 Drift multilingue

**Risque** : qualité significativement inférieure en italien (corpus d'entraînement LLM moindre), erreurs de détection de langue sur arrêts TI mixtes.

**Mitigations** :
- Évaluation séparée par langue en phase 9.
- Seuil d'alerte : si qualité IT < qualité FR/DE de plus de 10 points, bascule systématique des chunks IT vers Qwen3-Thinking ou Claude.
- Corpus de régression 100 arrêts par langue (entretenu en phase 3 et réutilisé en phases 5/9).

### 11.4 Drift de parseur structurel

**Risque** : évolution du style rédactionnel des cours (réorganisation, nouvelle numérotation) casse silencieusement les patterns.

**Mitigations** :
- `parser_version` stocké dans `chunks.meta`.
- Test de non-régression mensuel sur 500 décisions d'or.
- Alerte si taux de fallback augmente de > 2 points entre deux runs mensuels.

### 11.5 OCR dégradé (cantonaux anciens)

**Risque** : numérotations perdues, en-têtes corrompus, texte illisible.

**Mitigations** :
- Fallback sémantique (§3.5).
- Flag `ocr_quality` (déjà envisagé en phase 2) consulté avant parsing ; si `ocr_quality < 0,7`, passage direct en fallback paragraphe + `needs_enrichment=true`.
- Re-ingestion de PDF avec OCR amélioré (Tesseract 5, MinerU) planifiée en maintenance.

### 11.6 Coûts LLM hors contrôle

**Risque** : débordement budget, fournisseur change de pricing.

**Mitigations** :
- Budget cap hardcodé par vague (§7.1) et par mois (§8.3).
- Bascule automatique vers fallback sans LLM (§8.4) si `running_cost > 110 % budget`.
- Abstraction du fournisseur LLM derrière une interface stable → possibilité de bascule vers Qwen local en 48 h.

### 11.7 Idempotence faussée

**Risque** : changements subtils de normalisation Unicode ou de tokenizer invalidant silencieusement le cache hash.

**Mitigations** :
- Version de normalisation (`normalization_version`) stockée à côté du hash.
- Cache invalidé à toute bump de `normalization_version` ou `parser_version`.
- Tests unitaires : normalisation idempotente (normalize(normalize(x)) = normalize(x)).

### 11.8 Conflit avec retrieval phase 9

**Risque** : chunks trop petits (256 tokens) dégradent le cross-encoder ; chunks trop grands (768) dégradent l'ANN.

**Mitigations** :
- A/B test sur 3 tailles cibles (380, 450, 512 tokens) au début de la phase 4, avec un échantillon 50 k décisions ; choix tranché avant le rollout massif.
- Possibilité de re-chunker a posteriori à partir du texte canonique : le chunking n'est pas un engagement irréversible.

### 11.9 Violation de l'invariant span-exact

**Risque** : une normalisation oubliée décale les offsets d'un caractère.

**Mitigations** :
- Test fuzzer : sur 100 k chunks aléatoires, vérifier `content == text_canonical[start:end]`, taux d'échec < 10⁻⁵.
- CI bloquant tout merge qui casse ce test.

### 11.10 Fournisseur LLM unique (lock-in)

**Risque** : synthetic.new indisponible, prix qui s'envolent.

**Mitigations** :
- Interface LLM abstraite (§6).
- Fallback Qwen local documenté et **testé une fois** en environnement d'intégration avant mise en prod, pour garantir que la bascule n'est pas un code-path jamais exécuté.

---

## 12. Definition of Done

La phase 3 est considérée terminée lorsque **toutes** les conditions suivantes sont réunies :

### 12.1 Livrables techniques

- [ ] Parseur structurel multilingue DE/FR/IT opérationnel, couvrant les trois types de structure (faits / considérants hiérarchiques / dispositif), patterns documentés (§3.3).
- [ ] Fallback sémantique en place pour décisions non structurées (§3.5).
- [ ] Pipeline SAC (split récursif + overlap 15 % + summary header LLM + classification `chunk_type`) opérationnel en batch et idempotent par hash (§4, §7.3).
- [ ] Interface LLM abstraite, routage primaire GLM-5.x et secondaire Qwen3-Thinking, avec circuit breaker (§6).
- [ ] File d'attente `chunking_queue` + orchestration 8 workers + retry + DLQ (§7.2).
- [ ] Fallback sans LLM opérationnel et testé (§8.4).
- [ ] Table `chunks` peuplée pour **vague 1 (ATF complets)** et **au moins 30 % de la vague 2 (TF non publiés)** avant clôture de phase.

### 12.2 Invariants vérifiés

- [ ] Inv-1 à Inv-8 (§9.2) passent à 100 % sur un échantillon de 50 k chunks.
- [ ] Couverture corpus-wide ≥ 95 % mesurée sur le corpus actuel (hors long-tail cantonal).
- [ ] Couverture ≥ 90 % sur tout sous-corpus.

### 12.3 Qualité

- [ ] Métriques intrinsèques (§10.1) conformes aux cibles sur vague 1.
- [ ] Évaluation humaine sur 300 chunks stratifiés (§10.2) : accord ≥ 85 % sur `chunk_type`, ≥ 90 % sur header.
- [ ] Rapport de qualité versionné sous `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/docs/plan/reports/phase-3-quality-v1.md` (rédigé en fin de phase, hors présent plan).

### 12.4 Observabilité

- [ ] Dashboard minimal (§10.4) déployé.
- [ ] Métriques Prometheus exposées : `chunks_produced_total`, `chunking_llm_calls_total`, `chunking_llm_errors_total`, `chunking_coverage_ratio`, `chunking_queue_depth`.
- [ ] Logs structurés JSON dans `chunking_events` exploitables par requêtes SQL simples.

### 12.5 Coûts

- [ ] Coût LLM cumulé phase 3 ≤ 1 500 USD à la clôture (budget tenu pour vagues 1 + 2 amorcée).
- [ ] Cache hit ratio mesuré ≥ 40 % au deuxième run complet.

### 12.6 Risques maîtrisés

- [ ] Tous les risques §11 documentés et assortis d'une mitigation effectivement opérationnelle (pas seulement planifiée).
- [ ] Test de bascule fournisseur LLM (GLM → Qwen) exécuté au moins une fois en staging.
- [ ] Test de non-régression parseur structurel sur 500 décisions d'or intégré à la CI.

### 12.7 Compatibilité avec phases suivantes

- [ ] Schéma `chunks` validé par phase 4 (embeddings Longformer + pgvectorscale) : chaque chunk a bien un `content` ≤ 8 192 tokens (fenêtre Longformer) et un `token_count` renseigné.
- [ ] Champs nécessaires à la phase 5 (enrichissement PA-RAG) présents : `chunk_type`, `confidence`, `needs_enrichment`.
- [ ] Champs nécessaires à la phase 9 (évaluation span-level) présents : `span_start`, `span_end`, `decision_id`, `considerant_num`.

### 12.8 Sign-off

- [ ] Revue technique : ingénieur data + responsable produit juridique.
- [ ] Revue qualité : juriste suisse relit 50 chunks ATF + 50 chunks cantonaux, vise.
- [ ] Plan de maintenance post-phase 3 (versioning parseur, cycle de ré-enrichissement, budget récurrent) consigné dans `docs/runbooks/chunking-maintenance.md` (créé hors périmètre du présent plan mais listé comme DoD).

---

## Annexe A — Interactions avec les autres phases

- **Phase 1** (schéma Supabase) : fige la DDL de `chunks`, `chunking_queue`, `chunk_enrichment_cache`, `chunking_events`, `chunking_dlq`. Phase 3 consomme ce schéma sans le modifier.
- **Phase 2** (migration données) : garantit la présence de `decisions.text_canonical` et `decisions.text_hash` ; phase 3 en dépend directement.
- **Phase 4** (embeddings Longformer) : consomme `chunks.content` préfixé par `summary_header` au moment de l'embedding. Le format exact de la concaténation est fixé en phase 3 (« header + double saut de ligne + content »).
- **Phase 5** (enrichissement PA-RAG) : pondère les chunks par `chunk_type` selon les poids §5.1. Peut déclencher des reclassifications sur une base `confidence`.
- **Phase 7** (retrieval hybride) : exploite `chunks.decision_id`, `span_start`, `span_end` pour la restitution span-level.
- **Phase 8** (GraphRAG) : exploite `chunk_type='ratio'` pour identifier les passages qui fondent une relation `OVERRULES` ou `INTERPRETS` entre décisions.
- **Phase 9** (évaluation) : consomme les métriques intrinsèques et mesure l'uplift extrinsèque.

## Annexe B — Fichiers touchés / créés (indicatif, non engageant)

- **Créés** (ordres de grandeur — noms définitifs en phase d'implémentation) :
  - `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker_sac/` (package Python)
    - `parser_structural.py`
    - `parser_fallback.py`
    - `sac_splitter.py`
    - `llm_enrichment.py`
    - `llm_providers/` (abstraction GLM, Qwen, Claude, local vLLM)
    - `worker.py` (consommateur de `chunking_queue`)
    - `tests/` (golden tests, fuzzers d'invariants)
- **Déprécié** (archivé, non supprimé tant que la bascule n'est pas finalisée en phase 6) :
  - `/Users/damienhottelier/Documents/GitHub/caselaw-repo-1/search_stack/chunker.py` → déplacé vers `archive/chunker_legacy.py` à la fin de la phase 3.

## Annexe C — Hypothèses et points à trancher avant démarrage

1. **Tokenizer de référence** : confirmer en début de phase 3 que le tokenizer de `joelito/legal-swiss-longformer-base` est bien celui utilisé pour `token_count`. Si un autre modèle (ex. mE5-legal) est retenu en phase 4, re-calibrer les seuils 256/512.
2. **Langue RM** (romanche) : présence dans le corpus marginale. Décision : traiter comme `de` par défaut (parseur DE appliqué), sauf si un échantillon > 50 décisions le justifie.
3. **Arrêts pré-2000** : certains arrêts TF anciens ont une structure narrative sans considérants numérotés. Décision : fallback paragraphe, `chunk_type='motivation'` par défaut, marquage `parser_version_legacy=true`.
4. **Décisions multilingues** (TI, parfois BE) : si la décision alterne deux langues, la détection se fait chunk-par-chunk (fasttext sur chaque `content`), pas décision-par-décision.
5. **Politique de re-chunking** en cas de bump de `parser_version` : décision à prendre avec l'équipe infra — re-embedding complet coûte ~5-10 k USD en phase 4, à ne pas déclencher à la légère.

---

*Fin du sous-plan Phase 3.*
