"""Тесты AlmClient: построение URL и структура тела комментария (без сети)."""

from unittest.mock import MagicMock

from auto_git_review.alm import AlmClient
from auto_git_review.config import Settings


def _settings(**overrides):
    base = dict(
        azure_url="https://alm.example/TFS/GPN",
        azure_project="Proj1",
        azure_pat="pat",
        azure_repo="repo1",
        api_version="6.1-preview",
        llm_url="https://llm.example/v1/chat/completions",
        llm_api_key="key",
        llm_model="qwen3:latest",
        post_comments=False,
    )
    base.update(overrides)
    return Settings(**base)


def test_url_project_scoped():
    client = AlmClient(_settings())
    assert client._url("_apis/git/repositories/r/pullrequests") == (
        "https://alm.example/TFS/GPN/Proj1/_apis/git/repositories/r/pullrequests"
    )


def test_url_collection_scoped():
    client = AlmClient(_settings())
    assert client._url_collection("_apis/wit/workitems/1") == (
        "https://alm.example/TFS/GPN/_apis/wit/workitems/1"
    )


def test_create_thread_comment_body_and_url():
    client = AlmClient(_settings())
    client.session.post = MagicMock(
        return_value=_fake_response({"id": 42})
    )
    thread = client.create_thread_comment(123, "текст комментария")

    assert thread == {"id": 42}
    call_kwargs = client.session.post.call_args
    url, kwargs = call_kwargs.args[0], call_kwargs.kwargs
    assert "pullRequests/123/threads" in url
    body = kwargs["json"]
    assert body["status"] == 1
    assert body["comments"][0]["content"] == "текст комментария"
    assert body["comments"][0]["commentType"] == 1


def _fake_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp
