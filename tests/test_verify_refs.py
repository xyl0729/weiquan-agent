from pathlib import Path

from app.retrieval.database import rebuild_database
from app.retrieval.schema import load_seed_bundle
from scripts.verify_refs import verify_refs
from tests.test_ingest import VALID_SEED


def prepare_database(tmp_path: Path) -> Path:
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(VALID_SEED, encoding="utf-8")
    database_path = tmp_path / "statutes.db"
    rebuild_database(load_seed_bundle(seed_path), database_path)
    return database_path


def test_verify_refs_accepts_exact_match(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)
    playbook_dir = tmp_path / "playbooks"
    playbook_dir.mkdir()
    (playbook_dir / "test.yaml").write_text(
        "id: test\nlegal_basis:\n  - ref: 测试法.第一条\n",
        encoding="utf-8",
    )

    assert verify_refs(database_path, playbook_dir) == []


def test_verify_refs_reports_missing_reference(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)
    playbook_dir = tmp_path / "playbooks"
    playbook_dir.mkdir()
    (playbook_dir / "test.yaml").write_text(
        "id: test\nlegal_basis:\n  - ref: 测试法.第九条\n",
        encoding="utf-8",
    )

    errors = verify_refs(database_path, playbook_dir)

    assert len(errors) == 1
    assert "引用未命中 测试法.第九条" in errors[0]


def test_verify_refs_rejects_empty_directory(tmp_path: Path) -> None:
    database_path = prepare_database(tmp_path)
    playbook_dir = tmp_path / "playbooks"
    playbook_dir.mkdir()

    errors = verify_refs(database_path, playbook_dir)

    assert errors == [f"未找到 playbook: {playbook_dir}"]

