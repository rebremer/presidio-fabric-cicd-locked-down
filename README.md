# Presidio in a Fabric Environment — CI/CD with fabric-cicd

End-to-end CI/CD scaffold that deploys Microsoft **Presidio** (PII detection +
anonymization) into a Microsoft Fabric **Environment** item using
[fabric-cicd](https://microsoft.github.io/fabric-cicd/), and runs it from
**Azure DevOps** through a self-hosted agent that can reach a
**DEP-protected** Fabric workspace.

## Architecture

```
                        Laptop / dev box
                              │
                              │ git push
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Azure DevOps  ── project: test-presidio-cicd-privenv      │
   │   • Repos:    workspace/16_presidio/{Env, Notebook}        │
   │   • Pipeline: deploy-presidio-fabric (azure-pipelines.yml) │
   │   • Service connection (SPN, workload-identity)            │
   └─────────────────────────────┬──────────────────────────────┘
                                 │ job dispatched to pool 'Default'
                                 ▼
              ┌──────────────────────────────────────────┐
              │  Self-hosted agent (test-fabricjumphost-vm)
              │   • Python 3.11   • Azure CLI            │
              │   • fabric-cicd (pip)                    │
              │   • Outbound HTTPS to Fabric API only    │
              └─────────────────────────┬────────────────┘
                                        │ REST (SPN token)
                                        ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Fabric workspace  (DEP enabled, no pypi.org egress)       │
   │  Folder: 16_presidio                                       │
   │   ├── Presidio  (Environment)                              │
   │   │     ├── Sparkcompute.yml  → runtime 1.3 / Spark 3.5    │
   │   │     └── Libraries/                                     │
   │   │          ├── environment.yml  (public PyPI list)       │
   │   │          └── CustomLibraries/  ← pre-downloaded wheels │
   │   └── PresidioSmokeTest  (Notebook, attached to env above) │
   └────────────────────────────────────────────────────────────┘
```

## Repo layout

```
.
├── azure-pipelines.yml             # ADO pipeline definition
├── deploy.py                       # fabric-cicd entry point
├── requirements-deploy.txt         # fabric-cicd + azure-identity
├── scripts/
│   └── setup-agent.ps1             # one-shot bootstrap for the ADO agent VM
└── workspace/
    ├── parameter.yml               # fabric-cicd find_replace / spark_pool
    └── 16_presidio/
        ├── Presidio.Environment/
        │   ├── .platform
        │   ├── Setting/Sparkcompute.yml
        │   └── Libraries/
        │       ├── environment.yml          # public PyPI deps
        │       └── CustomLibraries/.gitkeep # drop wheels here for DEP
        └── PresidioSmokeTest.Notebook/
            ├── .platform
            └── notebook-content.py
```

## One-time setup

### 1. Azure DevOps project
Already created via `az devops project create`:
<https://dev.azure.com/renebremer/test-presidio-cicd-privenv>

### 2. Self-hosted agent
On the agent VM (e.g. `test-fabricjumphost-vm`), in **elevated PowerShell**:

```powershell
# Create a PAT with scope "Agent Pools (Read & manage)" at:
#   https://dev.azure.com/renebremer/_usersSettings/tokens
.\scripts\setup-agent.ps1 `
  -OrgUrl https://dev.azure.com/renebremer `
  -Pat   <pat>
```

If the VM has no internet, copy these to `C:\agent\` first and add
`-SkipDownloads`:

| File | Source |
|---|---|
| `python-installer.exe` | <https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe> |
| `az-cli.msi`           | <https://aka.ms/installazurecliwindowsx64> |
| `agent.zip`            | <https://vstsagentpackage.azureedge.net/agent/3.243.1/vsts-agent-win-x64-3.243.1.zip> |

After the script finishes, revoke the PAT — the agent has its own per-agent
OAuth token.

### 3. Service connection
Project Settings → **Service connections** → *Azure Resource Manager*
(workload-identity preferred). Any subscription in the right Entra tenant
works; the SPN is what matters. Grant that SPN **Member/Admin** on the
Fabric workspace, and ensure the tenant setting *"Service principals can use
Fabric APIs"* is enabled.

### 4. Pipeline variables
Pipelines → `deploy-presidio-fabric` → **Edit → Variables** (or a Library
variable group):

| Name | Example |
|---|---|
| `AZURE_SERVICE_CONNECTION` | `<service-connection-name>` |
| `FABRIC_WORKSPACE_ID`      | `601bdd87-8a8d-4501-a3b1-ae6b6b2d1b17` (testpub) |
| `FABRIC_ENVIRONMENT`       | `PPE` |

## Run it

`git push` to `main` → pipeline triggers automatically (or click **Run pipeline**).

The Environment publish is a long-running operation (~10-20 minutes);
fabric-cicd polls the staging endpoint until it completes.

## Move to the DEP private workspace

`pypi.org` is unreachable from a DEP workspace, so wheels must travel inside
the Environment item:

```bash
# On a machine WITH internet:
pip download \
  --dest workspace/16_presidio/Presidio.Environment/Libraries/CustomLibraries \
  --only-binary=:all: \
  --python-version 3.11 \
  --platform manylinux2014_x86_64 \
  presidio-analyzer==2.2.355 presidio-anonymizer==2.2.355 spacy==3.7.5

curl -L -o workspace/16_presidio/Presidio.Environment/Libraries/CustomLibraries/en_core_web_lg-3.7.1-py3-none-any.whl \
  https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.7.1/en_core_web_lg-3.7.1-py3-none-any.whl
```

Then **remove the `pip:` block from `Libraries/environment.yml`** so Fabric
doesn't try to reach pypi.org during publish, point `FABRIC_WORKSPACE_ID` at
the private workspace's GUID, and re-run the pipeline.

The agent only needs HTTPS egress to:
- `https://dev.azure.com/renebremer`
- `https://api.fabric.microsoft.com`
- `https://login.microsoftonline.com`

## Conflict mitigation

Presidio brings `spacy`, `pydantic`, `regex`, etc. which can clash with the
Fabric runtime. Mitigations:

- Pin exact versions (already done in `environment.yml`).
- Keep this `Presidio` environment dedicated; attach it only to notebooks
  that need PII detection — don't make it the workspace default.
- If a clash surfaces at runtime, vendor the conflicting package into
  `CustomLibraries/` at a compatible version.

## References

- fabric-cicd: <https://microsoft.github.io/fabric-cicd/>
- Presidio:    <https://microsoft.github.io/presidio/>
- Fabric Environment + custom libraries:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library>
