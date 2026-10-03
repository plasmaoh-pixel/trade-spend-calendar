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
    "KB_APP_KEY": "",                                # 비워 두면 KB_ENV_FILE(.env)의 KB_OPENAPI_APP_KEY 사용
    "KB_APP_SECRET": "",                             # 비워 두면 KB_ENV_FILE(.env)의 KB_OPENAPI_APP_SECRET 사용
    "KB_ENV_FILE": "~/kb_swing_gpt/kb_swing/.env",   # 기존 kb_swing 의 키를 그대로 재사용
    "KB_IP_ADDR": "",                                # 비우면 자동 (KB는 요청 헤더에 IP·MAC 필수)
    "KB_MAC_ADDR": "",
    "SELL_TAX_RATE": 0.0020,                         # 매도 제세금률(추정용) — 현행 세율 확인 필요
    "FEE_RATE": 0.00015,                             # 수수료율(추정용)
}

# ---------------------------------------------------------------------
# KB증권 Open API (B2C) — 공식 예제 저장소(kbsecurities/kb-openapi) 형식
#   요청:  POST {BASE}/api/v1/{tr소문자}   {dataHeader:{ipAddr,macAddr}, dataBody:{...}}
#   응답:  {dataHeader:{processFlag:'A'|'B', processCode, processMessage}, dataBody:{...}}
#   SSQM2341 계좌별주문체결조회(하루 단위) · SSQM2952 계좌자산평가(매입평균가)
# ---------------------------------------------------------------------
KB_API = {
    "BASE_URL": "https://developer.kbsec.com:32484",
    "TOKEN_PATH": "/oauth2/token",
    "EXEC_TR": "SSQM2341",
    "EXEC_INPUTS": ("inq_clsf", "ccls_clsf", "ordr_dt", "is_cd", "ordr_no", "mthr_ordr_no", "orgn_ordr_no",
                    "s_ccls_amt", "b_ccls_amt", "s_ccls_q", "b_ccls_q", "ac_nm", "is_nm", "cn_clsf", "nxt_key"),
    "BAL_TR": "SSQM2952",
    "EMPTY_CODES": ("1861", "2149"),      # 조회할 자료가 없습니다
    "HOLIDAY_CODES": ("2854",),           # 주문일자가 현재일자보다 큽니다(주말·휴장일)
    "EXEC_FIELDS": {
        "time": ["ccls_ntc_tm", "ordr_tm"],
        "ticker": ["stnd_is_no", "is_cd", "shrt_cd"],
        "name": ["is_nm", "hngl_is_nm"],
        "side": ["trd_dl_ccd_nm", "dl_clsf_nm"],
        "side_code": ["trd_clsf"],          # '1' 매도
        "qty": ["tl_ccls_q", "ccls_q"],
        "price": ["ccls_uprc", "ccls_prc"],
        "amount": ["ccls_amt"],
        "order_no": ["ordr_no", "odno"],
        "amended": ["crct_cncl_ccd"],
    },
    "BAL_FIELDS": {
        "ticker": ["is_cd", "shrt_cd", "is_no", "stnd_is_cd"],
        "name": ["is_nm"],
        "qty": ["ec_q"],                  # 실보유수량 (오늘 판 종목은 0)
        "avg": ["byng_avr_prc"],          # 매입평균가
    },
    "SLEEP_SEC": 0.6,
    "MAX_PAGES": 30,
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
_env = os.path.expanduser(CONFIG["KB_ENV_FILE"] or "")
if (not CONFIG["KB_APP_KEY"] or not CONFIG["KB_APP_SECRET"]) and _env and os.path.exists(_env):
    for _line in open(_env, encoding="utf-8"):
        _k, _, _v = _line.strip().partition("=")
        _v = _v.strip().strip('"').strip("'")
        if _k == "KB_OPENAPI_APP_KEY" and not CONFIG["KB_APP_KEY"]:
            CONFIG["KB_APP_KEY"] = _v
        elif _k == "KB_OPENAPI_APP_SECRET" and not CONFIG["KB_APP_SECRET"]:
            CONFIG["KB_APP_SECRET"] = _v


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
    """'매도' 문구 우선(공매도·신용매도 포함), 영문 sell, 또는 KB 매매구분 코드 '1'."""
    t = str(side).strip()
    if "매도" in t:
        return True
    if "매수" in t:
        return False
    return t.upper() in ("1", "SELL", "S")


def norm_code(v):
    """A005930 / KR7005930003 → 005930"""
    t = str(v).strip()
    if len(t) == 12 and t.startswith("KR7"):
        return t[3:9]
    if len(t) == 7 and t[0] == "A":
        return t[1:]
    return t


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
class KBError(RuntimeError):
    pass


def host_addr():
    import socket, uuid
    ip = CONFIG["KB_IP_ADDR"]
    if not ip:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                so.connect(("8.8.8.8", 80))
                ip = so.getsockname()[0]
        except OSError:
            ip = "127.0.0.1"
    mac = CONFIG["KB_MAC_ADDR"]
    if not mac:
        h = f"{uuid.getnode():012X}"
        mac = ":".join(h[i:i + 2] for i in range(0, 12, 2))
    return {"ipAddr": ip, "macAddr": mac}


_last_call = [0.0]


def kb_post(path, body, headers):
    time.sleep(max(0.0, KB_API["SLEEP_SEC"] - (time.monotonic() - _last_call[0])))
    _last_call[0] = time.monotonic()
    req = urllib.request.Request(KB_API["BASE_URL"] + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, text = r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:
        raise KBError(f"KB 서버에 연결하지 못했습니다: {e.reason}")
    try:
        data = json.loads(text)
    except ValueError:
        raise KBError(f"KB 응답이 JSON이 아닙니다 (HTTP {status})")
    head = data.get("dataHeader") if isinstance(data, dict) else None
    head = head if isinstance(head, dict) else {}
    return status, head, (data.get("dataBody", data) if isinstance(data, dict) else {})


def kb_token(force=False):
    cache = jload("kb_token.json", {})
    if not force and cache.get("token") and cache.get("exp", 0) > time.time() + 60:
        return cache["token"]
    if not CONFIG["KB_APP_KEY"] or not CONFIG["KB_APP_SECRET"]:
        sys.exit("KB 앱키를 찾지 못했습니다. config.local.json 의 KB_APP_KEY/KB_APP_SECRET 또는 "
                 f"{CONFIG['KB_ENV_FILE']} 를 확인하세요.")
    key, sec = CONFIG["KB_APP_KEY"], CONFIG["KB_APP_SECRET"]
    errors = []
    for body in ({"dataHeader": host_addr(), "dataBody": {"appKey": key, "appSecret": sec, "grantType": "client_credentials"}},
                 {"grant_type": "client_credentials", "appKey": key, "appSecret": sec}):
        status, head, b = kb_post(KB_API["TOKEN_PATH"], body, {})
        tok = b.get("access_token") or b.get("accessToken")
        if tok:
            ttl = to_num(b.get("expires_in") or b.get("expiresIn")) or 300
            jsave("kb_token.json", {"token": tok, "exp": time.time() + ttl - 60})
            return tok
        errors.append(f"HTTP {status} {head.get('processCode', '')} {head.get('processMessage', '')}".strip())
    raise KBError("토큰 발급 실패: " + " / ".join(errors))


def kb_call(tr, body):
    """TR 호출 → (rows, dataBody). 빈 결과·휴장일은 빈 목록."""
    for attempt in range(2):
        status, head, b = kb_post("/api/v1/" + tr.lower(), {"dataHeader": host_addr(), "dataBody": body},
                                  {"appKey": CONFIG["KB_APP_KEY"], "Authorization": "Bearer " + kb_token(force=attempt > 0)})
        code = str(head.get("processCode", "")).strip()
        if (status == 401 or code == "I445") and attempt == 0:
            continue                                        # 토큰 만료 → 재발급 후 1회 재시도
        flag = str(head.get("processFlag", "A")).strip().upper() or "A"
        if code in KB_API["EMPTY_CODES"] or code in KB_API["HOLIDAY_CODES"]:
            return [], b
        if status != 200 and not head:
            raise KBError(f"{tr} HTTP {status}")
        if flag != "A":
            raise KBError(f"{tr} 업무 오류: {code} {head.get('processMessage', '')}".strip())
        rows = next((v for v in b.values() if isinstance(v, list)), []) if isinstance(b, dict) else []
        return rows, b
    raise KBError("토큰을 갱신하지 못했습니다.")


def weekdays(d_from, d_to):
    d0, d1 = dt.date.fromisoformat(d_from), dt.date.fromisoformat(d_to)
    out = []
    while d0 <= d1:
        if d0.weekday() < 5:
            out.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    return out


def fetch_day_rows(day):
    """SSQM2341 하루치 체결 행 전부 (연속조회 포함)."""
    base = {k: "" for k in KB_API["EXEC_INPUTS"]}
    base.update({"inq_clsf": "1", "ccls_clsf": "1", "ordr_dt": day.replace("-", ""), "cn_clsf": "0"})
    rows, nxt, prev = [], "", None
    for _ in range(KB_API["MAX_PAGES"]):
        body = dict(base, nxt_key=nxt, cn_clsf="1" if nxt else "0")
        page, b = kb_call(KB_API["EXEC_TR"], body)
        rows += page
        prev, nxt = nxt, str((b or {}).get("nxt_key", "")).strip()
        if not nxt or nxt == prev:
            break
    return rows


def fetch_executions(d_from, d_to):
    """기간 체결 → 체결 1건 = 1행. 분할체결 연속 행(주문번호 0)은 앞 주문에 귀속."""
    F = KB_API["EXEC_FIELDS"]
    out = []
    for day in weekdays(d_from, d_to):
        header, seq = None, {}
        for r in fetch_day_rows(day):
            ono = str(pick(r, F["order_no"])).strip()
            cont = ono != "" and set(ono) == {"0"}
            if not cont:
                header = r
            elif header is None:
                continue                                      # 귀속할 주문이 없는 연속 행
            src = header
            ono = str(pick(src, F["order_no"])).strip()
            qty = to_num(pick(r, F["qty"]))
            price = to_num(pick(r, F["price"]))
            if qty <= 0 or price <= 0:
                continue
            side_name = pick(src, F["side"]) or pick(src, F["side_code"])
            seq[ono] = seq.get(ono, 0) + 1
            out.append({
                "date": day, "time": norm_time(pick(r, F["time"]) or pick(src, F["time"])),
                "ticker": norm_code(pick(src, F["ticker"])), "name": str(pick(src, F["name"])).strip(),
                "side": "매도" if is_sell(side_name) else "매수", "qty": qty, "price": price,
                "fee": None, "tax": None, "order_no": ono, "exec_no": str(seq[ono]), "api_pnl": None,
            })
    return out


def fetch_balance():
    rows, _ = kb_call(KB_API["BAL_TR"], {"excg_mktpr_ccd": "A"})
    F, pos = KB_API["BAL_FIELDS"], {}
    for r in rows:
        t = norm_code(pick(r, F["ticker"]))
        if not t or not t[:6].isdigit():
            continue                                          # 국내 종목만
        q, avg = to_num(pick(r, F["qty"])), to_num(pick(r, F["avg"]))
        if q > 0 or avg > 0:
            pos[t] = {"qty": q, "avg": avg, "name": str(pick(r, F["name"])).strip()}
    return pos


def normalize(r, F):
    """CSV 행 정규화 (HTS/MTS 체결내역 가져오기용)."""
    return {
        "date": norm_date(pick(r, F["date"])),
        "time": norm_time(pick(r, F["time"])),
        "ticker": norm_code(pick(r, F["ticker"])),
        "name": str(pick(r, F["name"])).strip(),
        "side": "매도" if is_sell(pick(r, F["side"])) else "매수",
        "qty": to_num(pick(r, F["qty"])),
        "price": to_num(pick(r, F["price"])),
        "fee": pick(r, F["fee"], None),
        "tax": pick(r, F["tax"], None),
        "order_no": str(pick(r, F["order_no"])).strip(),
        "exec_no": str(pick(r, F["exec_no"])).strip(),
        "api_pnl": None,
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
def sync(d_from, d_to, dry=False):
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
    if dry:
        print(f"[확인용 --dry] 체결 {len(rows)}건 — 시트로 보내지 않았고 동기화 기록도 저장하지 않았습니다.")
        return
    post_trades(rows)
    bal = fetch_balance()
    for t, p in pos.items():                          # 다 판 종목은 이름만 보존
        if t not in bal and p.get("name"):
            bal[t] = {"qty": 0, "avg": 0, "name": p["name"]}
    seen |= {r["id"] for r in rows}
    jsave("kb_state.json", {"positions": bal, "seen": sorted(seen)[-3000:], "last_day": d_to,
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
    """응답 구조 확인용. 금액·수량은 그대로, 키/토큰은 출력하지 않음."""
    for day in weekdays(d_from, d_to)[-3:]:
        rows = fetch_day_rows(day)
        print(f"===== {day} 체결 {len(rows)}행 =====")
        for r in rows[:5]:
            print(json.dumps(r, ensure_ascii=False))
    rows, _ = kb_call(KB_API["BAL_TR"], {"excg_mktpr_ccd": "A"})
    print(f"===== 계좌자산평가 {len(rows)}행 (필드명만) =====")
    if rows:
        print(sorted(rows[0].keys()))


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
    ap.add_argument("--from", dest="d_from", default=None, help="시작일 YYYY-MM-DD (기본: 마지막 동기화일, 첫 실행은 오늘)")
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
        st = jload("kb_state.json", None)
        d_from = a.d_from or (st.get("last_day") if st and st.get("last_day") else today)
        sync(d_from, a.d_to, dry=a.dry)
