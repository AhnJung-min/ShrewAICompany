#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""office_server.py - 오피스 대시보드 서버 (표준 라이브러리 http.server, 127.0.0.1 전용).

  python office_server.py [--port 8765] [--open] [--verbose]

  GET /            -> ROOT/office/index.html  (없으면 안내 페이지)
  GET /api/state   -> 규약 §2-4 의 JSON  (파일이 없거나 깨져도 500 을 내지 않는다)
  그 외 GET        -> ROOT/office/ 아래 정적 파일
모든 응답에 Cache-Control: no-store.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
EVENT_LIMIT = 80
PRODUCT_LIMIT = 20


# --------------------------------------------------------------------------
# /api/state 구성
# --------------------------------------------------------------------------
def _guard(fn: Callable[[], Any], default: Any) -> Any:
    """한 섹션이 예상 밖으로 실패해도 나머지는 정상 응답하도록."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - 서버는 어떤 데이터에도 죽지 않는다
        return default


def _agent_view(data: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    agent = dict(C.json_safe(data))
    raw_id = agent.get("id")
    iid = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else fallback_id  # 문자열 id 만 신뢰, 아니면 파일 이름
    agent["id"] = iid
    raw_role = agent.get("role")
    agent["role"] = raw_role if isinstance(raw_role, str) and raw_role else C.role_of(iid)
    state = agent.get("state")
    if not isinstance(state, str) or state not in C.STATES:
        agent["state"] = "idle"
    for key in ("task", "bubble", "updated_at"):
        v = agent.get(key)
        agent[key] = "" if v is None else str(v)
    if "product_id" in agent and not isinstance(agent["product_id"], str):
        agent["product_id"] = "" if agent["product_id"] is None else str(agent["product_id"])
    return agent


def read_agents(root) -> list[dict[str, Any]]:
    sdir = C.status_dir(root)
    agents: list[dict[str, Any]] = []
    if not sdir.is_dir():
        return agents
    for p in sdir.glob("*.json"):
        try:  # 파일 하나가 이상해도 그 파일만 건너뛴다 (전체 직원이 사라지지 않게)
            data = C.read_json_file(p)
            if isinstance(data, dict):
                agents.append(_agent_view(data, p.stem))
        except Exception:  # noqa: BLE001
            continue
    agents.sort(key=lambda a: C.instance_sort_key(a["id"]))
    return agents


def _event_view(ev: dict[str, Any]) -> dict[str, Any]:
    """계약의 이벤트 키(ts,id,role,type,msg)는 항상 문자열로 존재하게 한다 (state 는 있을 때만)."""
    out = dict(C.json_safe(ev))
    for key in ("ts", "id", "role", "type", "msg"):
        v = out.get(key)
        out[key] = "" if v is None else (v if isinstance(v, str) else str(v))
    return out


def _approval_view(row: dict[str, str]) -> dict[str, str]:
    return {c: row.get(c, "") for c in C.APPROVAL_COLS}


def _product_view(row: dict[str, str]) -> dict[str, Any]:
    view: dict[str, Any] = {c: row.get(c, "") for c in C.PRODUCT_COLS}
    price = C.parse_int(row.get("price_krw"))
    view["price_krw"] = price if price is not None else 0
    return view


def read_products(root) -> list[dict[str, str]]:
    return [r for r in C.read_csv_rows(C.products_csv(root)) if r.get("id")]


def compute_kpis(root, products: list[dict[str, str]], today=None) -> dict[str, Any]:
    by_status = {s: 0 for s in C.PRODUCT_STATUSES}
    for r in products:
        st = (r.get("status") or "").strip().lower() or "unknown"
        by_status[st] = by_status.get(st, 0) + 1

    this_month = C.month_key(today or C.today_kst())
    total = C.new_bucket()
    month = C.new_bucket()
    for entry in C.load_ledger(root):
        C.bucket_add(total, entry)
        if entry["month"] == this_month:
            C.bucket_add(month, entry)
    t, m = C.bucket_totals(total), C.bucket_totals(month)
    return {
        "products_total": len(products),
        "by_status": by_status,
        "revenue_krw_total": t["revenue"],
        "revenue_krw_this_month": m["revenue"],
        "expenses_krw_total": t["costs"],
        "net_krw_total": t["net"],
    }


def build_state(root=None) -> dict[str, Any]:
    root = Path(root) if root is not None else C.get_root()
    products = _guard(lambda: read_products(root), [])
    approvals = _guard(
        lambda: [_approval_view(r) for r in C.read_csv_rows(C.approvals_csv(root))
                 if r.get("id") and (r.get("status") or "").strip().lower() == "pending"], [])
    empty_kpis = {
        "products_total": 0, "by_status": {s: 0 for s in C.PRODUCT_STATUSES},
        "revenue_krw_total": 0, "revenue_krw_this_month": 0, "expenses_krw_total": 0, "net_krw_total": 0,
    }
    return {
        "now": C.now_iso(),
        "agents": _guard(lambda: read_agents(root), []),
        "events": _guard(lambda: [_event_view(e) for e in C.read_jsonl_tail(C.events_path(root), EVENT_LIMIT)], []),
        "kpis": _guard(lambda: compute_kpis(root, products), empty_kpis),
        "approvals": approvals,
        "products": _guard(lambda: [_product_view(r) for r in products[-PRODUCT_LIMIT:]], []),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
FALLBACK_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>AI Revenue Engine</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,'Malgun Gothic',sans-serif;max-width:640px;margin:15vh auto;padding:0 20px;color:#222}
code{background:#eee;padding:2px 6px;border-radius:4px}a{color:#0a58ca}</style></head>
<body><h1>AI Revenue Engine</h1>
<p>오피스 서버는 실행 중이지만 <code>office/index.html</code> 이 아직 없습니다.</p>
<p>상태 API는 <a href="/api/state">/api/state</a> 에서 확인할 수 있습니다.</p></body></html>
"""

_MIME = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm", ".map": "application/json",
}


EMPTY_STATE: dict[str, Any] = {
    "now": "", "agents": [], "events": [],
    "kpis": {"products_total": 0, "by_status": {s: 0 for s in C.PRODUCT_STATUSES}, "revenue_krw_total": 0,
             "revenue_krw_this_month": 0, "expenses_krw_total": 0, "net_krw_total": 0},
    "approvals": [], "products": [],
}


class OfficeHandler(SimpleHTTPRequestHandler):
    server_version = "AIREOffice/1.0"
    timeout = 30  # 데이터를 안 보내고 붙잡고만 있는 연결(브라우저 preconnect 등)이 스레드를 영구 점유하지 않게
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **_MIME}

    def __init__(self, *args, **kwargs):
        server = args[2] if len(args) > 2 else kwargs.get("server")
        super().__init__(*args, directory=str(server.office_dir), **kwargs)

    # -- 공통 --------------------------------------------------------------
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def list_directory(self, path):  # 디렉터리 목록 노출 금지
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        return None

    def _send_bytes(self, status: int, ctype: str, body: bytes, head_only: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- 라우팅 ------------------------------------------------------------
    def _route(self, head_only: bool) -> bool:
        """True 면 처리 완료, False 면 정적 파일 처리로 넘김."""
        path = urlsplit(self.path).path
        if path.rstrip("/") == "/api/state":
            state = _guard(lambda: build_state(self.server.root), None)
            if state is None:
                state = dict(EMPTY_STATE, now=C.now_iso())
            try:
                body = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, TypeError, RecursionError):  # 표준 JSON 이 아닌 값 -> 빈 상태라도 유효한 JSON 으로
                body = json.dumps(dict(EMPTY_STATE, now=C.now_iso()), ensure_ascii=False).encode("utf-8")
            self._send_bytes(200, "application/json; charset=utf-8", body, head_only)
            return True
        if path.startswith("/api/"):
            body = json.dumps({"error": "not found"}).encode("utf-8")
            self._send_bytes(404, "application/json; charset=utf-8", body, head_only)
            return True
        if path in ("/", "/index.html") and not (self.server.office_dir / "index.html").is_file():
            self._send_bytes(200, "text/html; charset=utf-8", FALLBACK_HTML.encode("utf-8"), head_only)
            return True
        return False

    def _static(self, method: Callable[[], None]) -> None:
        try:
            method()
        except ValueError:  # 예: /%00 -> open() 의 'embedded null character'. 스레드가 죽으며 응답이 끊기지 않게 400 으로.
            try:
                self.send_error(HTTPStatus.BAD_REQUEST, "Bad request path")
            except OSError:
                pass

    def do_GET(self) -> None:  # noqa: N802
        if not self._route(False):
            self._static(super().do_GET)

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._route(True):
            self._static(super().do_HEAD)


class OfficeServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 에서 SO_REUSEADDR 는 '이미 쓰는 포트에 또 바인딩'을 허용해 버리므로 끈다.
    allow_reuse_address = os.name != "nt"

    root: Path
    office_dir: Path
    verbose: bool = False

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return  # 브라우저가 탭을 닫으며 끊은 연결 - 무시
        super().handle_error(request, client_address)


def make_server(root=None, port: int = DEFAULT_PORT, verbose: bool = False) -> OfficeServer:
    """127.0.0.1 에 바인딩된 서버 생성 (port=0 이면 빈 포트 자동 선택)."""
    root = Path(root) if root is not None else C.get_root()
    server = OfficeServer((HOST, port), OfficeHandler)
    server.root = root
    server.office_dir = root / "office"
    server.verbose = verbose
    return server


def serve_in_thread(root=None, port: int = 0) -> tuple[OfficeServer, threading.Thread]:
    server = make_server(root, port)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    return server, t


def main(argv: list[str] | None = None) -> int:
    C.force_utf8()
    ap = argparse.ArgumentParser(prog="office_server.py", description="AI Revenue Engine 오피스 대시보드 서버")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    ap.add_argument("--open", action="store_true", help="시작 후 브라우저로 열기")
    ap.add_argument("--verbose", "-v", action="store_true", help="요청 로그 출력")
    args = ap.parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("오류: --port 는 0~65535 여야 합니다.", file=sys.stderr)
        return 2

    root = C.get_root()
    try:
        server = make_server(root, args.port, args.verbose)
    except OSError as exc:
        print(f"오류: {HOST}:{args.port} 에 서버를 열 수 없습니다 ({exc}). 이미 실행 중이면 --port 로 다른 포트를 쓰세요.",
              file=sys.stderr)
        return 1

    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"AI Revenue Engine 오피스 서버 시작: {url}", flush=True)
    print(f"  루트: {root}", flush=True)
    print(f"  상태 API: {url}api/state   (종료: Ctrl+C)", flush=True)
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
