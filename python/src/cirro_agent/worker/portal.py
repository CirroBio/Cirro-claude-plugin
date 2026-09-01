"""Building an authenticated client without ever blocking.

The hazard, verified in the SDK: `get_auth_info_from_config` constructs
`DeviceCodeAuth` without passing `await_completion=False`, so it defaults to True.
With no cached token that calls `_authenticate()`, which polls until the device
code expires. A bare `DataPortal()` is therefore an unbounded hang, and nothing
here may use one.

Instead we build `DeviceCodeAuth` ourselves with `enable_cache=True` and
`await_completion=False`, and treat "no token was loaded" as a typed error rather
than an invitation to log in.
"""
import os
import socket
from pathlib import Path
from typing import Optional, Tuple

from .. import errors

AUTH_DEVICE_CODE = "device_code"
AUTH_CLIENT_CREDENTIALS = "client_credentials"
AUTH_ACCESS_TOKEN = "access_token"

_LOGIN_HINT = "Run the cirro-session skill, or the login_start MCP tool, to log in."


def probe_network(base_url: str) -> None:
    """Separate DNS failure from TCP failure from a wrong hostname.

    The SDK collapses all three into one `RuntimeError('Failed connecting to ...')`,
    but a VPN outage and a typo'd tenant need different actions from the human.
    """
    host = base_url.split("/")[0]
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        raise errors.NetworkUnavailable(
            f"Cannot resolve {host!r}: {exc.strerror or exc}.",
            what="The hostname does not resolve from here.",
            user_action=(
                f"Check that {host!r} is the right Cirro hostname and that DNS/VPN is up."
            ),
        )
    try:
        with socket.create_connection((host, 443), timeout=8):
            pass
    except OSError as exc:
        raise errors.NetworkUnavailable(
            f"Cannot reach {host}:443: {exc}.",
            what="The host resolves but refuses or drops the connection.",
            user_action=f"Check network access to {host} (VPN, firewall, proxy).",
        )


def app_config(base_url: str):
    """AppConfig always makes an unauthenticated GET /api/info/system."""
    from cirro.config import AppConfig
    probe_network(base_url)
    try:
        return AppConfig(base_url=base_url)
    except RuntimeError as exc:
        raise errors.UpstreamError(
            f"Cirro at {base_url} did not return usable system info: {exc}",
            what="The host is reachable but did not answer /api/info/system.",
            user_action=f"Confirm {base_url} is a Cirro deployment and is healthy.",
            retry_with=[{"note": "retry once; this endpoint is occasionally slow"}],
        )


def token_path(config) -> Path:
    home = os.environ["CIRRO_HOME"]
    return Path(home, f"{config.client_id}.token.dat")


def auth_mode() -> str:
    if os.environ.get("CIRRO_ACCESS_TOKEN"):
        return AUTH_ACCESS_TOKEN
    if os.environ.get("CIRRO_CLIENT_ID") and os.environ.get("CIRRO_CLIENT_SECRET"):
        return AUTH_CLIENT_CREDENTIALS
    return AUTH_DEVICE_CODE


def build_auth(base_url: str, *, start_flow: bool = False) -> Tuple[object, object]:
    """Return (auth_info, app_config), or raise a typed auth error.

    `start_flow` is only ever True from the login path. Every other caller gets a
    refusal rather than a side effect.
    """
    config = app_config(base_url)
    mode = auth_mode()

    if mode == AUTH_ACCESS_TOKEN:
        from cirro.auth.access_token import AccessTokenAuth
        try:
            return AccessTokenAuth(os.environ["CIRRO_ACCESS_TOKEN"]), config
        except Exception as exc:
            raise errors.AuthExpired(
                f"CIRRO_ACCESS_TOKEN is not a usable JWT: {exc}",
                what="The static token could not be decoded.",
                user_action="Replace CIRRO_ACCESS_TOKEN with a current token.",
            )

    if mode == AUTH_CLIENT_CREDENTIALS:
        from cirro.auth.client_creds import ClientCredentialsAuth
        try:
            return ClientCredentialsAuth(
                os.environ["CIRRO_CLIENT_ID"], os.environ["CIRRO_CLIENT_SECRET"],
                auth_endpoint=config.auth_endpoint,
            ), config
        except Exception as exc:
            raise errors.AuthRequired(
                f"Client-credentials login failed: {exc}",
                what="The OAuth app credentials were rejected.",
                user_action="Check CIRRO_CLIENT_ID / CIRRO_CLIENT_SECRET for this tenant.",
            )

    from cirro.auth.device_code import DeviceCodeAuth

    cached = token_path(config)
    if not cached.exists() and not start_flow:
        # Checked before construction so a read never starts a device-code flow.
        raise errors.AuthRequired(
            f"Not logged in to {base_url}.",
            what="This session folder holds no Cirro credential yet.",
            user_action=_LOGIN_HINT,
            session_dir=os.environ["CIRRO_HOME"],
        )

    auth = DeviceCodeAuth(
        client_id=config.client_id,
        region=config.region,
        auth_endpoint=config.auth_endpoint,
        enable_cache=True,       # without this nothing is persisted at all
        await_completion=False,  # without this the constructor can block for minutes
    )
    if auth._token_info is None and not start_flow:
        raise errors.AuthExpired(
            f"The stored credential for {base_url} is no longer usable.",
            what=(
                "The SDK discards a cached token once its refresh token is within 12 "
                "hours of expiry."
            ),
            user_action=_LOGIN_HINT,
        )
    return auth, config


def identity(auth) -> Optional[str]:
    try:
        return auth.get_current_user()
    except Exception:
        return None


def wrap_upstream(exc: Exception, *, what: str, retry_with=None):
    """Map an SDK exception onto the taxonomy, including auth frames."""
    from cirro.sdk.exceptions import DataPortalAssetNotFound, DataPortalInputError

    if isinstance(exc, DataPortalAssetNotFound):
        return errors.NotFound(
            str(exc), what=what,
            retry_with=retry_with or [{"note": "list the collection first"}])
    if isinstance(exc, DataPortalInputError):
        return errors.AmbiguousName(str(exc), what=what,
                                    retry_with=retry_with or [{"note": "use an ID"}])
    if isinstance(exc, RuntimeError):
        frames = []
        tb = exc.__traceback__
        while tb:
            frames.append(tb.tb_frame.f_globals.get("__name__", ""))
            tb = tb.tb_next
        # Matching the SDK's error strings is brittle, so any RuntimeError raised
        # from inside cirro.auth counts as needing a fresh login.
        if any(name.startswith("cirro.auth") for name in frames):
            return errors.AuthExpired(
                str(exc), what="Re-authentication is required.", user_action=_LOGIN_HINT)
    return errors.UpstreamError(
        f"{type(exc).__name__}: {exc}", what=what,
        retry_with=retry_with or [{"note": "retry once"}],
        user_action="If this persists, check the Cirro web UI for service status.",
    )
