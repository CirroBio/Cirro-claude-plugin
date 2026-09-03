"""Small direct listings that need no facets.
"""
from typing import Optional
from .. import errors
from ..envelope import CONTRACT, DEFAULT_MAX_TOKENS, estimate_tokens
from . import rows as R
from .reads import Reader


def _flat(reader: Reader, request: dict, kind: str, items: list,
          next_calls: Optional[list] = None, note: Optional[str] = None) -> dict:
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = {
        "ok": True, "api": {"contract": CONTRACT},
        "query": {"verb": "list", "of": kind, "scope": request.get("scope") or {}},
        "result": {"of": kind, "count": len(items), "items": items,
                   "next_calls": next_calls or [],
                   **({"note": note} if note else {})},
        "cost": reader.cost.to_dict(),
        "budget": {"max_tokens": max_tokens, "degraded": []},
    }
    while estimate_tokens(envelope) > max_tokens and len(envelope["result"]["items"]) > 3:
        keep = max(3, len(envelope["result"]["items"]) // 2)
        envelope["result"]["items"] = envelope["result"]["items"][:keep]
        envelope["result"]["truncated_to"] = keep
        envelope["budget"]["degraded"].append({"what": "items", "to": str(keep)})
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_list(request: dict) -> dict:
    of = request.get("of")
    reader = Reader(request)
    scope = request.get("scope") or {}

    if of == "folders":
        project_id = reader.scope_project_ids(scope)[0]
        datasets, complete = reader.datasets(project_id)
        UNFILED = "(not in a folder)"
        counts, contributors = {}, {}
        for row in datasets:
            names = row.get("folders") or [UNFILED]
            for name in names:
                counts[name] = counts.get(name, 0) + 1
                contributors.setdefault(name, set()).add(row.get("created_by"))
        items = [
            {"folder": name, "datasets": n,
             "contributors": len(contributors.get(name, ()))}
            for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        note = None
        if not complete:
            note = "the dataset scan was truncated, so these counts are lower bounds"
        return _flat(reader, request, of, items, next_calls=[{
            "id": "nc_in_folder",
            "why": "list the datasets in the largest folder",
            "call": {"verb": "query", "entity": "dataset",
                     "scope": {"project_ids": [project_id]},
                     "filter": {"field": "folders", "op": "has_folder",
                                "value": items[0]["folder"]} if items else None},
        }] if items else [], note=note)

    if of == "reference_types":
        found = reader.client.references.get_types() or []
        reader.cost.call("references.get_types")
        return _flat(reader, request, of, [
            {"name": R.plain(getattr(t, "name", None)),
             "description": R.plain(getattr(t, "description", None)),
             "directory": R.plain(getattr(t, "directory", None))} for t in found])

    if of == "users":
        found = reader.client.users.list() or []
        reader.cost.call("users.list")
        return _flat(reader, request, of, [
            {"username": R.plain(getattr(u, "username", None)),
             "name": R.plain(getattr(u, "name", None)),
             "email": R.plain(getattr(u, "email", None)),
             "organization": R.plain(getattr(u, "organization", None))} for u in found])

    project_id = reader.scope_project_ids(scope)[0]

    if of == "compute_envs":
        found = reader.client.compute_environments.list_environments_for_project(
            project_id=project_id) or []
        reader.cost.call("compute_environments.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(e, "id", None)),
             "name": R.plain(getattr(e, "name", None))} for e in found])

    if of == "shares":
        found = reader.client.shares.list(project_id=project_id) or []
        reader.cost.call("shares.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(s, "id", None)),
             "name": R.plain(getattr(s, "name", None)),
             "share_type": R.plain(getattr(s, "share_type", None)),
             "description": R.plain(getattr(s, "description", None))} for s in found])

    if of == "workspaces":
        found = reader.client.workspaces.list(project_id=project_id) or []
        reader.cost.call("workspaces.list")
        return _flat(reader, request, of, [
            {"id": R.plain(getattr(w, "id", None)),
             "name": R.plain(getattr(w, "name", None)),
             "status": R.plain(getattr(w, "status", None)),
             "environment_id": R.plain(getattr(w, "environment_id", None)),
             "started_at": R.plain(getattr(w, "started_at", None))} for w in found])

    if of == "project_users":
        found = reader.client.projects.get_users(project_id=project_id) or []
        reader.cost.call("projects.get_users")
        return _flat(reader, request, of, [
            {"username": R.plain(getattr(u, "username", None)),
             "role": R.plain(getattr(u, "role", None))} for u in found])

    raise errors.InvalidQuery(
        f"Unknown listing {of!r}.", what="Pick a supported listing.", field="of", given=of,
        retry_with=[{"of": name} for name in ("folders", "reference_types", "users",
                                              "compute_envs", "shares", "workspaces",
                                              "project_users")],
    )
