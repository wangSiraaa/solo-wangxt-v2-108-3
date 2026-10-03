// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

export async function getBatches() {
  const r = await fetch('/api/batches');
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function seed() {
  const r = await fetch('/api/seed', { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getSeries(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/series?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getCompare(a, b, params = {}) {
  const r = await fetch(`/api/compare?${qs({ a, b, ...params })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addEvent(batchId, ev) {
  const r = await fetch(`/api/batches/${batchId}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(ev),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listEvents(batchId, includeHistory = false) {
  const r = await fetch(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportBatch(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/export?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function recompute(body) {
  const r = await fetch('/api/recompute', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------------------------------------------------------------------------
// offline observation-package import (local files only; never uploaded)
// ---------------------------------------------------------------------------

async function _jsonOrText(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function listImports(status) {
  const q = status ? `?${new URLSearchParams({ status })}` : '';
  const r = await fetch(`/api/imports${q}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function receiveImport(pkg, targetBatchId = null, createdBy = '操作员(界面)') {
  const r = await fetch('/api/imports', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ package: pkg, target_batch_id: targetBatchId, created_by: createdBy }),
  });
  const body = await _jsonOrText(r);
  if (!r.ok) {
    const err = new Error(importErrorText(body));
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body;
}

export function importErrorText(body) {
  const d = body?.detail;
  if (d && typeof d === 'object' && d.findings) {
    return `整包失败（${d.error_code}）：\n` + d.findings.map((f) => `· [${f.code}] ${f.message}`).join('\n');
  }
  return typeof body === 'string' ? body : JSON.stringify(body);
}

export async function previewImport(id, params = {}) {
  const r = await fetch(`/api/imports/${id}/preview?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function resolveImport(id, resolutions, resolvedBy = '操作员(界面)') {
  const r = await fetch(`/api/imports/${id}/resolve`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ resolutions, resolved_by: resolvedBy }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function applyImport(id) {
  const r = await fetch(`/api/imports/${id}/apply`, { method: 'POST' });
  const body = await _jsonOrText(r);
  if (!r.ok) {
    const err = new Error(importErrorText(body));
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body;
}

export async function abortImport(id) {
  const r = await fetch(`/api/imports/${id}/abort`, { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export const EVENT_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
  damper_change: '风门变化',
  custom: '自定义',
};

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
