# 카드 지출 달력

카드 결제(현대·삼성·KB국민)를 월간 달력으로 보는 개인 대시보드입니다.
GitHub Pages 링크로 어디서든 접속하고, **토큰을 넣어야만** 데이터가 보입니다. © OSJ

```
[iPhone 카드 문자] ──문자 전달──▶ [맥미니 chat.db] ──collector(10분)──▶ [Google 시트 (Apps Script API)]
                                                                              │ READ_TOKEN
[휴대폰·PC 브라우저] ◀── GitHub Pages (docs/index.html) ◀──── fetch ──────────┘
                     └── 수기 입력·메모 (WRITE_TOKEN) ─────────────────────────▶
```

- 저장소에는 **코드만** 있습니다. 결제 데이터와 토큰은 Google 시트(스크립트 속성)와 맥미니 로컬에만 있습니다.
- 토큰은 두 개입니다. `READ_TOKEN`(달력 보기)과 `WRITE_TOKEN`(수집기 전송, 수기 입력).

## 폴더 구성

| 경로 | 내용 |
|---|---|
| `docs/index.html` | 달력 화면 (GitHub Pages가 이 폴더를 서비스) |
| `docs/config.js` | 공개 설정 — Apps Script URL만. 토큰 금지 |
| `appsscript/Code.gs` | 시트 읽기(GET)·추가/수정/삭제(POST) API |
| `collector/card_sms_collector.py` | 맥미니 카드 문자 수집기 |
| `collector/config.example.json` | 수집기 설정 예시 → `config.local.json`으로 복사 (커밋 금지) |
| `collector/com.osj.cardsms.plist` | 수집기 10분 주기 실행 설정 |

## 설치

### 1. Google 시트 + API
1. 새 Google 시트 → **확장 프로그램 → Apps Script** → `appsscript/Code.gs` 붙여넣기
2. `setup` 실행 → 실행 로그의 **READ_TOKEN / WRITE_TOKEN** 보관 (Spend 시트 자동 생성)
3. **배포 → 새 배포 → 웹 앱** / 실행: 나 / 액세스: **모든 사용자** → `…/exec` URL 복사
   - 코드를 고친 뒤에는 **배포 관리 → ✏️ → 새 버전**으로 갱신해야 URL이 유지됩니다.
   - 토큰이 노출되면 `rotateTokens` 실행 후 수집기·달력에 새 토큰 입력

### 2. 달력 페이지 (GitHub Pages)
1. `docs/config.js`의 `API_URL`에 `…/exec` URL 입력
2. 저장소 **Settings → Pages → Branch: main / 폴더: /docs**
3. `https://<아이디>.github.io/<저장소>/` 접속 → READ_TOKEN 입력

### 3. 카드 문자 수집기 (맥미니)
1. iPhone 설정 → 메시지 → **문자 메시지 전달** → 맥미니 켜기
2. `git clone <저장소> ~/trade-spend-calendar`
3. `cp collector/config.example.json collector/config.local.json` → URL과 **WRITE_TOKEN** 입력
4. 시스템 설정 → 개인정보 보호 및 보안 → **전체 디스크 접근 권한**에 파이썬·터미널 추가
5. 확인: `python3 collector/card_sms_collector.py --scan 10`
6. plist의 `USERNAME` 수정 → `~/Library/LaunchAgents/`에 복사 → `launchctl load …`

## 화면
- 월간 플래너: 칸마다 사용처·금액, 하루 합계 / 주간 합계(PC 오른쪽 열, 휴대폰 주 아래 줄)
- 상단: 이번 달 지출 · 결제 건수 · 하루 평균 · 최다 분류
- 카드사 필터, 카테고리별 지출
- **+** 버튼·날짜 상세의 **+ 추가**로 수기 입력 (현금 포함), 결제 내역을 눌러 **메모** 작성
  - 카드 문자로 들어온 결제는 메모·카테고리만, 수기 입력은 전부 수정·삭제 가능
  - 처음 저장할 때 WRITE_TOKEN을 한 번 입력 (그 기기에 기억)
- 카테고리: 목록에서 선택(기본 '기타'), 목록 맨 아래 **+ 새 카테고리 추가…**로 바로 등록
  - 목록은 시트의 `Categories` 탭 — 시트에서 직접 추가·삭제·순서 변경 가능
