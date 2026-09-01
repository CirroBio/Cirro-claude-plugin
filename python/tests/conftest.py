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
                     created="2026-08-20T00:00:00Z", owner="sminot", parents=None):
    """Built through the generated model so the fixture cannot drift from the API."""
    from cirro_api_client.v1.models import Dataset, Status, Tag
    return Dataset(
        id=f"ds_{index:04d}", name=f"RNAseq_batch{index % 20}_S{index}",
        project_id="proj_a", process_id=process,
        source_dataset_ids=parents or [], status=Status(status),
        tags=[Tag(value="folder://Immuno"), Tag(value="qc-pass")],
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
            router.get(url__regex=rf"{API}/projects/proj_a/datasets/ds_\d+$"
                       ).respond(json=_detail_payload(datasets[0]))
            router.get(url__regex=rf"{API}/projects/[^/]+/datasets/[^/]+/files"
                       ).respond(json=_manifest_payload())
            yield {"base_url": BASE_URL, "n_datasets": n_datasets,
                   "datasets": datasets, "projects": projects,
                   "processes": processes, "router": router}


PARAMETER_SPEC = {
    "form": {"type": "object",
             "properties": {"aligner": {"type": "string", "default": "star"},
                            "genome": {"type": "string"}},
             "required": ["genome"]},
    "ui": {},
}


@pytest.fixture
def writable_tenant(fake_tenant):
    """Add the mutation endpoints to the active router of the read-only tenant."""
    router = fake_tenant["router"]
    router.post(f"{API}/projects/proj_a/execution").respond(
        json={"id": "ds_new_0001", "message": "submitted"})
    # The client parses this as a full DatasetDetail, so a partial body fails.
    router.put(url__regex=rf"{API}/projects/proj_a/datasets/ds_\d+$").respond(
        json=_detail_payload(fake_tenant["datasets"][0]))
    router.delete(url__regex=rf"{API}/projects/proj_a/datasets/ds_\d+$").respond(
        status_code=204)
    router.get(f"{API}/processes/proc_rnaseq/parameters").respond(json=PARAMETER_SPEC)
    return fake_tenant
