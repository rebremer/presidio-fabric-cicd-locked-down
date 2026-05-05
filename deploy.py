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

    Also rewrites the dependencies block so the env GUID is whatever the
    'PresidioPriv' env actually has in the target workspace right now,
    because Fabric silently strips dependencies that point at a
    non-existent environmentId.
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

    # Wait for the environment publish to settle. Fabric silently strips
    # notebook->environment bindings whose target env is mid-publish
    # (sparkLibraries.state == 'Running'); we must block until it
    # reaches a terminal state.
    _wait_for_env_publish(fabric_workspace_obj, env_guid, timeout_s=20 * 60)

    nb_dir = Path(nb.path)
    print(f"  source dir: {nb_dir}")
    parts = []
    # Only re-POST notebook-content.py (which carries the dependencies
    # block in its `# META` header). Including .platform with
    # updateMetadata=True caused Fabric to overwrite the env binding
    # because .platform has no dependencies field.
    fname = "notebook-content.py"
    fpath = nb_dir / fname
    if not fpath.exists():
        print(f"  WARN: {fpath} missing; cannot force-bind")
        return
    raw = fpath.read_bytes()
    txt = raw.decode("utf-8")
    txt = _rewrite_env_binding(txt, env_guid, workspace_guid)
    raw = txt.encode("utf-8")
    has_dep = '"dependencies"' in txt and '"environment"' in txt
    print(f"  rewrote env binding in {fname} ({len(raw)} bytes); dep block present: {has_dep}")
    print("  ----- outgoing notebook-content.py header (first 14 lines) -----")
    print("\n".join(txt.splitlines()[:14]))
    print("  ---------------------------------------------------------------")
    parts.append({
        "path": fname,
        "payload": base64.b64encode(raw).decode("ascii"),
        "payloadType": "InlineBase64",
    })

    url = (
        f"{fabric_workspace_obj.base_api_url}/notebooks/{nb.guid}"
        f"/updateDefinition"
    )
    body = {"definition": {"format": "fabricGitSource", "parts": parts}}
    print(f"  POST {url}")
    try:
        resp = fabric_workspace_obj.endpoint.invoke(method="POST", url=url, body=body)
        print(f"  status_code: {resp.get('status_code', '?') if isinstance(resp, dict) else resp}")
    except Exception as exc:
        print(f"  ERROR forcing notebook env binding: {exc!r}")
        raise
    time.sleep(5)

    # Read the published notebook back so we can confirm what Fabric
    # actually stored (the portal can be misleading about env binding).
    get_url = f"{fabric_workspace_obj.base_api_url}/notebooks/{nb.guid}/getDefinition"
    print(f"  POST {get_url}")
    try:
        getd = fabric_workspace_obj.endpoint.invoke(method="POST", url=get_url, body={})
        body_obj = getd.get("body", {}) if isinstance(getd, dict) else {}
        parts_out = body_obj.get("definition", {}).get("parts", [])
        for p in parts_out:
            if p.get("path") == "notebook-content.py":
                content = base64.b64decode(p["payload"]).decode("utf-8", errors="replace")
                head = "\n".join(content.splitlines()[:20])
                print("  ----- deployed notebook-content.py (first 20 lines) -----")
                print(head)
                print("  ---------------------------------------------------------")
                has_env = '"environmentId"' in content
                print(f"  deployed notebook contains environmentId: {has_env}")
                if not has_env:
                    raise RuntimeError(
                        "Fabric stripped the environment binding from the notebook "
                        "definition on save. The referenced environmentId likely "
                        "does not exist in the target workspace."
                    )
    except Exception as exc:
        print(f"  ERROR verifying notebook env binding: {exc!r}")
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


def _rewrite_env_binding(content: str, env_guid: str, workspace_guid: str) -> str:
    """
    Replace any existing `# META "environmentId": "..."` and
    `# META "workspaceId": "..."` lines in the notebook header with the
    resolved guids. Assumes the source already has a dependencies block
    (this repo's PresidioSmokeTest notebook does).
    """
    import re

    content = re.sub(
        r'(# META\s+"environmentId":\s*")[^"]*(")',
        rf"\g<1>{env_guid}\g<2>",
        content,
    )
    content = re.sub(
        r'(# META\s+"workspaceId":\s*")[^"]*(")',
        rf"\g<1>{workspace_guid}\g<2>",
        content,
    )
    return content


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
