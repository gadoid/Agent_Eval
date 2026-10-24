"""复核页端到端（doc/10 §10，可选）：队列 → 键盘打分 → 预览 → 提交 → 下一段 → 结论落库。

默认 `pytest tests/` 不收集本文件（文件名不是 test_*.py）。手动运行：

    pip install playwright
    pytest tests/e2e_review.py -q

浏览器优先用 Playwright 自带 Chromium，缺失时回退本机 Edge/Chrome；都没有则 skip。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from scoring import cli  # noqa: E402
from scoring.store import Store  # noqa: E402
from tests.helpers import make_run, mk_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed(tmp: Path) -> tuple[str, str, list[str]]:
    """两条独立用例、默认算子下都未决；返回库路径与队列里的两个 answer_id 前缀（uid）。"""
    r1 = mk_record()
    r2 = mk_record(uid="ec-2#0")
    r2["case"] = {**r1["case"], "id": "ec-2", "uid": "ec-2#0"}
    records = [r1, r2]
    spec_rows = [{**r["case"], "evalset_version": "v1"} for r in records]
    spec = tmp / "e2e.jsonl"
    spec.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in spec_rows), encoding="utf-8")
    db = str(tmp / "e2e.db")
    run_dir = make_run(tmp, "run", records, run_id="e2e")
    assert cli.main(["specs", "import", str(spec), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--db", db]) == 0
    return db, str(run_dir), ["ec-1#0", "ec-2#0"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e-review")
    db, _, uids = _seed(tmp)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "scoring", "serve", "--db", db, "--port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("复核服务未启动")
    yield base, db, uids
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        for kwargs in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                b = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception:
                continue
        else:
            pytest.skip("未找到可用浏览器（python -m playwright install chromium）")
        yield b
        b.close()


def test_review_keyboard_submit_and_next(server, browser):
    base, db, uids = server
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # 队列 → 第一段
    page.goto(base + "/review?tab=undecided")
    first = page.locator('.rv-cq a[href^="/review/"]').first
    href = first.get_attribute("href")
    page.goto(base + href)
    page.wait_for_selector(".rv-row")
    aid = href.split("/review/")[1].split("?")[0]
    assert quote(uids[0], safe="") in aid and page.locator(".rv-row").count() == 2

    # 键盘逐行：禁止事实按 2（未采信），必要事实按 1（对），J 换行
    kinds = page.locator(".rv-row").evaluate_all("els => els.map(e => e.dataset.kind)")
    for i, kind in enumerate(kinds):
        page.keyboard.press("2" if kind == "forbidden" else "1")
        if i < len(kinds) - 1:
            page.keyboard.press("j")

    # 预览（服务端算分，不落库）
    page.wait_for_function("document.querySelector('#concl').textContent.includes('预览')")
    page.wait_for_function("document.querySelector('#concl').textContent.includes('通过')")
    assert "100" in page.locator("#total").text_content()

    # 提交并跳到下一段
    page.keyboard.press("Control+Enter")
    page.wait_for_url(lambda u: "/review/" in u and aid not in u, timeout=10000)
    assert quote(uids[1], safe="") in page.url
    page.wait_for_selector(".rv-row")

    # 结论已落库：重新打开第一段，人工通过 + 100 分
    page.goto(base + href)
    page.wait_for_selector("#concl")
    concl = page.locator("#concl").text_content()
    assert "通过" in concl and "100 分" in concl
    store = Store(db)
    row = store.conn.execute(
        "SELECT status, score FROM conclusions WHERE answer_id=? ORDER BY rowid DESC LIMIT 1",
        (unquote(aid),)).fetchone()
    store.close()
    assert row is not None and row["status"] == "pass" and row["score"] == 100
    assert not errors, errors
    page.close()
