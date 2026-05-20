"""VDS Manager Cog — Owner-only multi-instance VDS management via Discord.

Supports any number of named VDS / bot instances.

Flow:
  /vds_add    → Modal: name + SSH creds → test connection → saved as named session
  /vds_list   → Shows all saved sessions with status
  /vds_manage → Select VDS from dropdown → Select action → Execute
  /vds_remove → Select VDS from dropdown → Remove session
  /vds_clear  → Wipe all sessions

Actions per VDS:
  📊 Status       — OS info, Python, Git, running procs, disk
  🔄 Pull & Restart — git pull → kill python → start bot
  🆕 Full Setup   — Clone → pip install → .env → start bot
  ⚙️ Update .env  — Rewrite .env only
  🛑 Stop Bot     — Kill python processes
  ▶️ Start Bot    — Launch bot.py detached
  💻 Shell        — Run arbitrary command

Only OWNER_ID (from .env) can use any of these.
Windows VDS; all SSH commands use cmd/PowerShell.

Requires:  pip install paramiko
Configure: OWNER_ID=<your_discord_id>  in MAIN bot's .env
           REPO_URL=<github_url>        in MAIN bot's .env
"""

import asyncio
import base64
import io
import json
import os
import secrets
import shutil
import subprocess
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

_OWNER_ID = int(os.getenv("OWNER_ID", "0"))
_REPO_URL  = os.getenv("REPO_URL", "")

# Public deploy repo — VDS instances git-clone/pull from this (no auth needed)
DEPLOY_REPO_URL = "https://github.com/emrevegas/vegasbot-deploy"

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# ── Session store ──────────────────────────────────────────────────────────────
# { user_id: { "client_name": {ip, port, user, pass, path, ts, ssh_url?}, ... } }
# Persisted to database/server/vds_sessions.json
_sessions: dict[int, dict[str, dict]] = {}

_SESSIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "database", "server", "vds_sessions.json"
)

def _load_sessions() -> None:
    """Load persisted sessions from disk into _sessions."""
    try:
        with open(_SESSIONS_PATH, encoding="utf-8") as f:
            raw: dict = json.load(f)
        for uid_str, smap in raw.items():
            _sessions[int(uid_str)] = smap
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def _save_sessions() -> None:
    """Write _sessions to disk."""
    try:
        os.makedirs(os.path.dirname(_SESSIONS_PATH), exist_ok=True)
        with open(_SESSIONS_PATH, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _sessions.items()}, f, indent=2)
    except Exception:
        pass

_load_sessions()

# ── Deploy key store ──────────────────────────────────────────────────────────
# { user_id: {"private": <pem str>, "public": <openssh str>} }
# Persisted to main bot's .env as VDS_DEPLOY_KEY_PRIV / VDS_DEPLOY_KEY_PUB (base64).
_deploy_keys: dict[int, dict[str, str]] = {}

# Pre-load key from .env so it survives bot restarts.
def _load_deploy_key_from_env() -> None:
    if _OWNER_ID == 0:
        return
    priv_b64 = os.getenv("VDS_DEPLOY_KEY_PRIV", "")
    pub_raw  = os.getenv("VDS_DEPLOY_KEY_PUB", "")
    if priv_b64 and pub_raw:
        try:
            priv = base64.b64decode(priv_b64).decode()
            _deploy_keys[_OWNER_ID] = {"private": priv, "public": pub_raw}
        except Exception:
            pass

_load_deploy_key_from_env()


def _get_user_sessions(user_id: int) -> dict[str, dict]:
    return _sessions.setdefault(user_id, {})


# Files/dirs to never upload to client VDS
_DEPLOY_EXCLUDE: tuple[str, ...] = (
    "tests/",
    "migrate_to_sqlite.py",
    "build.py",
    "scan_qq.py",
    "admin.py",
    "images/",
    ".github/",
    ".venv/",
)

def _filter_tracked(files: list[str]) -> list[str]:
    out = []
    for f in files:
        if any(f == ex or f.startswith(ex) for ex in _DEPLOY_EXCLUDE):
            continue
        out.append(f)
    return out


# ── SSH helpers ────────────────────────────────────────────────────────────────

def _get_repo_url() -> str:
    url = _REPO_URL
    if not url:
        try:
            r = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            url = r.stdout.strip()
        except Exception:
            pass
    return url


def _get_repo_ssh_url(session: dict | None = None) -> str:
    """Return SSH clone URL (git@github.com:user/repo.git).
    Priority: session['ssh_url'] → REPO_SSH_URL env → convert REPO_URL https→ssh.
    """
    if session and session.get("ssh_url"):
        return session["ssh_url"]
    ssh = os.getenv("REPO_SSH_URL", "")
    if ssh:
        return ssh
    https = _get_repo_url()
    if "github.com" in https:
        path = (
            https
            .replace("https://github.com/", "")
            .replace("http://github.com/", "")
            .lstrip("/")
        )
        return f"git@github.com:{path}"
    return ""


def _generate_deploy_key() -> tuple[str, str]:
    """Generate Ed25519 key pair.  Returns (private_pem, public_openssh)."""
    import struct
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
    )
    private_key = Ed25519PrivateKey.generate()
    priv = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
    ).decode()
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_type = b"ssh-ed25519"
    wire = struct.pack(">I", len(key_type)) + key_type + struct.pack(">I", len(raw)) + raw
    pub = f"ssh-ed25519 {base64.b64encode(wire).decode()} vegas-deploy-key"
    return priv, pub


def _write_deploy_key_ssh(session: dict, priv_key_str: str) -> str:
    """Write private deploy key + SSH config to VDS via Python/base64."""
    config_content = (
        "Host github.com\n"
        "    HostName github.com\n"
        "    IdentityFile ~/.ssh/vegas_deploy\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
    )
    priv_b64 = base64.b64encode(priv_key_str.encode()).decode()
    cfg_b64  = base64.b64encode(config_content.encode()).decode()
    cmd = (
        f"python -c \""
        f"import base64, pathlib; "
        f"ssh=pathlib.Path.home()/'.ssh'; ssh.mkdir(parents=True, exist_ok=True); "
        f"kf=ssh/'vegas_deploy'; kf.write_text(base64.b64decode('{priv_b64}').decode()); "
        f"cf=ssh/'config'; "
        f"existing=cf.read_text() if cf.exists() else ''; "
        f"block=base64.b64decode('{cfg_b64}').decode(); "
        f"cf.write_text(block if 'vegas_deploy' in existing else existing+'\\n'+block) if 'vegas_deploy' not in existing else None; "
        f"print('[Deploy Key] Written to', kf)"
        f"\""
    )
    return _ssh_run(session, cmd, timeout=20)


def _ssh_run(session: dict, command: str, timeout: int = 60) -> str:
    """Open SSH connection, run one command, return combined stdout+stderr."""
    if not HAS_PARAMIKO:
        return "❌ paramiko not installed on main bot. Run: pip install paramiko"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            session["ip"], port=session["port"],
            username=session["user"], password=session["pass"],
            timeout=15,
        )
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        ssh.close()
        combined = out + ("\n[stderr]\n" + err if err.strip() else "")
        return combined.strip() or "(no output)"
    except Exception as e:
        return f"SSH Error: {e}"


def _test_connection(session: dict) -> tuple[bool, str]:
    out = _ssh_run(session, "hostname & whoami & python --version & git --version", timeout=15)
    ok = not out.startswith("SSH Error") and "not installed" not in out
    return ok, out


def _ssh_status(session: dict) -> str:
    path = session["path"]
    cmd = (
        "hostname & whoami & echo. & "
        "echo [Python] & python --version & "
        "echo [Git] & git --version & "
        "echo [Running Python] & "
        'tasklist /FI "IMAGENAME eq python.exe" /NH 2>nul & '
        'tasklist /FI "IMAGENAME eq pythonw.exe" /NH 2>nul & '
        "echo [Disk C:] & "
        "powershell -Command \""
        "  $d=Get-PSDrive C;"
        "  Write-Host ('Used: '+[math]::Round($d.Used/1GB,1)+'GB  Free: '+[math]::Round($d.Free/1GB,1)+'GB')"
        "\" & "
        f'if exist "{path}" (echo [Bot path] EXISTS: {path}) else (echo [Bot path] NOT FOUND: {path}) & '
        f'if exist "{path}\\bot.log" ('
        f'echo [Last log line] & powershell -Command "Get-Content -Tail 5 \'{path}\\bot.log\'"'
        f') else (echo [bot.log] not found)'
    )
    return _ssh_run(session, cmd, timeout=30)


def _write_env_ssh(session: dict, env_vars: dict) -> str:
    """Merge env_vars into existing .env on Windows VDS (base64 encoded)."""
    path = session["path"]
    updates_b64 = base64.b64encode(json.dumps(env_vars).encode()).decode()
    script = (
        "import base64, json, os; "
        f"p=r'{path}\\.env'; "
        "existing={}; "
        "[existing.update({k.strip(): v.strip()}) for line in (open(p).readlines() if os.path.exists(p) else []) if '=' in line for k,v in [line.strip().split('=',1)]]; "
        f"existing.update(json.loads(base64.b64decode('{updates_b64}').decode())); "
        "open(p,'w',encoding='utf-8').write('\\n'.join(f'{k}={v}' for k,v in existing.items())); "
        "print('[.env] Merged OK, keys:', len(existing))"
    )
    cmd = f'python -c "{script}"'
    return _ssh_run(session, cmd, timeout=30)


def _ssh_stop_bot(session: dict) -> str:
    # Kill python, pythonw AND bot.exe (PyInstaller bundle)
    cmd = (
        'powershell -Command "'
        'Get-Process python,pythonw,bot -ErrorAction SilentlyContinue | '
        'Stop-Process -Force -ErrorAction SilentlyContinue; '
        'Write-Host \'[Stop] All bot processes killed\'"'
    )
    return _ssh_run(session, cmd, timeout=15)


def _ssh_start_bot(session: dict, force: bool = False) -> str:
    path = session["path"]
    if not force:
        # Guard: if bot.exe already running from this path, skip
        check = _ssh_run(
            session,
            f'powershell -Command "(Get-Process bot -ErrorAction SilentlyContinue | Where-Object {{$_.Path -like \'{path}*\'}}).Count"',
            timeout=10,
        )
        try:
            if int(check.strip()) > 0:
                return f'[Start] bot.exe already running ({check.strip()} instance). Run Stop first.'
        except (ValueError, AttributeError):
            pass
    cmd = (
        f'powershell -Command "'
        f'Set-Content -Path \'\'{path}\\bot.log\'\' -Value \'\'\'\'; '
        f'Set-Content -Path \'\'{path}\\bot_err.log\'\' -Value \'\'\'\'; '
        f'Start-Process -FilePath \'\'{path}\\bot\\bot.exe\'\' '
        f'-WorkingDirectory \'\'{path}\' '
        f'-WindowStyle Hidden '
        f'-RedirectStandardOutput \'\'{path}\\bot.log\'\' '
        f'-RedirectStandardError \'\'{path}\\bot_err.log\'\"'
        f' && timeout /t 7 /nobreak >nul'
        f' && echo === stdout === && powershell -Command "Get-Content -Tail 40 \'\'{path}\\bot.log\'\"'
        f' && echo === stderr === && powershell -Command "Get-Content -Tail 20 \'\'{path}\\bot_err.log\'\"'
    )
    return _ssh_run(session, cmd, timeout=30)


def _ssh_pull_restart(session: dict) -> str:
    path   = session["path"]
    branch = session.get("branch", "").strip()
    pull_cmd = f'cd /d "{path}" && git pull origin {branch}' if branch else f'cd /d "{path}" && git pull'
    parts = []
    parts.append(f"[1/3 git pull]\n{_ssh_run(session, pull_cmd, 60)}")
    parts.append(f"[2/3 stop]\n{_ssh_stop_bot(session)}")
    parts.append(f"[3/3 start]\n{_ssh_start_bot(session)}")
    return "\n\n".join(parts)


def _ssh_install_prerequisites(session: dict) -> str:
    """Install Python 3.12 + Git on a blank Windows VDS via winget.

    winget is built-in on Windows Server 2022+ / Windows 10+.
    Falls back to direct download if winget is not available.
    Returns combined log output.
    """
    parts = []

    # ── 1. Check / install winget ──────────────────────────────────────────
    winget_check = _ssh_run(session, "winget --version", timeout=15)
    if "SSH Error" in winget_check:
        return winget_check
    has_winget = "v" in winget_check.lower() or "winget" in winget_check.lower()

    # ── 2. Python ──────────────────────────────────────────────────────────
    py_check = _ssh_run(session, "python --version 2>&1", timeout=10)
    if "Python 3" in py_check:
        parts.append(f"[Python] Already installed: {py_check.strip()}")
    elif has_winget:
        r = _ssh_run(
            session,
            (
                "winget install --id Python.Python.3.12 -e --silent "
                "--accept-package-agreements --accept-source-agreements"
            ),
            timeout=300,
        )
        parts.append(f"[Python via winget]\n{r}")
    else:
        # Fallback: download installer directly
        r = _ssh_run(
            session,
            (
                'powershell -Command "'
                '$url=\'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe\'; '
                '$out=\'$env:TEMP\\python_setup.exe\'; '
                'Invoke-WebRequest -Uri $url -OutFile $out; '
                'Start-Process -FilePath $out -ArgumentList \'/quiet InstallAllUsers=1 PrependPath=1\' -Wait; '
                'Write-Host \'[Python] Installation complete\'"'
            ),
            timeout=300,
        )
        parts.append(f"[Python via direct download]\n{r}")

    # ── 3. Git ─────────────────────────────────────────────────────────────
    git_check = _ssh_run(session, "git --version 2>&1", timeout=10)
    if "git version" in git_check.lower():
        parts.append(f"[Git] Already installed: {git_check.strip()}")
    elif has_winget:
        r = _ssh_run(
            session,
            (
                "winget install --id Git.Git -e --silent "
                "--accept-package-agreements --accept-source-agreements"
            ),
            timeout=300,
        )
        parts.append(f"[Git via winget]\n{r}")
    else:
        r = _ssh_run(
            session,
            (
                'powershell -Command "'
                '$url=\'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe\'; '
                '$out=\'$env:TEMP\\git_setup.exe\'; '
                'Invoke-WebRequest -Uri $url -OutFile $out; '
                'Start-Process -FilePath $out -ArgumentList \'/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS\' -Wait; '
                'Write-Host \'[Git] Installation complete\'"'
            ),
            timeout=300,
        )
        parts.append(f"[Git via direct download]\n{r}")

    # ── 4. Refresh PATH so new installs are visible ────────────────────────
    _ssh_run(session, 'refreshenv 2>nul || echo [PATH] Use new terminal for updated PATH', timeout=10)

    return "\n\n".join(parts)


def _ssh_full_setup(
    session: dict,
    env_vars: dict,
    ssh_url: str,
    priv_key: str | None = None,
) -> str:
    """Full fresh-Windows setup: git clone from public deploy repo, write .env, start."""
    path  = session["path"]
    parts = []
    total = 3
    step  = 1

    # 1. Ensure git is available
    r = _ssh_run(session, "git --version", timeout=10)
    parts.append(f"[{step}/{total} Check git]\n{r}")
    if "SSH Error" in r:
        return "\n\n".join(parts) + "\n\n❌ Aborting — SSH error."
    step += 1

    # 2. git clone/pull from public deploy repo
    git_cmd = (
        f'if exist "{path}\\.git" ('
        f'  cd /d "{path}" && git fetch --depth 1 origin deploy-windows && git reset --hard origin/deploy-windows'
        f') else ('
        f'  git clone --depth 1 --branch deploy-windows {DEPLOY_REPO_URL} "{path}"'
        f')'
    )
    git_out = _ssh_run(session, git_cmd, timeout=300)
    parts.append(f"[{step}/{total} git clone/pull]\n{git_out}")
    if "fatal" in git_out.lower() or "error" in git_out.lower():
        return "\n\n".join(parts) + "\n\n❌ Aborting — git failed."
    step += 1

    # 3. Write .env + Start
    parts.append(f"[{step}/{total} .env]\n{_write_env_ssh(session, env_vars)}")
    parts.append(f"[{step}/{total} Start]\n{_ssh_start_bot(session)}")
    return "\n\n".join(parts)


# -- Ubuntu SSH helpers ─────────────────────────────────────────────────────────

def _test_connection_ubuntu(session: dict) -> tuple[bool, str]:
    out = _ssh_run(session, "hostname && whoami && python3 --version && git --version", timeout=15)
    ok = not out.startswith("SSH Error") and "not installed" not in out
    return ok, out


def _ssh_status_ubuntu(session: dict) -> str:
    path = session["path"]
    cmd = (
        "hostname && whoami && echo '' && "
        "echo '[Python]' && python3 --version && "
        "echo '[Git]' && git --version && "
        "echo '[Running Python]' && (ps aux | grep -E '[p]ython3?' || echo 'none') && "
        "echo '[Disk]' && df -h / && "
        f"( [ -d '{path}' ] && echo '[Bot path] EXISTS: {path}' || echo '[Bot path] NOT FOUND: {path}' ) && "
        f"( [ -f '{path}/bot.log' ] && (echo '[Last log lines]' && tail -5 '{path}/bot.log') || echo '[bot.log] not found' )"
    )
    return _ssh_run(session, cmd, timeout=30)


def _ssh_stop_bot_ubuntu(session: dict) -> str:
    # Kill python bot.py AND PyInstaller bot/bot executable
    path = session["path"]
    cmd = (
        f"pkill -f 'python.*bot\\.py' 2>/dev/null; "
        f"pkill -9 -f '{path}/bot/bot' 2>/dev/null; "
        f"pkill -9 -f 'bot/bot' 2>/dev/null; "
        f"echo '[Stop] Done'"
    )
    return _ssh_run(session, cmd, timeout=15)


def _ssh_start_bot_ubuntu(session: dict, force: bool = False) -> str:
    path = session["path"]
    if not force:
        # Guard: if bot/bot already running from this path, skip
        check = _ssh_run(session, f"pgrep -c -f '{path}/bot/bot' 2>/dev/null || echo 0", timeout=10)
        try:
            if int(check.strip()) > 0:
                return f'[Start] bot/bot already running ({check.strip()} instance). Run Stop first.'
        except (ValueError, AttributeError):
            pass
    # PyInstaller bundle: run bot/bot executable instead of python bot.py
    cmd = (
        f"cd '{path}' && "
        f"chmod +x bot/bot 2>/dev/null || true && "
        f"> '{path}/bot.log' && > '{path}/bot_err.log' && "
        f"nohup '{path}/bot/bot' >> '{path}/bot.log' 2>> '{path}/bot_err.log' & disown && "
        f"sleep 10 && "
        f"echo '=== stdout ===' && tail -n 30 '{path}/bot.log' && "
        f"echo '=== stderr ===' && tail -n 50 '{path}/bot_err.log'"
    )
    return _ssh_run(session, cmd, timeout=50)


def _write_env_ssh_ubuntu(session: dict, env_vars: dict) -> str:
    """Merge env_vars into existing .env on Ubuntu VDS (base64 encoded)."""
    path = session["path"]
    updates_b64 = base64.b64encode(json.dumps(env_vars).encode()).decode()
    script = (
        "import base64, json, os; "
        f"p='{path}/.env'; "
        "existing={}; "
        "[existing.update({k.strip(): v.strip()}) for line in (open(p).readlines() if os.path.exists(p) else []) if '=' in line for k,v in [line.strip().split('=',1)]]; "
        f"existing.update(json.loads(base64.b64decode('{updates_b64}').decode())); "
        "open(p,'w',encoding='utf-8').write('\\n'.join(f'{k}={v}' for k,v in existing.items())); "
        "print('[.env] Merged OK, keys:', len(existing))"
    )
    cmd = f"python3 -c \"{script}\""
    return _ssh_run(session, cmd, timeout=30)


def _write_deploy_key_ssh_ubuntu(session: dict, priv_key_str: str) -> str:
    """Write private deploy key + SSH config to Ubuntu VDS via python3/base64."""
    config_content = (
        "Host github.com\n"
        "    HostName github.com\n"
        "    IdentityFile ~/.ssh/vegas_deploy\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
    )
    priv_b64 = base64.b64encode(priv_key_str.encode()).decode()
    cfg_b64  = base64.b64encode(config_content.encode()).decode()
    cmd = (
        f"python3 -c \""
        f"import base64, pathlib, os as _os; "
        f"ssh=pathlib.Path.home()/'.ssh'; ssh.mkdir(parents=True, exist_ok=True); "
        f"kf=ssh/'vegas_deploy'; kf.write_text(base64.b64decode('{priv_b64}').decode()); "
        f"_os.chmod(kf, 0o600); "
        f"cf=ssh/'config'; "
        f"existing=cf.read_text() if cf.exists() else ''; "
        f"block=base64.b64decode('{cfg_b64}').decode(); "
        f"cf.write_text(block if 'vegas_deploy' in existing else existing+'\\n'+block) if 'vegas_deploy' not in existing else None; "
        f"print('[Deploy Key] Written to', kf)"
        f"\""
    )
    return _ssh_run(session, cmd, timeout=20)


def _ssh_pull_restart_ubuntu(session: dict) -> str:
    path   = session["path"]
    branch = session.get("branch", "").strip()
    pull_cmd = f"cd '{path}' && git pull origin {branch}" if branch else f"cd '{path}' && git pull"
    parts = []
    parts.append(f"[1/3 git pull]\n{_ssh_run(session, pull_cmd, 60)}")
    parts.append(f"[2/3 stop]\n{_ssh_stop_bot_ubuntu(session)}")
    parts.append(f"[3/3 start]\n{_ssh_start_bot_ubuntu(session)}")
    return "\n\n".join(parts)


def _ssh_install_prerequisites_ubuntu(session: dict) -> str:
    """Install Python3 + pip + Git on Ubuntu/Debian via apt."""
    parts = []
    r = _ssh_run(session, "sudo apt-get update -y 2>&1 | tail -3", timeout=90)
    parts.append(f"[apt update]\n{r}")
    if "SSH Error" in r:
        return "\n\n".join(parts)
    r = _ssh_run(
        session,
        "sudo apt-get install -y python3 python3-pip git 2>&1 | tail -10",
        timeout=180,
    )
    parts.append(f"[apt install python3 pip git]\n{r}")
    r2 = _ssh_run(session, "python3 --version && pip3 --version && git --version", timeout=10)
    parts.append(f"[Verify]\n{r2}")
    return "\n\n".join(parts)


def _ssh_full_setup_ubuntu(
    session: dict,
    env_vars: dict,
    ssh_url: str,
    priv_key: str | None = None,
) -> str:
    """Full fresh-Ubuntu setup: git clone from public deploy repo, write .env, start."""
    path  = session["path"]
    parts = []
    total = 3
    step  = 1

    # 1. Ensure git is available
    r = _ssh_run(session, "which git || sudo apt-get install -y git -qq 2>&1 | tail -3", timeout=60)
    parts.append(f"[{step}/{total} Check git]\n{r}")
    if "SSH Error" in r:
        return "\n\n".join(parts) + "\n\n❌ Aborting — SSH error."
    step += 1

    # 2. git clone/pull from public deploy repo
    git_cmd = (
        f"if [ -d '{path}/.git' ]; then "
        f"  cd '{path}' && git fetch --depth 1 origin deploy-linux && git reset --hard origin/deploy-linux; "
        f"else "
        f"  git clone --depth 1 --branch deploy-linux {DEPLOY_REPO_URL} '{path}'; "
        f"fi && chmod +x '{path}/bot/bot' 2>/dev/null; true"
    )
    git_out = _ssh_run(session, git_cmd, timeout=300)
    parts.append(f"[{step}/{total} git clone/pull]\n{git_out}")
    if "fatal" in git_out.lower() or "ssh error" in git_out.lower():
        return "\n\n".join(parts) + "\n\n❌ Aborting — git failed."
    step += 1

    # 3. Write .env + Start
    parts.append(f"[{step}/{total} .env]\n{_write_env_ssh_ubuntu(session, env_vars)}")
    parts.append(f"[{step}/{total} Start]\n{_ssh_start_bot_ubuntu(session)}")
    return "\n\n".join(parts)


# ── OS dispatch wrappers ───────────────────────────────────────────────────────

def _dispatch_test_connection(session: dict) -> tuple[bool, str]:
    if session.get("os") == "ubuntu":
        return _test_connection_ubuntu(session)
    return _test_connection(session)


def _dispatch_status(session: dict) -> str:
    if session.get("os") == "ubuntu":
        return _ssh_status_ubuntu(session)
    return _ssh_status(session)


def _dispatch_stop(session: dict) -> str:
    if session.get("os") == "ubuntu":
        return _ssh_stop_bot_ubuntu(session)
    return _ssh_stop_bot(session)


def _dispatch_start(session: dict, force: bool = False) -> str:
    if session.get("os") == "ubuntu":
        return _ssh_start_bot_ubuntu(session, force=force)
    return _ssh_start_bot(session, force=force)


def _do_sftp_upload(session: dict, local_root: str, tracked: list[str]) -> tuple[int, list[str]]:
    """Upload tracked files via SFTP. Returns (uploaded_count, failed_list)."""
    remote_root = session["path"]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        session["ip"], port=session["port"],
        username=session["user"], password=session["pass"],
        timeout=15,
    )
    sftp = ssh.open_sftp()

    def _mkdir_p(remote_dir: str) -> None:
        d = remote_dir.replace("\\", "/")
        parts_acc: list[str] = []
        while d and d != "/":
            parts_acc.insert(0, d)
            d = d.rsplit("/", 1)[0] if "/" in d else ""
        for p in parts_acc:
            try:
                sftp.stat(p)
            except FileNotFoundError:
                try:
                    sftp.mkdir(p)
                except Exception:
                    pass

    def _sftp_put(local_file: str, remote_rel: str) -> None:
        remote_f = (remote_root.rstrip("/\\") + "/" + remote_rel.replace("\\", "/"))
        _mkdir_p(remote_f.rsplit("/", 1)[0])
        sftp.put(local_file, remote_f)

    uploaded, failed = 0, []
    for rel in tracked:
        local_file = os.path.join(local_root, rel.replace("/", os.sep))
        if not os.path.isfile(local_file):
            continue
        try:
            _sftp_put(local_file, rel)
            uploaded += 1
        except Exception as e:
            failed.append(f"{rel}: {e}")

    sftp.close()
    ssh.close()
    return uploaded, failed


async def _run_deploy_with_progress(
    interaction: discord.Interaction, session: dict, name: str
) -> None:
    """Deploy: VDS does git clone/pull from the public deploy repo (no SFTP)."""
    loop = asyncio.get_event_loop()
    remote_root   = session["path"]
    is_ubuntu     = session.get("os") == "ubuntu"
    os_label      = "\U0001f427 Ubuntu" if is_ubuntu else "\U0001fa9f Windows"
    deploy_branch = "deploy-linux" if is_ubuntu else "deploy-windows"

    msg = await interaction.followup.send(embed=_vds_embed(
        "\U0001f4e4 Deploy Started",
        f"**Instance:** `{name}` — `{session['ip']}:{session['port']}`  {os_label}\n\n"
        f"\U0001f504 **[1/3]** Pulling `{deploy_branch}` from deploy repo...",
        0xFFA500,
    ))

    # ── 1. git clone/pull on VDS ──────────────────────────────────────────
    if is_ubuntu:
        git_cmd = (
            f"if [ -d '{remote_root}/.git' ]; then "
            f"  cd '{remote_root}' && git fetch --depth 1 origin {deploy_branch} && git reset --hard origin/{deploy_branch}; "
            f"else "
            f"  git clone --depth 1 --branch {deploy_branch} {DEPLOY_REPO_URL} '{remote_root}'; "
            f"fi && "
            f"chmod +x '{remote_root}/bot/bot' 2>/dev/null; true"
        )
    else:
        git_cmd = (
            f'if exist "{remote_root}\\.git" ('
            f'  cd /d "{remote_root}" && git fetch --depth 1 origin {deploy_branch} && git reset --hard origin/{deploy_branch}'
            f') else ('
            f'  git clone --depth 1 --branch {deploy_branch} {DEPLOY_REPO_URL} "{remote_root}"'
            f')'
        )

    git_out = await loop.run_in_executor(None, _ssh_run, session, git_cmd, 300)
    if "error" in git_out.lower() or "fatal" in git_out.lower():
        return await msg.edit(embed=_vds_embed(
            "❌ Deploy Failed",
            f"git clone/pull error:\n```{git_out[:800]}```",
            0xFF4444,
        ))

    await msg.edit(embed=_vds_embed(
        "📤 Deploying...",
        f"**Instance:** `{name}` — `{session['ip']}:{session['port']}`  {os_label}\n\n"
        f"✅ **[1/3]** Pulled `{deploy_branch}` branch\n"
        f"🔄 **[2/3]** Writing .env...",
        0xFFA500,
    ))

    # ── 2. Write .env ─────────────────────────────────────────────────────
    env_vars = {k: v for k, v in session.items() if k not in (
        "ip", "port", "user", "pass", "path", "os", "branch", "instance_token"
    )}
    instance_token = session.get("instance_token", "")
    if not instance_token:
        import secrets as _sec
        instance_token = _sec.token_hex(32)
        session["instance_token"] = instance_token
        _save_sessions()
    auth_secret    = os.getenv("AUTH_SECRET", "")
    auth_url       = os.getenv("MAIN_BOT_AUTH_URL", "").rstrip("/") or f"http://{os.getenv('HOST_IP', '31.210.40.211')}:8001"
    if instance_token:
        env_vars["INSTANCE_TOKEN"]    = instance_token
    if auth_secret:
        env_vars["AUTH_SECRET"]       = auth_secret
    if auth_url:
        env_vars["MAIN_BOT_AUTH_URL"] = auth_url

    env_out = await loop.run_in_executor(None, _dispatch_write_env, session, env_vars)

    await msg.edit(embed=_vds_embed(
        "📤 Deploying...",
        f"**Instance:** `{name}` — `{session['ip']}:{session['port']}`  {os_label}\n\n"
        f"✅ **[1/3]** Pulled `{deploy_branch}` branch\n"
        f"✅ **[2/3]** .env written\n"
        f"🔄 **[3/3]** Restarting bot...",
        0xFFA500,
    ))

    # ── 3. Stop + Start ───────────────────────────────────────────────────
    stop_out  = await loop.run_in_executor(None, _dispatch_stop, session)
    await asyncio.sleep(3)   # give processes time to fully terminate
    start_out = await loop.run_in_executor(None, _dispatch_start, session, True)  # force=True, stop already done

    desc = (
        f"**Instance:** `{name}` — `{session['ip']}:{session['port']}`  {os_label}\n\n"
        f"**Git pull:** `{deploy_branch}` from `{DEPLOY_REPO_URL}`\n\n"
        f"**Stop →** `{stop_out[:150]}`\n"
        f"**Start →** `{start_out[:150]}`"
    )
    await msg.edit(
        embed=_vds_embed("✅ Deploy Complete", desc, 0x00CC66),
        view=VdsActionView(name, interaction.user.id),
    )


def _sftp_deploy_and_restart(session: dict) -> str:
    """Sync deploy fallback — git clone/pull on VDS (no SFTP)."""
    remote_root   = session["path"]
    is_ubuntu     = session.get("os") == "ubuntu"
    deploy_branch = "deploy-linux" if is_ubuntu else "deploy-windows"
    parts         = []

    # ── 1. git clone / pull ────────────────────────────────────────────────
    if is_ubuntu:
        git_cmd = (
            f"if [ -d '{remote_root}/.git' ]; then "
            f"  cd '{remote_root}' && git fetch --depth 1 origin {deploy_branch} && git reset --hard origin/{deploy_branch}; "
            f"else "
            f"  git clone --depth 1 --branch {deploy_branch} {DEPLOY_REPO_URL} '{remote_root}'; "
            f"fi && chmod +x '{remote_root}/bot/bot' 2>/dev/null; true"
        )
    else:
        git_cmd = (
            f'if exist "{remote_root}\\.git" ('
            f'  cd /d "{remote_root}" && git fetch --depth 1 origin {deploy_branch} && git reset --hard origin/{deploy_branch}'
            f') else ('
            f'  git clone --depth 1 --branch {deploy_branch} {DEPLOY_REPO_URL} "{remote_root}"'
            f')'
        )
    git_out = _ssh_run(session, git_cmd, 300)
    parts.append(f"[1/3 git clone/pull]\n{git_out}")

    # ── 2. Restart ─────────────────────────────────────────────────────────
    parts.append(f"[2/3 stop]\n{_dispatch_stop(session)}")
    parts.append(f"[3/3 start]\n{_dispatch_start(session)}")

    return "\n\n".join(parts)


def _dispatch_pull_restart(session: dict) -> str:
    return _sftp_deploy_and_restart(session)


def _dispatch_write_env(session: dict, env_vars: dict) -> str:
    if session.get("os") == "ubuntu":
        return _write_env_ssh_ubuntu(session, env_vars)
    return _write_env_ssh(session, env_vars)


def _dispatch_full_setup(session: dict, env_vars: dict, ssh_url: str, priv_key: str | None) -> str:
    if session.get("os") == "ubuntu":
        return _ssh_full_setup_ubuntu(session, env_vars, ssh_url, priv_key)
    return _ssh_full_setup(session, env_vars, ssh_url, priv_key)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _session_options(user_id: int) -> list[discord.SelectOption]:
    sessions = _get_user_sessions(user_id)
    if not sessions:
        return []
    return [
        discord.SelectOption(
            label=name[:100],
            value=name,
            description=f"{s['ip']}:{s['port']}  {s['path']}"[:100],
        )
        for name, s in sessions.items()
    ]


def _chunk_output(header: str, out: str) -> list[str]:
    """Split long output into ≤1900-char codeblock messages."""
    chunks = []
    remaining = out
    first = True
    while remaining or first:
        take = remaining[:1800]
        remaining = remaining[1800:]
        prefix = f"{header}\n" if first else ""
        chunks.append(f"{prefix}```\n{take}\n```")
        first = False
        if not remaining:
            break
    return chunks or [f"{header}\n```(no output)```"]


def _vds_embed(title: str, description: str, color: int = 0x9945FF) -> discord.Embed:
    """Standard embed for VDS Manager responses."""
    e = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    e.set_footer(text="VDS Manager")
    return e


# ── Modals ─────────────────────────────────────────────────────────────────────

class VdsOsPickView(discord.ui.View):
    """Step 1 of /vds_add — pick OS, then open the add modal."""

    @discord.ui.select(
        placeholder="Select server OS…",
        options=[
            discord.SelectOption(
                label="🐧 Ubuntu / Debian",
                value="ubuntu",
                description="Linux: python3, apt, nohup, pkill",
            ),
            discord.SelectOption(
                label="🪟 Windows Server",
                value="windows",
                description="PowerShell, winget, Start-Process",
            ),
        ],
    )
    async def os_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(VdsAddModal(os_type=select.values[0]))


class VdsAddModal(discord.ui.Modal):
    def __init__(self, os_type: str = "windows"):
        is_ubuntu = os_type == "ubuntu"
        super().__init__(
            title="Add VDS — Ubuntu" if is_ubuntu else "Add VDS — Windows",
            timeout=None,
        )
        self._os_type = os_type

        self.name_f = discord.ui.TextInput(
            label="Instance Name",
            placeholder="client1 / shop-bot / test",
            max_length=50,
        )
        self.conn_f = discord.ui.TextInput(
            label="Host (IP:port)",
            placeholder="192.168.1.100:22",
            max_length=70,
        )
        self.user_f = discord.ui.TextInput(
            label="SSH Username",
            placeholder="ubuntu" if is_ubuntu else "Administrator",
            max_length=100,
        )
        self.pwd_f = discord.ui.TextInput(
            label="SSH Password",
            placeholder="••••••••",
            max_length=200,
        )
        default_path     = "/opt/vegasbot"       if is_ubuntu else r"C:\vegasbot"
        path_placeholder = "/opt/vegasbot::deploy" if is_ubuntu else r"C:\vegasbot::deploy"
        self.path_f = discord.ui.TextInput(
            label="Install Path  [::branch optional]",
            default=default_path,
            placeholder=path_placeholder,
            max_length=260,
        )
        self.add_item(self.name_f)
        self.add_item(self.conn_f)
        self.add_item(self.user_f)
        self.add_item(self.pwd_f)
        self.add_item(self.path_f)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_f.value.strip()
        host_port = self.conn_f.value.strip()
        if ":" in host_port:
            ip, _, port_str = host_port.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                ip = host_port
                port = 22
        else:
            ip = host_port
            port = 22
        user = self.user_f.value.strip()
        pwd = self.pwd_f.value
        is_ubuntu = self._os_type == "ubuntu"
        path_branch = self.path_f.value.strip()
        if not is_ubuntu:
            path_branch = path_branch.rstrip("\\")
        if "::" in path_branch:
            path, branch = path_branch.split("::", 1)
            if not is_ubuntu:
                path = path.rstrip("\\")
        else:
            path = path_branch
            branch = ""

        await interaction.response.defer(thinking=True)
        session = {
            "ip":             ip,
            "port":           port,
            "user":           user,
            "pass":           pwd,
            "path":           path,
            "ssh_url":        _get_repo_ssh_url(),
            "branch":         branch,
            "os":             self._os_type,
            "ts":             time.time(),
            "instance_token": secrets.token_hex(32),
        }
        ok, out = await asyncio.get_event_loop().run_in_executor(None, _dispatch_test_connection, session)
        if not ok:
            return await interaction.followup.send(
                f"❌ **Connection Failed**\n```\n{out[:1900]}\n```"
            )
        _get_user_sessions(interaction.user.id)[name] = session
        _save_sessions()
        os_label = "🐧 Ubuntu" if is_ubuntu else "🪟 Windows"
        await interaction.followup.send(
            f"✅ **Instance `{name}` connected** — {os_label}\n"
            f"`{session['ip']}:{session['port']}` | `{session['user']}` | `{session['path']}`\n"
            f"```\n{out[:800]}\n```"
        )


# ── .env key definitions ──────────────────────────────────────────────────────
_ENV_KEYS: list[tuple[str, str, str, bool]] = [
    # (key, label, placeholder, is_long)
    ("TOKEN",             "TOKEN — Bot Token",        "Discord bot token",        False),
    ("PREFIX",            "PREFIX",                   ".",                        False),
    ("GUILD_ID",          "GUILD_ID",                 "107124952938...",          False),
    ("OWNER_ID",          "OWNER_ID",                 "Your Discord user ID",     False),
    ("SUPER_ADMIN_ID",    "SUPER_ADMIN_ID",           "Your Discord user ID",     False),
    ("CRYPTO_MNEMONIC",   "CRYPTO_MNEMONIC",          "12-word mnemonic phrase",  True),
    ("TREASURY_MNEMONIC", "TREASURY_MNEMONIC",        "12-word mnemonic phrase",  True),
]


class VdsEnvEditModal(discord.ui.Modal):
    """Single-field modal to set one .env key. Opened from a select interaction — no chaining."""

    def __init__(self, key: str, label: str, placeholder: str, is_long: bool,
                 session_name: str, user_id: int):
        super().__init__(title=f"Set {key[:40]}", timeout=300)
        self._key          = key
        self._session_name = session_name
        self._user_id      = user_id
        self.value_input   = discord.ui.TextInput(
            label=label[:45],
            placeholder=placeholder,
            required=True,
            style=discord.TextStyle.paragraph if is_long else discord.TextStyle.short,
            max_length=500,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        session = _get_user_sessions(self._user_id).get(self._session_name)
        if not session:
            return await interaction.followup.send(
                f"❌ Session `{self._session_name}` not found."
            )
        val = self.value_input.value.strip()
        if not val:
            return await interaction.followup.send("⚠️ No value entered, nothing changed.")
        out = await asyncio.get_event_loop().run_in_executor(
            None, _dispatch_write_env, session, {self._key: val}
        )
        await interaction.followup.send(embed=_vds_embed(
            f"⚙️ `.env` Updated — `{self._session_name}`",
            f"`{self._key}` set.\n```\n{out[:800]}\n```",
        ))


class VdsEnvPickerView(discord.ui.View):
    """Select menu: choose which .env key to edit. Each selection opens a modal."""
    def __init__(self, session_name: str, user_id: int):
        super().__init__(timeout=300)
        self._session_name = session_name
        self._user_id      = user_id

    @discord.ui.select(
        placeholder="Choose a .env key to set / update…",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label=label, value=key, description=placeholder[:50])
            for key, label, placeholder, _ in _ENV_KEYS
        ],
    )
    async def key_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        key = select.values[0]
        _, label, placeholder, is_long = next(x for x in _ENV_KEYS if x[0] == key)
        # select → modal is allowed by Discord
        await interaction.response.send_modal(
            VdsEnvEditModal(key, label, placeholder, is_long, self._session_name, self._user_id)
        )



class VdsShellModal(discord.ui.Modal, title="Run Shell Command"):
    command = discord.ui.TextInput(
        label="Command",
        placeholder="git log --oneline -5",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, session_name: str, user_id: int):
        super().__init__(timeout=300)
        self._session_name = session_name
        self._user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        session = _get_user_sessions(self._user_id).get(self._session_name)
        if not session:
            return await interaction.followup.send(
                f"❌ Session `{self._session_name}` not found."
            )
        out = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _ssh_run(session, self.command.value, 60)
        )
        header = f"💻 **Shell — `{self._session_name}`** | `{self.command.value[:200]}`"
        for chunk in _chunk_output(header, out):
            await interaction.followup.send(chunk)


# ── Action view ────────────────────────────────────────────────────────────────

class VdsActionView(discord.ui.View):
    """Actions for a specific named session."""
    def __init__(self, session_name: str, user_id: int):
        super().__init__(timeout=300)
        self._name    = session_name
        self._user_id = user_id

    @discord.ui.select(
        placeholder="Choose an action…",
        options=[
            discord.SelectOption(label="📊 Status",         value="status",  description="OS, Python, Git, disk, running processes"),
            discord.SelectOption(label="� Deploy & Restart", value="pull",    description="SFTP upload tracked files → pip install → restart"),
            discord.SelectOption(label="⚙️ Update .env",    value="env",     description="Set or update any .env variable"),
            discord.SelectOption(label="▶️ Start Bot",      value="start",   description="Launch bot.py detached"),
            discord.SelectOption(label="🛑 Stop Bot",       value="stop",    description="Kill all python processes"),
            discord.SelectOption(label="💻 Shell Command",  value="shell",   description="Run any shell command on the server"),
        ],
        row=0,
    )
    async def action_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        action  = select.values[0]
        session = _get_user_sessions(self._user_id).get(self._name)
        if not session:
            return await interaction.response.send_message(
                f"❌ Session `{self._name}` not found. Run `/vds_add` again.", ephemeral=True
            )

        if action == "env":
            return await interaction.response.send_message(
                embed=_vds_embed("⚙️ Update .env", "Select which key to set or update:"),
                view=VdsEnvPickerView(self._name, self._user_id),
                ephemeral=True,
            )

        if action == "shell":
            return await interaction.response.send_modal(
                VdsShellModal(self._name, self._user_id)
            )

        await interaction.response.defer(thinking=True)

        if action == "pull":
            await _run_deploy_with_progress(interaction, session, self._name)
            return

        if action == "status":
            out   = await asyncio.get_event_loop().run_in_executor(None, _dispatch_status, session)
            embed = _vds_embed(
                f"📊 Status — `{self._name}`",
                f"`{session['ip']}:{session['port']}`  {'🐧 Ubuntu' if session.get('os')=='ubuntu' else '🪟 Windows'}"
                f"\n```\n{out[:3800]}\n```",
            )
        elif action == "start":
            out   = await asyncio.get_event_loop().run_in_executor(None, _dispatch_start, session)
            embed = _vds_embed(f"▶️ Started — `{self._name}`", f"```\n{out[:3800]}\n```", 0x00CC66)
        elif action == "stop":
            out   = await asyncio.get_event_loop().run_in_executor(None, _dispatch_stop, session)
            embed = _vds_embed(f"🛑 Stopped — `{self._name}`", f"```\n{out[:3800]}\n```", 0xFF4444)
        else:
            return await interaction.followup.send("❓ Unknown action.")

        await interaction.followup.send(embed=embed, view=VdsActionView(self._name, self._user_id))


# ── VDS picker views ───────────────────────────────────────────────────────────

class VdsPickerView(discord.ui.View):
    """Shows a select menu of all saved instances, then opens VdsActionView."""
    def __init__(self, user_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self._user_id = user_id
        sel = discord.ui.Select(
            placeholder="Select a VDS instance…",
            options=options[:25],
            row=0,
        )
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        name = interaction.data["values"][0]
        session = _get_user_sessions(self._user_id).get(name)
        if not session:
            return await interaction.response.send_message(f"❌ `{name}` not found.")
        os_label   = "🐧 Ubuntu" if session.get("os") == "ubuntu" else "🪟 Windows"
        branch_line = f"\n**Branch:** `{session['branch']}`" if session.get("branch") else ""
        embed = discord.Embed(
            title=f"🖥️ Managing: {name}",
            description=(
                f"**Host:** `{session['ip']}:{session['port']}`\n"
                f"**Path:** `{session['path']}`\n"
                f"**OS:** {os_label}{branch_line}\n"
                f"**Added:** <t:{int(session['ts'])}:R>"
            ),
            color=0x9945FF,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="VDS Manager — Choose an action below")
        await interaction.response.edit_message(
            content=None,
            embed=embed,
            view=VdsActionView(name, self._user_id),
        )


class VdsRemoveView(discord.ui.View):
    """Select instance to remove."""
    def __init__(self, user_id: int, options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self._user_id = user_id
        sel = discord.ui.Select(
            placeholder="Select instance to remove…",
            options=options[:25],
            row=0,
        )
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        name = interaction.data["values"][0]
        _get_user_sessions(self._user_id).pop(name, None)
        _save_sessions()
        await interaction.response.edit_message(
            content=f"🗑️ Removed `{name}`.",
            embed=None,
            view=None,
        )


def _github_deploy_keys_url() -> str:
    """Derive the GitHub Deploy Keys page URL from the repo URL."""
    url = _get_repo_url()
    # Convert SSH or HTTPS to https://github.com/user/repo
    if url.startswith("git@github.com:"):
        path = url.replace("git@github.com:", "").removesuffix(".git")
    elif "github.com" in url:
        path = (
            url.replace("https://github.com/", "")
               .replace("http://github.com/", "")
               .removesuffix(".git")
               .lstrip("/")
        )
    else:
        return "https://github.com"
    return f"https://github.com/{path}/settings/keys/new"


class _CopyKeyView(discord.ui.View):
    """Sends the public key as plain text so it's easy to copy."""
    def __init__(self, pub: str):
        super().__init__(timeout=300)
        self._pub = pub

    @discord.ui.button(label="📋 Copy Key", style=discord.ButtonStyle.secondary)
    async def copy_key(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            f"```\n{self._pub}\n```",
            ephemeral=True,
        )


class VdsManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check(self, user_id: int) -> bool:
        return _OWNER_ID != 0 and user_id == _OWNER_ID

    @app_commands.command(name="vds_add", description="[Owner] Add a new VDS/bot instance")
    async def vds_add(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        if not HAS_PARAMIKO:
            return await interaction.response.send_message(
                "❌ `paramiko` not installed.\nRun on main bot: `pip install paramiko`",
                ephemeral=True,
            )
        await interaction.response.send_message(
            "🖥️ **Add VDS Instance** — Select the operating system:",
            view=VdsOsPickView(),
        )

    @app_commands.command(name="vds_list", description="[Owner] List all saved VDS instances")
    async def vds_list(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        sessions = _get_user_sessions(interaction.user.id)
        if not sessions:
            return await interaction.response.send_message(
                "No VDS instances saved yet. Use `/vds_add`."
            )
        lines = [f"🖥️ **VDS Instances ({len(sessions)})**"]
        for name, s in sessions.items():
            os_icon = "🐧" if s.get("os") == "ubuntu" else "🪟"
            lines.append(
                f"\n{os_icon} **`{name}`** — `{s['ip']}:{s['port']}` | `{s['path']}` | Added <t:{int(s['ts'])}:R>"
            )
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="vds_manage", description="[Owner] Manage a VDS instance")
    async def vds_manage(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        options = _session_options(interaction.user.id)
        if not options:
            return await interaction.response.send_message(
                "No VDS instances saved yet. Use `/vds_add`."
            )
        await interaction.response.send_message(
            embed=_vds_embed(
                "🖥️ VDS Manager",
                f"Select an instance to manage.\n`{len(options)}` instance(s) saved.",
            ),
            view=VdsPickerView(interaction.user.id, options),
        )

    @app_commands.command(name="vds_remove", description="[Owner] Remove a saved VDS instance")
    async def vds_remove(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        options = _session_options(interaction.user.id)
        if not options:
            return await interaction.response.send_message("No VDS instances to remove.")
        await interaction.response.send_message(
            "🗑️ **Remove VDS Instance** — Select the instance to delete:",
            view=VdsRemoveView(interaction.user.id, options),
        )

    @app_commands.command(name="vds_clear", description="[Owner] Wipe ALL saved VDS sessions")
    async def vds_clear(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        count = len(_get_user_sessions(interaction.user.id))
        _sessions.pop(interaction.user.id, None)
        _save_sessions()
        await interaction.response.send_message(f"🗑️ Cleared {count} session(s).")

    @app_commands.command(
        name="vds_keygen",
        description="[Owner] Generate a GitHub Deploy Key — read-only repo access, no personal token needed",
    )
    async def vds_keygen(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        if not HAS_PARAMIKO:
            return await interaction.response.send_message(
                "❌ `paramiko` not installed. Run: `pip install paramiko`", ephemeral=True
            )
        await interaction.response.defer(thinking=True)
        priv, pub = await asyncio.get_event_loop().run_in_executor(None, _generate_deploy_key)
        _deploy_keys[interaction.user.id] = {"private": priv, "public": pub}

        # Persist to .env so key survives bot restarts
        try:
            from dotenv import set_key as _set_key
            env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            priv_b64 = base64.b64encode(priv.encode()).decode()
            _set_key(env_path, "VDS_DEPLOY_KEY_PRIV", priv_b64)
            _set_key(env_path, "VDS_DEPLOY_KEY_PUB", pub)
            persisted = True
        except Exception:
            persisted = False

        gh_url = _github_deploy_keys_url()
        persist_note = (
            "✅ Key saved to `.env` — survives bot restarts."
            if persisted else
            "⚠️ Could not save to `.env` — key is RAM-only this session."
        )
        await interaction.followup.send(
            f"🔑 **Deploy Key Generated**\n"
            f"{persist_note}\n\n"
            f"**1 — Add to GitHub:** {gh_url}\n"
            f"Paste below, leave *Allow write access* **unchecked**.\n"
            f"```\n{pub}\n```\n"
            f"**2 — Run Full Setup** via `/vds_manage` → Full Setup\n"
            f"*(Re-run `/vds_keygen` only if key is revoked on GitHub)*",
            view=_CopyKeyView(pub),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VdsManager(bot))


# ══════════════════════════════════════════════════════════════════════════════
#  DEPLOY WIZARD — for license buyers; NOT owner-only
#  /deploy_wizard  →  guided SSH + .env + Full Setup
# ══════════════════════════════════════════════════════════════════════════════

import urllib.request as _urllib_req
import json as _json_mod


def _has_valid_license(_discord_id: int) -> bool:
    return True  # License system removed — deploy wizard is owner-only


# Wizard .env keys — superset of owner _ENV_KEYS, includes license vars
_WIZARD_ENV_KEYS: list[tuple[str, str, str, bool]] = [
    ("TOKEN",                "Bot Token",                     "Discord bot token from Dev Portal",  False),
    ("PREFIX",               "Command Prefix",                ".",                                  False),
    ("GUILD_ID",             "Guild ID",                      "Your Discord server ID",             False),
    ("OWNER_ID",             "Owner Discord ID",              "Your Discord user ID",               False),
    ("SUPER_ADMIN_ID",       "Super Admin Discord ID",        "Your Discord user ID",               False),
    ("LICENSE_SERVER_URL",   "License Server URL",            "https://your-server.com",            False),
    ("LICENSE_DISCORD_ID",   "Your Discord ID (for license)", "123456789…",                         False),
    ("CRYPTO_MNEMONIC",      "Crypto Wallet Mnemonic",        "12-word BIP39 phrase",               True),
    ("TREASURY_MNEMONIC",    "Treasury Mnemonic",             "12-word BIP39 phrase",               True),
]


class WizardSshModal(discord.ui.Modal, title="Step 1 — SSH Connection"):
    name_f  = discord.ui.TextInput(label="Instance Name",    placeholder="my-bot",          max_length=50)
    conn_f  = discord.ui.TextInput(label="Host (IP:port)",   placeholder="1.2.3.4:22",      max_length=70)
    user_f  = discord.ui.TextInput(label="SSH User",         placeholder="ubuntu",           max_length=100)
    pwd_f   = discord.ui.TextInput(label="SSH Password",     placeholder="••••••••",         max_length=200)
    path_f  = discord.ui.TextInput(label="Install Path [::branch]", default="/opt/vegasbot", max_length=260)

    def __init__(self, os_type: str):
        super().__init__(title=f"Step 1 — SSH ({'Ubuntu' if os_type == 'ubuntu' else 'Windows'})", timeout=None)
        self._os_type = os_type

    async def on_submit(self, interaction: discord.Interaction):
        host_port = self.conn_f.value.strip()
        if ":" in host_port:
            ip, _, port_str = host_port.rpartition(":")
            try:
                port = int(port_str)
            except ValueError:
                ip, port = host_port, 22
        else:
            ip, port = host_port, 22

        path_branch = self.path_f.value.strip()
        if "::" in path_branch:
            path, branch = path_branch.split("::", 1)
        else:
            path, branch = path_branch, ""

        session = {
            "ip":             ip,
            "port":           port,
            "user":           self.user_f.value.strip(),
            "pass":           self.pwd_f.value,
            "path":           path,
            "branch":         branch,
            "ssh_url":        _get_repo_ssh_url(),
            "os":             self._os_type,
            "ts":             __import__("time").time(),
            "instance_token": __import__("secrets").token_hex(32),
        }
        await interaction.response.defer(thinking=True)
        ok, out = await asyncio.get_event_loop().run_in_executor(None, _dispatch_test_connection, session)
        if not ok:
            return await interaction.followup.send(f"❌ **SSH connection failed:**\n```\n{out[:1500]}\n```")

        name = self.name_f.value.strip()
        # Store temporarily under the user's session list
        _get_user_sessions(interaction.user.id)[name] = session
        _save_sessions()

        os_label = "🐧 Ubuntu" if self._os_type == "ubuntu" else "🪟 Windows"
        embed = _vds_embed(
            f"✅ Connected — `{name}`",
            f"SSH OK: `{ip}:{port}` as `{session['user']}`  {os_label}\n\n"
            f"**Step 2:** Configure your `.env` variables, then run Full Setup.",
        )
        await interaction.followup.send(
            embed=embed,
            view=WizardEnvView(name, interaction.user.id),
        )


class WizardOsView(discord.ui.View):
    """Wizard step 0 — pick OS."""
    def __init__(self, invoker: int):
        super().__init__(timeout=120)
        self._invoker = invoker

    @discord.ui.select(
        placeholder="Select server OS…",
        options=[
            discord.SelectOption(label="🐧 Ubuntu / Debian",  value="ubuntu",  description="python3, apt, venv, nohup"),
            discord.SelectOption(label="🪟 Windows Server",   value="windows", description="PowerShell, python.exe"),
        ],
    )
    async def os_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self._invoker:
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        await interaction.response.send_modal(WizardSshModal(os_type=select.values[0]))


class WizardEnvEditModal(discord.ui.Modal):
    """Single-field modal for wizard env editing."""
    def __init__(self, key: str, label: str, placeholder: str, is_long: bool,
                 session_name: str, user_id: int):
        super().__init__(title=f"Set {key[:40]}", timeout=300)
        self._key = key
        self._session_name = session_name
        self._user_id = user_id
        self.value_input = discord.ui.TextInput(
            label=label[:45],
            placeholder=placeholder,
            required=True,
            style=discord.TextStyle.paragraph if is_long else discord.TextStyle.short,
            max_length=500,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        session = _get_user_sessions(self._user_id).get(self._session_name)
        if not session:
            return await interaction.followup.send("❌ Session not found.")
        val = self.value_input.value.strip()
        if not val:
            return await interaction.followup.send("⚠️ Empty value, nothing changed.")
        out = await asyncio.get_event_loop().run_in_executor(
            None, _dispatch_write_env, session, {self._key: val}
        )
        await interaction.followup.send(
            embed=_vds_embed(
                f"⚙️ `.env` Updated — `{self._key}`",
                f"Value written to `{self._session_name}`.\n```\n{out[:600]}\n```",
            ),
            view=WizardEnvView(self._session_name, self._user_id),
        )


class WizardEnvView(discord.ui.View):
    """Wizard step 2 — configure .env + trigger Full Setup."""
    def __init__(self, session_name: str, user_id: int):
        super().__init__(timeout=600)
        self._name    = session_name
        self._user_id = user_id
        options = [
            discord.SelectOption(label=label, value=key, description=ph[:50])
            for key, label, ph, _ in _WIZARD_ENV_KEYS
        ]
        self.env_select.options = options

    @discord.ui.select(placeholder="⚙️  Set a .env variable…", row=0)
    async def env_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self._user_id:
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        key = select.values[0]
        _, label, placeholder, is_long = next(x for x in _WIZARD_ENV_KEYS if x[0] == key)
        await interaction.response.send_modal(
            WizardEnvEditModal(key, label, placeholder, is_long, self._name, self._user_id)
        )

    @discord.ui.button(label="🚀 Full Setup & Deploy", style=discord.ButtonStyle.green, row=1)
    async def run_full_setup(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self._user_id:
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        session = _get_user_sessions(self._user_id).get(self._name)
        if not session:
            return await interaction.response.send_message("❌ Session not found.")

        await interaction.response.defer(thinking=True)
        ssh_url = session.get("ssh_url") or _get_repo_ssh_url()
        deploy_key_data = _deploy_keys.get(interaction.user.id) or _deploy_keys.get(
            int(os.getenv("OWNER_ID", "0")), {}
        )
        priv_key = deploy_key_data.get("private")

        # Auto-inject auth credentials so the deployed bot can phone home
        auth_env: dict = {}
        token = session.get("instance_token", "")
        auth_url = os.getenv("MAIN_BOT_AUTH_URL", "")
        auth_secret = os.getenv("AUTH_SECRET", "")
        if token:
            auth_env["INSTANCE_TOKEN"] = token
        if auth_url:
            auth_env["MAIN_BOT_AUTH_URL"] = auth_url
        if auth_secret:
            auth_env["AUTH_SECRET"] = auth_secret

        out = await asyncio.get_event_loop().run_in_executor(
            None, _dispatch_full_setup, session, auth_env, ssh_url, priv_key
        )
        lines = out[:3000]
        embed = _vds_embed(
            f"🚀 Full Setup Complete — `{self._name}`",
            f"```\n{lines}\n```",
            0x00CC66,
        )
        await interaction.followup.send(embed=embed, view=VdsActionView(self._name, self._user_id))

    @discord.ui.button(label="📊 Status Check", style=discord.ButtonStyle.secondary, row=1)
    async def status_check(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self._user_id:
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        session = _get_user_sessions(self._user_id).get(self._name)
        if not session:
            return await interaction.response.send_message("❌ Session not found.")
        await interaction.response.defer(thinking=True)
        out = await asyncio.get_event_loop().run_in_executor(None, _dispatch_status, session)
        await interaction.followup.send(
            embed=_vds_embed(f"📊 Status — `{self._name}`", f"```\n{out[:3500]}\n```"),
            view=WizardEnvView(self._name, self._user_id),
        )


class DeployWizardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check(self, user_id: int) -> bool:
        return _OWNER_ID != 0 and user_id == _OWNER_ID

    @app_commands.command(name="deploy_wizard", description="[Owner] Set up a VPS step-by-step with guided SSH + .env + Full Setup")
    async def deploy_wizard(self, interaction: discord.Interaction):
        if not self._check(interaction.user.id):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        if not HAS_PARAMIKO:
            return await interaction.response.send_message(
                "❌ `paramiko` is not installed on this bot. Contact the owner.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="🧙 VPS Deploy Wizard",
            description=(
                "This wizard will guide you through setting up your VegasBot on a VPS.\n\n"
                "**Steps:**\n"
                "1. Choose your server OS\n"
                "2. Enter SSH credentials\n"
                "3. Configure `.env` variables\n"
                "4. Run Full Setup (clone, pip install, start)\n\n"
                "Make sure you have SSH access to your VPS before continuing."
            ),
            color=0x9945FF,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="VDS Deploy Wizard")
        await interaction.response.send_message(
            embed=embed,
            view=WizardOsView(interaction.user.id),
            ephemeral=True,
        )


async def _setup_wizard(bot: commands.Bot):
    await bot.add_cog(DeployWizardCog(bot))


# Both cogs are loaded from a single setup() entry point
# (bot.py calls `await bot.load_extension("cogs.vds_manager")`)
async def setup(bot: commands.Bot):
    await bot.add_cog(VdsManager(bot))
    await bot.add_cog(DeployWizardCog(bot))

