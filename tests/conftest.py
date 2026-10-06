from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in its own directory, so traces and workspaces never land in the repo."""
    monkeypatch.chdir(tmp_path)
