/**
 * 매매·지출 달력 API (Google Apps Script 웹앱)  —  Copyright (c) OSJ
 *
 *  GET  ?token=READ_TOKEN&from=YYYY-MM-DD&to=YYYY-MM-DD  → { ok, spend:[...], trades:[...] }
 *  POST { token: WRITE_TOKEN, kind: 'spend' | 'trades', rows: [...] }  → { ok, added }
 *
 *  비밀값(토큰)은 코드가 아니라 '스크립트 속성'에 저장됩니다. → GitHub에 올려도 안전
 *  최초 1회: 편집기에서 setup() 실행 → 실행 로그에 READ / WRITE 토큰 출력
 */

const SHEETS = {
  spend: {
    name: 'Spend',
    headers: ['id', 'datetime', 'date', 'card', 'status', 'amount', 'currency',
      'foreign_amount', 'installment', 'merchant', 'category', 'raw'],
  },
  trades: {
    name: 'Trades',
    headers: ['id', 'date', 'time', 'ticker', 'name', 'side', 'qty', 'price',
      'fee', 'pnl', 'memo'],
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
  Logger.log('WRITE_TOKEN (수집기 전송용): ' + PROPS.getProperty('WRITE_TOKEN'));
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
    trades: read_('trades', from, to),
  });
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); } catch (err) { return out_({ ok: false, error: 'bad json' }); }
  if (!auth_(body.token, 'WRITE_TOKEN')) return out_({ ok: false, error: 'auth' });
  const kind = body.kind || 'spend';
  if (!SHEETS[kind]) return out_({ ok: false, error: 'bad kind' });

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const def = SHEETS[kind];
    const sh = sheet_(kind);
    const last = sh.getLastRow();
    const existing = new Set(last > 1
      ? sh.getRange(2, 1, last - 1, 1).getValues().flat().map(String).filter(String) : []);
    const rows = (body.rows || [])
      .filter(r => !(r.id && existing.has(String(r.id))))          // id 기준 중복 제거
      .map(r => def.headers.map(h => (r[h] === undefined || r[h] === null) ? '' : r[h]));
    if (rows.length) sh.getRange(last + 1, 1, rows.length, def.headers.length).setValues(rows);
    return out_({ ok: true, added: rows.length });
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
  return sh;
}

function read_(kind, from, to) {
  const sh = sheet_(kind);
  const values = sh.getDataRange().getValues();
  const head = values.shift().map(String);
  const iDate = head.indexOf('date');
  return values
    .map(row => {
      const o = {};
      head.forEach((h, i) => { o[h] = norm_(h, row[i]); });
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
