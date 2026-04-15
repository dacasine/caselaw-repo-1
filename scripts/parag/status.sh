#!/usr/bin/env bash
# One-shot status dashboard for all parallel parag jobs.

cd "$(dirname "$0")/../.."

alive() {
    local pidfile="$1"
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null && echo "●" || echo "○"
}

echo "═══ DB state ═══"
for f in decisions.db parag_chunks.db statutes.db cantonal_laws.db reference_graph.db; do
    p="$HOME/.swiss-caselaw/$f"
    if [ -f "$p" ]; then
        size=$(du -h "$p" | awk '{print $1}')
        echo "  $f : $size"
    else
        echo "  $f : (absent)"
    fi
done

echo ""
echo "═══ Running jobs ═══"
printf "  %s SAC (LLM)       pid=%s\n" "$(alive logs/run_sac_atf.pid)" "$(cat logs/run_sac_atf.pid 2>/dev/null)"
printf "  %s Embedder         pid=%s\n" "$(alive logs/run_embed.pid)" "$(cat logs/run_embed.pid 2>/dev/null)"
printf "  %s Fedlex           pid=%s\n" "$(alive logs/fedlex.pid)" "$(cat logs/fedlex.pid 2>/dev/null)"
printf "  %s Cantonal direct  pid=%s\n" "$(alive logs/cantonal_direct.pid)" "$(cat logs/cantonal_direct.pid 2>/dev/null)"
printf "  %s Cantonal LexFind pid=%s\n" "$(alive logs/cantonal_lexfind.pid)" "$(cat logs/cantonal_lexfind.pid 2>/dev/null)"

echo ""
echo "═══ SAC progress ═══"
sqlite3 "$HOME/.swiss-caselaw/parag_chunks.db" \
  "SELECT '  ' || status || ' : ' || COUNT(*) FROM enrichment_state GROUP BY status;" 2>/dev/null

echo ""
echo "═══ Embedder progress ═══"
total=$(sqlite3 "$HOME/.swiss-caselaw/parag_chunks.db" "SELECT COUNT(*) FROM chunks WHERE summary_source != 'stub' AND length(cleaned) > 50;" 2>/dev/null)
done=$(sqlite3 "$HOME/.swiss-caselaw/parag_chunks.db" "SELECT COUNT(*) FROM chunk_embeddings_meta;" 2>/dev/null)
echo "  vectors: $done / $total"

echo ""
echo "═══ Fedlex progress ═══"
xmls=$(find output/fedlex/xml -name "*.xml" 2>/dev/null | wc -l | tr -d ' ')
echo "  XMLs: $xmls / ~27000"
tail -1 logs/fedlex_download_*.log 2>/dev/null | sed 's/^/  /'

echo ""
echo "═══ Cantonal direct progress ═══"
direct_files=$(ls output/cantonal_laws_direct/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo "  JSONL cantons: $direct_files"
tail -1 logs/cantonal_direct_*.log 2>/dev/null | sed 's/^/  /'

echo ""
echo "═══ Cantonal LexFind progress ═══"
lex_files=$(ls output/lexfind_cantonal/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo "  JSONL cantons: $lex_files / 26"
tail -1 logs/cantonal_lexfind_*.log 2>/dev/null | sed 's/^/  /'

echo ""
echo "═══ Quota synthetic.new ═══"
if [ -n "$SYNTHETIC_API_KEY" ] || [ -f .env ]; then
    [ -f .env ] && source .env
    curl -sS https://api.synthetic.new/v2/quotas -H "Authorization: Bearer $SYNTHETIC_API_KEY" 2>/dev/null \
      | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    r = d.get('rollingFiveHourLimit', {})
    w = d.get('weeklyTokenLimit', {})
    print(f'  5h : {r.get(\"remaining\", 0):.0f}/{r.get(\"max\", 0)} ({r.get(\"remaining\", 0)/max(r.get(\"max\", 1),1)*100:.1f}%)  limited={r.get(\"limited\")}')
    print(f'  week: {w.get(\"percentRemaining\", 0):.1f}% credits remaining ({w.get(\"remainingCredits\", \"?\")})')
except Exception as e:
    print(f'  (quota check failed: {e})')
"
fi
