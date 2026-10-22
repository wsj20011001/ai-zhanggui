# -*- coding: utf-8 -*-
"""一键跑完全部测试，输出汇总表。

评委验证项目时只需要一条命令：
    venv\\Scripts\\python run_tests.py

设计说明：
- 每个套件是独立进程，互不影响（某个套件崩了不会污染其他套件）；
- 汇总按"通过 / 失败 / 耗时"输出，退出码非 0 表示有失败，方便 CI 接入；
- AI 问答相关测试会临时把模型通道钉成规则模式，**不会消耗 API 额度**。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = [
    ("页面冒烟", "tests/smoke.py", "22 个页面全量渲染，检查模板/路由/变量"),
    ("AI 沙箱与执行", "tests/test_ai.py", "8 条越界 SQL 拦截 + 问答 + 洞察执行 + 撤销"),
    ("口径一致性", "tests/test_metrics.py", "原始表/视图/事实表/驾驶舱/报表/AI 五处同值"),
    ("CSV 导入", "tests/test_import.py", "表头识别 / 订单号幂等 / 失败行 / 事实表重算"),
    ("点餐端", "tests/test_mobile.py", "人数/备注落库、下单字段、催菜状态、取号不撞车"),
    ("模型链路（Mock）", "tests/mock_llm.py", "越界 SQL → 拦截 → 自修复 → 真实结果，无需真 Key"),
    ("本地通道（Mock）", "tests/test_local_channel.py", "Ollama 协议：识别模型、仅本地调用、绕过代理"),
    ("单元测试", "test_units.py", "金额舍入 / 订单号 / 口令哈希 / BOM 扣料"),
]


def main():
    print("=" * 78)
    print("AI 掌柜 · 测试总览    Python %s" % sys.version.split()[0])
    print("=" * 78)
    rows = []
    t_all = time.time()
    for name, path, desc in CASES:
        f = ROOT / path
        if not f.exists():
            rows.append((name, path, "缺失", 0.0, desc))
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, str(f)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        cost = time.time() - t0
        ok = p.returncode == 0
        tail = [l for l in (p.stdout or "").strip().splitlines() if l.strip()]
        summary = tail[-1][:40] if tail else (p.stderr or "").strip().splitlines()[-1][:40]
        rows.append((name, path, "通过" if ok else "失败", cost, summary))

    print()
    print("%-16s %-22s %-6s %8s  %s" % ("套件", "文件", "结果", "耗时", "说明"))
    print("-" * 78)
    for name, path, status, cost, desc in rows:
        flag = "✓" if status == "通过" else "✗"
        print("%s %-14s %-22s %-6s %7.1fs  %s"
              % (flag, name, path, status, cost, desc))
    passed = sum(1 for r in rows if r[2] == "通过")
    failed = len(rows) - passed
    total = time.time() - t_all
    print("-" * 78)
    print("套件 %d 个：通过 %d，失败 %d，总耗时 %.1fs" % (len(rows), passed, failed, total))
    if failed:
        print("\n有失败项，请单独运行对应文件查看详细输出。")
    else:
        print("\n全部通过。覆盖：页面渲染、SQL 沙箱、口径一致、数据导入、模型链路、单元逻辑。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
