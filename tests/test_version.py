"""Tests for backend/version.py — pure-filesystem git HEAD resolution.

No DB, no app, no subprocess — every case builds a fake .git directory
under tmp_path and reads it via backend.version.get_git_head(repo_root=).
"""
from unittest.mock import patch

import pytest

from backend import version


@pytest.fixture(autouse=True)
def reset_version():
    version.reset()
    yield
    version.reset()


def test_loose_ref(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    heads_dir = git_dir / "refs" / "heads"
    heads_dir.mkdir(parents=True)
    sha = "a" * 40
    (heads_dir / "master").write_text(sha + "\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) == sha


def test_packed_refs_skips_comment_and_peeled_lines(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    # No loose ref file — resolution must fall through to packed-refs.
    sha = "b" * 40
    other_sha = "c" * 40
    tag_sha = "d" * 40
    packed = (
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{other_sha} refs/heads/some-other-branch\n"
        f"{tag_sha} refs/tags/v1.0\n"
        f"^{('e' * 40)}\n"
        f"{sha} refs/heads/master\n"
    )
    (git_dir / "packed-refs").write_text(packed, encoding="utf-8")

    assert version.get_git_head(tmp_path) == sha


def test_packed_refs_not_found_returns_none(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    packed = f"{'f' * 40} refs/heads/other\n"
    (git_dir / "packed-refs").write_text(packed, encoding="utf-8")

    assert version.get_git_head(tmp_path) is None


def test_detached_head(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    sha = "1234567890abcdef1234567890abcdef12345678"
    (git_dir / "HEAD").write_text(sha + "\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) == sha


def test_detached_head_uppercase_is_lowercased(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    sha = "1234567890ABCDEF1234567890ABCDEF12345678"
    (git_dir / "HEAD").write_text(sha + "\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) == sha.lower()


def test_no_git_dir_returns_none(tmp_path):
    assert version.get_git_head(tmp_path) is None


def test_git_dir_is_a_file_worktree_pointer_returns_none(tmp_path):
    # Real git worktrees use a `gitdir: <path>` pointer FILE named .git, not a
    # directory. This must degrade to None (module docstring: worktrees are
    # explicitly out of scope), never raise.
    (tmp_path / ".git").write_text("gitdir: /some/other/path/.git/worktrees/foo\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) is None


def test_garbage_head_content_returns_none_never_raises(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("not a ref and not a sha\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) is None


def test_missing_head_file_returns_none_never_raises(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    # No HEAD file at all.
    assert version.get_git_head(tmp_path) is None


def test_loose_ref_garbage_content_returns_none(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    heads_dir = git_dir / "refs" / "heads"
    heads_dir.mkdir(parents=True)
    (heads_dir / "master").write_text("not-a-valid-sha\n", encoding="utf-8")

    assert version.get_git_head(tmp_path) is None


def test_capture_running_sha_and_reset_roundtrip():
    sha = "9" * 40
    with patch("backend.version.get_git_head", return_value=sha) as mock_head:
        result = version.capture_running_sha()
    mock_head.assert_called_once_with()  # zero-arg call
    assert result == sha
    assert version.running_sha() == sha

    version.reset()
    assert version.running_sha() is None


def test_running_sha_none_before_capture():
    assert version.running_sha() is None


def test_smoke_against_real_repo_root_tolerates_none_or_valid_sha():
    result = version.get_git_head()
    assert result is None or (
        isinstance(result, str) and len(result) == 40 and result == result.lower()
        and all(c in "0123456789abcdef" for c in result)
    )
