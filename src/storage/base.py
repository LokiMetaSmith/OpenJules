"""Abstract storage provider for persisting task history, thoughts, and Polyphony state."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseStorageProvider(ABC):
    """Abstract interface for OpenJules persistent storage."""

    @abstractmethod
    def save_task(self, task_id: str, task_data: Dict[str, Any]) -> None:
        """Persists task record."""
        pass

    @abstractmethod
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves task record by ID."""
        pass

    @abstractmethod
    def list_tasks(self) -> List[Dict[str, Any]]:
        """Lists all stored tasks."""
        pass

    @abstractmethod
    def save_baton(self, repo: str, resource: str, owner: str) -> None:
        """Persists active mutex baton."""
        pass

    @abstractmethod
    def remove_baton(self, repo: str, resource: str) -> None:
        """Removes mutex baton."""
        pass

    @abstractmethod
    def get_all_batons(self) -> Dict[str, str]:
        """Returns all currently held batons across repositories."""
        pass
