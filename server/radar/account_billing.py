"""Isolated official Alibaba BSS SDK. Only QueryAccountBalance is permitted here."""

import asyncio
import json
import os


async def main():
    from alibabacloud_bssopenapi20171214.client import Client
    from alibabacloud_tea_openapi.models import Config
    from alibabacloud_tea_util.models import RuntimeOptions

    client = Client(Config(access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        security_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
        endpoint="business.aliyuncs.com", protocol="https", connect_timeout=5000, read_timeout=10000))
    try:
        result = await client.query_account_balance_with_options_async(RuntimeOptions(autoretry=False))
        body = result.body.to_map()
        data = body.get("Data") or {}
        print(json.dumps({"Success": body.get("Success"), "Data": {k: data.get(k) for k in
            ("Currency", "AvailableAmount", "AvailableCashAmount")}}))
    except Exception as exc:
        code = str(getattr(exc, "code", ""))
        denied = any(s in code.lower() for s in ("accesskey", "signature", "permission", "authoriz", "securitytoken"))
        print(json.dumps({"error": "authorization_required" if denied else "query_failed"}))


if __name__ == "__main__":
    asyncio.run(main())
