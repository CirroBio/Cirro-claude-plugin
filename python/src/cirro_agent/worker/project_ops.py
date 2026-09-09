"""Projects, the people in them, and their sample metadata.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.
"""


from .. import errors
from . import mutations, reads, rows as R
from .mutations import (Draft, Op, RISK_DESTRUCTIVE, RISK_WRITE, _inv, _need, _project,
                        _tag_models, PROJECT_ARGS)


def plan_create_project(reader: reads.Reader, args: dict) -> Draft:
    # ProjectSettings requires a budget, so there is no way to create a project
    # without stating one -- and it belongs in the approval text regardless.
    _need(args, "create_project", "name", "billing_account_id", "budget_amount",
          "budget_period")
    accounts = reader.client.billing.list() or []
    reader.cost.call("billing.list")
    account = next((a for a in accounts
                    if R.plain(getattr(a, "id", None)) == args["billing_account_id"]), None)
    if account is None:
        raise errors.NotFound(
            f"No billing account {args['billing_account_id']!r} available to you.",
            what="A project must be attached to a billing account you can use.",
            field="billing_account_id", given=args["billing_account_id"],
            candidates=[{"id": R.plain(getattr(a, "id", None)),
                         "name": R.plain(getattr(a, "name", None))} for a in accounts],
            retry_with=[{"op": "create_project",
                         "args": {"billing_account_id": R.plain(getattr(a, "id", None))}}
                        for a in accounts[:3]],
        )
    contacts = args.get("contacts") or []
    for contact in contacts:
        missing = [k for k in ("name", "organization", "email", "phone")
                   if not contact.get(k)]
        if missing:
            raise errors.InvalidQuery(
                f"Each contact needs name, organization, email and phone; missing "
                f"{missing}.",
                what="Complete the contact entries.", field="contacts", given=contacts,
                retry_with=[{"op": "create_project", "args": {"contacts": [
                    {"name": "...", "organization": "...", "email": "...",
                     "phone": "..."}]}}],
            )
    lines = [
        "Create a new Cirro project.",
        "",
        f"  Name             {args['name']}",
        f"  Description      {args.get('description', '')!r}",
        f"  Billing account  {R.plain(getattr(account, 'name', None))} "
        f"({args['billing_account_id']})",
        f"  Budget           {args['budget_amount']} per {args['budget_period']}",
        f"  Contacts         {len(contacts)}",
        "",
        "A project provisions cloud infrastructure and begins accruing storage charges "
        "against that billing account.",
    ]
    return Draft(args={"name": args["name"], "description": args.get("description", ""),
                       "billing_account_id": args["billing_account_id"],
                       "budget_amount": args["budget_amount"],
                       "budget_period": args["budget_period"],
                       "contacts": contacts,
                       "classification_ids": args.get("classification_ids") or [],
                       "tags": args.get("tags") or []},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def apply_create_project(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ProjectInput, ProjectSettings, Contact
    request = ProjectInput(
        name=args["name"], description=args.get("description") or "",
        billing_account_id=args["billing_account_id"],
        settings=ProjectSettings(budget_amount=args["budget_amount"],
                                 budget_period=args["budget_period"]),
        contacts=[Contact(name=c["name"], organization=c["organization"],
                          email=c["email"], phone=c["phone"])
                  for c in (args.get("contacts") or [])],
    )
    if args.get("classification_ids"):
        request.classification_ids = args["classification_ids"]
    if args.get("tags"):
        request.tags = _tag_models(args["tags"])
    response = reader.client.projects.create(request=request)
    return {"created_project_id": R.plain(getattr(response, "id", None))}


def plan_update_project_tags(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_project_tags", "tags")
    project = _project(reader, args)
    lines = [f"Replace the tags on project {project['name']} ({project['id']}).", "",
             f"  from  {project.get('tags')}", f"  to    {args['tags']}"]
    return Draft(args={"project_id": project["id"], "tags": args["tags"]},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def apply_update_project_tags(reader: reads.Reader, args: dict) -> dict:
    reader.client.projects.update_tags(project_id=args["project_id"],
                                       tags=_tag_models(args["tags"]))
    return {"updated_tags_for": args["project_id"], "tags": args["tags"]}


def _plan_project_archive(reader: reads.Reader, args: dict, *, archive: bool) -> Draft:
    project = _project(reader, args)
    verb = "Archive" if archive else "Unarchive"
    datasets, _ = reader.datasets(project["id"]) if archive else ([], True)
    lines = [f"{verb} project {project['name']} ({project['id']}).", "",
             f"  Current status  {project['status']}"]
    if archive:
        lines.append(f"  Contains        {len(datasets)} datasets")
        lines.append("")
        lines.append("Archiving moves the project's data to cold storage and blocks new "
                     "analyses until it is unarchived.")
    return Draft(args={"project_id": project["id"], "archive": archive},
                 summary="\n".join(lines), invalidates=[_inv("projects")])


def plan_archive_project(reader: reads.Reader, args: dict) -> Draft:
    return _plan_project_archive(reader, args, archive=True)


def plan_unarchive_project(reader: reads.Reader, args: dict) -> Draft:
    return _plan_project_archive(reader, args, archive=False)


def apply_archive_project(reader: reads.Reader, args: dict) -> dict:
    if args["archive"]:
        reader.client.projects.archive(project_id=args["project_id"])
    else:
        reader.client.projects.unarchive(project_id=args["project_id"])
    return {"project_id": args["project_id"],
            "archived" if args["archive"] else "unarchived": True}


def plan_set_user_role(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "set_user_role", "username", "role")
    from cirro_api_client.v1.models import ProjectRole
    project = _project(reader, args)
    role = str(args["role"]).upper()
    valid = [r.name for r in ProjectRole]
    if role not in valid:
        raise errors.InvalidQuery(
            f"{args['role']!r} is not a project role.",
            what="Use a valid role.", field="role", given=args["role"],
            retry_with=[{"op": "set_user_role", "args": {"role": r}} for r in valid],
        )
    current = reader.project_users(project["id"])
    existing = next((u for u in current if u["username"] == args["username"]), None)
    was = existing["role"] if existing else "not a member"
    lines = [
        f"Change project access for {args['username']} on {project['name']} "
        f"({project['id']}).",
        "", f"  from  {was}", f"  to    {role}",
    ]
    warnings = []
    if role in ("OWNER", "ADMIN"):
        warnings.append(f"{role} can change project settings and other members' access.")
    if role == "NONE":
        warnings.append(f"This removes {args['username']}'s access to the project.")
    return Draft(args={"project_id": project["id"], "username": args["username"],
                       "role": role,
                       "suppress_notification": bool(args.get("suppress_notification"))},
                 summary="\n".join(lines), warnings=warnings)


def apply_set_user_role(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import ProjectRole
    reader.client.projects.set_user_role(
        project_id=args["project_id"], username=args["username"],
        role=ProjectRole[args["role"]],
        suppress_notification=args.get("suppress_notification", False))
    return {"project_id": args["project_id"], "username": args["username"],
            "role": args["role"]}


def plan_invite_user(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "invite_user", "name", "email", "organization")
    lines = [
        "Invite a new user to this Cirro tenant.",
        "", f"  Name          {args['name']}",
        f"  Email         {args['email']}",
        f"  Organization  {args['organization']}",
        "",
        "Cirro emails this address. The invitation cannot be recalled from here.",
    ]
    return Draft(args={"name": args["name"], "email": args["email"],
                       "organization": args["organization"]},
                 summary="\n".join(lines),
                 warnings=["This sends an email to an external address."],
                 invalidates=[_inv("users")])


def apply_invite_user(reader: reads.Reader, args: dict) -> dict:
    reader.client.users.invite_user(
        name=args["name"], organization=args["organization"], email=args["email"])
    return {"invited": args["email"]}


def plan_update_sample(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_sample", "sample_id", "metadata")
    project = _project(reader, args)
    existing, _ = reader.samples(project["id"])
    row = next((s for s in existing if s["id"] == args["sample_id"]), None)
    if row is None:
        raise errors.NotFound(
            f"No sample {args['sample_id']!r} in {project['name']}.",
            what="Check the sample ID.", field="sample_id", given=args["sample_id"],
            retry_with=[{"verb": "summarize", "of": "samples",
                         "scope": {"project_ids": [project["id"]]}}],
        )
    before = row.get("metadata") or {}
    supplied = args["metadata"]
    # SampleRequest replaces the whole metadata object, so a partial dict would delete
    # every key it omits. Merge unless the caller explicitly asks to replace.
    replace = bool(args.get("replace_metadata"))
    after = dict(supplied) if replace else {**before, **supplied}
    changed = {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}
    dropped = sorted(set(before) - set(after))

    lines = [f"Update metadata on sample {row['name']} ({row['id']}) in "
             f"{project['name']}.", ""]
    for key, (old, new) in sorted(changed.items()):
        lines.append(f"  {key}: {old!r} -> {new!r}")
    if dropped:
        lines += ["", f"  REMOVED: {dropped}"]
    if not changed and not dropped:
        lines.append("  (no values differ from what is stored)")

    warnings = []
    if dropped:
        warnings.append(f"replace_metadata was set, so these keys are deleted: {dropped}")
    elif not replace and set(before) - set(supplied):
        warnings.append(
            f"{len(set(before) - set(supplied))} existing metadata key(s) not named here "
            "are preserved; pass replace_metadata to delete them instead.")
    return Draft(args={"project_id": project["id"], "sample_id": row["id"],
                       "name": args.get("name") or row["name"], "metadata": after},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("samples", project["id"])])


def apply_update_sample(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import SampleRequest, SampleRequestMetadata
    reader.client.metadata.update_sample(
        project_id=args["project_id"], sample_id=args["sample_id"],
        sample=SampleRequest(name=args["name"],
                             metadata=SampleRequestMetadata.from_dict(args["metadata"])))
    return {"updated_sample": args["sample_id"]}


def _current_schema_keys(reader: reads.Reader, project_id: str) -> tuple:
    """The metadata keys a project declares today, or why they are unknown.

    Reading the absent `form_spec_json` left old_keys empty for every project, which
    rendered as "keys removed []" and suppressed the removal warning -- so a schema
    replacement that dropped keys was approved as though it dropped none. A document
    that cannot be read is an unknown baseline, never an empty one; only a document
    with nothing in it declares no keys.
    """
    try:
        document = reads.project_schema_document(reader, project_id)
    except Exception as exc:
        # The exception TYPE, never its text. This reason is rendered into the approval
        # and `_same_disclosure` compares it verbatim at apply, so an upstream body
        # carrying a request id would make every affected plan PLAN_STALE.
        return [], f"the schema request failed ({type(exc).__name__})"
    if document is None:
        return [], "the schema did not come back as a document"
    keys = reads.declared_schema_keys(document)
    if keys is None:
        if document:
            return [], (f"the schema document {sorted(document)} declares no properties "
                        "under form, metadataRequirements or its root")
        return [], None
    return keys, None


def plan_update_project_schema(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_project_schema", "schema")
    project = _project(reader, args)
    schema_payload = args["schema"]
    # Both sides of the diff are read the same way, so a payload that declares its
    # fields under `form` is not reported as removing every key it keeps.
    new_keys = (reads.declared_schema_keys(schema_payload) or []) \
        if isinstance(schema_payload, dict) else []
    old_keys, baseline_error = _current_schema_keys(reader, project["id"])

    lines = [f"Replace the sample-metadata schema of {project['name']} "
             f"({project['id']}).", ""]
    warnings = []
    removed = set(old_keys) - set(new_keys)
    if baseline_error is None:
        lines += [f"  keys removed  {sorted(removed)}",
                  f"  keys added    {sorted(set(new_keys) - set(old_keys))}",
                  f"  keys kept     {len(set(old_keys) & set(new_keys))}"]
    else:
        lines += [f"  keys after    {new_keys}",
                  "  keys removed  unknown -- the current schema could not be read"]
        warnings.append(
            f"The current schema could not be read ({baseline_error}), so this plan "
            "cannot say which keys would be removed. Replacing a schema you cannot "
            "see risks dropping keys that existing sample metadata validates against.")
    if removed:
        warnings.append(f"Removing schema keys {sorted(removed)} affects how existing "
                        "sample metadata validates.")
    return Draft(args={"project_id": project["id"], "schema": schema_payload},
                 summary="\n".join(lines), warnings=warnings)


def apply_update_project_schema(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import FormSchema
    schema_obj = FormSchema.from_dict(args["schema"]) \
        if isinstance(args["schema"], dict) else args["schema"]
    reader.client.metadata.update_project_schema(project_id=args["project_id"],
                                                 schema=schema_obj)
    return {"updated_schema_for": args["project_id"]}


PROJECT_OPS = [
    Op("create_project", RISK_WRITE, plan_create_project, apply_create_project,
       outputs=("created_project_id",),
       accepts=("name", "description", "billing_account_id", "budget_amount",
                "budget_period", "classification_ids", "contacts", "tags")),
    Op("update_project_tags", RISK_WRITE, plan_update_project_tags,
       apply_update_project_tags, accepts=PROJECT_ARGS + ("tags",)),
    Op("archive_project", RISK_DESTRUCTIVE, plan_archive_project, apply_archive_project,
       accepts=PROJECT_ARGS),
    Op("unarchive_project", RISK_WRITE, plan_unarchive_project, apply_archive_project,
       accepts=PROJECT_ARGS),
    Op("set_user_role", RISK_WRITE, plan_set_user_role, apply_set_user_role,
       accepts=PROJECT_ARGS + ("username", "role", "suppress_notification")),
    Op("invite_user", RISK_WRITE, plan_invite_user, apply_invite_user,
       accepts=("email", "name", "organization")),
    Op("update_sample", RISK_WRITE, plan_update_sample, apply_update_sample,
       accepts=PROJECT_ARGS + ("sample_id", "name", "metadata", "replace_metadata")),
    Op("update_project_schema", RISK_WRITE, plan_update_project_schema,
       apply_update_project_schema, accepts=PROJECT_ARGS + ("schema",)),
]

mutations.OPS.update({op.name: op for op in PROJECT_OPS})
