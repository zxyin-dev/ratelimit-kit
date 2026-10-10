# -*- coding: utf-8 -*-
"""退避与重试测试 —— 本库最核心的行为契约。

最重要的一条：**限流时按服务端说的秒数原地退避，且不加倍、不打折。**
所以在服务端给了等待时长的情况下，等待值是**可精确断言**的
（给定时长 + 固定余量，不含随机抖动）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import SleepRecorder  # noqa: E402

from ratelimit_kit import (  # noqa: E402
    Attempt,
    Backoff,
    RetryPolicy,
    RetryRunner,
    Verdict,
    parse_retry_after,
)


class TestParseRetryAfter(unittest.TestCase):
    def test_body_text_wins_over_header(self):
        body = {"errcode": 429, "errmsg": "请求过于频繁，请等待 15 秒后重试"}
        self.assertEqual(parse_retry_after(body, {"Retry-After": "3"}), 15.0)

    def test_chinese_patterns(self):
        for text, expected in [("请在 3 秒后重试", 3.0), ("请等待15秒", 15.0),
                               ("剩余 2.5 秒", 2.5)]:
            with self.subTest(text=text):
                self.assertEqual(parse_retry_after({"errmsg": text}), expected)

    def test_english_patterns(self):
        for text, expected in [("retry after 7", 7.0),
                               ("please wait 12 seconds", 12.0),
                               ("wait: 4s", 4.0)]:
            with self.subTest(text=text):
                self.assertEqual(parse_retry_after({"message": text}), expected)

    def test_numeric_field_then_header(self):
        self.assertEqual(parse_retry_after({"retry_after": 8}), 8.0)
        self.assertEqual(parse_retry_after({"retryAfter": "9"}), 9.0)
        self.assertEqual(parse_retry_after({"errmsg": "慢点"}, {"Retry-After": "11"}),
                         11.0)

    def test_bare_string_payload(self):
        self.assertEqual(parse_retry_after("retry after 5 seconds"), 5.0)

    def test_returns_none_when_unknown(self):
        """解析不出来必须返回 None —— "猜一个数"是调用方的策略，不是本库的。"""
        self.assertIsNone(parse_retry_after({"errmsg": "请求过于频繁"}))
        self.assertIsNone(parse_retry_after("", {}))
        self.assertIsNone(parse_retry_after(None, None))

    def test_boolean_is_not_a_number(self):
        """``True`` 是 ``int`` 的子类，不能当成 1 秒。"""
        self.assertIsNone(parse_retry_after({"retry_after": True}))

    def test_bad_header_is_ignored(self):
        self.assertIsNone(parse_retry_after({"errmsg": "慢"}, {"Retry-After": "soon"}))


class TestRetryPolicyValidation(unittest.TestCase):
    def test_rejects_impossible_values(self):
        for kwargs in [{"max_attempts": 0}, {"max_rate_limit_attempts": 0},
                       {"wait_ceiling": 0}, {"escalate": 0.5}]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RetryPolicy(**kwargs)


class TestRetryRunner(unittest.TestCase):
    def setUp(self):
        self.sleep = SleepRecorder()

    def runner(self, **policy_kwargs) -> RetryRunner:
        return RetryRunner(RetryPolicy(**policy_kwargs), sleep=self.sleep)

    # -- 成功路径 ---------------------------------------------------------
    def test_success_on_first_try_makes_one_attempt(self):
        calls = []

        def attempt(n):
            calls.append(n)
            return Attempt(Verdict.OK, payload={"n": n})

        report = self.runner().run(attempt)
        self.assertTrue(report.ok)
        self.assertEqual(calls, [1])
        self.assertEqual(report.attempts, 1)
        self.assertEqual(report.wait_seconds, 0.0)
        self.assertEqual(report.payload, {"n": 1})

    # -- 限流：精确断言 ---------------------------------------------------
    def test_rate_limited_waits_exactly_as_told_plus_margin(self):
        """服务端说 3 秒 → 退避 3 + margin(1) = 4 秒。不含抖动，可精确断言。"""
        state = {"n": 0}

        def attempt(n):
            state["n"] += 1
            if n == 1:
                return Attempt(Verdict.RATE_LIMITED, wait=3.0, error="被限流")
            return Attempt(Verdict.OK)

        report = self.runner(max_rate_limit_attempts=5).run(attempt)
        self.assertTrue(report.ok)
        self.assertEqual(self.sleep.calls, [4.0])
        self.assertEqual(report.waits, [4.0])
        self.assertEqual(report.rate_limited, 1)
        self.assertEqual(report.attempts, 2)

    def test_wait_ceiling_caps_the_server_hint(self):
        def attempt(n):
            if n == 1:
                return Attempt(Verdict.RATE_LIMITED, wait=600.0)
            return Attempt(Verdict.OK)

        self.runner(wait_ceiling=5.0).run(attempt)
        self.assertEqual(self.sleep.calls, [6.0])   # min(600, 5) + 1

    def test_margin_is_configurable(self):
        def attempt(n):
            return (Attempt(Verdict.RATE_LIMITED, wait=2.0) if n == 1
                    else Attempt(Verdict.OK))

        self.runner(margin=0.5).run(attempt)
        self.assertEqual(self.sleep.calls, [2.5])

    # -- 限流：兜底递增 ---------------------------------------------------
    def test_fallback_wait_escalates_when_server_says_nothing(self):
        """服务端没给时长时，兜底值要**递增** —— 固定值会反复撞墙。"""
        def attempt(n):
            if n <= 2:
                return Attempt(Verdict.RATE_LIMITED, wait=None)
            return Attempt(Verdict.OK)

        self.runner(fallback_wait=10.0, escalate=1.5,
                    fallback_jitter=0.0, margin=0.0).run(attempt)
        self.assertEqual(len(self.sleep.calls), 2)
        self.assertEqual(self.sleep.calls[0], 10.0)
        self.assertEqual(self.sleep.calls[1], 15.0)

    def test_fallback_never_exceeds_ceiling_plus_margin(self):
        def attempt(n):
            if n <= 6:
                return Attempt(Verdict.RATE_LIMITED, wait=None)
            return Attempt(Verdict.OK)

        self.runner(fallback_wait=10.0, escalate=2.0, fallback_jitter=0.0,
                    margin=1.0, wait_ceiling=20.0,
                    max_rate_limit_attempts=10).run(attempt)
        self.assertLessEqual(max(self.sleep.calls), 21.0)

    # -- 预算：两类失败各自计数 -------------------------------------------
    def test_rate_limit_budget_is_separate_from_error_budget(self):
        def attempt(n):
            return Attempt(Verdict.RATE_LIMITED, wait=1.0)

        report = self.runner(max_rate_limit_attempts=3).run(attempt)
        self.assertFalse(report.ok)
        self.assertEqual(report.attempts, 3)
        self.assertEqual(report.rate_limited, 3)
        self.assertEqual(len(self.sleep.calls), 2, "放弃时不应再等待")
        self.assertEqual(report.outcome.verdict, Verdict.RATE_LIMITED)

    def test_error_budget_gives_up_earlier(self):
        def attempt(n):
            return Attempt(Verdict.RETRYABLE, error="HTTP 502")

        report = self.runner(max_attempts=2, backoff_jitter=0.0).run(attempt)
        self.assertFalse(report.ok)
        self.assertEqual(report.attempts, 2)
        self.assertEqual(report.retried, 2)
        self.assertEqual(len(self.sleep.calls), 1)
        self.assertEqual(report.error, "HTTP 502")

    def test_mixed_failures_use_their_own_budgets(self):
        """先被限流两次再遇到瞬时故障：两个预算互不消耗。"""
        seq = [Verdict.RATE_LIMITED, Verdict.RETRYABLE, Verdict.RATE_LIMITED,
               Verdict.OK]

        def attempt(n):
            verdict = seq[n - 1]
            if verdict is Verdict.RATE_LIMITED:
                return Attempt(verdict, wait=1.0)
            return Attempt(verdict, error="boom")

        report = self.runner(max_attempts=3, max_rate_limit_attempts=3,
                             backoff_jitter=0.0).run(attempt)
        self.assertTrue(report.ok)
        self.assertEqual(report.attempts, 4)
        self.assertEqual(report.rate_limited, 2)
        self.assertEqual(report.retried, 1)

    # -- 不可重试 ---------------------------------------------------------
    def test_fatal_stops_immediately_without_waiting(self):
        calls = []

        def attempt(n):
            calls.append(n)
            return Attempt(Verdict.FATAL, error="签名错误")

        report = self.runner().run(attempt)
        self.assertFalse(report.ok)
        self.assertEqual(calls, [1])
        self.assertEqual(self.sleep.calls, [])
        self.assertEqual(report.error, "签名错误")

    # -- 事件回调 ---------------------------------------------------------
    def test_events_describe_what_happened(self):
        events = []
        seq = [Verdict.RATE_LIMITED, Verdict.RETRYABLE, Verdict.OK]

        def attempt(n):
            verdict = seq[n - 1]
            if verdict is Verdict.RATE_LIMITED:
                return Attempt(verdict, wait=2.0)
            if verdict is Verdict.RETRYABLE:
                return Attempt(verdict, error="boom")
            return Attempt(verdict)

        runner = RetryRunner(RetryPolicy(backoff_jitter=0.0), sleep=self.sleep,
                             on_event=lambda kind, msg, wait: events.append(
                                 (kind, wait)))
        report = runner.run(attempt)
        self.assertTrue(report.ok)
        self.assertEqual([kind for kind, _ in events],
                         ["rate_limited", "retryable", "ok"])
        self.assertEqual(events[0][1], 3.0)      # 2 + margin
        self.assertEqual(events[1][1], 1.0)      # backoff base

    # -- 记账 -------------------------------------------------------------
    def test_report_totals_add_up(self):
        def attempt(n):
            if n <= 3:
                return Attempt(Verdict.RATE_LIMITED, wait=2.0)
            return Attempt(Verdict.OK)

        report = self.runner(max_rate_limit_attempts=5).run(attempt)
        self.assertEqual(report.wait_seconds, sum(report.waits))
        self.assertEqual(report.wait_seconds, 9.0)   # 3 次 × (2+1)
        self.assertEqual(report.attempts, 4)
        self.assertAlmostEqual(self.sleep.total, report.wait_seconds)

    def test_attempt_index_is_one_based_and_increasing(self):
        seen = []

        def attempt(n):
            seen.append(n)
            return (Attempt(Verdict.RETRYABLE) if n < 3 else Attempt(Verdict.OK))

        self.runner(max_attempts=5, backoff_jitter=0.0).run(attempt)
        self.assertEqual(seen, [1, 2, 3])


class TestBackoff(unittest.TestCase):
    def test_grows_then_caps(self):
        backoff = Backoff(base=1.0, factor=2.0, cap=10.0, jitter=0.0)
        self.assertEqual([backoff.next_delay() for _ in range(5)],
                         [1.0, 2.0, 4.0, 8.0, 10.0])

    def test_reset(self):
        backoff = Backoff(jitter=0.0)
        backoff.next_delay()
        backoff.next_delay()
        backoff.reset()
        self.assertEqual(backoff.next_delay(), backoff.base)

    def test_jitter_spreads_values(self):
        backoff = Backoff(base=1.0, jitter=0.5)
        delays = {round(backoff.next_delay(), 6) for _ in range(20)}
        self.assertGreater(len(delays), 1)

    def test_rejects_impossible_parameters(self):
        with self.assertRaises(ValueError):
            Backoff(base=0)
        with self.assertRaises(ValueError):
            Backoff(factor=0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
