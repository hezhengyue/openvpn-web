#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
备份 / 回滚 / 迁移（快照与全备份已合并为一套）。

一个 tar.gz 既是本机的回滚点，也是可下载的迁移包：

  openvpn/  纯 OpenVPN 原生材料（CA、证书、ta.key、server.conf、company.ovpn、
            ccd/、openvpn.bin）。就算不用这个 Web 系统，拷到任何 OpenVPN
            服务器上手工改好路径也能跑。
  web/      Web 自身状态（SQLite 台账/授权/审计/配置、会话签名密钥、初始管理密码）。

来源标记：系统自动打点（改配置、升级前等）生成 origin=auto 的包，手动点
【立即备份】生成 origin=manual 的包。两者同目录、同格式，都能一键恢复和下载，
只是来源不同。老版本 data/snapshots/ 下的目录快照仍可恢复（core 里的兼容分支）。

安全：包内含 CA 私钥与 tls-crypt 密钥，等同于整个 VPN 的身份，必须当机密文件保管。
"""
import os
import io
import json
import time
import shutil
import tarfile
import tempfile
import hashlib
import sqlite3
import secrets
from datetime import datetime

import core

KIND = "openvpn-web-full-backup"
FORMAT = 1
FULL = os.path.join(core.DATA, "full-backups")
OVPN_FILES = ("ca.crt", "ca.key", "ca.srl", "server.crt", "server.key",
              "ta.key", "server.conf", "company.ovpn", "dh.pem")
WEB_FILES = ("web.secret", "admin_password.txt")


def _ensure():
    os.makedirs(FULL, exist_ok=True)
    os.makedirs(core.STAGING, exist_ok=True)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_text(m):
    vp = m.get("vpn_cidr") or ""
    return """OpenVPN Web 一体化管理系统 · 全备份包
========================================
Web 版本   : {web}
生成时间   : {ts}
标签       : {tag}
来源       : {origin}（auto=系统自动打点 / manual=手动备份）
VPN 网段   : {vp}
配置指纹   : {fp}
包格式     : {kind} v{fmt}

这个包分成两块， purpose 不同：

[ openvpn/ ]  不依赖 Web 系统，可以直接用 OpenVPN 恢复
  ca.crt / ca.key / server.crt / server.key / ta.key
  server.conf / company.ovpn / openvpn.bin / ccd/

  手工恢复：
  1. 把 openvpn/ 下的文件拷到目标机的 OpenVPN 数据目录（本项目是
     /opt/openvpn-web/data/）；ccd/ 整个目录覆盖。
  2. server.conf 里写的是生成时的绝对路径，换目录时用文本编辑器改一下
     ca/cert/key/tls-crypt/client-config-dir 等路径。
  3. 启动：openvpn --config server.conf
  4. 客户端用 company.ovpn 连接（全员一份，不含个人密码）。

[ web/ ]      Web 自身状态，装好 Web 后一键恢复
  openvpn-web.db  SQLite 数据库（账号、授权、白名单、审计、配置）
  web.secret      会话签名密钥（恢复后已登录的 Cookie 不会失效）
  admin_password.txt  初始管理密码（真正生效的密码哈希在数据库里）
  meta.json       人可读的配置摘要

  一键恢复（推荐）：
  - 同一台机器：后台「版本与更新 → 全备份与迁移 → 上传备份包 → 确认恢复」
  - 新机器：先跑 install.sh 装好同版本 Web，然后
        python3 app.py --restore-full 本包路径
        systemctl restart openvpn-web
    恢复程序会用目标机的路径重新生成 server.conf / company.ovpn，
    所以目录变了也不用手改。

【重要】本包含 CA 私钥与 tls-crypt 静态密钥，拿到它就能签发客户端、接入 VPN。
请加密保存，不要放公网网盘或发给无关人员。
""".format(web=m.get("web_version", ""), ts=m.get("created_at", ""),
           tag=m.get("tag", ""), vp=vp, fp=m.get("fingerprint", ""),
           origin="自动" if m.get("origin") == "auto" else "手动",
           kind=KIND, fmt=FORMAT)


def create(tag="手动", keep=10, kind="manual"):
    """生成备份包，返回 {path,name,size,manifest}。keep=0 表示不清理旧包。

    kind=auto 表示系统自动打点，manual 表示用户手工触发；格式完全一致。
    """
    _ensure()
    tmp = tempfile.mkdtemp(dir=core.STAGING)
    try:
        ov = os.path.join(tmp, "openvpn")
        web = os.path.join(tmp, "web")
        os.makedirs(ov)
        os.makedirs(web)

        # ---- 第一块：纯 OpenVPN 材料 ----
        for name in OVPN_FILES:
            p = os.path.join(core.DATA, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(ov, name))
        shutil.copytree(core.CCD, os.path.join(ov, "ccd"), dirs_exist_ok=True)
        # 二进制也进包：以前 core.snapshot() 单独存一份 openvpn.bin 用于升级失败回滚，
        # 两套合并后必须保留这份能力，否则「编译失败自动回滚」会退回旧配置却留着新二进制。
        if os.path.isfile(core.BIN):
            shutil.copy2(core.BIN, os.path.join(ov, "openvpn.bin"))

        # ---- 第二块：Web 状态 ----
        # 数据库走 SQLite 在线备份：WAL 下直接 cp .db 会漏掉还在 -wal 里的事务
        dbtmp = os.path.join(web, "openvpn-web.db")
        src = core.db()
        dst = sqlite3.connect(dbtmp)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()
        for name in WEB_FILES:
            p = os.path.join(core.DATA, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(web, name))
        con = sqlite3.connect(dbtmp)
        meta = {r[0]: r[1] for r in con.execute("SELECT k,v FROM meta")}
        con.close()
        with open(os.path.join(web, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"web_version": core.WEB_VERSION, "created_at": core.now(),
                       "meta": meta}, f, ensure_ascii=False, indent=2)

        # ---- 清单：每个文件都记 sha256，恢复时逐个校验 ----
        files = {}
        for block, base in (("openvpn", ov), ("web", web)):
            files[block] = {}
            for root, _dirs, names in os.walk(base):
                for fn in names:
                    p = os.path.join(root, fn)
                    rel = os.path.relpath(p, base).replace(os.sep, "/")
                    files[block][rel] = _sha256(p)
        m = {
            "kind": KIND, "format": FORMAT, "web_version": core.WEB_VERSION,
            "created_at": core.now(), "tag": str(tag or "手动")[:32],
            "origin": "auto" if kind == "auto" else "manual",
            "vpn_cidr": core.cfg().get("vpn_cidr", ""),
            "fingerprint": core.get_meta("config_fingerprint", ""),
            "files": files,
        }
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp, "恢复说明.txt"), "w", encoding="utf-8") as f:
            f.write(_manifest_text(m))

        safe_tag = "".join(ch for ch in m["tag"] if ch.isalnum() or ch in "-_")[:16] or "manual"
        name = "openvpn-web-full-%s-%s.tar.gz" % (
            datetime.now().strftime("%Y%m%d-%H%M%S"), safe_tag)
        path = os.path.join(FULL, name)
        with tarfile.open(path, "w:gz") as tf:
            tf.add(os.path.join(tmp, "manifest.json"), arcname="manifest.json")
            tf.add(os.path.join(tmp, "恢复说明.txt"), arcname="恢复说明.txt")
            tf.add(ov, arcname="openvpn")
            tf.add(web, arcname="web")
        if keep:
            old = sorted((x for x in os.listdir(FULL)
                          if x.endswith(".tar.gz") and x != name), reverse=True)
            for x in old[max(0, keep - 1):]:
                try:
                    os.remove(os.path.join(FULL, x))
                except Exception:
                    pass
        return {"path": path, "name": name, "size": os.path.getsize(path), "manifest": m}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _safe_member(name):
    """tar 里的路径必须规整，挡住 ../../etc/passwd 这类路径穿越"""
    return (name and not name.startswith("/") and not name.startswith("\\")
            and ".." not in name.replace("\\", "/").split("/")
            and (name in ("manifest.json", "恢复说明.txt", "openvpn", "web")
                 or name.startswith("openvpn/") or name.startswith("web/")))


def preview(path):
    """只读校验并给出摘要，不落地任何文件"""
    if not os.path.isfile(path):
        raise ValueError("备份包不存在")
    try:
        with tarfile.open(path, "r:gz") as tf:
            names = tf.getnames()
            bad = [n for n in names if not _safe_member(n)]
            if bad:
                raise ValueError("备份包含非法路径，已拒绝: %s" % bad[0])
            f = tf.extractfile("manifest.json")
            if not f:
                raise ValueError("不是全备份包：缺少 manifest.json")
            m = json.loads(f.read().decode("utf-8"))
    except (tarfile.TarError, UnicodeDecodeError, ValueError) as e:
        if str(e).startswith("备份"):
            raise
        raise ValueError("不是有效的全备份包：%s" % e)
    if m.get("kind") != KIND or m.get("format") != FORMAT:
        raise ValueError("备份包版本不匹配（kind=%s format=%s）"
                         % (m.get("kind"), m.get("format")))
    n_ov = len(m.get("files", {}).get("openvpn", {}))
    n_web = len(m.get("files", {}).get("web", {}))
    if n_ov < 3 or n_web < 1:
        raise ValueError("备份包内容不完整（openvpn %d 个文件 / web %d 个文件）" % (n_ov, n_web))
    return {"manifest": m, "openvpn_files": n_ov, "web_files": n_web,
            "origin": m.get("origin", "manual"),
            "size": os.path.getsize(path), "name": os.path.basename(path)}


def restore(path, audit=True, protect=True):
    """恢复一份备份包。protect=True 时动数据前先自动打一份保护备份。"""
    info = preview(path)
    m = info["manifest"]
    snap = core.snapshot("备份恢复前") if protect else ""
    tmp = tempfile.mkdtemp(dir=core.STAGING)
    try:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not _safe_member(member.name):
                    continue
                out = os.path.join(tmp, member.name)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                f = tf.extractfile(member)
                if not f:
                    continue
                with open(out, "wb") as w:
                    shutil.copyfileobj(f, w)

        # 逐个校验 sha256：包坏了 / 被改过就整包拒绝，绝不写一半
        for block in ("openvpn", "web"):
            base = os.path.join(tmp, block)
            for rel, sha in (m.get("files", {}).get(block) or {}).items():
                p = os.path.join(base, rel)
                if not os.path.isfile(p) or _sha256(p) != sha:
                    raise ValueError("备份包校验失败：%s/%s" % (block, rel))

        # ---- 恢复 openvpn/：证书 + 配置 + ccd ----
        ov = os.path.join(tmp, "openvpn")
        for rel in sorted(m["files"]["openvpn"]):
            src = os.path.join(ov, rel)
            if rel.startswith("ccd/") or rel == "openvpn.bin":
                continue
            shutil.copy2(src, os.path.join(core.DATA, rel))
        # 二进制单独放回 ROOT/bin（老版本包没有这一项，跳过即可）
        binfile = os.path.join(ov, "openvpn.bin")
        if os.path.isfile(binfile):
            os.makedirs(os.path.dirname(core.BIN), exist_ok=True)
            shutil.copy2(binfile, core.BIN)
        # ccd 整个目录替换，不能留目标机上多出来的旧账号
        for fn in os.listdir(core.CCD):
            p = os.path.join(core.CCD, fn)
            if os.path.isfile(p):
                os.remove(p)
        srcccd = os.path.join(ov, "ccd")
        if os.path.isdir(srcccd):
            for fn in os.listdir(srcccd):
                shutil.copy2(os.path.join(srcccd, fn), os.path.join(core.CCD, fn))

        # ---- 恢复 web/：数据库在线写回，不动当前审计（否则这次操作自己就没了） ----
        web = os.path.join(tmp, "web")
        dbfile = os.path.join(web, "openvpn-web.db")
        if os.path.isfile(dbfile):
            con = core.db()
            audit_rows = [tuple(r) for r in con.execute(
                "SELECT id,ts,actor,action,detail,ip FROM audit")]
            con.close()
            src = sqlite3.connect(dbfile)
            tgt = core.db()
            try:
                with tgt:
                    src.backup(tgt)
            finally:
                src.close()
                tgt.close()
            con = core.db()
            con.executemany("INSERT OR REPLACE INTO audit(id,ts,actor,action,detail,ip)"
                            " VALUES(?,?,?,?,?,?)", audit_rows)
            con.commit()
            con.close()
        for name in WEB_FILES:
            p = os.path.join(web, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(core.DATA, name))

        # 按目标机的 ROOT 重新生成 server.conf / company.ovpn，路径自动适配
        core.write_all_config()
        if audit:
            core.log_audit("admin", "backup.full_restore", info["name"])
        return {"name": info["name"], "snapshot": snap,
                "openvpn_files": info["openvpn_files"], "web_files": info["web_files"],
                "web_version": m.get("web_version", "")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_items():
    _ensure()
    out = []
    for fn in sorted(os.listdir(FULL), reverse=True):
        if not fn.endswith(".tar.gz"):
            continue
        p = os.path.join(FULL, fn)
        try:
            m = preview(p)["manifest"]
            out.append({"name": fn, "size": os.path.getsize(p),
                        "created_at": m.get("created_at", ""), "tag": m.get("tag", ""),
                        "origin": m.get("origin", "manual"),
                        "web_version": m.get("web_version", ""),
                        "fingerprint": m.get("fingerprint", "")})
        except Exception:
            out.append({"name": fn, "size": os.path.getsize(p),
                        "created_at": "", "tag": "(无法读取)", "origin": "manual",
                        "web_version": "", "fingerprint": ""})
    return out


def local_path(name):
    """取服务器上已有备份包的路径，供「恢复本机备份」接口用，挡路径穿越"""
    if not name or os.path.basename(name) != name or not name.endswith(".tar.gz"):
        raise ValueError("备份包名非法")
    p = os.path.join(FULL, name)
    if not os.path.isfile(p):
        raise ValueError("备份包不存在")
    return p


def delete(name):
    _ensure()
    if os.path.basename(name) != name or not name.endswith(".tar.gz"):
        raise ValueError("备份包名非法")
    p = os.path.join(FULL, name)
    if os.path.isfile(p):
        os.remove(p)
    return True


def stage(content, filename=""):
    """上传暂存在 staging，等前端确认后再恢复；和 Excel 预览是同一套思路"""
    _ensure()
    token = secrets.token_hex(16)
    p = os.path.join(core.STAGING, "full-" + token + ".tar.gz")
    with open(p, "wb") as f:
        f.write(content)
    # 只保留最近 8 个暂存包
    olds = sorted((x for x in os.listdir(core.STAGING) if x.startswith("full-")), reverse=True)
    for x in olds[8:]:
        try:
            os.remove(os.path.join(core.STAGING, x))
        except Exception:
            pass
    r = preview(p)
    if filename:
        r["name"] = os.path.basename(filename)
    return {"token": token, "preview": r}


def staged_path(token):
    if not re_safe_token(token):
        raise ValueError("令牌非法")
    p = os.path.join(core.STAGING, "full-" + token + ".tar.gz")
    if not os.path.isfile(p):
        raise ValueError("暂存备份已过期，请重新上传")
    return p


def re_safe_token(token):
    return bool(token) and len(token) == 32 and all(c in "0123456789abcdef" for c in token)
