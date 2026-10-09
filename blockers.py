#!/usr/bin/env python3
"""Deterministic environment-blocker detection for run_issues.py.

Some failures cannot be fixed by retrying: a missing SDK, a tool that is not on
PATH, a Docker daemon that is not running, absent credentials, no GPU. An agent
retried against one of these burns every attempt and fixes nothing. This module
decides, with rules rather than judgement, when a failed attempt has hit one.

    classify(text, source)  -> Blockers matched by the rule catalog
    probe(blocker, repo)    -> re-checks the environment itself:
                               True = still missing, False = actually present,
                               None = no probe for this kind
    failure_signature(...)  -> stable hash of a failure, for "same failure again"
    decide(...)             -> defer now, or retry (and with what budget)

A project may add its own rules in its config:

    "blockers": [{"kind": "EXTERNAL_SERVICE", "subject": "postgres",
                  "pattern": "could not connect to server.*5432",
                  "hint": "Start Postgres.", "severity": "hard",
                  "probe_argv": ["pg_isready", "-h", "localhost"]}]

probe_argv exiting 0 means the dependency is present (the blocker is gone).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

HARD = "hard"   # needs a human to install/configure something; retrying cannot help
SOFT = "soft"   # usually environmental, but an agent can plausibly work around it

# Kinds the agent may declare in its result block (BLOCKER: ...). UNKNOWN is soft.
DECLARABLE_KINDS = ("MISSING_TOOL", "MISSING_SDK", "PERMISSION", "CREDENTIALS",
                    "EXTERNAL_SERVICE", "PLATFORM", "HARDWARE", "UNKNOWN")

SCAN_TAIL_CHARS = 400_000      # logs can be large; blockers show up near the end
EVIDENCE_CHARS = 240


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str                 # case-insensitive; first non-empty group = subject
    severity: str
    hint: str                    # "{s}" is replaced by the subject
    subject: str = ""            # fixed subject when the pattern captures none
    probe: str = ""              # name of a builtin probe (see PROBES)
    probe_argv: tuple = ()       # custom probe: exit 0 = dependency present


@dataclass
class Blocker:
    kind: str
    subject: str
    severity: str
    hint: str
    evidence: str
    source: str                  # validation | agent-log | agent-declared
    probe: str = ""
    probe_argv: list = field(default_factory=list)
    confirmed: Optional[bool] = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.subject.lower()}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Blocker":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


_MISSING_EXE_HINT = "Install `{s}` or put it on PATH for the user the runner runs as."

BUILTIN_RULES: tuple[Rule, ...] = (
    # --- a command the environment does not have ---------------------------
    Rule("MISSING_EXECUTABLE", r"\b([\w.+-]+): command not found", HARD, _MISSING_EXE_HINT,
         probe="which"),
    Rule("MISSING_EXECUTABLE", r"'([^'\s]+)' is not recognized as an internal or external command",
         HARD, _MISSING_EXE_HINT, probe="which"),
    Rule("MISSING_EXECUTABLE", r"The term '([^'\s]+)' is not recognized as (?:the )?name of a cmdlet",
         HARD, _MISSING_EXE_HINT, probe="which"),
    Rule("MISSING_EXECUTABLE", r"\bsh: \d+: ([\w.+-]+): not found", HARD, _MISSING_EXE_HINT,
         probe="which"),
    Rule("MISSING_EXECUTABLE", r"executable not found: '([^']+)'", HARD, _MISSING_EXE_HINT,
         probe="which"),
    Rule("MISSING_EXECUTABLE", r"\bspawn ([\w.+-]+) ENOENT", HARD, _MISSING_EXE_HINT, probe="which"),
    Rule("MISSING_EXECUTABLE", r'exec: \\?"([^"\\]+)\\?": executable file not found', HARD,
         _MISSING_EXE_HINT, probe="which"),
    Rule("MISSING_EXECUTABLE", r'Cannot run program \\?"([^"\\]+)\\?"', HARD, _MISSING_EXE_HINT,
         probe="which"),
    # --- SDKs / toolchains --------------------------------------------------
    Rule("MISSING_SDK", r"SDK location not found|ANDROID_(?:HOME|SDK_ROOT)\b[^\n]{0,60}"
         r"(?:not set|invalid|does not exist|not found)", HARD,
         "Install the Android SDK and set ANDROID_HOME (or sdk.dir in local.properties).",
         subject="android-sdk", probe="android-sdk"),
    Rule("MISSING_SDK", r"Failed to find target with hash string|Failed to install the following "
         r"Android SDK packages|licen[cs]es? for (?:package|the following)[^\n]{0,80}not (?:been )?accepted",
         HARD, "Install the Android platform/build-tools the build names with sdkmanager and run "
         "`sdkmanager --licenses`.", subject="android-sdk-packages"),
    Rule("MISSING_SDK", r"JAVA_HOME is (?:not set|set to an invalid directory)|No Java runtime present"
         r"|Unable to locate a Java Runtime", HARD, "Install a JDK and set JAVA_HOME.",
         subject="java", probe="java"),
    Rule("TOOL_VERSION", r"Unsupported class file major version \d+|Android Gradle plugin requires "
         r"Java \d+|requires (?:at least )?Java \d+", HARD,
         "Install the JDK version the build requires and point JAVA_HOME at it.",
         subject="java-version"),
    Rule("MISSING_SDK", r"No \.NET SDKs were found|A compatible \.NET SDK was not found|"
         r"The \.NET SDK[^\n]{0,60}(?:could not be found|was not found)", HARD,
         "Install the .NET SDK version the project (global.json) asks for.",
         subject="dotnet-sdk", probe="dotnet"),
    Rule("MISSING_SDK", r"Flutter SDK not found|flutter\.sdk not set", HARD,
         "Install Flutter and set flutter.sdk / put flutter on PATH.", subject="flutter-sdk"),
    Rule("MISSING_SDK", r"xcode-select: error|xcrun: error: (?:invalid active developer path|"
         r"unable to find utility)", HARD, "Install the Xcode command-line tools.", subject="xcode"),
    Rule("MISSING_SDK", r"Microsoft Visual C\+\+ [\d.]+ or greater is required|gyp ERR! find VS|"
         r"MSBuild\.exe[^\n]{0,40}not found", HARD,
         "Install Visual Studio Build Tools with the C++ workload.", subject="msvc"),
    Rule("MISSING_BROWSER", r"Executable doesn't exist at [^\n]*ms-playwright|Please run the following "
         r"command to download new browsers|Could not find (?:Chrome|Chromium|expected browser)",
         HARD, "Install the browsers the tests drive (e.g. `npx playwright install`).",
         subject="test-browser"),
    Rule("MISSING_SYSTEM_LIB", r"error while loading shared libraries: ([^\s:]+)|Library not loaded: "
         r"(\S+)|Unable to load dynamic library '?([^'\s]+)|the requested PHP extension (\S+) is "
         r"missing", HARD, "Install the system library / extension `{s}`."),
    # --- hardware / platform ------------------------------------------------
    Rule("HARDWARE", r"no CUDA-capable device|CUDA driver version is insufficient|Found no NVIDIA "
         r"driver|Torch not compiled with CUDA enabled|NVIDIA-SMI has failed", HARD,
         "This needs an NVIDIA GPU with working CUDA drivers.", subject="cuda", probe="cuda"),
    Rule("PLATFORM", r"\bEBADPLATFORM\b|Unsupported platform|is not supported on (?:this platform|"
         r"windows|win32|darwin|macos|linux)", HARD,
         "This step needs a different OS; run it elsewhere or exclude the issue.",
         subject="platform"),
    # --- services / daemons -------------------------------------------------
    Rule("EXTERNAL_SERVICE", r"Cannot connect to the Docker daemon|docker daemon is not running|"
         r"error during connect:[^\n]*docker|//\./pipe/docker_engine", HARD,
         "Start Docker (Docker Desktop / dockerd).", subject="docker", probe="docker"),
    Rule("EXTERNAL_SERVICE", r"ECONNREFUSED[^\n]{0,20}?(?:127\.0\.0\.1|localhost|::1)\]?:(\d+)|"
         r"[Cc]onnection refused[^\n]{0,80}?(?:127\.0\.0\.1|localhost)\]?:(\d+)|"
         r"could not connect to server[^\n]{0,120}?port (\d+)", SOFT,
         "Start the local service on port {s} that the tests need (or add a preflight check).",
         probe="port"),
    # --- credentials / network ---------------------------------------------
    Rule("CREDENTIALS", r"Unable to locate credentials|NoCredentialsError|could not read Username for|"
         r"Permission denied \(publickey\)|gcloud auth (?:application-default )?login|"
         r"Invalid API key|\b[A-Z][A-Z0-9_]*_API_KEY\b[^\n]{0,40}(?:not set|missing|is required)",
         HARD, "Provide the credentials / API key in the runner's environment.",
         subject="credentials"),
    Rule("NETWORK", r"Could not resolve host|Temporary failure in name resolution|getaddrinfo "
         r"(?:ENOTFOUND|EAI_AGAIN)|Network is unreachable|No route to host", SOFT,
         "The runner cannot reach a network host the build needs.", subject="network"),
    # --- resources / permissions -------------------------------------------
    Rule("RESOURCE", r"No space left on device|\bENOSPC\b|There is not enough space on the disk",
         HARD, "Free disk space (or raise the file-watcher limit for ENOSPC watchers).",
         subject="disk", probe="disk"),
    Rule("PERMISSION", r"\bEACCES\b|Operation not permitted|requires (?:root|administrator|elevated) "
         r"privileges|Run as administrator", SOFT,
         "The runner's user lacks a permission the build needs.", subject="permission"),
    # --- the supervisor's own gate configuration ---------------------------
    Rule("CONFIG", r"cwd missing: (\S+)", HARD,
         "A validation command's cwd does not exist: fix the --validate config.", probe="path"),
)


def rules_from_config(entries: Sequence[dict]) -> list[Rule]:
    """Project-defined rules ("blockers" in the project config)."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("pattern"):
            continue
        re.compile(e["pattern"])           # a bad pattern fails loudly at startup
        out.append(Rule(kind=str(e.get("kind", "CUSTOM")).upper(), pattern=str(e["pattern"]),
                        severity=HARD if str(e.get("severity", HARD)).lower() == HARD else SOFT,
                        hint=str(e.get("hint", "")), subject=str(e.get("subject", "")),
                        probe_argv=tuple(str(a) for a in e.get("probe_argv", []) or ())))
    return out


def _subject(m: re.Match, rule: Rule) -> str:
    for g in m.groups():
        if g:
            s = g.strip().strip("'\"")
            if rule.probe == "which":
                s = re.split(r"[\\/]", s)[-1]        # C:\x\adb.exe -> adb.exe
            return s
    return rule.subject or rule.kind.lower()


def _evidence(text: str, m: re.Match) -> str:
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    line = text[start:end if end >= 0 else len(text)].strip()
    if len(line) > EVIDENCE_CHARS:
        mid = (m.start() - start)
        line = line[max(0, mid - 80): max(0, mid - 80) + EVIDENCE_CHARS]
    return line


def classify(text: str, source: str, extra_rules: Sequence[Rule] = ()) -> list[Blocker]:
    """Every distinct blocker the rules find in text (one per key, first match)."""
    text = (text or "")[-SCAN_TAIL_CHARS:]
    found: dict[str, Blocker] = {}
    for rule in tuple(extra_rules) + BUILTIN_RULES:   # project rules win on the same key
        for m in re.finditer(rule.pattern, text, re.IGNORECASE | re.MULTILINE):
            subj = _subject(m, rule)
            b = Blocker(kind=rule.kind, subject=subj, severity=rule.severity,
                        hint=rule.hint.replace("{s}", subj), evidence=_evidence(text, m),
                        source=source, probe=rule.probe, probe_argv=list(rule.probe_argv))
            found.setdefault(b.key, b)
    return list(found.values())


def declared_blocker(kind: str, needs: str, summary: str) -> Optional[Blocker]:
    """The blocker the agent itself reported (BLOCKER: line), if any."""
    kind = (kind or "").upper()
    if kind in ("", "NONE") or kind not in DECLARABLE_KINDS:
        return None
    needs = (needs or "").strip()
    subject = needs if needs and needs.upper() != "NONE" else kind.lower()
    return Blocker(kind=kind, subject=subject[:80], severity=SOFT if kind == "UNKNOWN" else HARD,
                   hint=f"Agent reports it needs: {subject}", evidence=summary[:EVIDENCE_CHARS],
                   source="agent-declared")


# ---------------------------------------------------------------------------
# Probes: ask the environment directly. True = blocker still present.
# ---------------------------------------------------------------------------

def _runs_ok(argv: Sequence[str], timeout: float = 20, cwd: Optional[Path] = None) -> bool:
    try:
        return subprocess.run(list(argv), cwd=str(cwd) if cwd else None,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _dir_env(*names: str) -> bool:
    return any(os.environ.get(n) and Path(os.environ[n]).is_dir() for n in names)


def _android_sdk_present(repo: Optional[Path]) -> bool:
    if _dir_env("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        return True
    for lp in ([repo / "local.properties", repo / "android" / "local.properties"] if repo else []):
        try:
            m = re.search(r"(?m)^\s*sdk\.dir\s*=\s*(.+?)\s*$", lp.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m and Path(m.group(1).replace("\\:", ":").replace("\\\\", "\\")).is_dir():
            return True
    return False


def _java_present(_repo) -> bool:
    jh = os.environ.get("JAVA_HOME")
    return Path(jh).is_dir() if jh else bool(shutil.which("java"))


def _port_open(port: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
            return True
    except (OSError, ValueError):
        return False


def _disk_ok(repo: Optional[Path]) -> bool:
    try:
        return shutil.disk_usage(str(repo or Path.cwd())).free > 1024 ** 3
    except OSError:
        return True


def probe(b: Blocker, repo: Optional[Path] = None) -> Optional[bool]:
    """Re-check b against the live environment. True = still missing."""
    if b.probe_argv:
        return not _runs_ok(b.probe_argv, cwd=repo)
    p = b.probe
    if p == "which":
        return shutil.which(b.subject) is None
    if p == "android-sdk":
        return not _android_sdk_present(repo)
    if p == "java":
        return not _java_present(repo)
    if p == "dotnet":
        exe = shutil.which("dotnet")
        if not exe:
            return True
        try:
            out = subprocess.run([exe, "--list-sdks"], capture_output=True, text=True, timeout=20)
            return not out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return True
    if p == "docker":
        exe = shutil.which("docker")
        return not (exe and _runs_ok([exe, "info"], timeout=20))
    if p == "cuda":
        exe = shutil.which("nvidia-smi")
        return not (exe and _runs_ok([exe], timeout=20))
    if p == "port":
        return not _port_open(b.subject)
    if p == "disk":
        return not _disk_ok(repo)
    if p == "path":
        return not Path(b.subject).exists()
    return None


def probe_all(blockers: list[Blocker], repo: Optional[Path] = None) -> list[Blocker]:
    """Set .confirmed on each blocker and drop those the environment disproves."""
    kept = []
    for b in blockers:
        b.confirmed = probe(b, repo)
        if b.confirmed is not False:
            kept.append(b)
    return kept


# ---------------------------------------------------------------------------
# Failure signature + the decision
# ---------------------------------------------------------------------------

_SALIENT = re.compile(r"(?i)\b(error|fail(?:ed|ure|s)?|exception|not found|denied|cannot|"
                      r"unable|missing|invalid|refused)\b")


def _normalize(line: str) -> str:
    s = line.strip().lower()
    s = re.sub(r"[a-z]:\\[^\s'\"]+|(?:/[\w.@+-]+){2,}", "<path>", s)
    s = re.sub(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{7,}\b", "<hex>", s)
    s = re.sub(r"\d+(?:\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s)[:200]


def failure_signature(status: str, validation: str, worker_reason: str,
                      blocker_keys: Sequence[str], gate_output: str = "") -> str:
    """Stable hash of what failed. Timings, paths, hashes and counts are
    normalised away, so the same failure on two attempts hashes the same.
    Only deterministic inputs (gate output, statuses) go in, never agent prose."""
    salient = sorted({_normalize(ln) for ln in (gate_output or "").splitlines()
                      if _SALIENT.search(ln)})[:300]
    parts = [status or "-", validation or "-", _normalize(worker_reason or ""),
             *sorted(blocker_keys), *salient]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


WATCHDOG_REASON = re.compile(r"session cap|no output for")


@dataclass
class Decision:
    defer: bool
    reason: str
    primary: Optional[Blocker] = None      # the blocker that caused the deferral, if any
    max_attempts: Optional[int] = None     # tighter retry budget for this issue
    feedback: str = ""                     # extra text for the agent's next prompt


def decide(*, status: str, blockers: list[Blocker], declared: Optional[Blocker],
           previous_keys: Sequence[str], known_keys: dict[str, list[int]],
           signature: str, previous_signature: str, tree: str, previous_tree: str,
           worker_reason: str) -> Decision:
    """Whether a failed attempt should be retried. `blockers` must already be
    probed (probe_all): anything the live environment disproves is gone.

    Defer now when:
      1. the agent declares BLOCKED with a concrete (non-UNKNOWN) blocker;
      2. the supervisor's own gate run hit a hard blocker;
      3. a hard blocker already parked another issue in this run;
      4. the same blocker shows up on two consecutive attempts (hard, or
         soft but confirmed by a probe);
      5. the agent says BLOCKED and the log carries a hard blocker;
      6. the failure is identical to the last attempt and the tree did not change.
    Otherwise retry, with a tighter budget for watchdog kills and vague BLOCKED.
    """
    hard = [b for b in blockers if b.severity == HARD]
    by_key = {b.key: b for b in blockers}

    if status == "BLOCKED" and declared and declared.severity == HARD:
        return Decision(True, f"agent declared BLOCKED ({declared.kind}): {declared.subject}",
                        primary=hard[0] if hard else declared)
    for b in hard:
        if b.source == "validation":
            return Decision(True, f"supervisor gate hit {b.kind} {b.subject}: {b.evidence}", b)
    for b in hard:
        if b.key in known_keys:
            seen = ", ".join(f"#{n}" for n in known_keys[b.key])
            return Decision(True, f"known environment blocker {b.key} (already parked {seen})", b)
    for key in previous_keys:
        b = by_key.get(key)
        if b and (b.severity == HARD or b.confirmed is True):
            return Decision(True, f"{b.kind} {b.subject} blocked two attempts in a row", b)
    if status == "BLOCKED" and hard:
        return Decision(True, f"agent BLOCKED; log shows {hard[0].kind} {hard[0].subject}", hard[0])
    if previous_signature and signature == previous_signature and tree == previous_tree:
        return Decision(True, "no progress: identical failure and an unchanged worktree "
                              "on consecutive attempts")

    feedback = ""
    if blockers:
        feedback = ("The supervisor detected possible environment blockers:\n"
                    + "\n".join(f"- {b.kind} {b.subject}: {b.evidence}" for b in blockers)
                    + "\nIf the issue cannot be finished without installing or configuring "
                      "something outside this repository, stop and report STATUS: BLOCKED "
                      "with the matching BLOCKER: and NEEDS: lines. Do not retry the same "
                      "command hoping it now works.")
    if WATCHDOG_REASON.search(worker_reason or ""):
        return Decision(False, "watchdog killed the worker", max_attempts=2, feedback=feedback)
    if status == "BLOCKED":
        return Decision(False, "agent BLOCKED without a concrete blocker", max_attempts=2,
                        feedback=feedback)
    return Decision(False, "ordinary failure", feedback=feedback)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> list[str]:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"{'PASS' if cond else 'FAIL'}  blockers: {name}")
        if not cond:
            failures.append(f"blockers: {name}")

    def keys(text: str) -> set[str]:
        return {b.key for b in classify(text, "agent-log")}

    check("bash command not found", keys("bash: adb: command not found") == {"MISSING_EXECUTABLE:adb"})
    check("cmd not recognized", "MISSING_EXECUTABLE:flutter" in keys(
        "'flutter' is not recognized as an internal or external command,"))
    check("powershell not recognized", "MISSING_EXECUTABLE:sdkmanager" in keys(
        "The term 'sdkmanager' is not recognized as the name of a cmdlet, function"))
    check("node spawn ENOENT", "MISSING_EXECUTABLE:java" in keys("Error: spawn java ENOENT"))
    check("supervisor exe-not-found message",
          "MISSING_EXECUTABLE:bash" in keys("FAIL: lint (executable not found: 'bash')"))
    check("go exec inside JSON escapes", "MISSING_EXECUTABLE:protoc" in keys(
        '{"out":"exec: \\"protoc\\": executable file not found in $PATH"}'))
    check("android sdk", "MISSING_SDK:android-sdk" in keys(
        "> SDK location not found. Define a valid SDK location with an ANDROID_HOME"))
    check("java home", "MISSING_SDK:java" in keys(
        "ERROR: JAVA_HOME is set to an invalid directory: C:\\nope"))
    check("docker daemon", "EXTERNAL_SERVICE:docker" in keys(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."))
    check("port refused captures port", "EXTERNAL_SERVICE:5432" in keys(
        "connect ECONNREFUSED 127.0.0.1:5432"))
    check("cuda", "HARDWARE:cuda" in keys("RuntimeError: Found no NVIDIA driver on your system."))
    check("php extension", "MISSING_SYSTEM_LIB:ext-gd" in keys(
        "the requested PHP extension ext-gd is missing from your system"))
    check("plain test failure is not a blocker",
          keys("FAILED tests/test_x.py::test_a - AssertionError: 2 != 3") == set())
    check("db_reset/reset noise is not a blocker", keys("php tools/db_reset.php OK") == set())

    custom = rules_from_config([{"kind": "EXTERNAL_SERVICE", "subject": "redis",
                                 "pattern": r"Error 111 connecting to localhost:6379",
                                 "probe_argv": ["__no_such_exe__"]}])
    cb = classify("redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379", "x", custom)
    check("custom rule matches", [b.key for b in cb] == ["EXTERNAL_SERVICE:redis"])
    check("custom probe failing confirms blocker", probe(cb[0]) is True)

    present = Blocker("MISSING_EXECUTABLE", Path(sys.executable).name, HARD, "", "", "agent-log",
                      probe="which")
    check("which probe disproves a present tool", probe(present) is False
          or shutil.which(present.subject) is None)
    check("which probe confirms a missing tool", probe(Blocker(
        "MISSING_EXECUTABLE", "__definitely_missing_tool__", HARD, "", "", "x", probe="which")) is True)

    sig = lambda gate: failure_signature("COMPLETE", "PASS", "worker exited normally", [], gate)
    check("signature ignores timings/paths",
          sig("FAIL: tests (exit 1)\nError at /home/a/b/c.py line 12 after 3.2s")
          == sig("FAIL: tests (exit 1)\nError at /tmp/x/y/z.py line 40 after 9.9s"))
    check("signature tells different failures apart",
          sig("FAIL: tests (exit 1)\nAssertionError") != sig("FAIL: lint (exit 1)\nE501"))

    def d(**kw) -> Decision:
        base = dict(status="INCOMPLETE", blockers=[], declared=None, previous_keys=[],
                    known_keys={}, signature="s2", previous_signature="s1", tree="t2",
                    previous_tree="t1", worker_reason="worker exited normally")
        base.update(kw)
        return decide(**base)

    sdk = Blocker("MISSING_SDK", "android-sdk", HARD, "h", "SDK location not found", "agent-log")
    gate = Blocker("MISSING_EXECUTABLE", "npm", HARD, "h", "executable not found", "validation")
    port = Blocker("EXTERNAL_SERVICE", "5432", SOFT, "h", "ECONNREFUSED", "agent-log", confirmed=True)
    check("declared hard BLOCKED defers", d(status="BLOCKED", declared=declared_blocker(
        "MISSING_SDK", "Android SDK", "")).defer)
    check("declared UNKNOWN BLOCKED retries once more",
          (lambda x: not x.defer and x.max_attempts == 2)(d(status="BLOCKED", declared=declared_blocker(
              "UNKNOWN", "", ""))))
    check("gate-side hard blocker defers", d(blockers=[gate]).defer)
    check("log-only hard blocker gets one more try", not d(blockers=[sdk]).defer
          and "BLOCKED" in d(blockers=[sdk]).feedback)
    check("same hard blocker twice defers", d(blockers=[sdk], previous_keys=[sdk.key]).defer)
    check("known run-wide blocker defers at once",
          d(blockers=[sdk], known_keys={sdk.key: [3]}).defer)
    check("confirmed soft blocker twice defers", d(blockers=[port], previous_keys=[port.key]).defer)
    check("BLOCKED + hard log evidence defers", d(status="BLOCKED", blockers=[sdk]).defer)
    check("identical failure, unchanged tree defers",
          d(signature="s", previous_signature="s", tree="t", previous_tree="t").defer)
    check("identical failure but tree changed retries",
          not d(signature="s", previous_signature="s", tree="t2", previous_tree="t").defer)
    check("watchdog kill gets budget 2",
          d(worker_reason="no output for 20 minutes").max_attempts == 2)
    check("ordinary failure keeps the default budget",
          (lambda x: not x.defer and x.max_attempts is None)(d()))
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if self_test() else 0)
