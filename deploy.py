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
import json
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
    Force the notebook's environment binding by round-tripping the
    notebook definition in **ipynb** format.

    Background: Fabric's REST `updateDefinition` silently strips the
    `# META "dependencies": { "environment": {...} }` block when the
    payload is `fabricGitSource` (the .py-with-#META source-control
    format that fabric-cicd publishes). The `ipynb` format does NOT
    have that bug -- `notebookutils.notebook.updateDefinition` itself
    relies on it to update env / lakehouse bindings.

    Workflow:
      1. GET current definition with ?format=ipynb
      2. Mutate metadata.dependencies.environment with the resolved
         workspace + env GUIDs
      3. POST it back with ?format=ipynb

    No live Spark session needed -- this is a pure metadata update on
    the notebook artifact.
    """
    print(f"==> Force-binding notebook '{notebook_name}' to its declared environment")
    repo_items = fabric_workspace_obj.repository_items
    nb = repo_items.get(ItemType.NOTEBOOK.value, {}).get(notebook_name)
    if nb is None:
        print(f"  WARN: notebook '{notebook_name}' not in repository_items; skipping")
        return
    if not nb.guid:
        print(f"  WARN: notebook '{notebook_name}' has no deployed guid; skipping")
        return

    env_item = repo_items.get(ItemType.ENVIRONMENT.value, {}).get("PresidioPriv")
    if env_item is None or not env_item.guid:
        # Notebook-only redeploy: env item isn't in scope. Look it up via REST.
        env_guid = _lookup_environment_guid(fabric_workspace_obj, "PresidioPriv")
    else:
        env_guid = env_item.guid
    if not env_guid:
        print("  WARN: could not resolve PresidioPriv env guid; skipping force-bind")
        return
    workspace_guid = fabric_workspace_obj.workspace_id
    print(f"  binding to environmentId={env_guid} workspaceId={workspace_guid}")

    # Wait for the environment publish to settle. Fabric rejects a
    # binding to an env whose publish is still Running.
    _wait_for_env_publish(fabric_workspace_obj, env_guid, timeout_s=20 * 60)

    # 1. Pull the deployed notebook in ipynb format.
    get_url = (
        f"{fabric_workspace_obj.base_api_url}/notebooks/{nb.guid}"
        f"/getDefinition?format=ipynb"
    )
    print(f"  POST {get_url}")
    try:
        getd = fabric_workspace_obj.endpoint.invoke(method="POST", url=get_url, body={})
    except Exception as exc:
        print(f"  ERROR fetching notebook definition: {exc!r}")
        raise
    body_obj = getd.get("body", {}) if isinstance(getd, dict) else {}
    parts_in = body_obj.get("definition", {}).get("parts", [])
    ipynb_part = None
    for p in parts_in:
        if p.get("path", "").endswith(".ipynb"):
            ipynb_part = p
            break
    if ipynb_part is None:
        raise RuntimeError(
            f"getDefinition?format=ipynb returned no .ipynb part "
            f"(parts: {[p.get('path') for p in parts_in]})"
        )
    ipynb_path = ipynb_part["path"]
    ipynb_bytes = base64.b64decode(ipynb_part["payload"])
    nb_json = json.loads(ipynb_bytes.decode("utf-8"))

    # 2. Mutate metadata.dependencies.environment.
    metadata = nb_json.setdefault("metadata", {})
    deps = metadata.setdefault("dependencies", {})
    deps["environment"] = {
        "environmentId": env_guid,
        "workspaceId": workspace_guid,
    }
    print(f"  set metadata.dependencies.environment = {deps['environment']}")

    new_bytes = json.dumps(nb_json, indent=2).encode("utf-8")
    new_part = {
        "path": ipynb_path,
        "payload": base64.b64encode(new_bytes).decode("ascii"),
        "payloadType": "InlineBase64",
    }

    # 3. POST it back in ipynb format.
    put_url = (
        f"{fabric_workspace_obj.base_api_url}/notebooks/{nb.guid}"
        f"/updateDefinition?format=ipynb"
    )
    body = {"definition": {"parts": [new_part]}}
    print(f"  POST {put_url}")
    try:
        resp = fabric_workspace_obj.endpoint.invoke(method="POST", url=put_url, body=body)
        status = resp.get("status_code", "?") if isinstance(resp, dict) else resp
        print(f"  status_code: {status}")
    except Exception as exc:
        print(f"  ERROR forcing notebook env binding: {exc!r}")
        raise

    # 4. Verify by reading back.
    time.sleep(5)
    print(f"  POST {get_url} (verify)")
    verifyd = fabric_workspace_obj.endpoint.invoke(method="POST", url=get_url, body={})
    vbody = verifyd.get("body", {}) if isinstance(verifyd, dict) else {}
    vparts = vbody.get("definition", {}).get("parts", [])
    for p in vparts:
        if p.get("path", "").endswith(".ipynb"):
            content = base64.b64decode(p["payload"]).decode("utf-8", errors="replace")
            try:
                vjson = json.loads(content)
                env_after = (
                    vjson.get("metadata", {})
                    .get("dependencies", {})
                    .get("environment", {})
                )
            except Exception:
                env_after = None
            print(f"  deployed metadata.dependencies.environment: {env_after}")
            if not env_after or env_after.get("environmentId") != env_guid:
                raise RuntimeError(
                    "Fabric did not persist the environment binding (ipynb format). "
                    f"Expected environmentId={env_guid}, got {env_after}."
                )
            break
    else:
        raise RuntimeError("Verification failed: no .ipynb part returned")


def _cancel_running_notebook_jobs(fabric_workspace_obj, notebook_guid: str) -> None:
    """
    List job instances for the notebook and cancel any in non-terminal
    state. Fabric pins notebook definition while a Spark session is
    active, so updateDefinition silently no-ops until the session ends.
    """
    list_url = f"{fabric_workspace_obj.base_api_url}/items/{notebook_guid}/jobs/instances"
    try:
        resp = fabric_workspace_obj.endpoint.invoke(method="GET", url=list_url)
    except Exception as exc:
        print(f"  WARN: could not list notebook jobs: {exc!r}")
        return
    body = resp.get("body", {}) if isinstance(resp, dict) else {}
    instances = body.get("value", [])
    active = [i for i in instances if i.get("status") in ("InProgress", "NotStarted")]
    if not active:
        print("  no active notebook jobs to cancel")
        return
    print(f"  cancelling {len(active)} active notebook job(s)")
    for inst in active:
        inst_id = inst.get("id")
        cancel_url = f"{fabric_workspace_obj.base_api_url}/items/{notebook_guid}/jobs/instances/{inst_id}/cancel"
        try:
            fabric_workspace_obj.endpoint.invoke(method="POST", url=cancel_url, body={})
            print(f"    cancelled {inst_id}")
        except Exception as exc:
            print(f"    WARN: could not cancel {inst_id}: {exc!r}")
    # Brief settle so the cancel takes effect before we updateDefinition.
    time.sleep(15)


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
    Fabric silently strips notebook->env bindings while a publish is
    Running, so we must block here before re-binding the notebook.
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
