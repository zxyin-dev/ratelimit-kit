# -*- coding: utf-8 -*-
"""断点测试。

三条规则各一组：只前进不后退、身份变化即作废、写文件是原子的。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import fresh_dir  # noqa: E402

from ratelimit_kit import Checkpoint, atomic_write_json, read_json  # noqa: E402


class TestReadAndWriteJson(unittest.TestCase):
    def setUp(self):
        self.dir = fresh_dir("ckpt_json")

    def test_roundtrip(self):
        path = self.dir / "a.json"
        atomic_write_json(path, {"x": 1, "中文": "值"})
        self.assertEqual(read_json(path), {"x": 1, "中文": "值"})

    def test_creates_parent_directories(self):
        path = self.dir / "nested" / "deep" / "a.json"
        atomic_write_json(path, {"ok": True})
        self.assertTrue(path.exists())

    def test_read_missing_returns_default(self):
        self.assertIsNone(read_json(self.dir / "nope.json"))
        self.assertEqual(read_json(self.dir / "nope.json", {}), {})

    def test_read_corrupted_returns_default(self):
        """断点文件损坏的代价应该是"这轮从头来"，而不是"程序崩了"。"""
        path = self.dir / "bad.json"
        path.write_text("{ 不是 json", encoding="utf-8")
        self.assertIsNone(read_json(path))

    def test_no_tmp_left_behind(self):
        path = self.dir / "a.json"
        atomic_write_json(path, {"x": 1})
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.dir = fresh_dir("ckpt")
        self.path = self.dir / "checkpoint.json"

    def test_roundtrip(self):
        ckpt = Checkpoint(self.path, {"page_size": 50})
        self.assertIsNone(ckpt.load())
        self.assertTrue(ckpt.save(7))
        self.assertEqual(ckpt.load(), 7)

    def test_only_moves_forward(self):
        ckpt = Checkpoint(self.path, {"page_size": 50})
        ckpt.save(9)
        self.assertFalse(ckpt.save(4), "回退写应被拒绝并返回 False")
        self.assertEqual(ckpt.load(), 9)

    def test_monotonic_can_be_disabled(self):
        ckpt = Checkpoint(self.path, monotonic=False)
        ckpt.save(9)
        self.assertTrue(ckpt.save(4))
        self.assertEqual(ckpt.load(), 4)

    def test_identity_mismatch_invalidates(self):
        Checkpoint(self.path, {"page_size": 50, "stage": "list"}).save(9)
        self.assertIsNone(Checkpoint(self.path, {"page_size": 10}).load())
        self.assertIsNone(Checkpoint(self.path, {"stage": "detail"}).load())
        self.assertEqual(Checkpoint(self.path, {"page_size": 50,
                                                "stage": "list"}).load(), 9)

    def test_empty_identity_accepts_any_file(self):
        """没有身份键 → 没有东西可以"对不上" → 断点始终有效。

        这是刻意的语义：``Checkpoint(path)`` 就是一个不绑定任务的普通断点。
        需要隔离的是**给了身份键**的场景（见上一条）。
        """
        Checkpoint(self.path, {"a": 1}).save(3)
        self.assertEqual(Checkpoint(self.path).load(), 3)

    def test_info_is_recorded_but_not_validated(self):
        """``info`` 写进文件但不参与校验 —— 用来记"这次跑了多大范围"这类溯源信息。

        关键区别：把任务范围放进 ``identity`` 会让"先跑 1~3 页、再扩到 1~6 页"
        这种正常的续采被判成换任务，然后从头重来。
        """
        Checkpoint(self.path, {"page_size": 50},
                   info={"page_start": 1, "page_end": 3}).save(9)
        state = Checkpoint(self.path, {"page_size": 50},
                           info={"page_end": 999}).state()
        self.assertEqual(state["cursor"], 9)
        self.assertEqual(state["page_start"], 1)
        self.assertEqual(state["page_end"], 3)     # 记录的仍是写入时的值
        self.assertEqual(Checkpoint(self.path, {"page_size": 50}).load(), 9)

    def test_corrupted_file_is_ignored(self):
        self.path.write_text("{ 坏", encoding="utf-8")
        self.assertIsNone(Checkpoint(self.path).load())

    def test_non_integer_cursor_is_rejected(self):
        path = self.dir / "bad.json"
        atomic_write_json(path, {"cursor": "7"})
        self.assertIsNone(Checkpoint(path).load())

    def test_boolean_cursor_is_rejected(self):
        """``True`` 是 ``int`` 的子类，别把断点读成 1。"""
        path = self.dir / "bool.json"
        atomic_write_json(path, {"cursor": True})
        self.assertIsNone(Checkpoint(path).load())

    def test_save_rejects_non_integer(self):
        ckpt = Checkpoint(self.path)
        for bad in ["7", 7.0, None, True]:
            with self.subTest(bad=bad):
                with self.assertRaises(TypeError):
                    ckpt.save(bad)  # type: ignore[arg-type]

    def test_state_exposes_identity(self):
        ckpt = Checkpoint(self.path, {"batch": "2026-10", "page_size": 50})
        ckpt.save(12)
        state = ckpt.state()
        self.assertEqual(state["cursor"], 12)
        self.assertEqual(state["batch"], "2026-10")

    def test_clear(self):
        ckpt = Checkpoint(self.path)
        ckpt.save(5)
        ckpt.clear()
        self.assertIsNone(ckpt.load())
        ckpt.clear()          # 再删一次不应抛异常

    def test_file_is_valid_json_at_every_step(self):
        """原子写保证：任何时刻读到的都是完整 JSON。"""
        ckpt = Checkpoint(self.path)
        for cursor in range(1, 6):
            ckpt.save(cursor)
            json.loads(self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
