/* B50 成绩图：把成长页的「综合段位 + 计入评估的 50 局」导出成一张竖图。
 *
 * 版式分两段：上面是总卡（ID、段位、样本、目标、四个练法），下面是 50 张分卡，
 * 5 列 × 10 行——横向排开方便一眼扫完，也不至于把图拉得太长。
 * 分卡只保留页面上那几项（曲名 / 难度 / 准确率 / 折算段位），不额外加工；
 * 不足 50 局时剩下的格子打一行斜的 EMPTY 当水印，让空位看起来是"还没打"，
 * 而不是渲染坏了。
 *
 * 折算段位达到目标段位的那几张分卡会单独标出来：深色版用透明浅粉底，
 * 浅色版（跟着界面主题走）用金底。整张图不跟着变，只有够到目标的那几格变。
 * 配色集中在两套调色板里，绘制代码只读 P，不写死颜色。
 */
(function () {
  const W = 1240;
  const S = 2;
  const PAD = 64;
  const INNER = W - PAD * 2;
  const FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", -apple-system, "Segoe UI", system-ui, sans-serif';

  const COLS = 5;
  const ROWS = 10;
  const GAP = 16;
  const CELL_W = (INNER - GAP * (COLS - 1)) / COLS;
  const CELL_H = 96;
  const ROW_GAP = 10;
  const GRID_TOP = 428;
  const SLOTS = COLS * ROWS;
  const H = GRID_TOP + ROWS * (CELL_H + ROW_GAP) - ROW_GAP + 92;

  const DARK = {
    light: false,
    bg: ["#0a0d1a", "#111731", "#171f40"],
    glowA: "rgba(124,92,255,0.40)",
    glowB: "rgba(34,211,238,0.18)",
    hair: "rgba(255,255,255,0.016)",
    frame: "rgba(255,255,255,0.055)",
    tick: "rgba(255,255,255,0.09)",
    ringA: "rgba(255,255,255,0.06)",
    ringB: "rgba(255,255,255,0.075)",
    cross: "rgba(255,255,255,0.125)",
    chipFill: "rgba(255,255,255,0.045)",
    chipStroke: "rgba(255,255,255,0.08)",
    cell0: "rgba(255,255,255,0.058)",
    cell1: "rgba(255,255,255,0.03)",
    cellStroke: "rgba(255,255,255,0.075)",
    emptyStroke: "rgba(255,255,255,0.10)",
    emptyText: "rgba(255,255,255,0.12)",
    divider: "rgba(255,255,255,0.09)",
    ruler: "rgba(255,255,255,0.07)",
    ink: "#ffffff",
    body: "#e9edfb",
    dim: "#8b93b5",
    rank: "rgba(255,255,255,0.30)",
    accentBar: "rgba(124,92,255,0.9)",
    gold: "#ffd166",
    track: "rgba(255,255,255,0.10)",
    bar0: "#7c5cff",
    bar1: "#22d3ee",
    sqA: "rgba(124,92,255,0.55)",
    sqB: "rgba(34,211,238,0.45)",
    // 折算段位够到目标：透明浅粉，盖在深色卡面上
    hitTop: "rgba(255,143,193,0.26)",
    hitBottom: "rgba(255,143,193,0.13)",
    hitStroke: "rgba(255,143,193,0.55)",
    hitInk: "#ffe6f1",
    hitDim: "rgba(255,220,236,0.72)",
    goldOnHit: "#ffd166",
  };

  // 浅色：跟界面的「白色毛玻璃 · 浅粉」主题一致。底色浅，所有字都反过来用深的；
  // 金色也要压深，否则在浅底上发白发飘。够到目标的卡改成金底。
  const LIGHT = {
    light: true,
    bg: ["#fffbfd", "#fff2f8", "#ffe8f3"],
    glowA: "rgba(255,143,193,0.38)",
    glowB: "rgba(124,92,255,0.10)",
    hair: "rgba(150,60,110,0.05)",
    frame: "rgba(150,60,110,0.13)",
    tick: "rgba(150,60,110,0.20)",
    ringA: "rgba(150,60,110,0.11)",
    ringB: "rgba(150,60,110,0.16)",
    cross: "rgba(150,60,110,0.20)",
    chipFill: "rgba(255,255,255,0.66)",
    chipStroke: "rgba(150,60,110,0.16)",
    cell0: "rgba(255,255,255,0.86)",
    cell1: "rgba(255,255,255,0.56)",
    cellStroke: "rgba(150,60,110,0.17)",
    emptyStroke: "rgba(150,60,110,0.20)",
    emptyText: "rgba(150,60,110,0.20)",
    divider: "rgba(150,60,110,0.16)",
    ruler: "rgba(150,60,110,0.13)",
    ink: "#3b1a2b",
    body: "#4a2335",
    dim: "#8f5f77",
    rank: "rgba(90,35,65,0.40)",
    accentBar: "rgba(214,51,132,0.9)",
    gold: "#b3760a",
    track: "rgba(150,60,110,0.15)",
    bar0: "#ff5fa2",
    bar1: "#7c5cff",
    sqA: "rgba(214,51,132,0.55)",
    sqB: "rgba(124,92,255,0.45)",
    // 够到目标的卡：金底 + 深金描边，数值转深棕才压得住
    hitTop: "rgba(255,208,102,0.66)",
    hitBottom: "rgba(255,190,62,0.44)",
    hitStroke: "rgba(186,128,10,0.55)",
    hitInk: "#6b4500",
    hitDim: "#7a4a20",
    goldOnHit: "#7b5200",
  };

  let P = DARK;

  function isLightTheme() {
    return document.documentElement.getAttribute("data-theme") === "sakura";
  }

  // 11 段往上每段一个希腊字母后缀：12.4 段 → 12.4 段（β）
  const GREEK = ["α", "β", "γ", "δ", "ε", "ζ", "η"];

  function danText(value) {
    const v = Number(value);
    if (!isFinite(v) || v === 0) return "—";
    const base = v.toFixed(2) + " 段";
    const tier = Math.floor(v);
    if (tier >= 11 && tier <= 17) return base + "（" + GREEK[tier - 11] + "）";
    return base;
  }

  function num(v, digits) {
    if (v === null || v === undefined || !isFinite(Number(v))) return "—";
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }

  function clipText(ctx, text, maxW) {
    let s = String(text || "");
    if (ctx.measureText(s).width <= maxW) return s;
    while (s.length > 1 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1);
    return s + "…";
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // 空格子里的水印：斜着压一行 EMPTY。字细、字距拉开，只是提醒"这里还空着"，
  // 不能比已填格子的读数更抢眼
  function paintEmptyMark(ctx, cx, cy) {
    const text = "EMPTY";
    const gap = 4.6;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(-Math.PI / 9);                 // -20°，从左下往右上
    ctx.font = "400 15px " + FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillStyle = P.emptyText;
    const widths = [];
    for (let i = 0; i < text.length; i += 1) widths.push(ctx.measureText(text[i]).width);
    const total = widths.reduce((a, b) => a + b, 0) + gap * (text.length - 1);
    let x = -total / 2;
    for (let i = 0; i < text.length; i += 1) {
      ctx.fillText(text[i], x, 0);
      x += widths[i] + gap;
    }
    ctx.restore();
  }

  function loadLogo() {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
      img.src = "/static/img/logo.svg";
    });
  }

  function paintBackground(ctx) {
    const g = ctx.createLinearGradient(0, 0, W * 0.4, H);
    g.addColorStop(0, P.bg[0]);
    g.addColorStop(0.45, P.bg[1]);
    g.addColorStop(1, P.bg[2]);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    let r = ctx.createRadialGradient(160, -120, 20, 160, -120, 900);
    r.addColorStop(0, P.glowA);
    r.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = r;
    ctx.fillRect(0, 0, W, H);

    r = ctx.createRadialGradient(W - 120, H + 100, 20, W - 120, H + 100, 900);
    r.addColorStop(0, P.glowB);
    r.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = r;
    ctx.fillRect(0, 0, W, H);
  }

  // 平面装饰：跟成绩卡同一套语言——海报外框、按行的刻度、同心圆、十字网格、
  // 两枚实心方块当重音。深色底最亮只有 13% 白，浅色底换成同浓度的玫红。
  function paintDecor(ctx) {
    ctx.save();

    // 横线打底，像印刷品的版心参考线（密一点，整张图不会空）
    ctx.strokeStyle = P.hair;
    ctx.lineWidth = 1;
    for (let y = 90; y < H; y += 90) {
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(W, y + 0.5);
      ctx.stroke();
    }

    // 外框
    ctx.strokeStyle = P.frame;
    ctx.strokeRect(24.5, 24.5, W - 49, H - 49);

    // 左右两侧按分卡行数打的刻度，读起来像版式的标尺
    ctx.strokeStyle = P.tick;
    for (let i = 0; i <= ROWS; i += 1) {
      const y = Math.round(GRID_TOP + i * (CELL_H + ROW_GAP) - (i === ROWS ? ROW_GAP : 0)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(24.5, y);
      ctx.lineTo(40, y);
      ctx.moveTo(W - 24.5, y);
      ctx.lineTo(W - 40, y);
      ctx.stroke();
    }

    // 右上角同心圆：一道实线、一道虚线，正好被画布边缘切掉一截
    ctx.strokeStyle = P.ringA;
    ctx.lineWidth = 1.1;
    ctx.beginPath();
    ctx.arc(W - 40, 6, 212, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([4, 10]);
    ctx.strokeStyle = P.ringB;
    ctx.beginPath();
    ctx.arc(W - 40, 6, 272, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);

    // 总卡中间那块空白：3 列 × 4 行的十字网格，最低一排正好落在进度条的高度上，
    // 这样它看起来是「从段位量到进度条」的刻度，而不是随手撒的几个加号
    ctx.strokeStyle = P.cross;
    ctx.lineWidth = 1.2;
    const arm = 8;
    for (let c = 0; c < 3; c += 1) {
      for (let r = 0; r < 4; r += 1) {
        const cx = 610 + c * 50;
        const cy = 226 + r * 50;
        ctx.beginPath();
        ctx.moveTo(cx - arm, cy);
        ctx.lineTo(cx + arm, cy);
        ctx.moveTo(cx, cy - arm);
        ctx.lineTo(cx, cy + arm);
        ctx.stroke();
      }
    }
    ctx.fillStyle = P.sqA;
    ctx.fillRect(592, 208, 11, 11);
    ctx.fillStyle = P.sqB;
    ctx.fillRect(722, 388, 10, 10);

    ctx.restore();
  }

  function paintHeader(ctx, logo, data) {
    const SIZE = 56;
    // 浅粉底上粉色 logo 会糊在一起，先垫一层白
    if (logo && P.light) {
      ctx.fillStyle = "rgba(255,255,255,0.78)";
      ctx.beginPath();
      ctx.arc(PAD + SIZE / 2, 46 + SIZE / 2, SIZE / 2 + 4, 0, Math.PI * 2);
      ctx.fill();
    }
    if (logo) ctx.drawImage(logo, PAD, 46, SIZE, SIZE);
    const x = PAD + SIZE + 14;
    ctx.textAlign = "left";
    ctx.fillStyle = P.ink;
    ctx.font = "700 24px " + FONT;
    ctx.fillText("Can I get a better score?", x, 76);
    ctx.fillStyle = P.dim;
    ctx.font = "400 12.5px " + FONT;
    ctx.fillText("osu!mania 4K 回放分析 · 个人 B50", x, 100);

    const d = new Date();
    const date = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-"
      + String(d.getDate()).padStart(2, "0");
    ctx.textAlign = "right";
    ctx.fillStyle = P.dim;
    ctx.font = "400 12.5px " + FONT;
    ctx.fillText("导出于 " + date, W - PAD, 98);
    ctx.textAlign = "left";

    // 分割线做成刻度尺：5 格一大刻度，其余小刻度
    ctx.fillStyle = P.divider;
    ctx.fillRect(PAD, 140, INNER, 1);
    ctx.fillStyle = P.ruler;
    for (let i = 0; i * 24 <= INNER; i += 1) {
      ctx.fillRect(Math.round(PAD + i * 24), 140, 1, i % 5 === 0 ? 7 : 4);
    }
  }

  function paintSummary(ctx, data) {
    const overall = data.overall || {};
    const target = data.target || {};
    const skills = data.skills || {};
    const name = (data.profile && data.profile.name) || (data.owner && data.owner.name) || "—";

    // ID 放在综合段位上面，前面一根紫色竖条当锚点
    ctx.fillStyle = P.accentBar;
    ctx.fillRect(PAD, 186, 3, 26);
    ctx.fillStyle = P.ink;
    ctx.font = "600 26px " + FONT;
    ctx.fillText(clipText(ctx, name, INNER * 0.58), PAD + 16, 208);

    ctx.fillStyle = P.dim;
    ctx.font = "400 12.5px " + FONT;
    ctx.fillText("综合段位", PAD, 256);

    const dan = overall.visible ? overall.dan : null;
    ctx.fillStyle = P.ink;
    ctx.font = "700 54px " + FONT;
    const text = dan == null ? "样本不足" : danText(dan);
    ctx.fillText(text, PAD, 316);

    ctx.fillStyle = P.dim;
    ctx.font = "400 13px " + FONT;
    const bits = [];
    if (overall.count != null) bits.push((data.fullSessions || 50) ? overall.count + " / " + (data.fullSessions || 50) + " 局计入" : overall.count + " 局计入");
    if (target.label) bits.push("目标 " + target.label);
    ctx.fillText(bits.join("  ·  "), PAD, 348);

    // 进度条：目标差值越大条越短，和页面上那条一致
    const barW = 460;
    const barY = 372;
    ctx.fillStyle = P.track;
    roundRect(ctx, PAD, barY, barW, 8, 4);
    ctx.fill();
    const p = Math.min(1, Math.max(0, Number(target.progress || 0)));
    if (p > 0) {
      const g = ctx.createLinearGradient(PAD, 0, PAD + barW, 0);
      g.addColorStop(0, P.bar0);
      g.addColorStop(1, P.bar1);
      ctx.fillStyle = g;
      roundRect(ctx, PAD, barY, Math.max(8, barW * p), 8, 4);
      ctx.fill();
    }

    // 右侧 2×2 练法
    const keys = ["jack", "tech", "speed", "stamina"];
    const chipW = 206;
    const chipH = 64;
    const cx0 = W - PAD - chipW * 2 - 12;
    const cy0 = 196;
    keys.forEach((key, i) => {
      const s = skills[key] || {};
      const x = cx0 + (i % 2) * (chipW + 12);
      const y = cy0 + Math.floor(i / 2) * (chipH + 12);
      ctx.fillStyle = P.chipFill;
      roundRect(ctx, x, y, chipW, chipH, 12);
      ctx.fill();
      ctx.strokeStyle = P.chipStroke;
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.fillStyle = P.dim;
      ctx.font = "400 11.5px " + FONT;
      ctx.fillText(s.label || key, x + 14, y + 25);
      ctx.fillStyle = P.ink;
      ctx.font = "600 19px " + FONT;
      ctx.fillText(s.dan == null ? "—" : num(s.dan, 2), x + 14, y + 50);
    });
  }

  // targetDan 是「目标」那一档的数值；折算段位够到它的分卡单独换底
  function paintCell(ctx, p, rank, x, y, targetDan) {
    const value = Number(p.value);
    const hit = targetDan !== null && targetDan !== undefined && isFinite(value)
      && value >= targetDan - 1e-9;

    const g = ctx.createLinearGradient(x, y, x, y + CELL_H);
    g.addColorStop(0, hit ? P.hitTop : P.cell0);
    g.addColorStop(1, hit ? P.hitBottom : P.cell1);
    roundRect(ctx, x, y, CELL_W, CELL_H, 12);
    ctx.fillStyle = g;
    ctx.fill();
    ctx.strokeStyle = hit ? P.hitStroke : P.cellStroke;
    ctx.lineWidth = 1;
    ctx.stroke();

    // 名次
    ctx.textAlign = "left";
    ctx.fillStyle = P.rank;
    ctx.font = "700 10.5px " + FONT;
    ctx.fillText(String(rank).padStart(2, "0"), x + 14, y + 23);

    // 折算段位（右上，最重要的一项）
    ctx.textAlign = "right";
    ctx.fillStyle = hit ? P.hitInk : P.ink;
    ctx.font = "700 17px " + FONT;
    ctx.fillText(num(p.value, 1), x + CELL_W - 14, y + 25);

    // 曲名
    ctx.textAlign = "left";
    ctx.fillStyle = P.body;
    ctx.font = "400 12.5px " + FONT;
    ctx.fillText(clipText(ctx, p.title || "—", CELL_W - 28), x + 14, y + 57);

    // 难度 + 准确率；99% 以上用金色标出来（金底上再压深一档）
    ctx.fillStyle = hit ? P.hitDim : P.dim;
    ctx.font = "400 10.5px " + FONT;
    ctx.fillText(clipText(ctx, p.difficulty || "", CELL_W - 82), x + 14, y + 79);
    const acc = Number(p.acc);
    const golden = isFinite(acc) && acc > 99;
    ctx.textAlign = "right";
    ctx.fillStyle = golden ? (hit ? P.goldOnHit : P.gold) : (hit ? P.hitDim : P.dim);
    ctx.font = (golden ? "600 10.5px " : "400 10.5px ") + FONT;
    ctx.fillText(num(p.acc, 2) + "%", x + CELL_W - 14, y + 79);
    ctx.textAlign = "left";
  }

  function paintEmpty(ctx, x, y) {
    ctx.save();
    ctx.setLineDash([4, 5]);
    ctx.strokeStyle = P.emptyStroke;
    ctx.lineWidth = 1;
    roundRect(ctx, x + 0.5, y + 0.5, CELL_W - 1, CELL_H - 1, 12);
    ctx.stroke();
    ctx.restore();
    paintEmptyMark(ctx, x + CELL_W / 2, y + CELL_H / 2);
  }

  function paintGrid(ctx, data) {
    const plays = (data.overall && data.overall.plays) || [];
    const target = data.target || {};
    const targetDan = (target.dan === null || target.dan === undefined)
      ? null : Number(target.dan);
    for (let i = 0; i < SLOTS; i += 1) {
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      const x = PAD + col * (CELL_W + GAP);
      const y = GRID_TOP + row * (CELL_H + ROW_GAP);
      if (i < plays.length) paintCell(ctx, plays[i], i + 1, x, y, targetDan);
      else paintEmpty(ctx, x, y);
    }
  }

  function paintFooter(ctx, version) {
    ctx.fillStyle = P.divider;
    ctx.fillRect(PAD, H - 74, INNER, 1);

    ctx.textAlign = "left";
    ctx.fillStyle = P.dim;
    ctx.font = "400 12px " + FONT;
    ctx.fillText("osumania4k_replay_analyser by [Crz]Renjie Lan" + (version ? "  ·  v" + version : ""),
      PAD, H - 34);

    ctx.textAlign = "right";
    ctx.fillText("github.com/SWt0619/osumania4k_replay_analyser", W - PAD, H - 34);
    ctx.textAlign = "left";
  }

  async function render(data, opts) {
    const options = opts || {};
    const canvas = document.createElement("canvas");
    canvas.width = W * S;
    canvas.height = H * S;
    const ctx = canvas.getContext("2d");
    ctx.scale(S, S);
    ctx.textBaseline = "alphabetic";

    const logo = await loadLogo();
    // 配色只在这里切；下面全部是同步绘制，不会和另一次导出抢 P。
    // 默认跟界面主题：浅粉主题导出浅色版，其余导出深色版
    const light = options.light === undefined ? isLightTheme() : !!options.light;
    P = light ? LIGHT : DARK;
    paintBackground(ctx);
    paintDecor(ctx);
    paintHeader(ctx, logo, data);
    paintSummary(ctx, data);
    paintGrid(ctx, data);
    paintFooter(ctx, options.version);
    return canvas;
  }

  function fileName(data) {
    const name = (data.profile && data.profile.name) || (data.owner && data.owner.name) || "玩家";
    return ("B50_" + name).replace(/[\\/:*?"<>|]/g, "_").slice(0, 80) + ".png";
  }

  async function save(data, opts) {
    const canvas = await render(data, opts);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("canvas 导出失败");
    const name = fileName(data);
    // 本机程序：直接写进程序目录的 card/，比让人去下载文件夹里翻更省事。
    // 服务端没这个接口（老版本）时退回浏览器下载，功能不至于不可用。
    if (window.API && window.API.saveCard) {
      try {
        const res = await window.API.saveCard(blob, name);
        if (res && res.ok) return res;
      } catch (e) { /* 退回下载 */ }
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return { ok: true, dir: "", name: name };
  }

  window.B50Card = { render, save, W, H };
})();
