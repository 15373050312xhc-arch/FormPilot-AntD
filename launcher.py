from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def run(cmd: list[str]) -> int:
    print(">", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def ensure_playwright() -> bool:
    if importlib.util.find_spec("playwright") is not None:
        return True

    print()
    print("Playwright 未安装，正在自动安装...")
    code = run([
        sys.executable, "-m", "pip", "install", "playwright",
        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--default-timeout=120", "--retries", "3",
    ])
    if code != 0:
        print("Playwright 安装失败，请检查网络后重试")
        return False
    # 安装浏览器内核
    run([sys.executable, "-m", "playwright", "install", "chromium"])
    return True


def ensure_pymupdf() -> bool:
    try:
        if importlib.util.find_spec("pymupdf") is not None:
            return True
    except Exception:
        pass
    try:
        if importlib.util.find_spec("fitz") is not None:
            return True
    except Exception:
        pass

    print()
    print("PyMuPDF 未安装，正在自动安装...")
    code = run([
        sys.executable, "-m", "pip", "install", "PyMuPDF",
        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--default-timeout=120", "--retries", "3",
    ])
    if code != 0:
        print("PyMuPDF 安装失败，请检查网络后重试")
        return False
    return True


def main() -> int:
    print("简历自动填表工具 v3 — 客户端排单模式")
    print("保持此窗口打开。浏览器打开后请登录并进入表单页。")
    print()

    if not ensure_playwright():
        input("按回车退出...")
        return 1
    if not ensure_pymupdf():
        input("按回车退出...")
        return 1

    print()
    print("=== 步骤选择 ===")
    print("[1] 抓取表单源码（生成 schema + prompt）")
    print("[2] 按计划填表（读 form_plan.json 执行填写）")
    print()

    if "--dump" in sys.argv:
        choice = "1"
    elif "--apply" in sys.argv:
        choice = "2"
    else:
        choice = input("选择 [1/2] (默认=1): ").strip()

    if choice == "2":
        script = ROOT / "outputs" / "apply_form_plan.py"
    else:
        script = ROOT / "outputs" / "dump_form_schema.py"

    return run([sys.executable, str(script)])


if __name__ == "__main__":
    raise SystemExit(main())
