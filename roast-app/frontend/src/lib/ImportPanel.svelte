<script>
  // Offline observation-package import panel.
  //
  // Privacy rule: the file is read locally (file.text()/arrayBuffer()); it is
  // only POSTed to the same-origin offline backend when the operator clicks
  // import. Nothing here talks to any external host.
  import { createEventDispatcher } from 'svelte';
  import {
    importObservationFile,
    listImports,
    getImport,
    resolveImport,
    applyImport,
    discardImport,
    summarizeLocalFile,
    IMPORT_STATUS_LABELS,
    DECISION_LABELS,
    EVENT_LABELS,
    fmtTime,
  } from './api.js';

  const dispatch = createEventDispatcher();

  let file = null;
  let localInfo = null;
  let ledger = [];
  let busy = '';
  let errMsg = '';
  let note = '';
  // import id currently open in the review pane
  let openId = null;
  let openDetail = null;
  // ref -> decision while editing
  let decisions = {};

  const STATUS_CLASS = {
    pending_review: 'tag-pending',
    applied: 'tag-applied',
    rejected: 'tag-rejected',
    discarded: 'tag-muted',
  };

  async function refreshLedger() {
    ledger = await listImports();
  }

  async function onPick(e) {
    errMsg = '';
    note = '';
    localInfo = null;
    file = e.target.files?.[0] || null;
    if (!file) return;
    try {
      localInfo = await summarizeLocalFile(file);
    } catch (ex) {
      errMsg = `本地读取失败：${ex.message}`;
    }
  }

  async function doImport() {
    if (!file) {
      errMsg = '请先选择本地观察包 JSON 文件';
      return;
    }
    busy = '投递观察包（仅本机后端，不上传）…';
    errMsg = '';
    try {
      const res = await importObservationFile(file);
      note = res.redelivered ? `重复投递：${res.message}` : '新包已进入待核验状态';
      await refreshLedger();
      openId = res.id;
      await openImport(res.id);
      dispatch('changed');
      file = null;
      localInfo = null;
    } catch (ex) {
      errMsg = ex.message;
      await refreshLedger();
    } finally {
      busy = '';
    }
  }

  async function openImport(id) {
    openId = id;
    decisions = {};
    errMsg = '';
    try {
      openDetail = await getImport(id);
      if (openDetail.preview) {
        // seed radio buttons from any previously saved decisions
        decisions = { ...openDetail.resolutions };
      }
    } catch (ex) {
      errMsg = ex.message;
    }
  }

  function closeReview() {
    openId = null;
    openDetail = null;
    decisions = {};
  }

  $: conflicts = openDetail?.preview?.conflicts || null;
  $: allResolved = openDetail?.preview?.all_conflicts_resolved ?? false;
  $: conflictRows = conflicts
    ? [
        ...conflicts.sample_conflicts.map((c) => ({ ...c, channel: '样本' })),
        ...conflicts.event_conflicts.map((c) => ({ ...c, channel: '事件' })),
      ]
    : [];

  async function saveDecisions() {
    busy = '保存裁决…';
    errMsg = '';
    try {
      openDetail = await resolveImport(openId, decisions);
      note = '裁决已保存（尚未应用）';
    } catch (ex) {
      errMsg = ex.message;
    } finally {
      busy = '';
    }
  }

  async function doApply() {
    busy = '一次性应用（单事务，失败整体回滚）…';
    errMsg = '';
    try {
      const res = await applyImport(openId);
      openDetail = res;
      note = res.redelivered
        ? res.message
        : `已应用：新增样本 ${res.applied_result.samples_added}（取代 ${res.applied_result.samples_superseded}），` +
          `新增事件 ${res.applied_result.events_added}（取代 ${res.applied_result.events_superseded}），` +
          `跳过重复 ${res.applied_result.duplicates_skipped}`;
      await refreshLedger();
      dispatch('applied', { batchId: res.batch_id });
      dispatch('changed');
    } catch (ex) {
      errMsg = ex.message;
      await openImport(openId);
    } finally {
      busy = '';
    }
  }

  async function doDiscard() {
    if (!confirm('丢弃该待核验包？账本记录会保留用于审计，但不会写入任何数据。')) return;
    busy = '丢弃…';
    errMsg = '';
    try {
      await discardImport(openId);
      closeReview();
      await refreshLedger();
      dispatch('changed');
    } catch (ex) {
      errMsg = ex.message;
    } finally {
      busy = '';
    }
  }

  refreshLedger();
</script>

<section class="panel">
  <h2>③ 离线观察包导入（只读本地文件，不上传）</h2>
  <div class="muted" style="font-size:12px;margin:4px 0 10px">
    流程：选择本机 JSON 文件 → 本地校验格式版本/稳定包标识/内容摘要 → 投递后进入<b>待核验</b> →
    预览曲线与冲突、逐项裁决 → <b>一次性应用</b>（整包事务，不会留下半套曲线）。
  </div>

  {#if errMsg}<div class="warn" style="margin-bottom:8px">⚠ {errMsg}</div>{/if}
  {#if note}<div class="tag tag-applied" style="margin-bottom:8px;display:inline-block">{note}</div>{/if}

  <div class="row" style="align-items:flex-end;gap:10px">
    <div>
      <div class="muted">本地观察包</div>
      <input type="file" accept="application/json,.json" on:change={onPick} />
    </div>
    <button on:click={doImport} disabled={!file || !!busy}>投递并核验</button>
    {#if busy}<span class="muted">{busy}</span>{/if}
  </div>

  {#if localInfo}
    <div style="margin-top:10px;border:1px solid var(--line);padding:10px;border-radius:6px">
      {#if !localInfo.ok}
        <span class="warn">⚠ {localInfo.error}</span>
      {:else}
        <div class="muted" style="font-size:12px;margin-bottom:4px">本地预检（未发送）</div>
        <table>
          <tr>
            <th>包标识</th><th>版本</th><th>批次</th><th>生成时间</th>
            <th>样本</th><th>事件</th><th>时间范围</th><th>缺测(豆/环)</th><th>摘要</th>
          </tr>
          <tr>
            <td>{localInfo.package_id || '—'}</td>
            <td>v{localInfo.format_version ?? '?'}</td>
            <td>{localInfo.batch_name || '—'}</td>
            <td style="font-size:11px">{localInfo.generated_at || '—'}</td>
            <td>{localInfo.n_samples}</td>
            <td>{localInfo.n_events}</td>
            <td>{localInfo.t_first_s}–{localInfo.t_last_s}s</td>
            <td>{localInfo.n_missing_bean}/{localInfo.n_missing_env}</td>
            <td>
              {#if localInfo.digest_matches}
                <span class="tag tag-applied">sha256 一致</span>
              {:else}
                <span class="tag tag-rejected">sha256 不一致</span>
              {/if}
            </td>
          </tr>
        </table>
      {/if}
    </div>
  {/if}

  <div class="row" style="margin-top:12px;align-items:flex-start">
    <div style="flex:0 0 320px;min-width:280px">
      <div class="muted" style="margin-bottom:4px">导入账本（审计）</div>
      <table>
        <tr><th>#</th><th>包标识</th><th>状态</th><th>批次</th></tr>
        {#each ledger.slice(0, 30) as imp}
          <tr style="cursor:pointer;{openId === imp.id ? 'background:#332c25' : ''}"
              on:click={() => openImport(imp.id)}>
            <td>{imp.id}</td>
            <td style="font-size:11px;max-width:130px;overflow:hidden;text-overflow:ellipsis">
              {#if imp.package_id}{imp.package_id}{:else}<span class="warn">无法解析</span>{/if}
            </td>
            <td><span class={`tag ${STATUS_CLASS[imp.status] || ''}`}>{IMPORT_STATUS_LABELS[imp.status] || imp.status}</span></td>
            <td class="muted" style="font-size:11px">{imp.batch_name || '—'}</td>
          </tr>
        {/each}
        {#if ledger.length === 0}
          <tr><td colspan="4" class="muted">暂无导入记录</td></tr>
        {/if}
      </table>
    </div>

    {#if openDetail}
      <div style="flex:1;min-width:360px;border:1px solid var(--line);border-radius:6px;padding:10px">
        <div class="row" style="justify-content:space-between">
          <div>
            <b>#{openDetail.id} · {openDetail.package_id || '（无标识）'}</b>
            <span class={`tag ${STATUS_CLASS[openDetail.status]}`} style="margin-left:8px">
              {IMPORT_STATUS_LABELS[openDetail.status]}
            </span>
          </div>
          <button class="ghost" on:click={closeReview}>关闭</button>
        </div>

        {#if openDetail.status === 'rejected'}
          <div class="warn" style="margin-top:8px">
            整包失败 · 错误码 <code>{openDetail.error_code}</code><br />
            {openDetail.error_detail}
          </div>
          <div class="muted" style="font-size:11px;margin-top:6px">
            未写入任何批次/样本/事件；原始包与错误已留在账本中可审计。重新投递完全相同的文件会返回本记录。
          </div>
        {:else if openDetail.status === 'discarded'}
          <div class="muted" style="margin-top:8px">该包已丢弃，未应用任何内容。</div>
        {:else if openDetail.status === 'applied'}
          <div class="muted" style="margin-top:8px">
            该包已应用（批次 #{openDetail.batch_id} · {openDetail.batch_name}）。重复投递不会再新增任何数据。
          </div>
        {:else}
          {#if openDetail.preview}
            {@const c = openDetail.preview.conflicts}
            {@const p = openDetail.preview.projected}
            <div style="margin-top:8px;font-size:12px">
              目标批次：<b>{c.batch_name}</b>
              {c.batch_exists ? '（已存在，将合入）' : '（新批次，应用时创建）'} ·
              新增样本 <b>{c.sample_additions}</b> ·
              重复跳过 {c.duplicate_samples} ·
              新增事件 <b>{c.event_additions}</b> ·
              重复事件 {c.duplicate_events}
            </div>

            {#if conflictRows.length}
              <h3 style="margin-top:10px">冲突审阅（{conflictRows.length} 项，必须逐项裁决）</h3>
              <table>
                <tr><th>类型</th><th>时刻</th><th>现有值</th><th>包内值</th><th>裁决</th></tr>
                {#each conflictRows as cf}
                  <tr>
                    <td>{cf.channel}</td>
                    <td>
                      {cf.channel === '事件' ? EVENT_LABELS[cf.event_type] || cf.event_type : '豆温/环温'}
                      <div class="muted" style="font-size:11px">{fmtTime(cf.t_s)} ({cf.t_s}s)</div>
                    </td>
                    <td style="font-size:11px">
                      {#if cf.channel === '样本'}
                        豆 {cf.existing.bean_temp_c ?? '缺测'} / 环 {cf.existing.env_temp_c ?? '缺测'}
                        <div class="muted">来源：{cf.existing.source}{cf.existing.import_package_id ? ` · ${cf.existing.import_package_id}` : ''}</div>
                      {:else}
                        {fmtTime(cf.existing.t_s)}{cf.existing.value_num !== null ? ` · ${cf.existing.value_num}%` : ''}
                        <div class="muted">来源：{cf.existing.source}{cf.existing.import_package_id ? ` · ${cf.existing.import_package_id}` : ''}</div>
                      {/if}
                    </td>
                    <td style="font-size:11px">
                      {#if cf.channel === '样本'}
                        豆 {cf.incoming.bean_temp_c ?? '缺测'} / 环 {cf.incoming.env_temp_c ?? '缺测'}
                      {:else}
                        {fmtTime(cf.incoming.t_s)}{cf.incoming.value_num !== null ? ` · ${cf.incoming.value_num}%` : ''}
                      {/if}
                    </td>
                    <td>
                      <label style="display:block">
                        <input type="radio" name={cf.ref} value="keep_existing" bind:group={decisions[cf.ref]} />
                        保留现有
                      </label>
                      <label style="display:block">
                        <input type="radio" name={cf.ref} value="use_incoming" bind:group={decisions[cf.ref]} />
                        采用新值（旧值 superseded 保留）
                      </label>
                    </td>
                  </tr>
                {/each}
              </table>
            {/if}

            <h3 style="margin-top:10px">预览（应用后的阶段指标）</h3>
            <table>
              <tr><th>脱水</th><th>梅纳</th><th>发展</th><th>总时长</th><th>DTR</th></tr>
              <tr>
                <td>{fmtTime(p.metrics.drying_s)}</td>
                <td>{fmtTime(p.metrics.maillard_s)}</td>
                <td>{fmtTime(p.metrics.development_s)}</td>
                <td>{fmtTime(p.metrics.total_s)}</td>
                <td>{p.metrics.development_ratio !== null ? (p.metrics.development_ratio * 100).toFixed(1) + '%' : '—'}</td>
              </tr>
            </table>
            <div class="muted" style="font-size:11px;margin-top:4px">
              预览基于当前库内数据实时计算；未裁决前现有批次的分析不会改变。
              合并后曲线点 {p.samples.length} 个，事件 {p.events.length} 条（含已取代历史）。
            </div>

            <div class="row" style="margin-top:10px;gap:8px">
              <button on:click={saveDecisions} disabled={!!busy || conflictRows.length === 0}>
                保存裁决
              </button>
              <button on:click={doApply} disabled={!!busy}
                      style={allResolved ? '' : 'opacity:.55'}
                      title={allResolved ? '' : '仍有冲突未裁决'}>
                ④ 一次性应用
              </button>
              <button class="ghost" on:click={doDiscard} disabled={!!busy}>丢弃（保留审计）</button>
              {#if !allResolved && conflictRows.length}
                <span class="warn" style="font-size:12px">仍有 {conflictRows.length} 项冲突未裁决，无法应用</span>
              {/if}
            </div>
          {/if}
        {/if}
      </div>
    {/if}
  </div>
</section>

