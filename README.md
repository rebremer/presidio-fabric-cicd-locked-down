# Presidio in a locked-down Fabric workspace — CI/CD with fabric-cicd

End-to-end CI/CD that deploys Microsoft **Presidio** (PII detection +
anonymization) into a Microsoft Fabric **Environment** item inside a
workspace locked down with both:

- **WSPL** — Workspace-Level Private Link: no public inbound, the workspace
  is reachable only over its private endpoint FQDN.
- **DEP** — Data Exfiltration Protection: no public outbound, so pypi.org,
  conda-forge, publicsuffix.org, etc. are unreachable from Spark.

Built on [fabric-cicd](https://microsoft.github.io/fabric-cicd/), driven
from **Azure DevOps** through a self-hosted Linux agent inside the same
VNet, authenticated to Fabric via a **User-Assigned Managed Identity** and
**Workload Identity Federation** (no client secrets stored anywhere).

## Quick start

1. Provision the agent VM ([infra/agent-vm.bicep](infra/agent-vm.bicep)).
2. Register it as an Azure DevOps self-hosted agent
   ([scripts/setup-agent.sh](scripts/setup-agent.sh)).
3. Create an ADO service connection (UAMI + Workload Identity Federation),
   grant the UAMI **Contributor** on the Fabric workspace.
4. Set the four pipeline variables (see [Pipeline variables](#4-pipeline-variables)).
5. `git push` to `main` → pipeline publishes the `PresidioPriv` Environment
   and binds `PresidioSmokeTest` to it.

For fast iteration after the env is already published, run the pipeline
with `skipEnvPublish=true` to skip the ~10-minute env publish and only
republish + re-bind the notebook.

## Architecture

```
                              git push
   Dev box ────────────────────────────────────────────────►  ADO Repos
                                                                  │
                                                                  ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  Azure DevOps pipeline (deploy-presidio-fabric)                    │
   │   - service connection: UAMI via Workload Identity Federation      │
   │   - dispatches to pool 'Default' (Linux demand)                    │
   └─────────────────────────────┬──────────────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────────┐
              │  Self-hosted Ubuntu agent VM (in VNet    │
              │  linked to the workspace's PE DNS zone)  │
              │   - pip download (cp311/manylinux)       │
              │   - fabric-cicd                          │
              │   - az cli (federated token)             │
              └─────────────────────────┬────────────────┘
                                        │ HTTPS via private FQDN
                                        ▼
              ┌──────────────────────────────────────────┐
              │  Workspace Private Endpoint              │
              │  fc5...zfc.w.api.fabric.microsoft.com    │
              └─────────────────────────┬────────────────┘
                                        │
                                        ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  Fabric workspace (WSPL inbound + DEP outbound)                    │
   │   16_presidio/                                                     │
   │     ├── PresidioPriv.Environment                                   │
   │     │     ├── Setting/Sparkcompute.yml    (runtime 1.3 / Spark 3.5)│
   │     │     └── Libraries/CustomLibraries/  (53 wheels)              │
   │     └── PresidioSmokeTest.Notebook        (bound to env above)     │
   └────────────────────────────────────────────────────────────────────┘
```

## Repo layout

```
.
├── azure-pipelines.yml             # ADO pipeline (Linux demand, manylinux pip download)
├── deploy.py                       # fabric-cicd entry point + monkey-patch
├── requirements-deploy.txt         # fabric-cicd + azure-identity (agent-side)
├── requirements-presidio.txt       # Presidio + spaCy model
├── infra/
│   └── agent-vm.bicep              # Bicep for the Ubuntu ADO agent VM
├── scripts/
│   ├── setup-agent.sh              # Bootstrap the Ubuntu agent VM
│   └── setup-agent.ps1             # (legacy) Windows agent bootstrap
├── workspace/
    ├── parameter.yml               # fabric-cicd find_replace / spark_pool
    └── 16_presidio/
        ├── PresidioPriv.Environment/
        │   ├── .platform
        │   ├── Setting/Sparkcompute.yml
        │   └── Libraries/
        │       ├── environment.yml          # placeholder (cleared from staging)
        │       └── CustomLibraries/         # populated by pipeline
        └── PresidioSmokeTest.Notebook/
            ├── .platform
            └── notebook-content.py
```

## One-time setup

### 1. Provision the agent VM

```powershell
az deployment group create `
  --resource-group <agent-rg> `
  --template-file infra/agent-vm.bicep `
  --parameters adminUsername='<user>' `
  --parameters adminPassword='<password>' `
  --parameters vnetResourceGroup='<vnet-rg>'
```

The VM lands on a subnet in the same VNet as the workspace's PE DNS zone
(`privatelink.fabric.microsoft.com`), so the WSPL FQDN resolves to the
private IP. Defaults: `Standard_B4ms`, Ubuntu 22.04.

### 2. Register the agent

```bash
# On the VM (sudoer):
git clone https://dev.azure.com/<org>/<project>/_git/<repo>
cd <repo>
ORG_URL=https://dev.azure.com/<org> PAT=<pat-with-Agent-Pools-rw> ./scripts/setup-agent.sh
```

Installs build deps, Azure CLI, and the ADO agent (v3.243.1) as a systemd
service in pool `Default`. Revoke the PAT once the agent shows online.

### 3. Service connection (UAMI + WIF, no secrets)

WSPL inbound policies validate the caller via the `xms_mirid` claim, which
only managed identities carry. Plain SPN tokens are rejected.

1. Create a UAMI in any RG of the right tenant.
2. Add it as **Contributor** on the Fabric workspace.
3. ADO → Project Settings → **Service connections** → *Azure Resource
   Manager* → **Workload Identity federation (manual)**. Bind the
   federated credential to the UAMI.

### 4. Pipeline variables

Pipelines → `deploy-presidio-fabric` → **Edit → Variables**:

| Name | Value |
|---|---|
| `AZURE_SERVICE_CONNECTION` | `<service-connection-name>` |
| `FABRIC_WORKSPACE_ID`      | `<<your workspace ID>>` |
| `FABRIC_BASE_API_URL`      | `https://<wsid-no-dashes>.zfc.w.api.fabric.microsoft.com` |
| `FABRIC_ENVIRONMENT`       | `<<your Fabric environment name, e.g. presidioPriv>>` |

### 5. Discover the workspace's private FQDN

```powershell
$peId = (az network private-endpoint show -g <pe-rg> -n <pe-name> --query id -o tsv)
$nic  = (az network private-endpoint show --ids $peId --query "networkInterfaces[0].id" -o tsv)
az resource show --ids $nic `
  --query "properties.ipConfigurations[].{name:name, fqdns:properties.privateLinkConnectionProperties.fqdns, ip:properties.privateIPAddress}" -o json
```

Use the `…zfc.w.api.fabric.microsoft.com` record for `FABRIC_BASE_API_URL`.

## Run it

`git push` to `main` triggers the pipeline. The Environment publish is
a long-running operation (~5–15 min for the full Presidio closure of
53 wheels); fabric-cicd polls until completion. The notebook re-bind
is a fast metadata-only call (~5 s) that runs after the env publish
settles.

Verify staging after a run from inside the VNet:

```bash
az rest --method get --resource https://api.fabric.microsoft.com \
  --url "https://<wsid>.zfc.w.api.fabric.microsoft.com/v1/workspaces/<wsid>/environments/<eid>/staging/libraries"
# Expect:
# { "customLibraries": { "wheelFiles": [...] }, "environmentYml": "" }
```

## Conflict mitigation

Presidio brings `pydantic`, `spacy`, `regex`, etc. that may overlap with
the Fabric runtime. Recommendations:

- Pin exact versions in `requirements-presidio.txt`.
- Keep this environment dedicated; attach it only to notebooks that need
  PII detection — don't make it the workspace default.
- If a clash surfaces at runtime, pin the conflicting package down to
  whatever the Fabric runtime ships and re-publish.

## Runtime caveats in a fully locked-down workspace

### `tldextract` / `publicsuffix.org`

Presidio's `UrlRecognizer` uses `tldextract`, which on first call refreshes
the public-suffix list from `https://publicsuffix.org/list/public_suffix_list.dat`.
DEP outbound blocks that and `tldextract` raises `ConnectionError` instead
of falling back to its bundled snapshot. The smoke notebook patches
`tldextract.suffix_list.find_first_response` to raise `SuffixListNotFound`,
which triggers the snapshot path.

If you want the live suffix list instead, **whitelist
`publicsuffix.org` in the workspace's outbound access policy**. We don't
do that here on purpose — the goal is a fully locked-down environment.

### spaCy model: `en_core_web_sm` vs `en_core_web_lg`

The pipeline ships the **small** model (`en_core_web_sm`, ~13 MB) as a
wheel into `CustomLibraries/`. The large model (`en_core_web_lg`, ~560 MB)
exceeds Fabric's `updateDefinition` payload limit and cannot go through
fabric-cicd as a wheel.

If you need `_lg` (better PERSON/ORG accuracy), the workaround is:

1. Upload the `.whl` (or extracted model dir) to a **OneLake** path in the
   workspace, e.g. `Files/models/en_core_web_lg-3.7.1-py3-none-any.whl`.
2. In your notebook, copy it to local disk and `pip install` it at
   runtime, then point the `NlpEngineProvider` at `en_core_web_lg`.

OneLake traffic stays inside the WSPL boundary, so this works without
opening any outbound exceptions.

## How this compares to the MS docs pattern

[Outbound access protection for Fabric Environments](https://learn.microsoft.com/fabric/data-engineering/environment-manage-library-with-outbound-access-protection)
recommends a private Azure Storage account configured as a pip mirror.
Functionally equivalent to what we do here — wheels live in
workspace-private storage either way. Differences:

|                              | MS storage-mirror | This repo                |
|------------------------------|-------------------|--------------------------|
| Extra Azure infra            | Storage + PE      | None (uses CustomLibraries) |
| Pip resolves at publish time | Yes, via mirror   | Skipped (env.yml cleared)|
| Detects missing transitives  | At publish        | Only at notebook runtime |
| Works with ADO + UAMI/WIF    | Yes               | Yes                      |

## Enhancements layered on top of fabric-cicd

To make fabric-cicd, Azure DevOps and Presidio work together inside a
WSPL + DEP locked-down Fabric workspace, `deploy.py` adds three thin shims
around vanilla fabric-cicd. Everything else stays stock.

### A. Pin the API base URL to the workspace's private FQDN

fabric-cicd 1.0 defaults `fabric_constants.DEFAULT_API_ROOT_URL` to
`https://api.powerbi.com` (a Power BI heritage leftover that
transparently proxies Fabric APIs on unrestricted tenants). That host
isn't on the WSPL allow-list, so every call fails at the network layer
before fabric-cicd hits any Fabric code path.

`deploy.py` overrides the constant **before** constructing
`FabricWorkspace` (it's read once, at `__init__` time):

```python
fabric_constants.DEFAULT_API_ROOT_URL = os.environ.get(
    "FABRIC_BASE_API_URL", "https://api.fabric.microsoft.com",
)
```

`FABRIC_BASE_API_URL` is set to the workspace's private FQDN
(`https://<wsid>.zfc.w.api.fabric.microsoft.com`) so traffic rides the
private endpoint end-to-end. fabric-cicd doesn't expose `base_api_url`
as a constructor argument, so patching the module constant is the only
seam without forking.

### B. Clear `environment.yml` from staging before publish

In a WSPL/DEP workspace the Fabric publish backend cannot reach pypi.org
or conda-forge, so any `environment.yml` containing public dependencies
(including the default `python=3.11` / `pip` block fabric-cicd adds)
causes the publish to settle as `sparkLibraries.state: "Failed"` with no
error surfaced through the API.

Workspace storage (where `CustomLibraries/` lives) IS reachable via WSPL,
so wheels uploaded there install fine — provided Fabric does not try to
resolve the public deps section at all.

`deploy.py` monkey-patches `fabric_cicd._items._environment` to call:

```
DELETE /v1/workspaces/{wsid}/environments/{eid}/staging/libraries?libraryToDelete=environment.yml
```

right after fabric-cicd uploads the definition and before it triggers
publish. Net effect: staging ends up with `environmentYml: ""` plus the
wheels — the same state a manually-created working env shows.

### C. Bind the notebook to its environment via the `ipynb` format

The notebook source under `workspace/.../PresidioSmokeTest.Notebook/`
uses Fabric's source-control format (`notebook-content.py` with `# META`
headers including a `dependencies.environment` block pointing at the
`PresidioPriv` environment). fabric-cicd publishes that file through
`notebooks/{id}/updateDefinition` in `fabricGitSource` format.

On `fabricGitSource` payloads, the saved definition does not retain the
`dependencies.environment` block: the POST returns 200 OK, reading the
notebook back shows no binding, the portal shows "Workspace default".

After publish, `deploy.py` does an `ipynb`-format round-trip on the same
endpoint (the same path `notebookutils.notebook.updateDefinition` uses
internally to update env / default-lakehouse bindings):

1. `POST .../notebooks/{id}/getDefinition?format=ipynb` to fetch the
   notebook as `.ipynb` JSON.
2. Set `metadata.dependencies.environment = { environmentId, workspaceId }`
   to the resolved GUIDs.
3. `POST .../notebooks/{id}/updateDefinition` with `"format": "ipynb"`
   inside the body (do NOT use `?format=ipynb` on the URL — on
   `updateDefinition` that means "convert .py to .ipynb" and is rejected).
4. Re-fetch and assert that `metadata.dependencies.environment` now
   contains the expected `environmentId`. If not, fail the deploy.

The env GUID is resolved at runtime by `displayName` lookup against
`/v1/workspaces/{wsid}/environments`, so the source notebook can ship
with any placeholder GUID; the deploy rewrites it to whatever the target
workspace's `PresidioPriv` actually has. `deploy.py` also blocks until
`publishDetails.state == Success` on the target environment before
attempting the bind — Fabric rejects the binding while the env publish
is `Running`.

## References

- fabric-cicd: <https://microsoft.github.io/fabric-cicd/>
- Presidio:    <https://microsoft.github.io/presidio/>
- Fabric Environment + custom libraries:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library>
- Outbound access protection:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library-with-outbound-access-protection>
- How to How to Anonymize and Share PII in Microsoft Fabric:
  <https://medium.com/data-science-collective/how-to-anonymize-and-share-pii-in-microsoft-fabric-670eaf9cd2be>

## Appendix: managing the workspace communication policy from CI

The Presidio workload pipeline assumes the workspace's communication
policy (inbound + outbound `Deny` plus any explicit allow rules) is
already applied — it's treated as standing platform/governance infra,
the same way WSPL + DEP enablement is. Configuring the policy is out of
scope for the workload pipeline.

If you also want to manage that policy from code (different lifecycle:
rare changes, security ownership, workspace-wide blast radius), this
repo ships an **optional second pipeline** under
[communication-policy/](communication-policy/):

| File | Purpose |
|---|---|
| [communication-policy/azure-pipelines.yml](communication-policy/azure-pipelines.yml) | ADO pipeline, triggers only on `communication-policy/**`. |
| [communication-policy/policy.json](communication-policy/policy.json) | Policy document (`inbound`/`outbound` defaults, allow rules). |
| [communication-policy/deploy.py](communication-policy/deploy.py) | `PUT /v1/workspaces/{id}/networking/communicationPolicy`. |

A few things to know if you adopt it:

### Private endpoints used by each pipeline

The two pipelines hit different Fabric planes and therefore need
different private endpoints if you want both on private link:

| Pipeline | Plane | Endpoint hit | Private endpoint type | Private DNS zone(s) |
|---|---|---|---|---|
| Workload (env + notebook) | Data | `<wsid>.zfc.w.api.fabric.microsoft.com` | **Workspace-level** PE on the Fabric workspace (sub-resource = `workspace`) | `privatelink.fabric.microsoft.com` |
| Communication policy | Control | `api.fabric.microsoft.com` (`/v1/workspaces/{id}/networking/communicationPolicy`) | **Tenant-level** PE on `Microsoft.PowerBI/privateLinkServicesForPowerBI` (sub-resource = `Tenant`) | `privatelink.analysis.windows.net`, `privatelink.pbidedicated.windows.net`, `privatelink.prod.powerquery.microsoft.com` |

The workspace-level PE is the **required** one (the workload pipeline
won't work without it). The tenant-level PE is **optional** — without it,
the policy pipeline still works, it just routes the admin call over the
public Fabric control plane via the agent's outbound internet. See
[tenant-level Azure Private Link for Fabric](https://learn.microsoft.com/fabric/security/security-private-links-use)
for the tenant-PE setup; *Block Public Internet Access* on the tenant is
**not** required for the routing to take effect.

### Proving the policy pipeline uses the tenant PE

[infra/agent-vm.bicep](infra/agent-vm.bicep) exposes `applyRestrictiveNsg`
(default `false`). When `true`, the NIC NSG denies all public outbound
except `AzureDevOps`, `AzureFrontDoor.FirstParty`, `AzureActiveDirectory`,
`AzureResourceManager` and intra-VNet. If the policy `PUT` still
succeeds under this lockdown, `api.fabric.microsoft.com` necessarily
resolved to the tenant PE. `deploy.py` is stdlib-only so it needs no
PyPI access; turn the lockdown off again before any run that downloads
packages.


