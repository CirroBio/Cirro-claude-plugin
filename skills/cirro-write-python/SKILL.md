---
name: cirro-write-python
description: Write a standalone Python script that uses the Cirro SDK, for the user to review and run themselves. Use when the user asks for a script, a notebook cell, reusable or automated code against Cirro, or something to run on a schedule or in CI — rather than an action performed now.
---

# Write a Cirro SDK script

Use this when the user wants **code they own**, not an action taken on their behalf:
something to commit, schedule, run in CI, or adapt.

You author the script. **You do not run it.** Executing SDK code directly would go
around the approval gate that every create, update and delete in this plugin passes
through, and the plugin's hook blocks it. Writing a `.py` file that imports `cirro`
prompts the user, which is correct — they are choosing to take on running it.

If the user actually wants the thing done now, use the operational skills instead:
**cirro-explore**, **cirro-run-analysis**, **cirro-upload-dataset**,
**cirro-monitor-analysis**.

## Authentication in a script

Three modes. Pick by where the script will run.

**Automation — no person present.** OAuth client credentials never prompt. This is the
only correct choice for CI or a scheduled job:

```python
import os
from cirro import CirroApi, DataPortal
from cirro.auth.client_creds import ClientCredentialsAuth
from cirro.config import AppConfig

portal = DataPortal.from_client_credentials(
  client_id=os.environ['CIRRO_CLIENT_ID'],
  client_secret=os.environ['CIRRO_CLIENT_SECRET'],
  base_url="app.cirro.bio"
)
```

**Interactive, with control over when it blocks.** Renders the link yourself:

```python
from cirro.sdk.login import DataPortalLogin

login = DataPortalLogin(base_url="app.cirro.bio", enable_cache=True)
print(login.auth_message)          # the user opens this
portal = login.await_completion()  # blocks until they do
```

**Never write bare `DataPortal()` in a script meant to run unattended.** With no cached
credential it starts a device-code login and blocks until the code expires — an
unbounded hang with nobody to click the link. Say so in a comment if you use it.

`enable_cache=True` is required for the token to persist at all; it defaults to
`False`.

## Where credentials land

The SDK reads `CIRRO_HOME` (default `~/.cirro`) for both `config.ini` and the token
cache. It is captured **at import time**, so if the script needs a project-local
credential:

```python
import os
os.environ["CIRRO_HOME"] = os.path.abspath(".cirro")   # BEFORE importing cirro
from cirro import DataPortal
```

Setting it after the import silently has no effect on `config.ini`.

## The object model

```
DataPortal
├── list_projects()        -> DataPortalProject
│   ├── list_datasets()    -> DataPortalDataset
│   │   ├── list_files()   -> DataPortalFile
│   │   └── tasks          -> DataPortalTask
│   └── list_references()  -> DataPortalReference
├── list_processes()       -> DataPortalProcess
└── list_reference_types() -> DataPortalReferenceType
```

Projects, datasets and processes accept either a name or an ID. Every `list_*` returns
a list subclass with `get_by_name`, `get_by_id` and `filter_by_pattern`.

## Reading files without downloading

```python
df = portal.read_file("Project Name", "Dataset Name", glob="*.csv")   # DataFrame
text = dataset.get_file("multiqc/report.html").read()
adata = dataset.read_file(glob="*.h5ad", filetype="h5ad")             # needs anndata
```

## Launching an analysis

```python
dataset = portal.get_dataset(project="Project Name", dataset="Dataset Name")
new_id = dataset.run_analysis(
    name="Output name",
    process="Process name or ID",
    params={...},
)
```

Get the accepted keys from the spec rather than from memory:

```python
process = portal.get_process_by_name("nf-core/rnaseq v3.14")
process.get_parameter_spec().print()
```

## Two behaviours that cause most script bugs

**Objects are snapshots.** `dataset.status` will not change on an object you already
hold, and `logs` and `tasks` are cached on it. To watch a run, re-fetch each time:

```python
import time
while True:
    current = portal.get_dataset(project=project_id, dataset=new_id)   # re-fetch
    if current.status in ("COMPLETED", "FAILED"):
        break
    time.sleep(60)
```

**Listing is client-side.** There is no server-side filtering: `list_datasets()`
fetches everything and you filter in Python. For a large project, fetch once and reuse
the list rather than calling it in a loop.

## Writing the script

- Put it where the user asked, or alongside the work it belongs to.
- Read credentials from the environment. Never write a token, client secret or password
  into the file.
- Explain in your reply what it will do, what it needs set (`CIRRO_CLIENT_ID`,
  `CIRRO_CLIENT_SECRET`, `CIRRO_BASE_URL`), and how to run it.
- If it performs writes, say so plainly — once the user runs it themselves, this
  plugin's approval gate is not involved.
