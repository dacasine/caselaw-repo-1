"""Generate doctrine fiches in batch using LLM (OpenRouter/Gemini).

Reads the taxonomy from doctrine/_taxonomy.yaml, generates each fiche
using the bail_habitation.md as few-shot example, and writes the .md files.

Usage:
    PYTHONPATH=/srv/caselaw .venv/bin/python scripts/doctrine/generate_fiches.py [--limit N] [--skip-existing]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from search_stack.parag.openrouter_client import OpenRouterClient

DOCTRINE_DIR = Path(__file__).resolve().parents[2] / "doctrine"
TAXONOMY_PATH = DOCTRINE_DIR / "_taxonomy.yaml"
EXAMPLE_PATH = DOCTRINE_DIR / "1_droit_prive" / "1.5_obligations" / "1.5.2_contrats_speciaux" / "1.5.2.3_bail_habitation.md"

SYSTEM_PROMPT = """Tu es un professeur de droit suisse spécialisé dans la rédaction de fiches pédagogiques pour un système RAG juridique.

Tu dois produire une fiche doctrinale structurée en français, avec le format EXACT suivant :
- Frontmatter YAML (entre ---) avec les champs : id, title_fr, title_de, title_it, articles, sr_numbers, parent, keywords_fr, keywords_de, keywords_it
- Sections obligatoires :
  ## Cadre général (200-400 mots)
  ## Distinctions essentielles (3-6 points avec **gras**)
  ## Articles centraux et leur articulation (tableau Markdown)
  ## Évolution du droit (timeline avec années, dernières réformes, jurisprudence marquante)
  ## Pièges courants (5-8 points numérotés)
  ## Arrêts de référence (5-8 BGE/ATF avec numéro exact et objet)
  ## Répertoire trilingue (tableau FR/DE/IT, 10-15 termes)

IMPORTANT :
- Le texte de base est en français
- Les BGE cités doivent être réels et vérifiables
- La section "Évolution du droit" doit mentionner les changements récents (2020-2025)
- Les keywords doivent inclure les termes juridiques précis dans les 3 langues
- Le parent dans le frontmatter est l'ID du niveau supérieur (ex: "1.5.2" pour "1.5.2.3")
- Total du body : 800-1500 mots
"""


def load_example() -> str:
    return EXAMPLE_PATH.read_text(encoding="utf-8")


def build_prompt(entry: dict, example: str) -> str:
    return f"""Voici un EXEMPLE de fiche doctrinale (format à reproduire exactement) :

```markdown
{example}
```

Maintenant, génère une fiche doctrinale pour :
- ID : {entry['id']}
- Titre FR : {entry['title_fr']}
- Articles : {entry.get('articles', [])}
- SR Numbers : {entry.get('sr_numbers', [])}
- Parent : {entry['id'].rsplit('.', 1)[0] if '.' in entry['id'] else ''}

Produis la fiche complète avec frontmatter YAML + toutes les sections. Commence directement par --- (le frontmatter)."""


def generate_fiche(client: OpenRouterClient, entry: dict, example: str) -> str:
    prompt = build_prompt(entry, example)
    resp = client.chat(system=SYSTEM_PROMPT, user=prompt, max_tokens=4000, temperature=0.3)
    content = resp.content.strip()
    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.endswith("```"):
            content = content[:-3].strip()
    return content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--rate", type=int, default=10, help="Requests per minute")
    ap.add_argument("--model", default="google/gemini-2.0-flash-001")
    args = ap.parse_args()

    # Load taxonomy
    with open(TAXONOMY_PATH) as f:
        taxonomy = yaml.safe_load(f)
    entries = taxonomy.get("batch_100", [])
    print(f"Taxonomy loaded: {len(entries)} entries")

    # Load example
    example = load_example()

    # Init client
    client = OpenRouterClient(
        model=args.model,
        fallback_model=args.model,
        rate_limit_per_minute=args.rate,
    )

    done = 0
    errors = 0
    for entry in entries:
        if args.limit and done >= args.limit:
            break

        fiche_id = entry["id"]
        path_parts = entry["path"].split("/")
        # Build filename from id
        filename = f"{fiche_id.replace('.', '_')}_{entry['title_fr'].split('(')[0].strip().lower().replace(' ', '_').replace('—', '').replace('–', '')[:40]}.md"
        output_dir = DOCTRINE_DIR / "/".join(path_parts)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename

        if args.skip_existing and output_path.exists():
            print(f"  SKIP {fiche_id} (exists)")
            continue

        # Also check if any file with this id exists in the directory
        existing = list(output_dir.glob(f"{fiche_id.replace('.', '_')}_*.md"))
        if args.skip_existing and existing:
            print(f"  SKIP {fiche_id} (found {existing[0].name})")
            continue

        print(f"  GEN {fiche_id}: {entry['title_fr']}...", end=" ", flush=True)
        try:
            content = generate_fiche(client, entry, example)
            output_path.write_text(content, encoding="utf-8")
            done += 1
            print(f"OK ({len(content)} chars)")
        except Exception as e:
            errors += 1
            print(f"ERROR: {e}")

        # Rate limit
        time.sleep(60 / args.rate)

    print(f"\nDone: {done} generated, {errors} errors")


if __name__ == "__main__":
    main()
