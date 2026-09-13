"""Exercise post-commit metadata without touching the operator's repository."""

import importlib.util
from pathlib import Path
import subprocess


def test_stamp_matches_head_and_refuses_dirty_server(tmp_path):
    source = Path(__file__).resolve().parents[2] / "tools/stamp_server_commit.py"
    spec = importlib.util.spec_from_file_location("stamp_server_commit", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "repo"
    (root / "server/app").mkdir(parents=True)
    (root / ".gitignore").write_text("server/app/build_commit.txt\n")
    code = root / "server/app/example.py"
    code.write_text("value = 1\n")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    git("init")
    git("config", "core.hooksPath", str(root / "no-hooks"))
    git("add", ".gitignore", "server/app/example.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture")
    assert module.stamp(root) == git("rev-parse", "HEAD")
    assert not git("status", "--porcelain")
    code.write_text("value = 2\n")
    assert module.stamp(root) == "unknown"
    assert (root / "server/app/build_commit.txt").read_text().strip() == "unknown"
