"""Tests for tools/eval_replay.py.

The replay runs real tests; here we test its guarding behaviour so that a mistaken
edit to the frozen node-ID set (or removal of an individual case) fails loudly
instead of silently shrinking the evaluation.
"""

from __future__ import annotations

import pytest

from tools import eval_replay

pytestmark = pytest.mark.unit


def test_replay_set_exists_in_repository():
    eval_replay._files_exist(eval_replay.ROOT)


def test_replay_set_is_non_empty_unique_and_node_level():
    assert eval_replay.REPLAY
    assert len(set(eval_replay.REPLAY)) == len(eval_replay.REPLAY)
    # Every entry must be a concrete node ID, not a whole module, so individual
    # cases cannot be removed without detection.
    for node in eval_replay.REPLAY:
        assert "::" in node, f"{node!r} is a module, not a node ID"


def test_committed_node_ids_all_collect():
    """If a replayed case was removed or renamed, this fails and names it.

    Collecting the real node IDs imports the application (langchain, pandas,
    ...), so this only runs where the dev environment is installed. In a bare
    tooling interpreter it is skipped; CI runs it with the full environment.
    """
    try:
        import langchain_core  # noqa: F401
    except ImportError:
        pytest.skip("application dependencies not installed; full-env only")
    eval_replay._verify_collects(eval_replay.ROOT)


def test_missing_module_is_unavailable(tmp_path):
    with pytest.raises(eval_replay.ReplayUnavailable):
        eval_replay._files_exist(tmp_path)


def test_missing_node_id_is_unavailable(tmp_path, monkeypatch):
    # A file that exists but lacks the referenced case: _files_exist passes,
    # _verify_collects must fail because pytest cannot collect the node ID.
    target = tmp_path / "tests"
    target.mkdir()
    (target / "test_only.py").write_text("def test_present():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(
        eval_replay,
        "REPLAY",
        ("tests/test_only.py::test_present", "tests/test_only.py::test_removed"),
    )
    eval_replay._files_exist(tmp_path)
    with pytest.raises(eval_replay.ReplayUnavailable, match="test_removed"):
        eval_replay._verify_collects(tmp_path)


def test_cli_returns_two_when_modules_missing(tmp_path, capsys):
    code = eval_replay.main(["--repo", str(tmp_path)])
    assert code == 2
    assert "could not run" in capsys.readouterr().err
