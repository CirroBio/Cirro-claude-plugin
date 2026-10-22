"""Worker entry point. The only process in this plugin that imports cirro.

Run as `python -m cirro_agent.worker`, reading one JSON request on stdin and
writing one JSON response on stdout. A fresh process per operation is what makes
CIRRO_HOME safe: `cirro.config.Constants` freezes it at import time, so a
long-lived process could only ever serve one session folder.
"""
import json
import os
import sys
from pathlib import Path

# Stdlib only, so importing it cannot import the SDK ahead of the guard below.
from ..session import is_global_folder


def _guard() -> Path:
    """Prove CIRRO_HOME took effect. The failure this catches is otherwise silent."""
    if "cirro" in sys.modules:
        raise RuntimeError(
            "cirro was imported before the CIRRO_HOME guard ran; the SDK would write to "
            "the caller's global ~/.cirro"
        )
    home = os.environ.get("CIRRO_HOME")
    if not home:
        raise RuntimeError("CIRRO_HOME is not set; refusing to fall back to ~/.cirro")
    resolved = Path(home).resolve()
    if is_global_folder(resolved):
        raise RuntimeError(f"CIRRO_HOME points at the global credential {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)

    from cirro.config import Constants
    # Constants.config_path is a class-body expression evaluated at import time. This
    # assertion is the only check that proves the environment beat the import.
    actual = Path(Constants.config_path).parent.resolve()
    if actual != resolved:
        raise RuntimeError(
            f"CIRRO_HOME did not take effect: the SDK resolved its home to {actual}, "
            f"not {resolved}. It must be set in the environment before python starts."
        )
    return resolved


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except ValueError as exc:
        json.dump({"ok": False, "error": {"code": "INTERNAL",
                                          "message": f"unreadable request: {exc}"}},
                  sys.stdout)
        return 1

    try:
        _guard()
    except RuntimeError as exc:
        json.dump({
            "ok": False,
            "error": {
                "code": "SESSION_UNSAFE", "message": str(exc), "severity": "fatal",
                "remediation": {
                    "what": "The session folder could not be established safely.",
                    "candidates": [], "retry_with": [],
                    "user_action": "Run /cirro:doctor, then /cirro:setup.",
                },
            },
        }, sys.stdout)
        return 1

    from . import dispatch
    json.dump(dispatch.run(request), sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
