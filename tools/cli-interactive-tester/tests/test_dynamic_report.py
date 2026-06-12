"""Tests for the dynamic / live HTML report.

Covers the behaviours added in the ``dynamic-reports`` branch:

* Live mode includes a meta-refresh tag and "Running" badge; static
  mode does not.
* All user-derived text fields go through ``html.escape`` so a
  ``<script>`` tag in a label / finding cannot execute when the
  auto-opened browser renders it.
* Live mode references SVG screenshots by relative path
  (``<img src="...">``) so meta-refresh doesn't rewrite every embedded
  asset; static mode inlines them for portability.
* Atomic writes leave no ``report.html.tmp`` behind on success.
* ``AgentSession.screenshot()`` and ``report_finding()`` allocate
  fresh capture paths and never overwrite each other (the bug rubber-
  duck flagged: both used to write ``step_{step_index:03d}.svg``).
* ``webbrowser.open`` is gated by the constructor arg, and its
  failure does not break ``start()``.
* The "Current Step" banner reflects the most recent event.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_test_tool.runner import (
    Finding,
    ScenarioResult,
    StepResult,
    _atomic_write_text,
    _latest_event,
    generate_html_report,
)


def _make_result(name="run", **kwargs) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        command="echo hi",
        start_time="2024-01-01T00:00:00",
        **kwargs,
    )


def test_live_mode_has_meta_refresh(tmp_path):
    r = _make_result()
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    assert 'http-equiv="refresh"' in content
    assert "Running" in content


def test_static_mode_has_no_meta_refresh(tmp_path):
    r = _make_result()
    generate_html_report(str(tmp_path), r, is_live=False)
    content = (tmp_path / "report.html").read_text()
    assert 'http-equiv="refresh"' not in content
    # Status text on the completed report
    assert "Complete" in content or "Failed" in content


def test_html_escapes_user_fields(tmp_path):
    """Auto-open browser + un-escaped labels would be live XSS. Don't."""
    r = _make_result(name='<script>alert("xss")</script>')
    r.findings.append(
        Finding(
            step_index=0,
            title='<img onerror=x>',
            description='</div><script>bad()</script>',
            category="bug",
        )
    )
    r.steps.append(
        StepResult(
            step_index=0,
            expect_pattern="",
            action='{"a": "<b>"}',
            label="<i>label</i>",
            error="<script>err</script>",
        )
    )
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    # None of the user-provided literal strings should appear unescaped.
    assert "<script>alert" not in content
    assert "<img onerror=x>" not in content
    assert "<script>bad()" not in content
    assert "<script>err</script>" not in content
    assert "<i>label</i>" not in content
    # But the escaped form must be present so the user sees the text.
    assert "&lt;script&gt;alert" in content


def test_live_mode_references_svg_by_relative_path(tmp_path):
    """Live mode uses <img src> referencing relative paths (no embedding)."""
    svg_path = tmp_path / "capture_000.svg"
    svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    r = _make_result()
    r.steps.append(
        StepResult(
            step_index=0,
            expect_pattern="",
            action="x",
            svg_path=str(svg_path),
            label="step 0",
        )
    )
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    assert '<img src="capture_000.svg"' in content
    # The SVG body must NOT be inlined in live mode.
    assert '<svg xmlns="http://www.w3.org/2000/svg"></svg>' not in content


def test_static_mode_inlines_svg_for_portability(tmp_path):
    svg_path = tmp_path / "capture_000.svg"
    svg_marker = '<svg xmlns="http://www.w3.org/2000/svg" id="MARK"></svg>'
    svg_path.write_text(svg_marker)
    r = _make_result()
    r.steps.append(
        StepResult(
            step_index=0,
            expect_pattern="",
            action="x",
            svg_path=str(svg_path),
            label="step 0",
        )
    )
    generate_html_report(str(tmp_path), r, is_live=False)
    content = (tmp_path / "report.html").read_text()
    assert svg_marker in content
    assert '<img src="capture_000.svg"' not in content


def test_atomic_write_leaves_no_tmp_on_success(tmp_path):
    p = tmp_path / "x.html"
    _atomic_write_text(str(p), "hello")
    assert p.read_text() == "hello"
    assert not (tmp_path / "x.html.tmp").exists()


def test_atomic_write_leaves_tmp_when_replace_fails(tmp_path, monkeypatch):
    """If os.replace blows up, the .tmp file is the only collateral and
    is not visible to the meta-refreshing browser at ``report.html``.
    """
    p = tmp_path / "x.html"
    p.write_text("original")

    def boom(src, dst):
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_write_text(str(p), "new")
    # Original untouched, browser sees nothing weird:
    assert p.read_text() == "original"


def test_no_report_tmp_left_after_generate_html_report(tmp_path):
    r = _make_result()
    generate_html_report(str(tmp_path), r, is_live=True)
    assert (tmp_path / "report.html").exists()
    assert not (tmp_path / "report.html.tmp").exists()


def test_live_banner_shows_latest_step(tmp_path):
    r = _make_result()
    r.steps.append(
        StepResult(
            step_index=0,
            expect_pattern="",
            action="x",
            label="first step",
            timestamp="2024-01-01T00:00:01",
        )
    )
    r.steps.append(
        StepResult(
            step_index=1,
            expect_pattern="",
            action="x",
            label="LATEST_STEP",
            timestamp="2024-01-01T00:00:02",
        )
    )
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    # Banner section should mention the most recent label
    banner = content.split('class="header"')[0]
    assert "LATEST_STEP" in banner


def test_live_banner_picks_finding_over_older_step(tmp_path):
    r = _make_result()
    r.steps.append(
        StepResult(
            step_index=0,
            expect_pattern="",
            action="x",
            label="older step",
            timestamp="2024-01-01T00:00:01",
        )
    )
    r.findings.append(
        Finding(
            step_index=0,
            title="LATEST_FINDING",
            description="newest event",
            category="bug",
            timestamp="2024-01-01T00:00:05",
        )
    )
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    banner = content.split('class="header"')[0]
    assert "LATEST_FINDING" in banner


def test_latest_event_helper_handles_empty():
    r = _make_result()
    assert _latest_event(r) is None


def test_live_mode_placeholder_when_no_steps(tmp_path):
    r = _make_result()
    generate_html_report(str(tmp_path), r, is_live=True)
    content = (tmp_path / "report.html").read_text()
    assert "Waiting for first event" in content


# -------- AgentSession-level tests (mocked tmux) ------------------------


@pytest.fixture
def fake_tmux(monkeypatch):
    """Mock tmux primitives so AgentSession doesn't need a real tmux."""
    from auto_test_tool import agent as agent_mod

    monkeypatch.setattr(agent_mod, "tmux_create_session", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "tmux_send_text", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "tmux_send_keys", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "tmux_kill_session", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod, "tmux_session_alive", lambda *a, **k: True)
    monkeypatch.setattr(
        agent_mod,
        "tmux_capture_pane",
        lambda *a, **k: "terminal contents",
    )
    # Don't actually sleep in tests.
    monkeypatch.setattr(agent_mod.time, "sleep", lambda *a, **k: None)
    return agent_mod


def test_screenshot_and_finding_do_not_overwrite_step(tmp_path, fake_tmux):
    """The original bug: screenshot() and report_finding() both wrote
    ``step_{step_index:03d}.svg``, clobbering the action's own capture
    on the next act(). Each must allocate a fresh path now."""
    from auto_test_tool.agent import AgentSession

    s = AgentSession(
        command="true",
        cwd=str(tmp_path / "wd"),
        output_dir=str(tmp_path / "out"),
        run_name="run",
        auto_open_report=False,
    )
    s.start()

    p_before_act = s.screenshot(label="manual-1")
    s.act({"action": "wait", "seconds": 0, "label": "act-1"})
    p_finding = s.report_finding("a bug", description="bad", category="bug")
    s.act({"action": "wait", "seconds": 0, "label": "act-2"})

    paths = [
        p_before_act,
        s.result.steps[0].before_svg_path,
        s.result.steps[0].svg_path,
        p_finding,
        s.result.steps[1].before_svg_path,
        s.result.steps[1].svg_path,
    ]
    assert len(set(paths)) == len(paths), f"duplicate capture paths: {paths}"
    for p in paths:
        assert os.path.exists(p), f"missing capture: {p}"


def test_act_after_capture_becomes_step_svg(tmp_path, fake_tmux):
    """The Current Step banner should show the post-action terminal, not
    the prelude. That's only true if act() stores the after-capture as
    svg_path."""
    from auto_test_tool.agent import AgentSession

    s = AgentSession(
        command="true",
        cwd=str(tmp_path / "wd"),
        output_dir=str(tmp_path / "out"),
        run_name="run",
        auto_open_report=False,
    )
    s.start()
    s.act({"action": "wait", "seconds": 0, "label": "go"})
    step = s.result.steps[0]
    assert step.svg_path.endswith("_after.svg")
    assert step.before_svg_path.endswith("_before.svg")


def test_auto_open_off_by_default(tmp_path, fake_tmux, monkeypatch):
    """No explicit param → must NOT open the browser."""
    from auto_test_tool.agent import AgentSession

    with patch("auto_test_tool.agent.webbrowser.open") as mock_open:
        s = AgentSession(
            command="true",
            cwd=str(tmp_path / "wd"),
            output_dir=str(tmp_path / "out"),
            run_name="run",
        )
        s.start()
    mock_open.assert_not_called()


def test_auto_open_param_true_opens_browser(tmp_path, fake_tmux, monkeypatch):
    """Explicit auto_open_report=True opts in."""
    from auto_test_tool.agent import AgentSession

    with patch("auto_test_tool.agent.webbrowser.open") as mock_open:
        s = AgentSession(
            command="true",
            cwd=str(tmp_path / "wd"),
            output_dir=str(tmp_path / "out"),
            run_name="run",
            auto_open_report=True,
        )
        s.start()
    assert mock_open.called
    uri = mock_open.call_args[0][0]
    assert uri.startswith("file://")
    assert "report.html" in uri


def test_auto_open_param_false_stays_off(tmp_path, fake_tmux, monkeypatch):
    """Explicit auto_open_report=False keeps it off."""
    from auto_test_tool.agent import AgentSession

    with patch("auto_test_tool.agent.webbrowser.open") as mock_open:
        s = AgentSession(
            command="true",
            cwd=str(tmp_path / "wd"),
            output_dir=str(tmp_path / "out"),
            run_name="run",
            auto_open_report=False,
        )
        s.start()
    mock_open.assert_not_called()


def test_webbrowser_failure_does_not_break_start(tmp_path, fake_tmux, monkeypatch):
    """A broken / headless browser must NOT kill the test session (when opt-in)."""
    from auto_test_tool.agent import AgentSession

    with patch(
        "auto_test_tool.agent.webbrowser.open",
        side_effect=RuntimeError("no display"),
    ):
        s = AgentSession(
            command="true",
            cwd=str(tmp_path / "wd"),
            output_dir=str(tmp_path / "out"),
            run_name="run",
            auto_open_report=True,
        )
        # Must not raise:
        s.start()
        # And report.html exists (from the initial skeleton write):
        assert os.path.exists(s.report_path)


def test_live_report_written_on_start_before_browser(tmp_path, fake_tmux):
    """When auto-open is on, the initial skeleton must exist before
    webbrowser.open is invoked so the browser has something to render
    on first paint."""
    from auto_test_tool.agent import AgentSession

    seen = {"path": None}

    def capture_open(uri):
        # Convert file:// URI back to a path and check it exists.
        from urllib.parse import urlparse, unquote

        path = unquote(urlparse(uri).path)
        seen["path"] = path
        seen["exists_at_call_time"] = os.path.exists(path)

    with patch("auto_test_tool.agent.webbrowser.open", side_effect=capture_open):
        s = AgentSession(
            command="true",
            cwd=str(tmp_path / "wd"),
            output_dir=str(tmp_path / "out"),
            run_name="run",
            auto_open_report=True,
        )
        s.start()
    assert seen["path"] is not None
    assert seen["exists_at_call_time"] is True


def test_finish_writes_static_report(tmp_path, fake_tmux):
    from auto_test_tool.agent import AgentSession

    s = AgentSession(
        command="true",
        cwd=str(tmp_path / "wd"),
        output_dir=str(tmp_path / "out"),
        run_name="run",
        auto_open_report=False,
    )
    s.start()
    report_path = s.finish()
    content = Path(report_path).read_text()
    assert 'http-equiv="refresh"' not in content
