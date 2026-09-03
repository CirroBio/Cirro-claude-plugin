"""Several build-up operations under one approval.

A chain widens the approval, never the authorization. An argument a later step cannot
know yet is a reference to an earlier step's recorded output, resolved by the server
from its own record -- never a wildcard, and never a value the caller supplies at
apply time. Destructive operations are refused, so one "yes" cannot authorize a
deletion nobody was looking at. A retry is a policy the person approves once: bounded
attempts, a fixed set of fields that may change, and the same operations.

Registers its operations into `mutations.OPS` on import; see mutations.py for why the
registration lives on this side.
"""
from typing import Iterator, Optional
from .. import errors, plans
from . import mutations, portal, reads
from .mutations import (Draft, Op, RISK_DESTRUCTIVE, RISK_LOCAL, RISK_WRITE, RISK_WRITE_LOCAL,
                        _inv, get_op)
from ..session import Session


# Reviewability bounds. Eight steps is about what one approval can be read in full;
# past five attempts a "retry" has stopped being a fix and become a compute budget.
CHAIN_MAX_STEPS = 8
RETRY_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------- references


def _reference(value) -> Optional[dict]:
    """Is this argument a reference to an earlier step's output?"""
    if isinstance(value, dict) and "from_step" in value:
        return value
    return None


def _references(value) -> Iterator[dict]:
    """Every reference anywhere in an argument tree."""
    reference = _reference(value)
    if reference is not None:
        yield reference
    elif isinstance(value, dict):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _map_references(value, substitute):
    """Rebuild an argument tree with every reference replaced by substitute(ref)."""
    reference = _reference(value)
    if reference is not None:
        return substitute(reference)
    if isinstance(value, dict):
        return {k: _map_references(v, substitute) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_references(v, substitute) for v in value]
    return value


def _resolve_references(value, outputs: list):
    """Substitute recorded step outputs into an argument tree.

    Only the server's own record of what a step produced is substituted. Any other
    key in the reference dict is discarded with it, so there is no path for a
    caller-supplied value to reach an approved argument.
    """
    return _map_references(value, lambda ref: outputs[ref["from_step"]][ref["field"]])


def _describe_args(steps: list, args: dict) -> dict:
    """Args as the person should read them: concrete values, or a named derivation."""
    def describe(reference):
        index = reference["from_step"]
        label = steps[index].get("label") or steps[index]["op"]
        return f"<{reference['field']} of step {index + 1}: {label}>"
    return _map_references(args, describe)


def _has_reference(value) -> bool:
    return any(True for _ in _references(value))


def _check_references(steps: list, start: int = 0) -> None:
    """Every reference must point backwards, at a field that operation produces.

    Indices are absolute positions in the full chain, so a resumed chain validates
    its tail against the steps it is reusing.
    """
    for index in range(start, len(steps)):
        for reference in _references(steps[index].get("args") or {}):
            _check_one_reference(steps, index, reference)


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


# --------------------------------------------------------------------- validation


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


def _widest_risk(steps: list) -> str:
    """A chain touching local disk anywhere must not present as a plain remote write."""
    for step in steps:
        if get_op(step["op"]).risk in (RISK_LOCAL, RISK_WRITE_LOCAL):
            return RISK_WRITE_LOCAL
    return RISK_WRITE


# ------------------------------------------------------------------- fresh chains


def _pre_plan(reader: reads.Reader, steps: list) -> list:
    """Run each reference-free step's own planner now.

    That is where the resolved names, the blast radius and the warnings come from --
    an approval showing only the caller's raw arguments would be no disclosure at all.
    It is also what lets apply's re-plan check bite: a chain whose approval text was a
    pure function of the request could never be found stale. Steps that depend on an
    earlier step's output cannot be planned yet and are planned when they run.
    """
    drafts = []
    for step in steps:
        if _has_reference(step.get("args") or {}):
            drafts.append(None)
            continue
        draft = get_op(step["op"]).plan(reader, dict(step.get("args") or {}))
        step["resolved_args"] = draft.args
        drafts.append(draft)
    return drafts


def plan_chain(reader: reads.Reader, args: dict) -> Draft:
    """Approve several build-up operations as one action."""
    if args.get("restart_from"):
        return _plan_restart(reader, args)

    steps = [dict(step) for step in _chain_steps(args)]
    retry = _retry_policy(args, steps)
    drafts = _pre_plan(reader, steps)

    lines = [f"Run {len(steps)} operations as one approved chain.", ""]
    warnings, invalidates = [], []
    for index, (step, draft) in enumerate(zip(steps, drafts)):
        label = step.get("label")
        lines.append(f"  {index + 1}. {step['op']}" + (f"  [{label}]" if label else ""))
        if draft is not None:
            lines += [f"       {line}" for line in draft.summary.splitlines() if line]
            warnings += [f"step {index + 1}: {w}" for w in draft.warnings]
            invalidates += draft.invalidates
        else:
            shown = _describe_args(steps, step.get("args") or {})
            lines += [f"       {key} = {shown[key]!r}" for key in sorted(shown)]
            lines.append("       (figures resolved when this step runs; it is "
                         "re-validated first, and the chain stops if it refuses)")
        lines.append("")

    deferred = [i + 1 for i, d in enumerate(drafts) if d is None]
    if deferred:
        # Only a coarse invalidation can be declared for steps not yet planned; the
        # exact scopes are collected as they run and returned by apply.
        invalidates += [_inv("datasets"), _inv("processes")]

    if retry:
        lines += [
            "  On failure:",
            f"       re-run {', '.join(retry['redo'])} up to "
            f"{retry['max_attempts']} attempts in total",
            f"       may change only: {', '.join(retry['may_change'])}",
            "       everything else stays exactly as approved above",
        ]
        if retry["halt_on_repeat_failure"]:
            lines.append("       stops early if an attempt fails the same way twice")
        lines.append("")
        warnings.append(
            f"Each attempt launches again and consumes compute. Up to "
            f"{retry['max_attempts']} attempts are authorized here; Cirro exposes no "
            "pricing API, so this is a count and not a cost.")

    lines.append("Steps run in order. If one fails the chain stops there, and every "
                 "step's outcome is reported.")

    return Draft(
        args={"steps": steps, "retry": retry},
        summary="\n".join(lines).rstrip(), warnings=warnings,
        risk=_widest_risk(steps), invalidates=invalidates,
    )


# ----------------------------------------------------------------------- restarts


def _restart_request(args: dict) -> dict:
    request = args.get("restart_from")
    if not isinstance(request, dict) or not request.get("plan_id") \
            or not request.get("label"):
        raise errors.InvalidQuery(
            "restart_from must be {plan_id, label}.",
            what="It names the approved chain and the labelled step to resume at.",
            field="restart_from", given=request,
            retry_with=[{"restart_from": {"plan_id": "plan_...", "label": "configure"}}],
        )
    return request


def _lineage(session: Session, plan_id: str) -> tuple:
    """The root chain and every finished attempt on it, oldest first.

    Every restart plan records its root, so any plan in the lineage identifies the
    whole of it. Only claimed-and-finished plans count: a pending plan has been shown
    to nobody, and a restart view is a diff against something the person approved.
    """
    given = plans.load(session, plan_id)
    if given.op != "chain":
        raise errors.InvalidQuery(
            f"Plan {plan_id} is a {given.op}, not a chain.",
            what="Only a chain can be restarted at one of its labelled steps.",
            field="restart_from", given=given.op,
            retry_with=[{"op": "chain", "note": "plan a fresh chain instead"}],
        )
    root_id = given.args.get("root") or given.plan_id
    root = given if root_id == given.plan_id else plans.load(session, root_id)
    finished = (plans.DONE, plans.FAILED)
    if root.status not in finished:
        raise errors.InvalidQuery(
            f"Chain {root.plan_id} was never approved and applied (status "
            f"{root.status}), so there is nothing to resume.",
            what="A restart reuses what an approved run produced.",
            field="restart_from", given=plan_id,
            retry_with=[{"op": "chain", "note": "plan it fresh and approve it"}],
        )
    attempts = [root]
    for path in sorted((session.path / "plans").glob("plan_*.json")):
        if path.stem == root.plan_id:
            continue
        try:
            candidate = plans.load(session, path.stem)
        except errors.AgentError:
            continue
        if (candidate.op == "chain" and candidate.args.get("root") == root.plan_id
                and candidate.status in finished):
            attempts.append(candidate)
    attempts.sort(key=lambda plan: plan.created_at)
    return root, attempts


def _failure_signature(session: Session, plan) -> Optional[tuple]:
    entry = plans.outcome(session, plan)
    failed_at = (entry.get("result") or {}).get("failed_at")
    if failed_at:
        return (failed_at.get("step"), failed_at.get("code"))
    error = entry.get("error")
    return ("chain", str(error).split(":")[0]) if error else None


def _restart_steps(root, outcomes: dict, label: str) -> tuple:
    """The tail to re-run, the outputs it reuses, and where the tail starts."""
    steps = root.args["steps"]
    labels = [step.get("label") for step in steps]
    if label not in labels:
        raise errors.InvalidQuery(
            f"The chain has no step labelled {label!r}.",
            what="restart_from.label names where to resume.",
            field="restart_from", given=label,
            candidates=[{"label": one} for one in labels if one],
            retry_with=[{"restart_from": {"plan_id": root.plan_id, "label": one}}
                        for one in labels if one],
        )
    offset = labels.index(label)
    missing = [steps[i].get("label") or steps[i]["op"] for i in range(offset)
               if i not in outcomes]
    if missing:
        raise errors.InvalidQuery(
            f"Cannot resume at {label!r}: step(s) {missing} never completed, so their "
            "outputs do not exist.",
            what="A resumed chain reuses what earlier steps produced.",
            field="restart_from", given=label,
            retry_with=[{"op": "chain", "note": "plan a fresh chain from the start"}],
        )
    tail = [dict(step) for step in steps[offset:]]
    return tail, [outcomes[i] for i in range(offset)], offset


def _replace_tail(tail: list, completed: list, replacements: list) -> list:
    """Splice the caller's edited steps in by label, keeping every op as approved.

    A replacement may change arguments -- `_attempt_diff` decides which -- but never
    the operation behind a label: that would let a "retry" run something the person
    did not see. Callers naturally resend the whole chain, so an unchanged copy of a
    step that already ran is accepted as an echo; a changed one is refused, since a
    completed step cannot be edited. Unlabelled or unknown replacements are refused
    rather than guessed at: an unlabelled one used to match every unlabelled step.
    """
    by_label = {step.get("label"): step for step in tail if step.get("label")}
    done_by_label = {step.get("label"): step for step in completed if step.get("label")}
    for replacement in replacements:
        label = replacement.get("label") if isinstance(replacement, dict) else None
        if label in done_by_label and label not in by_label:
            earlier = done_by_label[label]
            if (replacement.get("op") != earlier["op"]
                    or (replacement.get("args") or {}) != (earlier.get("args") or {})):
                raise errors.InvalidQuery(
                    f"Step {label!r} already ran in the approved chain and cannot be "
                    "edited.",
                    what="Only steps being re-run may differ.",
                    field="steps", given=replacement,
                    retry_with=[{"note": "resend completed steps unchanged, or omit "
                                         "them"}],
                )
            continue
        if not label or label not in by_label:
            raise errors.InvalidQuery(
                f"Replacement step {label!r} does not match a step being re-run.",
                what="Edited steps are matched to the approved chain by label.",
                field="steps", given=replacement,
                candidates=[{"label": one} for one in by_label],
                retry_with=[{"note": "label the replacement as the step it edits"}],
            )
        original = by_label[label]
        if replacement.get("op") != original["op"]:
            raise errors.InvalidQuery(
                f"Step {label!r} was approved as {original['op']}; a retry cannot "
                f"run {replacement.get('op')} in its place.",
                what="A retry may change arguments the policy allows, never the "
                     "operation.",
                field="op", given=replacement.get("op"),
                retry_with=[{"op": "chain", "note": "plan a fresh chain to change "
                                                    "what runs"}],
            )
        original["args"] = dict(replacement.get("args") or {})
    return tail


def _attempt_diff(previous: list, tail: list, may_change: list) -> list:
    """What differs from the attempt already approved, field by field."""
    lines = []
    before_by_label = {s.get("label"): s for s in previous if s.get("label")}
    for step in tail:
        earlier = before_by_label.get(step.get("label"))
        if earlier is None:
            continue
        before, after = earlier.get("args") or {}, step.get("args") or {}
        for key in sorted(set(before) | set(after)):
            if before.get(key) == after.get(key):
                continue
            if key not in may_change:
                raise errors.InvalidQuery(
                    f"This attempt changes {key!r}, which the approved retry policy "
                    "does not allow.",
                    what=f"Only {', '.join(may_change)} may differ between attempts.",
                    field=key, given=after.get(key),
                    retry_with=[{"op": "chain",
                                 "note": "plan a fresh chain to change anything else"}],
                )
            lines += [f"       {step.get('label')}.{key}",
                      f"         from  {before.get(key)!r}",
                      f"         to    {after.get(key)!r}"]
    return lines


def _plan_restart(reader: reads.Reader, args: dict) -> Draft:
    """Resume an approved chain at a labelled step, as one more attempt.

    The person already approved the chain and the policy governing its retries.
    What they read here is everything this attempt will do -- each operation by name,
    every value carried over from the approved run, and exactly which fields differ
    from the attempt before -- checked against the same rules as a fresh chain.
    """
    request = _restart_request(args)
    session = reader.session
    root, attempts = _lineage(session, request["plan_id"])
    policy = root.args.get("retry")
    if not policy:
        raise errors.InvalidQuery(
            f"Chain {root.plan_id} was approved without a retry policy.",
            what="Only a chain whose approval authorized retries can be resumed.",
            field="restart_from",
            retry_with=[{"op": "chain", "note": "plan a fresh chain"}],
        )

    attempt = len(attempts) + 1
    if attempt > policy["max_attempts"]:
        raise errors.InvalidQuery(
            f"This would be attempt {attempt}; the approved policy allows "
            f"{policy['max_attempts']}.",
            what="A new approval is needed to keep going.",
            field="restart_from", given=attempt,
            retry_with=[{"op": "chain", "note": "plan a fresh chain to continue"}],
        )
    label = request["label"]
    if label not in policy["redo"]:
        raise errors.InvalidQuery(
            f"The approved policy re-runs {', '.join(policy['redo'])}, not {label!r}.",
            what="Resuming elsewhere is outside what was approved.",
            field="restart_from", given=label,
            retry_with=[{"restart_from": {"plan_id": root.plan_id,
                                          "label": policy["redo"][0]}}],
        )

    previous = attempts[-1]
    # Plans store the normalized policy, but tolerate one written without the flag.
    if policy.get("halt_on_repeat_failure", True) and len(attempts) >= 2:
        last, before_last = (_failure_signature(session, attempts[-1]),
                             _failure_signature(session, attempts[-2]))
        if last is not None and last == before_last:
            raise errors.InvalidQuery(
                f"The last two attempts failed the same way ({last[1]} at step "
                f"{last[0]}), and the approved policy stops there.",
                what="Repeating an identical failure means the fix is not working.",
                field="restart_from",
                user_action="Diagnose the failure before spending another attempt.",
                retry_with=[{"op": "chain", "note": "plan a fresh chain after fixing"}],
            )

    merged = {}
    for plan in attempts:
        merged.update(plans.outcomes(session, plan))
    tail, carried, offset = _restart_steps(root, merged, label)
    tail = _replace_tail(tail, root.args["steps"][:offset], args.get("steps") or [])
    assembled = [dict(step) for step in root.args["steps"][:offset]] + tail
    _chain_steps({"steps": assembled})
    diff = _attempt_diff(previous.args["steps"], tail, policy["may_change"])

    lines = [f"Attempt {attempt} of {policy['max_attempts']} on the chain approved as "
             f"{root.plan_id}.", ""]
    lines.append("  Re-running:")
    lines += [f"       {offset + i + 1}. {step['op']}  [{step.get('label')}]"
              for i, step in enumerate(tail)]
    lines.append("  Reusing from the approved run:")
    for i, produced in enumerate(carried):
        step = root.args["steps"][i]
        head = f"       {i + 1}. {step['op']}  [{step.get('label') or ''}]"
        lines.append(head.rstrip(" []"))
        referenceable = get_op(step["op"]).outputs
        for key, value in sorted(produced.items()):
            if key in referenceable:
                lines.append(f"           {key} = {value!r}")
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
        args={"steps": tail, "retry": policy, "carried": carried,
              "attempt": attempt, "restarted_from": previous.plan_id,
              "root": root.plan_id},
        summary="\n".join(lines), warnings=warnings, risk=_widest_risk(assembled),
        invalidates=[_inv("datasets"), _inv("processes")],
    )


# ---------------------------------------------------------------------- execution


def _failure(index: int, step: dict, exc: Exception) -> dict:
    typed = exc if isinstance(exc, errors.AgentError) else portal.wrap_upstream(
        exc, what=f"{step['op']} failed inside a chain.")
    return {"step": index + 1, "label": step.get("label") or step["op"],
            "op": step["op"], "code": typed.code, "error": typed.message,
            "exc": typed}


def apply_chain(reader: reads.Reader, args: dict) -> dict:
    """Run each step in order, feeding earlier steps' recorded outputs forward.

    A step planned at approval time runs with the arguments the person saw. A step
    that depends on an earlier one is planned here, because its inputs did not exist
    then; the planner is the same code that would have produced the approval text, so
    a step it now refuses is a step whose preconditions changed -- the chain stops.
    """
    outputs = list(args.get("carried") or [])
    offset = len(outputs)
    done, failure, touched = [], None, []
    for index, step in enumerate(args["steps"], start=offset):
        op = get_op(step["op"])
        try:
            if "resolved_args" in step:
                applied = step["resolved_args"]
            else:
                resolved = _resolve_references(step.get("args") or {}, outputs)
                draft = op.plan(reader, resolved)
                touched += draft.invalidates
                applied = draft.args
            result = op.apply(reader, applied)
            if op.outputs and not isinstance(result, dict):
                raise errors.UpstreamError(
                    f"{op.name} returned no record of what it produced.",
                    what="Later steps could not reference its output.",
                    retry_with=[{"note": "report this; the op declares outputs"}],
                )
        except Exception as exc:
            failure = _failure(index, step, exc)
            break
        outputs.append(result if isinstance(result, dict) else {})
        done.append({"step": index + 1, "label": step.get("label") or op.name,
                     "op": op.name, "result": result})

    total = offset + len(args["steps"])
    response = {"completed": done, "n_completed": len(done), "n_steps": total,
                "invalidates": touched}
    if failure is None:
        return response
    exc = failure.pop("exc")
    response["failed_at"] = failure
    remaining = f"steps {failure['step'] + 1}-{total}" if failure["step"] < total \
        else "no later steps"
    response["note"] = (
        f"Step {failure['step']} ({failure['label']}) failed, so {remaining} did not "
        f"run. The {len(done)} step(s) before it did, and are listed in `completed`.")
    if done:
        return response
    # Nothing changed, so the caller gets the step's own typed refusal -- a NotFound
    # or InvalidQuery is something to fix, not something to retry.
    raise type(exc)(exc.message, what=exc.what, field=exc.field, given=exc.given,
                    candidates=exc.candidates, retry_with=exc.retry_with,
                    user_action=exc.user_action or "Nothing was changed.",
                    failed_at=failure, **exc.detail)


CHAIN_OPS = [
    Op("chain", RISK_WRITE, plan_chain, apply_chain),
]

mutations.OPS.update({op.name: op for op in CHAIN_OPS})
