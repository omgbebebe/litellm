"""Read/append support for the static-token ``custom_auth`` script pattern.

When ``general_settings.custom_auth`` points at a local script whose token
table is a module-level ``STATIC_TOKENS = {...}`` dict literal (the
documented static-token pattern), the table can be provisioned through the
``POST /signup/{username}`` endpoint: the dict literal is located via AST,
a new token entry is appended with a minimal text insert (comments and
formatting elsewhere in the file are preserved), and the config-file
hot-reload machinery makes the new token live.
"""

from __future__ import annotations

import ast
import re
from typing import Final, cast

STATIC_TOKENS_NAME: Final = "STATIC_TOKENS"
USERNAME_KEY: Final = "user_id"

# charset-safe for embedding in a generated Python line and in a bearer token
_USERNAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_.@-]{1,64}")


class StaticTokenError(ValueError):
    """Base error for static-token script operations."""


class StaticTokensNotFound(StaticTokenError):
    """The script has no ``STATIC_TOKENS = {...}`` dict literal."""


class UsernameAlreadyRegistered(StaticTokenError):
    """The username already has an entry in the token table."""


def is_valid_signup_username(username: str) -> bool:
    return _USERNAME_PATTERN.fullmatch(username) is not None


def _locate_static_tokens(module: ast.Module) -> ast.Assign | None:
    """The top-level ``STATIC_TOKENS = {...}`` assignment with a dict
    literal value, or None."""
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == STATIC_TOKENS_NAME for t in node.targets):
            continue
        if isinstance(node.value, ast.Dict):
            return node
    return None


def _parse(source: str) -> ast.Assign:
    try:
        module: Final = ast.parse(source)
    except SyntaxError as e:
        raise StaticTokenError(f"custom_auth script is not valid Python: {e}") from e
    assign: Final = _locate_static_tokens(module)
    if assign is None:
        raise StaticTokensNotFound(f"no module-level '{STATIC_TOKENS_NAME} = {{...}}' dict literal found")
    return assign


def _entry_usernames(tokens: ast.Dict) -> tuple[str, ...]:
    """user_id values of the dict-literal entries, skipping entries whose
    shape does not match the pattern."""
    usernames: list[str] = []
    for value in tokens.values:
        if not isinstance(value, ast.Dict):
            continue
        for key, entry_value in zip(value.keys, value.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == USERNAME_KEY
                and isinstance(entry_value, ast.Constant)
                and isinstance(entry_value.value, str)
            ):
                usernames.append(entry_value.value)
    return tuple(usernames)


def static_token_usernames(script_source: str) -> tuple[str, ...]:
    """usernames (``user_id`` values) present in the script's token table."""
    value: Final = _parse(script_source).value
    if not isinstance(value, ast.Dict):
        raise StaticTokensNotFound(f"'{STATIC_TOKENS_NAME}' is not a dict literal")
    return _entry_usernames(value)


def _build_entry(tokens: ast.Dict, username: str) -> ast.Dict:
    """A new entry node mirroring the first existing entry's shape (extra
    fields like max_budget are copied), with user_id set to the new
    username; a default two-field shape when the table has no entries."""
    template = next((v for v in tokens.values if isinstance(v, ast.Dict)), None)
    if template is None:
        return ast.Dict(
            keys=[ast.Constant(value=USERNAME_KEY), ast.Constant(value="max_budget")],
            values=[ast.Constant(value=username), ast.Constant(value=100.0)],
        )
    entry: Final = ast.Dict(
        keys=[k for k in template.keys],  # type: ignore[arg-type]  # keys: list[expr | None] (None for ** unpacking, absent in this pattern)
        values=list(template.values),
    )
    for index, key in enumerate(entry.keys):
        if isinstance(key, ast.Constant) and key.value == USERNAME_KEY:
            entry.values[index] = ast.Constant(value=username)
            return entry
    entry.keys.insert(0, ast.Constant(value=USERNAME_KEY))
    entry.values.insert(0, ast.Constant(value=username))
    return entry


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def append_static_token(script_source: str, token: str, username: str) -> str:
    """Return the script source with a new ``token -> {user_id: username,
    ...}`` entry appended to the ``STATIC_TOKENS`` dict literal. Comments
    and formatting outside the inserted line are preserved. Raises
    UsernameAlreadyRegistered when the username has an entry already, and
    StaticTokenError when no dict literal exists or the script does not
    parse."""
    assign: Final = _parse(script_source)
    assign_value: Final = assign.value
    if not isinstance(assign_value, ast.Dict):
        raise StaticTokensNotFound(f"'{STATIC_TOKENS_NAME}' is not a dict literal")
    tokens = assign_value
    if username in _entry_usernames(tokens):
        raise UsernameAlreadyRegistered(f"username {username} already registered")
    entry: Final = _build_entry(tokens, username)
    entry_src: Final = ast.unparse(entry)

    lines: Final = script_source.splitlines(keepends=True)
    if not tokens.keys or assign.end_lineno == assign.lineno:
        # empty table or the whole assignment sits on one line: rewrite the
        # assignment's own line span (comments on other lines are untouched)
        tokens.keys.append(ast.Constant(value=token))
        tokens.values.append(entry)
        start: Final = assign.lineno - 1
        end: Final = assign.end_lineno
        rendered: Final = f"{STATIC_TOKENS_NAME} = {ast.unparse(tokens)}\n"
        return "".join(lines[:start]) + rendered + "".join(lines[end:])

    # multi-line table: insert a single line before the closing brace,
    # matching the indentation of the last entry line
    tokens.keys.append(ast.Constant(value=token))
    tokens.values.append(entry)
    close_index = cast("int", assign_value.end_lineno) - 1  # cast-ok: end_lineno is always set for parsed nodes
    indent: Final = _leading_whitespace(lines[close_index - 1])
    insert: Final = f'{indent}"{token}": {entry_src},\n'
    return "".join(lines[:close_index]) + insert + "".join(lines[close_index:])
