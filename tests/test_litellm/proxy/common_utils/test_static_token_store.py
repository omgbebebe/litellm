"""Tests for litellm/proxy/common_utils/static_token_store.py (AST
read/append of the STATIC_TOKENS dict in a custom_auth script)."""

from __future__ import annotations

import ast
import textwrap

import pytest

from litellm.proxy.common_utils.static_token_store import (
    StaticTokenError,
    StaticTokensNotFound,
    UsernameAlreadyRegistered,
    append_static_token,
    is_valid_signup_username,
    static_token_usernames,
)

SCRIPT = textwrap.dedent(
    """\
    from fastapi import Request, HTTPException

    # Your hardcoded, static mapping of keys to users
    STATIC_TOKENS = {
        "sk-user-alice-key-123": {"user_id": "alice", "max_budget": 100.0},
        "sk-user-bob-key-456": {"user_id": "bob", "max_budget": 100.0},
    }

    async def user_api_key_auth(request, api_key):
        raise HTTPException(status_code=401)
    """
)


# ---------------------------------------------------------------------------
# is_valid_signup_username
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username",
    ["alice", "bob_2", "carol.d", "d-4", "e@f", "g" * 64],
)
def test_valid_usernames(username):
    assert is_valid_signup_username(username)


@pytest.mark.parametrize(
    "username",
    ["", "x" * 65, "has space", "sl/ash", "new\nline", "col:on", "quote'in", 'dq"ote'],
)
def test_invalid_usernames(username):
    assert not is_valid_signup_username(username)


# ---------------------------------------------------------------------------
# static_token_usernames
# ---------------------------------------------------------------------------


def test_usernames_extracted_from_dict_literal():
    assert static_token_usernames(SCRIPT) == ("alice", "bob")


def test_usernames_empty_table():
    assert static_token_usernames("STATIC_TOKENS = {}\n") == ()


def test_usernames_skips_non_pattern_entries():
    source = textwrap.dedent(
        """\
        STATIC_TOKENS = {
            "t1": {"user_id": "alice"},
            "t2": "not-a-dict",
            "t3": {"other": "bob"},
            "t4": {1: "computed-key"},
        }
        """
    )
    assert static_token_usernames(source) == ("alice",)


def test_missing_dict_literal_raises():
    with pytest.raises(StaticTokensNotFound):
        static_token_usernames("def f():\n    pass\n")


def test_syntax_error_raises():
    with pytest.raises(StaticTokenError, match="not valid Python"):
        static_token_usernames("def broken(:\n")


def test_non_dict_assignment_raises():
    with pytest.raises(StaticTokensNotFound):
        static_token_usernames("STATIC_TOKENS = dict()\n")


# ---------------------------------------------------------------------------
# append_static_token
# ---------------------------------------------------------------------------


def test_append_preserves_comments_and_existing_lines():
    updated = append_static_token(SCRIPT, token="sk-carol-new", username="carol")
    # comments and untouched lines are byte-identical
    assert "# Your hardcoded, static mapping of keys to users" in updated
    assert '"sk-user-alice-key-123": {"user_id": "alice", "max_budget": 100.0},' in updated
    # the new entry landed before the closing brace with matching indent
    assert '"sk-carol-new": ' in updated
    assert updated.index("sk-user-bob-key-456") < updated.index("sk-carol-new")
    assert ast.parse(updated)  # still valid python


def test_append_mirrors_first_entry_shape():
    updated = append_static_token(SCRIPT, token="sk-carol-new", username="carol")
    module = ast.parse(updated)
    tokens = next(
        n.value
        for n in module.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "STATIC_TOKENS" for t in n.targets)
    )
    entry = tokens.values[-1]
    assert isinstance(entry, ast.Dict)
    fields = {k.value: v.value for k, v in zip(entry.keys, entry.values)}
    assert fields == {"user_id": "carol", "max_budget": 100.0}


def test_append_existing_username_raises():
    with pytest.raises(UsernameAlreadyRegistered, match="username alice already registered"):
        append_static_token(SCRIPT, token="sk-x", username="alice")


def test_append_to_empty_table():
    source = "# table lives here\nSTATIC_TOKENS = {}\n\nasync def auth(request, api_key):\n    pass\n"
    updated = append_static_token(source, token="sk-dave-new", username="dave")
    assert "sk-dave-new" in updated
    assert "# table lives here" in updated
    usernames = static_token_usernames(updated)
    assert usernames == ("dave",)


def test_append_to_one_line_dict():
    source = 'STATIC_TOKENS = {"t1": {"user_id": "alice"}}\n'
    updated = append_static_token(source, token="sk-bob-new", username="bob")
    assert static_token_usernames(updated) == ("alice", "bob")


def test_append_default_entry_shape_for_non_pattern_table():
    source = "STATIC_TOKENS = {\n    'k1': 'not-a-dict',\n}\n"
    updated = append_static_token(source, token="sk-eve-new", username="eve")
    assert static_token_usernames(updated) == ("eve",)


def test_append_inserts_with_entry_indentation():
    updated = append_static_token(SCRIPT, token="sk-carol-new", username="carol")
    inserted = next(line for line in updated.splitlines() if "sk-carol-new" in line)
    assert inserted.startswith(" " * 4 + '"sk-carol-new"')  # 4-space indent like the entries
