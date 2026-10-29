/* 成长与随机找图：段位评估、目标进度、推图筛选、镜像测速。 */
(function (App) {
  "use strict";

  const { $ } = window.UI;

  // ---------------------------------------------------------------- 成长
  // 折算规则、段位换算都在服务端（app/core/progress.py），这里只负责选段位和摆结果。
  // 段位阶梯要和后端 LADDER 一致：少一档，选了 Epsilon 就会出现"目标段位不在列表里"。
  const REFORM_OPTIONS = [
    { v: -3, t: "INTRO-1" }, { v: -2, t: "INTRO-2" }, { v: -1, t: "INTRO-3" },
    { v: 1, t: "1 段" }, { v: 2, t: "2 段" }, { v: 3, t: "3 段" }, { v: 4, t: "4 段" },
    { v: 5, t: "5 段" }, { v: 6, t: "6 段" }, { v: 7, t: "7 段" }, { v: 8, t: "8 段" },
    { v: 9, t: "9 段" }, { v: 10, t: "10 段" },
    // Alpha 起一律 1 段一跳：Alpha 就是 11 段，与后端 LADDER 保持一致
    { v: 11, t: "Alpha" }, { v: 12, t: "Beta" }, { v: 13, t: "Gamma" },
    { v: 14, t: "Delta" }, { v: 15, t: "Epsilon" },
    { v: 16, t: "Zeta" }, { v: 17, t: "Eta" },
  ];
  // Malody v3 的段位顺序是 Regular 0~10 → Extra 1~9 → Extra Final。
  // 值只是传给后端的选择码（11~19 表示 Extra 1~9、20 表示 Final），别拿它当段位用。
  const MALODY_OPTIONS = [
    ...Array.from({ length: 11 }, (_, i) => ({ v: i, t: "Regular " + i })),
    ...Array.from({ length: 9 }, (_, i) => ({ v: 11 + i, t: "Extra " + (i + 1) })),
    { v: 20, t: "Extra Final" },
  ];

  function fillDanOptions(type, current) {
    const sel = $("pgValue");
    if (!sel) return;
    const list = type === "malody" ? MALODY_OPTIONS : REFORM_OPTIONS;
    sel.replaceChildren(...list.map((o) => {
      const opt = document.createElement("option");
      opt.value = String(o.v);
      opt.textContent = o.t;
      return opt;
    }));
    const keep = list.find((o) => String(o.v) === String(current));
    if (keep) sel.value = String(keep.v);
  }

  // 计入评估的回放：综合是 50 格（5×10），单个练法 10 格。没填满的格子留空，
  // 玩家一眼就能看出"还差多少局"，也看得见具体是哪几把在算数。
  let pgState = { open: null, data: null };

  function playCell(p) {
    const cell = document.createElement("div");
    cell.className = "pg-cell";
    cell.title = [(p.artist ? p.artist + " - " : "") + p.title, "[" + (p.difficulty || "") + "]",
      Number(p.acc || 0).toFixed(2) + "%", "谱面 " + Number(p.dan || 0).toFixed(2) + " 段",
      "折算 " + Number(p.value || 0).toFixed(2) + " 段",
      p.note || (p.skill ? "练法：" + p.skill : "")].filter(Boolean).join(" · ");

    const body = document.createElement("div");
    body.className = "pg-cell-body";
    const title = document.createElement("span");
    title.className = "pg-cell-title";
    title.textContent = p.title || "—";
    const diff = document.createElement("span");
    diff.className = "pg-cell-diff";
    diff.textContent = p.difficulty || "";
    const acc = document.createElement("span");
    acc.className = "pg-cell-acc" + (Number(p.acc) < 96 ? " is-low" : "");
    acc.textContent = Number(p.acc || 0).toFixed(2) + "%";
    body.append(title, diff, acc);

    // 右侧竖条：这一局按我们的折算规则值多少段。窄格里放不下整句，
    // 上面一行写"折合"，下面把数字和"段"并在一起，避免拆成三行显得散。
    const side = document.createElement("div");
    side.className = "pg-cell-side";
    const cap = document.createElement("span");
    cap.className = "pg-cell-cap";
    cap.textContent = "折合";
    const val = document.createElement("b");
    val.className = "pg-cell-dan";
    val.textContent = Number(p.value || 0).toFixed(1);
    const unit = document.createElement("span");
    unit.className = "pg-cell-unit";
    unit.textContent = "段";
    val.append(unit);
    side.append(cap, val);

    cell.append(body, side);
    return cell;
  }

  function emptyCell() {
    const cell = document.createElement("div");
    cell.className = "pg-cell is-empty";
    return cell;
  }

  function renderCells(box, plays, total) {
    if (!box) return;
    const list = Array.isArray(plays) ? plays : [];
    const cells = [];
    const n = Math.max(total, Math.min(list.length, total));
    for (let i = 0; i < n; i += 1) {
      cells.push(i < list.length ? playCell(list[i]) : emptyCell());
    }
    box.replaceChildren(...cells);
  }

  // 同一时间只展开一个：综合与四个练法互斥
  function showGrid(which) {
    const data = pgState.data;
    if (!data) return;
    const next = pgState.open === which ? null : which;
    pgState.open = next;

    const hero = $("pgHero");
    if (hero) hero.classList.toggle("is-open", next === "overall");

    document.querySelectorAll(".pg-skill").forEach((el) => {
      el.classList.toggle("is-open", el.dataset.skill === next);
    });

    const grid = $("pgGrid");
    const skillGrid = $("pgSkillGrid");
    const note = $("pgSkillNote");
    if (grid) grid.hidden = next !== "overall";
    const skillOpen = next !== null && next !== "overall";
    if (skillGrid) skillGrid.hidden = !skillOpen;
    if (note) note.hidden = !skillOpen;

    if (next === "overall") {
      renderCells(grid, (data.overall || {}).plays || [], 50);
    } else if (skillOpen) {
      const s = (data.skills || {})[next] || {};
      if (note) {
        note.textContent = (s.label || next) + "：实际计入 " + (s.used || 0)
          + " 局（每类最多取 10 局）";
      }
      renderCells(skillGrid, s.plays || [], 10);
    }
  }

  function renderProgress(data) {
    const overall = data.overall || {};
    const target = data.target || {};
    const prof = data.profile || {};
    const owner = data.owner || {};

    // 没填本人名字就不出任何评估：否则会把别人的成绩也算进来
    const need = !!data.needPlayer;
    if ($("pgBody")) $("pgBody").hidden = need;
    // 没填名字（或还没有任何一局）就没有可导的内容
    if ($("btnExportB50")) $("btnExportB50").disabled = need || !data.overall || !data.overall.count;
    if (need) return;

    // 每次重绘都收起展开面：数据可能变了，留着旧的会误导
    pgState = { open: null, data: data };
    if ($("pgGrid")) $("pgGrid").hidden = true;
    if ($("pgSkillGrid")) $("pgSkillGrid").hidden = true;
    if ($("pgSkillNote")) $("pgSkillNote").hidden = true;

    const hero = $("pgHero");
    if (hero) hero.hidden = false;

    const warn = $("pgWarn");
    if (warn) {
      if (data.beyond) {
        warn.hidden = false;
        warn.textContent = "Delta 及以上段位超出了这套评估的能力范围：能参考的谱面样本很少，"
          + "MSD 标尺在高段位也不够稳。下面的结果只作参考。";
      } else {
        warn.hidden = true;
      }
    }

    if ($("pgDan")) {
      $("pgDan").textContent = overall.visible && overall.dan != null
        ? Number(overall.dan).toFixed(2) + " 段" : "—";
    }
    if ($("pgSample")) {
      // 只留最要紧的一条：参与评估的局数，以及为什么有些图不算。
      // 别人打的、认不出玩家的数量放在 hover 里，避免这一行读起来像账单。
      const f = data.filters || {};
      // 分母是"样本目标"：没过 25 局门槛时是门槛本身；过了门槛就换成满样本 50 局，
      // 不然会出现「34 / 25 局」这种读起来像出错的比例。
      const full = data.fullSessions || 50;
      const denom = overall.count > data.minSessions ? full : data.minSessions;
      const tip = ["参与评估 " + overall.count + " / " + denom + " 局"];
      if (f.others) tip.push("已排除别人 " + f.others + " 局");
      if (f.unknown) tip.push(f.unknown + " 局认不出玩家");
      if (f.lnHybrid) tip.push("LN / Hybrid " + f.lnHybrid + " 局暂不计入");
      // 这条是规则说明，不是统计：低于门槛的局直接不进估算，数字放括号里就够
      tip.push("仅 v1acc 高于 91% 的成绩进入个人段位估算"
        + (f.lowAcc ? "（已排除 " + f.lowAcc + " 局）" : ""));
      $("pgSample").textContent = overall.count + " / " + denom
        + " 局（LN Hybrid 图暂不进入评价体系）";
      $("pgSample").title = tip.join(" · ");
    }
    if ($("pgTarget")) $("pgTarget").textContent = target.label ? "目标 " + target.label : "";
    const fill = $("pgBarFill");
    if (fill) fill.style.width = Math.round(Math.min(1, Math.max(0, target.progress || 0)) * 100) + "%";

    const hint = $("pgHint");
    if (hint) {
      hint.className = "pg-hint";
      if (!overall.visible) {
        hint.textContent = "已记录 " + overall.count + " 局，再打 " + overall.need
          + " 局才能出综合评估。样本少的时候结果不稳，先按感觉练。";
        hint.classList.add("warn");
      } else if (target.status === "above") {
        hint.textContent = "现在的综合段位已经高出设定 " + Number(-target.gap).toFixed(2)
          + " 段，可以把当前段位往上调一档，进度条才有意义。";
      } else if (target.status === "ready") {
        hint.textContent = "已经够到 " + target.label + "：把这几张稳住，过段就是时间问题。";
      } else if (target.status === "close") {
        hint.textContent = "离 " + target.label + " 还差 " + Number(target.gap).toFixed(2)
          + " 段，最近这 50 局里已经有 " + target.qualified + " 局过了线。";
      } else if (target.gap !== null && target.gap !== undefined) {
        hint.textContent = "离 " + target.label + " 还差 " + Number(target.gap).toFixed(2)
          + " 段，先把同段位稳定打到 96% 以上。";
      } else {
        hint.textContent = "";
      }
      if (overall.preliminary) {
        hint.classList.add("warn");
        hint.textContent += "（样本 " + overall.count + " 局，属于初步评估）";
      }
    }

    const box = $("pgSkills");
    if (!box) return;
    const skills = data.skills || {};
    box.replaceChildren(...Object.keys(skills).map((key) => {
      const s = skills[key];
      const card = document.createElement("div");
      card.className = "pg-skill" + (s.visible ? "" : " is-empty");
      card.dataset.skill = key;
      card.title = "点一下展开这一类计入评估的回放";
      card.addEventListener("click", () => showGrid(key));
      const head = document.createElement("h4");
      head.textContent = s.label + "（" + key + "）";
      const big = document.createElement("b");
      big.textContent = s.visible && s.dan != null ? Number(s.dan).toFixed(2) + " 段" : "样本不足";
      const sub = document.createElement("span");
      sub.textContent = s.visible
        ? "样本 " + s.count + " 局 · 取最好 " + s.used + (s.preliminary ? " · 初步" : "")
        : "已 " + s.count + " 局，还差 " + s.need + " 局";
      card.append(head, big, sub);
      return card;
    }));
  }

  // ---------------------------------------------------------------- 随机找图
  // 筛选全部在本地索引里做（后端 finder.py），前端只管输入、请求和卡片。
  // 之所以把条件也存进 localStorage：找图是"挑着玩"的动作，关掉页面再回来
  // 还得重新调一遍段位区间会很烦。
  const FINDER_KEY = "mania-finder";
  const FINDER_CATS = ["cut", "stack", "spam", "tech", "ln"];
  const FINDER_N = 8;

  // 两套段位下拉：常规用 RC 标尺，面条用 LN 标尺（LN 5~17，16 段起有专门的名字）。
  // 面条段位单独一套是有原因的：面图的 RC 段位和 LN 段位能差好几段，用 RC 段位筛
  // 面条图只会筛出一堆根本不打 LN 的图。
  const FINDER_DAN = {
    rc: [
      { v: -3, t: "Intro 1" }, { v: -2, t: "Intro 2" }, { v: -1, t: "Intro 3" },
      { v: 1, t: "Reform 1" }, { v: 2, t: "Reform 2" }, { v: 3, t: "Reform 3" },
      { v: 4, t: "Reform 4" }, { v: 5, t: "Reform 5" }, { v: 6, t: "Reform 6" },
      { v: 7, t: "Reform 7" }, { v: 8, t: "Reform 8" }, { v: 9, t: "Reform 9" },
      { v: 10, t: "Reform 10" },
      { v: 11, t: "Alpha" }, { v: 12, t: "Beta" }, { v: 13, t: "Gamma" },
      { v: 14, t: "Delta" }, { v: 15, t: "Epsilon" }, { v: 16, t: "Emik Zeta" },
      { v: 17, t: "Thaumiel Eta" }, { v: 18, t: "CloverWisp Theta" },
    ],
    ln: [
      { v: 5, t: "LN 5" }, { v: 6, t: "LN 6" }, { v: 7, t: "LN 7" },
      { v: 8, t: "LN 8" }, { v: 9, t: "LN 9" }, { v: 10, t: "LN 10" },
      { v: 11, t: "LN 11" }, { v: 12, t: "LN 12" }, { v: 13, t: "LN 13" },
      { v: 14, t: "LN 14" }, { v: 15, t: "LN 15" },
      { v: 16, t: "Hypersovae LN 16" }, { v: 17, t: "Lnlism LN 17" },
    ],
  };
  const FINDER_DEFAULT_DAN = { rc: 6, ln: 7 };
  const finder = { mode: "rc", dan: { rc: 6, ln: 7 }, cat: "", seen: [] };

  function loadFinderPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(FINDER_KEY) || "{}");
      if (p.mode === "rc" || p.mode === "ln") finder.mode = p.mode;
      if (FINDER_CATS.indexOf(p.cat) >= 0 || p.cat === "") finder.cat = p.cat || "";
      ["rc", "ln"].forEach((m) => {
        const v = p.dan && Number(p.dan[m]);
        const list = FINDER_DAN[m];
        if (isFinite(v) && list.some((it) => it.v === v)) finder.dan[m] = v;
      });
    } catch (e) { /* 隐私模式下读不到就用默认值 */ }
  }

  function saveFinderPrefs() {
    try {
      localStorage.setItem(FINDER_KEY, JSON.stringify({
        mode: finder.mode, dan: finder.dan, cat: finder.cat,
      }));
    } catch (e) { /* ignore */ }
  }

  // 切类型时段位下拉要整条换掉：常规 21 项、面条 13 项，值域完全不重叠
  function fillFinderDanOptions() {
    const sel = $("fnDan");
    if (!sel) return;
    sel.textContent = "";
    FINDER_DAN[finder.mode].forEach((item) => {
      const opt = document.createElement("option");
      opt.value = String(item.v);
      opt.textContent = item.t;
      sel.appendChild(opt);
    });
    sel.value = String(finder.dan[finder.mode]);
  }

  function syncFinderUi() {
    const mode = $("fnMode");
    if (mode) mode.value = finder.mode;
    fillFinderDanOptions();
    const cat = $("fnCat");
    if (cat) {
      cat.value = finder.cat;
      // 面条图的主键型本来就是 LN，再选键型没有意义
      cat.disabled = finder.mode === "ln";
    }
  }

  // changed 是刚变动的控件 id。切类型时下拉里还是旧模式的值（"6" 在两边都存在），
  // 照着读会把常规的 6 段带进面条，所以那种情况下只认模式，段位回到它自己记着的值。
  function readFinderInputs(changed) {
    const mode = $("fnMode");
    if (mode) finder.mode = mode.value === "ln" ? "ln" : "rc";
    if (changed !== "fnMode") {
      const dan = $("fnDan");
      if (dan) {
        const value = Number(dan.value);
        const list = FINDER_DAN[finder.mode];
        finder.dan[finder.mode] = list.some((it) => it.v === value)
          ? value : FINDER_DEFAULT_DAN[finder.mode];
      }
      const cat = $("fnCat");
      if (cat) finder.cat = FINDER_CATS.indexOf(cat.value) >= 0 ? cat.value : "";
    }
    saveFinderPrefs();
    syncFinderUi();
  }

  function finderCard(it) {
    const card = document.createElement("article");
    card.className = "map-card";

    // 下载绑在封面上：用户明确要求"点封面下载"，所以只有封面是链接，其余不抢点击
    const cover = document.createElement("a");
    cover.className = "map-cover";
    cover.href = it.download;
    cover.target = "_blank";
    cover.rel = "noopener";
    cover.title = "点击封面下载 " + (it.title || "") + "（.osz）";
    // 分段并发下载（镜像支持 Range）：拿不到就自己退回新标签页直链
    if (window.MapDownload) window.MapDownload.attach(cover, it.download, it);

    const img = document.createElement("img");
    img.src = it.cover;
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    // 封面走外网 CDN，断网或被墙时会 404 —— 退回渐变底色，别显示裂图
    img.addEventListener("error", () => { img.remove(); cover.classList.add("is-blank"); });
    cover.appendChild(img);

    const tip = document.createElement("span");
    tip.className = "map-cover-tip";
    tip.textContent = "点击下载 .osz";
    cover.appendChild(tip);

    // 段位是筛选依据，放左上角（数值形式，方便横着比）；星数退到右上角当参考
    const danBadge = document.createElement("span");
    danBadge.className = "map-dan";
    danBadge.textContent = it.danShort || "";
    cover.appendChild(danBadge);

    const star = document.createElement("span");
    star.className = "map-star";
    star.textContent = "★ " + Number(it.star || 0).toFixed(2);
    cover.appendChild(star);

    const info = document.createElement("div");
    info.className = "map-info";

    const title = document.createElement("div");
    title.className = "map-title";
    title.textContent = it.title || "（无标题）";

    const sub = document.createElement("div");
    sub.className = "map-sub";
    sub.textContent = [it.artist, it.creator].filter(Boolean).join(" · ");

    const ver = document.createElement("div");
    ver.className = "map-version";
    ver.textContent = it.version || "";

    const foot = document.createElement("div");
    foot.className = "map-foot";
    // 段位标签带上估算依据（Roxy / Azusa / Sunny）：同一张图换算法能差一两段，
    // 只给个名字的话，用户遇到"这图怎么会是这难度"就没法判断该信几分。
    // 面条图一律走 Sunny，再标一遍是噪音。
    const danText = [it.dan, it.mode === "ln" ? "" : it.danMethod].filter(Boolean).join(" · ");
    // 面条模式看的是"这张图有多少面条"，主键型往往是 Jack/Stream 反而误导
    const mixText = it.mode === "ln"
      ? "LN " + Math.round(Number(it.lnRatio || 0) * 100) + "%"
      : (it.pattern ? it.pattern + " " + it.patternShare + "%" : "");
    [it.length, danText, mixText]
      .filter(Boolean).forEach((text) => {
        const chip = document.createElement("span");
        chip.className = "map-chip";
        chip.textContent = text;
        foot.appendChild(chip);
      });

    info.append(title, sub, ver, foot);
    card.append(cover, info);
    return card;
  }

  // 区间怎么写给人看：Reform 以上用"5.0~7.0 段"，INTRO 与面条用档位名
  // （0 段以下说"负几段"是错的，那里是 Intro 1~3；LN 段位本来就只有名字）。
  function finderRangeText(used) {
    const labels = [used.danMinLabel, used.danMaxLabel].filter(Boolean).join(" ~ ");
    const lo = Number(used.danMin || 0);
    const hi = Number(used.danMax || 0);
    if (used.mode === "ln" || lo < 0.5) return labels || (lo.toFixed(1) + "~" + hi.toFixed(1));
    const numbers = lo.toFixed(1) + "~" + hi.toFixed(1) + " 段";
    return labels ? numbers + "（" + labels + "）" : numbers;
  }

  function renderFinder(data) {
    const grid = $("fnGrid");
    if (!grid) return;
    grid.textContent = "";
    const items = (data && data.items) || [];
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "find-empty";
      empty.textContent = "这个条件下没有找到图，把段位区间放宽一点或换一类键型再试。";
      grid.appendChild(empty);
    } else {
      items.forEach((it) => {
        grid.appendChild(finderCard(it));
        if (finder.seen.indexOf(it.sid) < 0) finder.seen.push(it.sid);
      });
    }
    if (finder.seen.length > 60) finder.seen = finder.seen.slice(-60);

    const count = $("fnCount");
    if (count) count.textContent = "本批 " + items.length + " 张";
    const note = $("fnNote");
    if (note) {
      const used = (data && data.used) || {};
      const parts = [];
      if (data && data.relaxed) {
        let relax = "±1 段里没凑够，已放宽到 " + finderRangeText(used);
        if (used.cat) relax += "、" + used.cat + " ≥ " + used.share + "%";
        parts.push(relax);
      }
      parts.push("封面来自 osu! 官方 CDN；点封面直接下 .osz（sayobot 镜像），下不动可换 nerinyan");
      note.textContent = parts.join("　·　");
    }
  }

  async function runFinder(again, changed) {
    readFinderInputs(changed);
    const btn = $("btnFind");
    const btnAgain = $("btnFindAgain");
    if (btn) btn.disabled = true;
    if (btnAgain) btnAgain.disabled = true;
    if (!again) finder.seen = [];
    try {
      let data = await window.API.randomCharts({
        mode: finder.mode,
        dan: finder.dan[finder.mode],
        cat: finder.cat,
        exclude: again ? finder.seen : [],
        n: FINDER_N,
      });
      // 符合条件的图都被看过了：清空"已经给过"的列表重来，别让用户对着空列表
      if (again && !((data && data.items) || []).length && finder.seen.length) {
        finder.seen = [];
        data = await window.API.randomCharts({
          mode: finder.mode, dan: finder.dan[finder.mode],
          cat: finder.cat, exclude: [], n: FINDER_N,
        });
      }
      renderFinder(data);
      if (btnAgain) btnAgain.hidden = false;
    } catch (e) {
      window.UI.toast("找图失败：" + ((e && e.message) || "服务没有响应"), true);
    } finally {
      if (btn) btn.disabled = false;
      if (btnAgain) btnAgain.disabled = false;
    }
  }

  function bindFinder() {
    loadFinderPrefs();
    syncFinderUi();
    // 改任一项都直接重新找一批：下拉本身就是"确认"，再点一次按钮是多余动作
    ["fnMode", "fnDan", "fnCat"].forEach((id) => {
      const sel = $(id);
      if (sel) sel.addEventListener("change", () => runFinder(false, id));
    });
    const find = $("btnFind");
    if (find) find.addEventListener("click", () => runFinder(false));
    const again = $("btnFindAgain");
    if (again) again.addEventListener("click", () => runFinder(true));
  }

  async function loadProgress() {
    const status = $("pgStatus");
    try {
      const data = await window.API.progress();
      const prof = data.profile || {};
      if ($("pgType")) $("pgType").value = prof.type || "reform";
      if ($("pgPlayer")) $("pgPlayer").value = prof.player || "";
      fillDanOptions(prof.type || "reform", prof.value);
      renderProgress(data);
      if (status) {
        status.className = "dir-status";
        if (data.needPlayer) {
          status.textContent = "先填自己的 osu! 名字并保存：只有本人的成绩才算进评估。";
          return;
        }
        const owner = data.owner || {};
        if (!prof.dan) {
          status.textContent = "已认出你的记录 " + (owner.mine || 0) + " 局，选一个当前段位保存后就能看进度。";
          return;
        }
        // Malody 的定位要说清是"段位谱实测"，不然和 REFORM 的数值容易被当成同一把尺
        let head = "当前定位 " + (prof.label || "") + "（按 REFORM 标尺 "
          + Number(prof.dan || 0).toFixed(2) + " 段）";
        if (prof.type === "malody" && prof.malodyLabel) {
          head = "当前定位 " + prof.malodyLabel + "（段位谱实测相当于 REFORM "
            + Number(prof.dan || 0).toFixed(2) + " 段）";
        }
        const bits = [head];
        if (owner.others) bits.push("已排除别人打的 " + owner.others + " 局");
        if (owner.unknown) bits.push(owner.unknown + " 局认不出玩家，暂不计入");
        status.textContent = bits.join("，");
      }
    } catch (e) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = "读取失败：" + (e.message || e);
      }
    }
  }

  async function saveProfile() {
    const player = $("pgPlayer") ? $("pgPlayer").value.trim() : "";
    const type = $("pgType") ? $("pgType").value : "reform";
    const value = $("pgValue") ? $("pgValue").value : "";
    const status = $("pgStatus");
    if (!player) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = "先填自己的 osu! 名字或 ID。";
      }
      return;
    }
    if (status) {
      status.className = "dir-status";
      status.textContent = "换算中…";
    }
    try {
      const res = await window.API.saveProfile(player, type, Number(value));
      if (status) {
        status.className = "dir-status good";
        status.textContent = type === "malody"
          ? "已保存：" + (res.profile.malodyLabel || "")
            + "，段位谱实测相当于 REFORM " + Number(res.profile.dan).toFixed(2) + " 段"
          : "已保存：" + (res.profile.label || "");
      }
      await loadProgress();
    } catch (e) {
      if (status) {
        status.className = "dir-status bad";
        status.textContent = e.message || "保存失败";
      }
    }
  }

  // ---------------------------------------------------------------- 镜像站测速
  // 走一次真实下载链路（GET + Range 读 1KB），既能测延迟也能确认对方真给数据。
  // 只被拒绝（403/405）的站标黄，别让它看起来跟正常站一样快。
  const MIRROR_LABEL = { sayobot: "sayobot（国内）", nerinyan: "nerinyan", mino: "catboy / mino" };

  function mirrorLevel(ms) {
    // 阈值按"能不能顺畅下几十 MB"定：500ms 左右握手仍然算可用，
    // 一秒往上就明显拖慢补音频了。
    if (ms < 300) return "is-fast";
    if (ms < 1000) return "is-mid";
    return "is-slow";
  }

  function renderMirrorResults(res) {
    const box = $("mirrorResult");
    if (!box) return;
    window.UI.clearNode(box);
    const rows = (res && res.results) || [];
    rows.forEach((it) => {
      const li = document.createElement("li");
      const ok = !!it.ok;
      // 连得上但被拒绝（403 / 405 这类）跟完全连不上要分开：前者不是线路问题，
      // 但补音频同样下不动，不能显示成一个正常延迟。
      const denied = !ok && it.reachable !== false;
      li.className = "mirror-item "
        + (ok ? mirrorLevel(Number(it.ms) || 0) : (denied ? "is-denied" : "is-fail"));
      // 连不上时错误原文可能很长（DNS、证书、超时各不相同），放悬浮提示里，正文只写结论
      li.title = ok ? ("HTTP " + it.status + " · " + it.ms + " 毫秒") : (it.error || "没有回应");
      const name = document.createElement("span");
      name.className = "m-name";
      name.textContent = MIRROR_LABEL[it.name] || it.name;
      const ms = document.createElement("span");
      ms.className = "m-ms";
      // 超时和"直接连不上"分开写：前者多半是线路被墙，后者可能是 DNS 挂了。
      // 错误原文有中文也有英文（timed out / timeout），两种都认。
      const timedOut = /timed out|timeout|超时/i.test(String(it.error || ""));
      ms.textContent = ok ? (it.ms + " ms")
        : (denied && it.status ? ("HTTP " + it.status) : (timedOut ? "超时" : "连接失败"));
      li.append(name, ms);
      box.appendChild(li);
    });
    box.hidden = !rows.length;
  }

  async function testMirrors() {
    const btn = $("btnMirrorTest");
    if (btn) btn.disabled = true;
    window.UI.tasks.start("mirror", "测试镜像站");
    try {
      const res = await window.API.testMirrors();
      renderMirrorResults(res);
      window.UI.tasks.end("mirror");
    } catch (e) {
      window.UI.tasks.end("mirror", {
        error: true, label: "测试镜像站失败", message: (e && e.message) || "网络错误",
      });
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function bindProgress() {
    App.on("pgType", "change", () => fillDanOptions($("pgType").value, null));
    App.on("btnSaveProfile", "click", saveProfile);
    // 综合卡片整块可点：展开 5×10 的明细
    App.on("pgHero", "click", () => showGrid("overall"));
    fillDanOptions("reform", 7);
  }


  // pgState 导出 B50 时 bind 要读当前展开的成长页数据；模块内继续用局部变量，外面读写都走这个访问器
  Object.defineProperty(App, "pgState", {
    get() { return pgState; },
    set(v) { pgState = v; },
  });

  // 供其它模块调用；同名冲突在这里会直接覆盖，加载顺序见 index.html
  Object.assign(App, {
    fillDanOptions, playCell, emptyCell, renderCells, showGrid, renderProgress,
    loadFinderPrefs, saveFinderPrefs, fillFinderDanOptions, syncFinderUi, readFinderInputs,
    finderCard, finderRangeText, renderFinder, runFinder, bindFinder, loadProgress,
    saveProfile, mirrorLevel, renderMirrorResults, testMirrors, bindProgress,
  });
})(window.App = window.App || {});
