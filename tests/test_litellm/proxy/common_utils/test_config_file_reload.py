"""Tests for litellm/proxy/common_utils/config_file_reload.py (fingerprinting
and script contract validation for config-file hot reload)."""

from __future__ import annotations

import textwrap

import pytest

from litellm.proxy.common_utils.config_file_reload import (
    SCRIPT_SETTING_NAMES,
    ConfigFileReloadOutcome,
    collect_config_source_components,
    compute_parsed_config_fingerprint,
    compute_source_fingerprint,
    validate_custom_auth_callable,
    validate_custom_key_generate_callable,
    validate_custom_key_update_callable,
)
from litellm.proxy.types_utils.utils import resolve_local_script_path


# ---------------------------------------------------------------------------
# resolve_local_script_path
# ---------------------------------------------------------------------------


def test_resolve_local_script_path_resolves_file_next_to_config(tmp_path):
    script = tmp_path / "custom_auth.py"
    script.write_text("")
    config = tmp_path / "config.yaml"
    config.write_text("")

    assert resolve_local_script_path("custom_auth.user_api_key_auth", str(config)) == str(
        script
    )


def test_resolve_local_script_path_nested_module(tmp_path):
    nested = tmp_path / "my"
    nested.mkdir()
    nested.joinpath("script.py").write_text("")
    config = tmp_path / "config.yaml"
    config.write_text("")

    assert resolve_local_script_path("my.script.fn", str(config)) == str(
        nested / "script.py"
    )


@pytest.mark.parametrize(
    ("value", "config_file_path"),
    [
        ("custom_auth.user_api_key_auth", None),
        ("s3://bucket/key.py", "/some/config.yaml"),
        ("gcs://bucket/key.py", "/some/config.yaml"),
        ("no_dot_string", "/some/config.yaml"),
    ],
)
def test_resolve_local_script_path_returns_none_when_not_local(
    value, config_file_path
):
    assert resolve_local_script_path(value, config_file_path) is None


def test_resolve_local_script_path_returns_none_when_file_missing(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("")
    assert resolve_local_script_path("missing.module.fn", str(config)) is None


# ---------------------------------------------------------------------------
# compute_source_fingerprint
# ---------------------------------------------------------------------------


def test_compute_source_fingerprint_stable_and_sensitive(tmp_path):
    f1 = tmp_path / "a.yaml"
    f1.write_text("a: 1\n")
    f2 = tmp_path / "b.yaml"
    f2.write_text("b: 2\n")

    first = compute_source_fingerprint(file_paths=[str(f1), str(f2)])
    assert first == compute_source_fingerprint(file_paths=[str(f1), str(f2)])

    f1.write_text("a: 1  # comment\n")
    assert first != compute_source_fingerprint(file_paths=[str(f1), str(f2)])

    # order is part of the identity (main config is always hashed first)
    assert compute_source_fingerprint(file_paths=[str(f2), str(f1)]) != first


def test_compute_source_fingerprint_includes_remote_refs(tmp_path):
    f1 = tmp_path / "a.yaml"
    f1.write_text("a: 1\n")

    assert (
        compute_source_fingerprint(file_paths=[str(f1)], remote_refs=["s3://b/m.f"])
        != compute_source_fingerprint(file_paths=[str(f1)], remote_refs=["s3://b/m.g"])
    )
    assert (
        compute_source_fingerprint(file_paths=[str(f1)])
        != compute_source_fingerprint(file_paths=[str(f1)], remote_refs=["s3://b/m.f"])
    )


def test_compute_source_fingerprint_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        compute_source_fingerprint(file_paths=[str(tmp_path / "missing.yaml")])


# ---------------------------------------------------------------------------
# compute_parsed_config_fingerprint
# ---------------------------------------------------------------------------


def test_parsed_config_fingerprint_order_insensitive_and_value_sensitive():
    a = compute_parsed_config_fingerprint({"general_settings": {"x": 1}, "model_list": []})
    b = compute_parsed_config_fingerprint({"model_list": [], "general_settings": {"x": 1}})
    c = compute_parsed_config_fingerprint({"general_settings": {"x": 2}, "model_list": []})
    assert a == b
    assert a != c


def test_parsed_config_fingerprint_handles_non_serializable_values():
    class Marker:
        def __str__(self) -> str:
            return "marker"

    assert compute_parsed_config_fingerprint({"k": Marker()}) == compute_parsed_config_fingerprint(
        {"k": "marker"}
    )


# ---------------------------------------------------------------------------
# collect_config_source_components
# ---------------------------------------------------------------------------


def test_collect_components_file_mode(tmp_path):
    main = tmp_path / "config.yaml"
    included = tmp_path / "models.yaml"
    script = tmp_path / "custom_auth.py"
    main.write_text(
        textwrap.dedent(
            """\
            include:
              - models.yaml
            general_settings:
              custom_auth: custom_auth.user_api_key_auth
            """
        )
    )
    included.write_text("model_list: []\n")
    script.write_text("")

    files, refs = collect_config_source_components(
        config={"include": ["models.yaml"], "general_settings": {"custom_auth": "custom_auth.user_api_key_auth"}},
        config_file_path=str(main),
    )

    assert str(main) in files[0]
    assert str(included) in files
    assert str(script) in files
    assert refs == ()


def test_collect_components_missing_include_raises(tmp_path):
    main = tmp_path / "config.yaml"
    main.write_text("include:\n  - missing.yaml\n")

    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        collect_config_source_components(
            config={"include": ["missing.yaml"]}, config_file_path=str(main)
        )


def test_collect_components_remote_script_refs(tmp_path):
    main = tmp_path / "config.yaml"
    main.write_text("")

    files, refs = collect_config_source_components(
        config={
            "general_settings": {
                "custom_auth": "s3://bucket/auth.py.handler",
                "custom_sso": "gcs://bucket/sso.py.SSO",
            }
        },
        config_file_path=str(main),
    )
    assert refs == ("s3://bucket/auth.py.handler", "gcs://bucket/sso.py.SSO")
    assert len(files) == 1


def test_collect_components_ignores_importable_scripts(tmp_path):
    main = tmp_path / "config.yaml"
    main.write_text("")

    files, refs = collect_config_source_components(
        config={"general_settings": {"custom_auth": "installed_package.handler"}},
        config_file_path=str(main),
    )
    assert refs == ()
    assert len(files) == 1


def test_collect_components_remote_mode_ignores_local_paths():
    files, refs = collect_config_source_components(
        config={
            "include": ["models.yaml"],
            "general_settings": {"custom_auth": "s3://bucket/auth.py.handler"},
        },
        config_file_path=None,
    )
    assert files == ()
    assert refs == ("s3://bucket/auth.py.handler",)


def test_collect_components_tolerates_non_mapping_general_settings():
    files, refs = collect_config_source_components(
        config={"general_settings": "not-a-dict"}, config_file_path="/x/config.yaml"
    )
    assert files == ("/x/config.yaml",)
    assert refs == ()


def test_script_setting_names_covers_reloadable_script_keys():
    assert tuple(SCRIPT_SETTING_NAMES) == (
        "custom_auth",
        "custom_key_generate",
        "custom_key_update",
        "custom_sso",
        "custom_ui_sso_sign_in_handler",
        "custom_team_metadata_validate",
    )


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", [object(), 42, "not callable", None])
def test_validators_reject_non_callable(fn):
    with pytest.raises(ValueError):
        validate_custom_auth_callable(fn)
    with pytest.raises(ValueError):
        validate_custom_key_generate_callable(fn)
    with pytest.raises(ValueError):
        validate_custom_key_update_callable(fn)


def test_validate_custom_auth_callable_accepts_contract_signatures():
    async def by_name(request, api_key):
        return None

    async def reordered(api_key, request, extra=None):
        return None

    def var_keyword(**kwargs):
        return None

    for fn in (by_name, reordered, var_keyword):
        validate_custom_auth_callable(fn)


def test_custom_auth_methods_bound_to_class_instances_pass():
    class AuthHandler:
        async def __call__(self, request, api_key):  # type: ignore[no-untyped-def]
            return None

    validate_custom_auth_callable(AuthHandler())


def test_validate_custom_auth_callable_rejects_wrong_signature():
    async def wrong(token):
        return None

    with pytest.raises(ValueError, match="request"):
        validate_custom_auth_callable(wrong)


def test_validate_custom_key_generate_requires_coroutine_and_one_arg():
    async def ok(request):
        return {"decision": True}

    def sync(request):
        return {"decision": True}

    async def two_args(a, b):
        return None

    validate_custom_key_generate_callable(ok)
    with pytest.raises(ValueError, match="coroutine"):
        validate_custom_key_generate_callable(sync)
    with pytest.raises(ValueError, match="required"):
        validate_custom_key_generate_callable(two_args)


def test_validate_custom_key_update_requires_coroutine_and_one_arg():
    async def ok(request):
        return {"decision": True}

    async def no_args():
        return None

    async def kw_only_only(*, request):
        return None

    validate_custom_key_update_callable(ok)
    with pytest.raises(ValueError):
        validate_custom_key_update_callable(no_args)
    with pytest.raises(ValueError):
        validate_custom_key_update_callable(kw_only_only)


# ---------------------------------------------------------------------------
# outcome model
# ---------------------------------------------------------------------------


def test_config_file_reload_outcome_defaults_and_dump():
    outcome = ConfigFileReloadOutcome()
    dumped = outcome.model_dump()
    assert dumped == {
        "reloaded": False,
        "fingerprint": None,
        "applied_scripts": [],
        "cleared_scripts": [],
        "applied_general_settings": [],
        "restart_required": {},
        "section_errors": [],
        "fatal_error": None,
    }
