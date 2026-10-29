/* 结果展示：失误清单、相似谱面、判定口径切换、总览渲染、音频准备。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $, fmtSigned } = window.UI;

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
    const f = App.state.errorFilter || {};
    const cat = f.cat || "all";
    if (cat !== "all" && e.category !== cat) return false;
    if (f.severe && e.severity < 3) return false;
    return true;
  }

  function filteredErrors() {
    if (!App.state.data) return [];
    return (App.state.data.errors || []).filter((e) => {
      if (!matchesErrorFilters(e)) return false;
      if (App.state.hideLowConfidence && e.confidence != null && e.confidence < App.state.confThreshold) {
        return false;
      }
      return true;
    });
  }

  function renderErrorList() {
    const all = filteredErrors();
    const total = (App.state.data && App.state.data.errors ? App.state.data.errors.length : 0);
    const base = (App.state.data && App.state.data.errors ? App.state.data.errors : []).filter(matchesErrorFilters);
    const hidden = App.state.hideLowConfidence
      ? base.filter((e) => e.confidence != null && e.confidence < App.state.confThreshold).length
      : 0;
    const shown = all.slice(0, App.state.errorShown);

    // 每 30 秒一段的条数：上千条时靠时间标题分段，比无脑往下滚好找得多
    const bucketCounts = {};
    all.forEach((e) => {
      const b = Math.floor(e.t / App.ERROR_BUCKET_MS);
      bucketCounts[b] = (bucketCounts[b] || 0) + 1;
    });

    window.UI.renderErrors(shown, {
      bucketCounts,
      onSeek: (t, err) => {
        // 点一条失误 = "带我去看这一刻"：切到回放页，**停在音符落下前 0.2 秒**。
        // 不自动播放——直接开播会一下冲过这一击，用户得反复往回快进才看得清。
        // 顺带把这条选中，右侧「此刻」卡片会显示它的错因。
        App.switchTab("replay");
        if (App.state.playing) App.pause();
        requestAnimationFrame(() => {
          App.resizePlayer();
          App.seekTo(Math.max(0, t - 200), false);
          if (err) App.selectError({ note: { t: err.t, c: err.column } });
        });
      },
    });

    // 三个数字都从同一份筛选结果实时算：筛选/展开后立刻刷新，
    // 不再依赖"上一次渲染留下的档位数"。
    const sum = $("errorSummary");
    if (sum) {
      const parts = ["共 " + total + " 条"];
      if (hidden) parts.push("已隐藏 " + hidden + " 条低可信度（<" + Math.round(App.state.confThreshold * 100) + "%）");
      if (all.length !== total) parts.push("当前筛选 " + all.length + " 条");
      parts.push("已显示 " + shown.length + " 条");
      sum.textContent = parts.join(" · ");
    }

    const remain = Math.max(0, all.length - shown.length);
    const moreBox = $("errorMore");
    const more = $("btnMoreErrors");
    const allBtn = $("btnAllErrors");
    if (moreBox) moreBox.hidden = remain <= 0;
    if (more) more.textContent = "再看 " + Math.min(App.ERROR_PAGE, remain) + " 条（剩 " + remain + " 条）";
    if (allBtn) allBtn.hidden = remain <= App.ERROR_PAGE;
  }

  function syncThemeUI() {
    document.querySelectorAll("#themeToggle [data-theme]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.theme === App.state.theme);
    });
  }

  function applyTheme(theme) {
    const next = theme === "sakura" ? "sakura" : "dark";
    App.state.theme = next;
    document.documentElement.setAttribute("data-theme", next);
    App.savePrefs();
    syncThemeUI();
    if (App.state.player) {
      App.state.player.setOptions({ theme: App.readTheme() });
      App.state.player.render();
    }
    if (App.state.data || App.state.chartOnly) App.drawChartsForTab(App.state.tab);
  }

  // 禁用动画：只挂一个属性到 <html>，样式表那边统一关掉进场与过渡。
  // 关的是"显示效果"，不影响任何逻辑——播放器是 canvas 自绘，照样走自己的帧循环。
  function applyMotionPref(on) {
    App.state.disableMotion = !!on;
    const root = document.documentElement;
    if (App.state.disableMotion) root.setAttribute("data-motion", "off");
    else root.removeAttribute("data-motion");
    const cb = $("disableMotion");
    if (cb) cb.checked = App.state.disableMotion;
  }

  function syncLowConfidenceUI() {
    const cb = $("hideLowConfidence");
    if (cb) cb.checked = App.state.hideLowConfidence;
    const input = $("confThreshold");
    if (input) input.value = String(Math.round(App.state.confThreshold * 100));
    const btn = $("btnHideLowConf");
    if (btn) {
      btn.classList.toggle("is-on", App.state.hideLowConfidence);
      btn.textContent = App.state.hideLowConfidence ? "显示全部" : "隐藏低可信度";
    }
  }

  function setHideLowConfidence(on) {
    App.state.hideLowConfidence = !!on;
    App.savePrefs();
    syncLowConfidenceUI();
    App.state.errorShown = 50;
    if (App.state.data) renderErrorList();
  }

  function updateConfThreshold() {
    const input = $("confThreshold");
    if (!input) return;
    let v = Number(input.value);
    if (!Number.isFinite(v)) v = 50;
    v = Math.max(0, Math.min(100, v));
    input.value = String(v);
    App.state.confThreshold = v / 100;
    App.savePrefs();
    App.state.errorShown = 50;
    if (App.state.data) renderErrorList();
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
    window.API.setHistoryDan(key, value, !!(data && data.historyNew))
      .then((resp) => {
        const change = (resp && resp.change) || null;
        if (!change) return;
        // 段位变动由服务端折合（成长页同一套公式），这里只管画。
        // 用回放 key 判断有没有换图：切换 V1 / V2 会从 baseData 重新生成视图，
        // 所以变动要挂在 baseData 上，重画的 hero 才不会丢。
        const rp = (App.state.data && App.state.data.replay) || {};
        const curKey = String(rp.replayMd5 || rp.fileHash || "");
        if (curKey !== key) return;
        if (App.state.baseData) App.state.baseData.danChange = change;
        window.UI.applyDanChange(App.state.data, change);
      })
      .catch(() => { /* 回填失败不影响页面 */ });
  }

  async function loadSimilar(data) {
    const res = await window.UI.applySunnyDan(data);
    if (!res || !res.label || App.state.data !== data) return;
    saveDanToHistory(data, res);
    const items = await window.API.similar(data.session, res.label);
    if (App.state.data !== data) return;   // 期间换了回放，别把上一局的结果画上来
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
      btn.classList.toggle("is-active", btn.getAttribute("data-judge") === App.state.judgeMode);
    });
  }


  function setJudgeMode(mode, silent) {
    if (mode !== "v1" && mode !== "v2" && mode !== "classic") return;
    App.state.judgeMode = mode;
    App.savePrefs();
    updateJudgeToggle();
    if (!App.state.baseData) return;

    const t = App.currentTimeMs();
    App.state.data = viewOf(App.state.baseData, mode);
    App.state._errTimes = null;
    App.state._accShown = null;
    App.state.selectedError = null;
    App.state.errorShown = App.ERROR_PAGE;
    const view = App.state.data;

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
    if (App.state.player) App.ensurePlayer();
    // 对比模式：标杆那一路也要换成同一套口径，否则左右两边的判定色对不上
    if (App.state.compare && App.state.player2) {
      App.state.prog1 = App.buildProgress(App.state.data.notes || []);
      App.state.prog2 = App.buildProgress(App.state.compare.notes || []);
      App.state.player2.load({
        keys: App.state.compare.beatmap.keys,
        notes: App.state.judgeMode === "classic"
          ? (App.state.compare.notes || []).map((n) => Object.assign({}, n, { j: n.jc }))
          : App.state.judgeMode === "v2"
            ? (App.state.compare.notes || []).map((n) => Object.assign({}, n, { j: n.j2 }))
            : App.state.compare.notes,
        firstObject: App.state.compare.beatmap.firstObject,
        lastObject: App.state.compare.beatmap.lastObject,
      });
      App.state.player2.setTime(t);
      App.state.player2.render();
    }
    App.redrawAllCharts();
    App.updateTransport();
    App.updateNowPanel(t);
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
        App.state.data = null;
        App.state.baseData = null;
        App.state.chartOnly = null;
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
      App.state.judgeMode = autoJudge;
      App.savePrefs();
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
    const model = (data.judge && data.judge.model) || "";
    judgeBox.title = "长条判定口径：V1 头尾合并，V2 头尾分判（尾窗口 1.5 倍）。"
      + "已识别为 " + srcLabel + "，自动使用 " + App.state.judgeMode.toUpperCase() + "，可随时手动切换。";
    // lazer 的判定本身就是 320/300/200/100/50 六档，回放头记的 300/100/50 就是
    // 「perfect+great / good / ok+meh」的合并；这里不再用 stable 机器重算，标一句免得误会
    const v1Btn = document.querySelector('#judgeToggle [data-judge="v1"]');
    if (v1Btn) {
      v1Btn.title = model === "lazer"
        ? "这局是 lazer 打的，V1 是把六档并成 300/100/50 的三档视图（与游戏结算一致），只作对照"
        : "ScoreV1：长条整条算一个判定，头尾误差合并";
    }
    if (App.state._autoJudge !== autoJudge) {
      App.state._autoJudge = autoJudge;
      window.UI.toast("已识别 " + srcLabel + "，自动使用 " + App.state.judgeMode.toUpperCase());
    }
  }

  // 换回放时把上一局的播放状态清干净。
  // 不清的话进度条和「此刻」面板还停在上个回放的位置（虚拟时钟不会被换谱自动归零，
  // 失误索引缓存按条数判断、条数一样时也会脏）。
  function resetPlaybackState() {
    App.pause();
    App.state.virtualTime = 0;
    App.state.audioBroken = false;
    App.state._errTimes = null;
    App.state._tlFallbackAt = 0;
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
    App.closeCompare();     // 换了主回放，对比自然失效
    applyJudgeModeFromReplay(data);
    // 原始结果留着（含两套判定），当前视图按首选项挑一套；切换时只换视图
    App.state.baseData = data;
    // 快捷导入没有本地文件句柄，记一下来源，切换判定算法时走另一条重算路径
    if (data.quick) App.state.lastReplay = { kind: "quick" };
    App.state.data = viewOf(data, App.state.judgeMode);
    updateJudgeToggle();
    App.state.selectedError = null;
    // 换了回放，「此刻」卡片的内容整个作废，否则相同时间点的旧失误会让它以为没必要重画
    App.nowSig = "";
    const view = App.state.data;
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

    App.state.errorShown = App.ERROR_PAGE;
    renderErrorList();
    applyReplaySpeed(view.replay);
    setupAudio(view);
    // 先把「总览」切出来：隐藏状态的 canvas 宽度是 0，画出来会是空白
    App.switchTab("overview");
    App.redrawAllCharts();
    App.updateTransport();
    App.updateNowPanel(App.currentTimeMs());
    // 再补一次：布局稳定后（字体/滚动条落定）重绘，避免第一帧量到 0 宽
    setTimeout(App.redrawAllCharts, 120);
    window.UI.toast("分析完成：" + data.beatmap.title + " [" + data.beatmap.difficulty + "]");
  }

  // 回放倍率（DT 1.5× / HT 0.75×）要直接当播放速度用。
  // 否则 DT 的回放会用原速播一遍：音符间距看着比当时宽，像是在说"这张图没那么难"。
  // 仍然保留速度下拉框，玩家想按原速看细节可以自己改回去。
  function applyReplaySpeed(replay) {
    const rate = Number(replay && replay.rate) || 1;
    App.state.speed = rate;
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
    if (App.state.audio) {
      try { App.state.audio.playbackRate = rate; } catch (e) { /* 元数据还没就绪，忽略 */ }
    }
  }

  function setupAudio(data) {
    const note = $("audioNote");
    if (App.state.audio) {
      App.state.audio.pause();
      App.state.audio = null;
    }
    App.state.audioBroken = false;
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
    audio.addEventListener("loadedmetadata", () => App.updateTransport());
    audio.addEventListener("error", () => {
      App.state.audioBroken = true;
      note.hidden = false;
      note.textContent = "音频加载失败，改用虚拟时钟播放（没有声音）。";
    });
    // 音频一准备好就把速度同步过去：不然中途改了速度再换歌会变回 1×
    audio.addEventListener("canplay", () => {
      try { audio.playbackRate = App.state.speed; } catch (e) { /* ignore */ }
    });
    App.state.audio = audio;
    App.applyVolume();
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
    return !!(App.state.data && App.state.data.session && App.state.data.session === session);
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
    const now = App.state.data;
    if (!now || !now.session || now.session === done.session) return;
    if (now.media && now.media.audio) return;
    fetchAudioOnline(now);
  }


  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    renderLnStats, matchesErrorFilters, filteredErrors, renderErrorList, syncThemeUI,
    applyTheme, applyMotionPref, syncLowConfidenceUI, setHideLowConfidence,
    updateConfThreshold, saveDanToHistory, loadSimilar, scheduleSimilar, viewOf,
    updateJudgeToggle, setJudgeMode, rejectUnusableReplay, applyJudgeModeFromReplay,
    resetPlaybackState, showReplayNotices, renderView, renderAnalysis, applyReplaySpeed,
    setupAudio, stopAudioProgress, isCurrentSession, sizeText, audioDetail, startAudioProgress,
    fetchAudioOnline, fetchAudioForCurrent,
  });
})(window.App = window.App || {});
