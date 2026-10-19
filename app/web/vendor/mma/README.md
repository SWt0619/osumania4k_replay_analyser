# 第三方代码：难度估算器

这些文件原样来自 [LeoBlackMT/osumania_map_analyser](https://github.com/LeoBlackMT/osumania_map_analyser)（MIT 许可，全文见同目录 `LICENSE`），
用于把谱面换算成 Sunny 星级，再按 RC / LN 两张标尺查段位。

**只做了复制，没有改动任何一行。** 这样做的好处是对方的算法更新时，整目录覆盖即可。

复制范围（对应原仓库 `ManiaMapAnalyser by Leo_Black/js/` 下）：

```
parser/osuFileParser.js          谱面解析（同时算出 lnRatio）
parser/noteColumn.js             列号换算
rework/sunnyAlgorithm.js         Sunny 星级（RC）
rework/sunnyWindowAlgorithm.js   calculateLN：独立 LN 星级
rework/reworkMathCore.js         数学工具
estimator/reworkEstimatorUtils.js  estDiff2：按星级查段位表
estimator/intervals/*.js         各键数的 RC / LN 段位标尺
```

调用封装在 `app/web/js/estimator.js`，不要直接引用这里的文件。

段位标尺的横轴是 Sunny 星级（4K RC 表覆盖 1.5~11.1，LN 表覆盖 4.8~9.6），
与 MinaCalc 的 MSD 不是同一套量纲，两者不能互相查表。

原仓库另有 6K / 7K / 10K 的标尺，本项目只做 4K，未使用但一并保留，方便日后扩展。
