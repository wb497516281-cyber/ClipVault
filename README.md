# 📋 ClipVault

**本地可视化剪贴板管理器** —— 常驻后台自动记录剪贴板历史（文本 + 图片），通过**原生桌面窗口**（tkinter，无浏览器、无 Web 服务）查看、搜索、一键复制回剪贴板。

所有数据只存在你自己的电脑上：SQLite 存文本，图片存本地目录，**零外部请求**。AI 能力（自动分类 / 语义搜索）完全可选——不配 Key 就纯本地关键词搜索，配了才启用。

## ✨ 功能特性

- 🔄 **后台自动采集**：0.5 秒轮询剪贴板，文本和图片（复制/截图）自动入库
- 🗂️ **分组（AI + 手动）**：左侧分组栏把历史按项目/类型归拢；悬停卡片点「分组」勾选加入（可多选）、现场新建；「AI 自动分组」后台分批把未分组条目交给分类模型归组（优先复用现有分组，缺了就新建），单条还能让 AI 建议去哪组
- 🧹 **历史上限清理**：**未分组内容每周自动清理一次，分组内容永久保留**；分组栏「🧹 清理未分组」可随时手动触发（二次确认，展示将删条数与下次自动清理时间）
- 📦 **数据独立存放**：打包版数据放 `%LOCALAPPDATA%\ClipVault\data`（exe 外面），重新打包/覆盖更新都不碰它；旧版数据首次启动自动迁移
- 🔆 **自动更新**（打包版）：启动后台检查 GitHub Release，有新版本就下载，确认后重启热替换；托盘菜单也可手动「检查更新」
- 🖼️ **图片完整留存**：原图 + 200×200 缩略图存 `clipboard_data/images/`，数据库只存相对路径（**绝不存 BLOB**）
- ⚡ **内容去重**：文本按 `sha256(text)`，图片按「转 RGB 后 PNG 字节」做像素级哈希，重复内容不重复入库
- 🔍 **两级搜索**：关键词 LIKE 搜**内容 / 自定义命名 / 来源应用**（默认，纯本地）+ 语义向量搜索（可选 AI）；「智能」模式= 关键词命中排前 + 语义增量去重补后，搜命名一定不漏
- 🏷️ **AI 分类**（可选）：新文本自动打标签（链接/代码/命令/邮箱电话/地址/账号凭证/笔记/其他），卡片上直接显示
- ⚙ **AI 设置窗口**：顶栏「AI 设置」按钮直接在界面里配置 API Key / 接口地址 / 模型 / 分类 / 超时，保存到本地 settings.json **即时生效**，还能一键「测试连接」
- 🏭 **主流厂商预设**：OpenAI / DeepSeek / 通义千问 / 智谱 GLM / Moonshot / 硅基流动 / Ollama 本地，选中即填好地址和默认模型；填好 URL+Key 点「拉取模型」自动获取该账号可用的模型列表填进下拉框
- ✏️ **条目编辑**：悬停卡片点「编辑」，可修改文本内容、给条目命名（名称高亮显示在卡片顶部）；图片条目支持命名
- 📌 **置顶 / 删除**：置顶条目置前排 + 黄色高亮 + 左侧强调条；删除两步确认，图片条目连带清理文件
- 🎯 **一键复制**：点击卡片即把内容写回系统剪贴板——文本 `CF_UNICODETEXT`，图片同时写入 `CF_DIB + CF_BITMAP + PNG` 多种格式，**QQ / 微信 / 企业微信粘贴即用**
- 🕐 **来源追溯**：每条记录来源应用（前台窗口标题）与复制时间
- 🖥️ **托盘常驻**（可选）：单进程搞定「采集 + 界面 + 托盘菜单」，支持暂停采集、开机自启动
- 🌗 **深色界面**：窗口与画布统一深色配色，缩略图懒渲染
- 🛡️ **剪贴板独占兼容**：被其他程序占用时按 50ms 间隔重试 3 次，单轮异常不会让进程退出，适合 24 小时常开

## 🧱 技术栈

| 环节 | 选型 |
|---|---|
| 剪贴板监听 | pywin32（`win32clipboard`）+ Pillow（`ImageGrab.grabclipboard`）|
| 存储 | SQLite（WAL 模式，文本入字段，图片只存相对路径）|
| 界面 | **tkinter + Canvas 画布**（Python 内置，零额外依赖、零构建）|
| AI（可选） | OpenAI 兼容接口，标准库 urllib 实现 |
| 托盘（可选） | pystray |
| 打包（可选） | PyInstaller（onedir）|

## 📁 目录结构

```
clipvault/
├── gui.py               # 原生 GUI 主界面（tkinter Canvas 画布列表）
├── clipwriter.py        # 剪贴板写回（文本 CF_UNICODETEXT；图片 CF_DIB+CF_BITMAP+PNG 多格式）
├── storage.py           # SQLite 存储层：建表/迁移/插入/去重/列表搜索/置顶/删除/分组/向量
├── watcher.py           # 剪贴板采集主循环（0.5s 轮询、图片优先、独占重试）
├── ai_client.py         # 可选 AI：分类/向量化/智能分组/余弦相似度/后台队列（未配置自动降级）
├── cleanup.py           # 历史上限清理：未分组内容每周自动清（分组永久保留）+ 手动清理
├── updater.py           # 自动更新：检查 GitHub Release / 后台下载 / 重启热替换（仅打包版）
├── config.py            # 配置：.env 加载 + CLIPVAULT_* 环境变量 + 数据目录策略（打包版 %LOCALAPPDATA%）
├── tray.py              # 托盘常驻版（采集 + GUI 一体，需 pystray）
├── autostart.py         # 开机自启动管理（HKCU Run 键，无需管理员权限）
├── make_icon.py         # 生成 assets/ 图标（托盘 + exe）
├── tests/               # pytest 测试（172 个用例，含真实 GUI 冒烟）
│   ├── conftest.py      #   隔离数据目录 + 每用例清库 + 默认关闭 AI + 队列排空
│   ├── test_config.py   #   设置文件读写/优先级/.env/数据目录策略/旧数据迁移/版本同步
│   ├── test_storage.py  #   建表/迁移/去重/列表搜索/置顶/删除/编辑命名/向量
│   ├── test_watcher.py  #   哈希规则/命名/入库/去重/孤儿文件清理
│   ├── test_ai_client.py#   降级/解析/相似度/队列/厂商预设/拉取模型/陈旧任务
│   ├── test_groups.py   #   分组：CRUD/成员多对多/列表过滤/计数/AI 解析分配
│   ├── test_cleanup.py  #   周清理：到期判断/删未分组留分组/删图片向量/手动与钩子
│   ├── test_updater.py  #   自动更新：版本比较/release 解析/下载/热替换脚本
│   ├── test_clipwriter.py#  格式转换 + QQ/微信多格式剪贴板（真实剪贴板验证）
│   └── test_gui.py      #   真实 tkinter 窗口冒烟（渲染/筛选/置顶/删除两步/编辑保存/设置窗/命中）
├── assets/              # 图标（make_icon.py 生成）
├── packaging.spec       # PyInstaller 打包配置
├── pyproject.toml       # 项目元数据 / 依赖 / 命令行入口 / ruff / pytest
├── requirements.txt     # 运行时依赖
├── LICENSE              # MIT 协议
├── .env.example         # 环境变量模板（复制为 .env 使用）
├── .gitignore           # 忽略数据目录 / 虚拟环境 / 缓存 / .env
└── clipboard_data/      # 【运行时生成】数据库与图片，已被 .gitignore
    ├── clipboard.db
    └── images/
        ├── 20250101_120000_123456.png          # 原图
        └── thumb_20250101_120000_123456.png   # 200x200 缩略图
```

## 💻 环境要求

- Windows 10/11（依赖 Win32 剪贴板 API，其他平台后续扩展）
- Python 3.12+（自带 tkinter；官方安装包默认包含）

## 🚀 快速开始

```powershell
# 1. 获取代码
git clone https://github.com/wb497516281-cyber/ClipVault.git
cd ClipVault

# 2. 建议使用虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 3. 安装依赖（二选一）
pip install -r requirements.txt
pip install -e .            # 开发模式安装，附带 clipvault 命令
```

## ▶️ 运行

### 方式一：窗口模式（日常使用）

```powershell
python gui.py        # 或安装后直接：clipvault
```

一个窗口搞定：后台采集 + 界面展示。关闭窗口即退出（采集随之停止）。

```powershell
python gui.py --no-watch   # 只看界面不采集（调试用）
```

### 方式二：托盘常驻（推荐全天开机）

```powershell
pip install -e ".[tray]"   # 安装托盘依赖
python tray.py             # 或 clipvault-tray
```

托盘菜单：打开界面 / 暂停·恢复采集 / 开机自启动开关 / AI 状态与向量补建 / 退出。**关闭窗口只是隐藏到托盘**，采集继续。

### 开机自启动

```powershell
python autostart.py install    # 写入 HKCU 运行项（免管理员权限）
python autostart.py status     # 查看状态
python autostart.py remove     # 移除
```

### 使用方法

| 操作 | 方式 |
|---|---|
| 查看历史 | 打开窗口，最新条目在最上方，每 5 秒自动刷新 |
| 搜索 | 顶部搜索框输入关键词（300ms 防抖，无需回车；命中**内容 / 命名 / 来源应用**）|
| 切换检索模式 | 顶栏「智能 / 关键词 / 语义」（配置 AI 后才显示；智能=关键词命中 + 语义增量）|
| 筛选类型 | 「全部 / 文本 / 图片」|
| **分组浏览** | 左栏点分组名 / 「未分组」，只看该组内容 |
| **手动分组** | 悬停卡片 → 「分组」→ 勾选加入/移出（可多选）、＋新建分组、✨AI 建议本条去哪组 |
| **AI 自动分组** | 左栏「🤖 AI 自动分组」（或托盘菜单）：后台把未分组条目分批交给分类模型归组，跑完汇报新建几个组、入组几条 |
| **🧹 清理未分组** | 左栏「🧹 清理未分组」按钮：二次确认后立即清理；**每周也会自动清理一次**（未分组删、分组留） |
| 分组管理 | 左栏右键分组 → 重命名 / 删除（删组不删条目）；底部「＋ 新建分组」|
| 配置 AI | 顶栏「AI 设置」按钮（托盘模式也可从托盘菜单进入）|
| 自动更新 | 启动时自动检查；有新版本弹窗确认后重启热替换；托盘菜单「检查更新…」可手动触发 |
| 编辑条目 | 悬停卡片 → 「编辑」按钮（改文本内容 / 命名）|
| 复制回剪贴板 | **点击卡片**（可直接粘贴到 QQ/微信）|
| 置顶 / 取消 | 鼠标悬停卡片 → 右上角「置顶」按钮 |
| 删除 | 悬停卡片 → 「删除」按钮（需点两次确认）|

## 🤖 AI 配置（可选，界面直接配）

**打开窗口顶栏的「AI 设置」按钮**（托盘模式在托盘菜单里也有入口），直接填写：

| 界面项 | 说明 | 默认 |
|---|---|---|
| 启用 AI 功能 | 总开关，关掉后全部走关键词搜索 | 开 |
| API Key | 只存本机 `clipboard_data/settings.json`，不硬编码、不进仓库 | 无 |
| 接口地址 Base URL | 任何 OpenAI 兼容服务（官方 / 代理 / 本地 vLLM / Ollama）| `https://api.openai.com/v1` |
| 分类模型 / 向量模型 | 按服务商选 | `gpt-4o-mini` / `text-embedding-3-small` |
| 候选分类（逗号分隔） | 用于新文本自动打标签 | 链接、代码、命令、邮箱电话、地址、账号凭证、笔记、其他 |
| 请求超时（秒） | 单次请求超时 | `10` |

填完点「保存」**即时生效**（新复制的内容自动分类、语义搜索可用）；「测试连接」可立刻验证 Key 和网络是否通——**按当前配置自检**：配了向量模型测嵌入接口（返回维度），没配向量模型（DeepSeek / Moonshot 等无 embedding 的厂商）自动改测对话接口，失败时带上 HTTP 错误码等底层原因；「恢复默认」清除界面配置，回到「环境变量 + 默认值」行为。

**厂商预设**：设置窗顶部下拉选择厂商，自动填好 Base URL 和默认模型：

| 预设 | Base URL | 默认分类模型 | 默认向量模型 |
|---|---|---|---|
| OpenAI | api.openai.com/v1 | gpt-4o-mini | text-embedding-3-small |
| DeepSeek | api.deepseek.com/v1 | deepseek-chat | （无向量接口）|
| 通义千问 | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus | text-embedding-v3 |
| 智谱 GLM | open.bigmodel.cn/api/paas/v4 | glm-4-flash | embedding-2 |
| Moonshot Kimi | api.moonshot.cn/v1 | moonshot-v1-8k | （无向量接口）|
| 硅基流动 | api.siliconflow.cn/v1 | Qwen/Qwen2.5-7B-Instruct | BAAI/bge-m3 |
| Ollama 本地 | localhost:11434/v1 | qwen2.5 | nomic-embed-text |
| 自定义 | 手填 | 手填 | 手填 |

> Ollama 等本机服务（`http://localhost` / `http://127.0.0.1`）无需 API Key；云端厂商必须填 Key。向量模型为空的厂商（DeepSeek、Moonshot）语义搜索不可用，其余功能正常。

**拉取模型**：填好 URL 和 Key 后点 Base URL 行尾的「拉取模型」，会自动请求 `GET /models` 把该账号可用模型填进「分类模型 / 向量模型」下拉框（也可继续手输）。只配了分类模型没有向量模型时，语义搜索不可用，其余功能正常。

**优先级**：界面设置（settings.json）> 环境变量 / `.env` > 内置默认值。界面是最近一次显式操作，当场生效；想用环境变量锁定配置，清空界面设置即可。

> AI 分组（自动分组 / AI 建议）与自动分类共用「分类模型」，不需要向量接口；未配 Key 时分组按钮自动置灰，纯本地功能不受影响。每批最多 40 条、每批最多新建 8 个分组，某批请求失败只丢该批，其余结果照常生效。

> 备选：也可以 `copy .env.example .env` 用环境变量配置（适合脚本/部署场景），效果等同。

## 🗄️ 数据说明

- **数据库**：`clipboard_data/clipboard.db`，WAL 模式，采集线程写、界面读并发不阻塞
- **数据目录位置**：打包版在 `%LOCALAPPDATA%\ClipVault\data`（exe 外面，重建/覆盖更新都不碰它，旧版 exe 旁的数据首次启动自动迁移）；源码版在 `<项目根>/clipboard_data`；`CLIPVAULT_DATA_DIR` 可强制指定
- **AI 设置**：`clipboard_data/settings.json`（界面「AI 设置」保存的 API 配置，仅存本机，已被 gitignore）
- **清理状态**：`clipboard_data/cleanup_state.json`（记录上次清理时间与删除条数；**不放 settings.json**——那个文件由 AI 设置窗整体覆写会被冲掉）
- **图片**：`clipboard_data/images/` 下原图 + `thumb_` 前缀缩略图；数据库只存**相对数据目录**的路径（如 `images/thumb_xxx.png`），数据目录整体搬家后路径依然有效
- **去重**：`content_hash` 字段带 UNIQUE 约束；`watcher.last_hash` 记忆最近一次内容，双重保险
- **语义向量**：`clip_vectors` 表，float32 小端 BLOB（**是文本向量，不是图片**；图片仍然只存路径）
- **删除**：删记录即删文件与向量，不留孤儿

```sql
-- clipboard_items 表（storage.py 中定义，老库启动时自动补列）
clipboard_items(
  id, content_type,        -- 'text' | 'image'
  text_content,            -- 文本内容（图片行 NULL）
  image_path,              -- 原图相对路径（文本行 NULL）
  thumbnail_path,          -- 缩略图相对路径
  content_hash,            -- SHA-256，去重依据（UNIQUE）
  source_app,              -- 来源应用（前台窗口标题）
  created_at,              -- 'YYYY-MM-DD HH:MM:SS'
  is_pinned, pinned_at,    -- 置顶状态与时间
  category,                -- AI 分类标签（未配置 AI 时为 NULL）
  title                    -- 用户命名（编辑窗设置）
)

-- clip_vectors 表：item_id 对应 clipboard_items.id，仅文本有条目
clip_vectors(item_id, model, dim, vector BLOB, updated_at)

-- 分组（多对多）：一条记录可同时属于多个分组；删分组只删成员关系，不动条目
clip_groups(id, name UNIQUE, position, created_at)
clip_group_members(group_id, item_id, added_at, PRIMARY KEY(group_id, item_id))
```

## ⚙️ 配置

| 配置项 | 位置 / 变量 | 默认 |
|---|---|---|
| 数据目录 | `CLIPVAULT_DATA_DIR` | 打包版 `%LOCALAPPDATA%\ClipVault\data`；源码版 `<项目根>/clipboard_data` |
| 未分组清理周期 | `CLIPVAULT_CLEANUP_DAYS` | 7 天（每周） |
| 轮询间隔 | `watcher.py` → `POLL_INTERVAL` | 0.5 秒 |
| 剪贴板重试 | `watcher.py` → `OPEN_RETRIES`/`OPEN_RETRY_DELAY` | 3 次 × 50ms |
| 缩略图尺寸 | `watcher.py` → `THUMBNAIL_SIZE` | 200×200 |
| 界面自动刷新 | `gui.py` → `AUTO_REFRESH_MS` | 5 秒 |
| 文本预览长度 | `gui.py` → `TEXT_PREVIEW_CHARS` | 300 字符 |

## 📦 打包为 exe

```powershell
pip install -e ".[build]"     # 安装 pyinstaller
python make_icon.py           # 生成 assets/icon.ico（已生成可跳过）
pyinstaller packaging.spec
```

产物在 `dist/ClipVault/ClipVault.exe`（onedir 模式：启动快、误报率低）。**拷贝时整个 `ClipVault` 文件夹一起拷**（旁边的 `_internal` 是运行时依赖）。双击即启动托盘 + 采集 + 窗口，无需安装 Python。**数据不在这个文件夹里**（在 `%LOCALAPPDATA%\ClipVault\data`），重新打包/覆盖更新都不会丢数据。排障时可把 `packaging.spec` 里 `console=False` 临时改 `True` 看日志。

## 🧪 运行测试

```powershell
pip install -e ".[dev]"
pytest                        # 172 个用例，无需网络；GUI 用例需要桌面环境
ruff check .                  # 代码规范检查
```

测试覆盖：SQLite 建表/老库迁移/去重/列表搜索/置顶删除/向量存取、哈希规则与文件命名/孤儿文件清理、AI 降级与解析/队列、剪贴板格式转换与 QQ/微信 多格式落地（真实剪贴板）、**真实 tkinter 窗口冒烟**（渲染/筛选/两步删除/复制动作不崩溃）。测试用独立临时数据目录，不碰真实数据。

## 🔒 安全说明

- 纯本地进程，无网络服务、无端口监听，不存在被网页读取的可能
- 图片访问有路径校验，数据库中的路径必须位于 `clipboard_data/images/` 内
- 无任何硬编码密钥；AI API Key 只从环境变量 / `.env` 读取
- 剪贴板历史属于敏感数据，`clipboard_data/` 与 `.env` 均已加入 `.gitignore`

## ❓ 常见问题

**Q：复制出来的内容能粘贴到 QQ/微信吗？**
能。文本以 `CF_UNICODETEXT` 写入；图片会同时写入 `CF_DIB`、`CF_BITMAP` 和注册格式 `PNG`/`image/png`——QQ 读 DIB，微信/企业微信优先读 PNG，各取所需。若复制时提示「剪贴板被其他程序占用」，稍等再试。

**Q：托盘模式关掉窗口后程序还在吗？**
在。托盘模式下关闭窗口 = 隐藏到托盘继续采集；要退出请用托盘菜单「退出」。纯窗口模式（`gui.py`）关窗即退出。

**Q：界面空白/没有记录？**
确认采集器在运行（窗口模式默认自动拉起；托盘模式见托盘图标）。刚启动还没有历史时会显示「暂无剪贴板记录」。

**Q：截图/图片复制不进来？**
确认 pywin32、Pillow 已安装；部分程序（Office、远程桌面）会临时独占剪贴板，采集器会自动重试。

**Q：换了机器/目录，历史记录还在吗？**
历史跟着**数据目录**走：打包版把 `%LOCALAPPDATA%\ClipVault\data` 整个拷到新机器同位置即可（库里的图片路径是相对路径，放哪都有效）；源码版拷 `<项目根>/clipboard_data`。程序本身随便搬，数据不受影响。

## 🗺️ Roadmap

- [x] 原生桌面 GUI（tkinter Canvas）
- [x] AI 分类（自动打标签）与语义搜索
- [x] 分组：AI 自动分组 + 手动分组（左栏筛选 / 卡片菜单 / 分组管理）
- [x] 历史上限与自动清理策略（未分组内容每周清理，分组内容永久保留）
- [x] 系统托盘图标 / 暂停采集 / 开机自启
- [x] 打包为独立 exe（PyInstaller）
- [x] 自动化测试（pytest，含 GUI 冒烟）
- [ ] 跨平台支持（macOS/Linux 剪贴板后端）

## 📄 开源协议

[MIT](LICENSE) © 2026 ClipVault contributors

## 🙏 致谢

[pywin32](https://github.com/mhammond/pywin32) · [Pillow](https://python-pillow.org) · [pystray](https://github.com/moses-palmer/pystray) · [PyInstaller](https://pyinstaller.org)
