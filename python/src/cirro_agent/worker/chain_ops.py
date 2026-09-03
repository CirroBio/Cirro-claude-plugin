"""Several build-up operations under one approval.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.
"""

from typing import Optional

from .. import errors
from . import mutations, portal, reads
from .mutations import (Draft, Op, RISK_WRITE, RISK_DESTRUCTIVE, RISK_LOCAL,
                        RISK_WRITE_LOCAL, _inv, get_op)


# A chain builds something up across several operations under one approval. Tearing
# something down is a different activity and stays one approval at a time, so that a
# single "yes" can never authorize a deletion the person was not looking at.
CHAIN_MAX_STEPS = 8
RETRY_MAX_ATTEMPTS = 5


def _reference(value) -> Optional[dict]:
    """Is this argument a reference to an earlier step's output?"""
    if isinstance(value, dict) and "from_step" in value:
        return value
    return None


def _resolve_references(value, outputs: list):
    """Substitute recorded step outputs into an argument tree.

    Only the server's own record of what a step produced is substituted -- there is
    no path here for a caller-supplied value to reach an approved argument.
    """
    reference = _reference(value)
    if reference is not None:
        step = outputs[reference["from_step"]]
        return step[reference["field"]]
    if isinstance(value, dict):
        return {k: _resolve_references(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_references(v, outputs) for v in value]
    return value


def _describe_reference(steps: list, reference: dict) -> str:
    index = reference["from_step"]
    label = steps[index].get("label") or steps[index]["op"]
    return f"<{reference['field']} of step {index + 1}: {label}>"


def _describe_args(steps: list, args: dict) -> dict:
    """Args as the person should read them: concrete values, or a named derivation."""
    def walk(value):
        reference = _reference(value)
        if reference is not None:
            return _describe_reference(steps, reference)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value
    return {k: walk(v) for k, v in args.items()}


def _check_references(steps: list) -> None:
    """Every reference must point backwards, at a field that operation produces."""
    for index, step in enumerate(steps):
        def visit(value, _index=index, _step=step):
            reference = _reference(value)
            if reference is not None:
                _check_one_reference(steps, _index, reference)
                return
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(step.get("args") or {})


def _check_one_reference(steps: list, index: int, reference: dict) -> None:
    source = reference.get("from_step")
    field = reference.get("field")
    if not isinstance(source, int) or not 0 <= source < index:
        raise errors.InvalidQuery(
            f"Step {index + 1} references step {source}, which is not an earlier step.",
            what=("A reference may only read the output of a step that runs before "
                  "it."),
            field="from_step", given=reference,
            retry_with=[{"note": "reorder the steps so the producer runs first"}],
        )
    produced = get_op(steps[source]["op"]).outputs
    if field not in produced:
        raise errors.InvalidQuery(
            f"{steps[source]['op']} does not produce {field!r}.",
            what="A reference must name a field the earlier operation returns.",
            field="field", given=reference,
            candidates=[{"field": name} for name in produced],
            retry_with=([{"field": name} for name in produced]
                        or [{"note": "this operation exposes no referenceable output"}]),
        )


def _chain_steps(args: dict) -> list:
    steps = args.get("steps") or []
    if not steps:
        raise errors.InvalidQuery(
            "A chain needs at least one step.",
            what="Pass steps as a list of {op, args} entries.", field="steps",
            retry_with=[{"op": "chain", "args": {"steps": [
                {"op": "upload_dataset", "args": {"...": "..."}}]}}],
        )
    if len(steps) > CHAIN_MAX_STEPS:
        raise errors.InvalidQuery(
            f"A chain may hold at most {CHAIN_MAX_STEPS} steps, not {len(steps)}.",
            what="Long chains are hard to review in one approval.", field="steps",
            retry_with=[{"note": f"split into chains of at most {CHAIN_MAX_STEPS}"}],
        )
    labels = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("op"):
            raise errors.InvalidQuery(
                f"Step {index + 1} has no op.",
                what="Each step is {op, args, label?}.", field="steps", given=step,
                retry_with=[{"op": "upload_dataset", "args": {}}],
            )
        op = get_op(step["op"])
        if op.risk == RISK_DESTRUCTIVE:
            raise errors.InvalidQuery(
                f"{op.name} is destructive and cannot run inside a chain.",
                what=("A chain is approved as one action; a destructive step must be "
                      "seen and approved on its own."),
                field="steps", given=step["op"],
                retry_with=[{"op": op.name, "note": "plan this one separately"}],
            )
        if op.name == "chain":
            raise errors.InvalidQuery(
                "A chain cannot contain a chain.",
                what="Flatten the steps into one list.", field="steps",
                retry_with=[{"note": "list the inner chain's steps directly"}],
            )
        label = step.get("label")
        if label:
            if label in labels:
                raise errors.InvalidQuery(
                    f"Two steps are labelled {label!r}.",
                    what=("Labels name a step for restart_from, so they must be "
                          "unique."),
                    field="label", given=label,
                    retry_with=[{"note": f"rename one of the {label!r} steps"}],
                )
            labels.add(label)
    _check_references(steps)
    return steps


def _retry_policy(args: dict, steps: list) -> Optional[dict]:
    """The bounded discretion a chain may exercise on failure.

    What the person approves here is a policy, not a value: which steps may run
    again, how many times, and the single set of fields the agent may author anew.
    Everything else is pinned, re-checked before each attempt, and a chain that would
    step outside it stops and asks again.
    """
    policy = args.get("retry")
    if not policy:
        return None
    labels = [step.get("label") for step in steps if step.get("label")]
    redo = policy.get("redo") or []
    unknown = [one for one in redo if one not in labels]
    if not redo or unknown:
        raise errors.InvalidQuery(
            f"retry.redo names {unknown or 'nothing'}; label the steps to re-run.",
            what="redo lists step labels, so the steps it repeats are unambiguous.",
            field="retry.redo", given=policy.get("redo"),
            candidates=[{"label": one} for one in labels],
            retry_with=[{"retry": {"redo": labels[-1:] or ["<label a step first>"]}}],
        )
    attempts = policy.get("max_attempts")
    if not isinstance(attempts, int) or not 2 <= attempts <= RETRY_MAX_ATTEMPTS:
        raise errors.InvalidQuery(
            f"retry.max_attempts must be between 2 and {RETRY_MAX_ATTEMPTS}.",
            what=("Each attempt consumes compute, and Cirro exposes no pricing API, so "
                  "an unbounded loop cannot be priced or approved."),
            field="retry.max_attempts", given=attempts,
            retry_with=[{"retry": {"max_attempts": 3}}],
        )
    may_change = policy.get("may_change") or []
    if not may_change:
        raise errors.InvalidQuery(
            "retry.may_change must name the fields a retry may alter.",
            what=("Without it a retry could change anything, which is the open-ended "
                  "authorization a chain exists to avoid."),
            field="retry.may_change",
            retry_with=[{"retry": {"may_change": ["design_spec"]}}],
        )
    return {"redo": list(redo), "max_attempts": attempts,
            "may_change": sorted(may_change),
            "halt_on_repeat_failure": policy.get("halt_on_repeat_failure", True)}


def _prior_chain(reader: reads.Reader, args: dict) -> tuple:
    """Load a completed chain so a retry can resume inside it.

    Resuming reuses the earlier chain's recorded outputs, so an attempt that only
    needs to reconfigure and relaunch does not upload the test data again. The
    outputs come from that chain's own audit record -- the caller names which chain,
    never what it produced.
    """
    from .. import plans
    from ..session import Session
    session = Session(base_url=reader.base_url, dir=reader.session_dir)
    prior_id = args["restart_from"]["plan_id"]
    try:
        prior = plans.load(session, prior_id)
    except errors.AgentError:
        raise errors.NotFound(
            f"No plan {prior_id!r} in this session folder.",
            what="restart_from names the chain being retried.",
            field="restart_from", given=prior_id,
            retry_with=[{"op": "chain", "note": "plan a fresh chain instead"}],
        )
    if prior.op != "chain":
        raise errors.InvalidQuery(
            f"Plan {prior_id} is a {prior.op}, not a chain.",
            what="Only a chain can be restarted at one of its labelled steps.",
            field="restart_from", given=prior.op,
            retry_with=[{"op": "chain", "note": "plan a fresh chain instead"}],
        )
    return prior, plans.outcomes(session, prior)


def _restart_steps(prior, outcomes: dict, args: dict) -> tuple:
    """The steps to re-run, and the outputs earlier steps already produced."""
    label = args["restart_from"]["label"]
    steps = prior.args["steps"]
    labels = [step.get("label") for step in steps]
    if label not in labels:
        raise errors.InvalidQuery(
            f"The chain has no step labelled {label!r}.",
            what="restart_from.label names where to resume.",
            field="restart_from", given=label,
            candidates=[{"label": one} for one in labels if one],
            retry_with=[{"restart_from": {"plan_id": prior.plan_id, "label": one}}
                        for one in labels if one],
        )
    index = labels.index(label)
    missing = [steps[i].get("label") or steps[i]["op"] for i in range(index)
               if str(i) not in outcomes]
    if missing:
        raise errors.InvalidQuery(
            f"Cannot resume at {label!r}: step(s) {missing} never completed, so their "
            "outputs do not exist.",
            what="A resumed chain reuses what earlier steps produced.",
            field="restart_from", given=label,
            retry_with=[{"op": "chain", "note": "plan a fresh chain from the start"}],
        )
    carried = [outcomes[str(i)] for i in range(index)]
    return steps[index:], carried


def _attempt_diff(prior_steps: list, steps: list, may_change: list) -> list:
    """What differs from the attempt already approved, field by field."""
    lines = []
    by_label = {step.get("label"): step for step in prior_steps if step.get("label")}
    for step in steps:
        previous = by_label.get(step.get("label"))
        if previous is None:
            continue
        before, after = previous.get("args") or {}, step.get("args") or {}
        for key in sorted(set(before) | set(after)):
            if before.get(key) == after.get(key):
                continue
            if key not in may_change:
                raise errors.InvalidQuery(
                    f"This attempt changes {key!r}, which the approved retry policy "
                    f"does not allow.",
                    what=f"Only {', '.join(may_change)} may differ between attempts.",
                    field=key, given=after.get(key),
                    retry_with=[{"op": "chain",
                                 "note": "plan a fresh chain to change anything else"}],
                )
            lines.append(f"       {step.get('label')}.{key}")
            lines.append(f"         from  {before.get(key)!r}")
            lines.append(f"         to    {after.get(key)!r}")
    return lines


def plan_chain(reader: reads.Reader, args: dict) -> Draft:
    """Approve several build-up operations as one action.

    Arguments a later step cannot know yet are references to an earlier step's output
    -- `{"from_step": 0, "field": "process_id"}` -- not wildcards. The server
    substitutes from its own record of what each step produced, so nothing the caller
    supplies at execution time can reach an approved argument. What the person reads
    is every concrete value plus every derivation named for what it derives from.

    Each step is validated here as far as its inputs allow. A step whose arguments
    depend on an earlier step cannot be fully resolved before that step runs, so its
    planner runs again at execution time and the chain stops if it refuses.
    """
    if args.get("restart_from"):
        return _plan_restart(reader, args)

    steps = _chain_steps(args)
    retry = _retry_policy(args, steps)

    lines = [f"Run {len(steps)} operations as one approved chain.", ""]
    widest = RISK_WRITE
    warnings = []
    for index, step in enumerate(steps):
        op = get_op(step["op"])
        if op.risk == RISK_LOCAL and widest == RISK_WRITE:
            widest = RISK_WRITE_LOCAL
        label = step.get("label")
        heading = f"  {index + 1}. {op.name}" + (f"  [{label}]" if label else "")
        lines.append(heading)
        shown = _describe_args(steps, step.get("args") or {})
        for key in sorted(shown):
            lines.append(f"       {key} = {shown[key]!r}")
        lines.append("")

    resolvable = [index for index, step in enumerate(steps)
                  if not _has_reference(step.get("args") or {})]
    deferred = [index + 1 for index in range(len(steps)) if index not in resolvable]
    if deferred:
        warnings.append(
            f"Step(s) {deferred} take values produced by earlier steps, so their "
            "figures are not final here. Each one is re-resolved and re-validated "
            "immediately before it runs, and the chain stops rather than guessing.")

    if retry:
        lines += [
            "  On failure:",
            f"       re-run {', '.join(retry['redo'])} up to "
            f"{retry['max_attempts']} attempts in total",
            f"       may change only: {', '.join(retry['may_change'])}",
            "       everything else stays exactly as approved above",
            "",
        ]
        warnings.append(
            f"Each attempt launches again and consumes compute. Up to "
            f"{retry['max_attempts']} attempts are authorized here; Cirro exposes no "
            "pricing API, so this is a count and not a cost.")
        if retry["halt_on_repeat_failure"]:
            lines.append("       stops early if an attempt fails the same way twice")
            lines.append("")

    lines.append("Steps run in order. If one fails the chain stops there, and every "
                 "step's outcome is reported.")

    return Draft(
        args={"steps": steps, "retry": retry},
        summary="\n".join(lines).rstrip(), warnings=warnings, risk=widest,
        invalidates=[_inv("datasets"), _inv("processes")],
    )


def _plan_restart(reader: reads.Reader, args: dict) -> Draft:
    """Resume a chain at one of its labelled steps, as one more attempt.

    The person already approved this chain and the policy governing its retries, so
    what they read here is the difference: which attempt this is, and exactly which
    fields differ from the attempt before. Everything the policy pins is re-checked
    against the earlier plan, and a change outside `may_change` is refused rather
    than shown as a diff and hoped past.
    """
    prior, recorded = _prior_chain(reader, args)
    policy = prior.args.get("retry")
    if not policy:
        raise errors.InvalidQuery(
            f"Chain {prior.plan_id} was approved without a retry policy.",
            what="Only a chain whose approval authorized retries can be resumed.",
            field="restart_from",
            retry_with=[{"op": "chain", "note": "plan a fresh chain"}],
        )

    attempt = int(args["restart_from"].get("attempt") or 2)
    if attempt > policy["max_attempts"]:
        raise errors.InvalidQuery(
            f"This would be attempt {attempt}; the approved policy allows "
            f"{policy['max_attempts']}.",
            what="A new approval is needed to keep going.",
            field="restart_from", given=attempt,
            retry_with=[{"op": "chain", "note": "plan a fresh chain to continue"}],
        )

    steps, carried = _restart_steps(prior, recorded, args)
    label = args["restart_from"]["label"]
    if label not in policy["redo"]:
        raise errors.InvalidQuery(
            f"The approved policy re-runs {', '.join(policy['redo'])}, not {label!r}.",
            what="Resuming elsewhere is outside what was approved.",
            field="restart_from", given=label,
            retry_with=[{"restart_from": {"plan_id": prior.plan_id,
                                          "label": policy["redo"][0]}}],
        )

    replacements = args.get("steps") or []
    if replacements:
        by_label = {step.get("label"): step for step in replacements}
        steps = [by_label.get(step.get("label"), step) for step in steps]
    diff = _attempt_diff(prior.args["steps"], steps, policy["may_change"])

    lines = [f"Attempt {attempt} of {policy['max_attempts']} on the chain approved as "
             f"{prior.plan_id}.", ""]
    rerunning = ", ".join(step.get("label") or step["op"] for step in steps)
    lines.append(f"  Re-running: {rerunning}")
    lines.append(f"  Reusing:    {len(carried)} earlier step(s), unchanged")
    lines.append("")
    if diff:
        lines.append("  Changed since the last attempt:")
        lines += diff
    else:
        lines.append("  Nothing differs from the last attempt.")
    lines += ["", "Everything the approval pinned is unchanged. Launching consumes "
                  "compute again."]

    warnings = []
    if not diff:
        warnings.append("This attempt is identical to the one that just failed, so it "
                        "will fail the same way. Change something first.")
    return Draft(
        args={"steps": steps, "retry": policy, "carried": carried,
              "attempt": attempt, "restarted_from": prior.plan_id},
        summary="\n".join(lines), warnings=warnings, risk=prior.risk,
        invalidates=[_inv("datasets"), _inv("processes")],
    )


def _has_reference(value) -> bool:
    if _reference(value) is not None:
        return True
    if isinstance(value, dict):
        return any(_has_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_reference(item) for item in value)
    return False


def apply_chain(reader: reads.Reader, args: dict) -> dict:
    """Run each step in order, substituting earlier steps' recorded outputs.

    A step's planner runs here rather than at approval time for any step that depends
    on an earlier one, because its inputs did not exist then. The planner is the same
    code that produced the approved text, so a step it now refuses is a step whose
    preconditions changed -- the chain stops and says which one.
    """
    # A resumed chain starts with what its earlier steps already produced, so a
    # reference to step 1 still resolves without uploading anything again.
    outputs = list(args.get("carried") or [])
    offset = len(outputs)
    done, failure = [], None
    for index, step in enumerate(args["steps"], start=offset):
        op = get_op(step["op"])
        label = step.get("label") or op.name
        try:
            resolved = _resolve_references(step.get("args") or {}, outputs)
            draft = op.plan(reader, resolved)
            result = op.apply(reader, draft.args)
        except errors.AgentError as exc:
            failure = {"step": index + 1, "label": label, "op": op.name,
                       "code": exc.code, "error": exc.message}
            break
        except Exception as exc:
            wrapped = portal.wrap_upstream(exc, what=f"{op.name} failed inside a chain.")
            failure = {"step": index + 1, "label": label, "op": op.name,
                       "code": wrapped.code, "error": wrapped.message}
            break
        outputs.append(result if isinstance(result, dict) else {})
        done.append({"step": index + 1, "label": label, "op": op.name,
                     "result": result})

    response = {"completed": done, "n_completed": len(done),
                "n_steps": len(args["steps"])}
    if failure is None:
        return response
    response["failed_at"] = failure
    response["note"] = (
        f"Step {failure['step']} ({failure['label']}) failed, so steps "
        f"{failure['step'] + 1}-{len(args['steps'])} did not run. The "
        f"{len(done)} step(s) before it did, and are listed in `completed`.")
    if not done:
        raise errors.UpstreamError(
            f"The chain failed at its first step: {failure['error']}",
            what=f"{failure['op']} would not run.",
            retry_with=[{"op": "chain", "note": "fix the cause and plan again"}],
            user_action="Nothing was changed.",
            failed_at=failure,
        )
    return response


CHAIN_OPS = [
    Op("chain", RISK_WRITE, plan_chain, apply_chain),
]

mutations.OPS.update({op.name: op for op in CHAIN_OPS})
