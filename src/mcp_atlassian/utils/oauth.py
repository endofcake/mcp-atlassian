"""OAuth 2.0 utilities for Atlassian Cloud and Data Center authentication.

This module provides utilities for OAuth 2.0 (3LO) authentication with Atlassian.
It handles:
- OAuth configuration for both Cloud and Data Center
- Token acquisition, storage, and refresh
- Session configuration for API clients
"""

import hashlib
import json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import keyring
import requests

from .urls import is_atlassian_cloud_url

# Configure logging
logger = logging.getLogger("mcp-atlassian.oauth")

# Cloud OAuth endpoints
CLOUD_TOKEN_URL = "https://auth.atlassian.com/oauth/token"  # noqa: S105 - This is a public API endpoint URL, not a password
CLOUD_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
CLOUD_ID_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Legacy aliases for backward compatibility
TOKEN_URL = CLOUD_TOKEN_URL  # noqa: S105
AUTHORIZE_URL = CLOUD_AUTHORIZE_URL

# Data Center OAuth endpoint paths (appended to base_url)
DC_TOKEN_PATH = "/rest/oauth2/latest/token"  # noqa: S105
DC_AUTHORIZE_PATH = "/rest/oauth2/latest/authorize"

TOKEN_EXPIRY_MARGIN = 300  # 5 minutes in seconds

# HTTP request timeouts (in seconds)
# Connection timeout: Time to establish TCP connection
# Read timeout: Time to receive response after connection established
HTTP_CONNECT_TIMEOUT = 5
HTTP_READ_TIMEOUT = 20
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
KEYRING_SERVICE_NAME = "mcp-atlassian-oauth"


@dataclass
class OAuthConfig:
    """OAuth 2.0 configuration for Atlassian Cloud and Data Center.

    This class manages the OAuth configuration and tokens. It handles:
    - Authentication configuration (client credentials)
    - Token acquisition and refreshing
    - Token storage and retrieval
    - Cloud ID identification (Cloud) or base URL routing (Data Center)
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    cloud_id: str | None = None
    base_url: str | None = None
    refresh_token: str | None = None
    access_token: str | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        """Validate mutual exclusivity of cloud_id and base_url."""
        if self.cloud_id and self.base_url:
            # Check if base_url is a Cloud URL — if so, cloud_id takes precedence
            if is_atlassian_cloud_url(self.base_url):
                self.base_url = None
            else:
                raise ValueError(
                    "OAuthConfig cannot have both cloud_id and base_url set. "
                    "Use cloud_id for Cloud or base_url for Data Center."
                )

    @property
    def is_data_center(self) -> bool:
        """Check if this is a Data Center OAuth configuration.

        Returns:
            True if base_url is set and is not a Cloud URL.
        """
        if not self.base_url:
            return False
        return not is_atlassian_cloud_url(self.base_url)

    @property
    def token_url(self) -> str:
        """Get the token endpoint URL for the configured environment.

        Returns:
            Cloud token URL or Data Center instance-specific token URL.
        """
        if self.is_data_center and self.base_url:
            return f"{self.base_url.rstrip('/')}{DC_TOKEN_PATH}"
        return CLOUD_TOKEN_URL

    @property
    def authorize_url(self) -> str:
        """Get the authorization endpoint URL for the configured environment.

        Returns:
            Cloud authorize URL or Data Center instance-specific authorize URL.
        """
        if self.is_data_center and self.base_url:
            return f"{self.base_url.rstrip('/')}{DC_AUTHORIZE_PATH}"
        return CLOUD_AUTHORIZE_URL

    @property
    def is_token_expired(self) -> bool:
        """Check if the access token is expired or will expire soon.

        Returns:
            True if the token is expired or will expire soon, False otherwise.
        """
        # If we don't have a token or expiry time, consider it expired
        if not self.access_token or not self.expires_at:
            return True

        # Consider the token expired if it will expire within the margin
        return time.time() + TOKEN_EXPIRY_MARGIN >= self.expires_at

    def get_authorization_url(self, state: str) -> str:
        """Get the authorization URL for the OAuth 2.0 flow.

        Args:
            state: Random state string for CSRF protection

        Returns:
            The authorization URL to redirect the user to.
        """
        params: dict[str, str] = {
            "client_id": self.client_id,
            "scope": self.scope,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        # Cloud-specific params (DC doesn't use audience or prompt)
        if not self.is_data_center:
            params["audience"] = "api.atlassian.com"
            params["prompt"] = "consent"

        return f"{self.authorize_url}?{urllib.parse.urlencode(params)}"

    def exchange_code_for_tokens(self, code: str) -> bool:
        """Exchange the authorization code for access and refresh tokens.

        Args:
            code: The authorization code from the callback

        Returns:
            True if tokens were successfully acquired, False otherwise.
        """
        try:
            payload = {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            }

            token_endpoint = self.token_url
            logger.info(f"Exchanging authorization code for tokens at {token_endpoint}")
            logger.debug("Sending token exchange request")

            response = requests.post(token_endpoint, data=payload, timeout=HTTP_TIMEOUT)

            # Log more details about the response
            logger.debug(f"Token exchange response status: {response.status_code}")

            if not response.ok:
                logger.error(
                    f"Token exchange failed with status {response.status_code}. "
                    f"Response: {response.text}"
                )
                return False

            # Parse the response
            token_data = response.json()

            # Check if required tokens are present
            if "access_token" not in token_data:
                logger.error(
                    f"Access token not found in response. "
                    f"Keys found: {list(token_data.keys())}"
                )
                return False

            # DC does NOT require refresh_token (no offline_access scope needed)
            if "refresh_token" not in token_data:
                if self.is_data_center:
                    logger.warning(
                        "No refresh_token in DC response — token cannot be refreshed. "
                        "Re-authenticate when the token expires."
                    )
                else:
                    logger.error(
                        "Refresh token not found in response. "
                        "Ensure 'offline_access' scope is included. "
                        f"Keys found: {list(token_data.keys())}"
                    )
                    return False

            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token")
            self.expires_at = time.time() + token_data.get("expires_in", 3600)

            # Only get cloud ID for Cloud OAuth
            if not self.is_data_center:
                self._get_cloud_id()

            # Save the tokens
            self._save_tokens()

            # Log success message with token details
            logger.info(
                f"OAuth token exchange successful! "
                f"Access token expires in {token_data.get('expires_in', 3600)}s."
            )
            logger.info("Access token obtained successfully.")
            logger.info("Refresh token obtained successfully.")
            if self.cloud_id:
                logger.info(f"Cloud ID successfully retrieved: {self.cloud_id}")
            elif not self.is_data_center:
                logger.warning(
                    "Cloud ID was not retrieved after token exchange. "
                    "Check accessible resources."
                )
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during token exchange: {e}", exc_info=True)
            return False
        except json.JSONDecodeError as e:
            logger.error(
                f"Failed to decode JSON response from token endpoint: {e}",
                exc_info=True,
            )
            logger.error(
                f"Response text that failed to parse: "
                f"{response.text if 'response' in locals() else 'Response object not available'}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to exchange code for tokens: {e}")
            return False

    def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns:
            True if the token was successfully refreshed, False otherwise.
        """
        if not self.refresh_token:
            logger.error("No refresh token available")
            return False

        try:
            payload = {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }

            logger.debug(f"Refreshing access token at {self.token_url}...")
            response = requests.post(self.token_url, data=payload, timeout=HTTP_TIMEOUT)
            response.raise_for_status()

            # Parse the response
            token_data = response.json()
            self.access_token = token_data["access_token"]
            # Refresh token might also be rotated
            if "refresh_token" in token_data:
                self.refresh_token = token_data["refresh_token"]
            self.expires_at = time.time() + token_data.get("expires_in", 3600)

            # Save the tokens
            self._save_tokens()

            return True
        except Exception as e:
            logger.error(f"Failed to refresh access token: {e}")
            return False

    def ensure_valid_token(self) -> bool:
        """Ensure the access token is valid, refreshing if necessary.

        Returns:
            True if the token is valid (or was refreshed successfully), False otherwise.
        """
        if not self.is_token_expired:
            return True
        return self.refresh_access_token()

    def _get_cloud_id(self) -> None:
        """Get the cloud ID for the Atlassian instance.

        This method queries the accessible resources endpoint to get the cloud ID.
        The cloud ID is needed for API calls with Cloud OAuth.
        Data Center does not use cloud IDs.
        """
        if self.is_data_center:
            return

        if not self.access_token:
            logger.debug("No access token available to get cloud ID")
            return

        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            response = requests.get(CLOUD_ID_URL, headers=headers, timeout=HTTP_TIMEOUT)
            response.raise_for_status()

            resources = response.json()
            if resources and len(resources) > 0:
                # Use the first cloud site (most users have only one)
                self.cloud_id = resources[0]["id"]
                logger.debug(f"Found cloud ID: {self.cloud_id}")
            else:
                logger.warning("No Atlassian sites found in the response")
        except Exception as e:
            logger.error(f"Failed to get cloud ID: {e}")

    @staticmethod
    def _context_keyring_username(
        client_id: str,
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> str:
        """Build the context-specific keyring username for a token entry.

        Includes context (cloud_id or base_url hash) to prevent collisions
        when the same client_id is used across Cloud and Data Center, or
        across two Data Center products (e.g. Jira and Bitbucket) that were
        registered with the same client_id string.

        A Data Center entry is keyed on the full SHA-256 hex digest of the
        base URL (64 characters), so two distinct URLs cannot share a key.
        The configured base URL string is the identity on both sides, in the
        key and in the recorded context that a load checks, so editing the
        configured URL (a trailing slash, a change of case) produces a new
        key and a fresh authentication rather than a partial match.
        Keyring backends accept a username of this size: the tightest
        published limit is the Windows Credential Manager
        ``CRED_MAX_USERNAME_LENGTH`` of 513 characters, and the macOS
        Keychain and Secret Service backends impose no practical limit.

        Args:
            client_id: The OAuth client ID.
            cloud_id: The Atlassian Cloud ID, if this is a Cloud context.
            base_url: The instance base URL, if this is a Data Center context.

        Returns:
            A username string for keyring.
        """
        if base_url and not is_atlassian_cloud_url(base_url):
            url_hash = hashlib.sha256(base_url.encode()).hexdigest()
            return f"oauth-{client_id}-dc-{url_hash}"
        if cloud_id:
            return f"oauth-{client_id}-cloud-{cloud_id}"
        return f"oauth-{client_id}"

    @staticmethod
    def _legacy_context_keyring_username(
        client_id: str, *, base_url: str | None = None
    ) -> str | None:
        """Return the Data Center username earlier versions saved under.

        Earlier versions truncated the base URL digest to eight hex
        characters, which leaves room for two URLs to share a key. The
        legacy name is consulted on load only, as a fallback after the
        current key, and an entry found there is accepted only once
        :meth:`_tokens_match_context` confirms it records the requested
        base URL. New saves write the current key and then remove the
        legacy entry on a best-effort basis.

        Args:
            client_id: The OAuth client ID.
            base_url: The instance base URL, if this is a Data Center context.

        Returns:
            The legacy username, or None when the context is not Data Center.
        """
        if base_url and not is_atlassian_cloud_url(base_url):
            url_hash = hashlib.sha256(base_url.encode()).hexdigest()[:8]
            return f"oauth-{client_id}-dc-{url_hash}"
        return None

    @staticmethod
    def _candidate_keyring_usernames(
        client_id: str,
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> list[str]:
        """List the keyring usernames a load consults, in priority order.

        The current context key comes first, then the legacy Data Center
        key when one applies, then the shared ``oauth-{client_id}`` base
        key. Every candidate is validated against the requested context
        before it is accepted.
        """
        usernames = [
            OAuthConfig._context_keyring_username(
                client_id, cloud_id=cloud_id, base_url=base_url
            )
        ]
        legacy_username = OAuthConfig._legacy_context_keyring_username(
            client_id, base_url=base_url
        )
        if legacy_username:
            usernames.append(legacy_username)
        base_username = f"oauth-{client_id}"
        if base_username not in usernames:
            usernames.append(base_username)
        return usernames

    def _get_keyring_username(self) -> str:
        """Get the keyring username for storing this config's tokens.

        Returns:
            A username string for keyring
        """
        return self._context_keyring_username(
            self.client_id, cloud_id=self.cloud_id, base_url=self.base_url
        )

    def _save_tokens(self) -> None:
        """Save the tokens securely using keyring for later use.

        This allows the tokens to be reused between runs without requiring
        the user to go through the authorization flow again.
        """
        try:
            username = self._get_keyring_username()
            base_username = f"oauth-{self.client_id}"

            # Store token data as JSON string in keyring
            token_data = {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at,
                "cloud_id": self.cloud_id,
                "base_url": self.base_url,
            }

            token_json = json.dumps(token_data)

            # Store the token data in the system keyring using context-specific key
            keyring.set_password(KEYRING_SERVICE_NAME, username, token_json)
            logger.debug(f"Saved OAuth tokens to keyring for {username}")

            # Cloud saves also write the base username so the startup load,
            # which carries no cloud_id yet, and older versions that read only
            # the base key keep working. A Data Center entry is skipped: a
            # contextless load rejects any entry recording a base_url, so the
            # copy would be unreadable and would only duplicate the refresh
            # token and overwrite a Cloud entry sharing the client_id.
            if username != base_username and not self.is_data_center:
                keyring.set_password(KEYRING_SERVICE_NAME, base_username, token_json)
                logger.debug(f"Saved OAuth tokens to keyring for {base_username}")

            self._remove_legacy_keyring_entry()

            # Also maintain backwards compatibility with file storage
            # for environments where keyring might not work
            self._save_tokens_to_file(token_data)

        except Exception as e:
            logger.error(f"Failed to save tokens to keyring: {e}")
            # Fall back to file storage if keyring fails
            self._save_tokens_to_file()

    def _remove_legacy_keyring_entry(self) -> None:
        """Delete the keyring entry an earlier version saved for this context.

        Called after a successful save under the current key, so the refresh
        token does not stay live in a second slot. Best effort: a missing
        entry or a backend that cannot delete is logged and ignored.
        """
        legacy_username = self._legacy_context_keyring_username(
            self.client_id, base_url=self.base_url
        )
        if not legacy_username:
            return
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, legacy_username)
            logger.debug(f"Removed legacy keyring entry {legacy_username}")
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as e:
            logger.debug(f"Could not remove legacy keyring entry: {e}")

    def _save_tokens_to_file(self, token_data: dict | None = None) -> None:
        """Save the tokens to a file as fallback storage.

        Args:
            token_data: Optional dict with token data. If not provided,
                        will use the current object attributes.
        """
        try:
            # Create the directory if it doesn't exist (owner-only)
            token_dir = Path.home() / ".mcp-atlassian"
            token_dir.mkdir(exist_ok=True)
            os.chmod(token_dir, 0o700)

            # Save under the context-specific name so a context-keyed load can
            # find the right entry. Cloud saves also write the base name for
            # loads that carry no context; a Data Center entry under the base
            # name would be rejected by every load, so it is not written.
            token_paths = [token_dir / f"{self._get_keyring_username()}.json"]
            base_path = token_dir / f"oauth-{self.client_id}.json"
            if base_path not in token_paths and not self.is_data_center:
                token_paths.append(base_path)

            if token_data is None:
                token_data = {
                    "refresh_token": self.refresh_token,
                    "access_token": self.access_token,
                    "expires_at": self.expires_at,
                    "cloud_id": self.cloud_id,
                    "base_url": self.base_url,
                }

            # Persisted tokens are secrets: create/truncate owner-only so they are
            # never group/world-readable, independent of the process umask.
            for token_path in token_paths:
                fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(token_data, f)
                os.chmod(token_path, 0o600)
                logger.debug(
                    f"Saved OAuth tokens to file {token_path} (fallback storage)"
                )

            # The current key now holds the tokens; drop the file an earlier
            # version wrote under the short-hash name.
            legacy_username = self._legacy_context_keyring_username(
                self.client_id, base_url=self.base_url
            )
            if legacy_username:
                try:
                    (token_dir / f"{legacy_username}.json").unlink(missing_ok=True)
                except OSError as e:
                    logger.debug(f"Could not remove legacy token file: {e}")
        except Exception as e:
            logger.error(f"Failed to save tokens to file: {e}")

    @staticmethod
    def load_tokens(
        client_id: str,
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """Load tokens securely from keyring.

        Tokens are saved under a context-specific key (cloud_id or base_url
        hash), so the load must be context-keyed too: without it, two products
        registered with the same client_id string (e.g. Jira Data Center and
        Bitbucket Data Center) would read each other's tokens through the
        shared base key. Candidates are consulted in priority order: the
        context key, the legacy Data Center key written by earlier versions,
        then the shared ``oauth-{client_id}`` base key. Each entry found is
        accepted only when the context it recorded at save time matches the
        requested one, whichever key it was found under. Entries carrying no
        context fields cannot be attributed to a context and are rejected for
        any context-keyed load; re-authenticating rewrites them with context
        recorded.

        Args:
            client_id: The OAuth client ID
            cloud_id: The Atlassian Cloud ID, if loading for a Cloud context
            base_url: The instance base URL, if loading for a Data Center
                context

        Returns:
            Dict with the token data or empty dict if no tokens found
        """
        usernames = OAuthConfig._candidate_keyring_usernames(
            client_id, cloud_id=cloud_id, base_url=base_url
        )

        # Try to load tokens from keyring first. A candidate that cannot be
        # read or does not validate is skipped, so one bad entry does not
        # hide a good one under a later key.
        rejected: list[str] = []
        for username in usernames:
            try:
                token_json = keyring.get_password(KEYRING_SERVICE_NAME, username)
            except Exception as e:
                logger.warning(
                    f"Failed to read keyring entry {username}: {e}. "
                    "Trying the next candidate."
                )
                continue
            if not token_json:
                continue
            try:
                token_data = json.loads(token_json)
            except Exception:
                rejected.append(f"{username}: not valid JSON")
                continue
            reason = OAuthConfig._context_rejection_reason(
                token_data, cloud_id=cloud_id, base_url=base_url
            )
            if reason:
                rejected.append(f"{username}: {reason}")
                continue
            logger.debug(f"Loaded OAuth tokens from keyring for {username}")
            OAuthConfig._log_rejected_candidates("keyring", rejected)
            return token_data
        OAuthConfig._log_rejected_candidates("keyring", rejected)

        # Fall back to loading from file if keyring fails or returns None
        return OAuthConfig._load_tokens_from_file(
            client_id, usernames=usernames, cloud_id=cloud_id, base_url=base_url
        )

    @staticmethod
    def _context_rejection_reason(
        token_data: Any,
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> str | None:
        """Explain why a stored candidate cannot satisfy this load, or None."""
        if not isinstance(token_data, dict):
            return "not a JSON object"
        if OAuthConfig._tokens_match_context(
            token_data, cloud_id=cloud_id, base_url=base_url
        ):
            return None
        if not token_data.get("cloud_id") and not token_data.get("base_url"):
            return "records no service context"
        return "saved for a different service context"

    @staticmethod
    def _log_rejected_candidates(source: str, rejected: list[str]) -> None:
        """Report, once per load, the cached entries that were found but unusable."""
        if rejected:
            logger.info(
                f"Ignored {len(rejected)} cached OAuth token entr"
                f"{'y' if len(rejected) == 1 else 'ies'} in {source}: "
                + "; ".join(rejected)
            )

    @staticmethod
    def _tokens_match_context(
        token_data: dict[str, Any],
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> bool:
        """Whether a stored token entry belongs to the requested context.

        Saved entries record the ``cloud_id``/``base_url`` they were issued
        for. The check applies to every candidate key: the shared base key is
        rewritten by whichever context saved last, and a legacy Data Center
        key can be shared by two base URLs, so the key an entry was found
        under says nothing reliable about the context it belongs to. Entries
        carrying no context fields predate context recording and cannot be
        attributed to any context, so a context-keyed load rejects them
        rather than risk handing one provider's tokens to another; a fresh
        authentication re-saves them with context recorded.
        """
        stored_cloud_id = token_data.get("cloud_id")
        stored_base_url = token_data.get("base_url")
        if base_url:
            return stored_base_url == base_url
        if cloud_id:
            return stored_cloud_id == cloud_id
        # A contextless load can only be the Cloud branch (the Data Center
        # branch supplies base_url), so a stored DC entry is another
        # provider's credential and is rejected. A stored cloud_id is fine:
        # the standard Cloud flow discovers cloud_id during authentication,
        # stores it, and loads without one on startup, and rejecting it would
        # force a re-authentication loop.
        return not stored_base_url

    @staticmethod
    def _load_tokens_from_file(
        client_id: str,
        usernames: list[str] | None = None,
        *,
        cloud_id: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """Load tokens from a file as fallback.

        Every candidate file is validated against the requested context, as
        in :meth:`load_tokens`.

        Args:
            client_id: The OAuth client ID
            usernames: Candidate file basenames in priority order; defaults to
                the base ``oauth-{client_id}`` name.
            cloud_id: The Atlassian Cloud ID, if loading for a Cloud context
            base_url: The instance base URL, if loading for a Data Center
                context

        Returns:
            Dict with the token data or empty dict if no tokens found
        """
        token_dir = Path.home() / ".mcp-atlassian"
        base_name = f"oauth-{client_id}"
        rejected: list[str] = []
        for username in usernames or [base_name]:
            token_path = token_dir / f"{username}.json"

            if not token_path.exists():
                continue

            try:
                with open(token_path) as f:
                    token_data = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load tokens from file: {e}")
                rejected.append(f"{token_path.name}: unreadable")
                continue
            reason = OAuthConfig._context_rejection_reason(
                token_data, cloud_id=cloud_id, base_url=base_url
            )
            if reason:
                rejected.append(f"{token_path.name}: {reason}")
                continue
            logger.debug(
                f"Loaded OAuth tokens from file {token_path} (fallback storage)"
            )
            OAuthConfig._log_rejected_candidates("file storage", rejected)
            return token_data
        OAuthConfig._log_rejected_candidates("file storage", rejected)
        return {}

    @classmethod
    def from_env(
        cls,
        service_url: str | None = None,
        service_type: str | None = None,
        disallow_shared_fallback: bool = False,
    ) -> Optional["OAuthConfig"]:
        """Create an OAuth configuration from environment variables.

        Args:
            service_url: The service URL (e.g., JIRA_URL value) for DC detection.
            service_type: Service type ('jira', 'confluence', 'bitbucket') for
                service-specific env vars.
            disallow_shared_fallback: When True (and a service_type is given), do
                not fall back to the shared ``ATLASSIAN_OAUTH_*`` credentials.
                A distinct provider on a distinct host (e.g. Bitbucket Data
                Center) must not be satisfied by another product's credentials;
                this keeps the loader's decision aligned with the per-service
                availability gate. The shared ``ATLASSIAN_OAUTH_ENABLE`` mode
                flag is unaffected, since it selects user-provided-token mode
                rather than supplying a credential.

        Returns:
            OAuthConfig instance or None if OAuth is not enabled

        Raises:
            ValueError: If ``disallow_shared_fallback`` is set without a
                ``service_type``, since the flag scopes credential lookup to
                one service's env vars.
        """
        if disallow_shared_fallback and not service_type:
            raise ValueError(
                "disallow_shared_fallback requires a service_type: without one "
                "there are no service-specific credentials to prefer over the "
                "shared ones."
            )

        # Check if OAuth is explicitly enabled (allows minimal config)
        oauth_enabled = os.getenv("ATLASSIAN_OAUTH_ENABLE", "").lower() in (
            "true",
            "1",
            "yes",
        )

        # Service-specific env vars take precedence over shared ones. When
        # disallow_shared_fallback is set, the shared ATLASSIAN_OAUTH_* values
        # are not consulted for this service.
        prefix = service_type.upper() if service_type else None

        def _resolve(suffix: str) -> str | None:
            service_val = os.getenv(f"{prefix}_{suffix}") if prefix else None
            if disallow_shared_fallback and prefix:
                return service_val
            return service_val or os.getenv(f"ATLASSIAN_{suffix}")

        client_id = _resolve("OAUTH_CLIENT_ID")
        client_secret = _resolve("OAUTH_CLIENT_SECRET")
        redirect_uri = _resolve("OAUTH_REDIRECT_URI")
        scope = _resolve("OAUTH_SCOPE")

        # Determine if this is a DC instance
        is_dc = bool(service_url) and not is_atlassian_cloud_url(service_url)

        # For DC, redirect_uri and scope can have defaults
        if is_dc:
            if not redirect_uri:
                redirect_uri = "http://localhost:8080/callback"
            if not scope:
                # The Jira/Confluence Data Center default scope does not exist
                # on Bitbucket Data Center (its scopes are the REPO_READ /
                # REPO_WRITE / PUBLIC_REPOS family), so a silently defaulted
                # scope would fail only later, upstream. Fail fast instead.
                if service_type == "bitbucket" and client_id and client_secret:
                    raise ValueError(
                        "BITBUCKET_OAUTH_SCOPE is required for a Bitbucket "
                        "OAuth client: Bitbucket Data Center has no default "
                        "OAuth scope. Set it to the scopes granted to the "
                        "incoming application link, e.g. PUBLIC_REPOS, "
                        "REPO_READ, REPO_WRITE, REPO_ADMIN or PROJECT_ADMIN."
                    )
                scope = "WRITE"

        # Full OAuth configuration (traditional mode)
        if all([client_id, client_secret]):
            # Need redirect_uri + scope for Cloud, but DC has defaults above
            if not all([redirect_uri, scope]) and not is_dc:
                return None

            cloud_id = os.getenv("ATLASSIAN_OAUTH_CLOUD_ID") if not is_dc else None
            base_url = service_url if is_dc else None

            config = cls(
                client_id=client_id or "",
                client_secret=client_secret or "",
                redirect_uri=redirect_uri or "",
                scope=scope or "",
                cloud_id=cloud_id,
                base_url=base_url,
            )

            # Try to load existing tokens for this service's context, so two
            # products sharing a client_id string do not read each other's cache
            token_data = cls.load_tokens(
                client_id or "", cloud_id=cloud_id, base_url=base_url
            )
            if token_data:
                config.refresh_token = token_data.get("refresh_token")
                config.access_token = token_data.get("access_token")
                config.expires_at = token_data.get("expires_at")
                if not config.cloud_id and "cloud_id" in token_data:
                    config.cloud_id = token_data["cloud_id"]
                if not config.base_url and "base_url" in token_data:
                    config.base_url = token_data["base_url"]

            return config

        # Minimal OAuth configuration (user-provided tokens mode)
        elif oauth_enabled:
            # Create minimal config that works with user-provided tokens
            logger.info(
                "Creating minimal OAuth config for user-provided tokens "
                "(ATLASSIAN_OAUTH_ENABLE=true)"
            )
            cloud_id = os.getenv("ATLASSIAN_OAUTH_CLOUD_ID") if not is_dc else None
            base_url = service_url if is_dc else None

            return cls(
                client_id="",  # Will be provided by user tokens
                client_secret="",  # Not needed for user tokens
                redirect_uri="",  # Not needed for user tokens
                scope="",  # Will be determined by user token permissions
                cloud_id=cloud_id,
                base_url=base_url,
            )

        # No OAuth configuration
        return None


@dataclass
class BYOAccessTokenOAuthConfig:
    """OAuth configuration when providing a pre-existing access token.

    This class is used when the user provides their own access token directly,
    bypassing the full OAuth 2.0 (3LO) flow. Works for both Cloud (with cloud_id)
    and Data Center (with base_url).

    This configuration does not support token refreshing.
    """

    access_token: str = field(repr=False)
    cloud_id: str | None = None
    base_url: str | None = None
    refresh_token: None = field(default=None, repr=False)
    expires_at: None = field(default=None, repr=False)

    @property
    def is_data_center(self) -> bool:
        """Check if this is a Data Center configuration."""
        if not self.base_url:
            return False
        return not is_atlassian_cloud_url(self.base_url)

    @classmethod
    def from_env(
        cls,
        service_url: str | None = None,
        service_type: str | None = None,
        disallow_shared_fallback: bool = False,
    ) -> Optional["BYOAccessTokenOAuthConfig"]:
        """Create a BYOAccessTokenOAuthConfig from environment variables.

        Args:
            service_url: The service URL for DC detection.
            service_type: Service type ('jira', 'confluence', 'bitbucket') for
                service-specific env vars.
            disallow_shared_fallback: When True (and a service_type is given), do
                not fall back to the shared ``ATLASSIAN_OAUTH_ACCESS_TOKEN``. A
                distinct provider must not be satisfied by another product's
                token (see :meth:`OAuthConfig.from_env`).

        Returns:
            BYOAccessTokenOAuthConfig instance or None if required
            environment variables are missing.

        Raises:
            ValueError: If ``disallow_shared_fallback`` is set without a
                ``service_type`` (see :meth:`OAuthConfig.from_env`).
        """
        if disallow_shared_fallback and not service_type:
            raise ValueError(
                "disallow_shared_fallback requires a service_type: without one "
                "there are no service-specific credentials to prefer over the "
                "shared ones."
            )

        cloud_id = os.getenv("ATLASSIAN_OAUTH_CLOUD_ID")

        # Service-specific access token takes precedence; the shared token is
        # only consulted when fallback is allowed.
        prefix = service_type.upper() if service_type else None
        service_token = os.getenv(f"{prefix}_OAUTH_ACCESS_TOKEN") if prefix else None
        if disallow_shared_fallback and prefix:
            access_token = service_token
        else:
            access_token = service_token or os.getenv("ATLASSIAN_OAUTH_ACCESS_TOKEN")

        if not access_token:
            return None

        # Determine if DC
        is_dc = bool(service_url) and not is_atlassian_cloud_url(service_url)
        base_url = service_url if is_dc else None

        # Need either cloud_id (Cloud) or base_url (DC) to be useful
        if not cloud_id and not base_url:
            return None

        return cls(
            access_token=access_token,
            cloud_id=cloud_id if not is_dc else None,
            base_url=base_url,
        )


def get_oauth_config_from_env(
    service_url: str | None = None,
    service_type: str | None = None,
    disallow_shared_fallback: bool = False,
) -> OAuthConfig | BYOAccessTokenOAuthConfig | None:
    """Get the appropriate OAuth configuration from environment variables.

    This function attempts to load standard OAuth configuration first (OAuthConfig).
    If that's not available, it tries to load a "Bring Your Own Access Token"
    configuration (BYOAccessTokenOAuthConfig).

    Args:
        service_url: The service URL for DC detection.
        service_type: Service type ('jira', 'confluence', 'bitbucket') for
            service-specific env vars.
        disallow_shared_fallback: When True, the shared ``ATLASSIAN_OAUTH_*``
            credentials are not used to satisfy this service (see
            :meth:`OAuthConfig.from_env`).

    Returns:
        An instance of OAuthConfig or BYOAccessTokenOAuthConfig if environment
        variables are set for either, otherwise None.
    """
    return BYOAccessTokenOAuthConfig.from_env(
        service_url=service_url,
        service_type=service_type,
        disallow_shared_fallback=disallow_shared_fallback,
    ) or OAuthConfig.from_env(
        service_url=service_url,
        service_type=service_type,
        disallow_shared_fallback=disallow_shared_fallback,
    )


def configure_oauth_session(
    session: requests.Session, oauth_config: OAuthConfig | BYOAccessTokenOAuthConfig
) -> bool:
    """Configure a requests session with OAuth 2.0 authentication.

    This function ensures the access token is valid and adds it to the session headers.

    Args:
        session: The requests session to configure
        oauth_config: The OAuth configuration to use

    Returns:
        True if the session was successfully configured, False otherwise
    """
    logger.debug(
        f"configure_oauth_session: Received OAuthConfig with "
        f"access_token_present={bool(oauth_config.access_token)}, "
        f"refresh_token_present={bool(oauth_config.refresh_token)}, "
        f"cloud_id='{oauth_config.cloud_id}'"
    )

    # Early return when no tokens are available at all (#858)
    if not oauth_config.access_token and not oauth_config.refresh_token:
        logger.warning(
            "configure_oauth_session: No access_token or refresh_token available. "
            "Cannot configure OAuth session. If using per-request auth, "
            "the token should come from the request header."
        )
        return False

    # If user provided only an access token (no refresh_token), use it directly
    if oauth_config.access_token and not oauth_config.refresh_token:
        logger.info(
            "configure_oauth_session: Using provided OAuth access token directly "
            "(no refresh_token)."
        )
        session.headers["Authorization"] = f"Bearer {oauth_config.access_token}"
        return True
    logger.debug("configure_oauth_session: Proceeding to ensure_valid_token.")
    # Otherwise, ensure we have a valid token (refresh if needed)
    if isinstance(oauth_config, BYOAccessTokenOAuthConfig):
        logger.error(
            "configure_oauth_session: oauth access token configuration "
            "provided as empty string."
        )
        return False
    if not oauth_config.ensure_valid_token():
        logger.error(
            f"configure_oauth_session: ensure_valid_token returned False. "
            f"Token was expired: {oauth_config.is_token_expired}, "
            f"Refresh token present for attempt: {bool(oauth_config.refresh_token)}"
        )
        return False
    session.headers["Authorization"] = f"Bearer {oauth_config.access_token}"
    logger.info("Successfully configured OAuth session for Atlassian API")
    return True
