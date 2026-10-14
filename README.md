# ratelimit-kit

采集类程序的**限流、退避、断点、幂等落盘**。
零第三方依赖，只用 Python 标准库。

不是又一个"限流装饰器"库 —— 这里只有四件事，每一件都是从两个真实项目里
踩坑之后沉淀下来的。代码总量约 500 行，`python -m unittest` 有 **71 项测试**兜着。

---

## 它解决的具体问题

| 你会遇到的坑 | 本库的答案 |
|---|---|
| "每个请求前 sleep 一下"，并发一上来就叠乘成 N 倍速度 | `TokenBucket` / `FixedIntervalLimiter` —— **有状态**的全局限速器 |
| 把 429 抛成异常，上层框架自动重连，和你的退避叠加成连点，越重试越打不开 | `RetryRunner` —— 429 **原地退避**重试同一个请求，判决与处置分离 |
| 服务端只在 `errmsg` 里写「请在 N 秒后重试」，`Retry-After` 头却是空的 | `parse_retry_after` —— 正文正则 → 数值字段 → 响应头，三级优先级 |
| 中断后只能从头跑，重跑又产生重复 | `Checkpoint` + `JsonlDedupeStore` —— 断点续跑 + 天然幂等 |
| 断点说"这页完成了"，数据却没写进去 | 先 `fsync` 落盘**再**推进断点，顺序在 API 上就写死了 |
| 换了参数（比如页大小）后，旧断点让新一轮直接漏数据 | `Checkpoint(identity={...})` —— 参数对不上，断点自动作废 |
| CSV 每次跑都多一份重复行 | `write_csv` **整体重写**，输出是输入的纯函数 |
| 中文 CSV 用 Excel 打开是乱码 | 默认 `utf-8-sig`（带 BOM） |

---

## 安装

```bash
pip install git+https://github.com/zxyin-dev/ratelimit-kit
```

或者直接把 `ratelimit_kit/` 目录拷进你的项目 —— 它没有任何依赖，
也不需要安装。

---

## 四个原语

### 1. 限速：必须全局

```python
from ratelimit_kit import TokenBucket, make_limiter

# 服务端实测"10 秒内最多 2 次" → 稳态 0.2 次/秒，留 20% 余量 → 1/6
limiter = TokenBucket(capacity=2, refill_per_sec=1 / 6)

limiter.acquire()      # 取一个配额，不足则阻塞；返回实际等待秒数
```

**为什么强调"全局"**：限速一旦不是全局的，就等于没限速。典型错误写法是
"每个请求前 sleep 一下" —— 单线程看着没问题，一旦并发，N 个 worker 各自
sleep 会叠乘成 N 倍速度。所以这个库只提供**有状态的对象**，让你持单例。

`make_limiter(min_interval=12)` 可以切换回保守的固定间隔模式，
适合"不知道服务端上限，只知道不能快"的场景。

### 2. 重试：三种判决，三种处置

把"一次请求"写成一个纯函数，返回 `Attempt`：

```python
from ratelimit_kit import Attempt, Verdict, RetryRunner, RetryPolicy, parse_retry_after

def attempt(n):
    limiter.acquire()
    resp = do_request()
    if resp.status == 429:
        return Attempt(Verdict.RATE_LIMITED, wait=parse_retry_after(resp.json))
    if resp.status >= 500:
        return Attempt(Verdict.RETRYABLE, error=f"HTTP {resp.status}")
    if resp.status == 403:
        return Attempt(Verdict.FATAL, error="被拒绝，重试无意义")
    return Attempt(Verdict.OK, payload=resp.json)

report = RetryRunner(RetryPolicy(max_rate_limit_attempts=10)).run(attempt)
if not report.ok:
    raise RuntimeError(report.error)     # 明确报错，不要静默丢数据
```

| 判决 | 含义 | 处置 |
|---|---|---|
| `OK` | 成功 | 停止 |
| `RATE_LIMITED` | 被限流，服务端可能给了等待时长 | **原地**退避（独立预算） |
| `RETRYABLE` | 瞬时故障（网络抖动、5xx） | 指数退避 + 抖动（独立预算） |
| `FATAL` | 不该重试（鉴权失败、参数错误） | 立即停止并如实上报 |

**两类失败用独立预算**，因为"值得再试几次"完全不是一个量级：限流等一会儿
大概率能过（可以多试），真故障通常不会因为多试两次就好（少试，早点上报）。

**等待时长是可测的**：服务端给了时长时，等待值 = 服务端时长 + 固定余量，
**不含随机抖动**，所以可以精确断言。抖动只用在"服务端没说"的兜底递增上：

```python
def attempt(n):
    return Attempt(Verdict.RATE_LIMITED, wait=3.0) if n == 1 else Attempt(Verdict.OK)

runner = RetryRunner(RetryPolicy(margin=1.0), sleep=recorded_sleep)
runner.run(attempt)
assert recorded_sleep.calls == [4.0]     # 3 + 1，不多不少
```

### 3. 断点：进度不能领先于数据

```python
from ratelimit_kit import Checkpoint

ckpt = Checkpoint("data/checkpoint.json",
                  identity={"page_size": 50},                  # 参与校验
                  info={"page_start": 1, "page_end": 200})     # 只记录

start = (ckpt.load() or 0) + 1
for page in range(start, 201):
    records = fetch_page(page)
    store.add(records)        # 内部 flush + fsync
    ckpt.save(page)           # 数据先落盘，再推进进度
```

三条规则：

* **只前进不后退** —— 用 `--restart` 重跑旧页时不会被写回去（`save` 返回 `False`）；
* **身份变了就作废** —— `identity` 任一项对不上，`load()` 返回 `None`；
* **原子写** —— 临时文件 + `os.replace`，任何时刻读到的都是完整 JSON。

**`identity` 里只放"会改变游标含义"的参数。** 这是个容易搞反的地方：
`page_size` 决定"第 N 页对应哪段数据"，必须参与校验；而"这次要跑多少页"
（`page_start` / `page_end`）是任务范围，放进 `info` 记录即可。
把范围塞进 `identity`，会让"先跑 1~3 页、再扩到 1~6 页"这种**完全正常的续采**
被判成换了任务，然后从头重来 —— 这个坑本项目自己踩过。

### 4. 落盘：输出是输入的纯函数

```python
from ratelimit_kit import JsonlDedupeStore, read_jsonl, write_csv

key_of = lambda r: f"{r['title']}\x1f{r['date']}"   # 用不可打印分隔符

with JsonlDedupeStore("data/items.jsonl", key_of) as store:
    counts = store.add(records)
    # {"written": 12, "duplicates": 3}
```

* JSONL 是**事实日志**（只追加）；去重键从日志里重建 ——
  **不额外维护"已见集合"文件**，因为两份状态一旦不同步就会产生重复，
  而单份状态不可能不同步；
* 因此**重新打开同一个文件天然幂等**，重跑不会产生重复行；
* `read_jsonl` 容忍最后一行是半行（中断的常见产物），
  不会因为一个残行就报废整份数据；
* `write_csv` 每次**整体重写**，跑一次和跑十次字节完全相同。

> 拼去重键时注意：用 `"|"` 这类可打印字符连接多个字段，
> `"A|B" + "C"` 和 `"A" + "B|C"` 会撞成同一个键。用 `"\x1f"`。

---

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

**71 项**，覆盖：并发下令牌桶不被绕过、429 退避时长精确匹配服务端提示、
两类重试预算互不消耗、`FATAL` 立即停止、退避封顶、兜底时长递增、
断点身份失效与单调性、原子写不留残文件、残行容忍、去重幂等、CSV 字节幂等。

测试用注入的 `sleep` 替换真实等待，所以整套测试**没有 sleep 堆积**，
同时还能断言"等了多少秒"。

---

## 不做什么

刻意保持小。以下都不在本库范围内，因为它们需要替调用方做业务决定：

* 不做 HTTP 客户端（不绑定 `requests` / `httpx` / `aiohttp`）；
* 不做日志格式化（用 `on_event` 回调把事件给你，格式你定）；
* 不做"猜一个等待时长"（`parse_retry_after` 解析不出来就返回 `None`，
  兜底策略由 `RetryPolicy` 显式表达）；
* 不做任务调度、并发池、代理池、去重后端（Redis/Bloom filter）。

---

## English summary

**ratelimit-kit** provides four primitives for long-running data collection,
extracted from two production projects and hardened by the specific failures
they hit. Standard library only, no dependencies.

1. **Global rate limiting** (`TokenBucket`, `FixedIntervalLimiter`) —
   per-request `sleep()` collapses under concurrency; limiting must be a
   stateful, shared object.
2. **In-place 429 backoff** (`RetryRunner`, `parse_retry_after`, `Backoff`) —
   raising 429 to an upstream framework triggers its own retry loop, which
   compounds with yours into a hammering pattern that *widens* the throttle
   window. Verdicts (`OK` / `RATE_LIMITED` / `RETRYABLE` / `FATAL`) keep the
   decision in the layer that owns global state. Rate-limit and transient-error
   budgets are counted separately.
3. **Checkpoints** (`Checkpoint`) — monotonic, atomically written, and
   invalidated when task identity changes; progress is never allowed to lead
   the data.
4. **Idempotent persistence** (`JsonlDedupeStore`, `write_csv`) — an
   append-only fact log whose dedupe set is rebuilt from the log itself
   (a single source of truth cannot desynchronise), plus CSV export that
   rewrites the whole file so output is a pure function of input.

Waiting is injectable, so tests assert *how long* was waited without actually
sleeping. MIT licensed.

---

## License

MIT
