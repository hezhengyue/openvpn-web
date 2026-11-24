# 安全边界与上线清单

> 从 README 第 9 章拆出：特权隔离、安装码闸门、上线前安全清单、tls-crypt 说明。
> 返回 [README](../README.md)。

---

## 9. 安全边界与纪律

- 纯 HTTP 内网页，**默认只放行本机网卡所在网段 + 127.0.0.1**（升级时自动从「所有私网」收紧）；VPN 拨号用户只放行自助改密页 `/self`，进不了后台。名单可在「管理密码与安全 → 管理端访问白名单」里改，保存时会校验「你当前 IP 还在名单里」，防止把自己关在门外；配错了 SSH 上去 `python3 manage.py ovpn_cli allow 10.20.30.0/24`（`allow-reset` 恢复本机网段）。管理账户 + 密码（PBKDF2）、签名 Cookie（HttpOnly / 8h）、CSRF、失败 5 次锁来源 IP 10 分钟、全操作审计（含操作者 IP）。**别直接暴露公网**，远程管理请走 HTTPS 反代或堡垒机
- `X-Forwarded-For` 只在「本机反代」时才采信（`remote_addr` 是 127.0.0.1），否则任何人加个头就能伪装成白名单地址

- 认证脚本任何异常一律 `exit 1`（fail-closed）；密码走 `/dev/shm` 临时文件，不进环境变量/命令行；同一「账号+来源IP」10 分钟失败 5 次锁定
- 溯源到的是**账号**不是设备：`company.ovpn` 全员共用，账号密码不要转借；一人一账号，别共享。**只有开启「无 NAT 溯源」后内网服务器日志才能看到账号固定 IP**；默认 NAT 模式下内网日志源 IP 是 VPN 服务器，只能靠认证日志查来源公网 IP
- 固定 IP 只能管理员改，不放权给客户端
- 别把 `company.ovpn` 之外的个人凭据塞进客户端配置；服务器配置里不要加 `redirect-gateway` / `duplicate-cn` / `client-to-client`。**别手工加 `client-to-client`**：客户端互访走内核 FORWARD 才能被 ACL 与日志覆盖
- NAT 由自建 `OVPNWEB_NAT` 链接管；别再执行 `firewall-cmd --add-masquerade` 或另加 NAT 规则，否则开启溯源后会有残留导致链路失效
- 上传的二进制会被执行 `--version` 读版本号，**只上传可信来源的静态二进制**

### 9.1 上线前安全清单（v1.3.16 加固）

| 项 | 现状 |
|---|---|
| 安全响应头 | 全站下发 `Content-Security-Policy`（禁外链脚本/对象/嵌套）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Permissions-Policy` |
| 错误回显 | 500 只回「服务端处理失败，错误已记录」，异常栈只进服务器日志与审计；404/405 保持原状态码 |
| 未登录信息暴露 | `/api/session` 未登录不再返回管理账户名；白名单外来源一律 403 |
| 凭据重置 | Web 端「已登录即可重置管理凭据」的 `/api/admin-reset` 已删除，只保留 `reset-admin.sh` / `--reset-admin` |
| Excel 注入 | 导出的 xlsx 里以 `=` `+` `-` `@` 开头的内容强制按文本落盘，避免打开表格时执行公式 |
| 自助改密页 | 姓名/部门/账号名一律 `textContent` 写入，不再拼 `innerHTML`（台账数据经 Excel 导入，不能被当成 HTML） |
| CSRF / 会话 | 写接口全部校验 CSRF；登录成功 `session.clear()` 防会话固定；Cookie HttpOnly + SameSite=Lax + 8 小时过期 |
| 路径与命令 | 备份包 tar 成员只允许 `openvpn/`、`web/` 前缀；worker 只接受固定 action 白名单 + `safe_path`；Web 侧所有 `subprocess` 不走 shell |
| 文件权限 | `openvpn-web.db` + `-wal/-shm`、`web.secret`、`admin_password.txt` = 600；`data/backups` = 700；`data/` = 750（启动时自动纠正，安装脚本同步设置） |
| HTTPS 反代 | 走 HTTPS 时设 `OVPN_WEB_HTTPS=1` 启动，会话 Cookie 带 `Secure`，同时下发 HSTS |

**已知边界（架构决定，不是漏洞）**
- **Web 管理员 ≈ root**：管理员能给服务器下 iptables、重启服务、上传源码本机编译（编译以 root 执行）。所以后台不要给不信任的人，管理端务必只在内网 / 堡垒机后。
- **明文密码存库**：台账按你的要求保存明文（方便查看、导出 Excel、下发），数据库文件权限必须保持 600，`data/` 目录不要给别人读。
- **默认是 HTTP**：密码与台账在内网明文传输。要跨公网管理，请套 Nginx/Caddy HTTPS 反代（并设 `OVPN_WEB_HTTPS=1`），或只走堡垒机。
- 账号密码长度：管理密码与自助改密强制 ≥8 位；管理员建账号时目前不强制（历史台账常有短密码），建议上线后统一按 ≥8 位维护。

### `ta.key` / tls-crypt 是干什么的（为什么 iKuai 上没有）

`data/ta.key` 就是那段 `-----BEGIN OpenVPN Static key V1-----` 的静态密钥，它在 `server.conf` 里以 `tls-crypt ta.key` 引用、在 `company.ovpn` 里内联在 `<tls-crypt>` 中。它**不加密业务数据**（那是 `data-ciphers` 的事），只作用于 TLS 控制通道：

| 作用 | 没有它会怎样 |
|---|---|
| 给整个握手过程（含证书）再套一层对称加密 | 握手包是明文结构，中间人能看到证书、也能识别出「这是 OpenVPN」 |
| 用静态密钥做 HMAC 认证，密钥不对的包直接丢弃 | 任何人都能往端口上灌包，服务器要陪它走完 TLS 握手，容易被探测/拖资源 |
| 防中间人伪造控制包 | 攻击者可尝试注入/篡改控制通道报文 |

一句话：**它是「先把门藏起来，再验身份」** —— 端口扫描看不出这是 VPN，无效流量在第一层就被丢掉。iKuai 的 OpenVPN 界面通常只给 `tls-auth`（只签名不加密）或干脆不生成，所以你在那边没见过这个文件；不配也能连，只是把端口暴露成「谁都能敲门的 OpenVPN」。

全员共用同一份 `ta.key`：**换掉它 = 所有人必须换 `company.ovpn`**（配置指纹会变，页面会红字提示）。它不参与数据加密，所以平时不用动；但泄露后应连同 CA 一起重签。

---
