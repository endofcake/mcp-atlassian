"""Typed scalar readers shared by the Bitbucket Data Center models.

Every model reads its scalar fields through these helpers so a wrong-typed
value in an otherwise well-shaped object raises one descriptive ``ValueError``
at the model boundary. Without the check the value would reach the pydantic
constructor and surface to the caller as a multi-line validation dump.
"""

from typing import Any


def _shape_error(model: str, key: str, value: Any, expected: str) -> ValueError:
    """Build the response-shape error for a wrong-typed scalar."""
    return ValueError(
        "Bitbucket returned an unexpected response shape: "
        f"'{key}' in {model} is {type(value).__name__}, not {expected}."
    )


def _opt_str(data: dict[str, Any], key: str, *, model: str) -> str | None:
    """Read an optional string field.

    Args:
        data: The raw API object.
        key: The wire key to read.
        model: The model name, for the error message.

    Returns:
        The string, or None when the key is absent or null.

    Raises:
        ValueError: If the value is present and is not a string.
    """
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _shape_error(model, key, value, "a string")


def _opt_int(data: dict[str, Any], key: str, *, model: str) -> int | None:
    """Read an optional integer field; a boolean is rejected.

    Args:
        data: The raw API object.
        key: The wire key to read.
        model: The model name, for the error message.

    Returns:
        The integer, or None when the key is absent or null.

    Raises:
        ValueError: If the value is present and is not an integer.
    """
    value = data.get(key)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise _shape_error(model, key, value, "an integer")


def _opt_bool(data: dict[str, Any], key: str, *, model: str) -> bool | None:
    """Read an optional boolean field.

    Args:
        data: The raw API object.
        key: The wire key to read.
        model: The model name, for the error message.

    Returns:
        The boolean, or None when the key is absent or null.

    Raises:
        ValueError: If the value is present and is not a boolean.
    """
    value = data.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise _shape_error(model, key, value, "a boolean")


def _opt_list(data: dict[str, Any], key: str, *, model: str) -> list[Any]:
    """Read an optional collection of objects.

    A collection that is absent or null is empty, since the server omits it
    when there is nothing to report. A collection present with any other type, or
    holding an entry that is not an object, is reported as a shape error,
    because dropping entries would report a corrupt body as a complete one.

    Args:
        data: The raw API object.
        key: The wire key to read.
        model: The model name, for the error message.

    Returns:
        The list of raw entry objects (empty when the key is absent or null).

    Raises:
        ValueError: If the value is present and is not a list, or if any
            entry is not an object.
    """
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _shape_error(model, key, value, "a list")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(
                "Bitbucket returned an unexpected response shape: an entry of "
                f"'{key}' in {model} is {type(item).__name__}, not an object."
            )
    return value
