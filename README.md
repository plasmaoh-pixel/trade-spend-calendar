# 매매 · 지출 달력

매매일지(실현손익)와 카드 지출(현대·삼성·KB국민)을 한 달력에서 보는 개인 대시보드입니다.
GitHub Pages 링크로 어디서든 접속하고, **토큰을 넣어야만** 데이터가 보입니다. © OSJ

```
[iPhone 카드 문자] ──문자 전달──▶ [맥미니 chat.db] ──collector(10분)──▶ [Google 시트 (Apps Script API)]
                                                                              ▲
[KB증권 API → kb_trades_sync.py / 수동 입력] ─ Trades 시트 ───────────────────────┘
                                                                              │ READ_TOKEN
[휴대폰·PC 브라우저] ◀── GitHub Pages (docs/index.html) ◀──── fetch ──────────┘
```

- 저장소에는 **코드만** 있습니다. 결제·매매 데이터와 토큰은 Google 시트(스크립트 속성)와 맥미니 로컬에만 있습니다.
- 토큰은 두 개입니다. `READ_TOKEN`(달력 보기)과 `WRITE_TOKEN`(수집기 전송). 보기 토큰이 노출돼도 데이터를 쓸 수는 없습니다.

## 폴더 구성

| 경로 | 내용 |
|---|---|
| `docs/index.html` | 달력 화면 (GitHub Pages가 이 폴더를 서비스) |
| `docs/config.js` | 공개 설정 — Apps Script URL만. 토큰 금지 |
| `appsscript/Code.gs` | 시트 읽기(GET)·쓰기(POST) API |
| `collector/card_sms_collector.py` | 맥미니 카드 문자 수집기 |
| `collector/config.example.json` | 수집기 설정 예시 → `config.local.json`으로 복사 (커밋 안 됨) |
| `collector/com.osj.cardsms.plist` | 카드 수집 10분 주기 실행 설정 |
| `collector/kb_trades_sync.py` | KB증권 체결 → 실현손익 계산 → Trades 시트 |
| `collector/com.osj.kbtrades.plist` | 체결 동기화 평일 16:10 실행 설정 |

## 설치

### 1. Google 시트 + API
1. 새 Google 시트 생성 → **확장 프로그램 → Apps Script** → `appsscript/Code.gs` 내용 붙여넣기
2. 편집기에서 `setup` 함수 실행 → **실행 로그에 READ_TOKEN / WRITE_TOKEN 출력** (Spend·Trades 시트도 자동 생성)
3. **배포 → 새 배포 → 웹 앱** / 실행: 나 / 액세스: 모든 사용자 → `…/exec` URL 복사
   - 코드를 고친 뒤에는 **배포 관리 → 기존 배포 편집 → 새 버전**으로 갱신해야 URL이 유지됩니다.
   - 토큰이 노출되면 `rotateTokens` 실행 후 수집기·달력에 새 토큰 입력

### 2. 달력 페이지 (GitHub Pages)
1. `docs/config.js`의 `API_URL`에 위 `…/exec` URL 입력 후 커밋·푸시
2. GitHub 저장소 **Settings → Pages → Branch: main / 폴더: /docs** → 저장
3. 몇 분 뒤 `https://<아이디>.github.io/<저장소>/` 접속 → READ_TOKEN 입력
   - 휴대폰은 공유 → **홈 화면에 추가**하면 앱처럼 열립니다.
   - 무료 계정의 Pages는 **public 저장소**에서만 동작합니다. 코드에 비밀값이 없으니 public이어도 안전하며, private로 두려면 GitHub Pro가 필요합니다.
   - 데이터 없이 화면만 보려면 로그인 화면의 "샘플 데이터로 둘러보기"

### 3. 카드 문자 수집기 (맥미니)
1. iPhone 설정 → 메시지 → **문자 메시지 전달** → 맥미니 켜기
2. `git clone <저장소> ~/trade-spend-calendar`
3. `cp collector/config.example.json collector/config.local.json` → URL과 **WRITE_TOKEN** 입력
4. 시스템 설정 → 개인정보 보호 및 보안 → **전체 디스크 접근 권한**에 `/usr/bin/python3`, 터미널 추가
5. 실제 문자 파싱 확인: `python3 collector/card_sms_collector.py --scan 10`
6. plist의 `USERNAME` 수정 → `cp collector/com.osj.cardsms.plist ~/Library/LaunchAgents/` → `launchctl load ~/Library/LaunchAgents/com.osj.cardsms.plist`
   - 실행할 때마다 `git pull`로 최신 코드를 받습니다 (`AUTO_GIT_PULL`).

### 4. 매매 데이터 (KB증권 자동 동기화)
`collector/kb_trades_sync.py`가 KB증권 Open API(B2C)로 **계좌별주문체결조회(SSQM2341)**와 **계좌자산평가(SSQM2952)**를 불러
실현손익(이동평균법)을 계산하고 `Trades` 시트에 넣습니다. 요청 형식은 KB 공식 예제(kbsecurities/kb-openapi)와 같습니다.

1. 앱키: `~/kb_swing_gpt/kb_swing/.env`의 `KB_OPENAPI_APP_KEY / SECRET`을 자동으로 읽습니다 (다른 곳이면 `config.local.json`의 `KB_ENV_FILE` 또는 `KB_APP_KEY / KB_APP_SECRET`).
2. 확인: `python3 collector/kb_trades_sync.py --from 2026-10-01 --dry` (시트로 보내지 않고 결과만 출력)
3. 첫 동기화: `--dry` 빼고 실행 → 그 기간 체결이 시트로 들어갑니다.
4. 자동 실행: `com.osj.kbtrades.plist` 등록 → 평일 16:10, 마지막 동기화일부터 오늘까지
5. 응답 구조 확인: `--raw` (최근 3영업일 체결 행 일부와 잔고 필드명 출력)
6. 과거 내역: HTS/MTS 체결내역 CSV → `--import-csv 파일.csv --dry`

**손익 계산 방식**
- 매수: 평균단가 = (보유금액 + 매수금액 + 수수료) ÷ 총수량
- 매도: 실현손익 = 수량 × (체결가 − 평균단가) − 수수료 − 제세금
- 체결 조회에는 수수료·세금이 없어 `FEE_RATE`, `SELL_TAX_RATE`로 추정합니다. **세율은 현행 기준으로 확인 후 수정하세요.**
- 첫 실행의 기준 평단은 KB 계좌자산평가의 매입평균가에서 해당 기간 체결을 되돌려 추정합니다.
- 매 실행 후 실제 잔고로 평단 기준점을 다시 맞추므로 오차가 누적되지 않습니다. 이미 처리한 체결은 건너뜁니다.
- 시트에서 `memo` 열은 직접 적어도 덮어쓰지 않습니다 (매매 이유 기록용).

직접 입력하거나 다른 프로그램에서 넣을 때의 `Trades` 열 형식:

| 열 | 예 | 비고 |
|---|---|---|
| id | `20261001-005930-1` | 중복 방지 키 (수동 입력이면 비워도 됨) |
| date / time | `2026-10-01` / `10:32` | |
| ticker / name | `005930` / `삼성전자` | |
| side | `매수` / `매도` | |
| qty / price / fee | `10` / `71000` / `150` | fee는 수수료+세금 합 |
| pnl | `52000` | 매도 건의 실현손익. 달력 손익은 이 값의 합 |
| memo | `실적 발표 후 분할 매도` | 상세 화면에 표시 |

## 화면 (월간 플래너)
- **달력 칸 안에 그날 내역이 바로 적힙니다**: 매매 종목(왼쪽 막대 빨강=매수, 파랑=매도)과 종목별 실현손익, 카드 사용처와 금액, 하루 지출 합계
- **주간 합계**: PC는 달력 오른쪽 '주간' 열, 휴대폰은 각 주 아래 줄에 손익·지출·순액
- 상단: 월 실현손익 · 카드지출 · 순액 · 승률(매도일 기준)
- 보기 전환: 전체 / 매매만 / 지출만, 카드사 필터
- 휴대폰은 칸이 좁아 종목명을 앞 3~4글자로 줄이고, 사용처는 '지출' 보기에서 표시합니다. 칸을 누르면 시각·수량·메모까지 상세 표시
- 아래 패널: 종목별 매매(월간 매수·매도 횟수와 실현손익), 카테고리별 지출
- 인쇄(PC 브라우저 인쇄): A4 가로 한 장에 월 달력이 들어가도록 맞춰져 있습니다
- `https://…/#t=토큰` 형태로 열면 자동 로그인 (주소창에서 바로 지워짐)
