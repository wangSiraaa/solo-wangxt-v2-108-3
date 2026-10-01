<script>
  // ECharts rendering for one or two roast batches.
  //
  // Visual honesty rules encoded here:
  //  - measured points are dots; the continuous guide line is dashed across
  //    interpolated samples and BROKEN across gaps wider than maxGapFillS;
  //  - interpolated segments use a different symbol/colour and a legend entry;
  //  - RoR is drawn on its own axis and the window basis is shown in the title;
  //  - events are markLines; damper changes get a distinct dashed gold line;
  //  - nothing on the chart claims the damper change caused the shape.
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import * as echarts from 'echarts';

  export let payloads = []; // [{batch, series, events, metrics}]
  export let windowS = 30;
  export let smoothS = 12;

  const dispatch = createEventDispatcher();
  let el;
  let chart;

  const PALETTE = ['#e07a3f', '#4aa3df'];
  const EVENT_STYLE = {
    charge: { color: '#9a9a9a' },
    turning_point: { color: '#5fd08a' },
    first_crack_start: { color: '#e3d04a' },
    first_crack_end: { color: '#b5a93c' },
    drop: { color: '#e35d5d' },
    damper_change: { color: '#d4af37' },
  };

  function pair(arr, key) {
    return arr.map((p) => [p.t_s, p[key]]);
  }

  // Build guide line split into runs so interpolated vs measured sections
  // get distinct styling; nulls already encode unfilled-gap breaks.
  function guideRuns(points, key, isInterpRun) {
    const runs = [];
    let cur = [];
    let curInterp = null;
    for (let i = 0; i < points.length; i++) {
      const v = points[i][key];
      const interp = points[i].is_interpolated;
      if (v === null || v === undefined) {
        if (cur.length) runs.push({ interp: curInterp, data: cur });
        cur = [];
        curInterp = null;
        continue;
      }
      if (curInterp === null) curInterp = interp;
      if (interp !== curInterp) {
        runs.push({ interp: curInterp, data: cur });
        cur = [];
        curInterp = interp;
      }
      cur.push([points[i].t_s, v]);
    }
    if (cur.length) runs.push({ interp: curInterp, data: cur });
    return runs.filter((r) => r.interp === isInterpRun);
  }

  function buildOption() {
    const series = [];
    const legends = ['豆温(实测)', '环境温度', '温升率 RoR'];

    payloads.forEach((pl, bi) => {
      const c = PALETTE[bi % PALETTE.length];
      const pts = pl.series.raw_points;
      const measured = pts
        .filter((p) => !p.is_interpolated && p.bean_temp_c !== null)
        .map((p) => [p.t_s, p.bean_temp_c]);

      // measured guide runs (solid) and interpolated runs (dashed hollow)
      guideRuns(pts, 'bean_temp_c', false).forEach((run, ri) => {
        series.push({
          name: ri === 0 && bi === 0 ? '豆温(实测)' : `豆温 ${pl.batch.name}`,
          type: 'line',
          data: run.data,
          showSymbol: false,
          lineStyle: { width: 2, color: c },
          xAxisIndex: 0,
          yAxisIndex: 0,
          z: 3,
          tooltip: { show: false },
          legendHoverLink: false,
        });
      });

      const scatterSpec = {
        name: '豆温实测点',
        type: 'scatter',
        data: measured,
        symbolSize: 3,
        itemStyle: { color: c },
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 4,
      };
      series.push(scatterSpec);

      guideRuns(pts, 'bean_temp_c', true).forEach((run) => {
        series.push({
          name: '插值段(非实测)',
          type: 'line',
          data: run.data,
          showSymbol: true,
          symbol: 'diamond',
          symbolSize: 6,
          lineStyle: { width: 1.5, color: c, type: 'dashed' },
          itemStyle: { color: 'transparent', borderColor: c, borderWidth: 1.5 },
          xAxisIndex: 0,
          yAxisIndex: 0,
          z: 3,
        });
      });

      // env temp (thin, muted) on its own temperature axis scale but same axis
      series.push({
        name: bi === 0 ? '环境温度' : `环境温度 ${pl.batch.name}`,
        type: 'line',
        showSymbol: false,
        smooth: false,
        data: pair(pts, 'env_temp_c'),
        lineStyle: { width: 1, color: c, opacity: 0.45, type: 'dotted' },
        xAxisIndex: 0,
        yAxisIndex: 0,
        z: 2,
      });

      // RoR — display trace (window + smoothing stated in UI text)
      series.push({
        name: bi === 0 ? '温升率 RoR' : `RoR ${pl.batch.name}`,
        type: 'line',
        showSymbol: false,
        data: pair(pts, 'ror_display'),
        connectNulls: false,
        lineStyle: { width: 1.6, color: c, type: 'solid' },
        xAxisIndex: 0,
        yAxisIndex: 1,
        z: 1,
      });

      // events as markLines on first bean series — attach to measured scatter
      const markLines = pl.events
        .filter((e) => e.event_type !== 'damper_change')
        .map((e) => {
          const st = EVENT_STYLE[e.event_type] || { color: '#888' };
          return {
            xAxis: e.t_s,
            lineStyle: { color: st.color, type: 'solid', width: 1, opacity: 0.75 },
            label: {
              formatter: `${eventShort(e.event_type)}${e.source === 'manual' ? ' ✎' : ''}`,
              color: st.color,
              fontSize: 10,
              position: 'insideEndTop',
            },
          };
        });
      const damper = pl.events.filter((e) => e.event_type === 'damper_change');
      markLines.push(
        ...damper.map((e) => ({
          xAxis: e.t_s,
          lineStyle: { color: '#d4af37', type: 'dashed', width: 2 },
          label: {
            formatter: `风门${e.value_num !== null && e.value_num !== undefined ? ' ' + e.value_num + '%' : ''}`,
            color: '#d4af37',
            fontSize: 10,
            position: 'insideEndBottom',
          },
        }))
      );
      const target = scatterSpec;
      target.markLine = {
        silent: false,
        symbol: 'none',
        data: markLines,
      };
    });

    return {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#2c2621',
        borderColor: '#3a322b',
        textStyle: { color: '#efe7dd' },
        valueFormatter: (v) => (v === null || v === undefined ? '缺测' : Number(v).toFixed(1)),
      },
      legend: {
        data: ['豆温实测点', '插值段(非实测)', '环境温度', '温升率 RoR'],
        textStyle: { color: '#a89b8c' },
        top: 0,
      },
      grid: { left: 56, right: 56, top: 36, bottom: 40 },
      xAxis: {
        type: 'value',
        name: '自下豆起 (秒)',
        nameTextStyle: { color: '#a89b8c' },
        axisLabel: {
          color: '#a89b8c',
          formatter: (v) => `${Math.floor(v / 60)}:${String(v % 60).padStart(2, '0')}`,
        },
        splitLine: { lineStyle: { color: '#2a241f' } },
      },
      yAxis: [
        {
          type: 'value',
          name: '温度 °C',
          min: 80,
          max: 230,
          nameTextStyle: { color: '#a89b8c' },
          axisLabel: { color: '#a89b8c' },
          splitLine: { lineStyle: { color: '#2a241f' } },
        },
        {
          type: 'value',
          name: 'RoR °C/min',
          min: -20,
          max: 30,
          nameTextStyle: { color: '#a89b8c' },
          axisLabel: { color: '#a89b8c' },
          splitLine: { show: false },
        },
      ],
      series,
    };
  }

  function eventShort(t) {
    return {
      charge: '下豆',
      turning_point: '回温点',
      first_crack_start: '一爆',
      first_crack_end: '一爆末',
      drop: '出锅',
      custom: '标记',
    }[t] || t;
  }

  function render() {
    if (!chart || !payloads.length) return;
    chart.setOption(buildOption(), true);
  }

  onMount(() => {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    chart.on('click', (p) => dispatch('chartclick', p));
    render();
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
  });

  onDestroy(() => chart && chart.dispose());

  $: { payloads; windowS; smoothS; render(); }
</script>

<div bind:this={el} style="width:100%;height:420px"></div>
