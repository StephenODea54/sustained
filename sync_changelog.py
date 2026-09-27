#!/usr/bin/env python3
"""Render CHANGELOG.md as the docs site's changelog page.

The repository's CHANGELOG.md is the source of truth. It has one `## <version>`
section per release, each with `### Added`, `### Removed`, `### Changed`, and
`### Fixed` subsections. This script writes it to docs/changelog.md with the
Jekyll front matter and preamble the site needs, and groups the releases by
major version: `## Unreleased` stays a level 2 heading, each major version gets
a `## <major>.x` heading, each version becomes a level 3 heading, and each
subsection a level 4 heading. The site's "On this page" list links the level 2
headings, so it has one entry per major version instead of one per release.
The heading text is unchanged, so the anchor of each version stays the same.

Run it after editing the changelog; the pre-commit hook does this
automatically and fails the commit when the page is stale.
"""

import re
import sys
from pathlib import Path

PREAMBLE = """---
layout: default
title: Changelog
description: "Release notes for every version of Sustained, the Python ORM, query builder, and schema migration tool."
---

Every released version of Sustained, newest first, grouped by major version. The same text lives in `CHANGELOG.md` in the repository; this page is generated from it.

Version numbers follow semantic versioning. A major version marks a change that can break working code. A minor version adds new features. A patch version fixes a defect without changing public API signatures or introducing new functionality.
"""


VERSION_HEADING = re.compile(r"## (\d+)\.")


def group_by_major(lines: list[str]) -> list[str]:
    """Put each release under a `## <major>.x` heading, one level down."""
    grouped: list[str] = []
    major = None
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            grouped.append(line)
            continue
        version = VERSION_HEADING.match(line)
        if version:
            if version.group(1) != major:
                major = version.group(1)
                grouped.extend([f"## {major}.x", ""])
            grouped.append("#" + line)
        elif line.startswith("### ") and major is not None:
            grouped.append("#" + line)
        else:
            grouped.append(line)
    return grouped


def render(changelog_text: str) -> str:
    """Strip the changelog's own H1, group the releases by major version, and
    wrap the rest in front matter."""
    lines = changelog_text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(group_by_major(lines)).strip()
    return f"{PREAMBLE}\n{body}\n"


def main() -> int:
    project_root = Path(__file__).resolve().parent
    source = project_root / "CHANGELOG.md"
    target = project_root / "docs" / "changelog.md"

    if not source.exists():
        print(f"Error: {source} not found.", file=sys.stderr)
        return 1

    rendered = render(source.read_text())
    check_only = "--check" in sys.argv

    if target.exists() and target.read_text() == rendered:
        return 0

    if check_only:
        print(
            "docs/changelog.md is out of date. Run: python3 sync_changelog.py",
            file=sys.stderr,
        )
        return 1

    target.write_text(rendered)
    print(f"Wrote {target.relative_to(project_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
