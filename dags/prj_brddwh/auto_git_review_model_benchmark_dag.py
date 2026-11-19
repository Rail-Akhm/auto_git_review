"""Бенчмарк моделей LLM через LiteLLM-шлюз: контекстное окно, скорость, сравнение.

Зачем. Документация фиксировала ~30k токенов для `qwen3:latest`. При выборе
модели для авторевьюера нужно знать три вещи по каждой модели:

  1. фактическое контекстное окно (его задаёт конфигурация LiteLLM/Ollama на
     сервере — `num_ctx`, лимиты прокси, — а не карточка модели; превышение
     может обрезаться МОЛЧА, без HTTP-ошибки);
  2. скорость на типичных для ревью размерах промпта (сейчас батчи ~8–10k
     токенов, дальше планируется увеличивать);
  3. скорость генерации — насколько быстро модель отвечает при длинном выводе.

ДАГ меряет всё перечисленное для списка моделей, печатает сравнение и
рекомендацию. Модели по умолчанию: `qwen3.8-think`, `qwen3.8-27b-fp8`,
`qwen3.6:35b-a3b-q8_0`.

Как меряется контекст. Пробник отправляет запросы с промптом заданного размера и
двумя кодами-«иголками»: `START-…` в начале пользовательского сообщения и `END-…`
в самом конце. Модель просят вернуть оба кода. Если оба вернулись — промпт дошёл
целиком. Дополнительные признаки предела:

  - HTTP 400 с сообщением о превышении контекста (число из текста сохраняется
    как справочное);
  - `usage.prompt_tokens` заметно ниже калиброванной оценки при выросшем тексте
    (тихое обрезание, даже если иголки уцелели);
  - `finish_reason=length` без кодов — ответ мог обрезаться по лимиту ВЫВОДА, а
    не входа: случай помечается `inconclusive` и перепроверяется с бо́льшим
    `max_tokens`;
  - `GET /models` шлюза — рекламируемые лимиты (справочно, не гарантия).

Стратегия: экспоненциальный рост от `start_tokens` (×`step_factor`) до первого
сбоя, затем бинарный поиск до точности `tolerance_tokens`.

Как меряется скорость. Для размеров из `speed_prompt_tokens` (по умолчанию
2k / 8k / 32k токенов) выполняется `speed_repeats` запросов с коротким ответом;
метрики — медианы задержки, prompt/completion tokens и производные токены/с.
Скорость генерации оценивается отдельно: на промпте `generation_prompt_tokens`
сравниваются прогон с длинным выводом (просьба посчитать до 300) и базовый
прогон с коротким ответом; ток/с = Δcompletion / Δlatency. В начало промпта
добавляется случайный nonce, чтобы кэш шлюза не завысил скорость.

Ограничения методики (учитывать при интерпретации):
  - окно обычно общее для prompt + completion, поэтому ёмкость под вход чуть
    меньше измеренной, если нужен длинный ответ;
  - без стриминга нельзя идеально разделить обработку промпта и генерацию:
    `prompt_tokens_per_sec` / `output_tokens_per_sec` — оценки, а генерация
    измеряется разностью двух прогонов;
  - нагрузка на общий GPU и балансировка на несколько бэкендов влияют на
    числа — для надёжности повторите прогон и сравните медианы;
  - ориентир «батч≈… симв.» считается из калибровки символов на токен — это
    оценка под `MAX_BATCH_CHARS`, проверяйте её на реальных файлах ревью;
  - измеряются эффективные параметры эндпоинта, а не паспортные модели.

Запуск: только ручной (`schedule_interval=None`), Trigger DAG w/ config.
Параметры (все необязательные):

  - models                   : модели через запятую (по умолчанию три модели
                               из шапки);
  - start_tokens             : стартовый размер промпта (4096);
  - max_tokens_limit         : верхняя граница поиска контекста (524288);
  - step_factor              : шаг экспоненциальной фазы (2.0);
  - tolerance_tokens         : точность бинарного поиска (2048);
  - max_output_tokens        : `max_tokens` в пробах контекста (512);
  - max_probes               : предохранитель от зацикливания (40);
  - request_timeout          : таймаут одного запроса, секунды (900);
  - verify_ssl               : проверять TLS (false — самоподписанный сертификат);
  - extra_payload            : JSON-объект, домешиваемый в payload запроса;
  - speed_prompt_tokens      : размеры промпта для замера скорости
                               (по умолчанию `2048,8192,32768`);
  - speed_repeats            : повторов на размер, берётся медиана (2);
  - speed_output_tokens      : лимит вывода в замерах скорости (64);
  - speed_context_margin     : доля измеренного окна, выше которой скорость не
                               замеряется (0.9);
  - typical_prompt_tokens    : размер промпта для главной метрики задержки (8192);
  - generation_prompt_tokens : размер промпта для замера генерации (2048);
  - generation_output_tokens : лимит вывода в замере генерации (1024);
  - required_context_tokens  : минимальный контекст для рекомендации (32768).

Примеры config:

  {}                                       — три модели по умолчанию;
  {"models": "qwen3.8-27b-fp8,qwen3.6:35b-a3b-q8_0"}
  {"speed_prompt_tokens": "8192,32768", "speed_repeats": 1}
  {"required_context_tokens": 65536}       — критерий под рост батчей;
  {"extra_payload": "{\"chat_template_kwargs\": {\"enable_thinking\": false}}"}

Секреты: только Connection `llm_server` (host/port, password = LLM API key).
ALM не нужен — ДАГ обращается исключительно к LLM-шлюзу.
"""

import difflib
import json
import os
import re
import secrets
import time
from datetime import timedelta

import airflow
import requests
import urllib3
from airflow.exceptions import AirflowException
from airflow.hooks.base import BaseHook
from airflow.models import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.log.logging_mixin import LoggingMixin


# Самоподписанные сертификаты на корпоративных хостах (см. config.py).
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DAGConfiguration:
    DAG_NAME = "SERV__auto_git_review_model_benchmark"
    DAG_DESCRIPTION = "Бенчмарк моделей LLM: контекстное окно, скорость, сравнение"

    # Airflow Connection с адресом и ключом LiteLLM-шлюза.
    CONN_LLM = "llm_server"
    LLM_PATH = "/chat/completions"  # префикс /v1 задаётся в host коннектора
    MODELS_PATH = "/models"

    # Значения по умолчанию; переопределяются в Trigger DAG w/ config.
    PARAMS = {
        "models": "qwen3.8-think,qwen3.8-27b-fp8,qwen3.6:35b-a3b-q8_0",
        # Контекстное окно.
        "start_tokens": 4096,
        "max_tokens_limit": 524288,
        "step_factor": 2.0,
        "tolerance_tokens": 2048,
        "max_output_tokens": 512,
        "max_probes": 40,
        "request_timeout": 900,
        "verify_ssl": False,
        "extra_payload": "",
        # Скорость.
        "speed_prompt_tokens": "2048,8192,32768",
        "speed_repeats": 2,
        "speed_output_tokens": 64,
        "speed_context_margin": 0.9,
        "typical_prompt_tokens": 8192,
        "generation_prompt_tokens": 2048,
        "generation_output_tokens": 1024,
        # Порог для рекомендации (запас под рост батчей).
        "required_context_tokens": 32768,
    }

    # Сколько символов ошибки приложить к записи пробы и в лог.
    ERROR_TEXT_CHARS = 800
    # Минимальный размер промпта, который имеет смысл слать.
    MIN_TARGET_TOKENS = 256

    DAG_DEFAULT_ARGS = {
        "owner": "airflow",
        "depends_on_past": False,
        "start_date": airflow.utils.dates.days_ago(1),
        "email_on_failure": False,
        "email_on_retry": False,
        "retries": 0,
        "retry_delay": timedelta(minutes=5),
        "catchup": False,
        "max_active_runs": 1,
        "execution_timeout": timedelta(hours=6),
    }

    RESOURCE_CONFIG = {
        "KubernetesExecutor": {
            "request_memory": "1G",
            "limit_memory": "2G",
            "request_cpu": "500m",
            "limit_cpu": "1000m",
        }
    }


DC = DAGConfiguration
log = LoggingMixin().log

# Наполнитель: повторяющийся латинский текст — стабильная токенизация, без
# случайных данных и без риска, что модель «додумает» смысл.
FILLER_PHRASE = "the quick brown fox jumps over the lazy dog. "

SYSTEM_PROMPT = (
    "You are a context-window probe. The user message contains exactly two "
    "codes: one of the form START-XXXXXX at the beginning and one of the form "
    "END-YYYYYY at the very end. Reply with just those two codes separated by a "
    "space, nothing else. Do not explain."
)
SPEED_SYSTEM_PROMPT = "You are a benchmark probe. Follow the user instruction exactly."
SPEED_ANSWER_INSTRUCTION = "Reply with the single word OK."
GENERATION_INSTRUCTION = (
    "Count from 1 to 300, separated by single spaces. Output only the numbers."
)


# --------------------------------------------------------------------------
# Сборка/разбор параметров
# --------------------------------------------------------------------------


def _build_url(scheme, host, port, path):
    """Собирает URL из host/port/path. host может быть уже полным URL."""
    if not host:
        return ""
    if host.startswith("http://") or host.startswith("https://"):
        base = host.rstrip("/")
    else:
        base = f"{scheme}://{host}"
        if port and str(port) not in ("80", "443"):
            base += f":{port}"
    return base + path


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int_list(value, default):
    """`"2048,8192"` или `[2048, 8192]` -> отсортированный список положительных int."""
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = re.split(r"[,;\s]+", str(value or ""))
    numbers = set()
    for part in parts:
        if str(part).strip() == "":
            continue
        try:
            number = int(part)
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.add(number)
    return sorted(numbers) or list(default)


def _as_mapping(raw):
    """extra_payload: пусто -> {}, dict -> как есть, строка -> json.loads."""
    if isinstance(raw, dict):
        return raw
    if raw is None or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AirflowException(f"extra_payload — некорректный JSON: {exc}")
    if not isinstance(data, dict):
        raise AirflowException("extra_payload должен быть JSON-объектом")
    return data


def _resolve_params(context):
    raw = dict(DC.PARAMS)
    raw.update(context.get("params") or {})

    models = [
        part.strip()
        for part in re.split(r"[,;\n]+", str(raw.get("models") or ""))
        if part.strip()
    ]
    if not models:
        raise AirflowException("Параметр models пуст: укажите имя модели")

    start = max(DC.MIN_TARGET_TOKENS, _as_int(raw.get("start_tokens"), 4096))
    limit = max(start, _as_int(raw.get("max_tokens_limit"), 524288))

    return {
        "models": models,
        "start_tokens": start,
        "max_tokens_limit": limit,
        "step_factor": max(1.1, _as_float(raw.get("step_factor"), 2.0)),
        "tolerance_tokens": max(64, _as_int(raw.get("tolerance_tokens"), 2048)),
        "max_output_tokens": max(32, _as_int(raw.get("max_output_tokens"), 512)),
        "max_probes": max(2, _as_int(raw.get("max_probes"), 40)),
        "request_timeout": max(30, _as_int(raw.get("request_timeout"), 900)),
        "verify_ssl": _as_bool(raw.get("verify_ssl"), False),
        "extra_payload": _as_mapping(raw.get("extra_payload")),
        "speed_prompt_tokens": _as_int_list(
            raw.get("speed_prompt_tokens"), [2048, 8192, 32768]
        ),
        "speed_repeats": max(1, _as_int(raw.get("speed_repeats"), 2)),
        "speed_output_tokens": max(8, _as_int(raw.get("speed_output_tokens"), 64)),
        "speed_context_margin": min(
            1.0, max(0.5, _as_float(raw.get("speed_context_margin"), 0.9))
        ),
        "typical_prompt_tokens": max(
            256, _as_int(raw.get("typical_prompt_tokens"), 8192)
        ),
        "generation_prompt_tokens": max(
            256, _as_int(raw.get("generation_prompt_tokens"), 2048)
        ),
        "generation_output_tokens": max(
            32, _as_int(raw.get("generation_output_tokens"), 1024)
        ),
        "required_context_tokens": max(
            0, _as_int(raw.get("required_context_tokens"), 32768)
        ),
    }


def _resolve_llm_endpoint():
    """URL чата и API-ключ: env имеет приоритет, затем Connection llm_server."""
    conn = BaseHook.get_connection(DC.CONN_LLM)
    api_key = os.environ.get("LLM_API_KEY") or conn.get_password() or conn.login or ""
    api_key = api_key.strip()
    url = os.environ.get("LLM_URL") or _build_url(
        "https", conn.host, conn.port, DC.LLM_PATH
    )
    if not url:
        raise AirflowException(
            f"Не удалось определить LLM URL: Connection «{DC.CONN_LLM}» пуст "
            f"и LLM_URL не задан"
        )
    if not api_key:
        raise AirflowException(
            f"Не задан LLM API key: Connection «{DC.CONN_LLM}» (password) "
            f"или LLM_API_KEY"
        )
    log.info("LLM endpoint: %s", url)
    return url.rstrip("/"), api_key


# --------------------------------------------------------------------------
# Справочные лимиты из GET /models
# --------------------------------------------------------------------------


def _models_url(chat_url):
    if chat_url.endswith(DC.LLM_PATH):
        return chat_url[: -len(DC.LLM_PATH)] + DC.MODELS_PATH
    return chat_url.rsplit("/", 1)[0] + DC.MODELS_PATH


def _extract_limits(item):
    """Вытаскивает из записи /models любые числовые лимиты (справочно)."""
    found = {}
    for key in (
        "max_input_tokens",
        "max_output_tokens",
        "max_tokens",
        "context_window",
        "context_length",
    ):
        if isinstance(item.get(key), int):
            found[key] = item[key]
    for container in ("litellm_params", "model_info"):
        nested = item.get(container) or {}
        if not isinstance(nested, dict):
            continue
        for key in ("max_input_tokens", "max_output_tokens", "max_tokens", "num_ctx"):
            if isinstance(nested.get(key), int):
                found[f"{container}.{key}"] = nested[key]
    return found


def _fetch_advertised_limits(models_url, api_key, verify_ssl, timeout):
    """{model_id: {лимит: значение}} или ({}, текст ошибки) при неудаче."""
    try:
        resp = requests.get(
            models_url,
            headers={"x-litellm-api-key": api_key},
            verify=verify_ssl,
            timeout=min(timeout, 60),
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except Exception as exc:  # сеть, TLS, не-JSON — это только справочная инфа
        return {}, f"{type(exc).__name__}: {exc}"

    limits = {}
    for item in data:
        model_id = item.get("id")
        if model_id:
            limits[model_id] = _extract_limits(item)
    return limits, None


# --------------------------------------------------------------------------
# Общий HTTP-слой
# --------------------------------------------------------------------------


def _parse_advertised_limit(text):
    """Число из сообщения об ошибке («maximum context length is 131072 tokens»)."""
    if not text:
        return None
    low = text.lower()
    for pattern in _LIMIT_PATTERNS:
        match = re.search(pattern, low)
        if match:
            digits = re.sub(r"[^\d]", "", match.group(1))
            if digits:
                return int(digits)
    return None


_LIMIT_PATTERNS = (
    r"maximum context length is\s+([\d,\. ]+)\s*tokens",
    r"max(?:imum)?[ _-]*(?:context|input)[ _-]*(?:length|window|tokens)?[^\d]{0,24}([\d,\. ]{3,})",
    r"context (?:length|window)[^\d]{0,24}([\d,\. ]{3,})",
    r"num_ctx[^\d]{0,12}([\d,\. ]{3,})",
)


def _post_chat(*, url, api_key, verify_ssl, timeout, payload):
    """Один POST chat/completions с разбором транспорта/HTTP/JSON.

    Возвращает dict с полями http_status, latency_ms, data, usage, error,
    inconclusive, advertised_limit. Ошибки не пробрасывает — их классифицирует
    вызывающий код.
    """
    result = {
        "http_status": None,
        "latency_ms": None,
        "data": None,
        "usage": {},
        "error": None,
        "inconclusive": False,
        "advertised_limit": None,
    }
    try:
        started = time.monotonic()
        resp = requests.post(
            url,
            headers={
                "x-litellm-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            verify=verify_ssl,
            timeout=timeout,
        )
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except requests.exceptions.RequestException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[: DC.ERROR_TEXT_CHARS]
        result["inconclusive"] = True
        return result

    result["http_status"] = resp.status_code
    if resp.status_code != 200:
        text = (resp.text or "")[:2000]
        result["error"] = text[: DC.ERROR_TEXT_CHARS]
        result["advertised_limit"] = _parse_advertised_limit(text)
        # 5xx/429 — не признак предела контекста, а сбой сервиса.
        result["inconclusive"] = resp.status_code >= 500 or resp.status_code == 429
        return result

    try:
        result["data"] = resp.json()
    except ValueError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[: DC.ERROR_TEXT_CHARS]
        result["inconclusive"] = True
        return result

    result["usage"] = result["data"].get("usage") or {}
    return result


# --------------------------------------------------------------------------
# Контекстное окно
# --------------------------------------------------------------------------


def _make_filler(chars):
    """Детерминированный текст ровно заданной длины."""
    if chars <= 0:
        return ""
    repeats = chars // len(FILLER_PHRASE) + 1
    return (FILLER_PHRASE * repeats)[:chars]


def _probe_once(
    *,
    url,
    api_key,
    verify_ssl,
    timeout,
    model,
    target_tokens,
    chars_per_token,
    max_output_tokens,
    extra_payload,
):
    """Один запрос к LLM и разбор его результата в запись пробы."""
    start_code = f"START-{secrets.token_hex(3).upper()}"
    end_code = f"END-{secrets.token_hex(3).upper()}"
    filler_chars = max(0, int(target_tokens * chars_per_token))
    filler = _make_filler(filler_chars)
    # Инструкция дублируется перед END-кодом: модели-«рассуждалки» лучше
    # следуют просьбе, если она рядом с последним кодом. END-код остаётся
    # последним — иначе не поймать обрезание хвоста промпта.
    user_content = (
        f"{start_code}\n\n{filler}\n"
        f"Reply with exactly the two codes, nothing else. {end_code}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": False,
    }
    payload.update(extra_payload)

    record = {
        "model": model,
        "target_prompt_tokens": target_tokens,
        "sent_chars": len(user_content),
        "http_status": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "finish_reason": None,
        "start_seen": False,
        "end_seen": False,
        "latency_ms": None,
        "ok": False,
        "inconclusive": False,
        "advertised_limit": None,
        "reason": None,
        "error": None,
    }

    response = _post_chat(
        url=url,
        api_key=api_key,
        verify_ssl=verify_ssl,
        timeout=timeout,
        payload=payload,
    )
    record["http_status"] = response["http_status"]
    record["latency_ms"] = response["latency_ms"]
    record["error"] = response["error"]
    record["advertised_limit"] = response["advertised_limit"]

    if response["http_status"] is None:
        # Транспортная ошибка (сеть/TLS/таймаут).
        record["inconclusive"] = True
        record["reason"] = "connection_error"
        return record

    if response["http_status"] != 200:
        record["inconclusive"] = response["inconclusive"]
        if response["inconclusive"]:
            record["reason"] = "server_error"
        elif record["advertised_limit"] and "context" in (record["error"] or "").lower():
            record["reason"] = "context_exceeded"
        else:
            record["reason"] = "http_error"
        return record

    if response["data"] is None:
        # 200, но тело не JSON.
        record["inconclusive"] = True
        record["reason"] = "bad_json"
        return record

    usage = response["usage"]
    choice = (response["data"].get("choices") or [{}])[0]
    message = choice.get("message") or {}
    # У «thinking»-моделей ответ может лежать в reasoning_content.
    answer = (
        (message.get("content") or "") + "\n" + (message.get("reasoning_content") or "")
    ).upper()

    record.update(
        {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": choice.get("finish_reason"),
            "start_seen": start_code in answer,
            "end_seen": end_code in answer,
        }
    )

    if record["start_seen"] and record["end_seen"]:
        record["ok"] = True
        record["reason"] = "ok"
    elif record["finish_reason"] == "length":
        # Ответ обрезан по лимиту вывода — про контекст входа ничего не сказал.
        record["inconclusive"] = True
        record["reason"] = "output_truncated"
    else:
        record["reason"] = "needle_missing"
    return record


def _probe(*, max_output_tokens, log, **kwargs):
    """Проба с одной перепроверкой, если ответ обрезан по лимиту вывода."""
    record = _probe_once(max_output_tokens=max_output_tokens, **kwargs)
    if record.get("reason") == "output_truncated":
        bigger = min(max_output_tokens * 4, 4096)
        log.info(
            "    ответ обрезан по лимиту вывода — повтор с max_tokens=%d", bigger
        )
        record = _probe_once(max_output_tokens=bigger, **kwargs)
        record["retried_for_output"] = True
    return record


def _log_record(record):
    if record["error"]:
        detail = record["error"].replace("\n", " ")[:200]
    else:
        detail = ""
    log.info(
        "  [%s] цель=%d ток. (%d симв.): %s, prompt_tokens=%s, "
        "finish=%s, %sms %s",
        record["model"],
        record["target_prompt_tokens"],
        record["sent_chars"],
        record["reason"],
        record["prompt_tokens"],
        record["finish_reason"],
        record["latency_ms"],
        detail,
    )


def _search_model(model, endpoint, params, log):
    """Экспоненциальный рост + бинарный поиск фактического предела модели."""
    url, api_key = endpoint
    verify_ssl = params["verify_ssl"]
    timeout = params["request_timeout"]
    max_output_tokens = params["max_output_tokens"]
    extra_payload = params["extra_payload"]

    records = []
    chars_per_token = 4.0  # стартовая оценка; уточняется по usage.prompt_tokens
    best_ok = None
    budget = params["max_probes"]

    def do_probe(target):
        nonlocal chars_per_token, best_ok, budget
        budget -= 1
        record = _probe(
            url=url,
            api_key=api_key,
            verify_ssl=verify_ssl,
            timeout=timeout,
            model=model,
            target_tokens=target,
            chars_per_token=chars_per_token,
            max_output_tokens=max_output_tokens,
            extra_payload=extra_payload,
            log=log,
        )

        # Тихое обрезание: текст заметно вырос, а prompt_tokens почти не вырос
        # относительно калиброванной оценки. Иголки при этом могут уцелеть, если
        # сервер режет середину, — ловим именно по плато usage. Проверяем ДО
        # обновления калибровки: у обрезанной пробы отношение sent_chars/
        # prompt_tokens завышено и испортило бы оценку.
        expected_tokens = (
            record["sent_chars"] / chars_per_token if record["sent_chars"] else 0
        )
        if (
            record["ok"]
            and best_ok
            and record.get("prompt_tokens")
            and best_ok.get("prompt_tokens")
            and record["sent_chars"] > best_ok["sent_chars"] * 1.2
            and expected_tokens > best_ok["prompt_tokens"] * 1.1
            and record["prompt_tokens"] < expected_tokens * 0.8
        ):
            record["ok"] = False
            record["reason"] = "silent_truncation"

        # Уточняем оценку «символов на токен» по фактическому usage.
        if record["ok"] and record.get("prompt_tokens"):
            chars_per_token = min(
                10.0, max(1.0, record["sent_chars"] / record["prompt_tokens"])
            )

        if record["ok"] and (
            not best_ok
            or (record.get("prompt_tokens") or 0) > (best_ok.get("prompt_tokens") or 0)
        ):
            best_ok = record

        records.append(record)
        _log_record(record)
        return record

    # --- Фаза 1: экспоненциальный рост до первого сбоя -------------------
    target = params["start_tokens"]
    last_ok_target = None
    failure = None
    while target <= params["max_tokens_limit"] and budget > 0:
        record = do_probe(target)
        if record["reason"] == "connection_error" or record["inconclusive"]:
            return _conclusion(
                model,
                records,
                best_ok,
                last_ok_target,
                record,
                "inconclusive",
                chars_per_token,
            )
        if record["ok"]:
            last_ok_target = target
            target = int(target * params["step_factor"])
        else:
            failure = record
            break

    if failure is None:
        status = "at_least_limit" if last_ok_target is not None else "below_start"
        return _conclusion(
            model, records, best_ok, last_ok_target, None, status, chars_per_token
        )

    if last_ok_target is None:
        # Даже стартовый размер не прошёл — предел ниже start_tokens.
        return _conclusion(
            model, records, best_ok, None, failure, "below_start", chars_per_token
        )

    # --- Фаза 2: бинарный поиск между успехом и сбоем --------------------
    low = last_ok_target
    high = failure["target_prompt_tokens"]
    while high - low > params["tolerance_tokens"] and budget > 0:
        mid = (low + high) // 2
        record = do_probe(mid)
        if record["reason"] == "connection_error" or record["inconclusive"]:
            return _conclusion(
                model, records, best_ok, low, record, "inconclusive", chars_per_token
            )
        if record["ok"]:
            low = mid
        else:
            high = mid
            failure = record

    return _conclusion(
        model, records, best_ok, low, failure, "measured", chars_per_token
    )


def _conclusion(
    model, records, best_ok, max_ok_target, failure, status, chars_per_token
):
    advertised = next(
        (r["advertised_limit"] for r in records if r.get("advertised_limit")), None
    )
    # Предел, названный самим шлюзом в ответе ContextWindowExceededError, — это
    # уже не догадка, а подтверждённое значение окна.
    confirmed = next(
        (
            r["advertised_limit"]
            for r in records
            if r.get("reason") == "context_exceeded" and r.get("advertised_limit")
        ),
        None,
    )
    return {
        "model": model,
        "status": status,
        "max_ok_prompt_tokens": best_ok.get("prompt_tokens") if best_ok else None,
        "max_ok_target_tokens": max_ok_target,
        "first_failed_target_tokens": (
            failure.get("target_prompt_tokens") if failure else None
        ),
        "failure_reason": failure.get("reason") if failure else None,
        "advertised_limit_from_error": advertised,
        "confirmed_context_limit": confirmed,
        "chars_per_token": chars_per_token,
        "probes": records,
    }


def _format_context_summary(result):
    model = result["model"]
    status = result["status"]
    ok_tokens = result["max_ok_prompt_tokens"]
    failed_at = result["first_failed_target_tokens"]
    advertised = result["advertised_limit_from_error"] or next(
        iter(result.get("advertised_limits_from_models", {}).values()), None
    )

    if status == "measured":
        if result.get("confirmed_context_limit"):
            text = (
                f"Контекст {model}: окно = {result['confirmed_context_limit']} "
                f"токенов (подтверждено ответом шлюза; последняя успешная "
                f"проба — {ok_tokens} prompt-токенов)."
            )
        else:
            text = (
                f"Контекст {model}: измеренное окно — не меньше {ok_tokens} "
                f"prompt-токенов; первый сбой на цели {failed_at} "
                f"({result['failure_reason']})."
            )
            if result["max_ok_target_tokens"] is not None:
                text += (
                    f" Диапазон целей: [{result['max_ok_target_tokens']}, "
                    f"{failed_at}] токенов."
                )
    elif status == "at_least_limit":
        text = (
            f"Контекст {model}: до верхней границы поиска сбоев нет — окно не "
            f"меньше {ok_tokens} prompt-токенов (лимит поиска исчерпан)."
        )
    elif status == "below_start":
        first_probe = result["probes"][0] if result["probes"] else {}
        error_text = (first_probe.get("error") or "").lower()
        if (
            "invalid model name" in error_text
            or "model not found" in error_text
            or "unknown model" in error_text
        ):
            text = (
                f"Контекст {model}: модель не найдена на шлюзе — проверьте имя "
                f"по списку /models в начале лога (похожие имена ДАГ "
                f"подсказывает предупреждением)."
            )
            return text
        text = (
            f"Контекст {model}: сбой уже на стартовом размере "
            f"({failed_at} токенов) — уменьшите start_tokens или проверьте "
            f"ошибку пробы."
        )
        if first_probe.get("error"):
            text += " Ошибка: " + first_probe["error"].replace("\n", " ")[:300]
    else:
        text = (
            f"Контекст {model}: замер не завершён ({result['failure_reason']}) — "
            f"последний успешный размер {ok_tokens} prompt-токенов."
        )

    if result.get("confirmed_context_limit") and status != "measured":
        text += (
            f" Предел {result['confirmed_context_limit']} токенов подтверждён "
            f"ответом шлюза."
        )
    elif not result.get("confirmed_context_limit") and advertised:
        text += f" Рекламируемый лимит: {advertised} токенов (справочно)."
    return text


# --------------------------------------------------------------------------
# Скорость
# --------------------------------------------------------------------------


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _speed_once(
    *,
    url,
    api_key,
    verify_ssl,
    timeout,
    model,
    target_tokens,
    chars_per_token,
    max_output_tokens,
    instruction,
    extra_payload,
):
    """Один замер скорости: фиксированный промпт + короткий/длинный ответ."""
    # Nonce в начале промпта исключает попадание в кэш шлюза при повторах и
    # сравнении моделей — иначе более поздняя модель выглядела бы быстрее.
    nonce = secrets.token_hex(8)
    filler = _make_filler(max(0, int(target_tokens * chars_per_token)))
    user_content = f"nonce:{nonce}\n{filler}\n{instruction}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SPEED_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": False,
    }
    payload.update(extra_payload)

    record = {
        "model": model,
        "target_prompt_tokens": target_tokens,
        "sent_chars": len(user_content),
        "http_status": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "finish_reason": None,
        "latency_ms": None,
        "ok": False,
        "inconclusive": False,
        "error": None,
    }

    response = _post_chat(
        url=url,
        api_key=api_key,
        verify_ssl=verify_ssl,
        timeout=timeout,
        payload=payload,
    )
    record.update(
        {
            "http_status": response["http_status"],
            "latency_ms": response["latency_ms"],
            "error": response["error"],
            "inconclusive": response["inconclusive"],
        }
    )
    if response["data"] is None:
        return record

    usage = response["usage"]
    choice = (response["data"].get("choices") or [{}])[0]
    record.update(
        {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": choice.get("finish_reason"),
            # Содержимое для замера скорости неважно: важен только успешный
            # ответ с usage, чтобы посчитать токены и задержку.
            "ok": usage.get("prompt_tokens") is not None,
        }
    )
    return record


def _aggregate_speed(target_tokens, samples):
    """Медианы по повторам одного размера промпта."""
    ok_samples = [s for s in samples if s["ok"]]
    latency = _median([s.get("latency_ms") for s in ok_samples])
    prompt_tokens = _median([s.get("prompt_tokens") for s in ok_samples])
    completion_tokens = _median([s.get("completion_tokens") for s in ok_samples])
    seconds = latency / 1000 if latency else None
    truncated = sum(
        1 for s in ok_samples if s.get("finish_reason") == "length"
    )
    return {
        "target_prompt_tokens": target_tokens,
        "samples": samples,
        "failures": len(samples) - len(ok_samples),
        # Ответы, упёршиеся в max_tokens: задержка измерена «до лимита вывода»,
        # а не до завершения ответа (типично для «thinking»-моделей).
        "truncated_samples": truncated,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "median_latency_ms": latency,
        "prompt_tokens_per_sec": (
            round(prompt_tokens / seconds, 1)
            if prompt_tokens and seconds
            else None
        ),
        "output_tokens_per_sec": (
            round(completion_tokens / seconds, 1)
            if completion_tokens and seconds
            else None
        ),
    }


def _log_speed_record(record):
    detail = (record.get("error") or "").replace("\n", " ")[:160]
    log.info(
        "  [%s] скорость @%d ток.: %sms, prompt=%s, completion=%s, finish=%s %s",
        record["model"],
        record["target_prompt_tokens"],
        record["latency_ms"],
        record["prompt_tokens"],
        record["completion_tokens"],
        record["finish_reason"],
        detail,
    )


def _measure_speed(model, endpoint, params, context_result):
    """Замер скорости на размерах промпта, укладывающихся в измеренное окно."""
    url, api_key = endpoint
    chars_per_token = context_result.get("chars_per_token") or 4.0
    max_ok = context_result.get("max_ok_prompt_tokens")
    if not max_ok:
        return {"skipped": "контекстное окно не измерено — скорость не замерялась"}

    safe_limit = int(max_ok * params["speed_context_margin"])
    wanted = set(params["speed_prompt_tokens"])
    if params["generation_prompt_tokens"] <= safe_limit:
        wanted.add(params["generation_prompt_tokens"])
    sizes = sorted(size for size in wanted if size <= safe_limit)
    if not sizes:
        return {
            "skipped": (
                f"безопасный размер {safe_limit} токенов меньше минимального "
                f"speed_prompt_tokens — уменьшите размеры"
            )
        }

    def run(target_tokens, max_output_tokens, instruction):
        return _speed_once(
            url=url,
            api_key=api_key,
            verify_ssl=params["verify_ssl"],
            timeout=params["request_timeout"],
            model=model,
            target_tokens=target_tokens,
            chars_per_token=chars_per_token,
            max_output_tokens=max_output_tokens,
            instruction=instruction,
            extra_payload=params["extra_payload"],
        )

    entries = []
    for size in sizes:
        samples = []
        for _ in range(params["speed_repeats"]):
            record = run(size, params["speed_output_tokens"], SPEED_ANSWER_INSTRUCTION)
            samples.append(record)
            _log_speed_record(record)
        entry = _aggregate_speed(size, samples)
        if entry["truncated_samples"]:
            log.warning(
                "  размер %d: %d ответ(ов) обрезано по max_tokens=%d — задержка "
                "измерена до лимита вывода, а не до завершения ответа",
                size,
                entry["truncated_samples"],
                params["speed_output_tokens"],
            )
        entries.append(entry)

    measured = [e for e in entries if e["median_latency_ms"] is not None]
    typical = (
        min(
            measured,
            key=lambda e: abs(e["target_prompt_tokens"] - params["typical_prompt_tokens"]),
        )
        if measured
        else None
    )

    generation = None
    gen_size = params["generation_prompt_tokens"]
    baseline = next(
        (e for e in entries if e["target_prompt_tokens"] == gen_size), None
    )
    if gen_size <= safe_limit and baseline and baseline["median_latency_ms"]:
        gen_samples = []
        for _ in range(params["speed_repeats"]):
            record = run(
                gen_size, params["generation_output_tokens"], GENERATION_INSTRUCTION
            )
            gen_samples.append(record)
            _log_speed_record(record)
        gen_entry = _aggregate_speed(gen_size, gen_samples)
        delta_ms = (gen_entry["median_latency_ms"] or 0) - baseline["median_latency_ms"]
        delta_tokens = (gen_entry["completion_tokens"] or 0) - (
            baseline["completion_tokens"] or 0
        )
        gen_entry["baseline_latency_ms"] = baseline["median_latency_ms"]
        gen_entry["baseline_completion_tokens"] = baseline["completion_tokens"]
        gen_entry["generation_tokens_per_sec"] = (
            round(delta_tokens / (delta_ms / 1000), 1)
            if delta_ms > 0 and delta_tokens > 0
            else None
        )
        generation = gen_entry

    return {
        "safe_prompt_tokens": safe_limit,
        "sizes": entries,
        "typical_prompt_tokens": typical["target_prompt_tokens"] if typical else None,
        "typical_latency_ms": typical["median_latency_ms"] if typical else None,
        "generation": generation,
    }


# --------------------------------------------------------------------------
# Сравнение моделей
# --------------------------------------------------------------------------


def _rank_key(row):
    """Сначала модели с подтверждённым контекстом и замеренной скоростью."""
    if row["meets_required_context"] and row["typical_latency_ms"] is not None:
        return (0, row["typical_latency_ms"], -(row["context_tokens"] or 0))
    return (
        1,
        -(row["context_tokens"] or 0),
        row["typical_latency_ms"]
        if row["typical_latency_ms"] is not None
        else float("inf"),
    )


def _compare_models(results, params):
    required = params["required_context_tokens"]
    rows = []
    for model, result in results.items():
        speed = result.get("speed") or {}
        generation = speed.get("generation") or {}
        measured_context = result.get("max_ok_prompt_tokens")
        confirmed_limit = result.get("confirmed_context_limit")
        # Подтверждённый шлюзом предел точнее «последней успешной пробы».
        context_tokens = confirmed_limit or measured_context
        rows.append(
            {
                "model": model,
                "context_tokens": context_tokens,
                "context_confirmed": bool(confirmed_limit),
                "max_ok_prompt_tokens": measured_context,
                "context_at_least": result.get("status") == "at_least_limit",
                "context_status": result.get("status"),
                "typical_prompt_tokens": speed.get("typical_prompt_tokens"),
                "typical_latency_ms": speed.get("typical_latency_ms"),
                "generation_tokens_per_sec": generation.get(
                    "generation_tokens_per_sec"
                ),
                "meets_required_context": bool(
                    context_tokens and context_tokens >= required
                ),
                # Практический ориентир для MAX_BATCH_CHARS ревьюера: безопасный
                # размер промпта (уже с запасом speed_context_margin) в символах.
                "recommended_batch_chars": (
                    int(speed["safe_prompt_tokens"] * result["chars_per_token"])
                    if speed.get("safe_prompt_tokens")
                    and result.get("chars_per_token")
                    else None
                ),
            }
        )

    ranked = sorted(rows, key=_rank_key)
    eligible = [
        row for row in ranked if row["meets_required_context"] and row["typical_latency_ms"]
    ]
    if eligible:
        best = eligible[0]
        why = (
            f"контекст ≥ {required} токенов и наименьшая задержка "
            f"{best['typical_latency_ms']} мс на ~{best['typical_prompt_tokens']} токенах"
        )
    elif ranked:
        best = max(
            ranked,
            key=lambda r: (
                r["context_tokens"] or 0,
                -(r["typical_latency_ms"] or float("inf")),
            ),
        )
        why = (
            f"ни одна модель не подтвердила контекст ≥ {required} токенов; "
            f"выбрана модель с наибольшим измеренным окном"
        )
    else:
        best, why = None, "нет результатов"

    return {
        "required_context_tokens": required,
        "typical_prompt_tokens": params["typical_prompt_tokens"],
        "rows": ranked,
        "ranking": [row["model"] for row in ranked],
        "recommendation": (
            {"model": best["model"], "why": why} if best else None
        ),
    }


def _format_comparison(comparison):
    required = comparison["required_context_tokens"]
    lines = [
        "Сравнение моделей для авторевьюера "
        f"(критерий: контекст ≥ {required} prompt-токенов, затем задержка):"
    ]

    for position, row in enumerate(comparison["rows"], start=1):
        context = row["context_tokens"]
        if context is None:
            context_text = "не измерен"
        elif row.get("context_confirmed"):
            context_text = f"{context} (подтв.)"
        elif row["context_at_least"]:
            context_text = f"≥{context}"
        else:
            context_text = str(context)
        latency = row["typical_latency_ms"]
        latency_text = (
            f"{latency} мс @~{row['typical_prompt_tokens']} ток."
            if latency is not None
            else "не замерена"
        )
        generation = row["generation_tokens_per_sec"]
        generation_text = f"{generation} ток/с" if generation is not None else "—"
        batch = row.get("recommended_batch_chars")
        batch_text = (
            f", батч≈{batch} симв." if batch is not None else ""
        )
        verdict = "проходит" if row["meets_required_context"] else "не проходит"
        lines.append(
            f"  {position}. {row['model']}: контекст={context_text}, "
            f"задержка={latency_text}, генерация={generation_text}"
            f"{batch_text}, критерий={verdict}"
        )

    recommendation = comparison.get("recommendation")
    if recommendation:
        lines.append(
            f"Рекомендация: {recommendation['model']} — {recommendation['why']}."
        )
    else:
        lines.append("Рекомендация: нет данных — проверьте ошибки в логе.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Таска ДАГа
# --------------------------------------------------------------------------


def run_benchmark_wrapper(**context):
    """Таска: контекст + скорость для каждой модели и сравнение результатов."""
    params = _resolve_params(context)
    endpoint = _resolve_llm_endpoint()
    url, api_key = endpoint

    limits, limits_error = _fetch_advertised_limits(
        _models_url(url), api_key, params["verify_ssl"], params["request_timeout"]
    )
    if limits_error:
        log.info("GET /models недоступен (%s) — справочные лимиты пропущены", limits_error)
    else:
        log.info("Модели в /models: %s", ", ".join(sorted(limits)) or "нет данных")

    results = {}
    for model in params["models"]:
        log.info("=" * 60)
        log.info("Бенчмарк модели: %s", model)
        if limits and model not in limits:
            suggestions = difflib.get_close_matches(
                model, sorted(limits), n=3, cutoff=0.5
            )
            log.warning(
                "Модели «%s» нет в /models%s",
                model,
                f". Похожие имена: {', '.join(suggestions)}" if suggestions else "",
            )
        advertised = limits.get(model) or {}
        log.info("Рекламируемые лимиты (/models): %s", advertised or "нет данных")

        result = _search_model(model, endpoint, params, log)
        result["advertised_limits_from_models"] = advertised

        first_probe = result["probes"][0] if result["probes"] else {}
        if first_probe.get("http_status") in (401, 403):
            raise AirflowException(
                f"LLM-шлюз отклонил ключ (401/403) для модели {model}. "
                f"Проверьте password Connection llm_server / LLM_API_KEY"
            )
        if first_probe.get("reason") == "connection_error":
            raise AirflowException(
                f"LLM-шлюз недоступен для модели {model}: проверьте Connection "
                f"llm_server и сеть воркера"
            )

        context_summary = _format_context_summary(result)
        result["summary"] = context_summary
        log.info("%s", context_summary)

        log.info("Замер скорости (безопасный размер ≤ %s%% окна):", int(
            params["speed_context_margin"] * 100
        ))
        result["speed"] = _measure_speed(model, endpoint, params, result)
        if "skipped" in result["speed"]:
            log.info("  пропущено: %s", result["speed"]["skipped"])
        else:
            log.info(
                "  задержка @~%s ток.: %s мс; генерация: %s",
                result["speed"]["typical_prompt_tokens"],
                result["speed"]["typical_latency_ms"],
                (result["speed"].get("generation") or {}).get(
                    "generation_tokens_per_sec"
                ),
            )

        results[model] = result

    comparison = _compare_models(results, params)
    log.info("=" * 60)
    for line in _format_comparison(comparison).splitlines():
        log.info("%s", line)

    compact = {
        model: {
            "context_status": result["status"],
            "context_tokens": result["max_ok_prompt_tokens"],
            "confirmed_context_limit": result.get("confirmed_context_limit"),
            "context_at_least": result["status"] == "at_least_limit",
            "failure_reason": result["failure_reason"],
            "advertised_limit_from_error": result["advertised_limit_from_error"],
            "typical_prompt_tokens": (result.get("speed") or {}).get(
                "typical_prompt_tokens"
            ),
            "typical_latency_ms": (result.get("speed") or {}).get(
                "typical_latency_ms"
            ),
            "generation_tokens_per_sec": (
                ((result.get("speed") or {}).get("generation") or {}).get(
                    "generation_tokens_per_sec"
                )
            ),
            "recommended_batch_chars": (
                int(
                    (result.get("speed") or {}).get("safe_prompt_tokens")
                    * result["chars_per_token"]
                )
                if (result.get("speed") or {}).get("safe_prompt_tokens")
                and result.get("chars_per_token")
                else None
            ),
        }
        for model, result in results.items()
    }
    log.info(
        "MODEL_BENCHMARK_RESULTS %s",
        json.dumps(
            {"models": compact, "comparison": comparison}, ensure_ascii=False
        ),
    )

    return {"models": results, "comparison": comparison}


with DAG(
    dag_id=DC.DAG_NAME,
    description=DC.DAG_DESCRIPTION,
    schedule_interval=None,
    tags=["llm", "benchmark", "context", "speed", "auto_git_review"],
    max_active_runs=1,
    default_args=DC.DAG_DEFAULT_ARGS,
    params=DC.PARAMS,
) as dag:
    PythonOperator(
        task_id="benchmark_models",
        python_callable=run_benchmark_wrapper,
        executor_config=DC.RESOURCE_CONFIG,
    )
