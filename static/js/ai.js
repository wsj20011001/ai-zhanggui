/* AI 掌柜 · 前端公共脚本（无框架，只依赖 ECharts） */
const AI = (() => {
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  const COLORS = ['#4c4ddc', '#7f77dd', '#1d9e75', '#ef9f27', '#d4537e', '#5f9bd6'];

  async function post(url, payload = {}) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
      body: JSON.stringify(payload || {}),
    });
    return await r.json();
  }

  function toast(msg, type = 'ok') {
    const el = document.createElement('div');
    el.className = 'ai-toast ' + type;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.classList.add('show'), 10);
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 250); }, 3600);
  }

  function base(title) {
    return {
      color: COLORS,
      title: title ? { text: title, left: 0, top: 0, textStyle: { fontSize: 13, fontWeight: 600, color: '#1b1c33' } } : undefined,
      grid: { left: 8, right: 16, top: title ? 34 : 14, bottom: 6, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#fff', borderColor: '#e3e5f2', textStyle: { color: '#2b2f4a', fontSize: 12 } },
      textStyle: { fontFamily: 'system-ui, -apple-system, "Microsoft YaHei", sans-serif' },
    };
  }

  const axis = {
    axisLine: { lineStyle: { color: '#e3e5f2' } },
    axisTick: { show: false },
    axisLabel: { color: '#8a92a2', fontSize: 11 },
    splitLine: { lineStyle: { color: '#f2f3f9' } },
  };

  function line(el, labels, values, opts = {}) {
    if (!el) return null;
    const c = echarts.init(el);
    c.setOption({
      ...base(opts.title),
      xAxis: { type: 'category', data: labels, boundaryGap: false, ...axis, splitLine: { show: false } },
      yAxis: { type: 'value', ...axis },
      series: [{
        type: 'line', data: values, smooth: true, symbol: 'none',
        lineStyle: { width: 2.2, color: opts.color || '#4c4ddc' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: (opts.color || '#4c4ddc') + '33' },
            { offset: 1, color: (opts.color || '#4c4ddc') + '00' }]),
        },
      }],
    });
    window.addEventListener('resize', () => c.resize());
    return c;
  }

  function bar(el, labels, values, opts = {}) {
    if (!el) return null;
    const c = echarts.init(el);
    c.setOption({
      ...base(opts.title),
      grid: { left: 8, right: 16, top: opts.title ? 34 : 14, bottom: 6, containLabel: true },
      xAxis: opts.horizontal
        ? { type: 'value', ...axis }
        : { type: 'category', data: labels, ...axis, splitLine: { show: false },
            axisLabel: { color: '#8a92a2', fontSize: 11, interval: 0, rotate: opts.rotate || 0,
                         formatter: (v) => v.length > 7 ? v.slice(0, 7) + '…' : v } },
      yAxis: opts.horizontal
        ? { type: 'category', data: labels, ...axis, splitLine: { show: false },
            axisLabel: { color: '#6b7482', fontSize: 11 } }
        : { type: 'value', ...axis },
      series: [{
        type: 'bar', data: values, barMaxWidth: 22,
        itemStyle: { borderRadius: opts.horizontal ? [0, 5, 5, 0] : [5, 5, 0, 0],
                     color: opts.color || '#4c4ddc' },
      }],
    });
    window.addEventListener('resize', () => c.resize());
    return c;
  }

  function pie(el, data, opts = {}) {
    if (!el) return null;
    const c = echarts.init(el);
    c.setOption({
      color: COLORS,
      tooltip: { trigger: 'item', backgroundColor: '#fff', borderColor: '#e3e5f2',
                 textStyle: { color: '#2b2f4a', fontSize: 12 } },
      legend: { bottom: 0, icon: 'circle', itemWidth: 8, itemHeight: 8,
                textStyle: { fontSize: 11, color: '#6b7482' } },
      series: [{
        type: 'pie', radius: ['46%', '68%'], center: ['50%', '44%'],
        avoidLabelOverlap: true, label: { show: false },
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        data: data,
      }],
      textStyle: { fontFamily: 'system-ui, -apple-system, "Microsoft YaHei", sans-serif' },
    });
    window.addEventListener('resize', () => c.resize());
    return c;
  }

  function scatterQuadrant(el, items, medQty, medMargin) {
    if (!el) return null;
    const c = echarts.init(el);
    const map = { '明星': '#1d9e75', '金牛': '#4c4ddc', '潜力': '#ef9f27', '瘦狗': '#d64545' };
    const groups = {};
    items.forEach(i => {
      (groups[i.quadrant] = groups[i.quadrant] || []).push([i.qty, +(i.margin_rate * 100).toFixed(1), i.name, i.amount]);
    });
    c.setOption({
      color: Object.values(map),
      grid: { left: 8, right: 20, top: 34, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { fontSize: 11 } },
      tooltip: {
        backgroundColor: '#fff', borderColor: '#e3e5f2', textStyle: { color: '#2b2f4a', fontSize: 12 },
        formatter: p => `<b>${p.data[2]}</b><br/>销量 ${p.data[0]} 份 · 毛利率 ${p.data[1]}%<br/>销售额 ¥${p.data[3]}`,
      },
      xAxis: { type: 'value', name: '销量（份）', nameTextStyle: { color: '#8a92a2', fontSize: 11 }, ...axis },
      yAxis: { type: 'value', name: '毛利率 %', nameTextStyle: { color: '#8a92a2', fontSize: 11 }, ...axis },
      series: Object.keys(map).filter(k => groups[k]).map(k => ({
        name: k, type: 'scatter', data: groups[k], symbolSize: 9,
        itemStyle: { color: map[k], opacity: .82 },
        markLine: k === '明星' ? {
          silent: true, symbol: 'none',
          lineStyle: { color: '#c9cde0', type: 'dashed' },
          data: [{ xAxis: medQty }, { yAxis: +(medMargin * 100).toFixed(1) }],
          label: { show: false },
        } : undefined,
      })),
      textStyle: { fontFamily: 'system-ui, -apple-system, "Microsoft YaHei", sans-serif' },
    });
    window.addEventListener('resize', () => c.resize());
    return c;
  }

  return { post, toast, line, bar, pie, scatterQuadrant };
})();
