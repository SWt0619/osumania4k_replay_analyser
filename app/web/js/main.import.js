/* 导入与谱面：拖拽上传、分片、只看谱面、快捷导入。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $, el, fmtTime } = window.UI;

  // ---------------------------------------------------------------- 导入流程
  // 分析期间显示"已经等了多久"，并在超时后给出可执行的提示
  let waitTimer = null;
  function setStage(text) {
    App.state.stage = text;
    const box = window.UI.$("loadingText");
    if (box) box.textContent = text;
  }
  // 加载框里的"取消"按钮：能中止当前分析，不至于被永远困住
  function showLoading(text, onCancel) {
    App.state.cancelLoading = onCancel || null;
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
    App.state.cancelLoading = null;
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
    App.beacon("stale-server-code", { assetV: healthData.assetV });
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
      App.beacon("stale-frontend", { mine: myV, server: serverV });
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
      const stage = App.state.stage || "正在处理";
      if (sec >= 60 && !App.state.escaped) {
        App.state.escaped = true;
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
    if (App.state.busy) return "上一次分析还没结束，稍等一下";
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
  // ticket 是这一次导入的凭据：取消时把 cancelled 置真，分片循环每片前问一次。
  async function uploadReplay(base64, ticket) {
    const cancelled = () => !!(ticket && ticket.cancelled);
    // 上传走 XHR：能显示"已经发出去多少字节"。卡在 0% 就是内容没离开浏览器，
    // 卡在 100% 就是后端在处理——这两种情况的处理方式完全不同，所以必须能区分。
    setStage("正在上传回放");
    App.beacon("upload-start", { bytes: base64.length });
    try {
      return await window.API.analyzeUpload(
        { replay: base64, beatmap: null, beatmapPath: null, songsRoot: null },
        (loaded, total) => {
          const pct = total ? Math.round((loaded / total) * 100) : 0;
          if (pct >= 100) setStage("上传完成，正在分析");
          else setStage("正在上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
        },
        (xhr) => { App.state.activeXhr = xhr; }
      );
    } catch (uploadErr) {
      // 只有"传输层"失败才该改走分片通道：连不上、超时、被中途掐断
      // （本机安全软件或代理把大 POST 体整包吞掉的典型表现）。
      // 后端已经答复过（空文件、坏回放、4K 以外、内部错误）时重发没有意义，
      // 只会把「没有收到回放文件」盖成「分片 1 被拒绝（HTTP 400）」，把真正的原因弄丢。
      const answered = !!(uploadErr && (uploadErr.payload || uploadErr.answered));
      const isAbort = !!(uploadErr && (uploadErr.name === "AbortError" || cancelled()));
      if (isAbort) throw uploadErr;   // 用户主动取消：别再换通道重传一遍
      if (answered || cancelled()) throw uploadErr; // 取消过 / 后端已答复 → 不兜底
      App.beacon("fallback-chunked", { reason: (uploadErr && uploadErr.message) || "?" });
      App.state.activeXhr = null;
      return await window.API.analyzeUploadChunked(
        base64,
        (loaded, total) => {
          const pct = total ? Math.round((loaded / total) * 100) : 0;
          setStage("分片上传 " + pct + "%（" + Math.round(loaded / 1024) + " / " + Math.round(total / 1024) + " KB）");
        },
        (text) => setStage(text),
        cancelled
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

    App.beacon("file-chosen", { name: file.name, size: file.size });
    App.state.lastReplay = { kind: "file", file: file };
    App.state.busy = true;
    App.state.escaped = false;
    // 这一次导入的凭据：取消时置真，分片循环与结果渲染都会看它一眼
    const ticket = { cancelled: false };
    App.state.importTicket = ticket;
    showLoading("正在读取文件…", () => {
      ticket.cancelled = true;
      App.state.busy = false;
      if (App.state.activeXhr) {
        try { App.state.activeXhr.abort(); } catch (err) { /* 忽略 */ }
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
      App.state.replayBase64 = b64;
      const data = await uploadReplay(b64, ticket);
      if (ticket.cancelled) return;   // 取消之后后端可能照样算完，别把结果盖回页面
      App.renderAnalysis(data);
    } catch (e) {
      window.UI.loading(false);
      if (ticket.cancelled) return;   // 主动取消：不再补一条红色错误
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
      App.state.busy = false;
      App.state.stage = "";
      App.state.activeXhr = null;
    }
  }

  // 两个"选 .osu"的入口共用的前置检查。返回错误文案，空串表示可以继续。
  function osuFileProblem(file) {
    if (!/\.osu$/i.test(file.name)) return "谱面文件需要是 .osu（你选的是 " + file.name + "）";
    if (App.state.busy) return "上一次分析还没结束，稍等一下";
    return "";
  }

  // 这一轮重跑要用的回放从哪来：拖进来的在内存里，_replays 那条只有文件名。
  // 两个「选谱面」入口以前一律读 replayBase64，本地通道就会拿空内容、
  // 或者拿上一次拖进来的另一份回放去分析。
  function replaySource() {
    const last = App.state.lastReplay || {};
    if (last.kind === "local" && last.name) return { kind: "local", name: last.name };
    if (App.state.replayBase64) return { kind: "file", base64: App.state.replayBase64 };
    return null;
  }

  function noReplayToast() {
    window.UI.loading(false);
    window.UI.toast("这次没拿到回放内容：把 .osr 重新拖进来，或用「本地回放」里的那一份", true);
  }

  async function handleBeatmapFile(file) {
    if (!file) return;
    const problem = osuFileProblem(file);
    if (problem) {
      window.UI.toast(problem, true);
      return;
    }
    const src = replaySource();
    if (!src) { noReplayToast(); return; }
    App.state.busy = true;
    window.UI.loading(true, "正在用你选的谱面重新分析…");
    window.__cancelLoading = function () { hideLoading(); stopWaiting(); App.state.busy = false; };
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
      // 本地通道的回放还躺在 _replays 里，传文件名让后端自己读；拖进来的才传 base64
      const data = src.kind === "local"
        ? await window.API.analyzeLocal(src.name, { beatmap: b64 })
        : await window.API.analyze(src.base64, b64, null, null, null);
      closeModal("bmModal");
      App.renderAnalysis(data);
    } catch (e) {
      window.UI.loading(false);
      window.UI.toast(e.message || "分析失败", true);
    } finally {
      stopWaiting();
      App.state.busy = false;
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
    App.state.busy = true;
    window.UI.loading(true, "正在读取谱面…");
    try {
      const b64 = await window.API.fileToBase64(file);
      const data = await window.API.chartOnly(b64);
      renderChartOnly(data);
    } catch (e) {
      window.UI.loading(false);
      window.UI.toast(e.message || "谱面读取失败", true);
    } finally {
      App.state.busy = false;
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
    App.closeCompare();
    App.state.data = null;
    App.state.baseData = null;
    App.state.chartOnly = data;
    App.state.compare = null;
    App.state._errTimes = null;
    App.state.selectedError = null;
    App.pause();
    App.state.virtualTime = 0;
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
    App.switchTab("chartonly");
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
    const src = replaySource();
    if (!src) { noReplayToast(); return; }
    window.UI.loading(true, "正在用曲库里的谱面分析…");
    try {
      const data = src.kind === "local"
        ? await window.API.analyzeLocal(src.name, { beatmapPath: path })
        : await window.API.analyze(src.base64, null, null, path, null);
      closeModal("bmModal");
      App.renderAnalysis(data);
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
    if (pathEl) pathEl.textContent = App.state.localDir || "项目目录下的 _replays 文件夹";
    const hint = $("blockedHint");
    if (hint && reason) hint.textContent = reason + "。不用折腾浏览器，改用下面这条通道：";
    if (box) box.hidden = false;
    App.refreshLocalReplays();
  }

  function closeModal(id) {
    const m = $(id);
    if (m) m.hidden = true;
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
          await App.renderAnalysis(res.data);
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


  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    setStage, showLoading, hideLoading, warnIfStaleCode, assetVersion, checkFrontendVersion,
    startWaiting, stopWaiting, replayFileProblem, uploadReplay, osrModeProblem,
    handleReplayFile, osuFileProblem, handleBeatmapFile, handleChartFile, applyChartDan,
    renderChartOnly, renderChartOnlyPattern, openBeatmapModal, searchBeatmaps,
    useLibraryBeatmap, showBlockedUpload, closeModal, quickLoadingOn, quickLoadingOff,
    pollQuickImport, quickLoop, initQuickImport,
  });
})(window.App = window.App || {});
