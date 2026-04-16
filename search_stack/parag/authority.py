"""Authority scoring — PA-RAG pilier 1 (court hierarchy) + pilier 2 bonus.

Produces a per-decision authority score from:
  - court_level:      static position in the judicial hierarchy (1–5)
  - atf_published:    bonus for decisions published in the Recueil Officiel
  - (later) pagerank_temporal (pilier 2) and validity_status (pilier 3)
    are combined into the final retrieval-time composite score.

The static authority_score stored here is a cheap pre-compute the
retrieval layer uses as one of the ranking signals. Higher = more
authoritative.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Court hierarchy mapping
# ---------------------------------------------------------------------------

# Explicit overrides take priority; the fallback uses substring patterns.
_COURT_LEVEL_EXACT = {
    # Federal supreme court (TF)
    "bge": 5,
    "bger": 5,
    # Federal specialised supreme courts
    "bvger": 4,         # TAF (administratif)
    "bstger": 4,        # TPF (pénal)
    "bpatger": 4,       # TFB (brevets)
    # Federal human rights
    "bge_egmr": 4,
}

# Pattern → level mapping for cantonal courts. Higher specificity first.
_COURT_LEVEL_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # Supreme cantonal instances (Tribunal cantonal, Obergericht, Cour de justice)
    (re.compile(r"(?:^|_)(obergericht|kantonsgericht|appellationsgericht|cour.*justice|tribunal.cantonal)(?:_|$)", re.I), 3),
    (re.compile(r"(?:^|_)(verwaltungsgericht|sozialversicherungsgericht|handelsgericht|versicherungsgericht|steuerrekurs|baurekurs|kassationsgericht)(?:_|$)", re.I), 3),
    # Tribunal de première instance (Bezirksgericht, tribunaux de district, justice de paix)
    (re.compile(r"(?:^|_)(bezirksgericht|kreisgericht|tribunal.(?:district|arrondissement)|justice.paix)(?:_|$)", re.I), 2),
    (re.compile(r"(?:^|_)(strafgericht|zivilgericht|arbeitsgericht|mietgericht)(?:_|$)", re.I), 2),
    # Specialised administrative authorities, commissions
    (re.compile(r"(?:^|_)(anwaltskommission|aufsichtskommission|anwaltsaufsicht)(?:_|$)", re.I), 2),
    (re.compile(r"(?:^|_)(regierungsrat|departement|steuerverwaltung|ministere.public)(?:_|$)", re.I), 1),
    # Generic "gerichte" folder without other hints → assume "tribunaux" aggregated (mix of levels)
    (re.compile(r"(?:^|_)gerichte$", re.I), 3),
    (re.compile(r"(?:^|_)findinfo$", re.I), 3),
    (re.compile(r"(?:^|_)omni$", re.I), 3),
]

# Federal admin authorities, not courts per se — low level.
_ADMIN_SCOPE_PREFIXES = ("ch_vb", "finma_", "edoeb", "hudoc_ch", "ubi")


def map_court_level(court: str) -> int:
    """Return authority level for a court code. Default 2 (uncategorised)."""
    if not court:
        return 2
    if court in _COURT_LEVEL_EXACT:
        return _COURT_LEVEL_EXACT[court]
    for prefix in _ADMIN_SCOPE_PREFIXES:
        if court.startswith(prefix) or court == prefix.rstrip("_"):
            return 1
    for pat, level in _COURT_LEVEL_PATTERNS:
        if pat.search(court):
            return level
    return 2  # fallback


# ---------------------------------------------------------------------------
# ATF publication detection
# ---------------------------------------------------------------------------

# ATF/BGE/DTF decision ids in our DB look like: "bge_BGE_145_III_345" or
# "bge_10_I_1" (pre-modern). Both mean the decision is in the Recueil
# Officiel. `bge_egmr` is a separate series.
_ATF_ID_RE = re.compile(r"^bge_(BGE_\d+|\d+)_[IVX]+_\d+$", re.IGNORECASE)


def is_atf_published(decision_id: str) -> bool:
    return bool(_ATF_ID_RE.match(decision_id))


# ---------------------------------------------------------------------------
# Composite static score
# ---------------------------------------------------------------------------

def static_authority_score(court_level: int, atf_published: bool) -> float:
    """Simple static score in [0, 1]. To be combined at retrieval time
    with PageRank and validity signals.

    court_level=5 (TF)     → 0.90 base
    court_level=4 (TAF…)   → 0.70
    court_level=3 (cant.)  → 0.50
    court_level=2 (1re i.) → 0.30
    court_level=1 (admin)  → 0.15
    + 0.10 bonus if atf_published (caps at 1.0)
    """
    base = {5: 0.90, 4: 0.70, 3: 0.50, 2: 0.30, 1: 0.15}.get(court_level, 0.30)
    if atf_published:
        base = min(1.0, base + 0.10)
    return base


# ---------------------------------------------------------------------------
# DB runner
# ---------------------------------------------------------------------------

def populate_authority(conn: sqlite3.Connection, *, courts_filter: str | None = None) -> dict:
    """Fill decision_authority (court_level, atf_published, authority_score)
    for every decision present in decisions_ref (the source DB). Idempotent.

    If `courts_filter` is given, restrict to those courts only — useful for
    incremental updates after new scopes are enriched.
    """
    src_db = Path.home() / ".swiss-caselaw" / "decisions.db"
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    where = ""
    params: tuple = ()
    if courts_filter:
        where = "WHERE court IN (" + ",".join(["?"] * len(courts_filter.split(","))) + ")"
        params = tuple(c.strip() for c in courts_filter.split(","))

    rows = src.execute(
        f"SELECT decision_id, court FROM decisions {where}", params
    ).fetchall()
    src.close()

    cur = conn.cursor()
    n_written = 0
    for r in rows:
        did = r["decision_id"]
        court = r["court"]
        level = map_court_level(court)
        is_atf = is_atf_published(did)
        score = static_authority_score(level, is_atf)
        cur.execute(
            """
            INSERT INTO decision_authority
                (decision_id, court_level, atf_published, authority_score, computed_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(decision_id) DO UPDATE SET
                court_level     = excluded.court_level,
                atf_published   = excluded.atf_published,
                authority_score = excluded.authority_score,
                computed_at     = datetime('now')
            """,
            (did, level, int(is_atf), score),
        )
        n_written += 1
    conn.commit()
    return {"decisions_scored": n_written}
