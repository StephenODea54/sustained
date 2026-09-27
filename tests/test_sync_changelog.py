import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sync_changelog  # noqa: E402

CHANGELOG = """# Changelog

## Unreleased

### Added

- A feature.

## 2.1.0

### Added

- Another feature.

### Fixed

- A fix.

## 2.0.0

### Changed

- **Breaking:** A change.

## 1.0.0

First release.

### Added

- The first feature.
"""


def body(rendered: str) -> str:
    return rendered[len(sync_changelog.PREAMBLE) :].strip()


class RenderTests(unittest.TestCase):
    def test_releases_are_grouped_by_major_version(self) -> None:
        self.assertEqual(
            body(sync_changelog.render(CHANGELOG)),
            """## Unreleased

### Added

- A feature.

## 2.x

### 2.1.0

#### Added

- Another feature.

#### Fixed

- A fix.

### 2.0.0

#### Changed

- **Breaking:** A change.

## 1.x

### 1.0.0

First release.

#### Added

- The first feature.""",
        )

    def test_the_page_starts_with_the_preamble(self) -> None:
        rendered = sync_changelog.render(CHANGELOG)
        self.assertTrue(rendered.startswith(sync_changelog.PREAMBLE))
        self.assertNotIn("# Changelog", rendered)


if __name__ == "__main__":
    unittest.main()
