/* 纯渲染层：把分析数据写进 DOM。不做网络、不做状态管理。 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const CATS = {
    ability: { label: "超出能力范围", short: "能力", color: "var(--cat-ability)", cssVar: "--cat-ability", hex: "#ff9f6e" },
    rhythm: { label: "节奏感弱", short: "节奏", color: "var(--cat-rhythm)", cssVar: "--cat-rhythm", hex: "#6ee7ff" },
    distraction: { label: "分神", short: "分神", color: "var(--cat-distraction)", cssVar: "--cat-distraction", hex: "#c792ff" },
    // 第四类：三类签名都不成立的孤立小失误。放在最后 —— 图例顺序即"从最该管到最不用管"
    slip: { label: "随机失手", short: "失手", color: "var(--cat-slip)", cssVar: "--cat-slip", hex: "#8b93b5" },
  };

  // 画 canvas（甜甜圈）用的实色：优先读当前主题的 CSS 变量，
  // 读不到（测试桩 / 老浏览器）才退回写死的默认色。
  function catHex(key) {
    const c = CATS[key];
    if (!c) return "#999";
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(c.cssVar).trim();
      if (v) return v;
    } catch (e) { /* ignore */ }
    return c.hex;
  }

  // canvas 模块的配色。图表和播放器只能在绘制那一刻读 CSS 变量，所以这里每次现读、不缓存。
  // 在线页（main.js）与单文件报告（offline.js）共用这一份：两边各写一套键名时，
  // 改了一边忘了另一边，表现就是"在线页颜色对、导出的报告颜色不对"。
  function readTheme() {
    let cs = null;
    try { cs = getComputedStyle(document.documentElement); } catch (e) { cs = null; }
    const pick = (name, fallback) => {
      if (!cs) return fallback;
      try {
        return (cs.getPropertyValue(name) || "").trim() || fallback;
      } catch (e) {
        return fallback;
      }
    };
    return {
      text: pick("--text", "#eceffd"),
      dim: pick("--dim", "#a8afcb"),
      // 网格线跟描边走，不写死白色：面板底色一旦调浅，白网格会直接隐形
      grid: pick("--stroke", "rgba(255,255,255,0.10)"),
      accent: pick("--accent", "#7c5cff"),
      accent2: pick("--accent-2", "#22d3ee"),
      warn: pick("--warn", "#ffd479"),
      ability: pick("--cat-ability", "#ff9f6e"),
      rhythm: pick("--cat-rhythm", "#6ee7ff"),
      distraction: pick("--cat-distraction", "#c792ff"),
      miss: pick("--j-miss", "#ff6b8a"),
      bg: "transparent",
    };
  }

  // 时间轴上「此刻」的 v1 准确率：取当前时间所在那一小段的 accV1（老数据退回 acc）。
  // 主页面与单文件报告共用一份，免得两边各算一套、显示两个数。
  function accAt(timeline, ms) {
    const bk = (timeline && timeline.buckets) || [];
    if (!bk.length) return null;
    let lo = 0;
    let hi = bk.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (bk[mid].t <= ms) {
        ans = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    if (ans < 0) ans = 0;
    const b = bk[ans];
    const v = (b.accV1 !== undefined && b.accV1 !== null) ? b.accV1 : b.acc;
    return typeof v === "number" ? v : null;
  }

  // ---------------------------------------------------------------- 格式化
  function pad(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function fmtTime(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return "—";
    const total = Math.max(0, Math.round(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m + ":" + pad(s);
  }

  function fmtSigned(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const d = digits === undefined ? 1 : digits;
    return (v >= 0 ? "+" : "") + Number(v).toFixed(d);
  }

  function fmtInt(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US");
  }

  function fmtPct(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(digits === undefined ? 2 : digits) + "%";
  }

  function clearNode(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function el(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ---------------------------------------------------------------- 顶栏 / 空状态
  function renderTrack(data) {
    const bm = data.beatmap || {};
    const rp = data.replay || {};
    $("trackTitle").textContent = bm.artist + " - " + bm.title + "  [" + bm.difficulty + "]";
    $("trackSub").textContent = [
      bm.keys + "K",
      "OD" + bm.od,
      rp.mods || "无 Mod",
      rp.player ? "by " + rp.player : "",
      rp.date || "",
    ]
      .filter(Boolean)
      .join("  ·  ");
    document.title = bm.title + " [" + bm.difficulty + "] · 回放分析";
  }

  function renderLibrary(info) {
    const chip = $("libChip");
    const t = $("libChipText");
    if (!info || !info.count) {
      chip.classList.add("off");
      chip.classList.remove("ok");
      t.textContent = "未找到曲库";
      return;
    }
    chip.classList.add("ok");
    chip.classList.remove("off");
    t.textContent = info.count + " 张谱面";
  }

  // ---------------------------------------------------------------- 总评
  // 「如果解决了容易解决的失误」的预期成绩。数据来自 analyze 的 projection：
  // 三类成因各修 10%（至少 1 个、优先挑损失最小的），修好按 300 计，
  // 分数用官方 ScoreV1 公式算（不是比例拟合）。口径写进 title，别让人自己猜。
  function renderProjection(data) {
    const box = $("heroProjection");
    if (!box) return;
    const pj = data && data.projection;
    if (!pj || !pj.fixable) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    clearNode(box);
    box.appendChild(
      document.createTextNode("把 " + pj.fixable + " 个最容易改善的失误修好 → v1 ")
    );
    const b1 = el("b", null, pj.projected.accuracy.toFixed(2) + "%");
    const b2 = el("b", null, fmtInt(pj.projected.score));
    box.appendChild(b1);
    box.appendChild(document.createTextNode(" · "));
    box.appendChild(b2);
    box.appendChild(document.createTextNode(" 分"));
    const g = el("span", "gain", "（+" + pj.gain.accuracy.toFixed(2) + "% · +" + fmtInt(pj.gain.score) + "）");
    box.appendChild(g);
    box.title =
      "现在：" + pj.current.accuracy.toFixed(2) + "% / " + fmtInt(pj.current.score) + " 分（最大连击 " + pj.current.maxCombo + "）\n" +
      "预期：" + pj.projected.accuracy.toFixed(2) + "% / " + fmtInt(pj.projected.score) + " 分（最大连击 " + pj.projected.maxCombo + "）\n" +
      "口径：三类成因各只修 10%（至少 1 个），优先挑损失最小的（200 比漏接好修）；修好按 300 计，不给满分。\n" +
      (pj.note || "");
  }

  // 段位估算：MSD 那条链走 MinaCalc，段位这张卡片改用 Sunny 星级查表。
  // 估算器在前端跑（ES 模块），失败就保留 MinaCalc 的兜底文案，不影响页面其他部分。
  // 返回值给调用方用：相似谱面要按段位找，得等这里算完（离线报告没有估算器，返回 null）。
  async function applySunnyDan(data) {
    const cell = $("msdDan");
    const note = $("msdDanNote");
    if (!cell || !note || !window.Estimator || !data || !data.session) return null;
    const rate = Number((data.replay && data.replay.rate) || 1) || 1;
    const notes = Number(data.beatmap && data.beatmap.notes) || 0;
    let res;
    try {
      res = await window.Estimator.estimate(data.session, rate, { fast: notes > 30000 });
    } catch (e) {
      return null;
    }
    if (!res || !res.ok) return null;
    cell.textContent = res.label;
    const speed = rate === 1 ? "原速" : (rate > 1 ? "DT " : "HT ") + rate.toFixed(2) + "×";
    const parts = [];
    const methodName = res.method === "roxy" ? "Roxy" : (res.method === "azusa" ? "Azusa" : "Sunny");
    if (res.method === "roxy") {
      parts.push("Roxy " + Number(res.numeric).toFixed(2));
    } else if (res.method === "azusa") {
      parts.push("Azusa " + Number(res.numeric).toFixed(2));
    } else {
      parts.push("Sunny " + Number(res.rcStar).toFixed(2) + " 星" + (res.fast ? "（大图快速估算）" : ""));
    }
    if (res.lnStar > 0) parts.push("LN " + Number(res.lnStar).toFixed(2));
    note.textContent = parts.join(" · ") + " · " + speed;
    note.title =
      "段位来源：" + methodName + "。\n" +
      "本次按键占比 " + (res.lnRatio * 100).toFixed(1) + "%，倍率 " + speed + "。\n" +
      "Sunny 给的是星级，Azusa/Roxy 给的是 RC 数值难度，两者不是同一量纲；" +
      "左边的 MSD 来自 MinaCalc，只看谱面本身，不随倍率变化。";
    return res;
  }

  // 键型构成：把块级粗分类按物件数汇总成一条堆叠条 + 图例。
  // 四大类的颜色固定，和「分段」里的颜色一致，方便对上号。
  const PATTERN_COLORS = { stack: "#f472b6", cut: "#7c5cff", spam: "#22d3ee", tech: "#fbbf24", ln: "#34d399", calm: "#94a3b8" };

  function mapClassText(info) {
    if (!info || !info.label) return "";
    return info.key === "hybrid"
      ? "整图类型：" + info.label + "（长条占 " + info.lnShare + "%，常规键型占 " + (100 - info.lnShare).toFixed(1) + "%）"
      : "整图类型：" + info.label + (info.key === "ln" ? "（长条占 " + info.lnShare + "%）" : "");
  }

  function renderPatternShare(data) {
    const bar = $("patternBar");
    const legend = $("patternLegend");
    if (!bar || !legend) return;
    clearNode(bar);
    clearNode(legend);
    const items = (data && data.patternShare) || [];
    if (!items.length) {
      const card = $("patternCard");
      if (card) card.hidden = true;
      return;
    }
    // 整图大类：LN 占压倒性就是 LN 图，RC 与 LN 都不占压倒性就是 Hybrid
    const mc = $("mapClass");
    if (mc) {
      const text = mapClassText((data && data.mapClass) || null);
      mc.textContent = text;
      mc.hidden = !text;
    }
    const total = items.reduce((s, x) => s + Number(x.share || 0), 0) || 1;
    const detail = new Map(((data && data.patternDetail) || []).map((x) => [x.key, x]));
    items.forEach((it) => {
      const color = PATTERN_COLORS[it.key] || "var(--accent)";
      const seg = document.createElement("span");
      seg.style.width = (Number(it.share || 0) / total) * 100 + "%";
      seg.style.background = color;
      seg.title = it.label + " " + it.share + "%";
      bar.appendChild(seg);

      const li = document.createElement("li");
      const dot = document.createElement("i");
      dot.style.background = color;
      const name = document.createElement("b");
      name.textContent = it.label;
      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(document.createTextNode(" " + it.share + "%"));

      // 大类下面再列细分键型（只取前两个，够看且不挤）。
      // LN 段不列：它下面永远是"LN 100%"，和左边的类名重复。
      const d = it.key === "ln" ? null : detail.get(it.key);
      if (d && d.items && d.items.length) {
        const sub = document.createElement("em");
        sub.className = "pattern-sub";
        sub.textContent = d.items
          .slice(0, 2)
          .map((x) => x.label + " " + x.share + "%")
          .join(" · ");
        li.appendChild(sub);
      }
      legend.appendChild(li);
    });
  }

  // 谱面难度页：MSD 数字 + 段位换算 + 段位标尺
  // 段位不在这里算：它要等异步的 Sunny 估算（相似谱面还得靠它），
  // 由调用方先画 MSD、再调 applySunnyDan 覆写段位格。
  function renderMsd(data) {
    const box = $("msdOverall");
    if (!box) return;
    const m = (data && data.msd) || {};
    if (!m.available) {
      box.textContent = "—";
      $("msdDan").textContent = "—";
      $("msdDanNote").textContent = m.error || "MinaCalc 不可用";
      $("msdMeta").textContent = "";
      $("msdScale").innerHTML = "";
      return;
    }
    box.textContent = m.overall.toFixed(2);
    const dn = m.dan || {};
    $("msdDan").textContent = dn.label || "—";
    $("msdDanNote").textContent = dn.tier === "extra"
      ? "超出 10 段（REFORM EXTRA 层）"
      : dn.next ? "距「" + dn.next + "」还差 " + dn.gap.toFixed(2) + " MSD" : "参照 REFORM 段位标尺";
    const meta = $("msdMeta");
    clearNode(meta);
    // 只写「行数 / 达成目标」：不写"原速"，因为回放可能自带 DT/HT，
    // 写"原速"会让人以为这个数已经按倍率换算过。
    meta.appendChild(document.createTextNode("谱面行数 " + m.rows + " 行 / Etterna AA"));
    meta.title =
      "行数 " + m.rows + "：喂给 MinaCalc 的谱面行数（同一时刻的多押合并成一行）\n" +
      "Etterna AA：难度按「打到 93%（Etterna 的 AA）」这个目标算——目标定得越高，MSD 越大\n" +
      "这个数只看谱面本身，不会因为回放里开了 DT/HT 而变化";

    // 段位标尺：每行一个锚点，接近当前 MSD 的高亮
    const scale = $("msdScale");
    clearNode(scale);
    // 段位标尺只显示 4 段以上：低段位区间对打图的人没什么参考价值
    const anchors = (m.scale || []).filter((a) => Number(a.dan) >= 4);
    const cur = m.overall;
    anchors.forEach((a) => {
      const row = el("div", "msd-scale-row");
      const near = Math.abs(a.msd - cur) <= 0.6;
      if (near) row.classList.add("is-here");
      row.appendChild(el("span", null, a.label));
      const bar = el("div", "msd-scale-bar");
      const fill = document.createElement("i");
      fill.style.width = Math.min(100, (a.msd / (anchors[anchors.length - 1].msd || 1)) * 100) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      row.appendChild(el("em", null, a.msd.toFixed(1)));
      row.title = a.label + " · MSD " + a.msd.toFixed(2);
      scale.appendChild(row);
    });
  }

  // V1 视图没有后端算好的 ScoreV1 字段：优先用投影里的“当前分数”，
  // 投影缺失时退回回放头记录的分数。两种都不是时返回 null，宁可不显示也不写 0。
  function scoreV1Of(data) {
    const pj = data && data.projection;
    const cur = pj && pj.current && pj.current.score;
    if (Number.isFinite(cur) && cur > 0) return cur;
    const raw = Number(data && data.replay && data.replay.score);
    return Number.isFinite(raw) && raw > 0 ? raw : null;
  }

  function renderHero(data) {
    const ev = data.evaluation || {};
    const sm = data.summary || {};
    const card = $("heroCard");
    card.className = "card glass hero grade-" + (ev.grade || "B").replace("+", "");

    $("gradeLetter").textContent = ev.grade || "–";
    $("heroHeadline").textContent = ev.headline || "";
    $("heroDesc").textContent = ev.gradeDesc
      ? ev.gradeDesc + " · 准确率(" + (data.judgeMode === "classic" ? "classic" : data.judgeMode === "v2" ? "v2" : "v1") + ") "
        + fmtPct(sm.accuracy) + " · UR " + Number(sm.ur || 0).toFixed(0)
      : "";

    // 圆环：按**评级**给角度，不再拿准确率硬映射 ——
    // 99.9% 和 98.5% 在圆环上几乎看不出差别，评级才是有信息量的那一层。
    // A+ 及以上画满 360°，从 A 开始逐级递减。
    const RING_RATIO = { S: 1, "A+": 1, A: 0.84, B: 0.68, C: 0.52, D: 0.36 };
    const circumference = 2 * Math.PI * 50;
    const graded = RING_RATIO[ev.grade];
    const ratio = graded !== undefined ? graded : Math.max(0.02, Math.min(1, (sm.accuracy || 0) / 100));
    const ring = $("ringFg");
    ring.style.strokeDasharray = String(circumference);
    ring.style.strokeDashoffset = String(circumference);
    requestAnimationFrame(() => {
      ring.style.strokeDashoffset = String(circumference * (1 - ratio));
    });

    renderProjection(data);

    const badges = $("heroBadges");
    clearNode(badges);

    // V1 挂 ScoreV1，V2 挂 ScoreV2；两者只出现一个。
    // 同一个准确率被标成两种口径，正是之前 V1/V2 被混用的原因。
    const v2 = sm.scoreV2;
    if ((data.judgeMode === "v2" || data.judgeMode === "classic") && v2 !== undefined && v2 !== null) {
      const wrap = el("div", "badge badge-v2");
      wrap.appendChild(el("span", null, data.judgeMode === "classic" ? "Score V2 · Classic" : "Score V2"));
      wrap.appendChild(el("strong", null, fmtInt(v2)));
      const g2 = el("em", "badge-grade", String(sm.gradeV2 || ""));
      wrap.appendChild(g2);
      wrap.title =
        "Score V2 = 20 万连击分 + 80 万准度分（osu! 现行计分）\n" +
        "准度分 = 800,000 × 准确率^(2+2×准确率)，准确率 = Σ判定分 / (305 × 判定次数)\n" +
        "判定分 MAX=305 / 300=300 / 200=200 / 100=100 / 50=50 / MISS=0；长按算头 + 尾两次判定\n" +
        "连击分 = 200,000 × Σ(判定值 × log4 连击倍率) / Σ(300 × log4 连击倍率)";
      badges.appendChild(wrap);
    } else if (data.judgeMode !== "v2") {
      const v1 = scoreV1Of(data);
      if (v1 !== null) {
        const wrap = el("div", "badge badge-v1");
        wrap.appendChild(el("span", null, "Score V1"));
        wrap.appendChild(el("strong", null, fmtInt(v1)));
        wrap.title = "Score V1 = 回放里记录的实际分数（长条只算一次判定）";
        badges.appendChild(wrap);
      }
    }

    (ev.badges || []).forEach((b) => {
      const wrap = el("div", "badge");
      wrap.appendChild(el("span", null, b.label));
      wrap.appendChild(el("strong", null, b.value));
      badges.appendChild(wrap);
    });

    const paras = $("heroParas");
    clearNode(paras);
    (ev.paragraphs || []).forEach((p) => paras.appendChild(el("p", null, p)));

    const st = $("listStrengths");
    const wk = $("listWeaknesses");
    clearNode(st);
    clearNode(wk);
    (ev.strengths && ev.strengths.length ? ev.strengths : ["—"]).forEach((s) => st.appendChild(el("li", null, s)));
    (ev.weaknesses && ev.weaknesses.length ? ev.weaknesses : ["—"]).forEach((s) => wk.appendChild(el("li", null, s)));

    const advice = $("adviceList");
    clearNode(advice);
    (ev.advice || []).forEach((a) => {
      const li = el("li");
      const h = el("h4", null, a.title || "");
      if (a.tag && CATS[a.tag]) {
        const tag = el("span", "adv-tag", "针对「" + CATS[a.tag].short + "」");
        h.appendChild(tag);
      }
      li.appendChild(h);
      li.appendChild(el("p", null, a.body || ""));
      advice.appendChild(li);
    });
  }

  // ---------------------------------------------------------------- 失误成因（甜甜圈）
  function drawDonut(canvas, counts, total) {
    const dpr = window.devicePixelRatio || 1;
    const css = 180;
    canvas.width = css * dpr;
    canvas.height = css * dpr;
    canvas.style.width = css + "px";
    canvas.style.height = css + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, css, css);

    const cx = css / 2;
    const cy = css / 2;
    const r = 68;
    const lineWidth = 20;
    let start = -Math.PI / 2;
    const sum = Math.max(1, total);

    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = lineWidth;
    ctx.stroke();

    Object.keys(CATS).forEach((k) => {
      const v = counts[k] || 0;
      if (!v) return;
      const angle = (v / sum) * Math.PI * 2;
      // 相邻段之间留一点缝，但缝隙不能超过这一段的四分之一：
      // distraction 只有 2/482 时角度约 0.026，固定减 0.02 会让终点跑到起点前面，
      // canvas 会按顺时针补一整圈 —— 表现就是"一小段紫色盖住整张图"。
      const pad = Math.min(0.02, angle * 0.2);
      ctx.beginPath();
      ctx.arc(cx, cy, r, start + pad, start + angle - pad);
      ctx.strokeStyle = catHex(k);
      ctx.lineWidth = lineWidth;
      ctx.lineCap = "butt";
      ctx.stroke();
      start += angle;
    });
  }

  function renderCategories(data) {
    const cat = data.categories || {};
    const counts = cat.counts || {};
    const total = cat.total || 0;
    $("catTotal").textContent = total;
    drawDonut($("donutCat"), counts, total);

    const legend = $("catLegend");
    clearNode(legend);
    Object.keys(CATS).forEach((k) => {
      const v = counts[k] || 0;
      const pct = total ? Math.round((v / total) * 100) : 0;
      const li = el("li");
      const sw = el("i", "sw");
      // DOM 元素直接用 var()，主题切换时自动跟着变；canvas 才需要 catHex()
      sw.style.background = CATS[k].color;
      li.appendChild(sw);
      li.appendChild(el("span", "lg-name", CATS[k].label));
      li.appendChild(el("span", "lg-val", v + " · " + pct + "%"));
      const conf = (cat.avgConfidence || {})[k];
      if (conf) li.appendChild(el("span", "lg-conf", "可信 " + Math.round(conf * 100) + "%"));
      legend.appendChild(li);
    });

    const strip = $("catStats");
    clearNode(strip);
    const prof = cat.profile || {};
    const items = [
      { b: fmtSigned(prof.global_bias, 1) + "ms", s: "整局偏移" },
      { b: Number(prof.global_ur || 0).toFixed(0), s: "UR" },
      { b: prof.ceiling ? Number(prof.ceiling).toFixed(1) : "—", s: "稳定上限 NPS" },
    ];
    items.forEach((it) => {
      const box = el("div", "st");
      box.appendChild(el("b", null, it.b));
      box.appendChild(el("span", null, it.s));
      strip.appendChild(box);
    });
  }

  // ---------------------------------------------------------------- 元信息
  function renderMeta(data) {
    const bm = data.beatmap || {};
    const rp = data.replay || {};
    const sm = data.summary || {};
    const kv = $("metaKv");
    clearNode(kv);
    const rows = [
      ["谱面", bm.artist + " - " + bm.title],
      ["难度", bm.difficulty + "（" + bm.keys + "K）"],
      ["OD / HP", Number(bm.od).toFixed(1) + " / " + Number(bm.hp || 0).toFixed(1)],
      ["BPM", bm.bpmRange && bm.bpmRange[0] !== bm.bpmRange[1] ? bm.bpmRange[0] + "–" + bm.bpmRange[1] : String(bm.bpm)],
      ["时长", fmtTime(bm.duration)],
      ["物件 / 长条", bm.notes + " / " + bm.holds],
      ["平均 / 峰值 NPS", bm.nps + " / " + bm.peakNps],
      ["Mod", rp.mods || "无"],
    ];
    // 只展示当前口径的统计：V1 不出现任何 V2 字段，V2 也不再并排显示 V1，
    // 避免同一个准确率被标成两种口径。
    if (data.judgeMode === "v2" || data.judgeMode === "classic") {
      rows.push(
        ["准确率（" + (data.judgeMode === "classic" ? "Classic" : "ScoreV2") + " 口径）", fmtPct(sm.accuracy)],
        ["Score V2（stable）", fmtInt(sm.scoreV2) + (sm.gradeV2 ? "　评级 " + sm.gradeV2 : "")],
        ["Score V2（lazer 配比）", fmtInt(sm.scoreV2Lazer)]
      );
    } else {
      rows.push(["准确率（ScoreV1 口径）", fmtPct(sm.accuracy)]);
      const v1 = scoreV1Of(data);
      if (v1 !== null) rows.push(["Score V1", fmtInt(v1)]);
    }
    rows.push(
      ["最大连击（重算）", String(sm.maxCombo)],
      ["空按", String(sm.ghosts || 0)],
      ["分析耗时", (data.elapsedMs || 0) + " ms"]
    );
    // Random 会打乱列序：把推断结果留在面板里，别只在弹一下的提示里出现过
    if (rp.columnOrder && rp.columnOrder.length) {
      const at = rows.findIndex(([k]) => k === "Mod");
      rows.splice(at + 1, 0, ["列序（Random）",
        rp.columnOrder.map((pressCol, noteCol) => noteCol + "→" + pressCol).join("、")]);
    }
    rows.forEach(([k, v]) => {
      const d = el("div");
      d.appendChild(el("dt", null, k));
      d.appendChild(el("dd", null, v));
      kv.appendChild(d);
    });
    // 这里刻意不显示「解析自检：重算结果与回放头有差异」：
    // 回放头按 ScoreV1 记长按（按住的 tick 数进连击），本工具按 ScoreV2 只记头尾两次，
    // 用 V2 打出来的回放必然对不上 —— 那是口径差异，不是解析出错。
    // 比对数据本身仍留在 replay.verified 里，排查时能看。
  }

  // ---------------------------------------------------------------- 相似谱面
  // 数据来自后端 recommend：段位与键型构成都接近的几张图，带 sayobot 的下载地址。
  // 段位是前端算出来的，所以这里是「拿到 items 再画」，不自己发请求。
  function renderSimilar(items) {
    const card = $("similarCard");
    const ul = $("similarList");
    if (!card || !ul) return;
    const list = Array.isArray(items) ? items : [];
    clearNode(ul);
    if (!list.length) {
      card.hidden = true;
      return;
    }

    list.forEach((it) => {
      const li = el("li", "similar-item");
      const main = el("div", "s-main");
      const title = el("a", "s-title",
        (it.artist || "") + " - " + (it.title || "") + " [" + (it.version || "") + "]");
      title.href = it.pageUrl || "#";
      title.target = "_blank";
      title.rel = "noreferrer noopener";
      main.appendChild(title);

      // 键型中文名由后端给（和「键型构成」卡片同一张表），前端不再维护第二份
      const cat = it.label || it.dominant || "";
      const bits = [];
      if (cat) bits.push(cat + (it.dominantShare ? " " + Math.round(it.dominantShare * 100) + "%" : ""));
      if (it.dan) {
        const gap = Number(it.danGap);
        bits.push(it.dan + (gap > 0 ? "（差 " + gap + " 档）" : "（同档）"));
      }
      // BPM 差：同一首曲子的倍速版 dan 可能完全对上，但手感差很远，这里直接摊开
      if (it.bpm && Number(it.bpm) > 0) {
        const bg = it.bpmGap;
        bits.push("BPM " + Math.round(it.bpm) + (bg === null || bg === undefined ? "" : "（" + (bg > 0 ? "+" : "") + bg + "%）"));
      }
      if (it.similarity) bits.push("相似度 " + Math.round(it.similarity * 100) + "%");
      const line = el("div", "s-meta", bits.join(" · "));
      // MSD 只作参考、不参与排序，放悬浮说明里，不占正文
      line.title = [
        it.msd ? "这张图 MSD " + Number(it.msd).toFixed(1) : "",
        it.shareLabel ? "键型构成 " + it.shareLabel : "",
        it.relaxed ? "候选不够，已放宽段位门槛" : "",
        it.bpmTerm !== null && it.bpmTerm !== undefined ? "BPM 接近度 " + Math.round(it.bpmTerm * 100) + "%（计入排序）" : "",
      ].filter(Boolean).join("\n");
      main.appendChild(line);
      li.appendChild(main);

      const dl = el("a", "s-dl", "下载 .osz");
      dl.href = it.downloadUrl || "#";
      dl.target = "_blank";
      dl.rel = "noreferrer noopener";
      dl.title = "从 sayobot 下载这套谱面（含全部难度）";
      li.appendChild(dl);
      ul.appendChild(li);
    });
    card.hidden = false;
  }

  // ---------------------------------------------------------------- 分段条
  function renderSections(data) {
    const body = $("sectionsBody");
    clearNode(body);
    const names = ["前段", "中段", "后段"];
    (data.sections || []).forEach((s) => {
      const row = el("div", "bar-row");
      row.appendChild(el("span", null, names[s.index] || "第" + (s.index + 1) + "段"));
      const track = el("div", "bar-track");
      const fill = el("div", "bar-fill" + (s.acc < 90 ? " warn" : ""));
      fill.style.width = Math.max(2, s.acc) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("span", "bar-val", fmtPct(s.acc, 1) + " · UR " + (s.ur === null ? "—" : Number(s.ur).toFixed(0))));
      body.appendChild(row);
    });
    if (!(data.sections || []).length) body.appendChild(el("p", "muted", "暂无分段数据"));
  }

  // ---------------------------------------------------------------- 判定分布
  function renderJudgeBars(data) {
    const body = $("judgeBars");
    clearNode(body);
    const counts = (data.summary || {}).counts || {};
    const order = [
      ["perfect", "MAX", "--j-max"],
      ["great", "300", "--j-300"],
      ["good", "200", "--j-200"],
      ["ok", "100", "--j-100"],
      ["meh", "50", "--j-50"],
      ["miss", "MISS", "--j-miss"],
    ];
    const max = Math.max.apply(
      null,
      order.map((o) => counts[o[0]] || 0).concat([1])
    );
    order.forEach(([key, label, colorVar]) => {
      const v = counts[key] || 0;
      const row = el("div", "bar-row");
      row.appendChild(el("span", null, label));
      const track = el("div", "bar-track");
      const fill = el("div", "bar-fill is-judge");
      fill.style.width = Math.max(1, (v / max) * 100) + "%";
      // 颜色交给 CSS：判定色由变量传入，样式只负责怎么涂
      fill.style.setProperty("--bar-color", "var(" + colorVar + ")");
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("span", "bar-val", String(v)));
      body.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- 原始统计表
  function renderRawTable(data) {
    const table = $("rawTable");
    clearNode(table);
    const head = el("thead");
    const hr = el("tr");
    ["列", "物件", "失误", "失误率", "平均偏差", "|偏差|中位", "标准差"].forEach((h) => hr.appendChild(el("th", null, h)));
    head.appendChild(hr);
    table.appendChild(head);
    const body = el("tbody");
    (data.columnStats || []).forEach((c) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, String(c.column + 1)));
      tr.appendChild(el("td", null, String(c.notes)));
      tr.appendChild(el("td", null, String(c.errors)));
      tr.appendChild(el("td", null, fmtPct((c.errorRate || 0) * 100, 1)));
      tr.appendChild(el("td", null, c.meanDelta === null ? "—" : fmtSigned(c.meanDelta, 1) + "ms"));
      tr.appendChild(el("td", null, c.absMedian === null ? "—" : Number(c.absMedian).toFixed(1) + "ms"));
      tr.appendChild(el("td", null, c.stdev === null ? "—" : Number(c.stdev).toFixed(1)));
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  // 判定码 → 样式类（0/1 共用金色，4=50 用紫色，5=MISS 用红色）
  function judgementClass(err) {
    const map = { perfect: 0, great: 1, good: 2, ok: 3, meh: 4, miss: 5 };
    const cls = map[err.judgement];
    return cls === undefined ? 5 : cls;
  }

  // ---------------------------------------------------------------- 失误清单
  // 一条失误的"副文本"（偏差 / 键型 / 密度 / 列 / 可信度）。
  // 抽出来是因为「此刻」卡片选中失误时要显示**和清单一样**的原文，两边各写一份迟早会分叉。
  function errorBits(err) {
    const bits = [];
    if (err.delta !== null && err.delta !== undefined) bits.push("偏差 " + fmtSigned(err.delta, 0) + "ms");
    if (err.pattern) bits.push(err.pattern);
    if (err.density) bits.push(Number(err.density).toFixed(1) + " NPS");
    bits.push("第 " + (err.column + 1) + " 列");
    if (err.confidence) bits.push("判定可信度 " + Math.round(err.confidence * 100) + "%");
    // 落在这段"同类键型耐力下滑"窗口里的失误，带一个短标记；整句说明放不下，悬停看全文
    if (err.stamina) bits.push("耐力 " + err.stamina);
    return bits.join(" · ");
  }

  function errorItem(err, onSeek) {
    const li = el("div", "error-item");
    li.dataset.t = String(err.t);
    // 同一时刻可能有多个物件（多押），列号必须一起记，否则点哪个都对不上
    li.dataset.c = String(err.column);
    li.dataset.key = Math.round(err.t) + ":" + err.column;
    li.appendChild(el("span", "e-time", fmtTime(err.t) + "." + String(Math.round(err.t % 1000)).padStart(3, "0")));

    li.appendChild(el("span", "e-judge j-" + judgementClass(err), err.judgementLabel || err.judgement));

    const main = el("div", "e-main");
    const reason = (err.reasons && err.reasons[0]) || "没有足够证据判断原因";
    main.appendChild(el("div", "e-reason", reason));
    main.appendChild(el("div", "e-sub", errorBits(err)));
    li.appendChild(main);

    if (err.stamina) li.title = "后段耐力下滑：" + err.stamina;

    const cat = err.category || "distraction";
    const chip = el("span", "e-cat cat-" + cat);
    chip.appendChild(el("i"));
    chip.appendChild(document.createTextNode((CATS[cat] || {}).label || cat));
    if (err.secondaryLabel) chip.title = "也可能与「" + err.secondaryLabel + "」有关";
    li.appendChild(chip);

    li.addEventListener("click", () => onSeek(err.t, err));
    return li;
  }

  // 从回放里点中某个失误物件时，把失误清单里对应的那一条闪一下（不在当前页也能定位）
  function flashError(t, column) {
    const ul = $("errorList");
    if (!ul) return;
    const key = Math.round(t) + ":" + column;
    const item = ul.querySelector('[data-key="' + key + '"]');
    if (!item) return;
    ul.querySelectorAll(".is-flash").forEach((n) => n.classList.remove("is-flash"));
    item.classList.add("is-flash");
    if (typeof item.scrollIntoView === "function") {
      item.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    setTimeout(() => item.classList.remove("is-flash"), 1600);
  }

  // 时间分段标题：1000+ 条失误时靠它定位，点一下直接跳到那一段开头
  function errorGroup(bucket, count, onSeek) {
    const li = el("div", "error-group");
    const t0 = bucket * 30000;
    li.appendChild(el("span", "eg-time", fmtTime(t0) + " – " + fmtTime(t0 + 30000)));
    if (count) li.appendChild(el("em", "eg-count", count + " 条"));
    if (onSeek) li.addEventListener("click", () => onSeek(t0, null));
    return li;
  }

  function renderErrors(list, opts) {
    const ul = $("errorList");
    clearNode(ul);
    const empty = $("errorEmpty");
    empty.hidden = list.length > 0;
    const counts = (opts && opts.bucketCounts) || null;
    let last = null;
    list.forEach((e) => {
      const b = Math.floor(e.t / 30000);
      if (b !== last) {
        last = b;
        ul.appendChild(errorGroup(b, counts ? counts[b] : 0, opts && opts.onSeek));
      }
      ul.appendChild(errorItem(e, opts.onSeek));
    });
  }

  // ---------------------------------------------------------------- Toast / Loading
  let toastTimer = null;
  function toast(msg, bad) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("bad", !!bad);
    t.hidden = false;
    requestAnimationFrame(() => t.classList.add("show"));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => (t.hidden = true), 260);
    }, bad ? 4200 : 2400);
  }

  // ---------------------------------------------------------------- 关于
  // 版本号只有一个来源：在线页是 /api/health，离线报告是 REPORT_DATA.version。
  // 拿不到就保持 "—"，不猜。
  function renderAbout(version) {
    const box = $("aboutVersion");
    if (!box) return;
    const text = String(version || "").trim();
    if (!text) return;
    box.textContent = /^\d/.test(text) ? "v" + text : text;
  }

  function loading(on, text) {
    const box = $("loading");
    if (text) $("loadingText").textContent = text;
    box.hidden = !on;
  }

  window.UI = {
    $,
    el,
    clearNode,
    fmtTime,
    fmtSigned,
    fmtPct,
    fmtInt,
    CATS,
    readTheme,
    accAt,
    renderTrack,
    renderLibrary,
    renderHero,
    renderMsd,
    applySunnyDan,
    renderPatternShare,
    mapClassText,
    renderCategories,
    renderMeta,
    renderSimilar,
    renderAbout,
    renderSections,
    renderJudgeBars,
    renderRawTable,
    renderErrors,
    errorBits,
    flashError,
    toast,
    loading,
  };
})();
