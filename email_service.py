"""
邮箱服务类
对外保持接口不变：
- create_email() -> (token_like, email)
- fetch_first_email(token_like) -> str | None

支持 provider:
- gptmail（默认，GPTMail 免费 API）
- mailtm（免费，mail.tm API，无需 Key，推荐）
- luckmail（购买邮箱 + token 轮询）
- mailnest（MailNest 付费 API）
"""

import json
import os
import re
import random
import string as _string
import urllib.parse
from typing import Any, Dict, List, Optional

from curl_cffi import requests

# 标准 requests 用于 mail.tm（避免 curl_cffi TLS 兼容问题）
import requests as std_requests

try:
    from luckmail import LuckMailClient
    from luckmail.exceptions import LuckMailError
    # LuckMail SDK 的 _is_async_context() 用 get_event_loop().is_running() 判定,
    # 在 patchright sync API 启动后(其内部持有一个持续运行的 event loop,set_event_loop
    # 到当前线程)会误判为异步上下文 → 返回协程对象而非结果。
    # 本代码库所有 LuckMail 调用均为同步,强制走 sync 分支。
    import luckmail.http_client as _luckmail_http
    import luckmail.user as _luckmail_user
    import luckmail.supplier as _luckmail_supplier
    _f = lambda: False  # noqa: E731 强制同步分支
    _luckmail_http._is_async_context = _f
    _luckmail_user._is_async_context = _f
    _luckmail_supplier._is_async_context = _f
except Exception:
    LuckMailClient = None
    class LuckMailError(Exception):
        pass

from config import BROWSER_UA as UA


def _luckmail_settings() -> dict:
    return {
        "base_url": str(os.getenv("LUCKMAIL_BASE_URL") or "https://mails.luckyous.com").strip().rstrip("/"),
        "api_key": str(os.getenv("LUCKMAIL_API_KEY") or "").strip(),
        "api_secret": str(os.getenv("LUCKMAIL_API_SECRET") or "").strip(),
        "use_hmac": str(os.getenv("LUCKMAIL_USE_HMAC") or "").strip().lower() in {"1", "true", "yes", "y", "on"},
        "project_code": str(os.getenv("LUCKMAIL_PROJECT_CODE") or "grok").strip(),
        "email_type": str(os.getenv("LUCKMAIL_EMAIL_TYPE") or "ms_imap").strip(),
        "domain": str(os.getenv("LUCKMAIL_DOMAIN") or "outlook.com").strip(),
    }


def _luckmail_order_settings() -> dict:
    """接码订单模式配置。

    成本 = 仅 code_price,收到验证码才扣费(2026-08-16 实测:
    google_variant 0.006 / self_built 0.001 / ms 0.0125,均低于买断模式)。
    email_types 为回退链:第一个能下单成功的类型生效。
    """
    s = _luckmail_settings()
    s["email_types"] = [
        t.strip() for t in
        (os.getenv("LUCKMAIL_ORDER_TYPES") or "google_variant,self_built,ms_imap").split(",")
        if t.strip()
    ]
    s["variant_mode"] = str(os.getenv("LUCKMAIL_VARIANT_MODE") or "").strip() or None
    return s


def _mailnest_settings() -> dict:
    return {
        "api_key": str(os.getenv("MAILNEST_API_KEY") or "").strip(),
        "project_code": str(os.getenv("MAILNEST_PROJECT_CODE") or "x-ai001").strip(),
    }




class MailTMClient:
    """mail.tm 免费临时邮箱 — 无需 API Key，无需注册
    API 文档: https://docs.mail.tm/

    用法:
        client = MailTMClient()
        email = client.create_email()
        content = client.fetch_first_email()  # 阻塞轮询，最多 60s
    """

    BASE = "https://api.mail.tm"

    def __init__(self, proxies: Any = None):
        self.session = std_requests.Session()
        if proxies:
            self.session.proxies.update(proxies)
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
        })
        self.address = ""
        self._token = ""
        self._password = ""

    def _random_user(self) -> str:
        return "grok" + "".join(random.choices(_string.ascii_lowercase + _string.digits, k=10))

    def create_email(self) -> str:
        """创建临时邮箱，返回邮箱地址"""
        # 1. 获取可用域名
        r = self.session.get(f"{self.BASE}/domains", timeout=15)
        r.raise_for_status()
        data = r.json()
        # API 有时返回 list，有时返回 hydra:Collection 包装
        if isinstance(data, list):
            domains = data
        else:
            domains = data.get("hydra:member", [])
        if not domains:
            raise RuntimeError("mail.tm: 无可用域名")
        domain = domains[0]["domain"]

        # 2. 注册账号
        self._password = "Gk" + "".join(random.choices(_string.ascii_letters + _string.digits, k=14)) + "!1"
        self.address = f"{self._random_user()}@{domain}"
        r2 = self.session.post(f"{self.BASE}/accounts", json={
            "address": self.address,
            "password": self._password,
        }, timeout=15)
        if r2.status_code != 201:
            raise RuntimeError(f"mail.tm 创建账号失败: {r2.status_code} {r2.text[:200]}")
        account = r2.json()
        self.address = account["address"]

        # 3. 获取 JWT token
        r3 = self.session.post(f"{self.BASE}/token", json={
            "address": self.address,
            "password": self._password,
        }, timeout=15)
        r3.raise_for_status()
        self._token = r3.json()["token"]

        return self.address

    def fetch_first_email(self) -> Optional[str]:
        """单次检查收件箱，返回第一封邮件内容（或 None）。
        调用方负责轮询（grok.py 外层每 5s 调用一次，共 12 次 = 60s）。"""
        if not self._token:
            return None
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            r = self.session.get(f"{self.BASE}/messages", headers=headers, timeout=10)
            if r.status_code == 401:
                # token 过期，尝试刷新
                r2 = self.session.post(f"{self.BASE}/token", json={
                    "address": self.address, "password": self._password,
                }, timeout=10)
                if r2.status_code == 200:
                    self._token = r2.json()["token"]
                    headers = {"Authorization": f"Bearer {self._token}"}
                    r = self.session.get(f"{self.BASE}/messages", headers=headers, timeout=10)
            if r.status_code != 200:
                return None
            _msg_data = r.json()
            messages = _msg_data if isinstance(_msg_data, list) else _msg_data.get("hydra:member", [])
            if not messages:
                return None
            msg_id = messages[0]["id"]
            # 获取完整邮件内容
            r3 = self.session.get(f"{self.BASE}/messages/{msg_id}", headers=headers, timeout=10)
            if r3.status_code != 200:
                return None
            msg = r3.json()
            parts = []
            if msg.get("subject"):
                parts.append(msg["subject"])
            if msg.get("text"):
                parts.append(msg["text"])
            if msg.get("html"):
                parts.append("\n".join(msg["html"]) if isinstance(msg["html"], list) else str(msg["html"]))
            if not parts:
                parts.append(msg.get("intro", ""))
                parts.append(str(msg))
            return "\n".join(parts) if parts else str(msg)
        except Exception:
            return None


class LuckMailInbox:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str = "",
        use_hmac: bool = False,
        project_code: str = "grok",
        email_type: str = "ms_imap",
        domain: str = "outlook.com",
        timeout: int = 30,
    ):
        if LuckMailClient is None:
            raise RuntimeError("LuckMail SDK 不可用")
        if not base_url:
            raise RuntimeError("缺少 LUCKMAIL_BASE_URL")
        if not api_key:
            raise RuntimeError("缺少 LUCKMAIL_API_KEY")

        self.client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret or None,
            use_hmac=bool(use_hmac),
            timeout=float(timeout),
        )
        self.project_code = project_code or "grok"
        self.email_type = email_type or "ms_imap"
        self.domain = domain or "outlook.com"
        self.address = ""
        self.token = ""

    def create_email(self):
        try:
            result = self.client.user.purchase_emails(
                project_code=self.project_code,
                quantity=1,
                email_type=self.email_type,
                domain=self.domain,
            )
        except LuckMailError as e:
            raise RuntimeError(f"LuckMail 购买邮箱失败: {e}") from e
        except Exception as e:
            raise RuntimeError(f"LuckMail 初始化失败: {e}") from e

        purchases = list((result or {}).get("purchases") or [])
        if not purchases:
            raise RuntimeError("LuckMail 购买邮箱失败：未返回 purchases")

        purchase = purchases[0] or {}
        self.address = str(purchase.get("email_address") or "").strip()
        self.token = str(purchase.get("token") or "").strip()
        if not self.address or not self.token:
            raise RuntimeError("LuckMail 购买邮箱失败：缺少 email_address 或 token")
        return {"provider": "luckmail", "token": self.token, "email": self.address, "client": self}, self.address

    def fetch_first_email(self) -> Optional[str]:
        if not self.token:
            return None
        try:
            result = self.client.user.get_token_code(self.token)
            chunks: List[str] = []
            if getattr(result, "verification_code", None):
                chunks.append(str(getattr(result, "verification_code", "") or ""))
            if getattr(result, "mail", None):
                chunks.append(json.dumps(getattr(result, "mail", None) or {}, ensure_ascii=False))

            mail_list = self.client.user.get_token_mails(self.token)
            mails = list(getattr(mail_list, "mails", []) or [])
            if mails:
                mail = mails[0]
                message_id = str(getattr(mail, "message_id", "") or "").strip()
                chunks.extend([
                    str(getattr(mail, "subject", "") or ""),
                    str(getattr(mail, "body", "") or ""),
                    str(getattr(mail, "html_body", "") or ""),
                ])
                if message_id:
                    try:
                        detail = self.client.user.get_token_mail_detail(self.token, message_id)
                        chunks.extend([
                            str(getattr(detail, "subject", "") or ""),
                            str(getattr(detail, "body_text", "") or ""),
                            str(getattr(detail, "body_html", "") or ""),
                            str(getattr(detail, "verification_code", "") or ""),
                        ])
                    except Exception:
                        pass
            text = "\n".join([c for c in chunks if c])
            return text or None
        except Exception as e:
            print(f"获取 LuckMail 邮件失败: {e}")
            return None


class LuckMailOrderInbox:
    """LuckMail 接码订单模式 — 按成功收费(code_price),无码不扣费。

    与买断模式(LuckMailInbox)的区别(2026-08-16 实测):
    - 买断: 先付 buy_price(google 0.01 / self_built 0.002 / ms 0.02),24h 保修可申诉退款
    - 接码: create_order 免费分配邮箱,收到验证码才付 code_price
      (google 0.006 / self_built 0.001 / ms 0.0125);失败/超时 0 成本
    - 接码更便宜且失败免费;买断保留给需要 24h 重试/申诉的场景

    create_email: 依次尝试 email_types 回退链,第一个下单成功的生效。
    fetch_first_email: 轮询 get_order_code,success 返回验证码内容。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str = "",
        use_hmac: bool = False,
        project_code: str = "grok",
        email_types: Optional[List[str]] = None,
        variant_mode: Optional[str] = None,
        timeout: int = 30,
    ):
        if LuckMailClient is None:
            raise RuntimeError("LuckMail SDK 不可用")
        if not base_url:
            raise RuntimeError("缺少 LUCKMAIL_BASE_URL")
        if not api_key:
            raise RuntimeError("缺少 LUCKMAIL_API_KEY")

        self.client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret or None,
            use_hmac=bool(use_hmac),
            timeout=float(timeout),
        )
        self.project_code = project_code or "grok"
        self.email_types = list(email_types or ("google_variant", "self_built", "ms_imap"))
        self.variant_mode = variant_mode
        self.order_no = ""
        self.address = ""
        self.email_type = ""

    def create_email(self):
        errors = []
        for et in self.email_types:
            try:
                kwargs = dict(project_code=self.project_code, email_type=et)
                if et == "google_variant" and self.variant_mode:
                    kwargs["variant_mode"] = self.variant_mode
                order = self.client.user.create_order(**kwargs)
                self.order_no = str(getattr(order, "order_no", "") or "").strip()
                self.address = str(getattr(order, "email_address", "") or "").strip()
                if not self.order_no or not self.address:
                    raise RuntimeError(f"订单返回空: {order!r}")
                self.email_type = et
                return (
                    {"provider": "luckmail_order", "token": self.order_no,
                     "email": self.address, "client": self, "email_type": et},
                    self.address,
                )
            except Exception as e:
                errors.append(f"{et}: {e}")
                print(f"[LuckMailOrder] {et} 下单失败: {e}")
        raise RuntimeError("LuckMail 订单创建全部失败: " + "; ".join(errors))

    def fetch_first_email(self) -> Optional[str]:
        if not self.order_no:
            return None
        try:
            code = self.client.user.get_order_code(self.order_no)
            status = str(getattr(code, "status", "") or "")
            if status == "success":
                vc = str(getattr(code, "verification_code", "") or "")
                subject = str(getattr(code, "mail_subject", "") or "")
                body = str(getattr(code, "mail_body_html", "") or "")
                return "\n".join(x for x in (vc, subject, body) if x) or None
            if status in ("timeout", "cancelled"):
                # 订单终结且无码(0 成本)——返回 None,由调用方按超时处理
                return None
            return None  # pending: 继续轮询
        except Exception as e:
            print(f"[LuckMailOrder] 查询验证码失败: {e}")
            return None


class MailNestInbox:
    def __init__(
            self,
            api_key: str,
            project_code: str = "z-ai001",
            timeout: int = 30,
    ):
        if not api_key:
            raise RuntimeError("缺少 MailNest_API_KEY")

        self.api_key = api_key
        self.project_code = project_code
        self.timeout = timeout

    def __req(self, method, url, params=None, json=None):
        resp = requests.request(
            method,
            url,
            params=params,
            json=json,
            headers={
                "Authorization": f"Bearer {self.api_key}",
            },
            verify=False,
        )
        if resp.status_code == 401:
            print('api-key 无效')
            raise Exception('api-key 无效')
        resp.raise_for_status()
        resp_json = resp.json()
        print(resp_json)
        if resp_json['code'] != '00000':
            raise Exception(f'{resp_json}')
        return resp_json['data']

    def create_email(self):
        email = ''
        try:
            email = self.__req(
                'POST',
                "https://mailnest.top/api/v1/email/temporary/buy",
                json={
                    "project_code": self.project_code,
                    "count": 1,
                }
            )[0]['email']
        except:
            pass
        # 当项目邮箱数量不足时会获取失败 或 没有这个项目 购买独占邮箱
        if not email:
            try:
                email = self.__req(
                    'POST',
                    "https://mailnest.top/api/v1/email/exclusive/buy",
                    json={
                        "count": 1,
                    }
                )[0]['email']
            except:
                pass
        if not email:
            raise RuntimeError("MailNest 购买邮箱失败")
        return {"provider": "mailnest", "token": self.api_key, "email": email, "client": self}, email

    def fetch_first_email(self, email) -> Optional[str]:
        if not self.api_key:
            return None
        try:
            mails = self.__req(
                'POST',
                f'https://mailnest.top/api/v1/email/receive',
                json={
                    "email": email,
                },
            )
            if not mails:
                return None
            return '\n'.join([
                mails[0]['subject'],
                mails[0]['body_preview'],
                mails[0]['body'],
            ]) or None
        except Exception as e:
            print(f"获取 MailNest 邮件失败: {e}")
            return None


class GPTMailInboxV2:
    """GPTMail V2 客户端 — 使用新版 API（2026-07）

    API 流程:
      1. GET  /api/domains/public  → 获取活跃域名列表
      2. 客户端拼邮箱: prefix@random_domain
      3. POST /api/inbox-token     → 注册邮箱，获取 JWT token
      4. GET  /api/emails?email=.. → 轮询邮件
      5. GET  /api/email/{id}      → 获取邮件详情
    """

    def __init__(self, proxies: Any = None):
        self.base_url = "https://mail.chatgpt.org.uk"
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
        })
        self.email = ""
        self.token = ""
        self._domains = []

    def _warmup(self):
        try:
            self.session.get(f"{self.base_url}/", timeout=10)
        except Exception:
            pass

    def _get_domains(self):
        if self._domains:
            return self._domains
        self._warmup()
        r = self.session.get(f"{self.base_url}/api/domains/public", timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"获取域名失败: {r.status_code}")
        data = r.json()
        domains_list = (data.get("data") or {}).get("domains") or []
        self._domains = [d["domain_name"] for d in domains_list if d.get("is_active")]
        if not self._domains:
            raise RuntimeError("无活跃域名")
        return self._domains

    def create_email(self) -> str:
        domains = self._get_domains()
        prefix = "".join(random.choices(_string.ascii_lowercase + _string.digits, k=10))
        domain = random.choice(domains)
        self.email = f"{prefix}@{domain}"

        r = self.session.post(
            f"{self.base_url}/api/inbox-token",
            headers={"Content-Type": "application/json"},
            json={"email": self.email},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"inbox-token 失败: {r.status_code}")
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"inbox-token 返回失败: {data}")
        self.token = (data.get("auth") or {}).get("token") or ""
        if not self.token:
            raise RuntimeError("未获取到 inbox token")
        return self.email

    def fetch_first_email(self) -> Optional[str]:
        if not self.token:
            return None
        try:
            encoded = urllib.parse.quote(self.email)
            r = self.session.get(
                f"{self.base_url}/api/emails?email={encoded}",
                headers={"x-inbox-token": self.token},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            emails = (data.get("data") or {}).get("emails") or data.get("data") or []
            if isinstance(emails, dict):
                emails = [emails]
            for msg in (emails if isinstance(emails, list) else []):
                subject = str(msg.get("subject") or "")
                body = str(msg.get("text") or msg.get("html") or msg.get("body") or "")
                text = subject + " " + body
                # 先检查是否有验证码
                m = re.search(r"([A-Z0-9]{3})-?([A-Z0-9]{3})", text)
                if m:
                    return text
                # 有内容就返回
                if len(text) > 10:
                    return text
            # 尝试获取详情
            for msg in (emails if isinstance(emails, list) else []):
                msg_id = msg.get("id") or msg.get("message_id")
                if msg_id:
                    r2 = self.session.get(
                        f"{self.base_url}/api/email/{urllib.parse.quote(str(msg_id))}",
                        headers={"x-inbox-token": self.token},
                        timeout=15,
                    )
                    if r2.status_code == 200:
                        detail = r2.json()
                        d = (detail.get("data") or detail)
                        text = str(d.get("subject") or "") + " " + str(d.get("text") or d.get("html") or d.get("body") or "")
                        if len(text) > 10:
                            return text
        except Exception:
            pass
        return None


class GmailIMAPClient:
    """Gmail IMAP 客户端 — 用 +alias 无限衍生邮箱，IMAP 轮询收验证码"""

    def __init__(self, proxies: Any = None):
        import imaplib
        import email as _email_mod
        self.proxies = proxies
        self.email = ""
        self._imap = None
        # 凭据来自环境变量(勿硬编码——曾泄露到公开仓库)
        self._base_email = os.getenv("GMAIL_USER", "").strip()
        self._app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        self._imap_host = "imap.gmail.com"
        self._imap_port = 993
        self._last_uid = 0  # 记录上次检查到的最新邮件 UID

    def _connect(self):
        import imaplib
        if not self._base_email or not self._app_password:
            raise RuntimeError(
                "缺少 GMAIL_USER / GMAIL_APP_PASSWORD 环境变量"
                "(Google 账户 → 安全 → 两步验证 → 应用专用密码)")
        if self._imap:
            try:
                self._imap.noop()
                return
            except Exception:
                pass
        self._imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
        self._imap.login(self._base_email, self._app_password)
        self._imap.select("INBOX")

    def create_email(self) -> str:
        tag = "".join(random.choice(_string.ascii_lowercase + _string.digits) for _ in range(8))
        self.email = f"{self._base_email.split('@')[0]}+grok{tag}@{self._base_email.split('@')[1]}"
        # 连 IMAP 获取当前最新 UID 作为基准
        try:
            self._connect()
            _, data = self._imap.uid("SEARCH", None, "ALL")
            uids = data[0].split()
            self._last_uid = int(uids[-1]) if uids else 0
        except Exception:
            self._last_uid = 0
        return self.email

    def fetch_first_email(self) -> Optional[str]:
        import email as _email_mod
        try:
            self._connect()
            # 只搜 From x.ai 的邮件（避免 Windsurf 等干扰）
            _, data = self._imap.uid("SEARCH", None, f'(FROM "x.ai" UID {self._last_uid + 1}:*)')
            new_uids = data[0].split()
            if not new_uids:
                return None
            # 取最新一封
            latest_uid = int(new_uids[-1])
            self._last_uid = max(self._last_uid, latest_uid)
            _, msg_data = self._imap.uid("FETCH", str(latest_uid).encode(), "(BODY.PEEK[])")
            raw = msg_data[0][1]
            msg = _email_mod.message_from_bytes(raw)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype in ("text/plain", "text/html"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="replace") + "\n"
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
            return body
        except Exception as e:
            print(f"[Gmail] fetch error: {e}")
            return None


class EmailService:
    """统一邮箱服务门面，兼容旧调用方

    provider 支持逗号分隔回退链：EMAIL_PROVIDER="luckmail,gmail" 表示
    优先 LuckMail，失败后依次尝试 Gmail；全失败返回 (None, None)。
    """

    _PROVIDERS = ("gptmail", "mailtm", "luckmail", "luckmail_order", "mailnest", "gmail")

    def __init__(self, proxies: Any = None, provider: str = "gptmail"):
        self.proxies = proxies
        raw = str(provider or os.getenv("EMAIL_PROVIDER") or "gptmail").strip().lower()
        self.providers = [p.strip() for p in raw.split(",") if p.strip()]
        if not self.providers:
            raise ValueError("EMAIL_PROVIDER 为空")
        bad = [p for p in self.providers if p not in self._PROVIDERS]
        if bad:
            raise ValueError(f"不支持的邮箱提供商: {', '.join(bad)}")
        self.provider = self.providers[0]  # 兼容旧调用方读 .provider

    def _create_with(self, provider: str):
        """单个提供商创建邮箱；失败抛异常。返回 (token_like, email)。"""
        if provider == "mailtm":
            client = MailTMClient(self.proxies)
            email = client.create_email()
            token_like = {"provider": "mailtm", "client": client, "email": email}
            print(f"[+] mail.tm 邮箱已创建: {email}")
            return token_like, email
        elif provider == "luckmail":
            settings = _luckmail_settings()
            inbox = LuckMailInbox(
                base_url=settings["base_url"],
                api_key=settings["api_key"],
                api_secret=settings["api_secret"],
                use_hmac=settings["use_hmac"],
                project_code=settings["project_code"],
                email_type=settings["email_type"],
                domain=settings["domain"],
            )
            token_like, email = inbox.create_email()
            print(f"[+] LuckMail 邮箱已购买: {email}")
            return token_like, email
        elif provider == "luckmail_order":
            s = _luckmail_order_settings()
            inbox = LuckMailOrderInbox(
                base_url=s["base_url"],
                api_key=s["api_key"],
                api_secret=s["api_secret"],
                use_hmac=s["use_hmac"],
                project_code=s["project_code"],
                email_types=s["email_types"],
                variant_mode=s["variant_mode"],
            )
            token_like, email = inbox.create_email()
            print(f"[+] LuckMail 接码邮箱已分配: {email} ({inbox.email_type})")
            return token_like, email
        elif provider == 'mailnest':
            settings = _mailnest_settings()
            inbox = MailNestInbox(
                api_key=settings["api_key"],
                project_code=settings["project_code"],
            )
            token_like, email = inbox.create_email()
            print(f"[+] MailNest 邮箱已购买: {email}")
            return token_like, email
        elif provider == "gmail":
            client = GmailIMAPClient(self.proxies)
            email = client.create_email()
            token_like = {"provider": "gmail", "client": client, "email": email}
            print(f"[+] Gmail 别名已创建: {email}")
            return token_like, email
        # gptmail: 使用 V2 API
        client = GPTMailInboxV2(self.proxies)
        email = client.create_email()
        token_like = {"provider": "gptmail-v2", "client": client, "email": email}
        print(f"[+] GPTMail 邮箱已创建: {email}")
        return token_like, email

    def create_email(self):
        errors = []
        for idx, provider in enumerate(self.providers):
            try:
                result = self._create_with(provider)
                if result and result[0]:
                    return result
            except Exception as e:
                errors.append(f"{provider}: {e}")
                print(f"[Error] 请求 {provider} 出错: {e}")
                rest = self.providers[idx + 1:]
                if rest:
                    print(f"  -> 尝试下一个提供商: {', '.join(rest)}")
        if errors:
            print(f"[Error] 所有邮箱提供商均失败: {'; '.join(errors)}")
        return None, None

    def fetch_first_email(self, token_like):
        try:
            if not isinstance(token_like, dict):
                return None

            provider = str(token_like.get("provider") or "gptmail").strip().lower()
            client = token_like.get("client")
            if not client:
                return None

            if provider in ("mailtm", "luckmail", "luckmail_order", "gptmail-v2", "gmail"):
                return client.fetch_first_email()
            elif provider == "mailnest":
                return client.fetch_first_email(token_like.get("email"))

            # legacy gptmail
            email = str(token_like.get("email") or "").strip()
            if not email:
                return None

            emails = client.list_emails(email)
            if not emails:
                return None

            first = emails[0] or {}
            subject = str(first.get("subject") or "")
            from_name = str(((first.get("from") or {}).get("name") or ""))
            from_email = str(((first.get("from") or {}).get("address") or first.get("from_address") or ""))
            body_text = str(first.get("text") or first.get("content") or "")
            body_html = str(first.get("html") or first.get("html_content") or "")
            return "\n".join([f">{subject}<", subject, from_name, from_email, body_text, body_html])
        except Exception as e:
            print(f"获取邮件失败: {e}")
            return None
