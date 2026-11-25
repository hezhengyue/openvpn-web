# 故障复盘库（INC）

> 记录**会复发、且误判过**的故障。纯功能变更不在这里，看 [CHANGELOG](CHANGELOG.md)。

## 这份文档解决什么

`git log` 能告诉你「改了什么」，`CHANGELOG` 能告诉你「这个版本改了什么」，但都答不上三个问题：

1. **下次遇到同样现象，不该往哪查？** —— 误判的代价往往比修复本身大得多。
2. **怎么用一条命令在 2 分钟内定性？** —— 而不是靠回忆和猜。
3. **靠什么保证它不再复发？** —— 文档会腐化，断言不会。

所以每条记录固定六栏，其中**「误判与代价」和「定性方法」是最值钱的两栏**，写的时候不要省。

## 什么时候写

写：

- 根因**不显性**（现象指向 A，实际在 B）
- 排查过程中**走过弯路**（有误判、有返工）
- 依赖**特定环境组合**才会出现（某种发行版/防火墙/初始化系统）
- 涉及**安全边界**被绕过

不写：

- 纯功能新增/界面调整
- 崩溃栈直接指向根因、一次改对的
- 已经在 `TROUBLESHOOTING` 里能被现象表一枪命中的常规故障

## 三条硬规则

1. **没有断言就不写。** 每条必须挂至少一条自动断言（见「防复发」栏，写法是 `tests` + `/selftest.py::` + 断言名）。断言在，坑就不会复发；断言被删，`selftest` 的元检查会直接报错（`复盘库引用的断言全部仍然存在`）。谁想删断言，必须先改这份文档。
2. **只写会复发的。** 宁可少写、写透。索引表控制在 20 条以内；超过 20 条说明共性已经很清楚，该把规律抽进 [CODING_STANDARDS](CODING_STANDARDS.md) 而不是继续堆案例。
3. **误判必须如实写。** 写「我当时以为是 X，查了 N 小时」不是自曝其短，是给下一个人省 N 小时。省略这一栏，这条记录的价值就掉了一半。

## 与其他文档的分工

| 文档 | 回答的问题 | 组织维度 |
|---|---|---|
| `git log` | 改了什么 | 提交 |
| [CHANGELOG](CHANGELOG.md) | 这个版本改了什么 | **版本** |
| [TROUBLESHOOTING](TROUBLESHOOTING.md) | 出现这个现象该查什么 | **现象** |
| **本文档** | 为什么当时判断错了、怎么快速定性、靠什么不复发 | **事件** |
| [SECURITY](SECURITY.md) | 架构层面的安全边界在哪 | 边界 |

---

## 索引

| 编号 | 版本 | 一句话 | 断言数 |
|---|---|---|---|
| [INC-0001](#inc-0001) | 3.0.1 | 内网访问 VPN 客户端 ping 通、TCP 全不通——根因在服务端 firewalld 的 FORWARD 兜底 REJECT | 3 |
| [INC-0002](#inc-0002) | 1.3.28 | 配置项换行注入 → 以 root 执行的 `server.conf` | 2 |
| [INC-0003](#inc-0003) | 1.3.27 | 环境变量经 `sudo` 传递，绕过安装码闸门 → root 装任意 ELF | 3 |
| [INC-0004](#inc-0004) | 1.3.24 | 缺二进制就 `exit 1`，装完 Web 起不来、向导进不去 | 2 |

---

<a id="inc-0001"></a>
## INC-0001 · 内网访问 VPN 客户端 ping 通但 TCP 全不通

| 项 | 内容 |
|---|---|
| 版本 | 3.0.1（2026-11-25） |
| 影响面 | 所有启用 firewalld 的目标机（CentOS / RHEL / Anolis / Rocky 默认启用），**LAN → VPN 客户端**方向 |
| 严重度 | 高（功能完全不可用，但症状极具误导性） |

### 现象

内网机器（`10.255.114.11`）访问 VPN 客户端（`10.8.0.2`，Windows 笔记本）：

- `ping` 通，延迟正常（5~10ms）
- `nmap -p 3389` 显示 **`filtered`**（不是 `closed`）
- 任何 TCP 端口都不通，临时开的对照端口 50000 也一样
- 在客户端上抓包：**一条报文都没有**；RDP 事件日志无任何连接尝试；Windows 防火墙丢包日志无记录

### 误判与代价

前后耗时约 **2 小时**，几乎全部是无效方向：

| 当时的判断 | 实际 |
|---|---|
| `ping` 回包 TTL=64 → `10.8.0.2` 不是 Windows，是 Linux 设备 | 错。是 360 的「网络优化」把 `DefaultTTL` 改成了 64 |
| 客户端 Windows 防火墙静默 DROP | 错。临时关掉全部防火墙配置档仍然不通 |
| 360 内核驱动（`360netmon` / `360AntiAttack` / `360AntiHacker`）拦包 | 错。360 全程无辜，SYN 从没到过笔记本 |
| 之前的 `nmap` 触发 360 拉黑源 IP | 错。伪造源地址对照测试，`10.255.114.11` 和 `10.8.0.1` 都能拿到 SYN-ACK |
| RDP 服务缺 Network Service 权限 | 那处修复有效（RDP 确实起来了），但不是本次故障原因 |

**最贵的一条误判**：从服务端自己 `connect` 到 `10.8.0.2:3389` 成功了，于是判定「网络已通，剩下是应用层问题」。实际上服务端自己发起的包走 `OUTPUT` 链，**绕过 `FORWARD`**，这条路根本测不到 LAN → 客户端。

### 根因

CentOS 7 的 firewalld 0.6.3（`iptables` 后端）：

```text
-A IN_public -p icmp -j ACCEPT        ← zone 链末尾无条件放行 ICMP

-FORWARD
  -j FORWARD_IN_ZONES
  -j FORWARD_OUT_ZONES
  -m conntrack --ctstate INVALID -j DROP
  -j REJECT --reject-with icmp-host-prohibited   ← 兜底
```

`tun0` **不在任何 zone 里**（`firewall-cmd --get-zone-of-interface=tun0` → `no zone`）。于是从 `ens192`（LAN 方向）转发到 `tun0`（VPN 客户端）的流量没有匹配的 zone 放行规则，直接落到末尾的 `REJECT`。

ICMP 因为 `IN_public` 里那条无条件放行而能过 —— 这就是「ping 通、TCP 全不通」的来源。SYN 停在 `FORWARD` 链，**根本没上 `tun0`**，所以客户端侧什么都收不到。

### 定性方法

三条命令，2 分钟内出结论：

```bash
# 1) 兜底 REJECT 的计数在涨 → 转发流量正在被拒
iptables -L FORWARD -vn | grep REJECT

# 2) zone 链里只放行了 ICMP → 解释了「ping 通 TCP 不通」
iptables -S IN_public | tail -3

# 3) 没有「私网 → tun0」的放行 → 确认缺规则（3.0.1 起应有 3 条）
iptables -S FORWARD_direct
```

**判定纪律**：不要用「服务端能不能连上客户端」来证明 LAN 能连上。服务端发起走 `OUTPUT`，绕过 `FORWARD`，两者是不同的路径。要验证只能：
- 让内网机器实际发起，并同时观察 `FORWARD_direct` 的 ACCEPT 计数是否增长；或
- 在服务端用 raw socket 伪造源地址（但这仍走 `OUTPUT`，只能证伪客户端侧过滤，不能证明 `FORWARD` 通）。

### 修复

- `core.render_rules()` 幂等补三条 `--direct` 规则：`-s 10.0.0.0/8 | 172.16.0.0/12 | 192.168.0.0/16 -o tun0 -j ACCEPT`
  - 只放私网源，公网源访问 VPN 客户端仍由 firewalld 拦住
  - 用 `iptables -C FORWARD_direct` 判存在，重复执行安全
  - **绝不 `firewall-cmd --reload`**：reload 会重建 `FORWARD` 链，把 `OVPN_ACL` 的跳转冲掉
- `setup.py firewall()`：全新安装时同步写永久规则（末尾本来就有一次 `--reload`，正好让 permanent 生效）
- `worker.py`：系统状态采样增加 `FORWARD_direct`（Web 侧低权限跑不了 iptables，只能读快照）
- `core.selfcheck()`：新增「firewalld 转发放行」一项
- `precheck.py`：安装前提示转发语义（含 ufw `DEFAULT_FORWARD_POLICY=DROP` 的提醒）
- `install.sh --purge`：同步清理这三条 direct 规则

### 防复发

- `tests/selftest.py::firewalld 转发放行规则已生成`
- `tests/selftest.py::firewalld 修复不触发 reload`
- `tests/selftest.py::转发放行只对私网源段生效`

外加：自检页「firewalld 转发放行」、`precheck.py` 提示、[TROUBLESHOOTING](TROUBLESHOOTING.md) 两条现象条目、[VERIFICATION](VERIFICATION.md) 环境矩阵。

---

<a id="inc-0002"></a>
## INC-0002 · 配置项换行注入，写进以 root 执行的 `server.conf`

| 项 | 内容 |
|---|---|
| 版本 | 1.3.28（安全审查主动发现） |
| 影响面 | 所有可登录管理后台的部署 |
| 严重度 | 严重（Web 管理员 → root，且不需要安装码） |

### 现象

无外部现象。`tls_min` 是 `validate_cfg` 的白名单盲区：类型是字符串、没有取值约束。

### 误判与代价

这类问题**没有"误判"，只有"漏判"**。上一轮加固把「保存配置」当作受控输入，但只校验了值域，没校验**是否含控制字符**。整条链路是：

```text
绕过前端直接 POST /api/settings
  → 值原样落库
  → render 时拼进 server.conf 的 tls-version-min 行
  → settings_save 重启 OpenVPN
  → 该文件以 root 身份被 openvpn 解析，`up` / `plugin` 指令即 root 命令执行
```

### 根因

**「写进特权执行文件」的字段，校验的是语义而不是形态。** 任何能进 `server.conf` / `rules.sh` / `ccd/*` 的字符串，只要没做字符集收敛，换行就是注入。

同一次修复里还发现同类问题：

- `rules.sh` 里 `dns` / `ntp` 未加引号直接拼进 iptables 行
- 备份恢复：manifest 哈希只证明「包自洽」，不证明「包没被改过」——改库重算哈希就能把脏值送进 root 执行的脚本

### 定性方法

不用等出事，用这条纪律自查：

```bash
# 列出所有「会写进 root 执行文件」的渲染点
grep -n "server.conf\|rules.sh\|ccd/" system/core.py | grep -i "write\|render\|f.write"
```

逐个问：这个值有没有经过 **字符集收敛**（不只是值域白名单）？没有就是洞。

### 修复

- `tls_min` 加白名单（只认 `1.2` / `1.3`），渲染前再 fail-closed 拦一次
- 恢复前先复核包内配置，不合法**整包拒绝**（在任何生产文件被覆盖之前）
- 渲染面全部收口：网段强制 `IPv4Network` 规范化、账号 IP/资产网段/端口/协议渲染时再校验、ccd 生成前校验固定 IP
- 带副作用的 `GET /api/logs?kind=deny`、`GET /api/selfcheck` 纳入 CSRF

### 防复发

- `tests/selftest.py::validate_cfg 拒绝带换行的 tls_min`
- `tests/selftest.py::validate_cfg 接受合法 tls_min`

---

<a id="inc-0003"></a>
## INC-0003 · 环境变量经 `sudo` 传递，绕过安装码闸门

| 项 | 内容 |
|---|---|
| 版本 | 1.3.27（测试机实测发现） |
| 影响面 | 所有部署（1.3.23 ~ 1.3.26） |
| 严重度 | 严重（能调面板 API = root） |

### 现象

1.3.23 给「上传即 root」加了安装码闸门。测试机实测：

```text
无安装码 → replace-binary 被拦 ✅
带 OVPN_WEB_MOCK=1 → 直接进动作 ❌
```

### 误判与代价

上一版把 `OVPN_WEB_MOCK` 定义成「是否碰真实系统」的开关，同时**复用它**做闸门豁免（`if act in GATED and not MOCK`）。这两个语义被绑在一起，等于把「测试便利性」写进了「安全检查」。

问题不在于写错一行代码，而在于：**只要有一个 Web 进程能控制的环境变量参与特权判断，整个闸门就形同虚设**——因为 sudoers 的 `env_keep` 会把它原样送进 root 进程。

### 根因

`install.sh` 的 sudoers 配置用 `env_keep` 保留了 `OVPN_WEB_MOCK`（为了让 worker 在测试模式下不碰真实系统）。于是任何能调面板 API 的人，在自己进程里设这个变量，经 sudo 传进 worker，闸门判断被短路。

### 定性方法

```bash
sudo -l | grep -A5 env_keep
```

**看 `env_keep` 里有没有任何变量参与特权判断。** 有 → 就是洞。原则：`env_keep` 只应保留**路径类**配置（安装目录、数据目录），不应保留任何**行为开关**。

### 修复

- 闸门改看专用开关 `OVPN_WEB_SELFTEST`，它只在「直接跑 `worker.py` 自检」时才存在
- sudoers 加 `env_reset`，`env_keep` 只保留 `OVPN_WEB_ROOT`（数据目录）——该开关经 sudo 必被清空
- `OVPN_WEB_MOCK` 退回它原本的职责：只管「是否碰真实系统」

### 防复发

- `tests/selftest.py::worker 闸门不再因 MOCK 环境变量而放行`
- `tests/selftest.py::sudoers 显式 env_reset`
- `tests/selftest.py::三个「装外部代码」的动作都在闸门名单里`

---

<a id="inc-0004"></a>
## INC-0004 · 缺二进制就 `exit 1`，装完 Web 起不来

| 项 | 内容 |
|---|---|
| 版本 | 1.3.24 |
| 影响面 | 缺 `bin/openvpn` 的交付包 / 目标机 |
| 严重度 | 中（可用性，非安全） |

### 现象

`install.sh` 在缺 `bin/openvpn` 时直接 `exit 1`：

- Web 没起来，人只能对着终端干瞪眼
- 文档里「`--skip-openvpn` 之后去向导第 2 步上传二进制」这条补救路径**根本走不通**——向导本身就是那个没起来的 Web

### 误判与代价

这是**流程设计问题被当成错误处理问题**：把「缺一个组件」写成了「整单失败」，而实际上缺的这一个组件可以在 Web 起来之后再补。

同类思维在别处也有：装到一半失败就 `exit`，不区分「致命」和「可延后」。

### 根因

`install.sh` 第 7 步把「装 openvpn」和「起 web」串行硬绑定，前者失败即整体中止。

### 定性方法

装完只看退出码是不够的，要看**实际状态**：

```bash
systemctl is-active openvpn-web
systemctl is-active openvpn      # 手动 daemon，可能显示 unknown，看进程/端口
```

「退出码 0 但服务没起」和「退出码非 0 但服务起了」都是可能的，以实际状态为准。

### 修复

- 新增 `prepare-openvpn.sh`，三条兜底路径：包内二进制 → 用 `vendor/src` 就地离线编译 → 都没有则明确失败
- 二进制齐了才调 `setup.py`；它失败也照样把 Web 起起来（人能登录看日志），最后以 `exit 3` 结束
- **不拿非 0 退出码糊弄自动化**：`exit 3` 明确表示「部分成功，可人工续做」

### 防复发

- `tests/selftest.py::prepare-openvpn.sh 随包分发`
- `tests/selftest.py::install.sh 装完自动跑包内 selftest`

---

## 维护

- 新增一条：复制上面任意一节的六栏结构，**先写断言，再写文档**
- 修改断言名：同步改本文档（`selftest` 的元检查会拦住漏改）
- 删除一条：只有当对应代码路径整体移除、断言一并删除时才删，并在 `CHANGELOG` 里说明
- 索引超过 20 条：把反复出现的共性抽成 [CODING_STANDARDS](CODING_STANDARDS.md) 的硬规则，案例可精简保留
