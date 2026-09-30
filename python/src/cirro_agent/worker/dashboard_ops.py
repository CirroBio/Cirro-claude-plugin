"""Data Studio dashboards: create, edit and delete, each behind plan and apply.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.

The planner builds the whole new document, validates it against the portal's schema
and runs its data in DuckDB, so what the person approves is the finished dashboard
and what it will show -- not a request that might render as nothing. The portal
accepts any blob and degrades a bad one silently, which is why a plan with an error
in it is refused rather than warned about.

Concurrency: the portal auto-saves an open dashboard, so a plan can go stale while it
waits. Each blob write carries a revision in `criteria.revision` (the portal checks it
too); apply refuses when the record's revision or blob moved since the plan read it.
"""

import uuid

from .. import dashboards as D, errors
from . import dashboard_data, mutations, reads
from .mutations import (Draft, Op, RISK_WRITE, RISK_DESTRUCTIVE, _dataset, _inv, _need,
                        _project, PROJECT_ARGS)


def _source_resolver(reader: reads.Reader, project_id: str):
    """add_source names a dataset + path or a sheet; this turns either into the node
    config the portal's own source wizard would write."""
    sheet_rows = []

    def resolve(change: dict) -> tuple:
        ref = D.source_ref(change)
        if ref[0] == "file":
            dataset = _dataset(reader, project_id, ref[1])
            entries, _, _ = reads.dataset_manifest(
                reader, project_id, dataset["id"],
                is_terminal=bool(dataset.get("is_terminal", True)))
            by_path = {e["path"]: e for e in entries}
            entry = by_path.get(ref[2]) or by_path.get(f"data/{ref[2]}")
            if entry is None:
                tabular = sorted(p for p in by_path if D.file_type(p))
                raise ValueError(f"{ref[2]!r} is not a file in dataset {dataset['name']}; "
                                 f"its tabular files include {tabular[:20]}")
            kind = D.file_type(entry["path"])
            if kind is None:
                raise ValueError(f"{entry['path']} is not a table the portal can load "
                                 f"({', '.join(D.TABULAR_TYPES)})")
            config = {"datasetId": dataset["id"], "datasetName": dataset["name"],
                      "path": entry["path"], "fileType": kind,
                      "tableName": D.file_table_name(entry["path"])}
            if isinstance(entry.get("size"), int):
                config["sizeBytes"] = entry["size"]
            return "dataset-file", config
        if not sheet_rows:
            sheet_rows.extend(reads.sheets(reader, project_id))
        sheet = next((s for s in sheet_rows if s.get("id") == ref[1]), None)
        if sheet is None:
            named = [s for s in sheet_rows if s.get("name") == ref[1]
                     or s.get("tableName") == ref[1]]
            hint = f"; {ref[1]!r} is the name of {[s['id'] for s in named]}" if named else ""
            raise ValueError(
                f"no sheet with id {ref[1]!r}{hint}. Sheets: "
                f"{[{'id': s.get('id'), 'name': s.get('name')} for s in sheet_rows][:20]}")
        return "dataset-source", {"sheetId": sheet["id"], "tableName": sheet["tableName"],
                                  "sql": f"SELECT * FROM {sheet['tableName']}"}

    return resolve


def _build(reader, project_id, schema, document, changes) -> D.Builder:
    builder = D.Builder(schema, document, _source_resolver(reader, project_id))
    try:
        builder.apply(changes if changes is not None else [])
    except D.ChangeError as exc:
        where = f"changes[{exc.index}]" if exc.index >= 0 else "changes"
        raise errors.InvalidQuery(
            f"{where}: {exc.message}",
            what="Fix that change and plan again. `cirro-agent read dashboard-schema` "
                 "lists every change, chart and option.",
            field=f"{where}.{exc.field}" if exc.field else where,
            given=(changes[exc.index] if 0 <= exc.index < len(changes or []) else changes),
            retry_with=[{"verb": "dashboard_schema"}])
    return builder


def _check(reader, project_id, schema, document, touched, check_data: bool,
           before=None) -> tuple:
    """(issues, profile lines). Refuses the plan on any error it caused -- a problem on a
    node the change neither wrote nor feeds is carried as a warning."""
    issues = D.validate(schema, document, strict_nodes=touched, before=before)
    lines = []
    if check_data and not D.errors_only(issues):
        runner = dashboard_data.Runner(reader, project_id, schema)
        results = runner.run(document)
        issues += D.only_affected(dashboard_data.view_issues(schema, document, results),
                                  D.downstream(document, touched))
        lines = dashboard_data.render_profiles(document, results)
    failures = D.errors_only(issues)
    if failures:
        raise errors.InvalidQuery(
            f"The dashboard would not work: {len(failures)} problem(s). "
            + " | ".join(f"{i.get('node', '-')}: {i['message']}" for i in failures[:8]),
            what="Fix each problem in the change list and plan again. Nothing was written.",
            field="changes", issues=failures, retry_with=[{"verb": "dashboard_schema"}])
    return [i for i in issues if i["level"] == "warning"], lines


def _render(header: list, changes: list, profiles: list, warnings: list, schema_note,
            check_data) -> str:
    lines = header + ["", "  Changes"] + (changes or ["    (none to the graph)"])
    if check_data:
        lines += (["", "  Data, run locally in DuckDB the way the portal will"]
                  + (profiles or ["    (no tables)"]))
    else:
        lines += ["", "  Data was not checked (check_data=false)."]
    if schema_note:
        lines += ["", f"  {schema_note}"]
    return "\n".join(lines)


def _criteria(base: dict, document: dict) -> dict:
    """The portal's record envelope: its type tag, and a scope refined from the graph
    while the record is still provisional (the portal's `refineScopeCriteria`)."""
    criteria = {k: v for k, v in (base or {}).items() if k != "revision"}
    criteria["type"] = D.SESSION_TYPE
    provisional = (criteria.get("scope") in (None, "project") and not criteria.get("datasetId")
                   and not criteria.get("sheetId"))
    if provisional:
        inferred = D.infer_scope(document)
        criteria.update(inferred if inferred["scope"] != "project"
                        else {"scope": criteria.get("scope") or "project"})
    return criteria


def _warning_text(warnings: list) -> list:
    return [f"{w.get('node', 'dashboard')}: {w['message']}" for w in warnings]


# ------------------------------------------------------------------ create

CREATE_ARGS = PROJECT_ARGS + ("name", "description", "changes", "check_data")


def plan_create_dashboard(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "create_dashboard", "name")
    project = _project(reader, args)
    schema, note = reads.dashboard_schema(reader)
    check_data = args.get("check_data", True) is not False
    builder = _build(reader, project["id"], schema, D.empty_document(schema),
                     args.get("changes"))
    document = builder.doc
    warnings, profiles = _check(reader, project["id"], schema, document, builder.touched,
                                check_data)
    header = [f"Create Data Studio dashboard {args['name']!r} in {project['name']} "
              f"({project['id']}).",
              "", f"  Description  {args.get('description') or ''!r}"]
    summary = _render(header, D.render_changes(schema, None, document), profiles, warnings,
                      note, check_data)
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "description": args.get("description") or "",
                       "dashboard_data": document, "criteria": _criteria({}, document)},
                 summary=summary, warnings=_warning_text(warnings),
                 invalidates=[_inv("dashboards", project["id"])])


def _input(name, description, document, criteria, tags, schema_version):
    from cirro_api_client.v1.models import DashboardInput

    return DashboardInput.from_dict({"name": name, "description": description,
                                     "dashboardData": document, "criteria": criteria,
                                     "tags": tags or [], "schemaVersion": schema_version})


def apply_create_dashboard(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.dashboards import create_dashboard

    criteria = {**args["criteria"], "revision": str(uuid.uuid4())}
    created = reads.send(reader, create_dashboard, what="Creating the dashboard failed.",
                         cost="dashboards.create", project_id=args["project_id"],
                         body=_input(args["name"], args["description"],
                                     args["dashboard_data"], criteria, [],
                                     D.RECORD_SCHEMA_VERSION))
    dashboard_id = (created or {}).get("id")
    return {"created_dashboard_id": dashboard_id,
            "url": (D.studio_url(reader.base_url, args["project_id"], dashboard_id)
                    if dashboard_id else None)}


# ------------------------------------------------------------------ edit

EDIT_ARGS = PROJECT_ARGS + ("dashboard_id", "name", "description", "changes", "check_data")


def plan_edit_dashboard(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "edit_dashboard", "dashboard_id")
    project = _project(reader, args)
    record = reads.dashboard_record(reader, project["id"], args["dashboard_id"])
    schema, note = reads.dashboard_schema(reader)
    current = record.get("dashboardData")
    if (record.get("criteria") or {}).get("type") != D.SESSION_TYPE:
        raise errors.NotSupported(
            f"Dashboard {record['id']} is not a Data Studio dashboard.",
            what="Only records the Data Studio shows can be edited here.",
            user_action="Open it in the Cirro portal, or pick another dashboard.")
    before = current if D.is_session_blob(current) else D.empty_document(schema)
    structural = D._structure_issues(before)
    if structural:
        raise errors.NotSupported(
            f"Dashboard {record['id']}'s stored document is not readable: "
            f"{structural[0]['message']}.",
            what="Editing it would overwrite whatever it holds.",
            user_action="Open it in the Cirro portal to see what it contains.")
    changes = args.get("changes") or []
    name = args.get("name") or record["name"]
    description = (args["description"] if args.get("description") is not None
                   else (record.get("description") or ""))
    if (not changes and name == record["name"]
            and description == (record.get("description") or "")):
        raise errors.InvalidQuery("Nothing to change.",
                                  what="Give changes, a new name or a new description.",
                                  field="changes",
                                  retry_with=[{"verb": "dashboard", "id": record["id"]}])
    builder = _build(reader, project["id"], schema, before, changes)
    document = builder.doc
    check_data = args.get("check_data", True) is not False
    warnings, profiles = _check(reader, project["id"], schema, document, builder.touched,
                                check_data, before=before)
    header = [f"Edit Data Studio dashboard {record['name']!r} ({record['id']}) "
              f"in {project['name']}.", ""]
    if name != record["name"]:
        header.append(f"  Name         {record['name']!r} -> {name!r}")
    if description != (record.get("description") or ""):
        header.append(f"  Description  {record.get('description') or ''!r} -> {description!r}")
    header.append(
        f"  Open         {D.studio_url(reader.base_url, project['id'], record['id'])}")
    # The portal serves its schema from the release that also checks revisions, so a
    # schema that came from the portal is what says an open tab will not overwrite this.
    header.append(
        "  An open copy of this dashboard reloads to show the change, or, if it has unsaved "
        "edits, refuses to save over it." if note is None else
        "  This portal predates the save check: a copy of the dashboard open with unsaved "
        "edits will overwrite this change when it auto-saves.")
    summary = _render(header, D.render_changes(schema, before, document), profiles, warnings,
                      note, check_data)
    return Draft(args={"project_id": project["id"], "dashboard_id": record["id"], "name": name,
                       "description": description, "dashboard_data": document,
                       "base_revision": (record.get("criteria") or {}).get("revision"),
                       "base_digest": D.blob_digest(current)}, summary=summary,
                 warnings=_warning_text(warnings),
                 invalidates=[_inv("dashboards", project["id"])])


def _refuse_if_moved(record: dict, args: dict) -> None:
    moved = ((record.get("criteria") or {}).get("revision") != args["base_revision"]
             or D.blob_digest(record.get("dashboardData")) != args["base_digest"])
    if moved:
        raise errors.PlanStale(
            f"Dashboard {record['id']} was saved by someone else after this plan was made.",
            what="Writing now would discard their changes.",
            retry_with=[{"tool": "plan", "op": "edit_dashboard"}],
            user_action="Re-plan so the changes are made on top of the latest version.")


def apply_edit_dashboard(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.dashboards import update_dashboard

    record = reads.dashboard_record(reader, args["project_id"], args["dashboard_id"])
    _refuse_if_moved(record, args)
    criteria = {**_criteria(record.get("criteria") or {}, args["dashboard_data"]),
                "revision": str(uuid.uuid4())}
    reads.send(reader, update_dashboard, what="Saving the dashboard failed.",
               cost="dashboards.update", project_id=args["project_id"],
               dashboard_id=args["dashboard_id"],
               body=_input(args["name"], args["description"], args["dashboard_data"],
                           criteria, record.get("tags") or [],
                           record.get("schemaVersion") or D.RECORD_SCHEMA_VERSION))
    return {"dashboard_id": args["dashboard_id"],
            "url": D.studio_url(reader.base_url, args["project_id"], args["dashboard_id"])}


# ------------------------------------------------------------------ delete


def plan_delete_dashboard(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_dashboard", "dashboard_id")
    project = _project(reader, args)
    record = reads.dashboard_record(reader, project["id"], args["dashboard_id"])
    document = record.get("dashboardData")
    nodes = len(document["ir"]["nodes"]) if D.is_session_blob(document) else 0
    tiles = (sum(len(p.get("tiles", [])) for p in document.get("dashboards", []))
             if D.is_session_blob(document) else 0)
    lines = [
        f"DELETE dashboard {record['name']!r} ({record['id']}) from {project['name']}.", "",
        f"  Created by  {record.get('createdBy')}", f"  Updated     {record.get('updatedAt')}",
        f"  Contents    {nodes} nodes, {tiles} tiles", "",
        "The record and its layout are removed for everyone. "
        "The data it reads is not touched."]
    return Draft(args={"project_id": project["id"], "dashboard_id": record["id"],
                       "dashboard_name": record["name"],
                       "base_digest": D.blob_digest(document),
                       "base_revision": (record.get("criteria") or {}).get("revision")},
                 summary="\n".join(lines), invalidates=[_inv("dashboards", project["id"])])


def apply_delete_dashboard(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.api.dashboards import delete_dashboard

    record = reads.dashboard_record(reader, args["project_id"], args["dashboard_id"])
    _refuse_if_moved(record, args)
    reads.send(reader, delete_dashboard, what="Deleting the dashboard failed.",
               cost="dashboards.delete", project_id=args["project_id"],
               dashboard_id=args["dashboard_id"])
    return {"deleted_dashboard": args["dashboard_id"], "name": args.get("dashboard_name")}


DASHBOARD_OPS = [Op("create_dashboard", RISK_WRITE, plan_create_dashboard,
                    apply_create_dashboard, accepts=CREATE_ARGS,
                    outputs=("created_dashboard_id",)),
                 Op("edit_dashboard", RISK_WRITE, plan_edit_dashboard, apply_edit_dashboard,
                    accepts=EDIT_ARGS),
                 Op("delete_dashboard", RISK_DESTRUCTIVE, plan_delete_dashboard,
                    apply_delete_dashboard, accepts=PROJECT_ARGS + ("dashboard_id",))]

mutations.OPS.update({op.name: op for op in DASHBOARD_OPS})
