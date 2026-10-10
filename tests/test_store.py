# -*- coding: utf-8 -*-
"""落盘测试：容忍残行、去重幂等、CSV 整体重写。"""

from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support import fresh_dir  # noqa: E402

from ratelimit_kit import (  # noqa: E402
    JsonlDedupeStore,
    load_seen_keys,
    read_jsonl,
    write_csv,
)

# 用不可打印分隔符拼键：标题里可能含 "|"，用 "|" 拼会撞键
key_of = lambda r: f"{r.get('a', '')}\x1f{r.get('b', '')}"  # noqa: E731


class TestReadJsonl(unittest.TestCase):
    def test_skips_truncated_last_line(self):
        """进程被中断常留下半行 JSON —— 不能因此报废整份数据。"""
        path = fresh_dir("store_trunc") / "items.jsonl"
        path.write_text(
            json.dumps({"a": 1}, ensure_ascii=False) + "\n"
            + '{"a": 2, "b":',          # 半行
            encoding="utf-8")
        self.assertEqual(read_jsonl(path), [{"a": 1}])

    def test_skips_blank_and_non_object_lines(self):
        path = fresh_dir("store_junk") / "items.jsonl"
        path.write_text('{"a":1}\n\n[1,2]\n"str"\n{"a":2}\n', encoding="utf-8")
        self.assertEqual(read_jsonl(path), [{"a": 1}, {"a": 2}])

    def test_missing_file_is_empty(self):
        self.assertEqual(read_jsonl(fresh_dir("store_missing") / "x.jsonl"), [])


class TestJsonlDedupeStore(unittest.TestCase):
    def setUp(self):
        self.path = fresh_dir("store_dedupe") / "items.jsonl"

    def test_counts_written_and_duplicates(self):
        with JsonlDedupeStore(self.path, key_of) as store:
            counts = store.add([{"a": "x"}, {"a": "x"}, {"a": "y"}])
        self.assertEqual(counts, {"written": 2, "duplicates": 1})

    def test_reopening_rebuilds_seen_set(self):
        """重新打开同一个文件时去重集合从日志重建 —— 重跑天然幂等。"""
        with JsonlDedupeStore(self.path, key_of) as store:
            store.add([{"a": "x"}, {"a": "y"}])
        with JsonlDedupeStore(self.path, key_of) as store:
            counts = store.add([{"a": "x"}, {"a": "y"}, {"a": "z"}])
        self.assertEqual(counts, {"written": 1, "duplicates": 2})
        self.assertEqual(len(read_jsonl(self.path)), 3)

    def test_written_before_close(self):
        """落盘发生在 add() 内部（flush + fsync），不依赖 close。"""
        store = JsonlDedupeStore(self.path, key_of)
        store.add([{"a": "x"}])
        self.assertEqual(len(read_jsonl(self.path)), 1)
        store.close()

    def test_creates_parent_directories(self):
        deep = self.path.parent / "a" / "b" / "items.jsonl"
        with JsonlDedupeStore(deep, key_of) as store:
            store.add([{"a": "x"}])
        self.assertTrue(deep.exists())

    def test_preset_seen_skips_without_reading(self):
        with JsonlDedupeStore(self.path, key_of, seen={"x\x1f"}) as store:
            counts = store.add([{"a": "x"}, {"a": "y"}])
        self.assertEqual(counts, {"written": 1, "duplicates": 1})

    def test_key_func_can_use_multiple_fields(self):
        with JsonlDedupeStore(self.path, key_of) as store:
            counts = store.add([{"a": "x", "b": "1"}, {"a": "x", "b": "2"}])
        self.assertEqual(counts["written"], 2)

    def test_load_seen_keys_matches_written_keys(self):
        with JsonlDedupeStore(self.path, key_of) as store:
            store.add([{"a": "x"}, {"a": "y"}, {"a": "x"}])
        self.assertEqual(load_seen_keys(self.path, key_of),
                         {key_of({"a": "x"}), key_of({"a": "y"})})


class TestWriteCsv(unittest.TestCase):
    def setUp(self):
        self.path = fresh_dir("store_csv") / "out.csv"

    def test_is_idempotent(self):
        """整体重写：跑一次和跑十次，字节完全相同。"""
        rows = [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]
        self.assertEqual(write_csv(self.path, rows, ["a", "b"]), 2)
        first = self.path.read_bytes()
        self.assertEqual(write_csv(self.path, rows, ["a", "b"]), 2)
        self.assertEqual(first, self.path.read_bytes())

    def test_has_bom_for_excel(self):
        write_csv(self.path, [{"a": "标题"}], ["a"])
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_ignores_extra_fields(self):
        write_csv(self.path, [{"a": "1", "b": "2", "note": "会被忽略"}], ["a", "b"])
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh))
            row = next(csv.reader(fh))
        self.assertEqual(header, ["a", "b"])
        self.assertEqual(row, ["1", "2"])

    def test_missing_field_becomes_empty(self):
        write_csv(self.path, [{"a": "1"}], ["a", "b"])
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual(row, {"a": "1", "b": ""})

    def test_header_only_when_no_rows(self):
        self.assertEqual(write_csv(self.path, [], ["a", "b"]), 0)
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows, [["a", "b"]])

    def test_encoding_can_be_overridden(self):
        write_csv(self.path, [{"a": "1"}], ["a"], encoding="utf-8")
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
