# Resolve the pinned interpreter. Sourced, not executed.
# Never falls back to the ambient python3: the SDK pins awscrt and s3fs, which ship
# per-CPython wheels, so the interpreter has to be the one setup built against.
resolve_plugin_root() {
  if [ -n "${CIRRO_AGENT_PLUGIN_ROOT:-}" ]; then printf '%s' "$CIRRO_AGENT_PLUGIN_ROOT"; return; fi
  if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then printf '%s' "$CLAUDE_PLUGIN_ROOT"; return; fi
  printf '%s' "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
}

resolve_plugin_data() {
  if [ -n "${CIRRO_AGENT_PLUGIN_DATA:-}" ]; then printf '%s' "$CIRRO_AGENT_PLUGIN_DATA"; return; fi
  if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then printf '%s' "$CLAUDE_PLUGIN_DATA"; return; fi
  printf '%s' "$(resolve_plugin_root)/.plugin-data"
}

resolve_python() {
  if [ -n "${CIRRO_AGENT_PYTHON:-}" ]; then printf '%s' "$CIRRO_AGENT_PYTHON"; return 0; fi
  local data root
  data="$(resolve_plugin_data)"
  root="$(resolve_plugin_root)"
  if [ -x "$data/venv/bin/python" ]; then printf '%s' "$data/venv/bin/python"; return 0; fi
  if [ -x "$root/.dev-venv/bin/python" ]; then printf '%s' "$root/.dev-venv/bin/python"; return 0; fi
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
