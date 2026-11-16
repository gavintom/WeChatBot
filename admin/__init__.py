"""admin 包：个人微信智能客服的 Web 管理端。

子模块分工：
- app.py        Flask 管理界面 + REST API（策略 / 通讯录 / 知识库 / 配置 / 日志）
- cfg.py        读 config.yaml 的单一入口（支持 WXBOT_CONFIG 覆盖）
- confedit.py   config.yaml 文本级在线编辑、校验、备份与回滚
- kb.py         知识库文件管理、Office/PDF 导入、索引缓存、问答测试
- pricing.py    AI 模型单价与费用计算（AI 用量 / API 调用记录共用）
- contacts.py   手机插件通讯录同步与缓存
- static/       单页应用（原生 JS + CSS，无构建）
- templates/    index.html / login.html / error.html

数据层在项目根的 `store.py`（主服务与后台共用的 SQLite）。
"""