"""Verify the test suite never touches real credential storage.

The root ``conftest.py`` isolates the two stores ``OAuthConfig`` uses: the
keyring (forced to its no-op backend) and the on-disk fallback under
``~/.mcp-atlassian`` (redirected to a per-test temp home). These tests guard
both guarantees against regression, so the suite stays hermetic and never
reads a real user's tokens, leaves token files behind, or prompts for
keychain access.
"""

from pathlib import Path

import keyring
import keyring.backends.null


def test_null_keyring_backend_is_active() -> None:
    """The session runs against keyring's no-op backend."""
    assert isinstance(keyring.get_keyring(), keyring.backends.null.Keyring)


def test_keyring_reads_return_none() -> None:
    """Reading any credential yields None instead of hitting a real store."""
    assert keyring.get_password("any-service", "any-user") is None


def test_home_dir_is_redirected(tmp_path: Path) -> None:
    """The on-disk token fallback resolves to a temp home, not the real one."""
    assert Path.home() == tmp_path
