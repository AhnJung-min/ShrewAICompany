# -*- coding: utf-8 -*-
"""QA 에서 찾아 고친 결함의 회귀 테스트 + 병렬 부하 + /api/state 계약 형태 검사.

실행 (프로젝트 루트에서):  python -m unittest discover -s tests -v
또는 어디서든:            python <프로젝트 루트>/tests/test_regressions.py -v

모든 테스트는 임시 폴더를 AIRE_ROOT 로 쓴다 (실제 데이터를 건드리지 않음).
"""
from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import math
import os
import socket
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
for _p in (str(HERE), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _common as C  # noqa: E402
import office  # noqa: E402
import office_server  # noqa: E402
import tracker  # noqa: E402
from test_backend import BOM, ISO_KST, TempRootCase  # noqa: E402


# ==========================================================================
# 공용 헬퍼
# ==========================================================================
class CommonRegressionTests(unittest.TestCase):
    def test_parse_int_is_exact_for_big_numbers_and_rounds_half_up(self):
        self.assertEqual(C.parse_int("9007199254740993"), 9007199254740993)  # float 로 변환하면 ...992 가 되던 결함
        self.assertEqual(C.parse_int("12.5"), 13)   # 은행가 반올림(12)이 아니라 사사오입
        self.assertEqual(C.parse_int("13.5"), 14)
        self.assertEqual(C.parse_int("1500.0"), 1500)
        self.assertEqual(C.parse_int("-0.4"), 0)

    def test_parse_date_rejects_trailing_garbage(self):
        for bad in ("2026-10-1599", "2026-10-15abc", "2026-10-15x", "2026-10-155", "20261015x"):
            self.assertIsNone(C.parse_date(bad), bad)
        for ok in ("2026-10-15", " 2026-10-15 ", "2026-10-15T14:03:22+09:00", "2026-10-15 14:03", "2026/1/5", "20261015"):
            self.assertIsNotNone(C.parse_date(ok), ok)

    def test_clean_text_drops_control_chars_and_lone_surrogates(self):
        self.assertEqual(C.clean_text("a\x01b\x00c\x7fd"), "abcd")
        self.assertEqual(C.clean_text("줄1\n줄2\t\u2028줄3\x1f줄4"), "줄1 줄2 줄3 줄4")
        out = C.clean_text("앞\ud800뒤")  # 짝 없는 서로게이트: UTF-8 저장 시 UnicodeEncodeError 가 나던 값
        self.assertEqual(out, "앞뒤")
        out.encode("utf-8")

    def test_json_loads_safe_survives_deep_nesting(self):
        self.assertIsNone(C.json_loads_safe("[" * 200000))
        self.assertIsNone(C.json_loads_safe("{not json"))
        self.assertEqual(C.json_loads_safe('{"a": 1}'), {"a": 1})

    def test_json_safe_replaces_non_finite_numbers(self):
        out = C.json_safe({"a": float("nan"), "b": [float("inf"), -math.inf, 1.5], "c": {"d": float("nan")}})
        self.assertEqual(out, {"a": None, "b": [None, None, 1.5], "c": {"d": None}})
        json.dumps(out, allow_nan=False)

    def test_deeply_nested_jsonl_line_is_skipped_not_fatal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e.jsonl"
            p.write_bytes(b'{"i": 1}\n' + b"[" * 200000 + b'\n{"i": 2}\n')
            self.assertEqual([e["i"] for e in C.read_jsonl_tail(p, 10)], [1, 2])

    def test_csv_unbalanced_quote_does_not_swallow_following_rows(self):
        import tempfile
        header = "id,batch,title,model,platform,price_krw,status,created_at,listed_url,notes\r\n"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.csv"
            # (1) 가운데 행의 제목에 짝 없는 따옴표
            p.write_bytes(BOM + (header + 'p1,b,정상1,,,100,draft,,,\r\np2,b,"깨진 제목,,,200,draft,,,\r\n'
                                 'p3,b,정상3,,,300,qa_passed,,,\r\np4,b,정상4,,,400,draft,,,\r\n').encode("utf-8"))
            rows = C.read_csv_rows(p)
            self.assertEqual([r["id"] for r in rows], ["p1", "p2", "p3", "p4"])
            self.assertEqual(rows[1]["title"], "깨진 제목")
            self.assertEqual(rows[2]["status"], "qa_passed")
            # (2) 마지막 열(notes)에서 열린 따옴표 - 열 개수가 우연히 맞아도 뒤 행을 삼키지 않는다
            p.write_bytes(BOM + (header + 'p1,b,t1,,,1,draft,,,"메모 시작\r\np2,b,t2,,,2,draft,,,\r\np3,b,t3,,,3,draft,,,\r\n').encode("utf-8"))
            self.assertEqual([r["id"] for r in C.read_csv_rows(p)], ["p1", "p2", "p3"])
            # (3) 정상적인 여러 줄 칸(엑셀 Alt+Enter)은 그대로 한 칸
            p.write_bytes(BOM + (header + 'p1,b,t1,,,1,draft,,,"첫 줄\r\n둘째 줄"\r\np2,b,t2,,,2,draft,,,\r\n').encode("utf-8"))
            rows = C.read_csv_rows(p)
            self.assertEqual([r["id"] for r in rows], ["p1", "p2"])
            self.assertEqual(rows[0]["notes"], "첫 줄\r\n둘째 줄")
            # (4) 따옴표가 필드 중간에 있는 건 글자 그대로 (5" 모니터)
            p.write_bytes(BOM + (header + 'p1,b,5" 모니터 거치대,,,1,draft,,,\r\np2,b,t2,,,2,draft,,,\r\n').encode("utf-8"))
            rows = C.read_csv_rows(p)
            self.assertEqual((rows[0]["title"], rows[1]["id"]), ('5" 모니터 거치대', "p2"))

    def test_csv_header_is_case_insensitive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.csv"
            p.write_bytes(BOM + b"ID,Title\r\np1,hello\r\n")
            self.assertEqual(C.read_csv_rows(p), [{"id": "p1", "title": "hello"}])
            C.check_csv_header(p, ["id", "title"])  # 예외 없음

    def test_decode_csv_bytes_handles_excel_encodings(self):
        text = "id,title\r\np1,한글 제목\r\n"
        self.assertEqual(C.decode_csv_bytes(text.encode("cp949")), text)             # 한국어 엑셀 'CSV(쉼표로 분리)'
        self.assertEqual(C.decode_csv_bytes(BOM + text.encode("utf-8")), text)
        self.assertEqual(C.decode_csv_bytes(text.encode("utf-8")), text)
        self.assertEqual(C.decode_csv_bytes(text.encode("utf-16")), text)             # '유니코드 텍스트'/UTF-16 BOM

    def test_csv_safe_cell_blocks_formula_injection_but_keeps_numbers(self):
        self.assertEqual(C.csv_safe_cell("title", '=HYPERLINK("http://x","y")'), "'=HYPERLINK(\"http://x\",\"y\")")
        self.assertEqual(C.csv_safe_cell("note", "@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(C.csv_safe_cell("note", "+cmd|' /C calc'!A0"), "'+cmd|' /C calc'!A0")
        self.assertEqual(C.csv_safe_cell("note", "-5% 할인"), "'-5% 할인")
        self.assertEqual(C.csv_safe_cell("note", "- 목록 항목"), "- 목록 항목")      # 대시 뒤 공백: 수식 아님
        self.assertEqual(C.csv_safe_cell("note", "-3000원"), "-3000원")              # 순수 숫자
        self.assertEqual(C.csv_safe_cell("amount_krw", "-990"), "-990")             # 금액 열
        self.assertEqual(C.csv_safe_cell("title", "일반 제목"), "일반 제목")
        self.assertEqual(C.csv_safe_cell("title", "'=이미 방어됨"), "'=이미 방어됨")  # 이중 방어 안 함

    def test_protect_dash_text_only_touches_dash_texts(self):
        p = argparse.ArgumentParser()
        p.add_argument("--note")
        p.add_argument("--amount")
        p.add_argument("--flag", action="store_true")
        p.add_argument("words", nargs="*")
        args = C.parse_cli(p, ["--note", "-3000원", "--amount", "-9900", "--flag", "-5%", "-긴급", "단어"])
        self.assertEqual((args.note, args.amount, args.flag), ("-3000원", "-9900", True))
        self.assertEqual(args.words, ["-5%", "-긴급", "단어"])
        with self.assertRaises(SystemExit):   # 오타 난 옵션은 여전히 오류
            with mock.patch("sys.stderr", io.StringIO()):
                C.parse_cli(p, ["--nope", "x"])
        with self.assertRaises(SystemExit):   # -x 처럼 ASCII 글자 하나짜리는 옵션 오타로 취급
            with mock.patch("sys.stderr", io.StringIO()):
                C.parse_cli(p, ["-x"])

    def test_describe_oserror_explains_locked_files(self):
        msg = C.describe_oserror(PermissionError(13, "denied", "C:/x/products.csv"))
        self.assertIn("엑셀", msg)
        self.assertIn("products.csv", msg)
        self.assertNotIn("엑셀", C.describe_oserror(OSError(28, "no space")))


# ==========================================================================
# office.py
# ==========================================================================
class OfficeRegressionTests(TempRootCase):
    def test_unquoted_multiword_task_is_joined(self):
        self.office("status", "ceo", "working", "배치", "킥오프", "시작", "--bubble", "💡")
        self.assertEqual(self.status_file("ceo")["task"], "배치 킥오프 시작")
        self.office("event", "ceo", "note", "따옴표", "없는", "메시지")
        self.assertEqual(self.events()[-1]["msg"], "따옴표 없는 메시지")

    def test_option_before_positional_task(self):
        self.office("status", "ceo", "working", "--product", "p1", "옵션이 앞에 와도")
        self.assertEqual(self.status_file("ceo")["task"], "옵션이 앞에 와도")

    def test_dash_leading_text_is_text_not_option(self):
        self.office("event", "cfo", "note", "-5%")
        self.office("event", "cfo", "note", "-3000원", "수수료", "누락")
        self.office("status", "cfo", "working", "-긴급", "정산")
        self.assertEqual([e["msg"] for e in self.events()], ["-5%", "-3000원 수수료 누락", "-긴급 정산"])
        self.assertEqual(self.status_file("cfo")["task"], "-긴급 정산")
        self.office("status", "cfo", "working", "x", "--prodcut", "p1", expect=2)  # 옵션 오타는 여전히 오류

    def test_bubble_newlines_and_control_chars_are_flattened(self):
        self.office("status", "pm", "working", "x", "--bubble", "a\nb")
        self.assertEqual(self.status_file("pm")["bubble"], "a b")
        self.office("status", "pm", "working", "x", "--bubble", "\n\t")  # 공백뿐 -> 기본 말풍선
        self.assertEqual(self.status_file("pm")["bubble"], C.ROLES["pm"]["bubble"])

    def test_instance_number_is_normalised(self):
        self.office("status", "qa-01", "working", "x")
        self.assertTrue((self.root / "office" / "status" / "qa-1.json").exists())
        self.assertFalse((self.root / "office" / "status" / "qa-01.json").exists())
        self.assertEqual(self.status_file("qa-1")["id"], "qa-1")
        self.assertEqual(self.events()[-1]["id"], "qa-1")

    def test_empty_event_message_is_rejected(self):
        self.office("event", "ceo", "note", "", expect=2)
        self.office("event", "ceo", "note", "   ", expect=2)
        self.assertEqual(self.events(), [])

    def test_control_chars_never_reach_the_log(self):
        self.office("event", "ceo", "note", "a\x01b\x1fc\u2028d\x0be")
        raw = (self.root / "office" / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(self.events()[0]["msg"], "ab c d e")
        self.assertNotIn("\\u0001", raw)

    def test_twelve_parallel_cli_calls_keep_everything_intact(self):
        ids = ["ceo", "researcher-1", "researcher-2", "pm", "designer", "maker-1", "maker-2", "maker-3",
               "qa-1", "qa-2", "marketer", "cfo"]
        jobs = []
        for r in range(8):
            for iid in ids:
                jobs.append(("status", iid, ["working", "meeting", "blocked", "done", "idle"][r % 5], f"라운드{r} {iid} 😀 " + "가" * 150))
                jobs.append(("event", iid, "handoff", f"이벤트 r{r} {iid} " + "나" * 300))
            jobs += [("status", "maker-1", "working", f"경합 {r}-{k}") for k in range(4)]
        env = {**os.environ, "AIRE_ROOT": str(self.root)}

        def run(job):
            p = subprocess.run([sys.executable, str(SCRIPTS / "office.py"), *job], capture_output=True, env=env, timeout=120)
            return p.returncode, p.stderr.decode("utf-8", "replace")

        with ThreadPoolExecutor(12) as ex:
            results = list(ex.map(run, jobs))
        self.assertEqual([r for r in results if r[0] != 0 or r[1]], [])
        raw = (self.root / "office" / "events.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        lines = raw.split(b"\n")[:-1]
        self.assertEqual(len(lines), len(jobs))                       # 한 줄도 잃거나 합쳐지지 않음
        for ln in lines:
            ev = json.loads(ln.decode("utf-8"))                       # 모든 줄이 온전한 JSON
            self.assertIn(ev["type"], C.EVENT_TYPES)
        sdir = self.root / "office" / "status"
        self.assertEqual(sorted(p.stem for p in sdir.glob("*.json")), sorted(ids))
        self.assertEqual([p for p in sdir.iterdir() if p.suffix != ".json"], [])   # 임시 파일 흔적 없음
        for p in sdir.glob("*.json"):
            rec = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(rec["id"], p.stem)
            self.assertIn(rec["state"], C.STATES)


# ==========================================================================
# tracker.py
# ==========================================================================
class TrackerRegressionTests(TempRootCase):
    def add_product(self, pid="p1", **kw):
        args = ["add-product", "--id", pid, "--title", kw.pop("title", "테스트 상품")]
        for k, v in kw.items():
            args += [f"--{k.replace('_', '-')}", str(v)]
        return self.tracker(*args)

    def test_ledger_date_typos_are_rejected(self):
        for bad in ("2026-10-1599", "2026-10-15abc", "10/15/2026"):
            self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--date", bad, expect=2)
        self.assertFalse((self.root / "tracker" / "ledger.csv").exists())

    def test_amount_precision_and_absurd_amounts(self):
        self.tracker("add-ledger", "--type", "sale", "--amount", "99999999999", "--date", "2026-10-01")
        self.assertEqual(self.csv_rows("ledger.csv")[0]["amount_krw"], "99999999999")   # 정확히 저장
        for bad in ("9007199254740993", "100000000001", "99999999999999999999"):
            _, _, err = self.tracker("add-ledger", "--type", "sale", "--amount", bad, expect=2)
            self.assertIn("너무 큽니다", err)
        self.tracker("add-product", "--id", "p9", "--title", "t", "--price-krw", "99999999999999999999", expect=2)
        self.assertEqual(len(self.csv_rows("ledger.csv")), 1)

    def test_listed_url_must_be_http(self):
        for bad in ("javascript:alert(1)", "data:text/html,<b>", "ftp://x.com/a", "https://x.com/a b", "x.com"):
            self.tracker("add-product", "--id", "p1", "--title", "t", "--listed-url", bad, expect=2)
        self.assertFalse((self.root / "tracker" / "products.csv").exists())
        self.add_product("p1", listed_url="https://kmong.com/gig/123")
        self.assertEqual(self.csv_rows("products.csv")[0]["listed_url"], "https://kmong.com/gig/123")
        self.tracker("set-status", "p1", "listed", "--listed-url", "javascript:alert(1)", expect=2)
        self.assertEqual(self.csv_rows("products.csv")[0]["status"], "draft")   # 거절되면 아무것도 안 바뀐다

    def test_add_product_as_listed_without_approval_warns(self):
        _, _, err = self.tracker("add-product", "--id", "p1", "--title", "t", "--status", "listed")
        self.assertIn("publish_listing", err)
        _, _, err = self.tracker("add-product", "--id", "p2", "--title", "t", "--status", "draft")
        self.assertNotIn("publish_listing", err)

    def test_negative_or_dash_text_arguments(self):
        self.tracker("add-ledger", "--type", "fee", "--amount", "-3000원", "--date", "2026-10-01", "--note", "-3000원 수수료")
        self.tracker("request-approval", "--action", "create_account", "--note", "-긴급")
        self.assertEqual(self.csv_rows("ledger.csv")[0]["amount_krw"], "-3000")
        self.assertIn("긴급", self.csv_rows("approvals.csv")[0]["note"])

    def test_abbreviated_owner_flag_cannot_approve(self):
        self.tracker("request-approval", "--action", "create_account")
        aid = self.csv_rows("approvals.csv")[0]["id"]
        for flag in ("--i-am", "--i-am-o", "--owner", "-i"):
            self.tracker("approve", aid, flag, expect=2)
            self.tracker("reject", aid, flag, expect=2)
        self.assertEqual(self.csv_rows("approvals.csv")[0]["status"], "pending")
        self.tracker("approve", aid, expect=3)

    def test_concurrent_approve_and_reject_has_one_winner(self):
        self.tracker("request-approval", "--action", "create_account")
        aid = self.csv_rows("approvals.csv")[0]["id"]
        env = {**os.environ, "AIRE_ROOT": str(self.root)}

        def go(verb):
            return subprocess.run([sys.executable, str(SCRIPTS / "tracker.py"), verb, aid, "--i-am-owner"],
                                  capture_output=True, env=env, timeout=120).returncode

        with ThreadPoolExecutor(8) as ex:
            rcs = list(ex.map(go, ["approve", "reject"] * 4))
        self.assertEqual(sorted(rcs), [0] + [1] * 7)
        self.assertIn(self.csv_rows("approvals.csv")[0]["status"], ("approved", "rejected"))

    def test_formula_injection_is_neutralised_on_write(self):
        self.add_product("p1", title='=HYPERLINK("http://evil","click")', notes="@SUM(A1:A9)")
        self.tracker("request-approval", "--product-id", "p1", "--action", "send_proposal", "--note", "=cmd|' /C calc'!A0")
        self.tracker("add-ledger", "--type", "expense", "--amount", "500", "--date", "2026-10-01", "--note", "+1-555-0100")
        raw = (self.root / "tracker" / "products.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn(',=HYPERLINK', raw)
        self.assertTrue(self.csv_rows("products.csv")[0]["title"].startswith("'="))
        self.assertTrue(self.csv_rows("products.csv")[0]["notes"].startswith("'@"))
        self.assertTrue(self.csv_rows("approvals.csv")[0]["note"].startswith("'="))
        self.assertTrue(self.csv_rows("ledger.csv")[0]["note"].startswith("'+"))
        self.assertEqual(self.csv_rows("ledger.csv")[0]["amount_krw"], "-500")      # 금액은 그대로 음수
        self.tracker("set-status", "p1", "qa_passed")                                  # 다시 써도 ' 가 겹치지 않는다
        self.assertEqual(self.csv_rows("products.csv")[0]["title"], '\'=HYPERLINK("http://evil","click")')

    def test_blank_csv_files_are_recreated_with_header(self):
        (self.root / "tracker").mkdir()
        for name, content in (("products.csv", BOM), ("ledger.csv", b"\r\n"), ("approvals.csv", b"   \n\n")):
            (self.root / "tracker" / name).write_bytes(content)
        self.add_product("p1")
        self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--date", "2026-10-01")
        self.tracker("request-approval", "--action", "create_account")
        self.assertEqual(len(self.csv_rows("products.csv")), 1)
        self.assertEqual(len(self.csv_rows("ledger.csv")), 1)
        self.assertEqual(len(self.csv_rows("approvals.csv")), 1)
        self.assertTrue((self.root / "tracker" / "ledger.csv").read_bytes().startswith(BOM))

    def test_cp949_csv_saved_by_excel_is_converted_not_mixed(self):
        (self.root / "tracker").mkdir()
        header = ",".join(C.PRODUCT_COLS)
        (self.root / "tracker" / "products.csv").write_bytes(
            (header + "\r\np1,b1,한글 제목,template,kmong,9900,draft,,,메모\r\n").encode("cp949"))
        self.assertIn("한글 제목", self.tracker("list", "products")[1])
        self.add_product("p2", title="새 상품")            # UTF-8 줄을 CP949 파일 뒤에 그냥 붙이면 인코딩이 섞이던 결함
        raw = (self.root / "tracker" / "products.csv").read_bytes()
        self.assertTrue(raw.startswith(BOM))
        raw.decode("utf-8")                                 # 엄격한 UTF-8 로 디코드 가능
        rows = self.csv_rows("products.csv")
        self.assertEqual([(r["id"], r["title"]) for r in rows], [("p1", "한글 제목"), ("p2", "새 상품")])
        self.assertEqual(rows[0]["notes"], "메모")

    def test_utf8_without_bom_gets_bom_on_next_write(self):
        (self.root / "tracker").mkdir()
        (self.root / "tracker" / "ledger.csv").write_bytes((",".join(C.LEDGER_COLS) + "\n").encode("utf-8"))
        self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--date", "2026-10-01", "--note", "한글")
        self.assertTrue((self.root / "tracker" / "ledger.csv").read_bytes().startswith(BOM))   # 엑셀에서 한글이 안 깨짐
        self.assertEqual(self.csv_rows("ledger.csv")[0]["note"], "한글")

    def test_unbalanced_quote_keeps_later_rows_visible_and_editable(self):
        (self.root / "tracker").mkdir()
        (self.root / "tracker" / "products.csv").write_bytes(BOM + (
            ",".join(C.PRODUCT_COLS) + '\r\np1,b,"깨진 제목,,,100,draft,,,\r\np2,b,정상2,,,200,draft,,,\r\n').encode("utf-8"))
        _, out, _ = self.tracker("list", "products")
        self.assertIn("[products] 2건", out)
        self.tracker("set-status", "p2", "qa_passed")          # 뒤 행이 삼켜져 'p2 를 찾을 수 없습니다' 가 되던 결함
        self.add_product("p3")
        self.assertEqual([r["id"] for r in self.csv_rows("products.csv")], ["p1", "p2", "p3"])
        self.assertEqual({r["id"]: r["status"] for r in self.csv_rows("products.csv")}["p2"], "qa_passed")

    def test_header_case_difference_is_tolerated(self):
        (self.root / "tracker").mkdir()
        (self.root / "tracker" / "products.csv").write_bytes(
            BOM + b"ID,Batch,Title,Model,Platform,Price_KRW,Status,Created_At,Listed_URL,Notes\r\np1,b,t,,,100,draft,,,\r\n")
        self.assertIn("[products] 1건", self.tracker("list", "products")[1])
        self.tracker("set-status", "p1", "qa_passed")
        self.assertEqual(self.csv_rows("products.csv")[0]["status"], "qa_passed")

    def test_notes_do_not_grow_without_bound(self):
        self.add_product("p1")
        for i in range(40):
            self.tracker("set-status", "p1", "in_production", "--note", f"갱신 {i} " + "가" * 100)
        notes = self.csv_rows("products.csv")[0]["notes"]
        self.assertLessEqual(len(notes), tracker.NOTES_MAX)
        self.assertIn("갱신 39", notes)                       # 최신 내용은 남는다

    def test_abandoned_lock_file_heals_quickly(self):
        # 프로세스가 잠금 구간에서 강제 종료되어 남은 .lock 이 60초 가까이 모든 쓰기를 막던 문제
        import time
        self.tracker("init")
        lock = self.root / "tracker" / "ledger.csv.lock"
        lock.write_bytes(b"")
        old = time.time() - 40
        os.utime(lock, (old, old))
        t0 = time.monotonic()
        self.tracker("add-ledger", "--type", "sale", "--amount", "100", "--date", "2026-10-01")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertFalse(lock.exists())
        self.assertEqual(len(self.csv_rows("ledger.csv")), 1)

    def test_locked_csv_gives_actionable_message(self):
        # 엑셀이 파일을 열고 있을 때(쓰기 불가)의 안내 문구. 실제 대기(수 초)는 모의로 건너뛴다.
        err = io.StringIO()
        with mock.patch.object(C, "append_bytes", side_effect=PermissionError(13, "denied", "ledger.csv")), \
                mock.patch("sys.stderr", err):
            rc = tracker.main(["add-ledger", "--type", "sale", "--amount", "100", "--date", "2026-10-01"])
        self.assertEqual(rc, 1)
        self.assertIn("엑셀", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


# ==========================================================================
# office_server.py : 방어 + /api/state 계약 형태
# ==========================================================================
class ServerRegressionTests(TempRootCase):
    def setUp(self) -> None:
        super().setUp()
        self.server, self.thread = office_server.serve_in_thread(self.root, 0)
        self.port = self.server.server_address[1]
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def raw_get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def state(self) -> dict:
        status, body = self.raw_get("/api/state")
        self.assertEqual(status, 200)

        def no_constants(name):
            raise AssertionError(f"표준 JSON 이 아닌 값: {name}")

        return json.loads(body.decode("utf-8"), parse_constant=no_constants)  # NaN/Infinity 가 있으면 실패

    def write_status(self, name: str, content: str | bytes) -> None:
        d = self.root / "office" / "status"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    # -- 방어 ---------------------------------------------------------------
    def test_one_pathological_status_file_does_not_hide_every_agent(self):
        office.reset_office(root=self.root)
        self.write_status("deep.json", "[" * 200000)           # RecursionError -> 예전에는 직원 전원이 사라짐
        self.write_status("nan.json", '{"id": "qa-9", "state": "idle", "x": NaN, "y": [Infinity]}')
        self.write_status("ids.json", '{"id": ["a"], "role": {"b": 1}, "state": ["working"]}')
        self.write_status("utf16.json", "{}".encode("utf-16"))
        s = self.state()
        ids = [a["id"] for a in s["agents"]]
        for iid in C.DEFAULT_STAFF:
            self.assertIn(iid, ids)
        self.assertIn("qa-9", ids)
        self.assertIn("ids", ids)                                # 문자열이 아닌 id 는 파일 이름으로 대체
        weird = next(a for a in s["agents"] if a["id"] == "ids")
        self.assertEqual((weird["role"], weird["state"]), ("ids", "idle"))
        nan_agent = next(a for a in s["agents"] if a["id"] == "qa-9")
        self.assertIsNone(nan_agent["x"])

    def test_one_pathological_event_line_does_not_hide_every_event(self):
        office.log_event("ceo", "note", "앞", root=self.root)
        with open(self.root / "office" / "events.jsonl", "ab") as fh:
            fh.write(b"[" * 200000 + '\n{"ts": "t", "id": "pm", "msg": "수상한", "n": NaN}\n'.encode("utf-8"))
        office.log_event("ceo", "note", "뒤", root=self.root)
        self.assertEqual([e["msg"] for e in self.state()["events"]], ["앞", "수상한", "뒤"])

    def test_events_always_carry_contract_keys_as_strings(self):
        (self.root / "office").mkdir(parents=True)
        (self.root / "office" / "events.jsonl").write_text('{"msg": 5}\n{"ts": null, "id": "pm"}\n{}\n', encoding="utf-8")
        for ev in self.state()["events"]:
            for key in ("ts", "id", "role", "type", "msg"):
                self.assertIsInstance(ev[key], str, (key, ev))

    def test_approvals_rows_always_have_all_columns(self):
        (self.root / "tracker").mkdir()
        (self.root / "tracker" / "approvals.csv").write_bytes(
            BOM + b"id,product_id,action,status\r\nA-1,p1,publish_listing,pending\r\nA-2,p1,x,approved\r\n")
        rows = self.state()["approvals"]
        self.assertEqual([r["id"] for r in rows], ["A-1"])
        self.assertEqual(set(rows[0]), set(C.APPROVAL_COLS))

    def test_null_byte_in_path_is_a_400_and_server_survives(self):
        (self.root / "office").mkdir(parents=True)
        status, _ = self.raw_get("/%00")
        self.assertEqual(status, 400)
        self.assertEqual(self.raw_get("/api/state")[0], 200)

    def test_idle_connection_does_not_block_api(self):
        idle = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(idle.close)
        self.assertEqual(self.raw_get("/api/state")[0], 200)
        self.assertEqual(office_server.OfficeHandler.timeout, 30)

    # -- /api/state 계약 형태 (docs/90_시스템_인터페이스.md §2-4) -------------
    def test_api_state_matches_contract_shape_with_real_data(self):
        office.reset_office(root=self.root)
        office.set_status("maker-2", "working", "제작 중", bubble="💻", product_id="notion-job-hunt-kit", root=self.root)
        office.log_event("qa-1", "defect", "표지 누락", product_id="notion-job-hunt-kit", root=self.root)
        tracker.add_product("notion-job-hunt-kit", "노션 취업 준비 템플릿", batch="batch-001", platform="kmong",
                            price_krw=9900, root=self.root)
        tracker.add_ledger("sale", 9900, C.today_kst().isoformat(), "notion-job-hunt-kit", "kmong", root=self.root)
        tracker.add_ledger("fee", 990, C.today_kst().isoformat(), "notion-job-hunt-kit", "kmong", root=self.root)
        tracker.request_approval("notion-job-hunt-kit", "publish_listing", root=self.root)

        s = self.state()
        self.assertEqual(set(s), {"now", "agents", "events", "kpis", "approvals", "products"})
        self.assertRegex(s["now"], ISO_KST)

        self.assertIsInstance(s["agents"], list)
        self.assertEqual(len(s["agents"]), 11)
        for a in s["agents"]:
            for key in ("id", "role", "state", "task", "bubble", "updated_at"):
                self.assertIsInstance(a[key], str, (key, a))
            self.assertIn(a["state"], C.STATES)
            self.assertEqual(a["role"], C.role_of(a["id"]))
            self.assertRegex(a["updated_at"], ISO_KST)
            if "product_id" in a:
                self.assertIsInstance(a["product_id"], str)
        m2 = next(a for a in s["agents"] if a["id"] == "maker-2")
        self.assertEqual((m2["state"], m2["bubble"], m2["product_id"]), ("working", "💻", "notion-job-hunt-kit"))

        self.assertLessEqual(len(s["events"]), 80)
        types = [e["type"] for e in s["events"]]
        self.assertEqual(types[-1], "defect")
        for e in s["events"]:
            for key in ("ts", "id", "role", "type", "msg"):
                self.assertIsInstance(e[key], str)
            self.assertIn(e["type"], C.EVENT_TYPES)
        self.assertEqual(s["events"][-1]["product_id"], "notion-job-hunt-kit")

        k = s["kpis"]
        self.assertEqual(set(k), {"products_total", "by_status", "revenue_krw_total", "revenue_krw_this_month",
                                  "expenses_krw_total", "net_krw_total"})
        for key in ("products_total", "revenue_krw_total", "revenue_krw_this_month", "expenses_krw_total", "net_krw_total"):
            self.assertIs(type(k[key]), int, key)
        self.assertEqual((k["products_total"], k["revenue_krw_total"], k["revenue_krw_this_month"],
                          k["expenses_krw_total"], k["net_krw_total"]), (1, 9900, 9900, 990, 8910))
        self.assertEqual(set(k["by_status"]), set(C.PRODUCT_STATUSES))
        self.assertTrue(all(type(v) is int for v in k["by_status"].values()))
        self.assertEqual(k["by_status"]["draft"], 1)

        self.assertEqual(len(s["approvals"]), 1)
        self.assertEqual(set(s["approvals"][0]), set(C.APPROVAL_COLS))
        self.assertEqual(s["approvals"][0]["status"], "pending")

        self.assertEqual(len(s["products"]), 1)
        p = s["products"][0]
        self.assertEqual(set(p), set(C.PRODUCT_COLS))
        self.assertIs(type(p["price_krw"]), int)
        self.assertEqual((p["id"], p["price_krw"], p["status"]), ("notion-job-hunt-kit", 9900, "draft"))

    def test_api_state_shape_with_every_tracker_file_missing(self):
        office.set_status("ceo", "working", "x", root=self.root)       # 트래커 폴더가 아예 없음
        s = self.state()
        self.assertEqual(set(s), {"now", "agents", "events", "kpis", "approvals", "products"})
        self.assertEqual(len(s["agents"]), 1)
        self.assertEqual((s["approvals"], s["products"]), ([], []))
        self.assertEqual(s["kpis"]["products_total"], 0)
        self.assertEqual(set(s["kpis"]["by_status"]), set(C.PRODUCT_STATUSES))
        for name in ("products.csv", "ledger.csv", "approvals.csv"):  # 하나씩만 있는 경우도
            self.assertFalse((self.root / "tracker" / name).exists())
        (self.root / "tracker").mkdir()
        (self.root / "tracker" / "ledger.csv").write_bytes(BOM + b"date,product_id,platform,type,amount_krw,note\r\n"
                                                           + f"{C.today_kst().isoformat()},,,sale,1000,x\r\n".encode())
        s = self.state()
        self.assertEqual((s["kpis"]["revenue_krw_total"], s["kpis"]["products_total"]), (1000, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
