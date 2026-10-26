"""Creating, editing, deleting and downloading datasets.

Registers its operations into `mutations.OPS` on import; see mutations.py for
why the registration lives on this side.
"""

import math
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .. import errors, runner, summarize as S
from . import mutations, reads, rows as R
from .mutations import (Draft, Op, RISK_DESTRUCTIVE, RISK_LOCAL, RISK_WRITE,
                        _check_dataset_name, _check_tags, _dataset, _enum_choice, _inv,
                        _need, _project, _tag_models, _target_folders, _wire_tags,
                        FOLDER_ARGS,
                        PROJECT_ARGS)


# How many lines of a samplesheet diff the approval quotes before saying how many
# more there are.
DIFF_PREVIEW_LINES = 60
# A download that would fill the disk past this fraction is warned about, not refused --
# the person approving it knows things about their own disk that this does not. The
# margin is what the operating system and the transfer itself need.
FREE_SPACE_HEADROOM = 0.9


def _process_label(name: Optional[str], process_id: Optional[str]) -> str:
    """A process as the approval names it: never a bare id."""
    if not process_id:
        return "(none)"
    return f"{name} ({process_id})" if name else process_id


def _dataset_edit(row: dict, args: dict, process: Optional[dict],
                  followups: dict) -> Optional[dict]:
    """What one dataset's record should become, or None if nothing would change.

    UpdateDatasetRequest requires name, description, process_id AND tags -- it is a
    whole-record replacement. Omitting a field blanks it, so every one is resolved
    here from the current record and only the requested changes are applied.

    `process` is the resolved new data type or None, and `followups` says whether the
    sample ingest and the data transforms run behind it. They are recorded per dataset
    rather than per plan because they follow the retype: a dataset the batch leaves at
    its current type is not re-ingested and its transforms are not re-run.
    """
    current_folders = list(row.get("folders") or [])
    current_tags = list(row.get("tags") or [])
    target_folders = _target_folders(args, current_folders)
    target_tags = list(args["tags"]) if args.get("tags") is not None else current_tags
    target_process = process["id"] if process else row.get("process_id")
    retyped = target_process != row.get("process_id")

    changes = {key: args[key] for key in ("name", "description")
               if args.get(key) is not None and args[key] != row.get(key)}
    if (not changes and not retyped and target_folders == current_folders
            and sorted(target_tags) == sorted(current_tags)):
        return None
    was_process = _process_label((row.get("process") or {}).get("name"),
                                 row.get("process_id"))
    now_process = (_process_label(process["name"], process["id"]) if retyped
                   else was_process)
    return {
        "dataset_id": row["id"], "dataset_name": row.get("name"),
        "name": changes.get("name", row.get("name")),
        "description": changes.get("description", row.get("description") or ""),
        "process_id": target_process,
        "process": now_process,
        "ingest_samples": retyped and followups["ingest_samples"],
        "rerun_transform": retyped and followups["rerun_transform"],
        "folders": target_folders, "tags": target_tags,
        "was": {"name": row.get("name"), "description": row.get("description"),
                "process_id": row.get("process_id"), "process": was_process,
                "folders": current_folders, "tags": current_tags},
        "changed": sorted(changes),
    }


def _ingest_process(reader: reads.Reader, args: dict) -> Optional[dict]:
    """The data type a dataset is being retyped to, or None if it is not being retyped.

    Resolved over every process rather than over the ingest ones alone: narrowing the
    pool first would answer a pipeline name with "no such process", which reads as a
    typo when the real answer is that a pipeline is not a thing a dataset can be.
    """
    if args.get("process") is None:
        return None
    process = reader.resolve_process(args["process"])
    if process["executor"] != "INGEST":
        raise errors.InvalidQuery(
            f"{process['name']} ({process['id']}) is a pipeline, not a data type.",
            what="A dataset can only be retyped to an ingest process. A pipeline id "
                 "on a dataset says that pipeline produced it.",
            field="process", given=args["process"],
            retry_with=[{"verb": "query", "entity": "process",
                         "filter": "executor:INGEST"}],
        )
    return process


def _retyped_subject(n_retyped: int) -> str:
    return ("the retyped dataset" if n_retyped == 1
            else f"the {n_retyped} retyped datasets")


def _followup_lines(n_retyped: int, followups: dict) -> list:
    """What happens behind the retype, in the text the person approves.

    Both halves are stated either way. Silence about the one that is off reads as the
    plan doing it, and these are the two that decide whether the dataset's samples and
    its derived files agree with the type it now claims to be.
    """
    subject = _retyped_subject(n_retyped)
    if followups["rerun_transform"]:
        transform = (f"Then re-run the data transforms and web optimization for "
                     f"{subject}, regenerating what Cirro derives from the files under "
                     "the new type.")
    else:
        transform = ("The data transforms will NOT be re-run, so anything Cirro derived "
                     "from the files stays as the old type left it. Pass "
                     "rerun_transform=true to re-run them.")
    if followups["ingest_samples"]:
        ingest = (f"Then re-ingest samples for {subject}, updating the project's sample "
                  "metadata to match the new type.")
    else:
        ingest = ("The project's sample metadata will NOT be updated, so its samples "
                  "still describe the old type. Pass ingest_samples=true to re-ingest.")
    return [transform, ingest]


def plan_update_dataset(reader: reads.Reader, args: dict) -> Draft:
    """Rename, re-describe, retag, or move one or more datasets between folders.

    Folder membership and tags share one underlying list, and the API replaces that
    list wholesale. Writing tags alone would therefore silently move a dataset out of
    its folder, so the folder is always resolved explicitly here and carried through.

    Several datasets can be edited under one approval. Each one's current record is
    read separately, so "move these into QC" preserves each dataset's own tags rather
    than flattening them to a shared list.

    `process` retypes the dataset. The type it becomes must be an ingest process --
    a pipeline id describes what produced a dataset, and writing one onto a dataset
    no pipeline produced makes the record claim something untrue. The type it comes
    from is unrestricted: mistyping an upload and correcting an analysis output are
    the same fix.
    """
    given = args.get("dataset_ids") or ([args["dataset_id"]]
                                        if args.get("dataset_id") else [])
    if not given:
        raise errors.InvalidQuery(
            "update_dataset needs dataset_id or dataset_ids.",
            what="Name the dataset(s) to edit.", field="dataset_ids",
            retry_with=[{"verb": "query", "entity": "dataset"}],
        )
    if args.get("name") is not None:
        _check_dataset_name(args["name"])
    _check_tags(args)
    project = _project(reader, args)

    if len(given) > 1 and (args.get("name") is not None
                           or args.get("description") is not None):
        raise errors.InvalidQuery(
            f"name and description cannot be set on {len(given)} datasets at once.",
            what="They would all end up with the same name.",
            field="name" if args.get("name") is not None else "description",
            retry_with=[{"op": "update_dataset",
                         "args": {"dataset_ids": given, "folder": "Some Folder"}}],
        )

    process = _ingest_process(reader, args)
    followups = {"ingest_samples": bool(args.get("ingest_samples", True)),
                 "rerun_transform": bool(args.get("rerun_transform", True))}

    rows = [_dataset(reader, project["id"], one) for one in given]
    edits = [(row, _dataset_edit(row, args, process, followups)) for row in rows]
    actionable = [edit for _row, edit in edits if edit is not None]
    unchanged = [row["name"] for row, edit in edits if edit is None]

    if not actionable:
        raise errors.InvalidQuery(
            "Nothing would change.",
            what="Supply a new name, description, process, folder, or tags.",
            given=args,
            retry_with=[
                {"op": "update_dataset", "args": {"dataset_ids": given,
                                                  "folder": "Some Folder"}},
                {"op": "update_dataset", "args": {"dataset_ids": given,
                                                  "tags": ["example"]}},
            ],
        )

    if len(actionable) == 1:
        edit = actionable[0]
        lines = [f"Update dataset {edit['dataset_name']} ({edit['dataset_id']}) in "
                 f"{project['name']}.", ""]
        lines += _edit_lines(edit, indent="  ")
    else:
        lines = [f"Update {len(actionable)} datasets in {project['name']} "
                 f"({project['id']}).", ""]
        for edit in actionable:
            lines.append(f"  {edit['dataset_name']} ({edit['dataset_id']})")
            lines += _edit_lines(edit, indent="    ")
            lines.append("")

    retyped = [edit for edit in actionable
               if edit["process_id"] != edit["was"]["process_id"]]
    if retyped:
        lines += ["", *_followup_lines(len(retyped), followups)]

    warnings = []
    if retyped:
        keeps = "keeps the files it already has" if len(retyped) == 1 else \
            "keep the files they already have"
        warnings.append(
            f"Retyping does not re-validate the files: {_retyped_subject(len(retyped))} "
            f"{keeps}, which may not be what the new type expects.")
        if followups["ingest_samples"]:
            warnings.append(
                "Sample re-ingest re-derives samples and their metadata from the "
                "dataset's file names under the new type, replacing the project's "
                "current sample records for these datasets. Values edited by hand "
                "since the last ingest are overwritten.")
        # Already fetched by the resolver, so this costs no call.
        ingest_ids = {row["id"] for row in reader.processes(include_archived=True)
                      if row["executor"] == "INGEST"}
        one_way = [edit for edit in retyped
                   if edit["was"]["process_id"] and
                   edit["was"]["process_id"] not in ingest_ids]
        if one_way:
            warnings.append(
                f"{len(one_way)} of these dataset(s) are typed as a pipeline today, "
                "and this operation will not write a pipeline back -- so this retype "
                "cannot be undone from here. Use the Cirro web UI to restore it.")
        downstream = sorted({d["name"] for edit in retyped
                             for d in _derived_from(reader, project["id"],
                                                    edit["dataset_id"])})
        if downstream:
            warnings.append(
                f"{len(downstream)} dataset(s) list a retyped dataset as their source: "
                + ", ".join(downstream[:5]) + (" ..." if len(downstream) > 5 else "")
                + ". Their lineage will report a source of a different type.")
    if unchanged:
        warnings.append(f"{len(unchanged)} of {len(given)} already match and will be "
                        f"left alone: {', '.join(unchanged)}.")
    preserved = sorted({f for edit in actionable
                        if edit["folders"] == edit["was"]["folders"]
                        for f in edit["was"]["folders"]})
    if args.get("tags") is not None and preserved:
        warnings.append(f"Folder membership ({', '.join(preserved)}) is preserved by "
                        "this change.")
    if args.get("remove_from_folder"):
        emptied = sorted({f for edit in actionable for f in edit["was"]["folders"]})
        if emptied:
            warnings.append(f"This takes {len(actionable)} dataset(s) out of "
                            f"{', '.join(emptied)}; they will no longer appear under "
                            "any folder.")
    if args.get("tags") is not None and len(actionable) > 1:
        warnings.append(f"The tag list is replaced on all {len(actionable)} datasets, "
                        "so any tags unique to one of them are dropped.")

    scopes = [_inv("datasets", project["id"])]
    if any(edit["ingest_samples"] for edit in actionable):
        scopes.append(_inv("samples", project["id"]))
    return Draft(
        args={"project_id": project["id"], "edits": actionable},
        summary="\n".join(lines).rstrip(), warnings=warnings,
        invalidates=scopes,
        ungrantable=mutations.ungrantable(
            retyped and "it retypes, which re-derives sample records",
            any(set(e["was"]["tags"]) - set(e["tags"]) for e in actionable)
            and "it removes tags",
            any(set(e["was"]["folders"]) - set(e["folders"]) for e in actionable)
            and "it takes datasets out of a folder",
            len(actionable) > mutations.GRANT_MAX_TARGETS
            and f"it edits more than {mutations.GRANT_MAX_TARGETS} datasets"),
    )


def _edit_lines(edit: dict, *, indent: str) -> list:
    lines = []
    for key in edit["changed"]:
        lines += [f"{indent}{key}", f"{indent}  from  {edit['was'][key]!r}",
                  f"{indent}  to    {edit[key]!r}"]
    if edit["process_id"] != edit["was"]["process_id"]:
        lines += [f"{indent}process", f"{indent}  from  {edit['was']['process']}",
                  f"{indent}  to    {edit['process']}"]
    if edit["folders"] != edit["was"]["folders"]:
        was = ", ".join(edit["was"]["folders"]) or R.UNFILED
        now = ", ".join(edit["folders"]) or R.UNFILED
        lines += [f"{indent}folder", f"{indent}  from  {was}", f"{indent}  to    {now}"]
    if sorted(edit["tags"]) != sorted(edit["was"]["tags"]):
        lines += [f"{indent}tags", f"{indent}  from  {edit['was']['tags']}",
                  f"{indent}  to    {edit['tags']}"]
    return lines


def apply_update_dataset(reader: reads.Reader, args: dict) -> dict:
    """Apply each dataset's resolved record, reporting per dataset.

    One API call per dataset, so a batch can fail partway. Raising on the first
    failure would report the whole operation as failed while some datasets had
    already been changed, and the audit trail would say so -- so failures are
    collected and every dataset's outcome is named.
    """
    from cirro_api_client.v1.models import UpdateDatasetRequest

    updated, failed = [], []
    for edit in args["edits"]:
        # One list on the wire: folder membership plus plain tags. The planner already
        # decided the full contents, so nothing is dropped here.
        combined = _wire_tags(edit.get("folders"), edit.get("tags"))
        request = UpdateDatasetRequest(
            name=edit["name"],
            description=edit.get("description") or "",
            process_id=edit["process_id"],
            tags=_tag_models(combined),
        )
        try:
            detail = reader.client.datasets.update(
                project_id=args["project_id"], dataset_id=edit["dataset_id"],
                request=request)
        except Exception as exc:
            failed.append({"dataset_id": edit["dataset_id"],
                           "name": edit["dataset_name"],
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        entry = {"dataset_id": edit["dataset_id"],
                 "name": R.plain(getattr(detail, "name", None)),
                 "folders": edit.get("folders") or [],
                 "tags": edit.get("tags") or []}
        # The record is already written, so a failed follow-up is reported against
        # that dataset rather than raised: the retype stands either way, and saying
        # the whole operation failed would be a lie the audit trail would keep.
        for key, queue in (("rerun_transform", _queue_transform_rerun),
                           ("ingest_samples", _queue_sample_ingest)):
            if not edit.get(key):
                continue
            try:
                queue(reader, args["project_id"], edit["dataset_id"])
            except Exception as exc:
                entry.setdefault("followups_failed", {})[key] = \
                    f"{type(exc).__name__}: {exc}"
            else:
                entry[key] = True
        updated.append(entry)

    if failed and not updated:
        raise errors.UpstreamError(
            f"None of the {len(failed)} dataset(s) could be updated.",
            what=failed[0]["error"],
            retry_with=[{"op": "update_dataset", "note": "re-plan and try again"}],
            user_action="Check the Cirro web UI for service status if this persists.",
            failed=failed,
        )
    result = {"updated": updated, "n_updated": len(updated)}
    if any(entry.get("rerun_transform") or entry.get("ingest_samples")
           for entry in updated):
        result["followups_note"] = (
            "Transform re-runs and sample ingests were accepted and run "
            "asynchronously; derived files and project sample metadata catch up "
            "shortly.")
    if any(entry.get("followups_failed") for entry in updated):
        result["followups_note"] = (
            result.get("followups_note", "")
            + " Datasets carrying `followups_failed` were retyped, but the follow-up "
              "named there did not start and can be re-run from the Cirro web UI."
        ).strip()
    if failed:
        result["failed"] = failed
        result["note"] = (f"{len(updated)} of {len(updated) + len(failed)} datasets "
                          "were updated; the rest are unchanged and listed in `failed`.")
    return result


def plan_delete_dataset(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "delete_dataset", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    count = reads.file_count(reader, project["id"], row["id"])
    detail = reads.dataset_detail(reader, project["id"], row["id"], row)
    derived = _derived_from(reader, project["id"], row["id"])

    warnings = []
    if derived:
        warnings.append(
            f"{len(derived)} dataset(s) in this project list {row['id']} as a source: "
            + ", ".join(f"{d['name']} ({d['id']})" for d in derived[:5])
            + (" ..." if len(derived) > 5 else "")
            + ". Their lineage will point at a dataset that no longer exists.")
    if row.get("share"):
        warnings.append("This dataset arrives through a subscribed share; it may not be "
                        "yours to delete.")

    lines = [
        f"DELETE dataset {row['name']} ({row['id']}) from {project['name']} "
        f"({project['id']}).",
        "",
        f"  Status       {row['status']}",
        f"  Created      {row['created_at']} by {row['created_by']}",
        f"  Contents     {count} files, {detail.get('total_size_human')}",
        f"  Produced by  {(row.get('process') or {}).get('name')}",
        "",
        "This is not reversible from here.",
    ]
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "dataset_name": row["name"]},
                 summary="\n".join(lines), warnings=warnings,
                 invalidates=[_inv("datasets", project["id"])])


def apply_delete_dataset(reader: reads.Reader, args: dict) -> dict:
    reader.client.datasets.delete(project_id=args["project_id"],
                                  dataset_id=args["dataset_id"])
    return {"deleted": args["dataset_id"], "name": args.get("dataset_name")}


def plan_update_samplesheet(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "update_samplesheet", "dataset_id")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    contents = args.get("contents")
    if contents is None and args.get("file_path"):
        contents = Path(args["file_path"]).read_text()
    if contents is None:
        raise errors.InvalidQuery(
            "update_samplesheet needs contents or file_path.",
            what="Provide the new samplesheet text.", field="contents",
            retry_with=[{"op": "update_samplesheet",
                         "args": {"dataset_id": row["id"], "contents": "sample,fastq_1\n"}}],
        )

    before = ""
    try:
        from cirro_api_client.v1.models import ArtifactType
        wrapped = reads.portal_dataset(reader, project["id"], row["id"])
        before = wrapped.get_artifact(ArtifactType.SAMPLE_SHEET).read()
        reader.cost.call("artifact.samplesheet")
    except Exception:
        pass

    import difflib
    diff = list(difflib.unified_diff(
        before.splitlines(), contents.splitlines(),
        fromfile="current samplesheet", tofile="proposed samplesheet", lineterm="", n=2))
    shown = diff[:DIFF_PREVIEW_LINES]
    if len(diff) > DIFF_PREVIEW_LINES:
        shown.append(f"... {len(diff) - DIFF_PREVIEW_LINES} more diff lines")

    # Writing the CSV does not itself register anything: the SDK's update_samplesheet
    # puts samplesheet.csv in the dataset prefix and stops there. Re-reading it into
    # the project's sample metadata is a separate endpoint, so unless it is asked for
    # the samplesheet and the samples it describes silently disagree.
    ingest = bool(args.get("ingest_samples", True))

    lines = [f"Replace the samplesheet of {row['name']} ({row['id']}) in "
             f"{project['name']}.", ""]
    lines += ["  " + line for line in (shown or ["(no existing samplesheet to compare)"])]
    lines += ["", ("Then re-ingest this dataset's samples, updating the project's sample "
                   "metadata to match the new samplesheet.") if ingest else
              ("The project's sample metadata will NOT be updated: only the CSV is "
               "replaced. Pass ingest_samples=true to re-ingest.")]
    warnings = []
    if not before:
        warnings.append("No current samplesheet could be read, so this diff is against "
                        "an empty file.")
    if ingest:
        warnings.append("Sample re-ingest rewrites sample records for this dataset "
                        "across the project, not just the CSV. Values that were edited "
                        "by hand since the last ingest are overwritten by the "
                        "samplesheet.")
    scopes = [_inv("datasets", project["id"])]
    if ingest:
        scopes.append(_inv("samples", project["id"]))
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "contents": contents, "ingest_samples": ingest},
                 summary="\n".join(lines), warnings=warnings, invalidates=scopes)


def apply_update_samplesheet(reader: reads.Reader, args: dict) -> dict:
    reader.client.datasets.update_samplesheet(project_id=args["project_id"],
                                              dataset_id=args["dataset_id"],
                                              samplesheet=args["contents"])
    result = {"updated_samplesheet_for": args["dataset_id"],
              "bytes": len(args["contents"]), "samples_ingested": False}
    if not args.get("ingest_samples"):
        return result
    # The samplesheet is already replaced, so a failed ingest is reported rather than
    # raised, as update_dataset reports its follow-ups: a 429 here once made the audit
    # say the whole change failed while the new samplesheet stood.
    try:
        _queue_sample_ingest(reader, args["project_id"], args["dataset_id"])
    except Exception as exc:
        result["ingest_failed"] = f"{type(exc).__name__}: {exc}"
        result["note"] = ("The samplesheet was replaced, but the sample ingest did not "
                          "start. Planning this again re-runs the ingest, or start it "
                          "from the Cirro web UI.")
        return result
    result["samples_ingested"] = True
    result["note"] = ("Sample ingest was accepted and runs asynchronously; project "
                      "sample metadata updates shortly.")
    return result


def _queue_sample_ingest(reader: reads.Reader, project_id: str,
                         dataset_id: str) -> None:
    """Re-derive a dataset's samples, from its samplesheet and its file names.

    202: the ingest is queued, so the project's samples catch up shortly rather than
    by the time this returns. Saying "ingested" would overstate it.
    """
    from cirro_api_client.v1.api.datasets import ingest_samples
    ingest_samples.sync_detailed(project_id=project_id, dataset_id=dataset_id,
                                 client=reader.client._api_client)


def _queue_transform_rerun(reader: reads.Reader, project_id: str,
                           dataset_id: str) -> None:
    """Re-run the data transforms and web optimization over a dataset's files.

    202, like the sample ingest. The SDK wraps neither route, so both go through the
    generated API directly; this is the only place either is reached from.
    """
    from cirro_api_client.v1.api.datasets import rerun_transform
    rerun_transform.sync_detailed(project_id=project_id, dataset_id=dataset_id,
                                  client=reader.client._api_client)


def _scan_folder(folder: Path, include_hidden: bool = False) -> tuple:
    from cirro.file_utils import is_hidden_file
    found, total = [], 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        if not include_hidden and is_hidden_file(Path(relative)):
            continue
        found.append(relative)
        total += path.stat().st_size
    return found, total


# Sustained throughput measured against a live tenant. A rate, not a promise: it is
# used only to say BEFORE the approval that a transfer cannot finish inside the
# worker's lifetime. Discovering that afterwards costs a killed worker and, for an
# upload, a PENDING dataset with nothing in it that someone has to delete by hand.
OBSERVED_TRANSFER_BYTES_PER_S = 2.3 * 1024 * 1024


def _too_big_to_finish(total_bytes: int, verb: str) -> Optional[str]:
    """A warning when the transfer cannot plausibly complete, or None."""
    seconds = (total_bytes or 0) / OBSERVED_TRANSFER_BYTES_PER_S
    if seconds <= runner.TRANSFER_TIMEOUT:
        return None
    return (f"At the ~{OBSERVED_TRANSFER_BYTES_PER_S / 1024 ** 2:.1f} MB/s this plugin "
            f"has measured, {S.human_bytes(total_bytes)} would take about "
            f"{seconds / 3600:.1f} h, and the worker is stopped at "
            f"{runner.TRANSFER_TIMEOUT // 3600} h. {verb} fewer files at a time, or "
            f"use the Cirro web UI.")


def plan_upload_dataset(reader: reads.Reader, args: dict) -> Draft:
    # `source_dir` is the local directory; `folder` is the Cirro folder the dataset
    # lands in. Both were once called `folder`, which made one of the two a footgun.
    _need(args, "upload_dataset", "name", "process", "source_dir")
    _check_dataset_name(args["name"])
    _check_tags(args)
    project = _project(reader, args)
    process = reader.resolve_process(args["process"], ingest=True)
    source_dir = Path(args["source_dir"]).expanduser().resolve()
    if not source_dir.is_dir():
        raise errors.InvalidQuery(
            f"{source_dir} is not a directory.",
            what="Point source_dir at a local directory of files to upload.",
            field="source_dir", given=str(source_dir),
            retry_with=[{"op": "upload_dataset",
                         "args": {"source_dir": str(Path.cwd())}}],
        )
    files, total_bytes = _scan_folder(source_dir, bool(args.get("include_hidden")))
    if not files:
        raise errors.InvalidQuery(
            f"No files found under {source_dir}.",
            what="Check the directory, or pass include_hidden.",
            field="source_dir", given=str(source_dir),
            retry_with=[{"op": "upload_dataset",
                         "args": {"source_dir": str(source_dir),
                                  "include_hidden": True}}],
        )

    warnings = []
    try:
        reader.client.processes.check_dataset_files(
            files=files, process_id=process["id"], directory=str(source_dir))
        reader.cost.call("processes.check_dataset_files")
    except Exception as exc:
        # The SDK runs this check before uploading anyway, so surfacing it now costs
        # nothing and turns a failed upload into a pre-approval warning.
        warnings.append(f"File-name validation for {process['name']!r} reports: {exc}")

    folders = _target_folders(args, [])
    patterns = S.name_patterns(files, limit=5)
    lines = [
        f"Upload {len(files)} files ({S.human_bytes(total_bytes)}) into "
        f"{project['name']} ({project['id']}) as a new dataset.",
        "",
        f"  Dataset name  {args['name']}",
        f"  Data type     {process['name']} ({process['id']})",
        f"  From          {source_dir}",
        f"  Into folder   {', '.join(folders) or '(none)'}",
        "",
        "  Files:",
    ]
    lines += [f"    {p['template']}  x{p['n']}" for p in patterns]
    over_budget = _too_big_to_finish(total_bytes, "Upload")
    if over_budget:
        warnings.append(over_budget)
    return Draft(
        args={"project_id": project["id"], "process_id": process["id"],
              "name": args["name"], "description": args.get("description", ""),
              "source_dir": str(source_dir), "files": files,
              "folders": folders, "tags": args.get("tags") or [],
              "n_files": len(files), "total_bytes": total_bytes},
        summary="\n".join(lines), warnings=warnings,
        invalidates=[_inv("datasets", project["id"])],
    )


# A failed upload's message names at most this many kinds of failure, this many files
# of each, and this many characters of each failure's text. The SDK's own report is a
# line per file: 118 of them on a real tenant, each with nothing after the colon.
UPLOAD_FAILURE_KINDS = 3
UPLOAD_FAILURE_EXAMPLES = 3
UPLOAD_FAILURE_TEXT = 200


def apply_upload_dataset(reader: reads.Reader, args: dict) -> dict:
    """Validate, register and transfer -- the SDK's `upload_dataset`, done step by step.

    The SDK raises from the transfer without the id of the dataset it has just
    registered, and a transfer that fails partway leaves that dataset behind: the one
    thing the person has to act on. Doing the steps here keeps the id in hand. It is
    also what lets a later chain step reference this upload by value instead of
    looking it up again by name, which is racy when two uploads share a name.
    """
    from cirro_api_client.v1.models import UploadDatasetRequest
    files = args["files"]
    reader.client.processes.check_dataset_files(
        files=files, process_id=args["process_id"], directory=args["source_dir"])
    # Folder membership rides in the tag list, so the dataset is filed on creation
    # rather than by a second gated operation afterwards.
    combined = _wire_tags(args.get("folders"), args.get("tags"))
    created = reader.client.datasets.create(
        project_id=args["project_id"],
        upload_request=UploadDatasetRequest(
            name=args["name"], process_id=args["process_id"],
            description=args.get("description") or "", expected_files=files,
            tags=_tag_models(combined) or None))
    failures = {}
    try:
        with _recording_upload_failures(failures):
            reader.client.datasets.upload_files(
                project_id=args["project_id"], dataset_id=created.id,
                directory=args["source_dir"], files=files)
    except Exception as exc:
        raise _failed_upload(args, created.id, exc, failures) from exc
    return {"uploaded_to_project": args["project_id"], "name": args["name"],
            "dataset_id": created.id,
            "n_files": args.get("n_files"),
            "bytes": args.get("total_bytes"),
            "folders": args.get("folders") or [], "tags": args.get("tags") or [],
            "follow_with": {"verb": "get", "entity": "dataset", "id": created.id,
                            "scope": {"project_ids": [args["project_id"]]},
                            "freshness": "fresh"}}


@contextmanager
def _recording_upload_failures(failures: dict):
    """Keep each file's own exception, which the SDK's report reduces to its text.

    `cirro.file_utils` collects every failed transfer and raises one RuntimeError of
    "<key>: <str(exc)>" lines, so an exception that stringifies to nothing -- a
    cancelled s3transfer future, a bare timeout -- arrives as a path and a colon with
    its type gone. Every file goes through `S3Client.upload_file`, so wrapping it for
    the length of one upload sees each failure as it is raised; a retry that then
    succeeds takes its file back out. The worker runs one operation and exits, and the
    method is restored before this returns.
    """
    from cirro.clients.s3 import S3Client
    original = S3Client.upload_file

    def recording(self, file_path, bucket, key, callback=None):
        try:
            original(self, file_path, bucket, key, callback)
        except Exception as exc:
            failures[str(file_path)] = exc
            raise
        failures.pop(str(file_path), None)

    S3Client.upload_file = recording
    try:
        yield
    finally:
        S3Client.upload_file = original


def _failure_text(exc: BaseException) -> str:
    """One bounded line naming the exception's type, and its cause's when it says
    nothing itself."""
    said = str(exc)
    text = f"{type(exc).__name__}: {said}" if said else type(exc).__name__
    cause = exc.__cause__ or exc.__context__
    if not said and cause is not None:
        text += f" (from {type(cause).__name__}: {cause})"
    return text[:UPLOAD_FAILURE_TEXT]


def _failed_upload(args: dict, dataset_id: str, exc: Exception,
                   failures: dict) -> errors.AgentError:
    """A transfer that did not finish: the dataset it left, how much of it arrived,
    and why the rest did not -- summarised by kind of failure, not listed by file."""
    n_files = len(args["files"])
    label = f"{args['name']!r} ({dataset_id})"
    if failures:
        by_kind = {}
        for path, failure in sorted(failures.items()):
            relative = Path(path).relative_to(args["source_dir"]).as_posix()
            by_kind.setdefault(type(failure).__name__, []).append((relative, failure))
        ranked = sorted(by_kind.values(), key=len, reverse=True)
        kinds = [f"{len(hits)} x {_failure_text(hits[0][1])}, e.g. "
                 + ", ".join(path for path, _ in hits[:UPLOAD_FAILURE_EXAMPLES])
                 for hits in ranked[:UPLOAD_FAILURE_KINDS]]
        if len(ranked) > UPLOAD_FAILURE_KINDS:
            kinds.append(f"{len(ranked) - UPLOAD_FAILURE_KINDS} other kind(s) of failure")
        message = (f"Uploaded {n_files - len(failures)} of {n_files} files into "
                   f"{label}; {len(failures)} failed: " + "; ".join(kinds) + ".")
    else:
        message = (f"The upload into {label} failed, and no single file's transfer "
                   f"reported why, so how many of the {n_files} files arrived is not "
                   f"known: {_failure_text(exc)}.")
    return errors.UpstreamError(
        message,
        what="The dataset is registered before its files are sent, and sending them "
             "did not finish.",
        retry_with=[{"op": "delete_dataset",
                     "args": {"project_id": args["project_id"], "dataset_id": dataset_id}},
                    {"op": "upload_dataset",
                     "note": "plan the upload again once the partial dataset is deleted"}],
        user_action=(f"Dataset {label} exists but is missing files. Delete it before "
                     "uploading again, or the retry leaves a second one behind. If the "
                     "failures persist, upload fewer files at a time, or use the Cirro "
                     "web UI."),
        dataset_id=dataset_id, n_files=n_files,
        n_failed=len(failures) if failures else None,
    )


def plan_upload_reference(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "upload_reference", "name", "reference_type", "files")
    project = _project(reader, args)
    # cirro/services/references.py raises on this at upload, so without the check the
    # plan reads clean and apply dies -- inside a chain that spends the approval and
    # stops mid-way. Spaces only: the SDK guesses at no wider charset and neither can
    # this, or a name Cirro accepts would be refused here.
    if " " in args["name"]:
        raise errors.InvalidQuery(
            "Reference names cannot contain spaces.",
            what="Choose a name without spaces.", field="name", given=args["name"],
            retry_with=[{"op": "upload_reference",
                         "args": {"name": args["name"].replace(" ", "_")}}],
        )
    types = reader.reference_types()
    wanted = args["reference_type"]
    match = next((t for t in types if t["name"] == wanted), None)
    if match is None:
        raise errors.NotFound(
            f"No reference type {wanted!r} in this deployment.",
            what="Reference types are deployment-specific; pick one that exists.",
            field="reference_type", given=wanted,
            candidates=[{"name": t["name"], "description": t["description"]} for t in types],
            retry_with=[{"verb": "list", "of": "reference_types"}],
        )
    paths = [Path(f).expanduser().resolve() for f in args["files"]]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise errors.InvalidQuery(
            f"Files not found: {missing}.",
            what="Every reference file must exist locally.", field="files",
            given=args["files"], retry_with=[{"op": "upload_reference"}],
        )
    total = sum(p.stat().st_size for p in paths)
    lines = [
        f"Upload a reference into {project['name']} ({project['id']}).",
        "",
        f"  Reference name  {args['name']}",
        f"  Reference type  {wanted}",
        f"  Files           {len(paths)}, {S.human_bytes(total)}",
    ]
    lines += [f"                    {p}" for p in paths]
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "reference_type": wanted, "files": [str(p) for p in paths]},
                 summary="\n".join(lines),
                 invalidates=[_inv("references", project["id"])])


def apply_upload_reference(reader: reads.Reader, args: dict) -> dict:
    ref_type = reader.client.references.get_type(
        reference_type_name=args["reference_type"])
    reader.client.references.upload_reference(
        name=args["name"], reference_files=[Path(f) for f in args["files"]],
        project_id=args["project_id"], ref_type=ref_type)
    return {"uploaded_reference": args["name"], "project_id": args["project_id"]}


def _download_method(args: dict) -> str:
    """How Cirro fetches the accessions, as a value of the request model's enum.
    Unnamed, it is the request model's own default, which is what goes on the wire
    when nothing is set."""
    from cirro_api_client.v1.models import ImportDataRequestDownloadMethod as Method
    given = args.get("download_method")
    if given is None:
        return Method.SRATOOLS.value
    return _enum_choice(Method, given, field="download_method",
                        what="Cirro downloads public data in one of a few ways.")


def plan_import_public(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "import_public", "name", "public_ids")
    _check_dataset_name(args["name"])
    method = _download_method(args)
    project = _project(reader, args)
    ids = args["public_ids"]
    lines = [
        f"Import public data into {project['name']} ({project['id']}).",
        "",
        f"  Dataset name  {args['name']}",
        f"  Accessions    {len(ids)}: {', '.join(map(str, ids[:10]))}"
        + (" ..." if len(ids) > 10 else ""),
        f"  Download via  {method}"
        + (" (the default)" if args.get("download_method") is None else ""),
        "",
        "Cirro will download these from the public repository. Volume is not known "
        "until the transfer starts.",
    ]
    return Draft(args={"project_id": project["id"], "name": args["name"],
                       "public_ids": ids, "description": args.get("description", ""),
                       "download_method": method},
                 summary="\n".join(lines),
                 invalidates=[_inv("datasets", project["id"])])


def apply_import_public(reader: reads.Reader, args: dict) -> dict:
    from cirro_api_client.v1.models import (ImportDataRequest,
                                            ImportDataRequestDownloadMethod)
    # The field is an enum and `to_dict` calls `.value` on it, so the planner's string
    # has to be turned back into a member or the request fails to serialize.
    request = ImportDataRequest(
        name=args["name"], public_ids=args["public_ids"],
        description=args.get("description") or "",
        download_method=ImportDataRequestDownloadMethod(args["download_method"]))
    response = reader.client.datasets.import_public(
        project_id=args["project_id"], import_request=request)
    return {"created_dataset_id": R.plain(getattr(response, "id", None))}


def _coarse_free(free: int) -> int:
    """Free space rounded down to two significant figures.

    apply re-plans and refuses PLAN_STALE when anything the person read has changed,
    warnings included (gated._same_disclosure), so a figure that tracks the disk
    exactly makes every download plan fragile for a reason unrelated to the download:
    human_bytes prints one decimal on a 1000-base scale, so ~50 MB of unrelated disk
    activity on a 500 GB volume moved it. Rounding down states less, but states
    something ordinary churn cannot change -- and understates rather than overstates
    the room available.
    """
    if free <= 0:
        return 0
    step = 10 ** max(int(math.log10(free)) - 1, 0)
    return free - free % step


def plan_download_files(reader: reads.Reader, args: dict) -> Draft:
    _need(args, "download_files", "dataset_id", "destination")
    project = _project(reader, args)
    row = _dataset(reader, project["id"], args["dataset_id"])
    entries, total, _artifacts = reads.dataset_manifest(
        reader, project["id"], row["id"],
        is_terminal=bool(row.get("is_terminal")))
    capped = total is not None and len(entries) < total
    glob = args.get("glob")
    if glob:
        import fnmatch
        entries = [e for e in entries if fnmatch.fnmatch(e["path"].lower(), glob.lower())]
    total_bytes = sum(e.get("size") or 0 for e in entries)

    destination = Path(args["destination"]).expanduser().resolve()
    probe = destination if destination.exists() else destination.parent
    try:
        free = _coarse_free(shutil.disk_usage(probe).free)
    except FileNotFoundError:
        raise errors.InvalidQuery(
            f"{destination} has no existing parent directory.",
            what="The destination must be somewhere that already exists, or one level "
                 "below it.", field="destination", given=str(destination),
            retry_with=[{"destination": str(Path.home() / "Downloads")}],
        )
    warnings = []
    if total_bytes > free * FREE_SPACE_HEADROOM:
        warnings.append(
            f"This needs {S.human_bytes(total_bytes)} but only {S.human_bytes(free)} is "
            "free at the destination.")
    if capped and not glob:
        # Without a glob apply downloads everything, so approving a capped figure would
        # understate the transfer.
        warnings.append(
            f"This dataset has {total} files but the manifest was read only to "
            f"{len(entries)}, so the counts above are a lower bound and the actual "
            "download will be larger.")

    patterns = S.name_patterns([e["path"] for e in entries], limit=5)
    lines = [
        f"Download {len(entries)} files ({S.human_bytes(total_bytes)}) from "
        f"{row['name']} ({row['id']}) to local disk.",
        "", f"  Destination  {destination}",
        f"  Filter       {glob or '(all files)'}",
        f"  Free space   at least {S.human_bytes(free)} there",
        "", "  Files:",
    ]
    lines += [f"    {p['template']}  x{p['n']}" for p in patterns]
    lines += ["", "This writes to the local filesystem, not to Cirro."]

    over_budget = _too_big_to_finish(total_bytes, "Download")
    if over_budget:
        warnings.append(over_budget)
    return Draft(args={"project_id": project["id"], "dataset_id": row["id"],
                       "destination": str(destination),
                       "files": [e["path"] for e in entries] if glob else None,
                       "n_files": len(entries), "total_bytes": total_bytes},
                 summary="\n".join(lines), warnings=warnings)


def apply_download_files(reader: reads.Reader, args: dict) -> dict:
    destination = Path(args["destination"])
    destination.mkdir(parents=True, exist_ok=True)
    reader.client.datasets.download_files(
        project_id=args["project_id"], dataset_id=args["dataset_id"],
        download_location=str(destination), files=args.get("files"))
    return {"downloaded_to": str(destination), "n_files": args.get("n_files"),
            "bytes": args.get("total_bytes")}


DATASET_OPS = [
    Op("update_dataset", RISK_WRITE, plan_update_dataset, apply_update_dataset,
       accepts=PROJECT_ARGS + FOLDER_ARGS + (
           "dataset_id", "dataset_ids", "name", "description", "tags", "process",
           "ingest_samples", "rerun_transform"), grantable=True),
    Op("update_samplesheet", RISK_WRITE, plan_update_samplesheet, apply_update_samplesheet,
       accepts=PROJECT_ARGS + (
           "dataset_id", "contents", "file_path", "ingest_samples")),
    Op("delete_dataset", RISK_DESTRUCTIVE, plan_delete_dataset, apply_delete_dataset,
       accepts=PROJECT_ARGS + ("dataset_id",)),
    Op("upload_dataset", RISK_WRITE, plan_upload_dataset, apply_upload_dataset,
       outputs=("dataset_id", "uploaded_to_project"),
       accepts=PROJECT_ARGS + FOLDER_ARGS + (
           "source_dir", "name", "process", "description", "tags", "include_hidden")),
    Op("upload_reference", RISK_WRITE, plan_upload_reference, apply_upload_reference,
       accepts=PROJECT_ARGS + ("name", "files", "reference_type")),
    Op("import_public", RISK_WRITE, plan_import_public, apply_import_public,
       outputs=("created_dataset_id",),
       accepts=PROJECT_ARGS + (
           "name", "public_ids", "description", "download_method")),
    Op("download_files", RISK_LOCAL, plan_download_files, apply_download_files,
       accepts=PROJECT_ARGS + ("dataset_id", "destination", "glob")),
]

mutations.OPS.update({op.name: op for op in DATASET_OPS})


def _derived_from(reader: reads.Reader, project_id: str, dataset_id: str) -> list:
    """Reverse-scan the project for datasets that list this one as a parent."""
    datasets, _ = reader.datasets(project_id)
    return [{"id": d["id"], "name": d["name"]}
            for d in datasets if dataset_id in (d.get("source_dataset_ids") or [])]
