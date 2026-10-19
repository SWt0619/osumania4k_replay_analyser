/*
 * charts.js —— 手绘统计图表（canvas 2D，零依赖、无 import/export）
 *
 * 用法：<script src="/static/js/charts.js"></script> 之后调用 window.Charts.xxx()
 *
 * 设计约定：
 *  1) 每个绘制函数都会重设 canvas 的物理像素尺寸（devicePixelRatio 适配），
 *     所以可以反复调用，也可以跟着窗口尺寸变化重绘。
 *  2) 背景默认不填色（透明），让页面的毛玻璃面板透出来；颜色先取 opts.theme，
 *     再取 CSS 变量，最后才退到内置色板。
 *  3) 空数据 / 全 0 不抛异常，只画一行居中的淡色提示文字。
 *  4) 字体一律用内联的 11px 系统字族，不引外部字体。
 *  5) 不用网格墙、不用粗黑坐标轴，只保留 1px 的网格色提示线。
 */
(function () {
  'use strict';

  // 浏览器里是 window，其它运行环境（比如 bun 单测）退到 globalThis
  var ROOT = (typeof window !== 'undefined') ? window
    : (typeof globalThis !== 'undefined') ? globalThis
      : this;

  var FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif';
  var TAU = Math.PI * 2;
  var isArr = Array.isArray || function (v) {
    return Object.prototype.toString.call(v) === '[object Array]';
  };

  // 内置色板（opts.theme 与 CSS 变量都缺省时用这套）
  var DEFAULT_THEME = {
    text: '#eef0ff',
    dim: '#a8aecb',
    grid: 'rgba(255,255,255,0.10)',
    accent: '#7c5cff',
    accent2: '#22d3ee',
    warn: '#ffd479',
    ability: '#ff9f6e',
    rhythm: '#6ee7ff',
    distraction: '#c792ff',
    miss: '#ff6b8a',
    bg: 'transparent'
  };

  // 准确率带的纵轴下限。这个游戏里低于 88% 的成绩基本没有分析价值（哪一段都错），
  // 从 0 起画等于把 88 个百分点压成一条贴底的直线。改成从 88 起、上限仍是 100，
  // 同样的带宽只覆盖 12 个百分点，斜率因此放大约 8.3 倍，几分甚至零点几分的起伏才看得出来。
  var ACC_FLOOR = 88;
  // 参考线：96% 是"已经打得不错"和"还能再稳一点"的分界，横一条线比对着刻度猜方便
  var ACC_REF = 96;

  // CSS 变量候选名。页面样式由别人写，变量名没法预设，按可能性从高到低找。
  // 前几个是本项目 app.css 里实际存在的名字（--stroke / --j-miss / --cat-*），
  // 后面是通用的兜底名。背景刻意只认 --chart-bg，避免把面板色当成图表底色、破坏毛玻璃。
  var CSS_VARS = {
    text: ['--text', '--fg', '--color-text', '--text-primary', '--ink'],
    dim: ['--dim', '--muted', '--text-dim', '--text-secondary', '--subtle'],
    grid: ['--grid', '--stroke', '--stroke-2', '--border', '--line'],
    accent: ['--accent', '--primary', '--brand'],
    accent2: ['--accent2', '--accent-2', '--secondary', '--info'],
    warn: ['--warn', '--warning', '--caution'],
    ability: ['--ability', '--cat-ability', '--c-ability'],
    rhythm: ['--rhythm', '--cat-rhythm', '--c-rhythm'],
    distraction: ['--distraction', '--cat-distraction', '--c-distraction'],
    slip: ['--slip', '--cat-slip', '--c-slip'],
    miss: ['--miss', '--j-miss', '--danger', '--error'],
    bg: ['--chart-bg']
  };

  // ==================== 通用小工具 ====================

  function num(v, d) {
    var n = (typeof v === 'number') ? v : parseFloat(v);
    return isFinite(n) ? n : d;
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : (v > hi ? hi : v);
  }

  function fmtTime(ms) {
    var total = Math.max(0, Math.round(num(ms, 0) / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return (m < 10 ? '0' + m : '' + m) + ':' + (s < 10 ? '0' + s : '' + s);
  }

  function fmt1(v) {
    return (Math.round(num(v, 0) * 10) / 10).toFixed(1);
  }

  // 误差标注，例如 +2.0ms / -1.5ms
  function fmtSignedMs(v) {
    var n = num(v, 0);
    if (n > 0) return '+' + n.toFixed(1) + 'ms';
    if (n < 0) return '-' + Math.abs(n).toFixed(1) + 'ms';
    return '0.0ms';
  }

  // 刻度用：-200ms / 0 / +200ms
  function fmtMsTick(v) {
    var n = Math.round(num(v, 0));
    if (n > 0) return '+' + n + 'ms';
    if (n < 0) return '-' + Math.abs(n) + 'ms';
    return '0';
  }

  // 失误率换算成百分数。后端字段既可能是 0..1 的分数（0.0833），
  // 也可能是已经乘过 100 的百分数；opts.rateUnit 可强制指定（'fraction' / 'percent'）。
  function ratePercent(v, unit) {
    var n = num(v, NaN);
    if (!isFinite(n)) return null;
    if (unit === 'percent') return n;
    if (unit === 'fraction') return n * 100;
    return n <= 1.0000001 ? n * 100 : n;
  }

  function setFont(ctx, px) {
    ctx.font = (num(px, 11) | 0) + 'px ' + FONT_STACK;
  }

  function measure(ctx, s) {
    var str = String(s);
    if (typeof ctx.measureText === 'function') {
      try {
        var m = ctx.measureText(str);
        var w = num(m && m.width, 0);
        if (w > 0) return w;
      } catch (e) { /* 桩环境里可能没有 measureText */ }
    }
    return str.length * 6;
  }

  function setDash(ctx, arr) {
    if (typeof ctx.setLineDash !== 'function') return;
    try { ctx.setLineDash(arr); } catch (e) { /* ignore */ }
  }

  // 生成竖向/横向线性渐变；环境不支持渐变时退回纯色
  function linGrad(ctx, x0, y0, x1, y1, c0, c1) {
    if (typeof ctx.createLinearGradient !== 'function') return c0;
    var g = null;
    try { g = ctx.createLinearGradient(x0, y0, x1, y1); } catch (e) { g = null; }
    if (!g || typeof g.addColorStop !== 'function') return c0;
    try {
      g.addColorStop(0, c0);
      g.addColorStop(1, c1);
    } catch (e) { return c0; }
    return g;
  }

  // 柱子的填充：accent→accent2 的渐变，让柱体有"从底往上变亮"的方向感，
  // 平涂会让密集的柱子糊成一块。
  function barFill(ctx, th, x0, y0, x1, y1, c0, c1) {
    return linGrad(ctx, x0, y0, x1, y1, c0, c1);
  }

  // 柱顶圆角（只有上面两个角是圆的）
  function roundTopPath(ctx, x, y, w, h, r) {
    var rr = Math.max(0, Math.min(r, w / 2, h));
    ctx.beginPath();
    if (rr <= 0.2) {
      ctx.rect(x, y, w, h);
      return;
    }
    ctx.moveTo(x, y + h);
    ctx.lineTo(x, y + rr);
    ctx.quadraticCurveTo(x, y, x + rr, y);
    ctx.lineTo(x + w - rr, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
    ctx.lineTo(x + w, y + h);
    ctx.closePath();
  }

  // 柱右端圆角（横向柱状图用）
  function roundRightPath(ctx, x, y, w, h, r) {
    var rr = Math.max(0, Math.min(r, h / 2, w));
    ctx.beginPath();
    if (rr <= 0.2) {
      ctx.rect(x, y, w, h);
      return;
    }
    ctx.moveTo(x, y);
    ctx.lineTo(x + w - rr, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
    ctx.lineTo(x + w, y + h - rr);
    ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
    ctx.lineTo(x, y + h);
    ctx.closePath();
  }

  function drawEmpty(ctx, w, h, th, msg) {
    ctx.save();
    ctx.globalAlpha = 0.8;
    setFont(ctx, 11);
    ctx.fillStyle = th.dim;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(msg || '暂无数据', w / 2, h / 2);
    ctx.restore();
  }

  // ==================== 主题解析 ====================

  function readCssVar(names) {
    if (typeof document === 'undefined' || !document || !document.documentElement) return null;
    var style = null;
    try {
      style = ROOT.getComputedStyle ? ROOT.getComputedStyle(document.documentElement) : null;
    } catch (e) { style = null; }
    if (!style || typeof style.getPropertyValue !== 'function') return null;
    for (var i = 0; i < names.length; i++) {
      var v = '';
      try { v = style.getPropertyValue(names[i]); } catch (e) { v = ''; }
      if (v && String(v).trim()) return String(v).trim();
    }
    return null;
  }

  function resolveTheme(opts) {
    var given = (opts && opts.theme) || {};
    var out = {};
    for (var key in DEFAULT_THEME) {
      if (!Object.prototype.hasOwnProperty.call(DEFAULT_THEME, key)) continue;
      var v = given[key];
      if (typeof v === 'string' && v) {
        out[key] = v;
        continue;
      }
      var css = readCssVar(CSS_VARS[key] || []);
      out[key] = css || DEFAULT_THEME[key];
    }
    return out;
  }

  // 准确率带的取值：优先 accV1（v1 加权准确率，200 记 2/3），老数据没有这个字段才退回 acc。
  // acc 只统计"严重失误"，吃一个 200 仍显示 100%——折线会平得像条直线，这也是它"不灵敏"的根源。
  function accOf(bucket) {
    if (!bucket) return NaN;
    if (bucket.accV1 !== undefined && bucket.accV1 !== null) return bucket.accV1;
    return bucket.acc;
  }

  // ==================== 画布准备（DPR 适配） ====================

  function logicalSize(canvas, opts, want, prop) {
    // 顺序：opts 指定 → CSS 尺寸 → 上一次用的逻辑尺寸 → 由物理尺寸反推
    var dpr = Math.max(1, num(ROOT.devicePixelRatio, 1));
    var v = num(opts && opts[want], 0);
    if (!v) v = num(canvas[want === 'width' ? 'clientWidth' : 'clientHeight'], 0);
    if (!v) v = num(canvas[prop], 0);
    if (!v) v = num(canvas[want], 0) / dpr;
    if (!v) v = want === 'width' ? 320 : 160;
    return Math.max(want === 'width' ? 40 : 30, Math.round(v));
  }

  function begin(canvas, opts) {
    var dpr = Math.max(1, num(ROOT.devicePixelRatio, 1));
    var w = logicalSize(canvas, opts, 'width', '__chartsW');
    var h = logicalSize(canvas, opts, 'height', '__chartsH');

    // 物理像素尺寸每次都重设：既能适配 DPR，也能跟着尺寸变化重绘
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.__chartsW = w;
    canvas.__chartsH = h;

    var ctx = null;
    try { ctx = canvas.getContext('2d'); } catch (e) { ctx = null; }
    if (!ctx) return null;

    if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (typeof ctx.clearRect === 'function') ctx.clearRect(0, 0, w, h);

    var env = { ctx: ctx, w: w, h: h, dpr: dpr, theme: resolveTheme(opts) };
    if (env.theme.bg && env.theme.bg !== 'transparent') {
      ctx.fillStyle = env.theme.bg;
      if (typeof ctx.fillRect === 'function') ctx.fillRect(0, 0, w, h);
    }
    return env;
  }

  // ==================== 监听登记（destroy 可整体摘除） ====================

  var REGISTRY = [];

  function on(canvas, type, fn) {
    if (!canvas || typeof canvas.addEventListener !== 'function') return;
    try { canvas.addEventListener(type, fn, false); } catch (e) { return; }
    REGISTRY.push({ canvas: canvas, type: type, fn: fn });
  }

  function detachAll(canvas) {
    if (!canvas) return;
    for (var i = REGISTRY.length - 1; i >= 0; i--) {
      var rec = REGISTRY[i];
      if (rec.canvas !== canvas) continue;
      try {
        if (typeof canvas.removeEventListener === 'function') {
          canvas.removeEventListener(rec.type, rec.fn, false);
        }
      } catch (e) { /* ignore */ }
      REGISTRY.splice(i, 1);
    }
  }

  // 事件坐标换算成本地 CSS 像素（绘制坐标系就是 CSS 像素）
  function localPos(canvas, ev) {
    var out = { x: NaN, y: NaN };
    if (!ev) return out;
    var left = 0, top = 0;
    var rect = null;
    if (typeof canvas.getBoundingClientRect === 'function') {
      try { rect = canvas.getBoundingClientRect(); } catch (e) { rect = null; }
    }
    if (rect) {
      var l = num(rect.left, NaN);
      var t = num(rect.top, NaN);
      if (isFinite(l)) left = l;
      if (isFinite(t)) top = t;
    }
    var cx = num(ev.clientX, NaN);
    if (!isFinite(cx)) cx = num(ev.offsetX, NaN);
    if (!isFinite(cx)) cx = num(ev.pageX, NaN);
    var cy = num(ev.clientY, NaN);
    if (!isFinite(cy)) cy = num(ev.offsetY, NaN);
    if (!isFinite(cy)) cy = num(ev.pageY, NaN);
    if (isFinite(cx)) out.x = cx - left;
    if (isFinite(cy)) out.y = cy - top;
    return out;
  }

  function localX(canvas, ev) {
    return localPos(canvas, ev).x;
  }

  // 悬停：把本地坐标交给调用方定义的解析器，得到统一形状的 info 对象
  function attachHover(canvas, opts, resolver) {
    var o = opts || {};
    if (typeof o.onHover !== 'function') return;
    on(canvas, 'mousemove', function (ev) {
      var p = localPos(canvas, ev);
      var info = null;
      if (isFinite(p.x) || isFinite(p.y)) {
        try { info = resolver(p.x, p.y); } catch (e) { info = null; }
      }
      o.onHover(info);
    });
    on(canvas, 'mouseleave', function () {
      o.onHover(null);
    });
  }

  // ==================== 1) 误差分布直方图 ====================

  function histMedian(counts, lo, binW, total) {
    var half = total / 2;
    var acc = 0;
    for (var i = 0; i < counts.length; i++) {
      var c = Math.max(0, num(counts[i], 0));
      if (c <= 0) continue;
      if (acc + c >= half) {
        var frac = clamp((half - acc) / c, 0, 1);
        return lo + (i + frac) * binW;
      }
      acc += c;
    }
    return lo;
  }

  function drawHistogram(canvas, hist, opts) {
    if (!canvas) return null;
    var o = opts || {};
    detachAll(canvas);
    var env = begin(canvas, o);
    if (!env) return null;
    var ctx = env.ctx, w = env.w, h = env.h, th = env.theme;

    var counts = (hist && isArr(hist.counts)) ? hist.counts : [];
    var n = counts.length;
    var i, c;
    var total = 0;
    var maxCount = 0;
    for (i = 0; i < n; i++) {
      c = Math.max(0, num(counts[i], 0));
      total += c;
      if (c > maxCount) maxCount = c;
    }
    if (n < 1 || total <= 0 || maxCount <= 0) {
      drawEmpty(ctx, w, h, th);
      return null;
    }

    var lo = num(hist.lo, -1);
    var hi = num(hist.hi, 1);
    if (!(hi > lo)) hi = lo + 1;
    var binW = (hi - lo) / n;

    // 顶部 16px 留给中位标注（柱子上方留 14px + 余量），底部 18px 给刻度文字
    var padL = 10, padR = 10, padT = 16, padB = 18;
    var px = padL;
    var pw = Math.max(1, w - padL - padR);
    var py = padT;
    var ph = Math.max(1, h - padT - padB);
    var base = py + ph;
    var slot = pw / n;
    var gap = clamp(slot * 0.18, 0.5, 3);
    var bw = Math.max(1, slot - gap);

    ctx.save();

    // 一根极细的基线，代替粗黑坐标轴
    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, base + 0.5);
    ctx.lineTo(px + pw, base + 0.5);
    ctx.stroke();

    // 柱子：accent（底）→ accent2（顶）的竖向渐变，柱顶 2px 圆角
    for (i = 0; i < n; i++) {
      c = Math.max(0, num(counts[i], 0));
      if (c <= 0) continue;
      var bh = Math.max(1, (c / maxCount) * ph);
      var bx = px + i * slot + gap / 2;
      var by = base - bh;
      ctx.fillStyle = barFill(ctx, th, bx, base, bx, by, th.accent, th.accent2);
      roundTopPath(ctx, bx, by, bw, bh, Math.min(2, bw / 2, bh));
      ctx.fill();
    }

    // x=0 的 1px 竖线
    var hasZero = (lo <= 0 && hi >= 0);
    var xZero = px + (0 - lo) / (hi - lo) * pw;
    if (hasZero) {
      ctx.strokeStyle = th.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xZero, py);
      ctx.lineTo(xZero, base);
      ctx.stroke();
    }

    // 中位数：accent2 虚线 + 8px 小字标注
    var median = isFinite(num(o.median, NaN)) ? num(o.median, 0) : histMedian(counts, lo, binW, total);
    var xMed = px + clamp((median - lo) / (hi - lo), 0, 1) * pw;
    setDash(ctx, [3, 3]);
    ctx.strokeStyle = th.accent2;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xMed, py);
    ctx.lineTo(xMed, base);
    ctx.stroke();
    setDash(ctx, []);

    // 需求指定这一条标注用 8px，其余文字统一 11px
    setFont(ctx, 8);
    var lab = '中位 ' + fmtSignedMs(median);
    var tw = measure(ctx, lab);
    var lx = clamp(xMed, tw / 2 + 2, Math.max(tw / 2 + 2, w - tw / 2 - 2));
    ctx.fillStyle = th.accent2;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(lab, lx, py - 5);

    // x 轴刻度只写 0 与两端
    setFont(ctx, 11);
    ctx.fillStyle = th.dim;
    ctx.textBaseline = 'bottom';
    ctx.textAlign = 'left';
    ctx.fillText(fmtMsTick(lo), px, h - 3);
    ctx.textAlign = 'right';
    ctx.fillText(fmtMsTick(hi), px + pw, h - 3);
    if (hasZero) {
      ctx.textAlign = 'center';
      ctx.fillText('0', xZero, h - 3);
    }

    ctx.restore();

    attachHover(canvas, o, function (x) {
      if (x < px - 1 || x > px + pw + 1) return null;
      var idx = clamp(Math.floor((x - px) / slot), 0, n - 1);
      var binLo = lo + idx * binW;
      return {
        chart: 'histogram',
        index: idx,
        time: null,
        binLo: binLo,
        binHi: binLo + binW,
        count: Math.max(0, num(counts[idx], 0))
      };
    });

    return { width: w, height: h, bins: n, total: total, median: median };
  }

  // ==================== 2) 时间轴（NPS 面积 + 失误散点 + 准确率折线） ====================

  function inferBucket(buckets) {
    if (!isArr(buckets) || buckets.length < 2) return 2000;
    var d = num(buckets[1].t, 0) - num(buckets[0].t, 0);
    return d > 0 ? d : 2000;
  }

  // 把当前 canvas 的像素原样拷一份到离屏画布。
  // 用途：时间轴的静态内容（NPS 面积 / 准确率折线 / 散点 / 刻度）只在整幅重绘时算一次，
  // 播放时的"当前位置竖线"就只要 blit 这张快照再画一根线，代价和画线本身差不多。
  function snapshotLayer(canvas, env) {
    if (!canvas || !canvas.width || !canvas.height) return null;
    var off = null;
    try {
      if (typeof ROOT.document !== 'undefined' && ROOT.document && ROOT.document.createElement) {
        off = ROOT.document.createElement('canvas');
      }
    } catch (e) { off = null; }
    if (!off) return null;
    off.width = canvas.width;
    off.height = canvas.height;
    try {
      var oc = off.getContext('2d');
      if (!oc || typeof oc.drawImage !== 'function') return null;
      oc.drawImage(canvas, 0, 0);
    } catch (e) { return null; }
    return { img: off, w: env.w, h: env.h, dpr: env.dpr };
  }

  // 播放位置竖线（2px，半透明）。竖线只在 [t0, t1] 区间内画，越界就整条不画。
  function drawMarkLine(ctx, geo, markTime) {
    var t = num(markTime, NaN);
    if (!geo || !isFinite(t)) return false;
    if (t < geo.t0 || t > geo.t1) return false;
    var span = geo.span > 0 ? geo.span : 1;
    var x = geo.px + clamp((t - geo.t0) / span, 0, 1) * geo.pw;
    var bottom = isFinite(num(geo.plotBottom, NaN)) ? geo.plotBottom : geo.accBottom;
    ctx.save();
    ctx.globalAlpha = 0.5;
    ctx.strokeStyle = geo.accent;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, geo.plotTop);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.restore();
    return true;
  }

  // 只更新竖线，不重算整幅图。返回 true 表示已更新；false 表示没有可用的静态图层，
  // 调用方应该退回整幅重绘（比如从没画过、或尺寸/DPR 变了）。
  function updateTimelineMark(canvas, markTime) {
    if (!canvas) return false;
    var st = canvas.__chartsTl;
    if (!st || !st.layer || !st.geo) return false;
    var layer = st.layer;
    if (!canvas.width || canvas.width !== layer.img.width || canvas.height !== layer.img.height) return false;
    var ctx = null;
    try { ctx = canvas.getContext('2d'); } catch (e) { ctx = null; }
    if (!ctx) return false;
    var dpr = num(layer.dpr, 1) || 1;
    // blit 在物理像素下做（不套 dpr 变换），之后再切回 CSS 像素坐标画竖线
    if (typeof ctx.setTransform === 'function') ctx.setTransform(1, 0, 0, 1, 0, 0);
    try { ctx.clearRect(0, 0, canvas.width, canvas.height); } catch (e) { /* ignore */ }
    try { ctx.drawImage(layer.img, 0, 0); } catch (e) { return false; }
    if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawMarkLine(ctx, st.geo, markTime);
    return true;
  }

  // 时间轴的三条带（NPS / 准确率 / UR）怎么分高度。
  // 抽出来是因为"最后一条带吃掉余数"这条规则决定了底部会不会漏一条缝，
  // 混在 200 行的绘制流程里时，改权重很容易漏看它。
  function layoutTimelineBands(plotTop, plotH, hasUr) {
    var bandGap = 6;
    var weights = hasUr ? [0.42, 0.29, 0.29] : [0.52, 0.48];
    var usable = Math.max(24, plotH - bandGap * (weights.length - 1));
    var bands = [];
    var cursor = plotTop;
    for (var i = 0; i < weights.length; i++) {
      var bh = (i === weights.length - 1)
        ? Math.max(10, plotTop + plotH - cursor)
        : Math.max(10, Math.round(usable * weights[i]));
      bands.push({ top: cursor, h: bh });
      cursor += bh + bandGap;
    }
    var accTop = bands[1].top;
    var accBottom = accTop + bands[1].h;
    return {
      bands: bands,
      npsTop: bands[0].top,
      npsBottom: bands[0].top + bands[0].h,
      accTop: accTop,
      accBottom: accBottom,
      urTop: hasUr ? bands[2].top : accBottom,
      urBottom: hasUr ? bands[2].top + bands[2].h : accBottom
    };
  }

  // 失误散点：每个桶按"有瑕疵的击打"落点，按归因类别着色。
  // 桶里的 cats 是"含 200"的口径，与准确率线（只按严重失误）不同，所以有类别构成时以它为准。
  // 时间轴上只给「漏接」和「长条断连」画节点：
  // 200/100 这些判定太密，铺满整条曲线反而什么都看不出来。
  function collectErrorDots(buckets, n, xOf, yAcc, accBottom, th) {
    var dots = [];
    for (var k = 0; k < n; k++) {
      var b = buckets[k];
      var accv = num(accOf(b), NaN);
      if (!isFinite(accv)) continue;
      var dx = xOf(num(b.t, 0));
      // 低于下限的桶会被压到带底，散点会正好骑在底边上；抬高 3px 让它留在带内
      var dy = Math.min(yAcc(accv), accBottom - 3);
      if (num(b.miss, 0) > 0) dots.push({ x: dx, y: dy, color: th.miss, hollow: false });
      if (num(b.breaks, 0) > 0) dots.push({ x: dx, y: dy, color: th.j50 || th.miss, hollow: true });
    }
    if (dots.length > 400) {
      // 太密就等距抽样：画 400 个点和画 4000 个点看起来一样，但每帧都重绘得起
      var stride = Math.ceil(dots.length / 400);
      var sampled = [];
      for (var s = 0; s < dots.length; s += stride) sampled.push(dots[s]);
      dots = sampled;
    }
    return dots;
  }

  // 时间轴交互：点击/拖动定位、移动时回调悬停信息、触摸屏点按。
  function bindTimelineInteractions(canvas, geo) {
    var seek = (typeof geo.onSeek === 'function') ? geo.onSeek : null;
    var hover = (typeof geo.onHover === 'function') ? geo.onHover : null;
    var dragging = false;

    function emitSeek(x) {
      if (!isFinite(x)) return null;
      var t = geo.timeAt(x);
      if (seek) {
        try { seek(t); } catch (e) { /* 回调里的异常不打断绘制 */ }
      }
      return t;
    }
    function emitHover(x) {
      if (!hover) return;
      if (!isFinite(x)) { hover(null); return; }
      var t = geo.timeAt(x);
      var idx = (geo.span > 0) ? Math.round(((t - geo.t0) / geo.span) * (geo.count - 1)) : 0;
      idx = clamp(idx, 0, geo.count - 1);
      hover({ chart: 'timeline', time: t, index: idx, bucket: geo.buckets[idx] || null });
    }

    on(canvas, 'mousedown', function (ev) {
      dragging = true;
      emitSeek(localX(canvas, ev));
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
    });
    on(canvas, 'mousemove', function (ev) {
      var x = localX(canvas, ev);
      if (!isFinite(x)) return;
      if (dragging) emitSeek(x);
      emitHover(x);
    });
    on(canvas, 'mouseup', function (ev) {
      if (!dragging) return;
      dragging = false;
      emitSeek(localX(canvas, ev));
    });
    on(canvas, 'mouseleave', function () {
      dragging = false;
      if (hover) hover(null);
    });
    // 触摸屏上「点一下定位」也要能用
    on(canvas, 'touchstart', function (ev) {
      var t = ev && ev.touches && ev.touches[0];
      if (!t) return;
      dragging = true;
      emitSeek(localX(canvas, t));
    });
    on(canvas, 'touchend', function () {
      dragging = false;
    });
  }

  // 三条带各自怎么画。坐标换算（xOf / yNps / yAcc / yUr）由调用方组进 g 传进来，
  // 所以这里只管"画成什么样"；布局怎么分高度是 layoutTimelineBands 的事。
  // 绘制顺序仍然是 NPS → 准确率 → UR → 标签，与分层（z 序）有关，别随手调换。
  function drawNpsBand(ctx, g, npsPts, nn, th) {
    var k;
    // 面积（accent 20% 透明度）
    ctx.beginPath();
    ctx.moveTo(g.xOf(num(npsPts[0].t, 0)), g.npsBottom);
    for (k = 0; k < nn; k++) {
      ctx.lineTo(g.xOf(num(npsPts[k].t, 0)), g.yNps(npsPts[k].v));
    }
    ctx.lineTo(g.xOf(num(npsPts[nn - 1].t, 0)), g.npsBottom);
    ctx.closePath();
    ctx.globalAlpha = 0.2;
    ctx.fillStyle = th.accent;
    ctx.fill();
    ctx.globalAlpha = 1;

    // 折线（accent 2px）
    ctx.beginPath();
    for (k = 0; k < nn; k++) {
      var nx = g.xOf(num(npsPts[k].t, 0));
      var ny = g.yNps(npsPts[k].v);
      if (k === 0) ctx.moveTo(nx, ny);
      else ctx.lineTo(nx, ny);
    }
    ctx.strokeStyle = th.accent;
    ctx.lineWidth = 2;
    ctx.stroke();

    // 带底边（细提示线）
    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.px, g.npsBottom + 0.5);
    ctx.lineTo(g.px + g.pw, g.npsBottom + 0.5);
    ctx.stroke();
  }

  // 准确率带：v1 加权准确率折线 + 底边 + 右栏刻度 + 96% 参考线
  function drawAccBand(ctx, g, buckets, n, hasAcc, th) {
    if (!hasAcc) return;
    var k;
    var pen = false;
    ctx.beginPath();
    for (k = 0; k < n; k++) {
      var av = num(accOf(buckets[k]), NaN);
      if (!isFinite(av)) {
        pen = false;
        continue;
      }
      var ax = g.xOf(num(buckets[k].t, 0));
      var ay = g.yAcc(av);
      if (!pen) {
        ctx.moveTo(ax, ay);
        pen = true;
      } else {
        ctx.lineTo(ax, ay);
      }
    }
    ctx.strokeStyle = th.accent2;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // 带底边
    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.px, g.accBottom + 0.5);
    ctx.lineTo(g.px + g.pw, g.accBottom + 0.5);
    ctx.stroke();

    // 右侧的独立刻度（ACC_FLOOR..100%）
    setFont(ctx, 11);
    ctx.fillStyle = th.dim;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText('100%', g.px + g.pw + 4, g.accTop - 2);
    ctx.textBaseline = 'bottom';
    ctx.fillText(ACC_FLOOR + '%', g.px + g.pw + 4, g.accBottom + 2);

    // 96% 参考线：虚线而不是实线，它是"参考"，不该和真正的数据线抢注意力
    var refY = g.yAcc(ACC_REF);
    if (refY > g.accTop + 1 && refY < g.accBottom - 1) {
      ctx.save();
      ctx.globalAlpha = 0.55;
      ctx.strokeStyle = th.accent2;
      ctx.lineWidth = 1;
      if (typeof ctx.setLineDash === 'function') ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(g.px, refY + 0.5);
      ctx.lineTo(g.px + g.pw, refY + 0.5);
      ctx.stroke();
      ctx.restore();
      // 数值写右栏，和 100% / 96% 同一列；放在线下方一点，别和顶上的 100% 挤在一起
      setFont(ctx, 10);
      ctx.fillStyle = th.accent2;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText(ACC_REF + '%', g.px + g.pw + 4, refY + 1);
    }
  }

  // UR 带：误差总体标准差 ×10 的折线 + 底边 + 平均 UR 参考线。
  // UR 比准确率更早暴露"手开始飘了"：准确率还在 99% 时 UR 可能已经从 80 涨到 130。
  function drawUrBand(ctx, g, buckets, n, hasUr, avgUr, urPeak, th) {
    if (!hasUr) return;
    var k;
    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.px, g.urBottom + 0.5);
    ctx.lineTo(g.px + g.pw, g.urBottom + 0.5);
    ctx.stroke();

    var urPen = false;
    ctx.beginPath();
    for (k = 0; k < n; k++) {
      var urv = num(buckets[k].ur, NaN);
      if (!isFinite(urv)) {
        urPen = false;   // 样本不足的桶断线，别用直线跨过去假装连续
        continue;
      }
      var urx = g.xOf(num(buckets[k].t, 0));
      var ury = g.yUr(urv);
      if (!urPen) {
        ctx.moveTo(urx, ury);
        urPen = true;
      } else {
        ctx.lineTo(urx, ury);
      }
    }
    ctx.strokeStyle = th.warn;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // 平均 UR 参考线（虚线）：一局的整体稳定度，单看折线很难心算
    if (isFinite(avgUr) && avgUr > 0) {
      var avgY = g.yUr(avgUr);
      if (avgY > g.urTop + 1 && avgY < g.urBottom - 1) {
        ctx.save();
        ctx.globalAlpha = 0.65;
        ctx.strokeStyle = th.warn;
        ctx.lineWidth = 1;
        if (typeof ctx.setLineDash === 'function') ctx.setLineDash([3, 4]);
        ctx.beginPath();
        ctx.moveTo(g.px, avgY + 0.5);
        ctx.lineTo(g.px + g.pw, avgY + 0.5);
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  // 每条带的轴标签（落在左侧刻度栏里，不压曲线）+ 右栏的 UR 上限
  function drawBandLabels(ctx, g, bands, hasUr, urPeak, avgUr, th) {
    var items = [
      { text: 'NPS', band: bands[0], color: th.accent },
      { text: '准确率', band: bands[1], color: th.accent2 }
    ];
    if (hasUr) items.push({ text: 'UR', band: bands[2], color: th.warn });
    setFont(ctx, 10);
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.globalAlpha = 0.9;
    for (var bi = 0; bi < items.length; bi++) {
      ctx.fillStyle = items[bi].color;
      ctx.fillText(items[bi].text, g.px - 6, items[bi].band.top + items[bi].band.h / 2);
    }
    ctx.globalAlpha = 1;
    // UR 的上限值放在右栏顶部，和准确率的 100%/96% 同一套读法
    if (hasUr) {
      setFont(ctx, 10);
      ctx.fillStyle = th.warn;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText(String(Math.round(urPeak)), g.px + g.pw + 4, g.urTop + 1);
      // 平均 UR 写在左栏 UR 标签下方，和右栏的峰值区分开
      if (isFinite(avgUr) && avgUr > 0 && bands[2].h >= 30) {
        setFont(ctx, 9);
        ctx.fillStyle = th.warn;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText('均' + Math.round(avgUr), g.px - 6, bands[2].top + bands[2].h - 6);
      }
    }
  }

  // 时间轴的数据准备：砍掉前奏后的桶、NPS 曲线、是否存在准确率/UR 带，以及统一的坐标范围。
  // 单独抽出来，是因为这些选择决定了"图上看到的是哪一段"，改口径时不该翻绘制代码。
  // 数据为空时返回 null，由调用方画空状态。
  function prepareTimeline(timeline, o) {
    var buckets = (timeline && isArr(timeline.buckets)) ? timeline.buckets : [];
    var n = buckets.length;
    var i;

    // 砍掉前奏：和回放页"从第一个音符前 1.5 秒开始"同一个口径，
    // 否则前奏 40 秒的歌，时间轴左边三分之一全是空的。
    var startMs = num(o.startMs, 0);
    var bk = num(timeline && timeline.bucket, 0);
    if (startMs > 0 && bk > 0 && n > 2) {
      var keep = 0;
      while (keep < n - 2 && num(buckets[keep].t, 0) + bk <= startMs) keep += 1;
      if (keep > 0) {
        buckets = buckets.slice(keep);
        n = buckets.length;
      }
    }

    var totalNotes = 0;
    var hasAcc = false;
    for (i = 0; i < n; i++) {
      totalNotes += Math.max(0, num(buckets[i].notes, 0));
      if (num(accOf(buckets[i]), NaN) === num(accOf(buckets[i]), NaN)) hasAcc = true;
    }

    // NPS 有自己的一条曲线（固定 2 秒采样），不能借用准确率那套自适应分段：
    // 否则同一张图的密度曲线会随分段粒度变形（快歌上全是尖刺）。
    // 老数据没有 npsSeries 时才退回桶里的 nps 字段。
    var npsPts = [];
    if (timeline && timeline.npsSeries && isArr(timeline.npsSeries.points) && timeline.npsSeries.points.length) {
      npsPts = timeline.npsSeries.points;
    } else {
      for (i = 0; i < n; i++) npsPts.push({ t: buckets[i].t, v: buckets[i].nps });
    }
    if (startMs > 0) {
      npsPts = npsPts.filter(function (p) {
        return num(p.t, 0) + num(timeline && timeline.npsSeries && timeline.npsSeries.bucket, 2000) > startMs;
      });
    }

    var nn = npsPts.length;
    var peakNps = 0;
    for (i = 0; i < nn; i++) peakNps = Math.max(peakNps, num(npsPts[i].v, 0));
    if (n < 1 || (totalNotes <= 0 && peakNps <= 0)) return null;

    var bucket = num(timeline.bucket, 0) || inferBucket(buckets);
    var t0 = num(buckets[0].t, 0);
    var t1 = num(buckets[n - 1].t, 0) + bucket;
    if (!(t1 > t0)) t1 = t0 + bucket;

    // UR 是后加的字段，旧数据（早先导出的报告 JSON）里没有它：没有就不占位，
    // 布局与从前完全一致 —— 留一条空带比少一条带更误导人。
    var hasUr = false;
    for (i = 0; i < n; i++) {
      if (isFinite(num(buckets[i].ur, NaN))) { hasUr = true; break; }
    }

    return {
      buckets: buckets, n: n, npsPts: npsPts, nn: nn,
      peakNps: peakNps, hasAcc: hasAcc, hasUr: hasUr,
      bucket: bucket, t0: t0, t1: t1, span: t1 - t0
    };
  }

  function drawTimeline(canvas, timeline, opts) {
    if (!canvas) return null;
    var o = opts || {};
    detachAll(canvas);
    var env = begin(canvas, o);
    if (!env) return null;
    var ctx = env.ctx, w = env.w, h = env.h, th = env.theme;

    var view = prepareTimeline(timeline, o);
    if (!view) {
      drawEmpty(ctx, w, h, th);
      return null;
    }
    var buckets = view.buckets;
    var n = view.n;
    var npsPts = view.npsPts;
    var nn = view.nn;
    var peakNps = view.peakNps;
    var hasAcc = view.hasAcc;
    var hasUr = view.hasUr;
    var bucket = view.bucket;
    var t0 = view.t0;
    var t1 = view.t1;
    var span = view.span;
    var i;

    // 左边留出刻度栏：三条带的名字写在栏里（右对齐），就不会压在曲线上
    var padL = 44, padR = 32, padT = 10, padB = 16;
    var px = padL;
    var pw = Math.max(1, w - padL - padR);
    var plotTop = padT;
    var plotH = Math.max(1, h - padT - padB);

    // 分带：NPS 面积在上，准确率折线居中，UR 折线在下（共用 x 轴：时间）。
    var layout = layoutTimelineBands(plotTop, plotH, hasUr);
    var bands = layout.bands;
    var npsTop = layout.npsTop;
    var npsBottom = layout.npsBottom;
    var accTop = layout.accTop;
    var accBottom = layout.accBottom;
    var urTop = layout.urTop;
    var urBottom = layout.urBottom;

    var npsPeak = peakNps > 0 ? peakNps : 1;

    // UR 的纵轴上限按这一次的峰值定（不同谱面差得很远），向上取整到 10 的倍数，
    // 既好读又让曲线不至于贴着顶边。
    var urPeak = 0;
    if (hasUr) {
      for (i = 0; i < n; i++) {
        var uv = num(buckets[i].ur, NaN);
        if (isFinite(uv) && uv > urPeak) urPeak = uv;
      }
      urPeak = Math.max(30, Math.ceil(urPeak / 10) * 10);
    }
    // 本次游玩的平均 UR（summary.ur）：画一条参考线，省得对着折线心算整体水平
    var avgUr = num(o.avgUr, NaN);

    function xOf(t) {
      return px + clamp((t - t0) / span, 0, 1) * pw;
    }
    function timeAt(x) {
      return clamp(t0 + clamp((x - px) / pw, 0, 1) * span, t0, t1);
    }
    function yNps(v) {
      return npsBottom - clamp(num(v, 0) / npsPeak, 0, 1) * (npsBottom - npsTop);
    }
    function yAcc(v) {
      // 线性映射到 [ACC_FLOOR, 100]：低于下限的一律压到带底（这一段没有分析价值）
      var t = (num(v, 0) - ACC_FLOOR) / (100 - ACC_FLOOR);
      return accBottom - clamp(t, 0, 1) * (accBottom - accTop);
    }
    function yUr(v) {
      return urBottom - clamp(num(v, 0) / urPeak, 0, 1) * (urBottom - urTop);
    }

    ctx.save();
    var k;
    var g = {
      px: px, pw: pw,
      npsTop: npsTop, npsBottom: npsBottom,
      accTop: accTop, accBottom: accBottom,
      urTop: urTop, urBottom: urBottom,
      xOf: xOf, yNps: yNps, yAcc: yAcc, yUr: yUr
    };
    drawNpsBand(ctx, g, npsPts, nn, th);
    drawAccBand(ctx, g, buckets, n, hasAcc, th);
    drawUrBand(ctx, g, buckets, n, hasUr, avgUr, urPeak, th);
    drawBandLabels(ctx, g, bands, hasUr, urPeak, avgUr, th);

    // ---- 失误散点（画在准确率曲线上，按类别着色，半径 3px） ----
    var dots = collectErrorDots(buckets, n, xOf, yAcc, accBottom, th);
    for (k = 0; k < dots.length; k++) {
      ctx.beginPath();
      ctx.arc(dots[k].x, dots[k].y, 3, 0, TAU);
      if (dots[k].hollow) {
        ctx.strokeStyle = dots[k].color;   // 长条断连画空心，和"整点漏接"区分开
        ctx.lineWidth = 1.6;
        ctx.stroke();
      } else {
        ctx.fillStyle = dots[k].color;
        ctx.fill();
      }
    }

    // ---- 底部时间刻度：0 / 1/4 / 1/2 / 3/4 / 末尾 ----
    setFont(ctx, 11);
    ctx.fillStyle = th.dim;
    ctx.textBaseline = 'bottom';
    for (k = 0; k < 5; k++) {
      var tx = px + pw * (k / 4);
      // 刻度写**绝对时间**：砍掉前奏后，左端已经不是 0 秒了，
      // 还按 "span*k/4" 相对算的话，40 秒前奏的图会从"0:00"开始，直接看错。
      var tLabel = fmtTime(t0 + span * (k / 4));
      ctx.textAlign = k === 0 ? 'left' : (k === 4 ? 'right' : 'center');
      ctx.fillText(tLabel, tx, h - 3);
    }

    // ---- 当前播放位置的竖线（2px，半透明） ----
    // 静态内容到此为止：先拍快照（快照里没有竖线），再在最上层画竖线。
    // 这样播放时每帧只要 blit + 画线，不用把几百个桶重算一遍。
    canvas.__chartsTl = {
      layer: snapshotLayer(canvas, env),
      geo: {
        px: px, pw: pw, t0: t0, t1: t1, span: span,
        // 竖线要贯穿全部三条带：plotBottom 是最下面那条带的下沿
        plotTop: plotTop, plotBottom: urBottom, accTop: accTop, accBottom: accBottom,
        accFloor: ACC_FLOOR, accent: th.accent
      }
    };
    drawMarkLine(ctx, canvas.__chartsTl.geo, num(o.markTime, NaN));

    ctx.restore();

    // ---- 交互：点击 / 拖动定位，移动时回调悬停信息 ----
    bindTimelineInteractions(canvas, {
      timeAt: timeAt, span: span, t0: t0, count: n, buckets: buckets,
      onSeek: o.onSeek, onHover: o.onHover
    });

    return {
      width: w, height: h,
      t0: t0, t1: t1, bucket: bucket,
      dots: dots.length
    };
  }

  // ==================== 3) 密度—失误率柱状图 ====================

  function drawDensity(canvas, profile, opts) {
    if (!canvas) return null;
    var o = opts || {};
    detachAll(canvas);
    var env = begin(canvas, o);
    if (!env) return null;
    var ctx = env.ctx, w = env.w, h = env.h, th = env.theme;

    var rows = (profile && isArr(profile.density_profile)) ? profile.density_profile : [];
    var n = rows.length;
    var i;
    var rates = [];
    var maxRate = 0;
    for (i = 0; i < n; i++) {
      var r = ratePercent(rows[i].errorRate, o.rateUnit);
      rates.push(r === null ? 0 : r);
      if (rates[i] > maxRate) maxRate = rates[i];
    }
    if (n < 1 || maxRate <= 0) {
      drawEmpty(ctx, w, h, th);
      return null;
    }
    var domainMax = maxRate * 1.02;

    // 左侧给档位标签留位置（宽度按实际文字量算）
    setFont(ctx, 11);
    var labelW = 0;
    for (i = 0; i < n; i++) {
      labelW = Math.max(labelW, measure(ctx, rowLabel(rows[i])));
    }
    var padL = clamp(labelW + 10, 44, w * 0.46);
    var padR = clamp(measure(ctx, '100.0%') + 14, 34, w * 0.28);
    var padT = 14, padB = 8;
    var px = padL;
    var pw = Math.max(1, w - padL - padR);
    var py = padT;
    var ph = Math.max(1, h - padT - padB);
    var rowH = ph / n;
    var barH = clamp(rowH * 0.6, 4, 20);

    ctx.save();

    for (i = 0; i < n; i++) {
      var cy = py + rowH * (i + 0.5);
      var bwid = Math.max(1, (rates[i] / domainMax) * pw);
      var bx = px;
      var by = cy - barH / 2;

      ctx.fillStyle = barFill(ctx, th, bx, cy, bx + bwid, cy, th.accent, th.accent2);
      roundRightPath(ctx, bx, by, bwid, barH, Math.min(2, barH / 2));
      ctx.fill();

      // 档位标签
      setFont(ctx, 11);
      ctx.fillStyle = th.dim;
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(rowLabel(rows[i]), px - 8, cy);

      // 柱端右侧的百分比
      var vt = fmt1(rates[i]) + '%';
      var vw = measure(ctx, vt);
      var vx = bx + bwid + 6;
      ctx.textAlign = 'left';
      if (vx + vw > px + pw + 2) {
        // 贴到右边界时改成柱内右对齐，避免文字被裁掉
        ctx.textAlign = 'right';
        ctx.fillStyle = th.text;
        ctx.fillText(vt, px + pw, cy);
      } else {
        ctx.fillStyle = th.text;
        ctx.fillText(vt, vx, cy);
      }
    }

    // 稳定上限：横轴是"失误率"，而 ceiling 的单位是 NPS，
    // 所以不能用它去比横轴的比例；正确画法是把它标在**包含它的那一档**上（横向虚线）。
    var ceiling = num(profile.ceiling, NaN);
    if (isFinite(ceiling) && ceiling > 0) {
      var cIdx = -1;
      for (var ci = 0; ci < n; ci++) {
        var rr = rows[ci] || {};
        var lo = num(rr.low, NaN);
        var hi = num(rr.high, NaN);
        if (isFinite(lo) && isFinite(hi) && ceiling >= lo && ceiling <= hi) {
          cIdx = ci;
          break;
        }
      }
      if (cIdx >= 0) {
        var cyMid = py + rowH * cIdx + rowH / 2;
        setDash(ctx, [4, 3]);
        ctx.strokeStyle = th.accent2;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, cyMid);
        ctx.lineTo(px + pw, cyMid);
        ctx.stroke();
        setDash(ctx, []);

      }
    }

    ctx.restore();

    attachHover(canvas, o, function (x, y) {
      if (!isFinite(y) || y < py || y > py + ph) return null;
      var idx = clamp(Math.floor((y - py) / rowH), 0, n - 1);
      var row = rows[idx] || {};
      return {
        chart: 'density',
        index: idx,
        time: null,
        low: num(row.low, 0),
        high: num(row.high, 0),
        errorRate: rates[idx],
        count: num(row.count, 0)
      };
    });

    return { width: w, height: h, rows: n, maxRate: maxRate };
  }

  function rowLabel(row) {
    return fmt1(row && row.low) + '–' + fmt1(row && row.high) + ' NPS';
  }

  // ==================== 4) 各列表现 ====================

  function drawColumns(canvas, columnStats, opts) {
    if (!canvas) return null;
    var o = opts || {};
    detachAll(canvas);
    var env = begin(canvas, o);
    if (!env) return null;
    var ctx = env.ctx, w = env.w, h = env.h, th = env.theme;

    var cols = isArr(columnStats) ? columnStats : [];
    var n = cols.length;
    var i;
    var rates = [];
    var maxRate = 0;
    var maxAbsDelta = 0;
    for (i = 0; i < n; i++) {
      var r = ratePercent(cols[i] && cols[i].errorRate, o.rateUnit);
      rates.push(r === null ? 0 : r);
      if (rates[i] > maxRate) maxRate = rates[i];
      var md = Math.abs(num(cols[i] && cols[i].meanDelta, 0));
      if (md > maxAbsDelta) maxAbsDelta = md;
    }
    if (n < 1 || maxRate <= 0) {
      drawEmpty(ctx, w, h, th);
      return null;
    }

    var padL = 12, padR = 12, padT = 18, padB = 16;
    var px = padL;
    var pw = Math.max(1, w - padL - padR);
    var py = padT;
    var ph = Math.max(1, h - padT - padB);
    var base = py + ph;
    var slot = pw / n;
    var bw = Math.max(4, Math.min(slot * 0.62, 46));

    // 左半（左手）100% 不透明，右半（右手）72%
    var half = n / 2;

    ctx.save();

    // 极细基线
    ctx.strokeStyle = th.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px, base + 0.5);
    ctx.lineTo(px + pw, base + 0.5);
    ctx.stroke();

    for (i = 0; i < n; i++) {
      var cx = px + slot * (i + 0.5);
      var bh = Math.max(2, (rates[i] / maxRate) * ph);
      var bx = cx - bw / 2;
      var by = base - bh;

      ctx.globalAlpha = (i < half) ? 1 : 0.72;
      ctx.fillStyle = barFill(ctx, th, bx, base, bx, by, th.accent, th.accent2);
      roundTopPath(ctx, bx, by, bw, bh, Math.min(2, bw / 2, bh));
      ctx.fill();
      ctx.globalAlpha = 1;

      // 平均偏差画成一根**细竖线**（原来是个飘在柱顶的点，看不出是"偏差"）：
      // 横坐标位置表示偏早/偏晚，线从柱子底部一直伸到柱顶上方，像一枚指针。
      var del = num(cols[i] && cols[i].meanDelta, NaN);
      if (isFinite(del) && maxAbsDelta > 0) {
        var dx = cx + (del / maxAbsDelta) * (bw * 0.45);
        ctx.beginPath();
        ctx.moveTo(dx, by - 8);
        ctx.lineTo(dx, base);
        ctx.strokeStyle = th.accent2;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        // 顶端点一下，指针感更明确
        ctx.beginPath();
        ctx.arc(dx, by - 8, 1.6, 0, TAU);
        ctx.fillStyle = th.accent2;
        ctx.fill();
      }

      // 柱底写列号
      setFont(ctx, 11);
      ctx.fillStyle = th.dim;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      // 列号按 1 起算：图上写 1/2/3/4，和界面其它地方"第 N 列"的说法一致
      ctx.fillText(String(num(cols[i] && cols[i].column, i) + 1), cx, h - 3);
    }

    ctx.restore();

    attachHover(canvas, o, function (x) {
      if (x < px || x > px + pw) return null;
      var idx = clamp(Math.floor((x - px) / slot), 0, n - 1);
      return {
        chart: 'columns',
        index: idx,
        time: null,
        column: num(cols[idx] && cols[idx].column, idx),
        rate: rates[idx]
      };
    });


  return { width: w, height: h, columns: n, maxRate: maxRate };
  }

  // ==================== 对外接口 ====================

  
  // ---------------------------------------------------------------- 雷达图（MSD）
  // 七个键型维度画成正多边形。为什么用雷达而不是柱状：
  // "这张图难在耐力还是难在叠键"是一个**形状**问题，雷达一眼能看出偏科方向。
  var RADAR_KEYS = ["stream", "jumpstream", "handstream", "chordjack", "jackspeed", "stamina", "technical"];
  var RADAR_LABELS = {
    stream: "单押切",
    jumpstream: "双押切",
    handstream: "三押切",
    chordjack: "多押叠",
    jackspeed: "叠键速度",
    stamina: "耐力",
    technical: "技巧",
  };

  function drawRadar(canvas, skillsets, opts) {
    if (!canvas) return null;
    var o = opts || {};
    detachAll(canvas);
    var env = begin(canvas, o);
    if (!env) return null;
    var ctx = env.ctx, w = env.w, h = env.h, th = env.theme;
    if (!skillsets) {
      drawEmpty(ctx, w, h, th);
      return null;
    }

    var cx = w / 2;
    var cy = h / 2 + 4;
    var radius = Math.max(40, Math.min(w, h) / 2 - 54);
    var n = RADAR_KEYS.length;
    var vals = RADAR_KEYS.map(function (k) { return num(skillsets[k], 0); });
    var peak = Math.max.apply(null, vals.concat([1]));
    // 轴上限取"最大项向上取整到 5 的倍数"，保证形状铺得开又不会贴边
    var axisMax = Math.max(5, Math.ceil(peak / 5) * 5);

    function pt(i, ratio) {
      var a = -Math.PI / 2 + (Math.PI * 2 * i) / n;
      return [cx + Math.cos(a) * radius * ratio, cy + Math.sin(a) * radius * ratio];
    }

    // 底网：4 圈
    ctx.lineWidth = 1;
    for (var ring = 1; ring <= 4; ring++) {
      var rr = ring / 4;
      ctx.beginPath();
      for (var i = 0; i < n; i++) {
        var p = pt(i, rr);
        if (i === 0) ctx.moveTo(p[0], p[1]);
        else ctx.lineTo(p[0], p[1]);
      }
      ctx.closePath();
      ctx.strokeStyle = th.grid;
      ctx.stroke();
    }
    // 轴线
    for (var k = 0; k < n; k++) {
      var e = pt(k, 1);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(e[0], e[1]);
      ctx.strokeStyle = th.grid;
      ctx.stroke();
    }

    // 数据多边形
    ctx.beginPath();
    for (var j = 0; j < n; j++) {
      var q = pt(j, clamp(vals[j] / axisMax, 0.02, 1));
      if (j === 0) ctx.moveTo(q[0], q[1]);
      else ctx.lineTo(q[0], q[1]);
    }
    ctx.closePath();
    ctx.globalAlpha = 0.18;
    ctx.fillStyle = th.accent;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = th.accent;
    ctx.lineWidth = 2;
    ctx.stroke();

    // 顶点 + 数值
    setFont(ctx, 11);
    ctx.textBaseline = "middle";
    for (var m = 0; m < n; m++) {
      var v = pt(m, clamp(vals[m] / axisMax, 0.02, 1));
      ctx.beginPath();
      ctx.arc(v[0], v[1], 3, 0, Math.PI * 2);
      ctx.fillStyle = th.accent;
      ctx.fill();

      var out = pt(m, 1.16);
      var label = RADAR_LABELS[RADAR_KEYS[m]];
      var txt = label + " " + vals[m].toFixed(1);
      ctx.fillStyle = th.dim;
      ctx.textAlign = Math.abs(out[0] - cx) < 2 ? "center" : (out[0] > cx ? "left" : "right");
      ctx.fillText(txt, out[0], out[1]);
    }

    // 中心标尺
    setFont(ctx, 10);
    ctx.globalAlpha = 0.75;
    ctx.fillStyle = th.dim;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText("轴上限 " + axisMax, 8, 8);
    ctx.globalAlpha = 1;
    return { axisMax: axisMax };
  }

  var Charts = {
    drawHistogram: drawHistogram,
    drawTimeline: drawTimeline,
    drawRadar: drawRadar,
    // 只刷播放位置竖线（不重算整幅图），播放时每帧调用
    updateTimelineMark: updateTimelineMark,
    drawDensity: drawDensity,
    drawColumns: drawColumns,
    // 返回解析后的主题色（opts 可省略）
    colors: function (opts) {
      return resolveTheme(opts);
    },
    // 摘掉某个 canvas 上注册的全部监听（切页时调用）
    destroy: function (canvas) {
      detachAll(canvas);
    }
  };

  ROOT.Charts = Charts;
  if (typeof window !== 'undefined' && window !== ROOT) window.Charts = Charts;
})();
