#!/usr/bin/env python3
"""把离线 tar 包里关键文件的可执行位改对（Windows 打包必然丢 exec 位）。

规则：
  openvpn-web-*/bin/*             -> 755   （openvpn / openssl 等静态二进制）
  openvpn-web-*/python/bin/*      -> 755   （python3.12 解释器；软链本身保持 777）
  openvpn-web-*/ 顶层的 *.sh -> 755      （install.sh；serve.py 在 django/ 下不在此列）
  openvpn-web-*/system/*.sh|*.py  -> 755   （worker.py / setup.py / auth-verify.py ...）
  system/install-code.sh          -> 700   （能造安装码，只给 root）
  其余保持不变。
"""
import os
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "dist", "openvpn-web-x86_64.tar.gz")
DST = sys.argv[2] if len(sys.argv) > 2 else SRC + ".new"

ROOT = "openvpn-web-x86_64/"


def fix_mode(name):
    """返回目标 mode，None 表示不改。"""
    rel = name[len(ROOT):] if name.startswith(ROOT) else name
    if not rel or rel == ".":
        return None
    if rel.startswith("bin/") or rel.startswith("python/bin/"):
        return 0o755
    if rel == "system/install-code.sh":     # 造安装码的脚本必须只给 root
        return 0o700
    if rel.startswith("system/") and "/" not in rel[len("system/"):]:
        if rel.endswith((".sh", ".py")):
            return 0o755
    if "/" not in rel.rstrip("/"):          # 顶层文件
        if rel.endswith((".sh", ".py")):
            return 0o755
    return None


def main():
    total = changed = 0
    changed_list = []
    with tarfile.open(SRC, "r:gz") as ti, \
         tarfile.open(DST, "w:gz", compresslevel=6) as to:
        for m in ti:
            total += 1
            want = fix_mode(m.name)
            # 软链不要动 mode（它必须是 0777）
            if want is not None and m.isreg():
                if m.mode != want:
                    changed_list.append((m.name, oct(m.mode), oct(want)))
                    m.mode = want
                    changed += 1
            f = ti.extractfile(m) if m.isreg() else None
            to.addfile(m, f)
    print("条目 %d，修正权限 %d 个" % (total, changed))
    for n, a, b in changed_list[:40]:
        print("  %s  %s -> %s" % (n, a, b))
    if len(changed_list) > 40:
        print("  ... 共 %d 个" % len(changed_list))


if __name__ == "__main__":
    main()
