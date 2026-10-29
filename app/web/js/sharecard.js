/* 成绩卡：把一局分析压成一张竖版 1080×1350 的图，直接发群 / 发推。
 *
 * 为什么要单独画一张而不是截屏：截图里全是窗口尺寸、主题和滚动位置，发出去别人
 * 只能看出"这是某个工具"。这里固定版式，只留看得懂一局的几个数：曲名难度、
 * 官方评级与准确率、段位、UR、整局偏移、失误构成。
 *
 * 评级用 osu! 官方口径（按准确率）：SS 100% / S 95% / A 90% / B 80% / C 70% / D。
 * 不要用本程序的 evaluation.grade —— 那套有 A+、还会按失误结构降档，
 * 印在卡片上和游戏结算屏对不上，容易被当成"游戏给的就是这个"。
 */
(function () {
  const W = 1080;
  const H = 1350;
  const S = 2;                       // 2 倍超采样
  const PAD = 64;
  const INNER = W - PAD * 2;
  const FONT = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", -apple-system, "Segoe UI", system-ui, sans-serif';
  const DIM = "#8b93b5";
  const INK = "#e9edfb";

  const GRADE_COLOR = {
    SS: "#ffd479",
    S: "#7ef2d0",
    A: "#22d3ee",
    B: "#7c5cff",
    C: "#ff9f6e",
    D: "#ff6b8a",
  };
  // 环按评级填：SS / S 满圈，往下递减。和页面上那个圆环同一套比例，去掉 A+ 那一档
  const GRADE_RATIO = { SS: 1, S: 1, A: 0.84, B: 0.68, C: 0.52, D: 0.36 };

  // 和页面上的四类失误同一个色（ui.js 的 CATS）
  const CATS = [
    ["ability", "超出能力范围", "#ff9f6e"],
    ["rhythm", "节奏感弱", "#6ee7ff"],
    ["distraction", "分神", "#c792ff"],
    ["slip", "随机失手", "#8b93b5"],
  ];

  function num(v, digits) {
    if (v === null || v === undefined || !isFinite(Number(v))) return "—";
    return Number(v).toFixed(digits === undefined ? 0 : digits);
  }

  function signed(v, digits) {
    if (v === null || v === undefined || !isFinite(Number(v))) return "—";
    const n = Number(v);
    const d = digits === undefined ? 1 : digits;
    return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(d);
  }

  // osu!mania 官方评级（按准确率）。SS 只在满分时给，浮点误差不算
  function officialGrade(acc) {
    const a = Number(acc);
    if (!isFinite(a)) return "—";
    if (a >= 99.9995) return "SS";
    if (a >= 95) return "S";
    if (a >= 90) return "A";
    if (a >= 80) return "B";
    if (a >= 70) return "C";
    return "D";
  }

  function clipText(ctx, text, maxW) {
    let s = String(text || "");
    if (ctx.measureText(s).width <= maxW) return s;
    while (s.length > 1 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1);
    return s + "…";
  }

  // 行首禁则：这些标点不能出现在下一行开头，遇到就让上一行多背一个字
  const NO_START = "、。，．,.!?！？：；）]】》」』”’…～ー・";

  // 竖版标题一行放不下：最多折两行，尽量在空格处断，中文/日文按字断
  function wrapLines(ctx, text, maxW, maxLines) {
    const chars = Array.from(String(text || ""));
    const lines = [];
    let line = "";
    let truncated = false;
    for (let i = 0; i < chars.length; i += 1) {
      const test = line + chars[i];
      if (line && ctx.measureText(test).width > maxW) {
        if (NO_START.indexOf(chars[i]) >= 0) {
          // 标点跟着上一行走，下一轮再断
          line = test;
          continue;
        }
        if (lines.length + 1 >= maxLines) {
          // 已经到最后一行：把剩下的都塞进来，交给 clipText 补省略号
          line = line + chars.slice(i).join("");
          truncated = true;
          break;
        }
        const sp = line.lastIndexOf(" ");
        if (sp > line.length * 0.55) {
          lines.push(line.slice(0, sp));
          line = line.slice(sp + 1) + chars[i];
        } else {
          lines.push(line);
          line = chars[i];
        }
      } else {
        line = test;
      }
    }
    if (truncated) {
      lines.push(clipText(ctx, line, maxW));
    } else if (line) {
      lines.push(line);
    }
    return lines.slice(0, maxLines);
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

  // 读不到 logo.svg（离线报告、文件缺失）时按同一套形状画一个，卡片上仍然有标
  function drawFallbackLogo(ctx, x, y, size) {
    const cx = x + size / 2;
    const cy = y + size / 2;
    const r = size / 2;
    const g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.2, cx, cy, r);
    g.addColorStop(0, "#ff8fc0");
    g.addColorStop(0.62, "#ff66aa");
    g.addColorStop(1, "#e0508f");
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = g;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.87, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.96)";
    ctx.lineWidth = size * 0.026;
    ctx.stroke();

    const pts = [[0.30, 0.57], [0.38, 0.50], [0.45, 0.59], [0.52, 0.43], [0.59, 0.48], [0.69, 0.35]];
    ctx.beginPath();
    pts.forEach(([px, py], i) => {
      const X = x + px * size;
      const Y = y + py * size;
      if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
    });
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = size * 0.024;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
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
    g.addColorStop(0, "#0a0d1a");
    g.addColorStop(0.5, "#111731");
    g.addColorStop(1, "#171f40");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    let r = ctx.createRadialGradient(180, -120, 20, 180, -120, 780);
    r.addColorStop(0, "rgba(124,92,255,0.40)");
    r.addColorStop(1, "rgba(124,92,255,0)");
    ctx.fillStyle = r;
    ctx.fillRect(0, 0, W, H);

    r = ctx.createRadialGradient(960, H + 80, 20, 960, H + 80, 720);
    r.addColorStop(0, "rgba(34,211,238,0.20)");
    r.addColorStop(1, "rgba(34,211,238,0)");
    ctx.fillStyle = r;
    ctx.fillRect(0, 0, W, H);
  }

  // 平面装饰：构成主义那一路——硬边三角、十字网格、细规线、同心底圆。
  // 全部压在内容下面，最亮的一层也只有 12% 白，保证不影响读数。
  function paintDecor(ctx) {
    ctx.save();

    // 底纹：90px 一格的极淡横线，像印刷品的版心参考线
    ctx.strokeStyle = "rgba(255,255,255,0.018)";
    ctx.lineWidth = 1;
    for (let y = 90; y < H; y += 90) {
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(W, y + 0.5);
      ctx.stroke();
    }

    // 左下角：一个直角三角，描边 + 内侧浅填充
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(0, H - 20);
    ctx.lineTo(0, H - 250);
    ctx.lineTo(268, H - 20);
    ctx.closePath();
    ctx.stroke();
    ctx.fillStyle = "rgba(255,255,255,0.028)";
    ctx.beginPath();
    ctx.moveTo(0, H - 96);
    ctx.lineTo(0, H - 210);
    ctx.lineTo(150, H - 96);
    ctx.closePath();
    ctx.fill();

    // 左栏十字网格：4 × 5，间距 52，正好落在评级圈左边的空白里
    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1.3;
    const arm = 6;
    for (let col = 0; col < 4; col += 1) {
      for (let row = 0; row < 5; row += 1) {
        const cx = 96 + col * 52;
        const cy = 430 + row * 52;
        ctx.beginPath();
        ctx.moveTo(cx - arm, cy);
        ctx.lineTo(cx + arm, cy);
        ctx.moveTo(cx, cy - arm);
        ctx.lineTo(cx, cy + arm);
        ctx.stroke();
      }
    }

    // 右栏：一条竖规线带三处刻度，收住右侧的空白
    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(998.5, 430);
    ctx.lineTo(998.5, 690);
    ctx.stroke();
    [430, 560, 690].forEach((y) => {
      ctx.beginPath();
      ctx.moveTo(984, y + 0.5);
      ctx.lineTo(998.5, y + 0.5);
      ctx.stroke();
    });

    // 两个实心小方块，给整张图一点重音（一个紫、一个青）
    ctx.fillStyle = "rgba(124,92,255,0.55)";
    ctx.fillRect(96, 372, 12, 12);
    ctx.fillStyle = "rgba(34,211,238,0.45)";
    ctx.fillRect(974, 706, 10, 10);

    // 右上角：两道细斜线，跟左下的三角形成对角呼应
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1.2;
    for (let i = 0; i < 2; i += 1) {
      ctx.beginPath();
      ctx.moveTo(790 + i * 34, 150);
      ctx.lineTo(1016 + i * 34, -60);
      ctx.stroke();
    }

    ctx.restore();
  }

  function paintHeader(ctx, logo, data) {
    const SIZE = 60;
    if (logo) {
      ctx.drawImage(logo, PAD, 52, SIZE, SIZE);
    } else {
      drawFallbackLogo(ctx, PAD, 52, SIZE);
    }
    const x = PAD + SIZE + 16;
    ctx.textAlign = "left";
    ctx.fillStyle = "#ffffff";
    ctx.font = "700 26px " + FONT;
    ctx.fillText("Can I get a better score?", x, 86);
    ctx.fillStyle = DIM;
    ctx.font = "400 13px " + FONT;
    ctx.fillText("osu!mania 4K 回放分析", x, 112);

    const rp = data.replay || {};
    const right = W - PAD;
    ctx.textAlign = "right";
    ctx.fillStyle = INK;
    ctx.font = "600 18px " + FONT;
    ctx.fillText(clipText(ctx, rp.player || "", 360), right, 84);
    ctx.fillStyle = DIM;
    ctx.font = "400 13px " + FONT;
    ctx.fillText(clipText(ctx, [rp.date || "", rp.mods || "无 Mod"].filter(Boolean).join("  ·  "), 400),
      right, 110);
    ctx.textAlign = "left";

    ctx.fillStyle = "rgba(255,255,255,0.09)";
    ctx.fillRect(PAD, 148, INNER, 1);
  }

  // 返回副标题的基线，后面那一段要按标题占了几行来定位
  function paintTitle(ctx, data) {
    const bm = data.beatmap || {};
    const rp = data.replay || {};
    const title = (bm.artist ? bm.artist + " - " : "") + (bm.title || "");

    ctx.textAlign = "left";
    ctx.fillStyle = "#ffffff";
    ctx.font = "700 46px " + FONT;
    const lineH = 58;
    const lines = wrapLines(ctx, title, INNER, 2);
    lines.forEach((line, i) => ctx.fillText(line, PAD, 238 + i * lineH));

    const bits = [
      "[" + (bm.difficulty || "?") + "]",
      bm.keys ? bm.keys + "K" : "",
      (bm.od !== undefined && bm.od !== null) ? "OD " + Number(bm.od).toFixed(1) : "",
      rp.mods || "无 Mod",
      rp.rate ? Number(rp.rate).toFixed(2) + "×" : "",
    ].filter(Boolean);
    const subY = 238 + (lines.length - 1) * lineH + 42;
    ctx.fillStyle = "#a8afcb";
    ctx.font = "400 17px " + FONT;
    ctx.fillText(clipText(ctx, bits.join("  ·  "), INNER), PAD, subY);
    return subY;
  }

  const JUDGES = [
    ["perfect", "MAX", "#7ef2d0"],
    ["great", "300", "#ffd479"],
    ["good", "200", "#9fe870"],
    ["ok", "100", "#8fb8ff"],
    ["meh", "50", "#c9a0ff"],
    ["miss", "MISS", "#ff6b8a"],
  ];

  // 判定分布：六个颜色的点 + 名字 + 次数，整体居中。这一段是为了填中段，
  // 同时它本身也是玩家会看的东西（MAX 占比、有没有 MISS）
  function paintJudgeRow(ctx, data, y) {
    const counts = (data.summary || {}).counts || {};
    const gap = 26;
    const items = JUDGES.map(([key, label, color]) => {
      const value = String(counts[key] === undefined ? 0 : counts[key]);
      ctx.font = "400 13px " + FONT;
      const labelW = ctx.measureText(label).width;
      ctx.font = "600 16px " + FONT;
      const valueW = ctx.measureText(value).width;
      return { label, color, value, labelW, valueW, w: 7 + 8 + labelW + 7 + valueW };
    });
    const total = items.reduce((sum, it) => sum + it.w, 0) + gap * (items.length - 1);
    let x = (W - total) / 2;

    items.forEach((it) => {
      ctx.fillStyle = it.color;
      roundRect(ctx, x, y - 8, 7, 7, 2);
      ctx.fill();
      ctx.fillStyle = DIM;
      ctx.font = "400 13px " + FONT;
      ctx.fillText(it.label, x + 15, y);
      ctx.fillStyle = "#ffffff";
      ctx.font = "600 16px " + FONT;
      ctx.fillText(it.value, x + 15 + it.labelW + 7, y);
      x += it.w + gap;
    });
  }

  function paintGrade(ctx, data, subY, statsTop) {
    const sm = data.summary || {};
    const grade = officialGrade(sm.accuracy);
    const color = GRADE_COLOR[grade] || "#7c5cff";

    const bandTop = subY + 24;
    const bandBottom = statsTop - 30;
    // 标题折两行时中段矮一截，环跟着缩一点，别把判定行挤到数值格子上
    const r = (bandBottom - bandTop) >= 440 ? 104 : 92;
    const blockH = r * 2 + 176;                    // 环 + 准确率 + 小字 + 判定行
    const cy = bandTop + Math.max(0, (bandBottom - bandTop - blockH) / 2) + r;

    // 同心圆：外圈虚线当作版式框架，让中段不至于只有一个孤零零的圆
    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.055)";
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(W / 2, cy, r + 46, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([3, 9]);
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.beginPath();
    ctx.arc(W / 2, cy, r + 92, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.arc(W / 2, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.lineWidth = 14;
    ctx.stroke();

    const ratio = GRADE_RATIO[grade] === undefined ? 0.5 : GRADE_RATIO[grade];
    ctx.beginPath();
    ctx.arc(W / 2, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * ratio);
    ctx.strokeStyle = color;
    ctx.lineWidth = 14;
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.restore();

    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#ffffff";
    ctx.font = "700 " + (grade.length > 1 ? 68 : 88) + "px " + FONT;
    ctx.fillText(grade, W / 2, cy + 3);
    ctx.textBaseline = "alphabetic";

    const accY = cy + r + 88;
    ctx.fillStyle = "#ffffff";
    ctx.font = "700 72px " + FONT;
    ctx.fillText(num(sm.accuracy, 2) + "%", W / 2, accY);

    const modeLabel = data.judgeMode === "v2" ? "v2" : data.judgeMode === "classic" ? "classic" : "v1";
    ctx.fillStyle = DIM;
    ctx.font = "400 15px " + FONT;
    ctx.fillText("准确率（" + modeLabel + "）", W / 2, accY + 32);
    ctx.textAlign = "left";

    paintJudgeRow(ctx, data, accY + 78);
  }

  function paintStats(ctx, data, danLabel, danNote, top) {
    const sm = data.summary || {};
    const cells = [
      // 段位下面再挂一行算法与读数（Roxy 8.42 / Sunny 4.21 星），别让人以为这是游戏里的段位
      { label: "段位", value: danLabel || "—", sub: danNote || "" },
      { label: "最大连击", value: sm.maxCombo === undefined ? "—" : String(sm.maxCombo) },
      { label: "整局偏移", value: signed(sm.meanDelta, 1) + " ms" },
      { label: "UR", value: num(sm.ur, 0) },
    ];
    const gap = 20;
    const cw = (INNER - gap) / 2;
    const ch = 116;

    cells.forEach((cell, i) => {
      const x = PAD + (i % 2) * (cw + gap);
      const y = top + Math.floor(i / 2) * (ch + gap);
      ctx.fillStyle = "rgba(255,255,255,0.045)";
      roundRect(ctx, x, y, cw, ch, 16);
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.08)";
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.fillStyle = DIM;
      ctx.font = "400 13px " + FONT;
      ctx.fillText(cell.label, x + 22, y + 34);
      ctx.fillStyle = "#ffffff";
      ctx.font = "600 28px " + FONT;
      ctx.fillText(clipText(ctx, cell.value, cw - 44), x + 22, y + 76);
      if (cell.sub) {
        ctx.fillStyle = DIM;
        ctx.font = "400 12px " + FONT;
        ctx.fillText(clipText(ctx, cell.sub, cw - 44), x + 22, y + 99);
      }
    });
  }

  function paintErrors(ctx, data, top) {
    const cat = data.categories || {};
    const counts = cat.counts || {};
    const total = Number(cat.total || 0);

    ctx.fillStyle = DIM;
    ctx.font = "400 13.5px " + FONT;
    ctx.fillText(total ? "失误构成 · 共 " + total + " 次" : "失误构成 · 这一局没有失误", PAD, top);

    const by = top + 14;
    const bh = 14;
    ctx.save();
    roundRect(ctx, PAD, by, INNER, bh, 7);
    ctx.clip();
    if (!total) {
      ctx.fillStyle = "rgba(126,242,208,0.55)";
      ctx.fillRect(PAD, by, INNER, bh);
    } else {
      let x = PAD;
      CATS.forEach(([key, , color]) => {
        const v = Number(counts[key] || 0);
        if (!v) return;
        const w = (v / total) * INNER;
        ctx.fillStyle = color;
        ctx.fillRect(x, by, Math.max(w, 2), bh);
        x += w;
      });
    }
    ctx.restore();

    if (!total) {
      ctx.fillStyle = DIM;
      ctx.font = "400 14px " + FONT;
      ctx.fillText("四类失误归因都没有成立 —— 这一局打得很干净", PAD, by + 54);
      return;
    }

    // 图例：竖版够宽，四项排一行；放不下就自动换行
    let x = PAD;
    let y = by + 52;
    CATS.forEach(([key, label, color]) => {
      const v = Number(counts[key] || 0);
      const pct = total ? Math.round((v / total) * 100) : 0;
      const text = v + " · " + pct + "%";
      ctx.font = "400 14px " + FONT;
      const itemW = 16 + ctx.measureText(label).width + 8 + ctx.measureText(text).width;
      if (x + itemW > W - PAD && x > PAD) {
        x = PAD;
        y += 30;
      }
      ctx.fillStyle = color;
      roundRect(ctx, x, y - 11, 10, 10, 3);
      ctx.fill();
      ctx.fillStyle = INK;
      ctx.fillText(label, x + 16, y);
      ctx.fillStyle = DIM;
      ctx.fillText(text, x + 24 + ctx.measureText(label).width, y);
      x += itemW + 40;
    });
  }

  function paintFooter(ctx, data) {
    ctx.fillStyle = "rgba(255,255,255,0.09)";
    ctx.fillRect(PAD, H - 104, INNER, 1);

    ctx.textAlign = "left";
    ctx.fillStyle = DIM;
    ctx.font = "400 12.5px " + FONT;
    ctx.fillText("osumania4k_replay_analyser by [Crz]Renjie Lan" +
      (data.version ? "  ·  v" + data.version : ""), PAD, H - 58);

    ctx.textAlign = "right";
    ctx.fillText("github.com/SWt0619/osumania4k_replay_analyser", W - PAD, H - 58);
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

    const statsTop = 800;
    paintBackground(ctx);
    paintDecor(ctx);
    const logo = await loadLogo();
    paintHeader(ctx, logo, data);
    const subY = paintTitle(ctx, data);
    paintGrade(ctx, data, subY, statsTop);
    paintStats(ctx, data, options.danLabel, options.danNote, statsTop);
    paintErrors(ctx, data, statsTop + 252 + 56);
    paintFooter(ctx, data);
    return canvas;
  }

  function fileName(data) {
    const bm = (data && data.beatmap) || {};
    const raw = "成绩卡_" + [bm.artist, bm.title, bm.difficulty].filter(Boolean).join("_");
    return raw.replace(/[\\/:*?"<>|]/g, "_").slice(0, 120) + ".png";
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

  window.ShareCard = { render, save, W, H };
})();
