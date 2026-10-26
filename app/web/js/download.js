/* 谱面包下载：交给本地服务端分段并发取，页面只负责显示进度和收文件。
 *
 * 为什么不是浏览器自己分段取
 * --------------------------
 * 镜像站支持 Range，本来让页面发 8 个分段请求最省事。实测不成立：浏览器到镜像
 * 之间如果挂着会走 h2 的本地代理，8 个请求会被复用成一条连接，吞吐比单连接还差
 * ——同一个 4.3MB 的包，h2 单隧道 9.8 秒，强制 http/1.1 之后 2.9 秒。而 Python
 * 侧每个分段各走一条连接，4.6 秒。所以分段放到服务端做，对谁都成立。
 *
 * 流程：POST /api/map/download → 轮询 /status 看进度 → 下完把文件取回来存盘。
 * 这条链路任何一步不通（老版本服务端、被安全软件拦了）就退回直链，和改造前一样。
 */
(function () {
  "use strict";

  var TASK = "map-download";
  var POLL = 400;            // ms
  var TIMEOUT = 180000;      // 三分钟还没下完就放弃，免得一直转
  var BIG_BYTES = 100 * 1024 * 1024;   // 超过这个就不在内存里转一手
  var busy = false;

  function stack() { return window.UI && window.UI.tasks; }
  function toast(text) { if (window.UI && window.UI.toast) window.UI.toast(text); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function fmt(n) {
    if (!n) return "0 KB";
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }

  function cleanName(text) {
    return String(text || "beatmap.osz").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 120);
  }

  // 卡片数据里 artist / title 都有，拼成 "Artist - Title.osz" 比哈希名好认
  function nameOf(item) {
    var base = [item && item.artist, item && item.title].filter(Boolean).join(" - ");
    return cleanName((base || "beatmap") + ".osz");
  }

  function postJSON(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = JSON.parse(text); } catch (e) { data = null; }
        if (!res.ok || !data || data.ok === false) {
          throw new Error((data && data.error) || ("HTTP " + res.status));
        }
        return data;
      });
    });
  }

  // 把拿到的内容存盘。objectURL + download 属性是纯本地写盘，不经过网络栈，
  // 比直接导航到附件地址稳（后者在部分环境里会被下载管理器中途取消）。
  function save(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 10000);
  }

  // 交给下载管理器（只给特别大的包用，免得在内存里多占一份）
  function saveByLink(jobId, name) {
    var a = document.createElement("a");
    a.href = "/api/map/download/file?id=" + encodeURIComponent(jobId);
    a.download = name;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      if (a.parentNode) a.parentNode.removeChild(a);
    }, 2000);
  }

  /**
   * 把服务端下好的包交出去。小包用 fetch 接过来再存（稳），大包直接交给浏览器。
   */
  function deliver(jobId, size, label, bar) {
    var url = "/api/map/download/file?id=" + encodeURIComponent(jobId);
    if (size > BIG_BYTES) {
      saveByLink(jobId, label);
      return Promise.resolve(true);
    }
    if (bar) bar.update(TASK, { percent: 99, detail: "正在保存到本机…" });
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.blob();
    }).then(function (blob) {
      save(blob, label);
      return true;
    });
  }

  // 直链兜底：Content-Disposition 是 attachment，导航过去只会下载，页面不动
  function viaBrowser(url) {
    try {
      window.location.assign(url);
    } catch (e) {
      window.open(url, "_blank", "noopener");
    }
  }

  function poll(jobId, label, bar) {
    var deadline = Date.now() + TIMEOUT;

    function step() {
      return fetch("/api/map/download/status?id=" + encodeURIComponent(jobId))
        .then(function (res) {
          return res.text().then(function (text) {
            var data = null;
            try { data = JSON.parse(text); } catch (e) { data = null; }
            if (!res.ok || !data || data.ok === false) {
              throw new Error((data && data.error) || ("HTTP " + res.status));
            }
            return data;
          });
        })
        .then(function (info) {
          if (info.state === "error") throw new Error(info.error || "下载失败");
          if (info.state === "done") return info;
          if (Date.now() > deadline) throw new Error("下载超时");

          if (bar) {
            var total = Number(info.total) || 0;
            var got = Number(info.received) || 0;
            bar.update(TASK, {
              // 总大小还没探到时不报百分比，进度条自己走
              percent: total ? Math.min(99, Math.round(got * 100 / total)) : null,
              detail: total ? fmt(got) + " / " + fmt(total) : fmt(got),
            });
          }
          return sleep(POLL).then(step);
        });
    }
    return step();
  }

  /**
   * 开始下载。成功（文件已交给浏览器）返回 true；走了直链兜底返回 false。
   */
  function start(url, name) {
    if (!url || url === "#") return Promise.resolve(false);
    if (busy) {
      toast("正在下载另一张，等它下完再点");
      return Promise.resolve(false);
    }

    var label = name || "谱面包";
    busy = true;
    var bar = stack();
    if (bar) bar.start(TASK, "下载 " + label);

    return postJSON("/api/map/download", { url: url, name: label })
      .then(function (job) {
        return poll(job.id, label, bar).then(function (info) {
          return deliver(job.id, Number(info.size) || 0, label, bar).then(function (ok) {
            if (bar) {
              bar.end(TASK, { message: "已保存 " + label + "（" + fmt(info.size) + "）" });
            }
            return ok;
          });
        });
      })
      .catch(function () {
        // 服务端这条链路不通（老版本、被安全软件拦、超时）就退回原来的直链
        if (bar) bar.end(TASK, {});
        viaBrowser(url);
        return false;
      })
      .then(function (ok) {
        busy = false;
        return ok;
      }, function (err) {
        busy = false;
        throw err;
      });
  }

  window.MapDownload = {
    start: start,
    nameOf: nameOf,
    // 给「点击封面」用：能并行就并行，不行就退回原来的新标签页行为
    attach: function (anchor, url, item) {
      if (!anchor || !url || url === "#") return;
      anchor.addEventListener("click", function (ev) {
        ev.preventDefault();
        start(url, nameOf(item));
      });
    },
  };
})();
