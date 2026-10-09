#!/usr/bin/env python3
"""
serve.py —— 本地词库服务

给 xlate 用户脚本读 glossary.json 用。

为什么需要它：
  Chrome MV3 之后，浏览器扩展基本无法用 XHR 读取 file:// 本地文件，
  所以 file:// 那条路在多数环境下走不通。改成 localhost HTTP 最可靠：
  用户脚本用 GM_xmlhttpRequest 访问 http://127.0.0.1:8765/glossary.json 即可。

用法：
  & "C:\\Users\\14386\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" serve.py
  # 或改端口 / 目录：
  python serve.py --port 8765 --dir .

停止：Ctrl+C
"""

import argparse
import http.server
import os
import socket
import socketserver
import sys

# 只服务这些后缀，避免把整个目录暴露出去
ALLOWED_SUFFIX = (".json", ".txt", ".md")


class Handler(http.server.SimpleHTTPRequestHandler):
    """带正确编码 + 禁缓存的静态文件服务。"""

    def end_headers(self):
        # 禁缓存：改完 glossary.json 刷新页面就能拿到新版
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # 允许跨源（用户脚本其实用 GM_xmlhttpRequest 绕过 CORS，这里是双保险）
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def guess_type(self, path):
        # 关键：显式声明 charset=utf-8，否则中文可能变乱码
        base, ext = os.path.splitext(path)
        if ext.lower() == ".json":
            return "application/json; charset=utf-8"
        if ext.lower() in (".txt", ".md"):
            return "text/plain; charset=utf-8"
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        # 静默，避免刷屏；只保留错误
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("  ! " + (fmt % args) + "\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def local_ip():
    """拿本机在局域网里的地址（拿不到就返回 None）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="为 xlate 用户脚本提供 glossary.json")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    ap.add_argument("--dir", default=".", help="服务目录（默认当前目录）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址（默认只绑本机；填 0.0.0.0 允许局域网访问）")
    args = ap.parse_args()

    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        print(f"❌ 目录不存在: {root}")
        return 1

    gl = os.path.join(root, "glossary.json")
    if not os.path.isfile(gl):
        print(f"⚠️  警告：{root} 下没有 glossary.json")

    def handler(*a, **kw):
        return Handler(*a, directory=root, **kw)

    try:
        httpd = Server((args.host, args.port), handler)
    except OSError as e:
        print(f"❌ 无法绑定 {args.host}:{args.port} —— {e}")
        print("   端口可能被占用，换一个：--port 8766")
        return 1

    url = f"http://127.0.0.1:{args.port}/glossary.json"
    print("=" * 62)
    print("  xlate 本地词库服务")
    print("=" * 62)
    print(f"  目录    : {root}")
    print(f"  词库    : {url}")
    print()
    print("  把上面的地址填进用户脚本：")
    print("    Tampermonkey 菜单 → 📁 设置词库地址")
    print()
    if args.host == "0.0.0.0":
        ip = local_ip()
        if ip:
            print(f"  局域网  : http://{ip}:{args.port}/glossary.json")
    print("  停止    : Ctrl+C")
    print("=" * 62)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
