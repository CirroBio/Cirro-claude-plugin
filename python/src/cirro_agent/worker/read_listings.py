"""Small direct listings that need no facets.
"""
from typing import Optional

from .. import errors
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, shrink_to_fit, wrap
from . import rows as R
from .reads import Reader


def _flat(reader: Reader, request: dict, kind: str, items: list,
          next_calls: Optional[list] = None, note: Optional[str] = None) -> dict:
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = wrap(
        query={"verb": "list", "of": kind, "scope": request.get("scope") or {}},
        result={"of": kind, "count": len(items), "items": items,
                "next_calls": next_calls or [], **({"note": note} if note else {})},
        cost=reader.cost.to_dict(), max_tokens=max_tokens)
    shrink_to_fit(envelope["result"], "items", floor=3, max_tokens=max_tokens,
                  degraded=envelope["budget"]["degraded"])
    envelope["budget"]["used_est"] = estimate_tokens(envelope)
    return envelope


def do_list(request: dict) -> dict:
    of = request.get("of")
    reader = Reader(request)
    scope = request.get("scope") or {}

    if of == "folders":
        project_id = reader.scope_project_ids(scope)[0]
        datasets, complete = reader.datasets(project_id)
        counts, contributors = {}, {}
        for row in datasets:
            names = row.get("folders") or [R.UNFILED]
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
        return _flat(reader, request, of, reader.reference_types())

    if of == "users":
        return _flat(reader, request, of, reader.users())

    project_id = reader.scope_project_ids(scope)[0]

    if of == "compute_envs":
        return _flat(reader, request, of, reader.compute_environments(project_id))

    if of == "shares":
        return _flat(reader, request, of, reader.shares(project_id))

    if of == "workspaces":
        return _flat(reader, request, of, reader.workspaces(project_id))

    if of == "project_users":
        return _flat(reader, request, of, reader.project_users(project_id))

    raise errors.InvalidQuery(
        f"Unknown listing {of!r}.", what="Pick a supported listing.", field="of", given=of,
        retry_with=[{"of": name} for name in ("folders", "reference_types", "users",
                                              "compute_envs", "shares", "workspaces",
                                              "project_users")],
    )
