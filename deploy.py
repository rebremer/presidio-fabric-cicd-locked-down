# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Deploy the Presidio Fabric Environment + smoke-test notebook to a
WSPL/DEP-protected Fabric workspace using fabric-cicd
(https://microsoft.github.io/fabric-cicd/).

Two surgical workarounds layered on top of vanilla fabric-cicd:

  1. Clear `environment.yml` from staging before publish. In
     WSPL/DEP workspaces pypi.org/conda-forge are unreachable; the
     only env state that publishes is `environmentYml: ""` plus the
     wheels under `CustomLibraries/`.
  2. Re-bind the notebook to PresidioPriv via an `ipynb`-format
     round-trip on `notebooks/{id}/updateDefinition`. fabric-cicd
     publishes notebooks in `fabricGitSource` format, on which
     Fabric silently strips `metadata.dependencies.environment`;
     the `ipynb` format preserves it (same path
     `notebookutils.notebook.updateDefinition` uses internally).

Required env: FABRIC_WORKSPACE_ID, FABRIC_BASE_API_URL,
optional: FABRIC_ENVIRONMENT (default 'PPE'), FABRIC_ITEM_TYPES
(default 'Environment,Notebook'), FABRIC_DEBUG.

Auth: DefaultAzureCredential -> AzureCliCredential, federated via
AzureCLI@2 (WIF, no secrets stored).
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

ENV_NAME = "PresidioPriv"
NOTEBOOK_NAME = "PresidioSmokeTest"


def _invoke(ws, method, url, body=None):
    resp = ws.endpoint.invoke(method=method, url=url, body=body if body is not None else {})
    return resp.get("body", {}) if isinstance(resp, dict) else {}


def _patch_clear_environment_yml() -> None:
    """Delete environment.yml from staging right before publish (workaround #1)."""
    original = _env_mod._publish_environment_metadata

    def patched(ws, item_name):
        guid = ws.repository_items[ItemType.ENVIRONMENT.value][item_name].guid
        url = (
            f"{ws.base_api_url}/environments/{guid}"
            f"/staging/libraries?libraryToDelete=environment.yml"
        )
        try:
            _invoke(ws, "DELETE", url)
            print(f"  cleared environment.yml from staging for '{item_name}'")
        except Exception as exc:
            print(f"  warn: could not delete staged environment.yml: {exc}")
        return original(ws, item_name)

    _env_mod._publish_environment_metadata = patched


def _lookup_env_guid(ws, name: str) -> str | None:
    body = _invoke(ws, "GET", f"{ws.base_api_url}/environments")
    return next((e["id"] for e in body.get("value", []) if e.get("displayName") == name), None)


def _wait_for_env_publish(ws, env_guid: str, timeout_s: int = 1200) -> None:
    """Block until publishDetails.state is terminal. Bind fails on Running."""
    url = f"{ws.base_api_url}/environments/{env_guid}"
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        details = _invoke(ws, "GET", url).get("properties", {}).get("publishDetails", {}) or {}
        state = details.get("state")
        libs = (details.get("componentPublishInfo", {}) or {}).get("sparkLibraries", {}).get("state")
        if state != last:
            print(f"  env publish state={state} sparkLibraries={libs}")
            last = state
        if state in ("Success", None) and libs in ("Success", None):
            return
        if state in ("Failed", "Cancelled"):
            raise RuntimeError(f"Environment publish ended in state={state}")
        time.sleep(20)
    raise RuntimeError(f"Timed out after {timeout_s}s waiting for env publish (last={last})")


def _get_ipynb_part(ws, nb_guid: str) -> dict:
    """GET notebook definition as ipynb and return the .ipynb part."""
    url = f"{ws.base_api_url}/notebooks/{nb_guid}/getDefinition?format=ipynb"
    parts = _invoke(ws, "POST", url).get("definition", {}).get("parts", [])
    part = next((p for p in parts if p.get("path", "").endswith(".ipynb")), None)
    if part is None:
        raise RuntimeError(f"getDefinition?format=ipynb returned no .ipynb part: {[p.get('path') for p in parts]}")
    return part


def _force_notebook_env_binding(ws, notebook_name: str) -> None:
    """Re-bind notebook to PresidioPriv via ipynb-format updateDefinition (workaround #2)."""
    print(f"==> Binding notebook '{notebook_name}' to '{ENV_NAME}'")
    nb = ws.repository_items.get(ItemType.NOTEBOOK.value, {}).get(notebook_name)
    if nb is None or not nb.guid:
        print(f"  warn: notebook '{notebook_name}' not deployed; skipping")
        return

    env_item = ws.repository_items.get(ItemType.ENVIRONMENT.value, {}).get(ENV_NAME)
    env_guid = (env_item.guid if env_item else None) or _lookup_env_guid(ws, ENV_NAME)
    if not env_guid:
        raise RuntimeError(f"environment '{ENV_NAME}' not found in workspace {ws.workspace_id}")
    print(f"  environmentId={env_guid} workspaceId={ws.workspace_id}")

    _wait_for_env_publish(ws, env_guid)

    part = _get_ipynb_part(ws, nb.guid)
    nb_json = json.loads(base64.b64decode(part["payload"]).decode("utf-8"))
    nb_json.setdefault("metadata", {}).setdefault("dependencies", {})["environment"] = {
        "environmentId": env_guid,
        "workspaceId": ws.workspace_id,
    }

    # POST it back. Format goes inside the body; ?format=ipynb on the URL means
    # "convert .py -> .ipynb" on this endpoint and is rejected.
    url = f"{ws.base_api_url}/notebooks/{nb.guid}/updateDefinition"
    body = {
        "definition": {
            "format": "ipynb",
            "parts": [{
                "path": part["path"],
                "payload": base64.b64encode(json.dumps(nb_json, indent=2).encode("utf-8")).decode("ascii"),
                "payloadType": "InlineBase64",
            }],
        },
    }
    _invoke(ws, "POST", url, body)
    time.sleep(5)

    after = json.loads(base64.b64decode(_get_ipynb_part(ws, nb.guid)["payload"]).decode("utf-8"))
    bound = after.get("metadata", {}).get("dependencies", {}).get("environment", {})
    if bound.get("environmentId") != env_guid:
        raise RuntimeError(f"binding not persisted; expected {env_guid}, got {bound}")
    print(f"  bound: {bound}")


def main() -> None:
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    # WSPL/DEP workspaces reject the legacy api.powerbi.com endpoint that
    # fabric-cicd 1.0 defaults to; force the Fabric endpoint.
    fabric_constants.DEFAULT_API_ROOT_URL = os.environ.get(
        "FABRIC_BASE_API_URL", "https://api.fabric.microsoft.com",
    )

    item_types = [s.strip() for s in os.environ.get(
        "FABRIC_ITEM_TYPES", "Environment,Notebook").split(",") if s.strip()]
    print(f"Item types in scope: {item_types}")

    if os.environ.get("FABRIC_DEBUG", "").lower() in ("1", "true", "yes"):
        change_log_level("DEBUG")

    _patch_clear_environment_yml()

    ws = FabricWorkspace(
        workspace_id=workspace_id,
        environment=os.environ.get("FABRIC_ENVIRONMENT", "PPE"),
        repository_directory=str(Path(__file__).resolve().parent / "workspace"),
        item_type_in_scope=item_types,
        token_credential=DefaultAzureCredential(),
    )

    publish_all_items(ws)
    _force_notebook_env_binding(ws, NOTEBOOK_NAME)
    unpublish_all_orphan_items(
        ws, item_name_exclude_regex=rf"^(?!{ENV_NAME}$|{NOTEBOOK_NAME}$).*",
    )


if __name__ == "__main__":
    main()
