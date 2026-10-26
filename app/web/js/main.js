/* 页面装配：状态、事件、图表与播放器联动。 */
(function () {
  "use strict";

  const { $, el, fmtTime, fmtSigned, CATS } = window.UI;

  // 失误清单每页条数、按多少毫秒分一段（1000+ 条的谱面靠分段标题找位置）
  const ERROR_PAGE = 100;
  const ERROR_BUCKET_MS = 30000;

  // 打击音音量的上限（3 = 300%）。回放音量仍然只到 1，两条滑杆量程不同：
  // 谱面原声响的时候，100% 的打击音往往还是听不见，所以单独放开一档。
  const HIT_VOL_MAX = 3;

  const state = {
    data: null,
    chartOnly: null,      // 只看谱面（.osu）时的精简结果；与 data 互斥
    replayBase64: null,
    localItems: [],       // _replays 目录的原始列表，搜索时在内存里过滤
    tab: "overview",
    errorFilter: { cat: "all", severe: false },
    errorShown: 50,
    player: null,
    player2: null,        // 对比模式下的"回放 2"（标杆）
    compare: null,        // 对比模式下第二份回放的完整分析结果
    prog1: null,          // { ts, accW, score, total }：主回放的前缀和，用来算实时分差
    prog2: null,
    errorsOnly: false,
    busy: false,
    audio: null,
    activeXhr: null,
    virtualTime: 0,
    selectedError: null,   // 被点中的那条失误（此刻卡片会钉住它）
    playing: false,
    lastFrame: 0,
    speed: 1,
    scrollMs: 1200,     // 下落时间：音符从画布顶端落到判定线的毫秒数（越小越快）
    fpsCap: 120,        // 回放帧率上限：60 / 120 / 240，跑不动会自动降到一半
    audioOffset: 0,     // 音画偏移（ms）：正值 = 谱面比音频提前
    volume: 0.6,        // 回放音量 0..1（曲目原声往往很吵，默认压到 60%）
    muted: false,
    // 打击音：off / planned（按谱面时间响）/ actual（按实际击打时间响）
    hitsoundMode: "off",
    hitsoundVolume: 0.7,   // 打击音自己的音量 0..HIT_VOL_MAX，和回放音量分开
    // 下面这些在流程中按需赋值，先在这里声明清楚类型与默认值，
    // 免得靠"某个函数里突然多出一个属性"来传递状态
    audioBroken: false,
    stage: "",
    // _replays 的真实路径由服务端给（/api/local-replays 的 dir）。
    // 前端写死绝对路径的话，换台机器/换个目录就会指着别人家硬盘。
    localDir: "",
    escaped: false,
    cancelLoading: null,
    _errTimes: null,
    _tlFallbackAt: 0,
    _searchTimer: null,
    _accShown: null,
    // 判定口径：v1（stable 合并）/ v2（头尾分判）。两套结果后端都已算好，
    // 切换只是换一份视图，不重新请求分析。
    judgeMode: "v1",
    baseData: null,
    // 判定算法（仅调试用）：C 头尾都走事件流（默认）、D 在 A 口径下放宽晚松。
    // 页面上只有这两个选项，服务端也只认这两个值。
    judgeAlgo: "C",
    lastReplay: null,   // 最后一次回放来源：{kind:"local", name} 或 {kind:"file", file}
    // 失误清单过滤：只影响显示，不参与判定
    hideLowConfidence: false,
    confThreshold: 0.5,
    theme: "dark",
    // 禁用动画：默认关。开了以后切页不再播 rise/fade，界面立刻到位
    disableMotion: false,
    // 性能面板：默认关。开着的时候播放会在右下角显示 FPS 与每帧耗时，
    // 只有排查卡顿时才需要，平时关掉免得自己成为负担。
    perfPanel: false,
  };

  // 回放页的两个滑杆记在本地，下次打开还是上次的设置
  const PREFS_KEY = "mania-replay-prefs";
  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) return;
      const p = JSON.parse(raw);
      if (typeof p.scrollMs === "number" && p.scrollMs >= 300 && p.scrollMs <= 2600) state.scrollMs = p.scrollMs;
      if (FPS_CAPS.indexOf(p.fpsCap) >= 0) state.fpsCap = p.fpsCap;
      if (typeof p.audioOffset === "number" && p.audioOffset >= -500 && p.audioOffset <= 500) state.audioOffset = p.audioOffset;
      if (typeof p.volume === "number" && p.volume >= 0 && p.volume <= 1) state.volume = p.volume;
      if (p.hitsoundMode === "off" || p.hitsoundMode === "planned" || p.hitsoundMode === "actual") {
        state.hitsoundMode = p.hitsoundMode;
      }
      // 打击音最高 300%，回放音量仍然只到 100%（两条滑杆的量程本来就不一样）
      if (typeof p.hitsoundVolume === "number" && p.hitsoundVolume >= 0 && p.hitsoundVolume <= HIT_VOL_MAX) {
        state.hitsoundVolume = p.hitsoundVolume;
      }
      if (p.judgeMode === "v1" || p.judgeMode === "v2" || p.judgeMode === "classic") state.judgeMode = p.judgeMode;
      if (p.judgeAlgo === "C" || p.judgeAlgo === "D") state.judgeAlgo = p.judgeAlgo;
      if (typeof p.hideLowConfidence === "boolean") state.hideLowConfidence = p.hideLowConfidence;
      if (typeof p.confThreshold === "number" && p.confThreshold >= 0 && p.confThreshold <= 1) {
        state.confThreshold = p.confThreshold;
      }
      if (p.theme === "dark" || p.theme === "sakura") state.theme = p.theme;
      if (typeof p.disableMotion === "boolean") state.disableMotion = p.disableMotion;
      if (typeof p.perfPanel === "boolean") state.perfPanel = p.perfPanel;
    } catch (e) { /* 隐私模式下拿不到就算了 */ }
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        scrollMs: state.scrollMs,
        fpsCap: state.fpsCap,
        audioOffset: state.audioOffset,
        volume: state.volume,
        hitsoundMode: state.hitsoundMode,
        hitsoundVolume: state.hitsoundVolume,
        judgeMode: state.judgeMode,
        judgeAlgo: state.judgeAlgo,
        hideLowConfidence: state.hideLowConfidence,
        confThreshold: state.confThreshold,
        theme: state.theme,
        disableMotion: state.disableMotion,
        perfPanel: state.perfPanel,
      }));
    } catch (e) { /* ignore */ }
  }

  // 性能面板：只在设置里勾了才统计。样本用中位数——偶尔一帧卡顿不该被当成常态，
  // 但连续掉帧会立刻把中位数拉上去，正好是排查时想看的信号。
  // 面板自己跑一条 rAF 循环：没在放回放时也能看到刷新率，不至于勾上之后一片空白。
  // 注意「渲染」和「刷新」是两个数：渲染是程序真的画了多少帧（受帧率上限与机器影响），
  // 刷新是屏幕给 rAF 的节奏。60Hz 屏上选 120 档时前者 120、后者 60，别混为一谈。
  let perfCost = [];
  let perfDt = [];
  let perfStamp = [];
  let perfPaintedAt = 0;
  let perfRaf = 0;
  let perfLastAt = 0;

  function applyPerfPanel(on) {
    state.perfPanel = !!on;
    const box = $("perfPanel");
    if (box) {
      box.hidden = !state.perfPanel;
      if (state.perfPanel) box.textContent = "性能面板\n等待第一帧…";
    }
    const toggle = $("perfPanelToggle");
    if (toggle) toggle.checked = state.perfPanel;
    perfCost = [];
    perfDt = [];
    perfStamp = [];
    perfPaintedAt = 0;
    perfLastAt = 0;
    if (state.perfPanel) startPerfLoop();
    else stopPerfLoop();
  }

  function startPerfLoop() {
    if (perfRaf) return;      // 已经在跑，别叠第二条
    const loop = (now) => {
      if (!state.perfPanel) { perfRaf = 0; return; }
      if (perfLastAt) {
        const dt = now - perfLastAt;
        // 超过 500ms 多半是切到后台/最小化了，记进去会把中位数整体带偏
        if (dt > 0 && dt < 500) {
          perfDt.push(dt);
          if (perfDt.length > 90) perfDt.shift();
        }
      }
      perfLastAt = now;
      paintPerf();
      perfRaf = requestAnimationFrame(loop);
    };
    perfRaf = requestAnimationFrame(loop);
  }

  function stopPerfLoop() {
    if (perfRaf) cancelAnimationFrame(perfRaf);
    perfRaf = 0;
  }

  // 播放循环每帧报一次：绘制耗时 + 这一帧的时间戳（用来算真正画出来的帧率）
  function perfCostSample(cost) {
    if (!state.perfPanel) return;
    perfCost.push(cost);
    if (perfCost.length > 90) perfCost.shift();
    const t = performance.now();
    perfStamp.push(t);
    if (perfStamp.length > 120) perfStamp.shift();
  }

  function perfMedian(arr) {
    if (!arr.length) return 0;
    const s = arr.slice().sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  }

  // 用最近若干帧的间隔算"程序实际渲染帧率"
  function perfRenderFps() {
    if (perfStamp.length < 8) return 0;
    const gaps = [];
    for (let i = 1; i < perfStamp.length; i += 1) gaps.push(perfStamp[i] - perfStamp[i - 1]);
    const m = perfMedian(gaps);
    return m > 0 ? 1000 / m : 0;
  }

  function paintPerf() {
    const now = performance.now();
    if (now - perfPaintedAt < 400) return;   // 4Hz 就够看，写太勤反而看不清数字
    perfPaintedAt = now;
    const el = $("perfPanel");
    if (!el) return;
    const medDt = perfMedian(perfDt);
    const render = perfRenderFps();
    const notes = (state.data && (state.data.notes || []).length) || 0;
    // 撑不住时 noteFrame 会把 frameMs 降一档，这里说清楚，免得和设置里选的档位对不上
    const dropped = frameMs > capToMs(state.fpsCap) + 0.01;
    const target = Math.round(1000 / frameMs);
    const targetText = dropped
      ? target + " fps（已降档）"
      : (state.fpsCap === 0 ? "无限制（≈ " + target + " fps）" : target + " fps");
    el.textContent =
      "性能面板" + (state.playing ? " · 播放中" : "") + "\n" +
      "渲染     " + (render > 0 ? render.toFixed(1) + " fps" : "—") + "\n" +
      "绘制     " + (perfCost.length ? perfMedian(perfCost).toFixed(2) + " ms" : "—") + "\n" +
      "刷新     " + (medDt > 0 ? (1000 / medDt).toFixed(0) + " Hz" : "—") + "\n" +
      "目标     " + targetText + "\n" +
      "音符     " + notes;
  }

  // 轻量自报：用 GET 把一个事件打到服务端日志里（GET 在浏览器上传被挡时仍然可用）
  function beacon(ev, data) {
    try {
      const qs = "ev=" + encodeURIComponent(ev) + "&data=" + encodeURIComponent(
        typeof data === "string" ? data : JSON.stringify(data || {})
      );
      const img = new Image();
      img.src = "/api/beacon?" + qs + "&_=" + Date.now();
    } catch (e) {
      /* 上报失败不影响功能 */
    }
  }
  window.__beacon = beacon;

  // ---------------------------------------------------------------- 主题色（给 canvas 模块用）
  // 读法只有一份（UI.readTheme）：在线页与单文件报告要显示同样的颜色，
  // 各留一套实现迟早会只改好其中一边。
  const readTheme = window.UI.readTheme;

  // ---------------------------------------------------------------- 心跳 / 关页即退服务
  // 这是个从浏览器打开的本地工具：页面关掉了，后台那个 python 进程就没必要留着。
  // 页面每隔 10 秒报一次到，关闭时用 sendBeacon 发一个 bye，服务端据此自动退出。
  // 两个细节：
  //   * 后台标签页的定时器会被浏览器降到约 1 分钟一次，所以服务端的超时给到 75 秒；
  //   * 刷新会先发 bye 再重新加载，新页面几毫秒内就报到，不会被误杀。
  var CLIENT_ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

  // 连续几次报不到就说明后台进程没了（重启、崩了、被别的东西关了）。
  // 之前这里只是静默吞掉，页面会停在旧状态：设置页读不到配置、输入框空着，
  // 用户以为是"没保存"，其实只是这个页面已经连不上服务了。
  var lostCount = 0;
  function heartbeat() {
    try {
      fetch("/api/alive?c=" + CLIENT_ID, { cache: "no-store", keepalive: true })
        .then(function (r) {
          if (!r.ok) throw new Error("bad");
          if (lostCount >= 2) {
            try { window.UI.toast("已重新连上服务"); } catch (e) { /* 界面还没就绪 */ }
          }
          lostCount = 0;
        })
        .catch(function () {
          lostCount += 1;
          if (lostCount === 2) {
            try {
              window.UI.toast("连不上服务了：重新打开程序后按 F5 刷新本页", true);
            } catch (e) { /* 界面还没就绪 */ }
          }
        });
    } catch (e) { /* 忽略 */ }
  }
  heartbeat();
  setInterval(heartbeat, 10000);
  window.addEventListener("pagehide", function () {
    try { navigator.sendBeacon("/api/bye?c=" + CLIENT_ID); } catch (e) { /* 忽略 */ }
  });

  // ---------------------------------------------------------------- 标签页
  // 这些页签的数据全部来自回放分析结果。没导入回放时点进去只有空壳，
  // 所以直接把入口收起来，只留「导入（总览）/ 成长 / 设置 / 关于」。
  const DATA_TABS = ["timeline", "errors", "replay", "chart", "details"];

  function syncTabLocks() {
    const hasData = !!state.data;
    document.querySelectorAll(".rail-btn[data-tab]").forEach((b) => {
      const tab = b.dataset.tab;
      // 「谱面难度」还有另一条入口：只导入 .osu 时它是有内容的，要留着
      if (tab === "chart") {
        b.hidden = !(hasData || state.chartOnly);
        return;
      }
      b.hidden = !hasData && DATA_TABS.indexOf(tab) >= 0;
    });
  }

  function switchTab(name) {
    // 兜底：热键、旧链接、或者刚清掉数据时仍可能喊到这些页签
    if (!state.data && DATA_TABS.indexOf(name) >= 0) name = "overview";
    // 只看谱面时那页叫 chartonly；点回「谱面难度」要落到它上面，否则是空壳
    if (!state.data && state.chartOnly && name === "chart") name = "chartonly";
    syncTabLocks();
    state.tab = name;
    document.querySelectorAll(".rail-btn").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.tab === name);
    });
    ["overview", "timeline", "errors", "replay", "chart", "chartonly", "finder", "details", "progress", "settings", "about"].forEach((t) => {
      const p = $("panel-" + t);
      if (p) p.hidden = t !== name;
    });
    // 「关于」「设置」「成长」不依赖分析结果：没导入回放时也要能看；「只看谱面」同理
    $("emptyState").hidden = !!state.data || name === "about" || name === "chartonly"
      || name === "settings" || name === "progress" || name === "finder";
    if (name === "about") checkUpdate(false);
    if (name === "progress") loadProgress();
    if (name === "settings") {
      loadPathSettings();
      loadHotkeySettings();
    }
    // 切到某页时补一次重绘（隐藏时 canvas 尺寸是 0）
    if (state.data || state.chartOnly) {
      // 播放器等到真的要看回放页再建：几万物件时 load() 要复制一份 notes，
      // 放在首屏会和总览渲染抢主线程。
      if (name === "replay") ensurePlayer();
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          drawChartsForTab(name);
          if (name === "replay") resizePlayer();
        });
      });
      // 兜底：有些情况下第一帧量不到宽度（比如窗口刚从后台恢复）
      setTimeout(() => drawChartsForTab(name), 180);
    }
  }

  // ---------------------------------------------------------------- 折叠
  function bindFolds() {
    document.querySelectorAll(".fold-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const card = document.getElementById(btn.dataset.fold);
        if (card) {
          card.classList.toggle("is-folded");
          if (!card.classList.contains("is-folded")) {
            requestAnimationFrame(() => drawChartsForTab(state.tab));
          }
        }
      });
    });
  }

  // ---------------------------------------------------------------- 图表
  function drawChartsForTab(tab) {
    if (!state.data && !state.chartOnly) return;
    const Charts = window.Charts;
    if (!Charts) return;
    const d = state.data || state.chartOnly;
    const theme = readTheme();

    const draw = (id, fn) => {
      const c = $(id);
      if (!c || !c.clientWidth) return;
      try {
        fn(c);
      } catch (e) {
        console.warn("chart error", id, e);
      }
    };

    if (tab === "overview") {
      draw("chartHist", (c) =>
        Charts.drawHistogram(c, d.offsetHistogram, { theme, width: c.clientWidth, height: 200, median: d.summary.medianDelta })
      );
      // 本图走势：数据在 ui.js 里，切页/改窗口时按当前宽度重画
      if (window.UI.drawProgressChart) window.UI.drawProgressChart();
    }
    if (tab === "timeline") {
      draw("chartTimeline", (c) =>
        Charts.drawTimeline(c, d.timeline, {
          theme,
          width: c.clientWidth,
          height: 300,
          markTime: currentTimeMs(),
          startMs: songStartMs(),  // 和回放页同一个口径：砍掉没有物件的前奏
          avgUr: d.summary.ur,     // 本次游玩的平均 UR，画成参考线
          onSeek: (t) => seekTo(t, true),
        })
      );
      draw("chartDensity", (c) =>
        Charts.drawDensity(c, d.categories.profile, {
          theme,
          width: c.clientWidth,
          height: 220,
        })
      );
    }
    if (tab === "chart") {
      // 谱面难度页：只有一张雷达图，画在 tab 切换的下一帧（隐藏时 canvas 宽度是 0）
      draw("chartRadar", (c) =>
        Charts.drawRadar(c, d.msd && d.msd.available ? d.msd.skillsets : null, {
          theme,
          width: c.clientWidth,
          height: 380,
        })
      );
      return;
    }
    if (tab === "chartonly") {
      draw("coRadar", (c) =>
        Charts.drawRadar(c, d.msd && d.msd.available ? d.msd.skillsets : null, {
          theme,
          width: c.clientWidth,
          height: 360,
        })
      );
      return;
    }

    if (tab === "details") {
      draw("chartColumns", (c) =>
        Charts.drawColumns(c, d.columnStats, { theme, width: c.clientWidth, height: 240 })
      );
    }
  }

  function redrawAllCharts() {
    drawChartsForTab(state.tab);
  }

  // ---------------------------------------------------------------- 回放对比
  // 两路回放并排同步播放：左边是当前分析的回放（主），右边是上传的标杆（辅助）。
  // 只在"同一张谱面 + 同样 mods/倍率"时才允许对比 —— 否则分差/acc 差没有意义。

  // V1 计分的判定表（与后端 analyze.py 的 V1_SCORE_TABLE 一致）
  const V1_W = [1, 1, 2 / 3, 1 / 3, 1 / 6, 0];          // acc 权重
  const V1_HV = [320, 300, 200, 100, 50, 0];
  const V1_HVB = [32, 32, 16, 8, 4, 0];
  const V1_HB = [2, 1, 0, 0, 0, 0];
  const V1_HP = [0, 0, 8, 24, 44, Infinity];

  // 当前判定口径下的判定码（0..5）。后端把两套都放在同一条 note 上：
  // j 是 V1，j2 是 V2，播放器与实时前缀和都从这里取。
  function jCodeOf(note) {
    const v = state.judgeMode === "classic"
      ? note.jc
      : (state.judgeMode === "v2" ? note.j2 : note.j);
    return v >= 0 && v <= 5 ? v | 0 : 5;
  }

  // 把一份 notes 折成前缀和：任意时刻 t 都能 O(log n) 拿到累计 acc 与累计分
  function buildProgress(notes) {
    const n = notes.length;
    if (!n) return null;
    const unit = 1000000 * 0.5 / n;
    const ts = new Float64Array(n);
    const accW = new Float64Array(n);
    const score = new Float64Array(n);
    // 逐判定的累计个数（6 档），对比时用来显示"MAX −x / 300 +x"这种细节差
    const counts = [0, 1, 2, 3, 4, 5].map(() => new Float64Array(n));
    const run = [0, 0, 0, 0, 0, 0];
    let bonus = 100;
    let w = 0;
    let s = 0;
    for (let i = 0; i < n; i += 1) {
      const c = jCodeOf(notes[i]);
      run[c] += 1;
      for (let k = 0; k < 6; k += 1) counts[k][i] = run[k];
      if (c === 5) bonus = 0;
      else bonus = Math.min(100, Math.max(0, bonus + V1_HB[c] - V1_HP[c]));
      s += unit * (V1_HV[c] / 320) + unit * (V1_HVB[c] * Math.sqrt(bonus) / 320);
      w += V1_W[c];
      ts[i] = notes[i].t;
      accW[i] = w;
      score[i] = s;
    }
    return { ts, accW, score, counts, total: n };
  }

  // 取"打到 t 为止"的累计值（t 之前最后一个物件）
  function progressAt(prog, t) {
    if (!prog) return { acc: 0, score: 0, done: 0, counts: [0, 0, 0, 0, 0, 0] };
    const ts = prog.ts;
    let lo = 0;
    let hi = ts.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid + 1;
      else hi = mid;
    }
    if (lo === 0) return { acc: 0, score: 0, done: 0, counts: [0, 0, 0, 0, 0, 0] };
    return {
      acc: (prog.accW[lo - 1] / prog.total) * 100,
      score: prog.score[lo - 1],
      done: lo,
      counts: [0, 1, 2, 3, 4, 5].map((k) => prog.counts[k][lo - 1]),
    };
  }

  function closeCompare() {
    if (state.player2) {
      try {
        state.player2.destroy();
      } catch (e) {
        /* 已经没了就算了 */
      }
      state.player2 = null;
    }
    state.compare = null;
    state.prog2 = null;
    const slot2 = $("pfSlot2");
    if (slot2) slot2.hidden = true;
    const wrap = $("playfieldWrap");
    if (wrap) wrap.classList.remove("is-compare");
    const panel = $("panel-replay");
    if (panel) panel.classList.remove("is-compare");
    const badge1 = $("pfBadge1");
    if (badge1) badge1.hidden = true;
    resizePlayer();
    updateNowPanel(currentTimeMs());
  }

  async function startCompare(file) {
    if (!file || !state.data) return;
    const ref = state.data.replay || {};
    let data2;
    try {
      window.UI.loading(true, "正在分析对比回放…");
      const b64 = await window.API.fileToBase64(file);
      data2 = await window.API.analyzeUpload({ replay: b64 });
    } catch (e) {
      window.UI.toast("对比回放解析失败：" + (e && e.message ? e.message : e), true);
      return;
    } finally {
      window.UI.loading(false);
    }

    // 两个必要条件：同一张谱面；同样 mods + 倍率
    const ref2 = data2.replay || {};
    if (!ref2.beatmapMd5 || ref2.beatmapMd5 !== ref.beatmapMd5) {
      window.UI.toast("不是同一张谱面，无法对比", true);
      return;
    }
    const m1 = (ref.modsList || []).slice().sort().join(",");
    const m2 = (ref2.modsList || []).slice().sort().join(",");
    if (m1 !== m2 || Number(ref.rate) !== Number(ref2.rate)) {
      window.UI.toast("两份回放的 mods / 倍率不同，分差与 acc 不可比", true);
      return;
    }

    closeCompare();
    state.compare = data2;
    state.prog1 = buildProgress(state.data.notes || []);
    state.prog2 = buildProgress(data2.notes || []);

    const slot2 = $("pfSlot2");
    if (slot2) slot2.hidden = false;
    const wrap = $("playfieldWrap");
    if (wrap) wrap.classList.add("is-compare");
    const panel = $("panel-replay");
    if (panel) panel.classList.add("is-compare");   // 让整块网格变宽，两个画布才有地方
    const badge1 = $("pfBadge1");
    if (badge1) badge1.hidden = false;

    state.player2 = window.ManiaPlayer.create($("playfield2"), {
      scrollMs: state.scrollMs,
      judgementFilter: state.errorsOnly ? "errors" : "all",
      showJudgements: true,
      // 标杆这一路：判定色固定成白/黄/绿/蓝/紫/红，并且不画"实际击打位置"——
      // 它是参照物，画太多细节反而压过左边真正要分析的那份
      fixedJudgeColors: true,
      showPresses: false,
      theme: readTheme(),
    });
    state.player2.load({
      keys: data2.beatmap.keys,
      notes: state.judgeMode === "v2"
        ? (data2.notes || []).map((n) => Object.assign({}, n, { j: n.j2 }))
        : data2.notes,
      firstObject: data2.beatmap.firstObject,
      lastObject: data2.beatmap.lastObject,
    });
    state.player2.resize();
    state.player2.setTime(currentTimeMs());
    state.player2.render();
    resizePlayer();
    updateNowPanel(currentTimeMs());
    window.UI.toast("已开启对比：左＝本局，右＝标杆");
  }

  // ---------------------------------------------------------------- 回放点选
  // 「此刻」卡片只能提前告诉你"下一条失误在几秒后"，那一刻要是三四个物件一起错，
  // 光看卡片根本读不过来。所以允许直接在画面上点那个物件看原因。
  function findErrorAt(t, column) {
    const errs = (state.data && state.data.errors) || [];
    let best = null;
    let bestD = 999;
    errs.forEach((e) => {
      if (e.column !== column) return;
      const d = Math.abs(e.t - t);
      if (d <= 1.5 && d < bestD) {
        best = e;
        bestD = d;
      }
    });
    return best;
  }

  // 画布上只把"压着失误物件"的地方做成手型，普通音符不响应，避免误以为是可点的
  function bindReplayHover(canvas) {
    canvas.addEventListener("mousemove", (ev) => {
      if (!state.player || !state.data) return;
      const rect = canvas.getBoundingClientRect();
      const hit = state.player.pickAt(ev.clientX - rect.left, ev.clientY - rect.top);
      canvas.classList.toggle("is-pick", !!hit);
    });
    canvas.addEventListener("mouseleave", () => {
      canvas.classList.remove("is-pick");
    });
  }

  // 回放页滚轮 = 小幅快进/快退，而不是滚页面。
  // 暂停时滚完仍然保持暂停：这里只调 seekTo(..., false)，不碰播放状态。
  function bindReplayWheel() {
    const panel = $("panel-replay");
    if (!panel || panel.dataset.wheelBound) return;
    panel.dataset.wheelBound = "1";
    let acc = 0;
    panel.addEventListener(
      "wheel",
      (ev) => {
        if (!state.data) return; // 没载入回放时保持页面正常滚动
        ev.preventDefault();
        // 鼠标一格通常是 ±100，触控板会给出几十的碎值：累加起来再走，碎值也能平滑推进
        acc += ev.deltaY;
        const step = Math.max(-240, Math.min(240, acc));
        if (Math.abs(step) < 8) return;
        acc = 0;
        seekTo(currentTimeMs() + step, false);
      },
      { passive: false }
    );
  }

  // 把「失误」清单里对应那一条闪一下。清单默认只渲染前 50 条，
  // 目标排在后面时先展开再闪 —— 否则用户切过去会找不到。
  function revealErrorRow(err) {
    const ul = $("errorList");
    const key = Math.round(err.t) + ":" + err.column;
    if (ul && ul.querySelector('[data-key="' + key + '"]')) {
      window.UI.flashError(err.t, err.column);
      return;
    }
    const all = filteredErrors();
    const idx = all.indexOf(err);
    if (idx < 0) return; // 被筛选条件挡住了，不去动清单
    state.errorShown = idx + 1;
    renderErrorList();
    window.UI.flashError(err.t, err.column);
  }

  // 选中/取消选中一条失误：画面上的物件套高亮圈，右侧「此刻」卡片钉住它的错因。
  // 不用弹窗 —— 弹层会挡住画面，而且点完就消失，反而看不清。
  function selectError(payload) {
    if (!payload || !payload.note) return;
    const err = findErrorAt(payload.note.t, payload.note.c);
    if (!err) {
      clearSelection();
      return;
    }
    if (state.selectedError && state.selectedError.t === err.t && state.selectedError.column === err.column) {
      clearSelection();   // 点同一条 = 取消
      return;
    }
    if (state.playing) pause();   // 播放中物件一直在滚，钉住才看得清
    state.selectedError = err;
    if (state.player && state.player.setSelected) state.player.setSelected(err.t, err.column);
    repaintPlayer();
    updateNowPanel(currentTimeMs());
    revealErrorRow(err);
  }

  function clearSelection() {
    if (!state.selectedError) return;
    state.selectedError = null;
    if (state.player && state.player.setSelected) state.player.setSelected(null);
    repaintPlayer();
    updateNowPanel(currentTimeMs());
  }

  function repaintPlayer() {
    if (!state.player) return;
    state.player.setTime(currentTimeMs());
    state.player.render();
  }

  // ---------------------------------------------------------------- 打击音
  // AudioContext 要等用户真的点过播放才允许出声，所以实例建得晚、建一次就复用。
  let hitsoundInst = null;
  function ensureHitsound() {
    if (hitsoundInst) return hitsoundInst;
    if (!window.HitSound) return null;
    hitsoundInst = window.HitSound.create();
    return hitsoundInst;
  }

  // 把当前模式/音量推给播放器与音效引擎。切模式、拖音量、点静音都走这里，
  // 免得三处各写一遍、日后漏掉一处就出现"看着开着但没声音"。
  function syncHitsound() {
    const on = state.hitsoundMode !== "off";
    const hs = on ? ensureHitsound() : hitsoundInst;
    if (hs) {
      hs.setVolume(state.muted ? 0 : state.volume);
      hs.setHitVolume(state.hitsoundVolume);
      if (on) hs.unlock(state.muted ? 0 : state.volume);
    }
    if (state.player) {
      state.player.setOptions({
        hitsound: on ? hs : null,
        hitsoundMode: state.hitsoundMode,
      });
    }
    // 对比模式那一路不出声：两路同时响会变成两份叠在一起的节奏，反而听不清哪边有问题
    if (state.player2) state.player2.setOptions({ hitsound: null, hitsoundMode: "off" });
  }

  // ---------------------------------------------------------------- 播放器
  // 播放器实例只 create 一次，但每次分析都必须重新 load 音符：
  // 换第二份回放时实例已经存在，只更新选项不会换掉上一局的音符。
  function ensurePlayer() {
    if (!state.data || !window.ManiaPlayer) return;
    const canvas = $("playfield");
    if (!canvas) return;
    const opts = {
      scrollMs: state.scrollMs,
      judgementFilter: state.errorsOnly ? "errors" : "all",
      showJudgements: true,
      theme: readTheme(),
      hitsound: state.hitsoundMode === "off" ? null : ensureHitsound(),
      hitsoundMode: state.hitsoundMode,
    };
    if (!state.player) {
      state.player = window.ManiaPlayer.create(canvas, opts);
      if (state.player.on) {
        state.player.on("seek", (t) => {
          clearSelection();
          seekTo(t, true);
        });
        state.player.on("pick", selectError);
      }
      bindReplayHover(canvas);
      bindReplayWheel();
    } else {
      state.player.setOptions(opts);
    }
    state.player.load({
      keys: state.data.beatmap.keys,
      // 播放器只认 note.j：切到 V2 时把 j2 换到 j 上，播放器本身不必知道有两套口径
      notes: state.judgeMode === "v2"
        ? state.data.notes.map((n) => Object.assign({}, n, { j: n.j2 }))
        : state.data.notes,
      firstObject: state.data.beatmap.firstObject,
      lastObject: state.data.beatmap.lastObject,
    });
    applyNoteSkin(state.data);
    syncHitsound();
    state.player.resize();
    state.player.setTime(currentTimeMs());
    state.player.render();
  }

  // 把服务端找好的皮肤素材（每列一张图 + 长按条）交给播放器。
  // 找不到素材时 noteImages 全是 null，播放器会自己退回色块。
  function applyNoteSkin(data) {
    if (!state.player) return;
    const media = (data && data.media) || {};
    const urls = media.noteImages;
    if (!urls || !urls.some(Boolean)) return;
    try {
      state.player.setNoteImages(urls, media.lnBody || null, media.lnTail || null);
    } catch (e) {
      console.warn("皮肤素材加载失败，继续使用色块", e);
    }
  }

  function resizePlayer() {
    ensurePlayer();
    if (state.player) {
      state.player.setTime(currentTimeMs());
      state.player.resize();
      state.player.render();
    }
    if (state.player2) {
      state.player2.setTime(currentTimeMs());
      state.player2.resize();
      state.player2.render();
    }
  }

  // ---------------------------------------------------------------- 时间与播放
  //
  // 时间基准（这里是最容易出错、也最容易被忽略的地方）：
  // 音符时间、音频时间都是「歌曲绝对时间」——第一个音符可能在第 30 秒。
  // 但 payload 里的 beatmap.duration 是**相对**时长（最后一个物件 − 第一个物件）。
  // 直接拿相对值当进度条终点和自动停止线，整条时间轴就会相对音频平移
  // firstObject 毫秒（前奏一长就是几十秒），表现是「音频和谱面对不上、延迟很大」。
  // 所以这里一律按绝对时间算，再用音频真实长度兜住上限。
  function songEndMs() {
    if (!state.data) return 1;
    const bm = state.data.beatmap || {};
    const last = Number(bm.lastObject || 0);
    const relEnd = Number(bm.duration || 0) + Number(bm.firstObject || 0);
    let end = Math.max(last, relEnd) + 1500;
    if (state.audio && isFinite(state.audio.duration) && state.audio.duration > 0) {
      end = Math.min(end, state.audio.duration * 1000);
    }
    return Math.max(1, end);
  }

  // 前奏太长时按播放直接跳到第一个音符前 1.5 秒，不然要干等几十秒的空白
  function songStartMs() {
    if (!state.data) return 0;
    return Math.max(0, Number(state.data.beatmap.firstObject || 0) - 1500);
  }

  function durationMs() {
    return songEndMs();
  }

  // 音画偏移：谱面时间 = 音频时间 + offset。
  // offset 为正 → 谱面跑在音频前面（音符更早到达判定线）；为负则更晚。
  function audioUsable() {
    // 音频元素在，但播放被拒 / 加载失败过 —— 这时不能再拿它的 currentTime 当时间基准，
    // 否则画面会一直停在原地（音频没走，currentTime 也不动）。
    return !!(state.audio && !state.audioBroken && state.audio.readyState >= 1);
  }

  function currentTimeMs() {
    if (audioUsable()) return state.audio.currentTime * 1000 + state.audioOffset;
    return state.virtualTime;
  }

  function seekTo(ms, autoPlay) {
    const clamped = Math.max(0, Math.min(durationMs(), ms));
    state.virtualTime = clamped;
    if (state.audio) {
      try {
        // 反解：要显示到谱面时间 clamped，音频得待在 clamped − offset
        state.audio.currentTime = Math.max(0, (clamped - state.audioOffset) / 1000);
      } catch (e) {
        /* 元数据还没加载好，忽略 */
      }
    }
    if (state.player) {
      state.player.setTime(clamped);
      state.player.render();
    }
    if (state.player2) {
      state.player2.setTime(clamped);
      state.player2.render();
    }
    updateTransport();
    updateNowPanel(clamped);
    updateTimelineMark();
    if (autoPlay) play();
  }

  function togglePlay() {
    if (state.playing) pause();
    else play();
  }

  function play() {
    if (!state.data) return;

    // 前奏跳过：谱面第一个音符在很后面时，从 0 开始要空等很久，看起来就像"音频和画面没关系"
    const start = songStartMs();
    if (start > 3000 && currentTimeMs() < start - 200) {
      seekTo(start, false);
      window.UI.toast("前奏较长，已从 " + fmtTime(start) + " 开始播放");
    }

    state.playing = true;
    setPlayIcon(true);
    // 打击音要在"用户手势"里解锁，点播放是最自然的一次。
    // 没开音效时这里什么也不做，不会白建一个 AudioContext。
    if (state.hitsoundMode !== "off") {
      const hs = ensureHitsound();
      if (hs) hs.unlock(state.muted ? 0 : state.volume);
    }
    if (state.audio) {
      state.audio.play().then(() => {
        const wasBroken = state.audioBroken;
        state.audioBroken = false;
        if (wasBroken) {
          // 音频播放失败时画面走的是虚拟时钟，位置已经和音频本体脱开了。
          // 直接开响会从这个错位继续播，所以先把音频拉回当前画面位置。
          try {
            state.audio.currentTime = Math.max(0, (state.virtualTime - state.audioOffset) / 1000);
          } catch (e) { /* ignore */ }
        }
      }).catch(() => {
        // 浏览器拦截 / 音频损坏：退化成虚拟时钟（此时画面照常走，只是没声音）
        state.audioBroken = true;
        startVirtualClock();
      });
    } else {
      state.audioBroken = false;
      startVirtualClock();
    }
    state.lastFrame = performance.now();
    nextFrameAt = state.lastFrame + frameMs;
    scheduleNext();
  }

  function startVirtualClock() {
    if (!state.playing) return;
    // 有音频但播放失败时，虚拟时钟从"当前音频位置"接着走，
    // 否则画面会停在 0 而音频已经播到一半（另一种"对不上"）。
    if (state.audio && state.audio.readyState >= 1) {
      state.virtualTime = state.audio.currentTime * 1000 + state.audioOffset;
    }
    if (state.virtualTime >= durationMs()) state.virtualTime = 0;
  }

  function pause() {
    state.playing = false;
    stopScheduler();
    setPlayIcon(false);
    if (state.audio) state.audio.pause();
  }

  // 播放键有两处（回放页 / 时间轴页），图标要一起翻 —— 否则在一页暂停、
  // 另一页的按钮还显示"暂停中"。
  function setPlayIcon(playing) {
    const pauseSvg = '<rect x="7.5" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/><rect x="13.1" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/>';
    const playSvg = '<path d="M8 5.5v13l11-6.5-11-6.5Z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>';
    ["icoPlay", "icoPlayTl"].forEach((id) => {
      const ico = $(id);
      if (ico) ico.innerHTML = playing ? pauseSvg : playSvg;
    });
  }

  // ---- 画面刷新：上限取设置里的「回放帧率上限」（30 / 60 / 90 / 120 / 180 / 240 / 无限制）----
  // requestAnimationFrame 只在显示器刷新时回调，60Hz 屏就是 60 次/秒 —— 对音游分析来说，
  // 快速拖动时每帧跨过的距离太大（1200ms 窗口 / 60fps ≈ 每帧 8px），看着是"跳"的。
  // 所以：RAF 照用，另外补一个 frameMs 的定时器，让"计算 + 重绘"也能超过刷新率；
  // 两条路径共用 lastFrame 闸门，不会重复画同一帧。
  // 0 表示"无限制"：定时器本身有 4ms 下限，实际天花板约 250fps，
  // 这里就按 250 当目标来节流和判断要不要降档。
  const FPS_CAPS = [30, 60, 90, 120, 180, 240, 0];
  const UNLIMITED_FPS = 250;
  let frameMs = 1000 / 120;       // 当前实际节拍（降档后比设置值慢）
  let fallbackMs = 1000 / 60;     // 撑不住时退到这一档，只降不升
  let rafId = 0;
  let timerId = 0;
  let frameSamples = [];

  function capToMs(cap) {
    return 1000 / (cap === 0 ? UNLIMITED_FPS : cap);
  }

  // 撑不住时退到哪一档：无限制退 240（它没有更高的档可退，退一档反而更稳），
  // 其余退一半，但不低于 30。
  function capFallbackMs(cap) {
    if (cap === 0) return 1000 / 240;
    return 1000 / Math.max(30, cap / 2);
  }

  function syncFpsCapUI() {
    document.querySelectorAll("#fpsCapToggle [data-fps]").forEach((btn) => {
      btn.classList.toggle("is-active", Number(btn.dataset.fps) === state.fpsCap);
    });
  }

  // 换档要立刻生效：正在播时重排下一帧的时间点，
  // 否则得等旧节拍走完才换速，看起来像卡了一下。
  function applyFpsCap(cap, opts) {
    const next = FPS_CAPS.indexOf(Number(cap)) >= 0 ? Number(cap) : 120;
    state.fpsCap = next;
    frameMs = capToMs(next);
    fallbackMs = capFallbackMs(next);
    frameSamples = [];
    if (!(opts && opts.silent)) savePrefs();
    syncFpsCapUI();
    if (state.playing) {
      nextFrameAt = performance.now() + frameMs;
      scheduleNext();
    }
  }

  // 用最近 40 帧的**中位间隔**判断"撑不撑得住"：
  // 偶尔掉一帧不算，连续 40 帧都远超目标才降级（且只降一次，不再回弹）
  function noteFrame(dt) {
    // 超过 500ms 的那种间隔是切后台/最小化回来，不是"机器慢"，记进去会把中位数带偏。
    // 原来按 frameMs*4 过滤，240 档等于只收 16ms 以内的帧——真慢到 30fps 反而被当成噪声丢掉，
    // 降档永远不触发。
    if (!(dt > 0) || dt > 500) return;
    frameSamples.push(dt);
    if (frameSamples.length > 40) frameSamples.shift();
    if (frameSamples.length < 40 || frameMs >= fallbackMs - 0.01) return;
    const sorted = frameSamples.slice().sort((a, b) => a - b);
    if (sorted[20] > frameMs * 1.5) {
      frameMs = fallbackMs;
      frameSamples = [];
    }
  }

  function stopScheduler() {
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = 0;
    }
    if (timerId) {
      clearTimeout(timerId);
      timerId = 0;
    }
  }

  // 用"下一个绝对时刻"来排期，而不是每次 setTimeout(6.94)：
  // 后者会把每帧的绘制耗时也算进间隔里（实测只跑到 ~123fps），前者会把误差补回来。
  let nextFrameAt = 0;

  function scheduleNext() {
    stopScheduler();
    // 两条路都排：RAF 跟着屏幕走，定时器补 RAF 之间的空隙（高帧率档就靠它）
    rafId = requestAnimationFrame((t) => tick(t, false));
    const wait = Math.max(0, nextFrameAt - performance.now());
    timerId = setTimeout(() => tick(performance.now(), true), wait);
  }

  function tick(now, fromTimer) {
    if (!state.playing) {
      stopScheduler();
      return;
    }
    // 这一帧离上一帧太近：丢掉，等下一拍。两个来源的容差不一样：
    //   · 定时器：浏览器会把小数延迟截断/合并到整数毫秒，11.11ms 的请求常在 11.0 就来，
    //     容差给 2ms 才不会把自己的帧全拒掉（拒掉的代价是退到下一个 RAF，90 档会掉成 66.7）。
    //   · RAF：容差必须很小，否则它会抢在定时器前面把帧画掉 —— 200Hz 屏上 60 档的 RAF
    //     落在 15ms 处，容差一大就跑成 66.7fps。
    const tolerance = fromTimer ? 2 : 0.5;
    if (now - state.lastFrame < Math.max(0.5, frameMs - tolerance)) {
      scheduleNext();
      return;
    }
    const dt = Math.max(0, now - state.lastFrame);
    state.lastFrame = now;
    noteFrame(dt);
    const perfT0 = performance.now();
    if (!audioUsable()) {
      state.virtualTime += dt * state.speed;
    }
    const t = currentTimeMs();
    // 音频自己结束了也要停：否则画面会卡在最后一帧继续空转
    if (state.audio && state.audio.ended) {
      pause();
      updateTransport();
      return;
    }
    if (t >= durationMs()) {
      state.virtualTime = durationMs();
      pause();
      updateTransport();
      return;
    }
    if (state.player) {
      state.player.setTime(t);
      state.player.render();
    }
    if (state.player2) {
      // 两路必须吃同一个时间：差一帧就对不齐
      state.player2.setTime(t);
      state.player2.render();
    }
    updateTransport();
    updateNowPanel(t);
    updateTimelineMark();
    // 这一帧花在"计算 + 绘制"上的时间，不含浏览器合成
    perfCostSample(performance.now() - perfT0);
    // 按绝对时间网格推进：nextFrameAt += frameMs 会把每次的误差补回来。
    // 若写成 now + frameMs，240 档的 4.17ms 会被浏览器的 4ms 定时器下限一直压到 4.0，
    // 实测多跑出 10 帧（250fps）；网格推进则稳定落在 240。
    // 落后太多（切后台回来、机器突然卡）就重新起网格，不然会补一串帧。
    nextFrameAt = now + frameMs;
    scheduleNext();
  }

  // 时间轴上的播放竖线。整幅重绘只在切页签 / 折叠 / 改窗口尺寸时发生，
  // 不在这里补一次的话，播放时竖线会一直杵在原地，只有切走再切回来才跳一下。
  // 走的是"blit 静态快照 + 画一根线"，比整幅重绘便宜两个数量级。
  function updateTimelineMark() {
    if (state.tab !== "timeline") return;
    const Charts = window.Charts;
    const c = $("chartTimeline");
    if (!Charts || !c || !c.clientWidth) return;
    if (typeof Charts.updateTimelineMark !== "function") return;
    if (!Charts.updateTimelineMark(c, currentTimeMs())) {
      // 没有静态图层（还没画过 / 尺寸变了）：退回整幅重绘，但限流，别每帧都来一次
      const now = performance.now();
      if (now - (state._tlFallbackAt || 0) > 250) {
        state._tlFallbackAt = now;
        drawChartsForTab("timeline");
      }
    }
  }

  function updateTransport() {
    const t = currentTimeMs();
    const dur = durationMs();
    const text = fmtTime(t) + " / " + fmtTime(dur);
    $("playTime").textContent = text;
    const tl = $("playTimeTl");
    if (tl) tl.textContent = text;
    // 时间轴上的「此刻 v1」：跟着竖线走，取当前这一小段自己的准确率。
    // 每帧都写 textContent 会触发排版，所以只在数值变化时写。
    const accEl = $("accNowTl");
    if (accEl && state.data) {
      const v = window.UI.accAt(state.data.timeline, t);
      // 两位小数：99.74% 和 99.7% 是两种手感，位宽也够
      const shown = (v === null) ? "—" : v.toFixed(2) + "%";
      if (state._accShown !== shown) {
        state._accShown = shown;
        accEl.textContent = "此刻 " + (state.judgeMode === "v2" ? "v2 " : "v1 ") + shown;
      }
    }
    const pct = Math.max(0, Math.min(100, (t / dur) * 100));
    const seek = $("seek");
    seek.value = String(Math.round(pct * 10));
    seek.style.backgroundSize = pct + "% 100%";
  }

  // 两个滑杆的文字读数 + 已填充进度（range 没有原生"已选"样式，得自己刷 background-size）
  function syncScrollUi() {
    const s = $("scroll");
    if (s) {
      s.value = String(state.scrollMs);
      s.style.backgroundSize = (((state.scrollMs - 300) / 2300) * 100).toFixed(1) + "% 100%";
    }
    const v = $("scrollVal");
    if (v) v.textContent = state.scrollMs + " ms";
  }

  function syncOffsetUi() {
    const s = $("offset");
    if (s) {
      s.value = String(state.audioOffset);
      s.style.backgroundSize = (((state.audioOffset + 500) / 1000) * 100).toFixed(1) + "% 100%";
    }
    const v = $("offsetVal");
    if (v) v.textContent = (state.audioOffset > 0 ? "+" : "") + state.audioOffset + " ms";
  }

  function syncVolumeUi() {
    const pct = Math.round(state.volume * 100);
    const s = $("volume");
    if (s) {
      s.value = String(pct);
      s.style.backgroundSize = pct + "% 100%";
    }
    const v = $("volumeVal");
    if (v) v.textContent = pct + "%";
  }

  // 「打击音」下拉：bindEvents 里先按默认值画过一次，读完本地偏好要再同步一遍，
  // 否则刷新后界面显示「关」，实际状态却还是上次选的那个 —— 看着像没保存。
  function syncHitsoundUi() {
    const sel = $("hitsound");
    if (sel) sel.value = state.hitsoundMode;
  }

  // 打击音自己的音量条（和上面那条回放音量互相独立）
  function syncHitVolUi() {
    const pct = Math.round(state.hitsoundVolume * 100);
    const s = $("hitVol");
    if (s) {
      s.value = String(Math.min(pct, HIT_VOL_MAX * 100));
      // 滑杆量程是 0~300，填充按量程比例算 —— 直接用 pct 会在 100% 时就把整条填满
      s.style.backgroundSize = Math.min(100, pct / HIT_VOL_MAX).toFixed(1) + "% 100%";
    }
    const v = $("hitVolVal");
    if (v) v.textContent = pct + "%";
  }

  // 把音量/静音状态推给音频元素（换歌、改设置、取消静音都要走这里）
  function applyVolume() {
    if (state.audio) {
      state.audio.volume = state.volume;
      state.audio.muted = state.muted;
    }
    // 打击音跟着同一个音量走：只调谱面原声、打击音不跟着变的话，静音时它还在响
    if (hitsoundInst) hitsoundInst.setVolume(state.muted ? 0 : state.volume);
  }

  // 失误按时间预排序一次，播放时二分找最近的那条。
  // 每帧线性扫几千条会在播放时明显吃 CPU，而帧回调里没有做这件事的预算。
  function errorIndex() {
    if (state._errTimes && state._errTimes.length === (state.data.errors || []).length) return state._errTimes;
    const errs = (state.data && state.data.errors) || [];
    state._errTimes = errs.map((e) => e.t);
    return state._errTimes;
  }

  function nearestError(t) {
    const errs = (state.data && state.data.errors) || [];
    if (!errs.length) return null;
    const times = errorIndex();
    let lo = 0;
    let hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (times[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    let best = null;
    let bestD = Infinity;
    for (let i = Math.max(0, lo - 1); i <= Math.min(times.length - 1, lo + 1); i++) {
      const d = Math.abs(times[i] - t);
      if (d < bestD) {
        bestD = d;
        best = errs[i];
      }
    }
    return best ? { err: best, dist: bestD } : null;
  }

  // 一条失误的三个小标签：归因分类、判定、键型。
  // 「此刻」卡片有两条分支（钉住的 / 播放位置附近的那条）都要这套标签，
  // 各拼一遍的话，改配色或改字段时必然只改好一处。
  function errorChips(err) {
    const cat = err.category;
    const catLabel = (CATS[cat] || {}).label || cat;
    const patChip = err.pattern ? '<span class="tag now-pattern">' + err.pattern + "</span>" : "";
    return (
      '<span class="e-cat cat-' + cat + '" style="margin-right:6px"><i></i>' + catLabel + "</span>" +
      '<span class="e-cat j-' + (err.judgement === "miss" ? 5 : 3) + '">' + (err.judgementLabel || "") + "</span>" +
      patChip
    );
  }

  // 对比模式：判定数差值、分差、acc 三张竖排小卡片，不给错因 ——
  // 这时看的是"我和标杆差多少"。三块用同一种小卡片、纵向排开，
  // 在窄栏（约 258px）里比并排 chip 好读：数字各占一行，不会挤成一团。
  function renderCompareNow(box, hint, t) {
    const a = progressAt(state.prog1, t);
    const b = progressAt(state.prog2, t);
    const dScore = Math.round(a.score - b.score);
    const dAcc = a.acc - b.acc;
    const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
    const fmtDiff = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toLocaleString("en-US");
    const NAMES = ["MAX", "300", "200", "100", "50", "MISS"];
    const bits = [];
    for (let i = 0; i < 6; i += 1) {
      const d = Math.round(a.counts[i] - b.counts[i]);
      if (d === 0) continue;
      bits.push('<i class="' + tone(d) + '">' + NAMES[i] + " " + fmtDiff(d) + "</i>");
    }
    box.innerHTML =
      '<div class="now-compare">' +
      '<div class="nc"><span>判定数差值（回放1 − 回放2）</span>' +
      '<b class="nc-judge">' + (bits.length ? bits.join("") : '<i class="flat">判定分布一致</i>') + "</b></div>" +
      '<div class="nc"><span>分差</span><b class="' + tone(dScore) + '">' + fmtDiff(dScore) + "</b></div>" +
      '<div class="nc"><span>acc</span><b class="' + tone(dAcc) + '">' +
      (dAcc === 0 ? "0.00" : (dAcc > 0 ? "+" : "−") + Math.abs(dAcc).toFixed(2)) + "%</b></div>" +
      "</div>";
    hint.textContent = "";
    hint.hidden = true;
  }

  // 「此刻」卡片跟着播放每帧刷新，但里面的内容是离散的：
  // 时间数字只在毫秒变化时变，标签只在"最近一条失误"换了之后才需要重建。
  // 之前每帧都 innerHTML 重写一遍，播放时 120 次/秒的 DOM 重建白烧 CPU。
  let nowSig = "";

  function updateNowPanel(t) {
    const timeEl = $("nowTime");
    const txt = (t / 1000).toFixed(3) + "s";
    if (timeEl.textContent !== txt) timeEl.textContent = txt;
    if (!state.data) return;
    const box = $("nowCats");
    const hint = $("nowHint");

    if (state.compare) {
      // 对比模式的三张卡片数值是连续变化的，按 100ms 刷新足够肉眼跟得上
      const sig = "c:" + Math.round(t / 100);
      if (sig === nowSig) return;
      nowSig = sig;
      renderCompareNow(box, hint, t);
      return;
    }
    hint.hidden = false;

    // 手动选中的那条优先：它是用户主动钉住的，不能被"附近恰好有别的失误"抢走
    const pinned = state.selectedError;
    if (pinned) {
      const sig = "p:" + pinned.t + "/" + pinned.category + "/" + (pinned.pattern || "");
      if (sig === nowSig) return;
      nowSig = sig;
      box.innerHTML = errorChips(pinned);
      // 文案直接复用「失误」清单里的原文（原因 + 副文本），不再自己拼"已选中…"
      const reason = (pinned.reasons && pinned.reasons[0]) || "没有足够证据判断原因";
      hint.textContent = reason + " · " + window.UI.errorBits(pinned);
      return;
    }

    const found = nearestError(t);
    const near = found ? found.err : null;
    const best = found ? found.dist : 1e9;
    const inRange = !!(near && best <= 2500);
    const sig = inRange ? "n:" + near.t + "/" + near.category + "/" + (near.pattern || "") : "none";
    if (sig === nowSig) return;
    nowSig = sig;

    if (inRange) {
      const catLabel = (CATS[near.category] || {}).label || near.category;
      box.innerHTML = errorChips(near);
      // 键型取的是这条失误自己的键型（errors[].pattern）——和「失误」页的统计同一个来源，
      // 不是给每个音符单独打标。没有就只显示分类与判定。
      hint.textContent =
        fmtTime(near.t) +
        " 附近有一次" +
        catLabel +
        (near.pattern ? "（" + near.pattern + "）" : "") +
        "： " +
        ((near.reasons && near.reasons[0]) || "");
    } else {
      box.innerHTML = "";
      hint.textContent = "播放时这里会显示最近一次击打的判定与误差。";
    }
  }

  // ---------------------------------------------------------------- 失误清单
  function renderLnStats(data) {
    const card = $("lnCard");
    const box = $("lnStats");
    if (!card || !box) return;
    const stats = data && data.lnStats;
    if (!stats || !stats.total) {
      card.hidden = true;
      box.textContent = "";
      return;
    }
    card.hidden = false;
    box.textContent = "";
    const fmt = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(1) + "ms");

    const head = document.createElement("div");
    head.className = "ln-total";
    const headLabel = document.createElement("span");
    headLabel.textContent = "总平均";
    const headValue = document.createElement("b");
    headValue.textContent = fmt(stats.mean);
    const headCount = document.createElement("em");
    headCount.textContent = stats.total + " 个长条";
    head.appendChild(headLabel);
    head.appendChild(headValue);
    head.appendChild(headCount);
    box.appendChild(head);

    const grid = document.createElement("div");
    grid.className = "ln-cols";
    (stats.columns || []).forEach((col) => {
      const cell = document.createElement("div");
      cell.className = "ln-cell";
      if (col.mean > 0) cell.classList.add("is-late");
      if (col.mean < 0) cell.classList.add("is-early");
      const label = document.createElement("span");
      label.className = "ln-lab";
      label.textContent = (col.column + 1) + " 轨";
      const value = document.createElement("b");
      value.textContent = fmt(col.mean);
      const count = document.createElement("em");
      count.textContent = (col.count || 0) + " 个";
      cell.appendChild(label);
      cell.appendChild(value);
      cell.appendChild(count);
      grid.appendChild(cell);
    });
    box.appendChild(grid);
  }

  function matchesErrorFilters(e) {
    const f = state.errorFilter || {};
    const cat = f.cat || "all";
    if (cat !== "all" && e.category !== cat) return false;
    if (f.severe && e.severity < 3) return false;
    return true;
  }

  function filteredErrors() {
    if (!state.data) return [];
    return (state.data.errors || []).filter((e) => {
      if (!matchesErrorFilters(e)) return false;
      if (state.hideLowConfidence && e.confidence != null && e.confidence < state.confThreshold) {
        return false;
      }
      return true;
    });
  }

  function renderErrorList() {
    const all = filteredErrors();
    const total = (state.data && state.data.errors ? state.data.errors.length : 0);
    const base = (state.data && state.data.errors ? state.data.errors : []).filter(matchesErrorFilters);
    const hidden = state.hideLowConfidence
      ? base.filter((e) => e.confidence != null && e.confidence < state.confThreshold).length
      : 0;
    const shown = all.slice(0, state.errorShown);

    // 每 30 秒一段的条数：上千条时靠时间标题分段，比无脑往下滚好找得多
    const bucketCounts = {};
    all.forEach((e) => {
      const b = Math.floor(e.t / ERROR_BUCKET_MS);
      bucketCounts[b] = (bucketCounts[b] || 0) + 1;
    });

    window.UI.renderErrors(shown, {
      bucketCounts,
      onSeek: (t, err) => {
        // 点一条失误 = "带我去看这一刻"：切到回放页，**停在音符落下前 0.2 秒**。
        // 不自动播放——直接开播会一下冲过这一击，用户得反复往回快进才看得清。
        // 顺带把这条选中，右侧「此刻」卡片会显示它的错因。
        switchTab("replay");
        if (state.playing) pause();
        requestAnimationFrame(() => {
          resizePlayer();
          seekTo(Math.max(0, t - 200), false);
          if (err) selectError({ note: { t: err.t, c: err.column } });
        });
      },
    });

    // 三个数字都从同一份筛选结果实时算：筛选/展开后立刻刷新，
    // 不再依赖"上一次渲染留下的档位数"。
    const sum = $("errorSummary");
    if (sum) {
      const parts = ["共 " + total + " 条"];
      if (hidden) parts.push("已隐藏 " + hidden + " 条低可信度（<" + Math.round(state.confThreshold * 100) + "%）");
      if (all.length !== total) parts.push("当前筛选 " + all.length + " 条");
      parts.push("已显示 " + shown.length + " 条");
      sum.textContent = parts.join(" · ");
    }

    const remain = Math.max(0, all.length - shown.length);
    const moreBox = $("errorMore");
    const more = $("btnMoreErrors");
    const allBtn = $("btnAllErrors");
    if (moreBox) moreBox.hidden = remain <= 0;
    if (more) more.textContent = "再看 " + Math.min(ERROR_PAGE, remain) + " 条（剩 " + remain + " 条）";
    if (allBtn) allBtn.hidden = remain <= ERROR_PAGE;
  }

  function syncThemeUI() {
    document.querySelectorAll("#themeToggle [data-theme]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.theme === state.theme);
    });
  }

  function applyTheme(theme) {
    const next = theme === "sakura" ? "sakura" : "dark";
    state.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    savePrefs();
    syncThemeUI();
    if (state.player) {
      state.player.setOptions({ theme: readTheme() });
      state.player.render();
    }
    if (state.data || state.chartOnly) drawChartsForTab(state.tab);
  }

  // 禁用动画：只挂一个属性到 <html>，样式表那边统一关掉进场与过渡。
  // 关的是"显示效果"，不影响任何逻辑——播放器是 canvas 自绘，照样走自己的帧循环。
  function applyMotionPref(on) {
    state.disableMotion = !!on;
    const root = document.documentElement;
    if (state.disableMotion) root.setAttribute("data-motion", "off");
    else root.removeAttribute("data-motion");
    const cb = $("disableMotion");
    if (cb) cb.checked = state.disableMotion;
  }

  function syncLowConfidenceUI() {
    const cb = $("hideLowConfidence");
    if (cb) cb.checked = state.hideLowConfidence;
    const input = $("confThreshold");
    if (input) input.value = String(Math.round(state.confThreshold * 100));
    const btn = $("btnHideLowConf");
    if (btn) {
      btn.classList.toggle("is-on", state.hideLowConfidence);
      btn.textContent = state.hideLowConfidence ? "显示全部" : "隐藏低可信度";
    }
  }

  function setHideLowConfidence(on) {
    state.hideLowConfidence = !!on;
    savePrefs();
    syncLowConfidenceUI();
    state.errorShown = 50;
    if (state.data) renderErrorList();
  }

  function updateConfThreshold() {
    const input = $("confThreshold");
    if (!input) return;
    let v = Number(input.value);
    if (!Number.isFinite(v)) v = 50;
    v = Math.max(0, Math.min(100, v));
    input.value = String(v);
    state.confThreshold = v / 100;
    savePrefs();
    state.errorShown = 50;
    if (state.data) renderErrorList();
  }

  // ---------------------------------------------------------------- 相似谱面
  // 难度轴是段位，而段位要前端用 Sunny 算（后端没有 JS 运行时），所以这条链是：
  // 「先算段位 → 把段位标签发给后端 → 后端用会话里已有的键型构成去索引里找」。
  // 任何一步失败都只是这张卡片不出现，不影响报告其余部分，也不弹错误。
  // 段位卡片算出来是多少，成长页就该按多少折合。
  // 卡片用的是 Sunny → Azusa → Roxy；写入历史时用的是 MSD 标定，两者能差一两段，
  // 不回填就会出现"页面写 11.17、折合按 12.39 算"的错位。
  function saveDanToHistory(data, res) {
    if (!res || !res.ok) return;
    // Sunny 只给星级，没有段位数值；只有 Azusa / Roxy 才有可比的数
    if (res.method !== "roxy" && res.method !== "azusa") return;
    const value = Number(res.numeric);
    if (!Number.isFinite(value) || value <= 0) return;
    const replay = (data && data.replay) || {};
    const key = String(replay.replayMd5 || replay.fileHash || "").trim();
    if (!key) return;
    window.API.setHistoryDan(key, value).catch(() => { /* 回填失败不影响页面 */ });
  }

  async function loadSimilar(data) {
    const res = await window.UI.applySunnyDan(data);
    if (!res || !res.label || state.data !== data) return;
    saveDanToHistory(data, res);
    const items = await window.API.similar(data.session, res.label);
    if (state.data !== data) return;   // 期间换了回放，别把上一局的结果画上来
    window.UI.renderSimilar(items);
  }

  // 段位与推荐放到首屏之后：Roxy/Azusa 是同步算法，大图上会占住主线程，
  // 用户分析完立刻滚动/点击就会被它挡住（INP 飙高）。空闲时再算，超时兜底。
  function scheduleSimilar(data) {
    const run = () => { void loadSimilar(data); };
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(run, { timeout: 2000 });
    } else {
      setTimeout(run, 350);
    }
  }

  // ---------------------------------------------------------------- 检查更新
  // 打开「关于」页时静默查一次；手动点按钮时失败会 toast。
  // 只请求 GitHub 的 latest release，不上传任何本地数据。
  function checkUpdate(manual) {
    const box = $("aboutUpdate");
    const btn = $("btnCheckUpdate");
    if (!box) return;
    if (state._checkingUpdate) return;
    // 自动检查 5 分钟内不重复请求：来回切页不该反复打 GitHub
    if (!manual && state._updateCheckedAt && Date.now() - state._updateCheckedAt < 5 * 60 * 1000) return;
    state._checkingUpdate = true;
    state._updateCheckedAt = Date.now();
    if (btn) { btn.disabled = true; btn.textContent = "检查中…"; }
    box.hidden = false;
    box.className = "about-update";
    box.textContent = "正在检查…";
    window.API.checkUpdate()
      .then((res) => {
        if (!res || !res.ok) throw new Error((res && res.error) || "检查失败");
        if (res.hasUpdate) {
          box.className = "about-update is-new";
          window.UI.clearNode(box);
          box.appendChild(document.createTextNode("发现新版本 v" + res.latest + " · "));
          const a = el("a", null, "去下载");
          a.href = res.url || "https://github.com/SWt0619/osumania4k_replay_analyser/releases/latest";
          a.target = "_blank";
          a.rel = "noreferrer noopener";
          box.appendChild(a);
          if (manual) window.UI.toast("有新版本 v" + res.latest + "，建议更新");
        } else {
          box.className = "about-update is-latest";
          box.textContent = "已是最新";
        }
      })
      .catch((e) => {
        box.className = "about-update is-error";
        box.textContent = "检查失败：" + (e && e.message ? e.message : "网络不可用");
        if (manual) window.UI.toast("检查更新失败，稍后再试", true);
      })
      .finally(() => {
        state._checkingUpdate = false;
        if (btn) { btn.disabled = false; btn.textContent = "检查更新"; }
      });
  }

  // ---------------------------------------------------------------- 判定口径切换
  // 后端一次分析会把 V1 / V2 两套统计都算好（data.modes），这里只负责换视图。
  // 不重新请求分析：同一份回放的判定结果与统计在两次切换之间必须完全一致。
  function viewOf(data, mode) {
    if (!data || !data.modes || !data.modes[mode]) return data;
    return Object.assign({}, data, data.modes[mode], { judgeMode: mode });
  }

  function updateJudgeToggle() {
    const box = $("judgeToggle");
    if (!box) return;
    box.querySelectorAll("[data-judge]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.getAttribute("data-judge") === state.judgeMode);
    });
  }

  function setJudgeAlgo(algo) {
    if (algo !== "C" && algo !== "D") return;
    if (state.busy) {
      window.UI.toast("正在分析，等这次结束再切");
      return;
    }
    state.judgeAlgo = algo;
    savePrefs();
    const box = $("judgeAlgoToggle");
    if (box) {
      box.querySelectorAll("[data-algo]").forEach((b) => {
        b.classList.toggle("is-active", b.getAttribute("data-algo") === algo);
      });
    }
    const last = state.lastReplay;
    if (!last) {
      window.UI.toast("导入文件后生效");
      return;
    }
    if (last.kind === "local") {
      state.busy = true;
      window.UI.loading(true, "正在重新分析…");
      window.API.analyzeLocal(last.name, algo)
        .then((data) => {
          renderAnalysis(data);
          window.UI.toast("已生效");
        })
        .catch((e) => {
          window.UI.loading(false);
          window.UI.toast((e && e.message) || "重新分析失败", true);
        })
        .finally(() => { state.busy = false; });
    } else if (last.kind === "file") {
      handleReplayFile(last.file);
      window.UI.toast("已生效");
    } else if (last.kind === "quick") {
      // 快捷导入没有本地文件句柄，只能让服务端按新算法把最近一份重算一遍；
      // 结果还是走轮询那条路回来。
      window.API.quickImport(algo, true)
        .then(() => window.UI.toast("已按新算法重新分析"))
        .catch((e) => window.UI.toast((e && e.message) || "重新分析失败", true));
    }
  }

  function setJudgeMode(mode, silent) {
    if (mode !== "v1" && mode !== "v2" && mode !== "classic") return;
    state.judgeMode = mode;
    savePrefs();
    updateJudgeToggle();
    if (!state.baseData) return;

    const t = currentTimeMs();
    state.data = viewOf(state.baseData, mode);
    state._errTimes = null;
    state._accShown = null;
    state.selectedError = null;
    state.errorShown = ERROR_PAGE;
    const view = state.data;

    window.UI.renderHero(view);
    window.UI.renderMeta(view);
    window.UI.renderCategories(view);
    window.UI.renderSections(view);
    window.UI.renderPatternShare(view);
    window.UI.renderJudgeBars(view);
    window.UI.renderRawTable(view);

    const sm = view.summary;
    $("histMeta").textContent = "中位 " + fmtSigned(sm.medianDelta, 1) + "ms · UR " + Number(sm.ur).toFixed(0);
    $("timelineMeta").textContent = "共 " + view.beatmap.notes + " 个物件 · " + sm.flaws + " 次有瑕疵";

    renderErrorList();
    if (state.player) ensurePlayer();
    // 对比模式：标杆那一路也要换成同一套口径，否则左右两边的判定色对不上
    if (state.compare && state.player2) {
      state.prog1 = buildProgress(state.data.notes || []);
      state.prog2 = buildProgress(state.compare.notes || []);
      state.player2.load({
        keys: state.compare.beatmap.keys,
        notes: state.judgeMode === "classic"
          ? (state.compare.notes || []).map((n) => Object.assign({}, n, { j: n.jc }))
          : state.judgeMode === "v2"
            ? (state.compare.notes || []).map((n) => Object.assign({}, n, { j: n.j2 }))
            : state.compare.notes,
        firstObject: state.compare.beatmap.firstObject,
        lastObject: state.compare.beatmap.lastObject,
      });
      state.player2.setTime(t);
      state.player2.render();
    }
    redrawAllCharts();
    updateTransport();
    updateNowPanel(t);
    if (!silent) {
      window.UI.toast(
        mode === "classic"
          ? "已切到 Classic：stable 判定窗口 + 长条头尾分判"
          : mode === "v2"
            ? "已切到 ScoreV2：长条头尾分判，尾窗口 1.5 倍"
            : "已切回 ScoreV1：长条头尾合并"
      );
    }
  }

  // 4K 检查 + 20 星上限。返回 true 表示这一份不该显示（已经给过提示）。
  async function rejectUnusableReplay(data) {
    // 最后一道闸：后端已经拒过 4K 以外的谱面，这里再挡一次是为了"页面连着旧版服务"的情况
    // （改完代码但服务没重启）。宁可什么都不显示，也不要让人看到一份没有依据的报告。
    const keys = data && data.beatmap ? Number(data.beatmap.keys) : 0;
    if (keys && keys !== 4) {
      window.UI.loading(false);
      window.UI.toast("本工具只支持 4K（4 键）谱面，这份是 " + keys + "K，已停止显示", true);
      return true;
    }
    // 20 星上限：观赏图/特殊图不进分析流程，先用 Sunny 星数快速判一下。
    // 只算 Sunny（几十毫秒），不跑 Roxy/Azusa；失败时放行，避免误杀。
    const rate = Number((data.replay && data.replay.rate) || 1) || 1;
    if (window.Estimator && window.Estimator.sunnyStar) {
      const stars = await window.Estimator.sunnyStar(data.session, rate);
      if (stars && Number.isFinite(stars.star) && stars.star > 20) {
        window.UI.loading(false);
        // 清掉上一局的结果，否则用户会以为这张新回放已经分析完了
        state.data = null;
        state.baseData = null;
        state.chartOnly = null;
        $("emptyState").hidden = false;
        window.UI.toast(
          "这张图 " + stars.star.toFixed(1) + " 星，超过 20 星上限，已按兜底策略跳过分析",
          true
        );
        return true;
      }
    }
    return false;
  }

  // 按回放来源自动选口径，并更新顶部按钮与提示。
  // lazer 默认 V2；stable 只有开了 ScoreV2 才是 V2；旧版服务没有 judge 字段时保持上次选择。
  function applyJudgeModeFromReplay(data) {
    const autoJudge = data.judge && data.judge.default;
    if (autoJudge === "v1" || autoJudge === "v2" || autoJudge === "classic") {
      state.judgeMode = autoJudge;
      savePrefs();
    }
    // 只有 lazer + Classic 的回放才有第三套口径，其余回放把按钮藏起来
    const classicBtn = document.querySelector('#judgeToggle [data-judge="classic"]');
    if (classicBtn) {
      classicBtn.hidden = !(data.judge && (data.judge.modes || []).includes("classic"));
    }
    const judgeBox = $("judgeToggle");
    if (!judgeBox || !data.judge) return;
    const srcLabel = data.judge.source === "lazer"
      ? "lazer 回放"
      : data.judge.source === "lazer-classic"
        ? "lazer + Classic"
        : (data.judge.source === "stable-v2" ? "stable + ScoreV2" : "stable 回放");
    judgeBox.title = "长条判定口径：V1 头尾合并，V2 头尾分判（尾窗口 1.5 倍）。"
      + "已识别为 " + srcLabel + "，自动使用 " + state.judgeMode.toUpperCase() + "，可随时手动切换。";
    if (state._autoJudge !== autoJudge) {
      state._autoJudge = autoJudge;
      window.UI.toast("已识别 " + srcLabel + "，自动使用 " + state.judgeMode.toUpperCase());
    }
  }

  // 换回放时把上一局的播放状态清干净。
  // 不清的话进度条和「此刻」面板还停在上个回放的位置（虚拟时钟不会被换谱自动归零，
  // 失误索引缓存按条数判断、条数一样时也会脏）。
  function resetPlaybackState() {
    pause();
    state.virtualTime = 0;
    state.audioBroken = false;
    state._errTimes = null;
    state._tlFallbackAt = 0;
  }

  // 残卷 / 可能配错谱面的提醒都走这里。
  // warnings 以前没有任何前端消费者：MD5 对不上、键数不符这些话只写进了 JSON，
  // 用户看不到，也就等于没提示。
  function showReplayNotices(view) {
    const notices = [];
    if (view && view.warning) notices.push(view.warning);
    if (view && Array.isArray(view.warnings)) notices.push.apply(notices, view.warnings);
    if (notices.length) window.UI.toast(notices.join(" "), true);
  }

  // 把一份视图铺到页面上（首次分析后与切换判定口径时共用）
  function renderView(view) {
    window.UI.renderTrack(view);
    window.UI.renderHero(view);
    window.UI.renderMsd(view);
    window.UI.renderAbout(view.version);
    window.UI.renderCategories(view);
    window.UI.renderMeta(view);
    window.UI.renderSections(view);
    window.UI.renderPatternShare(view);
    window.UI.renderJudgeBars(view);
    window.UI.renderRawTable(view);

    // 误差直方图标题里的摘要
    const sm = view.summary;
    $("histMeta").textContent = "中位 " + fmtSigned(sm.medianDelta, 1) + "ms · UR " + Number(sm.ur).toFixed(0);
    $("timelineMeta").textContent = "共 " + view.beatmap.notes + " 个物件 · " + sm.flaws + " 次有瑕疵";
  }

  async function renderAnalysis(data) {
    if (await rejectUnusableReplay(data)) return;
    closeCompare();     // 换了主回放，对比自然失效
    applyJudgeModeFromReplay(data);
    // 原始结果留着（含两套判定），当前视图按首选项挑一套；切换时只换视图
    state.baseData = data;
    // 快捷导入没有本地文件句柄，记一下来源，切换判定算法时走另一条重算路径
    if (data.quick) state.lastReplay = { kind: "quick" };
    state.data = viewOf(data, state.judgeMode);
    updateJudgeToggle();
    state.selectedError = null;
    // 换了回放，「此刻」卡片的内容整个作废，否则相同时间点的旧失误会让它以为没必要重画
    nowSig = "";
    const view = state.data;
    showReplayNotices(view);
    resetPlaybackState();
    window.UI.loading(false);
    $("emptyState").hidden = true;

    renderView(view);
    // 导入这一刻曲库里可能刚多了一张图（lazer 下载之后索引往往是旧的）。
    // 后端在找不到谱面时会自己重扫，这里把右上角的数量重新读一次，
    // 否则那个数字会一直停在启动时的值。
    window.API.library()
      .then((info) => window.UI.renderLibrary(info))
      .catch(() => { /* 读不到就保持原样，不影响报告 */ });
    window.UI.renderSimilar(null);   // 段位还没算出来，这张卡片先不显示
    scheduleSimilar(view);          // 首屏之后空闲时再算段位 → 推荐
    renderLnStats(data);

    state.errorShown = ERROR_PAGE;
    renderErrorList();
    applyReplaySpeed(view.replay);
    setupAudio(view);
    // 先把「总览」切出来：隐藏状态的 canvas 宽度是 0，画出来会是空白
    switchTab("overview");
    redrawAllCharts();
    updateTransport();
    updateNowPanel(currentTimeMs());
    // 再补一次：布局稳定后（字体/滚动条落定）重绘，避免第一帧量到 0 宽
    setTimeout(redrawAllCharts, 120);
    window.UI.toast("分析完成：" + data.beatmap.title + " [" + data.beatmap.difficulty + "]");
  }

  // 回放倍率（DT 1.5× / HT 0.75×）要直接当播放速度用。
  // 否则 DT 的回放会用原速播一遍：音符间距看着比当时宽，像是在说"这张图没那么难"。
  // 仍然保留速度下拉框，玩家想按原速看细节可以自己改回去。
  function applyReplaySpeed(replay) {
    const rate = Number(replay && replay.rate) || 1;
    state.speed = rate;
    const sel = $("speed");
    if (sel) {
      // 自定义倍率（比如 1.33×）不在选项里就补一条，否则下拉框会跳回 1×
      if (!Array.from(sel.options).some((o) => Math.abs(Number(o.value) - rate) < 1e-6)) {
        const opt = document.createElement("option");
        opt.value = String(rate);
        opt.textContent = rate + "×";
        sel.appendChild(opt);
      }
      sel.value = String(rate);
    }
    if (state.audio) {
      try { state.audio.playbackRate = rate; } catch (e) { /* 元数据还没就绪，忽略 */ }
    }
  }

  function setupAudio(data) {
    const note = $("audioNote");
    if (state.audio) {
      state.audio.pause();
      state.audio = null;
    }
    state.audioBroken = false;
    if (!data.session || !data.media || !data.media.audio) {
      // 进行中的进度统一放在右上角的任务条（一行一件事 + 进度条），
      // 这一行只负责写结果：补不到时说明会没有声音。
      note.hidden = true;
      fetchAudioOnline(data);
      return;
    }
    const audio = document.createElement("audio");
    audio.src = "/api/media/" + data.session + "/audio";
    audio.preload = "auto";
    audio.addEventListener("loadedmetadata", () => updateTransport());
    audio.addEventListener("error", () => {
      state.audioBroken = true;
      note.hidden = false;
      note.textContent = "音频加载失败，改用虚拟时钟播放（没有声音）。";
    });
    // 音频一准备好就把速度同步过去：不然中途改了速度再换歌会变回 1×
    audio.addEventListener("canplay", () => {
      try { audio.playbackRate = state.speed; } catch (e) { /* ignore */ }
    });
    state.audio = audio;
    applyVolume();
    // 默认不显示任何提示：这句「音频按绝对时间对齐…」对用户没信息量
    note.hidden = true;
  }

  // 本地没有音频时补一份：服务端按谱面 MD5 去镜像站取 .osz，只把音频留下。
  // 不阻塞界面——补到了就把播放器接上，补不到就退回虚拟时钟。
  //
  // 补的过程中每 0.5 秒读一次进度。这一步以前只有一句不动的"正在尝试补一份…"，
  // 而它其实要下整个谱面包（几十 MB、服务端预算 45 秒）：没有任何数字在动，
  // 用户的判断就是"程序卡死了"。现在至少能看到下到多少、等到第几秒。
  let audioFetchSession = "";
  let audioProgressTimer = 0;

  function stopAudioProgress() {
    if (audioProgressTimer) {
      clearInterval(audioProgressTimer);
      audioProgressTimer = 0;
    }
  }

  function isCurrentSession(session) {
    return !!(state.data && state.data.session && state.data.session === session);
  }

  function sizeText(bytes) {
    const n = Number(bytes) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }

  // 一行字说清现在在做什么：阶段 · 下到多少 · 等了多久。
  // 阶段是服务端给的（正在从 sayobot 下载 / 正在校验 / 正在抽取），这里只负责拼。
  function audioDetail(p, sec) {
    const bits = [];
    if (p && p.stage) bits.push(p.stage);
    const done = Number(p && p.done) || 0;
    const total = Number(p && p.total) || 0;
    if (done > 0) bits.push(sizeText(done) + (total > 0 ? " / " + sizeText(total) : ""));
    if (p && Number(p.attempt) > 1) bits.push("第 " + p.attempt + " 个镜像");
    bits.push("已等 " + sec + " 秒");
    return bits.join(" · ");
  }

  function startAudioProgress(session) {
    const startedAt = Date.now();
    let latest = null;
    const paint = () => {
      const sec = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
      const done = Number(latest && latest.done) || 0;
      const total = Number(latest && latest.total) || 0;
      window.UI.tasks.update("audio", {
        label: "补音频" + (latest && latest.mirror ? " · " + latest.mirror : ""),
        percent: total > 0 ? done / total : null,
        detail: audioDetail(latest, sec),
      });
    };
    window.UI.tasks.start("audio", "补音频");
    paint();
    audioProgressTimer = setInterval(async () => {
      // 轮询失败（切了页签、服务端忙）不影响计时：沿用上一次的数字，秒数继续走
      const p = await window.API.audioFetchStatus(session);
      if (p) latest = p;
      paint();
    }, 500);
  }

  async function fetchAudioOnline(data) {
    const note = $("audioNote");
    if (!data || !data.session) return;
    if (audioFetchSession) {
      // 已经有一份在补：同一张图就把提示接回来，换成别的图则等它结束（结果会进缓存）
      if (audioFetchSession === data.session) startAudioProgress(data.session);
      return;
    }
    audioFetchSession = data.session;
    startAudioProgress(data.session);

    let res = null;
    try {
      res = await window.API.fetchAudio(data.session);
    } finally {
      audioFetchSession = "";
      stopAudioProgress();
    }

    // 下载期间可能已经换了一份回放：结果照样进了缓存，但别拿旧结论去改新页面
    const current = isCurrentSession(data.session);
    if (res && res.ok) {
      window.UI.tasks.end("audio");
      data.media = data.media || {};
      data.media.audio = true;
      const from = res.source === "cache" ? "本地缓存"
        : res.source === "local" ? "本地曲库"
        : (res.source || "镜像站");
      window.UI.toast("已补上音频（来源：" + from + "）");
      if (current) setupAudio(data);
      else fetchAudioForCurrent(data);
      return;
    }

    const why = (res && res.error) || "";
    if (current && note) {
      note.hidden = false;
      if (res && res.disabled) {
        note.textContent = "本地没找到音频，在线补音频已关闭；回放会用虚拟时钟播放（没有声音）。";
      } else {
        note.textContent = "本地没找到音频，在线补也没成功"
          + (why ? "（" + why + "）" : "")
          + "，回放会用虚拟时钟播放（没有声音）。";
      }
    }
    // 结果直接写在任务条上（不管当前在哪个页签都看得见），不再另外弹一条浮条：
    // 同一件事说两遍，反而会盖住下面正在看的内容。回放页那行字保留着，
    // 因为过一会儿它会消失，而"这份回放没有声音"需要一直写着。
    if (res && res.disabled) {
      window.UI.tasks.end("audio");
    } else {
      window.UI.tasks.end("audio", { error: true, label: "补音频失败", message: why || "镜像站没有回应" });
    }
    if (!current) fetchAudioForCurrent(data);
  }

  // 下载期间换了回放：新的这份也缺音频的话，接着补它。
  // 不接这一步的话，它那行提示会永远停在"正在尝试在线补一份…"。
  function fetchAudioForCurrent(done) {
    const now = state.data;
    if (!now || !now.session || now.session === done.session) return;
    if (now.media && now.media.audio) return;
    fetchAudioOnline(now);
  }

  // ---------------------------------------------------------------- 导入流程
  // 分析期间显示"已经等了多久"，并在超时后给出可执行的提示
  let waitTimer = null;
  function setStage(text) {
    state.stage = text;
    const box = window.UI.$("loadingText");
    if (box) box.textContent = text;
  }
  // 加载框里的"取消"按钮：能中止当前分析，不至于被永远困住
  function showLoading(text, onCancel) {
    state.cancelLoading = onCancel || null;
    // 内联兜底：即使脚本是旧版本，按钮也能靠 index.html 里的 onclick 关掉加载框
    window.__cancelLoading = onCancel
      ? function () {
          try {
            onCancel();
          } catch (e) {
            hideLoading();
          }
        }
      : function () {
          hideLoading();
        };
    window.UI.loading(true, text);
    const btn = $("loadingCancel");
    if (btn) btn.textContent = onCancel ? "取消" : "关闭";
  }
  function hideLoading() {
    state.cancelLoading = null;
    window.__cancelLoading = null;
    window.UI.loading(false);
  }

  // 页面版本自愈：服务端把当前静态资源版本号放在 /api/health 里返回。
  // 如果浏览器拿着旧缓存的 html/js（版本对不上），自动刷新一次，
  // 避免「旧脚本 + 新后端」把人困在永远转圈、连阶段提示都没有的加载框里。
  // 服务端进程可能比代码旧（改完 .py 没重启，Python 只在 import 时读模块）。
  // 这时后端缺新字段、前端却已经按新结构画图，表现是"某块内容静悄悄没了"、且不报错。
  // 这种事猜不出来，只能让服务端自报（/api/health 的 stale），页面负责把话说清楚。
  let staleCodeWarned = false;
  function warnIfStaleCode(healthData) {
    if (!healthData || !healthData.stale || staleCodeWarned) return;
    staleCodeWarned = true;
    beacon("stale-server-code", { assetV: healthData.assetV });
    window.UI.toast("分析器还在跑旧代码（改完没重启），图表会缺内容。请双击「停止分析器.vbs」再双击「启动分析器.vbs」，然后刷新本页。", true);
  }

  // 当前页面自己的资源版本号：从 <script src="...main.js?v=xxx"> 里读。
  // 启动自检与"版本自愈"都要用它，写两遍的话改了一处就会拿错版本、把页面刷成死循环。
  function assetVersion() {
    const scriptSrc = (Array.from(document.scripts).map((s) => s.src).find((s) => /main\.js/.test(s || "")) || "");
    return (scriptSrc.match(/v=([0-9a-f]+)/) || [])[1] || "";
  }

  async function checkFrontendVersion(healthData) {
    try {
      const serverV = (healthData && healthData.assetV) || "";
      if (!serverV) return;
      const myV = assetVersion();
      if (!myV || myV === serverV) return;
      const key = "vr-" + serverV;
      try {
        if (sessionStorage.getItem(key)) return; // 每个版本只自动刷新一次，防止循环
        sessionStorage.setItem(key, "1");
      } catch (err) { /* 隐私模式等拿不到 sessionStorage 就直接刷 */ }
      beacon("stale-frontend", { mine: myV, server: serverV });
      window.UI.toast("检测到页面版本过旧，正在自动刷新…", true);
      setTimeout(() => location.reload(), 600);
    } catch (e) {
      /* 自愈失败不影响主流程 */
    }
  }

  function startWaiting() {
    const t0 = Date.now();
    clearInterval(waitTimer);
    waitTimer = setInterval(() => {
      const sec = Math.round((Date.now() - t0) / 1000);
      const box = window.UI.$("loadingText");
      if (!box) return;
      const stage = state.stage || "正在处理";
      if (sec >= 60 && !state.escaped) {
        state.escaped = true;
        showBlockedUpload("等了 60 秒还没结果——上传通道被拦了，自动兜底也没成功");
        return;
      }
      box.textContent = stage + "（" + sec + "s）";
    }, 400);
  }
  function stopWaiting() {
    clearInterval(waitTimer);
    waitTimer = null;
  }

  // 明显的错误先挡掉，别让它卡在"正在解析"。返回错误文案，空串表示可以继续。
  function replayFileProblem(file) {
    if (state.busy) return "上一次分析还没结束，稍等一下";
    if (!/\.osr$/i.test(file.name)) return "这是 " + file.name + "，不是回放文件（需要 .osr）";
    if (file.size === 0) {
      // 空文件在后端只会得到一句「没有收到回放文件」，用户容易以为是自己没拖好
      return "这个文件是空的（0 字节）：" + file.name + "。可能是导出时没写成功，重新导出一份再试";
    }
    if (file.size > 8 * 1024 * 1024) {
      return "这个文件 " + (file.size / 1048576).toFixed(1) + "MB，回放一般只有几百 KB，确认一下选对了没";
    }
    return "";
  }

  // 上传并分析。整包 POST 失败且不是"后端已答复"时，自动改走分片通道。
  async function uploadReplay(base64) {
    // 上传走 XHR：能显示"已经发出去多少字节"。卡在 0% 就是内容没离开浏览器，
    // 卡在 100% 就是后端在处理——这两种情况的处理方式完全不同，所以必须能区分。
    setStage("正在上传回放");
    beacon("upload-start", { bytes: base64.length });
    try {
      return await window.API.analyzeUpload(
        { replay: base64, beatmap: null, beatmapPath: null, songsRoot: null, algo: state.judgeAlgo },
        (loaded, total) => {
          const pct = total ? Math.round((loaded / total) * 100) : 0;
          if (pct >= 100) setStage("上传完成，正在分析");
          else setStage("正在上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
        },
        (xhr) => { state.activeXhr = xhr; }
      );
    } catch (uploadErr) {
      // 只有"传输层"失败才该改走分片通道：连不上、超时、被中途掐断
      // （本机安全软件或代理把大 POST 体整包吞掉的典型表现）。
      // 后端已经答复过（空文件、坏回放、4K 以外、内部错误）时重发没有意义，
      // 只会把「没有收到回放文件」盖成「分片 1 被拒绝（HTTP 400）」，把真正的原因弄丢。
      const answered = !!(uploadErr && (uploadErr.payload || uploadErr.answered));
      if (!state.cancelLoading || answered) throw uploadErr; // 取消过 / 后端已答复 → 不兜底
      beacon("fallback-chunked", { reason: (uploadErr && uploadErr.message) || "?" });
      state.activeXhr = null;
      return await window.API.analyzeUploadChunked(
        base64,
        (loaded, total) => {
          const pct = total ? Math.round((loaded / total) * 100) : 0;
          setStage("分片上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
        },
        (text) => setStage(text),
        state.judgeAlgo
      );
    }
  }

  // .osr 头第一个字节就是 gameMode（1 字节），后面紧跟 4 字节的版本号。
  // 按 int32 读前 4 字节会把 stable 的版本号（如 30000019）当成模式，认不出来，
  // 所以我只读这 1 个字节。读到的值不是 0~3 就当没结论，交给后端解析。
  async function osrModeProblem(file) {
    if (!/\.osr$/i.test(file.name || "")) return "";
    let head;
    try {
      head = await file.slice(0, 1).arrayBuffer();
    } catch (e) {
      return "";   // 读不出来就交给后端判断，别在本地误拦
    }
    if (!head || head.byteLength < 1) return "";
    const mode = new DataView(head).getUint8(0);
    if (mode === 3 || mode > 3) return "";
    const NAMES = { 0: "osu!standard", 1: "osu!taiko", 2: "osu!catch" };
    return "这可能是其他模式的回放（" + NAMES[mode] + "），本工具只支持 osu!mania";
  }

  async function handleReplayFile(file) {
    if (!file) return;
    const problem = replayFileProblem(file);
    if (problem) {
      window.UI.toast(problem, true);
      return;
    }
    const wrongMode = await osrModeProblem(file);
    if (wrongMode) {
      window.UI.toast(wrongMode, true);
      return;
    }

    beacon("file-chosen", { name: file.name, size: file.size });
    state.lastReplay = { kind: "file", file: file };
    state.busy = true;
    state.escaped = false;
    showLoading("正在读取文件…", () => {
      state.busy = false;
      if (state.activeXhr) {
        try { state.activeXhr.abort(); } catch (err) { /* 忽略 */ }
      }
      hideLoading();
      stopWaiting();
      window.UI.toast("已取消");
    });
    setStage("正在读取文件");
    startWaiting();
    try {
      // 先探一下后端：连不上就别把大文件丢过去干等
      const health = await window.API.health(4000);
      await checkFrontendVersion(health);
      warnIfStaleCode(health);
      setStage("正在读取文件");
      const b64 = await window.API.fileToBase64(file);
      state.replayBase64 = b64;
      renderAnalysis(await uploadReplay(b64));
    } catch (e) {
      window.UI.loading(false);
      if (e && e.name === "AbortError") {
        window.UI.toast("等了 90 秒还没结果，已经放弃。请确认启动窗口里没有报错；也可以把 app\\_cache\\server.log 发我看看", true);
      } else if (e && /Failed to fetch|NetworkError|network/i.test(e.message || "")) {
        window.UI.toast("连不上本地服务——重新双击 启动分析器.vbs 再开一次", true);
      } else if (e.needsInput) {
        openBeatmapModal(e.payload || {});
      } else {
        window.UI.toast(e.message || "分析失败", true);
      }
    } finally {
      stopWaiting();
      state.busy = false;
      state.stage = "";
      state.activeXhr = null;
    }
  }

  // 两个"选 .osu"的入口共用的前置检查。返回错误文案，空串表示可以继续。
  function osuFileProblem(file) {
    if (!/\.osu$/i.test(file.name)) return "谱面文件需要是 .osu（你选的是 " + file.name + "）";
    if (state.busy) return "上一次分析还没结束，稍等一下";
    return "";
  }

  async function handleBeatmapFile(file) {
    if (!file) return;
    const problem = osuFileProblem(file);
    if (problem) {
      window.UI.toast(problem, true);
      return;
    }
    state.busy = true;
    window.UI.loading(true, "正在用你选的谱面重新分析…");
    window.__cancelLoading = function () { hideLoading(); stopWaiting(); state.busy = false; };
    startWaiting();
    try {
      const b64 = await window.API.fileToBase64(file);
      // 先只要一份元数据：选错键数（同一图集里常混着 7K 难度）时立刻给结论，
      // 不必把整份回放重新上传一遍再等后端报错。
      const meta = await window.API.beatmapMeta(b64);
      if (meta && meta.keys && Number(meta.keys) !== 4) {
        window.UI.loading(false);
        window.UI.toast("本工具只支持 4K（4 键）谱面，你选的是 " + meta.keys + "K：" + meta.label, true);
        return;
      }
      const data = await window.API.analyze(state.replayBase64, b64, null, null, null, state.judgeAlgo);
      closeModal("bmModal");
      renderAnalysis(data);
    } catch (e) {
      window.UI.loading(false);
      window.UI.toast(e.message || "分析失败", true);
    } finally {
      stopWaiting();
      state.busy = false;
    }
  }

  // ---------------------------------------------------------------- 只看谱面
  // 只解析 .osu：不算成绩、不建会话、不需要音频。
  async function handleChartFile(file) {
    if (!file) return;
    const problem = osuFileProblem(file);
    if (problem) {
      window.UI.toast(problem, true);
      return;
    }
    state.busy = true;
    window.UI.loading(true, "正在读取谱面…");
    try {
      const b64 = await window.API.fileToBase64(file);
      const data = await window.API.chartOnly(b64);
      renderChartOnly(data);
    } catch (e) {
      window.UI.loading(false);
      window.UI.toast(e.message || "谱面读取失败", true);
    } finally {
      state.busy = false;
    }
  }

  const PATTERN_HEX = {
    stack: "#f472b6", cut: "#7c5cff", spam: "#22d3ee",
    tech: "#fbbf24", ln: "#34d399", calm: "#94a3b8",
  };

  // 仅看谱面页的段位：用和回放页同一套估算链（Sunny → Azusa → Roxy）。
  // 左边 MSD 是 MinaCalc 口径，两者量纲不同，所以这里算出来直接覆盖那两行文字；
  // 算不出来就保留 MinaCalc 的兜底，不影响页面其他部分。
  async function applyChartDan(data) {
    const cell = $("coDan");
    const note = $("coDanNote");
    if (!cell || !note || !window.Estimator || !window.Estimator.estimateFromText) return;
    if (!data || !data.osuText) return;
    let res;
    try {
      res = await window.Estimator.estimateFromText(data.osuText, 1);
    } catch (e) {
      return;
    }
    if (!res || !res.ok) return;
    cell.textContent = res.label;
    const parts = [];
    if (res.method === "roxy") parts.push("Roxy " + Number(res.numeric).toFixed(2));
    else if (res.method === "azusa") parts.push("Azusa " + Number(res.numeric).toFixed(2));
    else parts.push("Sunny " + Number(res.rcStar).toFixed(2) + " 星");
    if (res.lnStar > 0) parts.push("LN " + Number(res.lnStar).toFixed(2));
    note.textContent = parts.join(" · ") + " · 原速";
    note.title = "段位来源："
      + (res.method === "roxy" ? "Roxy" : (res.method === "azusa" ? "Azusa" : "Sunny"))
      + "。左边的 MSD 来自 MinaCalc，只看谱面本身。";
  }

  function renderChartOnly(data) {
    // 谱面模式与回放模式互斥：清掉回放状态，避免播放器/失误面板还挂着上一局
    closeCompare();
    state.data = null;
    state.baseData = null;
    state.chartOnly = data;
    state.compare = null;
    state._errTimes = null;
    state.selectedError = null;
    pause();
    state.virtualTime = 0;
    window.UI.loading(false);
    $("emptyState").hidden = true;

    const bm = data.beatmap || {};
    $("coTitle").textContent =
      (bm.artist || "") + " - " + (bm.title || "") + " [" + (bm.difficulty || "") + "]";
    $("coMeta").textContent = "只分析谱面，不读取回放与成绩 · " + (bm.source || "");
    const msd = data.msd || {};
    $("coMsd").textContent = msd.available ? Number(msd.overall).toFixed(2) : "—";
    $("coDan").textContent = (msd.dan && msd.dan.label) || "—";
    $("coDanNote").textContent = msd.available ? "段位估计（MinaCalc 口径）" : "难度数据不可用";
    void applyChartDan(data);
    $("coInfo").textContent =
      bm.keys + "K · " + bm.notes + " 物件 · " + bm.holds + " 长条 · BPM " +
      bm.bpm + " · " + fmtTime(bm.duration);
    $("coClass").textContent = window.UI.mapClassText(data.mapClass);
    renderChartOnlyPattern(data);
    switchTab("chartonly");
    window.UI.toast("已读取谱面，未分析成绩");
  }

  function renderChartOnlyPattern(data) {
    const bar = $("coBar");
    const legend = $("coLegend");
    if (!bar || !legend) return;
    window.UI.clearNode(bar);
    window.UI.clearNode(legend);
    const items = data.patternShare || [];
    const total = items.reduce((s, x) => s + Number(x.share || 0), 0) || 1;
    items.forEach((it) => {
      const color = PATTERN_HEX[it.key] || "var(--accent)";
      const seg = document.createElement("span");
      seg.style.width = (Number(it.share || 0) / total) * 100 + "%";
      seg.style.background = color;
      seg.title = it.label + " " + it.share + "%";
      bar.appendChild(seg);
      const li = document.createElement("li");
      const dot = document.createElement("i");
      dot.style.background = color;
      const name = document.createElement("b");
      name.textContent = it.label;
      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(document.createTextNode(" " + it.share + "%"));
      legend.appendChild(li);
    });
    if (!items.length) legend.appendChild(el("li", "muted", "这份谱面没有可用的键型数据"));
  }

  function openBeatmapModal(payload) {
    const hint = $("bmModalHint");
    // payload.error 本身就是一句完整的话（自带句号，也带了"请手动选择 .osu"），
    // 早先被塞进「MD5 是 …」后面，读起来不通顺、还出现过两个句号。
    // 现在有就原样显示，没有才用默认提示 —— 再补一句引导会和它重复。
    hint.textContent = String((payload && payload.error)
      || "曲库里没有找到这张谱面。可以手动选 .osu，或按曲名搜索本机曲库。").trim();
    $("bmModal").hidden = false;
    $("bmQuery").focus();
  }

  async function searchBeatmaps(q) {
    const ul = $("bmList");
    window.UI.clearNode(ul);
    try {
      const res = await window.API.beatmapSearch(q);
      (res.items || []).forEach((it) => {
        const li = el("li", null, it.label);
        li.appendChild(el("span", null, it.keys + "K · OD" + it.od + " · " + it.notes + " notes"));
        li.addEventListener("click", () => useLibraryBeatmap(it.path));
        ul.appendChild(li);
      });
      if (!(res.items || []).length) ul.appendChild(el("li", "muted", "没有匹配的谱面"));
      // 非 4K 的谱面被后端滤掉了。不说一声的话，用户会以为曲库里根本没有这张图。
      if (res.hidden) {
        ul.appendChild(el("li", "muted",
          "另有 " + res.hidden + " 张非 4K 谱面未列出（本工具只支持 4K）"));
      }
    } catch (e) {
      window.UI.toast(e.message || "搜索失败", true);
    }
  }

  async function useLibraryBeatmap(path) {
    window.UI.loading(true, "正在用曲库里的谱面分析…");
    try {
      const data = await window.API.analyze(state.replayBase64, null, null, path, null, state.judgeAlgo);
      closeModal("bmModal");
      renderAnalysis(data);
    } catch (e) {
      window.UI.loading(false);
      window.UI.toast(e.message || "分析失败", true);
    }
  }

  // 上传被浏览器/安全软件挡住时的兜底：引导走本地文件夹
  function showBlockedUpload(reason) {
    hideLoading();
    stopWaiting();
    const box = $("uploadBlocked");
    const pathEl = $("blockedPath");
    if (pathEl) pathEl.textContent = state.localDir || "项目目录下的 _replays 文件夹";
    const hint = $("blockedHint");
    if (hint && reason) hint.textContent = reason + "。不用折腾浏览器，改用下面这条通道：";
    if (box) box.hidden = false;
    refreshLocalReplays();
  }

  function closeModal(id) {
    const m = $(id);
    if (m) m.hidden = true;
  }

  // ------------------------------------------------------ 手动指定游戏目录
  // 自动探测兜不住的情况（装在哪都不知道、目录被改得面目全非）留一个出口：
  // 设置页里填两次路径，保存后立刻重建索引。
  async function loadPathSettings() {
    const status = $("pathStatus");
    if (!status) return;
    status.className = "dir-status";
    status.textContent = "读取当前设置…";
    try {
      const info = await window.API.paths();
      $("pathStable").value = info.stableDir || "";
      $("pathLazer").value = info.lazerDir || "";
      const ex = info.examples || {};
      if (ex.stable) $("pathStable").placeholder = "例如 " + ex.stable;
      if (ex.lazer) $("pathLazer").placeholder = "例如 " + ex.lazer;
      // 同时填了两个客户端时，只报第一个根会让人以为第二个没生效，这里标一下总数
      const rootCount = (info.songsRoots || []).length;
      status.textContent = info.count
        ? "当前曲库：" + info.songsRoot
          + (rootCount > 1 ? "（共 " + rootCount + " 处曲库）" : "")
          + "，" + info.count + " 张谱面"
        : "当前还没找到曲库。填完下面的路径点保存试试。";
      // 把配置文件的落点写出来：存不住的时候，看这一行是不是每次都换地方就够了
      const cfg = $("pathConfig");
      if (cfg) {
        cfg.textContent = info.configPath
          ? (info.configShared
            ? "配置文件（不在程序目录，已改存到固定位置）："
            : "配置文件：") + info.configPath
          : "";
      }
    } catch (e) {
      status.textContent = "读取当前设置失败：" + (e.message || e);
    }
  }

  async function savePaths(forceClear) {
    const btn = $("btnSavePaths");
    const status = $("pathStatus");
    const stable = $("pathStable").value.trim();
    const lazer = $("pathLazer").value.trim();
    const clearing = !!forceClear || (!stable && !lazer);
    // 两栏都空 = 要清除。空表单本身不再有副作用（后端也拦了），
    // 只有这里明确确认过、带上 clear 标记才真的删。
    if (clearing && !forceClear
        && !window.confirm("两栏都是空的，会清掉之前保存的目录、改回自动探测。确定吗？")) {
      return;
    }
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "扫描中…";
    status.className = "dir-status";
    status.textContent = "正在按新路径扫描曲库，谱面多的话要几秒…";
    try {
      const r = await window.API.savePaths(clearing
        ? { clear: true }
        : { stableDir: stable, lazerDir: lazer });
      if (r.unchanged) {
        status.className = "dir-status";
        status.textContent = r.hint || "没有改动。";
        return;
      }
      status.className = "dir-status " + (r.count ? "good" : "bad");
      status.textContent = r.count
        ? "完成：找到 " + r.count + " 张谱面（" + r.songsRoot + "）"
        : r.hint || "按这个路径没找到谱面，确认目录选对了吗？";
      // 顺手把左上角的曲库标签刷新
      try {
        window.UI.renderLibrary(await window.API.library());
      } catch (e) {
        /* 刷新标签失败不影响保存结果 */
      }
    } catch (e) {
      status.className = "dir-status bad";
      status.textContent = e.message || "保存失败";
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  // 安全绑定：某个元素在旧版 HTML 里不存在时，不能因此让后面的绑定全部失效
  function on(id, ev, fn) {
    const elx = $(id);
    if (elx) elx.addEventListener(ev, fn);
  }

  // 打开 _replays 文件夹。页面里有两个入口（空状态 / 上传被挡时的弹窗），
  // 文案必须一致——分头写两遍迟早只剩一处改过。
  async function openReplaysFolder() {
    try {
      await window.API.openReplays();
      window.UI.toast("已打开文件夹，把 .osr 复制进去后点「刷新列表」");
    } catch (e) {
      window.UI.toast("打不开文件夹：" + (e.message || e), true);
    }
  }

  // 救命按钮：取消 / 自检 / 本地文件夹。放最前面绑，别的地方写错了它们也要能用。
  function bindRescueActions() {
    on("loadingCancel", "click", () => {
      beacon("cancel-click", { busy: state.busy, hasFn: !!state.cancelLoading });
      const fn = state.cancelLoading;
      if (fn) fn();
      else hideLoading();
      state.busy = false;
      stopWaiting();
    });
    on("btnSelfTest", "click", async () => {
      const t0 = performance.now();
      try {
        await window.API.health(4000);
        const t1 = performance.now();
        const echo = await window.API.echo();
        const t2 = performance.now();
        window.UI.toast("后端连接正常：GET " + Math.round(t1 - t0) + " ms，POST " + Math.round(t2 - t1) +
          " ms（服务端收到 " + echo.received + " 字节）");
      } catch (e) {
        window.UI.toast("后端连接有问题：" + (e.message || e), true);
      }
    });
    on("btnOpenFolder", "click", openReplaysFolder);
    on("btnOpenFolder2", "click", openReplaysFolder);
    on("btnRefreshLocal", "click", async () => {
      await refreshLocalReplays();
      const n = document.querySelectorAll("#localReplayList li").length;
      window.UI.toast(n ? ("列表里现在有 " + n + " 个回放，点文件名即可分析") : "文件夹里还没有 .osr，复制进去再刷新");
    });
    on("btnRetryUpload", "click", () => {
      const box = $("uploadBlocked");
      if (box) box.hidden = true;
      window.UI.toast("请重新把 .osr 拖进页面");
    });
  }

  // 导入通道与弹窗：选文件、帮助、手动选谱面、曲库搜索
  function bindImportActions() {
    $("btnImport").addEventListener("click", () => $("fileOsr").click());
    $("btnPickFile").addEventListener("click", () => $("fileOsr").click());
    $("btnPickChart").addEventListener("click", () => $("fileChartOsu").click());
    $("btnCheckUpdate").addEventListener("click", () => checkUpdate(true));

    $("fileOsr").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";   // 清空才能连续选同一个文件
      handleReplayFile(f);
    });
    $("fileChartOsu").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      handleChartFile(f);
    });
    const localSearch = $("localSearch");
    if (localSearch) {
      localSearch.addEventListener("input", renderLocalReplayList);
      localSearch.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          e.target.value = "";
          renderLocalReplayList();
        }
      });
    }
    $("fileOsu").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      handleBeatmapFile(f);
    });

    $("btnCloseModal").addEventListener("click", () => closeModal("bmModal"));
    $("btnPickOsu").addEventListener("click", () => $("fileOsu").click());
    // 曲库标签点一下就能手动指定游戏目录（自动找不到时的兜底）
    $("btnSavePaths").addEventListener("click", () => savePaths(false));
    if ($("btnClearPaths")) {
      $("btnClearPaths").addEventListener("click", async () => {
        if (!window.confirm("清掉手动指定的游戏目录，改回自动探测？")) return;
        $("pathStable").value = "";
        $("pathLazer").value = "";
        await savePaths(true);
      });
    }
    on("btnSaveHotkey", "click", saveHotkeySettings);
    on("btnMirrorTest", "click", testMirrors);
    $("btnAlgoHelp").addEventListener("click", () => { $("algoHelpModal").hidden = false; });
    $("btnCloseAlgoHelp").addEventListener("click", () => { $("algoHelpModal").hidden = true; });
    document.querySelectorAll(".modal").forEach((m) => {
      m.addEventListener("click", (e) => {
        if (e.target === m) m.hidden = true;
      });
    });

    $("bmQuery").addEventListener("input", (e) => {
      clearTimeout(state._searchTimer);
      state._searchTimer = setTimeout(() => searchBeatmaps(e.target.value), 220);
    });
  }

  // 拖拽：dragenter/dragleave 成对计数，否则从子元素上划过就会把遮罩闪掉
  function bindDropzone() {
    let dragDepth = 0;
    window.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
      dragDepth++;
      $("dropzone").hidden = false;
    });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) $("dropzone").hidden = true;
    });
    window.addEventListener("drop", (e) => {
      e.preventDefault();
      dragDepth = 0;
      $("dropzone").hidden = true;
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      if (/\.osu$/i.test(f.name)) handleChartFile(f);
      else handleReplayFile(f);
    });
  }

  // 播放器控件：播放/进度/速度/静音/音量/下落速度/音画偏移
  function bindTransportControls() {
    $("btnPlay").addEventListener("click", togglePlay);
    // 时间轴页也要能暂停：那条竖线就是播放位置，只能看不能停最难受
    $("btnPlayTl").addEventListener("click", togglePlay);
    $("seek").addEventListener("input", (e) => {
      const pct = Number(e.target.value) / 1000;
      seekTo(pct * durationMs(), false);
    });
    $("speed").addEventListener("change", (e) => {
      state.speed = Number(e.target.value) || 1;
      if (state.audio) state.audio.playbackRate = state.speed;
      window.UI.toast("播放速度 " + state.speed + "×（音频与画面同步变速）");
    });
    // 打击音：关 / 正解音 / 实际击打。切换时顺手解锁 AudioContext ——
    // 浏览器只认"用户手势"这个入口，等播放再解锁会漏掉第一小节。
    const hsSel = $("hitsound");
    if (hsSel) {
      syncHitsoundUi();
      hsSel.addEventListener("change", () => {
        const v = hsSel.value;
        state.hitsoundMode = (v === "actual" || v === "planned") ? v : "off";
        syncHitsound();
        savePrefs();
        if (state.hitsoundMode === "off") {
          window.UI.toast("打击音已关闭");
        } else if (state.hitsoundMode === "actual") {
          window.UI.toast("打击音按实际击打播放：抢拍会更早、拖拍会更晚，漏接没有声音");
        } else {
          window.UI.toast("打击音按谱面正解时间播放：这是这张图本来的节奏");
        }
      });
    }

    // 打击音音量：独立于回放音量，调到 0 就等于只留谱面原声
    const hv = $("hitVol");
    if (hv) {
      hv.addEventListener("input", () => {
        state.hitsoundVolume = Math.max(0, Math.min(HIT_VOL_MAX, Number(hv.value) / 100));
        if (hitsoundInst) hitsoundInst.setHitVolume(state.hitsoundVolume);
        syncHitVolUi();
        savePrefs();
      });
    }

    $("btnMute").addEventListener("click", () => {
      state.muted = !state.muted;
      applyVolume();
      $("btnMute").classList.toggle("is-on", state.muted);
    });

    // 音量：曲目原声经常压得很大，默认就给 60%，这里能再调（记在本地）
    $("volume").addEventListener("input", (e) => {
      state.volume = Math.max(0, Math.min(1, Number(e.target.value) / 100));
      if (state.volume > 0) state.muted = false;
      applyVolume();
      $("btnMute").classList.toggle("is-on", state.muted);
      syncVolumeUi();
      savePrefs();
    });

    // 下落速度：scrollMs 是"音符从顶端落到判定线的毫秒数"，越小越快
    $("scroll").addEventListener("input", (e) => {
      state.scrollMs = Number(e.target.value) || 1200;
      syncScrollUi();
      if (state.player) {
        state.player.setOptions({ scrollMs: state.scrollMs });
        state.player.render();
      }
      savePrefs();
    });

    // 音画偏移：只影响"谱面时间"相对音频的平移，不改数据
    $("offset").addEventListener("input", (e) => {
      state.audioOffset = Number(e.target.value) || 0;
      syncOffsetUi();
      if (state.player) {
        state.player.setTime(currentTimeMs());
        state.player.render();
      }
      updateTransport();
      savePrefs();
    });
  }

  // 判定口径、长条算法（调试）、低可信度过滤
  function bindJudgeControls() {
    // 判定口径：点一下就换视图，不重新分析
    const judgeBox = $("judgeToggle");
    if (judgeBox) {
      judgeBox.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest ? ev.target.closest("[data-judge]") : null;
        if (!btn) return;
        setJudgeMode(btn.getAttribute("data-judge"));
      });
    }

    // 判定算法（调试）：C/D 切换后按最后一次回放来源重新分析
    const algoBox = $("judgeAlgoToggle");
    if (algoBox) {
      algoBox.addEventListener("click", (ev) => {
        const btn = ev.target && ev.target.closest ? ev.target.closest("[data-algo]") : null;
        if (!btn) return;
        setJudgeAlgo(btn.getAttribute("data-algo"));
      });
      algoBox.querySelectorAll("[data-algo]").forEach((b) => {
        b.classList.toggle("is-active", b.getAttribute("data-algo") === state.judgeAlgo);
      });
    }

    $("btnHideLowConf").addEventListener("click", () => {
      setHideLowConfidence(!state.hideLowConfidence);
    });
    $("hideLowConfidence").addEventListener("change", (ev) => {
      setHideLowConfidence(!!(ev.target && ev.target.checked));
    });
    $("confThreshold").addEventListener("change", updateConfThreshold);
    syncLowConfidenceUI();

    const perfToggle = $("perfPanelToggle");
    if (perfToggle) {
      perfToggle.addEventListener("change", (ev) => {
        applyPerfPanel(!!(ev.target && ev.target.checked));
        savePrefs();
      });
    }
  }

  // 回放页的显示开关：主题、只看失误
  function bindViewToggles() {
    document.querySelectorAll("#themeToggle [data-theme]").forEach((btn) => {
      btn.addEventListener("click", () => applyTheme(btn.dataset.theme));
    });

    document.querySelectorAll("#fpsCapToggle [data-fps]").forEach((btn) => {
      btn.addEventListener("click", () => applyFpsCap(btn.dataset.fps));
    });

    const motion = $("disableMotion");
    if (motion) {
      motion.addEventListener("change", (ev) => {
        applyMotionPref(!!(ev.target && ev.target.checked));
        savePrefs();
      });
    }

    $("btnToggleErrors").addEventListener("click", () => {
      state.errorsOnly = !state.errorsOnly;
      $("btnToggleErrors").classList.toggle("is-on", state.errorsOnly);
      [state.player, state.player2].forEach((pl) => {
        if (!pl) return;
        pl.setOptions({ judgementFilter: state.errorsOnly ? "errors" : "all" });
        pl.render();
      });
      window.UI.toast(state.errorsOnly ? "只显示有瑕疵的音符" : "显示全部音符");
    });
  }

  // 对比：选文件 → 分析 → 校验 → 并排
  function bindCompareControls() {
    $("btnCompare").addEventListener("click", () => {
      if (!state.data) {
        window.UI.toast("先导入一份回放", true);
        return;
      }
      $("fileCompare").click();
    });
    $("fileCompare").addEventListener("change", (ev) => {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = "";   // 允许重复选同一个文件
      if (f) startCompare(f);
    });
    $("btnCloseCompare").addEventListener("click", () => closeCompare());
  }

  function bindPlaybackControls() {
    bindTransportControls();
    bindJudgeControls();
    bindViewToggles();
    bindCompareControls();
  }

  // 失误清单的筛选（类别单选 + 只看严重）与"再看 50 条"
  function bindErrorFilter() {
    document.querySelectorAll("#errorFilters .chip-btn[data-cat], #errorFilters .chip-btn[data-sev]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.sev) {
          state.errorFilter.severe = !state.errorFilter.severe;
          btn.classList.toggle("is-on", state.errorFilter.severe);
        } else {
          state.errorFilter.cat = btn.dataset.cat;
          document.querySelectorAll("#errorFilters .chip-btn[data-cat]").forEach((b) => {
            b.classList.toggle("is-on", b === btn);
          });
        }
        state.errorShown = ERROR_PAGE;   // 换筛选条件后回到第一页，不然会停在"第 300 条"
        renderErrorList();
      });
    });
    $("btnMoreErrors").addEventListener("click", () => {
      state.errorShown += ERROR_PAGE;
      renderErrorList();
    });
    const allBtn = $("btnAllErrors");
    if (allBtn) {
      allBtn.addEventListener("click", () => {
        state.errorShown = filteredErrors().length;
        renderErrorList();
      });
    }
  }

  // 页签、窗口尺寸、键盘快捷键
  function bindNavigation() {
    document.querySelectorAll(".rail-btn").forEach((b) => {
      b.addEventListener("click", () => switchTab(b.dataset.tab));
    });

    // 尺寸变化：图表与播放器都要重画，节流到 160ms 避免拖动窗口时反复重绘
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        redrawAllCharts();
        resizePlayer();
      }, 160);
    });

    // 空格播放/暂停（回放页与时间轴页，且焦点不在输入控件上，也没有弹窗挡着）
    window.addEventListener("keydown", (e) => {
      const onPlayableTab = state.tab === "replay" || state.tab === "timeline";
      // 弹窗开着时空格属于弹窗（教程里正好在读字），别让底下的回放跟着动
      const modalOpen = ["helpModal", "bmModal", "uploadBlocked"].some((id) => {
        const m = $(id);
        return m && !m.hidden;
      });
      if (e.code === "Space" && onPlayableTab && !modalOpen
          && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault();
        togglePlay();
      }
    });
  }

  function bindEvents() {
    bindRescueActions();
    bindImportActions();
    bindDropzone();
    bindPlaybackControls();
    bindErrorFilter();
    bindNavigation();
    bindFinder();
    bindHelp();
  }

  // 帮助弹窗：分页教程。页号只存在闭包里 —— 一次会话内翻到哪、合上再开还在哪，
  // 关掉程序重开就从头讲，符合"教程"的预期。
  function bindHelp() {
    let page = 0;

    function show(i) {
      const pages = document.querySelectorAll("#tutPages .tut-page");
      const tabs = document.querySelectorAll("#tutNav .tut-tab");
      if (!pages.length) return;
      page = Math.max(0, Math.min(pages.length - 1, i));
      pages.forEach((el, idx) => el.classList.toggle("is-active", idx === page));
      tabs.forEach((el, idx) => el.classList.toggle("is-active", idx === page));
      const counter = $("tutCounter");
      if (counter) counter.textContent = "第 " + (page + 1) + " / " + pages.length + " 页";
      const prev = $("tutPrev");
      const next = $("tutNext");
      if (prev) prev.disabled = page === 0;
      if (next) next.disabled = page === pages.length - 1;
      const box = $("tutPages");
      if (box) box.scrollTop = 0;
    }

    document.querySelectorAll("#tutNav .tut-tab").forEach((btn) => {
      btn.addEventListener("click", () => show(Number(btn.dataset.page) || 0));
    });
    const prev = $("tutPrev");
    const next = $("tutNext");
    if (prev) prev.addEventListener("click", () => show(page - 1));
    if (next) next.addEventListener("click", () => show(page + 1));

    const open = $("btnHelp");
    if (open) {
      open.addEventListener("click", () => {
        $("helpModal").hidden = false;
        show(page);
      });
    }
    const close = $("btnCloseHelp");
    if (close) close.addEventListener("click", () => closeModal("helpModal"));

    // 弹窗开着时：Esc 关闭、左右方向键翻页
    window.addEventListener("keydown", (e) => {
      const modal = $("helpModal");
      if (!modal || modal.hidden) return;
      if (e.key === "Escape") { closeModal("helpModal"); return; }
      if (e.key === "ArrowRight") { e.preventDefault(); show(page + 1); }
      if (e.key === "ArrowLeft") { e.preventDefault(); show(page - 1); }
    });

    show(0);
  }

  // 备用通道：直接列 _replays 文件夹里的回放
  async function refreshLocalReplays() {
    try {
      const res = await window.API.localReplays();
      const box = $("localFolder");
      const pathEl = $("localFolderPath");
      if (!res || !res.items) return;
      state.localDir = res.dir || "";
      state.localItems = res.items;
      pathEl.textContent = res.dir;
      // 上传被挡的提示里也写着同一个路径：服务端一给出就同步过去，别留占位文案
      const blockedPath = $("blockedPath");
      if (blockedPath && res.dir) blockedPath.textContent = res.dir;
      renderLocalReplayList();
      box.hidden = false;   // 就算一个文件都没有也显示，让人知道该往哪放
    } catch (e) {
      /* 备用通道不可用就不显示 */
    }
  }

  // 本地回放列表：搜索框只在内存里过滤，不重新请求目录
  function renderLocalReplayList() {
    const ul = $("localReplayList");
    if (!ul) return;
    const input = $("localSearch");
    const q = ((input && input.value) || "").trim().toLowerCase();
    const all = state.localItems || [];
    const items = q
      ? all.filter((it) => String(it.name || "").toLowerCase().includes(q))
      : all;
    const count = $("localCount");
    if (count) count.textContent = q ? items.length + " / " + all.length : all.length + " 个";

    window.UI.clearNode(ul);
    items.forEach((it) => {
      const li = el("li", null, it.name);
      li.appendChild(el("span", null, Math.round(it.size / 1024) + " KB"));
      li.addEventListener("click", () => {
        if (state.busy) return;
        state.busy = true;
        state.lastReplay = { kind: "local", name: it.name };
        window.UI.loading(true, "正在读取本地回放…");
        setStage("正在分析本地回放");
        startWaiting();
        window.API.analyzeLocal(it.name, state.judgeAlgo)
          .then((data) => renderAnalysis(data))
          .catch((e) => {
            window.UI.loading(false);
            if (e.needsInput) openBeatmapModal(e.payload || {});
            else window.UI.toast(e.message || "分析失败", true);
          })
          .finally(() => {
            stopWaiting();
            state.busy = false;
          });
      });
      ul.appendChild(li);
    });
    if (!items.length) {
      const msg = all.length
        ? "没有匹配「" + q + "」的回放"
        : "这个文件夹现在是空的——把 .osr 复制进去，再点「刷新列表」";
      ul.appendChild(el("li", "muted", msg));
    }
  }

  // ---------------------------------------------------------------- 快捷导入
  // 交互约定：先在结算屏按 F2 导出，再按组合键（默认 Shift+F2）。
  // 服务端用长轮询：热键一按下就把它叫醒，所以"正在分析"的提示几乎立刻出现，
  // 不用等几秒后的下一次轮询——否则用户会以为没触发，再按一次。
  let quickToken = 0;
  let quickLoading = false;

  function quickLoadingOn() {
    if (quickLoading) return;
    quickLoading = true;
    window.UI.loading(true, "已接到回放，正在分析…");
  }

  function quickLoadingOff() {
    if (!quickLoading) return;
    quickLoading = false;
    window.UI.loading(false);
  }

  async function pollQuickImport() {
    const startedAt = Date.now();
    try {
      const res = await window.API.quickPending(quickToken, 25);
      if (!res || !res.ok) return;
      // 服务端说"开始分析了"：立刻给反馈，哪怕结果还没出来
      if (res.busy) quickLoadingOn();
      if (typeof res.token === "number" && res.token > quickToken) {
        quickToken = res.token;
        if (res.data) {
          await renderAnalysis(res.data);
          window.UI.toast("已接入刚导出的回放");
        } else if (res.error) {
          window.UI.toast(res.error, true);
        }
        quickLoadingOff();
      }
    } catch (e) {
      /* 服务没起或已经退出：歇一下再试，别把控制台刷满 */
      await new Promise((r) => setTimeout(r, 1500));
      return;
    }
    // 服务端没实现长轮询时请求会立刻返回，这里兜一下，避免空转打满 CPU
    if (Date.now() - startedAt < 400) {
      await new Promise((r) => setTimeout(r, 400));
    }
  }

  async function quickLoop() {
    for (;;) {
      await pollQuickImport();
    }
  }

  // 启动时先把当前 token 收下：页面刷新不该把上一次的快捷导入结果再放一遍
  async function initQuickImport() {
    try {
      const res = await window.API.quickPending(0, 0);
      if (res && typeof res.token === "number") quickToken = res.token;
    } catch (e) {
      /* 拿不到就保持 0，之后照常轮询 */
    }
    void quickLoop();
  }

  // ---------------------------------------------------------------- 快捷键设置
  async function loadHotkeySettings() {
    const input = $("hotkeyCombo");
    const status = $("hotkeyStatus");
    if (!input || !status) return;
    status.className = "dir-status";
    status.textContent = "读取中…";
    try {
      const info = await window.API.hotkey();
      input.value = info.combo || "";
      status.className = "dir-status " + (info.active ? "good" : "bad");
      status.textContent = info.active
        ? "已启用：" + info.combo
        : (info.combo ? "已设置但没注册上：" + (info.error || "未知原因") : "当前未启用快捷导入");
    } catch (e) {
      status.textContent = "读取失败：" + (e.message || e);
    }
  }

  async function saveHotkeySettings() {
    const input = $("hotkeyCombo");
    const status = $("hotkeyStatus");
    if (!input || !status) return;
    const combo = input.value.trim();
    status.className = "dir-status";
    status.textContent = "正在注册 " + (combo || "（关闭）") + "…";
    try {
      const info = await window.API.saveHotkey(combo);
      status.className = "dir-status " + (info.active ? "good" : "bad");
      status.textContent = info.active
        ? "已启用：" + info.combo
        : "当前未启用快捷导入";
      input.value = info.combo || "";
      window.UI.toast(info.active ? "快捷键已生效：" + info.combo : "快捷导入已关闭");
    } catch (e) {
      status.className = "dir-status bad";
      status.textContent = e.message || "保存失败";
    }
  }

  // ---------------------------------------------------------------- 成长
  // 折算规则、段位换算都在服务端（app/core/progress.py），这里只负责选段位和摆结果。
  // 段位阶梯要和后端 LADDER 一致：少一档，选了 Epsilon 就会出现"目标段位不在列表里"。
  const REFORM_OPTIONS = [
    { v: -3, t: "INTRO-1" }, { v: -2, t: "INTRO-2" }, { v: -1, t: "INTRO-3" },
    { v: 1, t: "1 段" }, { v: 2, t: "2 段" }, { v: 3, t: "3 段" }, { v: 4, t: "4 段" },
    { v: 5, t: "5 段" }, { v: 6, t: "6 段" }, { v: 7, t: "7 段" }, { v: 8, t: "8 段" },
    { v: 9, t: "9 段" }, { v: 10, t: "10 段" },
    // Alpha 起一律 1 段一跳：Alpha 就是 11 段，与后端 LADDER 保持一致
    { v: 11, t: "Alpha" }, { v: 12, t: "Beta" }, { v: 13, t: "Gamma" },
    { v: 14, t: "Delta" }, { v: 15, t: "Epsilon" },
    { v: 16, t: "Zeta" }, { v: 17, t: "Eta" },
  ];
  // Malody v3 的段位顺序是 Regular 0~10 → Extra 1~9 → Extra Final。
  // 值只是传给后端的选择码（11~19 表示 Extra 1~9、20 表示 Final），别拿它当段位用。
  const MALODY_OPTIONS = [
    ...Array.from({ length: 11 }, (_, i) => ({ v: i, t: "Regular " + i })),
    ...Array.from({ length: 9 }, (_, i) => ({ v: 11 + i, t: "Extra " + (i + 1) })),
    { v: 20, t: "Extra Final" },
  ];

  function fillDanOptions(type, current) {
    const sel = $("pgValue");
    if (!sel) return;
    const list = type === "malody" ? MALODY_OPTIONS : REFORM_OPTIONS;
    sel.replaceChildren(...list.map((o) => {
      const opt = document.createElement("option");
      opt.value = String(o.v);
      opt.textContent = o.t;
      return opt;
    }));
    const keep = list.find((o) => String(o.v) === String(current));
    if (keep) sel.value = String(keep.v);
  }

  // 计入评估的回放：综合是 50 格（5×10），单个练法 10 格。没填满的格子留空，
  // 玩家一眼就能看出"还差多少局"，也看得见具体是哪几把在算数。
  let pgState = { open: null, data: null };

  function playCell(p) {
    const cell = document.createElement("div");
    cell.className = "pg-cell";
    cell.title = [(p.artist ? p.artist + " - " : "") + p.title, "[" + (p.difficulty || "") + "]",
      Number(p.acc || 0).toFixed(2) + "%", "谱面 " + Number(p.dan || 0).toFixed(2) + " 段",
      "折算 " + Number(p.value || 0).toFixed(2) + " 段",
      p.note || (p.skill ? "练法：" + p.skill : "")].filter(Boolean).join(" · ");

    const body = document.createElement("div");
    body.className = "pg-cell-body";
    const title = document.createElement("span");
    title.className = "pg-cell-title";
    title.textContent = p.title || "—";
    const diff = document.createElement("span");
    diff.className = "pg-cell-diff";
    diff.textContent = p.difficulty || "";
    const acc = document.createElement("span");
    acc.className = "pg-cell-acc" + (Number(p.acc) < 96 ? " is-low" : "");
    acc.textContent = Number(p.acc || 0).toFixed(2) + "%";
    body.append(title, diff, acc);

    // 右侧竖条：这一局按我们的折算规则值多少段。窄格里放不下整句，
    // 上面一行写"折合"，下面把数字和"段"并在一起，避免拆成三行显得散。
    const side = document.createElement("div");
    side.className = "pg-cell-side";
    const cap = document.createElement("span");
    cap.className = "pg-cell-cap";
    cap.textContent = "折合";
    const val = document.createElement("b");
    val.className = "pg-cell-dan";
    val.textContent = Number(p.value || 0).toFixed(1);
    const unit = document.createElement("span");
    unit.className = "pg-cell-unit";
    unit.textContent = "段";
    val.append(unit);
    side.append(cap, val);

    cell.append(body, side);
    return cell;
  }

  function emptyCell() {
    const cell = document.createElement("div");
    cell.className = "pg-cell is-empty";
    return cell;
  }

  function renderCells(box, plays, total) {
    if (!box) return;
    const list = Array.isArray(plays) ? plays : [];
    const cells = [];
    const n = Math.max(total, Math.min(list.length, total));
    for (let i = 0; i < n; i += 1) {
      cells.push(i < list.length ? playCell(list[i]) : emptyCell());
    }
    box.replaceChildren(...cells);
  }

  // 同一时间只展开一个：综合与四个练法互斥
  function showGrid(which) {
    const data = pgState.data;
    if (!data) return;
    const next = pgState.open === which ? null : which;
    pgState.open = next;

    const hero = $("pgHero");
    if (hero) hero.classList.toggle("is-open", next === "overall");

    document.querySelectorAll(".pg-skill").forEach((el) => {
      el.classList.toggle("is-open", el.dataset.skill === next);
    });

    const grid = $("pgGrid");
    const skillGrid = $("pgSkillGrid");
    const note = $("pgSkillNote");
    if (grid) grid.hidden = next !== "overall";
    const skillOpen = next !== null && next !== "overall";
    if (skillGrid) skillGrid.hidden = !skillOpen;
    if (note) note.hidden = !skillOpen;

    if (next === "overall") {
      renderCells(grid, (data.overall || {}).plays || [], 50);
    } else if (skillOpen) {
      const s = (data.skills || {})[next] || {};
      if (note) {
        note.textContent = (s.label || next) + "：实际计入 " + (s.used || 0)
          + " 局（每类最多取 10 局）";
      }
      renderCells(skillGrid, s.plays || [], 10);
    }
  }

  function renderProgress(data) {
    const overall = data.overall || {};
    const target = data.target || {};
    const prof = data.profile || {};
    const owner = data.owner || {};

    // 没填本人名字就不出任何评估：否则会把别人的成绩也算进来
    const need = !!data.needPlayer;
    if ($("pgBody")) $("pgBody").hidden = need;
    if (need) return;

    // 每次重绘都收起展开面：数据可能变了，留着旧的会误导
    pgState = { open: null, data: data };
    if ($("pgGrid")) $("pgGrid").hidden = true;
    if ($("pgSkillGrid")) $("pgSkillGrid").hidden = true;
    if ($("pgSkillNote")) $("pgSkillNote").hidden = true;

    const hero = $("pgHero");
    if (hero) hero.hidden = false;

    const warn = $("pgWarn");
    if (warn) {
      if (data.beyond) {
        warn.hidden = false;
        warn.textContent = "Delta 及以上段位超出了这套评估的能力范围：能参考的谱面样本很少，"
          + "MSD 标尺在高段位也不够稳。下面的结果只作参考。";
      } else {
        warn.hidden = true;
      }
    }

    if ($("pgDan")) {
      $("pgDan").textContent = overall.visible && overall.dan != null
        ? Number(overall.dan).toFixed(2) + " 段" : "—";
    }
    if ($("pgSample")) {
      // 只留最要紧的一条：参与评估的局数，以及为什么有些图不算。
      // 别人打的、认不出玩家的数量放在 hover 里，避免这一行读起来像账单。
      const f = data.filters || {};
      // 分母是"样本目标"：没过 25 局门槛时是门槛本身；过了门槛就换成满样本 50 局，
      // 不然会出现「34 / 25 局」这种读起来像出错的比例。
      const full = data.fullSessions || 50;
      const denom = overall.count > data.minSessions ? full : data.minSessions;
      const tip = ["参与评估 " + overall.count + " / " + denom + " 局"];
      if (f.others) tip.push("已排除别人 " + f.others + " 局");
      if (f.unknown) tip.push(f.unknown + " 局认不出玩家");
      if (f.lnHybrid) tip.push("LN / Hybrid " + f.lnHybrid + " 局暂不计入");
      // 这条是规则说明，不是统计：低于门槛的局直接不进估算，数字放括号里就够
      tip.push("仅 v1acc 高于 91% 的成绩进入个人段位估算"
        + (f.lowAcc ? "（已排除 " + f.lowAcc + " 局）" : ""));
      $("pgSample").textContent = overall.count + " / " + denom
        + " 局（LN Hybrid 图暂不进入评价体系）";
      $("pgSample").title = tip.join(" · ");
    }
    if ($("pgTarget")) $("pgTarget").textContent = target.label ? "目标 " + target.label : "";
    const fill = $("pgBarFill");
    if (fill) fill.style.width = Math.round(Math.min(1, Math.max(0, target.progress || 0)) * 100) + "%";

    const hint = $("pgHint");
    if (hint) {
      hint.className = "pg-hint";
      if (!overall.visible) {
        hint.textContent = "已记录 " + overall.count + " 局，再打 " + overall.need
          + " 局才能出综合评估。样本少的时候结果不稳，先按感觉练。";
        hint.classList.add("warn");
      } else if (target.status === "above") {
        hint.textContent = "现在的综合段位已经高出设定 " + Number(-target.gap).toFixed(2)
          + " 段，可以把当前段位往上调一档，进度条才有意义。";
      } else if (target.status === "ready") {
        hint.textContent = "已经够到 " + target.label + "：把这几张稳住，过段就是时间问题。";
      } else if (target.status === "close") {
        hint.textContent = "离 " + target.label + " 还差 " + Number(target.gap).toFixed(2)
          + " 段，最近这 50 局里已经有 " + target.qualified + " 局过了线。";
      } else if (target.gap !== null && target.gap !== undefined) {
        hint.textContent = "离 " + target.label + " 还差 " + Number(target.gap).toFixed(2)
          + " 段，先把同段位稳定打到 96% 以上。";
      } else {
        hint.textContent = "";
      }
      if (overall.preliminary) {
        hint.classList.add("warn");
        hint.textContent += "（样本 " + overall.count + " 局，属于初步评估）";
      }
    }

    const box = $("pgSkills");
    if (!box) return;
    const skills = data.skills || {};
    box.replaceChildren(...Object.keys(skills).map((key) => {
      const s = skills[key];
      const card = document.createElement("div");
      card.className = "pg-skill" + (s.visible ? "" : " is-empty");
      card.dataset.skill = key;
      card.title = "点一下展开这一类计入评估的回放";
      card.addEventListener("click", () => showGrid(key));
      const head = document.createElement("h4");
      head.textContent = s.label + "（" + key + "）";
      const big = document.createElement("b");
      big.textContent = s.visible && s.dan != null ? Number(s.dan).toFixed(2) + " 段" : "样本不足";
      const sub = document.createElement("span");
      sub.textContent = s.visible
        ? "样本 " + s.count + " 局 · 取最好 " + s.used + (s.preliminary ? " · 初步" : "")
        : "已 " + s.count + " 局，还差 " + s.need + " 局";
      card.append(head, big, sub);
      return card;
    }));
  }

  // ---------------------------------------------------------------- 随机找图
  // 筛选全部在本地索引里做（后端 finder.py），前端只管输入、请求和卡片。
  // 之所以把条件也存进 localStorage：找图是"挑着玩"的动作，关掉页面再回来
  // 还得重新调一遍段位区间会很烦。
  const FINDER_KEY = "mania-finder";
  const FINDER_CATS = ["cut", "stack", "spam", "tech", "ln"];
  const FINDER_N = 8;

  // 两套段位下拉：常规用 RC 标尺，面条用 LN 标尺（LN 5~17，16 段起有专门的名字）。
  // 面条段位单独一套是有原因的：面图的 RC 段位和 LN 段位能差好几段，用 RC 段位筛
  // 面条图只会筛出一堆根本不打 LN 的图。
  const FINDER_DAN = {
    rc: [
      { v: -3, t: "Intro 1" }, { v: -2, t: "Intro 2" }, { v: -1, t: "Intro 3" },
      { v: 1, t: "Reform 1" }, { v: 2, t: "Reform 2" }, { v: 3, t: "Reform 3" },
      { v: 4, t: "Reform 4" }, { v: 5, t: "Reform 5" }, { v: 6, t: "Reform 6" },
      { v: 7, t: "Reform 7" }, { v: 8, t: "Reform 8" }, { v: 9, t: "Reform 9" },
      { v: 10, t: "Reform 10" },
      { v: 11, t: "Alpha" }, { v: 12, t: "Beta" }, { v: 13, t: "Gamma" },
      { v: 14, t: "Delta" }, { v: 15, t: "Epsilon" }, { v: 16, t: "Emik Zeta" },
      { v: 17, t: "Thaumiel Eta" }, { v: 18, t: "CloverWisp Theta" },
    ],
    ln: [
      { v: 5, t: "LN 5" }, { v: 6, t: "LN 6" }, { v: 7, t: "LN 7" },
      { v: 8, t: "LN 8" }, { v: 9, t: "LN 9" }, { v: 10, t: "LN 10" },
      { v: 11, t: "LN 11" }, { v: 12, t: "LN 12" }, { v: 13, t: "LN 13" },
      { v: 14, t: "LN 14" }, { v: 15, t: "LN 15" },
      { v: 16, t: "Hypersovae LN 16" }, { v: 17, t: "Lnlism LN 17" },
    ],
  };
  const FINDER_DEFAULT_DAN = { rc: 6, ln: 7 };
  const finder = { mode: "rc", dan: { rc: 6, ln: 7 }, cat: "", seen: [] };

  function loadFinderPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(FINDER_KEY) || "{}");
      if (p.mode === "rc" || p.mode === "ln") finder.mode = p.mode;
      if (FINDER_CATS.indexOf(p.cat) >= 0 || p.cat === "") finder.cat = p.cat || "";
      ["rc", "ln"].forEach((m) => {
        const v = p.dan && Number(p.dan[m]);
        const list = FINDER_DAN[m];
        if (isFinite(v) && list.some((it) => it.v === v)) finder.dan[m] = v;
      });
    } catch (e) { /* 隐私模式下读不到就用默认值 */ }
  }

  function saveFinderPrefs() {
    try {
      localStorage.setItem(FINDER_KEY, JSON.stringify({
        mode: finder.mode, dan: finder.dan, cat: finder.cat,
      }));
    } catch (e) { /* ignore */ }
  }

  // 切类型时段位下拉要整条换掉：常规 21 项、面条 13 项，值域完全不重叠
  function fillFinderDanOptions() {
    const sel = $("fnDan");
    if (!sel) return;
    sel.textContent = "";
    FINDER_DAN[finder.mode].forEach((item) => {
      const opt = document.createElement("option");
      opt.value = String(item.v);
      opt.textContent = item.t;
      sel.appendChild(opt);
    });
    sel.value = String(finder.dan[finder.mode]);
  }

  function syncFinderUi() {
    const mode = $("fnMode");
    if (mode) mode.value = finder.mode;
    fillFinderDanOptions();
    const cat = $("fnCat");
    if (cat) {
      cat.value = finder.cat;
      // 面条图的主键型本来就是 LN，再选键型没有意义
      cat.disabled = finder.mode === "ln";
    }
  }

  // changed 是刚变动的控件 id。切类型时下拉里还是旧模式的值（"6" 在两边都存在），
  // 照着读会把常规的 6 段带进面条，所以那种情况下只认模式，段位回到它自己记着的值。
  function readFinderInputs(changed) {
    const mode = $("fnMode");
    if (mode) finder.mode = mode.value === "ln" ? "ln" : "rc";
    if (changed !== "fnMode") {
      const dan = $("fnDan");
      if (dan) {
        const value = Number(dan.value);
        const list = FINDER_DAN[finder.mode];
        finder.dan[finder.mode] = list.some((it) => it.v === value)
          ? value : FINDER_DEFAULT_DAN[finder.mode];
      }
      const cat = $("fnCat");
      if (cat) finder.cat = FINDER_CATS.indexOf(cat.value) >= 0 ? cat.value : "";
    }
    saveFinderPrefs();
    syncFinderUi();
  }

  function finderCard(it) {
    const card = document.createElement("article");
    card.className = "map-card";

    // 下载绑在封面上：用户明确要求"点封面下载"，所以只有封面是链接，其余不抢点击
    const cover = document.createElement("a");
    cover.className = "map-cover";
    cover.href = it.download;
    cover.target = "_blank";
    cover.rel = "noopener";
    cover.title = "点击封面下载 " + (it.title || "") + "（.osz）";

    const img = document.createElement("img");
    img.src = it.cover;
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    // 封面走外网 CDN，断网或被墙时会 404 —— 退回渐变底色，别显示裂图
    img.addEventListener("error", () => { img.remove(); cover.classList.add("is-blank"); });
    cover.appendChild(img);

    const tip = document.createElement("span");
    tip.className = "map-cover-tip";
    tip.textContent = "点击下载 .osz";
    cover.appendChild(tip);

    // 段位是筛选依据，放左上角（数值形式，方便横着比）；星数退到右上角当参考
    const danBadge = document.createElement("span");
    danBadge.className = "map-dan";
    danBadge.textContent = it.danShort || "";
    cover.appendChild(danBadge);

    const star = document.createElement("span");
    star.className = "map-star";
    star.textContent = "★ " + Number(it.star || 0).toFixed(2);
    cover.appendChild(star);

    const info = document.createElement("div");
    info.className = "map-info";

    const title = document.createElement("div");
    title.className = "map-title";
    title.textContent = it.title || "（无标题）";

    const sub = document.createElement("div");
    sub.className = "map-sub";
    sub.textContent = [it.artist, it.creator].filter(Boolean).join(" · ");

    const ver = document.createElement("div");
    ver.className = "map-version";
    ver.textContent = it.version || "";

    const foot = document.createElement("div");
    foot.className = "map-foot";
    // 段位标签带上估算依据（Roxy / Azusa / Sunny）：同一张图换算法能差一两段，
    // 只给个名字的话，用户遇到"这图怎么会是这难度"就没法判断该信几分。
    // 面条图一律走 Sunny，再标一遍是噪音。
    const danText = [it.dan, it.mode === "ln" ? "" : it.danMethod].filter(Boolean).join(" · ");
    // 面条模式看的是"这张图有多少面条"，主键型往往是 Jack/Stream 反而误导
    const mixText = it.mode === "ln"
      ? "LN " + Math.round(Number(it.lnRatio || 0) * 100) + "%"
      : (it.pattern ? it.pattern + " " + it.patternShare + "%" : "");
    [it.length, danText, mixText]
      .filter(Boolean).forEach((text) => {
        const chip = document.createElement("span");
        chip.className = "map-chip";
        chip.textContent = text;
        foot.appendChild(chip);
      });

    info.append(title, sub, ver, foot);
    card.append(cover, info);
    return card;
  }

  // 区间怎么写给人看：Reform 以上用"5.0~7.0 段"，INTRO 与面条用档位名
  // （0 段以下说"负几段"是错的，那里是 Intro 1~3；LN 段位本来就只有名字）。
  function finderRangeText(used) {
    const labels = [used.danMinLabel, used.danMaxLabel].filter(Boolean).join(" ~ ");
    const lo = Number(used.danMin || 0);
    const hi = Number(used.danMax || 0);
    if (used.mode === "ln" || lo < 0.5) return labels || (lo.toFixed(1) + "~" + hi.toFixed(1));
    const numbers = lo.toFixed(1) + "~" + hi.toFixed(1) + " 段";
    return labels ? numbers + "（" + labels + "）" : numbers;
  }

  function renderFinder(data) {
    const grid = $("fnGrid");
    if (!grid) return;
    grid.textContent = "";
    const items = (data && data.items) || [];
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "find-empty";
      empty.textContent = "这个条件下没有找到图，把段位区间放宽一点或换一类键型再试。";
      grid.appendChild(empty);
    } else {
      items.forEach((it) => {
        grid.appendChild(finderCard(it));
        if (finder.seen.indexOf(it.sid) < 0) finder.seen.push(it.sid);
      });
    }
    if (finder.seen.length > 60) finder.seen = finder.seen.slice(-60);

    const count = $("fnCount");
    if (count) count.textContent = "本批 " + items.length + " 张";
    const note = $("fnNote");
    if (note) {
      const used = (data && data.used) || {};
      const parts = [];
      if (data && data.relaxed) {
        let relax = "±1 段里没凑够，已放宽到 " + finderRangeText(used);
        if (used.cat) relax += "、" + used.cat + " ≥ " + used.share + "%";
        parts.push(relax);
      }
      parts.push("封面来自 osu! 官方 CDN；点封面直接下 .osz（sayobot 镜像），下不动可换 nerinyan");
      note.textContent = parts.join("　·　");
    }
  }

  async function runFinder(again, changed) {
    readFinderInputs(changed);
    const btn = $("btnFind");
    const btnAgain = $("btnFindAgain");
    if (btn) btn.disabled = true;
    if (btnAgain) btnAgain.disabled = true;
    if (!again) finder.seen = [];
    try {
      let data = await window.API.randomCharts({
        mode: finder.mode,
        dan: finder.dan[finder.mode],
        cat: finder.cat,
        exclude: again ? finder.seen : [],
        n: FINDER_N,
      });
      // 符合条件的图都被看过了：清空"已经给过"的列表重来，别让用户对着空列表
      if (again && !((data && data.items) || []).length && finder.seen.length) {
        finder.seen = [];
        data = await window.API.randomCharts({
          mode: finder.mode, dan: finder.dan[finder.mode],
          cat: finder.cat, exclude: [], n: FINDER_N,
        });
      }
      renderFinder(data);
      if (btnAgain) btnAgain.hidden = false;
    } catch (e) {
      window.UI.toast("找图失败：" + ((e && e.message) || "服务没有响应"), true);
    } finally {
      if (btn) btn.disabled = false;
      if (btnAgain) btnAgain.disabled = false;
    }
  }

  function bindFinder() {
    loadFinderPrefs();
    syncFinderUi();
    // 改任一项都直接重新找一批：下拉本身就是"确认"，再点一次按钮是多余动作
    ["fnMode", "fnDan", "fnCat"].forEach((id) => {
      const sel = $(id);
      if (sel) sel.addEventListener("change", () => runFinder(false, id));
    });
    const find = $("btnFind");
    if (find) find.addEventListener("click", () => runFinder(false));
    const again = $("btnFindAgain");
    if (again) again.addEventListener("click", () => runFinder(true));
  }

  async function loadProgress() {
    const status = $("pgStatus");
    try {
      const data = await window.API.progress();
      const prof = data.profile || {};
      if ($("pgType")) $("pgType").value = prof.type || "reform";
      if ($("pgPlayer")) $("pgPlayer").value = prof.player || "";
      fillDanOptions(prof.type || "reform", prof.value);
      renderProgress(data);
      if (status) {
        status.className = "dir-status";
        if (data.needPlayer) {
          status.textContent = "先填自己的 osu! 名字并保存：只有本人的成绩才算进评估。";
          return;
        }
        const owner = data.owner || {};
        if (!prof.dan) {
          status.textContent = "已认出你的记录 " + (owner.mine || 0) + " 局，选一个当前段位保存后就能看进度。";
          return;
        }
        // Malody 的定位要说清是"段位谱实测"，不然和 REFORM 的数值容易被当成同一把尺
        let head = "当前定位 " + (prof.label || "") + "（按 REFORM 标尺 "
          + Number(prof.dan || 0).toFixed(2) + " 段）";
        if (prof.type === "malody" && prof.malodyLabel) {
          head = "当前定位 " + prof.malodyLabel + "（段位谱实测相当于 REFORM "
            + Number(prof.dan || 0).toFixed(2) + " 段）";
        }
        const bits = [head];
        if (owner.others) bits.push("已排除别人打的 " + owner.others + " 局");
        if (owner.unknown) bits.push(owner.unknown + " 局认不出玩家，暂不计入");
        status.textContent = bits.join("，");
      }
    } catch (e) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = "读取失败：" + (e.message || e);
      }
    }
  }

  async function saveProfile() {
    const player = $("pgPlayer") ? $("pgPlayer").value.trim() : "";
    const type = $("pgType") ? $("pgType").value : "reform";
    const value = $("pgValue") ? $("pgValue").value : "";
    const status = $("pgStatus");
    if (!player) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = "先填自己的 osu! 名字或 ID。";
      }
      return;
    }
    if (status) {
      status.className = "dir-status";
      status.textContent = "换算中…";
    }
    try {
      const res = await window.API.saveProfile(player, type, Number(value));
      if (status) {
        status.className = "dir-status good";
        status.textContent = type === "malody"
          ? "已保存：" + (res.profile.malodyLabel || "")
            + "，段位谱实测相当于 REFORM " + Number(res.profile.dan).toFixed(2) + " 段"
          : "已保存：" + (res.profile.label || "");
      }
      await loadProgress();
    } catch (e) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = e.message || "保存失败";
      }
    }
  }

  // ---------------------------------------------------------------- 镜像站测速
  // 走一次真实下载链路（GET + Range 读 1KB），既能测延迟也能确认对方真给数据。
  // 只被拒绝（403/405）的站标黄，别让它看起来跟正常站一样快。
  const MIRROR_LABEL = { sayobot: "sayobot（国内）", nerinyan: "nerinyan", mino: "catboy / mino" };

  function mirrorLevel(ms) {
    // 阈值按"能不能顺畅下几十 MB"定：500ms 左右握手仍然算可用，
    // 一秒往上就明显拖慢补音频了。
    if (ms < 300) return "is-fast";
    if (ms < 1000) return "is-mid";
    return "is-slow";
  }

  function renderMirrorResults(res) {
    const box = $("mirrorResult");
    if (!box) return;
    window.UI.clearNode(box);
    const rows = (res && res.results) || [];
    rows.forEach((it) => {
      const li = document.createElement("li");
      const ok = !!it.ok;
      // 连得上但被拒绝（403 / 405 这类）跟完全连不上要分开：前者不是线路问题，
      // 但补音频同样下不动，不能显示成一个正常延迟。
      const denied = !ok && it.reachable !== false;
      li.className = "mirror-item "
        + (ok ? mirrorLevel(Number(it.ms) || 0) : (denied ? "is-denied" : "is-fail"));
      // 连不上时错误原文可能很长（DNS、证书、超时各不相同），放悬浮提示里，正文只写结论
      li.title = ok ? ("HTTP " + it.status + " · " + it.ms + " 毫秒") : (it.error || "没有回应");
      const name = document.createElement("span");
      name.className = "m-name";
      name.textContent = MIRROR_LABEL[it.name] || it.name;
      const ms = document.createElement("span");
      ms.className = "m-ms";
      // 超时和"直接连不上"分开写：前者多半是线路被墙，后者可能是 DNS 挂了。
      // 错误原文有中文也有英文（timed out / timeout），两种都认。
      const timedOut = /timed out|timeout|超时/i.test(String(it.error || ""));
      ms.textContent = ok ? (it.ms + " ms")
        : (denied && it.status ? ("HTTP " + it.status) : (timedOut ? "超时" : "连接失败"));
      li.append(name, ms);
      box.appendChild(li);
    });
    box.hidden = !rows.length;
  }

  async function testMirrors() {
    const btn = $("btnMirrorTest");
    if (btn) btn.disabled = true;
    window.UI.tasks.start("mirror", "测试镜像站");
    try {
      const res = await window.API.testMirrors();
      renderMirrorResults(res);
      window.UI.tasks.end("mirror");
    } catch (e) {
      window.UI.tasks.end("mirror", {
        error: true, label: "测试镜像站失败", message: (e && e.message) || "网络错误",
      });
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function bindProgress() {
    on("pgType", "change", () => fillDanOptions($("pgType").value, null));
    on("btnSaveProfile", "click", saveProfile);
    // 综合卡片整块可点：展开 5×10 的明细
    on("pgHero", "click", () => showGrid("overall"));
    fillDanOptions("reform", 7);
  }

  // ---------------------------------------------------------------- 启动
  // 建索引期间轮询进度：右上角任务条显示"扫描曲库 + 已扫多少"，
  // 曲库标签同时写张数。索引建完任务条收走，标签换成总张数。
  async function loadLibraryWithProgress() {
    const chipText = $("libChipText");
    let polling = true;
    let started = false;
    const tick = async () => {
      if (!polling) return;
      try {
        const p = await window.API.libraryProgress();
        if (p && p.building) {
          if (!started) {
            // 已经建好索引时不会进这里，也就不会闪一下空任务条
            started = true;
            window.UI.tasks.start("library", "扫描曲库");
          }
          if (chipText) chipText.textContent = "扫描中 " + (p.scanned || 0).toLocaleString() + "…";
          window.UI.tasks.update("library", {
            // 服务端只知道"扫到第几个"，给不出总数，所以用不确定态
            percent: null,
            detail: "已扫描 " + (p.scanned || 0).toLocaleString() + " 张谱面",
          });
        }
      } catch (e) {
        /* 轮询失败不影响主流程：索引建完总会走到下面的 renderLibrary */
      }
    };
    const timer = setInterval(tick, 500);
    void tick();
    try {
      const info = await window.API.library();
      window.UI.renderLibrary(info);
    } catch (e) {
      window.UI.renderLibrary(null);
    } finally {
      polling = false;
      clearInterval(timer);
      window.UI.tasks.end("library");
    }
  }

  async function init() {
    const ver = assetVersion() || "无版本参数";
    beacon("boot", {
      js: ver,
      ui: !!window.UI,
      api: !!window.API,
      charts: !!window.Charts,
      player: !!window.ManiaPlayer,
      cancelBtn: !!document.getElementById("loadingCancel"),
      cancelOnclick: !!(document.getElementById("loadingCancel") || {}).onclick,
      localBlock: !!document.getElementById("localFolder"),
    });
    bindFolds();
    bindEvents();
    bindProgress();
    // 建议里的「跳到 1:12 看这一段」：切到回放页并跳过去，自动开始播
    window.UI.setSeek((ms) => {
      switchTab("replay");
      seekTo(ms, true);
    });
    setPlayIcon(false);
    loadPrefs();
    applyTheme(state.theme);
    // 帧率上限跟着偏好走：这里把 frameMs 定下来，后面的播放/降档都用它
    applyFpsCap(state.fpsCap, { silent: true });
    applyMotionPref(state.disableMotion);
    updateJudgeToggle();
    syncScrollUi();
    syncOffsetUi();
    syncVolumeUi();
    // 勾选框是在 bindEvents 里先按默认值画的，那时还没读本地偏好。
    // 少了这一行，值其实存住了、过滤也生效，但界面永远显示成没勾 —— 看起来就像没保存。
    syncLowConfidenceUI();
    syncHitsoundUi();
    syncHitVolUi();
    applyPerfPanel(state.perfPanel || /(?:^|[?&])perf=1(?:&|$)/.test(location.search));
    syncTabLocks();

    // 首次启动要遍历上千个文件建索引。这一步不能挡住后面的初始化
    // （热键、快捷导入、健康检查都得在它之前就绪），所以放出去异步跑，
    // 期间用顶部细进度条 + 右上角文字告诉用户"程序还在动"。
    void loadLibraryWithProgress();

    // 启动时也做一次版本自愈（拿着旧缓存的标签页能自己刷新）；顺便看看服务端是不是旧进程
    try {
      const health = await window.API.health(4000);
      await checkFrontendVersion(health);
      warnIfStaleCode(health);
      window.UI.renderAbout(health && health.version);
      // 快捷导入的热键没注册上时把原因说出来，否则用户按了没反应会以为程序坏了
      const hk = health && health.hotkey;
      if (hk && hk.ok === false && hk.error) {
        window.UI.toast("快捷导入的热键没注册上：" + hk.error, true);
      }
    } catch (e) { /* 后端没起就不折腾 */ }

    refreshLocalReplays();
    initQuickImport();

    if (!window.Charts || !window.ManiaPlayer) {
      console.warn("图表/播放器模块未加载：", { charts: !!window.Charts, player: !!window.ManiaPlayer });
    }
  }

  // 脚本报错不再静默：弹出来，至少让人知道发生了什么
  window.addEventListener("error", (e) => {
    beacon("js-error", { msg: (e.message || "") + " @" + (e.filename || "") + ":" + (e.lineno || 0) });
    try {
      window.UI.toast("页面脚本报错：" + (e.message || "未知错误"), true);
    } catch (err) {
      /* ignore */
    }
  });

  document.addEventListener("DOMContentLoaded", init);
  window.__app = { state, switchTab, seekTo, handleReplayFile, startCompare, closeCompare };
})();
