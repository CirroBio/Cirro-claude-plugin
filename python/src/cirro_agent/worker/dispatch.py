"""Route one worker request to one handler, and render any failure as a typed error."""
from typing import Any
from .. import errors
from .. import local_reads
from . import gated
from . import auth, portal, read_listings, read_process, read_query, read_summaries


HANDLERS = {
    "query": read_query.do_query,
    "get": read_query.do_get,
    "summarize": read_summaries.do_summarize,
    "describe": local_reads.do_describe,
    "list": read_listings.do_list,
    "process_params": read_process.do_process_params,
    "process_config": read_process.do_process_config,
    "workflow_spec": local_reads.do_workflow_spec,
    "check_config": local_reads.do_check_config,
    "task_logs": read_summaries.do_task_logs,
    "login_start": auth.login_start,
    "login_wait": auth.login_wait,
    "login_daemon": auth.login_daemon,
    "whoami": auth.whoami,
    "logout": auth.logout,
    "plan": gated.do_plan,
    "apply": gated.do_apply,
}


def run(request: dict) -> Any:
    # Route on `verb` alone: `op` is the mutation name for plan/apply and
    # must never be mistaken for a handler route.
    handler = HANDLERS.get(request.get("verb"))
    if handler is None:
        return errors.NotSupported(
            f"No handler for verb {request.get('verb')!r}.",
            what="Use a supported verb.",
            retry_with=[{"verb": name} for name in sorted(HANDLERS)],
        ).to_dict()
    try:
        return handler(request)
    except errors.AgentError as exc:
        return exc.to_dict()
    except Exception as exc:
        return portal.wrap_upstream(
            exc, what="The operation failed before completing.").to_dict()
