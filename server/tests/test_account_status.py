import asyncio
import os
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from radar import account_status as accounts
from radar.api import create_app
from radar.config import Settings


def cfg(provider="deepseek", **kwargs):
    return accounts.AccountConfig(id=provider, provider=provider, name=provider, **kwargs)


def deepseek(value="25.00", available=True):
    return {"is_available": available, "balance_infos": [{"currency": "CNY", "total_balance": value,
        "granted_balance": "5.00", "topped_up_balance": "20.00"}]}


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "secret", lambda name: "test-query-credential")
    path = tmp_path / "accounts.toml"
    path.write_text('refresh_seconds=300\n[[accounts]]\nid="deepseek"\nprovider="deepseek"\nname="DeepSeek"\n')
    return accounts.AccountMonitor(str(path), str(tmp_path / "accounts.db"))


def test_exact_money_no_missing_zero_and_cloud_scope():
    assert accounts.amount(None) is None
    assert accounts.amount(True) is None
    assert accounts.amount("NaN") is None
    assert accounts.amount("1e-100000000") is None
    assert accounts.amount("1,234.5678") == "1234.5678"
    assert accounts.deepseek_result(deepseek("0", False), cfg())["status"] == "exhausted"
    assert accounts.deepseek_result(deepseek("5"), cfg())["status"] == "low"
    with pytest.raises(accounts.AccountQueryError):
        accounts.deepseek_result(deepseek(None), cfg())
    multiple = deepseek()
    multiple["balance_infos"].append({"currency": "USD", "total_balance": "0"})
    assert accounts.deepseek_result(multiple, cfg())["status"] == "ok"
    result = accounts.bailian_result({"Success": True, "Data": {"Currency": "CNY", "AvailableAmount": "1000",
        "AvailableCashAmount": "200"}}, cfg("bailian"))
    assert result["available"] is None  # Cannot infer model access from cloud funds.
    assert result["balances"][0]["amount"] == "1000"
    assert result["balances"][0]["cash_amount"] == "200"


def test_codex_multiple_buckets_optional_credits_and_expired_window():
    def bucket(used, reset=2000):
        return {"primary": {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset},
            "credits": {"balance": "93.2318510000", "hasCredits": True, "unlimited": False}, "planType": "pro"}
    result = accounts.codex_result({"rateLimits": bucket(100), "rateLimitsByLimitId": {
        "codex": bucket(91), "other": bucket(0), "invalid": {"primary": {"usedPercent": None}}}}, cfg("codex"), now=1000)
    assert result["status"] == "low" and result["available"] is None
    assert len(result["limits"]) == 2
    assert result["limits"][0]["windows"][0]["remaining_percent"] == 9
    assert result["limits"][0]["credits"]["balance"] == "93.2318510000"
    assert "balances" in result and result["balances"] == []  # Credits are not USD.
    expired = accounts.codex_result({"rateLimits": bucket(100, reset=999)}, cfg("codex"), now=1000)
    assert expired["limits"][0]["windows"][0]["expired"]
    with pytest.raises(accounts.AccountQueryError):
        accounts.codex_result({"rateLimits": None}, cfg("codex"))


async def test_real_http_contract_and_private_errors(monitor, respx_mock):
    route = respx_mock.get("https://api.deepseek.com/user/balance").mock(return_value=httpx.Response(200, json=deepseek()))
    await monitor.refresh()
    row = monitor.report()["items"][0]
    assert row["status"] == "ok" and row["balances"][0]["amount"] == "25.00"
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-query-credential"
    assert monitor.path.stat().st_mode & 0o077 == 0
    await monitor.refresh(force=True)
    assert route.call_count == 1
    with monitor.connect() as db:
        db.execute("UPDATE accounts SET checked=checked-61,next_check=0")
    route.mock(return_value=httpx.Response(401, json={"error": "PRIVATE-CREDENTIAL-DETAIL"}))
    await monitor.refresh()
    row = monitor.report()["items"][0]
    assert row["status"] == "authorization_required" and row["stale"] and row["available"] is None
    assert row["balances"][0]["amount"] == "25.00" and row["last_success_at"]
    with monitor.connect() as db:
        dump = "".join(db.iterdump())
    assert "PRIVATE" not in dump and "test-query-credential" not in dump


async def test_failure_is_unknown_then_recovers_and_restart_does_not_spam(monitor, respx_mock):
    route = respx_mock.get("https://api.deepseek.com/user/balance").mock(side_effect=httpx.ReadTimeout("private"))
    await monitor.refresh()
    row = monitor.report()["items"][0]
    assert row["status"] == "query_failed" and row["balances"] == [] and row["available"] is None
    restarted = accounts.AccountMonitor()
    restarted.enabled, restarted.path, restarted.config = True, monitor.path, monitor.config
    await restarted.refresh()
    assert route.call_count == 1
    with monitor.connect() as db:
        db.execute("UPDATE accounts SET checked=checked-61,next_check=0")
    route.mock(return_value=httpx.Response(200, json=deepseek("15")))
    await restarted.refresh()
    assert restarted.report()["items"][0]["status"] == "ok"


async def test_cross_process_lease_and_changed_credentials(monitor, respx_mock, monkeypatch):
    account = monitor.config.accounts[0]
    owner = monitor.claim(account)
    assert owner and monitor.claim(account, True) is None
    with monitor.connect() as db:
        db.execute("UPDATE accounts SET lease=0")
    owner2 = monitor.claim(account)
    assert owner2 != owner
    monitor.save(account, owner, result=accounts.deepseek_result(deepseek("999"), account))
    assert monitor.report()["items"][0]["balances"] == []
    monitor.save(account, owner2, result=accounts.deepseek_result(deepseek(), account))
    monkeypatch.setattr(accounts, "secret", lambda _: "different-private-key")
    assert monitor.report()["items"][0]["balances"] == []
    respx_mock.get("https://api.deepseek.com/user/balance").mock(return_value=httpx.Response(200, json=deepseek("40")))
    await monitor.refresh()
    assert monitor.report()["items"][0]["balances"][0]["amount"] == "40"


async def test_codex_read_only_handshake_and_process_cleanup(tmp_path):
    script = tmp_path / "fake_codex.py"
    log = tmp_path / "methods"
    script.write_text('''import sys,json,time,os
with open(sys.argv[1],"w") as out:
 for line in sys.stdin:
  r=json.loads(line);out.write(r['method']+'\\n');out.flush()
  if r['method']=='initialize': print(json.dumps({'id':r['id'],'result':{}}),flush=True)
  if r['method']=='account/rateLimits/read':
   print(json.dumps({'id':r['id'],'result':{'rateLimits':{'primary':{'usedPercent':80,'windowDurationMins':300,'resetsAt':2000000000}}}}),flush=True)
''')
    result = await accounts.query_codex(cfg("codex", codex_command=[sys.executable, str(script), str(log)]))
    assert result["limits"][0]["windows"][0]["remaining_percent"] == 20
    assert log.read_text().splitlines() == ["initialize", "initialized", "account/rateLimits/read"]
    pidfile = tmp_path / "pid"
    script.write_text('import time,os,sys\nopen(sys.argv[1],"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await accounts.query_codex(cfg("codex", codex_command=[sys.executable, str(script), str(pidfile)]))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_account_api_auth_refresh_and_background_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "secret", lambda _: "")
    config = tmp_path / "radar.toml"
    config.write_text('[provider]\nkind="extractive"\n')
    ac = tmp_path / "accounts.toml"
    ac.write_text('[[accounts]]\nid="bailian"\nprovider="bailian"\nname="百炼"\n')
    settings = Settings(database_url="sqlite:///" + str(tmp_path / "radar.db"), config_path=str(config), reader_token="reader",
        accounts_config_path=str(ac), accounts_database_path=str(tmp_path / "account.db"), scheduler_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/accounts").status_code == 401
        assert client.post("/v1/accounts/refresh").status_code == 401
        headers = {"Authorization": "Bearer reader"}
        response = client.post("/v1/accounts/refresh", headers=headers)
        assert response.status_code == 200
        for _ in range(20):
            row = client.get("/v1/accounts", headers=headers).json()["items"][0]
            if row["status"] == "authorization_required":
                break
            time.sleep(0.01)
        assert row["status"] == "authorization_required" and row["balances"] == []


async def test_unsupported_provider_and_read_failure_never_zero(monitor):
    with pytest.raises(accounts.AccountQueryError, match="unsupported"):
        await accounts.query_account(cfg("openai"))
    a = monitor.config.accounts[0]
    owner = monitor.claim(a)
    monitor.save(a, owner, result={"status": "ok"})
    with monitor.connect() as db:
        db.execute("UPDATE accounts SET payload='broken-json'")
    row = monitor.report()["items"][0]
    assert row["status"] == "query_failed" and row["balances"] == []
