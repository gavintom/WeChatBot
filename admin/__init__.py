"""admin 包：个人微信智能客服的 Web 管理端。

子模块分工：
- app.py        Flask 应用装配 + 登录/安全中间件 + 页面路由（不再放业务接口）
- web.py        共用层：响应助手、登录校验、主服务转发、分页工具、应用常量
- bp_sys.py     运行监控 / 服务控制 / 日志 / 系统信息
- bp_plugins.py 插件清单 / 私有配置 / 定时任务 / 授权激活
- bp_contacts.py 通讯录 / 群成员 / 备注与群昵称
- bp_media.py   聊天媒体（图片 / 文件）落盘流水
- bp_kb.py      知识库文档 / 策略 / 群聊与私聊规则
- bp_records.py 对话记录 / 聊天记录 / 统计 / API 与 AI 用量
- bp_moments.py 朋友圈浏览与点赞评论
- bp_config.py  系统配置 / 对话延续 / 大模型纳管
- cfg.py        读 config.yaml 的单一入口（支持 WXBOT_CONFIG 覆盖）
- confedit.py   config.yaml 文本级在线编辑、校验、备份与回滚
- kb.py         知识库文件管理、Office/PDF 导入、索引缓存、问答测试
- pricing.py    AI 模型单价与费用计算（AI 用量 / API 调用记录共用）
- contacts.py   手机插件通讯录同步与缓存
- static/       单页应用（原生 JS + CSS，无构建）
- templates/    index.html / login.html / error.html

数据层在项目根的 `store.py`（主服务与后台共用的 SQLite）。
"""