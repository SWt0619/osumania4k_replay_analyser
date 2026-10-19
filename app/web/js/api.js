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
  };
})();
