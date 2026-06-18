#!/usr/bin/env python3
"""Inject a pre-obtained access token into az CLI's MSAL cache.

Usage (in CI):
  Set env vars AZ_ACCESS_TOKEN, AZ_TENANT_ID, AZ_SUB_ID, then run this script.
  After running, `az account get-access-token` will return the injected token.

The token is valid for ~1 hour. Obtain fresh token locally with:
  az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
"""

import json
import os
import sys
import base64
import time
from pathlib import Path


def decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification (just to extract claims)."""
    parts = token.split(".")
    if len(parts) != 3:
        sys.exit("ERROR: AZ_ACCESS_TOKEN is not a valid JWT (expected 3 parts)")
    payload = parts[1]
    # Add padding
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main():
    token = os.environ.get("AZ_ACCESS_TOKEN", "").strip()
    tenant_id = os.environ.get("AZ_TENANT_ID", "").strip()
    sub_id = os.environ.get("AZ_SUB_ID", "").strip()

    if not token:
        sys.exit("ERROR: AZ_ACCESS_TOKEN env var is empty")
    if not tenant_id:
        sys.exit("ERROR: AZ_TENANT_ID env var is empty")
    if not sub_id:
        sys.exit("ERROR: AZ_SUB_ID env var is empty")

    # Extract oid and upn from token
    claims = decode_jwt_payload(token)
    oid = claims.get("oid", "unknown")
    upn = claims.get("upn", claims.get("unique_name", "user@unknown"))
    exp = claims.get("exp", int(time.time()) + 3600)

    print(f"Token subject: {upn}")
    print(f"Token expires: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(exp))} UTC")
    remaining = exp - int(time.time())
    print(f"Time remaining: {remaining // 60}m {remaining % 60}s")
    if remaining < 600:
        print("WARNING: Token expires in less than 10 minutes!")

    # az CLI client ID (well-known)
    az_client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
    home_account_id = f"{oid}.{tenant_id}"
    now = str(int(time.time()))

    # Construct MSAL token cache
    cache_key = f"{home_account_id}-login.microsoftonline.com-accesstoken-{az_client_id}-{tenant_id}-https://management.azure.com//.default"
    account_key = f"{home_account_id}-login.microsoftonline.com-{tenant_id}"

    msal_cache = {
        "AccessToken": {
            cache_key: {
                "credential_type": "AccessToken",
                "secret": token,
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "realm": tenant_id,
                "target": "https://management.azure.com//.default",
                "client_id": az_client_id,
                "cached_at": now,
                "expires_on": str(exp),
                "extended_expires_on": str(exp),
            }
        },
        "Account": {
            account_key: {
                "home_account_id": home_account_id,
                "environment": "login.microsoftonline.com",
                "realm": tenant_id,
                "local_account_id": oid,
                "username": upn,
                "authority_type": "MSSTS",
            }
        },
        "RefreshToken": {},
        "IdToken": {},
        "AppMetadata": {},
    }

    # Construct azureProfile.json
    profile = {
        "installationId": "e2e-ci-test",
        "subscriptions": [
            {
                "id": sub_id,
                "name": "E2E Test Subscription",
                "state": "Enabled",
                "tenantId": tenant_id,
                "user": {"name": upn, "type": "user"},
                "isDefault": True,
                "environmentName": "AzureCloud",
                "homeTenantId": tenant_id,
                "managedByTenants": [],
            }
        ],
    }

    # Write files
    azure_dir = Path.home() / ".azure"
    azure_dir.mkdir(parents=True, exist_ok=True)

    cache_path = azure_dir / "msal_token_cache.json"
    cache_path.write_text(json.dumps(msal_cache, indent=2))
    cache_path.chmod(0o600)

    profile_path = azure_dir / "azureProfile.json"
    profile_path.write_text(json.dumps(profile, indent=2))

    print(f"Wrote {cache_path} ({cache_path.stat().st_size} bytes)")
    print(f"Wrote {profile_path} ({profile_path.stat().st_size} bytes)")
    print("az CLI token injection complete.")


if __name__ == "__main__":
    main()
