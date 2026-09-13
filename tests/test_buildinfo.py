"""Health reports image provenance, including an honest unversioned fallback."""

import httpx
import pytest
from app import buildinfo


@pytest.mark.parametrize("content", [None, "", "abc123", "not-a-commit"])
def test_missing_or_invalid_revision_is_unknown(tmp_path, monkeypatch, content):
    path = tmp_path / "build_commit.txt"
    if content is not None:
        path.write_text(content, encoding="ascii")
    monkeypatch.setattr(buildinfo, "COMMIT_FILE", path)
    assert buildinfo.build_commit() == "unknown"


def test_health_returns_baked_revision_despite_runtime_override(server, tmp_path, monkeypatch):
    revision = "1234567890abcdef1234567890abcdef12345678"
    path = tmp_path / "build_commit.txt"
    path.write_text(revision.upper() + "\n", encoding="ascii")
    monkeypatch.setattr(buildinfo, "COMMIT_FILE", path)
    monkeypatch.setenv("ACWW_BUILD_COMMIT", "f" * 40)
    response = httpx.get(server.base + "/v1/health", timeout=30)
    assert response.status_code == 200
    assert response.json()["commit"] == revision
    assert response.headers["cache-control"] == "no-store"
