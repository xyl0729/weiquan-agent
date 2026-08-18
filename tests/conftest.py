from __future__ import annotations

from pathlib import Path

import pytest

@pytest.fixture
def project_attachment_temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "attachment-jobs"
