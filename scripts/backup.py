# -*- coding: utf-8 -*-
"""数据备份（对应上线清单 A5）。

用法：
    python scripts/backup.py                      # 备份到 backups/，保留最近 7 份
    python scripts/backup.py --keep 30            # 保留 30 份
    python scripts/backup.py --out D:/bak         # 指定目录（建议挂到另一块盘）

为什么不用 cp 直接拷 data.db：
    SQLite 开了 WAL，正在写入时 cp 出来的文件可能只包含一半事务，
    恢复时轻则丢数据、重则库损坏。这里用 sqlite3 的在线备份 API，
    它会在事务边界上取一致快照，服务运行中也能安全执行。

恢复方式（重要，写在这里以免出事时找不到）：
    1. 停服务
    2. 删除 data.db / data.db-wal / data.db-shm
    3. 把备份里的 data.db 拷回原位置
    4. 启动服务（会自行重建索引）
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "backups"))
    ap.add_argument("--keep", type=int, default=7)
    args = ap.parse_args()

    if not settings.DB_URL.startswith("sqlite"):
        print("当前不是 SQLite，请改用数据库自带的备份工具（pg_dump 等）")
        return 0

    src = settings.DB_URL.replace("sqlite:///", "")
    if not os.path.isabs(src):
        src = os.path.join(ROOT, src)
    if not os.path.exists(src):
        print(f"数据库不存在：{src}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = os.path.join(args.out, stamp)
    os.makedirs(dest_dir, exist_ok=True)
    dest_db = os.path.join(dest_dir, "data.db")

    # 在线备份：一致性快照，服务运行中执行也安全
    with sqlite3.connect(src) as s, sqlite3.connect(dest_db) as d:
        s.backup(d)
    print(f"✅ 数据库已备份 → {dest_db}（{os.path.getsize(dest_db) / 1024:.1f} KB）")

    # 规则源文件：没有它，规则库只能重新上传
    kd = os.path.join(ROOT, "knowledge_docs")
    if os.path.isdir(kd):
        shutil.copytree(kd, os.path.join(dest_dir, "knowledge_docs"), dirs_exist_ok=True)
        print(f"✅ 规则源文件已备份 → {dest_dir}/knowledge_docs")

    # 只保留最近 N 份，避免备份把磁盘吃满（这是备份脚本最常见的反噬）
    olds = sorted(d for d in os.listdir(args.out)
                  if os.path.isdir(os.path.join(args.out, d)))
    for d in olds[:-args.keep] if len(olds) > args.keep else []:
        shutil.rmtree(os.path.join(args.out, d), ignore_errors=True)
        print(f"  清理旧备份：{d}")
    print(f"当前保留 {min(len(olds), args.keep)} 份，目录：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
