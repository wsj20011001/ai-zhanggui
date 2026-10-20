# -*- coding: utf-8 -*-
"""把演示库恢复到初始状态（现场演示前跑一次，防止彩排把数据改乱）。

原理：seed_demo 之后生成一份一致性快照 demo_snapshot.db（VACUUM INTO），
需要时直接覆盖回 restaurant.db，并清掉 WAL 旁文件。
"""
import sqlite3
import sys
from pathlib import Path

from config import DB_PATH

SNAP = DB_PATH.parent / "demo_snapshot.db"


def make():
    conn = sqlite3.connect(str(DB_PATH))
    try:
        SNAP.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(SNAP),))
    finally:
        conn.close()
    print("快照已生成：%s（%.1f MB）" % (SNAP, SNAP.stat().st_size / 1024 / 1024))


def restore():
    if not SNAP.exists():
        print("没有快照，请先运行：python reset_demo.py --make")
        return 1
    for suffix in ("-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    import shutil
    shutil.copyfile(str(SNAP), str(DB_PATH))
    print("已恢复演示库（%s）" % DB_PATH)
    return 0


if __name__ == "__main__":
    if "--restore" in sys.argv:
        sys.exit(restore())
    make()
