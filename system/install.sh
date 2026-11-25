#!/bin/bash
# OpenVPN Web 一体化管理系统 —— 一键安装（在目标机上以 root 执行，全程离线）
#
#   ./install.sh                          # 装到 /opt/openvpn-web，默认 1194/tcp + NAT
#   ./install.sh /srv/ovpn                # 自定义安装目录
#   ./install.sh -- --public-addr 1.2.3.4 --port 1194 --lan 10.10.0.0/16
#   ./install.sh -- --user alice:Passw0rd # 顺手建一个账号
#   ./install.sh --with-build-tools       # 顺带装 gcc/make/perl/pkgconfig（以后能在本机编译 openvpn）
#   ./install.sh --purge                  # 卸载：停服务、拆 iptables 链、删目录/账号/规则（data 先备份到 /root）
#
# 它把「Web + OpenVPN 服务端 + CA + 证书 + ACL/NAT + systemd」一次装完，
# 结束时浏览器打开就能用，不需要再点安装向导。
#
# 需要联网吗：不需要。python 运行时、Django/openpyxl wheel、静态 openvpn 全在包内。
set -euo pipefail

DEST="/opt/openvpn-web"
SETUP_ARGS=()
PURGE=0
WITH_TOOLS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --) shift; SETUP_ARGS=("$@"); break ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        --purge) PURGE=1; shift ;;
        --with-build-tools) WITH_TOOLS=1; shift ;;
        *) DEST="$1"; shift ;;
    esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
# 双位置兼容：包根的 ./install.sh 与 system/install.sh 是同一份内容，
# 但「安装源」必须是含 Django 项目（django/manage.py + config/）的那一层。脚本万一
# 被从 system/ 里直接调用，就自动上跳一级，避免把 system/ 当成整个包来铺。
# 3.0 起 Django 源码在 django/ 子目录；兼容历史平铺布局（manage.py 在根）。
if [ -f "$SRC/django/manage.py" ]; then
    SRC="$SRC"
elif [ -f "$SRC/../django/manage.py" ]; then
    SRC="$(cd "$SRC/.." && pwd)"
elif [ ! -f "$SRC/manage.py" ] && [ -f "$SRC/../manage.py" ]; then
    SRC="$(cd "$SRC/.." && pwd)"   # 兼容 2.6 起的平铺布局
fi
UNIT="/etc/systemd/system/openvpn-web.service"
SUDOERS="/etc/sudoers.d/openvpn-web"
USER="ovpnweb"
ETC="${OVPN_WEB_ETC:-/etc/openvpn-web}"
PORT="${OVPN_WEB_PORT:-8800}"
PY=""
# manage.py 位置：3.0 起在安装根的 django/ 下；兼容历史平铺布局。
if [ -f "$DEST/django/manage.py" ]; then
    MGMT="$DEST/django/manage.py"
else
    MGMT="$DEST/manage.py"
fi

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "错误: 请用 root 执行"; exit 1; }

# ---------------------------------------------------------------- 卸载（--purge）
# 「删干净再重装」是运维高频动作（换版本、换机器规格、验证全新安装），
# 所以卸载必须是一等公民：一条命令、幂等、先把台账备份走再删。
purge_all() {
    local IPT l cmd tbl tgt i done_any left bk r
    IPT="$(command -v iptables || echo /usr/sbin/iptables)"
    say "============================================================"
    say " 卸载 OpenVPN Web    安装目录: $DEST"
    say "============================================================"

    step "[1/7] 备份台账（有就备走，不会静默丢数据）"
    if [ -d "$DEST/data" ]; then
        bk="/root/openvpn-web-data-$(date +%Y%m%d-%H%M%S).tar.gz"
        if tar czf "$bk" -C "$DEST" data 2>/dev/null; then
            say "      $bk"
            say "      要恢复：解开后把 data/ 放回 $DEST 再装"
        else
            say "      备份失败，已中止。确认 $DEST/data 里没有要留的东西后再重跑。"
            exit 1
        fi
    else
        say "      没有 $DEST/data，跳过"
    fi

    step "[2/7] 停服务、删 systemd 单元"
    for i in openvpn-web openvpn-web-replay.timer openvpn-web-replay.service; do
        systemctl stop "$i" >/dev/null 2>&1 || true
        systemctl disable "$i" >/dev/null 2>&1 || true
    done
    rm -f /etc/systemd/system/openvpn-web.service \
          /etc/systemd/system/openvpn-web-replay.timer \
          /etc/systemd/system/openvpn-web-replay.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    say "      已删 openvpn-web.service / openvpn-web-replay.*"

    step "[3/7] 结束 openvpn 进程"
    # 不能只按 server.conf 匹配：排障时常见有人手工起过客户端隧道
    # （--config company.ovpn --dev tun1），证书删了它还占着 tun 设备，重装后会顶掉新隧道。
    # 按二进制路径匹配，只动本系统起的实例，不误伤系统里别的 openvpn。
    pkill -f "$DEST/bin/openvpn" >/dev/null 2>&1 || true
    sleep 1
    pkill -9 -f "$DEST/bin/openvpn" >/dev/null 2>&1 || true
    say "      已结束（残留自查: ps -ef | grep openvpn）"

    step "[4/7] 拆 iptables 自建链（OVPN_ACL / OVPNWEB_NAT）"
    # 顺序很重要：链被 FORWARD / POSTROUTING 引用着，直接 -X 会失败，必须先把引用删掉。
    # 引用里带 VPN 网段，网段可能被改过，所以不猜参数——直接把 -S 输出里的规则改 -A 为 -D 逐条删。
    del_refs() {
        tbl="$1"; tgt="$2"
        for i in 1 2 3; do
            done_any=0
            while IFS= read -r l; do
                case "$l" in
                    "-A "*)
                        cmd="-D ${l#-A }"
                        if [ -n "$tbl" ]; then
                            if $IPT -t "$tbl" $cmd 2>/dev/null; then done_any=1; fi
                        else
                            if $IPT $cmd 2>/dev/null; then done_any=1; fi
                        fi ;;
                esac
            done < <( if [ -n "$tbl" ]; then $IPT -t "$tbl" -S 2>/dev/null; else $IPT -S 2>/dev/null; fi | grep -F -- "-j $tgt" )
            [ "$done_any" = 1 ] || break
        done
    }
    del_refs "" OVPN_ACL
    del_refs nat OVPNWEB_NAT
    $IPT -F OVPN_ACL 2>/dev/null || true
    $IPT -X OVPN_ACL 2>/dev/null || true
    $IPT -t nat -F OVPNWEB_NAT 2>/dev/null || true
    $IPT -t nat -X OVPNWEB_NAT 2>/dev/null || true
    say "      已拆两条链及其引用"

    step "[5/7] 撤防火墙放行"
    if command -v firewall-cmd >/dev/null 2>&1; then
        firewall-cmd --permanent --list-rich-rules 2>/dev/null | grep -E "8800|1194" | while IFS= read -r r; do
            firewall-cmd --permanent --remove-rich-rule="$r" >/dev/null 2>&1 || true
        done || true
        firewall-cmd --permanent --remove-port=8800/tcp >/dev/null 2>&1 || true
        firewall-cmd --remove-port=8800/tcp >/dev/null 2>&1 || true
        # 安装时补的「私网 → tun0」转发放行（见 setup.py firewall() / core.render_rules）
        for _src in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
            firewall-cmd --permanent --direct --remove-rule ipv4 filter FORWARD 0 \
                -s "$_src" -o tun0 -j ACCEPT >/dev/null 2>&1 || true
            firewall-cmd --direct --remove-rule ipv4 filter FORWARD 0 \
                -s "$_src" -o tun0 -j ACCEPT >/dev/null 2>&1 || true
        done
        firewall-cmd --reload >/dev/null 2>&1 || true
        say "      已撤 Web 端口放行与转发放行（VPN 端口如手工加过请自行确认）"
    else
        say "      无 firewalld，跳过"
    fi

    step "[6/7] 删目录、sudoers、sysctl、安装码、运行账号"
    rm -rf "$DEST"
    rm -f /etc/sudoers.d/openvpn-web /etc/sysctl.d/99-openvpn-web.conf
    rm -rf "$ETC"
    userdel ovpnweb >/dev/null 2>&1 || true
    say "      已删 $DEST / sudoers / sysctl 配置 / 安装码 / ovpnweb 账号"

    step "[7/7] 残留自查"
    left=0
    if systemctl list-unit-files 2>/dev/null | grep -q openvpn-web; then
        say "      仍有 unit：systemctl list-unit-files | grep openvpn-web"; left=1
    fi
    if $IPT -S 2>/dev/null | grep -q OVPN_ACL; then say "      仍有 OVPN_ACL 规则"; left=1; fi
    if $IPT -t nat -S 2>/dev/null | grep -q OVPNWEB_NAT; then say "      仍有 OVPNWEB_NAT 规则"; left=1; fi
    if [ -d "$DEST" ]; then say "      目录仍在：$DEST"; left=1; fi
    if [ "$left" = 0 ]; then say "      干净，可以重新安装了"; fi

    say ""
    say "卸载完成。重装：解包后 ./install.sh"
}

if [ "$PURGE" = 1 ]; then
    purge_all
    exit 0
fi

say "============================================================"
say " OpenVPN Web 一体化管理系统 · 一键安装（离线）"
say " 安装目录: $DEST     Web 端口: $PORT"
say "============================================================"

case "$(uname -m)" in
    x86_64|amd64) ;;
    *) say "警告: 包内是 x86_64 静态二进制，当前 $(uname -m) 需要自行编译（build-offline.sh）" ;;
esac

# ---------------------------------------------------------------- 1) 落地文件
step "[1/9] 释放程序文件"
if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$DEST"
    # 不用 rsync（最小化系统可能没有）；data/ 与 .git 不覆盖，保证重装不丢库
    tar -C "$SRC" -cf - --exclude=./data --exclude=./.git --exclude=./.workbuddy . \
        | tar -C "$DEST" -xf -
fi
mkdir -p "$DEST"/{bin,data/{state,ccd,tasks,snapshots,staging,backups}}
chmod 755 "$DEST" "$DEST/bin"

# 包在 Windows/网盘里滚过一圈后，python/bin 下的软链会变成 10 字节的文本文件，
# 可执行位也可能丢，这里统一修一遍（这是踩过的真实坑，不是防御性编程）
prepare_runtime() {
    local d="$1" pair l t
    [ -d "$d/bin" ] || return 0
    chmod +x "$d/bin"/* 2>/dev/null || true
    chmod -R a+rX "$d" 2>/dev/null || true
    for pair in python:python3.12 python3:python3.12 2to3:2to3-3.12 idle3:idle3.12 \
                pydoc3:pydoc3.12 python3-config:python3.12-config; do
        l="${pair%%:*}"; t="${pair##*:}"
        [ -L "$d/bin/$l" ] && continue
        [ -f "$d/bin/$t" ] || continue
        if [ "$(wc -c < "$d/bin/$l" 2>/dev/null || echo 999)" -lt 64 ]; then
            ln -sfn "$t" "$d/bin/$l" && say "      修复软链 bin/$l -> $t"
        fi
    done
    if [ -f "$d/lib/libpython3.12.so.1.0" ] && [ ! -L "$d/lib/libpython3.12.so" ]; then
        ln -sfn libpython3.12.so.1.0 "$d/lib/libpython3.12.so" 2>/dev/null || true
    fi
}
[ -d "$DEST/python" ] && prepare_runtime "$DEST/python"
chmod +x "$DEST"/*.sh "$DEST"/*.py 2>/dev/null || true
[ -f "$DEST/bin/openvpn" ] && chmod 755 "$DEST/bin/openvpn" || true
[ -f "$DEST/bin/openssl" ] && chmod 755 "$DEST/bin/openssl" || true

# ---------------------------------------------------------------- 2) 解释器
# 交付约定：只用随包携带的离线 Python，绝不退回目标机的系统 python3。
# 系统 Python 版本/依赖千差万别（CentOS 7 只有 3.6、最小化系统压根没装），
# 一旦用它，「拷过去就能用」就变成看环境脸色；包里全部备齐才有确定性。
step "[2/9] 选择 Python 运行时（强制随包便携版）"
if [ -x "$DEST/python/bin/python3" ] && "$DEST/python/bin/python3" -V >/dev/null 2>&1; then
    PY="$DEST/python/bin/python3"
    say "      使用包内离线运行时：$("$PY" -V 2>&1)"
    say "      不使用目标机 python3（避免版本/依赖差异导致装完用不了）"
else
    say "错误: 包内 python/bin/python3 不可用（缺失、软链未还原、可执行位丢失或 ABI 不符）。"
    say "      本交付包强制使用随包 Python，不会退回系统 python3。"
    say "      请重新解包（Windows/网盘传输容易把软链变成存根），或重新下载完整交付包。"
    exit 1
fi

# 依赖：包可能是「干净运行时」（在 Windows 上打的包没法执行 Linux 的 pip），
# 依赖全在 vendor/wheels 里，这里离线补齐。目标机不用联网，也不用装 pip 包。
if ! "$PY" -c 'import django, axes, auditlog, import_export, simpleui, whitenoise, waitress, environ, openpyxl' >/dev/null 2>&1; then
    say "      包内运行时缺依赖，从 vendor/wheels 离线补装…"
    if ls "$DEST"/vendor/wheels/*.whl "$DEST"/vendor/wheels/*.tar.gz >/dev/null 2>&1; then
        "$PY" -m pip install --no-index --no-cache-dir --disable-pip-version-check \
              --find-links "$DEST/vendor/wheels" -r "$DEST/requirements-standalone.txt" >/dev/null 2>&1 || true
    fi
    if ! "$PY" -c 'import django, axes, auditlog, import_export, simpleui, whitenoise, waitress, environ, openpyxl' >/dev/null 2>&1; then
        # pip 不可用或解析失败：wheel 本身就是 zip，直接解开也能 import
        _pysp="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
        [ -n "$_pysp" ] || _pysp="$DEST/python/lib/python3.12/site-packages"
        "$PY" - "$DEST/vendor/wheels" "$_pysp" <<'PYEOF'
import glob, os, sys, zipfile, tarfile, tempfile, shutil
wheels, sp = sys.argv[1], sys.argv[2]
os.makedirs(sp, exist_ok=True)
n = 0
# wheel 本质是 zip，直接解到 site-packages
for w in sorted(glob.glob(os.path.join(wheels, "*.whl"))):
    with zipfile.ZipFile(w) as z:
        z.extractall(sp)
    n += 1
# sdist（simpleui 只有源码包）：解开后把顶层纯 Python 包目录搬到 site-packages
for w in sorted(glob.glob(os.path.join(wheels, "*.tar.gz"))):
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(w, "r:gz") as t:
            t.extractall(td)
        for root in os.listdir(td):
            src = os.path.join(td, root)
            if not os.path.isdir(src):
                continue
            # 找到含 simpleui/__init__.py 的那一层
            candidate = os.path.join(src, "simpleui")
            if os.path.isdir(candidate):
                shutil.copytree(candidate, os.path.join(sp, "simpleui"), dirs_exist_ok=True)
                n += 1
                break
print("      直接解包 %d 个依赖到 site-packages" % n)
PYEOF
    fi
    if "$PY" -c 'import django, axes, auditlog, import_export, simpleui, whitenoise, waitress, environ, openpyxl' >/dev/null 2>&1; then
        say "      依赖已就位"
    else
        say "      警告: 依赖仍不完整，Web 可能起不来（检查包内 vendor/wheels）"
    fi
fi

# ---------------------------------------------------------------- 2.5) 开局预检
# 在动 /etc、运行账号、systemd 之前，一次报齐所有环境问题：缺 TUN、glibc 太老、
# 端口被占、磁盘不足、时钟异常、没有 systemd，以及最关键的——交付包本身是否完整。
# 预检只探测、不修改系统；有硬错误直接停手，别装到一半留下半套环境。
say ""
say "==> 安装前环境预检（只探测、不改系统）"
PRECHECK_STATE="未通过"
# 重装时端口多半是本服务自己占着（后面会 restart），只警告不拦；首装才硬检查。
if [ -s "$DEST/data/admin_password.txt" ]; then
    PRECHECK_RC=0
    "$PY" "$DEST/system/precheck.py" --root "$DEST" --port "$PORT" --reinstall || PRECHECK_RC=$?
else
    PRECHECK_RC=0
    "$PY" "$DEST/system/precheck.py" --root "$DEST" --port "$PORT" || PRECHECK_RC=$?
fi
if [ "$PRECHECK_RC" != 0 ]; then
    say "错误: 安装前预检未通过，已中止（上面一次列齐了所有问题，修完直接重跑本脚本）"
    exit 1
fi
PRECHECK_STATE="通过（详见上方预检明细）"
say ""

# ---------------------------------------------------------------- 3) 用户与权限
step "[3/9] 创建运行账号与目录权限"
if ! id "$USER" >/dev/null 2>&1; then
    useradd -r -s /sbin/nologin -d "$DEST" "$USER" 2>/dev/null || \
    useradd -r -s /usr/sbin/nologin -d "$DEST" "$USER"
fi
# -h：只改条目本身，不跟随软链。data/ 归 Web 可写，装/升级时若里面已有
# 指向 /etc 的软链，普通 chown -R 会把目标文件一并改成 Web 属主。
find "$DEST/data" -print0 | xargs -0 -r chown -h "$USER:$USER"
# bin/ 不交给 Web 用户：二进制由 root 侧的 worker 落盘，Web 只要有可执行位就够了。
# 万一 Web 进程被打穿，也改不了这个「下次启动会以 root 跑」的程序。
chown -R root:root "$DEST/bin" 2>/dev/null || true
chmod 755 "$DEST/bin" 2>/dev/null || true
chmod 755 "$DEST/bin"/* 2>/dev/null || true
chown root:root "$DEST" 2>/dev/null || true
chmod 755 "$DEST"
# system/ 与 Django 代码目录（django/）同样 root 所有、Web 不可写；只有 data/ 归服务用户。
# 任何「下次会以 root 执行」的代码都不能被 Web 进程改。
chown -R root:root "$DEST/system" 2>/dev/null || true
chmod 644 "$DEST/system"/*.py 2>/dev/null || true
chmod 755 "$DEST/system"/*.sh 2>/dev/null || true
chmod 700 "$DEST/system/install-code.sh" 2>/dev/null || true
# 3.0 起 Django 源码集中在 django/；其余根级目录是历史平铺布局残留（升级场景会有）。
for d in django scripts config core accounts common audit jobs ovpn templates static; do
    [ -d "$DEST/$d" ] || continue
    chown -R root:root "$DEST/$d" 2>/dev/null || true
    find "$DEST/$d" -type f -name '*.py' -exec chmod 644 {} + 2>/dev/null || true
    find "$DEST/$d" -type d -exec chmod 755 {} + 2>/dev/null || true
done
# Django 入口脚本需可执行（systemd 用 $PY 调用，执行位只为运维手工启动）
[ -f "$DEST/django/serve.py" ] && chmod 755 "$DEST/django/serve.py" 2>/dev/null || true
# Django 运行期目录：config/settings.py 在 import 时会尝试 mkdir(STATIC_ROOT/MEDIA_ROOT/static)。
# 项目根必须保持 root 所有（Web 不能改下次会以 root 执行的代码），所以这里预先建好这些
# 子目录并交给服务用户，mkdir(exist_ok=True) 就不会因父目录不可写而 PermissionError。
for d in logs staticfiles media; do
    if command -v install >/dev/null 2>&1; then
        install -d -o "$USER" -g "$USER" -m 750 "$DEST/$d" 2>/dev/null || true
    fi
    [ -d "$DEST/$d" ] || mkdir -p "$DEST/$d"
    chown "$USER:$USER" "$DEST/$d" 2>/dev/null || true
    chmod 750 "$DEST/$d" 2>/dev/null || true
done
# WhiteNoise 从 STATIC_ROOT 提供资源，安装时以 root 收集一次；Web 只需读。
# settings 在生产模式要求非空 SECRET_KEY，这里直接复用 serve.py 会用的同一个文件，
# 没有就先生成一份（与 serve.py 规则一致，之后重启沿用同一把，会话不会失效）。
if [ -f "$MGMT" ]; then
    SKF="$DEST/data/web.secret"
    if [ ! -s "$SKF" ]; then
        "$PY" -c "import secrets,sys;open(sys.argv[1],'w').write(secrets.token_urlsafe(48))" "$SKF" || true
        chown "$USER:$USER" "$SKF" 2>/dev/null || true
        chmod 600 "$SKF" 2>/dev/null || true
    fi
    if ! SECRET_KEY="$(tr -d '\r\n' < "$SKF")" OVPN_WEB_ROOT="$DEST" \
         STANDALONE=True HOSTS="localhost,127.0.0.1" \
         "$PY" "$MGMT" collectstatic --noinput \
         >/dev/null 2>"$DEST/logs/collectstatic.err"; then
        say "      警告: collectstatic 失败，见 $DEST/logs/collectstatic.err"
        tail -3 "$DEST/logs/collectstatic.err" 2>/dev/null | sed 's/^/        /' || true
    fi
fi
# 上面两步 manage.py 是以 root 跑的，会在 logs/ 下生成 root 所有的日志文件；
# 服务是以 ovpnweb 跑的，不交还属主就会 PermissionError（表现为接口 500）。
chown -R "$USER:$USER" "$DEST/logs" "$DEST/staticfiles" "$DEST/media" 2>/dev/null || true
# 安装码脚本是「装外部代码」的唯一人工闸门，只能 root 执行
if [ -f "$DEST/system/install-code.sh" ]; then
    chown root:root "$DEST/system/install-code.sh" && chmod 700 "$DEST/system/install-code.sh"
fi
# 安装码存放目录（600 的令牌文件由 install-code.sh 写）
install -d -m 700 -o root -g root "$ETC" 2>/dev/null || { mkdir -p "$ETC" && chmod 700 "$ETC"; }

# ---------------------------------------------------------------- 4) 提权白名单
step "[4/9] 配置提权白名单（Web 只能执行 worker.py 一个命令）"
cat > "$SUDOERS" <<EOF
# 显式 env_reset：除 env_keep 列出的以外，调用方环境变量一律不带进 root 进程。
# 这样 OVPN_WEB_SELFTEST / OVPN_WEB_MOCK 都不可能经 sudo 到 worker 去豁免安装码闸门。
Defaults:$USER env_reset
# 只保留 ROOT 与运行时数据目录：worker 必须用和 Web ORM 同一个 db.sqlite3，
# 否则设了 DATA_DIR/RUNTIME_DIR 后两边各写一个库。绝不放开 MOCK/SELFTEST。
Defaults:$USER env_keep += "OVPN_WEB_ROOT DATA_DIR RUNTIME_DIR"
# 末尾必须是 " *"：sudoers 里不带通配符的规则要求参数完全一致，
# 而实际调用是 worker.py <32位task_id>，只写 worker.py 会导致 "sudo: a password is required"。
# worker.py 自己校验 task_id 格式，并且动作只能从 data/tasks 里的白名单读取，不给自由参数。
$USER ALL=(root) NOPASSWD: $PY $DEST/system/worker.py *
EOF
chmod 440 "$SUDOERS"
if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$SUDOERS" >/dev/null || { say "错误: sudoers 语法不通过"; exit 1; }
fi

# ---------------------------------------------------------------- 5) systemd
step "[5/9] 写 systemd unit（兼容 CentOS 7 的 systemd 219）"
cat > "$UNIT" <<EOF
[Unit]
Description=OpenVPN Web 一体化管理系统
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$DEST
Environment=OVPN_WEB_ROOT=$DEST
Environment=OVPN_WEB_PORT=$PORT
Environment=DJANGO_SETTINGS_MODULE=config.settings
ExecStart=$PY $DEST/django/serve.py
Restart=always
RestartSec=3
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- 6) 初始密码
step "[6/9] 初始管理密码"
NEWPW=0
if [ ! -s "$DEST/data/admin_password.txt" ]; then
    # 新装统一用默认账户 admin / 默认密码 admin；首次登录由 Web 强制改密，
    # 不再随机生成一串让人抄写（也避免抄错把自己锁在外面）。
    PW="admin"
    printf '%s\n' "$PW" > "$DEST/data/admin_password.txt"
    NEWPW=1
fi
chown "$USER:$USER" "$DEST/data/admin_password.txt"
chmod 600 "$DEST/data/admin_password.txt"
# 台账存的是明文密码，库和备份目录只给服务用户自己
for f in "$DEST"/data/openvpn-web.db "$DEST"/data/openvpn-web.db-wal "$DEST"/data/openvpn-web.db-shm "$DEST"/data/web.secret \
         "$DEST"/data/db.sqlite3 "$DEST"/data/db.sqlite3-wal "$DEST"/data/db.sqlite3-shm; do
    [ -e "$f" ] && { chown "$USER:$USER" "$f" && chmod 600 "$f"; } || true
done
[ -d "$DEST/data/backups" ] && { chown -R "$USER:$USER" "$DEST/data/backups" && chmod 700 "$DEST/data/backups"; } || true
# data 目录里有账号名（ccd/）和明文台账，别让同机其他用户随便列
chmod 750 "$DEST/data" 2>/dev/null || true

if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$DEST" >/dev/null 2>&1 || true
fi
systemctl daemon-reload
systemctl enable openvpn-web >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 7) 二进制
# 装完必须 openvpn + web 都在跑。缺二进制不能「装到一半退出、让用户去网页向导上传」——
# 那时候 Web 还没起，人只能干瞪眼。所以这里自己解决：包内 → 就地编译。
step "[7/9] 准备 OpenVPN 二进制（包内 → 就地编译）"
if [ "$WITH_TOOLS" = 1 ] && [ -x "$DEST/system/prepare-openvpn.sh" ]; then
    say "      顺带补齐编译工具（--with-build-tools）"
    bash "$DEST/system/prepare-openvpn.sh" --tools-only "$DEST" || \
        say "      编译工具没装成（可能离线）；不影响已有二进制，只是以后不能在本机编"
fi
BIN_OK=0
if [ -x "$DEST/system/prepare-openvpn.sh" ]; then
    if bash "$DEST/system/prepare-openvpn.sh" "$DEST"; then BIN_OK=1; fi
elif [ -x "$DEST/bin/openvpn" ] && "$DEST/bin/openvpn" --version >/dev/null 2>&1; then
    say "      已就位：$("$DEST/bin/openvpn" --version 2>/dev/null | head -1)"
    BIN_OK=1
else
    say "      缺少 prepare-openvpn.sh，且 bin/openvpn 不可用"
fi

# ---------------------------------------------------------------- 8) 配置服务端
step "[8/9] 配置 OpenVPN 服务端（CA / 配置 / ACL / NAT / 启动）"
VPN_OK=0
if [ "$BIN_OK" = 1 ]; then
    export OVPN_WEB_ROOT="$DEST" OVPN_WEB_PY="$PY" OVPN_WEB_PORT="$PORT" OVPN_WEB_OWNER="$USER"
    if SETUP_OUT="$("$PY" "$DEST/system/setup.py" "${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}")"; then
        printf '%s\n' "$SETUP_OUT"
        VPN_OK=1
    else
        # 配置写了一半也要继续起 Web：人能登录看日志、重试，比停在这里强
        printf '%s\n' "$SETUP_OUT" | tail -20
        say "      错误: 服务端配置失败（上面是最后 20 行，完整日志在 $DEST/data/openvpn.log）"
    fi
else
    say "      跳过：没有可用的 openvpn 二进制（Web 仍会启动，登录后可在「升级」里补）"
fi

# ---------------------------------------------------------------- 8) 起 Web
step "[9/9] 启动 Web 服务"
# 迁移放在起服务之前：这样「迁移失败」会当场报出来，而不是变成 systemd 无限重启。
# --fake-initial 用于接管「旧版 core.py 用裸 SQL 建过表」的库：只标记初始迁移，
# 不重建表、不动数据；全新库上它的行为与普通 migrate 完全一致。
if [ -f "$MGMT" ]; then
    if ! SECRET_KEY="$(tr -d '\r\n' < "$DEST/data/web.secret" 2>/dev/null || echo)" \
         OVPN_WEB_ROOT="$DEST" STANDALONE=True HOSTS="localhost,127.0.0.1" \
         "$PY" "$MGMT" migrate --fake-initial --noinput \
         >/dev/null 2>"$DEST/logs/migrate.err"; then
        say "      错误: 数据库迁移失败，见 $DEST/logs/migrate.err"
        tail -5 "$DEST/logs/migrate.err" 2>/dev/null | sed 's/^/        /' || true
    fi
fi
chown -R "$USER:$USER" "$DEST/logs" 2>/dev/null || true
systemctl restart openvpn-web
# 探活不能只靠 curl：最小化系统常连 curl 都没有，那会误报「Web 没起来」。
# 优先用刚选好的包内 python（urllib 是标准库），没有才退回 curl / wget。
web_ok() {
    if [ -n "$PY" ] && "$PY" -c 'import urllib.request,sys; urllib.request.urlopen("http://127.0.0.1:"+sys.argv[1]+"/api/session", timeout=2).read()' "$PORT" >/dev/null 2>&1; then
        return 0
    fi
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$PORT/api/session" >/dev/null 2>&1; then
        return 0
    fi
    if command -v wget >/dev/null 2>&1 && wget -q -O /dev/null "http://127.0.0.1:$PORT/api/session" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}
ok=0
for _ in $(seq 1 20); do
    sleep 1
    if web_ok; then ok=1; break; fi
done
[ "$ok" = 1 ] || { say "错误: Web 未响应，执行 journalctl -u openvpn-web -n 50 查看原因"; exit 1; }

# 交付即验收：包里自带 selftest.py，装完立刻跑一遍，结果直接打给装机的人。
# 它自己把 OVPN_WEB_ROOT 指到临时目录并开 MOCK，不会碰生产 data/。
say ""
say "==> 装完自检（包内 selftest.py，只用临时目录，不碰生产数据）"
SELFTEST_RC=0
SELFTEST_OUT="$("$PY" "$DEST/tests/selftest.py" 2>&1)" || SELFTEST_RC=$?
printf '%s\n' "$SELFTEST_OUT" | tail -6
if [ "$SELFTEST_RC" = 0 ]; then
    SELFTEST_STATE="全部通过"
    say "      装完自检全部通过"
else
    SELFTEST_STATE="有失败项（见上方输出）"
    say "      警告: 自检有失败项；服务已在运行，请把上面的输出发给维护者"
    say "      完整输出可重跑：$PY $DEST/tests/selftest.py"
fi
say ""

# 安装尾声：本机能否源码编译 OpenVPN，提前查一遍并落库，避免日后到「升级」页才发现缺 gcc。
# 这只是能力探测，不改任何配置或二进制；Web 启动时也会复查并写同样的缓存。
BUILD_ENV="$("$PY" - <<'PYEOF' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ.get("OVPN_WEB_ROOT", "/opt/openvpn-web"))
from system import core
e = core.check_build_env(force=True)
if e["ok"]:
    print("可用 · gcc / make / pkg-config 齐备")
else:
    print("不完整 · 缺 " + "、".join(e["missing"]) + "（不影响现有服务；可在构建机编好二进制后替换）")
PYEOF
)"
[ -n "$BUILD_ENV" ] || BUILD_ENV="未探测到（可稍后在「版本与更新」页点【重新检测】）"

IP="$("$PY" - <<'PYEOF' 2>/dev/null || true
import subprocess, re
try:
    out = subprocess.run(["ip","-4","route","get","1.1.1.1"], capture_output=True, text=True).stdout
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
    if m: print(m.group(1))
except Exception: pass
PYEOF
)"
[ -n "$IP" ] || IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)"

VERSION="$("$DEST/bin/openvpn" --version 2>/dev/null | head -1 | awk '{print $2}' || true)"

# 装完二进制就位、服务直接可用；这个码是留给「升级」页的：换二进制 / 编源码 / 更新 Web
# 都要人工放行。先印一个 2 小时的在这里，省得装完还要另开终端（一次性，填错 5 次作废）。
FIRSTCODE=""
if [ -x "$DEST/system/install-code.sh" ]; then
    FIRSTCODE="$(OVPN_CODE_QUIET=1 "$DEST/system/install-code.sh" 7200 2>/dev/null || true)"
fi
CLIENT_IP="$("$PY" - "$DEST" <<'PYEOF' 2>/dev/null || true
import os, sys, json, sqlite3
root = sys.argv[1]
db = os.path.join(root, "data", "db.sqlite3")
try:
    con = sqlite3.connect(db)
    row = con.execute("SELECT v FROM meta WHERE k='public_addr'").fetchone()
    print(json.loads(row[0]) if row else "")
except Exception:
    pass
PYEOF
)"

VPID="$("$PY" - "$DEST" <<'PYEOF' 2>/dev/null || true
import os, sys
sys.path.insert(0, sys.argv[1])
from system import core          # 业务模块在 system/，与 Django 的 core app 分开
print("yes" if core._vpn_alive() else "no")
PYEOF
)"
if [ "$VPID" = "yes" ]; then
    VPN_STATE="运行中（${VERSION:-未知版本}）"
else
    VPN_STATE="未运行（${VERSION:-无二进制}）← 见上面的错误"
fi

cat <<EOF

------------------------------------------------------------
 安装完成$([ "$VPN_OK" = 1 ] && [ "$VPID" = "yes" ] || echo "（但有项目没起来，见下）")
------------------------------------------------------------
 管理后台:   http://${IP:-<服务器IP>}:$PORT
 管理账户:   admin
 管理密码:   $( [ "$NEWPW" = 1 ] && echo "admin（首次登录强制修改）" || echo "（沿用已有 data/admin_password.txt）" )
 OpenVPN:    ${VPN_STATE}  监听 ${CLIENT_IP:-$IP}:1194
 安装码:     ${FIRSTCODE:-（生成失败，请执行 $DEST/system/install-code.sh）}  ← 「升级」页换二进制/源码时填，2 小时内有效
 本机编译:   ${BUILD_ENV}
 客户端配置: $DEST/data/company.ovpn   （全员共用，含 CA 与 tls-crypt）
 服务端配置: $DEST/data/server.conf
 日志:       journalctl -u openvpn-web -f   /   tail -f $DEST/data/openvpn.log
 规则重放:   systemctl status openvpn-web-replay.timer（firewalld reload / 重启后自动补规则）
 环境预检:   ${PRECHECK_STATE}
 装完自检:   ${SELFTEST_STATE}
 运行时:     随包离线 Python（不依赖目标机 python3）
------------------------------------------------------------
 下一步: 1) 浏览器登录，在「账号」里加人（或安装时加 --user 名字:密码）
         2) 把 company.ovpn 发给成员，用 OpenVPN 客户端导入
         3) 内网资源授权在「ACL」页配；默认 NAT 模式开箱可用
------------------------------------------------------------
EOF

if [ "$NEWPW" = 1 ]; then
    say "提示: 默认密码 admin 只在首次登录有效，登录后系统会强制你设置新密码。"
fi
if [ "$WITH_TOOLS" != 1 ] && ! command -v gcc >/dev/null 2>&1; then
    say "提示: 本机没有 gcc，暂时不能用「升级」页上传源码编译。需要的话执行："
    say "      $0 --with-build-tools        （或 yum install -y gcc make perl pkgconfig）"
fi
if [ "$SRC" != "$DEST" ] && [ -d "$SRC" ]; then
    say "提示: 安装源目录 $SRC 已不再需要，可删掉腾空间（rm -rf $SRC）"
fi

# 装完必须两样都在跑。少一样就以非 0 退出——别让自动化以为装好了。
if [ "$VPN_OK" != 1 ] || [ "$VPID" != "yes" ]; then
    say ""
    say "警告: 服务端没跑起来（OpenVPN: $VPN_STATE）。Web 已经起来了，可以登录看日志；"
    say "      补齐 openvpn 二进制后重跑 $DEST/system/setup.py，或直接重跑本脚本。"
    exit 3
fi
