import re
import tomllib
from pathlib import Path

import agentkit

ROOT = Path(__file__).parents[2]


def test_the_changelog_names_the_current_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    first_release = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert first_release is not None, "CHANGELOG.md needs a '## <version>' heading"
    assert first_release.group(1) == agentkit.__version__ == pyproject["project"]["version"]


def test_every_migration_note_says_who_is_affected_and_what_to_change() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    notes = re.findall(r"^### Migration: .*?(?=^### |^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    assert len(notes) >= 4
    for note in notes:
        assert "Affects:" in note and "Before:" in note and "After:" in note, note.splitlines()[0]
