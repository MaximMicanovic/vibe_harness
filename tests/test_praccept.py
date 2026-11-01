"""PR accept (app/praccept.py): the pure PR body, the gh/git calls, the setting.

No network: every `gh` / `git push` invocation goes through `praccept._run`,
which these tests replace with a recorder.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import praccept
from app.gitops import GitError, GitPreflightError
from app.settings import (
    ACCEPT_MODE_KEYS,
    DEFAULT_ACCEPT_MODE,
    SettingsError,
    SettingsStore,
    describe_accept_modes,
)


# ----------------------------------------------------------------------
# the PR body: a pure function, no network, no git
# ----------------------------------------------------------------------

CARD = {
    "id": "abc123",
    "title": "Add a hello file",
    "body": "# Goal\n\nWrite hello.txt so the app greets the world.\n",
}

REVIEW = {
    "approved": True,
    "reason": "the diff does what the spec asks and the suite is green",
    "round": 2,
    "model": "deepseek-v4-1-flash-1017",
}

TEST_OUTPUT = "....\n4 passed in 0.12s\n"
DIFF_STAT = " hello.txt | 1 +\n 1 file changed, 1 insertion(+)\n"


def test_pr_body_carries_the_card_spec():
    body = praccept.build_pr_body(card=CARD, review=REVIEW,
                                  test_output=TEST_OUTPUT, diff_stat=DIFF_STAT)
    assert "Write hello.txt so the app greets the world." in body
    assert "Add a hello file" in body


def test_pr_body_carries_the_review_verdict():
    body = praccept.build_pr_body(card=CARD, review=REVIEW,
                                  test_output=TEST_OUTPUT, diff_stat=DIFF_STAT)
    # approved + reason + round + model — the audit trail the PR is for.
    assert "approved" in body.lower()
    assert REVIEW["reason"] in body
    assert "round 2" in body.lower()
    assert REVIEW["model"] in body


def test_pr_body_carries_the_test_output_tail_and_diff_stat():
    body = praccept.build_pr_body(card=CARD, review=REVIEW,
                                  test_output=TEST_OUTPUT, diff_stat=DIFF_STAT)
    assert "4 passed in 0.12s" in body
    assert "1 file changed, 1 insertion(+)" in body


def test_pr_body_keeps_only_the_tail_of_a_long_test_output():
    long_output = "x" * 20000 + "\nTHE LAST LINE\n"
    body = praccept.build_pr_body(card=CARD, review=REVIEW,
                                  test_output=long_output, diff_stat=DIFF_STAT)
    assert "THE LAST LINE" in body
    assert len(body) < len(long_output)


def test_pr_body_survives_a_missing_review_and_test_output():
    """A card accepted after the review rounds were exhausted has no approval;
    a run without a test command has no output. The body must still build."""
    body = praccept.build_pr_body(
        card=CARD, review={"approved": False, "exhausted": True, "round": 5},
        test_output=None, diff_stat=None)
    assert "Write hello.txt" in body
    assert "exhausted" in body.lower()
    assert "no test output" in body.lower()
    assert "no diff" in body.lower()


def test_pr_body_names_the_branch_and_base():
    body = praccept.build_pr_body(card=CARD, review=REVIEW,
                                  test_output=TEST_OUTPUT, diff_stat=DIFF_STAT,
                                  branch="harness/abc123", base_branch="main")
    assert "harness/abc123" in body
    assert "main" in body


# ----------------------------------------------------------------------
# parsing what gh prints
# ----------------------------------------------------------------------

def test_parse_pr_url_takes_the_last_url():
    out = ("Warning: 1 uncommitted change\n"
           "https://github.com/me/app/pull/7\n")
    assert praccept.parse_pr_url(out) == "https://github.com/me/app/pull/7"


def test_parse_pr_url_returns_none_without_a_url():
    assert praccept.parse_pr_url("") is None
    assert praccept.parse_pr_url("something went wrong") is None


def test_parse_pr_number():
    assert praccept.parse_pr_number("https://github.com/me/app/pull/42") == 42
    assert praccept.parse_pr_number(None) is None
    assert praccept.parse_pr_number("https://github.com/me/app") is None


# ----------------------------------------------------------------------
# preflight: name the missing piece, change nothing
# ----------------------------------------------------------------------

GIT_INFO = {"repo": "/tmp/repo", "branch": "harness/abc123", "base_branch": "main"}


@pytest.fixture
def calls(monkeypatch):
    """Record every gh/git invocation instead of running it."""
    recorded: list[list[str]] = []

    def fake_run(cmd, cwd=None):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(praccept, "_run", fake_run)
    return recorded


def test_preflight_names_a_missing_gh(monkeypatch):
    monkeypatch.setattr(praccept, "gh_available", lambda: False)
    with pytest.raises(GitPreflightError) as exc:
        praccept.preflight(GIT_INFO)
    assert "gh" in str(exc.value)
    assert "not installed" in str(exc.value)


def test_preflight_names_a_logged_out_gh(monkeypatch):
    monkeypatch.setattr(praccept, "gh_available", lambda: True)
    monkeypatch.setattr(praccept, "gh_authenticated", lambda: False)
    with pytest.raises(GitPreflightError) as exc:
        praccept.preflight(GIT_INFO)
    assert "gh auth" in str(exc.value)


def test_preflight_names_a_missing_origin(monkeypatch):
    monkeypatch.setattr(praccept, "gh_available", lambda: True)
    monkeypatch.setattr(praccept, "gh_authenticated", lambda: True)
    monkeypatch.setattr(praccept, "has_origin", lambda root: False)
    with pytest.raises(GitPreflightError) as exc:
        praccept.preflight(GIT_INFO)
    assert "origin" in str(exc.value)


def test_preflight_passes_when_everything_is_there(monkeypatch):
    monkeypatch.setattr(praccept, "gh_available", lambda: True)
    monkeypatch.setattr(praccept, "gh_authenticated", lambda: True)
    monkeypatch.setattr(praccept, "has_origin", lambda root: True)
    monkeypatch.setattr(praccept, "branch_exists", lambda root, branch: True)
    praccept.preflight(GIT_INFO)          # does not raise


def test_preflight_is_a_git_error_so_the_api_maps_it_to_409():
    assert issubclass(GitPreflightError, GitError)


# ----------------------------------------------------------------------
# the side effects: push, then gh pr create
# ----------------------------------------------------------------------

def test_push_branch_pushes_the_card_branch_to_origin(calls):
    praccept.push_branch(GIT_INFO)
    assert calls == [["git", "push", "-u", "origin", "harness/abc123"]]


def test_push_branch_raises_a_git_error_on_failure(monkeypatch):
    monkeypatch.setattr(praccept, "_run", lambda cmd, cwd=None: subprocess.CompletedProcess(
        cmd, 128, "", "fatal: could not read from remote repository"))
    with pytest.raises(GitError) as exc:
        praccept.push_branch(GIT_INFO)
    assert "could not read from remote" in str(exc.value)


def test_open_pr_calls_gh_pr_create_with_the_spec_flags(calls):
    url = praccept.open_pr(GIT_INFO, title="Add a hello file", body="the body")
    assert calls == [["gh", "pr", "create", "--title", "Add a hello file",
                      "--body", "the body", "--head", "harness/abc123"]]
    # No URL in the stubbed output -> no URL, but the call still happened.
    assert url is None


def test_open_pr_returns_the_url_gh_prints(monkeypatch):
    monkeypatch.setattr(praccept, "_run", lambda cmd, cwd=None: subprocess.CompletedProcess(
        cmd, 0, "https://github.com/me/app/pull/9\n", ""))
    assert praccept.open_pr(GIT_INFO, title="t", body="b") == \
        "https://github.com/me/app/pull/9"


def test_open_pr_raises_a_git_error_on_failure(monkeypatch):
    monkeypatch.setattr(praccept, "_run", lambda cmd, cwd=None: subprocess.CompletedProcess(
        cmd, 1, "", "pull request create failed: a pull request already exists"))
    with pytest.raises(GitError) as exc:
        praccept.open_pr(GIT_INFO, title="t", body="b")
    assert "already exists" in str(exc.value)


def test_diff_stat_reads_the_branch_against_its_base(monkeypatch):
    seen = []

    def fake_run(cmd, cwd=None):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, DIFF_STAT, "")

    monkeypatch.setattr(praccept, "_run", fake_run)
    assert praccept.diff_stat(GIT_INFO) == DIFF_STAT.strip()
    assert seen == [["git", "diff", "--stat", "main...harness/abc123"]]


def test_diff_stat_is_empty_when_git_fails(monkeypatch):
    monkeypatch.setattr(praccept, "_run", lambda cmd, cwd=None: subprocess.CompletedProcess(
        cmd, 128, "", "fatal: bad revision"))
    assert praccept.diff_stat(GIT_INFO) == ""


# ----------------------------------------------------------------------
# the setting: local (default) or pr
# ----------------------------------------------------------------------

def test_accept_mode_defaults_to_local(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.accept_mode() == DEFAULT_ACCEPT_MODE == "local"


def test_accept_mode_roundtrips_through_the_file(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.set_accept_mode("pr") == "pr"
    assert store.accept_mode() == "pr"
    assert SettingsStore(tmp_path / "settings.json").accept_mode() == "pr"


def test_accept_mode_rejects_an_unknown_value(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(SettingsError):
        store.set_accept_mode("merge")
    assert store.accept_mode() == "local"


def test_accept_mode_falls_back_to_the_config_default(tmp_path):
    """The env var is the default; the sidebar choice (settings.json) wins."""
    store = SettingsStore(tmp_path / "settings.json")
    assert store.accept_mode("pr") == "pr"          # nothing stored yet
    store.set_accept_mode("local")
    assert store.accept_mode("pr") == "local"       # the stored choice wins


def test_accept_mode_ignores_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert SettingsStore(path).accept_mode() == "local"


def test_accept_modes_are_described_for_the_ui():
    assert ACCEPT_MODE_KEYS == ("local", "pr")
    described = describe_accept_modes()
    assert [m["key"] for m in described] == ["local", "pr"]
    assert all(m["label"] and m["description"] for m in described)


# ----------------------------------------------------------------------
# the config default: VIBE_HARNESS_ACCEPT_MODE
# ----------------------------------------------------------------------

def test_config_accept_mode_defaults_to_local():
    from app.config import load_config
    assert load_config({}).accept_mode == "local"


def test_config_accept_mode_reads_the_env_var():
    from app.config import load_config
    assert load_config({"VIBE_HARNESS_ACCEPT_MODE": "pr"}).accept_mode == "pr"
    assert load_config({"VIBE_HARNESS_ACCEPT_MODE": "PR"}).accept_mode == "pr"
    assert load_config({"VIBE_HARNESS_ACCEPT_MODE": "local"}).accept_mode == "local"


def test_config_accept_mode_falls_back_to_local_on_a_typo():
    from app.config import load_config
    assert load_config({"VIBE_HARNESS_ACCEPT_MODE": "pull-request"}).accept_mode == "local"
