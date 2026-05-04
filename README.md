# Presidio in a Fabric Environment — CI/CD with fabric-cicd

End-to-end CI/CD scaffold that deploys Microsoft **Presidio** (PII detection +
anonymization) into a Microsoft Fabric **Environment** item using
[fabric-cicd](https://microsoft.github.io/fabric-cicd/), and runs it from
**Azure DevOps** through a self-hosted agent that can reach a
**DEP-protected** Fabric workspace.

## Architecture

```
                        Laptop / dev box / Fabric feature workspace
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
              │   • In VNet linked to privatelink.fabric │
              │     .microsoft.com private DNS zone      │
              └─────────────────────────┬────────────────┘
                                        │ REST (UAMI token via WIF)
                                        │ to <wsid>.zfc.w.api.fabric...
                                        ▼
              ┌──────────────────────────────────────────┐
              │  Private Endpoint (workspace-level PL)   │
              │   • 5 sub-resources (w/api, c, onelake,  │
              │     dfs, blob) → 10.2.0.11..13/4/5       │
              │   • Inbound policy: only this PE allowed │
              └─────────────────────────┬────────────────┘
                                        │
                                        ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Fabric workspace  (DEP + WSPL, no pypi.org egress,        │
   │   public api.fabric.microsoft.com BLOCKED inbound)         │
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
works; the SPN is what matters. Grant that SPN **Contributor** on the
Fabric workspace (sufficient to create/update Environment & Notebook items),
and ensure the tenant setting *"Service principals can use Fabric APIs"* is
enabled.

### 4. Pipeline variables
Pipelines → `deploy-presidio-fabric` → **Edit → Variables** (or a Library
variable group):

| Name | Example |
|---|---|
| `AZURE_SERVICE_CONNECTION` | `<service-connection-name>` |
| `FABRIC_WORKSPACE_ID`      | `601bdd88-8a8d-0805-a3b1-af6b6b2d1b17` (testpriv) |
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

## Deploy to a workspace reachable only via Private Link

If the target workspace has a **Workspace-level Private Link (WSPL)** and the
public Fabric endpoint is blocked by an inbound communication policy, you'll
hit:

```
Request denied due to inbound communication policy
```

Fabric requires that REST calls hit the workspace's **dedicated FQDN** so the
PE can authorize them. fabric-cicd's default base URL
(`https://api.fabric.microsoft.com`) won't satisfy that; it has to be
overridden per workspace.

### 1. Use a User-Assigned Managed Identity (recommended)

A WSPL inbound policy validates the caller via the `xms_mirid` claim, which
only managed identities carry. Plain SPN tokens are rejected. UAMI also
removes the need to manage a client secret.

1. Create a UAMI (e.g. `fabric-cicd-uami`) in any RG of the right tenant.
2. Add it as **Contributor** on the Fabric workspace.
3. In ADO → Project Settings → **Service connections** → *New* →
   *Azure Resource Manager* → **Workload Identity federation (manual)**, and
   bind the federated credential to the UAMI (Subject identifier:
   `sc://<org>/<project>/<connection-name>`, Issuer:
   `https://vstoken.dev.azure.com/<org-guid>`).
4. Reference that connection from `azure-pipelines.yml` (`AzureCLI@2` task
   with `addSpnToEnvironment: true`). The Az CLI inside the task will mint
   tokens via WIF — no secrets stored anywhere.

### 2. Discover the workspace's private FQDN

Each workspace's PE exposes 5 sub-resources. Get them from the PE NIC:

```powershell
$peId = (az network private-endpoint show -g <pe-rg> -n <pe-name> --query id -o tsv)
$nic  = (az network private-endpoint show --ids $peId --query "networkInterfaces[0].id" -o tsv)
az resource show --ids $nic `
  --query "properties.ipConfigurations[].{name:name, fqdns:properties.privateLinkConnectionProperties.fqdns, ip:properties.privateIPAddress}" -o json
```

The control-plane FQDN you need is the `…zfc.w.api.fabric.microsoft.com`
record (e.g. `fc5a31aa23f64a079b8b8df04c70facd.zfc.w.api.fabric.microsoft.com`
→ `10.2.0.11`). The other four (`c`, `onelake`, `dfs`, `blob`) are for the
runtime and OneLake.

### 3. Make the agent resolve that FQDN privately

The Private DNS zone `privatelink.fabric.microsoft.com` (auto-created in the
PE's RG) holds A-records for all 5 FQDNs. The agent VM's VNet must be linked
to that zone:

```powershell
az network private-dns link vnet list -g <zone-rg> -z privatelink.fabric.microsoft.com -o table
# If your agent VNet isn't listed, add a link:
az network private-dns link vnet create -g <zone-rg> -z privatelink.fabric.microsoft.com `
  -n <link-name> -v <agent-vnet-id> -e false
```

Verify from the agent VM:

```powershell
ipconfig /flushdns; Clear-DnsClientCache
Resolve-DnsName fc5a31aa23f64a079b8b8df04c70facd.zfc.w.api.fabric.microsoft.com
# → must return the private IP (e.g. 10.2.0.11), not a public 40.x.x.x
```

### 4. Point fabric-cicd at the workspace FQDN

Add a single pipeline variable (Pipelines → `deploy-presidio-fabric` → Edit →
Variables → New):

| Name | Value |
|---|---|
| `FABRIC_BASE_API_URL` | `https://<workspace-id-no-dashes>.zfc.w.api.fabric.microsoft.com` |

`deploy.py` reads it and overrides
`fabric_cicd.constants.DEFAULT_API_ROOT_URL` before constructing
`FabricWorkspace`. No code change needed.

### 5. Outbound egress required from the agent

Only HTTPS to:

- `https://dev.azure.com/<org>` (ADO control plane)
- `https://login.microsoftonline.com` (token endpoint, public)
- `https://<workspace-id>.zfc.w.api.fabric.microsoft.com` (private IP via PE)
- `https://login.windows.net` for federated token exchange (WIF)

The public `api.fabric.microsoft.com` is **not** required — and shouldn't be
reachable if the inbound policy is doing its job.

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
