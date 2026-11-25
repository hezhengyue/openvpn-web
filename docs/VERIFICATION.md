# 验证情况与已知限制

> 从 README 第 11 章拆出：已验证项、未覆盖项与交付边界。
> 返回 [README](../README.md)。

---

## 11. 验证情况与限制

- `python3 src/selftest.py`（或装机后在安装目录 `python3 selftest.py`）：**424 项全通过（实测 Windows 424、装机目录 426）**——配置渲染、双网络模式（默认 NAT / 无 NAT 溯源）与切换不改指纹、自建 NAT 链、IPv4 转发前提、证书签发、账号校验（含中文账号 + 中文 ccd + 改名时授权/用户组跟着搬、**明文密码入库与改密同步**）、动态地址池开关与地址冲突、**安装码闸门（一码一次 / 过期作废 / 错 5 次作废 / 脚本生成的码能直接过）**、认证脚本（放行/拒绝/锁定/fail-closed/**有效期到期拒绝**）、Excel 全量同步（**导出带明文密码、密码列留空整批拒绝、有效期落库**）、**账号有效期到期自动停用并清 ccd**、ACL 默认拒绝与改 IP 不残留、客户端互访授权与过期过滤、跨大版本闸门、安装向导长任务、全部 API（含首页禁用缓存、网络模式切换、P2P 增删、下一个可用 IP、容量、白名单保存与「排除自己」拒绝、独立管理账户登录与免密重置、改密码双次确认、**默认 admin/admin 首登强改：未改密拦接口 / 改密免填旧密码 / 改完默认口令失效 / 凭据文件不留明文**、审计带 IP、按钮式保存/恢复默认/重签 CA）、资产条目编辑（改 IP/协议/端口、重复拒绝）、二进制更新、备份打点与回滚、备份生成/下载/上传/按包名恢复/删除与安全校验
- 前端在 jsdom 里逐页真跑（`tests/smoke.js`），另有 `tests/shots.js` 用无头 Edge 截图肉眼核对版式
- **真实环境实测（2026-11-18，Anolis 8.10 / OpenVPN 2.6.23 / TCP）**：建中文账号 `张三` → 服务端生成 `ccd/张三` → 客户端用中文账号认证通过 → `PUSH_REPLY ... ifconfig 10.8.0.2` 拿到台账固定 IP；白名单按来源判定：隧道内 IP 访问 `/api/state` = 403、`/self` = 200，白名单内网段可进后台，公网 IP 全 403

- 真实 Django HTTP 服务启动验证：首页、会话、登录、CSRF、state、向导自检、账号、版本、导出均 200；测试机 10.255.114.15 上 `install.sh` 完整跑通，装完自检 426 项全过，OpenVPN 与 Web 双进程在跑
- 测试机 192.168.9.223（CentOS 7.9 + firewalld 0.6.3）：3.0.1 起实测「LAN → VPN 客户端」转发路径打通，详见 [INC-0001](INCIDENTS.md#inc-0001)
- 前端 JS 通过 `node --check`
- 所有 `.py` 通过 `py_compile`，所有 `.sh` 通过 `bash -n`

---

## 11.1 部署环境矩阵

**这张表的用途是暴露缺口，不是炫耀覆盖。** 没打 ✅ 的组合就是下一次的坑——[INC-0001](INCIDENTS.md#inc-0001) 就是「只验了无防火墙的机器」的代价。

矩阵针对**防火墙形态**，因为它决定「LAN → VPN 客户端」这条转发路径通不通（OpenVPN 自身的 `tun`、`ip_forward`、`OVPN_ACL`、`OVPNWEB_NAT` 与防火墙无关，各环境一致）。

| 防火墙形态 | 状态 | 说明 |
|---|---|---|
| 无防火墙 / iptables 无 FORWARD 规则 | ✅ 实测 | `FORWARD -P ACCEPT`，转发天然通 |
| firewalld 启用（CentOS 7 / Anolis / Rocky） | ✅ 实测 | 3.0.1 修复后通过；`tun0` 不在任何 zone，需 direct 规则 |
| firewalld 启用但被 `reload` 过 | ⚠️ 自愈 | reload 重建 `FORWARD` 链会冲掉 `OVPN_ACL` 跳转，timer 5 分钟内重放恢复 |
| ufw 启用（Ubuntu / Debian） | ❓ **未实测** | `DEFAULT_FORWARD_POLICY=DROP` 同样会拦转发，3.0.1 只在 `precheck.py` 给了提示，**没有自动写入规则** |
| nftables 原生（无 firewalld / ufw） | ❓ **未实测** | 若 `inet filter forward` 链有 `policy drop`，行为等同 firewalld；当前无自动处理 |
| 云主机安全组 / 上游 ACL 拦 | ➖ 不在范围 | 属网络层，本系统无法感知；`precheck.py` 不检测 |
| SELinux enforcing | ⚠️ 部分 | 预检会读取状态并提示；OpenVPN 读写 `data/` 与端口绑定实测正常，未做完整用例 |

**已知风险最高的空格是 ufw。** Debian/Ubuntu 用户装完大概率复现 `ping` 通、TCP 不通，且现象与 INC-0001 完全一致。3.0.1 的处理是：

- `precheck.py` 会提示 `DEFAULT_FORWARD_POLICY`
- `TROUBLESHOOTING` 有对应条目
- 但**不会自动改 ufw 配置**（`/etc/default/ufw` 的改动需要 `ufw reload`，风险与我们刻意避免的 `firewall-cmd --reload` 同类）

如果后续要正式支持 Debian/Ubuntu，应该单独做一轮实测，而不是靠推断。

---

## 11.2 限制（如实说明）

1. 沙盒内以 MOCK 模式验证业务逻辑，**`iptables` / `systemd` / 真实 OpenVPN 进程未实际执行**，这部分由目标机上的 `install.sh` + 安装向导完成，向导自带环境自检与失败回滚
2. 静态 openvpn 二进制与离线 Python 运行时**未在本环境实际编译/下载**（需要联网构建机），由 `build/build-offline.sh` / `build/Dockerfile.build` 复现；也可在目标机就地编译，或在「升级」页上传二进制（要安装码）
3. 是功能与操作逻辑对齐爱快风格，**不是像素级复刻**，未使用其任何界面素材
4. 单机部署，不做高可用、集群、SSO/MFA、多管理员 RBAC、自动 HTTPS
5. 目标机限定 **x86_64 + glibc ≥ 2.17 + systemd + 可用的 `/dev/net/tun`**：Alpine（musl）、ARM、无 init 系统的容器暂不支持，预检会明确拦住并说明原因；不做无 systemd 的 cron 降级，也不出「依赖系统 python3」的薄包——交付包自带全部运行时，就是为了不因环境差异报错

---
