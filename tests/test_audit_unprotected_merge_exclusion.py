"""Merge-commit exclusion in audit_unprotected_commits.py.

An integration merge with an empty combined diff carries no content that
differs from both parents: every line is already covered by a parent (local
side via keep refs, upstream side via origin). Such merges must be reported
as OK-MERGE instead of UNPROTECTED. A merge with a unique manual resolution
(`git diff-tree --cc` non-empty) must still be flagged, with a merge-specific
reason. Plain commits keep the existing behavior with and without a declared
ref.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "audit_unprotected_commits.py"

FAKE_UPDATER = '''#!/usr/bin/env python3
WEBUI_PROTECTED_PATCHES = (
    {
        "name": "demo-patch",
        "ref": "local/keep-demo",
        "path": pathlib.Path("static/ui.js"),
        "markers": ("demo-marker",),
    },
)

WEBUI_CONFLICT_PREFERRED_OURS = ()
'''


def load_module():
    spec = importlib.util.spec_from_file_location("audit_unprotected_commits", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    def commit(self, msg: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def write(self, name: str, content: str) -> None:
        target = self.path / name
        target.write_text(content, encoding="utf-8")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def test_clean_integration_merge_is_excluded(tmp_path, monkeypatch, capsys):
    mod = load_module()
    repo = Repo(tmp_path / "repo")
    updater = tmp_path / "fake-nightly-update.py"
    updater.write_text(FAKE_UPDATER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", repo.path)
    monkeypatch.setattr(mod, "UPDATER", updater)

    repo.write("f.txt", "base\n")
    base = repo.commit("base")
    repo.write("local.txt", "local feature\n")
    repo.commit("local feature")
    repo.git("checkout", "-q", "-b", "upstream", base)
    repo.write("upstream.txt", "upstream change\n")
    repo.commit("upstream change")
    repo.git("checkout", "-q", "master")
    repo.git("merge", "-q", "--no-edit", "upstream")
    merge_sha = repo.head()

    monkeypatch.setattr("sys.argv", ["audit", "--rev", merge_sha])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "UNPROTECTED" not in out
    assert "OK-MERGE" in out


def test_merge_with_unique_resolution_is_flagged(tmp_path, monkeypatch, capsys):
    mod = load_module()
    repo = Repo(tmp_path / "repo")
    updater = tmp_path / "fake-nightly-update.py"
    updater.write_text(FAKE_UPDATER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", repo.path)
    monkeypatch.setattr(mod, "UPDATER", updater)

    repo.write("f.txt", "base line\n")
    base = repo.commit("base")
    repo.write("f.txt", "local line\n")
    repo.commit("local change")
    repo.git("checkout", "-q", "-b", "upstream", base)
    repo.write("f.txt", "upstream line\n")
    repo.commit("upstream change")
    repo.git("checkout", "-q", "master")
    repo.git("merge", "--no-edit", "upstream", check=False)
    repo.write("f.txt", "merged unique line\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "--no-edit")
    merge_sha = repo.head()

    monkeypatch.setattr("sys.argv", ["audit", "--rev", merge_sha])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "UNPROTECTED" in out
    assert "unique manual resolution" in out


def test_plain_commit_without_ref_still_flagged(tmp_path, monkeypatch, capsys):
    mod = load_module()
    repo = Repo(tmp_path / "repo")
    updater = tmp_path / "fake-nightly-update.py"
    updater.write_text(FAKE_UPDATER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", repo.path)
    monkeypatch.setattr(mod, "UPDATER", updater)

    repo.write("f.txt", "base\n")
    repo.commit("base")
    repo.write("f.txt", "local edit\n")
    sha = repo.commit("local edit")

    monkeypatch.setattr("sys.argv", ["audit", "--rev", sha])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "no local/keep-* ref covers this commit" in out


def test_plain_commit_with_declared_ref_ok(tmp_path, monkeypatch, capsys):
    mod = load_module()
    repo = Repo(tmp_path / "repo")
    updater = tmp_path / "fake-nightly-update.py"
    updater.write_text(FAKE_UPDATER, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", repo.path)
    monkeypatch.setattr(mod, "UPDATER", updater)

    repo.write("f.txt", "base\n")
    repo.commit("base")
    repo.write("f.txt", "local edit\n")
    sha = repo.commit("local edit")
    repo.git("branch", "local/keep-demo", sha)

    monkeypatch.setattr("sys.argv", ["audit", "--rev", sha])
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "via local/keep-demo" in out
