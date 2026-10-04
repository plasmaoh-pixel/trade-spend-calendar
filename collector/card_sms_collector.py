#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
카드 승인 문자 수집기 (KB국민 · 삼성 · 현대)
- 맥미니 '메시지' 앱 DB(chat.db)에서 카드 승인/취소 문자를 읽어
  로컬 CSV에 저장하고, Google 시트(Apps Script 웹앱)로 전송합니다.
- 사용법
    python3 card_sms_collector.py            # 새 문자 수집 (launchd로 주기 실행)
    python3 card_sms_collector.py --scan 20  # 최근 카드 문자 20건 원문+파싱 결과 확인
    python3 card_sms_collector.py --test     # 내장 샘플로 파서 테스트
Copyright (c) OSJ
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request

# ============================== CONFIG ==============================
# 기본값. 비밀값(WEBAPP_URL, WRITE_TOKEN)은 같은 폴더의 config.local.json 에 넣습니다.
# (config.local.json 은 .gitignore 처리되어 GitHub에 올라가지 않음)
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    "CHAT_DB": os.path.expanduser("~/Library/Messages/chat.db"),
    "WORK_DIR": os.path.expanduser("~/card_sms"),        # 상태/로그 저장 폴더 (저장소 밖)
    "WEBAPP_URL": "",                                     # Apps Script 웹앱 URL (비우면 CSV만 저장)
    "WRITE_TOKEN": "",                                    # setup() 로그의 WRITE_TOKEN
    "FIRST_RUN_LOOKBACK_DAYS": 30,                        # 첫 실행 시 과거 며칠치까지 가져올지
    "AUTO_GIT_PULL": True,                                # 실행 시 저장소 최신 코드 받기 (다음 실행부터 반영)
}
_local = os.path.join(HERE, "config.local.json")
if os.path.exists(_local):
    with open(_local, encoding="utf-8") as _f:
        CONFIG.update({k: os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v
                       for k, v in json.load(_f).items()})

# 카드사 판별 (위에서부터 순서대로 검사)
CARD_PATTERNS = [
    ("현대", r"현대카드|(^|\n)\s*현대\s+\S*?\d{3,4}\s*(승인|취소)"),   # "현대 대한항공030 승인" 형식 포함
    ("KB국민", r"KB국민|국민카드|KB카드"),
    ("삼성", r"삼성카드|삼성\s?\d[\d*]{3}"),
]

# 가맹점명 키워드 → 카테고리 (원하는 대로 추가)
CATEGORY_RULES = [
    ("카페", ["스타벅스", "커피", "투썸", "이디야", "메가", "빽다방", "카페"]),
    ("편의점", ["GS25", "CU", "세븐일레븐", "이마트24"]),
    ("마트", ["이마트", "홈플러스", "롯데마트", "코스트코", "트레이더스"]),
    ("교통", ["택시", "카카오T", "코레일", "SRT", "주유", "충전", "하이패스"]),
    ("온라인", ["쿠팡", "네이버페이", "11번가", "G마켓", "옥션", "무신사", "배달의민족", "요기요"]),
    ("병원/약국", ["병원", "의원", "약국", "치과"]),
]
# ===================================================================

PARSER_VERSION = 2   # 2: 현대카드 "현대 ○○030 승인" 형식, 후불하이패스 사용합계

HEADERS = ["id", "datetime", "date", "card", "status", "amount",
           "currency", "foreign_amount", "installment", "merchant", "category", "raw"]

RE_DATETIME = re.compile(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")
RE_CUMUL = re.compile(r"(누적|잔여|한도|사용가능)[^\n]*")
RE_AMOUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*원")
RE_FOREIGN = re.compile(r"(USD|EUR|JPY|GBP|CNY|HKD|SGD)\s*([\d,]+(?:\.\d+)?)")
RE_INSTALL = re.compile(r"일시불|\d{1,2}\s*개월")
RE_STATUS = re.compile(r"승인\s*취소|취소|승인")
NOISE = [
    r"\[Web발신\]", r"\[국외발신\]",
    r"현대카드\s*[A-Za-z0-9]*", r"^\s*현대\s+\S*?\d{3,4}", r"KB국민카드|국민카드|KB카드|KB국민", r"삼성카드|삼성",
    r"승인\s*취소|승인|취소|체크|신용|해외",
    r"\S*\*\S*님?", r"\S+님",                 # 마스킹된 이름 (홍*동님)
    r"\(?\b[\d*]{4}\)?",                      # 카드번호 끝자리 (1*2* / (1234))
]


def detect_card(text):
    for name, pat in CARD_PATTERNS:
        if re.search(pat, text):
            return name
    return None


def guess_category(merchant):
    for cat, kws in CATEGORY_RULES:
        if any(k.lower() in merchant.lower() for k in kws):
            return cat
    return "기타"


def parse_card_sms(text, msg_time):
    """카드 문자 1건 → dict. 카드 승인/취소 문자가 아니면 None."""
    if not text:
        return None
    card = detect_card(text)
    if not card or "거절" in text:
        return None
    m_status = RE_STATUS.search(text)
    hipass = "하이패스" in text and "합계" in text      # 후불하이패스 월 사용합계 문자 (승인 단어 없음)
    if not m_status and not hipass:
        return None
    status = "취소" if m_status and "취소" in m_status.group() else "승인"

    body = RE_CUMUL.sub("", text)                       # 누적/한도 금액 제거
    m_amt = RE_AMOUNT.search(body)
    m_fx = RE_FOREIGN.search(body)
    if not m_amt and not m_fx:
        return None
    amount = int(m_amt.group(1).replace(",", "")) if m_amt else None
    currency, foreign = ("KRW", "") if not m_fx else (m_fx.group(1), m_fx.group(2).replace(",", ""))
    if status == "취소" and amount:
        amount = -amount

    # 거래 일시: 문자 본문 우선, 없으면 수신 시각
    when = msg_time
    m_dt = RE_DATETIME.search(body)
    if m_dt:
        mo, d, hh, mm = map(int, m_dt.groups())
        year = msg_time.year - (1 if mo > msg_time.month + 1 else 0)  # 1월에 받은 12/31 문자 처리
        try:
            when = dt.datetime(year, mo, d, hh, mm)
        except ValueError:
            pass
    m_ins = RE_INSTALL.search(body)
    installment = re.sub(r"\s", "", m_ins.group()) if m_ins else ""

    # 가맹점: 알려진 토큰을 지우고 남은 첫 조각
    merchant = ""
    for line in body.splitlines():
        s = line
        for pat in [RE_DATETIME.pattern, RE_AMOUNT.pattern, RE_FOREIGN.pattern, RE_INSTALL.pattern] + NOISE:
            s = re.sub(pat, " ", s)
        s = re.sub(r"\s+", " ", s).strip(" -:/()[]")
        if s:
            merchant = s
            break

    if hipass:
        merchant = "후불하이패스"
    return {
        "datetime": when.strftime("%Y-%m-%d %H:%M"),
        "date": when.strftime("%Y-%m-%d"),
        "card": card,
        "status": status,
        "amount": amount if amount is not None else "",
        "currency": currency,
        "foreign_amount": foreign,
        "installment": installment,
        "merchant": merchant,
        "category": guess_category(merchant),
        "raw": text.replace("\n", " / "),
    }


# ---------------------------- chat.db ----------------------------
APPLE_EPOCH = dt.datetime(2001, 1, 1)


def apple_to_local(v):
    sec = v / 1e9 if v > 1e11 else v                    # 최신 macOS는 나노초 단위
    utc = APPLE_EPOCH + dt.timedelta(seconds=sec)
    return utc.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)


def local_to_apple(t):
    utc = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return int((utc - APPLE_EPOCH).total_seconds() * 1e9)


def decode_attributed_body(blob):
    """최신 macOS에서 text가 비어 있을 때 attributedBody에서 본문 추출."""
    if not blob:
        return None
    b = bytes(blob)
    i = b.find(b"NSString")
    if i < 0:
        return None
    b = b[i + len(b"NSString"):]
    j = b.find(b"+")
    if j < 0:
        return None
    b = b[j + 1:]
    n, off = b[0], 1
    if n == 0x81:
        n, off = int.from_bytes(b[1:3], "little"), 3
    elif n == 0x82:
        n, off = int.from_bytes(b[1:4], "little"), 4
    return b[off:off + n].decode("utf-8", errors="ignore")


def open_chat_db_copy():
    """잠금 충돌을 피하려고 chat.db(+wal/shm)를 임시 폴더에 복사해서 연다."""
    src = CONFIG["CHAT_DB"]
    if not os.path.exists(src):
        sys.exit(f"chat.db를 찾을 수 없습니다: {src}")
    tmp = tempfile.mkdtemp(prefix="cardsms_")
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(src + suffix):
                shutil.copy2(src + suffix, os.path.join(tmp, "chat.db" + suffix))
    except PermissionError:
        sys.exit("chat.db 읽기 권한이 없습니다. 시스템 설정 → 개인정보 보호 및 보안 → "
                 "전체 디스크 접근 권한에 python3(또는 터미널)을 추가하세요.")
    return sqlite3.connect(os.path.join(tmp, "chat.db")), tmp


def fetch_messages(conn, after_rowid, since_apple=None, limit=None):
    q = ("SELECT m.ROWID, m.date, m.text, m.attributedBody FROM message m "
         "WHERE m.is_from_me = 0 AND m.ROWID > ?")
    args = [after_rowid]
    if since_apple is not None:
        q += " AND m.date >= ?"
        args.append(since_apple)
    q += " ORDER BY m.ROWID"
    if limit:
        q = q.replace("ORDER BY m.ROWID", "ORDER BY m.ROWID DESC") + f" LIMIT {int(limit)}"
    for rowid, date, text, body in conn.execute(q, args):
        yield rowid, apple_to_local(date or 0), text or decode_attributed_body(body) or ""


# ---------------------------- 저장/전송 ----------------------------
def state_path():
    return os.path.join(CONFIG["WORK_DIR"], "state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_rowid": 0}


def save_state(st):
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f)


def append_csv(rows):
    path = os.path.join(CONFIG["WORK_DIR"], "spend_log.csv")
    new = not os.path.exists(path)
    if not new:
        with open(path, encoding="utf-8-sig") as f:
            have = {r.get("id") for r in csv.DictReader(f)}
        rows = [r for r in rows if str(r["id"]) not in have]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        if new:
            w.writeheader()
        w.writerows(rows)


def post_webapp(rows):
    if not CONFIG["WEBAPP_URL"]:
        return True
    data = json.dumps({"token": CONFIG["WRITE_TOKEN"], "kind": "spend", "rows": rows}).encode("utf-8")
    req = urllib.request.Request(CONFIG["WEBAPP_URL"], data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
            if not res.get("ok"):
                print(f"[경고] 시트 응답 오류: {res}")
            return bool(res.get("ok"))
    except Exception as e:
        print(f"[경고] 시트 전송 실패: {e}")
        return False


def git_pull():
    """저장소 최신 코드 받기 — 2분마다 실행돼도 GitHub 조회는 10분에 한 번만."""
    if not CONFIG.get("AUTO_GIT_PULL"):
        return
    import subprocess
    stamp = os.path.join(CONFIG["WORK_DIR"], ".last_pull")
    try:
        if time.time() - os.path.getmtime(stamp) < 600:
            return
    except OSError:
        pass
    try:
        open(stamp, "w").close()
        subprocess.run(["git", "-C", HERE, "pull", "--ff-only", "-q"],
                       timeout=60, capture_output=True)
    except Exception as e:
        print(f"[경고] git pull 실패: {e}")


def collect():
    os.makedirs(CONFIG["WORK_DIR"], exist_ok=True)
    git_pull()
    st = load_state()
    if st.get("parser_version", 1) < PARSER_VERSION:     # 인식 규칙이 바뀌면 지난 문자를 한 번 다시 읽음 (시트는 id로 중복 제거)
        st["last_rowid"] = 0
    conn, tmp = open_chat_db_copy()
    try:
        since = None
        if st["last_rowid"] == 0:
            since = local_to_apple(dt.datetime.now().astimezone()
                                   - dt.timedelta(days=CONFIG["FIRST_RUN_LOOKBACK_DAYS"]))
        rows, max_id = [], st["last_rowid"]
        for rowid, t, text in fetch_messages(conn, st["last_rowid"], since):
            max_id = max(max_id, rowid)
            p = parse_card_sms(text, t)
            if p:
                rows.append({"id": rowid, **p})
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    if rows:
        append_csv(rows)
        if not post_webapp(rows):
            pending = st.get("pending", []) + rows      # 실패분은 다음 실행 때 재전송
            st["pending"] = pending[-500:]
    if st.get("pending") and post_webapp(st["pending"]):
        st["pending"] = []
    st["last_rowid"] = max_id
    st["parser_version"] = PARSER_VERSION
    save_state(st)
    now = dt.datetime.now()
    if rows or st.get("pending") or now.minute < 2:       # 새 내역이 있을 때 + 매시 정각 무렵 1줄(동작 확인용)
        print(f"[{now:%Y-%m-%d %H:%M}] 신규 카드 내역 {len(rows)}건")


def scan(n):
    conn, tmp = open_chat_db_copy()
    try:
        shown = 0
        for rowid, t, text in fetch_messages(conn, 0, limit=3000):
            if detect_card(text) and RE_STATUS.search(text):
                print("─" * 60)
                print(f"#{rowid}  {t:%Y-%m-%d %H:%M}\n{text}")
                print("→", json.dumps(parse_card_sms(text, t), ensure_ascii=False))
                shown += 1
                if shown >= n:
                    break
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


SAMPLES = [
    "[Web발신]\nKB국민카드1*2*승인\n홍*동님\n12,500원 일시불\n10/01 12:31\n스타벅스\n누적1,234,560원",
    "[Web발신]\n삼성1234승인 홍*동\n89,000원 03개월\n10/01 19:05 쿠팡\n누적845,000원",
    "[Web발신]\n현대카드 M 승인\n홍*동\n4,800원 일시불\n10/01 08:12\nGS25\n누적 512,300원",
    "[Web발신]\n현대카드 M 승인취소\n홍*동\n4,800원 일시불\n10/01 08:40\nGS25",
    "[Web발신]\nKB국민카드1*2*해외승인\n홍*동님\nUSD 25.99\n09/30 23:10\nNETFLIX.COM",
    "[Web발신]\n삼성증권 체결 안내 삼성전자 10주 매수",  # 카드 문자 아님 → None
    "[Web발신]\n현대 대한항공030 승인\n오*주\n15,800원 일시불\n10/02 08:13\n스타벅스코리아\n누적605,206원",
    "[Web발신]\n[삼성카드]8807\n10월접수 후불하이패스\n사용합계\n7,600원",
    "[Web발신]\n[삼성카드]7166 10/02 09:25 네이버페이 10,900원 승인거절 이용정지카드",  # 거절 → None
    "[Web발신]\n[프리미엄콘텐츠] 블랙 아카데미 1개월 이용권 10,900원이 결제되었습니다.",  # 카드 문자 아님 → None
]


def test():
    now = dt.datetime(2026, 10, 1, 20, 0)
    for s in SAMPLES:
        print(json.dumps(parse_card_sms(s, now), ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="카드 승인 문자 수집기 (OSJ)")
    ap.add_argument("--scan", type=int, metavar="N", help="최근 카드 문자 N건 원문/파싱 결과 출력")
    ap.add_argument("--test", action="store_true", help="내장 샘플 파싱 테스트")
    a = ap.parse_args()
    if a.test:
        test()
    elif a.scan:
        scan(a.scan)
    else:
        collect()
