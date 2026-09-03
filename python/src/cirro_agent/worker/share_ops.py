"""Shares between projects, and interactive workspaces.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.
"""


from .. import errors
from . import mutations, reads, rows as R
from .mutations import (Draft, Op, RISK_WRITE, RISK_DESTRUCTIVE, _inv, _need, _project)


def _share(reader: reads.Reader, project_id: str, share_id: str) -> dict:
    return reads.pick(reader.shares(project_id), share_id, kind="share", field="share_id",
                      brief=lambda s: {"id": s["id"], "name": s["name"],
                                       "share_type": s["share_type"]})


def plan_create_share(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_share", "name")
    project = _project(reader, args)
    targets = args.get("shared_project_ids") or []
    lines = [
        f"Create a data share published from {project['name']} ({project['id']}).",
        "", f"  Share name       {args['name']}",
        f"  Description      {args.get('description', '')!r}",
        f"  Shared with      {len(targets)} project(s): {targets}",
        f"  View restricted  {bool(args.get('is_view_restricted'))}",
        "",
        "A share exposes matching datasets to other projects. Anyone with access to "
        "those projects can read what it selects.",
    ]
    conditions = args.get("conditions") or []
    warnings = ["This makes data readable outside its originating project."]
    if not conditions:
        warnings.append(
            "No dataset conditions were given, so which datasets this exposes is decided "
            "entirely by Cirro's defaults -- confirm the scope in the UI after creating "
            "it.")
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "description": args.get("description", ""),
                       "shared_project_ids": targets,
                       "keywords": args.get("keywords") or [],
                       "classification_ids": args.get("classification_ids") or [],
                       "conditions": conditions,
                       "is_view_restricted": bool(args.get("is_view_restricted"))},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("shares", project["id"])])


def apply_create_share(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ShareInput, DatasetCondition
    request = ShareInput(
        name=args["name"], description=args.get("description") or "",
        classification_ids=args.get("classification_ids") or [],
        conditions=[DatasetCondition(field=c["field"], value=c["value"])
                    for c in (args.get("conditions") or [])],
    )
    for attribute in ("shared_project_ids", "keywords", "is_view_restricted"):
        if args.get(attribute):
            setattr(request, attribute, args[attribute])
    response = reader.client.shares.create(project_id=args["project_id"], share=request)
    return {"created_share_id": R.plain(getattr(response, "id", None))}


def plan_delete_share(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_share", "share_id")
    project = _project(reader, args)
    share = _share(reader, project["id"], args["share_id"])
    lines = [f"DELETE share {share['name']} ({share['id']}) from {project['name']}.", "",
             f"  Share type  {share['share_type']}", "",
             "Subscribers lose access to every dataset this share exposed."]
    return Draft(args={"project_id": project["id"], "share_id": share["id"],
                       "share_name": share["name"]},
                 summary="\n".join(lines),
                 invalidates=[_inv("shares", project["id"])])


def apply_delete_share(reader: reads.Reader, args: dict) -> dict:
    reader.client.shares.delete(project_id=args["project_id"], share_id=args["share_id"])
    return {"deleted_share": args["share_id"], "name": args.get("share_name")}


def _plan_share_subscription(reader: reads.Reader, args: dict, *, subscribe: bool) -> Draft:
    _need(args, "subscribe_share" if subscribe else "unsubscribe_share", "share_id")
    project = _project(reader, args)
    share = _share(reader, project["id"], args["share_id"])
    verb = "Subscribe to" if subscribe else "Unsubscribe from"
    effect = ("Datasets from this share become visible in the project."
              if subscribe else
              "Datasets from this share stop appearing in the project.")
    lines = [f"{verb} share {share['name']} ({share['id']}) for {project['name']}.", "",
             effect]
    return Draft(args={"project_id": project["id"], "share_id": share["id"],
                       "subscribe": subscribe},
                 summary="\n".join(lines),
                 invalidates=[_inv("datasets", project["id"]), _inv("shares", project["id"])])


def plan_subscribe_share(reader: reads.Reader, args: dict) -> Draft:
    return _plan_share_subscription(reader, args, subscribe=True)


def plan_unsubscribe_share(reader: reads.Reader, args: dict) -> Draft:
    return _plan_share_subscription(reader, args, subscribe=False)


def apply_share_subscription(reader: reads.Reader, args: dict) -> dict:
    if args["subscribe"]:
        reader.client.shares.subscribe(project_id=args["project_id"],
                                       share_id=args["share_id"])
    else:
        reader.client.shares.unsubscribe(project_id=args["project_id"],
                                         share_id=args["share_id"])
    return {"share_id": args["share_id"], "subscribed": args["subscribe"]}


def _workspace(reader: reads.Reader, project_id: str, workspace_id: str) -> dict:
    return reads.pick(reader.workspaces(project_id), workspace_id, kind="workspace",
                      field="workspace_id",
                      brief=lambda w: {"id": w["id"], "name": w["name"],
                                       "status": w["status"]})


def plan_create_workspace(reader: reads.Reader, args: dict) -> Draft:
    # WorkspaceComputeConfig requires the container image, so there is no valid
    # workspace without one -- and which image runs is exactly what an approver checks.
    _need(args, "create_workspace", "name", "environment_id", "container_image_uri")
    project = _project(reader, args)
    environments = reader.client.workspaces.list_environments() or []
    reader.cost.call("workspaces.list_environments")
    match = next((e for e in environments
                  if R.plain(getattr(e, "id", None)) == args["environment_id"]), None)
    if match is None:
        raise errors.NotFound(
            f"No workspace environment {args['environment_id']!r}.",
            what="Pick an available environment.", field="environment_id",
            given=args["environment_id"],
            candidates=[{"id": R.plain(getattr(e, "id", None)),
                         "name": R.plain(getattr(e, "name", None))}
                        for e in environments],
            retry_with=[{"op": "create_workspace",
                         "args": {"environment_id": R.plain(getattr(e, "id", None))}}
                        for e in environments[:3]],
        )
    mounted = args.get("mounted_datasets") or []
    sharing = str(args.get("sharing_type") or "PRIVATE").upper()
    valid_sharing = {"PRIVATE", "READ_WRITE", "READ_WRITE_CONTROL"}
    if sharing not in valid_sharing:
        raise errors.InvalidQuery(
            f"{args.get('sharing_type')!r} is not a workspace sharing mode.",
            what="Use a valid sharing mode.", field="sharing_type",
            given=args.get("sharing_type"),
            retry_with=[{"op": "create_workspace", "args": {"sharing_type": mode}}
                        for mode in sorted(valid_sharing)],
        )
    lines = [
        f"Create a workspace in {project['name']} ({project['id']}).",
        "", f"  Name              {args['name']}",
        f"  Environment       {R.plain(getattr(match, 'name', None))} "
        f"({args['environment_id']})",
        f"  Container image   {args['container_image_uri']}",
        f"  Resources         {args.get('cpu') or 'default'} vCPU, "
        f"{args.get('memory_gi_b') or 'default'} GiB"
        + (f", GPU {args['gpu']}" if args.get("gpu") else ""),
        f"  Mounted datasets  {len(mounted)}: {mounted}",
        f"  Sharing           {sharing}",
        f"  Auto-stop         {args.get('auto_stop_timeout') or 'environment default'}",
        "",
        "A running workspace accrues compute charges until it stops.",
    ]
    warnings = []
    if sharing != "PRIVATE":
        warnings.append(f"Sharing is {sharing}: others in the project can reach this "
                        "workspace.")
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "environment_id": args["environment_id"],
                       "description": args.get("description", ""),
                       "mounted_datasets": mounted,
                       "container_image_uri": args["container_image_uri"],
                       "cpu": args.get("cpu"), "memory_gi_b": args.get("memory_gi_b"),
                       "gpu": args.get("gpu"),
                       "volume_size_gi_b": args.get("volume_size_gi_b"),
                       "sharing_type": sharing,
                       "auto_stop_timeout": args.get("auto_stop_timeout")},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("workspaces", project["id"])])


def apply_create_workspace(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (WorkspaceInput, WorkspaceComputeConfig,
                                            SharingType)
    compute = WorkspaceComputeConfig(container_image_uri=args["container_image_uri"])
    for key, attribute in (("cpu", "cpu"), ("memory_gi_b", "memory_gi_b"),
                           ("gpu", "gpu"), ("volume_size_gi_b", "volume_size_gi_b")):
        if args.get(key) is not None:
            setattr(compute, attribute, args[key])
    request = WorkspaceInput(
        name=args["name"], mounted_datasets=args.get("mounted_datasets") or [],
        compute_config=compute,
        sharing_type=SharingType(args.get("sharing_type") or "PRIVATE"))
    request.environment_id = args["environment_id"]
    if args.get("description"):
        request.description = args["description"]
    if args.get("auto_stop_timeout"):
        request.auto_stop_timeout = args["auto_stop_timeout"]
    response = reader.client.workspaces.create(project_id=args["project_id"],
                                               workspace=request)
    return {"created_workspace_id": R.plain(getattr(response, "id", None))}


def _plan_workspace_action(reader: reads.Reader, args: dict, action: str) -> Draft:
    _need(args, f"{action}_workspace", "workspace_id")
    project = _project(reader, args)
    workspace = _workspace(reader, project["id"], args["workspace_id"])
    effects = {
        "start": "The workspace boots and begins accruing compute charges.",
        "stop": "Running processes are terminated; persisted files are kept.",
        "delete": "The workspace and anything stored only inside it are destroyed.",
    }
    lines = [
        f"{action.capitalize()} workspace {workspace['name']} ({workspace['id']}) in "
        f"{project['name']}.",
        "", f"  Current status  {workspace['status']}", "", effects[action],
    ]
    return Draft(args={"project_id": project["id"], "workspace_id": workspace["id"],
                       "action": action, "workspace_name": workspace["name"]},
                 summary="\n".join(lines),
                 invalidates=[_inv("workspaces", project["id"])])


def plan_start_workspace(reader, args):
    return _plan_workspace_action(reader, args, "start")


def plan_stop_workspace(reader, args):
    return _plan_workspace_action(reader, args, "stop")


def plan_delete_workspace(reader, args):
    return _plan_workspace_action(reader, args, "delete")


# An explicit map, so a tampered `action` is a rejected plan rather than an arbitrary
# method call on the workspaces client.
_WORKSPACE_ACTIONS = ("start", "stop", "delete")


def apply_workspace_action(reader: reads.Reader, args: dict) -> dict:
    action = args.get("action")
    if action not in _WORKSPACE_ACTIONS:
        raise errors.PlanInvalid(
            f"{action!r} is not a workspace action.",
            what="The stored plan names an operation that does not exist.",
            retry_with=[{"tool": "plan", "op": f"{a}_workspace"}
                        for a in _WORKSPACE_ACTIONS],
        )
    getattr(reader.client.workspaces, action)(project_id=args["project_id"],
                                              workspace_id=args["workspace_id"])
    return {"workspace_id": args["workspace_id"], "action": action}


SHARE_OPS = [
    Op("create_share", RISK_WRITE, plan_create_share, apply_create_share),
    Op("delete_share", RISK_DESTRUCTIVE, plan_delete_share, apply_delete_share),
    Op("subscribe_share", RISK_WRITE, plan_subscribe_share, apply_share_subscription),
    Op("unsubscribe_share", RISK_WRITE, plan_unsubscribe_share, apply_share_subscription),
    Op("create_workspace", RISK_WRITE, plan_create_workspace, apply_create_workspace),
    Op("start_workspace", RISK_WRITE, plan_start_workspace, apply_workspace_action),
    Op("stop_workspace", RISK_WRITE, plan_stop_workspace, apply_workspace_action),
    Op("delete_workspace", RISK_DESTRUCTIVE, plan_delete_workspace,
       apply_workspace_action),
]

mutations.OPS.update({op.name: op for op in SHARE_OPS})
