"""Shared mock-response helpers for the Bitbucket client tests.

The client streams every response body and reads it through
``iter_content`` under a byte cap, so a mocked success response has to carry
its body as bytes as well as through ``.json()``. ``attach_json`` wires both
onto a ``MagicMock`` response in one place.
"""

import json
from typing import Any
from unittest.mock import MagicMock


def attach_body(response: MagicMock, raw: bytes) -> MagicMock:
    """Attach a raw body to a mock response for the streamed read path.

    Sets ``Content-Length`` (merging into an existing ``headers`` dict) and
    makes ``iter_content`` yield the body afresh on every call, so one mock can
    serve repeated requests.
    """
    headers = response.headers if isinstance(response.headers, dict) else {}
    headers["Content-Length"] = str(len(raw))
    response.headers = headers
    response.content = raw
    response.iter_content.side_effect = lambda *args, **kwargs: iter([raw])
    return response


def attach_json(response: MagicMock, body: Any) -> MagicMock:
    """Attach a JSON body to a mock response (both ``.json()`` and raw bytes)."""
    response.json.return_value = body
    return attach_body(response, json.dumps(body).encode())


def json_response(body: Any) -> MagicMock:
    """Build a successful mock response carrying a JSON body."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    return attach_json(response, body)


def no_content_response() -> MagicMock:
    """Build a 204 success: an empty body that must not be parsed as JSON."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = json.JSONDecodeError("no body", "", 0)
    return attach_body(response, b"")
