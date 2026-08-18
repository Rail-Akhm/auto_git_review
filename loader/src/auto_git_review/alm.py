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
        # Git-эндпоинты — project-scoped: /{collection}/{project}/_apis/...
        return f"{self.settings.azure_url}/{self.settings.azure_project}/{path.lstrip('/')}"

    def _url_collection(self, path: str) -> str:
        # Коллекционные эндпоинты (без проекта в пути), напр. WIT.
        return f"{self.settings.azure_url}/{path.lstrip('/')}"

    def _get(self, path: str, params=None) -> dict:
        p = dict(params or {})
        p.setdefault("api-version", self.settings.api_version)
        resp = self.session.get(self._url(path), params=p, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _get_collection(self, path: str, params=None) -> dict:
        p = dict(params or {})
        p.setdefault("api-version", self.settings.api_version)
        resp = self.session.get(self._url_collection(path), params=p, timeout=60)
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
        # WIT — коллекционный эндпоинт (не привязан к проекту).
        return self._get_collection(f"_apis/wit/workitems/{work_item_id}")

    def get_pr_changes(self, source_commit: str, target_commit: str, repo: str = None) -> dict:
        """Низкоуровневый diff между двумя коммитами (diffs/commits).

        ВАЖНО: без ``baseVersionType``/``targetVersionType=commit`` сервер трактует
        SHA как имя ветки и отвечает 404 (GitUnresolvableToCommitException).

        Возвращает ``changes[]``, но вместе с файлами там и ДИРЕКТОРИИ
        (tree-объекты: /db, /db/current, ...). Контента в ответе НЕТ.
        Для списка реально изменённых файлов PR используй get_pr_iteration_changes().
        """
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/diffs/commits",
            params={
                "baseVersion": target_commit,
                "baseVersionType": "commit",
                "targetVersion": source_commit,
                "targetVersionType": "commit",
            },
        )

    def get_pr_iterations(self, pr_id: int, repo: str = None) -> dict:
        """Итерации PR. Каждая содержит sourceRefCommit/targetRefCommit/commonRefCommit."""
        repo = repo or self.settings.azure_repo
        return self._get(f"_apis/git/repositories/{repo}/pullRequests/{pr_id}/iterations")

    def get_pr_iteration_changes(self, pr_id: int, iteration_id: int, repo: str = None) -> dict:
        """Точный список изменённых ФАЙЛОВ PR в итерации (changeEntries[]).

        Каждая запись: {changeType: add|edit|delete|..., item: {path, objectId,
        originalObjectId, gitObjectType}}. Контента в ответе НЕТ — его надо
        дотягивать отдельно через get_file_content().
        """
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/pullRequests/{pr_id}/iterations/{iteration_id}/changes",
            params={"$top": 2000},
        )

    def get_file_content(self, path: str, version: str, repo: str = None) -> str:
        """Сырое содержимое файла ``path`` на версии ``version`` (SHA коммита).

        Endpoint ``items?includeContent=true`` для одного файла возвращает ТЕКСТ
        файла напрямую (не JSON), поэтому здесь читается resp.text, а не resp.json().
        """
        repo = repo or self.settings.azure_repo
        resp = self.session.get(
            self._url(f"_apis/git/repositories/{repo}/items"),
            params={
                "path": path,
                "includeContent": "true",
                "versionDescriptor.version": version,
                "versionDescriptor.versionType": "commit",
                "api-version": self.settings.api_version,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.text

    def get_threads(self, pr_id: int, repo: str = None) -> dict:
        """Все комментарии (threads) PR. Ответ: {value: [...], count: N}."""
        repo = repo or self.settings.azure_repo
        return self._get(
            f"_apis/git/repositories/{repo}/pullRequests/{pr_id}/threads"
        )

    def create_thread_comment(self, pr_id: int, content: str, repo: str = None) -> dict:
        """Создать общий комментарий (thread) к PR. Возвращает созданный thread.

        commentType=1 (text), status=1 (active), без threadContext — комментарий
        к самому PR, не к конкретному файлу/строке.
        """
        repo = repo or self.settings.azure_repo
        body = {
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": 1,
        }
        resp = self.session.post(
            self._url(f"_apis/git/repositories/{repo}/pullRequests/{pr_id}/threads"),
            params={"api-version": self.settings.api_version},
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

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
