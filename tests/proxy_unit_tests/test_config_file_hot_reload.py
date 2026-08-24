"""App-level proof for config-file hot reload (custom_auth + master key).

Boots the proxy with a real config.yaml and a real custom_auth.py script next
to it (the documented static-token-table pattern), then hot-reloads after
editing the files and asserts the request path picks up the new state without
a restart.

"auth passed" below means the request left the auth layer: with a fake
provider base URL the provider call then fails (5xx), while an auth failure
surfaces earlier (401 custom-auth / 400 built-in without DB).
"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.proxy_server import app, initialize

SCRIPT_V1 = """\
from fastapi import Request, HTTPException
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

STATIC_TOKENS = {
    "sk-user-alice-key-123": {"user_id": "alice", "max_budget": 100.0},
    "sk-user-bob-key-456": {"user_id": "bob", "max_budget": 100.0},
}

async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    if api_key == "sk-admin-reload":
        return UserAPIKeyAuth(
            api_key=api_key,
            user_id="admin",
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )
    if api_key in STATIC_TOKENS:
        user_info = STATIC_TOKENS[api_key]
        return UserAPIKeyAuth(
            api_key=api_key,
            user_id=user_info["user_id"],
            max_budget=user_info["max_budget"],
        )
    raise HTTPException(status_code=401, detail="Invalid Static API Key")
"""

SCRIPT_V2 = """\
from fastapi import Request, HTTPException
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

STATIC_TOKENS = {
    "sk-user-carol-key-789": {"user_id": "carol", "max_budget": 100.0},
}

async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    if api_key == "sk-admin-reload":
        return UserAPIKeyAuth(
            api_key=api_key,
            user_id="admin",
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )
    if api_key in STATIC_TOKENS:
        user_info = STATIC_TOKENS[api_key]
        return UserAPIKeyAuth(
            api_key=api_key,
            user_id=user_info["user_id"],
            max_budget=user_info["max_budget"],
        )
    raise HTTPException(status_code=401, detail="Invalid Static API Key")
"""

MODEL_LIST_YAML = """\
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: fake-key
      api_base: http://fake-provider.invalid
"""

ADMIN_HEADERS = {"Authorization": "Bearer sk-admin-reload"}


def _config_yaml(custom_auth: str | None, master_key: str | None = None) -> str:
    entries = []
    if custom_auth is not None:
        entries.append(f"  custom_auth: {custom_auth}")
    if master_key is not None:
        entries.append(f"  master_key: {master_key}")
    if not entries:
        entries.append("  pass_through_endpoints: []")
    return MODEL_LIST_YAML + "general_settings:\n" + "\n".join(entries) + "\n"


CHAT_BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


def _auth_passed(response) -> bool:
    """True when the request cleared the auth layer and reached the fake
    provider (which then fails)."""
    return response.status_code >= 500


def _script_path() -> str:
    import litellm.proxy.proxy_server as ps

    return os.path.join(os.path.dirname(ps.user_config_file_path), "custom_auth.py")


@pytest.fixture(scope="function")
def client_custom_auth(tmp_path, monkeypatch):
    config_path = os.path.join(tmp_path, "config.yaml")
    with open(os.path.join(tmp_path, "custom_auth.py"), "w") as f:
        f.write(SCRIPT_V1)
    with open(config_path, "w") as f:
        f.write(_config_yaml(custom_auth="custom_auth.user_api_key_auth"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    asyncio.run(initialize(config=config_path, debug=True))
    return TestClient(app)


@pytest.fixture(scope="function")
def client_master_key(tmp_path, monkeypatch):
    config_path = os.path.join(tmp_path, "config.yaml")
    with open(config_path, "w") as f:
        f.write(_config_yaml(custom_auth=None, master_key="mk-v1"))
    asyncio.run(initialize(config=config_path, debug=True))
    return TestClient(app), config_path


def test_custom_auth_script_hot_swap_without_restart(client_custom_auth):
    client = client_custom_auth

    # v1 token table: bob authenticates, unknown key rejected
    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-bob-key-456"},
        )
    )
    denied = client.post(
        "/chat/completions",
        json=CHAT_BODY,
        headers={"Authorization": "Bearer sk-user-unknown"},
    )
    assert denied.status_code == 401
    assert "Invalid Static API Key" in denied.text

    # reload is admin-gated
    forbidden = client.post(
        "/reload/config_file",
        headers={"Authorization": "Bearer sk-user-bob-key-456"},
    )
    assert forbidden.status_code == 403

    # edit the script file (YAML pointer unchanged) and reload via the
    # admin endpoint
    with open(_script_path(), "w") as f:
        f.write(SCRIPT_V2)
    reload_report = client.post("/reload/config_file", headers=ADMIN_HEADERS)
    assert reload_report.status_code == 200
    report = reload_report.json()
    assert report["reloaded"] is True
    assert report["applied_scripts"] == ["custom_auth"]
    assert report["fatal_error"] is None

    # v2 token table is live without a restart
    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-carol-key-789"},
        )
    )
    assert (
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-bob-key-456"},
        ).status_code
        == 401
    )

    # a broken script fails closed: the previous token table keeps working
    with open(_script_path(), "w") as f:
        f.write("def broken(:\n")
    broken_report = client.post("/reload/config_file", headers=ADMIN_HEADERS)
    assert broken_report.status_code == 200
    broken = broken_report.json()
    assert broken["reloaded"] is False
    assert broken["section_errors"]
    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-carol-key-789"},
        )
    )

    # fixing the script lets the retry succeed
    with open(_script_path(), "w") as f:
        f.write(
            SCRIPT_V2.replace("sk-user-carol-key-789", "sk-user-dave-key-000")
            .replace('"user_id": "carol"', '"user_id": "dave"')
        )
    fixed_report = client.post("/reload/config_file", headers=ADMIN_HEADERS)
    fixed = fixed_report.json()
    assert fixed["reloaded"] is True
    assert fixed["section_errors"] == []
    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-dave-key-000"},
        )
    )


def test_master_key_rotation_invalidates_cached_identity(client_master_key):
    client, config_path = client_master_key
    import litellm.proxy.proxy_server as ps

    assert _auth_passed(
        client.post("/chat/completions", json=CHAT_BODY, headers={"Authorization": "Bearer mk-v1"})
    )
    wrong = client.post(
        "/chat/completions",
        json=CHAT_BODY,
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert wrong.status_code in (400, 401)

    # simulate the cached identity a live proxy writes for the master key
    asyncio.run(
        ps.user_api_key_cache.async_set_cache(
            key=ps.hash_token("mk-v1"),
            value=UserAPIKeyAuth(api_key="mk-v1", user_id="admin"),
        )
    )

    # rotate the key in the YAML; the pre-rotation key still authenticates
    # the reload call itself
    with open(config_path, "w") as f:
        f.write(_config_yaml(custom_auth=None, master_key="mk-v2"))
    reload_report = client.post(
        "/reload/config_file",
        headers={"Authorization": "Bearer mk-v1"},
    )
    assert reload_report.status_code == 200
    report = reload_report.json()
    assert report["reloaded"] is True
    assert report["applied_general_settings"] == ["master_key"]

    # old key no longer authenticates (cached identity evicted) and the new
    # key does
    old = client.post(
        "/chat/completions",
        json=CHAT_BODY,
        headers={"Authorization": "Bearer mk-v1"},
    )
    assert old.status_code in (400, 401)
    assert ps.user_api_key_cache.get_cache(key=ps.hash_token("mk-v1")) is None
    assert _auth_passed(
        client.post("/chat/completions", json=CHAT_BODY, headers={"Authorization": "Bearer mk-v2"})
    )
