## 목표
`architecture.snu.ac.kr`(서울대학교 건축학과/DAAE)에 **새 공지가 올라오면 Slack 채널로 자동 알림**을 보내는 스크립트입니다. 설정하면 Notion 페이지에도 함께 기록할 수 있습니다.

이 프로젝트는 “봇을 계속 켜두는 방식”이 아니라, **주기적으로 실행되는 폴링(polling)** 스크립트로 설계되어 있습니다.
즉, cron / GitHub Actions / 서버 스케줄러로 **10분마다 한 번 실행** 같은 형태로 운영하는 것을 권장합니다.

---

## 왜 HTML 스크래핑이 아니라 JSON API인가?
`architecture.snu.ac.kr`는 2026년 사이트 개편으로 **SPA(Single Page App)** 로 재구축되었습니다. 예전의 WordPress REST API(`/wp-json/wp/v2/posts`)는 더 이상 존재하지 않으며, 새 공지는 아래 JSON API로 제공됩니다.

```
POST https://architecture.snu.ac.kr/rest/activities/getNotices
body: {"page": 1}
resp: {"err": 0, "list": [{"id", "category", "ctype", "title", "post_date"}, ...]}
```

- 글 링크는 `https://architecture.snu.ac.kr/post/{id}` 형식입니다.
- `post_date`는 `YYYY.MM.DD`(날짜만, 시간 없음) 형식입니다.
- **장점**: HTML 구조가 바뀌어도 깨질 확률이 훨씬 낮고, “제목/링크/게시일”을 정규화된 JSON으로 바로 받습니다.
- **주의**: 이 API는 JSON 본문을 보내면서도 `Content-Type`을 `text/html`로 응답합니다. 그래서 봇은 헤더가 아니라 **본문**으로 JSON/HTML 여부를 판별하며, SPA fallback(index.html)이 돌아오면 “API 경로/도메인이 바뀌었을 수 있다”는 오류를 냅니다.

---

## 동작 방식(중복 알림 방지)
공지 `id`는 **날짜순으로 단조증가하지 않고**(고정 공지 등이 섞임) `post_date`에는 시간이 없기 때문에, “커서(cursor)” 방식으로는 새 글을 안정적으로 구분할 수 없습니다.

그래서 이 봇은 **이미 본 글 id 집합(`seen_ids`)** 으로 중복을 방지합니다.

- 상태 저장 위치: 기본 `./state.json` (`STATE_PATH`로 변경 가능)
- 상태 구조: `{"version": 2, "streams": {"<API 키>": {"seen_ids": [...], "updated_at": "..."}}}`
- `seen_ids`는 무한정 커지지 않도록 가장 큰 id 기준 최신 N개만 유지합니다.

### 페이지네이션(다운타임/버스트 대응)
봇이 잠시 멈춰 있는 동안 새 글이 **2페이지 이상** 쌓일 수 있습니다. 실제 알림 경로에서는 `page=1`부터 시작해 다음 조건 중 하나를 만족할 때까지 페이지를 이어서 조회합니다.

1. 빈 페이지가 나옴(더 이상 글이 없음),
2. 해당 페이지에 `seen_ids`에 없던 새 글이 하나도 없음(그 아래는 더 오래된 글이므로 이미 봤다고 간주),
3. 안전 상한(`MAX_FETCH_PAGES`)에 도달.

페이지 간에 같은 글(고정 공지 등)이 중복되면 id 기준으로 한 번만 취합합니다. `--init` / 최초 실행 / `--test-latest` 경로는 “현재 화면(1페이지)”만 조회합니다.

---

## 설치
```bash
cd /path/to/daae_slack_bot   # 레포를 클론한 경로로 변경
python3 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 설정

### 1) Slack Incoming Webhook 만들기
Slack에서 Incoming Webhook URL을 만든 뒤, 그 값을 환경변수로 넣으면 됩니다.

- Slack App 생성 → Incoming Webhooks 활성화 → Webhook URL 발급
- 발급된 URL을 `SLACK_WEBHOOK_URL`에 넣기

### 2) 환경변수(.env) 준비
이 레포에는 예시 파일로 `env.example`을 넣어두었습니다. `env.example`이 실제로 사용하는 값의 기준(source of truth)입니다.

```bash
cp env.example .env
```

### 환경변수 목록
아래 변수만 `bot.py`가 실제로 읽습니다.

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `SLACK_WEBHOOK_URL` | 사실상 필수 | Slack Incoming Webhook URL. (Notion만 설정한 경우 없어도 되지만, Slack/Notion 모두 없으면 “무음 장애”를 막기 위해 실행이 실패합니다.) |
| `BASE_URL` | 선택 | 감시할 사이트. 기본 `https://architecture.snu.ac.kr`. 구버전 변수명 `WP_BASE_URL`도 하위호환으로 인식됩니다. |
| `MAX_NOTIFY_PER_RUN` | 선택 | 한 번의 실행에서 Slack으로 보낼 최대 글 수(기본 20). 초과분은 가장 최근 것 위주로 일부만 전송하고 나머지는 seen 처리합니다. |
| `SEND_ON_FIRST_RUN` | 선택 | 상태 파일이 없을 때 기존 글을 전송할지 여부. 기본 `false`(기준점만 저장, 스팸 방지). |
| `STATE_PATH` | 선택 | 상태 저장 파일 경로(기본 `./state.json`). |
| `ALERT_FEED_NAME` | 선택 | Slack 메시지에 표시할 이름(기본 `건축학과`). |
| `ALERT_EMOJI` | 선택 | 요약 헤더 이모지(기본 `📰`). |
| `SLACK_CHANNEL` | 선택 | Slack 채널 오버라이드. |
| `SLACK_USERNAME` | 선택 | Slack 표시 이름 오버라이드. |
| `NOTION_TOKEN` | 선택 | 설정 시 Notion 페이지에도 새 글을 추가합니다(`NOTION_PAGE_ID`와 함께 설정해야 동작). |
| `NOTION_PAGE_ID` | 선택 | Notion 페이지 ID 또는 URL. |

일부 값은 CLI 플래그로도 오버라이드할 수 있습니다: `--base-url`, `--max-notify`, `--send-on-first-run`, `--state-path`, `--slack-webhook-url`, `--slack-channel`, `--slack-username`.

---

## 실행

### 1) 최초 1회: 기준점만 저장(권장)
기존 글이 한꺼번에 Slack으로 전송되는 것을 막기 위해, 최초에는 기준점만 저장하는 것을 권장합니다.

```bash
python3 bot.py --init
```

### 2) Dry-run(전송 없이 확인)
```bash
python3 bot.py --dry-run
```

### 3) 정상 실행(새 글 있으면 Slack 전송)
```bash
python3 bot.py
```

### 4) 최신 글 1건 테스트 전송(상태 저장 없음)
```bash
python3 bot.py --test-latest
```

### 5) Slack 연결 확인
```bash
python3 bot.py --ping
```

### 6) 정기 생존 신호(heartbeat) 전송(상태 저장 없음)
```bash
python3 bot.py --heartbeat
```

---

## cron으로 10분마다 실행(예시)
`crontab -e`에서 아래처럼 등록하면 됩니다(경로는 본인 환경에 맞게 수정).

```cron
*/10 * * * * cd /path/to/daae_slack_bot && /path/to/daae_slack_bot/.venv/bin/python bot.py >> /path/to/daae_slack_bot/bot.log 2>&1
```

---

## GitHub Actions로 주기 실행(권장 운영 방식)
이 레포에는 GitHub Actions 워크플로우가 포함되어 있습니다:

- 파일: `.github/workflows/architecture_snu_notify.yml`
- 기본 주기: **10분마다 실행**(UTC 기준)
- 중복 방지: `state.json`을 **레포에 커밋해서 영구 보존**(아래 참고)

### 1) Slack Webhook을 GitHub Secret으로 등록
GitHub 레포 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `SLACK_WEBHOOK_URL`
- Value: (Slack Incoming Webhook URL)

> Webhook URL은 절대 코드/README에 커밋하지 마세요.

### 2) 워크플로우 수동 실행(테스트)
GitHub 레포 → **Actions** 탭 → 워크플로우 선택 → **Run workflow**

### 3) 상태(state) 보존 방식 — 레포에 커밋
예전에는 `state.json`을 **GitHub Actions cache**로 유지했지만, 캐시는 **best-effort**라서 만료/정리되면 상태가 사라졌습니다.
그러면 매번 “첫 실행” 경로(`SEND_ON_FIRST_RUN=false`)로 빠져 **기준점만 저장하고 알림을 보내지 않아**, 결과적으로 알림이 조용히 멈추는 문제가 있었습니다.

이제는 상태를 **레포의 `state.json` 파일로 커밋**해서 영구 보존합니다.

- `actions/checkout`이 최신 `state.json`을 가져오고, 봇 실행 후 변경되면 워크플로우가 다시 커밋/푸시합니다.
- 이를 위해 워크플로우 job에는 **`permissions: contents: write`** 가 필요합니다(keepalive 워크플로우와 동일한 방식).
- 커밋은 **실제 알림 실행 경로(schedule / workflow_dispatch)** 에서, **`state.json`이 바뀐 경우에만** 이루어집니다.
- 커밋 메시지에 **`[skip ci]`** 를 넣어 커밋으로 인한 재실행을 막고, keepalive 등 다른 워크플로우와의 충돌에 대비해 push 전에 `git pull --rebase` 후 실패 시 1회 재시도합니다.
- 레포에는 **기준(baseline) `state.json`**(`{"version": 2, "streams": {}}`)이 커밋되어 있어, 첫 스케줄 실행이 예측 가능하게 기준점을 세웁니다.
- 기본 동작(`SEND_ON_FIRST_RUN=false`): 상태가 비어 있으면 **기준점만 저장하고 알림은 보내지 않음(스팸 방지)**. 상태가 커밋으로 보존되므로 이 “첫 실행”은 사실상 한 번만 발생합니다.
- 필요하면 `SEND_ON_FIRST_RUN=true`로 바꿔서 “상태 초기화 시 최근 글도 전송”하도록 할 수 있지만 스팸 위험이 있습니다.

### 3-1) 정기 생존 신호(heartbeat)
알림이 없어도 봇이 살아있는지 확인할 수 있도록, 워크플로우는 **매일 1회(00:00 UTC = 09:00 KST)** heartbeat 메시지를 Slack으로 보냅니다.

- 해당 단계는 `python bot.py --heartbeat`를 실행하며, 상태(`state.json`)를 저장하지 않습니다.
- `continue-on-error: true`로 설정되어 있어 **heartbeat 실패가 본 알림(notifier)을 실패시키지 않습니다.**
- 로컬에서 직접 보내려면: `python3 bot.py --heartbeat`

### 3-2) 실패 알림(failure notification)
봇 실행 중 오류가 나거나(API 드리프트/네트워크 등) Slack Webhook이 설정되지 않은 경우, 예전에는 **조용히 실패**해서 알림이 안 오는 이유를 알기 어려웠습니다.
이제는 가능한 경우 **실패 사실을 Slack으로 알려**, 문제를 빨리 인지할 수 있도록 개선했습니다(자세한 동작은 코드/워크플로우 로그 참고). 실패 알림 본문에는 예외 메시지 요약만 담기며, 환경변수/시크릿 값은 포함되지 않습니다.

### 4) 수동 실행에서 “최신 글 1건 [TEST] 전송” 옵션
워크플로우는 수동 실행 시 입력값으로 아래 옵션을 제공합니다.

- `ping=true`: Slack 연결 테스트 메시지 1건 전송(`bot.py --ping`)
- `test_post=true`: **최신 글 1건을 `[TEST]`로 전송**(`bot.py --test-latest`)

새 글이 없을 때도 end-to-end로 “공지 API 조회 → Slack 전송”을 확인할 때 유용합니다.

---

## CI(테스트 자동 실행)
- 파일: `.github/workflows/ci.yml`
- `push`와 `pull_request`(대상: `main`)에서 실행됩니다.
- `python -m py_compile bot.py`로 컴파일을 확인하고 `python -m unittest discover -s tests -v`로 오프라인 단위 테스트를 돌립니다.
- 테스트는 `tests/_stubs.py`가 `requests`/`urllib3`/`dotenv`를 스텁으로 대체해 **네트워크 없이** 실행되므로, CI에서 서드파티 의존성을 설치하지 않습니다.
- 이 워크플로우는 시크릿을 사용하지 않으며 권한은 `contents: read`로 제한되어 있습니다.

---

## 트러블슈팅
- **알림이 안 와요**: `python3 bot.py --dry-run`으로 “새 글 감지 자체가 되는지” 먼저 확인하세요.
- **최초 실행에서 아무 것도 안 보내요**: 기본은 스팸 방지를 위해 “기준점만 저장”합니다. 필요하면 `SEND_ON_FIRST_RUN=true`를 사용하세요.
- **JSON 대신 HTML이 온다는 오류**: 공지 API 경로/도메인이 바뀌었을 수 있습니다. `BASE_URL`과 API 경로(`/rest/activities/getNotices`)를 확인하세요.
- **SSL/네트워크 문제**: 회사/학교 네트워크 프록시, 방화벽 등 환경 영향을 받을 수 있습니다.
