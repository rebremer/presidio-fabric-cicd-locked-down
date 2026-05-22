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

Optional env:
  FABRIC_ADMIN_API_URL    base URL for the /networking/communicationPolicy call
                          only. Defaults to FABRIC_BASE_API_URL. Set to
                          https://api.fabric.microsoft.com if WSPL inbound
                          does not expose networking admin endpoints over the
                          private FQDN.

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
    admin_url = os.environ.get("FABRIC_ADMIN_API_URL", base_url).rstrip("/")
    policy_path = Path(os.environ.get(
        "POLICY_FILE",
        str(Path(__file__).resolve().parent / "policy.json"),
    ))

    print(f"Workspace:    {workspace_id}")
    print(f"Data URL:     {base_url}")
    print(f"Admin URL:    {admin_url}")
    print(f"Policy file:  {policy_path}")

    if not policy_path.is_file():
        print(f"ERROR: policy file not found: {policy_path}", file=sys.stderr)
        return 2

    policy = _strip_comments(json.loads(policy_path.read_text(encoding="utf-8")))
    inbound = (policy.get("inbound", {}).get("publicAccessRules", {})
               .get("defaultAction"))
    outbound = (policy.get("outbound", {}).get("publicAccessRules", {})
                .get("defaultAction"))
    print(f"Policy posture: inbound.defaultAction={inbound} "
          f"outbound.defaultAction={outbound}")

    token = DefaultAzureCredential().get_token(FABRIC_SCOPE).token
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Sanity: a known-working data-plane call. If this fails we have a
    #    general auth/network problem, not an endpoint-exposure problem.
    probe_url = f"{base_url}/v1/workspaces/{workspace_id}"
    print(f"==> GET {probe_url}  (auth/network probe)")
    probe = requests.get(probe_url, headers=headers, timeout=60)
    print(f"    status={probe.status_code}")
    if probe.status_code >= 400:
        print(probe.text)
        print("Auth/network probe failed; aborting before networking calls.")
        return 1

    policy_url = f"{admin_url}/v1/workspaces/{workspace_id}/networking/communicationPolicy"

    # 2. GET the existing policy. If this 403s with RequestDeniedByInboundPolicy
    #    while the probe above succeeded, the endpoint is simply not exposed
    #    over this base URL -- try FABRIC_ADMIN_API_URL=https://api.fabric.microsoft.com.
    print(f"==> GET {policy_url}  (current policy)")
    get_resp = requests.get(policy_url, headers=headers, timeout=60)
    print(f"    status={get_resp.status_code}")
    try:
        print(json.dumps(get_resp.json(), indent=2))
    except ValueError:
        print(get_resp.text)
    if get_resp.status_code >= 400:
        print("GET on /networking/communicationPolicy failed; not attempting PUT.")
        return 1

    # 3. PUT the desired policy.
    print(f"==> PUT {policy_url}")
    resp = requests.put(
        policy_url,
        headers={**headers, "Content-Type": "application/json"},
        json=policy,
        timeout=60,
    )
    print(f"    status={resp.status_code}")
    if resp.text:
        try:
            print(json.dumps(resp.json(), indent=2))
        except ValueError:
            print(resp.text)

    if resp.status_code >= 400:
        return 1

    # 4. Read back to confirm what was applied.
    print(f"==> GET {policy_url}  (verify)")
    verify = requests.get(policy_url, headers=headers, timeout=60)
    print(f"    status={verify.status_code}")
    try:
        print(json.dumps(verify.json(), indent=2))
    except ValueError:
        print(verify.text)

    return 0 if verify.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
