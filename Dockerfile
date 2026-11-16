# 个人微信智能客服 · Docker 镜像
#
# 推荐：直接拉取发布镜像 docker pull gavintom/wechatbot:1.1.0
# 运行： docker run -d --name wxbot -p 7780:7780 -p 29999:29999 \
#          -v wxbot-data:/app/data gavintom/wechatbot:1.1.0
# 从源码构建（可选）： docker build -t gavintom/wechatbot:1.1.0 .
#
# 所有可变数据（配置 / 数据库 / 日志 / 备份 / 知识库文档）都在 /app/data，
# 挂一个卷即可完整持久化，镜像本身不含任何密钥。
# pilk（微信 SILK_V3 语音解码）先用独立阶段编成 Linux wheel，
# 最终镜像只拷编译产物，不引入编译工具链。
FROM python:3.11-slim AS wheel-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --timeout 120 --retries 5 \
      --wheel-dir /wheels "pilk>=0.0.3"

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WXBOT_DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.docker.txt /app/requirements.docker.txt
# 默认安装 CPU-only 的 torch / sentence-transformers（配置默认开启混合检索）。
# 只要纯 BM25 的精简镜像用 --build-arg INSTALL_VECTOR=0
ARG INSTALL_VECTOR=1
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r /app/requirements.docker.txt \
    && if [ "$INSTALL_VECTOR" = "1" ]; then \
         pip install --no-cache-dir --timeout 120 --retries 5 \
           --index-url https://download.pytorch.org/whl/cpu "torch>=2.0"; \
         pip install --no-cache-dir --timeout 120 --retries 5 "sentence-transformers>=2.2.2"; \
       fi

# 微信语音：装上上面编好的 pilk Linux wheel，并在构建期做 import 校验。
COPY --from=wheel-builder /wheels /tmp/pilk-wheels
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
      --no-index --find-links=/tmp/pilk-wheels "pilk>=0.0.3" \
    && python -c "import pilk; print('pilk OK:', pilk.__file__)" \
    && rm -rf /tmp/pilk-wheels

# 权重模型 bge-base-zh-v1.5（约 390MB）随下面的 COPY . /app 一起进镜像，配置用相对路径
# ./models/bge-base-zh-v1.5，容器内离线加载；重排模型不含在镜像内，需要时自行下载到
# models/ 目录（下载方式见 README）。构建期 test -f 校验可防止误排除权重模型。
COPY . /app

# 配置模板源码在 data/config.yaml.example，但 /app/data 是卷、运行时会被覆盖，
# 所以构建时另存一份到非卷路径；entrypoint 从那里生成 data/config.yaml。
# 知识库文档属于运行时数据，既不在 git 仓库里（.gitignore）也不进镜像
# （.dockerignore）。构建时把 data/knowledge/ 的目录骨架（images/）与初始文档
# README.md 一起复制到非卷路径 /app/.knowledge.default；首次启动由 entrypoint
# 补进卷（卷里没有的才写，不覆盖用户改过的文档），随后主服务自动切块建索引。
# 代码与配置统一用 ./data/knowledge，镜像里不再需要 knowledge 符号链接。
RUN test -f /app/models/bge-base-zh-v1.5/config.json \
    && mkdir -p /app/.knowledge.default /app/.config-template /app/.plugins.default \
    && cp /app/data/config.yaml.example /app/.config-template/config.yaml.example \
    && cp -r /app/data/knowledge/. /app/.knowledge.default/ \
    && if [ -d /app/data/plugins ]; then cp -r /app/data/plugins/. /app/.plugins.default/; fi \
    && chmod +x /app/docker-entrypoint.sh

# 初始知识库文档：把 README.md 作为第一条知识库文档放进默认知识库目录。
# entrypoint 首次启动（卷里还没有 knowledge/ 目录时）会把 .knowledge.default
# 复制进 data/knowledge/，主服务启动时递归扫 .md 自动切块建索引，无需手工导入。
# 卷里已有 knowledge/ 时不动它 —— 用户后来改过的文档永远不会被镜像覆盖。
#
# 文档名沿用本项目既有命名惯例（knowledge/wechatbot_README.md、
# knowledge/wechatbottweak_README.md），避免与用户自己的 README.md 撞名，
# 也避免「README.md」和「wechatbot_README.md」两份同内容文档重复召回。
#
# README 里的官网预览图一并复制到 knowledge/images/，并把 md 中的相对路径
# 从 docs/images/xxx 改写成 images/xxx：resolve_image 只接受落在
# knowledge_dir 内的本地图片，路径不改的话图会在知识库里解析不出来。
#
# 图同时放 docs/images/ 一份：知识库里的 README 副本可能来自「项目文档同步」
# （保留原始 docs/images/ 路径），两个位置都备一份，两种路径引用都能解析到。
RUN mkdir -p /app/.knowledge.default/images /app/.knowledge.default/docs/images \
    && cp -r /app/docs/images/. /app/.knowledge.default/images/ \
    && cp -r /app/docs/images/. /app/.knowledge.default/docs/images/ \
    && sed 's#](docs/images/#](images/#g' /app/README.md > /app/.knowledge.default/wechatbot_README.md \
    && test -f /app/.knowledge.default/wechatbot_README.md

EXPOSE 7780 29999
VOLUME ["/app/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
