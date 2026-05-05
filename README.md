# Presidio in a locked-down Fabric workspace — CI/CD with fabric-cicd

End-to-end CI/CD that deploys Microsoft **Presidio** (PII detection +
anonymization) into a Microsoft Fabric **Environment** item inside a
**WSPL/DEP-protected** workspace (no public inbound, no pypi.org/conda-forge
outbound), using [fabric-cicd](https://microsoft.github.io/fabric-cicd/)
driven from **Azure DevOps** through a self-hosted Linux agent.

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
   │     │     └── Libraries/                                           │
   │     │           ├── environment.yml       (uploaded then DELETED   │
   │     │           │                          from staging by         │
   │     │           │                          deploy.py before publish)│
   │     │           └── CustomLibraries/      (53 wheels uploaded by   │
   │     │                                      fabric-cicd)            │
   │     └── PresidioSmokeTest.Notebook        (attached to env above)  │
   └────────────────────────────────────────────────────────────────────┘
```

## The key insight: clear `environment.yml` before publish

In a WSPL/DEP workspace, pypi.org and conda-forge are unreachable from the
Fabric publish backend. Any `environment.yml` with public dependencies
(including the seemingly innocent `python=3.11` and `pip` defaults that
fabric-cicd adds) makes the publish fail silently with
`sparkLibraries.state: "Failed"` — Fabric's API exposes no error message.

Workspace storage (where `CustomLibraries/` lives) IS reachable via WSPL,
so wheels uploaded there install fine — provided we stop Fabric from
trying to resolve the public deps section at all.

`deploy.py` monkey-patches `fabric_cicd._items._environment` to call:

```
DELETE /v1/workspaces/{wsid}/environments/{eid}/staging/libraries?libraryToDelete=environment.yml
```

right after fabric-cicd uploads the definition and right before it
triggers publish. Net effect: staging ends up with `environmentYml: ""`
plus the wheels — the same state a manually-created working env shows.

Verify after a run from inside the VNet:

```bash
az rest --method get --resource https://api.fabric.microsoft.com \
  --url "https://<wsid>.zfc.w.api.fabric.microsoft.com/v1/workspaces/<wsid>/environments/<eid>/staging/libraries"
# Expect:
# { "customLibraries": { "wheelFiles": [...] }, "environmentYml": "" }
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
└── workspace/
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
| `FABRIC_WORKSPACE_ID`      | `fc5a31aa-23f6-4a07-9b8b-8df04c70facd` |
| `FABRIC_BASE_API_URL`      | `https://<wsid-no-dashes>.zfc.w.api.fabric.microsoft.com` |
| `FABRIC_ENVIRONMENT`       | `PPE` |

### 5. Discover the workspace's private FQDN

```powershell
$peId = (az network private-endpoint show -g <pe-rg> -n <pe-name> --query id -o tsv)
$nic  = (az network private-endpoint show --ids $peId --query "networkInterfaces[0].id" -o tsv)
az resource show --ids $nic `
  --query "properties.ipConfigurations[].{name:name, fqdns:properties.privateLinkConnectionProperties.fqdns, ip:properties.privateIPAddress}" -o json
```

Use the `…zfc.w.api.fabric.microsoft.com` record for `FABRIC_BASE_API_URL`.

## Run it

`git push` to `main` → pipeline triggers automatically. The Environment
publish is a long-running operation (~5–15 min for the full Presidio
closure of 53 wheels); fabric-cicd polls until completion.

## Conflict mitigation

Presidio brings `pydantic`, `spacy`, `regex`, etc. that may overlap with
the Fabric runtime. Recommendations:

- Pin exact versions in `requirements-presidio.txt`.
- Keep this environment dedicated; attach it only to notebooks that need
  PII detection — don't make it the workspace default.
- If a clash surfaces at runtime, pin the conflicting package down to
  whatever the Fabric runtime ships and re-publish.

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

## References

- fabric-cicd: <https://microsoft.github.io/fabric-cicd/>
- Presidio:    <https://microsoft.github.io/presidio/>
- Fabric Environment + custom libraries:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library>
- Outbound access protection:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library-with-outbound-access-protection>
