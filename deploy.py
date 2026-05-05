# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Deploy the Presidio Fabric Environment + smoke-test notebook to a
WSPL/DEP-protected Fabric workspace using the fabric-cicd library
(https://microsoft.github.io/fabric-cicd/).

Run from an Azure DevOps agent that has line-of-sight to the workspace's
private FQDN, after `pip install -r requirements-deploy.txt`.

Required environment variables:
  FABRIC_WORKSPACE_ID   GUID of the target workspace
  FABRIC_ENVIRONMENT    Logical environment name used in parameter.yml (e.g. 'PPE')
  FABRIC_BASE_API_URL   https://<wsid-no-dashes>.zfc.w.api.fabric.microsoft.com

Auth: AzureCLI@2 (with addSpnToEnvironment: true) sets AZURE_TENANT_ID and
AZURE_CLIENT_ID and leaves an authenticated `az` session. DefaultAzureCredential
resolves to AzureCliCredential -- no client secret stored anywhere (WIF).
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential

from fabric_cicd import (
    FabricWorkspace,
    change_log_level,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd import constants as fabric_constants
from fabric_cicd._items import _environment as _env_mod
from fabric_cicd.constants import ItemType


def _patch_clear_environment_yml() -> None:
    """
    Monkey-patch fabric-cicd to clear `environment.yml` from staging/libraries
    BEFORE triggering publish.

    Why: in WSPL/DEP-locked workspaces, Fabric's publish backend cannot resolve
    public conda/pip dependencies (pypi.org and conda-forge are blocked). The
    only state that publishes successfully is `environmentYml: ""` (verified
    against a manually-created working env). fabric-cicd uploads environment.yml
    as a definition part and the API rejects empty payload, so we delete it
    server-side after upload but before publish.
    """
    original = _env_mod._publish_environment_metadata

    def patched(fabric_workspace_obj, item_name):
        item_guid = fabric_workspace_obj.repository_items[ItemType.ENVIRONMENT.value][item_name].guid
        url = (
            f"{fabric_workspace_obj.base_api_url}/environments/{item_guid}"
            f"/staging/libraries?libraryToDelete=environment.yml"
        )
        try:
            fabric_workspace_obj.endpoint.invoke(method="DELETE", url=url)
            print(f"  Cleared environment.yml from staging/libraries for '{item_name}'")
        except Exception as exc:
            # Non-fatal: if there was no environment.yml staged, the API may 404.
            print(f"  Warning: could not delete staged environment.yml for '{item_name}': {exc}")
        return original(fabric_workspace_obj, item_name)

    _env_mod._publish_environment_metadata = patched


def _force_notebook_env_binding(fabric_workspace_obj, notebook_name: str) -> None:
    """
    Re-POST the notebook's source-controlled definition through the
    notebook-specific updateDefinition endpoint. This is a workaround for
    fabric-cicd 1.0 publishing notebooks via the generic /items endpoint,
    which drops the `# META "dependencies": { "environment": {...} }`
    block from notebook-content.py and leaves the notebook attached to
    the workspace's default environment.
    """
    repo_items = fabric_workspace_obj.repository_items
    nb = repo_items.get(ItemType.NOTEBOOK.value, {}).get(notebook_name)
    if nb is None or not nb.guid:
        print(f"  Skipping env-binding force: notebook '{notebook_name}' not in repo or not deployed")
        return

    nb_dir = Path(nb.path)
    parts = []
    for fname in ("notebook-content.py", ".platform"):
        fpath = nb_dir / fname
        if not fpath.exists():
            continue
        payload_b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
        parts.append({"path": fname, "payload": payload_b64, "payloadType": "InlineBase64"})

    url = (
        f"{fabric_workspace_obj.base_api_url}/notebooks/{nb.guid}"
        f"/updateDefinition?updateMetadata=True"
    )
    body = {"definition": {"parts": parts}}
    print(f"  Forcing notebook env binding via {url}")
    resp = fabric_workspace_obj.endpoint.invoke(method="POST", url=url, body=body)
    # 202 = accepted, LRO; 200 = sync success
    print(f"  updateDefinition response: status={resp.get('status_code', '?')}")
    # Brief settle to let LRO complete (fabric-cicd's invoke usually polls,
    # but we add a small grace period before any downstream smoke test).
    time.sleep(5)


def main() -> None:
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    environment = os.environ.get("FABRIC_ENVIRONMENT", "PPE")
    # Override the API endpoint when the workspace blocks public inbound
    # traffic (DEP / Private Link). fabric-cicd 1.0 still defaults to the
    # legacy https://api.powerbi.com endpoint, which is rejected by DEP
    # workspaces; force the Fabric endpoint by default.
    base_api_url = os.environ.get("FABRIC_BASE_API_URL", "https://api.fabric.microsoft.com")
    fabric_constants.DEFAULT_API_ROOT_URL = base_api_url

    # Point fabric-cicd at the workspace folder that contains the
    # source-controlled Fabric items. Folder layout mirrors what the
    # Fabric Source Control UI produces.
    repository_directory = str(Path(__file__).resolve().parent / "workspace")

    # Item scope is overridable via env so the pipeline can do a fast
    # notebook-only redeploy (skipping the 10-minute environment publish).
    item_type_in_scope = [
        s.strip()
        for s in os.environ.get("FABRIC_ITEM_TYPES", "Environment,Notebook").split(",")
        if s.strip()
    ]
    print(f"Item types in scope: {item_type_in_scope}")

    if os.environ.get("FABRIC_DEBUG", "").lower() in ("1", "true", "yes"):
        change_log_level("DEBUG")

    _patch_clear_environment_yml()

    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        environment=environment,
        repository_directory=repository_directory,
        item_type_in_scope=item_type_in_scope,
        token_credential=DefaultAzureCredential(),
    )

    publish_all_items(target_workspace)

    # Force the notebook's environment binding via the notebook-specific
    # REST endpoint. fabric-cicd publishes notebooks via the generic
    # /items endpoint which appears to drop the `# META "dependencies"`
    # block from notebook-content.py, leaving the notebook attached to
    # the workspace's default (i.e. no) environment. Re-POST the same
    # definition through the notebook-specific updateDefinition endpoint
    # which preserves it.
    _force_notebook_env_binding(target_workspace, "PresidioSmokeTest")

    # Only unpublish items that we own (safety guard:
    # do not touch unrelated items if scope is widened later).
    unpublish_all_orphan_items(
        target_workspace,
        item_name_exclude_regex=r"^(?!PresidioPriv$|PresidioSmokeTest$).*",
    )


if __name__ == "__main__":
    main()
