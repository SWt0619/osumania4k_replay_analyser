/* 设置与关于：更新说明、检查更新、游戏目录、快捷导入热键。 */
(function (App) {
  "use strict";

  // 全局状态由 main.core.js 建好，这里拿同一份引用
  const state = App.state;
  const { $, el } = window.UI;

  // ---------------------------------------------------------------- 更新说明
  // 内容由服务端从仓库根的 CHANGELOG.md 解析，前端只负责排版。
  // 只在这一页真被打开时请求一次；解析不出来就整块藏起来，不留空卡。
  function loadChangelog() {
    if (App.state._changelogLoaded) return;
    App.state._changelogLoaded = true;
    fetch("/api/changelog")
      .then((r) => r.json())
      .then((res) => {
        const versions = (res && res.versions) || [];
        if (!versions.length) return;
        window.UI.renderReleases(versions);
        const card = $("releaseCard");
        if (card) card.hidden = false;
      })
      .catch(() => { App.state._changelogLoaded = false; });
  }

  // 「展开全部」按的是当前状态：只要还有收起的，就全部展开
  function bindReleaseNotes() {
    const btn = $("btnReleaseAll");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const rows = Array.prototype.slice.call(document.querySelectorAll("#releaseList .rel"));
      if (!rows.length) return;
      const expand = rows.some((row) => !row.open);
      rows.forEach((row) => { row.open = expand; });
      btn.textContent = expand ? "收起全部" : "展开全部";
    });
  }

  // ---------------------------------------------------------------- 检查更新
  // 打开「关于」页时静默查一次；手动点按钮时失败会 toast。
  // 只请求 GitHub 的 latest release，不上传任何本地数据。
  function checkUpdate(manual) {
    const box = $("aboutUpdate");
    const btn = $("btnCheckUpdate");
    if (!box) return;
    if (App.state._checkingUpdate) return;
    // 自动检查 5 分钟内不重复请求：来回切页不该反复打 GitHub
    if (!manual && App.state._updateCheckedAt && Date.now() - App.state._updateCheckedAt < 5 * 60 * 1000) return;
    App.state._checkingUpdate = true;
    App.state._updateCheckedAt = Date.now();
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
        App.state._checkingUpdate = false;
        if (btn) { btn.disabled = false; btn.textContent = "检查更新"; }
      });
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
        : r.hint || "没找到谱面，看看上面的路径对不对。";
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


  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    loadChangelog, bindReleaseNotes, checkUpdate, loadPathSettings, savePaths,
    loadHotkeySettings, saveHotkeySettings,
  });
})(window.App = window.App || {});
