/* 谱面段位估算（浏览器端）。
 *
 * 做法：拿服务端给的 .osu 原文，用 Sunny 算星级，再按 RC / LN 两张标尺查段位。
 * 算法与标尺来自 osumania_map_analyser（MIT），原样放在 vendor/mma 下。
 *
 * 为什么放前端算：这套估算器本来就是纯 JS，而打包里只有 Python 运行时，
 * 服务端跑不了 JS；前端本来是零构建的原生 JS，加一个 module 就能用。
 *
 * 关键约定：倍率要传进来。开 DT 时按 1.5 倍速算，密度与星级都会跟着变高，
 * 这才是玩家那一局实际面对的难度；谱面本身的 MSD 不受倍率影响，两者会并存显示。
 */
import { calculate as calculateRC } from "../vendor/mma/rework/sunnyAlgorithm.js";
import { calculateLN } from "../vendor/mma/rework/sunnyWindowAlgorithm.js";
import { estDiff2 } from "../vendor/mma/estimator/reworkEstimatorUtils.js";

const cache = new Map(); // key: session + 倍率

function normalize(result) {
  // 对方有两种返回形态：数组 [star, lnRatio, columnCount] 或对象 {star, lnRatio, columnCount}
  if (Array.isArray(result)) {
    return { star: Number(result[0]), lnRatio: Number(result[1]), keys: Number(result[2]) };
  }
  if (result && typeof result === "object") {
    return {
      star: Number(result.star),
      lnRatio: Number(result.lnRatio),
      keys: Number(result.columnCount),
    };
  }
  return { star: NaN, lnRatio: NaN, keys: NaN };
}

async function beatmapText(session) {
  const res = await fetch("/api/beatmap-text?session=" + encodeURIComponent(session));
  if (!res.ok) throw new Error("取谱面原文失败（HTTP " + res.status + "）");
  const data = await res.json();
  if (!data || !data.ok || typeof data.text !== "string") {
    throw new Error((data && data.error) || "谱面原文为空");
  }
  return data.text;
}

async function estimate(session, rate) {
  if (!session) return { ok: false, reason: "没有会话" };
  const speedRate = Number(rate) > 0 ? Number(rate) : 1;
  const key = session + "|" + speedRate.toFixed(3);
  if (cache.has(key)) return cache.get(key);

  let text;
  try {
    text = await beatmapText(session);
  } catch (e) {
    return { ok: false, reason: (e && e.message) || "取谱面失败" };
  }

  const t0 = performance.now();
  let rc;
  let lnRaw;
  try {
    rc = normalize(calculateRC(text, speedRate, null, null, {}, null));
    lnRaw = calculateLN(text, speedRate, null, null, {}, null);
  } catch (e) {
    return { ok: false, reason: "估算器解析谱面失败：" + ((e && e.message) || e) };
  }

  if (!Number.isFinite(rc.star) || !Number.isFinite(rc.lnRatio) || !Number.isFinite(rc.keys)) {
    return { ok: false, reason: "估算器没有返回有效星级" };
  }
  // 本项目只做 4K：其他键数直接放弃，避免拿错标尺
  if (rc.keys !== 4) {
    return { ok: false, reason: "只支持 4K 谱面（这张是 " + rc.keys + "K）" };
  }

  const noLn = !!(lnRaw && lnRaw.NoLN);
  const ln = noLn ? { star: 0 } : normalize(lnRaw);
  const lnStar = Number.isFinite(ln.star) ? ln.star : 0;
  const lnPartsRatio = Number((lnRaw && lnRaw.lnPartsRatio) || 0);

  // 与对方 runSunnyWindowEstimatorFromText 的判定保持一致
  const showLN =
    rc.lnRatio > 0.15 || (lnStar > 1.5 && lnStar > rc.star * 0.7) || lnPartsRatio > 0.3;

  let label = "";
  try {
    label = estDiff2(rc.star, showLN ? lnStar : 0, rc.keys, false);
  } catch (e) {
    return { ok: false, reason: "段位查表失败：" + ((e && e.message) || e) };
  }

  const out = {
    ok: true,
    label: label,
    rcStar: rc.star,
    lnStar: showLN ? lnStar : 0,
    lnRatio: rc.lnRatio,
    rate: speedRate,
    ms: Math.round(performance.now() - t0),
  };
  cache.set(key, out);
  return out;
}

window.Estimator = { estimate };
