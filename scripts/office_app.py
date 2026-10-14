"""Shrew AI 오피스 데스크톱 실행기.

더블클릭용 실행 파일(ShrewOffice.exe)의 본체다.
1) 오피스 서버를 띄운다 (8765가 이미 우리 서버면 그대로 쓰고, 다른 프로그램이 쓰고 있으면 빈 포트로).
2) Edge(없으면 Chrome)를 주소창 없는 앱 창으로 연다.
3) 그 창을 닫으면 서버도 함께 종료한다.

빌드: powershell -File scripts/build_office_app.ps1
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import importlib.util

# 서버 모듈들. exe 로 실행될 때는 빌드 시점 사본 대신 프로젝트 폴더의 최신 scripts/ 를 읽는다
# (코드를 고칠 때마다 exe 를 다시 빌드하지 않아도 되게). 디스크에 없으면 exe 안의 사본을 쓴다.
SERVER_MODULES = ("_common", "office_chat", "office_stream", "office_server")


def _load_server_modules():
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    else:
        # exe 는 프로젝트 루트에 둔다: 루트 = exe 가 있는 폴더
        os.environ.setdefault("AIRE_ROOT", str(Path(sys.executable).resolve().parent))
        scripts = Path(os.environ["AIRE_ROOT"]) / "scripts"
        if (scripts / "office_server.py").is_file():
            sys.path.insert(0, str(scripts))
            for name in SERVER_MODULES:
                path = scripts / f"{name}.py"
                if not path.is_file():
                    continue
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module  # 먼저 등록해야 office_server 의 import _common 이 이 사본을 쓴다
                spec.loader.exec_module(module)
    import _common
    import office_server
    return _common, office_server


C, office_server = _load_server_modules()

PORT = office_server.DEFAULT_PORT
BROWSERS = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def _is_our_server(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as r:
            return "agents" in json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def _start_server(root: Path):
    """(server 또는 None, url). None 이면 이미 떠 있는 서버를 재사용한다."""
    if _is_our_server(PORT):
        return None, f"http://127.0.0.1:{PORT}/"
    try:
        server, _ = office_server.serve_in_thread(root, PORT)
    except OSError:
        server, _ = office_server.serve_in_thread(root, 0)  # 8765 를 다른 프로그램이 쓰는 중
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def _open_window(url: str) -> subprocess.Popen | None:
    exe = next((b for b in BROWSERS if b.is_file()), None)
    if exe is None:
        return None
    profile = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ShrewAIOffice" / "browser"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        str(exe), f"--app={url}", f"--user-data-dir={profile}",
        "--window-size=1440,900", "--no-first-run", "--no-default-browser-check",
    ])


def main() -> int:
    root = C.get_root()
    server, url = _start_server(root)
    window = _open_window(url)
    try:
        if window is not None:
            window.wait()  # 앱 창을 닫을 때까지
        else:
            # 앱 창으로 못 열면 기본 브라우저로 열고, 서버가 꺼지지 않게 프로세스를 붙잡아 둔다
            webbrowser.open(url)
            if server is not None:
                threading.Event().wait()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
