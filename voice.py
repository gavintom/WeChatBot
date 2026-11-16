"""语音消息处理：语音二进制 → 转码 → ASR 识别。

链路：
    webhook 收到 type=34 语音 → GET /api/media?chat=..&localID=.. 拿到 SILK 二进制
      → pilk 解 SILK→WAV（可选依赖）/ ffmpeg 转 mp3
      → 按 provider 预设调用 ASR
      → 返回识别文本

素材是 webhook 里直接下载的 bytes，不需要 access_token / media_id。

两种 ASR 协议（config 里 provider 选预设即自动匹配）：
  · openai    —— multipart POST {base}/audio/transcriptions（OpenAI / 硅基流动 / 自建 whisper）
  · qwen_asr  —— POST {base}/chat/completions，音频走 base64（阿里云百炼 Qwen3-ASR）

内置预设（provider 填名字即可，base_url / model / 协议自动带出）：
  · siliconflow —— 硅基流动 FunAudioLLM/SenseVoiceSmall，中文优化、免费，推荐
  · dashscope   —— 阿里云百炼 qwen3-asr-flash
  · openai      —— OpenAI whisper-1
  · custom      —— 全部手填；asr_base_url 留空则复用 brain.external_llm
  · selfhosted  —— 自建 ASR 服务：multipart POST 你给的完整 endpoint，返回 {"text":...}，
                   无需密钥（适合内网/本地 whisper 等自建服务）

任何一步失败都返回空串，由调用方回兜底话术，绝不抛异常拖垮轮询线程。
"""
import base64
import os
import shutil
import subprocess
import tempfile

import requests

DEFAULT_ASR_MODEL = "whisper-1"

# —— ASR 服务商预设：provider 选中后，未显式填写的字段由预设补齐 ——
PROVIDERS = {
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "protocol": "openai",
        "language": "",          # SenseVoice 自动识别语种，不传 language
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-asr-flash",
        "protocol": "qwen_asr",
        "language": "zh",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "whisper-1",
        "protocol": "openai",
        "language": "zh",
    },
    "custom": {},
    "selfhosted": {
        "base_url": "",
        "model": "",
        "protocol": "selfhosted",
        "language": "",
    },
}


def resolve_asr(cfg):
    """把 config 的 voice 段解析成实际调用参数（provider 预设 → 显式配置 → 外部 LLM 兜底）。

    优先级：显式填的字段 > provider 预设 > brain.external_llm（仅 provider=custom 时）。
    """
    cfg = cfg or {}
    provider = str(cfg.get("provider") or "custom").strip().lower()
    preset = PROVIDERS.get(provider) or {}

    base = (cfg.get("asr_base_url") or preset.get("base_url") or "").rstrip("/")
    key = cfg.get("asr_api_key") or ""
    model = cfg.get("asr_model") or preset.get("model") or DEFAULT_ASR_MODEL
    protocol = str(cfg.get("asr_protocol") or preset.get("protocol") or "openai").lower()

    language = cfg.get("asr_language")
    if language is None:
        language = preset.get("language", "")

    # 仅 provider=custom 才回落到外部 LLM（同源 OpenAI 兼容服务）；
    # 用了预设就不该把别家的密钥发过去。
    if provider == "custom":
        if not base:
            base = (cfg.get("_llm_base_url") or "").rstrip("/")
        if not key:
            key = cfg.get("_llm_api_key") or ""

    return {"provider": provider, "protocol": protocol, "base_url": base,
            "api_key": key, "model": model, "language": language}


def _log(log, msg, *a):
    if log:
        try:
            log(msg, *a)
        except Exception:
            pass


def decode_silk(raw, log=None):
    """把 iOS 微信语音（SILK_V3）解成 16kHz 单声道 WAV，返回 bytes；失败返回 None。

    插件 v1.2.0 的 /api/media 按文件头把语音标成 `audio/silk`（微信原生
    SILK_V3，可能带 1 字节 0x02 前缀），多数 ASR 不认这个格式。
    可选依赖 `pilk`（pip install pilk）能在纯 Python 里解码；没装就交给 ffmpeg
    碰运气，再不行由调用方直传 ASR / 回兜底话术。
    """
    if not raw:
        return None
    data = raw
    # 微信语音有时在 #!SILK_V3 前多一个 0x02 字节
    if data[:1] == b"\x02" and data[1:10] == b"#!SILK_V3":
        data = data[1:]
    if not data.startswith(b"#!SILK_V3"):
        return None
    try:
        import io
        import wave
        import pilk
        src = pcm = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as f:
                f.write(data)
                src = f.name
            pcm = src + ".pcm"
            pilk.decode(src, pcm)
            with open(pcm, "rb") as f:
                pcm_bytes = f.read()
            if not pcm_bytes:
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm_bytes)
            return buf.getvalue()
        finally:
            for p in (src, pcm):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
    except ImportError:
        _log(log, "未安装 pilk，SILK 语音无法解码（pip install pilk）")
    except Exception as e:
        _log(log, "SILK 解码失败: %s", e)
    return None


def _ffmpeg_bin(cfg):
    """返回可用的 ffmpeg 路径；没有则 None。

    查找顺序：① config.ffmpeg_path / PATH 里的 ffmpeg；
              ② imageio-ffmpeg 自带的静态二进制（`pip install imageio-ffmpeg`，
                 无需 Homebrew，macOS/Windows/Linux 通用）。
    """
    p = (cfg or {}).get("ffmpeg_path") or "ffmpeg"
    hit = shutil.which(p)
    if hit:
        return hit
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


def transcode(raw, cfg, log=None, src_ext="amr"):
    """把语音转成 16kHz / 单声道 MP3 交给 ASR。返回 (bytes, filename)。

    src_ext：源文件真实扩展名（silk / amr / mp3 / wav / m4a）。
      - silk 先用 pilk 解成 16kHz / 16bit / 单声道 PCM WAV；
      - 默认（voice.transcode=true）用 ffmpeg 转成 **16kHz 单声道 MP3**
        （码率 voice.mp3_bitrate，默认 32k）再上传：10 秒约 40KB，
        比 16bit PCM WAV（约 312KB）小 8 倍、比旧的 64k MP3 小一半；
      - voice.transcode=false 时把 WAV 原样直传（不依赖 ffmpeg）。
    MP3 是有损压缩格式，没有「位深」概念；16bit 指上游 pilk 解出的 PCM。
    任何一步失败都原样返回，让 ASR 端碰运气或由调用方回兜底话术。
    """
    ext = (src_ext or "amr").lower().lstrip(".")
    if ext == "silk":
        wav = decode_silk(raw, log=log)
        if wav:
            raw, ext = wav, "wav"
    cfg = cfg or {}
    fallback = "voice." + ("silk" if ext == "silk" else ext)
    # 没解出来的 SILK 不能直传（ASR 不认），即使关了转码也要让 ffmpeg 兜底。
    if not cfg.get("transcode", True) and ext != "silk":
        _log(log, "语音不转码直传 | 格式=%s 字节=%d", ext, len(raw))
        return raw, fallback
    ff = _ffmpeg_bin(cfg)
    if not ff:
        _log(log, "未找到 ffmpeg，语音将以 %s 原样直传 ASR", fallback)
        return raw, fallback
    # 只接受 NNk(8~320)，避免把任意字符串塞进 ffmpeg 命令行
    br = str(cfg.get("mp3_bitrate") or "32k").strip().lower()
    if not (br.endswith("k") and br[:-1].isdigit() and 8 <= int(br[:-1]) <= 320):
        br = "32k"
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as f:
            f.write(raw)
            src = f.name
        dst = src + ".mp3"
        r = subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", src,
             "-ar", "16000", "-ac", "1", "-b:a", br, dst],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(dst):
            _log(log, "ffmpeg 转码失败（改原样直传）: %s",
                 (r.stderr or b"")[:200])
            return raw, fallback
        with open(dst, "rb") as f:
            out = f.read()
        _log(log, "语音转码 MP3 | %d→%d 字节 | 16kHz/单声道 %s",
             len(raw), len(out), br)
        return out, "voice.mp3"
    except Exception as e:
        _log(log, "转码异常（改原样直传）: %s", e)
        return raw, fallback
    finally:
        for p in (src, dst):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def _asr_openai(a, audio_bytes, filename, log=None):
    """OpenAI 兼容协议：multipart POST /audio/transcriptions。"""
    url = a["base_url"] + "/audio/transcriptions"
    data = {"model": a["model"]}
    if a.get("language"):
        data["language"] = a["language"]
    try:
        r = requests.post(url, headers={"Authorization": "Bearer " + a["api_key"]},
                          files={"file": (filename, audio_bytes)},
                          data=data, timeout=120)
    except Exception as e:
        _log(log, "ASR 请求异常(%s): %s", url, e)
        return ""
    try:
        j = r.json()
    except ValueError:
        _log(log, "ASR 返回非 JSON（HTTP %s）: %s", r.status_code, r.text[:200])
        return ""
    if r.status_code != 200 or (isinstance(j, dict) and j.get("code")):
        _log(log, "ASR 失败 HTTP %s: %s", r.status_code, str(j)[:300])
        return ""
    return (j.get("text") or "").strip()


def _asr_selfhosted(a, audio_bytes, filename, log=None):
    """自建 ASR 服务：multipart POST 完整 endpoint（如 http://host:port/asr）。

    请求体：files={'file': (filename, audio)}，可选 data={'model'/'language'}。
    返回 JSON 形如 {"text": "...", "finished": true, "idx": 0}；直接取 text 字段。
    无鉴权（内网/本地服务），不传 Authorization 头。
    """
    url = a["base_url"]            # 直接是完整 endpoint，不再拼 /audio/transcriptions
    data = {}
    if a.get("model"):
        data["model"] = a["model"]
    if a.get("language"):
        data["language"] = a["language"]
    try:
        # 自建 ASR 一般在内网/本机，绕过环境代理直连
        sess = requests.Session()
        sess.trust_env = False
        r = sess.post(url, files={"file": (filename, audio_bytes)},
                      data=data, timeout=120)
    except Exception as e:
        _log(log, "ASR 请求异常(selfhosted %s): %s", url, e)
        return ""
    try:
        j = r.json()
    except ValueError:
        _log(log, "ASR 返回非 JSON（HTTP %s）: %s", r.status_code, r.text[:200])
        return ""
    if r.status_code != 200 or (isinstance(j, dict) and j.get("code")):
        _log(log, "ASR 失败 HTTP %s: %s", r.status_code, str(j)[:300])
        return ""
    return (j.get("text") or "").strip()


def _asr_qwen(a, audio_bytes, filename, log=None):
    """阿里云百炼 Qwen3-ASR（OpenAI 兼容 chat/completions，音频走 base64 data URL）。"""
    ext = os.path.splitext(filename)[1].lower().lstrip(".") or "mp3"
    mime = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
            "amr": "audio/amr", "silk": "audio/silk", "ogg": "audio/ogg",
            "flac": "audio/flac"}.get(ext, "audio/mpeg")
    data_url = "data:%s;base64,%s" % (mime, base64.b64encode(audio_bytes).decode("ascii"))
    body = {
        "model": a["model"],
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_url}}]}],
        "stream": False,
    }
    asr_options = {"enable_itn": True}
    if a.get("language"):
        asr_options["language"] = a["language"]
    body["asr_options"] = asr_options
    try:
        r = requests.post(a["base_url"] + "/chat/completions",
                          headers={"Authorization": "Bearer " + a["api_key"],
                                   "Content-Type": "application/json"},
                          json=body, timeout=60)
    except Exception as e:
        _log(log, "ASR 请求异常(qwen): %s", e)
        return ""
    try:
        j = r.json()
    except ValueError:
        _log(log, "ASR 返回非 JSON（HTTP %s）: %s", r.status_code, r.text[:200])
        return ""
    if r.status_code != 200 or j.get("error"):
        _log(log, "ASR 失败 HTTP %s: %s", r.status_code, str(j)[:300])
        return ""
    try:
        content = j["choices"][0]["message"]["content"]
    except Exception:
        _log(log, "ASR 返回结构异常: %s", str(j)[:300])
        return ""
    if isinstance(content, list):        # 少数版本返回 [{"text": "..."}]
        content = "".join(seg.get("text", "") for seg in content if isinstance(seg, dict))
    return (content or "").strip()


def asr_transcribe(audio_bytes, filename, cfg, log=None):
    """按 provider 预设调用 ASR，返回识别文本；失败返回 ''。"""
    a = resolve_asr(cfg)
    # selfhosted 自建服务无需密钥；其余协议必须同时有端点与密钥
    if not a["base_url"] or (a["provider"] != "selfhosted" and not a["api_key"]):
        _log(log, "未配置 ASR 端点/密钥（provider=%s），跳过语音识别", a["provider"])
        return ""
    _log(log, "ASR 请求 | provider=%s protocol=%s model=%s file=%s(%d字节)",
         a["provider"], a["protocol"], a["model"], filename, len(audio_bytes))
    if a["protocol"] == "qwen_asr":
        return _asr_qwen(a, audio_bytes, filename, log=log)
    if a["protocol"] == "selfhosted":
        return _asr_selfhosted(a, audio_bytes, filename, log=log)
    return _asr_openai(a, audio_bytes, filename, log=log)


def voice_bytes_to_text(raw, cfg, log=None, src_ext="silk"):
    """webhook 语音入口：二进制 → 转码 → ASR。失败返回 ''。

    素材已由 `GET /api/media` 下载成 bytes，这里只做转码与识别。
    """
    if not raw:
        _log(log, "语音二进制为空")
        return ""
    _log(log, "语音素材 %d 字节 | 源格式=%s", len(raw), src_ext)
    data, fn = transcode(raw, cfg, log=log, src_ext=src_ext)
    txt = asr_transcribe(data, fn, cfg, log=log)
    if txt:
        _log(log, "ASR 识别成功（%s）: %s", fn, txt[:60])
    else:
        _log(log, "ASR 未返回文本（文件=%s）", fn)
    return txt


def describe_asr(cfg):
    """给启动日志用：返回一行可读的 ASR 配置说明（密钥打码）。"""
    a = resolve_asr(cfg)
    if not a["base_url"] or (a["provider"] != "selfhosted" and not a["api_key"]):
        return "未配置（语音消息只会回兜底话术）"
    if a["provider"] == "selfhosted":
        return "provider=%s | endpoint=%s | 自建(无密钥)" % (a["provider"], a["base_url"])
    k = a["api_key"]
    masked = (k[:6] + "***" + k[-4:]) if len(k) > 12 else "***"
    return "provider=%s | protocol=%s | model=%s | key=%s" % (
        a["provider"], a["protocol"], a["model"], masked)
