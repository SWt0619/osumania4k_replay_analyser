/* 页面装配：状态、事件、图表与播放器联动。 */
(function () {
  "use strict";

  const { $, el, fmtTime, fmtSigned, CATS } = window.UI;

  const state = {
    data: null,
    replayBase64: null,
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
    audioOffset: 0,     // 音画偏移（ms）：正值 = 谱面比音频提前
    volume: 0.6,        // 回放音量 0..1（曲目原声往往很吵，默认压到 60%）
    muted: false,
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
  };

  // 回放页的两个滑杆记在本地，下次打开还是上次的设置
  const PREFS_KEY = "mania-replay-prefs";
  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) return;
      const p = JSON.parse(raw);
      if (typeof p.scrollMs === "number" && p.scrollMs >= 300 && p.scrollMs <= 2600) state.scrollMs = p.scrollMs;
      if (typeof p.audioOffset === "number" && p.audioOffset >= -500 && p.audioOffset <= 500) state.audioOffset = p.audioOffset;
      if (typeof p.volume === "number" && p.volume >= 0 && p.volume <= 1) state.volume = p.volume;
    } catch (e) { /* 隐私模式下拿不到就算了 */ }
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        scrollMs: state.scrollMs,
        audioOffset: state.audioOffset,
        volume: state.volume,
      }));
    } catch (e) { /* ignore */ }
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

  // ---------------------------------------------------------------- 标签页
  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll(".rail-btn").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.tab === name);
    });
    ["overview", "timeline", "errors", "replay", "chart", "details"].forEach((t) => {
      const p = $("panel-" + t);
      if (p) p.hidden = t !== name;
    });
    $("emptyState").hidden = !!state.data;
    // 切到某页时补一次重绘（隐藏时 canvas 尺寸是 0）
    if (state.data) {
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
    if (!state.data) return;
    const Charts = window.Charts;
    if (!Charts) return;
    const d = state.data;
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
      const c = notes[i].j >= 0 && notes[i].j <= 5 ? notes[i].j | 0 : 5;
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
      notes: data2.notes,
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
      notes: state.data.notes,
      firstObject: state.data.beatmap.firstObject,
      lastObject: state.data.beatmap.lastObject,
    });
    applyNoteSkin(state.data);
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

  // ---- 画面刷新：目标 144Hz ----
  // requestAnimationFrame 只在显示器刷新时回调，60Hz 屏就是 60 次/秒 —— 对音游分析来说，
  // 快速拖动时每帧跨过的距离太大（1200ms 窗口 / 60fps ≈ 每帧 8px），看着是"跳"的。
  // 所以：RAF 照用（高刷屏上它自己就是 144），另外补一个 6.94ms 的定时器，
  // 让"计算 + 重绘"也跑到 144；两条路径共用 lastFrame 闸门，不会重复画同一帧。
  // 目标 120Hz；跑不动就自动退到 60（比如集显、后台标签、超大窗口）
  const TARGET_FPS = 120;
  const FPS_FALLBACK = 60;
  let frameMs = 1000 / TARGET_FPS;
  let rafId = 0;
  let timerId = 0;
  let frameSamples = [];

  // 用最近 40 帧的**中位间隔**判断"撑不撑得住"：
  // 偶尔掉一帧不算，连续 40 帧都远超目标才降级（且只降一次，不再回弹）
  function noteFrame(dt) {
    if (!(dt > 0) || dt > frameMs * 4) return; // seek 后的第一帧 dt 很大，是噪声
    frameSamples.push(dt);
    if (frameSamples.length > 40) frameSamples.shift();
    if (frameSamples.length < 40 || frameMs >= 1000 / FPS_FALLBACK - 0.01) return;
    const sorted = frameSamples.slice().sort((a, b) => a - b);
    if (sorted[20] > frameMs * 1.5) {
      frameMs = 1000 / FPS_FALLBACK;
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
    rafId = requestAnimationFrame(tick);
    const wait = Math.max(0, nextFrameAt - performance.now());
    timerId = setTimeout(() => tick(performance.now()), wait);
  }

  function tick(now) {
    if (!state.playing) {
      stopScheduler();
      return;
    }
    // 这一帧离上一帧太近（RAF 和定时器撞在一起了）：丢掉它，等下一拍
    if (now - state.lastFrame < frameMs * 0.8) {
      scheduleNext();
      return;
    }
    const dt = Math.max(0, now - state.lastFrame);
    state.lastFrame = now;
    noteFrame(dt);
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
        accEl.textContent = "此刻 v1 " + shown;
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

  // 把音量/静音状态推给音频元素（换歌、改设置、取消静音都要走这里）
  function applyVolume() {
    if (!state.audio) return;
    state.audio.volume = state.volume;
    state.audio.muted = state.muted;
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

  function updateNowPanel(t) {
    $("nowTime").textContent = (t / 1000).toFixed(3) + "s";
    if (!state.data) return;
    const box = $("nowCats");
    const hint = $("nowHint");

    if (state.compare) {
      renderCompareNow(box, hint, t);
      return;
    }
    hint.hidden = false;

    // 手动选中的那条优先：它是用户主动钉住的，不能被"附近恰好有别的失误"抢走
    const pinned = state.selectedError;
    if (pinned) {
      box.innerHTML = errorChips(pinned);
      // 文案直接复用「失误」清单里的原文（原因 + 副文本），不再自己拼"已选中…"
      const reason = (pinned.reasons && pinned.reasons[0]) || "没有足够证据判断原因";
      hint.textContent = reason + " · " + window.UI.errorBits(pinned);
      return;
    }

    const found = nearestError(t);
    const near = found ? found.err : null;
    const best = found ? found.dist : 1e9;
    if (near && best <= 2500) {
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
  function filteredErrors() {
    if (!state.data) return [];
    const f = state.errorFilter;
    return (state.data.errors || []).filter((e) => {
      if (f.cat !== "all" && e.category !== f.cat) return false;
      if (f.severe && e.severity < 3) return false;
      return true;
    });
  }

  function renderErrorList() {
    const all = filteredErrors();
    const shown = all.slice(0, state.errorShown);
    window.UI.renderErrors(shown, {
      onSeek: (t) => {
        // 点一条失误 = "带我去看这一刻"，所以顺手切到回放页
        switchTab("replay");
        requestAnimationFrame(() => {
          resizePlayer();
          seekTo(t, true);
        });
      },
    });
    const more = $("btnMoreErrors");
    more.hidden = all.length <= state.errorShown;
    more.textContent = "再看 50 条（还有 " + Math.max(0, all.length - state.errorShown) + " 条）";
  }

  // ---------------------------------------------------------------- 主渲染
  function renderAnalysis(data, meta) {
    closeCompare();     // 换了主回放，对比自然失效
    state.data = data;
    state.selectedError = null;
    // 残卷 / 可能配错谱面的提醒都走这里。
    // warnings 以前没有任何前端消费者：MD5 对不上、键数不符这些话只写进了 JSON，
    // 用户看不到，也就等于没提示。
    const notices = [];
    if (data && data.warning) notices.push(data.warning);
    if (data && Array.isArray(data.warnings)) notices.push.apply(notices, data.warnings);
    if (notices.length) window.UI.toast(notices.join(" "), true);
    // 上一局的播放状态必须清干净，否则进度条和「此刻」面板还停在上个回放的位置
    // （虚拟时钟不会被换谱自动归零，失误索引缓存按条数判断、条数一样时也会脏）。
    pause();
    state.virtualTime = 0;
    state.audioBroken = false;
    state._errTimes = null;
    state._tlFallbackAt = 0;
    window.UI.loading(false);
    $("emptyState").hidden = true;

    window.UI.renderTrack(data);
    window.UI.renderHero(data);
    window.UI.renderMsd(data);
    window.UI.renderCategories(data);
    window.UI.renderMeta(data);
    window.UI.renderSections(data);
    window.UI.renderPatternShare(data);
    window.UI.renderJudgeBars(data);
    window.UI.renderRawTable(data);

    // 误差直方图标题里的摘要
    const sm = data.summary;
    $("histMeta").textContent = "中位 " + fmtSigned(sm.medianDelta, 1) + "ms · UR " + Number(sm.ur).toFixed(0);
    $("timelineMeta").textContent = "共 " + data.beatmap.notes + " 个物件 · " + data.summary.flaws + " 次有瑕疵";

    state.errorShown = 50;
    renderErrorList();
    setupAudio(data);
    // 先把「总览」切出来：隐藏状态的 canvas 宽度是 0，画出来会是空白
    switchTab("overview");
    ensurePlayer();
    redrawAllCharts();
    updateTransport();
    updateNowPanel(currentTimeMs());
    // 再补一次：布局稳定后（字体/滚动条落定）重绘，避免第一帧量到 0 宽
    setTimeout(redrawAllCharts, 120);
    window.UI.toast("分析完成：" + data.beatmap.title + " [" + data.beatmap.difficulty + "]");
  }

  function setupAudio(data) {
    const note = $("audioNote");
    if (state.audio) {
      state.audio.pause();
      state.audio = null;
    }
    state.audioBroken = false;
    if (!data.session || !data.media || !data.media.audio) {
      note.hidden = false;
      note.textContent = "本地没找到音频，正在尝试在线补一份…";
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
  let audioFetching = false;
  async function fetchAudioOnline(data) {
    const note = $("audioNote");
    if (!data || !data.session || audioFetching) return;
    audioFetching = true;
    const res = await window.API.fetchAudio(data.session);
    audioFetching = false;
    if (res && res.ok) {
      data.media = data.media || {};
      data.media.audio = true;
      const from = res.source === "cache" ? "本地缓存"
        : res.source === "local" ? "本地曲库"
        : (res.source || "镜像站");
      window.UI.toast("已补上音频（来源：" + from + "）");
      setupAudio(data);
      return;
    }
    if (!note) return;
    note.hidden = false;
    if (res && res.disabled) {
      note.textContent = "本地没找到音频，在线补音频已关闭；回放会用虚拟时钟播放（没有声音）。";
    } else {
      note.textContent = "本地没找到音频，在线补也没成功"
        + (res && res.error ? "（" + res.error + "）" : "")
        + "，回放会用虚拟时钟播放（没有声音）。";
    }
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

  async function handleReplayFile(file) {
    if (!file) return;
    if (state.busy) {
      window.UI.toast("上一次分析还没结束，稍等一下");
      return;
    }
    // 先把明显的错误挡掉，别让它卡在"正在解析"
    if (!/\.osr$/i.test(file.name)) {
      window.UI.toast("这是 " + file.name + "，不是回放文件（需要 .osr）", true);
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      window.UI.toast("这个文件 " + (file.size / 1048576).toFixed(1) + "MB，回放一般只有几百 KB，确认一下选对了没", true);
      return;
    }

    beacon("file-chosen", { name: file.name, size: file.size });
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

      // 上传走 XHR：能显示"已经发出去多少字节"。卡在 0% 就是内容没离开浏览器，
      // 卡在 100% 就是后端在处理——这两种情况的处理方式完全不同，所以必须能区分。
      setStage("正在上传回放");
      beacon("upload-start", { bytes: b64.length });
      let data;
      try {
        data = await window.API.analyzeUpload(
          { replay: b64, beatmap: null, beatmapPath: null, songsRoot: null },
          (loaded, total) => {
            const pct = total ? Math.round((loaded / total) * 100) : 0;
            if (pct >= 100) setStage("上传完成，正在分析");
            else setStage("正在上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
          },
          (xhr) => { state.activeXhr = xhr; }
        );
      } catch (uploadErr) {
        // 直接上传失败/超时（本机安全软件或代理把大 POST 体整包吞掉的典型表现）：
        // 自动改走分片通道——把回放切成 24KB 小片逐个发、逐片重试，不再让人干等。
        if (!state.cancelLoading) throw uploadErr; // 用户已经点了取消
        beacon("fallback-chunked", { reason: (uploadErr && uploadErr.message) || "?" });
        state.activeXhr = null;
        data = await window.API.analyzeUploadChunked(
          b64,
          (loaded, total) => {
            const pct = total ? Math.round((loaded / total) * 100) : 0;
            setStage("分片上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
          },
          (text) => setStage(text)
        );
      }
      renderAnalysis(data);
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

  async function handleBeatmapFile(file) {
    if (!file) return;
    if (!/\.osu$/i.test(file.name)) {
      window.UI.toast("谱面文件需要是 .osu（你选的是 " + file.name + "）", true);
      return;
    }
    if (state.busy) {
      window.UI.toast("上一次分析还没结束，稍等一下");
      return;
    }
    state.busy = true;
    window.UI.loading(true, "正在用你选的谱面重新分析…");
    window.__cancelLoading = function () { hideLoading(); stopWaiting(); state.busy = false; };
    startWaiting();
    try {
      const b64 = await window.API.fileToBase64(file);
      const data = await window.API.analyze(state.replayBase64, b64);
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

  function openBeatmapModal(payload) {
    const hint = $("bmModalHint");
    hint.textContent = "回放里的谱面 MD5 是 " + ((payload && payload.error) || "未知") + "。可以手动选 .osu，或按曲名搜索本机曲库。";
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
    } catch (e) {
      window.UI.toast(e.message || "搜索失败", true);
    }
  }

  async function useLibraryBeatmap(path) {
    window.UI.loading(true, "正在用曲库里的谱面分析…");
    try {
      const data = await window.API.analyze(state.replayBase64, null, null, path);
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
    $("fileOsr").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";   // 清空才能连续选同一个文件
      handleReplayFile(f);
    });
    $("fileOsu").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      handleBeatmapFile(f);
    });

    $("btnHelp").addEventListener("click", () => ($("helpModal").hidden = false));
    $("btnCloseHelp").addEventListener("click", () => closeModal("helpModal"));
    $("btnCloseModal").addEventListener("click", () => closeModal("bmModal"));
    $("btnPickOsu").addEventListener("click", () => $("fileOsu").click());
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
      if (/\.osu$/i.test(f.name)) handleBeatmapFile(f);
      else handleReplayFile(f);
    });
  }

  // 播放器控件：播放/进度/速度/静音/音量/下落速度/音画偏移/只看失误
  function bindPlaybackControls() {
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
    $("btnMute").addEventListener("click", () => {
      if (!state.audio) return;
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

    // 对比：选文件 → 分析 → 校验 → 并排
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

  // 失误清单的筛选（类别单选 + 只看严重）与"再看 50 条"
  function bindErrorFilter() {
    document.querySelectorAll("#errorFilters .chip-btn").forEach((btn) => {
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
        state.errorShown = 50;   // 换筛选条件后回到第一页，不然会停在"第 300 条"
        renderErrorList();
      });
    });
    $("btnMoreErrors").addEventListener("click", () => {
      state.errorShown += 50;
      renderErrorList();
    });
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

    // 空格播放/暂停（回放页与时间轴页，且焦点不在输入控件上）
    window.addEventListener("keydown", (e) => {
      const onPlayableTab = state.tab === "replay" || state.tab === "timeline";
      if (e.code === "Space" && onPlayableTab && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
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
  }

  // 备用通道：直接列 _replays 文件夹里的回放
  async function refreshLocalReplays() {
    try {
      const res = await window.API.localReplays();
      const box = $("localFolder");
      const ul = $("localReplayList");
      const pathEl = $("localFolderPath");
      if (!res || !res.items) return;
      state.localDir = res.dir || "";
      pathEl.textContent = res.dir;
      // 上传被挡的提示里也写着同一个路径：服务端一给出就同步过去，别留占位文案
      const blockedPath = $("blockedPath");
      if (blockedPath && res.dir) blockedPath.textContent = res.dir;
      window.UI.clearNode(ul);
      res.items.forEach((it) => {
        const li = el("li", null, it.name);
        li.appendChild(el("span", null, Math.round(it.size / 1024) + " KB"));
        li.addEventListener("click", () => {
          if (state.busy) return;
          state.busy = true;
          window.UI.loading(true, "正在读取本地回放…");
          setStage("正在分析本地回放");
          startWaiting();
          window.API.analyzeLocal(it.name)
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
      box.hidden = false;   // 就算一个文件都没有也显示，让人知道该往哪放
      if (res.items.length === 0) {
        const li0 = el("li", "muted", "这个文件夹现在是空的——把 .osr 复制进去，再点「刷新列表」");
        ul.appendChild(li0);
      }
    } catch (e) {
      /* 备用通道不可用就不显示 */
    }
  }

  // ---------------------------------------------------------------- 启动
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
    setPlayIcon(false);
    loadPrefs();
    syncScrollUi();
    syncOffsetUi();
    syncVolumeUi();

    try {
      const info = await window.API.library();
      window.UI.renderLibrary(info);
    } catch (e) {
      window.UI.renderLibrary(null);
    }

    // 启动时也做一次版本自愈（拿着旧缓存的标签页能自己刷新）；顺便看看服务端是不是旧进程
    try {
      const health = await window.API.health(4000);
      await checkFrontendVersion(health);
      warnIfStaleCode(health);
    } catch (e) { /* 后端没起就不折腾 */ }

    refreshLocalReplays();

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
