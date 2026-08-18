"""Клиент REST API Azure DevOps Server (on-prem)."""

import requests
from requests.auth import HTTPBasicAuth

from .config import get_settings


class AlmClient:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth("", self.settings.azure_pat)
        self.session.verify = self.settings.verify_ssl

    def _url(self, path: str) -> str:
        return f"{self.settings.azure_url}/{path.lstrip('/')}"

    def _get(self, path: str, params=None) -> dict:
        p = dict(params or {})
        p.setdefault("api-version", self.settings.api_version)
        resp = self.session.get(self._url(path), params=p, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def list_open_pull_requests(self, repo: str = None) -> dict:
        """Все открытые PR репозитория."""
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/pullrequests",
            params={"searchCriteria.status": "active"},
        )

    def list_completed_pull_requests(self, repo: str = None, top: int = 200) -> dict:
        """Последние завершённые PR (нужны для контекста «5 предыдущих PR»)."""
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/pullrequests",
            params={"searchCriteria.status": "completed", "$top": top},
        )

    def get_pull_request(self, pr_id: int, repo: str = None) -> dict:
        repo = repo or self.settings.azure_repo
        return self._get(f"_apis/git/repositories/{repo}/pullrequests/{pr_id}")

    def get_pr_work_items(self, pr_id: int, repo: str = None) -> dict:
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/pullrequests/{pr_id}/workitems"
        )

    def get_work_item(self, work_item_id: int) -> dict:
        return self._get(f"_apis/wit/workitems/{work_item_id}")

    def get_pr_changes(self, source_commit: str, target_commit: str, repo: str = None) -> dict:
        """Diff PR: изменённые файлы с содержимым (от target_commit к source_commit)."""
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/diffs/commits",
            params={
                "baseVersion": target_commit,
                "targetVersion": source_commit,
            },
        )

    def get_file_commits(self, path: str, target_version: str, top: int = 5, repo: str = None) -> dict:
        """История коммитов, затрагивавших файл path (до версии target_version)."""
        repo = repo or self.settings.azure_repo
        params = {
            "searchCriteria.itemPath": path,
            "$top": top,
        }
        if target_version:
            params["searchCriteria.itemVersion.version"] = target_version
            params["searchCriteria.itemVersion.versionType"] = "commit"
        return self._get(f"_apis/git/repositories/{repo}/commits", params=params)
