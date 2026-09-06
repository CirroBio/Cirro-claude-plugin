"""Test fixtures.

CIRRO_HOME is set here, at import time and before anything imports cirro, because
`cirro.config.Constants` freezes it in a class body. A fixture that set it later
would silently be testing against the developer's real ~/.cirro.
"""
import os
import sys
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="cirro-agent-tests-"))
os.environ["CIRRO_HOME"] = str(_SANDBOX / "session" / ".cirro")
os.environ.pop("CIRRO_CLIENT_ID", None)
os.environ.pop("CIRRO_CLIENT_SECRET", None)
os.environ.pop("CIRRO_BASE_URL", None)
assert "cirro" not in sys.modules, "conftest must run before cirro is imported"

import time  # noqa: E402

import pytest  # noqa: E402

BASE_URL = "app.cirro.test"
API = f"https://{BASE_URL}/api"


def mint_token(username: str = "sminot", ttl: int = 3600) -> str:
    """An unsigned JWT. The SDK decodes with verify_signature=False throughout."""
    import jwt
    return jwt.encode({"username": username, "exp": int(time.time()) + ttl,
                       "sub": "sub-sminot"}, key="", algorithm="none")


SYSTEM_INFO = {
    "auth": {"sdkAppId": "test-client-id", "userPoolId": "pool-1"},
    "referencesBucket": "refs", "resourcesBucket": "res", "region": "us-west-2",
}


def _dataset_payload(index, *, status="COMPLETED", process="proc_rnaseq",
                     created="2026-08-20T00:00:00Z", owner="sminot", parents=None,
                     dataset_id=None):
    """Built through the generated model so the fixture cannot drift from the API."""
    from cirro_api_client.v1.models import Dataset, Status, Tag
    return Dataset(
        id=dataset_id or f"ds_{index:04d}", name=f"RNAseq_batch{index % 20}_S{index}",
        project_id="proj_a", process_id=process,
        source_dataset_ids=parents or [], status=Status(status),
        # Varied per dataset: a fixture where every row carries the same tags cannot
        # tell "each dataset keeps its own tags" from "one row's tags were reused".
        tags=[Tag(value="folder://Immuno" if index % 2 == 0 else "folder://Archive"),
              Tag(value="qc-pass")] + ([] if index == 0
                                       else [Tag(value=f"batch-{index}")]),
        created_by=owner,
        created_at=_dt(created), updated_at=_dt(created),
        description=f"run {index}",
    ).to_dict()


def _dt(text):
    from datetime import datetime, timezone
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _project_payload(pid, name, description, organization="Cirro", tags=()):
    from cirro_api_client.v1.models import Project, Status, Tag
    return Project(
        id=pid, name=name, description=description, status=Status("COMPLETED"),
        tags=[Tag(value=t) for t in tags], organization=organization,
        classification_ids=[], billing_account_id="bill_1",
    ).to_dict()


def _user_payload(username="sminot", email="sminot@cirro.test", notifications=True):
    """The authenticated account's own record, which is where its email lives.

    A launch defaults its notification address to this, so a tenant that does not serve
    it puts every run_analysis plan on the lookup-failure path -- passing, but
    exercising the degraded branch rather than the one people will hit.
    """
    from cirro_api_client.v1.models import UserDetail, UserSettings
    return UserDetail(
        username=username, name="Sam Minot", email=email, organization="Cirro",
        project_assignments=[], global_roles=[],
        settings=UserSettings(analysis_update_notifications_enabled=notifications),
    ).to_dict()


def _process_payload(pid, name, executor, category, pipeline_type, description=""):
    from cirro_api_client.v1.models import Process, Executor
    return Process(
        id=pid, name=name, description=description, data_type="paired_dnaseq",
        executor=Executor(executor), child_process_ids=[], parent_process_ids=[],
        linked_project_ids=[], is_tenant_wide=True, allow_multiple_sources=True,
        uses_sample_sheet=True, is_archived=False, tags=[],
        category=category, pipeline_type=pipeline_type,
    ).to_dict()


def _detail_payload(base):
    from cirro_api_client.v1.models import (DatasetDetail, Status, Tag,
                                            DatasetDetailParams, DatasetDetailInfo,
                                            DatasetDetailSourceSampleFilesMap)
    return DatasetDetail(
        id=base["id"], name=base["name"], s3="s3://bucket/ds",
        process_id=base["processId"], project_id="proj_a",
        source_dataset_ids=base["sourceDatasetIds"], source_datasets=[],
        source_sample_ids=[],
        source_sample_files_map=DatasetDetailSourceSampleFilesMap.from_dict({}),
        status=Status(base["status"]), status_message="Task FASTQC exited 137",
        tags=[Tag(value="qc-pass")],
        params=DatasetDetailParams.from_dict(
            {"aligner": "star", "genome": "GRCh38"}),
        info=DatasetDetailInfo.from_dict({}),
        is_view_restricted=False, created_by=base["createdBy"],
        created_at=_dt(base["createdAt"]), updated_at=_dt(base["updatedAt"]),
        description=base.get("description") or "",
        total_size_bytes=41203118, file_count=482,
    ).to_dict()


def _manifest_payload():
    from cirro_api_client.v1.models import (DatasetAssetsManifest, FileEntry, Artifact,
                                            ArtifactType)
    files = [FileEntry(path=f"data/SAMP{i:03d}_R{r}_001.fastq.gz",
                       size=3_000_000_000 + i)
             for i in range(240) for r in (1, 2)]
    files.append(FileEntry(path="multiqc/multiqc_report.html", size=4_200_000))
    files.append(FileEntry(path="star/Log.final.out"))
    return DatasetAssetsManifest(
        domain="d", total_files=482, files=files,
        artifacts=[Artifact(type_=ArtifactType.WORKFLOW_LOGS, path="logs.txt")],
        viz=[], tables=[],
    ).to_dict()


def _cost_payload():
    """One run's cost through the generated models.

    Shaped like a real fan-out: a handful of ALIGN tasks carrying most of the money,
    a long tail of QC tasks, and one failure -- so the rollup, the priciest-task list
    and the wasted-spend figure each have something to be wrong about.
    """
    from cirro_api_client.v1.models import CostResponse, GroupCost, TaskCost

    tasks = [TaskCost(name=f"ALIGN (sample_{i})", task_id=f"t_align_{i}",
                      status="COMPLETED", cost=1.25 + i * 0.1) for i in range(4)]
    tasks += [TaskCost(name=f"FASTQC (sample_{i})", task_id=f"t_qc_{i}",
                       status="COMPLETED", cost=0.02) for i in range(20)]
    tasks.append(TaskCost(name="MULTIQC", task_id="t_multiqc", status="FAILED",
                          cost=0.75))
    tasks.append(TaskCost(name="TRIM (sample_0)", task_id="t_trim_0", status="CACHED",
                          cost=0.0))
    return CostResponse(
        total_cost=6.55, is_estimate=True,
        groups=[GroupCost(name="COMPLETED", cost=5.80),
                GroupCost(name="FAILED", cost=0.75),
                GroupCost(name="CACHED", cost=0.0)],
        tasks=tasks,
    ).to_dict()


def _metrics_payload(project_id, *, storage_unit="GB"):
    """Both metrics series, through the generated models.

    The shape is the point: a record carries `unit`, `date` and a `services` map, and
    has no scalar value. Reading a `value` attribute made project storage report null
    for every project, and no fixture existed to catch it.
    """
    from cirro_api_client.v1.models import ProjectMetrics, MetricRecord
    from cirro_api_client.v1.models.metric_record_services import MetricRecordServices

    def record(day, unit, services):
        from datetime import date
        return MetricRecord(
            unit=unit, date=date.fromisoformat(day),
            services=MetricRecordServices.from_dict(services))

    return ProjectMetrics(
        project_id=project_id,
        costs=[
            record("2026-07-01", "$", {"Amazon Elastic Compute Cloud - Compute": 140.59,
                                       "Amazon Simple Storage Service": 24.91,
                                       "EC2 - Other": 3.66}),
            record("2026-08-01", "$", {"Amazon Elastic Compute Cloud - Compute": 210.10,
                                       "Amazon Simple Storage Service": 25.40}),
        ],
        storage_metrics=[
            record("2026-08-30", storage_unit, {"StandardStorage": 1.0}),
            # The later record must win, and its tiers must be summed rather than
            # one tier being reported as the whole figure.
            record("2026-08-31", storage_unit, {"StandardStorage": 1.5,
                                                "IntelligentTieringIAStorage": 2.5}),
        ],
    ).to_dict()


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    from cirro_agent import session as session_mod
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("CIRRO_HOME", str(work / ".cirro"))
    current = session_mod.init(work, BASE_URL)
    return current


@pytest.fixture
def fake_tenant(monkeypatch):
    """A Cirro tenant on the wire: requests for system info, httpx for the API."""
    import respx
    import responses as responses_lib

    monkeypatch.setenv("CIRRO_ACCESS_TOKEN", mint_token())
    # The DNS/TCP probe is real by design; a fake hostname does not resolve, so stub
    # the probe rather than putting a skip-the-probe switch in production code.
    from cirro_agent.worker import portal
    monkeypatch.setattr(portal, "probe_network", lambda base_url: None)

    n_datasets = 1200
    datasets = []
    for index in range(n_datasets):
        status = "COMPLETED"
        if index % 200 == 0:
            status = "FAILED"
        elif index % 377 == 0:
            status = "RUNNING"
        datasets.append(_dataset_payload(
            index, status=status,
            process="proc_rnaseq" if index % 2 else "proc_sarek",
            created=f"2026-0{(index % 8) + 1}-1{index % 10}T00:00:00Z",
            owner="sminot" if index % 3 else "jlee",
            parents=[f"ds_{index - 1:04d}"] if index else []))

    projects = [
        _project_payload("proj_a", "Immuno-Oncology Core", "main", tags=("oncology",)),
        _project_payload("proj_b", "Sandbox", "scratch"),
    ]
    processes = [
        _process_payload("proc_rnaseq", "nf-core/rnaseq v3.14", "NEXTFLOW",
                         "Transcriptomics", "nf-core", "RNA-seq"),
        _process_payload("proc_sarek", "nf-core/sarek v3.4", "NEXTFLOW",
                         "Genomics", "nf-core", "WGS"),
        _process_payload("proc_ingest", "Paired DNA sequencing", "INGEST",
                         "Upload", "ingest", "upload"),
    ]

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses_lib.GET, f"{API}/info/system", json=SYSTEM_INFO, status=200)
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/projects").respond(json=projects)
            router.get(f"{API}/processes").respond(json=processes)
            router.get(f"{API}/projects/proj_a/datasets").respond(
                json={"data": datasets, "nextToken": None})
            router.get(f"{API}/projects/proj_b/datasets").respond(
                json={"data": [], "nextToken": None})
            router.get(url__regex=rf"{API}/projects/[^/]+/shares$").respond(json=[])
            router.get(f"{API}/projects/proj_a/metrics").respond(
                json=_metrics_payload("proj_a"))
            router.get(f"{API}/projects/proj_b/metrics").respond(
                json=_metrics_payload("proj_b"))
            # proj_gone is deliberately absent from /projects: observed live, the
            # metrics endpoint reports spend for projects the listing does not carry.
            router.get(f"{API}/metrics").respond(
                json=[_metrics_payload("proj_a"), _metrics_payload("proj_b"),
                      _metrics_payload("proj_gone")])
            router.get(url__regex=rf"{API}/projects/proj_a/datasets/ds_\d+$"
                       ).respond(json=_detail_payload(datasets[0]))
            router.get(url__regex=rf"{API}/projects/[^/]+/datasets/[^/]+/files"
                       ).respond(json=_manifest_payload())
            router.get(url__regex=rf"{API}/projects/[^/]+/execution/[^/]+/cost$"
                       ).respond(json=_cost_payload())
            # Registered under the exact path a test's own serve_user() uses, so
            # re-registering replaces this default rather than losing to a wider regex.
            router.get(f"{API}/users/sminot").respond(json=_user_payload())
            yield {"base_url": BASE_URL, "n_datasets": n_datasets,
                   "datasets": datasets, "projects": projects,
                   "processes": processes, "router": router}


# The dataset a launched analysis creates, and which then joins the project listing.
LAUNCHED_DATASET = "ds_new_0001"

PARAMETER_SPEC = {
    "form": {"type": "object",
             "properties": {"aligner": {"type": "string", "default": "star"},
                            "genome": {"type": "string"}},
             "required": ["genome"]},
    "ui": {},
}


@pytest.fixture
def writable_tenant(fake_tenant):
    """Add the mutation endpoints to the active router of the read-only tenant.

    A launched analysis joins the project listing, as it does on a real tenant. A
    fixture whose listing never grew could not tell a chain that re-reads after each
    step from one that answers step 2 out of a snapshot taken before step 1 ran.
    """
    import httpx
    router = fake_tenant["router"]
    datasets = fake_tenant["datasets"]

    def launch(request):
        datasets.append(_dataset_payload(9001, status="PENDING",
                                         dataset_id=LAUNCHED_DATASET))
        return httpx.Response(200, json={"id": LAUNCHED_DATASET,
                                         "message": "submitted"})

    def listing(request):
        return httpx.Response(200, json={"data": datasets, "nextToken": None})

    router.post(f"{API}/projects/proj_a/execution").mock(side_effect=launch)
    router.get(f"{API}/projects/proj_a/datasets").mock(side_effect=listing)
    # The client parses this as a full DatasetDetail, so a partial body fails.
    router.put(url__regex=rf"{API}/projects/proj_a/datasets/ds_\w+$").respond(
        json=_detail_payload(fake_tenant["datasets"][0]))
    router.delete(url__regex=rf"{API}/projects/proj_a/datasets/ds_\w+$").respond(
        status_code=204)
    router.get(f"{API}/processes/proc_rnaseq/parameters").respond(json=PARAMETER_SPEC)
    return fake_tenant


def approved_summary(session_dir, plan_id):
    """The text apply must be given, read where the approval hook reads it.

    The plan response deliberately no longer echoes the summary beside `rendered`,
    so tests take it from the stored plan exactly as scripts/gate.py does.
    """
    from cirro_agent import plans
    return plans.load(session_dir, plan_id).summary


def fake_reader(*, processes=(), username=None, base_url="app.cirro.test", session_dir=None):
    """A MagicMock standing in for reads.Reader, with the fields planners touch.

    Fourteen tests built this by hand; the shape lives here so it cannot drift.
    """
    from unittest.mock import MagicMock
    reader = MagicMock()
    reader.processes.return_value = list(processes)
    reader.username = username
    reader.base_url = base_url
    if session_dir is not None:
        reader.session_dir = str(session_dir)
    return reader


@pytest.fixture(autouse=True)
def _no_project_dir_from_the_outer_session(monkeypatch):
    """Tests decide their own session folder; the host's must not leak in."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
