/* 后端 API 封装。所有请求都走同源，不需要 CORS。 */
(function () {
  "use strict";

  async function jsonFetch(url, options) {
    const res = await fetch(url, options);
    const text = await res.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      throw new Error("服务器返回的不是 JSON（HTTP " + res.status + "）");
    }
    if (!res.ok && data && data.error) throw new Error(data.error);
    if (data && data.ok === false) {
      const err = new Error(data.error || "分析失败");
      err.needsInput = !!data.needsInput;
      err.payload = data;
      throw err;
    }
    return data;
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const s = String(reader.result || "");
        const comma = s.indexOf(",");
        resolve(comma >= 0 ? s.slice(comma + 1) : s);
      };
      reader.onerror = () => reject(new Error("读取文件失败：" + file.name));
      reader.readAsDataURL(file);
    });
  }

  window.API = {
    fileToBase64,

    // 在线补音频：失败是常态（镜像缺图、网络不通、用户关了开关），
    // 所以这里不抛异常，把结果原样交给调用方去决定怎么提示。
    // 90 秒上限：服务端的镜像预算是 45 秒，加上单次请求 20 秒超时与解包，
    // 正常不会到 90；真到了说明连接卡住，得让界面停在一个明确的结论上。
    async fetchAudio(session) {
      const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => ctrl.abort(), 90000) : 0;
      try {
        const res = await fetch("/api/audio-fetch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session: session }),
          signal: ctrl ? ctrl.signal : undefined,
        });
        const data = await res.json();
        return data && typeof data === "object" ? data : { ok: false, error: "返回格式异常" };
      } catch (e) {
        const timedOut = e && e.name === "AbortError";
        return { ok: false, error: timedOut ? "等了 90 秒没有回应" : ((e && e.message) || "网络错误") };
      } finally {
        if (timer) clearTimeout(timer);
      }
    },

    // 补音频的实时进度。轮询失败不抛异常：拿不到进度时界面还有计时在走，
    // 不能因为一次轮询失败就把提示变成错误。
    async audioFetchStatus(session) {
      try {
        const res = await fetch("/api/audio-fetch/status?session=" + encodeURIComponent(session),
          { cache: "no-store" });
        const data = await res.json();
        return data && data.progress ? data.progress : null;
      } catch (e) {
        return null;
      }
    },

    // 用 XHR 上传：能拿到"已经发出去多少字节"，卡在上传阶段时一眼能看出来。
    // 25 秒超时：正常分析 1~2 秒就完，超时基本等于「POST 体被吞了」，调用方会自动改走分片通道。
    analyzeUpload(payload, onProgress, onXhr) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/analyze", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.timeout = 25000;
        if (onXhr) onXhr(xhr);
        if (xhr.upload && onProgress) {
          xhr.upload.onprogress = (ev) => {
            if (ev.lengthComputable) onProgress(ev.loaded, ev.total);
          };
        }
        xhr.onload = () => {
          let data = null;
          try {
            data = JSON.parse(xhr.responseText || "{}");
          } catch (e) {
            // 有 HTTP 状态码就说明请求已经到达服务端，这是"服务端答复"而不是"传输失败"，
            // 调用方据此决定要不要改走分片重发（answered=true 就不该重发）
            const err = new Error("服务器返回的不是 JSON（HTTP " + xhr.status + "）");
            err.answered = true;
            err.status = xhr.status;
            reject(err);
            return;
          }
          if (data && data.ok === false) {
            const err = new Error(data.error || "分析失败");
            err.needsInput = !!data.needsInput;
            err.payload = data;
            err.answered = true;
            err.status = xhr.status;
            reject(err);
            return;
          }
          resolve(data);
        };
        xhr.onerror = () => reject(new Error("上传请求失败（连不上本地服务）"));
        xhr.ontimeout = () => reject(new Error("上传超时（25 秒没有回应）"));
        xhr.onabort = () => reject(new Error("上传被中止"));
        xhr.send(JSON.stringify(payload));
      });
    },

    openReplays() {
      return jsonFetch("/api/open-replays");
    },

    localReplays() {
      return jsonFetch("/api/local-replays");
    },

    // 快捷导入：先在结算屏按 F2 导出，再按我们的组合键；结果留在服务端，页面按 token 取。
    // wait>0 时长轮询——热键一按下服务端就叫醒请求，加载提示不用等到分析结束才显示。
    quickPending(since, waitSeconds) {
      return jsonFetch("/api/quick/pending?since=" + Number(since || 0) +
                       "&wait=" + Number(waitSeconds || 0));
    },

    // 手动触发一次「分析最近导出的回放」（热键被占用时的兜底）。
    // force=true 表示同一份文件也要重算一遍。
    quickImport(force) {
      return jsonFetch("/api/quick-import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: !!force }),
      });
    },

    // 快捷导入的快捷键：读当前值 / 换一个（空串表示关闭）
    hotkey() {
      return jsonFetch("/api/hotkey");
    },

    // 成长：本地存档的评估结果；保存自己选的当前段位
    progress() {
      return jsonFetch("/api/progress");
    },

    saveProfile(player, type, value) {
      return jsonFetch("/api/profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ player: player, type: type, value: value }),
      });
    },

    saveHotkey(combo) {
      return jsonFetch("/api/hotkey", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ combo: combo || "" }),
      });
    },

    analyzeLocal(name) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/analyze-local", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.timeout = 90000;
        xhr.onload = () => {
          let data = null;
          try {
            data = JSON.parse(xhr.responseText || "{}");
          } catch (e) {
            reject(new Error("服务器返回的不是 JSON（HTTP " + xhr.status + "）"));
            return;
          }
          if (data && data.ok === false) {
            const err = new Error(data.error || "分析失败");
            err.needsInput = !!data.needsInput;
            err.payload = data;
            reject(err);
            return;
          }
          resolve(data);
        };
        xhr.onerror = () => reject(new Error("请求发不出去"));
        xhr.ontimeout = () => reject(new Error("分析超时"));
        xhr.send(JSON.stringify({ name: name }));
      });
    },

    // 最小的 POST，自检用
    echo() {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/echo", true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.timeout = 8000;
        xhr.onload = () => {
          try {
            resolve(JSON.parse(xhr.responseText || "{}"));
          } catch (e) {
            reject(new Error("echo 返回非 JSON"));
          }
        };
        xhr.onerror = () => reject(new Error("POST 发不出去（连不上本地服务）"));
        xhr.ontimeout = () => reject(new Error("POST 超时（8 秒内没有回应）"));
        xhr.send(JSON.stringify({ hello: "world", time: Date.now() }));
      });
    },

    analyze(replayBase64, beatmapBase64, songsRoot, beatmapPath, signal) {
      return jsonFetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          replay: replayBase64,
          beatmap: beatmapBase64 || null,
          beatmapPath: beatmapPath || null,
          songsRoot: songsRoot || null,
        }),
        signal: signal || undefined,
      });
    },

    library() {
      return jsonFetch("/api/library");
    },

    // 建索引期间轮询，拿"已看过多少个文件"。不触发构建。
    // 测一次本机到各镜像站的延迟（设置页按钮触发，点一次发三个 HEAD）
    testMirrors() {
      return jsonFetch("/api/mirror-test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    },

    libraryProgress() {
      return jsonFetch("/api/library/progress", { cache: "no-store" });
    },

    // 自动探测找不到曲库时，让玩家在页面上手动指定游戏目录
    paths() {
      return jsonFetch("/api/paths");
    },

    savePaths(payload) {
      return jsonFetch("/api/paths", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    },

    // 把估算器算出的段位写回这一局，成长页折合要用同一个数。
    // fresh = 这一局是本次刚入库的：服务端据此把"导入前"还原成"库里没有这一局"，
    // 否则会拿入库时的 MSD 兜底值当基线，新成绩容易一律显示掉段。
    setHistoryDan(key, dan, fresh) {
      return jsonFetch("/api/history/dan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: key, dan: dan, fresh: !!fresh }),
      });
    },

    // 从成长页点开某一局：服务端按回放 MD5 找回 .osr 重跑分析。
    // 文件被删/被移走时接口返回 404，错误消息会原样抛给页面。
    historyReplay(key) {
      return jsonFetch("/api/history/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: key }),
      });
    },

    // 上传前先确认后端活着：连不上就立刻报错，别让页面干等
    health(timeoutMs) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs || 4000);
      return jsonFetch("/api/health?_=" + Date.now(), { signal: ctrl.signal }).finally(() => clearTimeout(timer));
    },

    // 分片上传兜底：把回放切成小片逐个 POST，每一片都是独立的小请求并带重试。
    // 用途：个别机器上安全软件/代理会把大 POST 体整包吞掉（表现为请求有去无回），
    // 小请求却畅通。直接上传失败时自动切到这条通道。
    analyzeUploadChunked(base64, onProgress, onStage) {
      const CHUNK = 24 * 1024; // 24KB 原始字节/片
      return new Promise((resolve, reject) => {
        let bin;
        try {
          bin = atob(base64);
        } catch (e) {
          reject(new Error("回放数据解码失败"));
          return;
        }
        const total = bin.length;
        const chunkCount = Math.max(1, Math.ceil(total / CHUNK));
        const sid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

        const sendOnce = (i, slice) =>
          new Promise((res, rej) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", "/api/upload-chunk?sid=" + sid + "&i=" + i, true);
            xhr.timeout = 10000;
            xhr.setRequestHeader("Content-Type", "application/octet-stream");
            xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? res() : rej(new Error("分片 " + (i + 1) + " 被拒绝（HTTP " + xhr.status + "）")));
            xhr.onerror = () => rej(new Error("分片 " + (i + 1) + " 发送失败"));
            xhr.ontimeout = () => rej(new Error("分片 " + (i + 1) + " 超时"));
            xhr.send(slice);
          });

        const sendWithRetry = async (i, slice) => {
          let lastErr = null;
          for (let attempt = 0; attempt < 3; attempt++) {
            try {
              await sendOnce(i, slice);
              return;
            } catch (e) {
              lastErr = e;
              await new Promise((r) => setTimeout(r, 350 * (attempt + 1)));
            }
          }
          throw lastErr || new Error("分片发送失败");
        };

        (async () => {
          try {
            for (let i = 0; i < chunkCount; i++) {
              if (onStage) onStage("分片上传 " + (i + 1) + " / " + chunkCount);
              const slice = bin.slice(i * CHUNK, (i + 1) * CHUNK);
              await sendWithRetry(i, slice);
              if (onProgress) onProgress(Math.min(total, (i + 1) * CHUNK), total);
            }
            if (onStage) onStage("分片传完，正在分析");
            const res = await fetch("/api/analyze-chunked", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ sid: sid }),
            });
            const text = await res.text();
            let data;
            try {
              data = JSON.parse(text);
            } catch (e) {
              throw new Error("服务器返回的不是 JSON（HTTP " + res.status + "）");
            }
            if (data && data.ok === false) {
              const err = new Error(data.error || "分析失败");
              err.needsInput = !!data.needsInput;
              err.payload = data;
              throw err;
            }
            resolve(data);
          } catch (e) {
            reject(e);
          }
        })();
      });
    },

    // 相似谱面：难度轴是段位，而段位在前端用 Sunny 算，所以这里要把段位标签回传给后端。
    // 推荐失败只是少一张卡片，不该弹错误、也不该影响页面上其他任何东西。
    async similar(session, dan) {
      if (!session || !dan) return [];
      try {
        const data = await jsonFetch("/api/recommend", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session: session, dan: dan }),
        });
        return (data && data.items) || [];
      } catch (e) {
        return [];
      }
    },

    // 随机找图：筛选全在本地索引里做，唯一的外部依赖是卡片上的封面与下载链接。
    randomCharts(params) {
      const qs = new URLSearchParams();
      if (params.mode) qs.set("mode", params.mode);
      if (params.dan != null) qs.set("dan", params.dan);
      if (params.cat) qs.set("cat", params.cat);
      if (params.exclude && params.exclude.length) qs.set("exclude", params.exclude.join(","));
      qs.set("n", params.n || 8);
      return jsonFetch("/api/random-charts?" + qs.toString());
    },

    beatmapSearch(query) {
      return jsonFetch("/api/beatmap-search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query || "" }),
      });
    },

    beatmapMeta(beatmapBase64) {
      return jsonFetch("/api/beatmap-meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ beatmap: beatmapBase64 }),
      });
    },

    // 只看谱面：不传回放，后端只返回难度与键型构成
    chartOnly(osuBase64) {
      return jsonFetch("/api/chart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ osu: osuBase64 }),
      });
    },

    // 成绩卡 / B50 存进程序目录的 card/。文件名交给服务端拼：
    // 成绩卡是「序号_曲名_SV1|SV2_日期」，B50 是「B50_玩家_日期」，
    // 序号要看目录里已有的图，前端拿不到这个信息。
    saveCard(blob, params) {
      const opts = typeof params === "string" ? { name: params } : (params || {});
      const qs = ["kind", "title", "id", "judge", "name"]
        .filter((k) => opts[k] !== undefined && opts[k] !== null && opts[k] !== "")
        .map((k) => k + "=" + encodeURIComponent(String(opts[k])))
        .join("&");
      return jsonFetch("/api/card/save?" + qs, {
        method: "POST",
        headers: { "Content-Type": "image/png" },
        body: blob,
      });
    },

    // 检查更新：只请求 GitHub 的 latest release，失败由调用方静默处理
    checkUpdate() {
      return jsonFetch("/api/check-update");
    },
  };
})();
