#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KB증권 체결내역 → Google 시트(Trades) 동기화
- KB증권 Open API로 당일(또는 기간) 체결내역과 잔고를 받아 실현손익을 계산하고
  매매·지출 달력의 Trades 시트로 전송합니다.
- 사용법
    python3 kb_trades_sync.py                       # 오늘 체결분 동기화 (launchd로 장 마감 후 실행)
    python3 kb_trades_sync.py --from 2026-09-01     # 기간 동기화
    python3 kb_trades_sync.py --raw                 # API 응답 원문 출력 → 아래 KB_API 매핑 맞추기용
    python3 kb_trades_sync.py --import-csv 체결.csv  # HTS/MTS 체결내역 CSV로 과거 내역 채우기
    python3 kb_trades_sync.py --test                # 손익 계산 엔진 자체 테스트
- 비밀값(KB 앱키·시크릿·계좌, 시트 URL·WRITE_TOKEN)은 config.local.json 에 둡니다.
Copyright (c) OSJ
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# ============================== CONFIG ==============================
CONFIG = {
    "WORK_DIR": os.path.expanduser("~/card_sms"),   # 토큰 캐시·평단 기록 저장 (저장소 밖)
    "WEBAPP_URL": "",                                # Apps Script 웹앱 URL
    "WRITE_TOKEN": "",
    "KB_APP_KEY": "",
    "KB_APP_SECRET": "",
    "KB_ACCOUNT": "",                                # 계좌번호 (하이픈 없이)
    "KB_ACCOUNT_PRODUCT": "01",                      # 계좌 상품코드 ★ 문서 확인
    "SELL_TAX_RATE": 0.0020,                         # 매도 제세금률 — API가 세금을 주면 그 값을 우선 사용. 현행 세율 확인 필요
    "FEE_RATE": 0.00015,                             # 수수료율 — API가 수수료를 주면 그 값을 우선 사용
}

# ---------------------------------------------------------------------
# KB증권 API 매핑 — ★ 표시는 KB 개발가이드(API 문서 엑셀/JSON) 값으로 교체하세요.
#   1) --raw 로 실제 응답을 본 뒤  2) *_FIELDS 의 오른쪽 값을 실제 필드명으로 바꾸면 됩니다.
#   후보를 여러 개 적어두면 응답에 있는 첫 필드를 씁니다.
# ---------------------------------------------------------------------
KB_API = {
    "BASE_URL": "https://openapi.kbsec.com",                       # ★
    "TOKEN_PATH": "/oauth2/token",                                 # ★
    "TOKEN_BODY": {"grant_type": "client_credentials",             # ★ 키 이름 (appkey/appsecret 등)
                   "appkey": "{KB_APP_KEY}", "appsecret": "{KB_APP_SECRET}"},
    "HEADERS": {"appkey": "{KB_APP_KEY}", "appsecret": "{KB_APP_SECRET}"},  # ★ 공통 헤더
    "TR_HEADER": "tr_id",                                          # ★ 거래ID 헤더 이름

    # 주문체결 내역 조회 (국내주식)
    "EXEC_PATH": "/domestic-stock/v1/trading/inquire-daily-ccld",   # ★
    "EXEC_TR": "KB_EXEC_TR_ID",                                    # ★
    "EXEC_PARAMS": {                                               # ★ {from} {to} {acct} {prod} {ctx} 치환
        "CANO": "{acct}", "ACNT_PRDT_CD": "{prod}",
        "INQR_STRT_DT": "{from}", "INQR_END_DT": "{to}",
        "CCLD_DVSN": "01",                                         # 체결분만
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "{ctx}",
    },
    "EXEC_LIST_KEYS": ["output1", "output", "list", "data"],       # 체결 목록이 들어 있는 키 후보
    "NEXT_KEY_FIELD": "ctx_area_nk100",                            # 연속조회 키 (없으면 1페이지만)
    "EXEC_FIELDS": {
        "date": ["ord_dt", "ccld_dt", "trd_dt"],                   # YYYYMMDD
        "time": ["ccld_tmd", "ord_tmd", "ccld_time"],              # HHMMSS
        "ticker": ["pdno", "isu_cd", "stk_cd"],
        "name": ["prdt_name", "isu_nm", "stk_nm"],
        "side": ["sll_buy_dvsn_cd_name", "sll_buy_dvsn_cd", "trd_tp"],  # 01/매도, 02/매수 등
        "qty": ["tot_ccld_qty", "ccld_qty"],
        "price": ["avg_prvs", "ccld_unpr", "ccld_pric"],
        "fee": ["fee", "cmsn"],
        "tax": ["tax", "tlex"],
        "order_no": ["odno", "ord_no"],
        "exec_no": ["ccld_no", "exec_no"],
        "pnl": ["rlzt_pfls", "realized_pnl"],                      # API가 실현손익을 주면 그대로 사용
    },
    "SELL_CODES": ["01", "매도", "SELL", "S"],                      # side 값 중 매도 의미

    # 잔고 조회 (평균단가 기준점)
    "BAL_PATH": "/domestic-stock/v1/trading/inquire-balance",      # ★
    "BAL_TR": "KB_BALANCE_TR_ID",                                  # ★
    "BAL_PARAMS": {"CANO": "{acct}", "ACNT_PRDT_CD": "{prod}"},    # ★
    "BAL_LIST_KEYS": ["output1", "output", "list", "data"],
    "BAL_FIELDS": {
        "ticker": ["pdno", "isu_cd", "stk_cd"],
        "name": ["prdt_name", "isu_nm", "stk_nm"],
        "qty": ["hldg_qty", "bal_qty", "qty"],
        "avg": ["pchs_avg_pric", "avg_prc", "avg_unpr"],
    },
    "SLEEP_SEC": 0.3,
}

# HTS/MTS 체결내역 CSV 컬럼 매핑 (후보 중 있는 첫 컬럼 사용) — 파일 머리글에 맞게 추가하세요
CSV_COLUMNS = {
    "date": ["체결일자", "매매일자", "거래일자", "주문일자", "일자"],
    "time": ["체결시간", "주문시간", "시간"],
    "ticker": ["종목코드", "종목번호"],
    "name": ["종목명"],
    "side": ["매매구분", "매도매수구분", "구분", "거래구분"],
    "qty": ["체결수량", "수량", "거래수량"],
    "price": ["체결단가", "체결가", "평균단가", "단가", "거래단가"],
    "fee": ["수수료"],
    "tax": ["제세금", "세금", "거래세"],
    "order_no": ["주문번호"],
    "exec_no": ["체결번호"],
}
# ===================================================================

_local = os.path.join(HERE, "config.local.json")
if os.path.exists(_local):
    with open(_local, encoding="utf-8") as _f:
        CONFIG.update({k: os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v
                       for k, v in json.load(_f).items() if k in CONFIG})


# ------------------------------------------------------------- utils
def pick(row, cands, default=""):
    for c in cands:
        if c in row and row[c] not in (None, ""):
            return row[c]
    return default


def to_num(v):
    if v in (None, ""):
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return 0.0


def norm_date(v):
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else ""


def norm_time(v):
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) >= 4:
        s = s.zfill(6) if len(s) in (5, 6) else s
        return f"{s[:2]}:{s[2:4]}"
    return ""


def is_sell(side):
    s = str(side).strip().upper()
    return any(s == c.upper() or c in str(side) for c in KB_API["SELL_CODES"]) and "매수" not in str(side)


def fill(template, **kw):
    if isinstance(template, dict):
        return {k: fill(v, **kw) for k, v in template.items()}
    out = str(template)
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def ensure_dir():
    os.makedirs(CONFIG["WORK_DIR"], exist_ok=True)


def jload(name, default):
    try:
        with open(os.path.join(CONFIG["WORK_DIR"], name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def jsave(name, obj):
    ensure_dir()
    with open(os.path.join(CONFIG["WORK_DIR"], name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------- KB API
def http_json(method, url, headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json; charset=UTF-8", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}\n{e.read().decode('utf-8', 'ignore')[:500]}")


def kb_token():
    cache = jload("kb_token.json", {})
    if cache.get("token") and cache.get("exp", 0) > time.time() + 600:
        return cache["token"]
    if not CONFIG["KB_APP_KEY"]:
        sys.exit("config.local.json 에 KB_APP_KEY / KB_APP_SECRET / KB_ACCOUNT 를 입력하세요.")
    body = fill(KB_API["TOKEN_BODY"], KB_APP_KEY=CONFIG["KB_APP_KEY"], KB_APP_SECRET=CONFIG["KB_APP_SECRET"])
    res, _ = http_json("POST", KB_API["BASE_URL"] + KB_API["TOKEN_PATH"], body=body)
    tok = res.get("access_token")
    if not tok:
        raise RuntimeError(f"토큰 발급 실패: {res}")
    ttl = int(to_num(res.get("expires_in")) or 86400)
    jsave("kb_token.json", {"token": tok, "exp": time.time() + ttl})
    return tok


def kb_get(path, tr, params):
    headers = fill(KB_API["HEADERS"], KB_APP_KEY=CONFIG["KB_APP_KEY"], KB_APP_SECRET=CONFIG["KB_APP_SECRET"])
    headers["Authorization"] = "Bearer " + kb_token()
    headers[KB_API["TR_HEADER"]] = tr
    url = KB_API["BASE_URL"] + path + "?" + urllib.parse.urlencode(params)
    time.sleep(KB_API["SLEEP_SEC"])
    return http_json("GET", url, headers)


def list_from(res, keys):
    for k in keys:
        v = res.get(k)
        if isinstance(v, list):
            return v
    return []


def fetch_executions(d_from, d_to):
    out, ctx = [], ""
    for _ in range(20):                                   # 연속조회 최대 20페이지
        params = fill(KB_API["EXEC_PARAMS"], acct=CONFIG["KB_ACCOUNT"], prod=CONFIG["KB_ACCOUNT_PRODUCT"],
                      **{"from": d_from.replace("-", ""), "to": d_to.replace("-", ""), "ctx": ctx})
        res, hdr = kb_get(KB_API["EXEC_PATH"], KB_API["EXEC_TR"], params)
        rows = list_from(res, KB_API["EXEC_LIST_KEYS"])
        out += [normalize(r, KB_API["EXEC_FIELDS"]) for r in rows]
        ctx = str(res.get(KB_API["NEXT_KEY_FIELD"], "")).strip()
        if not ctx or not rows or hdr.get("tr_cont", "") not in ("F", "M"):
            break
    return [e for e in out if e["qty"] > 0]


def fetch_balance():
    params = fill(KB_API["BAL_PARAMS"], acct=CONFIG["KB_ACCOUNT"], prod=CONFIG["KB_ACCOUNT_PRODUCT"])
    res, _ = kb_get(KB_API["BAL_PATH"], KB_API["BAL_TR"], params)
    pos = {}
    for r in list_from(res, KB_API["BAL_LIST_KEYS"]):
        f = KB_API["BAL_FIELDS"]
        t = str(pick(r, f["ticker"])).strip()
        q = to_num(pick(r, f["qty"]))
        if t and q > 0:
            pos[t] = {"qty": q, "avg": to_num(pick(r, f["avg"])), "name": pick(r, f["name"])}
    return pos


def normalize(r, F):
    side_raw = pick(r, F["side"])
    pnl_raw = pick(r, F.get("pnl", []), None)
    return {
        "date": norm_date(pick(r, F["date"])),
        "time": norm_time(pick(r, F["time"])),
        "ticker": str(pick(r, F["ticker"])).strip().lstrip("A"),
        "name": str(pick(r, F["name"])).strip(),
        "side": "매도" if is_sell(side_raw) else "매수",
        "qty": to_num(pick(r, F["qty"])),
        "price": to_num(pick(r, F["price"])),
        "fee": pick(r, F["fee"], None),
        "tax": pick(r, F["tax"], None),
        "order_no": str(pick(r, F["order_no"])).strip(),
        "exec_no": str(pick(r, F["exec_no"])).strip(),
        "api_pnl": None if pnl_raw in (None, "") else to_num(pnl_raw),
    }


def exec_uid(e):
    uid = "-".join(x for x in [e["date"].replace("-", ""), e["ticker"], e["order_no"], e["exec_no"]] if x)
    if not (e["order_no"] or e["exec_no"]):
        uid = f"{e['date']}-{e['ticker']}-{e['time']}-{e['side']}-{e['qty']:g}-{e['price']:g}"
    return "kb-" + uid


def buy_fee(e):
    amt = e["qty"] * e["price"]
    return to_num(e["fee"]) if e["fee"] is not None else round(amt * CONFIG["FEE_RATE"])


# ------------------------------------------------------------- 실현손익 엔진 (이동평균법)
def compute_pnl(execs, positions):
    """
    execs: normalize() 결과 목록, positions: {ticker: {qty, avg}} (이 체결들 '이전' 보유 상태)
    매수: 평균단가 = (보유금액 + 매수금액 + 수수료) / 총수량
    매도: 실현손익 = 수량 × (체결가 − 평균단가) − 수수료 − 제세금
    반환: (Trades 시트 행 목록, 갱신된 positions)
    """
    pos = {k: dict(v) for k, v in positions.items()}
    rows = []
    for e in sorted(execs, key=lambda x: (x["date"], x["time"], x["order_no"], x["exec_no"])):
        amt = e["qty"] * e["price"]
        fee = buy_fee(e)
        p = pos.setdefault(e["ticker"], {"qty": 0.0, "avg": 0.0})
        pnl = ""
        if e["side"] == "매수":
            tot = p["qty"] + e["qty"]
            p["avg"] = (p["qty"] * p["avg"] + amt + fee) / tot if tot else 0.0
            p["qty"] = tot
            tax = 0
        else:
            tax = to_num(e["tax"]) if e["tax"] is not None else round(amt * CONFIG["SELL_TAX_RATE"])
            if e.get("api_pnl") is not None:
                pnl = round(e["api_pnl"])
            elif p["qty"] > 0:
                pnl = round(e["qty"] * (e["price"] - p["avg"]) - fee - tax)
            p["qty"] = max(0.0, p["qty"] - e["qty"])
            if p["qty"] == 0:
                p["avg"] = 0.0
        if e.get("name"):
            p["name"] = e["name"]
        rows.append({
            "id": exec_uid(e), "date": e["date"], "time": e["time"], "ticker": e["ticker"],
            "name": e["name"] or p.get("name", ""), "side": e["side"], "qty": e["qty"], "price": e["price"],
            "fee": fee + tax, "pnl": pnl, "memo": "" if pnl != "" or e["side"] == "매수" else "평단 기록 없음",
        })
    return rows, pos


# ------------------------------------------------------------- 시트 전송
def post_trades(rows):
    if not rows:
        return True
    if not CONFIG["WEBAPP_URL"]:
        print("[안내] WEBAPP_URL 이 비어 있어 시트 전송을 건너뜁니다.")
        return True
    body = json.dumps({"token": CONFIG["WRITE_TOKEN"], "kind": "trades", "rows": rows}).encode("utf-8")
    req = urllib.request.Request(CONFIG["WEBAPP_URL"], data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read().decode("utf-8"))
    if not res.get("ok"):
        raise RuntimeError(f"시트 응답 오류: {res}")
    print(f"시트에 {res.get('added', 0)}건 추가 (중복 제외)")
    return True


def print_rows(rows):
    for r in rows:
        print(f"{r['date']} {r['time']:>5} {r['side']} {r['name'] or r['ticker']:<10} "
              f"{r['qty']:>6g}주 × {r['price']:>10,.0f}  손익 {r['pnl'] if r['pnl'] != '' else '-'}")


# ------------------------------------------------------------- modes
def sync(d_from, d_to):
    """
    평단 기준점: kb_state.json (직전 동기화 직후의 실제 잔고 스냅샷)
    → 아직 처리하지 않은 체결만 순서대로 반영해 실현손익 계산
    → 끝나면 실제 잔고로 기준점을 덮어써 오차를 매번 보정
    권장 실행 시각: 장 마감 후(16시 이후) 하루 1회
    """
    ensure_dir()
    state = jload("kb_state.json", None)
    execs = fetch_executions(d_from, d_to)
    if state is None:
        bal = fetch_balance()
        base = rewind(bal, execs)                     # 첫 실행: 현재 잔고에서 체결을 되돌려 시작점 추정
        seen = set()
        print(f"[첫 실행] 잔고 {len(bal)}종목에서 기준 평단을 추정했습니다.")
    else:
        base, seen = state["positions"], set(state.get("seen", []))
    new = [e for e in execs if exec_uid(e) not in seen]
    rows, pos = compute_pnl(new, base)
    print_rows(rows)
    post_trades(rows)
    bal = fetch_balance()
    for t, p in pos.items():                          # 다 판 종목은 이름만 보존
        if t not in bal and p.get("name"):
            bal[t] = {"qty": 0, "avg": 0, "name": p["name"]}
    seen |= {r["id"] for r in rows}
    jsave("kb_state.json", {"positions": bal, "seen": sorted(seen)[-3000:],
                            "updated": dt.datetime.now().isoformat(timespec="seconds")})
    print(f"신규 체결 {len(rows)}건 처리, 보유 {sum(1 for v in bal.values() if v['qty'] > 0)}종목 기준점 갱신")


def rewind(bal, execs):
    """현재 잔고에서 주어진 체결을 역순으로 되돌려 '체결 이전' 상태 추정 (매도분은 같은 평단으로 복원)."""
    pos = {k: dict(v) for k, v in bal.items()}
    for e in sorted(execs, key=lambda x: (x["date"], x["time"]), reverse=True):
        p = pos.setdefault(e["ticker"], {"qty": 0.0, "avg": 0.0, "name": e["name"]})
        if e["side"] == "매도":
            p["qty"] += e["qty"]                          # 이동평균법: 매도는 평단 불변
        else:
            amt = e["qty"] * e["price"] + buy_fee(e)
            before = p["qty"] - e["qty"]
            p["avg"] = (p["qty"] * p["avg"] - amt) / before if before > 0 else 0.0
            p["qty"] = max(0.0, before)
    return pos


def raw_dump(d_from, d_to):
    params = fill(KB_API["EXEC_PARAMS"], acct=CONFIG["KB_ACCOUNT"], prod=CONFIG["KB_ACCOUNT_PRODUCT"],
                  **{"from": d_from.replace("-", ""), "to": d_to.replace("-", ""), "ctx": ""})
    print("===== 체결내역 응답 =====")
    res, hdr = kb_get(KB_API["EXEC_PATH"], KB_API["EXEC_TR"], params)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:6000])
    print("===== 응답 헤더 =====\n", {k: v for k, v in hdr.items() if k.lower().startswith(("tr", "gt"))})
    print("===== 잔고 응답 =====")
    res, _ = kb_get(KB_API["BAL_PATH"], KB_API["BAL_TR"],
                    fill(KB_API["BAL_PARAMS"], acct=CONFIG["KB_ACCOUNT"], prod=CONFIG["KB_ACCOUNT_PRODUCT"]))
    print(json.dumps(res, ensure_ascii=False, indent=1)[:6000])


def import_csv(path, dry):
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc, newline="") as f:
                data = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit("CSV 인코딩을 읽을 수 없습니다.")
    data = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in data]
    if not data:
        sys.exit("CSV에 데이터가 없습니다.")
    missing = [k for k in ("date", "side", "qty", "price") if not any(c in data[0] for c in CSV_COLUMNS[k])]
    if missing:
        sys.exit(f"CSV 머리글에서 {missing} 컬럼을 찾지 못했습니다. 머리글: {list(data[0].keys())}\n"
                 f"→ 스크립트의 CSV_COLUMNS 에 해당 머리글을 추가하세요.")
    execs = []
    for r in data:
        e = normalize(r, {**CSV_COLUMNS, "pnl": []})
        e["fee"] = e["fee"] if e["fee"] not in (None, "") else None
        e["tax"] = e["tax"] if e["tax"] not in (None, "") else None
        if e["date"] and e["qty"] > 0:
            execs.append(e)
    rows, pos = compute_pnl(execs, {})                   # CSV는 첫 매수부터 있다고 보고 0에서 시작
    print_rows(rows)
    no_basis = sum(1 for r in rows if r["memo"] == "평단 기록 없음")
    if no_basis:
        print(f"[주의] 매수 기록 없이 매도된 {no_basis}건은 손익을 비워 두었습니다. 더 이전 기간 CSV를 함께 넣으면 계산됩니다.")
    if not dry:
        post_trades(rows)


def self_test():
    CONFIG["FEE_RATE"], CONFIG["SELL_TAX_RATE"] = 0.0, 0.0
    ex = lambda d, t, s, q, p: {"date": d, "time": t, "ticker": "005930", "name": "삼성전자", "side": s,
                                "qty": q, "price": p, "fee": None, "tax": None, "order_no": "", "exec_no": "", "api_pnl": None}
    rows, pos = compute_pnl([ex("2026-10-01", "09:01", "매수", 10, 70000), ex("2026-10-01", "10:00", "매수", 10, 72000),
                             ex("2026-10-02", "11:00", "매도", 5, 75000)], {})
    assert pos["005930"]["avg"] == 71000 and pos["005930"]["qty"] == 15, pos
    assert rows[2]["pnl"] == 5 * (75000 - 71000), rows[2]
    # 되돌리기: 잔고(15주@71000) - 위 체결 → 0주
    back = rewind({"005930": {"qty": 15, "avg": 71000}}, [ex("2026-10-01", "09:01", "매수", 10, 70000),
                  ex("2026-10-01", "10:00", "매수", 10, 72000), ex("2026-10-02", "11:00", "매도", 5, 75000)])
    assert back["005930"]["qty"] == 0, back
    # 기존 보유 20주@60000 기준 매도
    rows, _ = compute_pnl([ex("2026-10-03", "09:30", "매도", 20, 66000)], {"005930": {"qty": 20, "avg": 60000}})
    assert rows[0]["pnl"] == 120000
    # 평단 기록 없는 매도
    rows, _ = compute_pnl([ex("2026-10-03", "09:30", "매도", 1, 66000)], {})
    assert rows[0]["pnl"] == "" and rows[0]["memo"] == "평단 기록 없음"
    print("손익 계산 엔진 테스트 통과")


if __name__ == "__main__":
    today = dt.date.today().isoformat()
    ap = argparse.ArgumentParser(description="KB증권 체결 → Trades 시트 동기화 (OSJ)")
    ap.add_argument("--from", dest="d_from", default=today, help="시작일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--to", dest="d_to", default=today, help="종료일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--raw", action="store_true", help="API 응답 원문 출력 (매핑 확인용)")
    ap.add_argument("--import-csv", metavar="FILE", help="HTS/MTS 체결내역 CSV 가져오기")
    ap.add_argument("--dry", action="store_true", help="시트로 보내지 않고 결과만 출력")
    ap.add_argument("--test", action="store_true", help="손익 계산 엔진 테스트")
    a = ap.parse_args()
    if a.test:
        self_test()
    elif a.raw:
        raw_dump(a.d_from, a.d_to)
    elif a.import_csv:
        import_csv(a.import_csv, a.dry)
    else:
        if a.dry:
            CONFIG["WEBAPP_URL"] = ""
        sync(a.d_from, a.d_to)
