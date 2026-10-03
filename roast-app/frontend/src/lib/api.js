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
// Offline observation-package import.  Files are read LOCALLY in the browser
// (FileReader) and posted as raw bytes to the same offline backend — nothing
// is uploaded to any external service.
// ---------------------------------------------------------------------------

export async function importObservationFile(file) {
  const bytes = await file.arrayBuffer();
  const r = await fetch('/api/imports', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: bytes,
  });
  // 409 (package_id/content clash) still carries a JSON error envelope
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `导入请求失败 (${r.status})`);
  return body;
}

export async function listImports(status) {
  const r = await fetch(`/api/imports${status ? `?status=${encodeURIComponent(status)}` : ''}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getImport(id) {
  const r = await fetch(`/api/imports/${id}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function resolveImport(id, resolutions) {
  const r = await fetch(`/api/imports/${id}/resolutions`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ resolutions }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function applyImport(id) {
  const r = await fetch(`/api/imports/${id}/apply`, { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function discardImport(id) {
  const r = await fetch(`/api/imports/${id}/discard`, { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// Client-side helper: compute the v1 content digest of a locally selected
// package BEFORE sending it, purely for display/verification.  Uses the same
// canonicalisation as the backend (documented keys, sorted JSON).
const SAMPLE_KEYS = ['t_s', 'bean_temp_c', 'env_temp_c', 'sampled_at'];
const EVENT_KEYS = ['event_type', 't_s', 'label', 'source', 'created_by', 'value_num', 'note'];

function canonicalValue(v) {
  if (Array.isArray(v)) return v.map(canonicalValue);
  if (v && typeof v === 'object') {
    return Object.fromEntries(Object.keys(v).sort().map((k) => [k, canonicalValue(v[k])]));
  }
  return v;
}

export function canonicalPackageContent(pkg) {
  const batch = pkg.batch || {};
  return canonicalValue({
    format_version: pkg.format_version,
    package_id: pkg.package_id,
    generated_at: pkg.generated_at ?? null,
    batch: Object.fromEntries(Object.keys(batch).sort().map((k) => [k, batch[k]])),
    samples: (pkg.samples || []).map((s) => Object.fromEntries(SAMPLE_KEYS.map((k) => [k, s[k] ?? null]))),
    events: (pkg.events || []).map((e) => Object.fromEntries(EVENT_KEYS.map((k) => [e[k] ?? null]))),
  });
}

export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(text)
  );
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export async function summarizeLocalFile(file) {
  // Inspect a local observation file WITHOUT sending it anywhere.
  const text = await file.text();
  let pkg;
  try {
    pkg = JSON.parse(text);
  } catch {
    return { ok: false, error: '本地预览：文件不是合法 JSON' };
  }
  if (!pkg || typeof pkg !== 'object') return { ok: false, error: '本地预览：根节点必须是对象' };
  const samples = Array.isArray(pkg.samples) ? pkg.samples : [];
  const events = Array.isArray(pkg.events) ? pkg.events : [];
  const canonical = JSON.stringify(canonicalPackageContent(pkg));
  const digest = await sha256Hex(canonical);
  return {
    ok: true,
    package_id: pkg.package_id,
    format_version: pkg.format_version,
    generated_at: pkg.generated_at,
    batch_name: pkg.batch?.name,
    n_samples: samples.length,
    n_events: events.length,
    t_first_s: samples.length ? Math.min(...samples.map((s) => Number(s.t_s))) : null,
    t_last_s: samples.length ? Math.max(...samples.map((s) => Number(s.t_s))) : null,
    n_missing_bean: samples.filter((s) => s.bean_temp_c === null || s.bean_temp_c === undefined).length,
    n_missing_env: samples.filter((s) => s.env_temp_c === null || s.env_temp_c === undefined).length,
    local_sha256: digest,
    summary_sha256: pkg.summary?.sha256,
    digest_matches: pkg.summary?.sha256 === digest,
  };
}

export const IMPORT_STATUS_LABELS = {
  pending_review: '待核验',
  applied: '已应用',
  rejected: '整包失败',
  discarded: '已丢弃',
};

export const DECISION_LABELS = {
  keep_existing: '保留现有值',
  use_incoming: '采用包内新值（旧值标记 superseded）',
  supersede: '采用包内新值（旧值标记 superseded）',
};


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
