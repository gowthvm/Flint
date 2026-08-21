"""Unit tests for parse_queue_file()."""

import os

from core.cli import parse_queue_file


def test_strips_whitespace(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("  a.iso  \n")
    images, warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1
    assert warnings == []


def test_ignores_empty_lines(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("\n\na.iso\n\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_ignores_comments(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("# comment\na.iso\n# another\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_strips_inline_comments(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("a.iso # my image\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_unwraps_double_quotes(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text('"a.iso"\n')
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_unwraps_single_quotes(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("'a.iso'\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_strips_bom(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_bytes(b"\xef\xbb\xbf a.iso\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1


def test_resolves_relative_paths(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text("a.iso\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1
    assert os.path.isabs(images[0])


def test_keeps_absolute_paths(tmp_path):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    q = tmp_path / "queue.txt"
    q.write_text(f"{img}\n")
    images, _warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert len(images) == 1
    assert str(img) in images[0]


def test_warns_on_missing(tmp_path):
    q = tmp_path / "queue.txt"
    q.write_text("nonexistent.iso\n")
    images, warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert images == []
    assert len(warnings) == 1


def test_returns_empty_on_all_bad(tmp_path):
    q = tmp_path / "queue.txt"
    q.write_text("bad1.iso\nbad2.iso\n")
    images, warnings = parse_queue_file(str(q), base_dir=str(tmp_path))
    assert images == []
    assert len(warnings) == 2
