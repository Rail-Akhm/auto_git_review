# auto_git_review — рабочая память

## Что это

Автоматический ревьюер pull request'ов для локального корпоративного **Azure DevOps
Server 2020 Update 1.2** (on-prem, ALM): находит открытые PR, собирает diff +
полное содержимое изменённых файлов + историю 5 последних коммитов по каждому файлу,
отдаёт всё локальной LLM через LiteLLM-шлюз, публикует вердикт
(вердикт `approve` / `request_changes` / `comment` / `forbidden`) комментарием в PR.

Стек: Python 3.10+, `requests`, Apache Airflow (оркестрация), pytest. Всё работает
внутри корпоративного контура — внешних сервисов нет. Ревью настраивается на
репозитории проектов ALM `U200001871_mkhdbrd` и `dmpdwh` (инженеры-ревьюеры этих
команд — потребители результата; явно в документации не зафиксировано).
Разработка ведётся в GitHub (`Rail-Akhm/auto_git_review`), эксплуатация — на
корпоративном сервере Airflow.

## Состояние на 2026-11-19

- Пайплайн ревью реализован целиком: сбор PR/diff/истории, батчевое ревью,
  агрегация вердиктов, dedup по SHA source-коммита с повторным ревью, постинг
  комментария. Рабочий ДАГ —
  `dags/prj_brddwh/auto_git_review_dag.py` (`SERV__auto_git_review`), 6 тасок —
  по одной на репозиторий (`DAGConfiguration.REPOSITORIES`), `max_active_tasks=10`,
  `max_active_runs=1`, `execution_timeout=2h`, `retries=0`, ресурсы под
  KubernetesExecutor (1–2G RAM).
- **Запуск только ручной:** у всех трёх ДАГов `schedule_interval=None`. Планировщика
  (Этап 2) нет — расписание пока не настроено.
- Второй ДАГ — `dags/prj_brddwh/auto_git_review_tests_dag.py`
  (`SERV__auto_git_review_tests`, ручной прогон pytest с параметрами).
- Третий ДАГ — `dags/prj_brddwh/auto_git_review_model_benchmark_dag.py`
  (`SERV__auto_git_review_model_benchmark`, ручной бенчмарк моделей: контекстное
  окно, скорость, сравнение с рекомендацией). Самодостаточен — `loader/src` не
  требует.
- **Выбор модели (2026-11-19): основная — `qwen3.8-27b-fp8`, запасная —
  `qwen3.8-think`.** У обеих (и у `qwen3.6:35b-a3b-q8_0`) фактическое окно —
  262 144 токена; рабочее допущение — 250k. 35b-a3b быстрее на ~20% при 8k и на
  ~40% при 32k, но для ревью выбрана 3.8-27B как более сильная в reasoning;
  переключение на запасную автоматическое.
- **Повторное ревью:** в комментарии записан SHA source-коммита; новый коммит —
  новое ревью, без изменений — пропуск. Старые комментарии без SHA сверяются по
  датам. Движение target само по себе ре-ревью не запускает.
- **PR в master:** обязательна ссылка на предыдущий develop-PR и отдельная ветка
  отбора. Нарушение — `forbidden`, красный блок в комментарии и голос `-10`
  («Отклонить»); после исправления голос снимается в `0`.
- **Устойчивость LLM:** fallback на `qwen3.8-think`; пустой ответ — JSON из
  `reasoning` + повтор; при сбое — `LlmUnavailableError`/`LlmResponseError`,
  задача падает, частичный отчёт не публикуется.
- 86 unit-тестов в `loader/src/auto_git_review/tests/` покрывают чистую логику
  (без обращений к ALM/LLM); запускаются `pytest` из корня проекта.
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
- **Контекст модели и лимиты батча.** Бенчмарк 2026-11-19 показал фактическое
  окно **262 144 токена** у всех трёх моделей; рабочее допущение — **250k**.
  Лимиты пересчитаны под это окно: `MAX_CHANGE_CHARS=200000` (один файл),
  `MAX_DIFF_CHARS=20000` (diff), `MAX_BATCH_CHARS=400000` (батч, ориентировочно
  90–200k токенов), `MAX_BATCHES=20` (в `DAGConfiguration`). Запас оставлен на
  промпт/ответ и плотную токенизацию кода и кириллицы. При превышении
  `MAX_BATCHES` PR не гоняется через LLM — формируется резюме «требуется ручное
  ревью» с числом файлов (не падает).
- **Умность vs скорость.** По открытым публикациям Qwen3.8-27B сильнее в
  reasoning/knowledge (GPQA-D до 89.2, HLE 33.9, IFBench 79.5), а
  Qwen3.6-35B-A3B — в agentic coding (SWE-bench Verified 73.4, Terminal-Bench 2.0
  51.5) и быстрее. Для ревью выбрана `qwen3.8-27b-fp8`; `qwen3.8-think` —
  запасная. FP8 по замерам почти не уступает BF16 (0.663 против 0.661).
- **Недоступность LLM — ошибка таски.** `LlmClient` при сбое основной модели
  пробует запасную; если ответа не удалось получить (обе недоступны, таймаут,
  5xx, отвергнутый запрос), `review.py` поднимает `LlmUnavailableError` и задача
  Airflow падает. Раньше ошибки LLM глотались, прогон завершался «успешно» без
  ревью. Ревью PR прерывается на первом сбое: остальные батчи не запускаются,
  частичный отчёт не публикуется.
- **Пустой ответ модели.** Если `content` пуст, JSON ищется в `reasoning`
  («thinking»-модели иногда оставляют итог только там), затем вызов повторяется
  один раз. Если разобрать не удалось — `LlmResponseError`, задача падает с
  диагностикой (`finish_reason`, `completion_tokens`, размеры полей).
- **Правило PR в master.** Если target — `master`/`main`, в описании PR
  обязательна ссылка на предыдущий PR в develop (URL `/pullrequest/<id>` или
  «Запрос на вытягивание <id>»); source-ветка должна быть отдельной веткой
  отбора, не личной/фича-веткой. Правило прописано во всех промптах; в
  `review.py` `_master_pr_issue()` детерминированно принуждает `forbidden`
  (полный запрет PR) с critical-замечанием, если ссылки нет или source-ветка
  совпадает с веткой предыдущего PR в develop. Дополнительно при `post_comments`
  ставится голос ревьюера **-10 («Отклонить»)** от имени PAT — только для
  master/main и только при нарушении; на обычные PR не влияет. После устранения
  нарушения следующий прогон снимает голос `-10`, если в комментарии есть
  маркер агента `vote=-10`. Для develop-PR правило не действует.
- **Вердикт `forbidden` — полный запрет PR.** Используется только для нарушений
  регламента (например, прямой PR в master), не для дефектов кода. В
  комментарии оформляется красным блоком: `## 🔴 PR ЗАПРЕЩЁН К ВЛИВАНИЮ`,
  цитата-предупреждение и разделитель; в приоритете выше `request_changes`.
  Для PR вне master/main `forbidden` понижается до `request_changes`.
- **Dedup — по маркеру, а не по автору.** `COMMENT_MARKER = "auto_git_review"`
  ищется в тексте комментариев PR **до** вызова LLM: владелец PAT — реальный
  пользователь, у него есть и свои комментарии. В комментарий пишется SHA
  source-коммита; совпал — пропуск, новый коммит — новое ревью. Если SHA нет
  (старый формат/маркер вырезан), сравниваются даты комментария и коммита.
  Target хранится справочно и сам по себе ре-ревью не запускает.
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
  `forbidden > request_changes > comment > approve`, summary склеивается «Часть N»,
  comments объединяются.
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
pytest                                   # все 86 тестов
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
  все три ДАГа запускаются только вручную.
- Мгновенная реакция через Service Hook (webhook) вместо планировщика — заявлено
  на будущее.
- Построчные комментарии (`threadContext`) вместо одного общего резюме —
  опционально, не сделано.
- Живой прогон eval на golden-датасете с реальной моделью в корпоративном контуре
  (датасет пока только пример схемы) — не проверено.
- Сравнить качество `qwen3.8-27b-fp8` и `qwen3.8-think` на golden-датасете:
  текущий выбор сделан по открытым бенчмаркам и скорости, не по качеству ревью.
- Проверить качество ревью на новых больших батчах (`MAX_BATCH_CHARS=400000`):
  растёт ли доля пропусков/ложных замечаний; при необходимости скорректировать
  лимиты.

## Источники

- `CLAUDE.md` — цель, текущее состояние, таблицы ALM/LLM, лимиты, деплой, кодировка.
- `README.md` — критерии приёмки, метрики качества, структура, тестирование, eval,
  конфигурация, запуск в Airflow.
- `C:\Users\raila\.claude\projects\D--projects-auto-git-review\memory\MEMORY.md` и
  `deploy-manual-copy-no-github.md` (изм. 2026-08-18) — ручной перенос кода без GitHub.
- `dags/prj_brddwh/auto_git_review_dag.py`, `auto_git_review_tests_dag.py` и
  `auto_git_review_model_benchmark_dag.py` — параметры ДАГов, `REPOSITORIES`,
  `MAX_BATCHES=20`, `schedule_interval=None`, методика бенчмарка.
- `loader/src/auto_git_review/{config,review,llm}.py`, `pytest.ini`,
  `requirements.txt`, `requirements-dev.txt`, `.env.example`, `.gitignore` —
  переменные окружения, модель по умолчанию, лимиты, состав тестов (46).
- `logs/` — загруженные вручную логи прогонов (в `.gitignore`): бенчмарк моделей
  от 2026-11-19 подтвердил окно 262 144 у всех трёх моделей и выбор
  `qwen3.8-27b-fp8` (35b-a3b быстрее, но слабее в reasoning).
- `git log` / `git status` (2026-11-19): выбор модели и бенчмарк закоммичены.
