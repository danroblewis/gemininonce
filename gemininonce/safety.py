"""Rough safety rails for gemininonce: none of this is a guarantee, it's defense in depth.

1. sandbox_argv(): run the test command with no internet, writes confined to the project + temp dirs,
   and credential stores unreadable (macOS sandbox-exec, Linux bwrap).
2. scan_edit()/scan_command(): flag dangerous things in lines Gemini *added* (deletes, system paths,
   exfiltration, obfuscation, persistence, publishing, secrets) + new Bandit findings for Python.
3. find_secrets()/redact(): keep secrets out of what we send to Gemini.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HOME = Path.home()

# --- Pattern checks -----------------------------------------------------------------------------
# (severity, label, regex). "high" needs the user's OK; "low" is just shown.
RISKS = [
    ("high", "recursive/forced delete", r"\brm\s+-[a-zA-Z]*[rRf]|shutil\.rmtree|rimraf|\brmdir\s+/s|"
                                        r"fs\.(rm|rmdir)(Sync)?\([^)]*recursive|Remove-Item\b.*-Recurse|\bdel\s+/[sq]"),
    ("high", "disk/partition wipe", r"\bmkfs\b|\bdd\s+if=|\bdiskutil\s+(erase|partition)|\bformat\s+[a-z]:"),
    ("high", "destructive SQL", r"(?i)\b(drop\s+(table|database|schema)|truncate\s+table)\b"),
    ("high", "destructive git", r"git\s+(push\b.*(--force|-f\b)|reset\s+--hard|clean\s+-[a-z]*f|filter-branch|"
                                r"update-ref\s+-d)"),
    ("high", "system path", r"""['"`](/etc|/usr|/bin|/sbin|/System|/Library|/private|/var|/boot|/dev/sd|"""
                            r"""C:\\\\Windows|C:\\\\Program Files)(/|\\\\|['"`])"""),
    ("high", "home directory / credentials", r"""~/|\$HOME|%USERPROFILE%|expanduser\(|Path\.home\(\)|os\.homedir\(|"""
                                             r"""\.ssh\b|\.aws\b|\.gnupg|id_rsa|id_ed25519|\.kube/config|\.netrc|"""
                                             r"""Keychains?\b|Login Data|/Cookies\b|\.npmrc|\.pypirc|\.docker/config"""),
    ("high", "download-and-execute", r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b|iex\s*\(|Invoke-Expression|"
                                     r"DownloadString|Invoke-WebRequest.*\|\s*iex"),
    ("high", "exfiltration endpoint", r"(?i)hooks\.slack\.com|discord(app)?\.com/api/webhooks|api\.telegram\.org|"
                                      r"pastebin\.com|ngrok\.io|requestbin|webhook\.site|transfer\.sh|"
                                      r"https?://\d{1,3}(\.\d{1,3}){3}(?![\d.])(?<!127\.0\.0\.1)"),
    ("high", "obfuscated code", r"(?:exec|eval)\s*\(\s*(?:base64|codecs|zlib|marshal|bytes\.fromhex|atob|"
                                r"Buffer\.from)|marshal\.loads|__import__\(\s*['\"](?:os|subprocess|socket)|"
                                r"String\.fromCharCode\((?:\s*\d+\s*,){8}|(?:\\x[0-9a-fA-F]{2}){16}|"
                                r"['\"][A-Za-z0-9+/]{200,}={0,2}['\"]"),
    ("high", "persistence / privilege", r"\bcrontab\b|LaunchAgents|LaunchDaemons|systemctl\s+(enable|start)|"
                                        r"\.bashrc|\.zshrc|\.bash_profile|[~/'\"]\.profile\b|authorized_keys|\bsudo\s|"
                                        r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]{2}\s+/|\bchmod\s+[ug]\+s|\bsetuid\b|"
                                        r"schtasks|HKEY_(LOCAL_MACHINE|CURRENT_USER)\\\\.*\\\\Run"),
    ("high", "publishes / acts as you", r"\bgit\s+(push|commit|tag)\b|\bnpm\s+publish|\btwine\s+upload|"
                                        r"\bpoetry\s+publish|\bcargo\s+publish|\bdocker\s+push|\bgh\s+(pr|issue|"
                                        r"release|repo|api)\b|smtplib|nodemailer|sendmail"),
    ("high", "crypto miner", r"(?i)stratum\+tcp|xmrig|coinhive|cryptonight|minexmr|nicehash"),
    ("high", "fork bomb", r":\(\)\s*\{\s*:\|:&\s*\};:|os\.fork\(\)\s*(while|for)|while\s+True:\s*os\.fork"),
    ("low", "runs shell commands", r"os\.system|subprocess\.|child_process|Runtime\.getRuntime\(\)\.exec|"
                                   r"shell\s*=\s*True|\bpopen\b|execSync|spawnSync|`[^`]*\$\("),
    ("low", "dynamic code execution", r"(?<![\w.])(eval|exec)\s*\(|new\s+Function\(|pickle\.loads|yaml\.load\((?!.*Loader)"),
    ("low", "network access", r"requests\.(get|post|put|delete|patch)|urllib\.request|http\.client|\bfetch\(|"
                              r"axios\.|XMLHttpRequest|socket\.socket|websocket|net\.connect|https?\.request\("),
    ("low", "deletes files", r"os\.(remove|unlink)\(|\.unlink\(|fs\.(unlink|rm)(Sync)?\(|File\.Delete|\bos\.RemoveAll"),
    ("low", "environment variables", r"os\.environ|process\.env|getenv\("),
]
RISKS = [(sev, label, re.compile(rx)) for sev, label, rx in RISKS]

# Files Gemini may not touch, and files we never send.
PROTECTED = re.compile(r"(^|/)(\.git/|\.github/workflows/|\.gitlab-ci\.yml$|\.circleci/|Jenkinsfile$|"
                       r"\.env($|\.)|.*\.(pem|key|p12|pfx|keystore|jks)$|id_(rsa|dsa|ecdsa|ed25519)|"
                       r"\.npmrc$|\.pypirc$|\.netrc$|\.ssh/|\.aws/|\.docker/config\.json$|credentials(\.json)?$)")
INSTALL_HOOKS = re.compile(r'"(pre|post)?install"\s*:|cmdclass\s*=|\[tool\.setuptools\.cmdclass\]|build\.rs$')

SECRETS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"\b(AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}|"
    r"\bAIza[0-9A-Za-z_-]{35}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,}|"
    r"\b[rs]k_live_[0-9a-zA-Z]{20,}|\bglpat-[A-Za-z0-9_-]{20,}|\bnpm_[A-Za-z0-9]{36}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"(?i:\b(api[_-]?key|secret|token|passw(or)?d|access[_-]?key)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"])")


def find_secrets(text: str) -> list[str]:
    return [m.group(0)[:12] + "…" for m in SECRETS.finditer(text)]


def redact(text: str) -> str:
    return SECRETS.sub("[REDACTED]", text)


def added_lines(old: str, new: str) -> list[tuple[int, str]]:
    """Lines in new that weren't already in old (by content), so existing code isn't re-flagged."""
    have = Counter(ln.strip() for ln in old.splitlines())
    out = []
    for n, ln in enumerate(new.splitlines(), 1):
        s = ln.strip()
        if have[s] > 0:
            have[s] -= 1
        elif s:
            out.append((n, ln))
    return out


def scan_lines(lines) -> list[tuple[str, str, int, str]]:
    found = []
    for n, ln in lines:
        for sev, label, rx in RISKS:
            if rx.search(ln):
                found.append((sev, label, n, ln.strip()[:120]))
    return found


def scan_edit(rel: str, old: str | None, new: str) -> list[tuple[str, str, int, str]]:
    """Findings as (severity, label, line number, line) for a proposed file write."""
    found = []
    if PROTECTED.search(rel):
        found.append(("high", "protected path (git internals, CI, secrets, credentials)", 0, rel))
    if INSTALL_HOOKS.search(rel) or any(INSTALL_HOOKS.search(ln) for _, ln in added_lines(old or "", new)):
        found.append(("high", "install/build hook (runs code on install)", 0, rel))
    if old and len(old.strip()) > 200 and len(new.strip()) < 0.2 * len(old.strip()):
        found.append(("high", f"file shrinks from {len(old)} to {len(new)} chars (wipe?)", 0, rel))
    added = added_lines(old or "", new)
    found += scan_lines(added)
    if find_secrets("\n".join(ln for _, ln in added)):
        found.append(("high", "hard-coded secret/credential", 0, rel))
    if rel.endswith(".py"):
        found += bandit_new_findings(old or "", new)
    return found


def scan_command(cmd: str) -> list[tuple[str, str, int, str]]:
    return scan_lines(list(enumerate(cmd.splitlines(), 1)))


# --- Bandit (public Python security scanner), only if available --------------------------------
def _bandit_cmd() -> list[str] | None:
    if shutil.which("bandit"):
        return ["bandit"]
    if shutil.which("uvx"):
        return ["uvx", "--quiet", "bandit"]
    return None


def _bandit(code: str) -> list[tuple[str, str, str, int, str]]:
    """(test_id, severity, text, line number, line) for each Bandit finding in code."""
    cmd = _bandit_cmd()
    if not cmd or not code.strip():
        return []
    lines = code.splitlines()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "snippet.py"
        p.write_text(code)
        try:
            r = subprocess.run([*cmd, "-q", "-f", "json", str(p)], capture_output=True, text=True, timeout=120)
            results = json.loads(r.stdout or "{}").get("results", [])
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return []
    return [(x["test_id"], x["issue_severity"], x["issue_text"], x["line_number"],
             lines[x["line_number"] - 1].strip() if 0 < x["line_number"] <= len(lines) else "") for x in results]


def bandit_new_findings(old: str, new: str) -> list[tuple[str, str, int, str]]:
    """Bandit findings on new code that weren't already present in old (matched by test id + line text)."""
    seen = Counter((tid, line) for tid, _, _, _, line in _bandit(old))
    out = []
    for tid, sev, text, n, line in _bandit(new):
        if seen[(tid, line)] > 0:
            seen[(tid, line)] -= 1
        elif sev in ("HIGH", "MEDIUM"):
            out.append(("high" if sev == "HIGH" else "low", f"bandit {tid}: {text}", n, line[:120]))
    return out


# --- Sandbox for running Gemini-written code ----------------------------------------------------
SECRET_DIRS = [".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", ".docker", ".azure", ".gemininonce",
               "Library/Keychains", "Library/Application Support/Google/Chrome", "Library/Cookies",
               ".config/google-chrome", ".mozilla", "Library/Application Support/Firefox"]
SECRET_FILES = [".netrc", ".npmrc", ".pypirc", ".git-credentials", ".zsh_history", ".bash_history"]


def _sbpl_str(p) -> str:
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_argv(cmd: str, root: Path, network: bool = False) -> list[str] | None:
    """argv running `cmd` sandboxed (writes only in root/temp, secrets unreadable, and no internet unless
    `network`), or None if no sandbox is available."""
    root = root.resolve()
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        writable = [root, "/private/tmp", "/private/var/folders", Path(tempfile.gettempdir()).resolve()]
        profile = "\n".join([
            "(version 1)",
            "(allow default)",
            *([] if network else [
                "(deny network*)",
                '(allow network-bind (local ip "localhost:*"))',
                '(allow network-inbound (local ip "localhost:*"))',
                '(allow network* (remote ip "localhost:*"))',
                "(allow network* (remote unix-socket))",
            ]),
            "(deny file-write*)",
            "(allow file-write* " + " ".join(f"(subpath {_sbpl_str(p)})" for p in writable) + ' (regex #"^/dev/"))',
            "(deny file-read* " + " ".join(f"(subpath {_sbpl_str(HOME / d)})" for d in SECRET_DIRS)
            + " " + " ".join(f"(literal {_sbpl_str(HOME / f)})" for f in SECRET_FILES) + ")",
        ])
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", cmd]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--bind", str(root), str(root), *([] if network else ["--unshare-net"]),
                "--die-with-parent", "--chdir", str(root)]
        for d in SECRET_DIRS:
            if (HOME / d).is_dir():
                argv += ["--tmpfs", str(HOME / d)]
        for f in SECRET_FILES:
            if (HOME / f).is_file():
                argv += ["--ro-bind", "/dev/null", str(HOME / f)]
        return argv + ["/bin/sh", "-c", cmd]
    return None


def format_findings(findings) -> str:
    return "\n".join(f"    [{sev.upper()}] {label}" + (f" (line {n}): {line}" if n else f": {line}")
                     for sev, label, n, line in findings)
