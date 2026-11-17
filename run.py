"""Start the Contract Register.  python run.py [--lan] [--port 8000]"""
import argparse
import socket
import sys
import threading
import time
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本 / Python 3.9+ required")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "本机IP"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lan", action="store_true", help="allow colleagues on the office network to connect")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://localhost:{args.port}"
    print("=" * 60)
    print(" 合同管理系统 Contract Register")
    print(f" 本机访问 Open:      {url}")
    if args.lan:
        print(f" 同事访问 Colleagues: http://{lan_ip()}:{args.port}")
    print(" 关闭此窗口即停止系统 / Close this window to stop")
    print("=" * 60)
    threading.Thread(target=lambda: (time.sleep(2.5), webbrowser.open(url)), daemon=True).start()
    import uvicorn
    uvicorn.run("app.main:app", host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        from pathlib import Path
        err = traceback.format_exc()
        print(err)
        with open(Path(__file__).with_name("start.log"), "a", encoding="utf-8") as f:
            f.write(err)
        raise
