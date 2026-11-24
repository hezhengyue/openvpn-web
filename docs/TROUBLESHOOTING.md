# 排障清单

> 从 README 第 10 章拆出：常见故障的现象、原因与处理。
> 返回 [README](../README.md)。

---

## 10. 排障清单

| 现象 | 查什么 |
|---|---|
| 连上隧道但 ping 不通内网 | **仅无 NAT 溯源模式**：网关回程静态路由没配（第 2 节）；或切回 NAT 模式先恢复 |
| **ping 通但 HTTPS 超时 / SSH 秒断（10053）** | **仅无 NAT 溯源模式 + 爱快网关**：缺 NAT 过滤规则，回程 TCP 被 SNAT 成网关 IP。按第 2.1 节配「回程流量跳过 NAT」规则 |
| 内网日志源 IP 全是 VPN 服务器 | 当前是默认 NAT 模式（正常）；想看账号固定 IP 就开启「无 NAT 溯源」。若已开启仍如此 → 有 NAT 残留，点「重建规则」，自检页会报 |
| 全员连不上 | 服务没起 / 端口被占 / `ccd-exclusive` 下账号没有 ccd / firewalld reload 冲了 FORWARD（timer 5 分钟内恢复） |
| 某账号连不上 | 状态是否停用、密码、是否被失败锁定、`ccd/<用户名>` 是否存在 |
| Windows 报 `local and remote VPN endpoints must exist within the same 255.255.255.252 subnet` | 客户端把 ccd 下发的 `10.8.0.x 255.255.255.0` 按点对点（net30）解析了，即拓扑没对上。服务端 `server.conf` 要有 `topology subnet` **和** `push "topology subnet"`；客户端 ovpn 也要有 `topology subnet`。缺 push 时 Linux 客户端能连、Windows 直接 fatal，所以别只拿 Linux 验 |
| 改了 IP 权限不对 | 看是否触发了全量重建；自检页对比「启用账号数 / ccd 文件数」 |
| ACL 没生效 | 是否还在观察模式；授权是否过期；目标是否被 LB/NAT 改了源 IP |
| 客户端之间互访不通 | 是否加了互访授权（默认全拒）、是否还在观察模式、双方账号是否启用；自检页看 IPv4 转发是否开启 |
| 时间串不起来 | 所有机器的 NTP |
| Web 打不开 | 先看是不是**白名单拦的**：页面会直接告诉你「来源 IP x.x.x.x 不在管理端白名单内」。SSH 上去 `python3 manage.py ovpn_cli allow <你的网段>`，或 `allow-reset` 恢复成「127.0.0.1 + 本机网卡网段」；再看 `journalctl -u openvpn-web -n 50`、`systemctl status openvpn-web` |
| 特权动作失败 | `journalctl` + 任务日志 `data/tasks/<id>.log`；确认 sudoers 里的解释器路径与 `core.PY` 一致 |
| 上传二进制/源码提示「需要安装码」 | 这是设计如此：这三个动作会把外部代码装成 root 服务，必须有人在机器上按一下。`sudo /opt/openvpn-web/install-code.sh` 拿一个（8 位、15 分钟、只能用一次、填错 5 次作废） |
| 「升级」页说本机不能编译 | 缺 `gcc/make/pkconfig`。装一次：`yum install -y gcc make perl pkgconfig`（Debian/Ubuntu 用 `apt-get install -y gcc make perl pkg-config`），或在安装目录跑 `./install.sh --with-build-tools`。CentOS 7 已 EOL，官方源失效时把 `CentOS-Base.repo` 的 `baseurl` 指到 `mirrors.aliyun.com/centos-vault/7.9.2009/` |

---
