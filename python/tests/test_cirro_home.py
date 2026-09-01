"""The CIRRO_HOME import-order contract.

The highest-value test here, because the failure it guards against is silent: the
SDK writes credentials to the user's real ~/.cirro and nothing complains.
`cirro.config.Constants.config_path` is a class-body expression, so the variable
must be in the environment before the interpreter starts.
"""
import json
import subprocess
import sys
from pathlib import Path

PROBE = """
import json, os, sys
from pathlib import Path
from cirro.config import Constants
print(json.dumps({"config_path": str(Constants.config_path),
                  "home": str(Constants.home)}))
"""

LATE_SET_PROBE = """
import json, os
import cirro.config as config          # import FIRST
os.environ["CIRRO_HOME"] = {target!r}  # then try to change it
from pathlib import Path
print(json.dumps({{"config_path": str(config.Constants.config_path)}}))
"""


def _run(code: str, env_extra: dict, src: Path) -> dict:
    import os
    env = dict(os.environ)
    env.pop("CIRRO_HOME", None)
    env.update(env_extra)
    env["PYTHONPATH"] = str(src)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _src() -> Path:
    import cirro_agent
    return Path(cirro_agent.__file__).resolve().parents[1]


def test_cirro_home_set_before_start_relocates_the_config(tmp_path):
    target = tmp_path / "session" / ".cirro"
    target.mkdir(parents=True)
    result = _run(PROBE, {"CIRRO_HOME": str(target)}, _src())
    assert Path(result["config_path"]).parent == target


def test_setting_cirro_home_after_import_does_not_move_it(tmp_path):
    """The trap, encoded so a refactor cannot quietly reintroduce it."""
    target = tmp_path / "session" / ".cirro"
    target.mkdir(parents=True)
    result = _run(LATE_SET_PROBE.format(target=str(target)), {}, _src())
    assert Path(result["config_path"]).parent != target
    assert Path(result["config_path"]).parent == Path.home() / ".cirro", (
        "a late assignment leaks to the user's global credential -- which is exactly "
        "why the worker sets CIRRO_HOME in a child's environment instead")


GUARD_PROBE = """
import sys
sys.path.insert(0, {src!r})
{preamble}
from cirro_agent.worker.__main__ import _guard
try:
    print("OK", _guard())
except RuntimeError as exc:
    print("REFUSED", exc)
"""


def _guard_result(env_extra: dict, preamble: str = "") -> str:
    import os
    env = dict(os.environ)
    env.pop("CIRRO_HOME", None)
    env.update(env_extra)
    code = GUARD_PROBE.format(src=str(_src()), preamble=preamble)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=60)
    return done.stdout.strip()


def test_guard_accepts_a_correctly_prepared_environment(tmp_path):
    target = tmp_path / "s" / ".cirro"
    assert _guard_result({"CIRRO_HOME": str(target)}).startswith("OK")


def test_guard_refuses_when_cirro_home_is_unset(tmp_path):
    assert "REFUSED" in _guard_result({})


def test_guard_refuses_the_global_credential_directory():
    out = _guard_result({"CIRRO_HOME": str(Path.home() / ".cirro")})
    assert "REFUSED" in out and "global credential" in out


def test_guard_refuses_if_cirro_was_already_imported(tmp_path):
    target = tmp_path / "s" / ".cirro"
    out = _guard_result({"CIRRO_HOME": str(target)}, preamble="import cirro")
    assert "REFUSED" in out and "imported before" in out
