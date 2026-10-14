# -*- coding: utf-8 -*-
"""백엔드 테스트: office.py / office_server.py / tracker.py / _common.py

실행 (프로젝트 루트에서, 리눅스는 python 대신 python3):
    python -m unittest discover -s tests -v
또는 어디서든 (ROOT = 저장소 폴더):
    python ROOT/tests/test_backend.py -v

모든 테스트는 임시 폴더를 AIRE_ROOT 로 써서 실제 데이터를 건드리지 않는다.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common as C  # noqa: E402
import office  # noqa: E402
import office_server  # noqa: E402
import tracker  # noqa: E402

BOM = b"\xef\xbb\xbf"
ISO_KST = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")


class TempRootCase(unittest.TestCase):
    """테스트마다 새 임시 ROOT 를 만들고, CLI 는 AIRE_ROOT 로 그곳을 가리키게 한다."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="aire-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved_env = os.environ.get("AIRE_ROOT")
        os.environ["AIRE_ROOT"] = str(self.root)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._saved_env is None:
            os.environ.pop("AIRE_ROOT", None)
        else:
            os.environ["AIRE_ROOT"] = self._saved_env

    def cli(self, script: str, *args: str, expect: int | None = 0) -> tuple[int, str, str]:
        env = {**os.environ, "AIRE_ROOT": str(self.root)}
        env.pop("PYTHONIOENCODING", None)  # 스크립트가 스스로 UTF-8 로 맞추는지 확인
        proc = subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, env=env, timeout=60)
        out, err = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")
        if expect is not None:
            self.assertEqual(proc.returncode, expect, f"{script} {args}\nstdout={out}\nstderr={err}")
        return proc.returncode, out, err

    def office(self, *args: str, expect: int | None = 0):
        return self.cli("office.py", *args, expect=expect)

    def tracker(self, *args: str, expect: int | None = 0):
        return self.cli("tracker.py", *args, expect=expect)

    # 헬퍼
    def status_file(self, iid: str) -> dict:
        return json.loads((self.root / "office" / "status" / f"{iid}.json").read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        p = self.root / "office" / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def csv_rows(self, name: str) -> list[dict]:
        with open(self.root / "tracker" / name, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))


# ==========================================================================
# 공용 헬퍼
# ==========================================================================
class CommonTests(unittest.TestCase):
    def test_parse_int(self):
        self.assertEqual(C.parse_int("12,000"), 12000)
        self.assertEqual(C.parse_int("₩9900"), 9900)
        self.assertEqual(C.parse_int("5,000원"), 5000)
        self.assertEqual(C.parse_int("-990"), -990)
        self.assertEqual(C.parse_int("1500.0"), 1500)
        for bad in ("", "abc", None, "1e3", "NaN", "--5", True):
            self.assertIsNone(C.parse_int(bad), bad)

    def test_parse_date(self):
        self.assertEqual(C.parse_date("2026-10-15"), dt.date(2026, 10, 15))
        self.assertEqual(C.parse_date("2026/1/5"), dt.date(2026, 1, 5))
        self.assertEqual(C.parse_date("2026-10-15T14:03:22+09:00"), dt.date(2026, 10, 15))
        self.assertEqual(C.parse_date("20261015"), dt.date(2026, 10, 15))
        self.assertIsNone(C.parse_date("2026-13-40"))
        self.assertIsNone(C.parse_date("어제"))

    def test_root_resolution(self):
        saved = os.environ.pop("AIRE_ROOT", None)
        try:
            self.assertEqual(C.get_root(), SCRIPTS.parent)
            os.environ["AIRE_ROOT"] = str(SCRIPTS)
            self.assertEqual(C.get_root(), SCRIPTS.resolve())
        finally:
            os.environ.pop("AIRE_ROOT", None)
            if saved is not None:
                os.environ["AIRE_ROOT"] = saved

    def test_now_iso_is_kst(self):
        self.assertRegex(C.now_iso(), ISO_KST)

    def test_table_alignment_with_korean_and_emoji(self):
        self.assertEqual(C.disp_width("가a"), 3)
        self.assertEqual(C.disp_width("💤"), 2)
        table = C.format_table(["이름", "값"], [["가나다", "X"], ["abcdef", "X"], ["💻작업", "X"]])
        offsets = {C.disp_width(line.split("X")[0]) for line in table.splitlines()[2:]}
        self.assertEqual(len(offsets), 1, table)  # 두 번째 열이 모든 줄에서 같은 표시 폭 위치에서 시작
        self.assertIn("…", C.format_table(["a"], [["가" * 40]], max_widths=[10]))

    def test_bubble_len_ignores_joiners(self):
        self.assertEqual(C.bubble_len("👨\u200d💻"), 2)
        self.assertEqual(C.bubble_len("⚠\ufe0f"), 1)
        self.assertEqual(C.bubble_len("abcdef"), 6)

    def test_read_csv_rows_is_forgiving(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            self.assertEqual(C.read_csv_rows(p), [])  # 없음
            p.write_bytes(b"")
            self.assertEqual(C.read_csv_rows(p), [])  # 빈 파일
            p.write_bytes(BOM + "a,b\r\n1,2\r\n\r\n3\r\n4,5,6,7\r\n".encode("utf-8"))
            rows = C.read_csv_rows(p)
            self.assertEqual(rows[0], {"a": "1", "b": "2"})
            self.assertEqual(rows[1], {"a": "3", "b": ""})  # 모자란 칸은 빈 문자열
            self.assertEqual(rows[2], {"a": "4", "b": "5,6,7"})  # 넘치는 칸은 마지막 열에 합침
            p.write_bytes(os.urandom(2048))
            C.read_csv_rows(p)  # 예외 없이 끝나야 함

    def test_read_jsonl_tail_large_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e.jsonl"
            with open(p, "w", encoding="utf-8") as fh:
                for i in range(3000):  # ~600KB > 256KB 첫 청크
                    fh.write(json.dumps({"i": i, "pad": "가" * 40}, ensure_ascii=False) + "\n")
                fh.write("이건 JSON 이 아님\n")
                fh.write('{"i": 3000}\n')
                fh.write('{"i": 3001')  # 잘린 마지막 줄
            tail = C.read_jsonl_tail(p, 80)
            self.assertEqual(len(tail), 80)
            self.assertEqual([e["i"] for e in tail], list(range(2921, 3001)))
            self.assertEqual(C.read_jsonl_tail(p, 5000)[0]["i"], 0)
            self.assertEqual(C.read_jsonl_tail(Path(d) / "none.jsonl", 80), [])

    def test_append_bytes_creates_dirs_and_appends(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a" / "b.log"
            C.append_bytes(p, "하나\n".encode("utf-8"))
            C.append_bytes(p, b"two\n")
            self.assertEqual(p.read_bytes(), "하나\ntwo\n".encode("utf-8"))

    def test_ledger_math(self):
        b = C.new_bucket()
        for t, a in (("sale", 10000), ("sale", 5000), ("refund", -3000), ("fee", -1500), ("expense", 500), ("payout", 9000)):
            C.bucket_add(b, {"type": t, "amount": a})
        tot = C.bucket_totals(b)
        self.assertEqual((tot["revenue"], tot["costs"], tot["net"], tot["payouts"]), (12000, 2000, 10000, 9000))


# ==========================================================================
# office.py
# ==========================================================================
class OfficeCliTests(TempRootCase):
    def test_status_writes_file_and_event(self):
        _, out, _ = self.office("status", "maker-2", "working", "노션 취업 준비 템플릿 제작 중",
                                "--bubble", "💻", "--product", "notion-job-hunt-kit")
        self.assertIn("maker-2", out)
        rec = self.status_file("maker-2")
        self.assertEqual(rec["id"], "maker-2")
        self.assertEqual(rec["role"], "maker")
        self.assertEqual(rec["state"], "working")
        self.assertEqual(rec["task"], "노션 취업 준비 템플릿 제작 중")
        self.assertEqual(rec["bubble"], "💻")
        self.assertEqual(rec["product_id"], "notion-job-hunt-kit")
        self.assertRegex(rec["updated_at"], ISO_KST)
        raw = (self.root / "office" / "status" / "maker-2.json").read_bytes()
        self.assertFalse(raw.startswith(BOM))
        self.assertIn("노션".encode("utf-8"), raw)  # ensure_ascii=False

        evs = self.events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "status")
        self.assertEqual(evs[0]["state"], "working")
        self.assertEqual(evs[0]["id"], "maker-2")
        self.assertEqual(evs[0]["role"], "maker")
        self.assertEqual(evs[0]["msg"], "노션 취업 준비 템플릿 제작 중")
        self.assertEqual(evs[0]["product_id"], "notion-job-hunt-kit")
        self.assertRegex(evs[0]["ts"], ISO_KST)
        self.assertEqual(list((self.root / "office" / "status").glob("*.tmp")), [])
        self.assertEqual(list((self.root / "office" / "status").glob(".*")), [])  # 임시 파일 흔적 없음

    def test_status_overwrites_and_drops_product(self):
        self.office("status", "qa-1", "working", "검수", "--product", "p1")
        self.office("status", "qa-1", "done", "검수 완료")
        rec = self.status_file("qa-1")
        self.assertEqual(rec["state"], "done")
        self.assertNotIn("product_id", rec)
        self.assertEqual(rec["bubble"], "✅")
        self.assertEqual(len(self.events()), 2)

    def test_status_defaults(self):
        self.office("status", "ceo", "working", "배치 목표 수립")
        self.assertEqual(self.status_file("ceo")["bubble"], C.ROLES["ceo"]["bubble"])
        self.office("status", "ceo", "blocked")
        rec = self.status_file("ceo")
        self.assertEqual(rec["bubble"], "⛔")
        self.assertTrue(rec["task"])

    def test_status_custom_event_type(self):
        self.office("status", "qa-2", "blocked", "결함 발견: 표지 누락", "--type", "defect")
        self.assertEqual(self.events()[-1]["type"], "defect")
        self.assertEqual(self.status_file("qa-2")["state"], "blocked")

    def test_status_rejects_bad_state(self):
        rc, _, err = self.office("status", "maker-1", "sleeping", "x", expect=2)
        self.assertIn("idle", err)
        self.assertIn("working", err)
        self.assertFalse((self.root / "office" / "status" / "maker-1.json").exists())
        self.assertEqual(self.events(), [])

    def test_status_rejects_bad_type_and_writes_nothing(self):
        _, _, err = self.office("status", "maker-1", "idle", "x", "--type", "gossip", expect=2)
        self.assertIn("handoff", err)
        self.assertFalse((self.root / "office" / "status" / "maker-1.json").exists())

    def test_status_rejects_bad_ids(self):
        for bad in ("nobody-1", "Maker-1", "maker-", "../evil", "maker-1/../../x", "maker 1"):
            self.office("status", bad, "idle", "x", expect=2)
        self.assertFalse((self.root / "evil.json").exists())
        status_dir = self.root / "office" / "status"
        self.assertTrue(not status_dir.exists() or not any(status_dir.iterdir()))

    def test_bubble_validation(self):
        self.office("status", "pm", "working", "x", "--bubble", "OK!")
        self.office("status", "pm", "working", "x", "--bubble", "👨\u200d💻")
        self.assertEqual(self.status_file("pm")["bubble"], "👨\u200d💻")
        _, _, err = self.office("status", "pm", "working", "x", "--bubble", "너무너무긴말풍선", expect=2)
        self.assertIn("6", err)

    def test_event_types(self):
        for t in C.EVENT_TYPES:
            self.office("event", "pm", t, f"{t} 메시지", "--product", "p1")
        evs = self.events()
        self.assertEqual([e["type"] for e in evs], list(C.EVENT_TYPES))
        self.assertTrue(all(e["role"] == "pm" and e["product_id"] == "p1" for e in evs))
        _, _, err = self.office("event", "pm", "rumor", "x", expect=2)
        self.assertIn("approval_request", err)
        self.assertEqual(len(self.events()), len(C.EVENT_TYPES))

    def test_event_korean_roundtrip_and_newline_flattened(self):
        self.office("event", "designer", "handoff", "제작자에게\n레이아웃 전달 😀")
        raw = (self.root / "office" / "events.jsonl").read_bytes()
        self.assertIn("레이아웃".encode("utf-8"), raw)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(self.events()[0]["msg"], "제작자에게 레이아웃 전달 😀")

    def test_reset_default_staff(self):
        self.office("status", "maker-2", "working", "작업")
        self.office("status", "maker-4", "working", "추가 인스턴스")
        self.office("reset")
        files = sorted(p.stem for p in (self.root / "office" / "status").glob("*.json"))
        self.assertEqual(files, sorted(C.DEFAULT_STAFF + ["maker-4"]))
        self.assertEqual(len(C.DEFAULT_STAFF), 11)
        for iid in files:
            rec = self.status_file(iid)
            self.assertEqual(rec["state"], "idle", iid)
            self.assertEqual(rec["id"], iid)
            self.assertEqual(rec["role"], C.role_of(iid))
        self.assertEqual(self.events()[-1]["type"], "note")

    def test_reset_prune_and_no_event(self):
        self.office("status", "maker-4", "working", "추가")
        n = len(self.events())
        self.office("reset", "--prune", "--no-event")
        files = sorted(p.stem for p in (self.root / "office" / "status").glob("*.json"))
        self.assertEqual(files, sorted(C.DEFAULT_STAFF))
        self.assertEqual(len(self.events()), n)

    def test_show(self):
        self.office("reset")
        self.office("status", "qa-1", "working", "노션 템플릿 열어보는 중", "--product", "notion-job-hunt-kit")
        _, out, _ = self.office("show", "--events", "3")
        self.assertIn("qa-1", out)
        self.assertIn("검수 (Testing)", out)
        self.assertIn("노션 템플릿 열어보는 중", out)
        self.assertIn("working 1", out)
        _, out, _ = self.office("show", "--json")
        data = json.loads(out)
        self.assertEqual(len(data["agents"]), 11)
        self.assertEqual({a["id"]: a["state"] for a in data["agents"]}["qa-1"], "working")

    def test_show_empty_root(self):
        _, out, _ = self.office("show")
        self.assertIn("0명", out)

    def test_concurrent_event_appends_lose_nothing(self):
        n_proc, n_each = 8, 30
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import office\n"
            "who = sys.argv[2]\n"
            "for i in range(int(sys.argv[3])):\n"
            "    office.log_event('maker-' + who, 'note', '병렬 메시지 %s-%d ' % (who, i) + '가' * 200)\n"
        )
        env = {**os.environ, "AIRE_ROOT": str(self.root)}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(k + 1), str(n_each)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for k in range(n_proc)]
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        lines = (self.root / "office" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), n_proc * n_each)
        seen = {json.loads(line)["msg"].split(" ")[2] for line in lines}  # 모든 줄이 온전한 JSON
        self.assertEqual(len(seen), n_proc * n_each)

    def test_concurrent_status_writes_stay_valid(self):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import office\n"
            "for i in range(25):\n"
            "    office.set_status('ceo', 'working' if i % 2 else 'idle', '라운드 %d' % i)\n"
        )
        env = {**os.environ, "AIRE_ROOT": str(self.root)}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        reader_ok = True
        path = self.root / "office" / "status" / "ceo.json"
        while any(p.poll() is None for p in procs):  # 쓰는 동안 읽어도 항상 온전한 JSON
            if path.exists():
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    reader_ok = reader_ok and isinstance(sys.exc_info()[1], OSError)
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        self.assertTrue(reader_ok)
        self.assertIn(self.status_file("ceo")["state"], C.STATES)


# ==========================================================================
# tracker.py
# ==========================================================================
class TrackerCliTests(TempRootCase):
    def add_product(self, pid="notion-job-hunt-kit", **kw):
        args = ["add-product", "--id", pid, "--title", kw.get("title", "노션 취업 준비 템플릿"), "--batch", "batch-001",
                "--model", "template", "--platform", kw.get("platform", "kmong"), "--price-krw", str(kw.get("price", 9900))]
        if "status" in kw:
            args += ["--status", kw["status"]]
        return self.tracker(*args)

    def test_init_creates_bom_headers_and_is_idempotent(self):
        _, out, _ = self.tracker("init")
        expect = {"products.csv": C.PRODUCT_COLS, "ledger.csv": C.LEDGER_COLS, "approvals.csv": C.APPROVAL_COLS}
        for name, cols in expect.items():
            raw = (self.root / "tracker" / name).read_bytes()
            self.assertTrue(raw.startswith(BOM), name)
            self.assertEqual(raw[len(BOM):].decode("utf-8").strip(), ",".join(cols))
        self.assertIn("생성", out)
        self.add_product()
        before = (self.root / "tracker" / "products.csv").read_bytes()
        _, out, _ = self.tracker("init")
        self.assertEqual((self.root / "tracker" / "products.csv").read_bytes(), before)  # 데이터 보존
        self.assertIn("유지", out)

    def test_contract_headers(self):
        self.assertEqual(",".join(C.PRODUCT_COLS), "id,batch,title,model,platform,price_krw,status,created_at,listed_url,notes")
        self.assertEqual(",".join(C.LEDGER_COLS), "date,product_id,platform,type,amount_krw,note")
        self.assertEqual(",".join(C.APPROVAL_COLS), "id,product_id,action,requested_at,status,decided_at,note")

    def test_add_product_and_validation(self):
        self.add_product()
        row = self.csv_rows("products.csv")[0]
        self.assertEqual(row["id"], "notion-job-hunt-kit")
        self.assertEqual(row["title"], "노션 취업 준비 템플릿")
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["price_krw"], "9900")
        self.assertRegex(row["created_at"], ISO_KST)
        self.assertEqual(list(row), C.PRODUCT_COLS)
        self.assertTrue((self.root / "tracker" / "products.csv").read_bytes().startswith(BOM))

        _, _, err = self.tracker("add-product", "--id", "NOTION-job-hunt-kit", "--title", "중복", expect=1)  # 대소문자 무시 중복
        self.assertIn("이미", err)
        _, _, err = self.tracker("add-product", "--id", "p2", "--title", "t", "--status", "shipped", expect=2)
        self.assertIn("awaiting_owner", err)
        self.tracker("add-product", "--id", "p2", "--title", "t", "--price-krw", "-5", expect=2)
        self.tracker("add-product", "--id", "../p3", "--title", "t", expect=2)
        self.assertEqual(len(self.csv_rows("products.csv")), 1)

    def test_add_product_with_comma_and_quotes_roundtrips(self):
        self.tracker("add-product", "--id", "p1", "--title", '가계부, "엑셀" 시트', "--notes", "a,b\nc")
        row = self.csv_rows("products.csv")[0]
        self.assertEqual(row["title"], '가계부, "엑셀" 시트')
        self.assertEqual(row["notes"], "a,b c")

    def test_add_product_upsert(self):
        self.add_product()
        self.tracker("add-product", "--id", "notion-job-hunt-kit", "--title", "새 제목", "--status", "qa_passed",
                     "--price-krw", "12000", "--upsert")
        rows = self.csv_rows("products.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["status"], rows[0]["price_krw"], rows[0]["platform"]),
                         ("새 제목", "qa_passed", "12000", "kmong"))

    def test_set_status(self):
        self.add_product("p1")
        self.add_product("p2")
        self.tracker("set-status", "p1", "awaiting_owner", "--note", "SHIP")
        rows = {r["id"]: r for r in self.csv_rows("products.csv")}
        self.assertEqual(rows["p1"]["status"], "awaiting_owner")
        self.assertEqual(rows["p1"]["notes"], "SHIP")
        self.assertEqual(rows["p2"]["status"], "draft")
        _, _, err = self.tracker("set-status", "p1", "done", expect=2)
        self.assertIn("listed", err)
        _, _, err = self.tracker("set-status", "ghost", "listed", expect=1)
        self.assertIn("ghost", err)
        _, _, err = self.tracker("set-status", "p1", "listed", "--listed-url", "https://example.com/p1")
        self.assertIn("publish_listing", err)  # 승인 기록 없이 listed -> 경고
        self.assertEqual({r["id"]: r for r in self.csv_rows("products.csv")}["p1"]["listed_url"], "https://example.com/p1")

    def test_add_ledger_sign_normalisation_and_validation(self):
        self.tracker("add-ledger", "--type", "sale", "--amount", "9,900", "--date", "2026-10-01", "--platform", "kmong")
        self.tracker("add-ledger", "--type", "fee", "--amount", "990", "--date", "2026-10-01")
        self.tracker("add-ledger", "--type", "refund", "--amount", "-9900", "--date", "2026-10-02")
        self.tracker("add-ledger", "--type", "expense", "--amount", "3000", "--date", "2026-10-03", "--note", "폰트, 구입")
        self.tracker("add-ledger", "--type", "payout", "--amount", "-8000", "--date", "2026-10-04")
        rows = self.csv_rows("ledger.csv")
        self.assertEqual([r["amount_krw"] for r in rows], ["9900", "-990", "-9900", "-3000", "8000"])
        self.assertEqual(rows[3]["note"], "폰트, 구입")
        self.tracker("add-ledger", "--type", "sale", "--amount", "-5", expect=2)
        self.tracker("add-ledger", "--type", "gift", "--amount", "5", expect=2)
        self.tracker("add-ledger", "--type", "sale", "--amount", "abc", expect=2)
        self.tracker("add-ledger", "--type", "sale", "--amount", "0", expect=2)
        self.tracker("add-ledger", "--type", "sale", "--amount", "5", "--date", "2026-02-31", expect=2)
        self.assertEqual(len(self.csv_rows("ledger.csv")), 5)
        _, _, err = self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--product-id", "ghost")
        self.assertIn("ghost", err)  # 경고만, 기록은 됨
        self.assertRegex(self.csv_rows("ledger.csv")[-1]["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_request_approval_ids(self):
        self.add_product("p1")
        _, out, _ = self.tracker("request-approval", "--product-id", "p1", "--action", "publish_listing", "--note", "SHIP")
        today = C.today_kst().strftime("%Y%m%d")
        self.assertIn(f"approval_id: A-{today}-001", out)
        self.tracker("request-approval", "--product-id", "p1", "--action", "price_change")
        rows = self.csv_rows("approvals.csv")
        self.assertEqual([r["id"] for r in rows], [f"A-{today}-001", f"A-{today}-002"])
        self.assertEqual({r["status"] for r in rows}, {"pending"})
        self.assertEqual(rows[0]["decided_at"], "")
        self.assertRegex(rows[0]["requested_at"], ISO_KST)
        # 같은 (상품, 행동)의 대기 요청은 중복 생성하지 않고 기존 ID 를 돌려준다
        _, out, _ = self.tracker("request-approval", "--product-id", "p1", "--action", "publish_listing")
        self.assertIn(f"approval_id: A-{today}-001", out)
        self.assertEqual(len(self.csv_rows("approvals.csv")), 2)
        self.tracker("request-approval", "--product-id", "p1", "--action", "Publish Listing!", expect=2)

    def test_approve_requires_owner_flag(self):
        self.add_product("p1")
        self.tracker("request-approval", "--product-id", "p1", "--action", "publish_listing")
        aid = self.csv_rows("approvals.csv")[0]["id"]
        for verb in ("approve", "reject"):
            rc, out, err = self.tracker(verb, aid, expect=3)
            self.assertIn("--i-am-owner", err)
            self.assertIn("오너", err)
        row = self.csv_rows("approvals.csv")[0]
        self.assertEqual((row["status"], row["decided_at"]), ("pending", ""))  # 아무것도 바뀌지 않음

    def test_approve_and_reject_with_owner_flag(self):
        self.add_product("p1")
        self.tracker("request-approval", "--product-id", "p1", "--action", "publish_listing", "--note", "SHIP")
        self.tracker("request-approval", "--product-id", "p1", "--action", "price_change")
        a1, a2 = [r["id"] for r in self.csv_rows("approvals.csv")]
        self.tracker("approve", a1, "--i-am-owner", "--note", "좋아요")
        self.tracker("reject", a2, "--i-am-owner")
        rows = {r["id"]: r for r in self.csv_rows("approvals.csv")}
        self.assertEqual(rows[a1]["status"], "approved")
        self.assertRegex(rows[a1]["decided_at"], ISO_KST)
        self.assertEqual(rows[a1]["note"], "SHIP | 좋아요")
        self.assertEqual(rows[a2]["status"], "rejected")
        _, _, err = self.tracker("approve", a1, "--i-am-owner", expect=1)  # 재결정 불가
        self.assertIn("approved", err)
        self.tracker("approve", "A-19990101-001", "--i-am-owner", expect=1)
        # approved 이후 listed 로 바꾸면 경고가 없다
        _, _, err = self.tracker("set-status", "p1", "listed")
        self.assertNotIn("publish_listing", err)

    def seed_ledger(self):
        self.add_product("p1", title="노션 템플릿", platform="kmong")
        self.add_product("p2", title="가계부", platform="smartstore")
        data = [
            ("2026-09-10", "p1", "kmong", "sale", 10000), ("2026-09-10", "p1", "kmong", "fee", 1000),
            ("2026-10-01", "p2", "smartstore", "sale", 20000), ("2026-10-02", "p2", "smartstore", "refund", 5000),
            ("2026-10-03", "", "", "expense", 3000), ("2026-10-05", "p1", "kmong", "payout", 9000),
            ("2026-10-06", "p1", "kmong", "sale", 4000),
        ]
        for d, pid, plat, t, amt in data:
            args = ["add-ledger", "--type", t, "--amount", str(amt), "--date", d]
            if pid:
                args += ["--product-id", pid]
            if plat:
                args += ["--platform", plat]
            self.tracker(*args)

    def test_report_math(self):
        self.seed_ledger()
        _, out, _ = self.tracker("report", "--json")
        rep = json.loads(out)
        t = rep["totals"]
        # 판매 34000, 환불 5000 -> 순매출 29000 / 수수료 1000 + 비용 3000 -> 4000 / 순이익 25000 / 정산 9000(제외)
        self.assertEqual((t["sales"], t["refunds"], t["revenue"]), (34000, 5000, 29000))
        self.assertEqual((t["fees"], t["expenses"], t["costs"], t["net"], t["payouts"]), (1000, 3000, 4000, 25000, 9000))
        months = {m["key"]: m for m in rep["by_month"]}
        self.assertEqual(list(months), ["2026-09", "2026-10"])
        self.assertEqual((months["2026-09"]["revenue"], months["2026-09"]["net"]), (10000, 9000))
        self.assertEqual((months["2026-10"]["revenue"], months["2026-10"]["net"]), (19000, 16000))
        plats = {p["key"]: p for p in rep["by_platform"]}
        self.assertEqual(plats["kmong"]["revenue"], 14000)
        self.assertEqual(plats["smartstore"]["revenue"], 15000)
        self.assertIn("(미지정)", plats)
        self.assertEqual(plats["(미지정)"]["net"], -3000)
        prods = {p["key"]: p for p in rep["by_product"]}
        self.assertEqual((prods["p1"]["revenue"], prods["p1"]["title"]), (14000, "노션 템플릿"))
        self.assertEqual(prods["p2"]["revenue"], 15000)
        self.assertEqual(rep["by_product"][0]["key"], "p2")  # 순매출 내림차순
        self.assertEqual(rep["products"]["total"], 2)
        # 월 필터
        _, out, _ = self.tracker("report", "--json", "--month", "2026-10")
        self.assertEqual(json.loads(out)["totals"]["revenue"], 19000)
        self.tracker("report", "--month", "2026-13", expect=2)

    def test_report_markdown_and_text(self):
        self.seed_ledger()
        _, md, _ = self.tracker("report", "--md")
        self.assertTrue(md.startswith("# AI Revenue Engine 매출 리포트"))
        for heading in ("## 요약", "## 월별", "## 플랫폼별", "## 상품별", "## 상품 현황"):
            self.assertIn(heading, md)
        self.assertIn("| 합계 | 34,000 | 5,000 | 29,000 | 1,000 | 3,000 | 25,000 | 9,000 |", md)
        self.assertIn("| 2026-10 | 24,000 | 5,000 | 19,000 | 0 | 3,000 | 16,000 | 9,000 |", md)
        self.assertIn("p1 (노션 템플릿)", md)
        _, txt, _ = self.tracker("report")
        self.assertIn("29,000", txt)
        self.assertIn("[월별]", txt)

    def test_report_and_list_on_empty_root(self):
        _, out, _ = self.tracker("report")
        self.assertIn("장부 기록 없음", out)
        _, out, _ = self.tracker("report", "--md")
        self.assertIn("_장부 기록 없음_", out)
        _, out, _ = self.tracker("list")
        self.assertIn("[products] 0건", out)
        self.assertFalse((self.root / "tracker").exists())  # 읽기 전용 명령은 파일을 만들지 않는다

    def test_list_tables(self):
        self.seed_ledger()
        self.tracker("request-approval", "--product-id", "p1", "--action", "publish_listing")
        _, out, _ = self.tracker("list")
        for token in ("[products] 2건", "[approvals] 1건", "[ledger] 7건", "노션 템플릿", "p2", "smartstore", "pending"):
            self.assertIn(token, out)
        _, out, _ = self.tracker("list", "approvals", "--pending")
        self.assertIn("publish_listing", out)
        _, out, _ = self.tracker("list", "ledger", "--type", "refund")
        self.assertIn("[ledger] 1건", out)
        self.assertIn("-5,000", out)
        _, out, _ = self.tracker("list", "ledger", "--limit", "2")
        self.assertIn("최근 2건만", out)
        # 정렬: 모든 줄에서 'status' 열이 같은 표시 폭에서 시작
        _, out, _ = self.tracker("list", "products")
        lines = out.splitlines()[1:]
        starts = {C.disp_width(line[: line.index("draft")]) for line in lines[2:]}
        self.assertEqual(len(starts), 1, out)

    def test_header_mismatch_is_refused(self):
        self.tracker("init")
        (self.root / "tracker" / "ledger.csv").write_bytes(BOM + b"foo,bar\r\n")
        _, _, err = self.tracker("add-ledger", "--type", "sale", "--amount", "100", expect=1)
        self.assertIn("헤더", err)
        self.assertEqual((self.root / "tracker" / "ledger.csv").read_bytes(), BOM + b"foo,bar\r\n")

    def test_append_after_missing_trailing_newline(self):
        self.tracker("init")
        p = self.root / "tracker" / "ledger.csv"
        p.write_bytes(p.read_bytes().rstrip(b"\r\n"))  # 줄바꿈 없는 헤더만 있는 파일
        self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--date", "2026-10-01")
        self.assertEqual(len(self.csv_rows("ledger.csv")), 1)

    def test_concurrent_requests_get_distinct_ids(self):
        self.add_product("p1")
        env = {**os.environ, "AIRE_ROOT": str(self.root)}
        procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "tracker.py"), "request-approval", "--product-id", "p1",
                                   "--action", f"act_{i}"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(6)]
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        ids = [r["id"] for r in self.csv_rows("approvals.csv")]
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(list((self.root / "tracker").glob("*.lock")), [])


# ==========================================================================
# office_server.py  (/api/state, 정적 파일)
# ==========================================================================
class ServerTests(TempRootCase):
    def setUp(self) -> None:
        super().setUp()
        self.server, self.thread = office_server.serve_in_thread(self.root, 0)
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str, method: str = "GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def state(self) -> dict:
        status, headers, body = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(headers["content-type"].startswith("application/json"))
        return json.loads(body.decode("utf-8"))

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_empty_root_gives_valid_shape(self):
        s = self.state()
        self.assertEqual(set(s), {"now", "agents", "events", "kpis", "approvals", "products"})
        self.assertRegex(s["now"], ISO_KST)
        self.assertEqual((s["agents"], s["events"], s["approvals"], s["products"]), ([], [], [], []))
        k = s["kpis"]
        self.assertEqual(set(k), {"products_total", "by_status", "revenue_krw_total", "revenue_krw_this_month",
                                  "expenses_krw_total", "net_krw_total"})
        self.assertEqual((k["products_total"], k["revenue_krw_total"], k["revenue_krw_this_month"],
                          k["expenses_krw_total"], k["net_krw_total"]), (0, 0, 0, 0, 0))
        self.assertEqual(set(k["by_status"]), set(C.PRODUCT_STATUSES))
        self.assertEqual(set(k["by_status"].values()), {0})

    def test_populated_state(self):
        office.reset_office(root=self.root)
        office.set_status("maker-2", "working", "제작 중", product_id="p1", root=self.root)
        today = C.today_kst()
        prev_month_day = (today.replace(day=1) - dt.timedelta(days=1)).isoformat()
        for i in range(1, 26):
            tracker.add_product(f"p{i}", f"상품 {i}", batch="b1", platform="kmong", price_krw=1000 * i,
                                status="qa_passed" if i % 5 == 0 else None, root=self.root)
        with contextlib.redirect_stderr(io.StringIO()):  # 승인 없이 listed 로 바꾸는 경고 억제
            tracker.set_product_status("p1", "listed", root=self.root)
        tracker.add_ledger("sale", 10000, today.isoformat(), "p1", "kmong", root=self.root)
        tracker.add_ledger("sale", 20000, prev_month_day, "p1", "kmong", root=self.root)
        tracker.add_ledger("fee", 1000, today.isoformat(), "p1", "kmong", root=self.root)
        tracker.add_ledger("refund", 5000, today.isoformat(), "p1", "kmong", root=self.root)
        tracker.add_ledger("expense", 3000, prev_month_day, root=self.root)
        tracker.add_ledger("payout", 7000, today.isoformat(), root=self.root)  # 손익에 포함되지 않음
        tracker.request_approval("p1", "publish_listing", root=self.root)
        row, _ = tracker.request_approval("p2", "price_change", root=self.root)
        tracker.decide_approval(row["id"], "approved", root=self.root)
        for i in range(1, 101):
            office.log_event("ceo", "note", f"이벤트 {i}", root=self.root)

        s = self.state()
        self.assertEqual(len(s["agents"]), 11)
        self.assertEqual([a["id"] for a in s["agents"]][:6], ["ceo", "researcher", "pm", "designer", "maker-1", "maker-2"])
        maker2 = next(a for a in s["agents"] if a["id"] == "maker-2")
        self.assertEqual((maker2["state"], maker2["task"], maker2["product_id"]), ("working", "제작 중", "p1"))
        self.assertEqual(len(s["events"]), 80)
        self.assertEqual(s["events"][-1]["msg"], "이벤트 100")  # 최신이 마지막
        self.assertEqual(s["events"][0]["msg"], "이벤트 21")
        k = s["kpis"]
        self.assertEqual(k["products_total"], 25)
        self.assertEqual(k["by_status"]["listed"], 1)
        self.assertEqual(k["by_status"]["qa_passed"], 5)
        self.assertEqual(k["by_status"]["draft"], 19)
        self.assertEqual(k["revenue_krw_total"], 25000)       # 30000 - 5000
        self.assertEqual(k["revenue_krw_this_month"], 5000)   # 10000 - 5000
        self.assertEqual(k["expenses_krw_total"], 4000)       # 1000 + 3000
        self.assertEqual(k["net_krw_total"], 21000)
        self.assertEqual([a["product_id"] for a in s["approvals"]], ["p1"])
        self.assertEqual(s["approvals"][0]["status"], "pending")
        self.assertEqual(len(s["products"]), 20)
        self.assertEqual([p["id"] for p in s["products"]][-1], "p25")
        self.assertEqual(s["products"][-1]["price_krw"], 25000)  # 숫자로 변환

    def test_malformed_data_never_breaks_api(self):
        sdir = self.root / "office" / "status"
        sdir.mkdir(parents=True)
        (sdir / "broken.json").write_text("{not json", encoding="utf-8")
        (sdir / "list.json").write_text("[1, 2]", encoding="utf-8")
        (sdir / "empty.json").write_text("", encoding="utf-8")
        (sdir / "weird.json").write_text(json.dumps({"id": "qa-9", "state": "dancing", "task": None}), encoding="utf-8")
        (sdir / "ok.json").write_text(json.dumps({"id": "pm", "role": "pm", "state": "working", "task": "기획"},
                                                 ensure_ascii=False), encoding="utf-8")
        (sdir / ".tmp.json.123.tmp").write_text("{}", encoding="utf-8")
        (self.root / "office" / "events.jsonl").write_bytes(
            b'garbage\n{"ts":"x","id":"pm","msg":"ok"}\n[1,2,3]\n\xff\xfe broken\n{"msg": "\xed\x95\x9c\xea\xb8\x80"}\n{"cut":')
        tdir = self.root / "tracker"
        tdir.mkdir()
        (tdir / "ledger.csv").write_bytes(BOM + (
            "date,product_id,platform,type,amount_krw,note\r\n"
            "2026-10-01,p1,kmong,sale,\"12,000\",ok\r\n"
            "2026-10-01,p1,kmong,sale,abc,bad amount\r\n"
            "2026-10-01,p1,kmong,barter,500,bad type\r\n"
            "\r\n"
            "not-a-date,p1,kmong,expense,-700,undated\r\n"
            "2026-10-02,p1\r\n").encode("utf-8"))
        (tdir / "products.csv").write_bytes(BOM + (
            "id,batch,title,model,platform,price_krw,status,created_at,listed_url,notes\r\n"
            ",b,no id row,,,100,draft,,,\r\n"
            "p1,b,좋은 상품,,kmong,abc,mystery,,,\r\n"
            "p2,b,상품2,,kmong,5000,draft\r\n").encode("utf-8"))
        (tdir / "approvals.csv").write_bytes(os.urandom(500))

        s = self.state()
        ids = {a["id"]: a for a in s["agents"]}
        self.assertEqual(set(ids), {"qa-9", "pm"})
        self.assertEqual(ids["qa-9"]["state"], "idle")      # 알 수 없는 state -> idle
        self.assertEqual(ids["qa-9"]["task"], "")
        self.assertEqual(ids["pm"]["task"], "기획")
        self.assertEqual([e.get("msg") for e in s["events"]], ["ok", "한글"])
        k = s["kpis"]
        self.assertEqual(k["products_total"], 2)
        self.assertEqual(k["by_status"]["draft"], 1)
        self.assertEqual(k["by_status"]["mystery"], 1)
        self.assertEqual(k["revenue_krw_total"], 12000)
        self.assertEqual(k["expenses_krw_total"], 700)
        self.assertEqual(k["net_krw_total"], 11300)
        self.assertEqual([p["price_krw"] for p in s["products"]], [0, 5000])
        self.assertIsInstance(s["approvals"], list)

    def test_directory_instead_of_file_does_not_500(self):
        (self.root / "tracker" / "ledger.csv").mkdir(parents=True)
        (self.root / "office" / "events.jsonl").mkdir(parents=True)
        s = self.state()
        self.assertEqual(s["events"], [])
        self.assertEqual(s["kpis"]["revenue_krw_total"], 0)

    def test_query_string_head_and_unknown_api(self):
        status, _, _ = self.get("/api/state?cb=123")
        self.assertEqual(status, 200)
        status, headers, body = self.get("/api/state", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["cache-control"], "no-store")
        status, _, _ = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_index_html_and_static_files(self):
        odir = self.root / "office"
        odir.mkdir(parents=True, exist_ok=True)
        (odir / "index.html").write_text("<!doctype html><title>오피스</title>", encoding="utf-8")
        (odir / "app.js").write_text("console.log('안녕')", encoding="utf-8")
        (odir / "style.css").write_text("body{}", encoding="utf-8")
        (odir / "sprites").mkdir()
        (odir / "sprites" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        for path in ("/", "/index.html", "/?x=1"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("오피스".encode("utf-8"), body)
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertIn("text/html", headers["content-type"])
        status, headers, body = self.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        self.assertIn("utf-8", headers["content-type"])
        self.assertIn("text/css", self.get("/style.css")[1]["content-type"])
        self.assertEqual(self.get("/sprites/a.png")[1]["content-type"], "image/png")
        self.assertEqual(self.get("/missing.txt")[0], 404)
        self.assertEqual(self.get("/sprites/")[0], 404)  # 디렉터리 목록 노출 안 함

    def test_missing_index_gives_friendly_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("index.html".encode("utf-8"), body)
        self.assertIn("/api/state".encode("utf-8"), body)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_path_traversal_blocked(self):
        (self.root / "office").mkdir(parents=True, exist_ok=True)
        (self.root / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
        for path in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/..%5csecret.txt",
                     "/sub/../../secret.txt", "/%2e%2e%5csecret.txt"):
            status, _, body = self.get(path)
            self.assertNotIn(b"TOP-SECRET", body, path)
            self.assertIn(status, (301, 400, 404), path)

    def test_live_update_visible_without_restart(self):
        self.assertEqual(self.state()["agents"], [])
        office.set_status("designer", "meeting", "회의", root=self.root)
        agents = self.state()["agents"]
        self.assertEqual([(a["id"], a["state"]) for a in agents], [("designer", "meeting")])
        office.set_status("designer", "done", "완료", root=self.root)
        self.assertEqual(self.state()["agents"][0]["state"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
