"""Unit tests for UsersMixin (the authenticated caller's profile)."""

from unittest.mock import MagicMock, patch

import pytest
from requests import HTTPError

from mcp_atlassian.bitbucket import BitbucketConfig, BitbucketFetcher
from mcp_atlassian.bitbucket.client import BitbucketResourceNotFoundError
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.bitbucket import BitbucketUserProfile
from mcp_atlassian.utils.oauth import BYOAccessTokenOAuthConfig
from tests.unit.bitbucket.mock_responses import response_with_header

_PROFILE = {
    "name": "jdoe",
    "emailAddress": "jdoe@example.com",
    "id": 101,
    "displayName": "J. Doe",
    "active": True,
    "slug": "jdoe-slug",
    "type": "NORMAL",
    "avatarUrl": "https://bitbucket.corp.example.com/avatar",
    "links": {"self": [{"href": "https://bitbucket.corp.example.com/users/jdoe"}]},
}


def _fetcher() -> BitbucketFetcher:
    """Build a DC BYO-token fetcher for user tests."""
    return BitbucketFetcher(
        config=BitbucketConfig(
            url="https://bitbucket.corp.example.com",
            auth_type="oauth",
            oauth_config=BYOAccessTokenOAuthConfig(
                access_token="user-bearer-token",
                base_url="https://bitbucket.corp.example.com",
            ),
        )
    )


def _http_error_response(status: int) -> MagicMock:
    """Build a mock response whose raise_for_status raises an HTTPError."""
    response = MagicMock()
    response.status_code = status
    error = HTTPError(f"{status} error")
    error.response = response
    response.raise_for_status.side_effect = error
    return response


def _directory(username, users, profile=_PROFILE, profile_response=None):
    """A session.get serving the prime, the slug lookup, and the profile."""

    def _get(url, params=None, timeout=None, **kwargs):
        if url.endswith("/inbox/pull-requests/count"):
            return response_with_header({"count": 0}, username)
        if url.endswith("/users"):
            return response_with_header({"values": users, "isLastPage": True}, username)
        if "/users/" in url:
            if profile_response is not None:
                return profile_response
            return response_with_header(profile, username)
        raise AssertionError(f"unexpected request to {url}")

    return _get


class TestGetCurrentUserProfile:
    """Slug resolution followed by one profile call."""

    def test_resolves_slug_then_fetches_profile(self):
        """A cold client primes the username, resolves the slug, then fetches."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ) as mock_get:
            profile = fetcher.get_current_user_profile()

        assert isinstance(profile, BitbucketUserProfile)
        assert profile.name == "jdoe"
        assert profile.slug == "jdoe-slug"
        assert profile.display_name == "J. Doe"
        assert profile.id == 101
        assert profile.active is True
        assert profile.type == "NORMAL"
        paths = [call.args[0] for call in mock_get.call_args_list]
        assert paths[0].endswith("/rest/api/1.0/inbox/pull-requests/count")
        assert paths[1].endswith("/rest/api/1.0/users")
        assert paths[2].endswith("/rest/api/1.0/users/jdoe-slug")
        assert len(paths) == 3

    def test_later_calls_issue_one_request(self):
        """After the slug is memoised only the profile request is made."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ) as mock_get:
            fetcher.get_current_user_profile()
            first = mock_get.call_count
            fetcher.get_current_user_profile()

        assert mock_get.call_count == first + 1
        assert mock_get.call_args[0][0].endswith("/rest/api/1.0/users/jdoe-slug")

    def test_refresh_resolves_the_identity_again(self):
        """``refresh`` discards both memos and repeats the whole resolution."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ) as mock_get:
            fetcher.get_current_user_profile()
            mock_get.reset_mock()
            users[0]["slug"] = "jdoe-renamed"
            fetcher.get_current_user_profile(refresh=True)

        paths = [call.args[0] for call in mock_get.call_args_list]
        assert len(paths) == 3
        assert paths[0].endswith("/rest/api/1.0/inbox/pull-requests/count")
        assert paths[1].endswith("/rest/api/1.0/users")
        assert paths[2].endswith("/rest/api/1.0/users/jdoe-renamed")

    def test_refresh_recovers_from_a_renamed_account(self):
        """A rename changes the username too; one refreshed call follows it."""
        fetcher = _fetcher()
        directory = {"username": "jdoe", "users": [{"name": "jdoe", "slug": "jdoe"}]}

        def _get(url, params=None, timeout=None, **kwargs):
            username = directory["username"]
            if url.endswith("/inbox/pull-requests/count"):
                return response_with_header({"count": 0}, username)
            if url.endswith("/users"):
                assert params["filter"] == username
                return response_with_header(
                    {"values": directory["users"], "isLastPage": True}, username
                )
            slug = url.rsplit("/", 1)[1]
            return response_with_header({**_PROFILE, "slug": slug}, username)

        with patch.object(fetcher._session, "get", side_effect=_get):
            assert fetcher.get_current_user_profile().slug == "jdoe"
            directory["username"] = "jane"
            directory["users"] = [{"name": "jane", "slug": "jane"}]
            assert fetcher.get_current_user_profile(refresh=True).slug == "jane"

    def test_primed_username_skips_the_prime_request(self):
        """A username captured earlier is reused, so no prime call is made."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ) as mock_get:
            fetcher.get_current_user()  # the dependency layer's validation call
            mock_get.reset_mock()
            fetcher.get_current_user_profile()

        paths = [call.args[0] for call in mock_get.call_args_list]
        assert len(paths) == 2
        assert paths[0].endswith("/rest/api/1.0/users")
        assert paths[1].endswith("/rest/api/1.0/users/jdoe-slug")

    def test_slug_is_percent_encoded_in_the_path(self):
        """A slug with reserved characters cannot alter the request path."""
        fetcher = _fetcher()
        users = [{"name": "j doe", "slug": "j doe/../x"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("j doe", users)
        ) as mock_get:
            fetcher.get_current_user_profile()

        assert mock_get.call_args[0][0].endswith("/rest/api/1.0/users/j%20doe%2F..%2Fx")

    def test_missing_ausername_raises_without_profile_request(self):
        """Without X-AUSERNAME the identity is unknown and no profile is fetched."""
        fetcher = _fetcher()
        with patch.object(
            fetcher._session, "get", side_effect=_directory(None, [])
        ) as mock_get:
            with pytest.raises(ValueError, match="X-AUSERNAME"):
                fetcher.get_current_user_profile()

        assert not any("/users/" in call.args[0] for call in mock_get.call_args_list)

    def test_no_exact_match_raises(self):
        """A directory with no exact name match raises rather than guessing."""
        fetcher = _fetcher()
        users = [{"name": "jdoe2", "slug": "jdoe2-slug"}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ):
            with pytest.raises(ValueError, match="no exact name match"):
                fetcher.get_current_user_profile()

    @pytest.mark.parametrize("body", ["nonsense", [], {}, None])
    def test_non_object_body_raises_value_error(self, body):
        """A non-object or empty body surfaces as a descriptive ValueError."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session,
            "get",
            side_effect=_directory(
                "jdoe", users, profile_response=response_with_header(body, "jdoe")
            ),
        ):
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_current_user_profile()

    def test_wrong_typed_scalar_raises_value_error(self):
        """A wrong-typed scalar is rejected at the model boundary."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        profile = {**_PROFILE, "active": "yes"}
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users, profile)
        ):
            with pytest.raises(ValueError, match="'active' in BitbucketUserProfile"):
                fetcher.get_current_user_profile()

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_status_raises_authentication_error(self, status):
        """A 401/403 on the profile call surfaces as an authentication error."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session,
            "get",
            side_effect=_directory(
                "jdoe", users, profile_response=_http_error_response(status)
            ),
        ):
            with pytest.raises(MCPAtlassianAuthenticationError, match=str(status)):
                fetcher.get_current_user_profile()

    def test_not_found_raises_resource_not_found_with_refresh_hint(self):
        """A 404 on the profile call names the slug and points at refresh."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": "jdoe-slug"}]
        with patch.object(
            fetcher._session,
            "get",
            side_effect=_directory(
                "jdoe", users, profile_response=_http_error_response(404)
            ),
        ):
            with pytest.raises(BitbucketResourceNotFoundError) as excinfo:
                fetcher.get_current_user_profile()

        message = str(excinfo.value)
        assert "HTTP 404" in message
        assert "/users/jdoe-slug" in message
        assert "refresh=true" in message

    def test_missing_ausername_error_is_identity_neutral(self):
        """The identity error does not mention an unrelated write operation."""
        fetcher = _fetcher()
        with patch.object(fetcher._session, "get", side_effect=_directory(None, [])):
            with pytest.raises(ValueError) as excinfo:
                fetcher.get_current_user_profile()

        message = str(excinfo.value)
        assert "X-AUSERNAME" in message
        assert "review status" not in message

    @pytest.mark.parametrize("bad_slug", ["   ", ".", ".."])
    def test_unusable_resolved_slug_raises_without_profile_request(self, bad_slug):
        """A server-supplied slug that cannot be a path segment is rejected."""
        fetcher = _fetcher()
        users = [{"name": "jdoe", "slug": bad_slug}]
        with patch.object(
            fetcher._session, "get", side_effect=_directory("jdoe", users)
        ) as mock_get:
            with pytest.raises(ValueError, match="unexpected response shape"):
                fetcher.get_current_user_profile()

        assert not any("/users/" in call.args[0] for call in mock_get.call_args_list)
