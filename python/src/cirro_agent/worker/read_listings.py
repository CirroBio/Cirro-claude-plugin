"""Small direct listings that need no facets.
"""
from typing import Optional

from .. import errors
from ..envelope import DEFAULT_MAX_TOKENS, estimate_tokens, shrink_to_fit, wrap
from . import rows as R
from .reads import Reader, all_project_metrics, project_metrics

LISTINGS = ("folders", "reference_types", "users", "compute_envs", "shares",
            "workspaces", "project_users", "spend")


def _flat(reader: Reader, request: dict, kind: str, items: list,
          next_calls: Optional[list] = None, note: Optional[str] = None,
          totals: Optional[dict] = None) -> dict:
    max_tokens = int(request.get("max_tokens") or DEFAULT_MAX_TOKENS["query"])
    envelope = wrap(
        query={"verb": "list", "of": kind, "scope": request.get("scope") or {}},
        result={"of": kind, "count": len(items), "items": items,
                **({"totals": totals} if totals else {}),
                "next_calls": next_calls or [], **({"note": note} if note else {})},
        cost=reader.cost.to_dict(), max_tokens=max_tokens)
    shrink_to_fit(envelope, "items", floor=3, max_tokens=max_tokens,
                  fix={"verb": "list", "of": kind, "scope": request.get("scope") or {},
                       "max_tokens": max_tokens * 2})
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

    # Handled before the scope is resolved: spend across every project is one call,
    # so it must not go through the per-project fan-out gate.
    if of == "spend":
        return _spend(reader, request, scope)

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
        retry_with=[{"of": name} for name in LISTINGS],
    )


def _spend(reader: Reader, request: dict, scope: dict) -> dict:
    """What a project, or the whole tenant, has actually cost.

    Cirro reports spend that has already happened, by month and by AWS service. It
    prices nothing in advance, which is why plans still carry no estimate.
    """
    if scope.get("projects") == "*":
        return _spend_by_project(reader, request)

    project_id = reader.scope_project_ids(scope)[0]
    months = sorted((r for r in project_metrics(reader, project_id)
                     if r["series"] == "cost"),
                    key=lambda r: str(r.get("date") or ""), reverse=True)
    items = [{"month": _month(row["date"]), "unit": row["unit"], "total": row["total"],
              "by_service": row["by_service"]} for row in months]
    if not items:
        return _flat(reader, request, "spend", [],
                     note="the metrics API holds no cost records for this project")
    return _flat(
        reader, request, "spend", items,
        totals={"unit": items[0]["unit"], "months": len(items),
                "total": round(sum(i["total"] for i in items), 2),
                "latest_month": items[0]["month"], "latest_total": items[0]["total"]},
        note="cost already incurred, by month; Cirro prices nothing in advance",
        next_calls=[{
            "id": "nc_spend_everywhere",
            "why": "compare every visible project, which is a single API call",
            "call": {"verb": "list", "of": "spend", "scope": {"projects": "*"}},
        }])


def _spend_by_project(reader: Reader, request: dict) -> dict:
    names = {p["id"]: p["name"] for p in reader.projects()}
    by_project: dict = {}
    for row in all_project_metrics(reader):
        if row["series"] != "cost":
            continue
        entry = by_project.setdefault(row["project_id"], {
            "project_id": row["project_id"],
            "project": names.get(row["project_id"]),
            "unlisted": row["project_id"] not in names,
            "unit": row["unit"], "total": 0.0, "months": 0,
            "latest_month": None, "latest_total": None})
        entry["total"] = round(entry["total"] + row["total"], 2)
        entry["months"] += 1
        month = _month(row["date"])
        if entry["latest_month"] is None or str(month) > str(entry["latest_month"]):
            entry["latest_month"], entry["latest_total"] = month, row["total"]
    items = sorted(by_project.values(), key=lambda e: -e["total"])
    if not items:
        return _flat(reader, request, "spend", [],
                     note="the metrics API holds no cost records for any visible project")
    top = items[0]["project"] or items[0]["project_id"]
    note = ("cost already incurred per project, from one call; Cirro prices nothing "
            "in advance")
    # Observed live: the metrics endpoint returns projects the project listing does
    # not, so a name is sometimes genuinely unavailable. An unlabeled id with no
    # explanation is the thing this response shape exists to avoid.
    unlisted = [item for item in items if item["unlisted"]]
    if unlisted:
        note += (f". {len(unlisted)} of these {'is' if len(unlisted) == 1 else 'are'} "
                 "not in your project list -- archived, deleted, or access since "
                 "removed -- so no name is available and only the id is given; the "
                 "total includes them")
    return _flat(
        reader, request, "spend", items,
        totals={"unit": items[0]["unit"], "projects": len(items),
                "unlisted": len(unlisted),
                "total": round(sum(i["total"] for i in items), 2)},
        note=note,
        next_calls=[{
            "id": "nc_spend_top_project",
            "why": f"month-by-month breakdown for {top}",
            "call": {"verb": "list", "of": "spend",
                     "scope": {"project_ids": [items[0]["project_id"]]}},
        }])


def _month(stamp: Optional[str]) -> Optional[str]:
    """Cost records are dated on the first of the month; report the month."""
    return str(stamp)[:7] if stamp else None
