"""Split the travel guide into one Markdown file per city, the input of rag_setup.py.

Why: the free search tier extracts at most 262,144 characters from one document, and the
generated skillset cuts chunks every 2,000 characters without looking at headings. One file
per city stays far below the limit and a chunk never mixes two cities. Every section heading
gets the city name ("### Kraków: Getting Around"), so each chunk names its city.

Run it after every change to the full guide, then run rag_setup.py:
    python utils/split_travel_guide.py [--source <guide.md>] [--output <folder>]
"""

import argparse
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "inputs" / "world_city_travel_guide.md"
DEFAULT_OUTPUT = PROJECT_ROOT / "inputs" / "travel_guide"

CITY_HEADING = re.compile(r"(?m)^(?=## )")  # "## Kraków, Poland" starts a city
SECTION_HEADING = re.compile(r"(?m)^### (.+)$")  # "### Getting Around"


def slugify(text: str) -> str:
    """File name from a city heading: 'Kraków, Poland' -> 'krakow-poland'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def split_guide(text: str) -> dict[str, str]:
    """Return {file name: Markdown} with one entry per city. The guide title is dropped."""
    files = {}
    for block in CITY_HEADING.split(text)[1:]:
        heading = block.splitlines()[0][3:].strip()
        city = heading.split(",")[0].strip()
        body = SECTION_HEADING.sub(lambda m: f"### {city}: {m.group(1)}", block.rstrip() + "\n")
        name = f"{slugify(heading)}.md"
        if name in files:
            raise ValueError(f"Two cities give the same file name: {name}")
        files[name] = body
    return files


def same_content(original: str, files: dict[str, str]) -> bool:
    """True if the files hold the whole guide after the title (city prefixes and whitespace ignored)."""
    joined = "".join(re.sub(r"(?m)^### [^:\n]+: ", "### ", body) for body in files.values())
    cities = "".join(CITY_HEADING.split(original)[1:])
    return re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", cities)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="full travel guide (Markdown)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="folder for the city files")
    args = parser.parse_args()

    text = args.source.read_text(encoding="utf-8")
    try:
        files = split_guide(text)
    except ValueError as e:
        print(f"❌ {e}")
        return 1
    if not files:
        print(f"❌ No '## <city>' headings in {args.source}")
        return 1
    if not same_content(text, files):
        print("❌ The city files do not add up to the guide; nothing was written")
        return 1

    sections = Counter(len(SECTION_HEADING.findall(body)) for body in files.values())
    if len(sections) > 1:
        print(f"⚠️ Cities have different numbers of sections: {dict(sections)}")

    # Replace the folder content, so cities removed from the guide disappear (rag_setup.py then deletes their blobs)
    args.output.mkdir(parents=True, exist_ok=True)
    removed = [old.name for old in args.output.glob("*.md") if old.name not in files]
    for old in args.output.glob("*.md"):
        old.unlink()
    for name, body in files.items():
        # UTF-8 with BOM: without it the indexer guesses the encoding and read some short files as GBK
        (args.output / name).write_text(body, encoding="utf-8-sig", newline="\n")

    sizes = [len(body) for body in files.values()]
    print(f"✅ {len(files)} city files in {args.output} ({min(sizes)}–{max(sizes)} characters)")
    if removed:
        print(f"   Removed files of cities no longer in the guide: {removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
