"""评分脚本要读对场景自己的数据和文档。不调用模型。"""
from __future__ import annotations

import sqlite3

import pytest

from evals.quality_score import URL, _clean, load, score_s1


def fake_run(root, name: str, reply: str, doc: str) -> None:
    folder = root / name
    (folder / "outgoing").mkdir(parents=True)
    (folder / "outgoing" / "report.md").write_text(doc, "utf-8")
    db = sqlite3.connect(folder / "host.db")
    db.execute("CREATE TABLE outbox (kind TEXT, body TEXT)")
    db.execute("INSERT INTO outbox VALUES ('reply', ?)", (reply,))
    db.commit()
    db.close()


def test_s1_score_ignores_s10_in_same_run_dir(tmp_path):
    # s10 的目录排在前面，以前读到的就是它
    fake_run(tmp_path, "test_s10_researcher_then_secr0", "Python 交接完成", "# 无关文档")
    fake_run(tmp_path, "test_s1_s2_csv_report_then_cha0", "产品 C 下滑 32%，6 月数据不完整", "产品 C 下降 32%")
    score = score_s1(str(tmp_path))
    assert score["回复写出C下滑"] is True and score["文档写出C下滑"] is True
    assert score["回复说明6月不完整"] is True
    _, doc, replies = load(str(tmp_path), "s1")
    assert "无关文档" not in doc and "交接" not in replies


def test_missing_or_ambiguous_db_is_an_error(tmp_path):
    fake_run(tmp_path, "test_s10_researcher_then_secr0", "x", "x")
    with pytest.raises(SystemExit, match="找到 0 个"):
        load(str(tmp_path), "s1")
    fake_run(tmp_path, "test_s1_s2_csv_report_then_cha0", "x", "x")
    with pytest.raises(SystemExit, match="找到 2 个"):
        load(str(tmp_path))


def test_url_stops_at_chinese_punctuation():
    found = [_clean(u) for u in URL.findall("见 https://a.example/p；另见 https://b.example/q，完")]
    assert found == ["https://a.example/p", "https://b.example/q"]
