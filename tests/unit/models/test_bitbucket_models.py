"""Unit tests for the Bitbucket Data Center Pydantic models."""

from mcp_atlassian.models.bitbucket import BitbucketProject, BitbucketRepository

# Shapes mirror the pinned Bitbucket DC REST spec (RestProject / RestRepository).
_PROJECT_API = {
    "key": "PROJ",
    "id": 11,
    "name": "My Project",
    "description": "A project",
    "type": "NORMAL",
    "public": False,
    "links": {"self": [{"href": "https://bitbucket.example.com/projects/PROJ"}]},
}

_REPO_API = {
    "slug": "my-repo",
    "id": 42,
    "name": "My Repo",
    "description": "The repo",
    "scmId": "git",
    "state": "AVAILABLE",
    "statusMessage": "Available",
    "forkable": True,
    "archived": False,
    "public": False,
    "defaultBranch": "refs/heads/main",
    "project": _PROJECT_API,
    "links": {"clone": [{"href": "ssh://git@bitbucket/PROJ/my-repo.git"}]},
}


class TestBitbucketProject:
    """BitbucketProject parsing and simplification."""

    def test_from_api_response_maps_fields(self):
        project = BitbucketProject.from_api_response(_PROJECT_API)
        assert project.key == "PROJ"
        assert project.id == "11"  # stringified
        assert project.name == "My Project"
        assert project.description == "A project"
        assert project.type == "NORMAL"
        assert project.public is False

    def test_from_api_response_empty_returns_default(self):
        project = BitbucketProject.from_api_response({})
        assert project.key == ""
        assert project.id == ""

    def test_from_api_response_non_dict_returns_default(self):
        project = BitbucketProject.from_api_response("nonsense")  # type: ignore[arg-type]
        assert project.key == ""

    def test_explicit_null_fields_fall_back_to_sentinels(self):
        """A present-but-null key/name uses the sentinel, not the string 'None'."""
        project = BitbucketProject.from_api_response({"key": None, "name": None})
        assert project.key == ""
        assert project.name == "Unknown"

    def test_to_simplified_dict_includes_present_fields(self):
        result = BitbucketProject.from_api_response(_PROJECT_API).to_simplified_dict()
        assert result == {
            "key": "PROJ",
            "name": "My Project",
            "description": "A project",
            "type": "NORMAL",
            "public": False,
        }

    def test_to_simplified_dict_omits_absent_optionals(self):
        result = BitbucketProject.from_api_response(
            {"key": "P", "name": "P"}
        ).to_simplified_dict()
        assert result == {"key": "P", "name": "P"}


class TestBitbucketRepository:
    """BitbucketRepository parsing and simplification."""

    def test_from_api_response_maps_fields_and_nested_project(self):
        repo = BitbucketRepository.from_api_response(_REPO_API)
        assert repo.slug == "my-repo"
        assert repo.id == "42"  # stringified
        assert repo.name == "My Repo"
        assert repo.description == "The repo"
        assert repo.scm_id == "git"  # from scmId
        assert repo.state == "AVAILABLE"
        assert repo.forkable is True
        assert repo.archived is False
        assert repo.public is False
        assert repo.default_branch == "refs/heads/main"  # from defaultBranch
        assert repo.project is not None
        assert repo.project.key == "PROJ"

    def test_from_api_response_without_project(self):
        data = {k: v for k, v in _REPO_API.items() if k != "project"}
        repo = BitbucketRepository.from_api_response(data)
        assert repo.project is None

    def test_malformed_project_degrades_to_none_not_raise(self):
        data = {**_REPO_API, "project": "not-an-object"}
        repo = BitbucketRepository.from_api_response(data)
        assert repo.project is None

    def test_from_api_response_empty_returns_default(self):
        repo = BitbucketRepository.from_api_response({})
        assert repo.slug == ""
        assert repo.project is None

    def test_explicit_null_fields_fall_back_to_sentinels(self):
        """A present-but-null slug/name uses the sentinel, not the string 'None'."""
        repo = BitbucketRepository.from_api_response({"slug": None, "name": None})
        assert repo.slug == ""
        assert repo.name == "Unknown"

    def test_to_simplified_dict_full(self):
        result = BitbucketRepository.from_api_response(_REPO_API).to_simplified_dict()
        assert result == {
            "slug": "my-repo",
            "name": "My Repo",
            "description": "The repo",
            "state": "AVAILABLE",
            "default_branch": "refs/heads/main",
            "scm_id": "git",
            "public": False,
            "archived": False,
            "forkable": True,
            "project": {
                "key": "PROJ",
                "name": "My Project",
                "description": "A project",
                "type": "NORMAL",
                "public": False,
            },
        }

    def test_to_simplified_dict_minimal(self):
        result = BitbucketRepository.from_api_response(
            {"slug": "r", "name": "r"}
        ).to_simplified_dict()
        assert result == {"slug": "r", "name": "r"}
