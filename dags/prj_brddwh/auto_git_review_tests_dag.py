"""ДАГ ручного прогона unit-тестов проекта: все сразу или выбранные.

Зачем отдельный ДАГ. Тесты лежат в `loader/src/auto_git_review/tests/` и рабочим
ДАГом ревью не запускаются: Airflow обходит только `dags_folder`, а таска ревью
выполняет ровно свой `python_callable` (`review.run_review`). Этот ДАГ — вторая,
ручная точка входа: он вызывает pytest подпроцессом из корня проекта, поэтому
подхватывается `pytest.ini` (`pythonpath = loader/src`, `testpaths = ...`).

Параметры задаются при запуске (Trigger DAG w/ config), все необязательные:

  - test_target : что запускать. Пустая строка или `all` — все тесты;
                  путь к файлу/каталогу (`test_alm.py`, `tests/`), конкретный тест
                  (`test_alm.py::test_decode_content_utf8`), несколько целей через
                  запятую; просто имя теста (`test_decode_content`) уходит в `-k`.
  - keyword     : выражение `-k` для фильтра по именам (`not eval`).
  - fail_fast   : остановиться на первом падении (`-x`).
  - collect_only: не запускать, только собрать и показать список тестов.
  - extra_args  : дополнительные аргументы pytest строкой.

Примеры config:

  {}                                                        — все тесты;
  {"test_target": "test_alm.py"}                            — один файл;
  {"test_target": "test_alm.py::test_decode_content_utf8"}  — один тест;
  {"test_target": "test_alm.py, test_llm.py"}               — два файла;
  {"test_target": "test_decode_content, test_batch"}        — по именам (-k);
  {"keyword": "not eval", "fail_fast": true}                — фильтр + стоп на падении;
  {"collect_only": true}                                    — список тестов.

Секреты и Connections ДАГу не нужны: тесты не обращаются ни к ALM, ни к LLM.
pytest относится к зависимостям разработки (`requirements-dev.txt`) — в окружении
воркера он должен быть установлен, иначе таска падает с понятной подсказкой.
"""

import os
import re
import shlex
import subprocess
import sys
from datetime import timedelta

import airflow
from airflow.exceptions import AirflowException
from airflow.models import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.log.logging_mixin import LoggingMixin


class DAGConfiguration:
    DAG_NAME = "SERV__auto_git_review_tests"
    DAG_DESCRIPTION = "Прогон unit-тестов auto_git_review: все или выбранные"

    # Пути относительно корня проекта.
    PYTEST_INI = "pytest.ini"
    LOADER_SRC = os.path.join("loader", "src")
    TESTS_DIR = os.path.join("loader", "src", "auto_git_review", "tests")

    # Значения параметров по умолчанию. Переопределяются в Trigger DAG w/ config
    # (conf имеет приоритет над params ДАГа).
    PARAMS = {
        "test_target": "",
        "keyword": "",
        "fail_fast": False,
        "collect_only": False,
        "extra_args": "",
    }

    # Сколько последних строк вывода pytest приложить к ошибке таски.
    ERROR_TAIL_LINES = 40

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
        "execution_timeout": timedelta(minutes=30),
    }

    RESOURCE_CONFIG = {
        "KubernetesExecutor": {
            "request_memory": "512M",
            "limit_memory": "1G",
            "request_cpu": "500m",
            "limit_cpu": "1000m",
        }
    }


DC = DAGConfiguration
log = LoggingMixin().log


def _find_project_root():
    """Корень проекта — ближайший каталог вверх от файла ДАГа, где есть loader/src."""
    root = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(root, DC.LOADER_SRC)):
            return root
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    raise AirflowException(
        f"Не найден корень проекта: от {os.path.abspath(__file__)} вверх нет "
        f"каталога с {DC.LOADER_SRC}"
    )


def _available_targets(root):
    """Список файлов тестов — для подсказки, если цель указана с ошибкой."""
    tests_dir = os.path.join(root, DC.TESTS_DIR)
    try:
        return sorted(f for f in os.listdir(tests_dir) if f.startswith("test_"))
    except OSError:
        return []


def _lookup_target(root, part):
    """Ищет цель в каталоге тестов или от корня проекта; None — если нет.

    Возвращает путь относительно корня проекта (с сохранением ``::тест``).
    """
    path_part, _, test_name = part.partition("::")
    suffix = f"::{test_name}" if test_name else ""
    candidates = [path_part, os.path.join(DC.TESTS_DIR, path_part)]
    if path_part.strip("/\\") in ("tests", os.path.basename(DC.TESTS_DIR)):
        candidates.append(DC.TESTS_DIR)  # короткое имя каталога тестов
    for candidate in candidates:
        if os.path.exists(os.path.join(root, candidate)):
            return candidate.replace("\\", "/") + suffix
    return None


def _resolve_targets(root, test_target, keyword):
    """Разбирает параметры в (пути для pytest, выражение -k).

    Цель ищется в каталоге тестов и от корня проекта (`test_alm.py`,
    `loader/src/auto_git_review/tests/test_alm.py`); имя без пути
    (`test_decode_content`) превращается в выражение ``-k``.
    """
    paths, keywords = [], []
    for part in re.split(r"[,\s]+", (test_target or "").strip()):
        if not part or part.lower() == "all":
            continue
        resolved = _lookup_target(root, part)
        if resolved:
            paths.append(resolved)
        elif "::" in part or part.endswith(".py") or "/" in part or "\\" in part:
            available = ", ".join(_available_targets(root)) or "не найдены"
            raise AirflowException(
                f"Цель «{part}» не найдена ни в каталоге тестов ({DC.TESTS_DIR}), "
                f"ни от корня проекта ({root}). Доступные файлы тестов: {available}"
            )
        else:
            keywords.append(part)

    if keyword and keyword.strip():
        keywords.append(f"({keyword.strip()})")

    # Если pytest.ini не перенесён на сервер, testpaths не подхватится — запускаем
    # каталог тестов явно (см. также PYTHONPATH в _run_pytest).
    if not paths and not os.path.isfile(os.path.join(root, DC.PYTEST_INI)):
        paths.append(DC.TESTS_DIR)

    return paths, " or ".join(keywords)


def _ensure_pytest_available(env):
    """Проверяет, что pytest установлен, и возвращает его версию."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            cwd=None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:  # нет самого интерпретатора
        raise AirflowException(f"Не удалось запустить python ({sys.executable}): {exc}")

    if proc.returncode != 0:
        raise AirflowException(
            "pytest не установлен в окружении воркера. Установите зависимости "
            "разработки: pip install -r requirements-dev.txt"
        )
    return (proc.stdout or "").strip()


def _build_command(paths, keyword_expr, fail_fast, collect_only, extra_args):
    """Собирает команду pytest."""
    cmd = [sys.executable, "-m", "pytest", "-v", "--color=no", "-p", "no:cacheprovider"]
    if collect_only:
        cmd += ["--collect-only", "-q"]
    if fail_fast:
        cmd.append("-x")
    if keyword_expr:
        cmd += ["-k", keyword_expr]
    cmd += list(paths)
    if extra_args and extra_args.strip():
        cmd += shlex.split(extra_args.strip())
    return cmd


def _run_pytest(cmd, root, env):
    """Запускает pytest подпроцессом, стримит вывод в лог, возвращает (код, строки)."""
    log.info("Команда: %s", " ".join(cmd))
    log.info("Рабочий каталог: %s", root)
    proc = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = (proc.stdout or "").splitlines()
    for line in lines:
        log.info("%s", line)
    return proc.returncode, lines


def _summarize(lines):
    """Последняя содержательная строка вывода pytest (итог прогона)."""
    for line in reversed(lines):
        text = line.strip().strip("=").strip()
        if re.search(r"\b(passed|failed|error|errors|skipped|deselected|collected|no tests ran)\b", text):
            return text
    return lines[-1].strip() if lines else ""


def run_tests_wrapper(**context):
    """Таска: прогон unit-тестов с параметрами из Trigger DAG w/ config."""
    params = dict(DC.PARAMS)
    params.update(context.get("params") or {})

    root = _find_project_root()
    loader_src = os.path.join(root, DC.LOADER_SRC)
    log.info("Корень проекта: %s", root)

    paths, keyword_expr = _resolve_targets(root, params.get("test_target"), params.get("keyword"))
    cmd = _build_command(
        paths=paths,
        keyword_expr=keyword_expr,
        fail_fast=bool(params.get("fail_fast")),
        collect_only=bool(params.get("collect_only")),
        extra_args=params.get("extra_args"),
    )

    # PYTHONPATH — чтобы тесты видели пакет auto_git_review, даже если на сервер
    # не перенесли pytest.ini (тогда pythonpath = loader/src не сработает).
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [loader_src] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    log.info("pytest: %s", _ensure_pytest_available(env))
    returncode, lines = _run_pytest(cmd, root, env)
    summary = _summarize(lines)

    log.info("Итог: %s (код возврата %s)", summary, returncode)
    if returncode != 0:
        tail = "\n".join(lines[-DC.ERROR_TAIL_LINES:])
        raise AirflowException(
            f"Прогон тестов не прошёл (код {returncode}): {summary}\n"
            f"Последние {DC.ERROR_TAIL_LINES} строк вывода:\n{tail}"
        )

    return {
        "command": " ".join(cmd),
        "returncode": returncode,
        "summary": summary,
    }


with DAG(
    dag_id=DC.DAG_NAME,
    description=DC.DAG_DESCRIPTION,
    schedule_interval=None,
    tags=["tests", "pytest", "auto_git_review"],
    max_active_runs=1,
    default_args=DC.DAG_DEFAULT_ARGS,
    params=DC.PARAMS,
) as dag:
    PythonOperator(
        task_id="run_unit_tests",
        python_callable=run_tests_wrapper,
        executor_config=DC.RESOURCE_CONFIG,
    )
