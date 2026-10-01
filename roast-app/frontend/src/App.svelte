<script>
  import { onMount } from 'svelte';
  import RoastChart from './lib/RoastChart.svelte';
  import {
    getBatches,
    seed,
    getSeries,
    getCompare,
    addEvent,
    listEvents,
    exportBatch,
    recompute,
    EVENT_LABELS,
    fmtTime,
  } from './lib/api.js';

  let batches = [];
  let view = 'single'; // single | compare
  let selA = null;
  let selB = null;
  let dataA = null;
  let dataB = null;
  let comparePayload = null;
  let eventHistory = [];
  let loading = '';
  let error = '';

  // Analysis parameters — affect DERIVED traces only, never stored samples.
  let windowS = 30;
  let smoothS = 12;
  let maxGapFillS = 45;

  // event correction form
  let newEventType = 'turning_point';
  let newEventTime = '1:00';
  let newEventDamper = '';
  let showHistory = false;

  // export verification
  let verifyResult = null;

  const phaseKeys = [
    ['drying_s', '脱水期', '下豆 → 回温点'],
    ['maillard_s', '梅纳/反应期', '回温点 → 一爆开始'],
    ['development_s', '发展期', '一爆开始 → 出锅'],
    ['first_crack_window_s', '一爆持续', '一爆开始 → 一爆结束'],
    ['total_s', '总时长', '下豆 → 出锅'],
  ];

  onMount(loadBatches);

  async function loadBatches() {
    error = '';
    try {
      batches = await getBatches();
      if (batches.length) {
        selA = batches[0].id;
        selB = batches[batches.length - 1].id;
        await refresh();
      }
    } catch (e) {
      error = `无法连接后端：${e.message}`;
    }
  }

  async function doSeed() {
    loading = '正在生成合成批次…';
    error = '';
    try {
      await seed();
      await loadBatches();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function refresh() {
    if (!selA) return;
    loading = '加载曲线…';
    error = '';
    verifyResult = null;
    const params = {
      window_s: windowS,
      display_smooth_s: smoothS,
      max_gap_fill_s: maxGapFillS,
    };
    try {
      dataA = await getSeries(selA, { ...params, include_history: showHistory });
      eventHistory = await listEvents(selA, showHistory);
      if (view === 'compare' && selB && selB !== selA) {
        comparePayload = await getCompare(selA, selB, params);
        dataB = comparePayload.batches[1];
      } else {
        comparePayload = null;
        dataB = null;
      }
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function parseMMSS(str) {
    const m = /^(\d+):([0-5]?\d)$/.exec(str.trim());
    if (!m) return null;
    return Number(m[1]) * 60 + Number(m[2]);
  }

  async function submitEvent() {
    const t = parseMMSS(newEventTime);
    if (t === null) {
      error = '时间格式应为 m:ss，例如 1:05';
      return;
    }
    const body = {
      event_type: newEventType,
      t_s: t,
      source: 'manual',
      created_by: '操作员(界面)',
      label: `${EVENT_LABELS[newEventType] || newEventType} 人工修正`,
    };
    if (newEventType === 'damper_change') {
      const v = Number(newEventDamper);
      if (!Number.isFinite(v)) {
        error = '风门变化需要填写新风门开度 (%)';
        return;
      }
      body.value_num = v;
      body.label = `风门 → ${v}% 人工标记`;
    }
    loading = '保存修正…';
    error = '';
    try {
      await addEvent(selA, body);
      await refresh();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function verifyExport() {
    loading = '导出并重算校验…';
    error = '';
    verifyResult = null;
    try {
      const ex = await exportBatch(selA, {
        window_s: windowS,
        display_smooth_s: smoothS,
      });
      const rc = await recompute({
        samples: ex.series.raw_points.map((p) => ({
          t_s: p.t_s,
          bean_temp_c: p.bean_temp_c,
          env_temp_c: p.env_temp_c,
        })),
        events: ex.events,
        params: ex.params,
      });
      const keys = Object.keys(ex.metrics).filter(
        (k) => k.endsWith('_s') || k === 'development_ratio'
      );
      const rows = keys.map((k) => ({
        key: k,
        exported: ex.metrics[k],
        recomputed: rc.metrics[k],
        match: ex.metrics[k] === rc.metrics[k],
      }));
      // Changing window/smoothing must leave every stored sample untouched.
      const alt = await getSeries(selA, {
        window_s: windowS * 2,
        display_smooth_s: smoothS === 0 ? 30 : 0,
        max_gap_fill_s: maxGapFillS,
      });
      const sig = (arr) =>
        JSON.stringify(arr.map((p) => [p.t_s, p.bean_temp_c, p.env_temp_c]));
      const rawSame = sig(ex.series.raw_points) === sig(alt.series.raw_points);
      verifyResult = { rows, rawSame, exportObj: ex };
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function downloadExport() {
    if (!verifyResult?.exportObj) return;
    const blob = new Blob([JSON.stringify(verifyResult.exportObj, null, 2)], {
      type: 'application/json',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${verifyResult.exportObj.batch.name}-export.json`;
    a.click();
  }

  $: chartPayloads =
    view === 'compare' && comparePayload
      ? comparePayload.batches
      : dataA
        ? [dataA]
        : [];

  let refreshTimer;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }
</script>

<header style="padding:14px 20px;border-bottom:1px solid var(--line)">
  <h1>咖啡烘焙批次曲线 · 过程记录对比</h1>
  <div class="muted" style="margin-top:2px">
    豆温 / 环境温度 / 操作事件的过程视角 · 合成数据离线运行，<b>未连接真实烘焙机</b>
  </div>
</header>

<main style="padding:16px 20px;display:flex;flex-direction:column;gap:14px">
  {#if error}
    <div class="warn">⚠ {error}</div>
  {/if}

  <section class="panel">
    <div class="row" style="align-items:flex-end">
      <div>
        <div class="muted">数据</div>
        {#if batches.length === 0}
          <button on:click={doSeed}>① 生成两个合成批次（含噪声/不均采样/探针缺测）</button>
        {:else}
          <button class="ghost" on:click={doSeed}>重新生成合成批次</button>
        {/if}
      </div>
      <div>
        <div class="muted">视图</div>
        <label class="inline">
          <input type="radio" bind:group={view} value="single" on:change={refresh} />单批次
        </label>
        <label class="inline">
          <input type="radio" bind:group={view} value="compare" on:change={refresh} />双批次对比
        </label>
      </div>
      <div>
        <div class="muted">批次 A</div>
        <select bind:value={selA} on:change={refresh}>
          {#each batches as b}
            <option value={b.id}>{b.name} · {b.bean}</option>
          {/each}
        </select>
      </div>
      {#if view === 'compare'}
        <div>
          <div class="muted">批次 B</div>
          <select bind:value={selB} on:change={refresh}>
            {#each batches as b}
              <option value={b.id}>{b.name} · {b.bean}</option>
            {/each}
          </select>
        </div>
      {/if}
    </div>

    <div class="row" style="margin-top:12px;align-items:flex-end">
      <label class="inline">
        RoR 回归窗口
        <input
          type="number"
          min="5"
          max="300"
          step="5"
          bind:value={windowS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        显示平滑（仅 RoR 曲线）
        <input
          type="number"
          min="0"
          max="180"
          step="3"
          bind:value={smoothS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      <label class="inline">
        最大插值桥接
        <input
          type="number"
          min="5"
          max="600"
          step="5"
          bind:value={maxGapFillS}
          on:input={scheduleRefresh}
          style="width:70px"
        />
        s
      </label>
      {#if loading}<span class="muted">{loading}</span>{/if}
    </div>
    <div class="muted" style="margin-top:6px;font-size:12px">
      RoR 口径：在每个实测时刻，对居中 ±{(windowS / 2).toFixed(0)}s 时间窗内的<b>实测</b>豆温点做最小二乘直线拟合取斜率（°C/min），
      至少 4 个点且跨度 ≥10s 才出值；插值点不参与拟合，缺测宽缺口处 RoR 断档。调整窗口/平滑<b>只改变派生曲线，不改原始温度</b>。
    </div>
  </section>

  {#if dataA}
    <section class="panel">
      <RoastChart {chartPayloads} {windowS} {smoothS} />
      <div class="row" style="margin-top:6px;font-size:12px">
        <span class="tag">圆点＝实测豆温</span>
        <span class="tag">虚线菱形＝线性插值（非实测）</span>
        <span class="tag">细点线＝环境温度</span>
        <span class="tag">金色竖虚线＝风门变化</span>
        <span class="tag">曲线断档＝缺测未桥接</span>
      </div>
      {#if comparePayload}
        <div class="warn" style="margin-top:8px">{comparePayload.interpretation}</div>
      {/if}
    </section>

    <section class="row">
      <div class="panel col">
        <h2>阶段指标（明确区间）</h2>
        <div class="row" style="gap:8px">
          {#each view === 'compare' && dataB ? [dataA, dataB] : [dataA] as pl, i}
            <div style="flex:1;min-width:260px">
              <div class="muted" style="margin-bottom:4px">
                {i === 0 ? 'A' : 'B'} · {pl.batch.name}
              </div>
              <table>
                <tr><th>阶段</th><th>区间定义</th><th>时长</th><th>来源</th></tr>
                {#each phaseKeys as [key, label, def]}
                  <tr>
                    <td>{label}</td>
                    <td class="muted" style="font-size:11px">{def}</td>
                    <td>{fmtTime(pl.metrics[key])}</td>
                    <td style="font-size:11px">
                      {#if key === 'drying_s'}
                        <span class="tag {pl.metrics.anchors.turning_point?.source}">
                          {pl.metrics.anchors.turning_point?.source || '—'}
                        </span>
                      {:else if key === 'maillard_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {:else if key === 'development_s' || key === 'first_crack_window_s'}
                        <span class="tag {pl.metrics.anchors.first_crack_start?.source}">
                          FC {pl.metrics.anchors.first_crack_start?.source || '—'}
                        </span>
                      {/if}
                    </td>
                  </tr>
                {/each}
                <tr>
                  <td><b>发展时间比 DTR</b></td>
                  <td class="muted" style="font-size:11px">发展期 / 总时长</td>
                  <td>
                    <b>
                      {pl.metrics.development_ratio !== null
                        ? (pl.metrics.development_ratio * 100).toFixed(1) + '%'
                        : '—'}
                    </b>
                  </td>
                  <td></td>
                </tr>
              </table>
            </div>
          {/each}
        </div>
      </div>

      <div class="panel col">
        <h2>人工修正事件（批次 A）· 保留来源</h2>
        <div class="row" style="gap:8px;align-items:flex-end">
          <div>
            <div class="muted">类型</div>
            <select bind:value={newEventType}>
              {#each Object.entries(EVENT_LABELS) as [k, v]}
                {#if k !== 'charge'}<option value={k}>{v}</option>{/if}
              {/each}
            </select>
          </div>
          <div>
            <div class="muted">时间 m:ss</div>
            <input bind:value={newEventTime} placeholder="1:05" style="width:80px" />
          </div>
          {#if newEventType === 'damper_change'}
            <div>
              <div class="muted">新风门 %</div>
              <input bind:value={newEventDamper} type="number" min="0" max="100" style="width:80px" />
            </div>
          {/if}
          <button on:click={submitEvent}>提交修正</button>
          <label class="inline" style="align-self:center">
            <input type="checkbox" bind:checked={showHistory} on:change={refresh} />
            显示已被取代的旧值
          </label>
        </div>

        <table style="margin-top:10px">
          <tr><th>事件</th><th>时间</th><th>来源</th><th>备注</th><th>状态</th></tr>
          {#each eventHistory as e}
            <tr style={e.superseded ? 'opacity:.45' : ''}>
              <td>
                {EVENT_LABELS[e.event_type] || e.event_type}
                {e.value_num !== null && e.value_num !== undefined ? ` → ${e.value_num}%` : ''}
              </td>
              <td>{fmtTime(e.t_s)}</td>
              <td>
                <span class="tag {e.source}">{e.source === 'manual' ? '人工' : '自动建议'}</span>
                {e.created_by}
              </td>
              <td class="muted" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis">
                {e.label}
              </td>
              <td>{e.superseded ? '已被修正取代（保留）' : '当前'}</td>
            </tr>
          {/each}
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>缺测与插值审计 · 导出可复现</h2>
      <div class="row">
        <div style="flex:1;min-width:280px">
          <table>
            <tr><th>通道</th><th>起(s)</th><th>止(s)</th><th>缺测点</th><th>处理</th></tr>
            {#each dataA.series.missing_segments as g}
              <tr>
                <td>{g.channel === 'bean' ? '豆温' : '环境'}</td>
                <td>{g.t_start_s.toFixed(1)}</td>
                <td>{g.t_end_s.toFixed(1)}</td>
                <td>{g.n_missing}</td>
                <td>
                  {#if g.status === 'interpolated'}
                    <span style="color:#f3c98b">线性插值并标记（非实测）</span>
                  {:else if g.status === 'wide_unfilled'}
                    <span style="color:#e35d5d">缺口超 {maxGapFillS}s，不桥接（曲线断档）</span>
                  {:else}
                    端点缺测，不填充
                  {/if}
                </td>
              </tr>
            {/each}
          </table>
          <div class="muted" style="font-size:12px;margin-top:6px">
            实测豆温 {dataA.series.raw_points.filter((p) => p.bean_temp_c !== null).length} /
            总点 {dataA.series.raw_points.length}；
            插值点 {dataA.series.interpolated_t_s.length} 个，仅用于引导线，不写回原始采样表。
          </div>
        </div>
        <div style="flex:1;min-width:280px">
          <button on:click={verifyExport}>
            ② 导出 JSON 并用 /api/recompute 重算全部阶段指标
          </button>
          {#if verifyResult}
            <table style="margin-top:10px">
              <tr><th>指标</th><th>导出值</th><th>独立重算</th><th>一致</th></tr>
              {#each verifyResult.rows as r}
                <tr>
                  <td>{r.key}</td>
                  <td>{r.exported ?? '—'}</td>
                  <td>{r.recomputed ?? '—'}</td>
                  <td>{r.match ? '✅' : '❌'}</td>
                </tr>
              {/each}
            </table>
            <div style="margin-top:8px">
              <span class="{verifyResult.rawSame ? '' : 'warn'}">
                改变窗口/平滑后原始豆温/环温逐点比对：
                {verifyResult.rawSame ? '✅ 完全不变' : '❌ 被修改'}
              </span>
              <button class="ghost" style="margin-left:10px" on:click={downloadExport}>
                下载导出 JSON
              </button>
            </div>
          {/if}
        </div>
      </div>
    </section>
  {/if}
</main>
