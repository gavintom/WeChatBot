#!/usr/bin/env bash
# 容器入口：初始化数据卷，然后同时拉起管理端(7780)与主服务(29999)。
#
# 主服务由 entrypoint 启动（不是管理端点按钮启动的），所以管理端
# /api/service 会显示「由外部启动」，不会越权去停止它 —— 这是预期的。
set -euo pipefail

DATA_DIR="${WXBOT_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"

# 1) 首次启动：一律以 data/ 目录下的 config.yaml.example 为模板生成配置。
#    卷里自带模板就直接用；否则用 Dockerfile 预置在非卷路径的副本
#    （/app/data 是卷，镜像内该路径的内容运行时会被卷覆盖）。
if [ ! -f "$DATA_DIR/config.yaml" ]; then
    TEMPLATE="$DATA_DIR/config.yaml.example"
    if [ ! -f "$TEMPLATE" ]; then
        TEMPLATE="/app/.config-template/config.yaml.example"
    fi
    cp "$TEMPLATE" "$DATA_DIR/config.yaml"
    echo "[init] 已生成 $DATA_DIR/config.yaml（模板：$TEMPLATE；请到「系统配置」填写手机插件地址与 LLM 密钥）"
fi

# 2) 首次启动：把镜像内的初始知识库文档复制进卷。
#    ⚠️ 不能只判断 knowledge/ 目录在不在：Docker 新建命名卷时会拿镜像里
#    /app/data 的内容做「播种」（VOLUME 声明路径），卷里天生就有 knowledge/
#    骨架目录，目录判空这条路永远走不通。
#    改成按文件逐个补（cp -n 不覆盖）：卷里没有的文档才写入，
#    用户后来改过或删掉的文档永远不会被镜像改动。
mkdir -p "$DATA_DIR/knowledge"
cp -rn /app/.knowledge.default/. "$DATA_DIR/knowledge/" 2>/dev/null || true
if [ -f "$DATA_DIR/knowledge/wechatbot_README.md" ]; then
    echo "[init] 知识库初始文档就位 $DATA_DIR/knowledge（含 wechatbot_README.md）"
else
    echo "[init] 知识库目录 $DATA_DIR/knowledge"
fi

# 3) 首次启动：初始化插件私有配置目录。镜像里只带骨架，真实配置随卷持久化。
if [ ! -d "$DATA_DIR/plugins" ]; then
    mkdir -p "$DATA_DIR/plugins"
    if [ -d /app/.plugins.default ]; then
        cp -r /app/.plugins.default/. "$DATA_DIR/plugins/"
    fi
    echo "[init] 已初始化插件配置目录 $DATA_DIR/plugins"
fi

echo "[run] 数据卷：$DATA_DIR | 管理端 :7780 | 主服务 :29999"

# 想换成 gunicorn 生产模式，把下面两行改成：
#   gunicorn -c admin/gunicorn.conf.py admin.app:app
#   gunicorn -c gunicorn.conf.py webhook_server:app
python -m admin.app &
ADMIN_PID=$!
python webhook_server.py &
MAIN_PID=$!

cleanup() {
    trap - TERM INT
    echo "[stop] 正在停止两个服务…"
    kill "$ADMIN_PID" "$MAIN_PID" 2>/dev/null || true
    wait "$ADMIN_PID" 2>/dev/null || true
    wait "$MAIN_PID" 2>/dev/null || true
}
trap cleanup TERM INT

# 任一进程退出就结束容器，方便 Docker 的 restart policy 接管
wait -n "$ADMIN_PID" "$MAIN_PID" || true
cleanup
