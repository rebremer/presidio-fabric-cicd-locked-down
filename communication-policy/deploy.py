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

Auth: shells out to `az account get-access-token` (the AzureCLI@2 task on
the agent has already established the federated/WIF session). This avoids
needing `azure-identity` and `requests` as pip dependencies, so the script
runs under the restrictive NSG with only AzureDevOps / AAD / ARM service
tags allowed (no PyPI egress required).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"


def _strip_comments(doc: dict) -> dict:
    """Remove top-level `_comment` keys so the JSON file can carry inline docs."""
    return {k: v for k, v in doc.items() if k != "_comment"}


def _get_token() -> str:
    """Fetch a Fabric access token via the Azure CLI (already authenticated)."""
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["accessToken"]


def _request(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, str]:
    """Perform an HTTPS request with a bearer token, returning (status, body)."""
    headers = {"Authorization": f"Bearer {token}"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _print_body(text: str) -> None:
    try:
        print(json.dumps(json.loads(text), indent=2))
    except ValueError:
        print(text)


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

    token = _get_token()

    # 1. Sanity: a known-working data-plane call. If this fails we have a
    #    general auth/network problem, not an endpoint-exposure problem.
    probe_url = f"{base_url}/v1/workspaces/{workspace_id}"
    print(f"==> GET {probe_url}  (auth/network probe)")
    status, body = _request("GET", probe_url, token)
    print(f"    status={status}")
    if status >= 400:
        _print_body(body)
        print("Auth/network probe failed; aborting before networking calls.")
        return 1

    policy_url = f"{admin_url}/v1/workspaces/{workspace_id}/networking/communicationPolicy"

    # 2. GET the existing policy. If this 403s with RequestDeniedByInboundPolicy
    #    while the probe above succeeded, the endpoint is simply not exposed
    #    over this base URL -- try FABRIC_ADMIN_API_URL=https://api.fabric.microsoft.com.
    print(f"==> GET {policy_url}  (current policy)")
    status, body = _request("GET", policy_url, token)
    print(f"    status={status}")
    _print_body(body)
    if status >= 400:
        print("GET on /networking/communicationPolicy failed; not attempting PUT.")
        return 1

    if os.environ.get("DISCOVER_ONLY", "").lower() in ("1", "true", "yes"):
        print("DISCOVER_ONLY set -- skipping PUT (use the GET output above "
              "to populate policy.json, then unset DISCOVER_ONLY).")
        return 0

    # 3. PUT the desired policy.
    print(f"==> PUT {policy_url}")
    status, body = _request("PUT", policy_url, token, body=policy)
    print(f"    status={status}")
    if body:
        _print_body(body)

    if status >= 400:
        return 1

    # 4. Read back to confirm what was applied.
    print(f"==> GET {policy_url}  (verify)")
    status, body = _request("GET", policy_url, token)
    print(f"    status={status}")
    _print_body(body)

    return 0 if status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
