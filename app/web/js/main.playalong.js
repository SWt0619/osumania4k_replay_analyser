/* 跟打：把当前谱面当成一张可玩的图，在网页里按键打。

   判定窗口不另写一套：照 beatmap.judgement_windows 的 stable 公式算，
   同一个 OD 在游戏里和这里落在同一档。长条按 V1 口径合成一个判定
   （头判与尾判取较差的一档），所以结算的条数和谱面物件数对得上。

   时间用 performance.now 推进，音频只在开始和漂移时对齐：音频 currentTime
   的精度是几毫秒到几十毫秒，逐帧拿它判定会让 16ms 的 MAX 窗口抖掉。 */
(function (App) {
  "use strict";

  const state = App.state;
  const { $ } = window.UI;

  const JUDGES = ["max", "great", "good", "ok", "meh", "miss"];
  const LABEL = { max: "MAX", great: "300", good: "200", ok: "100", meh: "50", miss: "MISS" };
  // V1 权重：MAX 与 300 同权，所以全 MAX 也是 100%
  const WEIGHT = { max: 300, great: 300, good: 200, ok: 100, meh: 50, miss: 0 };
  // 判定提示点的颜色：白 / 黄 / 绿 / 蓝 / 紫 / 红，和 player.js 的 PRESS_COLORS 同一套。
  // 固定色不跟主题走——这个点始终落在深色轨道上，换主题变淡反而看不清。
  const JUDGE_DOT = {
    max: "#ffffff", great: "#ffd54a", good: "#4ade80",
    ok: "#5aa2ff", meh: "#b06bff", miss: "#ff4d4d",
  };
  const TAIL_FACTOR = 1.5;      // 尾判窗口放大 1.5 倍，和 match.py 一致
  const LEAD_IN_MS = 1200;      // 开始时刻至少比第一颗音符早这么多（另留 220ms 顶边余量）
  const PREPARE_MS = 3000;      // 点下跟打后先停三秒：数完 3-2-1 才走时钟和音频
  // 退场淡出的时长，和 CSS 的 paExit 动画对齐；进场只由 CSS 播，脚本不用等
  const EXIT_MS = 700;
  const TAIL_MS = 1500;         // 最后一个物件之后再等一会儿，长条尾判能结算完
  // 跟打开放到 Reform 4 及以下：后端 PLAYALONG_DAN = 4.6，即 Reform 5 low 之前
  const PLAYALONG_DAN = 4.6;

  let pa = null;                // 进行中的一局
  let exiting = false;          // 正在播退场动画：这期间不接受新的一局，也不让回放快捷键生效
  let rafId = 0;
  let lastFrameAt = 0;          // 上一帧的 rAF 时间戳，用来按回放的帧率上限排帧
  let captureBtn = null;        // 正在等待按键的键位按钮

  // ---------------------------------------------------------------- 判定窗与档位
  function windowsOf(od) {
    const o = Math.max(0, Math.min(10, Number(od) || 0));
    const cut = (v) => Math.floor(v + 1e-9);   // 官方是向下取整，见 beatmap.judgement_windows
    return {
      max: 16,
      great: cut(64 - 3 * o),
      good: cut(97 - 3 * o),
      ok: cut(127 - 3 * o),
      meh: cut(151 - 3 * o),
      miss: cut(188 - 3 * o),
    };
  }

  function scaledWindows(w, factor) {
    const out = {};
    for (const k of JUDGES) out[k] = w[k] * factor;
    return out;
  }

  function gradeOf(absMs, w) {
    if (absMs <= w.max) return "max";
    if (absMs <= w.great) return "great";
    if (absMs <= w.good) return "good";
    if (absMs <= w.ok) return "ok";
    if (absMs <= w.meh) return "meh";
    return "miss";
  }

  // 越靠后越差；长条头尾合成时取这个顺序的较大者
  function worse(a, b) {
    return JUDGES.indexOf(a) >= JUDGES.indexOf(b) ? a : b;
  }

  // ---------------------------------------------------------------- 建局
  function buildNotes(data) {
    const src = Array.isArray(data.notes) ? data.notes : [];
    const out = src.map((n) => ({
      t: Number(n.t),
      c: Number(n.c) | 0,
      h: (typeof n.h === "number" && n.h > n.t) ? n.h : null,
      // 播放器读 j 上色：-1 表示还没判，画成中性灰
      j: -1,
      d: null,
      rd: null,
      e: 0,
      sev: 0,
      e2: 0,
      sev2: 0,
      cat: null,
      cat2: null,
      jc: -1,
      _judged: false,
      _head: null,
      _holding: false,
    }));
    return out.filter((n) => Number.isFinite(n.t) && n.c >= 0 && n.c < 4)
      .sort((a, b) => a.t - b.t);
  }

  function accuracy(counts, judged) {
    if (!judged) return 0;
    let got = 0;
    for (const g of JUDGES) got += WEIGHT[g] * counts[g];
    return (got / (300 * judged)) * 100;
  }

  // ---------------------------------------------------------------- 时间轴
  // 以 performance.now 为基准，音频只用来纠偏：两者差超过 60ms 才重新对齐，
  // 避免音频 currentTime 的粗粒度把每一帧都拽回去。
  function chartTime() {
    if (!pa) return 0;
    // 倒计时期间画面钉在起始帧：时钟不走，按键也不判定
    if (pa.preparing) return pa.startMs;
    // 音频比谱面短时（或播放中途被掐断），后面的音符用虚拟时钟走完
    if (!pa.virtual && state.audio && state.audio.ended) pa.virtual = true;
    if (!pa.virtual && App.audioUsable()) {
      const at = state.audio.currentTime * 1000 + state.audioOffset;
      const pt = pa.startMs + (performance.now() - pa.perfStart);
      if (Math.abs(at - pt) > 60) pa.perfStart = performance.now() - (at - pa.startMs);
    }
    return pa.startMs + (performance.now() - pa.perfStart);
  }

  // ---------------------------------------------------------------- 判定
  function finish(note, grade, at) {
    note._judged = true;
    note._holding = false;
    // 判定发生在哪一刻（谱面毫秒）：播放器按它放击打闪光，漏接和长条也能对上时间
    if (typeof at === "number") note._jt = at;
    note.j = JUDGES.indexOf(grade);
    pa.counts[grade] += 1;
    pa.judged += 1;
    pa.hudDirty = true;
    // 屏幕中心偏上的判定点：一帧里判了好几个也只按最后一个画，避免密集段反复回流
    pa.judgeGrade = grade;
    pa.judgeDirty = true;
    if (grade === "miss") {
      pa.combo = 0;
    } else {
      pa.combo += 1;
      if (pa.combo > pa.maxCombo) pa.maxCombo = pa.combo;
    }
  }

  function press(col, t) {
    if (!pa || pa.preparing || pa.hold[col]) return;   // 倒计时里不算；这根手指还按着长条也不算
    const w = pa.win;
    const list = pa.byCol[col];
    let i = pa.cursor[col];
    let note = null;
    while (i < list.length) {
      const n = list[i];
      if (n._judged) { i += 1; continue; }
      if (n.t + w.miss < t) { finish(n, "miss", t); i += 1; continue; }
      note = n;
      break;
    }
    pa.cursor[col] = i;
    if (!note) return;

    const delta = t - note.t;
    if (Math.abs(delta) > w.miss) return;   // 太早：空敲，不消费音符
    const head = gradeOf(Math.abs(delta), w);
    // 声音和手指同时到，不等渲染循环按 note.t 补播；空敲不响（游戏里也是音符带声音）。
    // 按到 miss 窗里也算消费了这一下，但声音该是漏接那一档。
    playHit(head === "miss" ? "miss" : (note.h ? "hold" : "hit"));
    note.d = Math.round(delta * 100) / 100;
    if (note.h) {
      // 长条先记头判，等松手（或按到结尾）再合成
      note._head = head;
      note._holding = true;
      pa.hold[col] = note;
    } else {
      finish(note, head, t);
    }
  }

  function release(col, t) {
    const note = pa && !pa.preparing && pa.hold[col];
    if (!note) return;
    pa.hold[col] = null;
    note._holding = false;
    const tail = gradeOf(Math.abs(t - note.h), scaledWindows(pa.win, TAIL_FACTOR));
    finish(note, worse(note._head || "miss", tail), t);
  }

  function advance(t) {
    for (let col = 0; col < 4; col += 1) {
      const list = pa.byCol[col];
      let i = pa.cursor[col];
      while (i < list.length) {
        const n = list[i];
        if (n._judged) { i += 1; continue; }
        if (n._holding) {
          // 一直按到结尾：尾判按 MAX 收，之后松手不再结算
          if (t >= n.h) {
            n._holding = false;
            if (pa.hold[col] === n) pa.hold[col] = null;
            finish(n, worse(n._head || "miss", "max"), t);
            i += 1;
            continue;
          }
          break;
        }
        if (t > n.t + pa.win.miss) { finish(n, "miss", t); i += 1; continue; }
        break;
      }
      pa.cursor[col] = i;
    }
  }

  // ---------------------------------------------------------------- 开局 / 收局
  // 段位由后端定档（evaluation.playalong），前端不重复算；旧服务端没有这个字段时
  // 退回简易报告的档位，再不行才拿 MSD 换算值兜底。
  function playalongAllowed(data) {
    if (!data) return false;
    const ev = data.evaluation || {};
    if (typeof ev.playalong === "boolean") return ev.playalong;
    if (typeof ev.simple === "boolean") return ev.simple;
    const dan = data.msd && data.msd.dan ? Number(data.msd.dan.dan) : NaN;
    return Number.isFinite(dan) && dan < PLAYALONG_DAN;
  }

  // 按钮的锁定态跟着段位走（UI.applySimpleMode 与建议回传后都会调到这里）。
  // 不用 disabled：禁用后 title 在多数浏览器里不再弹，玩家不知道为什么不给点。
  function updatePlayalongGate() {
    const btn = $("btnPlayalong");
    if (!btn) return;
    const allowed = playalongAllowed(state.data);
    btn.classList.toggle("is-locked", !allowed);
    btn.setAttribute("aria-disabled", allowed ? "false" : "true");
  }

  function replayCard() {
    return document.querySelector("#panel-replay .player-card");
  }

  // 「禁用动画」和系统级的减少动态效果都算：不做过渡，也不等过渡时长
  function motionOff() {
    return !!state.disableMotion ||
      !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function start() {
    if (exiting || (pa && pa.active)) return;
    const data = state.data;
    if (!data || !Array.isArray(data.notes) || !data.notes.length) {
      window.UI.toast("先导入一份回放，跟打需要谱面音符", true);
      return;
    }
    if (!playalongAllowed(data)) {
      window.UI.toast("跟打只对 Reform 4 及以下的谱面开放", true);
      return;
    }
    if (!data.beatmap || Number(data.beatmap.keys) !== 4) {
      window.UI.toast("跟打只支持 4K 谱面", true);
      return;
    }
    if (state.compare) App.closeCompare();
    if (state.playing) App.pause();
    // 倒计时的三秒要安静：即使播放标志和音频状态不同步，也先把声音停掉
    if (state.audio) {
      try { state.audio.pause(); } catch (e) { /* ignore */ }
    }

    const notes = buildNotes(data);
    if (!notes.length) {
      window.UI.toast("这张谱面没有可打的音符", true);
      return;
    }

    const first = Number(data.beatmap.firstObject) || notes[0].t;
    const last = Number(data.beatmap.lastObject) || notes[notes.length - 1].t;
    // 起步点取「第一颗音符刚进画面一小段」：倒计时里轨道不是空的，数完就能接着落。
    // 晚 220ms 起步，让第一颗音符离顶边留一小段，不然只画出半个头。
    const startMs = Math.max(0, first - Math.max(LEAD_IN_MS, state.scrollMs) + 220);
    const byCol = [[], [], [], []];
    for (const n of notes) byCol[n.c].push(n);

    pa = {
      active: true,
      notes,
      byCol,
      cursor: [0, 0, 0, 0],
      hold: [null, null, null, null],
      counts: { max: 0, great: 0, good: 0, ok: 0, meh: 0, miss: 0 },
      judged: 0,
      combo: 0,
      maxCombo: 0,
      win: windowsOf(data.beatmap.od),
      startMs,
      endT: last + TAIL_MS,
      perfStart: performance.now(),
      virtual: !(state.audio && !state.audioBroken),
      prevTime: App.currentTimeMs(),
      hudDirty: true,
      judgeGrade: null,
      judgeDirty: false,
      preparing: true,
      prepareUntil: performance.now() + PREPARE_MS,
      cdShown: 0,
      hitsound: ensureHitsound(),
    };

    $("panel-replay").classList.add("is-playalong");
    document.body.classList.add("is-playalong");
    // 进场：先停在「透明 + 略微放大」的初始态，下一帧再打开，同一帧加两个类不会有过渡。
    // 禁用动画时直接到位。
    const card = replayCard();
    if (card) {
      // 清掉上一次退场留下的类，再加 is-open：动画从起始帧开始，不会先闪一下整屏
      card.classList.remove("is-return", "is-exiting");
      card.classList.add("is-open");
    }
    $("paHud").hidden = false;
    const judgeBox = $("paJudge");
    if (judgeBox) {
      judgeBox.hidden = false;
      judgeBox.classList.remove("is-pop");
    }

    // 进入全屏布局后再建/量播放器，否则画布还是旧高度
    App.ensurePlayer();
    const player = state.player;
    if (!player) {
      window.UI.toast("播放器没有就绪，稍后再试", true);
      endRound(false, false);
      return;
    }
    player.load({ keys: 4, notes, firstObject: first, lastObject: last });
    player.setOptions({
      scrollMs: state.scrollMs,
      // 跟打固定 1.0×（音频和时钟都不变速），窗口不需要折算
      rate: 1,
      showPresses: false,
      deviationBars: false,
      showJudgements: true,
      judgementFilter: "all",
      // 判定在网页这边算，播放器只按时间画音符；声音也由 press() 直接播，
      // 不能再让播放器按 note.t 补一遍（会双响，而且时机偏）
      hitsound: null,
      hitsoundMode: "off",
      // 打掉的音符立刻消失，只留一次击打闪光 —— 这是「打中了」最直接的反馈
      hideJudged: true,
    });
    player.resize();
    player.setTime(startMs);
    player.render();

    paintHud();
    showCountdown(PREPARE_MS / 1000);
    scheduleNext();
  }

  // 三秒倒计时走完：从这里才开始走时钟、放音频。
  // round 是这一局的引用：中途退出/重开时 pa 已经换了，回调不能再动新的一局。
  function beginRun() {
    const round = pa;
    if (!round) return;
    round.preparing = false;
    hideCountdown();
    round.perfStart = performance.now();
    const audio = state.audio;
    if (round.virtual || !audio) return;
    try {
      // 跟打固定 1.0×：回放可能选了 0.5~1.5×，退出时再还回去
      audio.playbackRate = 1;
      audio.currentTime = Math.max(0, (round.startMs - state.audioOffset) / 1000);
    } catch (e) { /* 元数据没就绪：虚拟时钟接着走 */ }
    audio.play().then(() => {
      if (pa !== round) return;
      // 真正开始出声的时刻才是画面上第一帧，按它重新对齐
      round.virtual = false;
      round.perfStart = performance.now()
        - (audio.currentTime * 1000 + state.audioOffset - round.startMs);
    }).catch(() => {
      if (pa === round) round.virtual = true;
    });
  }

  // ---------------------------------------------------------------- 帧调度
  // 跟打的节拍跟回放的「帧率上限」共用一套：App.frameMs 由 applyFpsCap 维护。
  // 回放里设 60，这里也按 60 排；设「不限制」时 frameMs 是 4ms，实际跟着屏幕刷新率走。
  // 只在 rAF 里按时间戳做闸门，不额外开定时器：跟打不需要超过屏幕刷新率的帧。
  function scheduleNext() {
    const capMs = Number(App.frameMs) || 0;
    if (!(capMs > 0)) {
      rafId = requestAnimationFrame(frame);
      return;
    }
    rafId = requestAnimationFrame(function wait(ts) {
      if (ts - lastFrameAt < capMs - 0.5) {
        rafId = requestAnimationFrame(wait);
        return;
      }
      lastFrameAt = ts;
      frame();
    });
  }

  // ---------------------------------------------------------------- 倒计时
  function showCountdown(sec) {
    const box = $("paCountdown");
    if (!box) return;
    const num = box.querySelector("b");
    if (num) {
      num.textContent = String(sec);
      // 重播一次弹出动画：摘掉 → 强制回流 → 挂回，否则数字换了却没动
      num.style.animation = "none";
      void num.offsetWidth;
      num.style.animation = "";
    }
    box.hidden = false;
  }

  function hideCountdown() {
    const box = $("paCountdown");
    if (box) box.hidden = true;
  }

  function paintCountdown(leftMs) {
    const sec = Math.max(1, Math.ceil(leftMs / 1000));
    if (pa && pa.cdShown === sec) return;
    if (pa) pa.cdShown = sec;
    showCountdown(sec);
  }

  // ---------------------------------------------------------------- 打击音
  // 复用回放那套 HitSound（同一个采样、同一个打击音音量），实例由 main.replay.js 持有，
  // 这里只负责按键时响，不接管它的生命周期。
  function ensureHitsound() {
    if (!window.HitSound) return null;
    try {
      const hs = App.ensureHitsound ? App.ensureHitsound() : window.HitSound.create();
      if (!hs) return null;
      hs.setMuted(!!state.muted);
      hs.setVolume(state.muted ? 0 : state.volume);
      hs.setHitVolume(state.hitsoundVolume);
      hs.unlock(state.muted ? 0 : state.volume);
      return hs;
    } catch (e) {
      return null;   // 没有 WebAudio 也要能打，静音继续
    }
  }

  function playHit(kind) {
    if (!pa || !pa.hitsound) return;
    if (!state.hitsoundVolume) return;
    try {
      pa.hitsound.play(kind);
    } catch (e) { /* 放不出声音不影响判定 */ }
  }

  function frame() {
    if (!pa || !pa.active) return;
    if (pa.preparing) {
      const left = pa.prepareUntil - performance.now();
      if (left > 0) {
        paintCountdown(left);
        scheduleNext();
        return;
      }
      beginRun();
    }
    const t = chartTime();
    advance(t);
    if (pa.judgeDirty) paintJudge();
    if (t > pa.endT) {
      for (const n of pa.notes) if (!n._judged) finish(n, "miss", t);
      endRound(true);
      return;
    }
    const player = state.player;
    if (player) {
      player.setTime(t);
      player.render();
    }
    if (pa.hudDirty) paintHud();
    scheduleNext();
  }

  function endRound(natural, restore = true) {
    if (!pa) return;
    const info = pa;
    pa = null;
    exiting = true;
    cancelAnimationFrame(rafId);
    rafId = 0;
    if (state.audio) {
      try { state.audio.pause(); } catch (e) { /* ignore */ }
    }
    $("paHud").hidden = true;
    const judgeBox = $("paJudge");
    if (judgeBox) judgeBox.hidden = true;
    hideCountdown();
    // 掐掉还在响的打击音：结算弹窗弹出来时不该还吊着上一击的尾音
    if (info.hitsound) {
      try { info.hitsound.stopAll(); } catch (e) { /* ignore */ }
    }

    // 淡出跑完（或禁用动画）之后才真正恢复页面：先恢复布局会「啪」地跳一下
    const finish = () => {
      if (pa) return;   // 退场途中又开了新的一局，别把新界面收掉
      exiting = false;
      const card = replayCard();
      if (card) card.classList.remove("is-open", "is-exiting");
      $("panel-replay").classList.remove("is-playalong");
      document.body.classList.remove("is-playalong");
      // 回到页面原位时补一个短淡入：位置是瞬间换回来的，但不要在视觉上「啪」一下
      if (card && !motionOff()) {
        card.classList.add("is-return");
        setTimeout(() => card.classList.remove("is-return"), 240);
      }

      if (restore) {
        // 回到回放视图：重新载入回放音符，并把时间放回进跟打之前
        try {
          // 跟打时音频固定 1.0×，回放可能选了 0.5~1.5×，退出时要把倍率还回去
          if (state.audio) state.audio.playbackRate = state.speed;
          App.ensurePlayer();
          const player = state.player;
          if (player) {
            player.setOptions({
              showPresses: true, deviationBars: true, hideJudged: false,
              hitsoundMode: state.hitsoundMode,
            });
          }
          App.seekTo(info.prevTime, false);
          if (player) player.resize();
        } catch (e) { /* 恢复失败也不该挡住结算 */ }
      }

      renderResult({
        counts: info.counts,
        judged: info.judged,
        total: info.notes.length,
        maxCombo: info.maxCombo,
        acc: accuracy(info.counts, info.judged),
        natural: !!natural,
      });
    };

    const card = replayCard();
    if (card && !motionOff()) {
      // 退场：换成 is-exiting 播淡出，等动画走完再恢复页面布局
      card.classList.remove("is-open");
      card.classList.add("is-exiting");
      setTimeout(finish, EXIT_MS);
    } else {
      finish();
    }
  }

  function renderResult(res) {
    $("paResultTitle").textContent = res.natural ? "跟打完成" : "已退出跟打";
    $("paResultAcc").textContent = res.acc.toFixed(2) + "%";
    const box = $("paResultCounts");
    box.replaceChildren(...JUDGES.map((g) => {
      const cell = document.createElement("div");
      cell.className = "pa-count-item is-" + g;
      const label = document.createElement("span");
      label.textContent = LABEL[g];
      const value = document.createElement("b");
      value.textContent = String(res.counts[g]);
      cell.append(label, value);
      return cell;
    }));
    const parts = ["最大连击 " + res.maxCombo, "已判 " + res.judged + " / " + res.total];
    if (res.judged < res.total) parts.push("未打 " + (res.total - res.judged));
    $("paResultMeta").textContent = parts.join(" · ");
    $("paResult").hidden = false;
  }

  // ---------------------------------------------------------------- 界面
  function paintHud() {
    if (!pa) return;
    pa.hudDirty = false;
    $("paCombo").textContent = String(pa.combo);
    // 一颗都没打时显示 100%，比 0.00% 更符合"还没掉准"的直觉
    $("paAccHud").textContent = (pa.judged ? accuracy(pa.counts, pa.judged) : 100).toFixed(2) + "%";
    $("paCountHud").textContent = JUDGES.map((g) => LABEL[g] + " " + pa.counts[g]).join(" · ");
    $("paScrollVal").textContent = state.scrollMs + "ms";
    $("paKeyHint").textContent = state.playKeys.map(keyLabel).join(" ");
  }

  // 判定点：换色 + 重播一次缩放动画。动画挂在 .is-pop 上，
  // 连着两下判定时先摘掉类再强制回流，否则第二下不会重新弹。
  function paintJudge() {
    if (!pa) return;
    pa.judgeDirty = false;
    const box = $("paJudge");
    if (!box) return;
    box.style.setProperty("--pa-judge-color", JUDGE_DOT[pa.judgeGrade] || JUDGE_DOT.max);
    box.classList.remove("is-pop");
    void box.offsetWidth;
    box.classList.add("is-pop");
  }

  function nudgeScroll(delta) {
    const next = Math.max(250, Math.min(800, state.scrollMs + delta));
    if (next === state.scrollMs) return;
    state.scrollMs = next;
    App.savePrefs();
    App.syncScrollUi();
    if (pa && state.player) state.player.setOptions({ scrollMs: next });
    if (pa) paintHud();
  }

  // ---------------------------------------------------------------- 键位设置
  const KEY_LABEL = {
    Space: "空格", ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓",
    Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backslash: "\\",
    BracketLeft: "[", BracketRight: "]", Minus: "-", Equal: "=", Backquote: "`",
  };

  function keyLabel(code) {
    if (!code) return "?";
    if (code.indexOf("Key") === 0) return code.slice(3);
    if (code.indexOf("Digit") === 0) return code.slice(5);
    if (code.indexOf("Numpad") === 0) return "小" + code.slice(6);
    return KEY_LABEL[code] || code;
  }

  function renderKeyButtons() {
    document.querySelectorAll("#paKeyRow .pa-key").forEach((btn) => {
      const col = Number(btn.dataset.col);
      btn.textContent = keyLabel(state.playKeys[col]);
      btn.classList.remove("is-listening");
    });
  }

  function captureKey(btn, e) {
    e.preventDefault();
    e.stopPropagation();
    if (e.code === "Escape") {
      captureBtn = null;
      renderKeyButtons();
      return;
    }
    if (!e.code || e.ctrlKey || e.metaKey || e.altKey) return;
    const col = Number(btn.dataset.col);
    const keys = state.playKeys.slice();
    const used = keys.indexOf(e.code);
    if (used >= 0 && used !== col) keys[used] = keys[col];   // 和别的列撞了就对调
    keys[col] = e.code;
    state.playKeys = keys;
    App.savePrefs();
    captureBtn = null;
    renderKeyButtons();
  }

  // ---------------------------------------------------------------- 事件
  function onKeyDown(e) {
    if (captureBtn) { captureKey(captureBtn, e); return; }
    if (!pa || !pa.active) return;
    if (e.repeat) return;
    const tag = (e.target && e.target.tagName) || "";
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
    if (e.code === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      endRound(false);
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const col = state.playKeys.indexOf(e.code);
    if (col < 0) return;
    e.preventDefault();
    e.stopPropagation();   // 别让空格之类的全局快捷键同时触发回放播放
    press(col, chartTime());
  }

  function onKeyUp(e) {
    if (!pa || !pa.active) return;
    const col = state.playKeys.indexOf(e.code);
    if (col < 0) return;
    e.preventDefault();
    e.stopPropagation();
    release(col, chartTime());
  }

  function onBlur() {
    if (!pa || !pa.active) return;
    const t = chartTime();
    for (let col = 0; col < 4; col += 1) if (pa.hold[col]) release(col, t);
  }

  function bindPlayalong() {
    const btn = $("btnPlayalong");
    if (btn) btn.addEventListener("click", start);
    const exit = $("btnPaExit");
    if (exit) exit.addEventListener("click", () => endRound(false));
    const again = $("btnPaAgain");
    if (again) again.addEventListener("click", () => { $("paResult").hidden = true; start(); });
    const close = $("btnPaClose");
    if (close) close.addEventListener("click", () => { $("paResult").hidden = true; });
    const slower = $("paSlower");
    if (slower) slower.addEventListener("click", () => nudgeScroll(25));
    const faster = $("paFaster");
    if (faster) faster.addEventListener("click", () => nudgeScroll(-25));

    document.querySelectorAll("#paKeyRow .pa-key").forEach((b) => {
      b.addEventListener("click", () => {
        captureBtn = b;
        document.querySelectorAll("#paKeyRow .pa-key").forEach((x) => x.classList.remove("is-listening"));
        b.classList.add("is-listening");
        b.textContent = "按…";
      });
    });

    renderKeyButtons();
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onBlur);
  }

  function isPlayalongActive() {
    // 退场动画期间也算「在用跟打」：否则空格会在这半秒里把回放播起来
    return !!(pa && pa.active) || exiting;
  }

  // 当前跟打时间（谱面毫秒）。给调试和自动化测试用：从外面没法读 pa 的闭包。
  function playalongTime() {
    return pa && pa.active ? chartTime() : null;
  }

  Object.assign(App, {
    bindPlayalong, startPlayalong: start, stopPlayalong: endRound,
    isPlayalongActive, playalongTime, updatePlayalongGate,
    // 读取本地偏好之后再画一次键位：bindEvents 那会儿用的还是默认值
    syncPlayKeys: renderKeyButtons,
  });
})(window.App = window.App || {});
