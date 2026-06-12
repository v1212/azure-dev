"""Tests for `list_scenarios` MCP tool and tag handling.

Hermetic: synthesises a tmp scenarios tree with valid + malformed YAMLs
and asserts the tool's filtering / warning behaviour without touching
the real ./scenarios directory.
"""

from pathlib import Path

import pytest

from auto_test_tool import mcp_server, ports


@pytest.fixture(autouse=True)
def _clean_registry():
    ports.reset_registry()
    yield
    ports.reset_registry()


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "scenarios"
    root.mkdir()
    (root / "smoke.yaml").write_text(
        'name: smoke\ncommand: "echo hi"\ntags: ["smoke", "fast"]\n'
    )
    (root / "regression.yaml").write_text(
        'name: regression\ncommand: "echo hi"\ntags: ["regression"]\n'
    )
    (root / "untagged.yaml").write_text(
        'name: untagged\ncommand: "echo hi"\n'
    )
    sub = root / "nested"
    sub.mkdir()
    (sub / "deep-smoke.yml").write_text(
        'name: deep-smoke\ncommand: "echo hi"\ntags: ["smoke"]\n'
    )
    return root


def test_no_filter_returns_all_scenarios(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root))
    assert "name: smoke" in out
    assert "name: regression" in out
    assert "name: untagged" in out
    assert "name: deep-smoke" in out
    assert "Total: 4 scenario(s)" in out


def test_or_filter_matches_any_requested_tag(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root), tags=["smoke"])
    assert "name: smoke" in out
    assert "name: deep-smoke" in out
    assert "name: regression" not in out
    assert "name: untagged" not in out
    assert "Total: 2 scenario(s)" in out


def test_or_filter_multiple_tags(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root), tags=["regression", "fast"])
    # smoke.yaml has "fast"; regression.yaml has "regression"; deep-smoke
    # has only "smoke" and should be excluded.
    assert "name: smoke" in out
    assert "name: regression" in out
    assert "name: deep-smoke" not in out
    assert "Total: 2 scenario(s)" in out


def test_filter_with_no_matches(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root), tags=["nonexistent"])
    assert "No scenarios found" in out
    assert "['nonexistent']" in out


def test_output_includes_tags_display(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root))
    assert "tags: smoke, fast" in out
    assert "tags: (none)" in out  # untagged.yaml


def test_malformed_yaml_does_not_abort_scan(tmp_path):
    root = _make_tree(tmp_path)
    (root / "broken.yaml").write_text("name: broken\n  bad: indent:\n: : :\n")
    out = mcp_server.list_scenarios(str(root))
    # Other scenarios still appear.
    assert "name: smoke" in out
    assert "name: regression" in out
    # The broken file is reported as a warning, not silently dropped.
    assert "WARN:" in out
    assert "broken.yaml" in out


def test_malformed_tags_field_skipped_with_warning(tmp_path):
    root = _make_tree(tmp_path)
    (root / "bad-tags.yaml").write_text(
        'name: bad-tags\ncommand: "echo hi"\ntags: "not-a-list"\n'
    )
    out = mcp_server.list_scenarios(str(root))
    assert "name: bad-tags" not in out
    assert "WARN:" in out
    assert "bad-tags.yaml" in out
    assert "must be a list of strings" in out


def test_non_mapping_root_skipped(tmp_path):
    root = _make_tree(tmp_path)
    (root / "list-root.yaml").write_text("- just\n- a\n- list\n")
    out = mcp_server.list_scenarios(str(root))
    assert "list-root.yaml" in out  # appears in warning
    assert "must be a mapping" in out


def test_missing_root_returns_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    out = mcp_server.list_scenarios(str(missing))
    assert out.startswith("ERROR: scenarios root not found")


def test_empty_root_returns_no_scenarios(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    out = mcp_server.list_scenarios(str(root))
    assert out == "No scenarios found."


def test_results_sorted_by_path(tmp_path):
    root = _make_tree(tmp_path)
    out = mcp_server.list_scenarios(str(root))
    # nested/deep-smoke comes after top-level files alphabetically when sorted
    # by full path.
    idx_regression = out.find(str(root / "regression.yaml"))
    idx_smoke = out.find(str(root / "smoke.yaml"))
    idx_nested = out.find(str(root / "nested" / "deep-smoke.yml"))
    assert -1 < idx_nested < idx_regression < idx_smoke


# --- load_scenario tag handling -------------------------------------------


def _write_scenario(tmp_path: Path, body: str) -> str:
    p = tmp_path / "scenario.yaml"
    p.write_text(body)
    return str(p)


def test_load_scenario_surfaces_tags_when_present(tmp_path):
    s = _write_scenario(
        tmp_path,
        'name: tagged\ncommand: "echo hi"\ntags: ["smoke", "fast"]\n',
    )
    out = mcp_server._read_scenario_file(s)
    assert "Tags: smoke, fast" in out


def test_load_scenario_omits_tags_line_when_absent(tmp_path):
    s = _write_scenario(tmp_path, 'name: untagged\ncommand: "echo hi"\n')
    out = mcp_server._read_scenario_file(s)
    assert "Tags:" not in out


def test_load_scenario_rejects_malformed_tags(tmp_path):
    s = _write_scenario(
        tmp_path, 'name: bad\ncommand: "echo hi"\ntags: "not-a-list"\n'
    )
    out = mcp_server._read_scenario_file(s)
    assert out.startswith("ERROR:")
    assert "must be a list of strings" in out


def test_load_scenario_rejects_non_string_tag_elements(tmp_path):
    s = _write_scenario(
        tmp_path, 'name: bad\ncommand: "echo hi"\ntags: ["ok", 42]\n'
    )
    out = mcp_server._read_scenario_file(s)
    assert out.startswith("ERROR:")
    assert "must be a list of strings" in out
