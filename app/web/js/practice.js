/* 练习谱面：把报告里的「练这一段」变成一张能直接拖进 osu! 的 .osz。
 *
 * 谱面裁切在服务端（app/core/practice.py），这里只做音频：浏览器天生能解 mp3 / ogg，
 * 服务端不能（纯标准库、不带 ffmpeg）。所以流程是「服务端给谱面和取音区间 →
 * 这里解码、变速、编 WAV → 送回服务端打 zip」。
 *
 * 变速用 playbackRate，和 osu 自己的 HT 一样会降调。练图时的听感与游戏里开 0.9×
 * 一致；真做成不变调反而容易让人以为速度没降下来。
 */
(function () {
  "use strict";

  var TASK = "practice";

  function fmtTime(ms) {
    var t = Math.max(0, Math.round(Number(ms) / 1000));
    return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0");
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (body) {
        if (!res.ok || !body || body.ok === false) {
          throw new Error((body && body.error) || ("HTTP " + res.status));
        }
        return body;
      });
    });
  }

  // 16bit PCM：osu 读 wav 没别的要求，44 字节头 + 交错采样就够
  function encodeWav(buffer) {
    var ch = buffer.numberOfChannels;
    var len = buffer.length;
    var bytes = 44 + len * ch * 2;
    var view = new DataView(new ArrayBuffer(bytes));
    function tag(off, s) {
      for (var i = 0; i < s.length; i += 1) view.setUint8(off + i, s.charCodeAt(i));
    }
    tag(0, "RIFF");
    view.setUint32(4, bytes - 8, true);
    tag(8, "WAVE");
    tag(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, ch, true);
    view.setUint32(24, buffer.sampleRate, true);
    view.setUint32(28, buffer.sampleRate * ch * 2, true);
    view.setUint16(32, ch * 2, true);
    view.setUint16(34, 16, true);
    tag(36, "data");
    view.setUint32(40, len * ch * 2, true);
    var chans = [];
    for (var c = 0; c < ch; c += 1) chans.push(buffer.getChannelData(c));
    var off = 44;
    for (var i = 0; i < len; i += 1) {
      for (var k = 0; k < ch; k += 1) {
        var s = Math.max(-1, Math.min(1, chans[k][i]));
        view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        off += 2;
      }
    }
    return new Blob([view.buffer], { type: "audio/wav" });
  }

  function download(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name || "practice.osz";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  async function generate(data, advice, btn, rateArg) {
    var stack = window.UI && window.UI.tasks;
    var range = (advice && advice.timeRange) || [];
    var full = !!(advice && advice.fullMap);
    if (!data || !data.session || (!full && range.length !== 2)) return;
    // 按钮会把这一档的倍率传进来；没传就退回建议里的第一档
    var rate = Number(rateArg) || Number(((advice && advice.rates) || [])[0]) || 0.9;
    if (btn) btn.disabled = true;
    if (stack) stack.start(TASK, "生成练习谱面");
    try {
      // 1. 服务端：对齐八拍、裁谱面、把 0.9× 烘进时间轴
      var cut = await postJSON("/api/practice/cut", {
        session: data.session,
        t0: full ? 0 : range[0],
        t1: full ? 0 : range[1],
        rate: rate,
        full: full,
      });
      if (stack) {
        stack.update(TASK, {
          percent: 0.15,
          detail: "裁到 " + fmtTime(cut.cutStart) + "–" + fmtTime(cut.cutEnd) +
            " · " + cut.objects + " 个物件，解码音频…",
        });
      }

      // 2. 取原曲并解码（几 MB 的 mp3/ogg，几百毫秒）
      var res = await fetch(cut.audioUrl);
      if (!res.ok) throw new Error("取音频失败（HTTP " + res.status + "）");
      var raw = await res.arrayBuffer();
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) throw new Error("这个浏览器不支持 Web Audio，无法变速");
      var ac = new AC();
      var audio;
      try {
        audio = await ac.decodeAudioData(raw);
      } catch (e) {
        throw new Error("浏览器解不开这张谱面的音频（可能是少见的编码），只能自己开 0.9×");
      }
      if (stack) stack.update(TASK, { percent: 0.4, detail: "渲染 0.9× 音频…" });

      // 3. 裁切 + 变速。start 的第三个参数是「源缓冲里取多长」，
      //    输出长度是它除以倍率，正好和谱面的时间轴对得上。
      var start = Math.max(0, cut.audioStart / 1000);
      var end = Math.min(audio.duration, cut.audioEnd / 1000);
      var srcDur = Math.max(0.2, end - start);
      var sr = audio.sampleRate;
      var ch = Math.min(2, audio.numberOfChannels);
      var outDur = srcDur / cut.rate;
      var off = new OfflineAudioContext(ch, Math.ceil(outDur * sr), sr);
      var src = off.createBufferSource();
      src.buffer = audio;
      src.playbackRate.value = cut.rate;
      src.connect(off.destination);
      src.start(0, start, srcDur);
      var rendered = await off.startRendering();

      // 4. 前面补 3 秒空白，让第一个音符有反应时间；结尾 300ms 淡出，
      //    不然裁在波形中间会「啪」一声。
      var pad = Math.round((cut.padMs / 1000) * sr);
      var buf = ac.createBuffer(ch, pad + rendered.length, sr);
      for (var c = 0; c < ch; c += 1) {
        buf.copyToChannel(rendered.getChannelData(c), c, pad);
      }
      var fade = Math.min(Math.round(0.3 * sr), rendered.length);
      for (var k = 0; k < ch; k += 1) {
        var d = buf.getChannelData(k);
        for (var i = 0; i < fade; i += 1) {
          d[buf.length - 1 - i] *= i / fade;
        }
      }
      if (stack) stack.update(TASK, { percent: 0.75, detail: "打包 .osz…" });

      var wav = encodeWav(buf);
      var packed = await fetch(cut.packUrl, { method: "POST", body: wav });
      if (!packed.ok) {
        var msg = "";
        try { msg = (await packed.json()).error || ""; } catch (e) { /* 不是 JSON 就用状态码 */ }
        throw new Error(msg || ("打包失败（HTTP " + packed.status + "）"));
      }
      var blob = await packed.blob();
      download(blob, cut.fileName);
      if (stack) stack.end(TASK, {});
      if (window.UI && window.UI.toast) window.UI.toast("已生成 " + cut.fileName);
    } catch (e) {
      var text = (e && e.message) || String(e);
      if (stack) stack.end(TASK, { error: text });
      if (window.UI && window.UI.toast) window.UI.toast(text, true);
    } finally {
      if (btn) btn.disabled = false;
      if (btn) btn.blur();
    }
  }

  // 按页面上那个段位重算建议：首屏的建议是用 MSD 兜底算的倍率，估算器跑完
  // 才知道真实段位（11 段以上该给 0.95）。只影响建议里的倍率与文案，
  // 所以只重画建议列表，不动 hero 那张卡。
  async function syncAdvice(data, label) {
    if (!data || !data.session || !label) return;
    var res;
    try {
      res = await postJSON("/api/report/advice", { session: data.session, label: label });
    } catch (e) {
      return;                      // 重算失败就保留首屏那份，不打扰用户
    }
    if (!res || !res.ok || !res.advice || !res.advice.length) return;
    var old = (data.evaluation && data.evaluation.advice) || [];
    var changed = old.length !== res.advice.length || old.some(function (a, i) {
      var before = (a.rates || (a.rate ? [a.rate] : [])).join("|");
      var after = ((res.advice[i] || {}).rates || []).join("|");
      return before !== after;
    });
    data.evaluation = data.evaluation || {};
    data.evaluation.advice = res.advice;
    data.practiceRates = res.rates;
    if (changed && window.UI && window.UI.renderAdviceList) {
      window.UI.renderAdviceList(data);
    }
  }

  window.Practice = { generate: generate, syncAdvice: syncAdvice };
})();
