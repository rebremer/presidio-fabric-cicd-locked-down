# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Apply (PUT) a workspace communication policy -- the set of outbound
"data connection rules" enforced on a WSPL/DEP-protected Fabric workspace.

API:  PUT {FABRIC_BASE_API_URL}/v1/workspaces/{FABRIC_WORKSPACE_ID}
          /networking/communicationPolicy

Required env:
  FABRIC_WORKSPACE_ID     workspace GUID
  FABRIC_BASE_API_URL     https://<wsid-no-dashes>.zfc.w.api.fabric.microsoft.com
  POLICY_FILE             path to the JSON policy document (defaults to
                          communication-policy/policy.json next to this script)

Auth: DefaultAzureCredential -> AzureCliCredential (federated session left
by AzureCLI@2 / WIF). No client secrets stored.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from azure.identity import DefaultAzureCredential

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def _strip_comments(doc: dict) -> dict:
    """Remove top-level `_comment` keys so the JSON file can carry inline docs."""
    return {k: v for k, v in doc.items() if k != "_comment"}


def main() -> int:
    workspace_id = os.environ["FABRIC_WORKSPACE_ID"]
    base_url = os.environ["FABRIC_BASE_API_URL"].rstrip("/")
    policy_path = Path(os.environ.get(
        "POLICY_FILE",
        str(Path(__file__).resolve().parent / "policy.json"),
    ))

    print(f"Workspace:   {workspace_id}")
    print(f"Base URL:    {base_url}")
    print(f"Policy file: {policy_path}")

    if not policy_path.is_file():
        print(f"ERROR: policy file not found: {policy_path}", file=sys.stderr)
        return 2

    policy = _strip_comments(json.loads(policy_path.read_text(encoding="utf-8")))
    print(f"Rules in policy: {len(policy.get('rules', []))} "
          f"(defaultAction={policy.get('defaultAction')})")

    token = DefaultAzureCredential().get_token(FABRIC_SCOPE).token
    url = f"{base_url}/v1/workspaces/{workspace_id}/networking/communicationPolicy"

    print(f"==> PUT {url}")
    resp = requests.put(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=policy,
        timeout=60,
    )
    print(f"    status={resp.status_code}")
    if resp.text:
        # Pretty-print JSON response when possible, otherwise raw.
        try:
            print(json.dumps(resp.json(), indent=2))
        except ValueError:
            print(resp.text)

    if resp.status_code >= 400:
        return 1

    # Read back to confirm what's actually applied.
    print(f"==> GET {url}")
    get_resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    print(f"    status={get_resp.status_code}")
    try:
        print(json.dumps(get_resp.json(), indent=2))
    except ValueError:
        print(get_resp.text)

    return 0 if get_resp.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
