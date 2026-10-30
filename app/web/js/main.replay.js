/* 回放视图：对比、失误点选、打击音、播放器、时间推进与帧率调度。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $, fmtTime, CATS } = window.UI;

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
    const v = App.state.judgeMode === "classic"
      ? note.jc
      : (App.state.judgeMode === "v2" ? note.j2 : note.j);
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
    if (App.state.player2) {
      try {
        App.state.player2.destroy();
      } catch (e) {
        /* 已经没了就算了 */
      }
      App.state.player2 = null;
    }
    App.state.compare = null;
    App.state.prog2 = null;
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
    if (!file || !App.state.data) return;
    const ref = App.state.data.replay || {};
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
    App.state.compare = data2;
    App.state.prog1 = buildProgress(App.state.data.notes || []);
    App.state.prog2 = buildProgress(data2.notes || []);

    const slot2 = $("pfSlot2");
    if (slot2) slot2.hidden = false;
    const wrap = $("playfieldWrap");
    if (wrap) wrap.classList.add("is-compare");
    const panel = $("panel-replay");
    if (panel) panel.classList.add("is-compare");   // 让整块网格变宽，两个画布才有地方
    const badge1 = $("pfBadge1");
    if (badge1) badge1.hidden = false;

    App.state.player2 = window.ManiaPlayer.create($("playfield2"), {
      scrollMs: App.state.scrollMs,
      rate: App.state.speed,
      judgementFilter: App.state.errorsOnly ? "errors" : "all",
      showJudgements: true,
      // 标杆这一路：判定色固定成白/黄/绿/蓝/紫/红，并且不画"实际击打位置"——
      // 它是参照物，画太多细节反而压过左边真正要分析的那份
      fixedJudgeColors: true,
      showPresses: false,
      theme: App.readTheme(),
    });
    App.state.player2.load({
      keys: data2.beatmap.keys,
      notes: App.state.judgeMode === "v2"
        ? (data2.notes || []).map((n) => Object.assign({}, n, { j: n.j2 }))
        : data2.notes,
      firstObject: data2.beatmap.firstObject,
      lastObject: data2.beatmap.lastObject,
    });
    App.state.player2.resize();
    App.state.player2.setTime(currentTimeMs());
    App.state.player2.render();
    resizePlayer();
    updateNowPanel(currentTimeMs());
    window.UI.toast("已开启对比：左＝本局，右＝标杆");
  }

  // ---------------------------------------------------------------- 回放点选
  // 「此刻」卡片只能提前告诉你"下一条失误在几秒后"，那一刻要是三四个物件一起错，
  // 光看卡片根本读不过来。所以允许直接在画面上点那个物件看原因。
  function findErrorAt(t, column) {
    const errs = (App.state.data && App.state.data.errors) || [];
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
      if (App.isPlayalongActive && App.isPlayalongActive()) return;
      if (!App.state.player || !App.state.data) return;
      const rect = canvas.getBoundingClientRect();
      const hit = App.state.player.pickAt(ev.clientX - rect.left, ev.clientY - rect.top);
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
        if (App.isPlayalongActive && App.isPlayalongActive()) return;
        if (!App.state.data) return; // 没载入回放时保持页面正常滚动
        // 窗口矮到一屏放不下时，滚轮先让给页面：不然想翻到下面的设置卡，
        // 只会把时间轴推来推去。按住 Ctrl 仍然可以强制拖动时间轴。
        const page = document.scrollingElement || document.documentElement;
        const overflowing = page && page.scrollHeight > window.innerHeight + 2;
        if (overflowing && !ev.ctrlKey) return;
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
    const all = App.filteredErrors();
    const idx = all.indexOf(err);
    if (idx < 0) return; // 被筛选条件挡住了，不去动清单
    App.state.errorShown = idx + 1;
    App.renderErrorList();
    window.UI.flashError(err.t, err.column);
  }

  // 选中/取消选中一条失误：画面上的物件套高亮圈，右侧「此刻」卡片钉住它的错因。
  // 不用弹窗 —— 弹层会挡住画面，而且点完就消失，反而看不清。
  function selectError(payload) {
    if (App.isPlayalongActive && App.isPlayalongActive()) return;
    if (!payload || !payload.note) return;
    const err = findErrorAt(payload.note.t, payload.note.c);
    if (!err) {
      clearSelection();
      return;
    }
    if (App.state.selectedError && App.state.selectedError.t === err.t && App.state.selectedError.column === err.column) {
      clearSelection();   // 点同一条 = 取消
      return;
    }
    if (App.state.playing) pause();   // 播放中物件一直在滚，钉住才看得清
    App.state.selectedError = err;
    if (App.state.player && App.state.player.setSelected) App.state.player.setSelected(err.t, err.column);
    repaintPlayer();
    updateNowPanel(currentTimeMs());
    revealErrorRow(err);
  }

  function clearSelection() {
    if (!App.state.selectedError) return;
    App.state.selectedError = null;
    if (App.state.player && App.state.player.setSelected) App.state.player.setSelected(null);
    repaintPlayer();
    updateNowPanel(currentTimeMs());
  }

  function repaintPlayer() {
    if (!App.state.player) return;
    App.state.player.setTime(currentTimeMs());
    App.state.player.render();
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
    const on = App.state.hitsoundMode !== "off";
    const hs = on ? ensureHitsound() : hitsoundInst;
    if (hs) {
      hs.setVolume(App.state.muted ? 0 : App.state.volume);
      hs.setHitVolume(App.state.hitsoundVolume);
      if (on) hs.unlock(App.state.muted ? 0 : App.state.volume);
    }
    if (App.state.player) {
      App.state.player.setOptions({
        hitsound: on ? hs : null,
        hitsoundMode: App.state.hitsoundMode,
      });
    }
    // 对比模式那一路不出声：两路同时响会变成两份叠在一起的节奏，反而听不清哪边有问题
    if (App.state.player2) App.state.player2.setOptions({ hitsound: null, hitsoundMode: "off" });
  }

  // ---------------------------------------------------------------- 播放器
  // 播放器实例只 create 一次，但每次分析都必须重新 load 音符：
  // 换第二份回放时实例已经存在，只更新选项不会换掉上一局的音符。
  function ensurePlayer() {
    if (!App.state.data || !window.ManiaPlayer) return;
    const canvas = $("playfield");
    if (!canvas) return;
    const opts = {
      scrollMs: App.state.scrollMs,
      // 倍速交给播放器折算窗口：谱面时间走得快，落速不该跟着快
      rate: App.state.speed,
      judgementFilter: App.state.errorsOnly ? "errors" : "all",
      showJudgements: true,
      theme: App.readTheme(),
      hitsound: App.state.hitsoundMode === "off" ? null : ensureHitsound(),
      hitsoundMode: App.state.hitsoundMode,
    };
    if (!App.state.player) {
      App.state.player = window.ManiaPlayer.create(canvas, opts);
      if (App.state.player.on) {
        App.state.player.on("seek", (t) => {
          clearSelection();
          seekTo(t, true);
        });
        App.state.player.on("pick", selectError);
      }
      bindReplayHover(canvas);
      bindReplayWheel();
    } else {
      App.state.player.setOptions(opts);
    }
    App.state.player.load({
      keys: App.state.data.beatmap.keys,
      // 播放器只认 note.j：切到 V2 时把 j2 换到 j 上，播放器本身不必知道有两套口径
      notes: App.state.judgeMode === "v2"
        ? App.state.data.notes.map((n) => Object.assign({}, n, { j: n.j2 }))
        : App.state.data.notes,
      firstObject: App.state.data.beatmap.firstObject,
      lastObject: App.state.data.beatmap.lastObject,
    });
    applyNoteSkin(App.state.data);
    syncHitsound();
    App.state.player.resize();
    App.state.player.setTime(currentTimeMs());
    App.state.player.render();
  }

  // 变速时把倍速同步给播放器（含对比那一路）。播放器拿它把 scrollMs 折回真实时间：
  // 只改谱面时间与音频，音符在屏幕上的下落速度保持不变。
  function setPlaybackRate(rate) {
    const r = Number(rate) > 0 ? Number(rate) : 1;
    [App.state.player, App.state.player2].forEach((p) => {
      if (!p || typeof p.setOptions !== "function") return;
      p.setOptions({ rate: r });
      p.render();
    });
  }

  // 把服务端找好的皮肤素材（每列一张图 + 长按条）交给播放器。
  // 找不到素材时 noteImages 全是 null，播放器会自己退回色块。
  function applyNoteSkin(data) {
    if (!App.state.player) return;
    const media = (data && data.media) || {};
    const urls = media.noteImages;
    if (!urls || !urls.some(Boolean)) return;
    try {
      App.state.player.setNoteImages(urls, media.lnBody || null, media.lnTail || null);
    } catch (e) {
      console.warn("皮肤素材加载失败，继续使用色块", e);
    }
  }

  function resizePlayer() {
    ensurePlayer();
    if (App.state.player) {
      App.state.player.setTime(currentTimeMs());
      App.state.player.resize();
      App.state.player.render();
    }
    if (App.state.player2) {
      App.state.player2.setTime(currentTimeMs());
      App.state.player2.resize();
      App.state.player2.render();
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
    if (!App.state.data) return 1;
    const bm = App.state.data.beatmap || {};
    const last = Number(bm.lastObject || 0);
    const relEnd = Number(bm.duration || 0) + Number(bm.firstObject || 0);
    let end = Math.max(last, relEnd) + 1500;
    if (App.state.audio && isFinite(App.state.audio.duration) && App.state.audio.duration > 0) {
      end = Math.min(end, App.state.audio.duration * 1000);
    }
    return Math.max(1, end);
  }

  // 前奏太长时按播放直接跳到第一个音符前 1.5 秒，不然要干等几十秒的空白
  function songStartMs() {
    if (!App.state.data) return 0;
    return Math.max(0, Number(App.state.data.beatmap.firstObject || 0) - 1500);
  }

  function durationMs() {
    return songEndMs();
  }

  // 音画偏移：谱面时间 = 音频时间 + offset。
  // offset 为正 → 谱面跑在音频前面（音符更早到达判定线）；为负则更晚。
  function audioUsable() {
    // 音频元素在，但播放被拒 / 加载失败过 —— 这时不能再拿它的 currentTime 当时间基准，
    // 否则画面会一直停在原地（音频没走，currentTime 也不动）。
    return !!(App.state.audio && !App.state.audioBroken && App.state.audio.readyState >= 1);
  }

  function currentTimeMs() {
    if (audioUsable()) return App.state.audio.currentTime * 1000 + App.state.audioOffset;
    return App.state.virtualTime;
  }

  function seekTo(ms, autoPlay) {
    // 跟打时画布上的点击/滚轮不该动时间轴，时间由跟打自己的循环推
    if (App.isPlayalongActive && App.isPlayalongActive()) return;
    const clamped = Math.max(0, Math.min(durationMs(), ms));
    App.state.virtualTime = clamped;
    if (App.state.audio) {
      try {
        // 反解：要显示到谱面时间 clamped，音频得待在 clamped − offset
        App.state.audio.currentTime = Math.max(0, (clamped - App.state.audioOffset) / 1000);
      } catch (e) {
        /* 元数据还没加载好，忽略 */
      }
    }
    if (App.state.player) {
      App.state.player.setTime(clamped);
      App.state.player.render();
    }
    if (App.state.player2) {
      App.state.player2.setTime(clamped);
      App.state.player2.render();
    }
    updateTransport();
    updateNowPanel(clamped);
    updateTimelineMark();
    if (autoPlay) play();
  }

  function togglePlay() {
    if (App.state.playing) pause();
    else play();
  }

  function play() {
    if (App.isPlayalongActive && App.isPlayalongActive()) return;
    if (!App.state.data) return;

    // 前奏跳过：谱面第一个音符在很后面时，从 0 开始要空等很久，看起来就像"音频和画面没关系"
    const start = songStartMs();
    if (start > 3000 && currentTimeMs() < start - 200) {
      seekTo(start, false);
      window.UI.toast("前奏较长，已从 " + fmtTime(start) + " 开始播放");
    }

    App.state.playing = true;
    setPlayIcon(true);
    // 打击音要在"用户手势"里解锁，点播放是最自然的一次。
    // 没开音效时这里什么也不做，不会白建一个 AudioContext。
    if (App.state.hitsoundMode !== "off") {
      const hs = ensureHitsound();
      if (hs) hs.unlock(App.state.muted ? 0 : App.state.volume);
    }
    if (App.state.audio) {
      App.state.audio.play().then(() => {
        const wasBroken = App.state.audioBroken;
        App.state.audioBroken = false;
        if (wasBroken) {
          // 音频播放失败时画面走的是虚拟时钟，位置已经和音频本体脱开了。
          // 直接开响会从这个错位继续播，所以先把音频拉回当前画面位置。
          try {
            App.state.audio.currentTime = Math.max(0, (App.state.virtualTime - App.state.audioOffset) / 1000);
          } catch (e) { /* ignore */ }
        }
      }).catch(() => {
        // 浏览器拦截 / 音频损坏：退化成虚拟时钟（此时画面照常走，只是没声音）
        App.state.audioBroken = true;
        startVirtualClock();
      });
    } else {
      App.state.audioBroken = false;
      startVirtualClock();
    }
    App.state.lastFrame = performance.now();
    nextFrameAt = App.state.lastFrame + frameMs;
    scheduleNext();
  }

  function startVirtualClock() {
    if (!App.state.playing) return;
    // 有音频但播放失败时，虚拟时钟从"当前音频位置"接着走，
    // 否则画面会停在 0 而音频已经播到一半（另一种"对不上"）。
    if (App.state.audio && App.state.audio.readyState >= 1) {
      App.state.virtualTime = App.state.audio.currentTime * 1000 + App.state.audioOffset;
    }
    if (App.state.virtualTime >= durationMs()) App.state.virtualTime = 0;
  }

  function pause() {
    App.state.playing = false;
    stopScheduler();
    setPlayIcon(false);
    if (App.state.audio) App.state.audio.pause();
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
      btn.classList.toggle("is-active", Number(btn.dataset.fps) === App.state.fpsCap);
    });
  }

  // 换档要立刻生效：正在播时重排下一帧的时间点，
  // 否则得等旧节拍走完才换速，看起来像卡了一下。
  function applyFpsCap(cap, opts) {
    const next = FPS_CAPS.indexOf(Number(cap)) >= 0 ? Number(cap) : 120;
    App.state.fpsCap = next;
    frameMs = capToMs(next);
    fallbackMs = capFallbackMs(next);
    frameSamples = [];
    if (!(opts && opts.silent)) App.savePrefs();
    syncFpsCapUI();
    if (App.state.playing) {
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
    if (!App.state.playing) {
      stopScheduler();
      return;
    }
    // 这一帧离上一帧太近：丢掉，等下一拍。两个来源的容差不一样：
    //   · 定时器：浏览器会把小数延迟截断/合并到整数毫秒，11.11ms 的请求常在 11.0 就来，
    //     容差给 2ms 才不会把自己的帧全拒掉（拒掉的代价是退到下一个 RAF，90 档会掉成 66.7）。
    //   · RAF：容差必须很小，否则它会抢在定时器前面把帧画掉 —— 200Hz 屏上 60 档的 RAF
    //     落在 15ms 处，容差一大就跑成 66.7fps。
    const tolerance = fromTimer ? 2 : 0.5;
    if (now - App.state.lastFrame < Math.max(0.5, frameMs - tolerance)) {
      scheduleNext();
      return;
    }
    const dt = Math.max(0, now - App.state.lastFrame);
    App.state.lastFrame = now;
    noteFrame(dt);
    const perfT0 = performance.now();
    if (!audioUsable()) {
      App.state.virtualTime += dt * App.state.speed;
    }
    const t = currentTimeMs();
    // 音频自己结束了也要停：否则画面会卡在最后一帧继续空转
    if (App.state.audio && App.state.audio.ended) {
      pause();
      updateTransport();
      return;
    }
    if (t >= durationMs()) {
      App.state.virtualTime = durationMs();
      pause();
      updateTransport();
      return;
    }
    if (App.state.player) {
      App.state.player.setTime(t);
      App.state.player.render();
    }
    if (App.state.player2) {
      // 两路必须吃同一个时间：差一帧就对不齐
      App.state.player2.setTime(t);
      App.state.player2.render();
    }
    updateTransport();
    updateNowPanel(t);
    updateTimelineMark();
    // 这一帧花在"计算 + 绘制"上的时间，不含浏览器合成
    App.perfCostSample(performance.now() - perfT0);
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
    if (App.state.tab !== "timeline") return;
    const Charts = window.Charts;
    const c = $("chartTimeline");
    if (!Charts || !c || !c.clientWidth) return;
    if (typeof Charts.updateTimelineMark !== "function") return;
    if (!Charts.updateTimelineMark(c, currentTimeMs())) {
      // 没有静态图层（还没画过 / 尺寸变了）：退回整幅重绘，但限流，别每帧都来一次
      const now = performance.now();
      if (now - (App.state._tlFallbackAt || 0) > 250) {
        App.state._tlFallbackAt = now;
        App.drawChartsForTab("timeline");
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
    if (accEl && App.state.data) {
      const v = window.UI.accAt(App.state.data.timeline, t);
      // 两位小数：99.74% 和 99.7% 是两种手感，位宽也够
      const shown = (v === null) ? "—" : v.toFixed(2) + "%";
      if (App.state._accShown !== shown) {
        App.state._accShown = shown;
        accEl.textContent = "此刻 " + (App.state.judgeMode === "v2" ? "v2 " : "v1 ") + shown;
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
      s.value = String(App.state.scrollMs);
      s.style.backgroundSize = (((App.state.scrollMs - 250) / 550) * 100).toFixed(1) + "% 100%";
    }
    const v = $("scrollVal");
    if (v) v.textContent = App.state.scrollMs + " ms";
  }

  function syncOffsetUi() {
    const s = $("offset");
    if (s) {
      s.value = String(App.state.audioOffset);
      s.style.backgroundSize = (((App.state.audioOffset + 500) / 1000) * 100).toFixed(1) + "% 100%";
    }
    const v = $("offsetVal");
    if (v) v.textContent = (App.state.audioOffset > 0 ? "+" : "") + App.state.audioOffset + " ms";
  }

  function syncVolumeUi() {
    const pct = Math.round(App.state.volume * 100);
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
    if (sel) sel.value = App.state.hitsoundMode;
  }

  // 打击音自己的音量条（和上面那条回放音量互相独立）
  function syncHitVolUi() {
    const pct = Math.round(App.state.hitsoundVolume * 100);
    const s = $("hitVol");
    if (s) {
      s.value = String(Math.min(pct, App.HIT_VOL_MAX * 100));
      // 滑杆量程是 0~300，填充按量程比例算 —— 直接用 pct 会在 100% 时就把整条填满
      s.style.backgroundSize = Math.min(100, pct / App.HIT_VOL_MAX).toFixed(1) + "% 100%";
    }
    const v = $("hitVolVal");
    if (v) v.textContent = pct + "%";
  }

  // 把音量/静音状态推给音频元素（换歌、改设置、取消静音都要走这里）
  function applyVolume() {
    if (App.state.audio) {
      App.state.audio.volume = App.state.volume;
      App.state.audio.muted = App.state.muted;
    }
    // 打击音跟着同一个音量走：只调谱面原声、打击音不跟着变的话，静音时它还在响
    if (hitsoundInst) hitsoundInst.setVolume(App.state.muted ? 0 : App.state.volume);
  }

  // 失误按时间预排序一次，播放时二分找最近的那条。
  // 每帧线性扫几千条会在播放时明显吃 CPU，而帧回调里没有做这件事的预算。
  function errorIndex() {
    if (App.state._errTimes && App.state._errTimes.length === (App.state.data.errors || []).length) return App.state._errTimes;
    const errs = (App.state.data && App.state.data.errors) || [];
    App.state._errTimes = errs.map((e) => e.t);
    return App.state._errTimes;
  }

  function nearestError(t) {
    const errs = (App.state.data && App.state.data.errors) || [];
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
    const a = progressAt(App.state.prog1, t);
    const b = progressAt(App.state.prog2, t);
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
    if (!App.state.data) return;
    const box = $("nowCats");
    const hint = $("nowHint");

    if (App.state.compare) {
      // 对比模式的三张卡片数值是连续变化的，按 100ms 刷新足够肉眼跟得上
      const sig = "c:" + Math.round(t / 100);
      if (sig === nowSig) return;
      nowSig = sig;
      renderCompareNow(box, hint, t);
      return;
    }
    hint.hidden = false;

    // 手动选中的那条优先：它是用户主动钉住的，不能被"附近恰好有别的失误"抢走
    const pinned = App.state.selectedError;
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


  // frameMs 当前实际帧长会被 core 读到；模块内继续用局部变量，外面读写都走这个访问器
  Object.defineProperty(App, "frameMs", {
    get() { return frameMs; },
    set(v) { frameMs = v; },
  });

  // hitsoundInst 绑定时要判断打击音实例在不在；模块内继续用局部变量，外面读写都走这个访问器
  Object.defineProperty(App, "hitsoundInst", {
    get() { return hitsoundInst; },
    set(v) { hitsoundInst = v; },
  });

  // nowSig 切回放时 result 会把它清空；模块内继续用局部变量，外面读写都走这个访问器
  Object.defineProperty(App, "nowSig", {
    get() { return nowSig; },
    set(v) { nowSig = v; },
  });

  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    jCodeOf, buildProgress, progressAt, closeCompare, startCompare, findErrorAt, setPlaybackRate,
    bindReplayHover, bindReplayWheel, revealErrorRow, selectError, clearSelection,
    repaintPlayer, ensureHitsound, syncHitsound, ensurePlayer, applyNoteSkin, resizePlayer,
    songEndMs, songStartMs, durationMs, audioUsable, currentTimeMs, seekTo, togglePlay, play,
    startVirtualClock, pause, setPlayIcon, capToMs, capFallbackMs, syncFpsCapUI, applyFpsCap,
    noteFrame, stopScheduler, scheduleNext, tick, updateTimelineMark, updateTransport,
    syncScrollUi, syncOffsetUi, syncVolumeUi, syncHitsoundUi, syncHitVolUi, applyVolume,
    errorIndex, nearestError, errorChips, renderCompareNow, updateNowPanel, FPS_CAPS,
  });
})(window.App = window.App || {});
