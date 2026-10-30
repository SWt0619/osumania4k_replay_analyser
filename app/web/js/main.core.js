/* 页面装配的基础层：全局 state、本地偏好、性能面板、心跳、标签页与图表调度。

   从这里开始页面主体拆成八个模块，共用 window.App：跨模块调用的函数挂在 App 上，
   模块内部仍用局部名字。加载顺序写在 index.html 里 —— core 必须最先（它建 state），
   main 必须最后（DOMContentLoaded 时启动）。 */
(function (App) {
  "use strict";

  const { $ } = window.UI;

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
    scrollMs: 800,      // 下落时间：音符从画布顶端落到判定线的毫秒数（越小越快）
    // 跟打键位：按 KeyboardEvent.code 存，换输入法/键盘布局不受影响
    playKeys: ["KeyD", "KeyF", "KeyJ", "KeyK"],
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
      if (typeof p.scrollMs === "number" && p.scrollMs >= 250 && p.scrollMs <= 800) state.scrollMs = p.scrollMs;
      // 四个键必须互不相同：两个列绑同一个键，按下时只会命中靠前的那一列
      if (Array.isArray(p.playKeys) && p.playKeys.length === 4
          && p.playKeys.every((k) => typeof k === "string" && k)
          && new Set(p.playKeys).size === 4) {
        state.playKeys = p.playKeys.slice();
      }
      if (App.FPS_CAPS.indexOf(p.fpsCap) >= 0) state.fpsCap = p.fpsCap;
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
        playKeys: state.playKeys,
        fpsCap: state.fpsCap,
        audioOffset: state.audioOffset,
        volume: state.volume,
        hitsoundMode: state.hitsoundMode,
        hitsoundVolume: state.hitsoundVolume,
        judgeMode: state.judgeMode,
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
    const dropped = App.frameMs > App.capToMs(state.fpsCap) + 0.01;
    const target = Math.round(1000 / App.frameMs);
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
    // 「按…」的改键状态只在回放页有意义，切走就收掉：
    // 否则下一次按键（哪怕是搜索框里打字）会被它吞掉并写成新键位
    if (name !== "replay" && App.cancelPlayalongCapture) App.cancelPlayalongCapture();
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
    if (name === "about") {
      App.checkUpdate(false);
      App.loadChangelog();
    }
    if (name === "progress") App.loadProgress();
    if (name === "settings") {
      App.loadPathSettings();
      App.loadHotkeySettings();
      App.loadCacheSettings();
    }
    // 切到某页时补一次重绘（隐藏时 canvas 尺寸是 0）
    if (state.data || state.chartOnly) {
      // 播放器等到真的要看回放页再建：几万物件时 load() 要复制一份 notes，
      // 放在首屏会和总览渲染抢主线程。
      if (name === "replay") App.ensurePlayer();
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          drawChartsForTab(name);
          if (name === "replay") App.resizePlayer();
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
          markTime: App.currentTimeMs(),
          startMs: App.songStartMs(),  // 和回放页同一个口径：砍掉没有物件的前奏
          avgUr: d.summary.ur,     // 本次游玩的平均 UR，画成参考线
          onSeek: (t) => App.seekTo(t, true),
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

  // 其它模块加载时要用到同一份状态对象
  App.state = state;

  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    loadPrefs, savePrefs, applyPerfPanel, startPerfLoop, stopPerfLoop, perfCostSample,
    perfMedian, perfRenderFps, paintPerf, beacon, heartbeat, syncTabLocks, switchTab,
    bindFolds, drawChartsForTab, redrawAllCharts, ERROR_PAGE, ERROR_BUCKET_MS, HIT_VOL_MAX,
    readTheme,
  });
})(window.App = window.App || {});
