## 목표
`architecture.snu.ac.kr`에 **새 글이 올라오면 Slack 채널로 자동 알림**을 보내는 스크립트입니다.

이 프로젝트는 “봇을 계속 켜두는 방식”이 아니라, **주기적으로 실행되는 폴링(polling)** 스크립트로 설계되어 있습니다.
즉, cron / GitHub Actions / 서버 스케줄러로 **10분마다 한 번 실행** 같은 형태로 운영하는 것을 권장합니다.

---

## 왜 HTML 스크래핑이 아니라 WordPress REST API인가?
`architecture.snu.ac.kr`는 WordPress 기반 사이트라서, 공식 REST API가 열려 있습니다.

- **장점**: HTML 구조가 바뀌어도 깨질 확률이 훨씬 낮음
- **장점**: “제목/링크/게시일” 같은 데이터를 정규화된 JSON으로 바로 받음
- **단점**: WordPress 설정에 따라 API 필드/권한이 제한될 수 있음(현재는 공개 조회 가능)

이 스크립트는 `/wp-json/wp/v2/posts`를 사용합니다.

---

## 동작 방식(중복 알림 방지)
스크립트는 마지막으로 처리한 글을 **커서(cursor)** 로 저장합니다.

- **cursor**: `(date_gmt, id)` 쌍
- 저장 위치: 기본 `./state.json`

매 실행 시 WordPress API에 “cursor 이후의 글”만 요청하고, 새 글이 있으면 Slack으로 보내고 커서를 갱신합니다.

---

## 설치
아래는 macOS 기준 예시입니다.

```bash
cd /Users/isangmin/daae_slack_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 설정

### 1) Slack Incoming Webhook 만들기
Slack에서 Incoming Webhook URL을 만든 뒤, 그 값을 환경변수로 넣으면 됩니다.

- Slack App 생성 → Incoming Webhooks 활성화 → Webhook URL 발급
- 발급된 URL을 `SLACK_WEBHOOK_URL`에 넣기

### 2) 환경변수(.env) 준비
이 레포에는 예시 파일로 `env.example`을 넣어두었습니다.

```bash
cp env.example .env
```

`.env`에서 아래 값만 최소로 채우면 됩니다.

- **SLACK_WEBHOOK_URL**: 필수
- **WP_CATEGORY_SLUGS** 또는 **WP_CATEGORY_IDS**: 선택(기본은 공지사항 `notice`)

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

---

## 어떤 글을 감시할지(카테고리)
이 사이트는 WordPress 카테고리를 사용합니다.

예시(2026-01-03 기준, 확인된 값):
- `notice` (공지사항/Notice): id=20
- `pinup` (주요공지): id=33
- `activities` (Activities(공지사항)): id=7

`.env`에서 아래처럼 지정할 수 있습니다.

```bash
# slug로 지정(추천)
WP_CATEGORY_SLUGS="notice,pinup"

# 또는 id로 지정
# WP_CATEGORY_IDS="20,33"
```

카테고리 필터 없이 “전체 글”을 감시하려면:

```bash
WP_WATCH_ALL=true
```

---

## cron으로 10분마다 실행(macOS 예시)
`crontab -e`에서 아래처럼 등록하면 됩니다(경로는 본인 환경에 맞게 수정).

```cron
*/10 * * * * cd /Users/isangmin/daae_slack_bot && /Users/isangmin/daae_slack_bot/.venv/bin/python bot.py >> /Users/isangmin/daae_slack_bot/bot.log 2>&1
```

---

## GitHub Actions로 주기 실행(권장 운영 방식)
이 레포에는 GitHub Actions 워크플로우가 포함되어 있습니다:

- 파일: `.github/workflows/architecture_snu_notify.yml`
- 기본 주기: **10분마다 실행**(UTC 기준)
- 감시 범위: **사이트 전체(posts)** (`WP_WATCH_ALL=true`)
- 중복 방지: `state.json`을 **GitHub Actions cache로 유지**

### 1) Slack Webhook을 GitHub Secret으로 등록
GitHub 레포 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

- Name: `SLACK_WEBHOOK_URL`
- Value: (Slack Incoming Webhook URL)

> Webhook URL은 절대 코드/README에 커밋하지 마세요.

### 2) 워크플로우 수동 실행(테스트)
GitHub 레포 → **Actions** 탭 → 워크플로우 선택 → **Run workflow**

### 3) 상태(state) 보존에 대한 주의사항
GitHub Actions cache는 **best-effort**라서, 드물게 만료/정리되면 `state.json`이 사라질 수 있습니다.

- 기본 동작(`SEND_ON_FIRST_RUN=false`): 상태가 없으면 **기준점만 저장하고 알림은 보내지 않음(스팸 방지)**
- 필요하면 `SEND_ON_FIRST_RUN=true`로 바꿔서 “상태 초기화 시 최근 글도 전송”하도록 할 수 있지만 스팸 위험이 있습니다.

### 4) 수동 실행에서 “최신 글 1건 [TEST] 전송” 옵션
워크플로우는 수동 실행 시 입력값으로 아래 옵션을 제공합니다.

- `ping=true`: Slack 연결 테스트 메시지 1건 전송
- `test_post=true`: **사이트 전체 posts 기준 최신 글 1건을 전송**

새 글이 없을 때도 end-to-end로 “WordPress API 조회 → Slack 전송”을 확인할 때 유용합니다.

---

## 트러블슈팅
- **알림이 안 와요**: `python3 bot.py --dry-run`으로 “새 글 감지 자체가 되는지” 먼저 확인하세요.
- **최초 실행에서 아무 것도 안 보내요**: 기본은 스팸 방지를 위해 “기준점만 저장”합니다. 필요하면 `SEND_ON_FIRST_RUN=true`를 사용하세요.
- **SSL/네트워크 문제**: 회사/학교 네트워크 프록시, 방화벽 등 환경 영향을 받을 수 있습니다.


