# -*- coding: utf-8 -*-
"""在「git 走不通、但 api.github.com 通」的环境里推送本地提交。

背景：本机到 github.com:443 的 CONNECT 会被代理打断（502），
`git push` 无法建链；而 api.github.com 直连正常。于是改成：
    blob（逐文件）→ tree（带 base_tree）→ commit（父提交 = 远端 head）→ 更新 ref
效果与 `git push` 等价（远端多一条你本地这条提交），且是 fast-forward，不需要 force。

安全约定：令牌只从环境变量 GITHUB_TOKEN 读取，不写进代码、不落盘、不回显。

用法：
    GITHUB_TOKEN=xxx python scripts/git_api_push.py            # 推送 main
    GITHUB_TOKEN=xxx python scripts/git_api_push.py --tag v1.0.0   # 顺带推 tag
    python scripts/git_api_push.py --dry-run                   # 只看要传哪些文件
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def sh(*args) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def remote_repo() -> str:
    url = sh("git", "remote", "get-url", "origin")
    # https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
    tail = url.split("github.com")[-1].lstrip(":/")
    return tail[:-4] if tail.endswith(".git") else tail


class Gh:
    def __init__(self, token):
        self.token = token
        # 关键：显式禁用系统代理，否则请求会被代理拦成 502
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "newstock-push",
        })
        try:
            with self.opener.open(req, timeout=60) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            print(f"[API {e.code}] {method} {path} -> {body}")
            raise


def local_files() -> dict:
    """path -> blob sha（用 --raw 的 sha，避免把文件内容读进内存两次）。"""
    out = {}
    for line in sh("git", "ls-files", "-s").splitlines():
        meta, path = line.split("\t", 1)
        _mode, sha, _stage = meta.split()
        out[path] = (sha, _mode)
    return out


def blob_content(sha: str) -> str:
    """取本地 blob 的原始字节并 base64（Git 对象是二进制安全的）。"""
    raw = subprocess.run(["git", "cat-file", "blob", sha], capture_output=True, check=True).stdout
    return base64.b64encode(raw).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="main")
    ap.add_argument("--tag")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token and not a.dry_run:
        sys.exit("缺少 GITHUB_TOKEN 环境变量（不要在代码里硬编码令牌）")

    repo = remote_repo()
    gh = Gh(token) if token else None

    local_head = sh("git", "rev-parse", "HEAD")
    local_tree = sh("git", "rev-parse", "HEAD^{tree}")
    msg = sh("git", "log", "-1", "--format=%B")
    author = sh("git", "log", "-1", "--format=%an|%ae|%aI").split("|")

    if a.dry_run:
        print(f"仓库 {repo} / 分支 {a.branch}")
        print(f"本地 HEAD {local_head[:8]} tree {local_tree[:8]}")
        print("文件数:", len(local_files()))
        return

    ref = gh.call("GET", f"/repos/{repo}/git/ref/heads/{a.branch}")
    remote_head, remote_tree = ref["object"]["sha"], None
    remote_tree = gh.call("GET", f"/repos/{repo}/git/commits/{remote_head}")["tree"]["sha"]
    print(f"远端 {remote_head[:8]} tree {remote_tree[:8]}")

    if remote_tree == local_tree:
        print("远端 tree 与本地完全一致，无需推送")
        return

    # 远端现有文件清单，用于判断新增/修改/删除
    rt = gh.call("GET", f"/repos/{repo}/git/trees/{remote_tree}?recursive=1")
    remote_map = {x["path"]: x["sha"] for x in rt["tree"] if x["type"] == "blob"}

    lf = local_files()
    changed = [p for p, (sha, _m) in lf.items() if remote_map.get(p) != sha]
    deleted = [p for p in remote_map if p not in lf]
    print(f"新增/修改 {len(changed)} 个，删除 {len(deleted)} 个")

    entries = []
    for i, path in enumerate(changed, 1):
        sha, mode = lf[path]
        blob = gh.call("POST", f"/repos/{repo}/git/blobs",
                       {"content": blob_content(sha), "encoding": "base64"})
        entries.append({"path": path, "mode": "100755" if mode == "100755" else "100644",
                        "type": "blob", "sha": blob["sha"]})
        print(f"  [{i}/{len(changed)}] {path}")
    for path in deleted:
        # sha=null 表示在 tree 中删除该路径
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    new_tree = gh.call("POST", f"/repos/{repo}/git/trees",
                       {"base_tree": remote_tree, "tree": entries})["sha"]
    if new_tree != local_tree:
        print(f"注意：服务端重建的 tree {new_tree[:8]} 与本地 {local_tree[:8]} 不同"
              "（行尾/可执行位差异），内容以本地工作区为准")

    commit = gh.call("POST", f"/repos/{repo}/git/commits", {
        "message": msg,
        "tree": new_tree,
        "parents": [remote_head],
        "author": {"name": author[0], "email": author[1], "date": author[2]},
        "committer": {"name": author[0], "email": author[1], "date": author[2]},
    })
    gh.call("PATCH", f"/repos/{repo}/git/refs/heads/{a.branch}",
            {"sha": commit["sha"], "force": False})
    print(f"已推送 {a.branch} -> {commit['sha'][:8]}")
    print("  https://github.com/" + repo + "/commit/" + commit["sha"])

    if a.tag:
        try:
            gh.call("POST", f"/repos/{repo}/git/refs",
                    {"ref": f"refs/tags/{a.tag}", "sha": commit["sha"]})
            print(f"已创建远端 tag {a.tag}")
        except urllib.error.HTTPError:
            print(f"tag {a.tag} 可能已存在，跳过")


if __name__ == "__main__":
    main()
