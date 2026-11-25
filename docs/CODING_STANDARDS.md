# 编码规范（openvpn-web 3.0）

目标：**读代码的人能一眼看出这段逻辑属于哪一层、会不会碰特权**。规范不追求形式统一，
只约束「会导致故障或安全问题的写法」。

---

## 一、分层与命名

| 层 | 位置 | 命名 | 允许做 |
| --- | --- | --- | --- |
| 视图 | `ovpn/views/<子域>.py` | `api_xxx` / `xxx_get` / `xxx_add` | 解析请求、调服务、封装响应 |
| 只读查询 | `ovpn/selectors.py` | `xxx_overview` / `xxx_detail` | 聚合读、派生字段 |
| 业务服务 | `ovpn/services/<子域>.py` | 动词开头：`add_user` / `update_grant` | ORM 读写、审计、返回 dict |
| 适配器 | `ovpn/bridge.py` | `sudo_worker` / `start_task` | 唯一调特权层的出口 |
| 特权层 | `system/*.py` | `action_xxx` | root 操作、系统命令 |
| 公共层 | `common/` | 领域无关 | 真实 IP、邮件、工具函数 |

- **禁止**再出现 `core.py` 这类「什么都放」的模块；`ovpn/services/core.py` 已在 3.0 拆分。
- 文件超过 ~400 行先想「是不是混了两个子域」，再拆。
- 模块内私有函数用 `_` 前缀；跨模块调用的一律去掉下划线并登记 `__all__`。

---

## 二、配置

- 业务配置只在 `config/settings.py` 定义，`OVPN_` 前缀，`.env.example` 同步登记。
- 业务代码禁止 `os.environ`；用 `settings.OVPN_xxx`。
- 默认值必须安全：`USE_HTTPS` 默认关闭只允许内网，`DEBUG` 默认 False，
  `EMAIL_ENABLED` 默认 False，`OVPN_WEB_MOCK` 默认 False。
- 生产启动校验（`settings.py` 末尾）失败要 `ImproperlyConfigured`，不能静默降级。

---

## 三、错误处理

- 参数/业务错误 `raise ValueError("中文提示")`；`ovpn/middleware.py` 统一转 400 JSON。
- 权限/白名单/未登录：`PermissionDenied` / 跳登录，不要手工拼 403 HTML。
- 外部命令失败：`subprocess.run(..., check=True)` + 捕获后转成可读消息，保留原始 stderr
  到日志（`logger.exception`），不要把 stderr 原样返回给浏览器。
- **fail-closed**：配置渲染、规则下发、认证钩子、备份恢复，任何一步校验不过就整体拒绝，
  绝不「先落一半再说」。
- 不要 `except Exception: pass`；至少 `logger.warning` 并说明为什么可以忽略。

---

## 四、安全

- 写接口必须 `write_only(...)`（GET 一律 405）。
- 任何文件路径来自请求：先 `realpath` 再确认在允许目录内（参考 `system/fullbackup.py` 的
  `_safe_rel` / `_inside`）；`shutil.copy` / `chown` 不跟随软链（`follow_symlinks=False`）。
- 渲染进 root 执行脚本的值（网段、端口、协议、IP）必须二次校验，宁可让下发失败。
- 不在源码根写裸 SQL；特权层用 `sqlite3` 是设计允许的（它在 `system/`）。
- 加密/口令：Django Auth 负责登录口令；VPN 拨号口令仍是 PBKDF2（`system/core.py`），
  两边不要混用同一份哈希。
- 审计只走 `audit.services.audit_log()`；不要直接 `AuditLog.objects.create(...)`。

---

## 五、日志

- 模块级 `logger = logging.getLogger(__name__)`；`ovpn` / `audit` 的 logger 已在
  `settings.LOGGING` 配好文件与级别。
- 记录「谁对谁做了什么」用审计；记录「程序执行到哪」用日志，两者不混。
- 不打印口令、token、私钥、cookie；必要时要打码。
- 高频轮询路径（`/api/task/<tid>/log`）不写文件日志。

---

## 六、测试

- 每个 app 的 `tests.py` 覆盖：模型约束、服务主流程、接口 405、权限边界。
- 回归测试命名说清「防的是什么」：`test_get_restart_is_rejected`、
  `test_manifest_traversal_rejected`。
- 涉及特权层的纯逻辑（备份 manifest 校验、路径校验）放 `system/tests/`（契约禁止源码根
  出现裸 SQL 夹具）。
- 前端脚本（`tests/smoke.js` / `shots.js`）只在需要真实浏览器时用；能用 Django test
  client 验证的不要起浏览器。

---

## 七、注释

- 注释解释**为什么**，不复述代码；尤其要写清「这里不这么做会出什么问题」。
- 安全相关的「看似多余」的校验必须注释原因（例：为什么 `_safe_rel` 只认白名单）。
- 历史包袱要写清来龙去脉（例：`serve.py` 为什么要把 `core` 迁移记录改成 `accounts`）。
- 中文注释、英文标识符；不要中英混排的变量名。

---

## 八、提交

- 一个提交只做一件事；重构与行为修改分开。
- 提交信息：`fix: ...` / `feat: ...` / `refactor: ...` / `docs: ...`，正文写清影响面与验证方式。
- 改前端必须同步 `core.WEB_VERSION` 与 `index.html` 的 `PAGE_VERSION`（否则浏览器缓存旧界面）。
- 不提交 `data/`、`logs/`、`bin/` 大文件、`.env`、`.workbuddy/`。
- 推送前跑完 [DJANGO_APP_GUIDE.md](DJANGO_APP_GUIDE.md) 第八节的五条命令。
- **修 bug 的提交，必须先过下面五个问题**（不是走过场，答不上来就说明还没修完）。

### 修完 bug 的五个检查点

| # | 问题 | 答不好会怎样 |
|---|---|---|
| 1 | 现象是**指向 A 而根因在 B** 吗？我是不是因为这个走了弯路？ | 下次同一现象还会走同一条弯路 |
| 2 | 有没有一条命令能在 2 分钟内定性，而不是靠回忆？ | 靠人记，换个人就重来一遍 |
| 3 | 这个坑会**复发**吗？需要特定环境组合才能触发吗？ | 漏掉就等着在客户现场复发 |
| 4 | 我加的断言，能挡住**这次的原始触发路径**吗（不是挡住「我以为的路径」）？ | 断言形同虚设，测的是别的东西 |
| 5 | 这些经验进 [INCIDENTS.md](INCIDENTS.md) 了吗？ | 三个月后没人知道当初为什么这么改 |

判定标准（同时满足「会复发」+「有误判或根因不显性」→ 必须写一条 INC）：

- 根因不显性（现象指向 A，实际在 B）
- 排查走过弯路
- 依赖特定环境组合（发行版 / 防火墙 / 初始化系统）
- 涉及安全边界被绕过

纯功能新增、崩栈直指根因一次改对的，不写。

**硬约束**：INC 里挂的 `tests/selftest.py::<断言名>` 必须真实存在，`selftest` 有元检查会校验。改断言名要同步改文档，否则验收直接失败——这条是为了防止「文档说防住了，断言其实早被删了」。

---

## 九、依赖

- 新增 Python 依赖必须同时更新 `django/requirements.txt` 与
  `django/requirements-standalone.txt`，并把 wheel 放进 `vendor/wheels/`（离线包要用）。
- 不引入需要编译的依赖（目标机没有 gcc），不引入 Node/前端构建链。
- 版本锁定到具体版本号，不用 `>=`。
