# -*- coding: utf-8 -*-
"""限速器测试。

重点不是"能不能等"，而是"**并发下会不会被绕过**" —— 那才是限速失效的真实形态。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ratelimit_kit import (  # noqa: E402
    FixedIntervalLimiter,
    Limiter,
    TokenBucket,
    make_limiter,
)


class TestTokenBucket(unittest.TestCase):
    def test_capacity_allows_immediate_burst(self):
        bucket = TokenBucket(capacity=2, refill_per_sec=0.01)
        self.assertEqual(bucket.acquire() + bucket.acquire(), 0.0)

    def test_third_request_has_to_wait(self):
        bucket = TokenBucket(capacity=2, refill_per_sec=50.0)  # 每 20ms 回一个
        bucket.acquire()
        bucket.acquire()
        waited = bucket.acquire()
        self.assertGreater(waited, 0.0)
        self.assertLess(waited, 0.5)

    def test_rejects_bad_parameters(self):
        with self.assertRaises(ValueError):
            TokenBucket(capacity=0)
        with self.assertRaises(ValueError):
            TokenBucket(refill_per_sec=0)
        with self.assertRaises(ValueError):
            TokenBucket(capacity=-1)

    def test_thread_safety_does_not_overissue(self):
        """5 线程 × 4 次 = 20 次请求；容量 5 + 每秒回填 20 个。

        不限速的话 20 次几乎是瞬间完成的。桶生效时至少要 (20-5)/20 = 0.75 秒。
        这里断言一个宽松下界（0.6s）—— 目的是证明**没有被绕过**，
        而不是精确测量时长。
        """
        bucket = TokenBucket(capacity=5, refill_per_sec=20.0)

        def worker():
            for _ in range(4):
                bucket.acquire()

        threads = [threading.Thread(target=worker) for _ in range(5)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(time.monotonic() - start, 0.6,
                                "令牌桶被并发绕过了 —— 这正是限速失效的典型形态")

    def test_satisfies_limiter_protocol(self):
        self.assertIsInstance(TokenBucket(), Limiter)


class TestFixedIntervalLimiter(unittest.TestCase):
    def test_zero_interval_never_waits(self):
        limiter = FixedIntervalLimiter(0)
        self.assertEqual(limiter.acquire(), 0.0)
        self.assertEqual(limiter.acquire(), 0.0)

    def test_enforces_minimum_gap(self):
        limiter = FixedIntervalLimiter(0.05)
        limiter.acquire()
        self.assertGreaterEqual(limiter.acquire(), 0.02)

    def test_negative_interval_rejected(self):
        with self.assertRaises(ValueError):
            FixedIntervalLimiter(-1)

    def test_satisfies_limiter_protocol(self):
        self.assertIsInstance(FixedIntervalLimiter(1), Limiter)


class TestMakeLimiter(unittest.TestCase):
    def test_explicit_interval_selects_fixed_mode(self):
        limiter = make_limiter(min_interval=12)
        self.assertIsInstance(limiter, FixedIntervalLimiter)
        self.assertEqual(limiter.min_interval, 12.0)

    def test_default_is_token_bucket(self):
        self.assertIsInstance(make_limiter(), TokenBucket)

    def test_interval_zero_is_still_accepted(self):
        """``min_interval=0`` 是合法输入（测试里常用），不能被当成"没指定"。"""
        self.assertIsInstance(make_limiter(min_interval=0), FixedIntervalLimiter)


if __name__ == "__main__":
    unittest.main(verbosity=2)
