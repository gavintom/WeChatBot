# WeChatBot · Docker 部署详细指南

> 适用版本：**V1.1.2**（`admin/web.py` 的 `APP_VERSION`）
> 官方镜像：`gavintom/wechatbot:1.1.2` · 体积约 **2.37 GB**（以镜像仓库显示为准）
> 与仓库根目录 [`deploy.md`](../deploy.md) 互补，本文偏「照着做」。
> 文中 `192.0.2.x` 与 `/opt/wxbot/data` 均为示例地址（RFC 5737 文档网段 / 示例目录），部署时请换成实际 IP 与路径。

---

## 0. 先记住这六件事

| # | 要点 | 为什么重要 |
|---|---|---|
| 1 | **一个容器跑两个服务** | `docker-entrypoint.sh` 同时拉起管理端（`7780`）与主服务（`29999`），任一进程退出容器即结束，由重启策略接管。后台会显示主服务「由外部启动」，**不要在后台点停止**。 |
| 2 | **只有 `/app/data` 需要持久化** | 配置、两个数据库、日志、知识库文档、插件私有配置、配置备份全在这一个目录；镜像里不带真实密钥与业务文档。 |
| 3 | **镜像不含业务知识库文档** | `.dockerignore` 排除了 `data/knowledge/*.md` / `*.txt`。首次启动自带 **`README.md` 一条初始文档**（自动入库并建索引）；其余业务文档必须自行导入，否则 AI 检索不到你的业务内容。 |
| 4 | **手机要能主动访问服务器** | webhook 是手机插件 POST 到 `29999`，不是服务器去连手机；两者必须网络互通，且 `server.public_base_url` 要填手机能到达的地址。 |
| 5 | **首次启动有预热** | 内置权重模型首次建索引约 40~90 秒，期间 `/healthz` 可能较慢；管理端 `/login` 通常几秒内即可访问。属正常现象，不要反复重启。 |
| 6 | **直接拉官方镜像，不用打包** | `docker pull gavintom/wechatbot:1.1.2` 即可启动；`docker build` 只在你改了源码或要精简依赖时才需要（见 §3.1）。 |

---

## 1. 部署架构

```
┌─────────────────── Docker 容器 wxbot ───────────────────┐
│  docker-entrypoint.sh                                   │
│   ├─ python -m admin.app          → :7780  Web 管理端    │
│   └─ python webhook_server.py     → :29999 主服务        │
│        /webhook · /healthz · /httpapi/query             │
│                                                         │
│  /app/data  ← 唯一持久化卷（VOLUME）                     │
│    config.yaml · analytics.db · chatlog.db · logs/      │
│    knowledge/ · plugins/ · .config_backup/               │
└─────────────────────────────────────────────────────────┘
        ▲ 7780 后台（内网）   ▲ 29999 webhook（仅可信网络）
        │                     │
     运维浏览器           iOS 微信 + WeChatBotTweak
                          （HTTP 端口通常 18888）
```

**官方镜像内部构成**（已预构建，无需自己打包；`Dockerfile` 供从源码构建时参考）：

- 多阶段构建：第一阶段装 `gcc` 把 **pilk**（微信 SILK_V3 语音解码，PyPI 只有 Windows wheel）编成 Linux wheel；最终镜像只拷产物并在构建期 `import pilk` 校验。
- 默认 `INSTALL_VECTOR=1`：从 PyTorch CPU-only 源装 `torch` + `sentence-transformers`，避免拉整套 CUDA 把镜像撑到数 GB。
- **权重模型 `bge-base-zh-v1.5`（约 390MB）随镜像分发**，已内置在 `/app/models/bge-base-zh-v1.5`，配置用相对路径 `./models/bge-base-zh-v1.5`，首次启动不联网下载；构建期 `test -f` 校验其存在。
- **重排模型 `bge-reranker-base` 不含在镜像内**，需要重排时自行下载放到本地 `models/bge-reranker-base` 后再构建；未放置则自动跳过重排。下载方式（huggingface-cli / ModelScope）见 [README §6「模型与下载」](../README.md#6-语音与检索)。
- 配置模板与知识库/插件骨架另存到**非卷路径** `/app/.config-template/`、`/app/.knowledge.default/`、`/app/.plugins.default/`，因为 `/app/data` 运行时会被卷覆盖。
- `.dockerignore` 排除了 `config.yaml`、`*.db`、`.admin_secret`、日志、业务文档与向量缓存，**镜像里没有真实密钥**。

---

## 2. 环境准备

### 2.1 宿主机

| 项 | 建议 | 说明 |
|---|---|---|
| Docker | Engine 24+ / Docker Desktop 4.x | 需要 `docker pull`、`docker run`、`docker compose`；只有从源码构建才用 `docker build` |
| CPU / 内存 | 2 核 / **≥ 4 GB** | 向量模型常驻内存几百 MB；纯 BM25 精简镜像可降到 2 GB |
| 磁盘 | **≥ 8 GB 可用** | 镜像约 2.37 GB + 数据卷与日志 |
| 网络 | 拉取镜像时可访问 Docker Hub（国内可配镜像加速） | 运行期模型离线；LLM 与手机插件需互通 |
| 端口 | `7780`、`29999` 未被占用 | `netstat -ano \| findstr "7780 29999"`（Windows）或 `ss -ltnp \| grep -E '7780\|29999'`（Linux） |

### 2.2 外部依赖

| 依赖 | 必需？ | 说明 |
|---|---|---|
| iOS 微信 + **WeChatBotTweak v1.2.7** | 必需 | 消息桥接插件，本项目只按它的 HTTP 协议对接，不修改插件 |
| OpenAI 兼容大模型端点 + API Key | 必需 | 可在后台「AI 大模型」页纳管多个并切换默认；群聊 / 私聊可各选一个模型 |
| ASR 语音识别端点 | 可选 | 不用语音就在 `config.yaml` 里设 `voice.enabled: false` |
| nginx / 域名 / 证书 | 可选 | 见 §9 反向代理 |

### 2.3 检查命令

```bash
docker version
docker info | grep -i -E "server version|storage driver"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18888/api/health   # 手机插件是否可达（IP 换成实际地址）
```

---

## 3. 获取官方镜像（无需本地打包）

推荐直接用发布镜像，**跳过 `docker build`**：

```bash
docker pull gavintom/wechatbot:1.1.2
```

确认拉取结果；也可以顺手验证镜像内关键依赖与离线模型：

```bash
docker images gavintom/wechatbot --format "{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}} {{.CreatedSince}}"

docker run --rm --entrypoint python gavintom/wechatbot:1.1.2 -c \
  "import pilk, flask, rank_bm25; import os; print('OK'); print('model:', os.path.isfile('models/bge-base-zh-v1.5/config.json'))"
```

> 不带 tag 默认取 `latest`；生产环境建议固定到 `1.1.2` 这类明确版本，升级时只改 tag。
> 镜像已内置权重模型，运行期无需下载；只有自行下载重排模型时国内才需要
> `-e HF_ENDPOINT=https://hf-mirror.com`。

### 3.1 可选：从源码自行构建

只有改了源码、或需要纯 BM25 精简镜像时才需要这一步。仓库不含模型权重，**从源码构建默认镜像
前必须先把权重模型下载到 `models/bge-base-zh-v1.5`**（下载方式见 README §6），否则构建期的
`test -f /app/models/bge-base-zh-v1.5/config.json` 会直接失败；需要重排时同样把
`bge-reranker-base` 下载到 `models/`。

```bash
git clone <你的仓库地址>.git
cd <仓库目录>

# 默认镜像：含 CPU-only torch + sentence-transformers + 本地 models/ 权重
docker build -t gavintom/wechatbot:1.1.2 .

# 精简镜像（纯 BM25，不装 torch）
docker build --build-arg INSTALL_VECTOR=0 -t gavintom/wechatbot:1.1.2-bm25 .
```

精简镜像下 `brain.py` 对缺失依赖有兜底，会自动降级、不会启动失败；但**配置模板仍默认开启向量**，
建议在后台「系统配置」把下面这项关掉，避免每次启动做无谓尝试：

```yaml
brain:
  retrieval:
    use_vector: false
```

（重排默认就是关闭的 `rerank.enabled: false`，无需处理。）

构建只在这一步需要外网（pip 安装依赖、编译 pilk）。国内环境可按需设置代理：

```bash
docker build \
  --build-arg HTTP_PROXY=http://192.0.2.2:7890 \
  --build-arg HTTPS_PROXY=http://192.0.2.2:7890 \
  -t gavintom/wechatbot:1.1.2 .
```

---

## 4. 首次启动

### 4.1 方案 A：命名卷（Linux / 服务器推荐）

```bash
docker run -d --name wxbot \
  --restart unless-stopped \
  -p 7780:7780 -p 29999:29999 \
  -v wxbot-data:/app/data \
  gavintom/wechatbot:1.1.2
```

### 4.2 方案 B：绑定宿主机目录（本机开发 / 本项目当前 Windows 部署方式）

```bash
# Git Bash / Docker Desktop on Windows
docker run -d --name wxbot \
  --restart unless-stopped \
  -p 7780:7780 -p 29999:29999 \
  -v "C:/opt/wxbot/data:/app/data" \
  gavintom/wechatbot:1.1.2

# Linux / macOS
docker run -d --name wxbot \
  --restart unless-stopped \
  -p 7780:7780 -p 29999:29999 \
  -v "/opt/wxbot/data:/app/data" \
  gavintom/wechatbot:1.1.2
```

> Windows PowerShell 里路径写成 `C:\opt\wxbot\data:/app/data` 亦可；若用 Git Bash，请用上面的正斜杠写法并保留引号。
> 若出现挂载后目录为空、权限报错，先确认 Docker Desktop 已共享该磁盘（Settings → Resources → File Sharing）。

### 4.3 两种卷的取舍

| | 命名卷 `wxbot-data` | bind mount `/opt/wxbot/data` |
|---|---|---|
| 直接查看/编辑文件 | 需 `docker cp` 或临时容器 | 直接在宿主机打开 |
| 备份 | `docker run --rm -v ... tar` | 直接打包目录 |
| 迁移 | `docker volume` 导入导出 | 拷贝目录即可 |
| 适合 | 生产服务器 | 本机开发、需要频繁改文档 |

### 4.4 启动参数逐项说明

| 参数 | 作用 |
|---|---|
| `--name wxbot` | 容器名，后续所有运维命令都用它 |
| `--restart unless-stopped` | 宿主机重启后自动拉起；手动 `docker stop` 后不自动启动 |
| `-p 7780:7780` | Web 管理端 |
| `-p 29999:29999` | 主服务：`/webhook`、`/healthz`、`/httpapi/query` |
| `-v ...:/app/data` | 唯一持久化点，**不要改成子目录挂载** |
| `gavintom/wechatbot:1.1.2` | 官方发布镜像，从 Docker Hub 拉取 |

### 4.5 entrypoint 首次启动会做什么

1. `config.yaml` 不存在 → 优先用卷内 `config.yaml.example`，否则用镜像内置副本，生成 `/app/data/config.yaml`；
2. 补齐知识库初始文档 → 从镜像 `/app/.knowledge.default` **按文件**补进 `knowledge/`
   （含 `README.md` 与 `images/` 骨架）；卷里已有的文档一律不覆盖，
   所以用户改过或删掉的文档不会被镜像改动。
   ⚠️ Docker 新建命名卷时会拿镜像里 `/app/data` 的内容「播种」，卷里天生就有
   `knowledge/` 骨架目录 —— 所以判定条件是「文件在不在」，不是「目录在不在」。
3. `plugins/` 不存在 → 初始化插件私有配置目录；
4. 启动两个服务；`store.init_db()` 自动建库、建表，并创建两个系统预置知识库（`默认知识库`、`API接口库`，均 0 篇文档、不可删改）与一条「默认私聊」兜底规则；
5. **群聊配置为空**——不手动加群，群里不会有任何响应。

查看初始化日志：

```bash
docker logs -f wxbot
# 应能看到 [init] 三行与 [run] 数据卷：/app/data | 管理端 :7780 | 主服务 :29999
```

---

## 5. 首次配置（按顺序做）

### 5.1 登录并改密码

浏览器打开 `http://<宿主机IP>:7780`，初始账号 **admin / admin123**，登录后**立刻修改密码**。

### 5.2 在「系统配置」里填最小必要配置

| 配置项 | 示例 | 说明 |
|---|---|---|
| `wechat.base_url` | `http://192.0.2.71:18888` | 手机插件 HTTP 地址 |
| `wechat.self_wxid` | `/api/health` 返回的 `self` | 建议填，换微信号必须改；群聊「@ 本机」判定更稳 |
| `wechat.token` | 插件 `config.plist` 的 token | 插件没设就留空 |
| `brain.external_llm.api_key / base_url / model` | `sk-xxx` / `https://api.deepseek.com/v1` / `deepseek-chat` | 也可在「AI 大模型」页纳管并选默认；每条群聊/私聊配置可单独指定模型 |
| `server.public_base_url` | `http://192.0.2.145:29999` | **手机能访问到的地址**，一键写 webhook 会用它拼接 |
| `webhook.token` | 自定义随机串 | 建议开启，推送地址带 `?token=` |
| `transfer.default_wxid / default_name` | `wxid_zhangsan` / `人工客服` | wxid 不填，群聊转人工不会 @ 人 |
| `voice.*` | — | 不用语音设 `voice.enabled: false` |
| `api.enabled / api.api_key` | `false` / 空 | 要开放 `/httpapi/query` 才设 `true` 并**必填** api_key |
| `chat_log.retention_months` | `3` | 1~12 个月，原始聊天消息自动滚动清理 |

> **生效口径要分清**：
> - 后台「群聊配置 / 私聊配置 / 知识库 / AI 大模型 / 插件」等改的是 SQLite，**保存即生效，无需重启**；
> - 「系统配置」里的 `config.yaml` 是模块级读取，**改完要重启容器**（`docker restart wxbot`）；
> - 例外：「群聊对话延续窗口」保存会文本级写回 `config.yaml` 并检查 mtime，**立即生效，无需重启**。
>
> **每条配置选模型（V1.1.2-beta5）**：`group_rules.llm_id` / `private_rules.llm_id`
> 让**每个群聊配置、每条私聊配置各自指定一个大模型**；不指定则跟随默认。
> 生效顺序为「本条配置指定的模型 > 分场景兜底模型 > 全局默认 > config.yaml」。
> 老库启动时自动补 `llm_id` 列（NULL = 不指定）；选的模型被停用/删除时自动回退默认，
> 列表里标「已停用 / 模型已删除」。
>
> **分场景兜底模型（V1.1.2-beta2，可选）**：群聊 / 私聊各选一个**兜底**模型，
> 只在这条消息命中的配置没有单独指定时才用。每条应答都会在 `data/logs/app-*.log` 打一条
> `AI 应答 | 场景=… | 配置=… | 模型=… | 纳管id=… | 来源=override(本条配置)`，
> 同时把使用的模型写进 `llm_usage`（`llm_id` / `scene`），可在「AI 用量」核对。
>
> **导出 / 导入模型配置（V1.1.2-beta3）**：「AI 大模型」页右上角可把整套配置导成 JSON
> （含默认模型与群聊/私聊场景选择，引用按**模型名称**对齐，跨实例导入会自动映射到本机 id）。
> 导入分「合并（同名覆盖，不删文件外的模型）」和「全量替换（文件外的模型会被删除）」两种；
> 导入前整体校验，任一条不合法则整批拒绝、库内不变。
> 导出**默认含明文 API Key**，文件请当密钥保管；`GET /api/llm-models/export?secrets=0`
> 可导出脱敏版（导入时沿用目标机已有的密钥）。

```bash
docker restart wxbot
```

### 5.3 写入手机插件的 webhook

后台「运行监控 → 把 webhook 地址写入手机插件」一键完成，或手动：

```bash
curl -X POST http://192.0.2.71:18888/api/webhook \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://192.0.2.145:29999/webhook?token=你的token"}'
```

校验：

```bash
curl http://192.0.2.71:18888/api/health          # 看 webhook 字段是否与上面一致、ok:true
curl http://127.0.0.1:29999/healthz               # 主服务 + 插件状态、检索器状态
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7780/login
```

> `/healthz` 在向量模型预热完成前可能连接被拒，用 `--retry-all-errors` 等待：
> `curl -s -o /dev/null -w "%{http_code}\n" --max-time 20 --retry 18 --retry-delay 5 --retry-all-errors http://127.0.0.1:29999/healthz`

### 5.4 导入知识库文档（**必做**）

三种方式任选：

```bash
# 方式 1：bind mount —— 直接把文档拷进宿主机目录
cp *.md /opt/wxbot/data/knowledge/

# 方式 2：命名卷 —— 用 docker cp
docker cp ./产品手册.docx wxbot:/app/data/knowledge/
docker cp ./常见问题.md   wxbot:/app/data/knowledge/
```

方式 3：后台「知识库文档」页 **新建 / 上传 / 导入 Office·PDF**。Word、Excel、PDF 的内嵌图片会被抽到 `data/knowledge/images/` 并在 Markdown 原位插入引用。

然后按这个顺序点一遍：

1. 「通讯录管理 → 从手机同步」；
2. 「群聊配置 → 新建配置」：选群 ID（自动带入群名）、填唤醒词、绑定知识库策略；
3. 「知识库管理 → 编辑」确认该策略确实勾选了刚导入的文档；
4. 「知识库文档 → **重建索引并重载**」（改完文档或图片必须做，否则不进检索）；
5. 页面底部「问答测试」输入一个问题，看召回片段与回答是否符合预期。

### 5.5 真实消息验收

在已配置的群里发 `@机器人 产品保修多久`。预期：群内收到基于知识库的回复并 @ 提问者；后台「对话记录」出现一条 `AI 回复`、`触发方式=@ 本机`；「AI 用量」出现对应 token 记录。

未回复时按 §10 排查。

---

## 6. 日常运维命令速查

| 目的 | 命令 |
|---|---|
| 看状态 | `docker ps --filter name=wxbot --format "{{.Names}} \| {{.Status}} \| {{.Ports}}"` |
| 看日志 | `docker logs -f --tail 100 wxbot` |
| 看容器参数 | `docker inspect wxbot --format '{{json .Mounts}} {{json .HostConfig.PortBindings}} {{.HostConfig.RestartPolicy.Name}}'` |
| 重启（改完 config.yaml） | `docker restart wxbot` |
| 停止 / 启动 | `docker stop wxbot` / `docker start wxbot` |
| 进容器排查 | `docker exec -it wxbot bash` |
| 看数据卷 | `docker exec wxbot sh -c "ls -la /app/data"` |
| 看两个服务探活 | `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7780/login` + `curl http://127.0.0.1:29999/healthz` |
| 卷占用 | `docker system df -v` |
| 清无用镜像层 | `docker image prune -f`（**确认没有需要回滚的旧镜像后再执行**） |

管理端页面里另有「运行日志」（主服务 / 管理端两个标签，默认 5 秒自动刷新）与「运行监控 → 主服务进程控制」。**容器里主服务由 entrypoint 启动，后台显示「外部启动」，不要从这里点停止。**

---

## 7. 升级 / 回滚

### 7.1 标准升级（停机窗口最小）

```bash
# 0) 先备份数据卷（见 §8），并给当前镜像留个本地回滚标签
docker tag gavintom/wechatbot:1.1.2 wechatbot:backup-$(date +%Y%m%d)

# 1) 拉取新版本（下载期间旧容器照常服务）
NEW_TAG=1.1.2        # 换成要升级的版本号
docker pull gavintom/wechatbot:$NEW_TAG

# 2) 停旧容器，用完全相同的参数启动新版本
docker rm -f wxbot
docker run -d --name wxbot --restart unless-stopped \
  -p 7780:7780 -p 29999:29999 \
  -v wxbot-data:/app/data \
  gavintom/wechatbot:$NEW_TAG

# 3) 验证
docker ps --filter name=wxbot
curl -s -o /dev/null -w "login=%{http_code}\n" http://127.0.0.1:7780/login
curl -s -o /dev/null -w "healthz=%{http_code}\n" \
  --max-time 20 --retry 18 --retry-delay 5 --retry-all-errors http://127.0.0.1:29999/healthz
```

> **为什么先 pull 再停容器**：镜像下载期间旧容器仍在提供服务，停机窗口只剩重建容器的几十秒。
> 旧镜像不要立刻 `docker rmi`，留着作为回滚兜底；确认稳定后再 `docker image prune -f` 清理悬空层。

**升级后必做**：浏览器 **Ctrl+F5 强刷**。模板里 CSS/JS 引用带版本号（当前为 `?v=20261114i`），每次前端改动都会推进，但老旧手机浏览器仍可能吃缓存；强刷能立刻拿到新样式与交互。

也可以核对镜像内确实包含新代码：

```bash
docker exec wxbot sh -c "grep -c '20261114i' /app/admin/templates/index.html"
curl -s "http://127.0.0.1:7780/login" | grep app.css
```

### 7.2 回滚

```bash
docker rm -f wxbot
docker run -d --name wxbot --restart unless-stopped \
  -p 7780:7780 -p 29999:29999 -v wxbot-data:/app/data \
  wechatbot:backup-YYYYMMDD      # 换成 §7.1 留下的本地备份标签
```

也可以直接拉旧版本 tag 回滚：`docker pull gavintom/wechatbot:1.1.2` 后用 `1.1.2` 启动。

若数据库结构在升级中发生过变更，回滚镜像前先确认旧版本能否读新库；本项目的 `store.init_db()` 是幂等补列设计，兼容方向通常安全，但**生产环境仍建议先恢复数据卷备份**（见 §8）。

---

## 8. 备份与恢复

### 8.1 要备份什么

整个 `/app/data`：

| 内容 | 路径 |
|---|---|
| 配置（含密钥） | `config.yaml` |
| 业务库 / 聊天库 | `analytics.db`、`chatlog.db`（及 `-wal` / `-shm`） |
| 知识库文档与图片 | `knowledge/`（含 `.index/` 向量缓存） |
| 插件私有配置与任务 | `plugins/` |
| 日志、配置备份、会话状态 | `logs/`、`.config_backup/`、`.conversations.json`、PID |

### 8.2 命名卷备份

```bash
# 停服务可拿到更一致的 SQLite 快照（WAL 文件一并落盘）
docker stop wxbot

docker run --rm \
  -v wxbot-data:/data \
  -v "$PWD":/backup \
  alpine tar czf /backup/wxbot-data-$(date +%Y%m%d).tar.gz -C /data .

docker start wxbot
```

恢复（**会覆盖现有数据，先确认备份文件无误**）：

```bash
docker stop wxbot
docker run --rm -v wxbot-data:/data -v "$PWD":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/wxbot-data-YYYYMMDD.tar.gz -C /data"
docker start wxbot
```

### 8.3 bind mount 备份

直接打包宿主机目录即可（Windows 可用 7-Zip / PowerShell `Compress-Archive`）：

```bash
tar czf wxbot-data-$(date +%Y%m%d).tar.gz -C /opt/wxbot/data .
```

> 向量缓存 `.index/` 可删，启动会按内容指纹自动重建（首次重建耗时数分钟）；删掉它不影响业务数据，但会让启动变慢。
> 建议定期备份并把备份文件放到另一块磁盘/机器；密钥就在 `config.yaml` 里，备份文件等同凭据，注意保管。

---

## 9. 安全与生产化建议

### 9.1 端口暴露

- `7780` 只暴露到内网或经 nginx + HTTPS 访问；**不要裸奔公网**。
- `29999` 只给手机插件与受信任调用方；如必须公网暴露，开启 `webhook.token` 并在网关层限制来源 IP。
- 插件 HTTP 扩展路由 `/ext/...` 默认关闭，开启后强制 `X-Internal-Token`，仅应对内网开放。

### 9.2 反向代理（会踩 403）

nginx 必须透传 Host，否则管理端 CSRF 同源校验会全站 403：

```nginx
location / {
    proxy_pass http://127.0.0.1:7780;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

HTTPS 场景给容器加环境变量：

```bash
-e KF_ADMIN_HTTPS=1                       # Cookie 带 Secure
-e KF_ADMIN_ALLOWED_ORIGINS=https://你的域名
```

### 9.3 其他加固

| 项 | 建议 |
|---|---|
| 默认密码 | 首次登录立刻改 `admin / admin123` |
| webhook token | `webhook.token` 设随机串，推送地址带 `?token=` |
| 内部令牌 | `webhook.internal_token` 留空复用上面的 token |
| 密钥存放 | 只在卷内 `config.yaml`；镜像与 git 仓库都不含（`.dockerignore` / `.gitignore`） |
| 纯接口模式 | `api.enabled=true` 时 `api.api_key` 必填；`api.default_policy` 默认指向空的「API接口库」，要开放哪些文档手动勾选 |
| 插件总开关 | 模板默认 `plugins.enabled: false`，上线前灰度、异常时一键关回原链路 |

### 9.4 生产化参数

- 容器内默认用 Flask 开发服务器。需要 gunicorn 时，**修改 `docker-entrypoint.sh` 里两行启动命令**（文件里有注释示例），且 `gunicorn.conf.py` 的 **`workers` 必须为 1**：BRAIN 索引、会话记忆、`brain_lock`、插件注册表都在单进程内；提高并发改 `server.threads` / `KF_THREADS`，不要加 worker。
- 容器时区：Dockerfile 设了 `TZ=Asia/Shanghai`，但精简基础镜像没装 `tzdata`，系统时区不保证生效；插件 `daily` 定时任务请显式设 `plugins.scheduler.utc_offset: 8`（配置模板已填 8），不要依赖容器本地时区。需要严格系统时区可在派生镜像里 `apt-get install -y tzdata`。
- 日志：`data/logs/` 按天滚动、每类最多 5 个，可加 `-e WXBOT_LOG_DIR=...`（注意仍要在卷内才持久化）。
- 资源限制：可加 `--memory=4g --cpus=2`，但别低于向量模型的常驻内存需求。

### 9.5 Docker Compose 示例（可选）

在同目录保存为 `docker-compose.yml`，`docker compose up -d`（本地没有镜像会自动拉取）：

```yaml
services:
  wxbot:
    image: gavintom/wechatbot:1.1.2
    container_name: wxbot
    restart: unless-stopped
    ports:
      - "7780:7780"
      - "29999:29999"
    volumes:
      - wxbot-data:/app/data
    environment:
      TZ: Asia/Shanghai
      # KF_ADMIN_HTTPS: "1"
      # KF_ADMIN_ALLOWED_ORIGINS: "https://你的域名"
      # HF_ENDPOINT: "https://hf-mirror.com"

volumes:
  wxbot-data:
```

> 换成 bind mount 时把 `wxbot-data:/app/data` 改成 `./data:/app/data`，并确认 `./data/config.yaml.example` 与 `./data/knowledge/images/.gitkeep` 存在。

---

## 10. 故障排查

| 现象 | 原因与处理 |
|---|---|
| 容器 `Up`，`/healthz` 拒绝连接 | **向量模型预热 40~90 秒**，看 `docker logs wxbot` 是否有索引加载日志；用带 `--retry-all-errors` 的 curl 等待，不要反复重启 |
| `/login` 也打不开 | 检查端口映射与容器日志；`docker logs --tail 50 wxbot` 看 entrypoint 是否报配置模板缺失 |
| 手机发消息完全没反应 | ① 手机能否访问服务器 29999；② 插件 `/api/health` 的 `webhook` 地址是否正确；③ **群是否在「群聊配置」表内**（空卷默认没有群配置）；④ 是否 @ / 唤醒词 / 引用本机消息 / 处于对话延续窗口；⑤ 后台「聊天记录」看原始消息是否入库 |
| 群聊回复但答非所问 / 说不确定 | 知识库策略没勾选文档，或改完文档没点「**重建索引并重载**」；用「知识库文档」页底部问答测试看召回片段 |
| 全新容器 AI 完全不查知识库 | 镜像不含业务文档，先按 §5.4 导入文档 |
| 后台所有按钮 403 | 反代没透传 `Host` / `X-Forwarded-Host`，见 §9.2 |
| 改了 `config.yaml` 没生效 | 该文件模块级读取，需 `docker restart wxbot`；SQLite 类配置与「对话延续窗口」不用重启 |
| 改了前端还是旧界面 | 浏览器强刷 Ctrl+F5；确认 `/login` 返回的 CSS/JS 版本号已推进 |
| 语音一直回「没听清」 | 检查 `voice.*` ASR 端点与密钥；镜像已内置 pilk 与 ffmpeg，也可先 `voice.enabled: false` 用文字验证链路 |
| 头像 / 朋友圈图片不显示 | 媒体一律经后端代理取 `blob:`，检查容器能否访问微信 CDN；失败时前端回退首字母 / 隐藏封面 |
| 朋友圈视频有声音没画面 | 前端会自动请求 ffmpeg H.264 转码版 `/api/moments/video`；确认镜像内 `imageio-ffmpeg` 正常 |
| 后台点「停止主服务」异常 | 容器里主服务由 entrypoint 启动，属于「外部启动」，后台不会越权停止；要重启整个容器用 `docker restart wxbot` |
| `/httpapi/query` 返回 404 | 路由只在 `api.enabled=true` 且 `api.api_key` 非空时注册，改完需重启 |
| 端口冲突 / 容器名已存在 | `netstat` 或 `ss` 查占用；换端口映射（同时改 `server.public_base_url`），或先 `docker rm -f wxbot` |
| Windows 挂载后目录为空 | Docker Desktop 未共享该磁盘；或路径写法不对，用 `E:/...` 正斜杠并加引号 |
| 从源码构建时 pilk / torch 失败（用官方镜像不会遇到） | 网络问题，见 §3.1；pilk 构建期 `import` 校验失败会主动让构建报错，不要跳过 |
| 磁盘被撑满 | `docker system df -v`；清理 `docker image prune -f`、旧日志与过期聊天记录（后台「聊天记录 → 清理过期」） |

---

## 11. 部署验收清单

- [ ] 镜像来自 `gavintom/wechatbot:1.1.2`（`docker inspect wxbot --format '{{.Config.Image}}'`），未在本地打包
- [ ] `docker ps` 显示 `wxbot` 为 `Up`，端口 `7780`、`29999` 均已映射，重启策略 `unless-stopped`
- [ ] `docker inspect wxbot` 的挂载是 `/app/data`，且卷内存在 `config.yaml`、`analytics.db`、`chatlog.db`、`knowledge/`、`plugins/`
- [ ] `/login` 返回 200，`/healthz` 返回 200 且插件在线、检索器就绪
- [ ] 默认密码已修改；`webhook.token` 已设置并写入插件
- [ ] 通讯录已同步；至少配置了一个群聊并绑定知识库策略
- [ ] 知识库文档已导入，且已执行「重建索引并重载」，问答测试能召回片段
- [ ] 真实群消息 `@机器人` 能得到基于知识库的回复，并在「对话记录 / AI 用量」留下流水
- [ ] 手机浏览器打开后台：抽屉导航可用、页面无横向溢出
- [ ] 数据卷已完成一次备份，备份文件存放于宿主机之外
- [ ] 已记录当前镜像 ID 与备份标签，升级/回滚路径演练过

---

## 附录 A：端口与地址

| 地址 | 用途 | 暴露范围 |
|---|---|---|
| `http://<host>:7780/login` | Web 管理端 | 内网 / HTTPS 反代 |
| `http://<host>:29999/webhook?token=...` | 手机插件推送入口 | 仅手机插件 |
| `http://<host>:29999/healthz` | 主服务健康与检索器状态 | 运维 |
| `POST http://<host>:29999/httpapi/query` | 纯接口问答（需开启 `api.enabled`） | 受信任调用方 |
| `http://<手机IP>:18888/api/health` | 手机插件状态（本项目只读取） | 内网 |

## 附录 B：环境变量速查

| 变量 | 作用 |
|---|---|
| `WXBOT_DATA_DIR` | entrypoint 的数据目录（默认 `/app/data`；**改它会脱离 VOLUME，除非同步改挂载点，否则别动**） |
| `WXBOT_CONFIG` / `WXBOT_DB` / `WXBOT_CHAT_DB` | 覆盖配置 / 业务库 / 聊天库路径 |
| `WXBOT_LOG` / `WXBOT_ADMIN_LOG` / `WXBOT_SERVICE_LOG` / `WXBOT_LOG_DIR` | 覆盖日志文件 / 目录 |
| `WXBOT_PID` / `WXBOT_SERVICE_LOG` / `WXBOT_BACKUP_DIR` / `WXBOT_CONV` | PID、启动日志、配置备份目录、会话记忆文件 |
| `KF_ADMIN_HTTPS` / `KF_ADMIN_ALLOWED_ORIGINS` | HTTPS Cookie / 跨域白名单 |
| `HF_ENDPOINT` / `HTTPS_PROXY` / `HTTP_PROXY` | 模型下载镜像 / 网络代理 |
| `TZ` | 容器时区环境变量（精简镜像未装 tzdata，定时任务请用 `plugins.scheduler.utc_offset`） |

> 覆盖路径时请确保新路径仍位于 `/app/data` 卷内，否则容器重建后数据会丢失。

## 附录 C：相关文档

- [`README.md`](../README.md)：完整功能说明、HTTP 接口、消息处理规则、插件系统
- [`deploy.md`](../deploy.md)：裸机 / systemd / 反向代理部署与运维排障
- [`plugins/README.md`](../plugins/README.md)：插件开发指南

---

## 联系与咨询

产品咨询、方案沟通与部署协助，欢迎通过 Telegram 联系：<https://t.me/wechatbotiphone>
