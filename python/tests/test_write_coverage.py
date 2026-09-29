"""Contract-drift guard: no SDK write method may go unconsidered.

Without this, "every mutation is gated" is a one-time audit. With it, a `cirro`
version bump that introduces a new write method breaks the build instead of
quietly shipping an ungated mutation.

Every write-shaped method in the SDK must appear in exactly one of the two maps
below. Adding a method to UNSUPPORTED is a deliberate, reviewable choice; forgetting
it entirely is a test failure.
"""
import importlib
import inspect
import pkgutil
import re
from cirro_agent import cli

WRITE_SHAPED = re.compile(
    r"^(create|update|delete|upload|run_|stop|archive|unarchive|invite|set_|sync_"
    r"|import_|subscribe|unsubscribe|start|resume|rerun|insert|save|rename"
    r"|refresh_view|trigger_)", re.I)

# SDK method -> the plugin operation that exposes it behind plan/apply.
GATED = {
    "ExecutionService.run_analysis": "run_analysis, resume_analysis",
    "ExecutionService.stop_analysis": "stop_analysis",
    "DataPortalDataset.run_analysis": "run_analysis",
    "DataPortalProcess.run_analysis": "run_analysis",
    "DataPortalProject.upload_dataset": "upload_dataset",
    "DatasetService.create": "upload_dataset",
    "DatasetService.upload_files": "upload_dataset",
    "DatasetService.update": "update_dataset",
    "DatasetService.delete": "delete_dataset",
    "DatasetService.update_samplesheet": "update_samplesheet",
    "DeveloperHelper.rerun_sample_ingest_for_dataset":
        "update_samplesheet, update_dataset",
    "DataPortalDataset.update_samplesheet": "update_samplesheet",
    "DatasetService.import_public": "import_public",
    "ReferenceService.upload_reference": "upload_reference",
    "ProjectService.create": "create_project",
    "ProjectService.update_tags": "update_project_tags",
    "ProjectService.archive": "archive_project",
    "ProjectService.unarchive": "unarchive_project",
    "ProjectService.set_user_role": "set_user_role",
    "UserService.invite_user": "invite_user",
    "MetadataService.update_sample": "update_sample",
    "MetadataService.update_project_schema": "update_project_schema",
    "ShareService.create": "create_share",
    "ShareService.delete": "delete_share",
    "ShareService.subscribe": "subscribe_share",
    "ShareService.unsubscribe": "unsubscribe_share",
    "WorkspaceService.create": "create_workspace",
    "WorkspaceService.start": "start_workspace",
    "WorkspaceService.stop": "stop_workspace",
    "WorkspaceService.delete": "delete_workspace",
    "DashboardService.create": "create_dashboard",
    "DashboardService.update": "edit_dashboard",
    "DashboardService.delete": "delete_dashboard",
    "DataPortalProject.create_dashboard": "create_dashboard",
    "DataPortalDashboard.save": "edit_dashboard",
    "DataPortalDashboard.rename": "edit_dashboard",
    "DataPortalDashboard.delete": "delete_dashboard",
}

_SHEET_WRITES = ("sheets are read as dashboard sources only; creating, changing and "
                 "deleting sheets and their rows is not exposed")

# Deliberately not exposed. Each entry is a decision with a reason, not an oversight.
UNSUPPORTED = {
    "ProjectService.update":
        "renaming or re-billing a project is a web-UI action; only update_tags is exposed",
    "ShareService.update":
        "editing a live share's conditions silently changes who can read what; web UI only",
    "ProcessService.archive":
        "process lifecycle belongs to pipeline developers, not this plugin",
    "ProcessService.create_custom_process":
        "pipeline registration is owned by the cirro-pipeline-config plugin",
    "ProcessService.update_custom_process":
        "pipeline registration is owned by the cirro-pipeline-config plugin",
    "ProcessService.sync_custom_process":
        "pipeline registration is owned by the cirro-pipeline-config plugin",
    "BillingService.update":
        "billing changes have contractual consequences; web UI only",
    "FileService.create_file":
        "a primitive beneath dataset upload; reachable only via upload_dataset",
    "FileService.upload_files":
        "a primitive beneath dataset upload; reachable only via upload_dataset",
    **{method: _SHEET_WRITES for method in (
        "SheetService.create", "SheetService.update", "SheetService.delete",
        "SheetService.insert_rows", "SheetService.update_rows", "SheetService.delete_rows",
        "SheetService.refresh_view", "SheetService.trigger_ingest",
        "DataPortalProject.create_sheet", "DataPortalSheet.update", "DataPortalSheet.delete",
        "DataPortalSheet.insert_rows", "DataPortalSheet.update_rows",
        "DataPortalSheet.delete_rows")},
}


def discover() -> dict:
    found = {}
    for package in ("cirro.services", "cirro.sdk"):
        module = importlib.import_module(package)
        for info in pkgutil.iter_modules(module.__path__):
            submodule = importlib.import_module(f"{package}.{info.name}")
            for class_name, cls in inspect.getmembers(submodule, inspect.isclass):
                if cls.__module__ != submodule.__name__:
                    continue
                for method_name, _ in inspect.getmembers(cls, inspect.isfunction):
                    if method_name.startswith("_"):
                        continue
                    if WRITE_SHAPED.match(method_name):
                        found[f"{class_name}.{method_name}"] = submodule.__name__
    return found


def test_every_sdk_write_method_is_gated_or_explicitly_unsupported():
    discovered = set(discover())
    considered = set(GATED) | set(UNSUPPORTED)
    unconsidered = discovered - considered
    assert not unconsidered, (
        "These SDK write methods are neither gated nor explicitly unsupported. The "
        "plugin's rule is that every create, update and delete is gated, so each one "
        "must be added to GATED (with the operation that exposes it) or to UNSUPPORTED "
        "(with a reason):\n  " + "\n  ".join(sorted(unconsidered)))


def test_the_maps_do_not_reference_methods_that_no_longer_exist():
    discovered = set(discover())
    stale = (set(GATED) | set(UNSUPPORTED)) - discovered
    assert not stale, (
        "These entries name SDK methods that no longer exist and should be removed:\n  "
        + "\n  ".join(sorted(stale)))


def test_the_two_maps_are_disjoint():
    overlap = set(GATED) & set(UNSUPPORTED)
    assert not overlap, f"listed as both gated and unsupported: {sorted(overlap)}"


def test_every_gated_entry_names_a_registered_operation():
    from cirro_agent.worker import mutations
    for method, ops in GATED.items():
        for op in (name.strip() for name in ops.split(",")):
            assert op in mutations.OPS, f"{method} claims op {op!r}, which is not registered"


def test_every_registered_operation_has_a_planner_and_an_executor():
    from cirro_agent.worker import mutations
    for name, op in mutations.OPS.items():
        assert callable(op.plan), name
        assert callable(op.apply), name
        assert op.risk in (mutations.RISK_WRITE, mutations.RISK_DESTRUCTIVE,
                           mutations.RISK_LOCAL, mutations.RISK_WRITE_LOCAL), name


def test_the_destructive_risk_name_is_one_string():
    """`plans` carries a copy because the PreToolUse gate reads a plan with it and
    cannot import `mutations`, which imports cirro. Two spellings would mean a
    destructive plan the gate had quietly stopped marking as one."""
    from cirro_agent import plans
    from cirro_agent.worker import mutations
    assert plans.DESTRUCTIVE_RISK == mutations.RISK_DESTRUCTIVE


def test_the_read_cli_cannot_reach_a_mutating_verb():
    from cirro_agent import cli
    from cirro_agent.worker import dispatch
    mutating = {"plan", "apply", "login_daemon"}
    assert not (cli.READ_VERBS & mutating)
    assert cli.READ_VERBS <= set(dispatch.HANDLERS), \
        "the CLI advertises a verb the worker cannot handle"


def test_every_verb_the_read_cli_builds_is_declared_in_read_verbs():
    """The converse of the check above, and the one that was missing.

    `chain_status` was routed by dispatch and built by the CLI but absent from
    READ_VERBS, so `read chain-status` refused itself as a write on every call. Only
    the subset direction was asserted, which cannot catch that.
    """
    import ast
    import pathlib
    from cirro_agent import cli
    tree = ast.parse(pathlib.Path(cli.__file__).read_text())
    built = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "verb"
                    and isinstance(value, ast.Constant)):
                built.add(value.value)
    assert built, "the scan found no verb literals, so it is not testing anything"
    assert built <= cli.READ_VERBS, \
        f"the CLI builds verbs it has not declared: {sorted(built - cli.READ_VERBS)}"


def test_the_read_cli_module_does_not_import_the_mutations_registry():
    import ast
    import pathlib
    source = pathlib.Path(cli.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("mutations" in name for name in imported), \
        "cli.py must not import the mutations registry; it is read-only by construction"


# --- an argument nobody reads must not be dropped in silence -----------------
#
# A planner reads the arguments it knows by name and nothing looked at the rest, so
# `execution_mode`, or a typo'd `enviroment_type`, vanished and the run went somewhere
# the approved text did not describe. `Op.accepts` closes that, and these two keep the
# declaration honest: every op has one, and no planner reads a key outside it.

def _op_modules():
    from cirro_agent.worker import mutations  # noqa: F401  (loads every family)
    import pathlib
    from cirro_agent.worker import (analysis_ops, chain_ops, dashboard_ops, dataset_ops,
                                    pipeline_ops, project_ops, share_ops)
    modules = [mutations, analysis_ops, chain_ops, dashboard_ops, dataset_ops, pipeline_ops,
               project_ops, share_ops]
    return {m.__name__.rsplit(".", 1)[-1]: pathlib.Path(m.__file__) for m in modules}


def _functions():
    import ast
    found = {}
    for name, path in _op_modules().items():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef):
                found.setdefault(node.name, node)
    return found


def _args_key_names(node):
    """Local names used as an `args` key: `args.get(n)`, `args[n]`, `n in args`."""
    import ast
    names = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "args" and sub.args
                and isinstance(sub.args[0], ast.Name)):
            names.add(sub.args[0].id)
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                and sub.value.id == "args" and isinstance(sub.slice, ast.Name)):
            names.add(sub.slice.id)
        if _membership_in_args(sub) and isinstance(sub.left, ast.Name):
            names.add(sub.left.id)
    return names


def _membership_in_args(sub):
    import ast
    return (isinstance(sub, ast.Compare) and len(sub.ops) == 1
            and isinstance(sub.ops[0], ast.In)
            and isinstance(sub.comparators[0], ast.Name)
            and sub.comparators[0].id == "args")


def _loop_keys(loop):
    """Keys a `for` over a literal sequence feeds to `args` through its loop variable.

    `plan_update_process` reads most of its surface this way -- a tuple of
    `(key, current)` pairs walked in a loop -- so a walk that only recognised
    `args.get("literal")` saw almost none of it and the declaration drifted unnoticed.
    """
    import ast
    if not isinstance(loop.iter, (ast.Tuple, ast.List)):
        return set()
    used = _args_key_names(loop)
    target = loop.target
    slots = ([target] if isinstance(target, ast.Name)
             else list(target.elts) if isinstance(target, ast.Tuple) else [])
    keys = set()
    for index, slot in enumerate(slots):
        if not isinstance(slot, ast.Name) or slot.id not in used:
            continue
        for element in loop.iter.elts:
            part = element
            if isinstance(target, ast.Tuple):
                if not isinstance(element, (ast.Tuple, ast.List)) \
                        or index >= len(element.elts):
                    continue
                part = element.elts[index]
            if isinstance(part, ast.Constant):
                keys.add(part.value)
    return keys


def _keys_read(node, functions, seen):
    """Argument keys this function reads, following the helpers it hands `args` to."""
    import ast
    keys, called = set(), set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if (isinstance(func, ast.Attribute) and func.attr == "get"
                    and isinstance(func.value, ast.Name) and func.value.id == "args"
                    and sub.args and isinstance(sub.args[0], ast.Constant)):
                keys.add(sub.args[0].value)
            if isinstance(func, ast.Name) and func.id == "_need":
                keys.update(a.value for a in sub.args[2:]
                            if isinstance(a, ast.Constant))
            if isinstance(func, ast.Name) and any(
                    isinstance(a, ast.Name) and a.id == "args" for a in sub.args):
                called.add(func.id)
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                and sub.value.id == "args" and isinstance(sub.slice, ast.Constant)):
            keys.add(sub.slice.value)
        if _membership_in_args(sub) and isinstance(sub.left, ast.Constant):
            keys.add(sub.left.value)
        if isinstance(sub, ast.For):
            keys |= _loop_keys(sub)
    for name in called:
        if name in functions and name not in seen:
            keys |= _keys_read(functions[name], functions, seen | {name})
    return keys


def test_every_op_declares_the_arguments_it_accepts():
    from cirro_agent.worker import mutations
    undeclared = sorted(name for name, op in mutations.OPS.items() if not op.accepts)
    assert not undeclared, \
        f"these ops accept no arguments, so every call is refused: {undeclared}"


def test_no_planner_reads_an_argument_its_op_does_not_declare():
    """The declaration is by hand, so this checks it against the code that reads args.

    A key the planner reads but does not declare is refused before the planner runs --
    the argument becomes unusable rather than silently ignored, which is a louder bug
    than the one this replaced but a bug all the same.
    """
    from cirro_agent.worker import mutations
    functions = _functions()
    missing = {}
    for name, op in mutations.OPS.items():
        planner = getattr(op.plan, "__name__", None)
        if planner not in functions:
            continue  # a test double or a closure; the declaration test still covers it
        gap = _keys_read(functions[planner], functions, {planner}) - set(op.accepts)
        if gap:
            missing[name] = sorted(gap)
    assert not missing, f"planners read arguments their op does not accept: {missing}"
