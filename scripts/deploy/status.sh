#!/bin/bash
# Usage: bash /srv/caselaw/status.sh
cd /srv/caselaw
.venv/bin/python << 'PY'
import sqlite3, os
c = sqlite3.connect("/srv/data/parag_chunks.db", timeout=10)

vecs = c.execute("SELECT COUNT(*) FROM chunk_embeddings_meta").fetchone()[0]
embeddable = c.execute("SELECT COUNT(*) FROM chunks WHERE summary_source != 'stub' AND length(cleaned) > 50").fetchone()[0]
enriched = c.execute("SELECT COUNT(*) FROM decision_enrichment WHERE status='ok'").fetchone()[0]
errors = c.execute("SELECT COUNT(*) FROM decision_enrichment WHERE status!='ok'").fetchone()[0]
courts_sac = c.execute("SELECT COUNT(DISTINCT court) FROM chunks").fetchone()[0]
courts_enr = c.execute("SELECT COUNT(DISTINCT court) FROM decision_enrichment WHERE status='ok'").fetchone()[0]
cost = c.execute("SELECT ROUND(SUM(prompt_tokens)*0.10/1e6 + SUM(completion_tokens)*0.40/1e6, 2) FROM decision_enrichment WHERE status='ok'").fetchone()[0]
law_cit = c.execute("SELECT COUNT(*) FROM chunk_law_citations").fetchone()[0]
case_cit = c.execute("SELECT COUNT(*) FROM chunk_case_citations").fetchone()[0]

print("═══ DB STATE ═══")
print(f"  chunks:      {c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]:>12,}")
print(f"  vectors:     {vecs:>12,} / {embeddable:,}  ({100*vecs/max(embeddable,1):.1f}%)")
print(f"  enriched ok: {enriched:>12,}")
print(f"  errors:      {errors:>12,}")
print(f"  courts SAC:  {courts_sac:>12}")
print(f"  courts enr:  {courts_enr:>12}")
print(f"  law cit:     {law_cit:>12,}")
print(f"  case cit:    {case_cit:>12,}")
print(f"  cost:           ${cost}")
print()

print("═══ PER COURT ═══")
print(f"  {'court':<40} {'SAC':>8} {'enriched':>10}")
rows = c.execute("""
    SELECT c.court, COUNT(DISTINCT c.decision_id) AS sac,
           COALESCE((SELECT COUNT(*) FROM decision_enrichment de
                     WHERE de.court=c.court AND de.status='ok'),0) AS enriched
    FROM chunks c GROUP BY c.court ORDER BY sac DESC LIMIT 25
""").fetchall()
for row in rows:
    ct, sac, enr = row[0], row[1], row[2]
    pct = int(40 * enr / max(sac, 1))
    bar = chr(9608) * pct + chr(9617) * (40 - pct)
    print(f"  {ct:<40} {sac:>8,} {enr:>10,}  {bar}")
print()
c.close()
PY

echo "═══ PROCESSES ═══"
ps aux | grep -E "python.*run_|python.*embed|python.*encode" | grep -v grep | \
    awk '{printf "  %-6s %6s%% CPU  %5s%% MEM  %s\n", $2, $3, $4, substr($0, index($0,$11),70)}'
echo ""
echo "═══ SAC+PHASE5 LOG ═══"
tail -3 /srv/data/sac_phase5_ovh_v2.log 2>/dev/null || echo "  (no log)"
echo ""
echo "═══ DISK ═══"
df -h /srv/data | tail -1 | awk '{print "  " $3 " used / " $2 " total (" $5 " full)"}'
