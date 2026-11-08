"""Abstract Forge Provider and Data Models for OpenJules."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class IssueData(BaseModel):
    """Normalized issue representation across forges."""

    id: str | int
    title: str
    body: str = ""
    author: str = ""
    labels: List[str] = Field(default_factory=list)
    repo_identifier: str = ""  # e.g., 'owner/repo' or 'rad:z4V1sJY...'
    raw_payload: Optional[Dict[str, Any]] = None


class ProposalResult(BaseModel):
    """Normalized result of creating a proposal (PR in GitHub or Patch in Radicle)."""

    id: str
    url_or_id: str
    title: str
    target_branch: str
    source_branch: str
    status: str = "open"


class ForgeProvider(ABC):
    """Abstract interface representing a Git collaboration forge."""

    @property
    @abstractmethod
    def forge_type(self) -> str:
        """Name of forge type (e.g. 'github', 'radicle')."""
        pass

    @abstractmethod
    def get_clone_url(self, repo_identifier: str, token: Optional[str] = None) -> str:
        """Returns the clone URL or URI for checking out the repository."""
        pass

    @abstractmethod
    def post_issue_comment(self, repo_identifier: str, issue_id: str | int, comment: str) -> bool:
        """Posts a comment or update on an issue."""
        pass

    @abstractmethod
    def create_proposal(
        self,
        repo_identifier: str,
        title: str,
        description: str,
        source_branch: str,
        target_branch: str,
        context: Optional[Any] = None,
    ) -> ProposalResult:
        """Creates a Pull Request (GitHub) or Patch (Radicle)."""
        pass
