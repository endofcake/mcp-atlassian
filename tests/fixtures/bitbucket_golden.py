"""Loader for the golden Bitbucket DC fixtures.

See tests/fixtures/bitbucket/README.md for the corpus layout and the
synthetic-value invariants the bodies (``bb9``) satisfy.
"""

import json
from pathlib import Path
from typing import Any

GOLDEN_ROOT = Path(__file__).parent / "bitbucket"

# Editions carrying the pull-request-surface bodies (projects, repos, pr_*).
# One edition today; the tuple lets the parametrised suites take a second
# capture set by adding its name here.
PR_SURFACE_EDITIONS = ("bb9",)


def load_golden(edition: str, name: str) -> dict[str, Any]:
    """Return the parsed golden body for one fixture.

    Args:
        edition: The fixture set (``"bb9"``).
        name: The fixture name without extension (e.g. ``"branches"``).

    Returns:
        The parsed JSON body exactly as stored.
    """
    return json.loads((GOLDEN_ROOT / edition / f"{name}.json").read_text())
