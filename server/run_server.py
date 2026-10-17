"""生产级启动入口：优先使用 waitress 多线程 WSGI，未安装则回退 Flask 内置服务器。

用法:
    python run_server.py
环境变量:
    SMARTTOURISM_HOST    监听地址，默认 0.0.0.0
    SMARTTOURISM_PORT    监听端口，默认 5050
    SMARTTOURISM_THREADS waitress 工作线程数，默认 8
    SMARTTOURISM_WSGI    设为 flask 可强制使用内置服务器（调试用）
"""

import os
import sys

_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

import app as app_module  # noqa: E402

HOST = os.getenv("SMARTTOURISM_HOST", "0.0.0.0")
PORT = int(os.getenv("SMARTTOURISM_PORT", "5050"))
THREADS = int(os.getenv("SMARTTOURISM_THREADS", "8"))
FORCE_FLASK = os.getenv("SMARTTOURISM_WSGI", "").strip().lower() == "flask"


def _banner(backend: str) -> None:
    flask_app = app_module.app
    print("=" * 60)
    print("智旅灵犀 SmartTourism Server")
    print("=" * 60)
    print(f"WSGI 后端 : {backend}")
    print(f"监听地址  : http://{HOST}:{PORT}")
    print(f"运维面板  : http://127.0.0.1:{PORT}/admin")
    print(f"健康检查  : http://127.0.0.1:{PORT}/api/health")
    print(f"日志目录  : {app_module.LOG_DIR}")
    print("=" * 60)


def _port_in_use(host: str, port: int) -> bool:
    """启动前探测端口是否已被占用，给出比原始 traceback 更清晰的提示。"""
    import socket
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe, port)) == 0


def _fail_port_busy() -> None:
    print("=" * 60)
    print(f"[run_server] 启动失败：端口 {PORT} 已被占用。")
    print("  很可能是之前的 SmartTourism 服务还没关闭。请任选其一：")
    print(f"  1) 关掉旧服务窗口，或结束占用进程：")
    print(f"     netstat -ano | findstr :{PORT}   然后   taskkill /PID <上面的PID> /F")
    print(f"  2) 换一个端口启动： set SMARTTOURISM_PORT=5060 后再运行本脚本")
    print("=" * 60)


def main() -> None:
    if _port_in_use(HOST, PORT):
        _fail_port_busy()
        sys.exit(1)

    if not FORCE_FLASK:
        try:
            from waitress import serve
        except ImportError:
            print("[run_server] 未检测到 waitress，回退到 Flask 内置服务器（仅建议开发环境使用）。")
            print("[run_server] 生产环境请执行: pip install waitress")
        else:
            _banner(f"waitress (threads={THREADS})")
            try:
                serve(app_module.app, host=HOST, port=PORT, threads=THREADS)
            except OSError as exc:
                print(f"[run_server] waitress 启动失败: {exc}")
                _fail_port_busy()
                sys.exit(1)
            return

    _banner(f"flask dev (threaded=True, debug={app_module.DEBUG_ENABLED})")
    try:
        app_module.app.run(host=HOST, port=PORT, debug=app_module.DEBUG_ENABLED, threaded=True)
    except OSError as exc:
        print(f"[run_server] Flask 启动失败: {exc}")
        _fail_port_busy()
        sys.exit(1)


if __name__ == "__main__":
    main()
