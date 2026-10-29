/* 单文件报告用的引导脚本：把已经算好的数据塞进页面，不连任何服务器。
   复用 ui.js 的渲染函数与 charts.js / player.js 的绘图能力，只补上"切页签 / 折叠 / 播放"这些胶水。 */
(function () {
  "use strict";

  const data = window.REPORT_DATA;
  const { $, el, fmtTime, fmtSigned, CATS } = window.UI;

  if (!data) {
    document.body.innerHTML = "<p style='padding:40px;color:#fff'>报告数据缺失</p>";
    return;
  }

  // ---- 基本渲染 ----
  // 报告里的回放也要按回放当时的倍率走（DT 1.5× / HT 0.75×），
  // 否则画面看起来比实际打的时候慢，会低估这张图。
  const reportRate = Number((data.replay && data.replay.rate) || 1) || 1;
  $("emptyState").hidden = true;
  // 成绩卡要读服务端上的 logo，单文件报告里点了也只会导出一张没 logo 的图，
  // 干脆不出现这个按钮
  const shareBtn = document.getElementById("btnShareCard");
  if (shareBtn) shareBtn.hidden = true;
  window.UI.renderTrack(data);
  window.UI.renderHero(data);
  window.UI.renderMsd(data);
  window.UI.renderAbout(data.version);
  // 相似谱面这张卡片在离线报告里不出来：推荐按 Sunny 段位排序，而段位要前端
  // 估算器算、要问后端索引（单文件报告既没有估算器也没有服务），
  // 只靠 MINACalc 的 REFORM 段位去凑会给出另一套口径的结果，宁可不显示。
  window.UI.renderCategories(data);
  window.UI.renderMeta(data);
  window.UI.renderSections(data);
  window.UI.renderPatternShare(data);
  window.UI.renderJudgeBars(data);
  window.UI.renderRawTable(data);

  // 残卷 / 配错谱面的提醒：报告是静态文件，用常驻横幅而不是几秒就消失的提示条
  const notices = [];
  if (data.warning) notices.push(data.warning);
  if (Array.isArray(data.warnings)) notices.push.apply(notices, data.warnings);
  if (notices.length) {
    const box = document.createElement("div");
    box.className = "report-notice";
    box.textContent = notices.join(" ");
    document.body.insertBefore(box, document.body.firstChild);
  }

  const sm = data.summary;
  $("histMeta").textContent = "中位 " + fmtSigned(sm.medianDelta, 1) + "ms · UR " + Number(sm.ur).toFixed(0);
  $("timelineMeta").textContent = "共 " + data.beatmap.notes + " 个物件 · " + sm.flaws + " 次有瑕疵";

  // ---- 失误清单（报告里限制条数，避免文件过大） ----
  const MAX_ERRORS = 200;
  let errorShown = 50;
  const errorFilter = { cat: "all", severe: false };

  // 筛选状态放在一处：列表、计数行、按钮文案全部由它实时算出来。
  // 以前筛选分支各自调 renderErrors 却忘了更新「再看」按钮，数字就停在筛选前。
  function filteredErrors() {
    return (data.errors || []).filter((e) => {
      if (errorFilter.cat !== "all" && e.category !== errorFilter.cat) return false;
      if (errorFilter.severe && e.severity < 3) return false;
      return true;
    });
  }

  function renderErrors() {
    const all = filteredErrors();
    const capped = Math.min(all.length, MAX_ERRORS);
    const list = all.slice(0, Math.min(errorShown, capped));

    const bucketCounts = {};
    all.forEach((e) => {
      const b = Math.floor(e.t / 30000);
      bucketCounts[b] = (bucketCounts[b] || 0) + 1;
    });

    window.UI.renderErrors(list, {
      bucketCounts,
      onSeek: (t) => { switchTab("replay"); seekTo(t); },
    });

    const total = (data.errors || []).length;
    const sum = $("errorSummary");
    if (sum) {
      const parts = ["共 " + total + " 条"];
      if (all.length !== total) parts.push("筛选后 " + all.length + " 条");
      parts.push("已显示 " + list.length + " 条" +
                 (all.length > MAX_ERRORS ? "（报告最多带 " + MAX_ERRORS + " 条）" : ""));
      sum.textContent = parts.join(" · ");
    }

    const remain = Math.max(0, capped - list.length);
    const moreBox = $("errorMore");
    const more = $("btnMoreErrors");
    const allBtn = $("btnAllErrors");
    if (moreBox) moreBox.hidden = remain <= 0;
    else if (more) more.hidden = remain <= 0;
    if (more) more.textContent = "再看 " + Math.min(50, remain) + " 条（剩 " + remain + " 条）";
    if (allBtn) allBtn.hidden = remain <= 50;
  }

  renderErrors();
  $("btnMoreErrors").addEventListener("click", () => {
    errorShown += 50;
    renderErrors();
  });
  const allBtnEl = $("btnAllErrors");
  if (allBtnEl) {
    allBtnEl.addEventListener("click", () => {
      errorShown = filteredErrors().length;
      renderErrors();
    });
  }

  document.querySelectorAll("#errorFilters .chip-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.sev) {
        errorFilter.severe = !errorFilter.severe;
        btn.classList.toggle("is-on", errorFilter.severe);
      } else {
        errorFilter.cat = btn.dataset.cat;
        document.querySelectorAll("#errorFilters .chip-btn[data-cat]").forEach((b) => {
          b.classList.toggle("is-on", b === btn);
        });
      }
      errorShown = 50;   // 换筛选条件后回到第一页
      renderErrors();
    });
  });

  // ---- 页签 ----
  const PANELS = ["overview", "timeline", "errors", "replay", "chart", "details", "about"];
  function switchTab(name) {
    document.querySelectorAll(".rail-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === name));
    PANELS.forEach((t) => {
      const p = $("panel-" + t);
      if (p) p.hidden = t !== name;
    });
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        drawCharts(name);
        if (name === "replay") resizePlayer();
      });
    });
  }
  document.querySelectorAll(".rail-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  // ---- 折叠 ----
  document.querySelectorAll(".fold-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const card = document.getElementById(btn.dataset.fold);
      if (card) card.classList.toggle("is-folded");
    });
  });

  // ---- 图表 ----
  // 配色只有一份实现（ui.js 的 readTheme），这里直接借用：
  // 单文件报告与在线页共用同一套 CSS 变量，各写一套迟早会分叉。
  function theme() {
    return window.UI.readTheme();
  }

  function drawCharts(tab) {
    const Charts = window.Charts;
    if (!Charts) return;
    const th = theme();
    const draw = (id, fn) => {
      const c = $(id);
      if (!c || !c.clientWidth) return;
      try {
        fn(c);
      } catch (e) {
        console.warn("chart", id, e);
      }
    };
    if (tab === "overview") {
      draw("chartHist", (c) => Charts.drawHistogram(c, data.offsetHistogram, { theme: th, width: c.clientWidth, height: 200, median: data.summary.medianDelta }));
    }
    if (tab === "timeline") {
      draw("chartTimeline", (c) => Charts.drawTimeline(c, data.timeline, { theme: th, width: c.clientWidth, height: 300, markTime: timeMs, avgUr: data.summary.ur, onSeek: (t) => seekTo(t) }));
      draw("chartDensity", (c) => Charts.drawDensity(c, data.categories.profile, { theme: th, width: c.clientWidth, height: 220 }));
    }
    if (tab === "chart") {
      draw("chartRadar", (c) => Charts.drawRadar(c, data.msd && data.msd.available ? data.msd.skillsets : null, { theme: th, width: c.clientWidth, height: 380 }));
    }
    if (tab === "details") {
      draw("chartColumns", (c) => Charts.drawColumns(c, data.columnStats, { theme: th, width: c.clientWidth, height: 240 }));
    }
  }

  // ---- 播放器（没有音频，用虚拟时钟） ----
  let player = null;
  let timeMs = 0;
  let playing = false;
  let lastTick = 0;
  const duration = Math.max(1, data.beatmap.duration);

  function ensurePlayer() {
    if (player || !window.ManiaPlayer) return;
    const canvas = $("playfield");
    if (!canvas) return;
    player = window.ManiaPlayer.create(canvas, { scrollMs: 800, judgementFilter: "all", showJudgements: true, theme: theme() });
    player.load({ keys: data.beatmap.keys, notes: data.notes, firstObject: data.beatmap.firstObject, lastObject: data.beatmap.lastObject });
    player.resize();
    player.setTime(timeMs);
    player.render();
    if (player.on) player.on("seek", (t) => seekTo(t));
  }
  function resizePlayer() {
    ensurePlayer();
    if (player) {
      player.resize();
      player.setTime(timeMs);
      player.render();
    }
  }
  let lastFallbackAt = 0;
  // 时间轴上那根播放竖线：整幅重绘只在切页签 / 改窗口时发生，
  // 播放和拖动定位时都靠这里只刷竖线（比整幅重绘便宜得多）。
  function updateTimelineMark() {
    const Charts = window.Charts;
    const c = $("chartTimeline");
    if (!Charts || !c || !c.clientWidth) return;
    if (typeof Charts.updateTimelineMark !== "function") return;
    if (!Charts.updateTimelineMark(c, timeMs)) {
      const now = performance.now();
      if (now - (lastFallbackAt || 0) > 250) {
        lastFallbackAt = now;
        drawCharts("timeline");
      }
    }
  }

  function seekTo(t) {
    timeMs = Math.max(0, Math.min(duration, t));
    if (player) {
      player.setTime(timeMs);
      player.render();
    }
    updateTransport();
    updateTimelineMark();
  }
  function updateTransport() {
    const text = fmtTime(timeMs) + " / " + fmtTime(duration);
    $("playTime").textContent = text;
    const tl = $("playTimeTl");
    if (tl) tl.textContent = text;
    // 与在线页同一个口径：当前这一小段的 v1 准确率
    const accEl = $("accNowTl");
    if (accEl) {
      const v = window.UI.accAt(data.timeline, timeMs);
      const shown = (v === null) ? "—" : v.toFixed(2) + "%";
      if (accEl.textContent !== "此刻 v1 " + shown) accEl.textContent = "此刻 v1 " + shown;
    }
    const pct = (timeMs / duration) * 100;
    $("seek").value = String(Math.round(pct * 10));
    $("seek").style.backgroundSize = pct + "% 100%";
    $("nowTime").textContent = (timeMs / 1000).toFixed(3) + "s";
  }
  function tick(now) {
    if (!playing) return;
    const dt = now - lastTick;
    lastTick = now;
    timeMs += dt * reportRate;
    if (timeMs >= duration) {
      timeMs = duration;
      playing = false;
      setIcon(false);
      updateTransport();
      return;
    }
    if (player) {
      player.setTime(timeMs);
      player.render();
    }
    updateTransport();
    updateTimelineMark();
    requestAnimationFrame(tick);
  }
  // 回放页与时间轴页各有一个播放键，图标要一起翻
  function setIcon(on) {
    const pauseSvg = '<rect x="7.5" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/><rect x="13.1" y="5.5" width="3.4" height="13" rx="1.2" fill="currentColor"/>';
    const playSvg = '<path d="M8 5.5v13l11-6.5-11-6.5Z" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linejoin="round"/>';
    ["icoPlay", "icoPlayTl"].forEach((id) => {
      const ico = $(id);
      if (ico) ico.innerHTML = on ? pauseSvg : playSvg;
    });
  }
  function togglePlay() {
    playing = !playing;
    setIcon(playing);
    if (playing) {
      lastTick = performance.now();
      requestAnimationFrame(tick);
    }
  }
  $("btnPlay").addEventListener("click", togglePlay);
  $("btnPlayTl").addEventListener("click", togglePlay);
  $("seek").addEventListener("input", (e) => seekTo((Number(e.target.value) / 1000) * duration));
  $("btnMute").addEventListener("click", () => window.UI.toast("单文件报告没有内嵌音频"));
  $("btnToggleErrors").addEventListener("click", () => {
    const on = $("btnToggleErrors").classList.toggle("is-on");
    if (player) {
      player.setOptions({ judgementFilter: on ? "errors" : "all" });
      player.render();
    }
  });
  $("speed").addEventListener("change", () => window.UI.toast("单文件报告不支持变速"));
  $("audioNote").textContent = "这是离线报告：只有画面，没有原声。要连音频回放请用 启动分析器.vbs 里的在线版。";

  // ---- 启动 ----
  switchTab("overview");
  updateTransport();
  window.addEventListener("resize", () => {
    drawCharts(document.querySelector(".rail-btn.is-active") ? document.querySelector(".rail-btn.is-active").dataset.tab : "overview");
    resizePlayer();
  });
})();
