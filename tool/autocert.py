#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent

    WACS = project_root / "win-acme" / "wacs.exe"

    cert_print = project_root / "certs"
    cert_print.mkdir(parents=True, exist_ok=True)

    SSL_API = os.environ.get("SSL_API")
    if SSL_API is None or not SSL_API.strip():
        raise RuntimeError("請設定 SSL_API 環境變數")

    wacs_args = [
        "--source", "manual",
        "--host", "capstone-project.me",
        "--validation", "cloudflare",
        "--cloudflareapitoken", SSL_API,
        "--store", "pemfiles",
        "--pemfilespath", str(cert_print),
        "--emailaddress", "evan060893@gmail.com",
        "--accepttos",
        "--verbose",
        "--closeonfinish",
    ]

    if os.name == "nt":
        cmd = [str(WACS), *wacs_args]
    else:
        cmd = ["wine", str(WACS), *wacs_args]

    print("執行指令：", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            errors="ignore",
            encoding="utf-8",
            cwd=str(project_root),
        )
    except FileNotFoundError as e:
        print("執行失敗：找不到可執行檔。")
        print("錯誤訊息：", e)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("執行逾時（超過 600 秒）。")
        sys.exit(1)

    print("Exit code:", proc.returncode)
    print("STDOUT:\n", proc.stdout)
    print("STDERR:\n", proc.stderr)

    if proc.returncode != 0:
        sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
