# system/tests/test_fullbackup_restore.py
"""特权层备份恢复的单测：只回灌业务表，底座运行态一律保留。

放在 system/ 下是因为契约检查器禁止源码根（Web 进程代码）出现裸 SQL，
而这些夹具必须用 sqlite3 构造两张结构不同的库来验证跨版本恢复。
"""
import io
import os
import json
import sqlite3
import tarfile
import tempfile
import unittest

from system import fullbackup


class RestoreBusinessTablesTest(unittest.TestCase):
    def test_base_and_runtime_tables_survive_restore(self):
        src = sqlite3.connect(":memory:")
        tgt = sqlite3.connect(":memory:")
        try:
            for con in (src, tgt):
                con.execute("CREATE TABLE users (username TEXT)")
                con.execute("CREATE TABLE meta (k TEXT, v TEXT)")
                con.execute("CREATE TABLE core_user (username TEXT)")
                con.execute("CREATE TABLE audit_auditlog (id INTEGER)")
                con.execute("CREATE TABLE tasks (id INTEGER)")
            src.execute("INSERT INTO users VALUES ('from-backup')")
            src.execute("INSERT INTO core_user VALUES ('backup-admin')")
            src.execute("INSERT INTO audit_auditlog VALUES (1)")
            src.execute("INSERT INTO tasks VALUES (99)")
            src.commit()
            tgt.execute("INSERT INTO users VALUES ('live')")
            tgt.execute("INSERT INTO core_user VALUES ('live-admin')")
            tgt.execute("INSERT INTO audit_auditlog VALUES (2)")
            tgt.execute("INSERT INTO tasks VALUES (7)")
            tgt.commit()

            fullbackup._restore_business_tables(src, tgt)

            self.assertEqual(tgt.execute("SELECT username FROM users").fetchone()[0],
                             "from-backup")
            self.assertEqual(tgt.execute("SELECT username FROM core_user").fetchone()[0],
                             "live-admin")
            self.assertEqual(tgt.execute("SELECT id FROM audit_auditlog").fetchone()[0], 2)
            self.assertEqual(tgt.execute("SELECT id FROM tasks").fetchone()[0], 7)
        finally:
            src.close()
            tgt.close()

    def test_old_backup_missing_new_required_column_is_rejected(self):
        """目标库多出的必填列在备份包里不存在 → 整体拒绝，不能半恢复。"""
        src = sqlite3.connect(":memory:")
        tgt = sqlite3.connect(":memory:")
        try:
            src.execute("CREATE TABLE users (username TEXT)")
            tgt.execute("CREATE TABLE users (username TEXT, new_required TEXT NOT NULL)")
            src.execute("INSERT INTO users VALUES ('alice')")
            src.commit()
            with self.assertRaises(ValueError):
                fullbackup._restore_business_tables(src, tgt)
        finally:
            src.close()
            tgt.close()


class ManifestPathTest(unittest.TestCase):
    """恢复时真正决定目标路径的是 manifest 的 rel，不是 tar 成员名。"""

    def test_safe_rel_only_allows_known_files(self):
        for block, rel in (
            ("openvpn", "../../../etc/sudoers.d/pwned"),
            ("openvpn", "ccd/../server.conf"),
            ("openvpn", "ccd/x;touch /tmp/pwned"),
            ("openvpn", "/etc/cron.d/pwned"),
            ("web", "../web.secret"),
            ("web", "evil.sh"),
            ("web", ""),
        ):
            with self.subTest(block=block, rel=rel), self.assertRaises(ValueError):
                fullbackup._safe_rel(block, rel)
        self.assertEqual(fullbackup._safe_rel("openvpn", "ccd/alice"), "ccd/alice")
        self.assertEqual(fullbackup._safe_rel("web", "web.secret"), "web.secret")

    def test_preview_rejects_package_with_traversal_in_manifest(self):
        """恶意 manifest 必须在恢复落地任何文件之前整包拒绝。"""
        manifest = {
            "kind": fullbackup.KIND,
            "format": fullbackup.FORMAT,
            "files": {
                "openvpn": {
                    "ca.crt": "0" * 64, "server.conf": "0" * 64, "ccd/alice": "0" * 64,
                    "../../../etc/sudoers.d/pwned": "0" * 64,
                },
                "web": {"openvpn-web.db": "0" * 64},
            },
        }
        raw = json.dumps(manifest).encode("utf-8")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "evil.tar.gz")
            with tarfile.open(p, "w:gz") as tf:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(raw)
                tf.addfile(info, io.BytesIO(raw))
            with self.assertRaises(ValueError) as ctx:
                fullbackup.preview(p)
            self.assertIn("非法文件路径", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
