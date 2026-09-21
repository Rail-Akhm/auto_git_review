"""Тесты AlmClient: построение URL, чтение содержимого файлов и тело комментария."""

from unittest.mock import MagicMock

from auto_git_review import alm
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


def test_decode_content_utf8():
    assert alm._decode_content("Договоры для проекта".encode("utf-8")) == (
        "Договоры для проекта"
    )


def test_decode_content_cp1251_fallback():
    """Файл в CP1251 (старые скрипты репозитория) тоже читается верно."""
    assert alm._decode_content("Договоры".encode("cp1251")) == "Договоры"


def test_decode_content_strips_bom():
    assert alm._decode_content("Договоры".encode("utf-8-sig")) == "Договоры"


def test_decode_content_broken_bytes_does_not_raise():
    """Мусорные байты не должны ронять ревью — возвращаем текст с заменами."""
    out = alm._decode_content(b"\xff\xfe\x98\x81 text")
    assert isinstance(out, str)


def test_get_file_content_decodes_utf8_despite_text_plain_header():
    """Регресс: ALM отдаёт text/plain без charset, а resp.text декодирует
    байты как ISO-8859-1 и портит русский текст в COMMENT'ах."""
    client = AlmClient(_settings())
    raw = "COMMENT ON VIEW v IS 'Договоры';".encode("utf-8")
    resp = MagicMock()
    resp.content = raw
    resp.text = raw.decode("iso-8859-1")  # то, что вернул бы requests
    resp.raise_for_status.return_value = None
    client.session.get = MagicMock(return_value=resp)

    assert client.get_file_content("/a.sql", "sha") == (
        "COMMENT ON VIEW v IS 'Договоры';"
    )
