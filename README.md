# ZCode 文档内网镜像

把 [zcode.z.ai/cn](https://zcode.z.ai/cn) 整个中文站点（首页 + 26 篇文档 + 20 个版本更新日志 + 隐私/条款）做成**离线镜像**，在内网以 Docker 形式部署，自带一个**接入本地大模型的 AI 问答助手**，并提供一套 **USB 摆渡**的同步更新方案。

```
外网机                        USB 摆渡              内网(Docker)
┌─────────────────┐         ┌──────────┐         ┌───────────────────────┐
│ collector/      │ .zdoc   │          │ import  │ nginx(静态镜像+AI浮窗) │
│ 爬取/提取/打包   │ ──────► │ U 盘     │ ──────► │ + FastAPI(RAG+LLM代理) │
└─────────────────┘         └──────────┘ 升级    └───────────────────────┘
```

## 能做什么

- 🪞 **全站保真镜像**：首页、文档、更新日志、隐私/条款全部离线可访问，样式/图片/字体原样保留。
- 🤖 **AI 问答助手**：每个页面右下角浮动入口，基于本站文档回答问题并给出来源（默认 BM25 关键词检索，可切换向量检索）。
- 💻 **本地大模型接入**：通过 OpenAI 兼容接口对接 Ollama / vLLM / Xinference / LM Studio 等本地部署的模型。
- 🪟 **仅 Windows 安装包**：内网镜像只打包最新版 Windows x64 + ARM64 安装包（Mac/Linux 按钮置灰提示）。
- 🔒 **安全摆渡**：`.zdoc` 包带 `manifest.json` + 全文件 SHA-256，内网导入时逐文件校验，损坏/被篡改会中止。
- 🔁 **可重复升级**：`import.sh` 自动比对版本号、原子替换、备份旧版、重建 AI 索引；同版本自动跳过。

---

## 目录结构

```
zcode-mirror/
├── collector/          # ① 外网侧：爬取 + 提取 + 打包（Python，需联网）
│   ├── config.py           #   抓取清单(30 路由) + 常量
│   ├── crawler.py          #   爬取 + 重写 + 下载资源
│   ├── rewriter.py         #   HTML/URL 离线重写（含 _next/image 优化器还原）
│   ├── extract_markdown.py #   docs + changelog → markdown（供 RAG）
│   ├── patch_offline.py    #   删 GTM、改写下载链接、中和客户端路由
│   ├── inject_widget.py    #   注入 AI 浮窗
│   ├── relativise.py       #   站内绝对路径转相对（双击 file:// 也能打开）
│   ├── download_releases.py#   下载最新 Windows 安装包
│   ├── manifest.py         #   生成 manifest.json + SHA256SUMS
│   ├── export.py           #   主入口：串联全流程 → .zdoc
│   └── requirements.txt
├── site-overlay/       # ② 注入到镜像站的 AI 浮窗组件（纯 JS/CSS，零依赖）
│   ├── chat-widget.js
│   └── chat-widget.css
├── intranet/           # ③ 内网侧：Docker 部署 + 导入升级
│   ├── docker-compose.yml
│   ├── import.sh           #   导入/升级脚本
│   ├── .env.example        #   本地模型配置模板
│   ├── web/                #   nginx：静态托管 + /api 反代 + /releases
│   └── ai/                 #   FastAPI：BM25/向量检索 + LLM 代理 + 管理后台
│       ├── rag.py              #   检索（默认 BM25，可选向量）
│       ├── llm.py              #   OpenAI 兼容客户端（支持热重载）
│       ├── app.py              #   /api/ask /api/health /api/reindex /api/admin/*
│       ├── config_store.py     #   管理后台配置持久化（llm.json/admin.json）
│       ├── admin_auth.py       #   管理后台登录会话
│       ├── importer.py         #   .zdoc 导入（Python 版 import.sh）
│       ├── skills_store.py     #   局域网 Skill 共享库存储
│       ├── admin/              #   管理后台静态页（/admin）
│       └── skills/             #   公开技能库静态页（/skills）
│   └── skills-seed/        #   手工导入技能的示例/暂存目录说明
├── Makefile            # 便捷命令
└── README.md
```

---

## 完整使用流程

### 一、外网机：打包 `.zdoc`

> 在**能访问 zcode.z.ai** 的机器上执行。

```bash
cd zcode-mirror

# 1. 安装依赖（Python 3.9+）
make deps

# 2. 打包（含最新 Windows 安装包，约 200MB）
make collect
#   或：不想下载安装包，只要文档 → make collect-fast

# 产物：dist/zcode-docs-<版本>-<日期>.zdoc
ls dist/
```

`make collect` 内部依次执行（可单独 `python -m collector.<step>` 调用）：
1. **crawl** — 抓取 30 个 `/cn` 路由，重写为离线可用，下载全部第一方资源
2. **extract** — 文档/更新日志转 markdown（给 AI 用）
3. **patch** — 删 Google 分析、改写下载链接为本地、中和 Next.js 客户端路由、写登录占位页
4. **inject** — 注入 AI 浮窗
5. **relativise** — 把站内绝对路径（`/_next/...`、`/_zc/...`、`/images/...`、`/cn/...`）转成相对路径，这样**双击 HTML 用 `file://` 打开也能正常显示**（不止 nginx 部署）
6. **releases** — 下载最新 Windows x64 + ARM64 安装包及 `latest.yml`
7. **manifest** — 生成 `manifest.json` + `SHA256SUMS`
8. **package** — 打成单个 `.zdoc`（tar.gz）

### 二、拷贝到内网

把 `dist/*.zdoc` 复制到 U 盘 / 移动介质，再拷进内网机。
> 包内带全文件 SHA-256，拷贝损坏会被检测出来。

### 三、内网机：部署 + 导入

> 在**内网**（已装 Docker）的机器上执行。

```bash
cd zcode-mirror/intranet

# 1. 配置本地大模型
cp .env.example .env
#    编辑 .env，至少改 LLM_BASE_URL 和 LLM_MODEL
#    （例如 Ollama:  LLM_BASE_URL=http://host.docker.internal:11434/v1
#                    LLM_MODEL=qwen2.5:7b ）

# 2. 导入 .zdoc（校验完整性 → 版本比对 → 原子替换 → 备份旧版）
./import.sh /path/to/zcode-docs-3.6.5-20260808.zdoc

# 3. 启动服务
docker compose up -d --build
#    或在仓库根目录:  make up

# 4. 浏览器打开
open http://<内网机IP>:8080
```

打开后即可看到完整的镜像站点；右下角有 **AI 问答** 浮动按钮，点开即可提问。

### 四、后期更新（软件发新版本后）

ZCode 发版后，在**外网机**重新 `make collect` 得到新版 `.zdoc` → 拷进内网 → `./import.sh <新包>`。

`import.sh` 会：
- 校验新包完整性
- 比对版本号：相同则跳过，不同则备份旧版、原子替换新版
- 自动触发 AI 容器重建检索索引

---

## 配置说明（`.env`）

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | ✅ | 本地模型的 OpenAI 兼容地址（到 `/v1`）。Ollama 默认 `http://host.docker.internal:11434/v1` |
| `LLM_MODEL` | ✅ | 模型名（需与本地服务一致），如 `glm-4-flash`、`qwen2.5:7b` |
| `LLM_API_KEY` |  | 本地服务需要鉴权才填 |
| `LLM_TEMPERATURE` |  | 默认 `0.3` |
| `LLM_MAX_TOKENS` |  | 默认 `1024` |
| `EMBEDDINGS_BASE_URL` |  | 配置后启用**向量检索**（默认 BM25，对几十篇文档足够且零依赖） |
| `EMBEDDINGS_MODEL` |  | 向量模型名，如 `bge-large-zh` |
| `WEB_PORT` |  | Web 端口，默认 `8080` |

> AI 容器通过 `host.docker.internal` 访问宿主机上跑的模型（docker-compose 已配置 `extra_hosts`）。

---

## 管理后台

部署完成后，浏览器打开 **`http://<内网机IP>:8080/admin`**，默认账号 **`admin` / `admin`**（首次登录后请到「账户」页修改）。

后台提供多个页面，**无需手改 `.env`、无需手动跑 `import.sh`**：

| 页面 | 能做什么 |
|---|---|
| **概览** | 当前内容版本、检索后端/片段数/文档数、当前模型地址与连通性、旧版备份状态 |
| **模型配置** | 在线修改 LLM 服务地址 / 模型名 / API Key / 温度等，**保存即热生效**（写入 `config/llm.json`，不重启容器）；「测试连接」会探测 `/v1/models` 并列出可用模型 |
| **内容导入** | 上传 `.zdoc` → 自动 **SHA-256 校验 → 版本比对 → 原子替换 + 备份旧版 → 重建检索索引**；失败自动回滚，并写入升级历史（与 `import.sh` 一致） |
| **技能库** | 管理员上传 / 删除局域网共享 Skill（zip 或粘贴 `SKILL.md`）；持久化在 `config/skills/`，不随文档包升级清空 |
| **账户** | 修改管理员用户名 / 密码 |

### 局域网 Skill 共享库

- **公开页**（只读下载）：`http://<内网IP>:8090/skills`
- **管理上传**：登录 `/admin` →「技能库」；或把技能目录拷到 `intranet/config/skills/<name>/`（可先放进 [`intranet/skills-seed/`](intranet/skills-seed/) 再复制）
- 同事下载 zip 后解压到本机 `~/.zcode/skills/`，在 ZCode「设置 → 技能」刷新启用

> 配置、账户与升级记录持久化在 `intranet/config/`（独立卷），**导入新内容不会覆盖它**。旧数据导入后自动备份到 `intranet/data.backup/`；升级失败会自动回滚，也可在管理后台「内容导入」一键回滚到上一版。

> CLI 方式仍然可用：`./import.sh <package>.zdoc` 与管理页导入走的是同一套校验/替换逻辑，互不冲突。

---

## 验收检查清单

部署完成后逐项确认：

- [ ] `http://<IP>:8080` 能打开首页，hero 文案「简单、迅捷、氛围十足」正常
- [ ] 左侧/顶部导航点击各文档页面，**所有链接可到达**、图片正常显示
- [ ] 「更新日志」页能看到全部 20 个版本
- [ ] 下载区 Windows 按钮点击可下载本地 `.exe`；Mac/Linux 按钮已置灰
- [ ] 右下角 **AI 问答** 按钮可展开面板
- [ ] 提问「如何安装 ZCode」「什么是 Agent」能得到带来源的回答
- [ ] `http://<IP>:8080/admin` 能打开管理后台，admin/admin 登录成功
- [ ] 管理后台「模型配置」保存后「测试连接」显示可用模型
- [ ] 管理后台「内容导入」上传同版本 `.zdoc` 提示「已是最新版本」
- [ ] `curl http://<IP>:8080/api/health` 返回 `{status:ok, retriever:{...}}`
- [ ] `http://<IP>:8080/skills` 能打开技能共享库；管理员在后台「技能库」上传后列表可见并可下载
- [ ] 重新 `./import.sh` 同版本会提示「无需升级」

---

## 常见问题

**Q: AI 回答时报「本地大模型暂不可用」？**
A: 检查 `.env` 的 `LLM_BASE_URL` / `LLM_MODEL` 是否正确，模型服务是否在运行，内网能否访问该地址。`curl http://<IP>:8080/api/health` 可查看配置。

**Q: 想要更好的回答质量？**
A: 在 `.env` 配置 `EMBEDDINGS_*` 启用向量检索（如 BGE 模型），并把 `LLM_MAX_TOKENS` 调大。

**Q: 包太大想精简？**
A: 用 `make collect-fast` 跳过 Windows 安装包（约省 200MB），下载按钮会显示「不可用」。

**Q: 已经在内网部署过，想强制重装同版本？**
A: 删掉 `intranet/data/manifest.json` 后再跑 `./import.sh`。

**Q: 升级后想回退？**
A: 旧版自动备份在 `intranet/data.backup/`，把它移回 `intranet/data/` 即可。

**Q: 在外网机打包后想直接预览效果？**
A: 两种方式都行。一是在仓库根目录跑 `make dev-server`，浏览器打开 `http://localhost:8765/cn/`；二是构建已把所有资源路径转成**相对路径**，所以也可以直接双击 `build/site/cn/index.html`（`file://` 方式）打开预览，样式和图片都能正常加载。后者无需任何服务器，最便于快速检查。

---

## 技术要点

- **采集**：zcode.z.ai 是 Next.js SSR，所有页面正文都在原始 HTML 里，无需浏览器渲染；更新日志用 `?page=N` 分页（page=3 为累积全量 20 版本）。
- **离线重写**：把 Next.js 的 `/_next/image?url=...` 图片优化器 URL 还原成原始文件路径并下载；中和客户端路由避免离线 404；删除 Google Tag Manager。
- **AI 检索**：默认纯 BM25（自实现，零依赖，对中文做了单字切分）；可选切换 OpenAI 兼容 embeddings + 余弦相似度。
- **摆渡安全**：`.zdoc` 内嵌 `SHA256SUMS`，导入时逐文件校验；版本号比对避免重复升级；原子替换 + 自动备份保证可回退。
