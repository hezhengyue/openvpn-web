# 目录结构整理方案（讨论稿，未执行）

> 状态：**仅方案，不动手**。本文分析当前布局的混乱点、三种候选方案的取舍与工作量，供决策。确认前不改变任何文件位置或 import 路径。

## 一、当前布局与问题

2.6.0 起源码与交付包统一为「项目根即 Django 根」，所有东西平铺在根目录：

```
openvpn-web/
├── Django 项目      config/ core/ common/ jobs/ ovpn/ audit/ templates/ static/
├── 入口             manage.py serve.py start.sh
├── 工具/测试        scripts/ tests/
├── 特权层           system/            （root 执行、安装码、worker、core.py 业务核心）
├── 构建/依赖        build/ vendor/ bin/ runtime/ dist/
├── 运行数据         data/ logs/ media/ staticfiles/
└── 文档             README.md CONTRACT.md BUILD-FROM-SOURCE.md requirements*.txt
```

**你的感觉是对的**：30+ 个顶层条目里，一眼分不清哪些是 Django app、哪些是编译产物。但也要理解这种平铺的由来——它是为了让**源码布局 = 交付包布局**（2.6.0 重构的核心目标），这样 `install.sh` / `serve.py` / 契约检查器只有一套路径假设，部署时不用做目录重组。

## 二、关键约束（任何方案都必须满足）

1. **特权边界**：`system/` 必须在 Web 进程不可写的位置，且契约检查器要求特权组件在项目源码根之外登记为 `external_boundaries`。
2. **布局自适应**：契约检查器的 `detect_layout()` 已同时支持「根布局」和「django/ 子目录布局」，所以挪回去在检查器这块成本可控。
3. **打包一致性**：`build-offline.py` / `build-offline.sh` 按目录名复制 app 到交付包；改布局要同步改这两处。
4. **数据不可动**：`data/`（证书、台账、数据库）路径由 `settings.PROJECT_ROOT` 推导，不能因重排而失效。
5. **部署脚本**：`install.sh`（MGMT 变量）、`scripts/compose.sh`、`start.sh`、`serve.py` 的路径探测都依赖当前布局。

## 三、三种候选方案

### 方案 A：零移动 + 导航文档（零风险，推荐先做）

**做法**：文件一个不动，新增一份「目录地图」文档（可直接并入 README 顶部），用表格把每个目录归类：

| 分类 | 目录 | 说明 |
|---|---|---|
| Django 应用 | config/ core/ common/ jobs/ ovpn/ audit/ | 标准 Django 项目本体 |
| 前端资源 | templates/ static/ | 页面与静态文件 |
| 项目入口 | manage.py serve.py start.sh | Django CLI / systemd 入口 / 运维命令 |
| 特权层 | system/ | root 执行、安装码闸门、业务核心 core.py |
| 工具与测试 | scripts/ tests/ | 运维脚本、自检与前端测试 |
| 构建与依赖 | build/ vendor/ bin/ runtime/ dist/ | 编译脚本、离线依赖、二进制、便携 Python、交付包 |
| 运行数据 | data/ logs/ media/ staticfiles/ | 证书台账数据库、日志、上传、收集静态 |

**优点**：零风险、零回归、不影响任何路径。
**缺点**：物理上仍然平铺，只是"有了地图"。

### 方案 B：分层分组（低风险，不改 import）

**做法**：用一个 `_src/` 之类的容器把 Django app 归组，但**不改 Python 包导入路径**——通过 `sys.path` 注入让 `config`/`ovpn` 等仍可从根导入。或者更保守：只把**非代码目录**归类，例如把构建产物类集中：

```
openvpn-web/
├── config/ core/ common/ jobs/ ovpn/ audit/ templates/   ← Django（保持根，import 不变）
├── manage.py serve.py start.sh
├── system/ scripts/ tests/
├── _build/            ← 新建：把 build/ vendor/ bin/ runtime/ dist/ 挪进去
│   ├── build/ vendor/ bin/ runtime/ dist/
└── data/ logs/ media/ staticfiles/
```

**优点**：代码层路径完全不动，只归置"构建/产物"这类非代码目录，视觉上立刻清爽；`bin/`、`vendor/` 的路径在 `core.py` 里多为常量，改动点集中。
**缺点**：`core.py` 里引用 `bin/openvpn`、`vendor/`、`runtime/` 的常量要改（预计 10~20 处），`build-offline.py` / `install.sh` 要同步；仍需完整回归 + 重新部署。

### 方案 C：完全标准 Django 布局（高风险，不推荐）

**做法**：把 config/ core/ common/ jobs/ ovpn/ audit/ templates/ 全部挪回 `django/` 子目录，恢复 2.5.0 的布局。

**优点**：最像教科书 Django 项目。
**缺点**：这正是 2.6.0 花大力气消除的"源码与交付不同构"——挪回去等于推翻该次重构，契约检查器/打包/install/serve/compose/selftest 全部要再改一遍并完整回归。**收益（视觉整齐）远小于成本（回到双布局维护）**。不推荐。

## 四、推荐路线

1. **立即做方案 A**：加目录地图，零风险解决"分不清谁是 Django"的认知问题。
2. **若仍嫌乱，再做方案 B**：只归置构建/产物目录到 `_build/`，代码层路径不动。改动集中在少数路径常量，回归范围可控。
3. **不做方案 C**：除非有强制的"必须通过外部 Django 规范审计"需求。

## 五、方案 B 的迁移清单（若采纳）

- `core.py` / `worker.py` 中 `bin/`、`vendor/`、`runtime/`、`dist/` 路径常量 → 指向 `_build/`。
- `build-offline.py` / `build-offline.sh` / `build-static-openvpn.sh` 的 ROOT/VENDOR/BIN 解析。
- `install.sh`（安装源判断、复制路径）、`scripts/compose.sh`（解释器探测）、`start.sh`（clean:pycache、wheel 路径）。
- `settings.py` 的 `PROJECT_ROOT`（仍以能找到 `system/` 的层为准，不受影响，但需验证）。
- 契约检查器 `IGNORED_DIRS` 增加 `_build`（避免扫描便携运行时）。
- 全量回归：selftest / Django 单测 / 契约检查 / 打包 / 部署验证。
- `.gitignore`：`_build/dist/`、`_build/runtime/` 等路径更新。

---

确认采用哪个方案后再动手。方案 A 只需改文档，方案 B 需要完整回归与重新部署。
