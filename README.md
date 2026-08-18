# auto_git_review

Автоматический ревьюер pull request'ов для локального **Azure DevOps Server**
(on-prem) на базе локальной LLM.

Инструмент снимает рутину первичного ревью: собирает изменения каждого открытого
PR, добавляет контекст истории файлов и просит локальную LLM (через
LiteLLM-шлюз) оценить корректность изменений. Результат — структурированное
резюме с вердиктом и построчными замечаниями, которое публикуется комментарием
прямо в PR.

Всё работает **полностью внутри корпоративного контура** — локальный ALM +
локальная LLM, без внешних сервисов.

## Как это работает

```
                  ┌──────────────────────────┐
                  │   Azure DevOps Server    │
                  │        (on-prem)         │
                  └────────────┬─────────────┘
                               │  REST API (PAT)
                               ▼
   1. Список открытых PR  ──►  2. Diff по каждому PR
   3. Контекст истории файлов (5 последних коммитов)
                               │
                               ▼
                  ┌──────────────────────────┐
                  │     Батчевое ревью LLM   │
                  │      (LiteLLM-шлюз)      │
                  └────────────┬─────────────┘
                               │  JSON-вердикт
                               ▼
   4. Агрегация батчей  ──►  5. Markdown-комментарий в PR
```

### Алгоритм ревью одного PR

1. **Сбор изменений.** Для каждого открытого PR берётся список изменённых файлов
   (`iterations/{id}/changes`).
2. **Контекст файла.** Для каждого файла подтягивается история — 5 последних
   коммитов, затрагивавших этот файл, чтобы модель видела сложившиеся паттерны.
3. **Полный код.** Для изменённых файлов в промпт кладётся и unified diff, и
   **полное новое содержимое** — модель видит весь файл, а не только фрагменты.
4. **Батчевое ревью.** Большие PR режутся на батчи (~8–10k токенов), каждый
   ревьюится отдельным вызовом LLM, результаты агрегируются в один итог.
5. **Dedup.** Перед вызовом LLM проверяется, нет ли уже нашего комментария (по
   маркеру) — повторные запуски не тратят вызовы модели.
6. **Лимит батчей.** Если PR порождает больше `MAX_BATCHES` батчей (например,
   тысячи файлов), полный анализ пропускается и формируется общее резюме
   «нужно ручное ревью».

## Возможности

- ✅ Мульти-проектность и мульти-репозиторность: репозитории в разных проектах ALM
- ✅ Отдельный промпт под каждый тип кодовой базы
- ✅ Батчевое ревью с агрегацией результата
- ✅ Dedup по маркеру — без дублей при повторных запусках
- ✅ Полное содержимое файлов + diff — модель видит весь код
- ✅ Комментарий в PR + markdown-отчёт
- ✅ Полная работа внутри корпоративного контура

## Поддерживаемые типы кодовых баз

| Тип кодовой базы | Промпт |
|---|---|
| Greenplum SQL | `review_prompt_greenplum.md` |
| ClickHouse SQL | `review_prompt_clickhouse.md` |
| PostgreSQL SQL | `review_prompt_postgres.md` |
| Airflow DAG (ETL-пайплайны) | `review_prompt_airflow_etl.md` |
| Airflow DAG (оркестрация внешнего инструмента) | `review_prompt_airflow_orchestration.md` |

Промпты лежат отдельно в `loader/src/auto_git_review/prompts/*.md` — их можно
править без изменения кода.

## Структура проекта

```
.
├── dags/
│   └── prj_brddwh/
│       └── auto_git_review_dag.py    # ДАГ Airflow: таска на каждый репозиторий
├── loader/
│   └── src/
│       └── auto_git_review/
│           ├── alm.py                # Клиент REST API Azure DevOps Server
│           ├── llm.py                # Клиент LLM (LiteLLM-шлюз)
│           ├── config.py             # Конфигурация (env-переменные)
│           ├── prompt.py             # Загрузка и рендеринг промптов
│           ├── review.py             # Основная логика ревью
│           ├── ALM_API.md            # Документация по REST API
│           └── prompts/              # Промпты под типы кодовых баз
├── .env.example                      # Пример конфигурации
└── requirements.txt
```

## Требования

- Python 3.10+
- Apache Airflow (для запуска ДАГа)
- Доступ к Azure DevOps Server (on-prem) с PAT
- Доступ к LiteLLM-шлюзу (OpenAI-совместимый endpoint)

## Конфигурация

Конфигурация задаётся переменными окружения (см. `.env.example`):

| Переменная | Описание |
|---|---|
| `AZURE_DEVOPS_URL` | База коллекции Azure DevOps Server (без проекта) |
| `AZURE_DEVOPS_PROJECT` | Имя проекта |
| `AZURE_DEVOPS_REPO` | Имя репозитория по умолчанию |
| `AZURE_DEVOPS_PAT` | Personal Access Token (чтение + запись) |
| `AZURE_DEVOPS_API_VERSION` | Версия REST API (по умолчанию `6.1-preview`) |
| `LLM_URL` | Endpoint LLM (`/v1/chat/completions`) |
| `LLM_API_KEY` | Ключ LLM |
| `LLM_MODEL` | Модель (по умолчанию `qwen3:latest`) |
| `POST_COMMENTS` | Публиковать комментарии в PR (`true`/`false`) |
| `VERIFY_SSL` | Проверять TLS-сертификат (`false` для самоподписанных) |

## Запуск в Airflow

1. Создайте Connections:
   - `api_git` — host/port Azure DevOps Server, `password` = PAT;
   - `llm_server` — host/port LiteLLM-шлюза, `password` = LLM API key.
2. Положите ДАГ `dags/prj_brddwh/auto_git_review_dag.py` в `dags_folder`.
3. Положите код проекта в `loader/src/auto_git_review/`.
4. Опишите репозитории в `DAGConfiguration.REPOSITORIES` — укажите для каждого
   `task_id`, `project`, `repo`, `prompt_name` и `post_comment`.

Каждый репозиторий — отдельная таска `PythonOperator`, вызывающая
`review.run_review(repo=..., project=..., prompt_name=..., ...)`.

Основные настройки ДАГа (`DAGConfiguration`):

| Параметр | Описание |
|---|---|
| `REPOSITORIES` | Список репозиториев (тасок) для ревью |
| `MAX_BATCHES` | Максимум батчей на один PR (по умолчанию `20`) |
| `DAG_MAX_ACTIVE_TASKS` | Параллелизм тасок |
| `CONN_ALM` / `CONN_LLM` | Имена Airflow Connections |
| `ALM_COLLECTION_PATH` | Путь коллекции (без проекта) |

## Результат ревью

```json
{
  "verdict": "approve | request_changes | comment",
  "summary": "краткое резюме ревью",
  "comments": [
    {
      "file": "путь/к/файлу",
      "line": 42,
      "severity": "critical | major | minor | nit",
      "text": "текст замечания"
    }
  ]
}
```

Результаты по батчам агрегируются в один итог (вердикт — по приоритету
`request_changes > comment > approve`) и публикуются комментарием в PR, если
включена отправка.
