"""Phase 5: per-decision metadata extraction (sort, ratio, obiter, doctrine).

One LLM call per decision (separate from Phase 3 SAC batched calls).
Input is the full text of the arrêt plus the list of SAC summary headers
already computed for its chunks. Output is a strict JSON object matching
the DecisionMetadata schema below.

Expected scope:
    BGE (published leading cases, 22k): full enrichment. Highest value —
        these decisions carry real ratio/obiter distinctions and usually
        discuss doctrine.
    BGer (unpublished, 175k): lighter variant. Most BGer decisions aren't
        leading cases; run a reduced prompt asking only for sort + legal
        areas + prior_case_treatment. Save Phase 5 LLM budget for BGE.
    Cantonal: deferred.

The builder intentionally keeps the prompt in a separate module so we
can validate it on 5 decisions, iterate, and bump PROMPT_VERSION
without touching the worker.
"""

from __future__ import annotations

from dataclasses import dataclass


# Bump when the prompt semantics change; worker re-enriches arrêts whose
# stored version is older.
ENRICHMENT_PROMPT_VERSION = 1


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FULL = """Tu es un juriste spécialiste du droit suisse. Tu analyses un arrêt \
du Tribunal fédéral et en extrais des métadonnées structurées.

Réponds UNIQUEMENT avec un objet JSON strict (pas de markdown, pas de \
préambule, pas d'explication). Le schéma exact à suivre est :

{
  "sort": {
    "procedural_stage": "recours" | "premiere_instance",
    "outcome": "admission" | "admission_partielle" | "rejet" | "irrecevabilite",
    "subject_matter": "phrase courte (≤ 25 mots) décrivant l'objet du litige"
  },
  "principle_questions": [
    {
      "question": "question juridique tranchée (≤ 30 mots)",
      "ratio": "règle de droit posée pour répondre à cette question (2-4 phrases)",
      "legal_basis": ["art. X CC", "ATF Y"]
    }
  ],
  "obiter_dicta": [
    "remarque incidente 1 (≤ 30 mots, ne fonde pas le dispositif)"
  ],
  "doctrine_discussion": {
    "discussed": true | false,
    "authors_cited": ["Nom d'auteur"],
    "positions_weighed": [
      "brève description de la position et si le TF la suit / écarte"
    ],
    "is_leading_case_signal": true | false
  },
  "prior_case_treatment": [
    {
      "cited_decision": "ATF 137 II 313" | "BGer 6B_123_2019",
      "direction": "confirms" | "develops" | "distinguishes" | "overrules" | "criticizes" | "neutral"
    }
  ]
}

RÈGLES STRICTES :

1. **Multiplicité des questions de principe** : un arrêt peut trancher \
PLUSIEURS questions de principe distinctes. Énumère-les TOUTES dans \
`principle_questions`. Ne te contente pas de la question "principale".

2. **Ratio vs obiter** : une règle est `ratio` si son renversement \
conduirait à un dispositif différent ; sinon c'est un `obiter`. Ne mets \
JAMAIS dans `principle_questions` une règle dont on peut retirer la \
mention sans changer l'issue.

3. **Doctrine** : `discussed=true` uniquement si le TF nomme un ou \
plusieurs auteurs (Tercier, Guillod, Werro, Piotet, etc.) ET discute \
leur position. Une simple citation (« cf. Tercier 2019 p. 45 ») ne \
compte PAS comme discussion. `is_leading_case_signal=true` si la \
discussion est substantielle (plusieurs auteurs, arbitrage entre \
positions).

4. **prior_case_treatment** : uniquement les arrêts où le TF pose un \
traitement normatif explicite (confirme, étend, distingue, renverse, \
critique). Une citation neutre « cf. ATF X Y Z » est `neutral` et peut \
être omise si on en a beaucoup ; limite-toi aux plus pertinents.

5. **Langue contextuelle** : rédige `subject_matter`, `question`, \
`ratio`, `obiter_dicta`, `positions_weighed` dans la langue du TEXTE \
PRINCIPAL de l'arrêt (les considérants « En droit » / « Erwägungen » / \
« In diritto »), PAS celle du regeste multilingue ni celle des \
métadonnées éventuellement contradictoires. Si l'arrêt rédige ses \
considérants en allemand, tu réponds en allemand ; s'ils sont en \
français, tu réponds en français ; s'ils sont en italien, tu réponds \
en italien. Les champs avec vocabulaire contrôlé (`procedural_stage`, \
`outcome`, `direction`, `discussed`, `is_leading_case_signal`) \
restent tels quels.

6. **Citations du Recueil Officiel — préfixe canonique "BGE"** : pour \
toute citation du Recueil Officiel du Tribunal fédéral suisse, utilise \
EXCLUSIVEMENT le préfixe "BGE" (jamais "ATF", jamais "DTF") suivi du \
numéro. Exemples valides : "BGE 128 IV 225", "BGE 135 III 329", "BGE 122 \
I 39". Cette règle s'applique dans TOUS les champs (y compris \
`legal_basis`, `cited_decision`, `ratio`, etc.) et INDÉPENDAMMENT de ce \
que l'arrêt source utilise ("ATF 128 IV 225" dans le texte → tu écris \
"BGE 128 IV 225"). Raison : la base de données utilise "BGE" comme clé \
canonique ; toute autre graphie empêche la jointure en aval.

7. **JSON valide** : pas de virgule de fin, pas de commentaires, pas de \
guillemets typographiques. Échappe les guillemets dans les chaînes.
"""

# Slimmer variant used for BGer: no doctrine, no ratio/obiter, keeps the
# outcome + legal basis + citation treatment.
SYSTEM_PROMPT_LIGHT = """Tu es un juriste spécialiste du droit suisse. Tu analyses un arrêt \
du Tribunal fédéral (non publié) et en extrais des métadonnées \
structurées minimales.

Réponds UNIQUEMENT avec un objet JSON strict (pas de markdown, pas de \
préambule). Schéma :

{
  "sort": {
    "procedural_stage": "recours" | "premiere_instance",
    "outcome": "admission" | "admission_partielle" | "rejet" | "irrecevabilite",
    "subject_matter": "phrase courte (≤ 25 mots)"
  },
  "legal_areas": ["CC", "CO", "CPC", "LTF", ...],
  "legal_basis_main": ["art. X CC", "art. Y LTF"],
  "prior_case_treatment": [
    { "cited_decision": "ATF 137 II 313", "direction": "confirms" | "distinguishes" | ... }
  ]
}

RÈGLES :
- JSON valide, vocabulaire contrôlé.
- `legal_areas` : codes usuels (CC, CO, CP, CPC, CPP, LTF, LP, LAI, LACI, etc.).
- `prior_case_treatment` : uniquement les traitements explicites.
- `subject_matter` dans la langue de l'arrêt.
"""


# ---------------------------------------------------------------------------
# User prompt builder
# ---------------------------------------------------------------------------

@dataclass
class DecisionContext:
    decision_id: str
    court: str
    language: str
    date_iso: str
    chamber: str | None
    regeste: str
    full_text: str
    chunk_headers: list[str]          # SAC summaries already produced
    dispositif_text: str | None       # pre-extracted dispositif span if we have it


def build_user_prompt(ctx: DecisionContext, *, light: bool = False,
                      max_body_chars: int = 30_000) -> str:
    """Assemble the user-facing prompt from a DecisionContext.

    For BGE we send the full body (or a truncated version if extraordinarily
    long). For BGer we use the --light variant which only needs dispositif
    + regeste for the outcome classification.
    """
    header_block = "\n".join(
        f"- {h}" for h in ctx.chunk_headers[:40]
    ) or "(aucun summary disponible)"

    body = ctx.full_text
    if len(body) > max_body_chars:
        # Send: first 40% + last 30% so we always have facts opening + dispositif.
        keep_head = int(max_body_chars * 0.6)
        keep_tail = max_body_chars - keep_head
        body = body[:keep_head] + "\n…[TRUNCATED]…\n" + body[-keep_tail:]

    lang_label = {"de": "allemand", "fr": "français", "it": "italien"}.get(ctx.language, "français")

    parts = [
        f"ARRÊT : {ctx.decision_id}",
        f"COUR : {ctx.court}",
        f"DATE : {ctx.date_iso}" if ctx.date_iso else "",
        f"CHAMBRE : {ctx.chamber}" if ctx.chamber else "",
        f"LANGUE : {lang_label}",
        "",
        "REGESTE :",
        ctx.regeste.strip()[:2000] if ctx.regeste else "(non disponible)",
        "",
        "SYNTHÈSE PAR CONSIDÉRANT (summary headers SAC) :",
        header_block,
        "",
    ]
    if ctx.dispositif_text:
        parts += [
            "DISPOSITIF (extrait) :",
            ctx.dispositif_text.strip()[:4000],
            "",
        ]
    parts += [
        "TEXTE INTÉGRAL DE L'ARRÊT :",
        body,
    ]
    return "\n".join(p for p in parts if p is not None)


# ---------------------------------------------------------------------------
# Output schema validation
# ---------------------------------------------------------------------------

OUTCOMES = {"admission", "admission_partielle", "rejet", "irrecevabilite"}
STAGES = {"recours", "premiere_instance"}
DIRECTIONS = {"confirms", "develops", "distinguishes", "overrules", "criticizes", "neutral"}


# ---------------------------------------------------------------------------
# Post-hoc safety net: canonicalise BGE references
# ---------------------------------------------------------------------------

import re as _re

# Match "ATF 128 IV 225", "DTF 128 IV 225", "ATF_128_IV_225" etc.,
# word-boundary anchored so we don't touch unrelated tokens.
_BGE_ALIAS_RE = _re.compile(r"\b(?:ATF|DTF)(\s+|_)(\d+[IVXLCDM]*\s+[IVXLCDM]+\s+\d+)", _re.IGNORECASE)


def _canonicalise_bge_in_string(s: str) -> str:
    """Replace ATF/DTF prefixes with BGE in any legal citation."""
    if not isinstance(s, str):
        return s
    return _BGE_ALIAS_RE.sub(r"BGE\1\2", s)


def canonicalise_bge(obj):
    """Walk a parsed JSON object (dict/list/str) and rewrite any
    ATF/DTF references to the canonical BGE prefix. Safety net in case
    the LLM ignores the instruction."""
    if isinstance(obj, dict):
        return {k: canonicalise_bge(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [canonicalise_bge(v) for v in obj]
    if isinstance(obj, str):
        return _canonicalise_bge_in_string(obj)
    return obj


def validate_full(obj: dict) -> list[str]:
    """Return a list of validation errors (empty list = ok)."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return ["top-level must be object"]

    sort = obj.get("sort")
    if not isinstance(sort, dict):
        errors.append("missing .sort")
    else:
        if sort.get("procedural_stage") not in STAGES:
            errors.append(f"sort.procedural_stage invalid: {sort.get('procedural_stage')!r}")
        if sort.get("outcome") not in OUTCOMES:
            errors.append(f"sort.outcome invalid: {sort.get('outcome')!r}")
        if not isinstance(sort.get("subject_matter"), str):
            errors.append("sort.subject_matter missing")

    pqs = obj.get("principle_questions")
    if not isinstance(pqs, list):
        errors.append(".principle_questions must be list")
    else:
        for i, q in enumerate(pqs):
            if not isinstance(q, dict):
                errors.append(f"principle_questions[{i}] not object")
                continue
            for k in ("question", "ratio"):
                if not isinstance(q.get(k), str):
                    errors.append(f"principle_questions[{i}].{k} missing")
            if not isinstance(q.get("legal_basis", []), list):
                errors.append(f"principle_questions[{i}].legal_basis not list")

    doc = obj.get("doctrine_discussion")
    if isinstance(doc, dict):
        if not isinstance(doc.get("discussed"), bool):
            errors.append("doctrine_discussion.discussed not bool")

    treatments = obj.get("prior_case_treatment", [])
    if isinstance(treatments, list):
        for i, t in enumerate(treatments):
            if not isinstance(t, dict):
                continue
            if t.get("direction") not in DIRECTIONS:
                errors.append(f"prior_case_treatment[{i}].direction invalid: {t.get('direction')!r}")

    return errors
