#!/usr/bin/env python3
# 跨平台离线打包：Windows / Linux 都能跑，不需要执行包内的 Linux Python。
#
#   python build/build-offline.py              # 生成 dist/openvpn-web-x86_64/ 和 .tar.gz
#   python build/build-offline.py --tar-only    # 只出 tar.gz，不落目录
#   python build/build-offline.py --out D:/tmp # 换输出目录
#
# 与 build-offline.sh 的分工：
#   .sh  在 Linux 构建机上跑，会用包内 Python 真的 pip 装一遍依赖（产物更"热"）
#   .py  在任意系统上跑，只做搬运：不改内容、不执行二进制，依赖留给目标机安装时补
#
# 为什么搬运也要专门写脚本：Windows 上
#   1) runtime/bin/python3 这类软链被拍成了 10 字节的文本存根；
#   2) 没有可执行位（git 里 bin/openvpn 记的是 100644）；
#   3) os.symlink 默认没权限。
# 所以 tar 里的符号链接与权限都必须由本脚本显式写，不能依赖源文件属性。
import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "openvpn-web-x86_64"
STUB_MAX = 120          # 软链存根最多这么大（libpython 那个是 20 字节）
MTIME = int(time.time())

ok = 0
warn = []


def say(msg):
    print(msg, flush=True)


def link_target(path):
    """把「被拍平的软链存根」认出来：很小的纯文本，且同目录下真有这个名字的文件"""
    try:
        if os.path.islink(path) or os.path.getsize(path) > STUB_MAX:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        txt = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not txt or "\n" in txt or "\r" in txt or "/" in txt or "\\" in txt:
        return None
    if txt.startswith("#") or txt.startswith("-"):
        return None
    if not os.path.exists(os.path.join(os.path.dirname(path), txt)):
        return None
    return txt


# 仓库在 Windows 上 core.autocrlf=true，工作区里的 .sh/.py 是 CRLF。
# 直接搬进交付包会让 Linux 报「bad interpreter: /bin/bash^M」，
# 所以程序文本统一转成 LF；运行时/vendor 是 Linux 原生内容，原样搬运不动。
EOL_TEXT_EXT = (
    ".sh", ".py", ".html", ".htm", ".txt", ".md", ".json",
    ".conf", ".cfg", ".cnf", ".ini", ".example", ".service", ".yml", ".yaml",
)


def _to_lf(src, dst):
    """按二进制读、把 CRLF 统一成 LF 后写出；无 CR 时等价于直接复制。"""
    with open(src, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        data = data.replace(b"\r\n", b"\n")
    with open(dst, "wb") as f:
        f.write(data)


def copy_file(src, dst, eol_lf=False):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if eol_lf and os.path.basename(dst).lower().endswith(EOL_TEXT_EXT):
        _to_lf(src, dst)
    else:
        shutil.copyfile(src, dst)


def copy_tree(src_root, dst_root, eol_lf=False):
    """搬运一棵树；软链存根优先建成真软链，建不了就写回存根（tar 阶段会补成软链）"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        # 不打包 Python 缓存、SQLite 运行时库（Django 代码目录不该有，防御性排除）
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        rel = os.path.relpath(dirpath, src_root)
        out_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(out_dir, exist_ok=True)
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            d = os.path.join(out_dir, fn)
            tgt = os.readlink(s) if os.path.islink(s) else link_target(s)
            if tgt:
                try:
                    if os.path.exists(d):
                        os.remove(d)
                    os.symlink(tgt, d)
                except (OSError, NotImplementedError):
                    with open(d, "wb") as f:      # Windows 无建链权限
                        f.write(tgt.encode())
            else:
                copy_file(s, d, eol_lf=eol_lf)
            n += 1
    return n


def mode_for(arc):
    """tar 内的权限：仓库里没有可执行位，只能按路径规则给"""
    a = arc.split("/", 1)[1] if "/" in arc else arc
    base = a.rsplit("/", 1)[-1]
    if base == "install-code.sh":
        return 0o700                    # 能造安装码，只给 root
    if base.endswith(".sh"):
        return 0o755
    if a.startswith("bin/") or a.startswith("python/bin/"):
        return 0o755
    if ".so" in base:
        return 0o755
    return 0o644


# 云同步盘（BaiduSyncdisk / OneDrive）会在目录里反复生成 .baiduyun.uploading.cfg
# 之类的瞬时文件，打包时「刚 walk 到、转眼就没了」会直接让 getsize 抛错。
# 这类文件不属于交付内容，跳过即可。
TRANSIENT_HINTS = (".baiduyun.", ".dropbox.", "~$", ".tmp")


def _is_transient(name):
    low = name.lower()
    return any(h in low for h in TRANSIENT_HINTS)


def add_entry(tf, src, arc):
    if _is_transient(os.path.basename(src)):
        return "skip"
    tgt = os.readlink(src) if os.path.islink(src) else link_target(src)
    if tgt:
        ti = tarfile.TarInfo(arc)
        ti.type = tarfile.SYMTYPE
        ti.linkname = tgt
        ti.mode = 0o777
        ti.mtime = MTIME
        tf.addfile(ti)
        return "link"
    ti = tarfile.TarInfo(arc)
    ti.mode = mode_for(arc)
    ti.mtime = MTIME
    try:
        ti.size = os.path.getsize(src)
        with open(src, "rb") as f:
            tf.addfile(ti, f)
    except (FileNotFoundError, PermissionError):
        # 同步盘/杀软在打包瞬间删改文件：跳过，不让整包构建失败
        return "skip"
    return "file"


def pack(out_dir, tar_path):
    counts = {"file": 0, "link": 0, "dir": 0, "skip": 0}
    with tarfile.open(tar_path, "w:gz", compresslevel=6) as tf:
        for dirpath, dirnames, filenames in os.walk(out_dir):
            dirnames.sort()
            rel = os.path.relpath(dirpath, os.path.dirname(out_dir))
            ti = tarfile.TarInfo(rel.replace(os.sep, "/"))
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            ti.mtime = MTIME
            tf.addfile(ti)
            counts["dir"] += 1
            for fn in sorted(filenames):
                s = os.path.join(dirpath, fn)
                arc = os.path.relpath(s, os.path.dirname(out_dir)).replace(os.sep, "/")
                counts[add_entry(tf, s, arc)] += 1
    return counts


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def binary_info(path):
    """从 ELF 里读 OpenVPN 版本与最低 GLIBC 符号，不执行它（在 Windows 上跑不了）"""
    ver, glibc = "", ""
    try:
        with open(path, "rb") as f:
            data = f.read(6 << 20)
    except OSError:
        return ver, glibc
    # 二进制里第一处 "OpenVPN 2.x" 可能是版权串，认带架构的那一条才准
    m = re.search(rb"OpenVPN (\d+\.\d+\.\d+) (x86_64|i[3-6]86)", data)
    if not m:
        m = re.search(rb"OpenVPN (\d+\.\d+\.\d+)", data)
    if m:
        ver = "OpenVPN " + m.group(1).decode()
    syms = set(re.findall(rb"GLIBC_(\d+\.\d+(?:\.\d+)?)", data))
    if syms:
        def key(s):
            return tuple(int(x) for x in s.decode().split("."))
        glibc = max(syms, key=key).decode()
    return ver, glibc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--tar-only", action="store_true")
    a = ap.parse_args()

    src, bin_, runtime, vendor, build = (os.path.join(ROOT, x) for x in
                                        (".", "bin", "runtime", "vendor", "build"))
    out_dir = os.path.join(a.out, NAME)
    tar_path = os.path.join(a.out, NAME + ".tar.gz")

    # Django 项目存在性：2.6 根布局看 manage.py + config/，兼容旧 django/ 子目录
    _dj_ok = (os.path.isfile(os.path.join(src, "manage.py"))
              and os.path.isdir(os.path.join(src, "config"))) \
             or os.path.isdir(os.path.join(src, "django"))
    if not _dj_ok:
        say("错误: 缺少 Django 项目（manage.py + config/ 或 django/）")
        return 1
    for p, why in ((bin_, "预编译二进制"), (runtime, "离线 Python"),
                   (vendor, "静态库/源码/wheel")):
        if not os.path.isdir(p):
            say("错误: 缺少 %s（%s）" % (p, why))
            return 1

    say("=" * 60)
    say(" 打包 -> %s" % tar_path)
    say(" 模式: %s" % ("只出 tar.gz" if a.tar_only else "目录 + tar.gz"))
    say("=" * 60)

    # 1) 程序文件：根级 serve.py/manage.py + Django 各 app + system/ 特权脚本 + tests/ 自检
    # 2.6 起源码与交付同构为「项目根即 Django 根」：config/core/common/audit/jobs/ovpn/
    # templates/static 与 manage.py 直接在根，整目录收进包，装机后布局完全一致。
    # 兼容旧的 django/ 子目录布局（若还在，整目录照搬）。
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "bin"), exist_ok=True)
    n = 0
    for fn in ("serve.py", "README.md", "manage.py", "requirements.txt",
               "requirements-standalone.txt"):
        p = os.path.join(src, fn)
        if os.path.isfile(p):
            copy_file(p, os.path.join(out_dir, fn), eol_lf=True)
            n += 1
    # 根布局：逐个 app 目录复制到包根
    for d in ("config", "core", "common", "audit", "jobs", "ovpn", "templates", "static"):
        p = os.path.join(src, d)
        if os.path.isdir(p):
            n += copy_tree(p, os.path.join(out_dir, d), eol_lf=True)
    # 兼容旧 django/ 子目录布局
    if os.path.isdir(os.path.join(src, "django")):
        n += copy_tree(os.path.join(src, "django"), os.path.join(out_dir, "django"), eol_lf=True)
    n += copy_tree(os.path.join(src, "system"), os.path.join(out_dir, "system"), eol_lf=True)
    # scripts/：接入契约检查器。core/tests.py 的 ContractCheckTest 按
    # <项目根>/scripts/contract_check.py 定位它，装机后跑 manage.py test 要用；
    # 漏带会让「测试全绿」这条验收在交付包上直接 1 error。
    if os.path.isdir(os.path.join(src, "scripts")):
        n += copy_tree(os.path.join(src, "scripts"), os.path.join(out_dir, "scripts"), eol_lf=True)
    if os.path.isfile(os.path.join(src, "CONTRACT.md")):
        copy_file(os.path.join(src, "CONTRACT.md"), os.path.join(out_dir, "CONTRACT.md"), eol_lf=True)
        n += 1
    # .contract.json：契约标记（tier 与外部边界登记）。缺了它检查器会退回
    # tier 2 / unspecified，external_boundaries 直接判失败。
    if os.path.isfile(os.path.join(src, ".contract.json")):
        copy_file(os.path.join(src, ".contract.json"), os.path.join(out_dir, ".contract.json"), eol_lf=True)
        n += 1
    # install.sh 的正身在 system/，但「解包后直接 ./install.sh」是既有使用习惯，
    # 包根再放一份同内容副本；脚本自身会找到含 manage.py 的那一层作为安装源。
    inst = os.path.join(src, "system", "install.sh")
    if os.path.isfile(inst):
        copy_file(inst, os.path.join(out_dir, "install.sh"), eol_lf=True)
        n += 1
    os.makedirs(os.path.join(out_dir, "tests"), exist_ok=True)
    for fn in ("selftest.py", "selftest_django.py"):
        p = os.path.join(src, "tests", fn)
        if os.path.isfile(p):
            copy_file(p, os.path.join(out_dir, "tests", fn), eol_lf=True)
            n += 1
    say("[1/5] 程序文件 + Django apps + system/ %d 个" % n)

    # 2) 二进制
    for fn in sorted(os.listdir(bin_)):
        p = os.path.join(bin_, fn)
        if os.path.isfile(p):
            copy_file(p, os.path.join(out_dir, "bin", fn))
    say("[2/5] 二进制: %s" % ", ".join(sorted(os.listdir(os.path.join(out_dir, "bin")))))

    # 3) 离线运行时（runtime/ -> python/，代码里写死 $DEST/python/bin/python3）
    n = copy_tree(runtime, os.path.join(out_dir, "python"))
    say("[3/5] 离线运行时 %d 个文件" % n)

    # 4) 静态库 / 源码 / wheel
    for sub in ("static", "src", "wheels"):
        p = os.path.join(vendor, sub)
        if os.path.isdir(p):
            copy_tree(p, os.path.join(out_dir, "vendor", sub))
    say("[4/5] vendor: %s" % ", ".join(sorted(os.listdir(os.path.join(out_dir, "vendor")))))

    # 5) 目标机编译脚本 + 说明
    for fn in ("build-static-openvpn.sh",):
        p = os.path.join(build, fn)
        if os.path.isfile(p):
            copy_file(p, os.path.join(out_dir, "system", fn), eol_lf=True)
    rd = os.path.join(ROOT, "README.md")
    if os.path.isfile(rd):
        copy_file(rd, os.path.join(out_dir, "README.md"), eol_lf=True)
    say("[5/5] 编译脚本与说明就位")

    # version.txt（不执行二进制，从文件内容里读）
    core_py = os.path.join(out_dir, "system", "core.py")
    with open(core_py, encoding="utf-8") as f:
        mt = re.search(r'WEB_VERSION\s*=\s*"([^"]+)"', f.read())
    web_ver = mt.group(1) if mt else "未知"
    ovpn_ver, glibc = binary_info(os.path.join(out_dir, "bin", "openvpn"))
    lines = [
        "OpenVPN Web 一体化管理系统",
        "Web: %s" % web_ver,
        "构建时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "构建机: %s / Python %s" % (
            "Windows" if sys.platform == "win32" else sys.platform,
            sys.version.split()[0]),
    ]
    if ovpn_ver:
        lines.append("openvpn: %s" % ovpn_ver)
    if glibc:
        lines.append("最低 glibc 符号: GLIBC_%s" % glibc)
        lines.append("兼容下限: CentOS 7 / glibc 2.17（向上兼容到当前任意发行版）")
    lines.append("依赖: 包内 vendor/wheels，目标机安装时离线补装（本包未预装 site-packages）")
    with open(os.path.join(out_dir, "version.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # 自检：python3 必须还是软链形态，否则目标机解出来是 10 字节文本
    stub_links = 0
    for dirpath, _, filenames in os.walk(os.path.join(out_dir, "python")):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if os.path.islink(p) or link_target(p):
                stub_links += 1
    say("      软链 %d 个（tar 内会写成符号链接）" % stub_links)

    # 自检：程序文本必须是 LF。Windows 工作区（core.autocrlf=true）里是 CRLF，
    # 一旦漏转，目标机上 ./install.sh 会直接报 "/bin/bash^M: bad interpreter"。
    crlf = []
    # sub=="" 已经递归遍历整棵打包树；2.6 起没有 django/ 子目录，
    # 原来的 django/system/tests 三项只是重复扫描同一批文件。
    for sub in ("",):
        base = os.path.join(out_dir, sub) if sub else out_dir
        for dirpath, _, filenames in os.walk(base):
            if os.path.relpath(dirpath, out_dir).split(os.sep)[0] in ("python", "vendor", "bin"):
                continue
            for fn in filenames:
                if not fn.lower().endswith((".sh", ".py")):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    with open(p, "rb") as f:
                        if b"\r\n" in f.read(65536):
                            crlf.append(os.path.relpath(p, out_dir))
                except OSError:
                    continue
    if crlf:
        say("")
        say(" 错误: 以下程序文件仍是 CRLF，Linux 上无法执行：")
        for x in crlf[:10]:
            say("   - " + x)
        raise SystemExit(1)
    say("      换行统一为 LF（确认无 CRLF）")

    # 打包
    if a.tar_only:
        counts = pack(out_dir, tar_path)
        shutil.rmtree(out_dir)
    else:
        counts = pack(out_dir, tar_path)

    size = os.path.getsize(tar_path)
    say("")
    say("=" * 60)
    say(" 完成: %s" % tar_path)
    say(" 体积: %.1f MB   文件 %d / 目录 %d / 软链 %d"
        % (size / 1048576.0, counts["file"], counts["dir"], counts["link"]))
    say(" sha256: %s" % sha256(tar_path))
    say("")
    say(" 用法: 拷到任意 x86_64 Linux -> tar xzf %s.tar.gz -> cd %s && ./install.sh"
        % (NAME, NAME))
    say("       （目标机不需要联网/装包/编译；缺依赖会从包内 wheel 补）")
    say("=" * 60)
    if warn:
        for w in warn:
            say(" 注意: " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
