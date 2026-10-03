/**
 * 카드 지출 달력 API (Google Apps Script 웹앱)  —  Copyright (c) OSJ
 *
 *  GET  ?token=READ_TOKEN&from=YYYY-MM-DD&to=YYYY-MM-DD  → { ok, spend:[...] }
 *  POST { token: WRITE_TOKEN, kind: 'spend', op: 'add' (기본), rows: [...] }                    → { ok, added }
 *  POST { token: WRITE_TOKEN, kind, op: 'update', id, fields: { memo, category, ... } }      → { ok, updated }
 *  POST { token: WRITE_TOKEN, kind, op: 'delete', id }                                       → { ok, deleted }
 *
 *  비밀값(토큰)은 코드가 아니라 '스크립트 속성'에 저장됩니다. → GitHub에 올려도 안전
 *  최초 1회: 편집기에서 setup() 실행 → 실행 로그에 READ / WRITE 토큰 출력
 *  코드를 바꾼 뒤에는: 배포 → 배포 관리 → ✏️ → 버전 '새 버전' → 배포 (URL 유지)
 */

const SHEETS = {
  spend: {
    name: 'Spend',
    headers: ['id', 'datetime', 'date', 'card', 'status', 'amount', 'currency',
      'foreign_amount', 'installment', 'merchant', 'category', 'raw', 'memo'],
    // 수기 입력(id가 m- 로 시작)은 전부 수정 가능, 카드 문자 자동 입력은 아래 AUTO_EDITABLE만
    editable: ['date', 'datetime', 'card', 'amount', 'installment', 'merchant', 'category', 'memo'],
    autoEditable: ['category', 'memo'],
  },
};
const PROPS = PropertiesService.getScriptProperties();
const TZ = Session.getScriptTimeZone() || 'Asia/Seoul';

/** 최초 1회 실행: 시트 생성 + 토큰 발급 */
function setup() {
  Object.keys(SHEETS).forEach(sheet_);
  if (!PROPS.getProperty('READ_TOKEN')) PROPS.setProperty('READ_TOKEN', Utilities.getUuid().replace(/-/g, ''));
  if (!PROPS.getProperty('WRITE_TOKEN')) PROPS.setProperty('WRITE_TOKEN', Utilities.getUuid().replace(/-/g, ''));
  Logger.log('READ_TOKEN  (달력 보기용) : ' + PROPS.getProperty('READ_TOKEN'));
  Logger.log('WRITE_TOKEN (입력·수집기용): ' + PROPS.getProperty('WRITE_TOKEN'));
}

/** 토큰 재발급이 필요할 때 실행 (기존 토큰 즉시 무효) */
function rotateTokens() {
  PROPS.deleteProperty('READ_TOKEN');
  PROPS.deleteProperty('WRITE_TOKEN');
  setup();
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!auth_(p.token, 'READ_TOKEN')) return out_({ ok: false, error: 'auth' });
  const from = p.from || '0000-00-00';
  const to = p.to || '9999-12-31';
  return out_({
    ok: true,
    spend: read_('spend', from, to),
  });
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'bad json' }); }
  if (!auth_(body.token, 'WRITE_TOKEN')) return out_({ ok: false, error: 'auth' });
  const kind = body.kind || 'spend';
  if (!SHEETS[kind]) return out_({ ok: false, error: 'bad kind' });
  const op = body.op || 'add';

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const def = SHEETS[kind];
    const sh = sheet_(kind);
    const head = headers_(sh, def);
    const iId = head.indexOf('id');
    const last = sh.getLastRow();
    const ids = last > 1 ? sh.getRange(2, iId + 1, last - 1, 1).getValues().flat().map(String) : [];

    if (op === 'add') {
      const seen = new Set(ids.filter(String));
      const rows = (body.rows || [])
        .filter(r => !(r.id && seen.has(String(r.id))))                 // id 기준 중복 제거
        .map(r => head.map(h => (r[h] === undefined || r[h] === null) ? '' : r[h]));
      if (rows.length) sh.getRange(last + 1, 1, rows.length, head.length).setValues(rows);
      return out_({ ok: true, added: rows.length });
    }

    const idx = ids.indexOf(String(body.id));
    if (!body.id || idx < 0) return out_({ ok: false, error: 'not found' });
    const rowNo = idx + 2;

    if (op === 'update') {
      const allowed = String(body.id).startsWith('m-') ? def.editable : def.autoEditable;
      const fields = body.fields || {};
      let n = 0;
      Object.keys(fields).forEach(k => {
        const c = head.indexOf(k);
        if (c >= 0 && allowed.indexOf(k) >= 0) { sh.getRange(rowNo, c + 1).setValue(fields[k]); n++; }
      });
      return out_({ ok: true, updated: n });
    }

    if (op === 'delete') {
      sh.deleteRow(rowNo);
      return out_({ ok: true, deleted: 1 });
    }
    return out_({ ok: false, error: 'bad op' });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------- helpers
function auth_(token, prop) {
  const t = PROPS.getProperty(prop);
  return !!t && token === t;
}

function sheet_(kind) {
  const def = SHEETS[kind];
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(def.name);
  if (!sh) sh = ss.insertSheet(def.name);
  if (sh.getLastRow() === 0) {
    sh.appendRow(def.headers);
    sh.setFrozenRows(1);
  }
  headers_(sh, def);
  return sh;
}

/** 시트 1행 머리글을 읽고, 새로 생긴 열(예: memo)이 없으면 오른쪽 끝에 추가 */
function headers_(sh, def) {
  const lastCol = Math.max(sh.getLastColumn(), 1);
  let head = sh.getRange(1, 1, 1, lastCol).getValues()[0].map(String);
  while (head.length && head[head.length - 1] === '') head.pop();
  const missing = def.headers.filter(h => head.indexOf(h) < 0);
  if (missing.length) {
    sh.getRange(1, head.length + 1, 1, missing.length).setValues([missing]);
    head = head.concat(missing);
  }
  return head;
}

function read_(kind, from, to) {
  const sh = sheet_(kind);
  const values = sh.getDataRange().getValues();
  const head = values.shift().map(String);
  const iDate = head.indexOf('date');
  return values
    .map(row => {
      const o = {};
      head.forEach((h, i) => { if (h) o[h] = norm_(h, row[i]); });
      if (!o.date && o.datetime) o.date = String(o.datetime).slice(0, 10);
      return o;
    })
    .filter(o => iDate >= 0 && o.date && o.date >= from && o.date <= to);
}

/** 시트가 날짜/시각을 Date 객체로 바꿔 저장해도 문자열로 정규화 */
function norm_(h, v) {
  if (v instanceof Date) {
    if (h === 'date') return Utilities.formatDate(v, TZ, 'yyyy-MM-dd');
    if (h === 'time') return Utilities.formatDate(v, TZ, 'HH:mm');
    return Utilities.formatDate(v, TZ, 'yyyy-MM-dd HH:mm');
  }
  return v;
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
