/* 事件绑定：DOM 监听、B50 导出与分享卡。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $, el } = window.UI;

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
      App.beacon("cancel-click", { busy: App.state.busy, hasFn: !!App.state.cancelLoading });
      const fn = App.state.cancelLoading;
      if (fn) fn();
      else App.hideLoading();
      App.state.busy = false;
      App.stopWaiting();
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
    $("btnCheckUpdate").addEventListener("click", () => App.checkUpdate(true));

    $("fileOsr").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";   // 清空才能连续选同一个文件
      App.handleReplayFile(f);
    });
    $("fileChartOsu").addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = "";
      App.handleChartFile(f);
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
      App.handleBeatmapFile(f);
    });

    $("btnCloseModal").addEventListener("click", () => App.closeModal("bmModal"));
    $("btnPickOsu").addEventListener("click", () => $("fileOsu").click());
    // 曲库标签点一下就能手动指定游戏目录（自动找不到时的兜底）
    $("btnSavePaths").addEventListener("click", () => App.savePaths(false));
    if ($("btnClearPaths")) {
      $("btnClearPaths").addEventListener("click", async () => {
        if (!window.confirm("清掉手动指定的游戏目录，改回自动探测？")) return;
        $("pathStable").value = "";
        $("pathLazer").value = "";
        await App.savePaths(true);
      });
    }
    on("btnSaveHotkey", "click", App.saveHotkeySettings);
    on("btnMirrorTest", "click", App.testMirrors);
    document.querySelectorAll(".modal").forEach((m) => {
      m.addEventListener("click", (e) => {
        if (e.target === m) m.hidden = true;
      });
    });

    $("bmQuery").addEventListener("input", (e) => {
      clearTimeout(App.state._searchTimer);
      App.state._searchTimer = setTimeout(() => App.searchBeatmaps(e.target.value), 220);
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
      if (/\.osu$/i.test(f.name)) App.handleChartFile(f);
      else App.handleReplayFile(f);
    });
  }

  // 播放器控件：播放/进度/速度/静音/音量/下落速度/音画偏移
  function bindTransportControls() {
    $("btnPlay").addEventListener("click", App.togglePlay);
    // 时间轴页也要能暂停：那条竖线就是播放位置，只能看不能停最难受
    $("btnPlayTl").addEventListener("click", App.togglePlay);
    $("seek").addEventListener("input", (e) => {
      const pct = Number(e.target.value) / 1000;
      App.seekTo(pct * App.durationMs(), false);
    });
    $("speed").addEventListener("change", (e) => {
      App.state.speed = Number(e.target.value) || 1;
      if (App.state.audio) App.state.audio.playbackRate = App.state.speed;
      // 画面这边的下落时间不跟着倍速走，只把谱面时间轴快进/慢放
      App.setPlaybackRate(App.state.speed);
      window.UI.toast("播放速度 " + App.state.speed + "×（下落速度保持 " + App.state.scrollMs + "ms）");
    });
    // 打击音：关 / 正解音 / 实际击打。切换时顺手解锁 AudioContext ——
    // 浏览器只认"用户手势"这个入口，等播放再解锁会漏掉第一小节。
    const hsSel = $("hitsound");
    if (hsSel) {
      App.syncHitsoundUi();
      hsSel.addEventListener("change", () => {
        const v = hsSel.value;
        App.state.hitsoundMode = (v === "actual" || v === "planned") ? v : "off";
        App.syncHitsound();
        App.savePrefs();
        if (App.state.hitsoundMode === "off") {
          window.UI.toast("打击音已关闭");
        } else if (App.state.hitsoundMode === "actual") {
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
        App.state.hitsoundVolume = Math.max(0, Math.min(App.HIT_VOL_MAX, Number(hv.value) / 100));
        if (App.hitsoundInst) App.hitsoundInst.setHitVolume(App.state.hitsoundVolume);
        App.syncHitVolUi();
        App.savePrefs();
      });
    }

    $("btnMute").addEventListener("click", () => {
      App.state.muted = !App.state.muted;
      App.applyVolume();
      $("btnMute").classList.toggle("is-on", App.state.muted);
    });

    // 音量：曲目原声经常压得很大，默认就给 60%，这里能再调（记在本地）
    $("volume").addEventListener("input", (e) => {
      App.state.volume = Math.max(0, Math.min(1, Number(e.target.value) / 100));
      if (App.state.volume > 0) App.state.muted = false;
      App.applyVolume();
      $("btnMute").classList.toggle("is-on", App.state.muted);
      App.syncVolumeUi();
      App.savePrefs();
    });

    // 下落速度：scrollMs 是"音符从顶端落到判定线的毫秒数"，越小越快
    $("scroll").addEventListener("input", (e) => {
      App.state.scrollMs = Number(e.target.value) || 800;
      App.syncScrollUi();
      // 对比模式有两路画布：两边都跟着改，否则左右落速不一致
      [App.state.player, App.state.player2].forEach((p) => {
        if (!p) return;
        p.setOptions({ scrollMs: App.state.scrollMs });
        p.render();
      });
      App.savePrefs();
    });

    // 音画偏移：只影响"谱面时间"相对音频的平移，不改数据
    $("offset").addEventListener("input", (e) => {
      App.state.audioOffset = Number(e.target.value) || 0;
      App.syncOffsetUi();
      if (App.state.player) {
        App.state.player.setTime(App.currentTimeMs());
        App.state.player.render();
      }
      App.updateTransport();
      App.savePrefs();
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
        App.setJudgeMode(btn.getAttribute("data-judge"));
      });
    }

    $("btnHideLowConf").addEventListener("click", () => {
      App.setHideLowConfidence(!App.state.hideLowConfidence);
    });
    $("hideLowConfidence").addEventListener("change", (ev) => {
      App.setHideLowConfidence(!!(ev.target && ev.target.checked));
    });
    $("confThreshold").addEventListener("change", App.updateConfThreshold);
    App.syncLowConfidenceUI();

    const perfToggle = $("perfPanelToggle");
    if (perfToggle) {
      perfToggle.addEventListener("change", (ev) => {
        App.applyPerfPanel(!!(ev.target && ev.target.checked));
        App.savePrefs();
      });
    }
  }

  // 回放页的显示开关：主题、只看失误
  function bindViewToggles() {
    document.querySelectorAll("#themeToggle [data-theme]").forEach((btn) => {
      btn.addEventListener("click", () => App.applyTheme(btn.dataset.theme));
    });

    document.querySelectorAll("#fpsCapToggle [data-fps]").forEach((btn) => {
      btn.addEventListener("click", () => App.applyFpsCap(btn.dataset.fps));
    });

    const motion = $("disableMotion");
    if (motion) {
      motion.addEventListener("change", (ev) => {
        App.applyMotionPref(!!(ev.target && ev.target.checked));
        App.savePrefs();
      });
    }

    $("btnToggleErrors").addEventListener("click", () => {
      App.state.errorsOnly = !App.state.errorsOnly;
      $("btnToggleErrors").classList.toggle("is-on", App.state.errorsOnly);
      [App.state.player, App.state.player2].forEach((pl) => {
        if (!pl) return;
        pl.setOptions({ judgementFilter: App.state.errorsOnly ? "errors" : "all" });
        pl.render();
      });
      window.UI.toast(App.state.errorsOnly ? "只显示有瑕疵的音符" : "显示全部音符");
    });
  }

  // 对比：选文件 → 分析 → 校验 → 并排
  function bindCompareControls() {
    $("btnCompare").addEventListener("click", () => {
      if (!App.state.data) {
        window.UI.toast("先导入一份回放", true);
        return;
      }
      $("fileCompare").click();
    });
    $("fileCompare").addEventListener("change", (ev) => {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = "";   // 允许重复选同一个文件
      if (f) App.startCompare(f);
    });
    $("btnCloseCompare").addEventListener("click", () => App.closeCompare());
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
          App.state.errorFilter.severe = !App.state.errorFilter.severe;
          btn.classList.toggle("is-on", App.state.errorFilter.severe);
        } else {
          App.state.errorFilter.cat = btn.dataset.cat;
          document.querySelectorAll("#errorFilters .chip-btn[data-cat]").forEach((b) => {
            b.classList.toggle("is-on", b === btn);
          });
        }
        App.state.errorShown = App.ERROR_PAGE;   // 换筛选条件后回到第一页，不然会停在"第 300 条"
        App.renderErrorList();
      });
    });
    $("btnMoreErrors").addEventListener("click", () => {
      App.state.errorShown += App.ERROR_PAGE;
      App.renderErrorList();
    });
    const allBtn = $("btnAllErrors");
    if (allBtn) {
      allBtn.addEventListener("click", () => {
        App.state.errorShown = App.filteredErrors().length;
        App.renderErrorList();
      });
    }
  }

  // 页签、窗口尺寸、键盘快捷键
  function bindNavigation() {
    document.querySelectorAll(".rail-btn").forEach((b) => {
      b.addEventListener("click", () => App.switchTab(b.dataset.tab));
    });

    // 尺寸变化：图表与播放器都要重画，节流到 160ms 避免拖动窗口时反复重绘
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        App.redrawAllCharts();
        App.resizePlayer();
      }, 160);
    });

    // 空格播放/暂停（回放页与时间轴页，且焦点不在输入控件上，也没有弹窗挡着）
    window.addEventListener("keydown", (e) => {
      const onPlayableTab = App.state.tab === "replay" || App.state.tab === "timeline";
      // 弹窗开着时空格属于弹窗（教程里正好在读字），别让底下的回放跟着动
      const modalOpen = ["helpModal", "bmModal", "uploadBlocked", "paResult"].some((id) => {
        const m = $(id);
        return m && !m.hidden;
      });
      if (App.isPlayalongActive && App.isPlayalongActive()) return;
      if (e.code === "Space" && onPlayableTab && !modalOpen
          && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault();
        App.togglePlay();
      }
    });
  }

  function bindEvents() {
    bindRescueActions();
    bindImportActions();
    bindShareCard();
    bindB50Export();
    App.bindReleaseNotes();
    bindDropzone();
    bindPlaybackControls();
    bindErrorFilter();
    bindNavigation();
    App.bindFinder();
    App.bindPlayalong();
    bindHelp();
  }

  // B50：成长页那 50 局摊成一张图。数据就在 pgState.data 里，不用再问一遍后端
  function bindB50Export() {
    const btn = $("btnExportB50");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (!window.B50Card) return;
      const data = App.pgState.data;
      if (!data || !data.overall || !data.overall.count) {
        window.UI.toast("先在成长页填上名字，等评估出来再导");
        return;
      }
      if (btn.disabled) return;
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "导出中…";
      try {
        const ver = String(($("aboutVersion") && $("aboutVersion").textContent) || "")
          .replace(/^v/i, "").trim();
        // 跟着界面主题走：浅粉主题导浅色版（够到目标的卡是金底），其余导深色版（浅粉底）
        const saved = await window.B50Card.save(data, { version: ver, light: App.state.theme === "sakura" });
        window.UI.toast(saved && saved.dir
          ? "B50 已保存到 " + saved.dir + "/" + saved.name
          : "B50 已保存到下载文件夹");
      } catch (e) {
        window.UI.toast("B50 导出失败：" + (e && e.message ? e.message : e), true);
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
  }

  // 成绩卡：点了才画（要读 logo、还要等段位估算），画完直接落成 PNG。
  // 段位优先取谱面难度页上已经算好的那个，没有再自己跑一次估算器。
  // 「Reform 8 mid || 6 low」是面图同时给 RC / LN 的写法，卡片要拆开用
  function splitDanLabel(raw) {
    const parts = String(raw || "").split("||").map((s) => s.trim()).filter(Boolean);
    return { rc: parts[0] || "", ln: parts[1] || "" };
  }

  function bindShareCard() {
    const btn = $("btnShareCard");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (!window.ShareCard) return;
      const data = App.state.data;
      if (!data || !data.ok) {
        window.UI.toast("先导入一份回放，再生成成绩卡");
        return;
      }
      if (btn.disabled) return;
      const label = btn.innerHTML;
      btn.disabled = true;
      btn.textContent = "生成中…";
      try {
        let danLabel = "";
        let danNote = "";
        // 段位下面那行要写清是哪个算法、读数多少：卡片上只挂一个 "Reform 8 mid"，
        // 容易被当成游戏里的段位，而这是本程序按 Sunny → Azusa → Roxy 估出来的
        const shown = $("msdDan");
        const shownNote = $("msdDanNote");
        const shownText = shown ? String(shown.textContent || "").trim() : "";
        if (shownText && shownText !== "—") {
          // 面图上 Sunny 会同时给 RC 与 LN 两个段位，用 "||" 连在一起；
          // 卡片一格放不下两段，RC 放主行、LN 挪到下面那行
          // 只留 RC 那一段 + 算法读数：面图的 LN 段位塞进来又变成"两个段位"，
          // 格子放不下，也偏离了「写清算法和读数」的初衷
          const lab = splitDanLabel(shownText);
          danLabel = lab.rc;
          danNote = String((shownNote && shownNote.textContent) || "").split(" · ")[0];
        } else if (window.Estimator && window.Estimator.estimate && data.session) {
          try {
            const rate = Number((data.replay && data.replay.rate) || 1) || 1;
            const notes = Number(data.beatmap && data.beatmap.notes) || 0;
            const res = await window.Estimator.estimate(data.session, rate, { fast: notes > 30000 });
            if (res && res.ok) {
              const lab = splitDanLabel(res.label);
              let method = "";
              if (res.method === "roxy") method = "Roxy " + Number(res.numeric).toFixed(2);
              else if (res.method === "azusa") method = "Azusa " + Number(res.numeric).toFixed(2);
              else if (res.rcStar) method = "Sunny " + Number(res.rcStar).toFixed(2) + " 星";
              danLabel = lab.rc;
              danNote = method;
            }
          } catch (e) {
            // 段位算不出来就留空格子，不影响整张卡
          }
        }
        const saved = await window.ShareCard.save(data, {
          danLabel,
          danNote,
          // 卡片画的是当前口径的数据，文件名也带上同一套标记（SV1 / SV2）
          judge: App.state.judgeMode,
        });
        window.UI.toast(saved && saved.dir
          ? "成绩卡已保存到 " + saved.dir + "/" + saved.name
          : "成绩卡已保存到下载文件夹");
      } catch (e) {
        window.UI.toast("成绩卡生成失败：" + (e && e.message ? e.message : e), true);
      } finally {
        btn.disabled = false;
        btn.innerHTML = label;
      }
    });
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
    if (close) close.addEventListener("click", () => App.closeModal("helpModal"));

    // 弹窗开着时：Esc 关闭、左右方向键翻页
    window.addEventListener("keydown", (e) => {
      const modal = $("helpModal");
      if (!modal || modal.hidden) return;
      if (e.key === "Escape") { App.closeModal("helpModal"); return; }
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
      App.state.localDir = res.dir || "";
      App.state.localItems = res.items;
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
    const all = App.state.localItems || [];
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
        if (App.state.busy) return;
        App.state.busy = true;
        App.state.lastReplay = { kind: "local", name: it.name };
        window.UI.loading(true, "正在读取本地回放…");
        App.setStage("正在分析本地回放");
        App.startWaiting();
        window.API.analyzeLocal(it.name)
          .then((data) => App.renderAnalysis(data))
          .catch((e) => {
            window.UI.loading(false);
            if (e.needsInput) App.openBeatmapModal(e.payload || {});
            else window.UI.toast(e.message || "分析失败", true);
          })
          .finally(() => {
            App.stopWaiting();
            App.state.busy = false;
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


  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    on, openReplaysFolder, bindRescueActions, bindImportActions, bindDropzone,
    bindTransportControls, bindJudgeControls, bindViewToggles, bindCompareControls,
    bindPlaybackControls, bindErrorFilter, bindNavigation, bindEvents, bindB50Export,
    splitDanLabel, bindShareCard, bindHelp, refreshLocalReplays, renderLocalReplayList,
  });
})(window.App = window.App || {});
