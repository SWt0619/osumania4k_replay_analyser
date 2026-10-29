/* 启动入口：等 DOM 就绪后把各模块接起来，并注册全局错误提示。

   这里只调用 App.*，不直接碰各模块的内部变量；模块之间的依赖也全部走 window.App，
   所以加载顺序只要求 core 在前、main 在后，中间的模块谁先谁后都不影响。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $ } = window.UI;

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
    const ver = App.assetVersion() || "无版本参数";
    App.beacon("boot", {
      js: ver,
      ui: !!window.UI,
      api: !!window.API,
      charts: !!window.Charts,
      player: !!window.ManiaPlayer,
      cancelBtn: !!document.getElementById("loadingCancel"),
      cancelOnclick: !!(document.getElementById("loadingCancel") || {}).onclick,
      localBlock: !!document.getElementById("localFolder"),
    });
    App.bindFolds();
    App.bindEvents();
    App.bindProgress();
    // 建议里的「跳到 1:12 看这一段」：切到回放页并跳过去，自动开始播
    window.UI.setSeek((ms) => {
      App.switchTab("replay");
      App.seekTo(ms, true);
    });
    App.setPlayIcon(false);
    App.loadPrefs();
    // 键位按钮在 bindEvents 里已经按默认值画过一遍，那时还没读本地偏好；
    // 少了这一行，键位其实存住了、按键也生效，但按钮上永远显示 D/F/J/K。
    if (App.syncPlayKeys) App.syncPlayKeys();
    App.applyTheme(App.state.theme);
    // 帧率上限跟着偏好走：这里把 frameMs 定下来，后面的播放/降档都用它
    App.applyFpsCap(App.state.fpsCap, { silent: true });
    App.applyMotionPref(App.state.disableMotion);
    App.updateJudgeToggle();
    App.syncScrollUi();
    App.syncOffsetUi();
    App.syncVolumeUi();
    // 勾选框是在 bindEvents 里先按默认值画的，那时还没读本地偏好。
    // 少了这一行，值其实存住了、过滤也生效，但界面永远显示成没勾 —— 看起来就像没保存。
    App.syncLowConfidenceUI();
    App.syncHitsoundUi();
    App.syncHitVolUi();
    App.applyPerfPanel(App.state.perfPanel || /(?:^|[?&])perf=1(?:&|$)/.test(location.search));
    App.syncTabLocks();

    // 首次启动要遍历上千个文件建索引。这一步不能挡住后面的初始化
    // （热键、快捷导入、健康检查都得在它之前就绪），所以放出去异步跑，
    // 期间用顶部细进度条 + 右上角文字告诉用户"程序还在动"。
    void loadLibraryWithProgress();

    // 启动时也做一次版本自愈（拿着旧缓存的标签页能自己刷新）；顺便看看服务端是不是旧进程
    try {
      const health = await window.API.health(4000);
      await App.checkFrontendVersion(health);
      App.warnIfStaleCode(health);
      window.UI.renderAbout(health && health.version);
      // 快捷导入的热键没注册上时把原因说出来，否则用户按了没反应会以为程序坏了
      const hk = health && health.hotkey;
      if (hk && hk.ok === false && hk.error) {
        window.UI.toast("快捷导入的热键没注册上：" + hk.error, true);
      }
    } catch (e) { /* 后端没起就不折腾 */ }

    App.refreshLocalReplays();
    App.initQuickImport();

    if (!window.Charts || !window.ManiaPlayer) {
      console.warn("图表/播放器模块未加载：", { charts: !!window.Charts, player: !!window.ManiaPlayer });
    }
  }

  // 脚本报错不再静默：弹出来，至少让人知道发生了什么
  window.addEventListener("error", (e) => {
    App.beacon("js-error", { msg: (e.message || "") + " @" + (e.filename || "") + ":" + (e.lineno || 0) });
    try {
      window.UI.toast("页面脚本报错：" + (e.message || "未知错误"), true);
    } catch (err) {
      /* ignore */
    }
  });

  document.addEventListener("DOMContentLoaded", init);
  window.__app = { state: App.state, switchTab: App.switchTab, seekTo: App.seekTo, handleReplayFile: App.handleReplayFile, startCompare: App.startCompare, closeCompare: App.closeCompare };


  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    loadLibraryWithProgress, init,
  });
})(window.App = window.App || {});
