"""Writes README.pypi.md: README.md with every relative link made absolute.

GitHub resolves relative links in README.md; PyPI cannot, so the package's long description
uses this copy. Run it before building a release:

    python -m tools.pypi_readme
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOB = "https://github.com/xerodahdev/zecpy/blob/main/"
RAW = "https://raw.githubusercontent.com/xerodahdev/zecpy/main/"
ABSOLUTE = re.compile(r"^(https?:|mailto:|#)")


def absolute(target: str, raw: bool) -> str:
    return target if ABSOLUTE.match(target) else (RAW if raw else BLOB) + target


def convert(text: str) -> str:
    # Markdown images and links, then HTML src and href attributes.
    text = re.sub(r"(!\[[^\]]*\]\()([^)\s]+)", lambda m: m.group(1) + absolute(m.group(2), True), text)
    text = re.sub(r"((?<!!)\[[^\]]*\]\()([^)\s]+)", lambda m: m.group(1) + absolute(m.group(2), False), text)
    text = re.sub(r'(src=")([^"]+)', lambda m: m.group(1) + absolute(m.group(2), True), text)
    text = re.sub(r'(href=")([^"]+)', lambda m: m.group(1) + absolute(m.group(2), False), text)
    return text


def main() -> None:
    out = ROOT / "README.pypi.md"
    out.write_text(
        "<!-- Generated from README.md by tools/pypi_readme.py for PyPI. Edit README.md. -->\n"
        + convert((ROOT / "README.md").read_text())
    )
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
