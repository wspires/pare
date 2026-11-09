#!/usr/bin/env python3
"""Claude Code PreToolUse hook: run noisy Bash commands through pare.

Reads the hook event on stdin. When the Bash command is a single simple
command that usually prints a lot (a test suite, a build, a package
install, terraform plan, kubectl logs, ...), it prints a rewrite to
`pare -- <command>` (with `--budget` if PARE_HOOK_BUDGET says); otherwise
it prints nothing and the command runs unchanged. It never fails closed: any error, an unknown event, or pare
missing from PATH means no rewrite.

What it leaves alone, on purpose:
- compound commands (`&&`, `||`, `;`, pipes, redirections, `$(...)`, `cd`):
  wrapping them would run them in a subshell and change what persists;
- watch modes and followed logs, which never exit (pare reports at exit);
- anything not on the list below, including `cat`, `git diff`, `grep`:
  output the agent needs exactly.

Environment
  PARE_BIN            pare executable (default: pare, looked up on PATH)
  PARE_HOOK_BUDGET    --budget for the rewrite: N, auto or auto:N (default: none,
                      so pare's own default applies: $PARE_BUDGET, else auto)
  PARE_HOOK_DECISION  "" (default), "ask" or "allow": the permissionDecision
                      sent with the rewrite. "allow" skips the permission
                      prompt for every rewritten command, so your allow and
                      ask rules for those commands no longer apply; "ask"
                      prompts for every one. See README.md here.
  PARE_HOOK_EXTRA     extra regex of commands to wrap (matched like the list)
  PARE_HOOK_DISABLE   set to disable the hook without editing settings

Check what it would do:
  pare-hook.py --explain "npm test -- --runInBand"
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys

# A --budget value pare accepts: a number of tokens, auto, or auto:N.
BUDGET_RX = re.compile(r"\d+|auto(?::\d+)?")

# Matched against the command with the program's directory stripped and the
# leading VAR=value assignments removed, e.g. "pytest -x tests/".
WRAP = [
    r"(pytest|py\.test)\b",
    r"python[0-9.]* -m (pytest|unittest|mypy|pip install)\b",
    r"(npm|pnpm|yarn|bun) (test|t|install|i|ci|build)\b",
    r"(npm|pnpm|yarn|bun) run (test|build|lint|typecheck|check|ci)(:\S*)?(\s|$)",
    r"(npx|bunx|pnpm exec|yarn) (jest|vitest|tsc|eslint|next build|playwright test)\b",
    r"(jest|vitest|tsc|eslint|mypy|tox|nox|rspec|phpunit|ctest)\b",
    r"ruff check\b",
    r"cargo (build|test|check|clippy|nextest|bench)\b",
    r"go (test|build|vet)\b",
    r"make\b",
    r"(mvn|mvnw|gradle|gradlew)\b",
    r"dotnet (build|test|restore)\b",
    r"terraform (init|plan|validate)\b",
    r"terraform apply\b.*\s-auto-approve(\s|$)",
    r"docker (build|buildx build|compose build|compose pull)\b",
    r"kubectl (logs|describe|apply|get events|rollout status)\b",
    r"(bazel|bazelisk) (build|test)\b",
    r"cmake --build\b",
    r"(pip|pip3) install\b",
    r"uv (sync|pip install)\b",
    r"(bundle|composer) install\b",
    r"swift (build|test)\b",
    r"xcodebuild\b",
    r"flutter (test|build)\b",
]
# Never-ending or interactive forms of the above.
SKIP = [
    r"--watch",
    r"\b(tsc|jest|vitest)\b.*\s-w(\s|$)",
    r"\bkubectl logs\b.*\s(-f|--follow)(=\S*)?(\s|$)",
    r"\b(gradle|gradlew)\b.*\s(--continuous|-t)(\s|$)",
    r"^make\b(.*\s)?(dev|serve|server|run|start|watch)(\s|$)",
]
OPERATORS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "<<<", "|&", "(", ")", ">&", "<&", ";;", "&>"}
ENV_PREFIX = re.compile(r"""^((?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"[^"]*"|[^\s'"]*)\s+)*)(.*)$""", re.S)


def plan(command: str) -> tuple[str | None, str]:
    """(rewritten command or None, the reason)."""
    if os.environ.get("PARE_HOOK_DISABLE"):
        return None, "PARE_HOOK_DISABLE is set"
    cmd = command.strip()
    if not cmd:
        return None, "empty command"
    # pare merges stderr itself, so a trailing 2>&1 (which agents add out of
    # habit) is dropped rather than treated as a redirection.
    cmd = re.sub(r"\s+2>&1$", "", cmd)
    if "\n" in cmd or "`" in cmd or "$(" in cmd:
        return None, "multi-line or command substitution"
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None, "unbalanced quotes"
    if any(t in OPERATORS or (t and set(t) <= set("&|;<>()")) for t in tokens):
        return None, "compound command (operators or redirection)"
    m = ENV_PREFIX.match(cmd)
    env_part, rest = m.group(1), m.group(2)
    try:
        words = shlex.split(rest)
    except ValueError:
        return None, "unbalanced quotes"
    if not words:
        return None, "only variable assignments"
    prog = os.path.basename(words[0])
    if prog in ("pare", "cd", "sudo", "exec", "time", "nohup", "env", "timeout"):
        return None, f"{prog} is not wrapped"
    norm = " ".join([prog] + words[1:])
    for rx in SKIP:
        if re.search(rx, norm):
            return None, "watch or follow mode: never exits"
    patterns = list(WRAP)
    extra = os.environ.get("PARE_HOOK_EXTRA")
    if extra:
        patterns.append(extra)
    if not any(re.match(rx, norm) for rx in patterns):
        return None, "not a command pare wraps"
    pare_bin = os.environ.get("PARE_BIN", "pare")
    if not shutil.which(pare_bin):
        return None, f"{pare_bin} not found on PATH"
    budget = os.environ.get("PARE_HOOK_BUDGET", "").strip()
    if budget and not BUDGET_RX.fullmatch(budget):
        return None, "PARE_HOOK_BUDGET is not N, auto or auto:N"
    flag = f"--budget {budget} " if budget else ""
    return f"{env_part}{shlex.quote(pare_bin)} {flag}-- {rest}", "wrapped"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--explain":
        new, why = plan(sys.argv[2])
        print(new if new else f"unchanged ({why})")
        return 0
    try:
        event = json.load(sys.stdin)
        if event.get("hook_event_name") not in (None, "PreToolUse") or event.get("tool_name") != "Bash":
            return 0
        tool_input = event.get("tool_input") or {}
        if tool_input.get("run_in_background"):
            return 0  # read later from a file; leave as is
        new, _ = plan(tool_input.get("command", ""))
        if not new:
            return 0
        out = {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "command": new}}
        decision = os.environ.get("PARE_HOOK_DECISION", "")
        if decision in ("ask", "allow"):
            out["permissionDecision"] = decision
            out["permissionDecisionReason"] = "output summarised by pare"
        json.dump({"hookSpecificOutput": out}, sys.stdout)
        sys.stdout.write("\n")
    except Exception:  # noqa: BLE001 - a hook must never break the tool call
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
