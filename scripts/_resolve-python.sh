# Resolve the pinned interpreter. Sourced, not executed.
# Never falls back to the ambient python3: the SDK pins awscrt and s3fs, which ship
# per-CPython wheels, so the interpreter has to be the one setup built against.
resolve_plugin_root() {
  if [ -n "${CIRRO_AGENT_PLUGIN_ROOT:-}" ]; then printf '%s' "$CIRRO_AGENT_PLUGIN_ROOT"; return; fi
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then printf '%s' "$CLAUDE_PLUGIN_ROOT"; return; fi
  printf '%s' "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
}

# Never inside the plugin root. A directory marketplace copies that tree verbatim
# into every install -- no .gitignore, no exclude list -- so a virtualenv left there
# is shipped with the plugin. Three of them once made a 1.2 MB plugin a 536 MB one.
# runner.plugin_data() is the other half of this and must agree; a test asserts it.
resolve_plugin_data() {
  if [ -n "${CIRRO_AGENT_PLUGIN_DATA:-}" ]; then printf '%s' "$CIRRO_AGENT_PLUGIN_DATA"; return; fi
  if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then printf '%s' "$CLAUDE_PLUGIN_DATA"; return; fi
  printf '%s' "${XDG_DATA_HOME:-$HOME/.local/share}/cirro-agent"
}

resolve_python() {
  if [ -n "${CIRRO_AGENT_PYTHON:-}" ]; then printf '%s' "$CIRRO_AGENT_PYTHON"; return 0; fi
  local data
  data="$(resolve_plugin_data)"
  if [ -x "$data/venv/bin/python" ]; then printf '%s' "$data/venv/bin/python"; return 0; fi
  if [ -x "$data/dev-venv/bin/python" ]; then printf '%s' "$data/dev-venv/bin/python"; return 0; fi
  return 1
}

runtime_missing_json() {
  cat <<'JSON'
{"ok":false,"error":{"code":"RUNTIME_NOT_INSTALLED","severity":"user_action",
"message":"The Cirro runtime is not installed.",
"remediation":{"what":"The plugin runs the Cirro SDK in its own pinned Python environment.",
"candidates":[],"retry_with":[],"user_action":"Run /cirro:setup (up to two minutes on first use)."}}}
JSON
}
