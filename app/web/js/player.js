/*!
 * player.js — osu!mania 下落式回放的 canvas 渲染器
 *
 * 设计取舍（为什么这么写）：
 * 1. 纯原生 JS、无依赖、无构建、不用 ES module：页面只需要一行
 *    <script src="/static/js/player.js"></script>，后端是本地 http.server 静态目录，
 *    不需要打包器，也能直接用 file:// 打开测试页。
 * 2. 本模块只管“画”，不管时间：时间由外部（音频/计时器）用 setTime() 推进，
 *    否则播放器和音频的同步逻辑会散落在两个地方，容易各走各的。
 * 3. 所有绘制都在 CSS 像素坐标里做，DPR 靠一次 setTransform 统一放大，
 *    这样布局计算不用到处乘 dpr（最容易写错的地方）。
 * 4. 每帧只遍历“可视时间窗”里的音符：二分找起点，而不是整表扫，
 *    因为一个谱面可能有上万物件，逐帧 O(n) 会让 60fps 掉帧。
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------------------
  // 常量
  // ---------------------------------------------------------------------------

  // 可选参数的默认值。scrollMs 取 1200ms 是因为 osu!mania 常见下落速度落在
  // 1000~1600ms 之间，1200 既不挤也不至于一屏看不到几个音符。
  var DEFAULTS = {
    scrollMs: 1200,
    judgementFilter: 'all',
    showJudgements: true,
    deviationBars: true,
    showPresses: true,        // 是否画"实际击打/松手位置"那两条线
    fixedJudgeColors: false   // 是否用固定判定色（白黄绿蓝紫红），而不是主题色
  };

  // 主题兜底色。键名与 CSS 变量一一对应（见 CSS_VARS），
  // 页面给了 CSS 变量就用页面的，给不到才落到这里，保证单独打开本模块也不瞎。
  var THEME_DEFAULTS = {
    bg: '#12142a',
    stroke: 'rgba(255,255,255,0.10)',
    accent: '#7c5cff',
    jMax: '#7ef2d0',
    j300: '#ffd479',
    j200: '#9fe870',
    j100: '#8fb8ff',
    j50: '#c9a0ff',
    jmiss: '#ff6b8a',
    catAbility: '#ff9f6e',
    catRhythm: '#6ee7ff',
    catDistraction: '#c792ff',
    // 轨道底色。osu!mania 的谱面舞台是近乎纯黑的实色（不是渐变、也不是半透明奶白），
    // 音符的高对比配色只有在黑底上才立得住。取 95% 而不是 100%，
    // 是为了让两侧的留白还能透出一丝页面底色，不至于糊成一块纯黑的洞。
    laneBg: 'rgba(0, 0, 0, 0.95)',
    laneLine: 'rgba(255, 255, 255, 0.12)'
  };

  // 主题键 -> CSS 变量名。用变量名做“事实来源”，页面改皮肤不用改 JS。
  var CSS_VARS = {
    bg: '--bg-1',
    stroke: '--stroke',
    accent: '--accent',
    jMax: '--j-max',
    j300: '--j-300',
    j200: '--j-200',
    j100: '--j-100',
    j50: '--j-50',
    jmiss: '--j-miss',
    catAbility: '--cat-ability',
    catRhythm: '--cat-rhythm',
    catDistraction: '--cat-distraction',
    catSlip: '--cat-slip',
    laneBg: '--lane-bg',
    laneLine: '--lane-line'
  };

  // 判定码 -> 主题键。0=MAX 1=300 共用金色（都是“好”），2=200 绿，3=100 蓝，4=50 紫，5=MISS 红。
  // MAX 与 300 用不同颜色（明细页的判定分布柱也是分开的），
  // 这样画面上「这一击是满分」能一眼看出来。
  var JUDGE_KEYS = ['jMax', 'j300', 'j200', 'j100', 'j50', 'jmiss'];

  // 「实际击打位置」那条线的颜色：按判定固定（不跟主题变，方便一眼认出判级）
  // MAX 白 / 300 黄 / 200 绿 / 100 蓝 / 50 紫 / MISS 红
  var PRESS_COLORS = ['#ffffff', '#ffd54a', '#4ade80', '#5aa2ff', '#b06bff', '#ff4d4d'];

  // 长条松手：早于尾判点是"提前松手"（会掉判定），晚于是"松晚了"。
  // 两个方向的意义完全不同，所以分色 —— 青=提前，橙=拖晚。
  var RELEASE_EARLY = '#38bdf8';
  var RELEASE_LATE = '#fb923c';

  // 归因类别 -> 主题键
  var CAT_KEYS = {
    ability: 'catAbility',
    rhythm: 'catRhythm',
    distraction: 'catDistraction',
    slip: 'catSlip'
  };

  // 主题键的别名。页面里的图表模块用的是 ability / rhythm / distraction / miss / grid
  // 这套简称（见 ui.js 的 readTheme），这里直接认，免得同一个人写两套键名，
  // 传进来的颜色被静默丢掉（“为什么我给了颜色却没生效”是最难查的一类问题）。
  var THEME_ALIASES = {
    ability: 'catAbility',
    rhythm: 'catRhythm',
    distraction: 'catDistraction',
    slip: 'catSlip',
    miss: 'jmiss',
    grid: 'stroke'
  };

  // 布局常量
  var JUDGE_BOTTOM_RATIO = 0.12; // 判定线距底边 12%
  var NOTE_W_RATIO = 0.86;       // 音符宽度 = 列宽 * 0.86（留出槽壁）
  var MAX_LANE_W = 148;          // 单列最大宽度。画布很宽时若还把每列拉到 200px，
                                 // 皮肤素材（本来就是按 80~120px 列宽做的）会被放大到糊满全屏；
                                 // 超过上限就把舞台居中，两边留空，观感跟 osu 一致。
  var NOTE_H = 13;               // 单点高度，规格要求 12~14
  var NOTE_RADIUS = 5;
  var HOLD_RADIUS = 5;
  var HOLD_BODY_ALPHA = 0.5;     // 长按条主体比头部淡，才不会盖住判定线
  var CAP_H = 4;                 // 长按头部亮端帽
  var CAT_BAR_W = 4;             // 归因色条宽度

  // 用皮肤素材画音符时的尺寸限制：
  // 素材本身是方的（比如 89×72），直接按 13px 高画会糊成一团看不出形状；
  // 但也不能完全放任——有些皮肤的 LNTail 是 128×40000 这种极端比例，必须夹住。
  var IMG_MIN_H_RATIO = 0.55;    // 素材高度至少是音符宽度的 55%
  var IMG_MAX_H_RATIO = 2.2;     // 至多是宽度的 2.2 倍
  var IMG_MAX_LANE_RATIO = 1.15; // 绝对上限：不超过一个列宽，免得密集段糊成一片
  var PAST_MS = 80;              // 判定线以下的“刚判完”尾巴
  // 打击特效存活时间。原来 120ms 只够一个光点飘一下；现在换成横向扩散的冲击波，
  // 扩散过程本身就是"这一下有多重"的读数，给到 220ms 才看得清，也不至于糊成一片。
  var IMPACT_MS = 220;
  var ZONE_H = 40;               // 判定区色带高度
  var EMPTY_ALPHA = 0.2;
  // 轨道里的白色细线（列分隔 / 空状态短横线）。轨道底色恒为近黑，
  // 所以这条线用固定的淡白，不跟主题 --stroke 走——那个变量是给"面板边缘"用的，
  // 直接拿来画轨道内部分隔线会随面板描边权重一起变淡或消失。
  var LANE_LINE = 'rgba(255, 255, 255, 0.12)';

  // 偏差柱（每列一条 15% 红柱，实时反映该列已判定音符的平均 |偏差|）：
  // 高度线性映射，DEV_BAR_FULL_MS 对应满高。45ms 的平均偏差已经相当飘，
  // 但正常发挥（10~20ms）柱子只有一两成高，高矮差看不出——所以统一放大 2.5 倍，
  // 让 8ms 和 16ms 的差别肉眼可辨。放大量只影响观感，精确数值仍看「此刻」面板。
  var DEV_BAR_FULL_MS = 45;
  var DEV_BAR_GAIN = 3.75;       // 视觉增益：柱子越高越好读（原 2.5，整体再拉长 1.5 倍）
  var DEV_BAR_MAX_RATIO = 0.42;  // 未放大时柱子最多长到判定线以上多高（占判定线高度的比例）
  var DEV_BAR_ALPHA = 0.15;      // 它是「背景」，而且是铺在近乎纯黑的轨道上：
                                 // 30% 时红得发亮、抢了音符的视线，15% 刚好能看出高矮

  // ---------------------------------------------------------------------------
  // 颜色小工具
  //
  // 主题色是外部给的字符串，可能是 #rgb / #rrggbb / #rrggbbaa / rgb() / rgba()，
  // 也可能是 CSS 变量解析出来的任意值。做“半透明 / 提亮”不能靠字符串拼接
  // （拼错了 canvas 会静默忽略，画面上直接消失，非常难查），
  // 所以这里把颜色解析成数字再重组，解析失败就原样返回让它至少还能画出来。
  // ---------------------------------------------------------------------------

  function parseColor(color) {
    if (typeof color !== 'string') return null;
    var s = color.trim();
    if (!s) return null;

    if (s.charAt(0) === '#') {
      var hex = s.slice(1);
      if (hex.length === 3 || hex.length === 4) {
        var expanded = '';
        for (var i = 0; i < hex.length; i += 1) expanded += hex.charAt(i) + hex.charAt(i);
        hex = expanded;
      }
      if (hex.length !== 6 && hex.length !== 8) return null;
      var r = parseInt(hex.slice(0, 2), 16);
      var g = parseInt(hex.slice(2, 4), 16);
      var b = parseInt(hex.slice(4, 6), 16);
      if (isNaN(r) || isNaN(g) || isNaN(b)) return null;
      var a = hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1;
      return { r: r, g: g, b: b, a: a };
    }

    var m = s.match(/^rgba?\(([^)]+)\)$/i);
    if (m) {
      var parts = m[1].split(/[,\/\s]+/).filter(function (piece) { return piece !== ''; });
      var nums = parts.map(parseFloat);
      if (nums.length >= 3 && !isNaN(nums[0]) && !isNaN(nums[1]) && !isNaN(nums[2])) {
        return {
          r: nums[0],
          g: nums[1],
          b: nums[2],
          a: nums.length > 3 && !isNaN(nums[3]) ? nums[3] : 1
        };
      }
    }
    return null;
  }

  // 给颜色换一个透明度（a 是相对系数，会乘上原色的 alpha）
  function withAlpha(color, alpha) {
    var p = parseColor(color);
    if (!p) return color;
    var a = p.a * alpha;
    if (a < 0) a = 0;
    if (a > 1) a = 1;
    return 'rgba(' + p.r + ',' + p.g + ',' + p.b + ',' + a.toFixed(3) + ')';
  }

  // 提亮(k>1) / 压暗(k<1)。用“向白色插值”而不是单纯加常数，
  // 否则浅色会被直接推到饱和、深色又提不起来。
  function shade(color, k) {
    var p = parseColor(color);
    if (!p) return color;
    var mix = function (v) {
      var out = k > 1 ? v + (255 - v) * (k - 1) : v * k;
      if (out < 0) out = 0;
      if (out > 255) out = 255;
      return Math.round(out);
    };
    return 'rgba(' + mix(p.r) + ',' + mix(p.g) + ',' + mix(p.b) + ',' + p.a + ')';
  }

  function clamp(v, lo, hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
  }

  // 读元素上生效的 CSS 变量。失败一律回退，因为 getComputedStyle 在
  // 未挂载 / 跨域 iframe / 老浏览器里都可能抛异常，而画不了画比配色难看严重得多。
  function readCssVar(el, name, fallback) {
    try {
      var doc = el && el.ownerDocument;
      var view = (doc && doc.defaultView) || (typeof global !== 'undefined' ? global : null);
      if (!view || typeof view.getComputedStyle !== 'function') return fallback;
      var style = view.getComputedStyle(el);
      if (!style || typeof style.getPropertyValue !== 'function') return fallback;
      var value = style.getPropertyValue(name);
      value = value ? value.trim() : '';
      return value || fallback;
    } catch (err) {
      return fallback;
    }
  }

  // ---------------------------------------------------------------------------
  // 工厂
  // ---------------------------------------------------------------------------

  function create(canvasEl, options) {
    if (!canvasEl || typeof canvasEl.getContext !== 'function') {
      throw new TypeError('ManiaPlayer.create(canvasEl, options): 第一个参数必须是 <canvas> 元素');
    }
    var ctx = canvasEl.getContext('2d');
    if (!ctx) {
      throw new Error('ManiaPlayer.create: 拿不到 2d 上下文（canvas 可能已被别的代码以其它类型 getContext 过）');
    }

    var opts = {
      scrollMs: DEFAULTS.scrollMs,
      judgementFilter: DEFAULTS.judgementFilter,
      showJudgements: DEFAULTS.showJudgements,
      deviationBars: DEFAULTS.deviationBars,
      showPresses: DEFAULTS.showPresses,
      fixedJudgeColors: DEFAULTS.fixedJudgeColors,
      // 打击音效：hitsound 是 HitSound 实例（页面创建），mode 决定按哪个时间点触发。
      // 播放器自己不出声，只负责"该响哪一下"的判断，方便两个实例里只让主播放器响。
      hitsound: null,
      hitsoundMode: 'off'
    };
    mergeOptions(options);
    var cssBgAtCreate = null; // CSS 里 --bg-1 的值，theme.bg 不透明性不可用时用它
    var theme = buildTheme(canvasEl, options && options.theme);

    // 音符数据：按 t 升序的“引用数组”，load 时排一次，渲染时只做二分
    var notes = [];
    var keys = 4;
    // 打击音的"上一帧时间"。每帧只在 (audioFrom, now] 这个区间里找该响的音符，
    // 所以同一帧调两次 render 也不会重复出声；seek 时把它置空，避免补播一大段。
    var audioFrom = null;
    var timeMs = 0;
    var maxHoldMs = 0;  // 最长长按时长，用来决定二分起点要往前退多少
    var minTime = 0;
    var maxTime = 0;

    // 偏差柱用的前缀和：lanePreAbs[k][i] = 前 i 个音符里、第 k 列已判定音符的 |偏差| 之和。
    // 播放时每帧对 now 做一次二分，就能 O(keys) 拿到各列的实时平均——
    // 不能逐帧扫全表（上万物件会掉帧），也不能用“上一帧的指针”（seek 往回跳就废了）。
    var lanePreAbs = [];
    var lanePreCnt = [];

    var listeners = { seek: [], hover: [], pick: [] };

    // 尺寸与布局（全部 CSS 像素）
    var W = 0;
    var H = 0;
    var dpr = 1;
    var judgeY = 0;
    var pxPerMs = 0;
    var laneW = 0;
    var stageLeft = 0;   // 舞台左缘：列宽被夹住时舞台要居中
    var noteW = 0;

    // 缓存：渐变每帧重建会白白分配对象，但这几样只跟尺寸/主题/列数有关
    var bgGrad = null;
    var zoneGrad = null;
    var laneFill = THEME_DEFAULTS.laneBg; // 轨道底色（纯色，见 THEME_DEFAULTS.laneBg 的注释）

    // 皮肤素材：noteImgs[列号] 是一张 <img>，lnBodyImg / lnTailImg 是长按条的身体与尾巴。
    // 站点把素材 URL 通过 /api/media 发过来，这里是异步加载的——
    // 没加载完之前必须照样能画（用原来的圆角矩形），不能让画面空着等图。
    var noteImgs = [];
    var lnBodyImg = null;
    var lnTailImg = null;
    // 上面两张图"预处理"之后的小画布（见 prepareStrip）：每帧都画原图会很贵
    var lnBodyTex = null;
    var lnTailTex = null;

    var needMeasure = true;   // 首次渲染前自动量一次尺寸
    var dragging = false;
    var lastSeekAt = 0;       // 用来给 mousedown / click 去重
    var selectedKey = null;   // 被点中的失误物件 {t, c}：画一圈高亮
    var destroyed = false;

    // -------------------------------------------------------------------------
    // 主题
    // -------------------------------------------------------------------------

    function buildTheme(el, override) {
      var out = {};
      Object.keys(CSS_VARS).forEach(function (key) {
        out[key] = readCssVar(el, CSS_VARS[key], THEME_DEFAULTS[key]);
      });
      cssBgAtCreate = out.bg; // 记住 CSS 给的底色，theme.bg 不可用时拿它兜底
      return applyThemePatch(out, override);
    }

    // 颜色能不能真的拿来铺底？'transparent' / 'none' / 全透明 rgba 都算“不可用”，
    // 因为画布必须是不透明的，才画得出规格要求的那层纵向纵深；
    // 页面若想让画布透出 CSS 背景，正确做法是给 canvas 元素自己设 background。
    function usableBg(color) {
      var p = parseColor(color);
      if (!p || p.a <= 0.01) return null;
      return color;
    }

    function effectiveBg() {
      return usableBg(theme.bg) || usableBg(cssBgAtCreate) || THEME_DEFAULTS.bg;
    }

    // 支持两种写法：JS 键名（accent / j300 / catAbility）、常见简称（ability / miss / grid）、
    // 以及 CSS 变量名（--accent / --j-300）。页面作者三种都会下意识写，这里都认。
    function keyFromCssName(name) {
      var found = null;
      Object.keys(CSS_VARS).forEach(function (key) {
        if (CSS_VARS[key] === name) found = key;
      });
      return found;
    }

    function applyThemePatch(base, patch) {
      if (!patch || typeof patch !== 'object') return base;
      Object.keys(patch).forEach(function (rawKey) {
        var value = patch[rawKey];
        if (typeof value !== 'string' || !value) return;
        var key = rawKey.charAt(0) === '-' ? keyFromCssName(rawKey) : rawKey;
        if (!key) return;
        if (Object.prototype.hasOwnProperty.call(THEME_ALIASES, key)) key = THEME_ALIASES[key];
        if (Object.prototype.hasOwnProperty.call(THEME_DEFAULTS, key)) base[key] = value;
      });
      return base;
    }

    // create() 与 setOptions() 共用一份合并逻辑，避免两处判断各写一遍、日后走样
    function mergeOptions(partial) {
      if (!partial || typeof partial !== 'object') return;
      if (typeof partial.scrollMs === 'number' && partial.scrollMs > 0) opts.scrollMs = partial.scrollMs;
      if (typeof partial.judgementFilter === 'string') {
        opts.judgementFilter = (partial.judgementFilter === 'errors') ? 'errors' : 'all';
      }
      if (typeof partial.showJudgements === 'boolean') opts.showJudgements = partial.showJudgements;
      if (typeof partial.deviationBars === 'boolean') opts.deviationBars = partial.deviationBars;
      // 这两个是「对比模式」要用的：showPresses=false 让标杆不画实际击打/松手位置，
      // fixedJudgeColors=true 让音符本身走白/黄/绿/蓝/紫/红那套判定色。
      // 漏掉它们的话 create() 时传进来会被静默丢弃 —— 表现就是"说了没生效"。
      if (typeof partial.showPresses === 'boolean') opts.showPresses = partial.showPresses;
      if (typeof partial.fixedJudgeColors === 'boolean') opts.fixedJudgeColors = partial.fixedJudgeColors;
      // 音效模式随时可切：切的时候把上一段的残留声音掐掉，否则新旧两种节奏会叠在一起
      if (partial.hitsound !== undefined) {
        opts.hitsound = partial.hitsound || null;
        audioFrom = null;
        if (opts.hitsound && typeof opts.hitsound.stopAll === 'function') opts.hitsound.stopAll();
      }
      if (typeof partial.hitsoundMode === 'string') {
        var m = (partial.hitsoundMode === 'actual' || partial.hitsoundMode === 'planned')
          ? partial.hitsoundMode : 'off';
        if (m !== opts.hitsoundMode) {
          opts.hitsoundMode = m;
          audioFrom = null;
          if (opts.hitsound && typeof opts.hitsound.stopAll === 'function') opts.hitsound.stopAll();
        }
      }
    }

    // DPR 从元素所在的 window 取：iframe / 不同缩放比的显示器上，
    // 元素自己窗口的 devicePixelRatio 才是画布真正该用的那个。
    function currentDpr() {
      var view = canvasEl.ownerDocument && canvasEl.ownerDocument.defaultView;
      var ratio = (view && typeof view.devicePixelRatio === 'number')
        ? view.devicePixelRatio
        : global.devicePixelRatio;
      if (typeof ratio !== 'number' || !(ratio > 0)) ratio = 1; // NaN / 0 / undefined 一律当 1
      return ratio;
    }

    // -------------------------------------------------------------------------
    // 尺寸与布局
    // -------------------------------------------------------------------------

    function measure() {
      // clientWidth 为 0 说明元素还没布局（display:none / 刚 create），
      // 此时退回 width 属性，避免除零把整帧画废。
      var cw = canvasEl.clientWidth || canvasEl.width || 0;
      var ch = canvasEl.clientHeight || canvasEl.height || 0;
      W = cw;
      H = ch;

      dpr = currentDpr();

      var pixelW = Math.max(1, Math.round(W * dpr));
      var pixelH = Math.max(1, Math.round(H * dpr));
      // 赋值 width/height 会自动清空画布，所以只在真的变了才写
      if (canvasEl.width !== pixelW) canvasEl.width = pixelW;
      if (canvasEl.height !== pixelH) canvasEl.height = pixelH;

      // 一次变换搞定 DPR：之后所有坐标都是 CSS 像素
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      needMeasure = false;
      layout();
    }

    function layout() {
      if (!(W > 0 && H > 0)) {
        needMeasure = true;
        return;
      }
      judgeY = H - H * JUDGE_BOTTOM_RATIO;
      laneW = Math.min(W / keys, MAX_LANE_W);
      stageLeft = Math.max(0, (W - keys * laneW) * 0.5);
      noteW = laneW * NOTE_W_RATIO;
      // 时间 -> y 的换算：y = judgeY - (t - now) * pxPerMs。
      // 于是“现在”正好落在判定线上，“now + scrollMs”正好落在画布顶端。
      pxPerMs = opts.scrollMs > 0 ? judgeY / opts.scrollMs : 0;
      rebuildGradients();
    }

    function rebuildGradients() {
      if (!(W > 0 && H > 0)) return;

      // 背景：很淡的纵向渐变，制造纵深（顶部略亮、底部压暗）
      var bg = effectiveBg();
      bgGrad = ctx.createLinearGradient(0, 0, 0, H);
      bgGrad.addColorStop(0, shade(bg, 1.35));
      bgGrad.addColorStop(0.55, bg);
      bgGrad.addColorStop(1, shade(bg, 0.7));

      // 判定区色带：主题色从上到下淡出
      zoneGrad = ctx.createLinearGradient(0, judgeY, 0, judgeY + ZONE_H);
      zoneGrad.addColorStop(0, withAlpha(theme.accent, 0.2));
      zoneGrad.addColorStop(1, withAlpha(theme.accent, 0));

      // 轨道底色 = 当前主题的纯色（默认 95% 黑）。兜一层：万一主题给的是
      // 全透明 / 空字符串，就退回不透明黑 —— 舞台必须是实的，
      // 透出去会看见上一帧的音符残渣。
      laneFill = usableBg(theme.laneBg) || 'rgba(0, 0, 0, 0.95)';
    }

    // -------------------------------------------------------------------------
    // 数据
    // -------------------------------------------------------------------------

    function load(data) {
      var source = (data && Array.isArray(data.notes)) ? data.notes : [];
      keys = (data && typeof data.keys === 'number' && data.keys > 0) ? (data.keys | 0) : 4;

      // 复制一份再排序：不能改调用方的数组（页面可能还要拿它算别的东西）
      notes = source.slice();
      notes.sort(function (a, b) { return a.t - b.t; });

      maxHoldMs = 0;
      minTime = 0;
      maxTime = 0;
      for (var i = 0; i < notes.length; i += 1) {
        var note = notes[i];
        if (note.h !== null && note.h !== undefined && note.h > note.t) {
          var span = note.h - note.t;
          if (span > maxHoldMs) maxHoldMs = span;
        }
      }
      if (notes.length) {
        minTime = notes[0].t;
        maxTime = notes[notes.length - 1].t;
      }
      if (data) {
        if (typeof data.firstObject === 'number') minTime = data.firstObject;
        if (typeof data.lastObject === 'number') maxTime = data.lastObject;
      }

      buildDeviationPrefix();
      layout();
      return api;
    }

    // 每列一条前缀和。d 缺失（漏接没有时间偏移）不计入分母，否则均值会被稀释。
    function buildDeviationPrefix() {
      lanePreAbs = [];
      lanePreCnt = [];
      var n = notes.length;
      for (var k = 0; k < keys; k += 1) {
        var pa = new Float64Array(n + 1);
        var pc = new Int32Array(n + 1);
        for (var i = 0; i < n; i += 1) {
          pa[i + 1] = pa[i];
          pc[i + 1] = pc[i];
          if (((notes[i].c | 0) !== k)) continue;
          var d = notes[i].d;
          if (typeof d === 'number' && isFinite(d)) {
            pa[i + 1] += Math.abs(d);
            pc[i + 1] += 1;
          }
        }
        lanePreAbs.push(pa);
        lanePreCnt.push(pc);
      }
    }

    // 二分：返回第一个 t >= target 的下标
    function lowerBound(target) {
      var lo = 0;
      var hi = notes.length;
      while (lo < hi) {
        var mid = (lo + hi) >> 1;
        if (notes[mid].t < target) lo = mid + 1;
        else hi = mid;
      }
      return lo;
    }

    // -------------------------------------------------------------------------
    // 绘制：基础层
    // -------------------------------------------------------------------------

    function drawBackground() {
      ctx.fillStyle = bgGrad || effectiveBg();
      ctx.fillRect(0, 0, W, H);
    }

    function drawLanes() {
      // 每列铺一块纯色。舞台负责黑、音符负责亮：任何加在轨道上的高光
      // 都会与音符抢视线，而 osu 的舞台本来就是平的。
      ctx.fillStyle = laneFill;
      for (var i = 0; i < keys; i += 1) {
        ctx.fillRect(stageLeft + i * laneW, 0, laneW, H);
      }

      // 列间 1px 分隔线。轨道底色恒为近黑，分隔线固定用极淡的白（见 LANE_LINE）。
      // 用 fillRect 而不是 stroke：1px 线用 stroke 在 DPR 非整数时特别容易糊成 2px 灰线。
      ctx.fillStyle = theme.laneLine || LANE_LINE;
      for (var k = 1; k < keys; k += 1) {
        ctx.fillRect(Math.round(stageLeft + k * laneW) - 0.5, 0, 1, H);
      }
    }

    // 每列的实时平均 |偏差|：now 之前已判定的音符（漏接不算，没有时间偏移可言）
    function laneAvgAbs(lane, judgedIdx) {
      if (lane < 0 || lane >= keys) return 0;
      var cnt = lanePreCnt[lane][judgedIdx];
      if (!cnt) return 0;
      return lanePreAbs[lane][judgedIdx] / cnt;
    }

    // 轨道底色上的偏差柱：15% 红，从判定线往上长，越偏越高。
    // 画在轨道之后、判定线与音符之前 —— 是“背景”，不能盖住任何前景元素。
    function drawDeviationBars(now) {
      if (!opts.deviationBars || !notes.length || !(laneW > 0)) return;
      // +ε 把「恰好落在当前时刻」的音符也算进去：它正卡在判定线上，判定结果已经可知
      var judgedIdx = lowerBound(now + 0.001);
      if (judgedIdx <= 0) return;
      var maxH = judgeY * DEV_BAR_MAX_RATIO;
      var fill = withAlpha(theme.jmiss, DEV_BAR_ALPHA);
      for (var k = 0; k < keys; k += 1) {
        var avg = laneAvgAbs(k, judgedIdx);
        if (avg <= 0) continue;
        var h = clamp(avg / DEV_BAR_FULL_MS, 0, 1) * maxH * DEV_BAR_GAIN;
        // 放大后可能顶出画面：最多长到判定线以上全部可用高度
        if (h > judgeY) h = judgeY;
        if (h < 1) continue;
        ctx.fillStyle = fill;
        ctx.fillRect(stageLeft + k * laneW, judgeY - h, laneW, h);
      }
    }

    function drawJudgeLine() {
      // 判定区色带：只用主题色做很淡的渐变，既能标出判定带又不会抢音符的视线
      if (zoneGrad) {
        ctx.fillStyle = zoneGrad;
        ctx.fillRect(0, judgeY, W, ZONE_H);
      }

      // 线本身要对齐到整数像素行，否则 2px 的线会落在半个像素上，
      // 抗锯齿会把它糊成 3 行淡色（实测：精确命中的主题色像素只有 510 个而不是 1800 个）。
      // 时间换算仍然用未取整的 judgeY，这里只影响观感。
      var lineTop = Math.round(judgeY) - 1;

      // 判定线上方一点点辉光，让线看起来“发亮”而不是贴了一条色带
      ctx.fillStyle = withAlpha(theme.accent, 0.1);
      ctx.fillRect(0, lineTop - 4, W, 4);

      // 2px 主体线
      ctx.fillStyle = theme.accent;
      ctx.fillRect(0, lineTop, W, 2);
    }

    // 圆角矩形路径。roundRect 是 Chrome 99+ / Safari 16+ / FF 112+ 才有的，
    // 所以留一条手写兜底路径，老浏览器上只是圆角略钝，不会画不出来。
    function notePath(x, y, w, h, r) {
      if (typeof ctx.roundRect === 'function') {
        ctx.beginPath();
        ctx.roundRect(x, y, w, h, r);
        return;
      }
      var rr = Math.min(r, w * 0.5, h * 0.5);
      ctx.beginPath();
      ctx.moveTo(x + rr, y);
      ctx.lineTo(x + w - rr, y);
      ctx.arcTo(x + w, y, x + w, y + rr, rr);
      ctx.lineTo(x + w, y + h - rr);
      ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
      ctx.lineTo(x + rr, y + h);
      ctx.arcTo(x, y + h, x, y + h - rr, rr);
      ctx.lineTo(x, y + rr);
      ctx.arcTo(x, y, x + rr, y, rr);
      ctx.closePath();
    }

    function judgeColor(code) {
      // 标杆那一路用固定判定色：不跟主题变，和按下位置线是同一套配色，读起来一致
      if (opts.fixedJudgeColors) return PRESS_COLORS[code] || PRESS_COLORS[1];
      var key = JUDGE_KEYS[code] || 'j300';
      return theme[key] || THEME_DEFAULTS[key];
    }

    function catColor(cat) {
      var key = CAT_KEYS[cat];
      if (!key) return null;
      return theme[key] || THEME_DEFAULTS[key];
    }

    // -------------------------------------------------------------------------
    // 皮肤素材
    // -------------------------------------------------------------------------

    // 只有真正解码完成的图才能画：complete 为真但 naturalWidth 为 0 说明加载失败，
    // 直接 drawImage 会抛 InvalidStateError，把整个播放循环打断。
    function usableImg(img) {
      return !!(img && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
    }

    function noteImgFor(lane) {
      var img = noteImgs[lane];
      return usableImg(img) ? img : null;
    }

    // 有些皮肤会打包"占位"素材：本机这套 4K 皮肤的 LNBody.png 就是 128×1、整张全透明。
    // 这种图能加载、也能画，但画上去什么都没有 —— 直接用它会让长条凭空消失。
    // 所以加载后采样一次，整张 alpha 都是 0 就当没有素材，退回下一套画法。
    function imgIsBlank(img) {
      if (img.__blank !== undefined) return img.__blank;
      img.__blank = false;
      var cv = null;
      try { cv = global.document.createElement('canvas'); } catch (e) { return false; }
      if (!cv || !cv.getContext) return false;
      cv.width = 8;
      cv.height = 8;
      var c2 = cv.getContext('2d');
      if (!c2 || typeof c2.getImageData !== 'function') return false;
      try {
        c2.drawImage(img, 0, 0, 8, 8);
        var d = c2.getImageData(0, 0, 8, 8).data;
        var any = false;
        for (var i = 3; i < d.length; i += 4) {
          if (d[i] > 8) { any = true; break; }
        }
        img.__blank = !any;
      } catch (e) {
        img.__blank = false;   // 拿不到像素（环境限制）：按"有内容"处理，别误伤正常素材
      }
      return img.__blank;
    }

    // 长条素材能不能用：解码成功 + 不是空占位图
    function lnImgUsable(img) {
      return usableImg(img) && !imgIsBlank(img);
    }

    // 把长条素材压成一张 128×24 的小画布。
    // 皮肤的 LNBody 常是 128×1 的横切片，LNTail 可能是 128×40000 的超长图：
    // 每帧把原图拉成几百像素高，等于反复重采样一张超大纹理；而这类素材纵向是均匀的，
    // 取中间一行再拉伸，观感一样、代价小两个数量级。
    function prepareStrip(img) {
      if (!lnImgUsable(img)) return null;
      var sw = img.naturalWidth | 0;
      var sh = img.naturalHeight | 0;
      if (!sw || !sh) return null;
      var cv = null;
      try { cv = global.document.createElement('canvas'); } catch (e) { return null; }
      if (!cv || !cv.getContext) return null;
      cv.width = Math.max(8, Math.min(128, sw));
      cv.height = 24;
      var c2 = cv.getContext('2d');
      if (!c2 || typeof c2.drawImage !== 'function') return null;
      try {
        // 取中间那一条：本皮肤 LNTail 顶部有 100px 透明过渡，从头取会得到空白
        var sy = sh > 8 ? Math.floor(sh / 2) : 0;
        c2.drawImage(img, 0, sy, sw, 1, 0, 0, cv.width, cv.height);
      } catch (e) {
        return null;
      }
      return cv;
    }

    // 长条身体用哪张：优先 LNBody；它是空占位图（或画不出）时退到 LNTail ——
    // 有些皮肤把整条长条的实体都放在 LNTail 里，LNBody 只留一个空文件占位。
    function lnBodyTexture() {
      return lnBodyTex || lnTailTex || null;
    }

    // 尾巴只有在"身体与尾巴是两套不同素材"时才单独画：
    // 本皮肤的身体就是 LNTail，再拿它画一次尾巴只是重复覆盖，没有意义。
    function lnTailTexture() {
      return (lnBodyTex && lnTailTex) ? lnTailTex : null;
    }

    // 异步加载一张图，加载好了主动补一帧（否则要等下一次播放才看得到素材）
    function loadImage(url, onReady) {
      var Ctor = (typeof global.Image === 'function') ? global.Image : null;
      if (!Ctor || !url) return null;
      var img = new Ctor();
      img.onload = function () {
        try { if (onReady) onReady(); } catch (err) { /* 回调抛错不能拖垮渲染 */ }
      };
      img.onerror = function () {
        // 素材 404 是常态（皮肤里没有某列的图），静默回退到色块即可
        if (typeof console !== 'undefined' && console.warn) console.warn('[ManiaPlayer] 音符素材加载失败：' + url);
      };
      img.src = url;
      return img;
    }

    // 素材的绘制尺寸：按原图比例定高，再夹到上面那几条限制里
    function imageBox(img, w) {
      var ratio = img.naturalHeight / img.naturalWidth;
      var h = w * ratio;
      var lo = w * IMG_MIN_H_RATIO;
      var hi = Math.min(w * IMG_MAX_H_RATIO, IMG_MAX_LANE_RATIO * (laneW || w));
      if (h < lo) h = lo;
      if (h > hi) h = hi;
      return h;
    }

    function drawImageCentered(img, cx, cy, w) {
      var h = imageBox(img, w);
      ctx.drawImage(img, cx - w * 0.5, cy - h * 0.5, w, h);
      return h;
    }

    // -------------------------------------------------------------------------
    // 绘制：判定闪光
    // -------------------------------------------------------------------------

    // 打击特效：主色块放在判定线**下方**。
    //
    // 这一版是被实机截图打回来的：4K 皮肤的音符几乎占满整列宽，判定线附近从上方到线上
    // 全被音符盖住，任何贴在线上画的特效都看不见。真正空着的地方只有判定线以下那一小条
    // （已判完的音符 80ms 后就消失了），所以主色块必须画在那里。
    //   1) 下方主色块 —— 整列宽，从上到下渐隐，是"这一下打在哪一列"的主要读数；
    //   2) 色块下缘向两侧扫开的细线 —— 速度感，衰减更快；
    //   3) 判定线上一条细亮线 —— 音符较小时能看清"有没有踩准"，被挡住也不影响前两层。
    function drawImpact(cx, p, color, laneW) {
      var alpha = (1 - p) * (1 - p);
      if (alpha <= 0.01) return;
      var ease = 1 - (1 - p) * (1 - p);      // 先快后慢：起手像被"打"出去，收尾稳住
      var half = laneW * 0.5;
      var top = judgeY + 3;
      var h = 24 + 10 * ease;

      ctx.save();
      ctx.globalCompositeOperation = 'lighter';

      // 1) 主色块
      var body = ctx.createLinearGradient(0, top, 0, top + h);
      body.addColorStop(0, withAlpha(color, 0.90 * alpha));
      body.addColorStop(0.55, withAlpha(color, 0.34 * alpha));
      body.addColorStop(1, withAlpha(color, 0));
      ctx.fillStyle = body;
      ctx.fillRect(cx - half * 0.88, top, laneW * 0.88, h);

      // 2) 两侧竖括号 + 底边横杠。
      // 只靠填充色的话，白色判定和白色音符会糊成一片；加一圈"硬边"之后，
      // 不管底下是黑背景还是白色音符，轮廓都在。
      var arm = 15 * (0.6 + 0.4 * ease);
      var armW = Math.max(1.6, 2.6 * (1 - ease * 0.5));
      var edge = withAlpha(color, 0.95 * alpha);
      ctx.fillStyle = edge;
      ctx.fillRect(cx - half * 0.92, top, armW, arm);
      ctx.fillRect(cx + half * 0.92 - armW, top, armW, arm);
      ctx.fillRect(cx - half * 0.92, top + arm, laneW * 0.92, armW * 0.8);

      // 3) 下缘的横扫
      var sweep = half * (0.5 + 0.95 * ease);
      var line = ctx.createLinearGradient(cx - sweep, 0, cx + sweep, 0);
      line.addColorStop(0, withAlpha(color, 0));
      line.addColorStop(0.5, withAlpha(color, 0.6 * alpha));
      line.addColorStop(1, withAlpha(color, 0));
      ctx.fillStyle = line;
      ctx.fillRect(cx - sweep, top + arm, sweep * 2, 1.6);

      // 4) 判定线上的细亮线
      ctx.fillStyle = withAlpha(color, 0.85 * alpha);
      ctx.fillRect(cx - half * 0.8, judgeY - 1, laneW * 0.8, 2);

      ctx.restore();
    }

    // -------------------------------------------------------------------------
    // 绘制：音符
    // -------------------------------------------------------------------------

    function drawEmptyState() {
      // 规格要求不用文字：一个圆环 + 一个圆点 + 一条短横线，
      // 形状本身就在暗示“这里会有一条下落轨道”。
      var cx = W * 0.5;
      var cy = H * 0.44;

      ctx.save();
      ctx.globalAlpha = EMPTY_ALPHA;

      ctx.strokeStyle = theme.accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy - 10, 17, 0, Math.PI * 2);
      ctx.stroke();

      ctx.fillStyle = theme.accent;
      ctx.beginPath();
      ctx.arc(cx, cy - 10, 4, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = theme.laneLine || LANE_LINE;
      ctx.fillRect(cx - 22, cy + 20, 44, 2);

      ctx.fillStyle = theme.laneLine || LANE_LINE;
      ctx.fillRect(cx - 12, cy + 30, 24, 2);

      ctx.restore();
    }

    // 长按的身体与尾帽。只有长按才有这一段，和"音符本体"是两件事：
    // 本体在任何判定下都要画，身体是从尾端到头部的一条矩形。
    function drawHoldBody(note, g, color) {
      var tailY = judgeY - (note.h - g.now) * pxPerMs;
      // 上端允许冲出画布（超长 LN 被裁掉是正常的），下端收进头部矩形里 1px，
      // 这样身体和头部之间不会出现一条亮缝
      var bodyTop = Math.max(tailY, -8);
      var bodyBottom = g.headY - 1;
      var bodyTex = lnBodyTexture();
      if (bodyBottom - bodyTop > 1) {
        if (bodyTex) {
          // 素材左右自带透明留白（本皮肤实体只占中间 50%），所以按整个音符宽画，
          // 视觉上的宽度自动和音符头对上，不必再手动缩到 64%。
          ctx.save();
          ctx.globalAlpha = 0.85;
          ctx.drawImage(bodyTex, g.x, bodyTop, g.noteW, bodyBottom - bodyTop);
          ctx.restore();
          // 判定色还得看得出来：沿中轴补一条很淡的色线（素材本身是白的）
          ctx.fillStyle = withAlpha(color, 0.45);
          ctx.fillRect(g.cx - 1, bodyTop, 2, bodyBottom - bodyTop);
        } else {
          ctx.fillStyle = withAlpha(color, HOLD_BODY_ALPHA);
          notePath(g.x + g.noteW * 0.18, bodyTop, g.noteW * 0.64, bodyBottom - bodyTop, HOLD_RADIUS * 0.6);
          ctx.fill();
          // 长条中轴线提亮一档，避免整块颜色在深底上显得发闷
          ctx.fillStyle = withAlpha(color, 0.28);
          ctx.fillRect(g.cx - 1, bodyTop, 2, bodyBottom - bodyTop);
        }
      }

      // 尾帽：皮肤给了独立的 LNTail 才画（画在身体之后，盖住尾端接缝）
      var tailTex = lnTailTexture();
      if (tailTex) {
        var capH = Math.min(g.noteW * 0.45, Math.max(4, bodyBottom - bodyTop));
        var capTop = tailY - capH * 0.5;
        if (capTop + capH > 0) {
          ctx.save();
          ctx.globalAlpha = 0.9;
          ctx.drawImage(tailTex, g.x, capTop, g.noteW, capH);
          ctx.restore();
        }
      }
    }

    // 音符本体：皮肤素材优先、没有就色块；再叠选中环、MISS 环、长按端帽与归因色条。
    // 后面这些都按「实际盒子」boxH / boxTop 定位 —— 素材的高度和 NOTE_H 并不一样。
    function drawNoteHead(note, g, color, code) {
      var headTop = g.headY - NOTE_H * 0.5;
      var boxH = NOTE_H;
      var boxTop = headTop;
      if (g.img) {
        boxH = drawImageCentered(g.img, g.cx, g.headY, g.noteW);
        boxTop = g.headY - boxH * 0.5;
      } else {
        ctx.fillStyle = color;
        notePath(g.x, headTop, g.noteW, NOTE_H, NOTE_RADIUS);
        ctx.fill();
      }

      // 被点中的失误：套一圈青色描边 + 外发光。不弹窗，靠这个"选中的样子"告诉用户点到了谁。
      var picked = selectedKey && Math.abs(note.t - selectedKey.t) < 0.5 && (note.c | 0) === selectedKey.c;
      if (picked) {
        ctx.save();
        ctx.strokeStyle = theme.accent;
        ctx.lineWidth = 2.5;
        ctx.shadowColor = theme.accent;
        ctx.shadowBlur = 14;
        notePath(g.x - 3, boxTop - 3, g.noteW + 6, boxH + 6, NOTE_RADIUS + 3);
        ctx.stroke();
        ctx.restore();
      }

      // MISS 加一圈亮描边：这是“漏接”的强信号，只靠颜色不够醒目
      // （用了素材就没有判定色可看，所以 MISS 还要额外叠一层红）
      if (code === 5) {
        ctx.strokeStyle = withAlpha('#ffffff', 0.85);
        ctx.lineWidth = 2;
        notePath(g.x + 1, boxTop + 1, g.noteW - 2, boxH - 2, Math.min(NOTE_RADIUS, boxH * 0.5) - 1);
        ctx.stroke();
        if (g.img) {
          ctx.save();
          ctx.globalAlpha = 0.4;
          ctx.fillStyle = theme.jmiss;
          notePath(g.x + 1, boxTop + 1, g.noteW - 2, boxH - 2, Math.min(NOTE_RADIUS, boxH * 0.5) - 1);
          ctx.fill();
          ctx.restore();
        }
      } else if (note.e) {
        // 有瑕疵但判上了：描一圈同色微亮边，让“瑕疵点”在密集段也能被扫到
        ctx.strokeStyle = withAlpha(shade(color, 1.8), 0.75);
        ctx.lineWidth = 1;
        notePath(g.x + 0.5, boxTop + 0.5, g.noteW - 1, boxH - 1, Math.min(NOTE_RADIUS, boxH * 0.5));
        ctx.stroke();
      }

      // 长按头部的 4px 亮端帽（在判定这一侧，标出“按下的瞬间”）
      if (note.h !== null && note.h !== undefined && note.h > note.t) {
        ctx.fillStyle = shade(color, 1.55);
        notePath(g.x + 2, boxTop + boxH - CAP_H - 1, g.noteW - 4, CAP_H, CAP_H * 0.5);
        ctx.fill();
      }

      // 归因色条：贴在音符右侧，4px 宽，是“这一个属于哪种失误”的标记
      var cat = catColor(note.cat);
      if (cat) {
        ctx.fillStyle = cat;
        notePath(g.x + g.noteW - CAT_BAR_W - 2, boxTop + 3, CAT_BAR_W, Math.max(2, boxH - 6), CAT_BAR_W * 0.5);
        ctx.fill();
      }
    }

    function drawNotes(now) {
      var n = notes.length;
      if (n === 0) {
        drawEmptyState();
        return;
      }

      var onlyErrors = opts.judgementFilter === 'errors';
      var pastEdge = now - PAST_MS;
      var futureEdge = now + opts.scrollMs + 1; // +1 让刚出现在顶端的音符也能进来

      // 二分起点要把“最长长按”退回去：一个很长的 LN 的头部可能早就过了
      // 判定线，但它的身体还在画面上（正在按住），不能因为 t < pastEdge 就漏掉。
      // 另外判定闪光（120ms）比音符尾巴（80ms）长，所以起点还要再兜住闪光那 40ms。
      var startT = Math.min(pastEdge - maxHoldMs, now - IMPACT_MS - 1);
      var i = lowerBound(startT);

      for (; i < n; i += 1) {
        var note = notes[i];
        if (note.t > futureEdge) break; // 升序，后面的更远，可以停

        var isHold = note.h !== null && note.h !== undefined && note.h > note.t;
        var activeHold = isHold && note.h > now;

        var age = now - note.t;
        var flashing = opts.showJudgements && age >= 0 && age <= IMPACT_MS;

        // 已过去：超过 80ms 就整条不画。但“正在按住的长按”不算已过去
        // （它的身体还连着判定线），否则长条会在按住的中途突然消失。
        // 音符本体没了、闪光还没结束的那 40ms 仍然要画，否则闪光会被腰斩。
        var headVisible = !(note.t < pastEdge && !activeHold);
        if (!headVisible && !flashing) continue;

        // errors 过滤：只留 e 为真的音符（规格里就是按 e 字段判断）
        if (onlyErrors && !note.e) continue;

        var code = (note.j >= 0 && note.j <= 5) ? (note.j | 0) : 0;
        var color = judgeColor(code);
        var lane = note.c | 0;
        if (lane < 0) lane = 0;
        if (lane >= keys) lane = keys - 1;

        var laneLeft = stageLeft + lane * laneW;
        var cx = laneLeft + laneW * 0.5;
        var x = cx - noteW * 0.5;

        if (headVisible) {
          // 时间 -> y（头部的判定位置）
          var headY = judgeY - (note.t - now) * pxPerMs;
          if (activeHold) headY = Math.min(headY, judgeY); // 按住时钉在判定线
          // 皮肤素材（有就画素材，没有就退回色块）
          var g = { x: x, cx: cx, noteW: noteW, headY: headY, now: now, img: noteImgFor(lane) };
          if (isHold) drawHoldBody(note, g, color);
          drawNoteHead(note, g, color, code);
        }

        // 判定闪光：只在“刚判完”这 120ms 内出现（MISS 也给一个，提示这一下没接上）
        if (flashing) drawImpact(cx, age / IMPACT_MS, color, laneW);
      }
    }

    // -------------------------------------------------------------------------
    // 渲染入口
    // -------------------------------------------------------------------------

    // 玩家**实际按下**的位置：每个有击打记录的物件，在 (note.t + delta) 处画一小段横条。
    // 样式刻意做得很轻（2px、低透明度、只占半个列宽），压在音符底下，不遮挡任何东西；
    // 用途是看"按早了还是按晚了、偏多少"——音符是谱面给的位置，横条是手落下的位置。
    function drawPresses(now) {
      if (!notes.length || !(pxPerMs > 0)) return;
      var left = now - PAST_MS;
      var right = now + opts.scrollMs;
      ctx.save();
      ctx.globalAlpha = 0.5;
      // 按下位置 = 音符头 + 误差，误差通常几百毫秒以内；从可见窗口往前退一点开始，
      // 不再每帧从第 0 条扫起（几万物件时这一步能吃掉大半帧）。
      var from = lowerBound(left - 400);
      for (var i = from; i < notes.length; i += 1) {
        var n = notes[i];
        if (n.t > right) break;
        if (n.t < left) continue;
        if (n.d === null || n.d === undefined) continue; // 漏接没有"按下"
        var pressT = n.t + n.d;
        var y = judgeY - (pressT - now) * pxPerMs;
        if (y < -4 || y > H + 4) continue;
        var lane = n.c | 0;
        if (lane < 0 || lane >= keys) continue;
        // 画得比音符略宽：音符盖住中间那段，两侧露出一截小"翅膀"，
        // 既不挡音符，又能看出击打线相对音符偏上还是偏下。
        var w = laneW * 0.98;
        var cx = stageLeft + lane * laneW + laneW * 0.5;
        var code = (n.j >= 0 && n.j <= 5) ? (n.j | 0) : 1;
        ctx.fillStyle = PRESS_COLORS[code] || PRESS_COLORS[1];
        ctx.fillRect(cx - w / 2, y - 1.5, w, 3);
      }
      ctx.restore();
    }

    // 长条的**松手位置**：尾判同样有误差（提前松手/松太晚都会掉判定），
    // 但它的位置不在音符头上，得单独画。样式比"按下"更淡更细，避免和按下线混淆。
    function drawReleases(now) {
      if (!notes.length || !(pxPerMs > 0)) return;
      var left = now - PAST_MS;
      var right = now + opts.scrollMs;
      ctx.save();
      ctx.globalAlpha = 0.55;
      // 松手位置跟着长条尾走：一个很长的 LN 头可能早就过去了，所以回退 maxHoldMs
      var from = lowerBound(Math.max(0, left - maxHoldMs - 400));
      for (var i = from; i < notes.length; i += 1) {
        var n = notes[i];
        if (n.t > right) break;
        if (n.t < left) continue;
        if (n.h === null || n.h === undefined || n.rd === null || n.rd === undefined) continue;
        var relT = n.h + n.rd;
        var y = judgeY - (relT - now) * pxPerMs;
        if (y < -4 || y > H + 4) continue;
        var lane = n.c | 0;
        if (lane < 0 || lane >= keys) continue;
        var dotw = laneW * 0.62;
        var dcx = stageLeft + lane * laneW + laneW * 0.5;
        ctx.fillStyle = n.rd < 0 ? RELEASE_EARLY : RELEASE_LATE;
        ctx.fillRect(dcx - dotw / 2, y - 1, dotw, 2);
      }
      ctx.restore();
    }

    // 打击音：决定"这一帧该响哪几个音"，真正出声由 HitSound 负责。
    //
    // 两种口径的差别就是用户要听的东西：
    //   planned（正解音）—— 一律用谱面时间 note.t，听起来是这张图本来的节奏；
    //   actual（实际击打）—— 用 note.t + note.d，也就是手真正落下的时刻；
    //                        d 缺失说明这一下没打（MISS），那就没有声音。
    // 后者放出来会明显比前者"散"，抢拍会提前、拖拍会滞后，位置偏得越多听起来越乱。
    function tickHitsound(now) {
      var hs = opts.hitsound;
      if (!hs || opts.hitsoundMode === 'off') { audioFrom = now; return; }
      if (audioFrom === null) { audioFrom = now; return; }

      var from = audioFrom;
      audioFrom = now;
      if (!(now > from)) return;
      // 跳转、首帧、掉帧超过半秒：不补播。补播会把一整段音在几十毫秒里倒出来，像爆音。
      if (now - from > 400) return;

      var actual = opts.hitsoundMode === 'actual';
      var hi = now + 260;
      var i = lowerBound(Math.min(from, now) - 260);
      for (; i < notes.length; i += 1) {
        var n = notes[i];
        if (n.t > hi) break;
        var off = 0;
        if (actual) {
          // 实际击打模式：没有偏差记录 = 这一下根本没打出去，静音（这是"听出漏接"的关键）
          if (typeof n.d !== 'number' || !isFinite(n.d)) continue;
          off = n.d;
        }
        var trig = n.t + off;
        if (trig > from && trig <= now) {
          var hold = n.h !== null && n.h !== undefined && n.h > n.t;
          hs.play(hold ? 'hold' : (n.j === 5 ? 'miss' : 'hit'));
        }
      }
    }

    function render() {
      if (destroyed) return;
      if (needMeasure) measure();
      if (!(W > 0 && H > 0)) return; // 还没布局，画了也是白画

      var now = timeMs;
      drawBackground();
      drawLanes();
      drawDeviationBars(now);
      drawJudgeLine();
      if (opts.showPresses) {
        drawPresses(now);   // 击打位置在音符之前画：压在下面，不会遮住音符
        drawReleases(now);  // 长条松手位置（更淡更细，和按下线区分）
      }
      drawNotes(now);
      tickHitsound(now);
    }

    // -------------------------------------------------------------------------
    // 交互
    // -------------------------------------------------------------------------

    function pointOf(ev) {
      var rect = (typeof canvasEl.getBoundingClientRect === 'function')
        ? canvasEl.getBoundingClientRect()
        : { left: 0, top: 0 };
      var clientX = (typeof ev.clientX === 'number') ? ev.clientX : (ev.offsetX || 0);
      var clientY = (typeof ev.clientY === 'number') ? ev.clientY : (ev.offsetY || 0);
      return { x: clientX - (rect.left || 0), y: clientY - (rect.top || 0) };
    }

    function timeAtY(y) {
      if (!(pxPerMs > 0)) return timeMs;
      return timeMs + (judgeY - y) / pxPerMs;
    }

    // 点击定位：把 y 反算成时间。夹在谱面范围内，免得点画布顶部
    // 得到“当前时间 + 好几秒”这种越界值（跳到播放器终点之外会让页面困惑）。
    function seekFromEvent(ev) {
      var pt = pointOf(ev);
      var t = timeAtY(pt.y);
      if (notes.length) {
        var lo = minTime - 200;
        var hi = maxTime + 600;
        t = clamp(t, lo, hi);
      }
      lastSeekAt = Date.now();
      emit('seek', t);
    }

    function nearestNoteAt(pt) {
      if (!notes.length || !(pxPerMs > 0)) return null;
      var lane = Math.floor((pt.x - stageLeft) / laneW);
      if (lane < 0 || lane >= keys) return null;

      var target = timeAtY(pt.y);
      // 只在“半行高”对应的时间范围内找，超出就算没命中
      var tolMs = (NOTE_H * 0.6) / pxPerMs;
      var from = lowerBound(target - tolMs);
      var best = null;
      var bestDelta = Infinity;
      for (var i = from; i < notes.length; i += 1) {
        var note = notes[i];
        if (note.t > target + tolMs) break;
        if ((note.c | 0) !== lane) continue;
        var delta = Math.abs(note.t - target);
        if (delta < bestDelta) {
          bestDelta = delta;
          best = i;
        }
      }
      return best;
    }

    function emit(type, payload) {
      var list = listeners[type];
      if (!list) return;
      for (var i = 0; i < list.length; i += 1) {
        try {
          list[i](payload);
        } catch (err) {
          // 回调是页面写的，抛错不能把整个播放循环带崩
          if (typeof console !== 'undefined' && console.error) console.error('[ManiaPlayer] ' + type + ' 回调抛错', err);
        }
      }
    }

    function handleMouseDown(ev) {
      // 点在失误物件上时交给页面弹说明，**不跳转** ——
      // 否则"点一下看看原因"会顺手把画面挪到别处，反而看不清。
      var pt = pointOf(ev);
      var hit = api.pickAt ? api.pickAt(pt.x, pt.y) : null;
      if (hit) {
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        // 顺手记一下时间：浏览器随后还会补一个 click，handleClick 的 400ms 去重窗口
        // 会把它吃掉 —— 否则"点失误看原因"会顺手 seek 一次，而 seek 是会自动播放的。
        lastSeekAt = Date.now();
        emit('pick', { note: hit, x: pt.x, y: pt.y });
        return;
      }
      dragging = true;
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      seekFromEvent(ev);
    }

    function handleMouseMove(ev) {
      var pt = pointOf(ev);
      if (dragging) {
        seekFromEvent(ev);
        return;
      }
      var index = nearestNoteAt(pt);
      emit('hover', {
        noteIndex: index,
        note: index === null ? null : notes[index],
        time: timeAtY(pt.y),
        x: pt.x,
        y: pt.y
      });
    }

    function handleMouseUp() {
      dragging = false;
    }

    // click 兜底：某些环境（或是页面对 canvas 做了 pointer-events 包装）只发 click。
    // 用时间窗去重，避免“按下 + 抬起”触发两次 seek 跳两次。
    function handleClick(ev) {
      if (Date.now() - lastSeekAt < 400) return;
      // 兜底：万一 mousedown 没送到（或去重窗口过了），点中失误物件也只发 pick，不 seek
      var pt = pointOf(ev);
      var hit = api.pickAt ? api.pickAt(pt.x, pt.y) : null;
      if (hit) {
        lastSeekAt = Date.now();
        emit('pick', { note: hit, x: pt.x, y: pt.y });
        return;
      }
      seekFromEvent(ev);
    }

    function handleLeave() {
      dragging = false;
      emit('hover', { noteIndex: null, note: null, time: timeMs, x: -1, y: -1 });
    }

    canvasEl.addEventListener('mousedown', handleMouseDown);
    canvasEl.addEventListener('mousemove', handleMouseMove);
    canvasEl.addEventListener('click', handleClick);
    canvasEl.addEventListener('mouseleave', handleLeave);
    // mouseup 挂在 window 上：拖出画布再松手也能结束拖动
    var upTarget = (canvasEl.ownerDocument && canvasEl.ownerDocument.defaultView) || global;
    if (upTarget && typeof upTarget.addEventListener === 'function') {
      upTarget.addEventListener('mouseup', handleMouseUp);
    }

    // -------------------------------------------------------------------------
    // 对外 API
    // -------------------------------------------------------------------------

    var api = {
      load: load,

      // 设定时间但**不**重绘：绘制节奏由页面决定（配合 requestAnimationFrame，
      // 或在暂停时只在你需要的时候画一帧），这里自己画会导致一帧画多次。
      setTime: function (ms) {
        var v = Number(ms);
        if (isFinite(v)) timeMs = v;
        return api;
      },

      render: render,

      // 设置/清除"被选中的失误物件"（只影响绘制，重绘由页面调 render 触发）
      setSelected: function (t, c) {
        selectedKey = (t === null || t === undefined) ? null : { t: Number(t), c: Number(c) };
        return api;
      },

      // 点选：把画布坐标反算成「第几列 + 哪个时间点」，返回压在那里的**失误**物件。
      // 只认失误物件 —— 普通音符点下去没有可解释的东西，反而容易误触。
      // 同一行有多个物件时靠列坐标区分（lane = floor((x - stageLeft) / laneW)）。
      pickAt: function (x, y) {
        if (!notes.length || !(laneW > 0) || !(pxPerMs > 0)) return null;
        var lane = Math.floor((x - stageLeft) / laneW);
        if (lane < 0 || lane >= keys) return null;
        var t = timeAtY(y);
        var tolMs = 20 / pxPerMs;   // 手指没那么准，给 20px 的容差
        var best = null;
        var bestDist = Infinity;
        for (var i = 0; i < notes.length; i++) {
          var n = notes[i];
          if ((n.c | 0) !== lane || !n.e) continue;
          var isHold = n.h !== null && n.h !== undefined && n.h > n.t;
          var d;
          if (isHold) {
            // 长条：头到尾之间都算（身体也在画面上），离头/尾的距离取小
            d = t < n.t ? n.t - t : (t > n.h ? t - n.h : 0);
          } else {
            d = Math.abs(t - n.t);
          }
          if (d <= tolMs && d < bestDist) {
            best = n;
            bestDist = d;
          }
        }
        return best;
      },

      resize: function () {
        measure();
        return api;
      },

      setOptions: function (partial) {
        if (partial && typeof partial === 'object') {
          mergeOptions(partial);
          if (partial.theme) applyThemePatch(theme, partial.theme);
          // scrollMs 变了 -> 每毫秒像素变了；主题变了 -> 渐变得重建
          layout();
        }
        return api;
      },

      setTheme: function (patch) {
        applyThemePatch(theme, patch);
        layout();
        return api;
      },

      // 换成皮肤素材来画音符。urls 是「每列一张图」的 URL 数组（某一列传 null 就退回色块），
      // lnUrl / lnTailUrl 是长按条的身体与尾巴（都可以缺）。
      // 图是异步加载的：加载期间照旧用色块，加载完自动补一帧。
      setNoteImages: function (urls, lnUrl, lnTailUrl) {
        var list = Array.isArray(urls) ? urls : [];
        var count = Math.max(keys, list.length);
        var onReady = function () {
          if (destroyed) return;
          lnBodyTex = prepareStrip(lnBodyImg);
          lnTailTex = prepareStrip(lnTailImg);
          render();
        };
        noteImgs = [];
        for (var i = 0; i < count; i += 1) {
          noteImgs.push(loadImage(list[i], onReady));
        }
        lnBodyImg = loadImage(lnUrl, onReady);
        lnTailImg = loadImage(lnTailUrl, onReady);
        // 图片可能已经在缓存里（complete 为真），先试着直接处理一次
        lnBodyTex = prepareStrip(lnBodyImg);
        lnTailTex = prepareStrip(lnTailImg);
        render();
        return api;
      },

      // 换回色块（比如用户关掉皮肤，或者新的一局还没拿到素材）
      clearNoteImages: function () {
        noteImgs = [];
        lnBodyImg = null;
        lnTailImg = null;
        lnBodyTex = null;
        lnTailTex = null;
        render();
        return api;
      },

      getTimeAtY: function (y) {
        return timeAtY(y);
      },

      on: function (type, fn) {
        if (!listeners[type] || typeof fn !== 'function') return function () {};
        listeners[type].push(fn);
        var list = listeners[type];
        return function off() {
          var idx = list.indexOf(fn);
          if (idx >= 0) list.splice(idx, 1);
        };
      },

      destroy: function () {
        destroyed = true;
        canvasEl.removeEventListener('mousedown', handleMouseDown);
        canvasEl.removeEventListener('mousemove', handleMouseMove);
        canvasEl.removeEventListener('click', handleClick);
        canvasEl.removeEventListener('mouseleave', handleLeave);
        if (upTarget && typeof upTarget.removeEventListener === 'function') {
          upTarget.removeEventListener('mouseup', handleMouseUp);
        }
        listeners.seek = [];
        listeners.hover = [];
        notes = [];
        lanePreAbs = [];
        lanePreCnt = [];
        noteImgs = [];
        lnBodyImg = null;
        bgGrad = null;
        zoneGrad = null;
        laneFill = THEME_DEFAULTS.laneBg;
      },

      // 只读视图，方便页面自己算进度条等（不是必需接口，但比让页面猜好）
      getState: function () {
        return {
          time: timeMs,
          keys: keys,
          noteCount: notes.length,
          judgeY: judgeY,
          pxPerMs: pxPerMs,
          width: W,
          height: H,
          dpr: dpr,
          devBarGain: DEV_BAR_GAIN,   // 偏差柱的视觉增益（测试对着它算理论高度，别写死）
          showPresses: opts.showPresses,
          fixedJudgeColors: opts.fixedJudgeColors,
          scrollMs: opts.scrollMs,
          judgementFilter: opts.judgementFilter,
          showJudgements: opts.showJudgements,
          deviationBars: opts.deviationBars,
          hasNoteImages: noteImgs.some(usableImg),
          minTime: minTime,
          maxTime: maxTime
        };
      }
    };

    // 建好就把尺寸量一次：页面常常在 canvas 已挂载后才 create，
    // 提前测量能让“先 create 再 load 再 render”的调用顺序直接可用。
    measure();

    return api;
  }

  global.ManiaPlayer = { create: create };
})(typeof window !== 'undefined' ? window : globalThis);
