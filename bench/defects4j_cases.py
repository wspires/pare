#!/usr/bin/env python3
"""Held-out cases from Defects4J: real failing Java test runs, labelled by the dataset.

Defects4J records, for each bug, the commit that fixed it, a patch that
reverts only the fix (patches/<id>.src.patch), and the tests that fail on
the buggy version together with the exception each one raises
(trigger_tests/<id>). This script rebuilds the buggy version from the
upstream project on GitHub (the fixed commit plus the revert patch, as
Defects4J's own checkout does), runs the failing test classes with the
project's Maven build in a JDK 8 container, and writes a case whose keep
list is those tests and their exceptions, as they appear in the output:

    NAME.log   the Maven run's output
    NAME.cmd   the Maven command
    NAME.keep  each trigger test's id and its exception line, in the form the log shows
    NAME.json  bug metadata, what was located, what was not

Cases go to corpus-heldout/defects4j/ with a MANIFEST.tsv line whose source
is "dataset". Nobody's judgement is involved, so the labels are blind to
pare by construction. Report them on their own:
`bench/run.py --corpus corpus-heldout/defects4j`.

Usage
    python bench/defects4j_cases.py run --sample 20 [--seed 0] [--dry-run]
    python bench/defects4j_cases.py run --bugs Lang-1,Codec-3,Jsoup-10

Needs git and Docker; not Defects4J's own installation. The Defects4J
metadata (bug lists, patches, trigger tests) is cloned from GitHub into
~/.cache/pare/defects4j on first use, or read from --defects4j DIR. Maven
projects only for now: Lang, Math, Codec, Collections, Compress, Csv, Cli,
JxPath, Gson, JacksonCore, JacksonDatabind, JacksonXml, Jsoup, Time.
Closure and Chart (Ant) and Mockito (Gradle) are not covered yet.

Differences from Defects4J's own checkout, recorded in each case's JSON:
the per-project post-checkout hooks and the removal of tests known to be
broken are not applied, and Maven runs the project's own build rather than
Defects4J's Ant wrapper. A bug whose build does not run, or whose trigger
tests do not fail, is skipped and reported.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import collect  # noqa: E402  (manifest helpers, pare version)

DEFECTS4J_GIT = "https://github.com/rjust/defects4j"
DEFAULT_META = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
                            "pare", "defects4j")
DEFAULT_OUT = os.path.join(ROOT, "corpus-heldout", "defects4j")
DEFAULT_IMAGE = "maven:3.9-eclipse-temurin-8"
M2_VOLUME = "pare-defects4j-m2"
START, EXIT = ">>>>> pare start", ">>>>> pare exit"

# Defects4J project -> upstream repository whose history its revision ids come from.
MAVEN_PROJECTS = {
    "Lang": "https://github.com/apache/commons-lang",
    "Math": "https://github.com/apache/commons-math",
    "Codec": "https://github.com/apache/commons-codec",
    "Collections": "https://github.com/apache/commons-collections",
    "Compress": "https://github.com/apache/commons-compress",
    "Csv": "https://github.com/apache/commons-csv",
    "Cli": "https://github.com/apache/commons-cli",
    "JxPath": "https://github.com/apache/commons-jxpath",
    "Gson": "https://github.com/google/gson",
    "JacksonCore": "https://github.com/FasterXML/jackson-core",
    "JacksonDatabind": "https://github.com/FasterXML/jackson-databind",
    "JacksonXml": "https://github.com/FasterXML/jackson-dataformat-xml",
    "Jsoup": "https://github.com/jhy/jsoup",
    "Time": "https://github.com/JodaOrg/joda-time",
}
NOT_COVERED = {"Closure": "Ant build", "Chart": "Subversion and Ant", "Mockito": "Gradle build"}

# Maven flags: no plugin that is not about compiling and testing, and test
# failures printed to the console (old Surefire versions write them only to
# report files, newer ones print them too).
MVN_FLAGS = ["-B", "-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false", "-Dsurefire.useFile=false",
             "-Drat.skip=true", "-Djacoco.skip=true", "-Danimal.sniffer.skip=true", "-Dcheckstyle.skip=true",
             "-Denforcer.skip=true", "-Dmaven.javadoc.skip=true", "-Djapicmp.skip=true", "-Dclirr.skip=true",
             "-Dbuildnumber.skip=true", "-Dspotbugs.skip=true", "-Dfindbugs.skip=true", "-Dpmd.skip=true"]
# Every repository through Maven Central over https: Maven 3.8+ refuses the
# http:// repositories that old parent poms declare.
SETTINGS_XML = """<settings>
  <mirrors>
    <mirror><id>central-https</id><mirrorOf>*</mirrorOf><url>https://repo.maven.apache.org/maven2</url></mirror>
  </mirrors>
</settings>
"""


# --------------------------------------------------------------------------
# Defects4J metadata
# --------------------------------------------------------------------------

SPARSE = ["/framework/projects/*/active-bugs.csv", "/framework/projects/*/patches/",
          "/framework/projects/*/trigger_tests/", "/framework/projects/*/compile-errors/"]
SPARSE_MARK = ".pare-sparse-2"


def ensure_metadata(path: str) -> str:
    """A sparse, blob-less clone of Defects4J holding only what is needed:
    bug lists, patches, trigger tests and compile-error fixes."""
    if os.path.exists(os.path.join(path, SPARSE_MARK)) or (
            os.path.isdir(os.path.join(path, "framework", "projects")) and not os.path.isdir(os.path.join(path, ".git"))):
        return path  # ours and current, or a full Defects4J checkout
    steps = []
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        print(f"cloning Defects4J metadata into {path}", flush=True)
        steps += [["git", "clone", "-q", "--filter=blob:none", "--no-checkout", "--depth", "1", DEFECTS4J_GIT, path],
                  ["git", "-C", path, "sparse-checkout", "init", "--no-cone"]]
    steps += [["git", "-C", path, "sparse-checkout", "set", *SPARSE], ["git", "-C", path, "checkout", "-q"]]
    for cmd in steps:
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit(f"defects4j_cases: {' '.join(cmd[:3])} failed: {p.stderr.strip()[-300:]}")
    open(os.path.join(path, SPARSE_MARK), "w").close()
    return path


def bugs_of(meta: str, project: str) -> list[dict]:
    path = os.path.join(meta, "framework", "projects", project, "active-bugs.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [{"project": project, "bug": int(r["bug.id"]), "fixed": r["revision.id.fixed"],
                 "buggy": r["revision.id.buggy"], "report": r.get("report.url", "")}
                for r in csv.DictReader(f)]


def trigger_tests(meta: str, project: str, bug: int) -> list[dict]:
    """[{class, method, exception}] from trigger_tests/<id>: a `--- cls::m`
    header, then the exception line, then its stack trace."""
    path = os.path.join(meta, "framework", "projects", project, "trigger_tests", str(bug))
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("--- ") and "::" in ln:
            cls, method = ln[4:].strip().split("::", 1)
            exc = lines[i + 1].strip() if i + 1 < len(lines) and not lines[i + 1].startswith("--- ") else ""
            out.append({"class": cls, "method": method, "exception": exc})
    return out


def parse_bug_ids(spec: str) -> list[tuple[str, int]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z]+)-(\d+)", part)
        if not m:
            sys.exit(f"defects4j_cases: {part!r} is not a bug id like Lang-1")
        out.append((m.group(1), int(m.group(2))))
    return out


def select(meta: str, *, ids: list[tuple[str, int]] | None, sample: int, seed: int, max_fail: int) -> list[dict]:
    """The requested bugs in order, or a seeded sample spread across projects
    (round robin) among bugs with 1..max_fail trigger tests."""
    if ids:
        chosen = []
        for project, bug in ids:
            if project not in MAVEN_PROJECTS:
                sys.exit(f"defects4j_cases: {project} is not covered ({NOT_COVERED.get(project, 'unknown project')})")
            found = [b for b in bugs_of(meta, project) if b["bug"] == bug]
            if not found:
                sys.exit(f"defects4j_cases: {project}-{bug} is not an active Defects4J bug")
            chosen.append({**found[0], "triggers": trigger_tests(meta, project, bug)})
        return chosen
    rng = random.Random(seed)
    pools: dict[str, list[dict]] = {}
    for project in sorted(MAVEN_PROJECTS):
        pool = []
        for b in sorted(bugs_of(meta, project), key=lambda b: b["bug"]):
            t = trigger_tests(meta, project, b["bug"])
            if 1 <= len(t) <= max_fail:
                pool.append({**b, "triggers": t})
        rng.shuffle(pool)
        if pool:
            pools[project] = pool
    picked: list[dict] = []
    while len(picked) < sample and any(pools.values()):
        for project in sorted(pools):
            if pools[project] and len(picked) < sample:
                picked.append(pools[project].pop())
    return picked


# --------------------------------------------------------------------------
# Buggy version, build, run
# --------------------------------------------------------------------------

def compile_fixes(meta: str, project: str, bug: int) -> list[str]:
    """Defects4J's compile-errors/<name>-<from>-<to>.diff patches whose bug
    range covers this bug: fixes that let old revisions compile on Java 8,
    applied to the fixed revision before the fix is reverted."""
    d = os.path.join(meta, "framework", "projects", project, "compile-errors")
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        m = re.search(r"-(\d+)-(\d+)\.diff$", f)
        if m and int(m.group(1)) <= bug <= int(m.group(2)):
            out.append(os.path.join(d, f))
    return out


def lang_enum_rename(work: str) -> bool:
    """Defects4J's Lang hook: commons-lang 2.x has a package named `enum`,
    a reserved word since Java 5; rename it to `oldenum` in sources and tests."""
    changed = False
    for root in ("src/java", "src/main/java", "src/test", "src/test/java"):
        pkg = os.path.join(work, root, "org", "apache", "commons", "lang", "enum")
        if not os.path.isdir(pkg):
            continue
        for dirpath, _dirs, files in os.walk(os.path.join(work, root)):
            for f in files:
                if f.endswith(".java"):
                    path = os.path.join(dirpath, f)
                    text = open(path, encoding="latin-1").read()
                    new = re.sub(r"\.enum([;.])", r".oldenum\1", text)
                    if new != text:
                        open(path, "w", encoding="latin-1").write(new)
        os.rename(pkg, os.path.join(os.path.dirname(pkg), "oldenum"))
        changed = True
    return changed


def prepare(work: str, bug: dict, meta: str, upstream: str, applied: list[str] | None = None) -> str | None:
    """Rebuild the buggy version in `work` as Defects4J's checkout does: the
    fixed commit, its compile-error fixes, then the patch that reverts the
    fix. Returns the reason it failed, or None. `applied` collects the
    fixes used."""
    applied = applied if applied is not None else []
    git = lambda *a: subprocess.run(["git", "-C", work, *a], capture_output=True, text=True)  # noqa: E731
    os.makedirs(work, exist_ok=True)
    git("init", "-q")
    p = git("fetch", "-q", "--depth", "1", upstream, bug["fixed"])
    if p.returncode != 0:
        return "fixed revision not found upstream"
    if git("checkout", "-q", "FETCH_HEAD").returncode != 0:
        return "checkout failed"
    for fix in compile_fixes(meta, bug["project"], bug["bug"]):
        if git("apply", "--whitespace=nowarn", fix).returncode != 0:
            return f"compile-error fix {os.path.basename(fix)} did not apply"
        applied.append(os.path.basename(fix))
    if bug["project"] == "Lang" and lang_enum_rename(work):
        applied.append("Lang enum package renamed")
    patch = os.path.join(meta, "framework", "projects", bug["project"], "patches", f"{bug['bug']}.src.patch")
    if not os.path.exists(patch):
        return "no source patch in the metadata"
    if git("apply", "--whitespace=nowarn", patch).returncode != 0:
        return "revert patch did not apply"
    if not os.path.exists(os.path.join(work, "pom.xml")):
        return "no Maven build at this revision"
    return None


def mvn_command(bug: dict) -> list[str]:
    # Simple class names: Surefire 2.x matches -Dtest against file names, and
    # does not accept fully qualified names before 2.19.
    classes = sorted({t["class"].rsplit(".", 1)[-1] for t in bug["triggers"]})
    return ["mvn", *MVN_FLAGS, f"-Dtest={','.join(classes)}", "test"]


class Docker:
    """Runs the build in a container; a test replaces it."""

    def __init__(self, image: str, extra_args: list[str], pass_env: list[str]):
        self.image, self.extra_args, self.pass_env = image, extra_args, pass_env

    def ensure_image(self) -> tuple[bool, str]:
        if subprocess.run(["docker", "image", "inspect", self.image], capture_output=True).returncode == 0:
            return True, ""
        p = subprocess.run(["docker", "pull", "-q", self.image], capture_output=True, text=True)
        return p.returncode == 0, (p.stderr or p.stdout).strip()[-300:]

    def run(self, work: str, command: list[str], *, timeout: int) -> tuple[str, bool]:
        """Run the command twice in the checkout: once to download plugins and
        dependencies (output discarded, so the case does not depend on what
        the local Maven cache happened to hold), once for the case."""
        with tempfile.TemporaryDirectory(prefix="pare-d4j-") as d:
            with open(os.path.join(d, "settings.xml"), "w") as f:
                f.write(SETTINGS_XML)
            cmd = " ".join(shlex.quote(c) for c in command[:1] + ["-s", "/pare/settings.xml", "-Dmaven.repo.local=/m2"] + command[1:])
            script = "\n".join([
                "cd /work",
                f"{cmd} > /dev/null 2>&1",
                f"echo '{START}'",
                f"{cmd} 2>&1",
                f"echo \"{EXIT} $?\"",
                f"chown -R {os.getuid()}:{os.getgid()} /work 2>/dev/null || true",
            ])
            with open(os.path.join(d, "run.sh"), "w") as f:
                f.write(script + "\n")
            name = f"pare-d4j-{uuid.uuid4().hex[:12]}"
            args = ["docker", "run", "--rm", "--name", name, "-v", f"{work}:/work", "-v", f"{d}:/pare:ro",
                    "-v", f"{M2_VOLUME}:/m2", *self.extra_args]
            for var in self.pass_env:
                if var in os.environ:
                    args += ["-e", f"{var}={os.environ[var]}"]
            args += ["--entrypoint", "/bin/bash", self.image, "/pare/run.sh"]
            try:
                p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
                return p.stdout.decode("utf-8", "replace"), False
            except subprocess.TimeoutExpired as e:
                subprocess.run(["docker", "kill", name], capture_output=True)
                return (e.stdout or b"").decode("utf-8", "replace"), True


def split_output(log: str) -> tuple[str, int | None]:
    """The second run's output and its exit status."""
    if START not in log:
        return "", None
    body = log.split(START, 1)[1].lstrip("\n")
    m = re.search(rf"^{re.escape(EXIT)} (\d+)\s*$", body, re.M)
    if not m:
        return body, None
    return body[: m.start()], int(m.group(1))


def test_id_forms(cls: str, method: str) -> list[str]:
    """How Surefire versions print a failing test: `m(pkg.Cls)` (2.x),
    `pkg.Cls.m` and `Cls.m` (3.x), or Defects4J's own `pkg.Cls::m`."""
    simple = cls.rsplit(".", 1)[-1]
    return [f"{method}({cls})", f"{cls}.{method}", f"{simple}.{method}", f"{cls}::{method}"]


def locate_exception(exc: str, output: str) -> str | None:
    if exc in output:
        return exc
    if len(exc) > 80 and exc[:80] in output:
        return exc[:80]
    _cls, sep, message = exc.partition(": ")
    message = message.strip()
    if sep and len(message) >= 12 and message in output:
        return message
    return None


FAILURES = re.compile(r"Tests run: \d+, Failures: (\d+), Errors: (\d+)")


def labels(bug: dict, output: str) -> tuple[list[str], list[dict], list[str]]:
    """(keep lines, trigger tests that failed, what could not be located).
    A trigger test counts as failed when its id is in the output: Surefire
    names methods only when they fail or error. Its exception line is kept
    when it appears verbatim, or its first 80 characters do, or else its
    message alone: Defects4J recorded failures under a JUnit 3-style Ant
    runner (junit.framework.AssertionFailedError), and Maven's JUnit 4
    runner prints the same message under another class
    (java.lang.AssertionError)."""
    keep, failed, unlocated = [], [], []
    for t in bug["triggers"]:
        form = next((f for f in test_id_forms(t["class"], t["method"]) if f in output), None)
        if form is None:
            unlocated.append(f"{t['class']}::{t['method']}")
            continue
        failed.append(t)
        if form not in keep:
            keep.append(form)
        exc = t["exception"]
        if exc:
            located = locate_exception(exc, output)
            if located and located not in keep:
                keep.append(located)
            elif not located:
                unlocated.append(f"exception: {exc[:100]}")
    return keep, failed, unlocated


def make_case(bug: dict, docker: Docker, args) -> tuple[str, dict]:
    name = f"{bug['project']}-{bug['bug']}"
    upstream = args.upstream.get(bug["project"], MAVEN_PROJECTS[bug["project"]])
    work = tempfile.mkdtemp(prefix=f"pare-d4j-{name}-")
    details: dict = {}
    applied: list[str] = []
    try:
        reason = prepare(work, bug, args.defects4j, upstream, applied)
        if reason:
            return reason, details
        command = mvn_command(bug)
        t0 = time.time()
        log, timed_out = docker.run(work, command, timeout=args.timeout)
        details["seconds"] = round(time.time() - t0, 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if timed_out:
        save_failed(args.out, name, log)
        return "timed out", details
    output, code = split_output(log)
    if code is None:
        save_failed(args.out, name, log)
        return "build did not run", details
    counts = [(int(a), int(b)) for a, b in FAILURES.findall(output)]
    if not counts:
        save_failed(args.out, name, log)
        return "no tests ran (build failed?)", details
    keep, failed, unlocated = labels(bug, output)
    if not failed:
        save_failed(args.out, name, log)
        return "trigger tests did not fail", {**details, "unlocated": len(unlocated)}
    nlines = output.count("\n")
    if nlines < args.min_lines:
        return f"under {args.min_lines} lines", {**details, "lines": nlines}
    base = os.path.join(args.out, name)
    with open(base + ".log", "w", encoding="utf-8") as f:
        f.write(output)
    with open(base + ".keep", "w", encoding="utf-8") as f:
        f.writelines(k + "\n" for k in keep)
    shown = " ".join(c for c in command if not c.startswith("-D") or c.startswith("-Dtest="))
    with open(base + ".cmd", "w", encoding="utf-8") as f:
        f.write(shown + "\n")
    meta = {
        "bug": name, "project": bug["project"], "bug_id": bug["bug"], "fixed_revision": bug["fixed"],
        "report": bug["report"], "upstream": upstream, "image": docker.image, "command": command,
        "exit": code, "triggers": bug["triggers"], "failed": [f"{t['class']}::{t['method']}" for t in failed],
        "keep": keep, "unlocated": unlocated, "lines": nlines, "seconds": details.get("seconds"),
        "fixes_applied": applied,
        "differences_from_defects4j": ["only the compile-error fixes of the post-checkout hook (and Lang's enum "
                                       "rename) are applied", "known-broken tests are not removed",
                                       "the project's Maven build instead of Defects4J's Ant wrapper"],
        "collected": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pare": collect.pare_version(), "commit": collect.git_commit(),
    }
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    collect.append_manifest(args.out, [
        name, meta["collected"], meta["pare"], meta["commit"], "", str(nlines), str(len(output.encode())),
        str(code), shown, "dataset", "dataset trigger tests"])
    return "written", {**details, "lines": nlines, "keep": len(keep), "unlocated": len(unlocated)}


def save_failed(out: str, name: str, log: str) -> None:
    d = os.path.join(out, "_failed")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name + ".run.log"), "w", encoding="utf-8") as f:
        f.write(log)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_run(args, docker: Docker | None = None) -> int:
    ensure_metadata(args.defects4j)
    ids = parse_bug_ids(args.bugs) if args.bugs else None
    chosen = select(args.defects4j, ids=ids, sample=args.sample, seed=args.seed, max_fail=args.max_fail_tests)
    print(f"{len(chosen)} bug(s), {len({b['project'] for b in chosen})} projects")
    if args.dry_run:
        for b in chosen:
            print(f"  {b['project'] + '-' + str(b['bug']):<20} {len(b['triggers'])} trigger test(s)  "
                  f"{' '.join(sorted({t['class'].rsplit('.', 1)[-1] for t in b['triggers']}))}")
        return 0
    docker = docker or Docker(args.image, args.docker_arg, args.pass_env)
    ok, msg = docker.ensure_image()
    if not ok:
        sys.exit(f"defects4j_cases: cannot pull {args.image}: {msg}")
    os.makedirs(args.out, exist_ok=True)
    counts: dict[str, int] = {}
    for n, bug in enumerate(chosen, 1):
        name = f"{bug['project']}-{bug['bug']}"
        if os.path.exists(os.path.join(args.out, name + ".log")) and not args.force:
            status, details = "already collected", {}
        else:
            status, details = make_case(bug, docker, args)
        counts[status] = counts.get(status, 0) + 1
        extra = ", ".join(f"{k} {v}" for k, v in details.items())
        print(f"[{n}/{len(chosen)}] {name:<20} {status}" + (f"  ({extra})" if extra else ""), flush=True)
    print("; ".join(f"{v} {k}" for k, v in counts.items()))
    return 0 if counts.get("written", 0) or counts.get("already collected", 0) else 1


def main(argv: list[str] | None = None, docker: Docker | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="collect cases from Defects4J bugs")
    p.add_argument("--defects4j", default=DEFAULT_META, help=f"Defects4J checkout or metadata clone (default {DEFAULT_META})")
    p.add_argument("--bugs", help="comma-separated bug ids like Lang-1,Codec-3 (default: a seeded sample)")
    p.add_argument("--sample", type=int, default=20, help="bugs to sample, spread across projects (default 20)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-fail-tests", type=int, default=5, help="skip bugs with more trigger tests than this when sampling")
    p.add_argument("--min-lines", type=int, default=40, help="skip outputs shorter than this (default 40)")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per bug, both runs (default 1800)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help=f"JDK and Maven image (default {DEFAULT_IMAGE})")
    p.add_argument("--docker-arg", action="append", default=[], metavar="ARG",
                   help="extra argument for docker run, repeatable (e.g. --docker-arg=--network=host)")
    p.add_argument("--pass-env", action="append", default=[], metavar="VAR",
                   help="pass this environment variable into the container, repeatable (e.g. MAVEN_OPTS for a proxy)")
    p.add_argument("--upstream", action="append", default=[], metavar="PROJECT=URL",
                   help="clone a project from this URL instead (a mirror, or a local path)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--force", action="store_true", help="redo bugs that already have a case")
    p.add_argument("--dry-run", action="store_true", help="show the selection and stop")
    p.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    upstream = {}
    for spec in args.upstream:
        project, _, url = spec.partition("=")
        if project not in MAVEN_PROJECTS or not url:
            sys.exit(f"defects4j_cases: --upstream expects PROJECT=URL with a covered project; got {spec!r}")
        upstream[project] = url
    args.upstream = upstream
    return args.func(args, docker)


if __name__ == "__main__":
    sys.exit(main())
