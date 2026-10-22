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
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import errors

SESSION_DIRNAME = ".cirro"
SESSION_FILE = "session.json"
AUTH_DEVICE_CODE = "device_code"
TENANTS_DIRNAME = "tenants"

# A tenant folder is named after its host, and that name is used to both create and
# delete a directory. `_normalize_base_url` strips the scheme and path but would
# return `..` for `../..` quite happily, which as a tenant path is the virtualenv.
TENANT_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*(:[0-9]{1,5})?\Z")

# Credentials a worker would take from its environment ahead of the folder's own.
AMBIENT_CREDENTIALS = ("CIRRO_ACCESS_TOKEN", "CIRRO_CLIENT_ID", "CIRRO_CLIENT_SECRET")

# Session resolution runs on the way to every operation, so it waits only briefly on
# git. Callers already inside a user's checkout pass their own, longer bound.
GIT_TIMEOUT = 5


@dataclass
class Session:
    base_url: str
    auth: str = AUTH_DEVICE_CODE
    client_id: Optional[str] = None
    dir: Optional[str] = None  # filled in on load; not persisted

    @property
    def path(self) -> Path:
        return Path(self.dir)


def git_output(repo: Path, *arguments: str, timeout: float = GIT_TIMEOUT) -> Optional[str]:
    """`git -C repo ...` stdout stripped, or None if git failed or could not be run.

    The one place this plugin asks git a question. Every caller reads git state it
    can do without -- a repository root, a version, a remote -- so a missing git
    binary, a directory outside any checkout and a non-zero exit are all one
    answer. An empty string is not that answer: it means git succeeded and printed
    nothing, which the caller may treat as it likes.

    It lives here because `session` is the stdlib-only module the parent process
    and `scripts/gate.py` both import, and because session resolution was the
    first caller.
    """
    try:
        done = subprocess.run(["git", "-C", str(repo), *arguments],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_root(start: Path, timeout: float = GIT_TIMEOUT) -> Optional[Path]:
    """The checkout `start` is inside, or None if it is not inside one."""
    top = git_output(start, "rev-parse", "--show-toplevel", timeout=timeout)
    return Path(top) if top else None


def tenants_root() -> Path:
    """Where tenant folders live: one per Cirro host, outside every checkout.

    Under `plugin_data()` for the reason that function gives -- a directory
    marketplace copies the plugin root verbatim into every install, so a credential
    left there is shipped to everyone who installs the plugin. This is the same
    argument that keeps the virtualenv out, arriving at the same answer.
    """
    # Imported here rather than at module scope: `runner` imports this module, and
    # `plugin_data` is defined below that import.
    from .runner import plugin_data
    return plugin_data() / TENANTS_DIRNAME


def tenant_host(base_url: str) -> str:
    """The directory name for a tenant, refused unless it is a plausible hostname.

    This is where a "host" that is not a host stops, before it becomes a path that
    something creates or deletes.
    """
    host = _normalize_base_url(base_url)
    if not TENANT_HOST.match(host):
        raise errors.InvalidQuery(
            f"{base_url!r} does not name a Cirro host.",
            what="A tenant folder is named after the hostname it serves.",
            field="base_url", given=base_url,
            retry_with=[{"base_url": "app.cirro.bio"}],
        )
    return host


def tenant_dir(base_url: str) -> Path:
    """The working directory for one tenant. Its `.cirro/` is the session folder."""
    return tenants_root() / tenant_host(base_url)


def tenant_contents(session_dir: Path) -> dict:
    """What a session folder holds: a credential, an audit trail, and how stale it is.

    One implementation, so the figures a person is shown by a listing are the figures
    they approve a removal against.
    """
    def count(sub: str, pattern: str) -> int:
        folder = session_dir / sub
        return len(list(folder.glob(pattern))) if folder.is_dir() else 0

    newest = max((found.stat().st_mtime for found in session_dir.rglob("*")
                  if found.is_file()), default=None)
    return {
        # Presence only. Nothing here reads a credential, on any code path.
        "credential": "present" if any(session_dir.glob("*.token.dat")) else "none",
        "plans": count("plans", "plan_*.json"),
        "runs": count("runs", "*.jsonl"),
        "last_used": (datetime.fromtimestamp(newest, timezone.utc).isoformat()
                      if newest else None),
    }


def remove_tenant(base_url: str) -> dict:
    """Delete one tenant's session folder, with its credential and its audit trail.

    The path is derived from the host and never supplied, so this can only ever reach
    a direct child of `tenants_root()` -- a host resolving anywhere else, through `..`
    or through a symlinked tenant folder, is refused before anything is removed.
    """
    target = tenant_dir(base_url)
    session_dir = target / SESSION_DIRNAME
    if (target.resolve().parent != tenants_root().resolve()
            or not (session_dir / SESSION_FILE).is_file()):
        raise errors.NotFound(
            f"No tenant folder is signed in for {tenant_host(base_url)}.",
            what="Only a tenant folder can be removed here, by the host it serves.",
            field="base_url", given=base_url,
            candidates=[found.parent.name for found in bound_tenants()],
            retry_with=[{"note": "list them with `cirro-agent read tenants`"}],
        )
    contents = tenant_contents(session_dir)
    shutil.rmtree(target)
    return {"host": target.name, "removed": str(target), **contents}


def is_tenant_dir(session_dir: Path) -> bool:
    """Whether a folder is a tenant's rather than one someone bound.

    Both sides are resolved: the walk returns a resolved path and the tenants root is
    built from `Path.home()`, so a symlink anywhere above either would otherwise make
    a tenant folder look like a bound directory.
    """
    return session_dir.resolve().parent.parent == tenants_root().resolve()


def bound_tenants() -> list:
    """Every tenant folder that has been signed in to, ordered by host."""
    root = tenants_root()
    if not root.is_dir():
        return []
    return sorted((host / SESSION_DIRNAME for host in root.iterdir()
                   if (host / SESSION_DIRNAME / SESSION_FILE).is_file()),
                  key=lambda found: found.parent.name)


def _search_ceiling(start: Path) -> Path:
    """Highest directory the upward walk is allowed to reach."""
    home = Path.home().resolve()
    candidates = []
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir).resolve())
    root = git_root(start)
    if root:
        candidates.append(root.resolve())
    # Deepest ceiling wins: it is the tightest bound on the search.
    inside = [c for c in candidates if c == start or c in start.parents]
    if inside:
        return max(inside, key=lambda p: len(p.parts))
    return home


def find_session_dir(start: Optional[Path] = None) -> Path:
    """Walk up for a `.cirro/session.json`. Refuse every unsafe resolution.

    `start` defaults to the process's working directory, which is the session's own.
    Passing it explicitly is how one Claude session works across several bound
    folders without being moved -- the resolved folder is echoed in every response,
    so which tenant an operation hit stays visible either way.
    """
    start = (start or Path.cwd()).expanduser()
    if not start.is_dir():
        raise errors.NoSessionFolder(
            f"{start} is not a directory, so there is nothing to resolve a Cirro "
            "session from.",
            what="A session folder is found by walking up from a real directory.",
            user_action="Give the path of a directory that exists.",
            searched_from=str(start),
        )
    start = start.resolve()
    ceiling = _search_ceiling(start)

    here = start
    while True:
        candidate = here / SESSION_DIRNAME
        if (candidate / SESSION_FILE).is_file():
            if is_global_folder(candidate):
                raise errors.NoSessionFolder(
                    f"Refusing to use {candidate}: that is the global Cirro credential "
                    "shared by every tool on this machine, not a session folder.",
                    what="Create a session folder inside the working directory instead.",
                    user_action=(
                        "Use session_init from the directory you are working in "
                        "to create a folder-local session."
                    ),
                )
            if is_git_tracked(candidate):
                # A session folder decides which host every request goes to and holds
                # the credential sent there. One that arrived in a checkout is the
                # choice of whoever committed it -- and the nearest folder wins, ahead
                # of the tenant this person signed in to.
                raise errors.NoSessionFolder(
                    f"Refusing to use {candidate}: it is tracked by git, so it came "
                    "with the repository rather than from a sign-in on this machine.",
                    what="A session folder is created by session_init, never committed.",
                    user_action=(
                        f"If it is yours, untrack it (git rm -r --cached {candidate}); "
                        "its own .gitignore keeps it out after that. If not, delete it "
                        "and sign in with session_init."),
                )
            return candidate
        if here == ceiling or here.parent == here:
            break
        here = here.parent

    # Nothing bound near the work, so fall back to a tenant folder -- but only when
    # there is exactly one, because choosing between tenants is the one thing this
    # resolver has never done. The fallback finds; it never creates.
    tenants = bound_tenants()
    if len(tenants) == 1:
        return tenants[0]
    if tenants:
        raise errors.NoSessionFolder(
            f"No Cirro session folder found in {start} or any parent up to {ceiling}, "
            f"and {len(tenants)} tenants are signed in, so there is no single one to "
            "fall back to.",
            what="One session folder serves exactly one Cirro tenant.",
            user_action=("Name the tenant folder to work against, with --dir on a read "
                         "or `directory` on an MCP tool."),
            candidates=[str(found.parent) for found in tenants],
            searched_from=str(start),
            ceiling=str(ceiling),
        )

    raise errors.NoSessionFolder(
        f"No Cirro session folder found in {start} or any parent up to {ceiling}, and "
        "no tenant is signed in.",
        what="A session folder holds a Cirro credential and cache.",
        user_action=("Create one with the session_init MCP tool (or the "
                     "cirro-session skill), giving the Cirro hostname. Pass "
                     "shared=true to sign in once for the tenant instead of for "
                     "this directory alone."),
        searched_from=str(start),
        ceiling=str(ceiling),
    )


def load(start: Optional[Path] = None) -> Session:
    session_dir = find_session_dir(start)
    raw = json.loads((session_dir / SESSION_FILE).read_text())
    try:
        # What session_init writes is always a bare host. Anything else was written by
        # hand, and `app.cirro.bio@evil.example` reaches evil.example.
        base_url = tenant_host(str(raw.get("base_url") or ""))
    except errors.AgentError:
        raise errors.NoSessionFolder(
            f"{session_dir} names {raw.get('base_url')!r} as its tenant, which is not "
            "a hostname.",
            what="A session folder is bound to one Cirro host, by name.",
            user_action=(f"Delete {session_dir} and sign in again with session_init."),
        ) from None
    session = Session(
        base_url=base_url,
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


def init_tenant(base_url: str, auth: str = AUTH_DEVICE_CODE) -> Session:
    """Create the session folder for a tenant rather than for one directory.

    Every directory with nothing bound nearer resolves to it, so one sign-in covers
    the whole machine -- and it sits somewhere no install overwrites and no checkout
    carries, which a project directory cannot promise.
    """
    workdir = tenant_dir(base_url)
    workdir.mkdir(parents=True, exist_ok=True)
    return init(workdir, base_url, auth)


def init(workdir: Path, base_url: str, auth: str = AUTH_DEVICE_CODE) -> Session:
    """Create the session folder. Never called implicitly."""
    workdir = workdir.resolve()
    session_dir = workdir / SESSION_DIRNAME
    if is_global_folder(session_dir):
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
    write_private(session_dir / ".gitignore", "*\n")

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
    """Write atomically, at 0600, and never through a symlink.

    A session folder can arrive in a copied directory, and a link planted at the
    temporary name was followed: the write landed wherever the link pointed. The
    temporary file is now created fresh -- O_EXCL refuses a link as well as a file --
    and the rename replaces a link at the final name rather than writing through it.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "w") as stream:
        stream.write(text)
    tmp.replace(path)


def is_global_folder(path: Path) -> bool:
    """Is `path` the machine-wide ~/.cirro, under any spelling?

    Compared by identity rather than by string: on a case-insensitive volume -- macOS
    by default -- /Users/Me/.CIRRO is the same folder as ~/.cirro, and resolve() does
    not canonicalize case, so a string comparison let it through.
    """
    try:
        return (path.name.lower() == SESSION_DIRNAME
                and os.path.samefile(path.parent, Path.home()))
    except OSError:
        return False


def is_git_tracked(session_dir: Path) -> bool:
    return git_output(session_dir.parent, "ls-files", "--error-unmatch",
                      str(session_dir)) is not None


def child_env(session: Session) -> dict:
    """The environment a worker subprocess is started with.

    CIRRO_HOME is set here and only here. It must be in place before the child's
    interpreter starts, because cirro.config.Constants reads it at import time.
    """
    env = dict(os.environ)
    if not os.environ.get("CIRRO_BASE_URL"):
        # A credential in the environment is for the host the environment names, and
        # `load` refuses a folder bound elsewhere when one is named. With none named,
        # the folder's host would receive it -- whatever host a copied folder names.
        for name in AMBIENT_CREDENTIALS:
            env.pop(name, None)
    env["CIRRO_HOME"] = str(session.path.resolve())
    env["CIRRO_BASE_URL"] = session.base_url
    env.pop("PYTHONSTARTUP", None)
    return env
