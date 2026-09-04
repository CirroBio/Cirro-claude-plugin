"""What each entity has, what it costs, and how it degrades.

One registry drives four things that must not drift apart: the field projections
the envelope shrinks through, the facets computed over a match set, the cost tier
an `include` request is gated against, and the `describe` verb's output.

A field belongs here only if a row projection in `worker/rows.py` actually produces it,
or an `include` can fetch it. `describe` advertises everything listed, and a DETAIL-tier
entry also lands in `shape.unavailable` with `include:["<field>"]` as the way to get it --
so a declaration nothing can deliver is a documented promise the code breaks.

Tiers are about API calls, not about how interesting a field is:
  list      already in the paged listing payload -- free
  probe     one small call per row (get_dataset_manifest(file_limit=1))
  detail    one full call per row
  manifest  one or more calls per row, response potentially enormous
  execution like manifest, and only meaningful for some executors
  derived   computed here from list-tier fields -- free
"""
from dataclasses import dataclass, field
from typing import Optional

from . import errors

LIST = "list"
PROBE = "probe"
DETAIL = "detail"
MANIFEST = "manifest"
EXECUTION = "execution"
DERIVED = "derived"

# Ascending cost. `include` is gated on anything at DETAIL or worse.
TIER_ORDER = [LIST, DERIVED, PROBE, DETAIL, MANIFEST, EXECUTION]
GATED_TIERS = {DETAIL, MANIFEST, EXECUTION}


def tier_rank(tier: str) -> int:
    return TIER_ORDER.index(tier)


@dataclass
class EntitySpec:
    name: str
    id_prefix: str
    # field name -> (tier, one-line meaning). The meaning is what `describe` emits.
    fields: dict
    projections: dict
    facet_priority: list
    text_fields: list
    default_sort: list
    # Facets whose values are opaque IDs and must be joined to a human label.
    labeled_facets: dict = field(default_factory=dict)
    date_field: Optional[str] = "created_at"
    scoped_by_project: bool = True

    def tier_of(self, field_name: str) -> str:
        entry = self.fields.get(field_name)
        return entry[0] if entry else LIST

    def gated_fields(self) -> list:
        return [f for f, (tier, _) in self.fields.items() if tier in GATED_TIERS]


DATASET = EntitySpec(
    name="dataset",
    id_prefix="ds_",
    fields={
        "id": (LIST, "stable dataset ID; the only safe handle for a mutation"),
        "name": (LIST, "display name; NOT unique within a project"),
        "project_id": (LIST, "owning project"),
        "process_id": (LIST, "the process that produced it, or the ingest"
                             " type it was uploaded as"),
        "source_dataset_ids": (LIST, "immediate parents; the lineage edge"),
        "status": (LIST, "Status enum; see status classes @active/@terminal/@bad"),
        "folders": (LIST, "folder(s) this dataset is organized into; usually exactly one"),
        "folder": (DERIVED, "the single folder name when there is exactly one, else null"),
        "tags": (LIST, "free-form tags; folder membership is reported under folders"
                       " instead and never appears here"),
        "created_by": (LIST, "username"),
        "created_at": (LIST, "ISO-8601 UTC"),
        "updated_at": (LIST, "ISO-8601 UTC"),
        "description": (LIST, "free text"),
        "share": (LIST, "set when the dataset arrives via a subscribed share;"
                        " read-only to this project if so"),
        "file_count": (PROBE, "number of files; one small manifest call per dataset"),
        # Declared PROBE but nothing fetched it, so include:["artifact_types"] silently
        # returned nothing. `summarize files` is where it actually comes from.
        "artifact_types": (MANIFEST, "which workflow artifacts exist; read these with"
                                     " `summarize files`, not include"),
        "total_size_bytes": (DETAIL, "sum of file sizes; one detail call per dataset"),
        "params": (DETAIL, "the parameters the analysis ran with"),
        "info": (DETAIL, "process-specific metadata"),
        "status_message": (DETAIL, "failure reason when status is FAILED"),
        "source_sample_ids": (DETAIL, "samples this analysis consumed"),
        "s3": (DETAIL, "underlying S3 location"),
        "is_view_restricted": (DETAIL, "true when contents are hidden from this principal"),
        "files": (MANIFEST, "full file manifest; use `summarize files` instead"),
        "logs": (EXECUTION, "head-node workflow log; use `summarize logs` instead"),
        "tasks": (EXECUTION, "Nextflow task list; use `summarize tasks` instead"),
        "trace": (EXECUTION, "Nextflow resource trace; use `summarize trace` instead"),
        "total_size_human": (DERIVED, "total_size_bytes formatted"),
        "age_days": (DERIVED, "days since created_at"),
        "is_terminal": (DERIVED, "status is not going to change on its own"),
        "process": (DERIVED, "{id, name} pair joined from the process listing"),
    },
    projections={
        "standard": ["id", "name", "status", "created_at", "created_by", "process",
                     "folder", "tags", "source_dataset_ids", "share"],
        "compact": ["id", "name", "status", "created_at"],
    },
    facet_priority=["status", "created_at", "folders", "process_id", "created_by",
                    "tags", "share"],
    labeled_facets={"process_id": "process", "project_id": "project", "share": "share"},
    text_fields=["name", "description", "tags", "folders"],
    default_sort=["-created_at"],
)

PROJECT = EntitySpec(
    name="project",
    id_prefix="proj_",
    fields={
        "id": (LIST, "stable project ID"),
        "name": (LIST, "display name"),
        "description": (LIST, "free text"),
        "status": (LIST, "Status enum"),
        "tags": (LIST, "free-form tags"),
        "organization": (LIST, "owning organization"),
        "classification_ids": (LIST, "data classifications applied to the project"),
        "billing_account_id": (LIST, "billing account"),
        # created_by, created_at, contacts, settings and dataset_count were declared
        # DETAIL and produced by nothing: the project row comes from the listing, and
        # `include` fetches extras for dataset and process only. They reached `describe`
        # as real fields and `shape.unavailable` as `include:["contacts"]`, which does
        # nothing for a project. Declaring them again means producing them.
        "storage_bytes": (PROBE, "current storage from the metrics API; one call per "
                                 "project, and brings the per-tier split, the unit and "
                                 "the record's date with it"),
    },
    projections={
        "standard": ["id", "name", "status", "description", "organization", "tags"],
        "compact": ["id", "name", "status"],
    },
    facet_priority=["status", "organization", "tags"],
    text_fields=["name", "description", "tags"],
    default_sort=["name"],
    date_field=None,
    scoped_by_project=False,
)

PROCESS = EntitySpec(
    name="process",
    id_prefix="proc_",
    fields={
        "id": (LIST, "stable process ID"),
        "name": (LIST, "display name"),
        "description": (LIST, "free text"),
        "data_type": (LIST, "the data type this process consumes or produces"),
        "executor": (LIST, "INGEST | NEXTFLOW | CROMWELL | OMICS_READY2RUN;"
                           " INGEST means an upload type, not a pipeline"),
        "category": (LIST, "grouping shown in the UI"),
        "pipeline_type": (LIST, "pipeline family"),
        "is_archived": (LIST, "archived processes cannot be run"),
        "uses_sample_sheet": (LIST, "whether a samplesheet is required"),
        "documentation_url": (LIST, "external docs"),
        "child_process_ids": (LIST, "processes that can consume this one's output"),
        "parent_process_ids": (LIST, "processes whose output this one can consume"),
        "file_requirements_message": (LIST, "human description of required input files"),
        "is_tenant_wide": (LIST, "shared with the whole tenant"),
        "owner": (LIST, "username of the creator; blank means Cirro-curated"),
        "created_at": (LIST, "ISO-8601 UTC"),
        "updated_at": (LIST, "ISO-8601 UTC"),
        "config_source": (DETAIL, "where the run-form config comes from: STORAGE (uploaded"
                                  " into Cirro), WITH_PROCESS (the code repo), EXTERNAL"
                                  " (a separate config repo)"),
        "custom_settings": (DETAIL, "the git repo, branch and folder Cirro syncs config"
                                    " from, for the repo-backed config sources"),
        "pipeline_code": (DETAIL, "the workflow code: repository, version, entrypoint"),
        "allow_multiple_sources": (DETAIL, "whether a run may take more than one input"
                                           " dataset"),
        "file_mapping_rules": (DETAIL, "how dataset files map to the workflow's inputs"),
        "is_custom": (DERIVED, "has an owner, so it was registered by a user rather than"
                               " curated by Cirro"),
        "config_location": (DERIVED, "one-line summary of where this process's config"
                                     " lives, from config_source plus custom_settings"),
        "code_location": (DERIVED, "one-line summary of repository@version:entrypoint"),
    },
    projections={
        "standard": ["id", "name", "executor", "category", "pipeline_type", "owner",
                     "description"],
        "compact": ["id", "name", "executor", "owner"],
    },
    facet_priority=["executor", "category", "pipeline_type", "data_type", "owner",
                    "config_source"],
    text_fields=["name", "description", "category", "pipeline_type", "data_type"],
    default_sort=["name"],
    date_field=None,
    scoped_by_project=False,
)

SAMPLE = EntitySpec(
    name="sample",
    id_prefix="smp_",
    fields={
        "id": (LIST, "stable sample ID"),
        "name": (LIST, "sample name"),
        "metadata": (LIST, "free-form dict; keys vary by project"),
        "dataset_ids": (LIST, "datasets this sample appears in"),
        "created_at": (LIST, "ISO-8601 UTC"),
        "updated_at": (LIST, "ISO-8601 UTC"),
        # `files` was declared LIST and produced by nothing. A sample's files are read
        # through the dataset that holds them (`summarize files`), not from here.
    },
    projections={
        "standard": ["id", "name", "metadata", "dataset_ids"],
        "compact": ["id", "name"],
    },
    facet_priority=["created_at"],
    text_fields=["name"],
    default_sort=["name"],
)

REFERENCE = EntitySpec(
    name="reference",
    id_prefix="ref_",
    fields={
        "id": (LIST, "reference ID"),
        "name": (LIST, "reference name; references are addressed by name, not ID"),
        "type": (LIST, "reference type, e.g. genome_fasta"),
        "description": (LIST, "free text"),
        "created_by": (LIST, "username"),
        "created_at": (LIST, "ISO-8601 UTC"),
        # `absolute_path` was declared LIST and produced by nothing; the row carries the
        # file count instead, and no read fetches a reference's file paths.
        "n_files": (DERIVED, "how many files the reference holds; the file list itself"
                             " is not carried"),
    },
    projections={
        "standard": ["name", "type", "description", "created_at", "created_by"],
        "compact": ["name", "type"],
    },
    facet_priority=["type", "created_by", "created_at"],
    text_fields=["name", "description", "type"],
    default_sort=["name"],
)

FILE = EntitySpec(
    name="file",
    id_prefix="",
    fields={
        "path": (LIST, "path relative to the dataset root"),
        "size": (LIST, "bytes; can be absent -- the API returns it as Unset"),
        "metadata": (LIST, "per-file metadata, e.g. read number"),
        "size_human": (DERIVED, "size formatted"),
        "ext": (DERIVED, "lowercased extension, compound suffixes preserved (.fastq.gz)"),
    },
    projections={
        "standard": ["path", "size_human", "metadata"],
        "compact": ["path"],
    },
    facet_priority=["ext"],
    text_fields=["path"],
    default_sort=["path"],
    date_field=None,
)

TASK = EntitySpec(
    name="task",
    id_prefix="",
    fields={
        "task_id": (LIST, "0-based index into the execution's task list"),
        "name": (LIST, "Nextflow process name plus its per-sample tag"),
        "status": (LIST, "free-form string from the API, NOT the Status enum"),
        "native_job_id": (LIST, "underlying batch job ID"),
        "status_message": (LIST, "failure detail"),
        "exit_code": (LIST, "process exit status"),
        "container_image": (LIST, "image the task ran in"),
        "work_dir": (LIST, "S3 work directory"),
        "command_line": (LIST, "the command that ran"),
        "requested_at": (LIST, "ISO-8601 UTC"),
        "started_at": (LIST, "ISO-8601 UTC"),
        "stopped_at": (LIST, "ISO-8601 UTC"),
        "logs": (EXECUTION, "stdout/stderr for this task"),
        "script": (EXECUTION, ".command.sh"),
        "process": (DERIVED, "task name with the (tag) suffix stripped"),
        "duration_s": (DERIVED, "stopped_at - started_at"),
    },
    projections={
        "standard": ["task_id", "name", "status", "exit_code", "duration_s", "status_message"],
        "compact": ["task_id", "name", "status"],
    },
    facet_priority=["status", "process", "exit_code"],
    text_fields=["name", "status_message"],
    default_sort=["task_id"],
    date_field="started_at",
)

ENTITIES = {e.name: e for e in (DATASET, PROJECT, PROCESS, SAMPLE, REFERENCE, FILE, TASK)}


def get(entity: str) -> EntitySpec:
    if entity not in ENTITIES:
        raise errors.InvalidQuery(
            f"Unknown entity {entity!r}.",
            what="Pick one of the supported entities.",
            field="entity",
            given=entity,
            retry_with=[{"entity": name} for name in sorted(ENTITIES)],
        )
    return ENTITIES[entity]
