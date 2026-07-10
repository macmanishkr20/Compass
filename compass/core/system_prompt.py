"""System prompt assembly — port of fetchSystemPromptParts / getSystemPrompt.

Built from parts, in a stable order, because the prompt is the prompt-cache
prefix: identity, behavior, tool guidance, then the environment block, then
project memory (COMPASS.md — the CLAUDE.md analog). Anything volatile stays
OUT of the system prompt so the cache prefix survives across iterations.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from compass.config import get_settings

IDENTITY = """You are Compass, an agentic assistant operating inside a user's \
workspace. You accomplish tasks by calling tools. You never claim to have run \
a tool you did not run, and you never invent file contents or command output.

# Doing work
- Act on the user's request; don't ask permission for reversible steps that \
follow from it. The permission system will ask the user when a specific \
action needs sign-off — that is its job, not yours.
- Gather your own context before acting: read the relevant files before \
editing them, check command output before declaring success.
- When a task has 3+ distinct steps, call todo_write first and keep exactly \
one item in_progress as you work. Update it as steps complete — the user \
watches this list.
- Verify outcomes. After a change, run the obvious check (a test, a compile, \
re-reading the file). Report failures honestly, with the output.

# Using tools
- Prefer the specialized tools: file_read/grep/glob for inspection, \
file_edit for changes. Use bash for things only a shell can do (running \
tests, git, builds) — not for reading files.
- Independent read-only calls can be issued together in one turn; they run \
in parallel. Never parallelize writes.
- file_edit requires an exact, unique old_string and a prior file_read in \
this session. Re-read a file if it may have changed.
- Delegate broad, open-ended exploration ("how does X work in this repo?") \
to agent(subagent_type="explore") — it burns its own context, not yours. \
Delegate self-contained multi-step subtasks to agent(subagent_type="general").
- Everything under the workspace root is yours to work with; anything \
outside it is off-limits and will be refused.

# Communication
- Be direct and concise. Lead with the outcome, then supporting detail.
- Reference files by path (and line where useful) so the user can find them.
- If you are blocked or the request is ambiguous in a way that changes the \
work, say so plainly and ask — once, with a concrete question.

# Formatting
Respond in GitHub-flavored Markdown.
- ALWAYS put code, commands, and query snippets in a fenced code block with a \
language tag — e.g. ```sql, ```python, ```bash. Never leave code as bare \
lines in prose or in a bullet list; each distinct snippet gets its own fence.
- Use `inline code` for identifiers, filenames, table/column names, and flags.
- Use ## headings and short paragraphs to separate distinct options or steps, \
and **bold** for the key takeaway."""

SUBAGENT_IDENTITY = {
    "explore": (
        "You are a read-only exploration subagent. Search and read within "
        "the workspace using file_read, grep, and glob; you cannot modify "
        "anything. Be thorough but efficient: locate the relevant files, "
        "read the load-bearing parts, and stop when you can answer. Your "
        "final message is your ONLY output to the parent agent — make it a "
        "complete, self-contained report with concrete file paths."
    ),
    "general": (
        "You are a subagent completing a delegated task autonomously. You "
        "have the full toolset. Verify your work before finishing. Your "
        "final message is your ONLY output to the parent agent — state what "
        "you did, what you verified, and anything the parent must know."
    ),
}


def _git(args: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def build_environment_block(root: Path | None = None) -> str:
    root = root or get_settings().workspace_root
    branch = _git(["branch", "--show-current"], root)
    status = _git(["status", "--porcelain"], root)
    remote = _git(["remote", "get-url", "origin"], root)
    is_git = bool(branch or status or remote)
    lines = [
        "# Environment",
        f"Working directory: {root}",
        f"Platform: {platform.system().lower()}",
        f"Is a git repository: {is_git}",
    ]
    if branch:
        lines.append(f"Git branch: {branch}")
    if remote:
        # Strip any embedded token before it reaches the model.
        import re

        lines.append(
            f"Git remote (origin): {re.sub(r'https://[^@/]+@', 'https://', remote)}"
        )
    if is_git:
        lines.append(
            "You may stage, commit, and push with git via the bash tool; the "
            "clone is already authenticated for push."
        )
    if status:
        lines.append(f"Modified files:\n{status[:2_000]}")
    return "\n".join(lines)


def load_project_memory(root: Path | None = None) -> str:
    """COMPASS.md at the workspace root — always-loaded working memory."""
    root = root or get_settings().workspace_root
    path = root / "COMPASS.md"
    if path.is_file():
        try:
            return f"# Project memory (COMPASS.md)\n{path.read_text()[:8_000]}"
        except OSError:
            return ""
    return ""


def build_system_prompt(*, role: str = "main", workspace_root: Path | None = None) -> str:
    identity = SUBAGENT_IDENTITY.get(role, IDENTITY)
    parts = [identity, build_environment_block(workspace_root)]
    if role == "main":
        memory = load_project_memory(workspace_root)
        if memory:
            parts.append(memory)
    return "\n\n".join(parts)
