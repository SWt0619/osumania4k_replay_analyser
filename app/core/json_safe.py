"""JSON 输出的最后一道关：保证不写出 `Infinity` / `NaN`。

Python 的 `json` 默认允许这两个值（写出来是 `Infinity`、`NaN`），标准 JSON 不允许。
后端自己读得回来，浏览器不行——`JSON.parse` 会直接抛错，前端只会看到
「服务器返回的不是 JSON（HTTP 200）」，非常难查。所以出口统一走这里：
先按严格的 `allow_nan=False` 序列化，真的遇到非有限数再降级成 `null`。
"""

from __future__ import annotations

import json
import math


def sanitize(obj):
    """把 NaN / ±Infinity 换成 None，其余原样（递归处理 dict / list / tuple）。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def dumps(obj, **kwargs) -> str:
    """严格的 json.dumps：正常情况下零额外开销，遇到非有限数才走一遍清洗。"""
    kwargs.setdefault("ensure_ascii", False)
    try:
        return json.dumps(obj, allow_nan=False, **kwargs)
    except ValueError:
        return json.dumps(sanitize(obj), allow_nan=False, **kwargs)
