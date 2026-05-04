# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Deploy the Presidio Fabric Environment to the `testpub` workspace
using the fabric-cicd library (https://microsoft.github.io/fabric-cicd/0.1.3/).

Run from an Azure DevOps agent that has line-of-sight to the (DEP-protected)
Fabric workspace, after `pip install -r requirements-deploy.txt`.

Required environment variables:
  FABRIC_WORKSPACE_ID   GUID of the target workspace (e.g. 'testpub')
  FABRIC_ENVIRONMENT    Logical environment name used in parameter.yml (e.g. 'PPE')

Optional (for SPN auth in Azure DevOps):
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
  -> picked up automatically by DefaultAzureCredential / EnvironmentCredential.
"""

from __future__ import annotations

import os
from pathlib import Path

from azure.identity import DefaultAzureCredential

from fabric_cicd import (
    FabricWorkspace,
    change_log_level,
    publish_all_items,
    unpublish_all_orphan_items,
)
from fabric_cicd import constants as fabric_constants


def main() -> None:
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    environment = os.environ.get("FABRIC_ENVIRONMENT", "PPE")
    # Override the API endpoint when the workspace blocks public inbound
    # traffic (DEP / Private Link). Defaults to the public Power BI URL.
    base_api_url = os.environ.get("FABRIC_BASE_API_URL")
    if base_api_url:
        fabric_constants.DEFAULT_API_ROOT_URL = base_api_url

    # Point fabric-cicd at the workspace folder that contains the
    # source-controlled Fabric items. Folder layout mirrors what the
    # Fabric Source Control UI produces.
    repository_directory = str(Path(__file__).resolve().parent / "workspace")

    item_type_in_scope = ["Environment", "Notebook"]

    if os.environ.get("FABRIC_DEBUG", "").lower() in ("1", "true", "yes"):
        change_log_level("DEBUG")

    target_workspace = FabricWorkspace(
        workspace_id=workspace_id,
        environment=environment,
        repository_directory=repository_directory,
        item_type_in_scope=item_type_in_scope,
        token_credential=DefaultAzureCredential(),
    )

    publish_all_items(target_workspace)

    # Only unpublish items that we own (safety guard:
    # do not touch unrelated items if scope is widened later).
    unpublish_all_orphan_items(
        target_workspace,
        item_name_exclude_regex=r"^(?!Presidio$|PresidioSmokeTest$).*",
    )


if __name__ == "__main__":
    main()
