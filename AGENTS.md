# auto_git_review — рабочая память

## Что это

Автоматический ревьюер pull request'ов для локального корпоративного **Azure DevOps
Server 2020 Update 1.2** (on-prem, ALM): находит открытые PR, собирает diff +
полное содержимое изменённых файлов + историю 5 последних коммитов по каждому файлу,
отдаёт всё локальной LLM через LiteLLM-шлюз, публикует вердикт
(`approve` / `request_changes` / `comment`) комментарием в PR.

Стек: Python 3.10+, `requests`, Apache Airflow (оркестрация), pytest. Всё работает
внутри корпоративного контура — внешних сервисов нет. Ревью настраивается на
репозитории проектов ALM `U200001871_mkhdbrd` и `dmpdwh` (инженеры-ревьюеры этих
команд — потребители результата; явно в документации не зафиксировано).
Разработка ведётся в GitHub (`Rail-Akhm/auto_git_review`), эксплуатация — на
корпоративном сервере Airflow.

## Состояние на 2026-10-10

- Пайплайн ревью реализован целиком: сбор PR/diff/истории, батчевое ревью,
  агрегация вердиктов, dedup по маркеру, постинг комментария. Рабочий ДАГ —
  `dags/prj_brddwh/auto_git_review_dag.py` (`SERV__auto_git_review`), 6 тасок —
  по одной на репозиторий (`DAGConfiguration.REPOSITORIES`), `max_active_tasks=10`,
  `max_active_runs=1`, `execution_timeout=2h`, `retries=0`, ресурсы под
  KubernetesExecutor (1–2G RAM).
- **Запуск только ручной:** у обоих ДАГов `schedule_interval=None`. Планировщика
  (Этап 2) нет — расписание пока не настроено.
- Второй ДАГ — `dags/prj_brddwh/auto_git_review_tests_dag.py`
  (`SERV__auto_git_review_tests`, ручной прогон pytest с параметрами).
- 46 unit-тестов в `loader/src/auto_git_review/tests/` покрывают чистую логику
  (без обращений к ALM/LLM). Последний коммит — `49900d1` от 2026-09-21
  («Убрать ложные срабатывания ревью: кодировка и промпты»).
- **Не закоммичено** (рабочее дерево на 2026-10-10): правки `CLAUDE.md` и
  `README.md`, а также untracked `dags/prj_brddwh/auto_git_review_tests_dag.py`.
- Папка `auto_git_review_research/` — комплект для подачи на валидацию
  (ноутбук со встроенным production-кодом, README, ответ на замечания, отчёт
  `[Номер ЗНО C-1101243]`, презентация). В `report/` — те же материалы плюс
  презентация по другому проекту. Обе папки в `.gitignore`.
- Легаси-CLI (корневой `main.py`, команды `list-prs`) **удалён** — в проекте его
  больше нет; единственные точки входа — ДАГи Airflow.

## Ключевые факты и грабли

- **Кодировка файлов из ALM.** ALM отдаёт `items?includeContent=true` как
  `text/plain` без charset, и `resp.text` декодирует байты как ISO-8859-1 (RFC
  2616 для `text/*`) — русский текст в `COMMENT`'ах превращался в «кракозябры», и
  модель писала о них замечания. Решение: `alm.py` читает `resp.content` и
  декодирует сам (`_decode_content()`): UTF-8 (с BOM и без) → CP1251 → `errors="replace"`.
- **Полное содержимое файла обязательно.** Если класть в промпт только diff,
  модель отвечает «нет всего кода». Поэтому в промпт идут и unified diff, и полное
  новое содержимое файла.
- **Лимиты контекста (~30k токенов у локальной модели):** `MAX_CHANGE_CHARS=12000`
  (один файл), `MAX_DIFF_CHARS=2000` (diff), `MAX_BATCH_CHARS=30000` (батч,
  ~8–10k токенов), `MAX_BATCHES=20` (в `DAGConfiguration`). При превышении
  `MAX_BATCHES` PR не гоняется через LLM — формируется резюме «требуется ручное
  ревью» с числом файлов (не падает).
- **Dedup — по маркеру, а не по автору.** `COMMENT_MARKER = "auto_git_review"`
  ищется в тексте комментариев PR **до** вызова LLM: владелец PAT — реальный
  пользователь, у него есть и свои комментарии.
- **Ложные `request_changes` дороже пропусков.** Они подрывают доверие к боту,
  поэтому в каждый промпт встроены архитектурная преамбула проекта, правила против
  галлюцинаций и раздел «ЧТО НЕ ЯВЛЯЕТСЯ ОШИБКОЙ»; `request_changes` разрешён
  только при реальных дефектах, не при стилевых придирках.
- **Тесты НЕ запускаются рабочим ДАГом ревью.** Airflow обходит только
  `dags_folder` (`dags/`), а тесты лежат в `loader/src/`; таска выполняет ровно свой
  `python_callable` (`review.run_review()`), `pytest` в `requirements.txt` не входит
  (он в `requirements-dev.txt`). Автозапуск тестов «гейтом перед ревью» не
  реализован — для этого отдельный ручной ДАГ.
- **pytest запускать только из корня проекта.** `pytest.ini` задаёт
  `pythonpath = loader/src`; в pytest 9 пути из `testpaths` отсчитываются от
  каталога запуска, поэтому из `dags/prj_brddwh` не соберётся ни один тест.
- **`verify=False` осознанно** для ALM и LLM (самоподписанные сертификаты);
  `config.py` глушит `InsecureRequestWarning` при `VERIFY_SSL != true`.
- **URL коллекции и проекта разделены.** `azure_url` — база коллекции без проекта
  (`…/TFS/GPN`), `azure_project` — имя проекта. Git-эндпоинты project-scoped
  (`/{collection}/{project}/_apis/...`), WIT — коллекционные (`/_apis/wit/...`).
  Репозитории лежат в разных проектах ALM.
- **Секреты только из env/Connections.** `AZURE_DEVOPS_PAT` и `LLM_API_KEY` —
  в `.env` локально и в Airflow Connections `api_git` (host/port, password=PAT) и
  `llm_server` (host/port, password=LLM key); ДАГ раскладывает их в `os.environ`
  через `_apply_connections()`. Несекретные URL-дефолты — в `config.py`.
- **`git pull` на сервере невозможен** — у корпоративного контура нет доступа к
  GitHub (см. «Как запускать»).
- **Батч-агрегация:** вердикт по приоритету
  `request_changes > comment > approve`, summary склеивается «Часть N», comments
  объединяются.
- **Мониторинг и eval:** `monitoring.py` пишет сводные метрики одной JSON-строкой
  `REVIEW_RUN_METRICS` в конце `run_review()`; `eval.py` считает accuracy /
  Precision / Recall / F1 (one-vs-rest по классам) и confusion matrix на
  golden-датасете, схема —
  `loader/src/auto_git_review/tests/eval/golden_dataset.example.json`. Целевые
  метрики: accuracy ≥ 80%, Precision/Recall по `request_changes` и `approve` ≥ 0.7.
- **Построчных комментариев нет:** результат постится одним общим thread без
  привязки к строке (`threadContext` не реализован).
- **`similarity_search/`** — исследовательская папка другого автора (Мозжухин В. Б.),
  к проекту не относится и в git не коммитится.
- **Адреса-примеры в демо-материалах:** в ноутбуке и его `.env.example` только
  `example.local`, `ExampleProject1` и т.п. — реальные корпоративные значения туда
  не добавлять.
- **В README устаревший пункт:** «Локальный прогон без Airflow:
  `python loader/src/run.py`» — `run.py` относится к корпоративному ETL-фреймворку
  (импортирует `commands.ods`, `common.logger`), а не к `auto_git_review`; папки
  `loader/src/commands`, `common`, `contracts`, `providers`, `utils`, `tests`
  в чекауте пусты. Локально ревью запускается вызовом
  `review.run_review(...)` с заполненным `.env`.

## Как запускать и проверять

Тесты (из корня проекта):

```bash
pip install -r requirements-dev.txt
pytest                                   # все 46 тестов
pytest loader/src/auto_git_review/tests/test_alm.py
pytest -k test_decode_content
```

Ревью — основная функция `run_review()` в
`loader/src/auto_git_review/review.py`:

```python
from auto_git_review import review
review.run_review(repo="U200001871_mkhdbrd_greenplum",
                  project="U200001871_mkhdbrd",
                  prompt_name="review_prompt_greenplum.md",
                  post_comment=True, max_batches=20)
```

Требуется непустой `AZURE_DEVOPS_PAT` и `LLM_API_KEY` (иначе `RuntimeError`).

Деплой в Airflow (ручное копирование, без git):

1. Создать Connections `api_git` (host/port ALM, `password` = PAT) и `llm_server`
   (host/port LiteLLM, `password` = LLM API key).
2. Скопировать ДАГи в `dags_folder` (`dags/prj_brddwh/auto_git_review_dag.py` и,
   при необходимости, `auto_git_review_tests_dag.py`).
3. Скопировать папку `loader/src/auto_git_review/` целиком (вместе с `tests/` и
   `prompts/`) в `loader/src/auto_git_review/` на сервере.
4. Репозитории описаны в `DAGConfiguration.REPOSITORIES` (`task_id`, `project`,
   `repo`, `prompt_name`, `post_comment`).

Проверка результата: XCom/лог таски, сводная строка `REVIEW_RUN_METRICS`, и сам
комментарий в PR с маркером `auto_git_review`.

## Открытые вопросы

- Планировщик периодического опроса открытых PR (Этап 2) — не реализован; сейчас
  оба ДАГа запускаются только вручную.
- Мгновенная реакция через Service Hook (webhook) вместо планировщика — заявлено
  на будущее.
- Построчные комментарии (`threadContext`) вместо одного общего резюме —
  опционально, не сделано.
- Живой прогон eval на golden-датасете с реальной моделью в корпоративном контуре
  (датасет пока только пример схемы) — не проверено.

## Источники

- `CLAUDE.md` — цель, текущее состояние, таблицы ALM/LLM, лимиты, деплой, кодировка.
- `README.md` — критерии приёмки, метрики качества, структура, тестирование, eval,
  конфигурация, запуск в Airflow.
- `C:\Users\raila\.claude\projects\D--projects-auto-git-review\memory\MEMORY.md` и
  `deploy-manual-copy-no-github.md` (изм. 2026-08-18) — ручной перенос кода без GitHub.
- `dags/prj_brddwh/auto_git_review_dag.py` и `auto_git_review_tests_dag.py` —
  параметры ДАГов, `REPOSITORIES`, `MAX_BATCHES=20`, `schedule_interval=None`.
- `loader/src/auto_git_review/{config,review,llm}.py`, `pytest.ini`,
  `requirements.txt`, `requirements-dev.txt`, `.env.example`, `.gitignore` —
  переменные окружения, лимиты, состав тестов (46).
- `git log` / `git status` (2026-10-10): последний коммит 2026-09-21, незакоммиченные
  правки `CLAUDE.md` и `README.md`, untracked тестовый ДАГ.
