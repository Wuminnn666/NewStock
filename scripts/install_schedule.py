# -*- coding: utf-8 -*-
"""把「备份」和「数据保留清理」挂成系统定时任务（对应上线清单 A5 / A3）。

为什么用脚本而不是让人手动敲 cron：
定时任务是最容易被「部署时忘了配」的一项——代码全对，但没人挂任务，
真出事时才发现没有备份。把它做成幂等脚本（重复执行只会覆盖同一个任务名），
就能写进发布手册，成为流程的一部分而不是口头约定。

用法：
    python scripts/install_schedule.py            # 只打印将要执行的命令（默认，安全）
    python scripts/install_schedule.py --apply    # 真正注册

说明：
- Windows 用 schtasks，任务名固定为 NewStock-Backup / NewStock-Retention，
  重复执行会先 delete 再 create（/F 覆盖），不会堆出一堆同名任务。
- Linux/macOS 用 crontab：把本项目已有条目之外的两行追加进去（同样按注释标记去重）。
- 备份每天 02:30、保留清理每月 1 日 03:30（清理默认 dry-run，确认无误后再加 --execute）。
"""
import os
import platform
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python"

# 备份：每天一次全量在线快照（SQLite backup API，不锁库）
BACKUP_CMD = f'"{PY}" "{os.path.join(ROOT, "scripts", "backup.py")}"'
# 清理：每月一次，默认 dry-run；确认统计无误后把 --execute 加上
RETENTION_CMD = f'"{PY}" "{os.path.join(ROOT, "scripts", "retention.py")}"'

MARK = "# NewStock-schedule"          # cron 条目的去重标记


def windows_cmds():
    """生成 schtasks 命令列表。用 /SC DAILY 与 /SC MONTHLY 表达频率。"""
    return [
        ["schtasks", "/Delete", "/TN", "NewStock-Backup", "/F"],
        ["schtasks", "/Create", "/F", "/TN", "NewStock-Backup",
         "/TR", BACKUP_CMD, "/SC", "DAILY", "/ST", "02:30", "/RL", "HIGHEST"],
        ["schtasks", "/Delete", "/TN", "NewStock-Retention", "/F"],
        ["schtasks", "/Create", "/F", "/TN", "NewStock-Retention",
         "/TR", RETENTION_CMD, "/SC", "MONTHLY", "/D", "1", "/ST", "03:30",
         "/RL", "HIGHEST"],
    ]


def cron_lines():
    return [
        f"{MARK} 每天 02:30 备份；每月 1 日 03:30 清理（默认 dry-run）",
        f"30 2 * * * cd {ROOT} && {BACKUP_CMD} >> logs/backup.log 2>&1",
        f"30 3 1 * * cd {ROOT} && {RETENTION_CMD} >> logs/retention.log 2>&1",
    ]


def install_cron():
    """把两行写进 crontab，先剔除旧的本项目条目（幂等）。"""
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = cur.stdout if cur.returncode == 0 else ""
    kept = [ln for ln in existing.splitlines() if "scripts/backup.py" not in ln
            and "scripts/retention.py" not in ln and ln.strip() != MARK]
    new = "\n".join([ln for ln in kept if ln.strip()] + cron_lines()) + "\n"
    p = subprocess.run(["crontab", "-"], input=new, text=True, capture_output=True)
    return p.returncode == 0, p.stderr


def main():
    apply = "--apply" in sys.argv
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    is_win = platform.system().lower().startswith("win")

    print(f"项目根目录：{ROOT}")
    print(f"执行身份：{PY}")
    print(f"模式：{'真正注册' if apply else '仅打印（加 --apply 才注册）'}\n")

    if is_win:
        for cmd in windows_cmds():
            line = subprocess.list2cmdline(cmd)
            print("  " + line)
            if apply:
                # 删除可能失败（任务不存在），属正常，不中断
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0 and "/Delete" not in cmd:
                    print("    失败：", (r.stderr or r.stdout).strip()[:300])
        if apply:
            print("\n已注册。核对：schtasks /Query /TN NewStock-Backup /V /FO LIST")
    else:
        for line in cron_lines():
            print("  " + line)
        if apply:
            ok, err = install_cron()
            print("\n已写入 crontab" if ok else f"\n写入失败：{err.strip()[:300]}")
            if ok:
                print("核对：crontab -l | grep -A2 NewStock")

    print("\n提醒：注册后必须做一次「删表 → 从备份恢复 → 比对行数」的演练，"
          "否则只是挂了个任务，不等于能恢复（清单 A5 的通过标准要求有演练记录）。")


if __name__ == "__main__":
    main()
