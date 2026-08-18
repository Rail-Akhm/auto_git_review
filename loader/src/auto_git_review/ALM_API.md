# Структура Azure DevOps Git REST API (выяснено probe-прогоном)

> Сервер: Azure DevOps Server 2020 Update 1.2 (on-prem), `api-version=6.1-preview`.
> Дата выяснения: 2026-08-18 (probe DAG `SERV__auto_git_review_probe`, PR #152308).

Ниже — реально проверенные эндпоинты, формат ответа и подводные камни.
Базовый префикс везде:
`/_apis/git/repositories/{repo}/...`

---

## 1. Список открытых PR

```
GET /_apis/git/repositories/{repo}/pullrequests?searchCriteria.status=active
```

Ответ: `{"value": [{pullRequestId, title, sourceRefName, targetRefName, createdBy, ...}]}`

Поле `pullRequestId` — числовой ID. Надёжный способ взять все активные PR.

## 2. Детали PR

```
GET /_apis/git/repositories/{repo}/pullrequests/{prId}
```

Ответ содержит:
- `lastMergeSourceCommit.commitId` — коммит source-ветки (это «новое» состояние);
- `lastMergeTargetCommit.commitId` — коммит target-ветки (это «база» для diff).

Эти два SHA используются как `source_commit` и `target_commit` во всех последующих вызовах.

## 3. Итерации PR

```
GET /_apis/git/repositories/{repo}/pullRequests/{prId}/iterations
```

Ответ: `{"value": [{id, reason, sourceRefCommit: {commitId}, targetRefCommit: {commitId}, commonRefCommit: {commitId}}]}`

Берём **последнюю** итерацию (`max(id)`) — именно она отражает текущее состояние PR.

## 4. Список изменённых файлов PR (ПРАВИЛЬНЫЙ способ)

```
GET /_apis/git/repositories/{repo}/pullRequests/{prId}/iterations/{iterationId}/changes?$top=2000
```

Ответ: `{"changeEntries": [{changeType, item: {path, objectId, originalObjectId, gitObjectType}}]}`

- `changeType` ∈ `add` | `edit` | `delete` (бывают и `rename`, `none` и др.);
- `item.path` — путь относительно корня репозитория, без ведущего `/` (напр.
  `db/current/edw/dm_udh_tkrs/views/create/v_era_repairs_repairs_acts.sql`);
- `item.objectId` / `item.originalObjectId` — SHA blob'ов нового/старого содержимого;
- `item.gitObjectType` — `blob` у файлов, `tree` у директорий.

**Контента в ответе НЕТ** — `newContent`/`originalContent` здесь отсутствуют.
Содержимое надо дотягивать отдельно (см. п. 6).

## 5. Diff между коммитами (diffs/commits) — для справки, не для списка файлов

```
GET /_apis/git/repositories/{repo}/diffs/commits
      ?baseVersion={target_sha}&baseVersionType=commit
      &targetVersion={source_sha}&targetVersionType=commit
```

### ⚠️ Критично: `versionType=commit`
По умолчанию `baseVersion`/`targetVersion` трактуются как **имена веток**. Если
передать SHA без `baseVersionType=commit`/`targetVersionType=commit`, сервер
отвечает **404** с ошибкой:

```
TF401175: The version descriptor <Branch: {sha}> could not be resolved to a version
```

Это была причина исходного бага. С `versionType=commit` запрос возвращает 200.

### Недостаток этого эндпоинта
`changes[]` содержит **и директории** (`tree`-объекты): для PR с 2 файлами пришло
8 записей — `/db`, `/db/current`, `/db/current/edw`, …, и только в конце 2 файла.
Контента также нет. Поэтому для списка файлов использовать **п. 4**, а не этот.

`diffCommonCommit=true` на результат (в нашем случае) не повлиял.

## 6. Содержимое файла

```
GET /_apis/git/repositories/{repo}/items
      ?path={path}
      &includeContent=true
      &versionDescriptor.version={sha}&versionDescriptor.versionType=commit
```

### ⚠️ Ответ — это ТЕКСТ, а не JSON
Для одного файла endpoint возвращает **сырое содержимое файла** напрямую
(Content-Type text, не `application/json`). Поэтому читать надо `resp.text`,
а не `resp.json()` (иначе — `JSONDecodeError`).

`versionDescriptor.versionType=commit` обязателен по той же причине, что в п. 5.

## 7. История файла

```
GET /_apis/git/repositories/{repo}/commits
      ?searchCriteria.itemPath={path}
      &searchCriteria.itemVersion.version={sha}&searchCriteria.itemVersion.versionType=commit
      &$top=5
```

Ответ: `{"value": [{commitId, comment, author: {name, email}, ...}]}`

- `$top` ограничивает число коммитов (для контекста берём 5);
- `searchCriteria.itemVersion.versionType=commit` обязателен (иначе SHA трактуется
  как ветка → не тот результат).

## 8. Work items PR

```
GET /_apis/git/repositories/{repo}/pullRequests/{prId}/workitems
```

Ответ: `{"count": N, "value": [{id, url}]}`

В probe-прогоне `count=0` — у PR #152308 не привязано ни одного work item. Это
штатная ситуация: endpoint отвечает 200, а пустота означает «нет связей». Код
должен корректно обрабатывать пустой список (писать «work items не найдены»).
Подробности work item берутся через `/_apis/wit/workitems/{id}`.

---

## 9. Комментарии к PR (threads)

### Чтение всех комментариев

```
GET /_apis/git/repositories/{repo}/pullRequests/{prId}/threads
```

Ответ: `{"value": [GitPullRequestCommentThread], "count": N}`. Каждый thread содержит
`comments[]` (у каждого `content`, `commentType`, `isDeleted`, `author.displayName`).

### Создание общего комментария к PR

```
POST /_apis/git/repositories/{repo}/pullRequests/{prId}/threads
Content-Type: application/json

{
  "comments": [
    {"parentCommentId": 0, "content": "текст комментария", "commentType": 1}
  ],
  "status": 1
}
```

- `commentType`: `1` = text (обычный), `2` = codeChange, `3` = system;
- `status`: `1` = active;
- **Без `threadContext`** — комментарий к PR целиком (не к файлу/строке). Это то,
  что нужно для публикации резюме ревью. Для комментария к строке добавляется
  `threadContext.filePath` + `rightFileStart/rightFileEnd` (line/offset) и
  `pullRequestThreadContext` (changeTrackingId/iterationContext).

Ответ `200 OK` — созданный thread с `id`. Markdown в `content` поддерживается
(свойство `Microsoft.TeamFoundation.Discussion.SupportsMarkdown`).

### Удаление комментария

```
DELETE /_apis/git/repositories/{repo}/pullRequests/{prId}/threads/{threadId}/comments/{commentId}
```

Требуемые права токена: `vso.code_write` (создание/управление PR и code reviews)
+ `vso.threads_full` (чтение/запись comment threads).

## Итоговая схема сбора данных одного PR

1. `pullrequests` (status=active) → список открытых PR.
2. `pullrequests/{id}` → `lastMergeSourceCommit` (source) и `lastMergeTargetCommit` (target).
3. `pullRequests/{id}/iterations` → последняя итерация (`max(id)`).
4. `pullRequests/{id}/iterations/{it}/changes` → `changeEntries[]` (файлы).
5. Для каждого файла:
   - `items?includeContent=true&version=source` → новый контент;
   - `items?includeContent=true&version=target` → старый контент (для edit/delete);
   - `commits?itemPath=...&itemVersion=target` → история (для edit/delete).
6. `pullRequests/{id}/workitems` → связанные work items (может быть пусто).
