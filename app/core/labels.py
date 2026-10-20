"""展示用文案：失误四类的中文名与配色。

为什么单独一份：这几个字符串属于「怎么显示」，不属于「怎么算」。放在 attribution 里，
报告模块为了取一张字表就得 import 算法模块，展示层与算法层从此绑死；放在 report 里，
归因结果自己又要回头去 import 展示层。两边都从这里取最省事。

`attribution.CATEGORY_LABEL` 仍然保留同名导出，外部调用方不需要改。
"""

from __future__ import annotations

CATEGORY_LABEL = {
    "ability": "超出能力范围",
    "rhythm": "节奏感弱",
    "distraction": "分神",
    # 第四类：三类签名都不成立的孤立小失误。它必须单独存在 —— 真实回放里最常见
    # 的瑕疵就是"手抖了一下"，把它硬塞进另外三类，会让高手局的分布全是"分神"。
    "slip": "随机失手",
}

CATEGORY_COLOR = {
    "ability": "#ff9f6e",
    "rhythm": "#6ee7ff",
    "distraction": "#c792ff",
    "slip": "#8b93b5",
}
