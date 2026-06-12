#!/usr/bin/env python3
"""
Automated test runner for interactive CLI tools like `azd ai agent init`.

Uses tmux as the terminal backend and pexpect for keystroke automation.
Captures screenshots via `tmux capture-pane` + Rich SVG rendering.
Test flows are defined in YAML scenario files.
"""

import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.text import Text

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _atomic_write_text(path: str, content: str) -> None:
    """Write ``content`` to ``path`` atomically via tmp + os.replace.

    Prevents a meta-refreshing browser (live report) or any concurrent
    reader from observing a half-written file. POSIX guarantees
    ``os.replace`` is atomic on the same filesystem.
    """
    tmp = f"{path}.tmp"
    Path(tmp).write_text(content)
    os.replace(tmp, path)

SESSION_PREFIX = "azd-auto-test"


@dataclass
class StepResult:
    """Result of executing a single scenario step."""

    step_index: int
    expect_pattern: str
    action: str
    matched_text: str = ""
    ansi_capture: str = ""
    svg_path: str = ""           # post-action screenshot (shown in banner & step display)
    before_svg_path: str = ""    # pre-action screenshot (for "before/after" comparison)
    text_path: str = ""
    elapsed_seconds: float = 0.0
    success: bool = True
    error: str = ""
    label: str = ""
    timestamp: str = ""


@dataclass
class Finding:
    """A finding from the testing session — bug, UX issue, or observation."""

    step_index: int
    title: str
    description: str = ""
    category: str = "bug"  # bug, ux-issue, observation
    screenshot_path: str = ""
    timestamp: str = ""


@dataclass
class ScenarioResult:
    """Result of executing a full scenario."""

    name: str
    command: str
    start_time: str = ""
    end_time: str = ""
    steps: list[StepResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    success: bool = True
    error: str = ""


def tmux_session_name(suffix: str | None = None) -> str:
    """Generate a unique tmux session name.

    The session name combines the current PID with a short random suffix
    so multiple ``AgentSession`` instances inside the same MCP server
    process don't collide on ``tmux new-session -s <name>``.

    Pass an explicit ``suffix`` (e.g. the MCP ``session_id``) for
    traceability; otherwise an 8-char uuid hex is generated.
    """
    token = suffix or uuid.uuid4().hex[:8]
    # Sanitise: tmux session names can't contain '.' or ':' or whitespace.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", token)
    return f"{SESSION_PREFIX}-{os.getpid()}-{safe}"


def tmux_is_installed() -> bool:
    """Check if tmux is available."""
    try:
        subprocess.run(
            ["tmux", "-V"], capture_output=True, check=True, timeout=5
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def tmux_create_session(
    session: str, command: str, cwd: str, env: dict[str, str], width: int = 120, height: int = 40
) -> None:
    """Create a detached tmux session running the given command.

    Scenario-provided env vars are passed via ``tmux new-session -e KEY=VAL``
    so they override the tmux server's frozen environment. Without this, vars
    like ``HOME`` silently no-op because the tmux server (started long ago)
    pins its own env at startup and new sessions inherit *server* env, not
    the caller's. See AGENTS.md for the failure mode this prevents.
    """
    full_env = os.environ.copy()
    full_env.update(env)
    # Force color output in the tmux session
    full_env.setdefault("FORCE_COLOR", "1")
    full_env.setdefault("TERM", "xterm-256color")

    cmd = [
        "tmux", "new-session",
        "-d",
        "-s", session,
        "-x", str(width),
        "-y", str(height),
    ]
    # Pass scenario-provided env vars (plus the forced color/term defaults)
    # via -e so they override anything the tmux server has pinned.
    per_session_env = dict(env)
    per_session_env.setdefault("FORCE_COLOR", "1")
    per_session_env.setdefault("TERM", "xterm-256color")
    for key, value in per_session_env.items():
        cmd.extend(["-e", f"{key}={value}"])
    if cwd:
        cmd.extend(["-c", os.path.expanduser(cwd)])
    cmd.append(command)

    subprocess.run(cmd, env=full_env, check=True, timeout=10)


def tmux_kill_session(session: str) -> None:
    """Kill a tmux session if it exists."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
        timeout=5,
    )


def tmux_session_alive(session: str) -> bool:
    """Check if a tmux session's pane process is still running."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        # pane_dead is "1" when the command has exited
        return result.returncode == 0 and result.stdout.strip() != "1"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def tmux_capture_pane(session: str, with_ansi: bool = True) -> str:
    """Capture the current tmux pane content.

    If the visible pane is empty (e.g. the process exited), falls back to
    capturing the full scrollback history so the final output is preserved.
    """
    cmd = ["tmux", "capture-pane", "-t", session, "-p"]
    if with_ansi:
        cmd.append("-e")  # include ANSI escape sequences
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    text = result.stdout

    # If the visible pane is empty, grab the full scrollback
    if not text.strip():
        cmd_full = ["tmux", "capture-pane", "-t", session, "-p", "-S", "-"]
        if with_ansi:
            cmd_full.append("-e")
        result_full = subprocess.run(cmd_full, capture_output=True, text=True, timeout=5)
        if result_full.stdout.strip():
            text = result_full.stdout

    return text


def tmux_send_keys(session: str, keys: str) -> None:
    """Send keys to a tmux session."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, keys],
        check=True,
        timeout=5,
    )


def tmux_send_text(session: str, text: str) -> None:
    """Send literal text to a tmux session (no key interpretation)."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", text],
        check=True,
        timeout=5,
    )


def wait_for_text(
    session: str, pattern: str, timeout: float = 30.0, poll_interval: float = 0.5
) -> tuple[bool, str]:
    """
    Poll tmux pane until pattern appears or timeout.
    Returns (found, captured_text).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        capture = tmux_capture_pane(session, with_ansi=False)
        if pattern.lower() in capture.lower():
            return True, capture
        time.sleep(poll_interval)
    # Return last capture even on timeout
    return False, tmux_capture_pane(session, with_ansi=False)


def render_ansi_to_svg(ansi_text: str, output_path: str, title: str = "") -> None:
    """Render ANSI text to an SVG file using Rich.

    Writes atomically via tmp + os.replace so a meta-refreshing browser
    never reads a half-written SVG. See ``_atomic_write_text``.
    """
    console = Console(record=True, width=120, force_terminal=True, file=open(os.devnull, "w"))
    text = Text.from_ansi(ansi_text)
    console.print(text)
    svg = console.export_svg(title=title or "Terminal Capture")
    _atomic_write_text(output_path, svg)


def save_text_capture(text: str, output_path: str) -> None:
    """Save plain text capture to a file (atomic write)."""
    clean = ANSI_RE.sub("", text)
    _atomic_write_text(output_path, clean)


def execute_action(session: str, step: dict) -> None:
    """Execute a prompt action (select, confirm, input, multi-select)."""
    action = step.get("action", "")

    if action == "select":
        # Navigate to the right choice using arrow keys
        choice_index = step.get("choice_index")
        if choice_index is not None:
            for _ in range(choice_index):
                tmux_send_keys(session, "Down")
                time.sleep(0.1)
        elif "choice" in step:
            # Try to find the choice by text — send Down until we see it
            # highlighted, with a reasonable cap
            target = step["choice"]
            for _ in range(20):
                capture = tmux_capture_pane(session, with_ansi=False)
                # Check if our target is near a selection indicator
                lines = capture.split("\n")
                for line in lines:
                    if target.lower() in line.lower() and (">" in line or "❯" in line):
                        break
                else:
                    tmux_send_keys(session, "Down")
                    time.sleep(0.15)
                    continue
                break
        tmux_send_keys(session, "Enter")

    elif action == "confirm":
        value = step.get("value", True)
        tmux_send_keys(session, "y" if value else "n")
        tmux_send_keys(session, "Enter")

    elif action == "input":
        text = step.get("text", "")
        tmux_send_text(session, text)
        tmux_send_keys(session, "Enter")

    elif action == "key":
        key = step.get("key", "")
        count = step.get("count", 1)
        for _ in range(count):
            tmux_send_keys(session, key)
            time.sleep(0.05)

    elif action == "multi_select":
        # Toggle specified items then confirm
        indices = step.get("toggle_indices", [0])
        current = 0
        for idx in sorted(indices):
            while current < idx:
                tmux_send_keys(session, "Down")
                time.sleep(0.1)
                current += 1
            tmux_send_keys(session, " ")  # space to toggle
            time.sleep(0.1)
        tmux_send_keys(session, "Enter")

    elif action == "wait":
        # Just wait, no action needed
        pass

    else:
        raise ValueError(f"Unknown action: {action}")


def _rel(path: str, run_dir: str) -> str:
    """Return ``path`` relative to ``run_dir`` for file:// use in HTML.

    Browsers loading ``run_dir/report.html`` via file:// can resolve
    relative paths without filesystem permission grants. Absolute file://
    URIs work too but are uglier and break if the run_dir is moved.
    """
    if not path:
        return ""
    try:
        return os.path.relpath(path, run_dir)
    except ValueError:
        return path


def _latest_event(result):
    """Return the most recent ('step'|'finding', timestamp, obj) tuple, or None.

    ISO-8601 timestamps sort lexicographically, so we sort on the raw
    string. Falls back to step_index for objects with no timestamp.
    """
    events = []
    for s in result.steps:
        events.append(("step", s.timestamp or "", s.step_index, s))
    for f in result.findings:
        events.append(("finding", f.timestamp or "", f.step_index, f))
    if not events:
        return None
    events.sort(key=lambda e: (e[1], e[2]))
    kind, ts, _idx, obj = events[-1]
    return kind, ts, obj


def generate_html_report(run_dir: str, result: ScenarioResult, is_live: bool = False) -> None:
    """Generate an HTML report with timeline, findings, and screenshots.

    Args:
        run_dir: Directory where ``report.html`` will be written.
        result: The scenario result snapshot. In live mode this may
            contain a growing list of steps/findings; the report is
            re-generated each time the session emits an event.
        is_live: When True, render a refreshing dashboard:
            * <meta http-equiv="refresh"> every 2 seconds
            * "🔴 Running" status badge
            * Sticky "Current Step" banner showing the latest event
            * SVGs referenced via <img src="..."> (not inlined) so the
              browser re-fetches them from disk on each refresh and we
              don't pay an O(n²) cost rewriting all past captures.
            * sessionStorage scroll preservation across refreshes
            When False (default), produces the final portable report with
            all SVGs inlined.

    Writes ``report.html`` atomically via tmp + os.replace.
    """

    total_time = sum(s.elapsed_seconds for s in result.steps)
    failed_steps = [s for s in result.steps if not s.success]

    # Finding categories and colors
    cat_colors = {
        "bug": "#ef4444",        # red
        "ux-issue": "#eab308",   # yellow
        "observation": "#3b82f6", # blue
    }
    cat_labels = {
        "bug": "Bug",
        "ux-issue": "UX Issue",
        "observation": "Observation",
    }
    cat_icons = {
        "bug": "🐛",
        "ux-issue": "⚠️",
        "observation": "💡",
    }
    # Count findings by category
    from collections import Counter
    finding_counts = Counter(f.category for f in result.findings)
    bug_count = finding_counts.get("bug", 0)

    esc = html.escape  # local alias

    def _svg_block(svg_path: str) -> str:
        """Render a screenshot. Inlines when static (portable), references when live."""
        if not svg_path or not os.path.exists(svg_path):
            return ""
        if is_live:
            rel = esc(_rel(svg_path, run_dir), quote=True)
            return f'<div class="screenshot"><img src="{rel}" alt="screenshot"></div>'
        try:
            svg_content = Path(svg_path).read_text()
        except OSError:
            return ""
        return f'<div class="screenshot">{svg_content}</div>'

    meta_refresh = '<meta http-equiv="refresh" content="2">' if is_live else ''

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{meta_refresh}
<title>{'🔴 ' if is_live else ''}Test Report: {esc(result.name)}</title>
<style>
:root {{
  --bg: #1a1a2e;
  --surface: #16213e;
  --surface2: #0f3460;
  --text: #e6e6e6;
  --text-muted: #94a3b8;
  --accent: #e94560;
  --green: #22c55e;
  --red: #ef4444;
  --yellow: #eab308;
  --blue: #3b82f6;
  --border: #334155;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}

/* Live banner (sticky current step) */
.live-banner {{
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--surface2);
  border: 2px solid var(--accent);
  border-radius: 12px;
  padding: 16px 20px;
  margin-bottom: 16px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
}}
.live-banner .banner-header {{
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}}
.live-banner .pulse {{
  display: inline-block;
  width: 10px;
  height: 10px;
  background: var(--red);
  border-radius: 50%;
  animation: pulse 1.5s ease-in-out infinite;
}}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; transform: scale(1); }}
  50% {{ opacity: 0.5; transform: scale(1.3); }}
}}
.live-banner .banner-title {{ font-weight: 600; font-size: 1.05rem; }}
.live-banner .banner-meta {{ color: var(--text-muted); font-size: 0.8rem; }}
.live-banner .screenshot {{ margin-top: 12px; max-height: 360px; overflow: hidden; }}
.live-banner .screenshot img,
.live-banner .screenshot svg {{ max-height: 360px; width: auto; max-width: 100%; }}

.status-badge {{
  display: inline-block;
  padding: 4px 12px;
  border-radius: 12px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.status-badge.running {{ background: var(--red); color: white; }}
.status-badge.success {{ background: var(--green); color: white; }}
.status-badge.failed {{ background: var(--red); color: white; }}

/* Header */
.header {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.header h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
.header .command {{ color: var(--text-muted); font-family: monospace; font-size: 0.9rem; }}
.stats {{
  display: flex;
  gap: 24px;
  margin-top: 16px;
  flex-wrap: wrap;
}}
.stat {{
  background: var(--bg);
  border-radius: 8px;
  padding: 12px 20px;
  text-align: center;
  min-width: 100px;
}}
.stat-value {{ font-size: 1.5rem; font-weight: 700; }}
.stat-label {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }}

/* Timeline */
.timeline-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.timeline-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.timeline-bar {{
  display: flex;
  height: 32px;
  border-radius: 6px;
  overflow: hidden;
  background: var(--bg);
  margin-bottom: 8px;
}}
.timeline-bar .segment {{
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.65rem;
  color: white;
  cursor: pointer;
  transition: opacity 0.2s;
  min-width: 2px;
  border-right: 1px solid var(--bg);
}}
.timeline-bar .segment:hover {{ opacity: 0.8; }}
.timeline-bar .segment.pass {{ background: var(--green); }}
.timeline-bar .segment.fail {{ background: var(--red); }}
.timeline-bar .segment.warn {{ background: var(--yellow); }}
.timeline-bar .segment.info {{ background: var(--blue); }}
.timeline-legend {{
  display: flex;
  gap: 16px;
  font-size: 0.75rem;
  color: var(--text-muted);
}}
.timeline-legend span::before {{
  content: '';
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: middle;
}}
.timeline-legend .leg-pass::before {{ background: var(--green); }}
.timeline-legend .leg-fail::before {{ background: var(--red); }}
.timeline-legend .leg-warn::before {{ background: var(--yellow); }}
.timeline-legend .leg-info::before {{ background: var(--blue); }}

/* Bugs Section */
.bugs-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.bugs-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.bug-card {{
  background: var(--bg);
  border-left: 4px solid var(--yellow);
  border-radius: 0 8px 8px 0;
  padding: 16px;
  margin-bottom: 12px;
}}
.bug-card .bug-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}}
.bug-card .bug-title {{ font-weight: 600; }}
.severity-badge {{
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: white;
}}
.bug-card .bug-desc {{ color: var(--text-muted); font-size: 0.9rem; }}
.bug-card .bug-step {{ color: var(--text-muted); font-size: 0.75rem; margin-top: 4px; }}
.no-bugs {{
  text-align: center;
  padding: 32px;
  color: var(--green);
  font-size: 1.1rem;
}}

/* Steps */
.steps-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.steps-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.step {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 8px;
  overflow: hidden;
}}
.step.fail {{ border-color: var(--red); }}
.step-summary {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
}}
.step-summary:hover {{ background: var(--surface2); }}
.step-summary .step-left {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.step-summary .step-icon {{ font-size: 0.9rem; }}
.step-summary .step-label {{ font-size: 0.9rem; }}
.step-summary .step-right {{
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 0.8rem;
  color: var(--text-muted);
}}
.step-details {{
  display: none;
  padding: 16px;
  border-top: 1px solid var(--border);
}}
.step.open .step-details {{ display: block; }}
.step-details pre {{
  background: var(--surface);
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 0.8rem;
  margin-top: 8px;
}}
.step-details .error-msg {{
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid var(--red);
  color: var(--red);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 0.85rem;
  margin-bottom: 8px;
}}
.step-details object, .step-details img {{
  max-width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-top: 8px;
}}
.screenshot svg, .screenshot img {{
  max-width: 100%;
  height: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-top: 8px;
}}

/* Final capture */
.final-section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 24px;
}}
.final-section h2 {{ font-size: 1.1rem; margin-bottom: 16px; }}
.final-section object {{ max-width: 100%; border: 1px solid var(--border); border-radius: 6px; }}
</style>
</head>
<body>
<div class="container">
"""

    # --- Live "Current Step" banner ---
    if is_live:
        latest = _latest_event(result)
        if latest is None:
            html_doc += """
<div class="live-banner">
  <div class="banner-header">
    <span class="pulse"></span>
    <span class="banner-title">Waiting for first event…</span>
    <span class="status-badge running">Running</span>
  </div>
  <div class="banner-meta">Session started. No actions yet.</div>
</div>
"""
        else:
            kind, ts, obj = latest
            if kind == "step":
                # obj is StepResult — use after-action screenshot (svg_path)
                banner_title = esc(obj.label or f"Step {obj.step_index} — {obj.action}")
                banner_meta = (
                    f"Step {obj.step_index} · {obj.elapsed_seconds:.1f}s"
                    + (f" · {esc(ts)}" if ts else "")
                )
                banner_svg = _svg_block(obj.svg_path)
            else:
                # obj is Finding — show finding's screenshot
                icon = cat_icons.get(obj.category, "📝")
                banner_title = f"{icon} {esc(obj.title)}"
                banner_meta = (
                    f"Finding (after step {obj.step_index})"
                    + (f" · {esc(ts)}" if ts else "")
                )
                banner_svg = _svg_block(obj.screenshot_path)

            html_doc += f"""
<div class="live-banner">
  <div class="banner-header">
    <span class="pulse"></span>
    <span class="banner-title">{banner_title}</span>
    <span class="status-badge running">Running</span>
  </div>
  <div class="banner-meta">{banner_meta}</div>
  {banner_svg}
</div>
"""

    # --- Header ---
    if is_live:
        status_emoji = "🔴"
        status_badge = '<span class="status-badge running">Running</span>'
    elif not result.success or bug_count > 0:
        status_emoji = "❌"
        status_badge = '<span class="status-badge failed">Failed</span>'
    else:
        status_emoji = "✅"
        status_badge = '<span class="status-badge success">Complete</span>'

    # Build stats for each finding category that has findings
    finding_stats = ""
    for cat in ["bug", "ux-issue", "observation"]:
        count = finding_counts.get(cat, 0)
        if count > 0:
            color = cat_colors[cat]
            label = cat_labels[cat] + ("s" if count != 1 else "")
            finding_stats += f"""
    <div class="stat">
      <div class="stat-value" style="color: {color};">{count}</div>
      <div class="stat-label">{label}</div>
    </div>"""

    end_time_str = esc(result.end_time) if result.end_time else "<em>in progress…</em>"
    html_doc += f"""
<div class="header">
  <h1>{status_emoji} {esc(result.name)} {status_badge}</h1>
  <div class="command">{esc(result.command)}</div>
  <div class="stats">{finding_stats}
    <div class="stat">
      <div class="stat-value">{total_time:.1f}s</div>
      <div class="stat-label">Duration</div>
    </div>
    <div class="stat">
      <div class="stat-value">{len(result.steps)}</div>
      <div class="stat-label">Steps</div>
    </div>
  </div>
  <div style="margin-top: 12px; font-size: 0.8rem; color: var(--text-muted);">
    {esc(result.start_time)} → {end_time_str}
  </div>
</div>
"""

    # --- Timeline ---
    finding_step_map = {}  # step_index -> worst category
    cat_priority = {"bug": 2, "ux-issue": 1, "observation": 0}
    step_indices = {s.step_index for s in result.steps}
    for finding in result.findings:
        idx = finding.step_index
        if idx not in step_indices and result.steps:
            idx = max(
                (s.step_index for s in result.steps if s.step_index <= finding.step_index),
                default=result.steps[-1].step_index,
            )
        prev = finding_step_map.get(idx)
        if prev is None or cat_priority.get(finding.category, 0) > cat_priority.get(prev, 0):
            finding_step_map[idx] = finding.category

    html_doc += '<div class="timeline-section"><h2>⏱ Timeline</h2>\n<div class="timeline-bar">\n'
    if total_time > 0:
        for step in result.steps:
            pct = max((step.elapsed_seconds / total_time) * 100, 0.5)
            finding_cat = finding_step_map.get(step.step_index)
            if not step.success or finding_cat == "bug":
                cls = "fail"
            elif finding_cat == "ux-issue":
                cls = "warn"
            elif finding_cat == "observation":
                cls = "info"
            else:
                cls = "pass"
            label = step.label or f"Step {step.step_index}"
            short_label = label[:20] if len(label) > 20 else label
            html_doc += (
                f'  <div class="segment {cls}" style="width:{pct:.1f}%" '
                f'title="{esc(label, quote=True)} ({step.elapsed_seconds:.1f}s)" '
                f'onclick="toggleStep({step.step_index})">'
                f'{esc(short_label) if pct > 8 else ""}</div>\n'
            )
    html_doc += '</div>\n'
    html_doc += '<div class="timeline-legend">'
    html_doc += '<span class="leg-pass">Pass</span>'
    html_doc += '<span class="leg-fail">Bug</span>'
    html_doc += '<span class="leg-warn">UX Issue</span>'
    html_doc += '<span class="leg-info">Observation</span>'
    html_doc += f'<span>Total: {total_time:.1f}s</span>'
    html_doc += '</div>\n</div>\n'

    # --- Findings Section ---
    html_doc += '<div class="bugs-section"><h2>📋 Findings</h2>\n'
    if result.findings:
        for finding in result.findings:
            color = cat_colors.get(finding.category, cat_colors["observation"])
            icon = cat_icons.get(finding.category, "📝")
            label = cat_labels.get(finding.category, finding.category)
            html_doc += f"""
<div class="bug-card" style="border-left-color: {color};">
  <div class="bug-header">
    <span class="bug-title">{icon} {esc(finding.title)}</span>
    <span class="severity-badge" style="background: {color};">{esc(label)}</span>
  </div>
  <div class="bug-desc">{esc(finding.description)}</div>
  <div class="bug-step">Step {finding.step_index}{' · ' + esc(finding.timestamp) if finding.timestamp else ''}</div>
"""
            html_doc += f'  {_svg_block(finding.screenshot_path)}\n'
            html_doc += '</div>\n'
    else:
        html_doc += '<div class="no-bugs">{}</div>\n'.format(
            '⏳ No findings yet' if is_live else '✅ No findings'
        )
    html_doc += '</div>\n'

    # --- Steps ---
    html_doc += '<div class="steps-section"><h2>📋 Steps</h2>\n'
    if not result.steps and is_live:
        html_doc += '<div class="no-bugs" style="color: var(--text-muted);">⏳ Waiting for first step…</div>\n'
    for step in result.steps:
        fail_cls = " fail" if not step.success else ""
        icon = "❌" if not step.success else "✅"
        label_text = step.label or f"expect \"{step.expect_pattern}\" → {step.action}"
        time_str = f"{step.elapsed_seconds:.1f}s"
        timestamp_str = f" at {esc(step.timestamp)}" if step.timestamp else ""

        html_doc += f"""
<div class="step{fail_cls}" id="step-{step.step_index}">
  <div class="step-summary" onclick="this.parentElement.classList.toggle('open')">
    <div class="step-left">
      <span class="step-icon">{icon}</span>
      <span class="step-label"><strong>Step {step.step_index}</strong> — {esc(label_text)}</span>
    </div>
    <div class="step-right">
      <span>{time_str}</span>
    </div>
  </div>
  <div class="step-details">
"""
        if step.error:
            html_doc += f'    <div class="error-msg">⚠️ {esc(step.error)}</div>\n'
        html_doc += f'    <pre>Action: {esc(step.action)}{timestamp_str}</pre>\n'
        html_doc += f'    {_svg_block(step.svg_path)}\n'
        html_doc += '  </div>\n</div>\n'

    html_doc += '</div>\n'

    # --- Final capture ---
    final_svg = os.path.join(run_dir, "final.svg")
    if os.path.exists(final_svg):
        html_doc += f"""
<div class="final-section">
  <h2>🏁 Final State</h2>
  {_svg_block(final_svg)}
</div>
"""

    # --- Script for interactivity (+ scroll preservation in live mode) ---
    scroll_preserve = """
// Preserve scroll position across meta-refresh
window.addEventListener('beforeunload', () => {
  sessionStorage.setItem('scrollY', String(window.scrollY));
});
window.addEventListener('load', () => {
  const y = sessionStorage.getItem('scrollY');
  if (y !== null) window.scrollTo(0, parseInt(y, 10));
});
""" if is_live else ""

    html_doc += f"""
<script>
function toggleStep(index) {{
  const el = document.getElementById('step-' + index);
  if (el) {{
    el.classList.toggle('open');
    el.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
  }}
}}
// Auto-expand failed steps
document.querySelectorAll('.step.fail').forEach(el => el.classList.add('open'));
{scroll_preserve}
</script>
"""

    html_doc += '</div></body></html>'

    report_path = os.path.join(run_dir, "report.html")
    _atomic_write_text(report_path, html_doc)
    if not is_live:
        print(f"📄 Report: {report_path}", file=sys.stderr)


