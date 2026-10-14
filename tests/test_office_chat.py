# -*- coding: utf-8 -*-
"""대표 명령창 백엔드 테스트: scripts/office_chat.py + office_server.py 의 /api/chat* 라우트

실행 (프로젝트 루트에서):
    python -m unittest discover -s tests -v

진짜 Claude CLI 는 부르지 않는다. 임시 폴더에 '가짜 CLI'(fake_claude.py)를 만들고 SHREW_CLAUDE_BIN 으로 가리킨다.
가짜 CLI 는 프롬프트 속 키워드(LOGIN / CRASH / SLEEP / GATE / PARTIAL / NOSESSION)에 따라 실제 CLI 와 같은 모양의
stream-json 줄을 내보내고, 받은 인자·cwd·환경을 FAKE_CLAUDE_LOG 에 남긴다.
모든 테스트는 임시 폴더를 ROOT 로 써서 실제 office/status, events.jsonl, tracker 를 건드리지 않는다.
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _common as C  # noqa: E402
import office  # noqa: E402
import office_chat  # noqa: E402
import office_server  # noqa: E402

FAKE_CLI = r'''
import json, os, subprocess, sys, time, uuid

args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


prompt = None
if "-p" in args:
    i = args.index("-p")
    if i + 1 < len(args) and not args[i + 1].startswith("--"):
        prompt = args[i + 1]
if prompt is None:  # .cmd 래퍼 경로: 프롬프트는 stdin 으로 온다
    prompt = sys.stdin.buffer.read().decode("utf-8")
resume = opt("--resume")

log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd(), "aire_root": os.environ.get("AIRE_ROOT"),
                             "resume": resume, "claudecode": os.environ.get("CLAUDECODE")}, ensure_ascii=False) + "\n")


def emit(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def wait_gate():
    gate = os.environ.get("FAKE_GATE")
    end = time.time() + 40
    while gate and not os.path.exists(gate) and time.time() < end:
        time.sleep(0.05)


def assistant(*blocks):
    emit({"type": "assistant", "message": {"id": "msg_" + uuid.uuid4().hex[:8], "role": "assistant", "content": list(blocks)},
          "parent_tool_use_id": None, "session_id": session})


session = resume or ("fake-" + uuid.uuid4().hex[:8])
if "NOSESSION" in prompt and resume:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "No conversation found with session ID: " + resume, "session_id": session, "total_cost_usd": 0})
    sys.exit(1)

emit({"type": "system", "subtype": "init", "session_id": session, "cwd": os.getcwd(), "model": "fake", "tools": ["Read"]})

if "LOGIN" in prompt:
    assistant({"type": "text", "text": "Invalid API key \u00b7 Please run /login"})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key \u00b7 Please run /login",
          "session_id": session, "total_cost_usd": 0})
    sys.exit(1)
if "CRASH" in prompt:
    sys.stderr.write("boom: something broke\n")
    sys.stderr.flush()
    sys.exit(3)
if "ORPHAN" in prompt:
    # 정상 종료하지만, stdout 파이프를 쥔 채 오래 사는 고아 손자 프로세스를 남긴다
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=sys.stdout, stderr=sys.stderr,
                              stdin=subprocess.DEVNULL)
    with open(os.environ["FAKE_ORPHAN_PID"], "w") as fh:
        fh.write(str(orphan.pid))
    assistant({"type": "text", "text": "고아 테스트"})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "고아 테스트",
          "session_id": session, "total_cost_usd": 0.001})
    sys.exit(0)
if "SLEEP" in prompt:
    hb_code = "import sys,time\nwhile True:\n    open(sys.argv[1], 'w').write(str(time.time()))\n    time.sleep(0.1)\n"
    subprocess.Popen([sys.executable, "-c", hb_code, os.environ["FAKE_HEARTBEAT"]])
    assistant({"type": "text", "text": "\uc2dc\uc791"})
    time.sleep(120)
    sys.exit(0)
if "PARTIAL" in prompt:
    emit({"type": "stream_event", "event": {"type": "message_start"}, "session_id": session})
    for piece in ("\ud558", "\ub098", "\ub458"):
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": piece}}, "session_id": session})
    assistant({"type": "text", "text": "\ud558\ub098\ub458"})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "\ud558\ub098\ub458", "session_id": session,
          "total_cost_usd": 0.001})
    sys.exit(0)
if "GATE" in prompt:
    assistant({"type": "text", "text": "\uccab \uc870\uac01"})
    wait_gate()
    assistant({"type": "text", "text": "\ub9c8\uc9c0\ub9c9 \uc870\uac01"})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "\ub9c8\uc9c0\ub9c9 \uc870\uac01",
          "session_id": session, "total_cost_usd": 0.5})
    sys.exit(0)

assistant({"type": "text", "text": "\ud30c\uc77c\uc744 \uc77d\uc5b4\ubcfc\uac8c\uc694."},
          {"type": "tool_use", "id": "toolu_1", "name": "Read",
           "input": {"file_path": os.path.join(os.getcwd(), "docs", "00_\uc804\ub7b5_Strategy.md")}})
emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "..."}]},
      "parent_tool_use_id": None, "session_id": session})
assistant({"type": "text", "text": "\ub2f5\ubcc0: " + prompt.strip()[:50]})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "\ub2f5\ubcc0: " + prompt.strip()[:50],
      "session_id": session, "total_cost_usd": 0.0123, "num_turns": 2})
'''


class FakeCliMixin:
    """클래스당 한 번 가짜 CLI 를 만든다."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._fake_tmp = tempfile.TemporaryDirectory(prefix="aire-fakecli-")
        cls.fake_dir = Path(cls._fake_tmp.name)
        cls.fake_py = cls.fake_dir / "fake_claude.py"
        cls.fake_py.write_text(textwrap.dedent(FAKE_CLI), encoding="utf-8")
        cls.fake_cmd = cls.fake_dir / "fake_claude.cmd"
        cls.fake_cmd.write_text(f'@echo off\r\n"{sys.executable}" "{cls.fake_py}" %*\r\n', encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fake_tmp.cleanup()
        super().tearDownClass()


class ChatBase(FakeCliMixin, unittest.TestCase):
    cli_kind = "py"  # "py" 또는 "cmd"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="aire-chat-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "office").mkdir()
        self.log = self.root.parent / (self.root.name + "-fake.log")
        self.gate = self.root.parent / (self.root.name + "-gate")
        self.heartbeat = self.root.parent / (self.root.name + "-heartbeat")
        self.orphan_pid = self.root.parent / (self.root.name + "-orphan-pid")
        for p in (self.log, self.gate, self.heartbeat, self.orphan_pid):
            self.addCleanup(lambda p=p: p.unlink() if p.exists() else None)
        self.addCleanup(self._kill_orphan)
        patcher = mock.patch.dict(os.environ, {
            "AIRE_ROOT": str(self.root),
            "SHREW_CLAUDE_BIN": str(self.fake_cmd if self.cli_kind == "cmd" else self.fake_py),
            "FAKE_CLAUDE_LOG": str(self.log),
            "FAKE_GATE": str(self.gate),
            "FAKE_HEARTBEAT": str(self.heartbeat),
            "FAKE_ORPHAN_PID": str(self.orphan_pid),
            "CLAUDECODE": "1",  # 부모 세션 표식은 자식에게 넘어가면 안 된다
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start_server()

    def _kill_orphan(self) -> None:
        try:
            pid = int(self.orphan_pid.read_text().strip())
        except (OSError, ValueError):
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    # -- 서버 ------------------------------------------------------------------
    def start_server(self) -> None:
        self.server, self.thread = office_server.serve_in_thread(self.root, 0)
        self.port = self.server.server_address[1]
        self.token = self.server.chat.token
        self._server_stopped = False
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        if self._server_stopped:
            return
        self._server_stopped = True
        self.server.shutdown()
        self.server.server_close()  # chat.close(): 돌고 있는 가짜 CLI 도 정리
        self.thread.join(timeout=5)
        deadline = time.time() + 10
        while time.time() < deadline:  # 작업 스레드가 마무리 기록을 끝낼 때까지 (임시 폴더 삭제 전)
            active = self.server.chat._active
            if active is None or active.status in ("done", "error"):
                break
            time.sleep(0.05)

    # -- HTTP ------------------------------------------------------------------
    def request(self, method: str, path: str, body=None, headers: dict | None = None, token: bool | str = True,
                raw_body: bytes | None = None, content_type: str | None = "application/json"):
        h: dict[str, str] = {}
        if token is True:
            h["X-Shrew-Token"] = self.token
        elif token:
            h["X-Shrew-Token"] = str(token)
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None and content_type:
            h["Content-Type"] = content_type
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), payload
        finally:
            conn.close()

    def api(self, method: str, path: str, body=None, **kw):
        status, headers, payload = self.request(method, path, body, **kw)
        try:
            return status, json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            self.fail(f"JSON 이 아닌 응답 ({status}): {payload[:200]!r}")

    def raw_socket(self, data: bytes, read_timeout: float = 8.0) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout) as s:
            s.sendall(data)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            # 본문은 길이가 짧으니 닫힐 때까지 읽는다
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
            except (OSError, socket.timeout):
                pass
            return status, rest

    # -- 작업 ------------------------------------------------------------------
    def send(self, message: str, model: str | None = None) -> str:
        body: dict = {"message": message}
        if model:
            body["model"] = model
        status, js = self.api("POST", "/api/chat", body)
        self.assertEqual(status, 202, js)
        self.assertEqual(set(js), {"job_id"})
        return js["job_id"]

    def job(self, job_id: str) -> dict:
        status, js = self.api("GET", f"/api/chat/jobs/{job_id}")
        self.assertEqual(status, 200, js)
        return js

    def wait_job(self, job_id: str, timeout: float = 40.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            j = self.job(job_id)
            if j["status"] in ("done", "error"):
                return j
            time.sleep(0.05)
        self.fail(f"작업이 {timeout}초 안에 끝나지 않음: {self.job(job_id)}")

    def run_message(self, message: str, model: str | None = None) -> dict:
        return self.wait_job(self.send(message, model))

    def wait_for(self, predicate, timeout: float = 20.0, what: str = "조건"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"{what} 이(가) {timeout}초 안에 충족되지 않음")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def history(self) -> dict:
        status, js = self.api("GET", "/api/chat/history")
        self.assertEqual(status, 200, js)
        return js

    def ceo(self) -> dict:
        return json.loads((self.root / "office" / "status" / "ceo.json").read_text(encoding="utf-8"))

    def ceo_task(self) -> str | None:
        try:
            return self.ceo()["task"]
        except (OSError, ValueError):
            return None

    def events(self) -> list[dict]:
        p = self.root / "office" / "events.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


# ==========================================================================
# 보안 / HTTP 계약
# ==========================================================================
class ChatSecurityTests(ChatBase):
    def test_served_index_gets_a_per_process_token(self):
        (self.root / "office" / "index.html").write_text(
            "<script>const T = '__SHREW_CHAT_TOKEN__';</script>", encoding="utf-8")
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("__SHREW_CHAT_TOKEN__", text)
        self.assertIn(self.token, text)
        self.assertGreaterEqual(len(self.token), 40)  # secrets.token_urlsafe(32)
        self.assertEqual(headers["cache-control"], "no-store")
        # 같은 응답으로 받은 토큰으로 API 가 열린다
        self.assertEqual(self.api("GET", "/api/chat/history", token=self.token)[0], 200)
        # /index.html, /?x=1, /./index.html 도 같은 처리
        for path in ("/index.html", "/?x=1", "/./index.html"):
            self.assertIn(self.token, self.request("GET", path, token=False)[2].decode("utf-8"), path)
        # HEAD 는 길이만
        status, headers, body = self.request("HEAD", "/", token=False)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["content-length"]), len(text.encode("utf-8")))

    def test_two_servers_have_different_tokens(self):
        other, thread = office_server.serve_in_thread(self.root, 0)
        self.addCleanup(lambda: (other.shutdown(), other.server_close(), thread.join(5)))
        self.assertNotEqual(other.chat.token, self.token)

    def test_foreign_host_is_blocked_on_every_route_dns_rebinding(self):
        """공격자 도메인이 127.0.0.1 로 풀려도 Host 는 공격자 도메인이다 -> 토큰·상태·정적 파일 어느 것도 내주지 않는다."""
        (self.root / "office" / "index.html").write_text("<script>'__SHREW_CHAT_TOKEN__'</script>", encoding="utf-8")
        (self.root / "office" / "events.jsonl").write_text('{"msg": "private-event"}\n', encoding="utf-8")
        evil = {"Host": "evil.example:%d" % self.port}
        for method, path in (("GET", "/"), ("GET", "/index.html"), ("GET", "/api/state"), ("GET", "/events.jsonl"),
                             ("HEAD", "/api/state"), ("GET", "/api/chat/history"), ("POST", "/api/chat"),
                             ("POST", "/api/other"), ("OPTIONS", "/api/state"), ("GET", "/nope")):
            status, _, body = self.request(method, path, token=False, headers=evil, raw_body=b"{}" if method == "POST" else None)
            self.assertEqual(status, 403, (method, path))
            self.assertNotIn(self.token.encode(), body, path)
            self.assertNotIn(b"private-event", body, path)
            if method != "HEAD":
                self.assertIn("error", json.loads(body.decode("utf-8")))
        # 올바른 Host 면 그대로 열린다 (127.0.0.1 / localhost)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self.assertEqual(self.request("GET", "/api/state", token=False, headers={"Host": host})[0], 200, host)
        # Host 가 없는 HTTP/1.0 요청도 막는다
        status, _ = self.raw_socket(b"GET /api/state HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 403)

    def test_token_is_only_given_to_same_origin_page_loads(self):
        (self.root / "office" / "index.html").write_text("<script>'__SHREW_CHAT_TOKEN__'</script>", encoding="utf-8")
        ok = [{}, {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
              {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
              {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}]  # 링크로 연 최상위 이동
        for h in ok:
            body = self.request("GET", "/", token=False, headers=h)[2]
            self.assertIn(self.token.encode(), body, h)
        blocked = [{"Origin": "http://evil.example"}, {"Origin": "null"},
                   {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"},
                   {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
                   {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
                   {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}]
        for h in blocked:
            status, _, body = self.request("GET", "/", token=False, headers=h)
            self.assertEqual(status, 200, h)  # 페이지 자체는 열리되 (명령창은 꺼진 채)
            self.assertNotIn(self.token.encode(), body, h)
            self.assertIn(b"__SHREW_CHAT_TOKEN__", body, h)

    def test_every_response_refuses_framing_and_cross_origin_embedding(self):
        for path in ("/", "/api/state", "/api/chat/history", "/nope"):
            status, headers, _ = self.request("GET", path)
            self.assertEqual(headers.get("x-frame-options"), "DENY", path)
            self.assertIn("frame-ancestors 'none'", headers.get("content-security-policy", ""), path)
            self.assertEqual(headers.get("cross-origin-resource-policy"), "same-origin", path)
            self.assertFalse([h for h in headers if h.startswith("access-control-")], path)

    def test_index_without_placeholder_is_unchanged(self):
        html = "<!doctype html><title>오피스</title>"
        (self.root / "office" / "index.html").write_text(html, encoding="utf-8")
        self.assertEqual(self.request("GET", "/", token=False)[2], html.encode("utf-8"))

    def test_chat_history_files_are_not_served_statically(self):
        secret = "OWNER-PRIVATE-SECRET"
        chat_dir = self.root / "office" / "chat"
        chat_dir.mkdir()
        (chat_dir / "history.jsonl").write_text(json.dumps({"text": secret}) + "\n", encoding="utf-8")
        (chat_dir / "session.json").write_text(json.dumps({"session_id": secret}), encoding="utf-8")
        (self.root / "office" / "events.jsonl").write_text("{}\n", encoding="utf-8")  # 대조군: 다른 파일은 그대로 나간다
        self.assertEqual(self.request("GET", "/events.jsonl", token=False)[0], 200)
        for path in ("/chat/history.jsonl", "/chat/session.json", "/CHAT/history.jsonl", "/Chat/History.JSONL",
                     "/chat./history.jsonl", "/%63hat/history.jsonl", "/./chat/history.jsonl", "/x/../chat/history.jsonl",
                     "/chat/", "/chat"):
            status, _, body = self.request("GET", path, token=False)
            self.assertNotIn(secret.encode(), body, path)
            self.assertIn(status, (301, 400, 404), path)

    def test_token_required_on_every_chat_route(self):
        routes = [("GET", "/api/chat/history", None), ("GET", "/api/chat/jobs/abc", None),
                  ("POST", "/api/chat", {"message": "안녕"}), ("POST", "/api/chat/reset", {})]
        for method, path, body in routes:
            for token in (False, "wrong-token", self.token[:-1], self.token + "x", "töken"):
                status, js = self.api(method, path, body, token=token)
                self.assertEqual(status, 403, (method, path, token))
                self.assertIn("error", js)
        self.assertEqual(self.calls(), [])  # 거부된 요청은 CLI 를 부르지 않는다
        self.assertEqual(self.api("GET", "/api/chat/history")[0], 200)

    def test_bad_host_is_rejected_and_localhost_is_allowed(self):
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1", "127.0.0.1:1", "localhost",
                     "[::1]:%d" % self.port, "127.0.0.1.evil.example:%d" % self.port, ""):
            status, js = self.api("GET", "/api/chat/history", headers={"Host": host})
            self.assertEqual(status, 403, host)
            self.assertIn("error", js)
        self.assertEqual(self.api("GET", "/api/chat/history", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.api("GET", "/api/chat/history", headers={"Host": f"127.0.0.1:{self.port}"})[0], 200)

    def test_foreign_origin_is_rejected_same_origin_is_allowed(self):
        for origin in ("http://evil.example", "https://evil.example:%d" % self.port, "null",
                       "http://127.0.0.1:1", f"https://127.0.0.1:{self.port}", f"http://localhost:{self.port}.evil.example"):
            for method, path, body in (("GET", "/api/chat/history", None), ("POST", "/api/chat/reset", {})):
                status, js = self.api(method, path, body, headers={"Origin": origin})
                self.assertEqual(status, 403, (origin, method))
        # 같은 출처: 브라우저가 POST 에 붙이는 Origin
        self.assertEqual(self.api("POST", "/api/chat/reset", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.api("GET", "/api/chat/history", headers={
            "Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})[0], 200)
        # 교차 사이트 표식
        self.assertEqual(self.api("GET", "/api/chat/history", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.api("GET", "/api/chat/history", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_no_cors_headers_and_no_preflight(self):
        status, headers, _ = self.request("GET", "/api/chat/history")
        self.assertEqual(status, 200)
        self.assertFalse([h for h in headers if h.startswith("access-control-")])
        for method in ("OPTIONS", "PUT", "DELETE", "PATCH", "HEAD"):
            status, headers, body = self.request(method, "/api/chat", token=False,
                                                 headers={"Origin": "http://evil.example",
                                                          "Access-Control-Request-Method": "POST"})
            self.assertEqual(status, 405, method)
            self.assertFalse([h for h in headers if h.startswith("access-control-")], method)
            if method != "HEAD":
                self.assertIn("error", json.loads(body.decode("utf-8")))
        self.assertEqual(self.api("POST", "/api/chat/history", {}, )[0], 405)
        self.assertEqual(self.api("GET", "/api/chat")[0], 405)
        self.assertEqual(self.api("GET", "/api/chat/reset")[0], 405)

    def test_post_requires_json_content_type(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
            status, js = self.api("POST", "/api/chat", raw_body=b'{"message": "hi"}', content_type=ctype)
            self.assertEqual(status, 415, ctype)
            self.assertIn("error", js)
        self.assertEqual(self.calls(), [])
        # charset 이 붙어도 JSON 이면 통과
        status, js = self.api("POST", "/api/chat/reset", raw_body=b"{}", content_type="application/json; charset=utf-8")
        self.assertEqual(status, 200)

    def test_oversize_body_is_413_and_server_survives(self):
        big = json.dumps({"message": "가" * 30000}).encode("utf-8")  # 90KB > 64KB
        self.assertGreater(len(big), office_chat.MAX_BODY_BYTES)
        status, js = self.api("POST", "/api/chat", raw_body=big)
        self.assertEqual(status, 413)
        self.assertIn("error", js)
        # 거대한 Content-Length 만 선언하고 본문은 보내지 않아도 즉시 413
        req = (f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Shrew-Token: {self.token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: 999999999\r\n\r\n").encode()
        status, body = self.raw_socket(req)
        self.assertEqual(status, 413)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.api("GET", "/api/chat/history")[0], 200)
        self.assertEqual(self.api("GET", "/api/state")[0], 200)

    def test_body_just_under_the_cap_is_accepted_by_the_size_check(self):
        # 8000자를 넘는 메시지는 크기 제한(413)이 아니라 글자 수 제한(400)으로 거절된다
        status, js = self.api("POST", "/api/chat", {"message": "a" * (office_chat.MAX_MESSAGE_CHARS + 1)})
        self.assertEqual(status, 400)
        self.assertIn("8,000", js["error"])

    def test_missing_content_length_is_411(self):
        req = (f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Shrew-Token: {self.token}\r\n"
               f"Content-Type: application/json\r\n\r\n").encode()
        status, body = self.raw_socket(req)
        self.assertEqual(status, 411)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_bad_payloads_are_400_with_korean_error(self):
        cases = [
            (b"{not json", None),
            (b"[1, 2]", None),
            (b'"just a string"', None),
            (json.dumps({"message": 123}).encode(), None),
            (json.dumps({"message": ["a"]}).encode(), None),
            (json.dumps({}).encode(), None),
            (json.dumps({"message": ""}).encode(), None),
            (json.dumps({"message": "   \n\t "}).encode(), None),
            (json.dumps({"message": "안녕", "model": "gpt"}).encode(), None),
            (json.dumps({"message": "안녕", "model": 5}).encode(), None),
            (b"\xff\xfe\x00bad", None),
        ]
        for raw, _ in cases:
            status, js = self.api("POST", "/api/chat", raw_body=raw)
            self.assertEqual(status, 400, raw)
            self.assertRegex(js["error"], r"[가-힣]")
            self.assertNotIn("Traceback", json.dumps(js))
        self.assertEqual(self.calls(), [])

    def test_unknown_routes_and_jobs_are_json_404(self):
        for path in ("/api/chat/jobs/nope", "/api/chat/jobs/", "/api/chat/jobs/../../etc", "/api/chat/jobs/a%00b",
                     "/api/chat/whatever", "/api/chat/jobs/" + "x" * 200):
            status, js = self.api("GET", path)
            self.assertEqual(status, 404, path)
            self.assertIn("error", js)

    def test_api_state_is_unchanged_and_needs_no_token(self):
        status, js = self.api("GET", "/api/state", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(set(js), {"now", "agents", "events", "kpis", "approvals", "products"})


# ==========================================================================
# 대화 흐름
# ==========================================================================
class ChatFlowTests(ChatBase):
    def test_full_flow_text_tools_session_cost_history(self):
        j = self.run_message("오늘 진행 상황 요약")
        self.assertEqual(j["status"], "done", j)
        self.assertIn("파일을 읽어볼게요.", j["text"])
        self.assertIn("답변: 오늘 진행 상황 요약", j["text"])
        self.assertEqual(j["tools"], [{"name": "Read", "summary": "docs/00_전략_Strategy.md"}])
        self.assertTrue(j["session_id"].startswith("fake-"))
        self.assertAlmostEqual(j["cost_usd"], 0.0123)
        self.assertIsNone(j["error"])
        self.assertRegex(j["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")
        self.assertRegex(j["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$")
        self.assertEqual(set(j), {"job_id", "status", "text", "tools", "session_id", "cost_usd", "error", "started_at", "finished_at"})

        h = self.history()
        self.assertEqual([m["role"] for m in h["messages"]], ["owner", "claude"])
        self.assertEqual(h["messages"][0]["text"], "오늘 진행 상황 요약")
        self.assertEqual(h["messages"][1]["text"], j["text"])
        self.assertEqual({m["job_id"] for m in h["messages"]}, {j["job_id"]})
        for m in h["messages"]:
            self.assertEqual(set(m), {"role", "text", "ts", "job_id"})
            self.assertRegex(m["ts"], r"\+09:00$")
        self.assertEqual(h["cli"]["found"], True)
        self.assertEqual(h["cli"]["path"], str(self.fake_py))
        self.assertTrue(h["cli"]["logged_in"])

        # 자식 프로세스 환경 / 작업 폴더
        (call,) = self.calls()
        self.assertEqual(Path(call["cwd"]).resolve(), self.root)
        self.assertEqual(Path(call["aire_root"]).resolve(), self.root)
        self.assertIsNone(call["claudecode"])  # 부모 세션 표식은 걸러졌다
        self.assertEqual(call["prompt"], "오늘 진행 상황 요약")

        # 파일 저장
        hist_file = self.root / "office" / "chat" / "history.jsonl"
        lines = hist_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["role"], "owner")
        self.assertEqual(json.loads((self.root / "office" / "chat" / "session.json").read_text(encoding="utf-8")),
                         {"session_id": j["session_id"]})

    def test_text_grows_while_running_then_completes(self):
        jid = self.send("GATE 조금씩 보여줘")
        j = self.wait_for(lambda: (lambda x: x if "첫 조각" in x["text"] else None)(self.job(jid)), what="첫 조각")
        self.assertEqual(j["status"], "running")
        self.assertNotIn("마지막 조각", j["text"])
        self.assertIsNone(j["finished_at"])
        self.assertIsNone(j["cost_usd"])
        self.assertEqual(self.history()["active_job_id"], jid)
        self.gate.write_text("go")
        done = self.wait_job(jid)
        self.assertEqual(done["status"], "done")
        self.assertIn("첫 조각", done["text"])
        self.assertTrue(done["text"].endswith("마지막 조각"))
        self.assertEqual(done["cost_usd"], 0.5)
        self.assertIsNone(self.history()["active_job_id"])

    def test_partial_stream_events_are_not_duplicated(self):
        j = self.run_message("PARTIAL")
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["text"], "하나둘")

    def test_concurrent_post_is_409_then_free_again(self):
        first = self.send("GATE 첫 번째")
        self.wait_for(lambda: "첫 조각" in self.job(first)["text"], what="첫 작업 실행")
        status, js = self.api("POST", "/api/chat", {"message": "두 번째"})
        self.assertEqual(status, 409)
        self.assertIn("처리 중", js["error"])
        self.assertEqual(js["job_id"], first)
        # 거절된 요청은 기록에도 CLI 호출에도 없다
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual([m["text"] for m in self.history()["messages"] if m["role"] == "owner"], ["GATE 첫 번째"])
        self.gate.write_text("go")
        self.assertEqual(self.wait_job(first)["status"], "done")
        j = self.run_message("세 번째")
        self.assertEqual(j["status"], "done")

    def test_parallel_posts_exactly_one_wins(self):
        import concurrent.futures
        results = []

        def post(i):
            return self.api("POST", "/api/chat", {"message": f"GATE 병렬 {i}"})[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(post, range(6)))
        self.assertEqual(sorted(results), [202] + [409] * 5)
        self.gate.write_text("go")
        self.wait_for(lambda: self.history()["active_job_id"] is None, what="작업 종료")
        self.assertEqual(len(self.calls()), 1)

    def test_resume_is_passed_on_second_message_and_reset_forgets(self):
        j1 = self.run_message("첫 질문")
        j2 = self.run_message("두 번째 질문")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--resume", calls[0]["argv"])
        self.assertIn("--resume", calls[1]["argv"])
        self.assertEqual(calls[1]["argv"][calls[1]["argv"].index("--resume") + 1], j1["session_id"])
        self.assertEqual(j2["session_id"], j1["session_id"])

        status, js = self.api("POST", "/api/chat/reset", {})
        self.assertEqual((status, js), (200, {"ok": True}))
        session = json.loads((self.root / "office" / "chat" / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session, {"session_id": None})
        msgs = self.history()["messages"]
        self.assertEqual(msgs[-1]["role"], "system")
        self.assertEqual(msgs[-1]["text"], "새 대화 시작")
        self.assertIsNone(msgs[-1]["job_id"])
        self.assertEqual(len(msgs), 5)  # 기록은 지워지지 않는다: 질문2 + 답2 + 시스템 1

        j3 = self.run_message("리셋 뒤 질문")
        self.assertNotIn("--resume", self.calls()[2]["argv"])
        self.assertNotEqual(j3["session_id"], j1["session_id"])

    def test_reset_while_running_is_not_undone_by_the_finishing_job(self):
        jid = self.send("GATE 리셋 경합")
        self.wait_for(lambda: "첫 조각" in self.job(jid)["text"], what="실행 중")
        self.assertEqual(self.api("POST", "/api/chat/reset", {})[0], 200)
        self.gate.write_text("go")
        self.wait_job(jid)
        self.assertEqual(json.loads((self.root / "office" / "chat" / "session.json").read_text(encoding="utf-8")),
                         {"session_id": None})
        self.run_message("다음")
        self.assertNotIn("--resume", self.calls()[1]["argv"])

    def test_argument_list_matches_the_contract(self):
        self.run_message("계약 확인", model="opus")
        self.run_message("기본 모델")
        first, second = self.calls()
        argv = first["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertEqual(argv[1], "계약 확인")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(second["argv"][second["argv"].index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        allowed = argv[argv.index("--allowedTools") + 1]
        disallowed = argv[argv.index("--disallowedTools") + 1]
        self.assertEqual(allowed, ",".join(office_chat.ALLOWED_TOOLS))
        for rule in ("WebSearch", "WebFetch",
                     "Bash(python scripts/office.py:*)", "Bash(python scripts/tracker.py list:*)",
                     "Bash(python scripts/tracker.py report:*)", "Bash(python scripts/tracker.py add-product:*)",
                     "Bash(python scripts/tracker.py set-status:*)", "Bash(python scripts/tracker.py add-ledger:*)",
                     "Bash(python scripts/tracker.py request-approval:*)",
                     "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)"):
            self.assertIn(rule, allowed.split(","))
        for rule in ("Bash(python scripts/tracker.py approve:*)", "Bash(python scripts/tracker.py reject:*)",
                     "Bash(git push:*)", "Bash(git reset:*)", "Bash(rm:*)", "Bash(del:*)"):
            self.assertIn(rule, disallowed.split(","))
        # 파일 도구 이름만 있는 허용 규칙은 경로를 가리지 않아 프로젝트 폴더 밖까지 열어 버린다(실제 CLI 로 확인) -> 넣지 않는다.
        # (폴더 안의 읽기·수정은 CLI 기본 동작 + acceptEdits 로 자동 허용, 폴더 밖은 헤드리스에서 거부)
        for bare in ("Read", "Glob", "Grep", "Edit", "Write", "Bash", "NotebookEdit"):
            self.assertNotIn(bare, allowed.split(","))
        self.assertFalse([r for r in allowed.split(",") if r.startswith(("Edit(", "Write(", "Read(", "Bash(*", "mcp__"))])
        # 생산 중인 대시보드 상태를 날리는 reset, git/gh 의 쓰기 명령, 파일을 쓰는 git 옵션도 막혀 있다
        for rule in ("Bash(python scripts/office.py reset:*)", "Bash(gh:*)", "Bash(git commit:*)", "Bash(git add:*)",
                     "Bash(git * --output*)", "Bash(git * --no-index*)", "Edit(scripts/**)", "Write(scripts/**)", "Read(.env)"):
            self.assertIn(rule, disallowed.split(","))
        # 사용자 전역 설정(허용 규칙)·MCP 커넥터를 이 세션에 섞지 않는다
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        # approve/reject 는 허용 목록에 절대 없다
        self.assertNotIn("approve", allowed)
        self.assertNotIn("reject", allowed)
        # 파일을 직접 고쳐 approve 를 우회하는 길도 막혀 있다
        for rule in ("Edit(tracker/approvals.csv)", "Write(tracker/approvals.csv)", "Edit(CLAUDE.md)", "Edit(.claude/**)"):
            self.assertIn(rule, disallowed.split(","))
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("CLAUDE.md", system)
        self.assertIn("approve", system)
        self.assertIn("워크플로", system)

    def test_popen_is_called_with_an_argument_list_and_never_a_shell(self):
        real_popen = subprocess.Popen
        seen: list[tuple[tuple, dict]] = []

        def spy(*args, **kwargs):
            seen.append((args, kwargs))
            return real_popen(*args, **kwargs)

        with mock.patch.object(subprocess, "Popen", spy):
            self.run_message("셸 없이")
        self.assertTrue(seen)
        for args, kwargs in seen:
            self.assertFalse(kwargs.get("shell", False), kwargs)
            self.assertIsInstance(args[0], list)
            self.assertTrue(all(isinstance(a, str) for a in args[0]))
        chat_calls = [(a, k) for a, k in seen if "--allowedTools" in a[0]]
        self.assertEqual(len(chat_calls), 1)
        args, kwargs = chat_calls[0]
        self.assertIn("Bash(python scripts/tracker.py approve:*)", args[0][args[0].index("--disallowedTools") + 1])
        self.assertEqual(Path(kwargs["cwd"]).resolve(), self.root)
        self.assertEqual(kwargs["env"]["AIRE_ROOT"], str(self.root))
        self.assertNotIn("CLAUDECODE", kwargs["env"])

    def test_user_settings_can_be_re_enabled_only_by_an_explicit_env_var(self):
        self.run_message("기본")
        with mock.patch.dict(os.environ, {"SHREW_CLAUDE_USER_SETTINGS": "1"}):
            self.run_message("사용자 설정 포함")
        first, second = self.calls()
        self.assertEqual(first["argv"][first["argv"].index("--setting-sources") + 1], "project,local")
        self.assertEqual(second["argv"][second["argv"].index("--setting-sources") + 1], "user,project,local")

    def test_tampered_session_file_cannot_inject_cli_options(self):
        chat_dir = self.root / "office" / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        for evil in ("--dangerously-skip-permissions", "abc def", "-x", "a" * 500, "a\nb", "", 5, ["--x"]):
            (chat_dir / "session.json").write_text(json.dumps({"session_id": evil}), encoding="utf-8")
            self.assertEqual(self.run_message("세션 변조")["status"], "done")
        for call in self.calls():
            self.assertNotIn("--resume", call["argv"], call["argv"])
            self.assertNotIn("--dangerously-skip-permissions", call["argv"])
        with self.assertRaises(ValueError):
            office_chat.build_command("x", "p", "sonnet", "s", "--dangerously-skip-permissions")
        self.assertTrue(office_chat.valid_session_id("3f2c1d9e-8b7a-4c6d-9e5f-0a1b2c3d4e5f"))

    def test_start_job_rejects_unknown_models(self):
        with self.assertRaises(office_chat.ChatError) as cm:
            self.server.chat.start_job("hi", "--evil")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(self.calls(), [])

    def test_an_orphan_holding_the_pipe_open_does_not_keep_the_job_running(self):
        with mock.patch.object(office_chat, "READER_GRACE_SECONDS", 0.5):
            t0 = time.time()
            j = self.run_message("ORPHAN 파이프를 쥔 고아")
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(j["text"], "고아 테스트")
        self.assertEqual(self.ceo()["state"], "idle")
        self.assertEqual(self.run_message("그 뒤")["status"], "done")  # 큐가 막히지 않는다

    def test_messages_with_dashes_newlines_and_korean_arrive_intact(self):
        tricky = "-rf --help 이상한 옵션처럼 보이는 글\n둘째 줄 \"따옴표\" & | < > ^ %PATH%"
        j = self.run_message(tricky)
        self.assertEqual(j["status"], "done", j)
        (call,) = self.calls()
        self.assertEqual(call["prompt"].strip(), tricky)
        self.assertNotEqual(call["argv"][1][0], "-")  # 옵션으로 읽히지 않도록 앞에 공백
        self.assertEqual(self.history()["messages"][0]["text"], tricky)

    def test_longest_allowed_message_works(self):
        msg = ("한" * 7990) + "\n끝입니다"
        j = self.run_message(msg)
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(self.calls()[0]["prompt"].strip(), msg)

    def test_system_prompt_carries_a_fresh_office_snapshot(self):
        office.set_status("maker-1", "working", "노션 템플릿 제작 중", root=self.root)
        office.set_status("qa-1", "idle", "대기", root=self.root)
        C.append_csv_row(C.approvals_csv(self.root), C.APPROVAL_COLS,
                         {"id": "AP-7", "product_id": "p-1", "action": "publish_listing", "requested_at": C.now_iso(),
                          "status": "pending", "decided_at": "", "note": ""})
        C.append_csv_row(C.approvals_csv(self.root), C.APPROVAL_COLS,
                         {"id": "AP-1", "product_id": "p-0", "action": "price_change", "requested_at": C.now_iso(),
                          "status": "approved", "decided_at": C.now_iso(), "note": ""})
        C.append_csv_row(C.products_csv(self.root), C.PRODUCT_COLS,
                         {"id": "p-1", "batch": "b1", "title": "T", "model": "m", "platform": "x", "price_krw": "9900",
                          "status": "awaiting_owner", "created_at": C.now_iso(), "listed_url": "", "notes": ""})
        C.append_csv_row(C.ledger_csv(self.root), C.LEDGER_COLS,
                         {"date": C.today_kst().isoformat(), "product_id": "p-1", "platform": "x", "type": "sale",
                          "amount_krw": "12000", "note": ""})
        self.run_message("스냅샷 확인")
        system = self.calls()[0]["argv"][self.calls()[0]["argv"].index("--append-system-prompt") + 1]
        self.assertIn("maker-1(working: 노션 템플릿 제작 중)", system)
        self.assertNotIn("qa-1", system)  # 대기 중인 직원은 줄이지 않고 생략
        self.assertIn("승인 대기 1건: AP-7[publish_listing]", system)
        self.assertNotIn("AP-1[", system)
        self.assertIn("awaiting_owner 1", system)
        self.assertIn("12,000원", system)

    def test_ceo_desk_shows_the_command_and_goes_back(self):
        office.set_status("ceo", "working", "배치 기획 중", bubble="📋", product_id="p-1", root=self.root)
        jid = self.send("GATE 책상 확인")
        self.wait_for(lambda: self.ceo_task() == "대표 명령 처리 중", what="CEO 책상 표시")
        cur = self.ceo()
        self.assertEqual((cur["state"], cur["bubble"]), ("working", "📨"))
        self.gate.write_text("go")
        self.wait_job(jid)
        back = self.ceo()
        self.assertEqual((back["state"], back["task"], back["bubble"], back.get("product_id")),
                         ("working", "배치 기획 중", "📋", "p-1"))
        notes = [e for e in self.events() if e["type"] == "note" and e["msg"].startswith("대표 명령: ")]
        self.assertEqual(notes[0]["msg"], "대표 명령: GATE 책상 확인")
        self.assertEqual(notes[0]["id"], "ceo")

    def test_ceo_desk_goes_idle_when_it_had_no_previous_state(self):
        self.run_message("처음 명령")
        cur = self.ceo()
        self.assertEqual((cur["state"], cur["bubble"]), ("idle", "💤"))

    def test_ceo_desk_is_left_alone_if_a_workflow_changed_it_meanwhile(self):
        jid = self.send("GATE 경합")
        self.wait_for(lambda: self.ceo_task() == "대표 명령 처리 중", what="표시")
        office.set_status("ceo", "meeting", "브리프 검토 회의", root=self.root)
        self.gate.write_text("go")
        self.wait_job(jid)
        cur = self.ceo()
        self.assertEqual((cur["state"], cur["task"]), ("meeting", "브리프 검토 회의"))

    def test_long_command_is_cut_in_the_note_event(self):
        self.run_message("가" * 100)
        note = [e for e in self.events() if e["type"] == "note" and e["msg"].startswith("대표 명령: ")][0]
        self.assertEqual(note["msg"], "대표 명령: " + "가" * 40)

    def test_stale_session_is_forgotten_and_retried_once(self):
        chat_dir = self.root / "office" / "chat"
        chat_dir.mkdir()
        (chat_dir / "session.json").write_text(json.dumps({"session_id": "stale-session"}), encoding="utf-8")
        j = self.run_message("NOSESSION 옛 세션")
        self.assertEqual(j["status"], "done", j)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["resume"], "stale-session")
        self.assertIsNone(calls[1]["resume"])
        self.assertEqual(json.loads((chat_dir / "session.json").read_text(encoding="utf-8"))["session_id"], j["session_id"])

    def test_history_survives_a_server_restart_and_session_continues(self):
        j1 = self.run_message("재시작 전")
        self.stop_server()
        self.start_server()
        h = self.history()
        self.assertEqual([m["text"] for m in h["messages"] if m["role"] == "owner"], ["재시작 전"])
        self.assertEqual(h["messages"][1]["text"], j1["text"])
        self.assertIsNone(h["active_job_id"])
        self.assertEqual(self.api("GET", f"/api/chat/jobs/{j1['job_id']}")[0], 404)  # 작업 객체는 메모리에만
        self.run_message("재시작 후")
        self.assertEqual(self.calls()[1]["resume"], j1["session_id"])

    def test_history_returns_only_the_last_100_in_order(self):
        chat_dir = self.root / "office" / "chat"
        chat_dir.mkdir()
        rows = [{"role": "owner" if i % 2 else "claude", "text": f"메시지 {i}", "ts": C.now_iso(), "job_id": f"j{i}"}
                for i in range(150)]
        with open(chat_dir / "history.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.write("{깨진 줄\n")
            fh.write(json.dumps({"role": "hacker", "text": "x", "ts": "", "job_id": None}) + "\n")
            fh.write(json.dumps({"role": "owner", "text": 5}) + "\n")
        msgs = self.history()["messages"]
        self.assertEqual(len(msgs), 100)
        self.assertEqual(msgs[-1]["text"], "메시지 149")
        self.assertEqual(msgs[0]["text"], "메시지 50")

    def test_not_logged_in_gives_login_guidance(self):
        j = self.run_message("LOGIN 상태 확인")
        self.assertEqual(j["status"], "error")
        self.assertIn("/login", j["error"])
        self.assertIn("로그인", j["error"])
        self.assertEqual(j["text"], "")  # CLI 의 영어 문구가 Claude 의 답처럼 보이지 않는다
        h = self.history()
        self.assertIs(h["cli"]["logged_in"], False)
        self.assertEqual(h["cli"]["found"], True)
        self.assertEqual([m["role"] for m in h["messages"]], ["owner", "system"])
        self.assertIn("/login", h["messages"][1]["text"])
        # 로그인 뒤에는 다시 정상 + 상태 갱신
        ok = self.run_message("이제 로그인했다")
        self.assertEqual(ok["status"], "done")
        self.assertIs(self.history()["cli"]["logged_in"], True)
        # 실패했어도 CEO 책상은 원래대로
        self.assertEqual(self.ceo()["state"], "idle")

    def test_login_words_inside_a_successful_answer_are_not_an_error(self):
        j = self.run_message("not logged in 이라는 말이 나오는 질문")
        self.assertEqual(j["status"], "done")
        self.assertIsNot(self.history()["cli"]["logged_in"], False)

    def test_timeout_kills_the_whole_process_tree(self):
        self.server.chat.timeout_seconds = 2
        t0 = time.time()
        j = self.run_message("SLEEP 오래 걸리는 일", )
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(j["status"], "error")
        self.assertIn("중단", j["error"])
        self.assertEqual(j["text"], "시작")  # 그때까지 받은 말은 남는다
        self.wait_for(self.heartbeat.exists, what="손자 프로세스 시작")
        time.sleep(0.6)
        before = self.heartbeat.stat().st_mtime_ns
        time.sleep(1.0)
        self.assertEqual(self.heartbeat.stat().st_mtime_ns, before, "손자 프로세스가 아직 살아 있다")
        msgs = self.history()["messages"]
        self.assertEqual([m["role"] for m in msgs], ["owner", "claude", "system"])
        self.assertIn("중단", msgs[-1]["text"])
        # 막히지 않고 다음 명령을 받는다
        self.server.chat.timeout_seconds = 60
        self.assertEqual(self.run_message("타임아웃 뒤")["status"], "done")
        self.assertEqual(self.ceo()["state"], "idle")

    def test_crash_without_result_reports_exit_code_and_stderr(self):
        j = self.run_message("CRASH 해보자")
        self.assertEqual(j["status"], "error")
        self.assertIn("종료 코드 3", j["error"])
        self.assertIn("boom", j["error"])
        self.assertNotIn("Traceback", j["error"])
        self.assertEqual(self.run_message("그 뒤에도 서버는 산다")["status"], "done")

    def test_cli_not_found_is_503_with_hint_and_recovers(self):
        real = office_chat.discover_cli
        with mock.patch.object(office_chat, "discover_cli", return_value=None):
            self.server.chat._cli_path = None
            status, js = self.api("POST", "/api/chat", {"message": "안녕"})
            self.assertEqual(status, 503)
            self.assertIn("찾을 수 없습니다", js["error"])
            self.assertIn("SHREW_CLAUDE_BIN", js["hint"])
            cli = self.history()["cli"]
            self.assertEqual((cli["found"], cli["path"]), (False, None))
        self.assertEqual(self.history()["messages"], [])  # 503 은 기록을 남기지 않는다
        # CLI 가 다시 나타나면 다시 찾는다
        with mock.patch.object(office_chat, "discover_cli", side_effect=real):
            self.assertEqual(self.run_message("이제 있음")["status"], "done")

    def test_cli_that_disappears_is_rediscovered(self):
        self.run_message("첫 번째")
        gone = self.fake_dir / "gone.py"
        gone.write_text("print('x')", encoding="utf-8")
        self.server.chat._cli_path = str(gone)
        gone.unlink()
        self.assertEqual(self.run_message("두 번째")["status"], "done")
        self.assertEqual(self.server.chat._cli_path, str(self.fake_py))

    def test_unlaunchable_cli_is_a_clean_error(self):
        broken = self.fake_dir / "broken.py"
        broken.write_text("raise SystemExit(0)", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SHREW_CLAUDE_BIN": str(broken)}):
            self.server.chat._cli_path = None
            j = self.run_message("아무 출력 없이 끝나는 CLI")
        self.assertEqual(j["status"], "error")
        self.assertIn("응답 없이", j["error"])
        self.assertEqual(self.api("GET", "/api/state")[0], 200)

    def test_exception_inside_the_worker_never_kills_the_server(self):
        with mock.patch.object(office_chat.ChatService, "_stream_once", side_effect=RuntimeError("boom")):
            j = self.run_message("예외 유발")
        self.assertEqual(j["status"], "error")
        self.assertNotIn("boom", j["error"])
        self.assertNotIn("Traceback", j["error"])
        self.assertEqual(self.run_message("그 뒤")["status"], "done")

    def test_a_failure_while_wrapping_up_still_frees_the_queue(self):
        with mock.patch.object(office_chat.ChatService, "_ceo_end", side_effect=RuntimeError("wrap-up broke")):
            j = self.run_message("마무리 단계 오류")
        self.assertIn(j["status"], ("done", "error"))
        self.assertEqual(self.run_message("바로 다음 명령")["status"], "done")

    def test_closing_the_server_stops_a_running_cli(self):
        jid = self.send("SLEEP 종료 시 정리")
        self.wait_for(self.heartbeat.exists, what="CLI 시작")
        self.stop_server()
        time.sleep(0.6)
        before = self.heartbeat.stat().st_mtime_ns
        time.sleep(1.0)
        self.assertEqual(self.heartbeat.stat().st_mtime_ns, before)
        self.assertIn(self.server.chat.job_snapshot(jid)["status"], ("error", "done"))



class ChatBatchShimTests(ChatBase):
    """npm 의 claude.cmd 처럼 .cmd 래퍼가 잡힐 때: 프롬프트는 stdin, 시스템 프롬프트는 한 줄."""
    cli_kind = "cmd"

    @unittest.skipUnless(os.name == "nt", "윈도우 전용 (.cmd 래퍼)")
    def test_batch_wrapper_gets_prompt_on_stdin_and_single_line_arguments(self):
        msg = "여러 줄\n\"따옴표\" 와 %PATH% 가 있는 명령"
        j = self.run_message(msg)
        self.assertEqual(j["status"], "done", j)
        (call,) = self.calls()
        self.assertEqual(call["prompt"], msg)        # stdin 으로 온 원문 그대로
        self.assertNotIn(msg, call["argv"])           # 인자에는 없다
        self.assertEqual(call["argv"][0], "-p")
        system = call["argv"][call["argv"].index("--append-system-prompt") + 1]
        self.assertNotIn("\n", system)
        self.assertIn("approve", system)
        self.assertIn("Bash(python scripts/tracker.py approve:*)", call["argv"][call["argv"].index("--disallowedTools") + 1])
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "sonnet")


# ==========================================================================
# 단위 테스트 (서버 없이)
# ==========================================================================
class ChatUnitTests(unittest.TestCase):
    def test_module_is_stdlib_only(self):
        tree = ast.parse((SCRIPTS / "office_chat.py").read_text(encoding="utf-8"))
        allowed_local = {"_common", "office", "office_server"}
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
        extra = {m for m in mods if m not in sys.stdlib_module_names and m not in allowed_local}
        self.assertEqual(extra, set())

    def test_source_never_asks_for_a_shell(self):
        for name in ("office_chat.py", "office_server.py"):
            src = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn("shell=True", src, name)
            self.assertNotIn("os.system(", src, name)
            self.assertNotIn("os.popen(", src, name)
        self.assertIn("shell=False", (SCRIPTS / "office_chat.py").read_text(encoding="utf-8"))

    def test_build_command_exe(self):
        argv, stdin = office_chat.build_command(r"C:\x\claude.exe", "프롬프트", "opus", "시스템\n프롬프트", "sess-1")
        self.assertIsNone(stdin)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:3], [r"C:\x\claude.exe", "-p", "프롬프트"])
        self.assertEqual(argv[-2:], ["--resume", "sess-1"])
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "시스템\n프롬프트")  # exe 는 여러 줄 인자 OK
        no_resume, _ = office_chat.build_command(r"C:\x\claude.exe", "a", "sonnet", "s")
        self.assertNotIn("--resume", no_resume)
        dash, _ = office_chat.build_command(r"C:\x\claude.exe", "-v 이건 글이다", "sonnet", "s")
        self.assertEqual(dash[2], " -v 이건 글이다")

    def test_build_command_batch_and_py(self):
        argv, stdin = office_chat.build_command(r"C:\x\claude.cmd", "원문\n둘째 줄", "sonnet", '줄1\n"인용" 100%')
        self.assertEqual(stdin, "원문\n둘째 줄".encode("utf-8"))
        self.assertEqual(argv[:2], [r"C:\x\claude.cmd", "-p"])
        self.assertEqual(argv[2], "--output-format")
        system = argv[argv.index("--append-system-prompt") + 1]
        self.assertNotIn("\n", system)
        self.assertNotIn('"', system)
        self.assertNotIn("%", system)
        py, stdin = office_chat.build_command("/t/fake.py", "x", "sonnet", "s")
        self.assertEqual(py[:3], [sys.executable, "/t/fake.py", "-p"])
        self.assertIsNone(stdin)

    def test_discover_cli_order(self):
        with tempfile.TemporaryDirectory(prefix="aire-disc-") as td:
            td = Path(td)
            env_exe = td / "env_claude.exe"
            path_exe = td / "path_claude.exe"
            for p in (env_exe, path_exe):
                p.write_bytes(b"MZ")
            appdata = td / "AppData"
            for ver in ("2.1.9", "2.1.280", "2.1.295", "2.1.1000", "garbage"):
                (appdata / "Claude" / "claude-code" / ver / "sub").mkdir(parents=True)
            (appdata / "Claude" / "claude-code" / "2.1.9" / "sub" / "claude.exe").write_bytes(b"MZ")
            (appdata / "Claude" / "claude-code" / "2.1.295" / "sub" / "claude.exe").write_bytes(b"MZ")
            (appdata / "Claude" / "claude-code" / "2.1.280" / "claude.exe").write_bytes(b"MZ")
            # 2.1.1000 폴더에는 claude.exe 가 없다 -> 다음으로 높은 2.1.295
            base_env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(td / "nolocal"), "SHREW_CLAUDE_BIN": ""}
            no_home = mock.patch.object(Path, "home", return_value=td / "nohome")
            with mock.patch.dict(os.environ, base_env), no_home:
                with mock.patch.object(office_chat.shutil, "which", return_value=None):
                    self.assertEqual(Path(office_chat.discover_cli()), appdata / "Claude" / "claude-code" / "2.1.295" / "sub" / "claude.exe")
                (appdata / "Claude" / "claude-code" / "2.1.1000" / "claude.exe").write_bytes(b"MZ")
                with mock.patch.object(office_chat.shutil, "which", return_value=None):
                    self.assertEqual(Path(office_chat.discover_cli()).parent.name, "2.1.1000")  # 문자열이 아니라 숫자로 비교
                with mock.patch.object(office_chat.shutil, "which", return_value=str(path_exe)):
                    self.assertEqual(office_chat.discover_cli(), str(path_exe))  # PATH 가 APPDATA 보다 먼저
                with mock.patch.dict(os.environ, {"SHREW_CLAUDE_BIN": str(env_exe)}):
                    with mock.patch.object(office_chat.shutil, "which", return_value=str(path_exe)):
                        self.assertEqual(office_chat.discover_cli(), str(env_exe))  # 환경변수가 최우선
                # 환경변수가 가리키는 파일이 없으면 다음 후보로
                with mock.patch.dict(os.environ, {"SHREW_CLAUDE_BIN": str(td / "missing.exe")}):
                    with mock.patch.object(office_chat.shutil, "which", return_value=str(path_exe)):
                        self.assertEqual(office_chat.discover_cli(), str(path_exe))
            with mock.patch.dict(os.environ, {"APPDATA": str(td / "empty"), "LOCALAPPDATA": str(td / "nolocal"), "SHREW_CLAUDE_BIN": ""}), no_home:
                with mock.patch.object(office_chat.shutil, "which", return_value=None):
                    self.assertIsNone(office_chat.discover_cli())

    def test_py_cli_is_ignored_inside_a_frozen_exe(self):
        with tempfile.TemporaryDirectory(prefix="aire-disc-") as td:
            fake = Path(td) / "fake.py"
            fake.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SHREW_CLAUDE_BIN": str(fake), "APPDATA": str(Path(td) / "none"), "LOCALAPPDATA": str(Path(td) / "nolocal")}):
                with mock.patch.object(office_chat.shutil, "which", return_value=None), \
                        mock.patch.object(Path, "home", return_value=Path(td) / "nohome"):
                    self.assertEqual(office_chat.discover_cli(), str(fake))
                    with mock.patch.object(sys, "frozen", True, create=True):
                        self.assertIsNone(office_chat.discover_cli())

    def test_tool_summary(self):
        root = Path(tempfile.gettempdir()) / "proj"
        read = office_chat.tool_summary("Read", {"file_path": str(root / "docs" / "00_전략_Strategy.md")}, root)
        self.assertEqual(read, "docs/00_전략_Strategy.md")
        outside = office_chat.tool_summary("Read", {"file_path": str(root.parent / "elsewhere" / "x.txt")}, root)
        self.assertTrue(outside.endswith("x.txt"))
        self.assertEqual(office_chat.tool_summary("Bash", {"command": "python scripts/office.py show"}, root),
                         "python scripts/office.py show")
        self.assertEqual(office_chat.tool_summary("Grep", {"pattern": "approve", "path": "scripts"}, root), "approve (scripts)")
        self.assertEqual(office_chat.tool_summary("WebFetch", {"url": "https://example.com/a"}, root), "https://example.com/a")
        long = office_chat.tool_summary("Bash", {"command": "echo " + "x" * 500}, root)
        self.assertLessEqual(len(long), 120)
        self.assertEqual(office_chat.tool_summary("Bash", {"command": "a\nb\tc"}, root), "a b c")
        self.assertEqual(office_chat.tool_summary("Mystery", {"foo": [1, 2]}, root), '{"foo": [1, 2]}')
        self.assertEqual(office_chat.tool_summary("Mystery", None, root), "")
        self.assertEqual(office_chat.tool_summary("Mystery", "plain", root), "plain")

    def test_summaries_never_carry_secrets(self):
        root = Path(tempfile.gettempdir())
        cases = [
            "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123' https://x",
            "export ANTHROPIC_API_KEY=sk-ant-api03-AAAAAAAAAAAAAAAAAAAA",
            "login --password=hunter2hunter2",
            "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "AKIAABCDEFGHIJKLMNOP",
        ]
        for cmd in cases:
            summary = office_chat.tool_summary("Bash", {"command": cmd}, root)
            for secret in ("abcdefghijklmnopqrstuvwxyz0123", "sk-ant-api03", "hunter2", "ghp_abcdef", "AKIAABCDEFGH"):
                self.assertNotIn(secret, summary, cmd)
            self.assertIn("***", summary)

    def test_snapshot_text_is_robust(self):
        self.assertIn("읽지 못했습니다", office_chat.snapshot_text(None))
        self.assertIn("읽지 못했습니다", office_chat.snapshot_text("nope"))  # type: ignore[arg-type]
        text = office_chat.snapshot_text({"now": "2026-10-15T01:00:00+09:00", "agents": ["x", {"id": "ceo", "state": "working", "task": "t"}],
                                          "approvals": [None, {"id": "A-1", "action": "publish_listing"}],
                                          "kpis": {"by_status": {"draft": 2, "listed": 0}, "products_total": 2}})
        self.assertIn("ceo(working: t)", text)
        self.assertIn("A-1[publish_listing]", text)
        self.assertIn("draft 2", text)
        self.assertNotIn("listed 0", text)
        self.assertIn("0원", text)
        self.assertTrue(office_chat.snapshot_text({}))

    def test_normalize_message(self):
        self.assertEqual(office_chat.normalize_message("  안녕\n"), "안녕")
        self.assertEqual(office_chat.normalize_message("a\x00b"), "ab")
        self.assertEqual(office_chat.normalize_message("x\ud800y"), "xy")  # 짝 없는 서로게이트 제거
        self.assertEqual(len(office_chat.normalize_message("가" * 8000)), 8000)
        for bad in ("", "   ", None, 5, [], {"a": 1}, "가" * 8001):
            with self.assertRaises(office_chat.ChatError) as cm:
                office_chat.normalize_message(bad)
            self.assertEqual(cm.exception.status, 400)

    def test_parsing_survives_garbage_events(self):
        with tempfile.TemporaryDirectory(prefix="aire-parse-") as td:
            svc = office_chat.ChatService(td)
            job = office_chat.Job("m", "sonnet")
            st = office_chat._Stream()
            weird = [
                {}, {"type": 5}, {"type": "assistant"}, {"type": "assistant", "message": None},
                {"type": "assistant", "message": {"content": "그냥 문자열 내용"}},
                {"type": "assistant", "message": {"content": [None, 3, {"type": "text", "text": 5}, {"type": "tool_use", "name": None, "input": 5}]}},
                {"type": "assistant", "parent_tool_use_id": "t", "message": {"content": [{"type": "text", "text": "서브에이전트"}]}},
                {"type": "stream_event", "event": "nope"}, {"type": "stream_event", "event": {"type": "content_block_delta", "delta": 3}},
                {"type": "system", "subtype": "init", "session_id": 5},
                {"type": "result", "total_cost_usd": "비싸다", "session_id": ""},
                {"type": "result", "total_cost_usd": float("nan")},
                {"type": "result", "total_cost_usd": True},
            ]
            for ev in weird:
                svc._on_event(job, st, ev)
            snap = job.snapshot()
            self.assertEqual(snap["text"], "그냥 문자열 내용")
            self.assertNotIn("서브에이전트", snap["text"])
            self.assertIsNone(snap["cost_usd"])
            self.assertIsNone(snap["session_id"])
            self.assertEqual(snap["tools"][-1]["name"], "tool")

    def test_text_is_capped(self):
        job = office_chat.Job("m", "sonnet")
        job.set_text("x" * (office_chat.MAX_TEXT_CHARS + 5000))
        self.assertLess(len(job.snapshot()["text"]), office_chat.MAX_TEXT_CHARS + 100)

    def test_old_finished_jobs_are_pruned_but_running_ones_are_kept(self):
        with tempfile.TemporaryDirectory(prefix="aire-prune-") as td:
            svc = office_chat.ChatService(td)
            running = office_chat.Job("진행 중", "sonnet")
            running.set_status("running")
            svc._jobs[running.job_id] = running
            svc._order.append(running.job_id)
            for i in range(office_chat.KEEP_JOBS + 5):
                job = office_chat.Job(f"m{i}", "sonnet")
                job.finish("done")
                svc._jobs[job.job_id] = job
                svc._order.append(job.job_id)
            svc._prune()
            self.assertIn(running.job_id, svc._jobs)  # 맨 앞의 실행 중 작업이 있으면 거기서 멈춘다
            self.assertEqual(len(svc._jobs), len(svc._order))
        with tempfile.TemporaryDirectory(prefix="aire-prune-") as td:
            svc = office_chat.ChatService(td)
            for i in range(office_chat.KEEP_JOBS + 5):
                job = office_chat.Job(f"m{i}", "sonnet")
                job.finish("done")
                svc._jobs[job.job_id] = job
                svc._order.append(job.job_id)
            svc._prune()
            self.assertEqual(len(svc._jobs), office_chat.KEEP_JOBS)

    def test_service_root_is_resolved_to_a_long_path(self):
        with tempfile.TemporaryDirectory(prefix="aire-root-") as td:
            svc = office_chat.ChatService(td)
            self.assertEqual(svc.root, Path(td).resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
