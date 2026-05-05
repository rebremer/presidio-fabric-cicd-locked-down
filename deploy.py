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

import base64  # noqa: F401  (kept for future definition-level workarounds)
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


def _set_workspace_default_environment(fabric_workspace_obj, env_name: str) -> None:
    """
    Set `env_name` as the workspace default Spark environment via
    PATCH /workspaces/{id}/spark/settings. Notebooks attached to
    "Workspace default" then inherit this environment automatically.

    Why this instead of binding the notebook directly: Fabric's public
    REST `notebooks/{id}/updateDefinition` for fabricGitSource format
    silently strips the `# META "dependencies": { "environment": {...} }`
    block from notebook-content.py. Per-notebook bindings can only be
    set via internal portal APIs. Setting the workspace default is the
    documented, supported path for governed shared workloads.

    Caller (SP/UAMI) must have workspace Admin role; Member is not
    sufficient for /spark/settings.
    """
    print(f"==> Setting '{env_name}' as workspace default Spark environment")
    env_guid = _lookup_environment_guid(fabric_workspace_obj, env_name)
    if not env_guid:
        print(f"  WARN: env '{env_name}' not found in workspace; skipping")
        return
    _wait_for_env_publish(fabric_workspace_obj, env_guid, timeout_s=20 * 60)

    url = f"{fabric_workspace_obj.base_api_url}/spark/settings"
    body = {"environment": {"name": env_name}}
    print(f"  PATCH {url} body={body}")
    try:
        resp = fabric_workspace_obj.endpoint.invoke(method="PATCH", url=url, body=body)
        status = resp.get("status_code", "?") if isinstance(resp, dict) else resp
        body_out = resp.get("body", {}) if isinstance(resp, dict) else {}
        print(f"  status_code: {status}")
        print(f"  workspace spark settings.environment: {body_out.get('environment')}")
    except Exception as exc:
        print(f"  ERROR setting workspace default environment: {exc!r}")
        print("  HINT: the deploying identity must have workspace Admin role.")
        raise


def _lookup_environment_guid(fabric_workspace_obj, env_name: str) -> str | None:
    """List workspace environments and return the GUID matching env_name."""
    url = f"{fabric_workspace_obj.base_api_url}/environments"
    resp = fabric_workspace_obj.endpoint.invoke(method="GET", url=url)
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    for env in body.get("value", []):
        if env.get("displayName") == env_name:
            return env.get("id")
    return None


def _wait_for_env_publish(fabric_workspace_obj, env_guid: str, timeout_s: int = 1200) -> None:
    """
    Poll /environments/{id} until publishDetails.state is terminal.
    Workspace default cannot be pointed at an env mid-publish.
    """
    url = f"{fabric_workspace_obj.base_api_url}/environments/{env_guid}"
    deadline = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        resp = fabric_workspace_obj.endpoint.invoke(method="GET", url=url)
        body = resp.get("body", {}) if isinstance(resp, dict) else {}
        details = body.get("properties", {}).get("publishDetails", {}) or {}
        state = details.get("state")
        comp = details.get("componentPublishInfo", {}) or {}
        libs_state = (comp.get("sparkLibraries") or {}).get("state")
        settings_state = (comp.get("sparkSettings") or {}).get("state")
        if state != last_state:
            print(f"  env publish state={state} sparkLibraries={libs_state} sparkSettings={settings_state}")
            last_state = state
        if state in ("Success", None) and libs_state in ("Success", None):
            print("  env publish settled")
            return
        if state in ("Failed", "Cancelled"):
            raise RuntimeError(f"Environment publish ended in state={state} ({details})")
        time.sleep(20)
    raise RuntimeError(f"Timed out waiting {timeout_s}s for env publish to settle (last state={last_state})")


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

    # Bind the notebook to PresidioPriv via the workspace default
    # environment (PATCH /workspaces/{id}/spark/settings). The
    # public REST updateDefinition endpoint silently strips the
    # `# META "dependencies"` block from notebook-content.py for
    # fabricGitSource format -- only the Fabric portal can set a
    # per-notebook env binding via internal APIs. Setting the
    # workspace default makes any notebook attached to "Workspace
    # default" pick up PresidioPriv automatically; this is the
    # supported path for governed envs and is also what Microsoft
    # docs recommend for shared workloads.
    _set_workspace_default_environment(target_workspace, "PresidioPriv")

    # Only unpublish items that we own (safety guard:
    # do not touch unrelated items if scope is widened later).
    unpublish_all_orphan_items(
        target_workspace,
        item_name_exclude_regex=r"^(?!PresidioPriv$|PresidioSmokeTest$).*",
    )


if __name__ == "__main__":
    main()
