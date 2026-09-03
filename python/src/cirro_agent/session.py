"""The session folder: one directory, one Cirro tenant, one credential.

`CIRRO_HOME` is what the SDK uses to locate both `config.ini` and the cached
token, and `cirro.config.Constants` freezes it at import time. So this module
never imports cirro and never mutates its own environment -- it only computes
the environment a child process will be started with.

The default the SDK would fall back to is `~/.cirro`, the user's real global
credential. Every rule here exists to make falling back there impossible.
"""
import json
import os
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from . import errors

SESSION_DIRNAME = ".cirro"
SESSION_FILE = "session.json"
AUTH_DEVICE_CODE = "device_code"


@dataclass
class Session:
    base_url: str
    auth: str = AUTH_DEVICE_CODE
    client_id: Optional[str] = None
    dir: Optional[str] = None  # filled in on load; not persisted

    @property
    def path(self) -> Path:
        return Path(self.dir)


def _git_root(start: Path) -> Optional[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _search_ceiling(start: Path) -> Path:
    """Highest directory the upward walk is allowed to reach."""
    home = Path.home().resolve()
    candidates = []
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir).resolve())
    root = _git_root(start)
    if root:
        candidates.append(root.resolve())
    # Deepest ceiling wins: it is the tightest bound on the search.
    inside = [c for c in candidates if c == start or c in start.parents]
    if inside:
        return max(inside, key=lambda p: len(p.parts))
    return home


def find_session_dir(start: Optional[Path] = None) -> Path:
    """Walk up for a `.cirro/session.json`. Refuse every unsafe resolution."""
    start = (start or Path.cwd()).resolve()
    ceiling = _search_ceiling(start)
    home = Path.home().resolve()

    here = start
    while True:
        candidate = here / SESSION_DIRNAME
        if (candidate / SESSION_FILE).is_file():
            if candidate.resolve() == (home / SESSION_DIRNAME):
                raise errors.NoSessionFolder(
                    f"Refusing to use {candidate}: that is the global Cirro credential "
                    "shared by every tool on this machine, not a session folder.",
                    what="Create a session folder inside the working directory instead.",
                    user_action=(
                        "Use session_init from the directory you are working in "
                        "to create a folder-local session."
                    ),
                )
            return candidate
        if here == ceiling or here.parent == here:
            break
        here = here.parent

    raise errors.NoSessionFolder(
        f"No Cirro session folder found in {start} or any parent up to {ceiling}.",
        what="A session folder holds this directory's Cirro credential and cache.",
        user_action=("Create one with the session_init MCP tool (or the "
                     "cirro-session skill), giving the Cirro hostname."),
        searched_from=str(start),
        ceiling=str(ceiling),
    )


def load(start: Optional[Path] = None) -> Session:
    session_dir = find_session_dir(start)
    raw = json.loads((session_dir / SESSION_FILE).read_text())
    session = Session(
        base_url=raw["base_url"],
        auth=raw.get("auth", AUTH_DEVICE_CODE),
        client_id=raw.get("client_id"),
        dir=str(session_dir),
    )

    # The SDK would happily read an ambient CIRRO_BASE_URL. We refuse to, because
    # an env var is invisible in the transcript and could silently be production.
    ambient = os.environ.get("CIRRO_BASE_URL")
    if ambient and _normalize_base_url(ambient) != session.base_url:
        raise errors.TenantMismatch(
            f"CIRRO_BASE_URL is set to {ambient!r} but this session folder is bound to "
            f"{session.base_url!r}.",
            what="One session folder serves exactly one Cirro tenant.",
            user_action=(
                f"Unset CIRRO_BASE_URL, or work in a different directory with its own "
                f"session folder for {ambient}."
            ),
            session_dir=str(session_dir),
        )
    return session


def _normalize_base_url(base_url: str) -> str:
    url = base_url.strip()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.split("/")[0]


def init(workdir: Path, base_url: str, auth: str = AUTH_DEVICE_CODE) -> Session:
    """Create the session folder. Never called implicitly."""
    workdir = workdir.resolve()
    home = Path.home().resolve()
    session_dir = workdir / SESSION_DIRNAME
    if session_dir == home / SESSION_DIRNAME:
        raise errors.NoSessionFolder(
            "Refusing to create a session folder at ~/.cirro -- that path is the global "
            "Cirro credential.",
            what="Choose a project directory.",
            user_action="Change to the directory you are working in and try again.",
        )

    # save_user_config does a non-recursive mkdir, so every level must exist first.
    for sub in ("", "plans", "runs", "cache/snapshots", "downloads"):
        (session_dir / sub).mkdir(parents=True, exist_ok=True)

    # Self-ignoring, so we never have to edit a file the user tracks.
    (session_dir / ".gitignore").write_text("*\n")

    normalized = _normalize_base_url(base_url)
    existing = session_dir / SESSION_FILE
    if existing.is_file():
        try:
            bound = json.loads(existing.read_text()).get("base_url")
        except (OSError, ValueError):
            bound = None
        if bound and bound != normalized:
            # Repointing a folder at another tenant redirects the next login to a host
            # the user did not choose, so it is never done implicitly.
            raise errors.TenantMismatch(
                f"{session_dir} is already bound to {bound!r}; refusing to rebind it to "
                f"{normalized!r}.",
                what="One session folder serves exactly one Cirro tenant.",
                user_action=(f"Work in a different directory for {normalized}, or delete "
                             f"{session_dir} first if you really mean to repoint it."),
                session_dir=str(session_dir),
            )

    session = Session(base_url=normalized, auth=auth, dir=str(session_dir))
    save(session)
    return session


def save(session: Session) -> None:
    payload = {k: v for k, v in asdict(session).items() if k != "dir" and v is not None}
    path = session.path / SESSION_FILE
    write_private(path, json.dumps(payload, indent=2) + "\n")


def write_private(path: Path, text: str) -> None:
    """Write atomically, never leaving the content readable at the process umask."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
        stream.write(text)
    tmp.replace(path)


def is_git_tracked(session_dir: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(session_dir.parent), "ls-files", "--error-unmatch",
             str(session_dir)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def child_env(session: Session) -> dict:
    """The environment a worker subprocess is started with.

    CIRRO_HOME is set here and only here. It must be in place before the child's
    interpreter starts, because cirro.config.Constants reads it at import time.
    """
    env = dict(os.environ)
    env["CIRRO_HOME"] = str(session.path.resolve())
    env["CIRRO_BASE_URL"] = session.base_url
    env.pop("PYTHONSTARTUP", None)
    return env
