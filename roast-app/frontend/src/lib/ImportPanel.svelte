<script>
  // Offline observation-package import panel.
  //
  // Privacy guarantee: the operator picks a LOCAL file; it is read with
  // FileReader and posted to the same-origin local API only. Nothing is ever
  // uploaded to a roaster or any external service.
  //
  // Flow: choose file -> 待核验 ledger row -> preview (curve projection) ->
  // adjudicate conflicts (保留已有 / 采用包内) -> 一次性应用.
  import { createEventDispatcher } from 'svelte';
  import RoastChart from './RoastChart.svelte';
  import {
    listImports,
    receiveImport,
    previewImport,
    resolveImport,
    applyImport,
    abortImport,
    importErrorText,
    fmtTime,
    EVENT_LABELS,
  } from './api.js';

  export let batches = [];
  export let windowS = 30;
  export let smoothS = 12;
  export let maxGapFillS = 45;

  const dispatch = createEventDispatcher();

  let fileInput;
  let targetBatchId = ''; // '' = create a new batch from the package
  let imports = [];
  let selectedId = null;
  let preview = null;
  let busy = '';
  let error = '';
  let info = '';
  let showFailed = false;

  $: visibleImports = showFailed
    ? imports
    : imports.filter((x) => x.status !== 'failed');
  $: selected = imports.find((x) => x.id === selectedId) || null;
  $: unresolved = preview
    ? preview.conflicts.filter((c) => c.resolution === null)
    : [];

  async function refreshList(selectId = selectedId) {
    imports = await listImports();
    if (selectId && imports.some((x) => x.id === selectId)) {
      selectedId = selectId;
      await loadPreview(selectId);
    } else {
      selectedId = null;
      preview = null;
    }
  }

  async function onPickFile() {
    error = '';
    info = '';
    const file = fileInput.files?.[0];
    if (!file) return;
    busy = `正在本地读取 ${file.name}（不会上传）…`;
    try {
      const text = await file.text();
      let pkg;
      try {
        pkg = JSON.parse(text);
      } catch {
        throw Object.assign(new Error('解析失败：文件不是合法 JSON（整包未接收）'), { status: 422 });
      }
      const res = await receiveImport(pkg, targetBatchId ? Number(targetBatchId) : null);
      info =
        `已接收包 ${res.package_id}：${res.n_samples} 个采样 / ${res.n_events} 个事件，` +
        (res.conflict_count
          ? `有 ${res.conflict_count} 处同刻读数冲突，保持待裁决。`
          : '无冲突，可直接应用。') +
        `（摘要 ${res.digest.algorithm} 校验${res.digest.ok ? '通过' : '异常'}）`;
      fileInput.value = '';
      await refreshList(res.id);
    } catch (e) {
      error = e.message || String(e);
      // ledger now contains the failed row — surface it for audit
      await refreshList().catch(() => {});
    } finally {
      busy = '';
    }
  }

  async function loadPreview(id) {
    busy = '生成预览投影（不写入）…';
    error = '';
    try {
      preview = await previewImport(id, {
        window_s: windowS,
        display_smooth_s: smoothS,
        max_gap_fill_s: maxGapFillS,
      });
    } catch (e) {
      error = e.message;
      preview = null;
    } finally {
      busy = '';
    }
  }

  async function choose(c, resolution) {
    error = '';
    try {
      await resolveImport(selectedId, { [c.id]: resolution });
      await loadPreview(selectedId);
    } catch (e) {
      error = e.message;
    }
  }

  async function decideAll(resolution) {
    error = '';
    const pending = preview.conflicts.filter((c) => c.resolution === null);
    if (!pending.length) return;
    const map = {};
    pending.forEach((c) => (map[c.id] = resolution));
    await resolveImport(selectedId, map);
    await loadPreview(selectedId);
  }

  async function apply() {
    error = '';
    busy = '一次性应用（单事务）…';
    try {
      const res = await applyImport(selectedId);
      info =
        `包 ${res.package_id} 已应用到批次 #${res.batch_id}（${res.batch_name}）：` +
        `新增采样 ${res.samples.added}、采用包内更新 ${res.samples.updated}、同值跳过 ${res.samples.identical_skipped}` +
        (res.samples.missing_kept + res.samples.existing_kept
          ? `、裁决保留 ${res.samples.missing_kept + res.samples.existing_kept}`
          : '') +
        `；新增事件 ${res.events.added}、取代旧事件 ${res.events.superseded}、事件重复跳过 ${res.events.duplicates}。`;
      await refreshList(selectedId);
      dispatch('applied', { batchId: res.batch_id });
    } catch (e) {
      error = e.message;
      await refreshList(selectedId).catch(() => {});
    } finally {
      busy = '';
    }
  }

  async function retryApplyAlreadyApplied() {
    // Idempotency demonstration: re-applying returns the original result.
    const res = await applyImport(selectedId);
    info = `重复投递/重试返回原导入结果：批次 #${res.batch_id}，未新增任何采样或事件。`;
  }

  async function abort() {
    error = '';
    if (!window.confirm('放弃该待核验包？不会对曲线做任何修改（账本保留记录）。')) return;
    await abortImport(selectedId);
    info = '已放弃该导入；曲线与分析保持不变。';
    await refreshList();
    dispatch('changed');
  }

  function statusLabel(s) {
    return {
      pending_review: '待核验',
      applied: '已应用',
      failed: '整包失败',
      aborted: '已放弃',
    }[s] || s;
  }

  // preview chart payload (reuse the existing chart without modification)
  $: chartPayloads =
    preview && preview.projection
      ? [
          {
            batch: preview.batch && preview.batch.id ? preview.batch : { name: preview.batch?.name || '（新批次）' },
            series: preview.projection.series,
            events: preview.projection.events.filter((e) => !e.superseded),
            metrics: preview.projection.metrics,
          },
        ]
      : [];
</script>

<section class="panel">
  <h2>③ 离线观察包导入（只读本地文件，绝不上传）</h2>
  <div class="muted" style="font-size:12px;margin-bottom:8px">
    流程：本地选文件 → 进入<b>待核验</b>（先不写曲线）→ 预览投影 → 解决同刻读数冲突 →
    <b>一次性应用</b>。文件只在浏览器本地读取并提交给同源的本机 API，不连接真实烘焙机、不发往任何外部服务。
  </div>

  <div class="row" style="align-items:flex-end;gap:10px">
    <div>
      <div class="muted">合并目标</div>
      <select bind:value={targetBatchId}>
        <option value="">（创建为新批次，取包内批次信息）</option>
        {#each batches as b}
          <option value={b.id}>并入 #{b.id} · {b.name}</option>
        {/each}
      </select>
    </div>
    <div>
      <div class="muted">观察包文件（JSON，本地）</div>
      <input bind:this={fileInput} type="file" accept=".json,application/json" on:change={onPickFile} />
    </div>
    <button class="ghost" on:click={() => refreshList()}>刷新账本</button>
    <label class="inline" style="align-self:center">
      <input type="checkbox" bind:checked={showFailed} on:change={() => refreshList()} />
      显示失败记录
    </label>
  </div>

  {#if busy}<div class="muted" style="margin-top:6px">{busy}</div>{/if}
  {#if info}<div class="tag" style="display:inline-block;margin-top:6px;padding:4px 8px">{info}</div>{/if}
  {#if error}
    <div class="warn" style="margin-top:8px;white-space:pre-wrap">⚠ {error}</div>
  {/if}

  <div class="row" style="margin-top:10px;gap:12px;align-items:flex-start">
    <!-- ledger -->
    <div style="flex:0 0 320px;min-width:280px">
      <table>
        <tr><th>包标识</th><th>状态</th><th>采样/事件</th><th>冲突</th></tr>
        {#each visibleImports as im}
          <tr
            style="cursor:pointer;{im.id === selectedId ? 'background:#3a322b' : ''}"
            on:click={() => (selectedId = im.id, loadPreview(im.id))}
            role="button"
          >
            <td style="font-size:11px">{im.package_id}</td>
            <td>
              <span class="tag {im.status}">{statusLabel(im.status)}</span>
            </td>
            <td>{im.n_samples}/{im.n_events}</td>
            <td>{im.unresolved_count ?? 0}{(im.conflict_count ?? 0) ? `/${im.conflict_count}` : ''}</td>
          </tr>
        {/each}
        {#if visibleImports.length === 0}
          <tr><td colspan="4" class="muted">暂无观察包</td></tr>
        {/if}
      </table>
    </div>

    <!-- review pane -->
    {#if preview}
      <div style="flex:1;min-width:360px">
        <div class="row" style="gap:8px;align-items:center">
          <b>{preview.package_id}</b>
          <span class="tag {preview.status}">{statusLabel(preview.status)}</span>
          <span class="muted" style="font-size:11px">
            生成于 {preview.generated_at} · 格式 v{preview.format_version} ·
            摘要 {preview.digest.ok ? '✓ 一致' : '✗ 不一致'}
          </span>
        </div>

        {#if preview.status === 'failed' || preview.status === 'aborted'}
          <div class="warn" style="margin-top:8px">
            {#if preview.status === 'failed'}
              整包失败（{preview.error_code}），未写入任何采样或事件。审计明细：
              <ul style="margin:6px 0 0;padding-left:18px">
                {#each preview.findings || [] as f}
                  <li>[{f.code}] {f.message}</li>
                {/each}
              </ul>
            {:else}
              该包已放弃；曲线与分析未发生变化（账本保留记录）。
            {/if}
          </div>
        {:else}
          <!-- conflicts -->
          {#if preview.conflicts.length}
            <h3 style="margin:10px 0 4px">同刻读数冲突 · 待裁决 {unresolved.length} 处</h3>
            <table>
              <tr>
                <th>时刻</th><th>通道</th><th>已有读数</th><th>包内读数</th><th>裁决</th>
              </tr>
              {#each preview.conflicts as c}
                <tr>
                  <td>{fmtTime(c.t_s)} ({c.t_s}s)</td>
                  <td>{c.channel === 'bean' ? '豆温' : '环温'}</td>
                  <td>{c.existing_value ?? '缺测'}</td>
                  <td>{c.incoming_value ?? '缺测'}</td>
                  <td>
                    {#if c.resolution === null}
                      <button on:click={() => choose(c, 'keep_existing')}>保留已有</button>
                      <button class="ghost" on:click={() => choose(c, 'use_incoming')}>采用包内</button>
                    {:else}
                      <span class="tag {c.resolution === 'use_incoming' ? 'manual' : 'auto'}">
                        {c.resolution === 'use_incoming' ? '采用包内' : '保留已有'}
                      </span>
                      <button class="ghost" on:click={() => choose(c, 'keep_existing')}>改留已有</button>
                      <button class="ghost" on:click={() => choose(c, 'use_incoming')}>改采用包内</button>
                    {/if}
                  </td>
                </tr>
              {/each}
            </table>
            {#if unresolved.length}
              <div class="row" style="margin-top:6px;gap:8px">
                <button class="ghost" on:click={() => decideAll('keep_existing')}>全部保留已有</button>
                <button class="ghost" on:click={() => decideAll('use_incoming')}>全部采用包内</button>
              </div>
            {/if}
          {/if}

          <!-- projection summary -->
          <h3 style="margin:10px 0 4px">预览投影（应用前，不写库）</h3>
          <div class="muted" style="font-size:12px">
            采样：新增 {preview.projection.sample_counts.added} ·
            采用包内 {preview.projection.sample_counts.updated} ·
            同值重复 {preview.projection.sample_counts.identical_skipped} ·
            冲突 {preview.projection.sample_counts.conflicted}
            ｜ 事件：新增 {preview.projection.event_counts.added} ·
            重复跳过 {preview.projection.event_counts.duplicate} ·
            将取代旧事件 {preview.projection.event_counts.supersedes}
          </div>
          {#if preview.current}
            <div class="muted" style="font-size:12px;margin-top:2px">
              未裁决前当前分析保持不变（当前 {preview.current.n_samples} 点）；
              预览仅显示应用后的目标形态。
            </div>
          {/if}

          <div style="margin-top:6px">
            <RoastChart {chartPayloads} {windowS} {smoothS} />
          </div>

          <!-- actions -->
          <div class="row" style="margin-top:8px;gap:8px">
            {#if preview.status === 'pending_review'}
              <button disabled={unresolved.length > 0} on:click={apply}>
                {unresolved.length ? `仍有 ${unresolved.length} 处待裁决` : '一次性应用到曲线'}
              </button>
              <button class="ghost" on:click={abort}>放弃该包</button>
            {:else if preview.status === 'applied'}
              <span class="tag applied">已应用 · 批次 #{preview.batch_id}</span>
              <button class="ghost" on:click={retryApplyAlreadyApplied}>
                重复投递重试（返回原结果，不新增）
              </button>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</section>
