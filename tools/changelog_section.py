"""Print one version's section of CHANGELOG.md (used for the GitHub release notes).

    python tools/changelog_section.py v1.1.2 > notes.md
"""
import re
import sys
from pathlib import Path


def section(text: str, version: str) -> str | None:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)          # skip the template comment
    want = version.lstrip("vV")
    parts = re.split(r"^## +", text, flags=re.M)[1:]
    for part in parts:
        head, _, body = part.partition("\n")
        m = re.match(r"v?(\d+(?:\.\d+)*)", head.strip())
        if m and m[1] == want:
            return body.strip()
    return None


if __name__ == "__main__":
    log = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    found = section(log.read_text(encoding="utf-8"), sys.argv[1])
    if found is None:
        print(f"No '## {sys.argv[1]}' section in CHANGELOG.md", file=sys.stderr)
        print("See CHANGELOG.md for what changed.")
    else:
        print(found)
