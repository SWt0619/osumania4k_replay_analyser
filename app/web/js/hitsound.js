/* 打击音效：优先播 hihat.wav 采样，加载不到就退回现场合成的短音。
 *
 * 采样放在 app/web/audio/hihat.wav。为什么还要留合成音兜底：单文件报告（离线 HTML）
 * 里没有 /static 目录，fetch 必然失败 —— 那种情况下至少要有个能响的东西，而不是静音。
 *
 * 三种音色靠 playbackRate 区分，不准备三份文件：
 *   击打 1.0、长条头 1.12（略高，能听出是长条的起手）、漏接 0.78（低沉、更轻）。
 */
(function () {
  "use strict";

  var SAMPLE_URL = "/static/audio/hihat.wav";
  var MIX = 0.55;                       // 打击音相对回放音量的上限，避免盖住谱面音乐

  // 同一个采样，靠变调区分三种情形：长条头略高，漏接更低沉也更轻
  var RATE = { hit: 1.0, hold: 1.12, miss: 0.78 };
  var LEVEL = { hit: 1.0, hold: 0.9, miss: 0.75 };

  function create() {
    var ctx = null;
    var master = null;                  // 回放音量 × MIX
    var hitGain = null;                 // 打击音自己的比例（用户可调）
    var buffers = null;                 // 合成音兜底
    var sample = null;                  // 解码后的 hihat
    var sampleState = 0;                // 0 未开始 / 1 加载中 / 2 就绪 / -1 失败
    var muted = false;
    var volume = 0.7;
    var hitVolume = 0.7;

    // 兜底音色：正弦基频 + 少量泛音 + 一小段噪声起音，指数衰减
    function render(freq, dur, noiseAmt) {
      var sr = ctx.sampleRate;
      var len = Math.max(1, Math.floor(sr * dur));
      var buf = ctx.createBuffer(1, len, sr);
      var d = buf.getChannelData(0);
      var seed = 22222;
      for (var i = 0; i < len; i += 1) {
        var t = i / sr;
        var atk = t < 0.001 ? t / 0.001 : 1;
        var env = atk * Math.exp(-t * 76);
        var tone = Math.sin(2 * Math.PI * freq * t) * 0.55 +
                   Math.sin(2 * Math.PI * freq * 2.02 * t) * 0.16;
        seed = (seed * 1103515245 + 12345) & 0x7fffffff;
        var n = (seed / 0x3fffffff - 1) * noiseAmt * Math.exp(-t * 320);
        d[i] = (tone + n) * env * 0.8;
      }
      return buf;
    }

    function loadSample() {
      if (sampleState !== 0) return;
      if (typeof fetch !== "function") { sampleState = -1; return; }
      sampleState = 1;
      fetch(SAMPLE_URL, { cache: "force-cache" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.arrayBuffer();
        })
        .then(function (raw) {
          return new Promise(function (resolve, reject) {
            // 有的实现只认回调形式，包一层 Promise，两种都能接
            var p = ctx.decodeAudioData(raw, resolve, reject);
            if (p && typeof p.then === "function") p.then(resolve, reject);
          });
        })
        .then(function (decoded) {
          sample = decoded;
          sampleState = 2;
        })
        .catch(function () {
          // 离线报告、静态资源缺失、格式不支持：一律退回合成音，不弹提示打扰用户
          sampleState = -1;
        });
    }

    function ensure() {
      if (ctx) return ctx;
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try {
        ctx = new AC({ latencyHint: "interactive" });
      } catch (e) {
        try { ctx = new AC(); } catch (e2) { return null; }
      }
      master = ctx.createGain();
      hitGain = ctx.createGain();
      master.gain.value = 0;
      hitGain.gain.value = hitVolume;
      master.connect(hitGain);          // 两级串联：回放音量 × 打击音自己的音量
      hitGain.connect(ctx.destination);
      buffers = {
        hit: render(980, 0.055, 0.55),
        hold: render(1240, 0.050, 0.35),
        miss: render(380, 0.075, 0.70),
      };
      loadSample();
      return ctx;
    }

    function applyGain() {
      if (!ctx || !master) return;
      var base = muted ? 0 : Math.max(0, Math.min(1, volume)) * MIX;
      master.gain.setTargetAtTime(base, ctx.currentTime, 0.01);
      hitGain.gain.setTargetAtTime(muted ? 0 : Math.max(0, Math.min(1, hitVolume)),
                                   ctx.currentTime, 0.01);
    }

    var live = [];
    function stopAll() {
      for (var i = 0; i < live.length; i += 1) {
        try { live[i].stop(); } catch (e) { /* 已经停了 */ }
      }
      live = [];
    }

    return {
      get ready() { return !!(ctx && ctx.state === "running"); },
      get usingSample() { return sampleState === 2; },

      unlock: function (vol) {
        if (!ensure()) return false;
        if (typeof vol === "number") volume = vol;
        if (ctx.state === "suspended" && typeof ctx.resume === "function") {
          ctx.resume().catch(function () { /* 用户还没点，下次再试 */ });
        }
        applyGain();
        return true;
      },

      // 谱面音量（和底部那个「音量」滑杆同一个）
      setVolume: function (v) {
        volume = Number(v) || 0;
        applyGain();
      },

      // 打击音自己的音量，独立于谱面音量
      setHitVolume: function (v) {
        hitVolume = Number(v) || 0;
        applyGain();
      },

      setMuted: function (on) {
        muted = !!on;
        applyGain();
        if (muted) stopAll();
      },

      stopAll: stopAll,

      // kind: hit | hold | miss
      play: function (kind) {
        if (!ctx || !master || ctx.state !== "running") return;
        var use = RATE[kind] ? kind : "hit";
        var src = ctx.createBufferSource();
        var local = ctx.createGain();
        if (sample) {
          src.buffer = sample;
          src.playbackRate.value = RATE[use];
          local.gain.value = LEVEL[use];
        } else {
          src.buffer = buffers[use] || buffers.hit;
          local.gain.value = 1;
          // 合成音本来就短，稍微抖一下音高更像连续击打
          src.playbackRate.value = 0.97 + Math.random() * 0.06;
        }
        src.connect(local);
        local.connect(hitGain);
        src.onended = function () {
          var i = live.indexOf(src);
          if (i >= 0) live.splice(i, 1);
        };
        live.push(src);
        if (live.length > 48) live.shift();
        src.start(0);
      },
    };
  }

  window.HitSound = { create: create };
})();
