#!/usr/bin/env python3
"""Check the documentation the way CI checks the code.

Three things, all offline:

1. Every relative Markdown link resolves — to a file, and to a heading when the
   link carries a `#fragment`.
2. Every ```mermaid block is closed and obeys the rules in
   docs/diagrams/README.md that a parser cannot catch on its own (a block can be
   syntactically valid and still render as an unreadable mess).
3. Every block is written out for rendering, so CI can hand them to
   mermaid-cli. A Mermaid syntax error is invisible until it ships as a red
   error box on the repository's front page.

    uv run python scripts/check_docs.py [--emit DIR]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache"}

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)
FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`]*)", re.MULTILINE)

# Node/edge label bodies: A[...], A{...}, A(...), and |...| edge labels.
BRACKET_LABEL = re.compile(r"[\[{]([^\[\]{}|]*)[\]}]")
EDGE_LABEL = re.compile(r"\|([^|]*)\|")
# `X --> Y` / `X -.-> Y` / `X ==> Y`, with an optional |label| in the middle.
# Run this over a line with its labels stripped, so shapes cannot hide an edge.
EDGE = re.compile(
    r"([A-Za-z0-9_-]+)\s*(?:-{2,}|-\.-*|={2,})[->]*\s*(?:\|[^|]*\|\s*)?([A-Za-z0-9_-]+)"
)
QUOTED = re.compile(r"\"[^\"]*\"")
SHAPE = re.compile(r"[\[({][^\[\]{}()]*[\])}]")
SUBGRAPH = re.compile(r"^\s*subgraph\s+([A-Za-z0-9_-]+)", re.MULTILINE)
# Punctuation that ends a label early unless the label is quoted.
NEEDS_QUOTING = re.compile(r"[(){}\[\]:/#]")


class Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: pathlib.Path, line: int, message: str) -> None:
        self.items.append(f"{path.relative_to(REPO)}:{line}: {message}")


def markdown_files() -> list[pathlib.Path]:
    return sorted(
        p for p in REPO.rglob("*.md") if not SKIP_DIRS.intersection(p.relative_to(REPO).parts)
    )


def slug(heading: str) -> str:
    """GitHub's heading-anchor slug, near enough for our own documents."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)  # code spans keep their text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = re.sub(r"[*_~]", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def anchors(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for level, text in HEADING.findall(path.read_text(encoding="utf-8")):
        del level
        base = slug(text)
        if not base:
            continue
        candidate, n = base, 0
        while candidate in found:
            n += 1
            candidate = f"{base}-{n}"
        found.add(candidate)
    return found


def check_links(problems: Problems) -> None:
    anchor_cache: dict[pathlib.Path, set[str]] = {}

    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            line = text.count("\n", 0, match.start()) + 1
            file_part, _, fragment = target.partition("#")

            resolved = path if not file_part else (path.parent / file_part).resolve()
            if not resolved.exists():
                problems.add(path, line, f"broken link: {target}")
                continue
            if not fragment or resolved.suffix != ".md":
                continue
            if resolved not in anchor_cache:
                anchor_cache[resolved] = anchors(resolved)
            if fragment.lower() not in anchor_cache[resolved]:
                problems.add(path, line, f"link to a missing heading: {target}")


def mermaid_blocks(path: pathlib.Path, problems: Problems) -> list[tuple[int, str]]:
    """Every ```mermaid block in `path`, as (first content line, source)."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocks: list[tuple[int, str]] = []
    open_fence: str | None = None
    start = 0

    for i, line in enumerate(lines):
        match = FENCE.match(line)
        if not match:
            continue
        _, ticks, info = match.groups()
        if open_fence is None:
            open_fence = ticks
            start = i
            continue
        # A closing fence is at least as long as the one that opened the block
        # and carries no info string; anything else is a nested example fence.
        if not info and ticks[0] == open_fence[0] and len(ticks) >= len(open_fence):
            if lines[start].strip().removeprefix(open_fence).strip() == "mermaid":
                blocks.append((start + 2, "\n".join(lines[start + 1 : i])))
            open_fence = None

    if open_fence is not None:
        problems.add(path, start + 1, "unterminated code fence")
    return blocks


def without_labels(code: str) -> str:
    """`A["foo (bar)"] --> B[baz]` becomes `A --> B`, so edges are findable."""
    code = QUOTED.sub("", code)
    while True:
        stripped = SHAPE.sub("", code)
        if stripped == code:
            return code
        code = stripped


def check_mermaid(path: pathlib.Path, first_line: int, source: str, problems: Problems) -> None:
    subgraphs = set(SUBGRAPH.findall(source))

    for offset, line in enumerate(source.split("\n")):
        line_no = first_line + offset
        code = line.split("%%", 1)[0]
        if not code.strip():
            continue

        for label in BRACKET_LABEL.findall(code) + EDGE_LABEL.findall(code):
            stripped = label.strip()
            if not stripped:
                continue
            quoted = stripped.startswith('"') and stripped.endswith('"')
            if "`" in stripped:
                problems.add(path, line_no, "backticks in a Mermaid label render literally")
            if not quoted and NEEDS_QUOTING.search(stripped):
                problems.add(
                    path,
                    line_no,
                    f'unquoted label with punctuation: {stripped!r} — write "…"',
                )

        for source_id, target_id in EDGE.findall(without_labels(code)):
            for node in (source_id, target_id):
                if node in subgraphs:
                    problems.add(
                        path,
                        line_no,
                        f"edge attached to the subgraph {node!r} rather than a node inside it",
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emit",
        type=pathlib.Path,
        help="write every Mermaid block here as a .mmd file, for rendering",
    )
    args = parser.parse_args()

    problems = Problems()
    check_links(problems)

    if args.emit:
        args.emit.mkdir(parents=True, exist_ok=True)

    count = 0
    for path in markdown_files():
        for index, (first_line, source) in enumerate(mermaid_blocks(path, problems), 1):
            count += 1
            check_mermaid(path, first_line, source, problems)
            if args.emit:
                name = str(path.relative_to(REPO)).replace("/", "-").removesuffix(".md")
                (args.emit / f"{name}-{index}.mmd").write_text(source + "\n")

    for item in problems.items:
        print(item, file=sys.stderr)

    if problems.items:
        print(f"\n{len(problems.items)} problem(s)", file=sys.stderr)
        return 1

    print(f"docs ok — {len(markdown_files())} files, {count} Mermaid blocks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
