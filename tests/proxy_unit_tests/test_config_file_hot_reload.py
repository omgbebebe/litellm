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


SCRIPT_NO_ADMIN = """\
from fastapi import Request, HTTPException
from litellm.proxy._types import UserAPIKeyAuth

STATIC_TOKENS = {}

async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    if api_key in STATIC_TOKENS:
        return UserAPIKeyAuth(api_key=api_key, user_id=api_key)
    raise HTTPException(status_code=401, detail="Invalid Static API Key")
"""


def _no_admin_script(tokens: tuple[str, ...]) -> str:
    table = "\n".join(f'    "{t}": {{"user_id": "{t}"}},' for t in tokens)
    return SCRIPT_NO_ADMIN.replace("STATIC_TOKENS = {}", f"STATIC_TOKENS = {{\n{table}\n}}")


@pytest.fixture(scope="function")
def client_custom_auth_no_admin(tmp_path, monkeypatch):
    """The documented static-token setup with no database and no admin token
    in the table: neither the DB-gated poll nor the admin-gated
    /reload/config_file endpoint can trigger a reload — only the DB-less
    poll job can."""
    config_path = os.path.join(tmp_path, "config.yaml")
    with open(os.path.join(tmp_path, "custom_auth.py"), "w") as f:
        f.write(_no_admin_script(("sk-user-bob-key-456",)))
    with open(config_path, "w") as f:
        f.write(_config_yaml(custom_auth="custom_auth.user_api_key_auth"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    asyncio.run(initialize(config=config_path, debug=True))
    return TestClient(app), os.path.join(tmp_path, "custom_auth.py")


def test_dbless_custom_auth_picks_up_script_edit_via_poll(
    client_custom_auth_no_admin,
):
    """DB-less deployment: editing custom_auth.py is picked up by the
    scheduled check_config_file_reload poll without a restart and without
    any admin credential."""
    import litellm.proxy.proxy_server as ps

    client, script_path = client_custom_auth_no_admin

    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-bob-key-456"},
        )
    )

    with open(script_path, "w") as f:
        f.write(_no_admin_script(("sk-user-carol-key-789",)))

    # the exact coroutine the scheduler invokes on every poll tick
    outcome = asyncio.run(ps.proxy_config.check_config_file_reload())
    assert outcome.reloaded is True
    assert outcome.applied_scripts == ["custom_auth"]
    assert outcome.fatal_error is None

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


def test_dbless_startup_schedules_config_reload_poll(client_custom_auth_no_admin):
    """proxy_startup_event must register the config-file reload job even
    without a database, or the poll never ticks. Start and shutdown share
    one asyncio.run: the AsyncIO scheduler keeps a reference to the loop it
    started on."""
    import litellm.proxy.proxy_server as ps

    async def _run() -> None:
        await ps.ProxyStartupEvent._initialize_config_file_reload_job()
        try:
            assert ps.scheduler is not None
            job = ps.scheduler.get_job("config_file_reload_job")
            assert job is not None
            assert job.func == ps.proxy_config.check_config_file_reload
        finally:
            if ps.scheduler is not None:
                ps.scheduler.shutdown(wait=False)
                ps.scheduler = None

    asyncio.run(_run())


@pytest.fixture(scope="function")
def client_custom_auth_reload_interval(tmp_path, monkeypatch):
    config_path = os.path.join(tmp_path, "config.yaml")
    with open(os.path.join(tmp_path, "custom_auth.py"), "w") as f:
        f.write(_no_admin_script(("sk-user-bob-key-456",)))
    with open(config_path, "w") as f:
        f.write(
            _config_yaml(custom_auth="custom_auth.user_api_key_auth")
            + "  proxy_config_reload_interval_seconds: 7\n"
        )
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    asyncio.run(initialize(config=config_path, debug=True))
    return TestClient(app)


def test_reload_poll_interval_configurable_via_config_yaml(
    client_custom_auth_reload_interval,
):
    """general_settings.proxy_config_reload_interval_seconds in config.yaml
    flows through load_config into the DB-less reload poll's schedule."""
    from datetime import timedelta

    import litellm.proxy.proxy_server as ps

    assert ps.proxy_config_reload_interval_seconds == 7

    async def _run() -> None:
        await ps.ProxyStartupEvent._initialize_config_file_reload_job()
        try:
            assert ps.scheduler is not None
            job = ps.scheduler.get_job("config_file_reload_job")
            assert job is not None
            assert job.trigger.interval == timedelta(seconds=7)
        finally:
            if ps.scheduler is not None:
                ps.scheduler.shutdown(wait=False)
                ps.scheduler = None

    asyncio.run(_run())


@pytest.fixture(scope="function")
def client_importable_custom_auth(tmp_path, monkeypatch):
    """custom_auth module importable via sys.path (e.g. PYTHONPATH=/app in
    the Docker images), living away from the config file."""
    import sys

    script_dir = os.path.join(tmp_path, "auth_src")
    os.makedirs(script_dir, exist_ok=True)
    script_path = os.path.join(script_dir, "hot_reload_import_auth.py")
    with open(script_path, "w") as f:
        f.write(SCRIPT_V1)
    config_path = os.path.join(tmp_path, "config.yaml")
    with open(config_path, "w") as f:
        f.write(_config_yaml(custom_auth="hot_reload_import_auth.user_api_key_auth"))
    monkeypatch.syspath_prepend(script_dir)
    monkeypatch.delitem(sys.modules, "hot_reload_import_auth", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    asyncio.run(initialize(config=config_path, debug=True))
    return TestClient(app), script_path


def test_importable_custom_auth_script_hot_swap_without_restart(
    client_importable_custom_auth,
):
    """Editing the importable module's file (YAML pointer unchanged) triggers
    a fingerprint change and swaps the request-time auth handler, bypassing
    the sys.modules cache."""
    client, script_path = client_importable_custom_auth

    assert _auth_passed(
        client.post(
            "/chat/completions",
            json=CHAT_BODY,
            headers={"Authorization": "Bearer sk-user-bob-key-456"},
        )
    )

    with open(script_path, "w") as f:
        f.write(SCRIPT_V2)
    reload_report = client.post("/reload/config_file", headers=ADMIN_HEADERS)
    assert reload_report.status_code == 200
    report = reload_report.json()
    assert report["reloaded"] is True
    assert report["applied_scripts"] == ["custom_auth"]
    assert report["fatal_error"] is None

    # v2 token table is live; the v1-only key no longer authenticates
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
            SCRIPT_V2.replace("sk-user-carol-key-789", "sk-user-dave-key-000").replace(
                '"user_id": "carol"', '"user_id": "dave"'
            )
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

    assert _auth_passed(client.post("/chat/completions", json=CHAT_BODY, headers={"Authorization": "Bearer mk-v1"}))
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
    assert _auth_passed(client.post("/chat/completions", json=CHAT_BODY, headers={"Authorization": "Bearer mk-v2"}))
