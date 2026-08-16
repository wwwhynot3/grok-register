"""统一路径与日志配置。

数据目录默认 ~/.grok-register —— 把 keys/auths/logs 挪出项目目录,
避免重演"误 git add 把 token 暂存进 index"的事故。
可用 GROK_DATA_DIR 整体覆盖,或 GROK_AUTH_DIR / GROK_KEYS_DIR / GROK_LOG_DIR 单独覆盖。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

# 浏览器 UA 单一来源(2026-08-16:grok.py/grok_free.py Chrome/144 vs email_service
# Chrome/145 不一致,CF 指纹相关常量统一此处)。改版本时同步 curl_cffi impersonate。
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

# 代理单一来源(2026-08-16 G 阶段:grok/auto_replenish/device_mint 各有一份
# 相同解析)。GROK_USE_PROXY=0/false/no/off 时禁用(直连,如美国 VPS)。
PROXY = os.getenv("GROK_PROXY") or "http://127.0.0.1:7897"
if os.getenv("GROK_USE_PROXY", "1").strip().lower() in ("0", "false", "no", "off"):
    PROXY = ""  # 禁用代理 → 直连

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.getenv("GROK_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".grok-register")
AUTH_DIR = os.getenv("GROK_AUTH_DIR") or os.path.join(DATA_ROOT, "auths")
KEYS_DIR = os.getenv("GROK_KEYS_DIR") or os.path.join(DATA_ROOT, "keys")
LOG_DIR = os.getenv("GROK_LOG_DIR") or os.path.join(DATA_ROOT, "logs")

for _d in (DATA_ROOT, AUTH_DIR, KEYS_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


def setup_logging(logfile: str = "grok.log") -> logging.Logger:
    """根 logger:控制台 + {LOG_DIR}/{logfile} 轮转(5MB×5)。幂等,可被多模块重复调用。

    之前各脚本只有 print,无持久化日志,故障只能靠 systemd journal 排查;
    现在所有入口都写文件日志,本机(无 systemd)也能翻日志。
    """
    logger = logging.getLogger()
    if getattr(logger, "_grok_configured", False):
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        fh = RotatingFileHandler(
            os.path.join(LOG_DIR, logfile),
            maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as e:
        print(f"[config] 日志文件不可写,仅控制台: {e}")
    setattr(logger, "_grok_configured", True)
    return logger
