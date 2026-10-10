# -*- coding: utf-8 -*-
"""ratelimit-kit —— 采集类程序的限流、退避、断点与幂等落盘。

这个库不是"最佳实践清单"，而是两个真实项目里反复踩坑之后沉淀下来的四件事：

1. **限速必须是全局的**（``TokenBucket`` / ``FixedIntervalLimiter``）
   —— 每处各自 sleep 等于没限速，并发一上来就叠乘成 N 倍速度。
2. **429 要原地退避，不能抛给上层**（``RetryRunner`` + ``parse_retry_after``）
   —— 抛异常会触发框架自动重连，和你的退避叠加成连点，把限流窗口越拖越长。
3. **进度不能领先于数据**（``Checkpoint``）
   —— 先落盘再推进；参数变了断点作废；只前进不后退。
4. **输出要是输入的纯函数**（``JsonlDedupeStore`` / ``write_csv``）
   —— 事实日志只追加、去重键从日志重建、导出整体重写，跑一次和跑十次结果相同。

零第三方依赖，只用标准库。

快速上手
--------

限流 + 原地退避：

```python
from ratelimit_kit import TokenBucket, RetryRunner, RetryPolicy, Attempt, Verdict

limiter = TokenBucket(capacity=2, refill_per_sec=1 / 6)
runner = RetryRunner(RetryPolicy(max_rate_limit_attempts=10))

def fetch(attempt_no):
    limiter.acquire()
    resp = do_request()
    if resp.status == 429:
        return Attempt(Verdict.RATE_LIMITED, wait=parse_retry_after(resp.json))
    if resp.status >= 500:
        return Attempt(Verdict.RETRYABLE, error=f"HTTP {resp.status}")
    return Attempt(Verdict.OK, payload=resp.json)

report = runner.run(fetch)
if not report.ok:
    ...   # 明确报错，不要静默丢数据
```

断点 + 幂等落盘：

```python
ckpt = Checkpoint("data/checkpoint.json", identity={"page_size": 50})
store = JsonlDedupeStore("data/items.jsonl", key_func=lambda r: r["id"])

start = (ckpt.load() or 0) + 1
for page in range(start, 201):
    result = fetch_page(page)
    store.add(result.records)      # 内部 flush + fsync
    ckpt.save(page)                # 数据先落盘，再推进进度
```
"""

from __future__ import annotations

from .checkpoint import Checkpoint, atomic_write_json, read_json
from .limiter import (
    FixedIntervalLimiter,
    Limiter,
    TokenBucket,
    make_limiter,
)
from .retry import (
    Attempt,
    Backoff,
    RetryPolicy,
    RetryReport,
    RetryRunner,
    Verdict,
    parse_retry_after,
)
from .store import (
    JsonlDedupeStore,
    load_seen_keys,
    read_jsonl,
    write_csv,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # 限流
    "Limiter",
    "TokenBucket",
    "FixedIntervalLimiter",
    "make_limiter",
    # 退避与重试
    "Verdict",
    "Attempt",
    "RetryPolicy",
    "RetryReport",
    "RetryRunner",
    "Backoff",
    "parse_retry_after",
    # 断点
    "Checkpoint",
    "read_json",
    "atomic_write_json",
    # 落盘
    "JsonlDedupeStore",
    "read_jsonl",
    "load_seen_keys",
    "write_csv",
]
