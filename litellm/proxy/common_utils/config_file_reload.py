"""Config file hot-reload support.

`ProxyConfig.check_config_file_reload` polls a fingerprint of the raw config
source (YAML bytes plus the bytes of every local script file the config
points at). When the fingerprint changes, the config is re-read, validated
with `ConfigYAML`, and the hot-safe sections are re-applied. Any failure
(fail-closed) keeps the previously applied state and the old fingerprint so
the next tick retries once the source is fixed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Mapping, Sequence
from typing import Final, cast

from litellm.proxy.types_utils.utils import resolve_local_script_path
from litellm.types.llms.base import LiteLLMPydanticObjectBase

# general_settings entries backed by a script (re-loaded on config change)
SCRIPT_SETTING_NAMES: Final[tuple[str, ...]] = (
    "custom_auth",
    "custom_key_generate",
    "custom_key_update",
    "custom_sso",
    "custom_ui_sso_sign_in_handler",
    "custom_team_metadata_validate",
)

_AUTH_CONTRACT_PARAMS: Final[frozenset[str]] = frozenset(("request", "api_key"))


class ConfigFileReloadOutcome(LiteLLMPydanticObjectBase):
    reloaded: bool = False
    fingerprint: str | None = None
    applied_scripts: list[str] = []  # mutable-ok: public pydantic response field for the /reload/config_file report
    cleared_scripts: list[str] = []  # mutable-ok: public pydantic response field for the /reload/config_file report
    applied_general_settings: list[str] = []  # mutable-ok: public pydantic response field for the /reload/config_file report
    restart_required: dict[str, str] = {}  # mutable-ok: public pydantic response field for the /reload/config_file report
    section_errors: list[str] = []  # mutable-ok: public pydantic response field for the /reload/config_file report
    fatal_error: str | None = None


def compute_source_fingerprint(
    file_paths: Sequence[str] = (), remote_refs: Sequence[str] = ()
) -> str:
    """Stable sha256 over the raw bytes of each file (in order) plus each
    remote reference string."""
    digest: Final = hashlib.sha256()
    for path in file_paths:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
    for ref in remote_refs:
        digest.update(b"\x00remote\x00")
        digest.update(ref.encode("utf-8"))
    return digest.hexdigest()


def compute_parsed_config_fingerprint(config: Mapping[str, object]) -> str:
    """Stable sha256 of a parsed (pre env-resolution) config mapping, used
    for remote (s3/gcs) config sources where raw bytes are not available."""
    canonical: Final = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _included_paths(config: Mapping[str, object], config_file_path: str) -> tuple[str, ...]:
    """Paths of the config's `include`d files, raising FileNotFoundError when
    one is missing."""
    include: Final = config.get("include")
    if not isinstance(include, (list, tuple)):
        return ()
    base_dir: Final = os.path.dirname(os.path.abspath(config_file_path))
    entries: Final = tuple(str(entry) for entry in cast("Sequence[object]", include))  # cast-ok: yaml include entries are scalar paths
    for entry in entries:
        if not os.path.isfile(os.path.join(base_dir, entry)):
            raise FileNotFoundError(f"config include file not found: {os.path.join(base_dir, entry)}")
    return tuple(os.path.join(base_dir, entry) for entry in entries)


def _script_refs(settings: Mapping[str, object], config_file_path: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(local script paths, remote script refs) for the script-backed
    general_settings entries."""
    refs: Final = tuple(
        ref
        for name in SCRIPT_SETTING_NAMES
        for ref in (settings.get(name),)
        if isinstance(ref, str)
    )
    remote: Final = tuple(ref for ref in refs if ref.startswith(("s3://", "gcs://")))
    local: Final = tuple(
        path
        for ref in refs
        if not ref.startswith(("s3://", "gcs://"))
        for path in (resolve_local_script_path(ref, config_file_path),)
        if path is not None
    )
    return local, remote


def collect_config_source_components(
    config: Mapping[str, object], config_file_path: str | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (local file paths, remote references) that make up the config
    source: the config file itself, its `include`d files, and the local
    script files referenced by `general_settings`. With `config_file_path=None`
    (remote s3/gcs config source) only remote references are collected. Raises
    `FileNotFoundError` when an included file is missing."""
    own_path: Final = (
        (os.path.abspath(config_file_path),) if config_file_path is not None else ()
    )
    includes: Final = (
        _included_paths(config, config_file_path) if config_file_path is not None else ()
    )
    general_settings: Final = config.get("general_settings")
    scripts: Final = (
        _script_refs(
            cast("Mapping[str, object]", general_settings),  # cast-ok: yaml general_settings is a flat mapping
            config_file_path,
        )
        if isinstance(general_settings, Mapping)
        else ((), ())
    )
    return own_path + includes + scripts[0], scripts[1]


def _signature_params(callable_: object) -> tuple[inspect.Parameter, ...] | None:
    """Signature parameters (minus self/cls), or None when not introspectable."""
    if not callable(callable_):
        return None
    try:
        sig: Final = inspect.signature(callable_)
    except (TypeError, ValueError):
        return None
    return tuple(
        p for p in sig.parameters.values() if p.name not in ("self", "cls")
    )


def _has_default(param: inspect.Parameter) -> bool:
    return (
        cast("object", param.default)  # cast-ok: Parameter.default is typed Any in typeshed
        is not inspect.Parameter.empty
    )


def validate_custom_auth_callable(callable_: object) -> None:
    """Contract for `custom_auth`: awaitable, called as
    `user_custom_auth(request=request, api_key=api_key)`."""
    if not callable(callable_):
        raise ValueError("custom_auth must be callable")
    params: Final = _signature_params(callable_)
    if params is None:
        return
    has_var_keyword: Final = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    accepted: Final = frozenset(
        p.name
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    )
    if not has_var_keyword and not _AUTH_CONTRACT_PARAMS <= accepted:
        raise ValueError(
            "custom_auth must accept 'request' and 'api_key' arguments; "
            f"got {sorted(accepted)}"
        )


def _required_arg_counts(callable_: object) -> tuple[int, int] | None:
    """(required positional-or-keyword count, required keyword-only count) for
    a call; None when not introspectable. Stops at *args, which absorbs the
    remaining positional arguments. **kwargs cannot satisfy a required
    keyword-only parameter."""
    params: Final = _signature_params(callable_)
    if params is None:
        return None
    var_positional_index: Final = next(
        (
            index
            for index, p in enumerate(params)
            if p.kind is inspect.Parameter.VAR_POSITIONAL
        ),
        len(params),
    )
    positional_prefix: Final = params[:var_positional_index]
    required_positional: Final = sum(
        1
        for p in positional_prefix
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and not _has_default(p)
    )
    required_keyword: Final = sum(
        1
        for p in params
        if p.kind is inspect.Parameter.KEYWORD_ONLY and not _has_default(p)
    )
    return required_positional, required_keyword


def _accepts_positional_arg(callable_: object) -> bool:
    """True when a single positional argument can be passed to the callable."""
    params: Final = _signature_params(callable_)
    if params is None:
        return True
    for p in params:
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        return p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
    return False


def _validate_single_request_coroutine(callable_: object, setting_name: str) -> None:
    """Shared contract for `custom_key_generate`/`custom_key_update`: a
    coroutine function called with a single request argument (the callers
    raise ValueError at request time for anything else)."""
    if not inspect.iscoroutinefunction(callable_):
        raise ValueError(f"{setting_name} must be a coroutine function")
    required: Final = _required_arg_counts(callable_)
    if required is not None and (required[0] > 1 or required[1] > 0):
        raise ValueError(
            f"{setting_name} takes exactly one positional argument; "
            "other required arguments found"
        )
    if not _accepts_positional_arg(callable_):
        raise ValueError(f"{setting_name} must accept a single request argument")


def validate_custom_key_generate_callable(callable_: object) -> None:
    """Contract for `custom_key_generate`: coroutine function called with the
    key-generation request."""
    _validate_single_request_coroutine(callable_, "custom_key_generate")


def validate_custom_key_update_callable(callable_: object) -> None:
    """Contract for `custom_key_update`: coroutine function called with the
    key-update request."""
    _validate_single_request_coroutine(callable_, "custom_key_update")
