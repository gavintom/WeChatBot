# 部署说明

## 1. 环境

- Python 3.10+（开发验证用 3.13），Linux 服务器推荐；Windows 可本地调试。
- 手机（越狱 iOS + WeChatBotTweak 插件）与服务器**网络互通**；服务器要能被手机访问
  （webhook 是手机主动 POST）。插件接口约定见 README 第 5 节。
- 一个 OpenAI 兼容的大模型端点；可选一个 ASR 端点（或本地 whisper）。
- 所有运行时可变数据集中在 `data/`：配置、两个数据库、日志、PID、会话状态与备份。
  Docker 部署只挂这一个目录即可完整持久化。

```bash
cd /opt/wechatbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# 国内/代理环境下载模型时可设：
# export HTTPS_PROXY=http://192.0.2.2:7890 HTTP_PROXY=http://192.0.2.2:7890
# export HF_ENDPOINT=https://hf-mirror.com
```

只跑通链路的最小依赖（对应纯 BM25 降级模式）：

```bash
pip install flask requests pyyaml numpy jieba rank-bm25
```

**模型权重**（裸机 / 源码部署才需要；官方 Docker 镜像已内置权重模型）：配置默认从本地目录加载
`./models/bge-base-zh-v1.5`（向量）与 `./models/bge-reranker-base`（重排）。仓库不含权重文件，
需自行下载到 `models/` 对应目录；重排模型缺失会自动跳过重排、不影响启动。下载方式见
[README §6「模型与下载」](README.md#6-语音与检索)（huggingface-cli / ModelScope，国内可设
`HF_ENDPOINT=https://hf-mirror.com`）。

> 下文 `192.0.2.x` 为 RFC 5737 文档示例地址，部署时请替换为实际 IP。

## 2. 配置检查清单

把 `data/config.yaml.example` 复制为 `data/config.yaml`，至少配置：

| 项 | 说明 |
|---|---|
| `wechat.base_url` | 手机插件地址，如 `http://192.0.2.71:18888` |
| `wechat.token` | 插件设了 token 才填 |
| `wechat.self_wxid` | 本机微信号；留空自动从 `/api/health` 的 `self` 读取 |
| `transfer.default_wxid` / `default_name` | 默认人工接待；不填 wxid 转人工时群聊不会 @ 人 |
| `webhook.token` | 建议开启；推送地址写成 `http://<host>:29999/webhook?token=<token>` |
| `webhook.internal_token` | 后台调主服务 `/internal/*` 用；留空复用 `webhook.token` |
| `server.public_base_url` | 手机能访问到的地址 |
| `brain.external_llm.*` | API Key / base_url / model |
| `brain.llm_pricing` | 可选，模型单价（元/百万 token），供后台估算费用 |
| `voice.*` | 语音 ASR 端点与密钥；不用则 `voice.enabled: false` |
| `api.enabled / api.api_key` | 纯接口模式开关，开启时 `api_key` 必填 |
| `admin.main_service` | 后台访问主服务的地址（同机用 `http://127.0.0.1:29999`） |

改完 `config.yaml` 需**重启主服务**才生效（模块级读一次）；后台页面的配置改动即时生效。

## 3. 启动方式二选一

**A. 管理端启动（单机/本地推荐）**：只启动管理端，然后在「运行监控 → 主服务进程控制」
点「启动主服务」。同一处可重启/停止。启动命令默认 `<当前 Python> webhook_server.py`，
可用 `admin.start_command` 换成 gunicorn：

```yaml
admin:
  start_command: "gunicorn -c gunicorn.conf.py webhook_server:app"
```

**B. systemd / 手动启动**：用下面的单元文件，或 `python webhook_server.py`。
⚠️ A、B 不要同时用；想改由管理端接管先在原处停止。

## 4. systemd（Linux 生产）

`/etc/systemd/system/wxbot-webhook.service`：

```ini
[Unit]
Description=WeChatBot Webhook Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/wechatbot
Environment=PYTHONUNBUFFERED=1
Environment=HF_ENDPOINT=https://hf-mirror.com
ExecStart=/opt/wechatbot/.venv/bin/gunicorn -c gunicorn.conf.py webhook_server:app
Restart=always
RestartSec=5
TimeoutStopSec=40
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/wxbot-admin.service`：

```ini
[Unit]
Description=WeChatBot Admin Service
After=wxbot-webhook.service

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/wechatbot
ExecStart=/opt/wechatbot/.venv/bin/gunicorn -c admin/gunicorn.conf.py admin.app:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wxbot-webhook wxbot-admin
curl http://127.0.0.1:29999/healthz | python3 -m json.tool
```

> `gunicorn.conf.py` 固定 `workers=1`（索引、会话记忆、锁都在单进程内）。
> 提高并发改 `server.threads` / `KF_THREADS`，不要加 worker。

## 5. 反向代理（可选）

若通过 nginx 暴露后台，必须透传 Host，否则后台 CSRF 同源校验会全站 403：

```nginx
location / {
    proxy_pass http://127.0.0.1:7780;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

HTTPS 时给后台进程设 `KF_ADMIN_HTTPS=1`（Cookie 才带 Secure），跨域另设
`KF_ADMIN_ALLOWED_ORIGINS=https://你的域名`。主服务的 `/webhook` 只给局域网手机访问，
不建议挂公网；如必须暴露，开启 `webhook.token` 并在 nginx 层限制来源 IP。

## 6. 手机端

1. 插件设置页确认 HTTP 端口（默认 18888）、token、webhook 地址。
2. 后台「运行监控 → 把 webhook 地址写入手机插件」，或手动：
   ```bash
   curl -X POST http://192.0.2.71:18888/api/webhook \
     -H 'Content-Type: application/json' \
     -d '{"url":"http://192.0.2.145:29999/webhook?token=你的token"}'
   ```
3. `curl http://192.0.2.71:18888/api/health` 应返回 `ok:true`、`self`、`webhook`。
4. 后台「通讯录管理 → 从手机同步」应能看到群和好友。

## 7. 日常运维

| 操作 | 位置 |
|---|---|
| 改知识库 / 群聊 / 私聊配置 | 后台页面，保存即生效 |
| 新增/编辑知识库文档 | 后台「知识库文档」，改完点「重建索引并重载」 |
| 切换 AI 大模型 | 后台「AI 大模型」页选默认，下一次生成即生效、无需重启 |
| 启停知识库接口 | 后台「运行监控 → 知识库接口」 |
| 改 config.yaml | 后台「系统配置」保存后需重启主服务；备份最多 10 份，可回滚 |
| 聊天记录保留期 | 后台「系统配置」改 `chat_log.retention_months`（1~12 个月），无需重启 |
| 看流水与配图 | 「对话记录」「API 记录」可筛选并导出 CSV |
| 看大模型花费 | 后台「AI 用量」 |
| 看日志 | 后台「运行日志」双标签，按天轮转、每类最多 5 个文件 |
| 备份 | 整个 `data/` 目录；Docker 下就是 `wxbot-data` 卷 |

数据文件：`analytics.db`（业务库）、`chatlog.db`（聊天记录）、`.conversations.json`
（会话记忆）、`data/logs/`（按天日志）、`data/knowledge/.index/`（向量缓存，删后自动重建）。

## 8. 故障排查

| 现象 | 检查 |
|---|---|
| 手机发消息没反应 | `/healthz` 插件是否在线；群是否在群聊配置表；是否 @ 或命中唤醒词 |
| 后台所有按钮 403 | 反代是否设了 `X-Forwarded-Host` |
| AI 只回兜底话术 | `brain.external_llm.api_key/base_url` 是否正确；看运行日志里的 LLM 报错 |
| 语音回「没听清」 | `voice.*` ASR 配置；本机装 `pilk` + `imageio-ffmpeg`（Docker 已内置） |
| 首次启动很慢 / 模型加载失败 | 权重模型应放在 `models/bge-base-zh-v1.5`（官方镜像已内置）；重排模型放 `models/bge-reranker-base`，缺失会自动跳过；下载见 README §6，国内可设 `HF_ENDPOINT` / `HTTPS_PROXY` |
| `/httpapi/query` 404 | `api.enabled=true` 且 `api.api_key` 非空才会注册路由，改完需重启 |
| 群里自问自答 | 检查 `wechat.self_wxid` 与插件版本 |
| 检索召回为空 | 「知识库 → 问答测试」看召回片段；索引 `chunk` 数应大于 0，必要时重建索引 |
| 某条消息为何没回 | 跳过/不支持消息不写对话记录；把 `server.log_level` 设为 `DEBUG` 看运行日志 |

## 9. Windows 本地开发

推荐双击 `run_admin.bat`，然后在后台点「启动主服务」。`run_webhook.bat` 供手动启动用，
两者不要同时跑。

```bat
python -m admin.app
:: 主服务交给管理端启动；需要手动时用 python webhook_server.py
```

## 10. Docker 部署

```bash
# 直接拉取官方镜像，无需本地打包
docker pull gavintom/wechatbot:1.1.0

# 运行：只挂一个卷
docker run -d --name wxbot \
  -p 7780:7780 -p 29999:29999 \
  -v wxbot-data:/app/data \
  --restart unless-stopped gavintom/wechatbot:1.1.0

# 查看日志 / 升级
docker logs -f wxbot
# 升级时 docker pull 新版本并重建容器即可，卷里的数据不动
```

- 端口：**7780** 管理端、**29999** 主服务；卷 `/app/data` 含配置、两个数据库、日志、
  备份、会话状态与知识库文档。
- 业务知识库文档不进镜像；首次启动自带 `README.md` 作为初始知识库文档（自动入库并建索引），
  其余业务文档需放进卷或在后台上传/导入。
- 首次启动 entrypoint 用 `data/config.yaml.example` 生成 `data/config.yaml`，然后同时拉起
  管理端与主服务。打开 `http://<宿主机>:7780` 到「系统配置」填写插件地址与 LLM 密钥。
- 主服务由 entrypoint 启动，管理端显示为「外部启动」、不会越权停止它，这是预期的。
- 默认镜像已装 CPU-only torch + sentence-transformers，并内置权重模型
  `models/bge-base-zh-v1.5`；**不含重排模型**，需要重排时按 README §6 下载放到
  `models/bge-reranker-base`，缺失会自动跳过重排。从源码构建镜像前需先下载权重模型到
  `models/`（Dockerfile 有 `test -f` 校验）；纯 BM25 精简镜像用 `--build-arg INSTALL_VECTOR=0`。
- `.dockerignore` 排除了本机 `config.yaml` 与所有 `*.db`，镜像里不含真实密钥。
  生产环境建议只把 7780 暴露到内网，29999 仅对手机插件与受信任调用方开放；
  反向代理配置见第 5 节。

更完整的步骤（源码构建、备份恢复、升级回滚、验收清单）见
[Docker 部署详细指南](docs/docker-deployment-guide.md)。
