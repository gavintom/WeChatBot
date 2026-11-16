"""config.yaml 在线编辑：读、写、语法校验、备份回滚。

三条硬约束（都是踩过才知道的）：
1. **绝不能「反序列化→重新序列化」来保存**。`yaml.safe_load` + `safe_dump` 会把
   config.yaml 里 20 多行精心写的中文注释**全部丢掉**（实测 97 行 → 75 行、注释 0 行），
   而那些注释是这份配置唯一的使用说明。ruamel.yaml 能保留注释但**没装**，
   而且引入新依赖不该由后台编辑功能单方面决定。
   → 所以保存走**文本级定点替换**：只改用户实际编辑过的行，其余字节原样写回。
2. **保存前必须语法校验**。config.yaml 语法错了，主服务下次启动直接崩
   （`yaml.safe_load(open("config.yaml"))` 在 webhook_server.py 模块级，崩了连日志都没有）。
3. **保存前必须备份**。改错了要能一键回滚。备份放 `.config_backup/`，保留最近 10 份。

关于密钥：按用户要求**明文显示可改**（他是唯一使用者，服务器也是他自己的）。
但提供 `redact` 参数让前端能把密钥位置标出来，避免截图/演示时泄露。
"""
import json
import os
import shutil
import time

import yaml

from admin import cfg as _cfgmod            # noqa: E402
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = _cfgmod.CONFIG_PATH          # 单一来源（admin/cfg.py），测试可改
BACKUP_DIR = os.environ.get("WXBOT_BACKUP_DIR") or os.path.join(BASE_DIR, "data", ".config_backup")
MAX_BACKUPS = 10                     # 系统配置页历史备份上限，超过自动删最老的
MAX_BYTES = 512 * 1024

# 密钥掩码占位符。**保存时会检测它并拒绝**（见 write）—— 否则用户「遮住密钥」后
# 直接保存，真密钥会被这串占位符覆盖，手机插件 / 大模型鉴权全挂且极难排查。
MASK_TOKEN = "********已配置********"

# —— 哪些字段算「密钥」——
# 这里用**精确名单**而不是"字段名里含 key/secret/token 就算"。因为模糊匹配会误伤：
#   max_tokens   含 "token"  → 实际是生成长度上限，不是密钥
#   keywords     含 "key"    → 实际是转人工关键词，不是密钥
# 误伤的实际后果不小：这些行会被掩码显示、且**保存时被掩码写保护挡住**，
# 用户想改个生成长度反而改不了（得先关掉遮蔽）。所以宁可用名单，可控可审计。
SECRET_KEYS_EXACT = frozenset((
    "token",                        # wechat.token / webhook.token（本项目裸 token 全是密钥）
    "api_key",                      # 外部 LLM / 纯接口模式的 API Key
    "asr_api_key",                  # 语音识别 ASR Key
    "password", "passwd", "pwd",
    "secret",
))
# 后缀规则：覆盖将来新增的密钥字段（如 xxx_api_key / xxx_token / xxx_secret），
# 放在名单之后判断。刻意不含 "key" 单独后缀 —— 那是 keywords 这类普通字段的坑。
SECRET_KEY_SUFFIXES = ("_api_key", "_apikey", "_secret", "_token",
                       "_password", "_passwd", "_aes_key", "_key_id")
# 明确不是密钥（即使名字里含 key/token/secret，防止将来有人往名单里乱加）。
# 本项目里裸 `token` 一律是密钥（插件 token / webhook token），所以不放进这个白名单；
# token 数量类字段用 max_tokens 等明确名字，仍在这里防误伤。
NOT_SECRET_KEYS = frozenset((
    "max_tokens", "max_token", "tokens", "token_limit", "token_count",
    "keywords", "keyword",
    "system_prompt", "prompt",
))
def _is_secret_line(line):
    """这一行是否含密钥（只看赋值行的**字段名**，注释行不算）。"""
    low = line.lower()
    if not low.strip() or low.strip().startswith("#"):
        return False
    if ":" not in line:
        return False
    key = line.split(":", 1)[0].strip().strip('"\'').strip()
    k = key.lower()
    if k in NOT_SECRET_KEYS:
        return False
    if k in SECRET_KEYS_EXACT:
        return True
    return any(k.endswith(sfx) for sfx in SECRET_KEY_SUFFIXES)


def secret_lines(text):
    """返回 1-based 行号列表，标记哪些行含密钥。"""
    return [i for i, l in enumerate((text or "").splitlines(), 1) if _is_secret_line(l)]


def read(redact=False):
    """读配置原文。同时返回结构化视图与密钥行号，供前端渲染。

    redact=True 时把密钥值替换成掩码，但**掩码行会被写保护**——
    保存时若该行仍是掩码，则保留原值（避免「以为改了其实没改」或「把掩码写进文件」）。
    """
    if not os.path.isfile(CONFIG_PATH):
        raise FileNotFoundError("config.yaml 不存在")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        text = f.read()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        data = None
    lines = secret_lines(text)
    if not redact:
        return {"text": text, "data": data, "secret_lines": lines,
                "size": len(text.encode("utf-8"))}
    masked, changed = _mask(text, lines)
    return {"text": masked, "data": None, "secret_lines": changed or lines,
            "size": len(text.encode("utf-8")), "masked": True}


def _mask(text, lines):
    """把指定行的值替换为掩码。返回 (新文本, 实际掩码了的行号)。"""
    out = list(text.splitlines(True))
    done = []
    for ln in lines:
        i = ln - 1
        if i < 0 or i >= len(out):
            continue
        line = out[i]
        newline = "\n" if line.endswith("\n") else ""
        raw = line.rstrip("\n")
        # 摘出行尾注释（前面是空白的 #），引号内的 # 属于值本身，不能切。
        # 旧实现 partition("#") 后又把整行拼回去，等于没摘，注释会被静默丢掉。
        quote, cut = None, -1
        for idx, ch in enumerate(raw):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "#" and (idx == 0 or raw[idx - 1].isspace()):
                cut = idx
                break
        body = raw[:cut].rstrip() if cut >= 0 else raw.rstrip()
        comment = ("  " + raw[cut:].rstrip()) if cut >= 0 else ""
        k, sep, v = body.partition(":")
        if not sep or not v.strip() or v.strip().startswith("#"):
            continue                          # 没值的不用掩
        out[i] = '%s: "%s"%s%s' % (k.rstrip(), MASK_TOKEN, comment, newline)
        done.append(ln)
    return "".join(out), done


def validate(text):
    """只做语法校验，不写盘。返回 (是否合法, 错误信息含行号)。"""
    try:
        yaml.safe_load(text or "")
        return True, ""
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            return False, "第 %d 行第 %d 列：%s" % (mark.line + 1, mark.column + 1,
                                                     getattr(e, "problem", str(e)))
        return False, str(e)


def _semantic_warn(data):
    """语法没错但配置可能有问题的提醒（不阻止保存，只提示）。"""
    warns = []
    if not isinstance(data, dict):
        return ["顶层不是键值映射，请确认格式"]
    for seg in ("wechat", "webhook", "brain", "server"):
        if seg not in data:
            warns.append("缺少 `%s` 段，主服务启动时可能取不到配置" % seg)
    w = (data.get("wechat") or {})
    if not w.get("base_url"):
        warns.append("`wechat.base_url` 为空，发消息/拉通讯录都会失败（填手机插件地址）")
    wh = (data.get("webhook") or {})
    if wh.get("token") and not (data.get("server") or {}).get("public_base_url"):
        warns.append("开了 webhook token 校验，记得把推送地址配成 "
                     "`<public_base_url>/webhook?token=<token>`")
    llm = ((data.get("brain") or {}).get("external_llm") or {})
    if not llm.get("api_key") or not llm.get("base_url"):
        warns.append("`brain.external_llm.api_key / base_url` 未填，AI 回答会走兜底话术")
    srv = (data.get("server") or {})
    p = srv.get("port")
    try:
        if p and not (0 < int(p) < 65536):
            warns.append("`server.port` = %s 不是合法端口" % p)
    except (TypeError, ValueError):
        warns.append("`server.port` 必须是数字")
    rp = (data.get("reply") or {})
    if "interval_seconds" in rp:
        try:
            gap = float(rp.get("interval_seconds"))
            if not (0 <= gap <= 30):
                warns.append("`reply.interval_seconds` 需在 0~30 秒之间，运行时会自动夹到边界")
        except (TypeError, ValueError):
            warns.append("`reply.interval_seconds` 不是数字，运行时会按 1 秒处理")
    fu = (rp.get("followup") or {}) if isinstance(rp, dict) else {}
    if isinstance(fu, dict):
        if "window_minutes" in fu:
            try:
                mins = float(fu.get("window_minutes"))
                if not (1 <= mins <= 30):
                    warns.append("`reply.followup.window_minutes` 需在 1~30 分钟之间，运行时会自动钳制")
            except (TypeError, ValueError):
                warns.append("`reply.followup.window_minutes` 不是数字，运行时按 3 分钟处理")
        if "window_seconds" in fu:
            warns.append("`reply.followup.window_seconds` 已移除，请改用 window_minutes（1~30 分钟）")
        if "scope" in fu:
            warns.append("`reply.followup.scope` 已废弃：作用范围固定为同一群友，该配置项会被忽略")
        if "hint_enabled" in fu and not isinstance(fu.get("hint_enabled"), bool):
            warns.append("`reply.followup.hint_enabled` 应是 true / false")
        if "hint" in fu:
            h = fu.get("hint")
            if not isinstance(h, str):
                warns.append("`reply.followup.hint` 应是字符串"
                             "（`{minutes}` 换成窗口分钟数）")
            elif len(h) > 200:
                warns.append("`reply.followup.hint` 超过 200 字，运行时会截断")
    ap = (data.get("api") or {})
    if ap.get("enabled") and not ap.get("api_key"):
        warns.append("`api.enabled=true` 但 `api.api_key` 为空 → /httpapi/query 不会注册（404）")
    return warns


def _prune_backups():
    """只保留最近 MAX_BACKUPS 份，按**文件修改时间**删最老的。

    ⚠️ 不能按文件名排序：备份名是 `config-<tag>.<时间戳>.yaml`，tag 有
    admin / before-rollback / verify 等，字典序会把 `config-admin.*` 永远排在
    最前，结果删掉的未必是最老的一份。list_backups() 也统一按 mtime 倒序展示。
    """
    try:
        rows = []
        for fn in os.listdir(BACKUP_DIR):
            if not fn.endswith(".yaml"):
                continue
            fp = os.path.join(BACKUP_DIR, fn)
            try:
                rows.append((os.stat(fp).st_mtime, fp))
            except OSError:
                continue
        rows.sort(reverse=True)                     # 新的在前
        for _, fp in rows[MAX_BACKUPS:]:
            try:
                os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass


def _backup(tag=""):
    """备份当前 config.yaml，返回备份路径。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = "config%s.%s.yaml" % ("-" + tag if tag else "", ts)
    dst = os.path.join(BACKUP_DIR, name)
    shutil.copy2(CONFIG_PATH, dst)
    try:
        # copy2 会连带复制源文件的 mtime；不刷成当前时间的话，新备份可能比旧备份
        # "还老"，紧接着的清理会把自己删掉。
        os.utime(dst, None)
    except OSError:
        pass
    _prune_backups()
    return dst


def write(text, tag="edit"):
    """保存配置。**先校验再备份再写**，任一步失败都不落盘。

    返回 (ok, message, detail)。text 必须是完整文件内容（文本编辑器模式）。
    """
    if text is None:
        return False, "内容不能为空", {}
    if len(text.encode("utf-8")) > MAX_BYTES:
        return False, "内容过大（上限 512KB）", {}
    ok, err = validate(text)
    if not ok:
        return False, "YAML 语法错误，未保存：" + err, {"stage": "validate"}

    try:
        data = yaml.safe_load(text)
    except Exception as e:                       # 理论上上面已过，兜底
        return False, "解析失败，未保存：%s" % e, {"stage": "validate"}

    if not isinstance(data, dict) or not data:
        return False, "配置内容为空或不是键值映射，已拒绝保存（否则主服务启动会崩）", \
            {"stage": "validate"}

    # 掩码写保护：页面上「遮住密钥」后前端拿到的是 ******** 已配置 ********，
    # 如果用户此时直接保存，会把真密钥覆盖成掩码 → 手机插件 / 大模型鉴权立刻全挂
    # 且**极难排查**（配置看起来是完整的，只有接口报 401）。所以这里检测到掩码就拒绝，
    # 让他先点「显示密钥」或直接填新值。
    masked = [i for i, l in enumerate((text or "").splitlines(), 1)
              if MASK_TOKEN in l]
    if masked:
        return False, ("第 %s 行还是被遮住的密钥（%s），保存会把真密钥覆盖成占位符、"
                       "导致手机插件或大模型鉴权全部失败。请取消「遮蔽密钥」后再保存，或直接填新值。"
                       % ("、".join(str(i) for i in masked[:6]), MASK_TOKEN)), \
            {"stage": "masked", "lines": masked}

    warns = _semantic_warn(data)

    bak = None
    try:
        bak = _backup(tag)
    except Exception as e:
        return False, "备份失败（为安全起见未保存）：%s" % e, {"stage": "backup"}

    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CONFIG_PATH)              # 原子替换，避免半写损坏
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "写入失败：%s" % e, {"stage": "write"}

    # 注意不能写成 `return True, ("...", {...})` —— 那样第二项被括号包成一个元组，
    # 隐式字符串拼接失效，返回值变成 (bool, (msg, detail))，所有调用方解包都会错位。
    detail = {
        "backup": os.path.basename(bak) if bak else None,
        "warnings": warns,
        "secret_lines": secret_lines(text),
        "needs_restart": True,
    }
    return True, ("已保存。配置在主服务启动时读取，**必须重启主服务后才生效**"
                  "（管理端「运行监控 → 服务控制」里点重启）"), detail


# ---------------------------------------------------------------- 群聊对话延续：定点写回
# 后台那几个开关不用 settings 覆盖表了，直接文本级写回 config.yaml 的这一段。
# 仍走 write() 的语法校验 / 密钥写保护 / 备份 / 原子替换，中文注释一个字都不会丢。
FOLLOWUP_KEYS = ("enabled", "window_minutes", "hint_enabled", "hint")
# 早期版本写进文件的秒单位键、以及从未生效过的 scope：写回时直接清掉，
# 保证 config.yaml 里只剩分钟配置，不留下会被忽略的僵尸行。
DEPRECATED_FOLLOWUP_KEYS = ("window_seconds", "scope")


def _yaml_scalar(v):
    """单行 YAML 标量：字符串用双引号（JSON 转义是合法 YAML 子集），中文不转义。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _tail_comment(body):
    """把一行按「引号外的 #」拆成 (值部分, 注释部分)，避免误伤文案里的 #。"""
    quote, esc = None, False
    for i, ch in enumerate(body):
        if esc:
            esc = False
            continue
        if quote == '"' and ch == "\\":
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return body[:i], body[i:]
    return body, ""


def _rewrite_followup(text, values):
    """只替换 reply.followup 段里的四个键；段 / 键缺失时补齐，其余字节原样保留。"""
    lines = text.splitlines(True)
    eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"

    def is_top(line):
        return bool(line.strip()) and not line[0].isspace() \
            and not line.lstrip().startswith("#")

    def block(indent_kv):
        return "".join("%s%s: %s%s" % (indent_kv, k, _yaml_scalar(values[k]), eol)
                       for k in FOLLOWUP_KEYS)

    reply = next((i for i, l in enumerate(lines)
                  if is_top(l) and l.split(":", 1)[0].strip() == "reply"), None)
    if reply is None:                       # 整个 reply 段都没有：追加到文件末尾
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += eol
        lines.append("reply:" + eol + "  followup:" + eol + block("    "))
        return "".join(lines)

    end = len(lines)
    for j in range(reply + 1, len(lines)):
        if is_top(lines[j]):
            end = j
            break

    fu = next((i for i in range(reply + 1, end)
               if not lines[i].lstrip().startswith("#")
               and lines[i].strip().split(":", 1)[0].strip() == "followup"), None)
    if fu is None:                          # reply 段在、followup 段不在
        lines.insert(end, "  followup:" + eol + block("    "))
        return "".join(lines)

    fu_indent = len(lines[fu]) - len(lines[fu].lstrip())
    fu_end = end
    for j in range(fu + 1, end):
        l = lines[j]
        if l.strip() and not l.lstrip().startswith("#") \
                and len(l) - len(l.lstrip()) <= fu_indent:
            fu_end = j
            break

    found, child_indent = set(), None
    for i in range(fu + 1, fu_end):
        line = lines[i]
        s = line.lstrip()
        if not s or s.startswith("#") or ":" not in line:
            continue
        if child_indent is None:
            child_indent = " " * (len(line) - len(s))
        key = s.split(":", 1)[0].strip()
        if key in DEPRECATED_FOLLOWUP_KEYS:
            lines[i] = ""            # 删掉整行（"" 占位不改变后续索引）
            continue
        if key not in values:
            continue
        pos = line.index(":") + 1
        prefix = line[:pos]
        rest, comment = _tail_comment(line[pos:].rstrip("\r\n"))
        val = _yaml_scalar(values[key])
        if comment:
            # 尽量把行尾注释保持在原来的列上，配置模板才不会越改越乱
            pad = max(2, pos + len(rest) - (len(prefix) + 1 + len(val)))
            lines[i] = prefix + " " + val + " " * pad + comment.rstrip() + eol
        else:
            lines[i] = prefix + " " + val + eol
        found.add(key)

    missing = [k for k in FOLLOWUP_KEYS if k not in found]
    if missing:
        ind = child_indent or " " * (fu_indent + 2)
        lines.insert(fu_end, "".join(
            "%s%s: %s%s" % (ind, k, _yaml_scalar(values[k]), eol) for k in missing))
    return "".join(lines)


def write_followup(values):
    """后台「保存并生效」：把 reply.followup 文本级写回 config.yaml。

    返回 (ok, message, detail)。写盘复用 write()，因此同样先校验、先备份、原子替换。
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return False, "读取 config.yaml 失败：%s" % e, {}
    vals = {k: values[k] for k in FOLLOWUP_KEYS if k in values}
    if not vals:
        return False, "没有要保存的配置", {}
    ok, msg, detail = write(_rewrite_followup(text, vals), tag="followup")
    if not ok:
        return False, msg, detail
    detail["needs_restart"] = False       # 这段配置是运行时读的，不需要重启
    return True, "已写入 config.yaml，立即生效", detail


def list_backups():
    """备份列表，新的在前（按修改时间，不按文件名——tag 不同时字典序会骗人）。"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in os.listdir(BACKUP_DIR):
        if not fn.endswith(".yaml"):
            continue
        fp = os.path.join(BACKUP_DIR, fn)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        out.append({"name": fn, "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def read_backup(name):
    name = os.path.basename(name or "")
    if (not name.endswith(".yaml") or "/" in name or "\\" in name or ".." in name):
        raise ValueError("备份名不合法")
    fp = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(fp):
        raise FileNotFoundError("备份不存在")
    with open(fp, encoding="utf-8") as f:
        return f.read()


def delete_backup(name):
    """手工删除一份备份。返回被删名字与剩余份数；名字不合法 / 不存在一律抛异常。"""
    name = os.path.basename(name or "")
    if (not name.endswith(".yaml") or "/" in name or "\\" in name or ".." in name):
        raise ValueError("备份名不合法")
    fp = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(fp):
        raise FileNotFoundError("备份不存在")
    os.remove(fp)
    return {"name": name, "remaining": len(list_backups())}


def rollback(name):
    """回滚到指定备份。会先给「当前配置」再备一份（回滚错了还能回来）。"""
    text = read_backup(name)                     # 校验过才继续
    ok, err = validate(text)
    if not ok:
        return False, "该备份自身语法错误，未回滚：" + err
    bak = _backup("before-rollback")
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, "回滚失败：%s" % e
    return True, ("已回滚到 %s。当前配置已另存为 %s。**需重启服务才生效**。"
                  % (name, os.path.basename(bak)))


