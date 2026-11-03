"""天翼网盘 PC 端协议实现（移植自 OpenList 的 189pc 驱动）。

为什么不用 cloud189 SDK
----------------------
SDK 走的是天翼**旧网页版**那套接口：

* ``appId=8025431004``
* 文件操作请求 ``cloud.189.cn/api/open/xxx.action``
* 会话靠 ``AccessToken`` + ``Signature``（MD5）签名

而天翼现行网页版（``open.e.189.cn/api/logbox/separate/web/``）已经换成：

* ``appId=9317140619``（官方 PC 客户端用的 appId）
* 文件操作请求 ``api.cloud.189.cn/xxx.action``
* 会话靠 ``SessionKey`` + ``sessionSecret`` 做 HMAC-SHA1 签名，参数还要 AES-ECB 加密

实测旧家族接口仍在，但只认 **cookie 会话**：

    GET cloud.189.cn/api/open/file/listFiles.action
    -> {"errorCode":"InvalidSessionKey",
        "errorMsg":"cookieUserSession is null or invalid, cookieUserSession=null"}

也就是说 SDK 传的 ``sessionKey`` 查询参数已经不被接受，所以整个 SDK 用不下去。
本模块按 OpenList ``drivers/189pc`` 的做法重写登录与文件操作，两边保持一致。

流程要点
--------
1. ``GET cloud.189.cn/api/portal/unifyLoginForPC.action``
   跳到 logbox 页面，**lt / reqId 在跳转地址的 query 里**（不是页面正文）
2. ``POST open.e.189.cn/api/logbox/oauth2/appConf.do``  取 paramId 等
3. ``POST open.e.189.cn/api/logbox/config/encryptConf.do``  取 RSA 公钥
4. ``POST open.e.189.cn/api/logbox/oauth2/needcaptcha.do``  判断是否要图形验证码
5. ``POST open.e.189.cn/api/logbox/oauth2/loginSubmit.do``  提交登录（version=v2.0）
6. ``POST api.cloud.189.cn/getSessionForPC.action``  用跳转地址换会话

二维码登录走 ``getUUID.do`` + ``image.do`` + ``qrcodeLoginState.do``，
可以完全绕开密码与图形验证码。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid as uuid_lib
from email.utils import formatdate
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import httpx
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

log = logging.getLogger("qunhuisync.pc189")

WEB_URL = "https://cloud.189.cn"
AUTH_URL = "https://open.e.189.cn"
API_URL = "https://api.cloud.189.cn"
UPLOAD_URL = "https://upload.cloud.189.cn"

# 官方 PC 客户端使用的 appId。登录、生成二维码、换取会话必须全程一致。
APP_ID = "9317140619"
CLIENT_TYPE = "10020"
QR_CLIENT_TYPE = "1"
ACCOUNT_TYPE = "02"
VERSION = "7.2.4.0"
CHANNEL_ID = "web_cloud.189.cn"
PC = "TELEPC"
RETURN_URL = "https://m.cloud.189.cn/zhuanti/2020/loginErrorPc/index.html"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 登录失败时的 result 码 -> 提示文案
ERROR_MESSAGES = {
    -1: "登录出错，请稍后重试",
    -2: "图形验证码错误，请重新输入",
    -3: "当前网络已被限制，请 24 小时后重新登录",
    -5: "服务器连接失败，请稍候再试",
    -16: "账号存在异常，请稍后重试",
    -17: "账户名或密码错误",
    -64: "服务器连接失败，请稍候再试",
    -68: "当前网络已被限制，请 24 小时后重新登录",
    -134: "账号开启了二次验证，请先在网页端登录一次以解除限制",
    -135: "双因子登录已过期，请重新登录",
    -136: "双因子登录不一致，请重新登录",
    -133: "账号需要短信二次验证，请先在网页端登录一次以解除限制",
    -149: "用户名不合法",
    -40010: "图形验证码错误次数过多，请 1 小时后再试",
    51002: "账户名或密码错误",
    51003: "服务器连接失败，请稍候再试",
    51014: "密码强度不足，请修改密码后登录",
    51175: "图形验证码已失效，请重新获取",
    51176: "图形验证码已失效，请重新获取",
    51177: "图形验证码错误，请重新输入",
    51179: "账号存在异常，已被冻结，请找回密码后解除",
    51317: "密码安全级别过低，请重置密码后再登录",
    51323: "登录出错已达上限，请找回密码后重试",
    51325: "账号行为异常，已被永久封停",
}

# 二维码扫码状态
QR_WAITING = -106        # 等待扫码
QR_SCANNED = -11002      # 已扫码，等待手机确认
QR_EXPIRED = -11001      # 二维码过期
QR_SUCCESS = 0           # 登录成功


class Pc189Error(Exception):
    """天翼网盘操作失败。message 为可直接展示给用户的中文原因。"""


class Pc189NetworkError(Pc189Error):
    """网络层错误（连接被断开、超时等），通常重试就能成功。"""


class AuthError(Pc189Error):
    """登录失败。"""


class CaptchaRequired(AuthError):
    """天翼要求图形验证码，调用方应取一张验证码图片让用户填写。"""


class QrCodeRequired(AuthError):
    """天翼要求二维码验证（需要短信二次验证等）。"""


# ---------------------------------------------------------------- 加密工具

def rsa_encrypt(pub_key: str, pre: str, plain: str) -> str:
    """按天翼要求用 RSA 公钥加密，输出十六进制大写串并带上前缀。"""
    pem = "-----BEGIN PUBLIC KEY-----\n%s\n-----END PUBLIC KEY-----" % pub_key
    key = RSA.import_key(pem)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(plain.encode("utf-8"))
    return pre + binascii.hexlify(encrypted).decode("utf-8").upper()


def aes_ecb_encrypt(data: str, key: str) -> str:
    """AES-ECB 加密（PKCS7 填充），输出十六进制大写串。

    对应 OpenList 的 ``AesECBEncrypt``。
    """
    block = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    padded = pad(data.encode("utf-8"), AES.block_size)
    return binascii.hexlify(block.encrypt(padded)).decode("utf-8").upper()


def signature_of_hmac(session_secret: str, session_key: str, method: str,
                      url: str, date: str, params: str = "") -> str:
    """生成请求签名（HMAC-SHA1）。

    对应 OpenList 的 ``signatureOfHmac``：签的是 path（不含域名和查询串），
    ``params`` 非空时追加 ``&params=...``。
    """
    match = re.search(r"://[^/]+((/[^/\s?#]+)*)", url)
    url_path = match.group(1) if match else "/"
    data = "SessionKey=%s&Operate=%s&RequestURI=%s&Date=%s" % (
        session_key, method, url_path, date)
    if params:
        data += "&params=%s" % params
    mac = hmac.new(session_secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return binascii.hexlify(mac.digest()).decode("utf-8").upper()


def _encode_params(params: Dict[str, str]) -> str:
    """把参数拼成 key 排序后的 ``k=v&k=v`` 串（对应 OpenList 的 ``Params.Encode``）。"""
    return "&".join("%s=%s" % (key, params[key]) for key in sorted(params))


def _http_date() -> str:
    """HTTP 规范的时间格式（OpenList 用的是 ``time.Now().UTC().Format(http.TimeFormat)``）。"""
    return formatdate(time.time(), usegmt=True)


def _parse_query(url: str) -> Dict[str, str]:
    """从 URL 里取出查询参数。"""
    _, _, tail = url.partition("?")
    tail = tail.split("#", 1)[0]
    result: Dict[str, str] = {}
    for pair in tail.split("&"):
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        result[unquote(key)] = unquote(value)
    return result


def _client_suffix() -> Dict[str, str]:
    """每个请求都要带的公共查询参数。"""
    return {
        "clientType": PC,
        "version": VERSION,
        "channelId": CHANNEL_ID,
        "rand": "%d_%d" % (int(time.time() * 1000) % 100000, int(time.time() * 1000) % 10000000000),
    }


def _response_error(data: Any) -> Optional[str]:
    """检查响应体里有没有错误。

    对应 OpenList 的 ``RespErr.HasError()``：``res_code`` / ``code`` /
    ``errorCode`` 任一不为空即为错误。天翼很多接口出错时仍返回 HTTP 200，
    所以必须查响应体，光看状态码不够。
    """
    if not isinstance(data, dict):
        return None
    res_code = data.get("res_code")
    if res_code not in (None, 0, "0", ""):
        return "%s / %s" % (res_code, data.get("res_message") or data.get("msg") or "")
    code = data.get("code")
    if code and code != "SUCCESS":
        return "%s / %s" % (code, data.get("msg") or data.get("message") or "")
    error_code = data.get("errorCode")
    if error_code:
        return "%s / %s" % (error_code, data.get("errorMsg") or "")
    return None


def _parse_response_error(text: str) -> Optional[str]:
    """从天翼返回体里解析错误信息。

    天翼的返回有四种形态：JSON 的 res_code、JSON 的 code、XML 的 code、
    以及 JSON 的 errorCode。挨个认一遍。
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            code = data.get("res_code")
            if code not in (None, 0, "0", ""):
                return "%s / %s" % (code, data.get("res_message") or data.get("msg") or "")
            if data.get("errorCode"):
                return "%s / %s" % (data["errorCode"], data.get("errorMsg") or "")
            if data.get("code") and data.get("code") != "SUCCESS":
                return "%s / %s" % (data["code"], data.get("msg") or data.get("message") or "")
    match = re.search(r"<code>(.*?)</code>", text)
    if match:
        message = re.search(r"<message>(.*?)</message>", text)
        return "%s / %s" % (match.group(1), message.group(1) if message else "")
    return text[:160].replace("\n", " ")


class _Base:
    """共用的一次性会话上下文。"""

    def __init__(self, timeout: float = 30.0):
        self.session = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json;charset=UTF-8"},
        )
        # 设备指纹：缺少这个头会被判定为陌生设备
        self.user_finger = str(int(time.time() * 1000) % 1000000000 + 1000000000)
        self.lt = ""
        self.req_id = ""
        self.referer = ""
        self.param_id = ""
        self.account_type = ACCOUNT_TYPE
        self.return_url = RETURN_URL
        self.mail_suffix = ""
        self.client_type = CLIENT_TYPE
        self.pub_key = ""
        self.rsa_pre = "{RSA}"

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # ---------- 登录参数 ----------
    def init_base_params(self) -> None:
        """第一步：拿 lt / reqId / paramId。

        现行登录页把 lt、reqId 放在**跳转地址的 query** 里，页面正文里没有，
        所以必须从最终地址取，取不到再退回页面正则。
        """
        resp = self.session.get(
            WEB_URL + "/api/portal/unifyLoginForPC.action",
            params={
                "appId": APP_ID,
                "clientType": CLIENT_TYPE,
                "returnURL": RETURN_URL,
                "timeStamp": int(time.time() * 1000),
            },
        )
        final_url = str(resp.url)
        self.referer = final_url
        query = _parse_query(final_url)
        self.lt = query.get("lt") or ""
        self.req_id = query.get("reqId") or ""

        if not self.lt or not self.req_id:
            # 老页面把参数写在页内变量里
            self.lt = self._find(resp.text, r'lt = "(.+?)"')
            self.req_id = self._find(resp.text, r'reqId = "(.+?)"')
            if self.referer:
                self.referer = AUTH_URL + "/api/logbox/separate/web/index.html?" + quote(
                    "appId=%s&lt=%s&reqId=%s" % (APP_ID, self.lt, self.req_id))
        if not self.lt or not self.req_id:
            raise AuthError("无法获取天翼登录页面参数，请检查网络后重试")

        # paramId 由 appConf.do 下发
        conf = self._post_json(AUTH_URL + "/api/logbox/oauth2/appConf.do",
                               data={"version": "2.0", "appKey": APP_ID})
        data = conf.get("data") or {}
        self.param_id = data.get("paramId") or ""
        self.account_type = data.get("accountType") or ACCOUNT_TYPE
        self.return_url = data.get("returnUrl") or RETURN_URL
        self.mail_suffix = data.get("mailSuffix") or ""
        self.client_type = str(data.get("clientType") or CLIENT_TYPE)
        if not self.param_id:
            raise AuthError("天翼未下发登录参数（paramId），请稍后重试")

        # RSA 公钥
        enc = self._post_json(AUTH_URL + "/api/logbox/config/encryptConf.do",
                              data={"appId": APP_ID})
        enc_data = enc.get("data") or {}
        self.pub_key = enc_data.get("pubKey") or ""
        self.rsa_pre = enc_data.get("pre") or "{RSA}"
        if not self.pub_key:
            raise AuthError("无法获取天翼加密公钥，请检查网络后重试")

    def login_headers(self) -> Dict[str, str]:
        """logbox 接口的公共请求头。

        缺 ``user-finger`` 或把 Referer 指向首页，都会被判定为陌生设备。
        """
        return {
            "REQID": self.req_id,
            "lt": self.lt,
            "user-finger": self.user_finger,
            "Referer": self.referer or AUTH_URL,
        }

    def _post_json(self, url: str, data: Optional[Dict[str, str]] = None,
                   headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        resp = self.session.post(url, data=data or {}, headers=headers or self.login_headers())
        try:
            return resp.json()
        except Exception:
            raise AuthError("天翼接口返回异常：%s" % (_parse_response_error(resp.text) or "未知错误"))

    # ---------- 工具 ----------
    @staticmethod
    def _find(text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1) if match else ""

    def _encrypt(self, plain: str) -> str:
        return rsa_encrypt(self.pub_key, self.rsa_pre, plain)


class Pc189Auth(_Base):
    """天翼网盘登录会话。

    典型用法::

        auth = Pc189Auth()
        auth.prepare()
        if auth.need_captcha:
            png = auth.captcha()
            session = auth.submit(user, pwd, captcha_code)
        else:
            session = auth.submit(user, pwd)
    """

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout)
        self.need_captcha = False
        self.captcha_token = ""
        self._prepared = False

    # ---------- 第一步：准备 ----------
    def prepare(self, username: str = "") -> Dict[str, Any]:
        """取登录参数，并判断该账号是否需要图形验证码。"""
        self.init_base_params()
        self._prepared = True

        need = False
        if username:
            need = self._need_captcha(username)
        self.need_captcha = need
        return {"need_captcha": need, "captcha_token": self.captcha_token}

    def _need_captcha(self, username: str) -> bool:
        """问天翼该账号是否被风控、需要图形验证码。

        这个接口本身不可用时返回 False —— 先按「不需要验证码」提交，
        万一被拒，提交阶段会抛 :class:`CaptchaRequired`。
        """
        try:
            resp = self.session.post(
                AUTH_URL + "/api/logbox/oauth2/needcaptcha.do",
                data={
                    "appKey": APP_ID,
                    "accountType": self.account_type,
                    "userName": self._encrypt(username),
                },
                headers=self.login_headers(),
            )
            value = resp.json()
        except Exception as exc:
            log.warning("needcaptcha.do 调用失败，按不需要验证码处理：%s", exc)
            return False
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return bool(value)

    # ---------- 第二步：图形验证码 ----------
    def captcha(self) -> bytes:
        """下载图形验证码图片（PNG 原始字节）。

        现行登录页的图形验证码是网易易盾的滑块，``picCaptcha.do`` 已经不再下发
        图片；这里保留旧接口以便老流程仍能工作，取不到时抛错。
        """
        resp = self.session.get(
            AUTH_URL + "/api/logbox/oauth2/picCaptcha.do",
            params={
                "token": self.captcha_token,
                "REQID": self.req_id,
                "rnd": int(time.time() * 1000),
            },
            headers=self.login_headers(),
        )
        if resp.status_code != 200 or not resp.content:
            raise AuthError("获取图形验证码失败，请改用二维码登录")
        return resp.content

    # ---------- 第三步：提交登录 ----------
    def submit(self, username: str, password: str, captcha_code: str = "") -> Dict[str, Any]:
        """提交登录，成功返回会话信息。"""
        if not self._prepared:
            self.prepare(username)

        body = {
            "version": "v2.0",
            "apToken": "",
            "appKey": APP_ID,
            "pageKey": "normal",
            "accountType": self.account_type,
            "userName": self._encrypt(username),
            # 现行网页版同时认 password 和 epd，两个都带上
            "password": self._encrypt(password),
            "epd": self._encrypt(password),
            "validateCode": captcha_code or "",
            "captchaToken": self.captcha_token,
            "returnUrl": self.return_url,
            "mailSuffix": self.mail_suffix,
            "dynamicCheck": "FALSE",
            "clientType": self.client_type,
            "cb_SaveName": "1",
            "isOauth2": "false",
            "state": "",
            "paramId": self.param_id,
        }
        resp = self.session.post(
            AUTH_URL + "/api/logbox/oauth2/loginSubmit.do",
            data=body,
            headers=self.login_headers(),
        )
        try:
            data = resp.json()
        except Exception:
            raise AuthError("天翼登录接口返回异常：%s"
                            % (_parse_response_error(resp.text) or "未知错误"))

        result = data.get("result")
        if result != 0:
            if result == -1101:
                raise CaptchaRequired("该账号需要图形验证码")
            if result == -133:
                raise QrCodeRequired(
                    "该账号需要短信二次验证，请先在浏览器登录一次天翼网盘，"
                    "或改用二维码登录")
            raise AuthError(self._error_text(result, data.get("msg")))

        to_url = data.get("toUrl")
        if not to_url:
            raise AuthError("天翼登录未返回跳转地址，请稍后重试")
        return exchange_session(self.session, to_url)

    @staticmethod
    def _error_text(result: Any, message: Optional[str]) -> str:
        try:
            code = int(result)
        except (TypeError, ValueError):
            code = None
        if code is not None and code in ERROR_MESSAGES:
            return ERROR_MESSAGES[code]
        return message or "登录失败（result=%s）" % result


class Pc189Qr(_Base):
    """二维码登录。

    扫码可以完全绕开密码与图形验证码，是账号被风控时的可靠选择。
    """

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout)
        self.uuid = ""
        self.encry_uuid = ""
        self.encode_uuid = ""

    def prepare(self) -> bytes:
        """取二维码图片（PNG 原始字节）。"""
        self.init_base_params()
        resp = self.session.post(
            AUTH_URL + "/api/logbox/oauth2/getUUID.do",
            data={"appId": APP_ID},
            headers=self.login_headers(),
        )
        try:
            data = resp.json()
        except Exception:
            raise AuthError("天翼二维码接口返回异常，请稍后重试")
        if data.get("result") != 0:
            raise AuthError("获取二维码失败：%s" % (data.get("msg") or "未知错误"))
        self.uuid = data.get("uuid") or ""
        self.encry_uuid = data.get("encryuuid") or ""
        self.encode_uuid = data.get("encodeuuid") or ""
        if not self.uuid:
            raise AuthError("天翼未返回二维码标识，请稍后重试")

        # 取二维码图片。
        #
        # 这里必须用**原始 uuid**，不能用 encodeuuid：httpx 传 params 时会自己
        # 做一次百分号编码，若再传已经编码过的 encodeuuid，就会变成双重编码
        # （%3A -> %253A）。服务端解析出来的 uuid 被截断，生成的二维码内容
        # 只有 "https://open.e.189.cn/api"，App 扫了会提示「二维码已失效」。
        img = self.session.get(
            AUTH_URL + "/api/logbox/oauth2/image.do",
            params={"uuid": self.uuid, "REQID": self.req_id},
            headers=self.login_headers(),
        )
        if img.status_code != 200 or not img.content:
            raise AuthError("获取二维码图片失败，请稍后重试")
        return img.content

    def poll(self) -> Dict[str, Any]:
        """轮询扫码状态。

        返回 ``{"status": int, "session": {...}}``；
        status 为 0 表示登录成功，session 里带 accessToken 等。
        """
        resp = self.session.post(
            AUTH_URL + "/api/logbox/oauth2/qrcodeLoginState.do",
            data={
                "appId": APP_ID,
                "clientType": QR_CLIENT_TYPE,
                "returnUrl": self.return_url,
                "paramId": self.param_id,
                # uuid 必须传原始值：传编码过的会返回「uuid 非法！」
                "uuid": self.uuid,
                "encryuuid": self.encry_uuid,
                "cb_SaveName": "3",
                "isOauth2": "false",
                "state": "",
                "date": time.strftime("%Y-%m-%d%H:%M:%S.") + "%03d" % (
                    int(time.time() * 1000) % 1000),
                "timeStamp": int(time.time() * 1000),
            },
            headers=self.login_headers(),
        )
        try:
            data = resp.json()
        except Exception:
            return {"status": -1, "message": "扫码状态查询失败"}

        status = data.get("status")
        if status == QR_SUCCESS:
            redirect = data.get("redirectUrl")
            if not redirect:
                return {"status": -1, "message": "扫码成功但未返回跳转地址"}
            try:
                session = exchange_session(self.session, redirect)
            except Pc189Error as exc:
                return {"status": -1, "message": str(exc)}
            return {"status": QR_SUCCESS, "session": session}
        if status == QR_EXPIRED:
            return {"status": QR_EXPIRED, "message": "二维码已过期，请重新获取"}
        if status == QR_SCANNED:
            return {"status": QR_SCANNED, "message": "已扫码，请在手机上确认"}
        if status == QR_WAITING:
            return {"status": QR_WAITING, "message": "请使用天翼网盘 App 扫码"}
        return {"status": status, "message": data.get("msg") or "扫码登录失败"}


def exchange_session(session: httpx.Client, redirect_url: str) -> Dict[str, Any]:
    """用登录成功后的跳转地址换取会话密钥。"""
    resp = session.post(
        API_URL + "/getSessionForPC.action",
        params=dict(_client_suffix(), returnType="JSON", redirectURL=redirect_url),
        headers={"X-Request-ID": str(uuid_lib.uuid4())},
    )
    try:
        data = resp.json()
    except Exception:
        raise AuthError("获取网盘会话失败：%s" % (_parse_response_error(resp.text) or "未知错误"))
    error = _response_error(data)
    if error:
        raise AuthError("获取网盘会话失败：%s" % error)
    if not data.get("sessionKey"):
        raise AuthError("获取网盘会话失败：%s"
                        % (data.get("res_message") or data.get("msg") or "未知原因"))
    return data


def restore_session(access_token: str) -> Dict[str, Any]:
    """用已保存的 accessToken 换回会话，避免重新登录。"""
    if not access_token:
        raise AuthError("没有可用的令牌")
    with httpx.Client(follow_redirects=True, timeout=30.0,
                      headers={"User-Agent": USER_AGENT}) as session:
        resp = session.get(
            API_URL + "/getSessionForPC.action",
            params=dict(_client_suffix(), returnType="JSON",
                        appId=APP_ID, accessToken=access_token),
            headers={"X-Request-ID": str(uuid_lib.uuid4())},
        )
        try:
            data = resp.json()
        except Exception:
            raise AuthError("令牌已失效，请重新登录")
    if not data.get("sessionKey"):
        raise AuthError("令牌已失效，请重新登录")
    return data


class Pc189Client:
    """天翼网盘文件操作客户端（对应 OpenList 189pc 的 API 家族）。

    所有请求都发往 ``api.cloud.189.cn``，用 ``SessionKey`` + ``sessionSecret``
    做 HMAC-SHA1 签名，查询参数用 AES-ECB 加密。
    """

    def __init__(self, session_data: Dict[str, Any], timeout: float = 60.0):
        self.session_key = session_data.get("sessionKey") or ""
        self.session_secret = session_data.get("sessionSecret") or ""
        self.access_token = session_data.get("accessToken") or ""
        self.refresh_token = session_data.get("refreshToken") or ""
        self.username = session_data.get("loginName") or ""
        if not self.session_key or not self.session_secret:
            raise Pc189Error("会话信息不完整，请重新登录")
        self.http = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json;charset=UTF-8"},
        )

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:
            pass

    def _aes_key(self) -> str:
        """AES 加密用的 key：sessionSecret 的前 16 个字符。

        天翼下发的 sessionSecret 固定 32 位，取前 16 位正好是 AES-128 的 key。
        """
        secret = self.session_secret or ""
        if len(secret) < 16:
            raise Pc189Error("会话密钥不完整，请重新登录")
        return secret[:16]

    # ---------- 底层请求 ----------
    def request(self, url: str, method: str = "GET",
                params: Optional[Dict[str, str]] = None,
                query: Optional[Dict[str, str]] = None,
                attempts: int = 3) -> Dict[str, Any]:
        """发一个带签名的请求，返回解析后的 JSON。

        ``params`` 是要加密的业务参数（AES-ECB 后放进 ``params`` 查询串），
        ``query`` 是明文查询参数。两者都会并入最终查询串。

        网络层错误（连接被断开、超时）会自动重试——天翼的对象存储和网关
        偶尔会在传输中途断开，尤其是大文件分片上传时。
        """
        merged = dict(_client_suffix())
        merged.update(query or {})
        encrypted = ""
        if params:
            encrypted = aes_ecb_encrypt(_encode_params(params), self._aes_key())
            merged["params"] = encrypted

        last = ""
        for attempt in range(attempts):
            date = _http_date()
            headers = {
                "Date": date,
                "SessionKey": self.session_key,
                "X-Request-ID": str(uuid_lib.uuid4()),
                "Signature": signature_of_hmac(
                    self.session_secret, self.session_key, method, url, date, encrypted),
            }
            try:
                resp = self.http.request(method, url, params=merged, headers=headers)
            except httpx.TransportError as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt + 1 >= attempts:
                    break
                log.warning("请求 %s 网络异常：%s，第 %d 次重试", url, last, attempt + 1)
                time.sleep(1 + attempt)
                continue
            try:
                data = resp.json()
            except Exception:
                raise Pc189Error("天翼接口返回异常：%s"
                                 % (_parse_response_error(resp.text) or "未知错误"))
            # 天翼的错误不一定带非 200 状态码，必须检查响应体
            error = _response_error(data)
            if error:
                raise Pc189Error("天翼接口返回错误：%s" % error)
            return data
        raise Pc189NetworkError("天翼接口连接失败：%s" % last)

    # ---------- 目录 ----------
    def list_files(self, folder_id: str = "-11", page_size: int = 1000) -> List[Dict[str, Any]]:
        """列出目录下的文件与子目录。"""
        result: List[Dict[str, Any]] = []
        page = 1
        while page <= 200:
            data = self.request(
                API_URL + "/listFiles.action",
                "GET",
                query={
                    "folderId": str(folder_id),
                    "fileType": "0",
                    "mediaAttr": "0",
                    "iconOption": "5",
                    "pageNum": str(page),
                    "pageSize": str(page_size),
                    "recursive": "0",
                    "orderBy": "filename",
                    "descending": "false",
                },
            )
            ao = data.get("fileListAO") or {}
            files = ao.get("fileList") or []
            folders = ao.get("folderList") or []
            for folder in folders:
                result.append({
                    "id": str(folder.get("id")),
                    "name": folder.get("name") or "",
                    "is_folder": True,
                    "size": 0,
                    "modified": folder.get("lastOpTime") or "",
                })
            for item in files:
                result.append({
                    "id": str(item.get("id")),
                    "name": item.get("name") or "",
                    "is_folder": False,
                    "size": int(item.get("size") or 0),
                    "modified": item.get("lastOpTime") or "",
                    "md5": item.get("md5") or "",
                })
            count = int(ao.get("count") or 0)
            page_count = len(files) + len(folders)
            # 本页取不满一页就说明到底了（与 OpenList 一致）
            if count == 0 or page_count == 0 or page_count < page_size:
                break
            page += 1
        return result

    def create_folder(self, parent_id: str, name: str) -> str:
        """新建目录，返回新目录的 id。

        服务端正常会直接返回 id；少数情况下不回，这时回查目录列表确认。
        天翼的目录列表有延迟，刚建的目录可能马上还查不到，所以要重试几次。
        """
        data = self.request(
            API_URL + "/createFolder.action",
            "POST",
            query={
                "folderName": name,
                "relativePath": "",
                "parentFolderId": str(parent_id),
            },
        )
        folder_id = data.get("id") or data.get("folderId") or ""
        if folder_id:
            return str(folder_id)
        # 回查确认（列表有延迟，多试几次）
        for attempt in range(4):
            for item in self.list_files(parent_id):
                if item.get("name") == name and item.get("is_folder"):
                    return str(item.get("id"))
            time.sleep(0.5 + attempt)
        return ""

    def delete_file(self, file_id: str, file_name: str, is_folder: bool = False) -> None:
        """删除文件或目录（走批量任务接口）。"""
        task_infos = [{
            "fileId": str(file_id),
            "fileName": file_name,
            "isFolder": 1 if is_folder else 0,
        }]
        data = self.request(
            API_URL + "/batch/createBatchTask.action",
            "POST",
            query={
                "type": "DELETE",
                "targetFolderId": "",
                "taskInfos": json.dumps(task_infos, ensure_ascii=False),
            },
        )
        task_id = data.get("taskId") or data.get("taskID") or ""
        if task_id:
            self._wait_batch_task("DELETE", task_id)

    def _wait_batch_task(self, task_type: str, task_id: str, timeout: float = 30.0) -> None:
        """等待批量任务结束。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.request(
                API_URL + "/batch/checkBatchTask.action",
                "POST",
                query={"type": task_type, "taskId": str(task_id)},
            )
            status = data.get("taskStatus")
            if status == 4:
                return
            if status == 2:
                raise Pc189Error("删除失败：云端存在同名文件")
            time.sleep(0.3)
        log.warning("批量任务 %s 未在超时内结束", task_id)

    # ---------- 整文件上传 ----------
    def _signed(self, method: str, url: str, extra: Optional[Dict[str, str]] = None
                ) -> Dict[str, str]:
        """构造带签名的请求头（OpenList 的 ``SignatureHeader``）。"""
        date = _http_date()
        headers = {
            "Date": date,
            "SessionKey": self.session_key,
            "X-Request-ID": str(uuid_lib.uuid4()),
            "Signature": signature_of_hmac(
                self.session_secret, self.session_key, method, url, date),
        }
        if extra:
            headers.update(extra)
        return headers

    def _post_form(self, url: str, form: Dict[str, str],
                   attempts: int = 3) -> Any:
        """POST 表单并返回解析后的响应体（JSON 或 XML）。"""
        last = ""
        for attempt in range(attempts):
            try:
                resp = self.http.post(
                    url, params=dict(_client_suffix()), data=form,
                    headers=self._signed("POST", url))
            except httpx.TransportError as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                if attempt + 1 >= attempts:
                    break
                log.warning("POST %s 网络异常：%s，第 %d 次重试", url, last, attempt + 1)
                time.sleep(1 + attempt)
                continue
            text = resp.text or ""
            if text.lstrip().startswith("<"):
                return text
            try:
                data = resp.json()
            except Exception:
                raise Pc189Error("天翼接口返回异常：%s"
                                 % (_parse_response_error(text) or "未知错误"))
            error = _response_error(data)
            if error:
                raise Pc189Error("天翼接口返回错误：%s" % error)
            return data
        raise Pc189NetworkError("天翼接口连接失败：%s" % last)

    def upload_whole(self, local_path: str, folder_id: str,
                     rename: Optional[str] = None, on_progress=None,
                     should_cancel=None) -> str:
        """整文件上传（OpenList 的 ``OldUpload`` 机制）。

        与分片上传不同，这里**一次性 PUT 整个文件**，天翼自己断点续传：

        1. ``createUploadFile.action``  -> 拿上传地址、提交地址、上传 id
        2. PUT 整个文件到 ``fileUploadUrl``
        3. ``getUploadFileStatus.action`` -> 查已传字节，不足则续传
        4. POST ``fileCommitUrl`` -> 提交，得到文件 id

        ``on_progress(done, total)`` 每传一块回调一次。
        """
        import hashlib
        import os

        file_size = os.path.getsize(local_path)
        name = rename or os.path.basename(local_path)

        with open(local_path, "rb") as handle:
            file_md5 = hashlib.md5(handle.read()).hexdigest()

        create_url = API_URL + "/createUploadFile.action"
        info = self._post_form(create_url, {
            "parentFolderId": str(folder_id),
            "fileName": name,
            "size": str(file_size),
            "md5": file_md5,
            "opertype": "3",
            "flag": "1",
            "resumePolicy": "1",
            "isLog": "0",
        })
        upload_url = info.get("fileUploadUrl") or ""
        commit_url = info.get("fileCommitUrl") or ""
        upload_id = info.get("uploadFileId")
        if not upload_url or not commit_url or upload_id is None:
            raise Pc189Error("创建上传失败：%s" % (info.get("res_message") or "未知错误"))

        # 云端已有同样的文件（秒传），直接提交
        if int(info.get("fileDataExists") or 0) == 1:
            return self._commit_whole(commit_url, upload_id)

        sent = {"value": 0}

        def body_stream(offset: int):
            """从 ``offset`` 开始读文件，边读边上报进度。"""
            with open(local_path, "rb") as handle:
                handle.seek(offset)
                while True:
                    piece = handle.read(256 * 1024)
                    if not piece:
                        break
                    yield piece
                    sent["value"] += len(piece)
                    if on_progress:
                        on_progress(offset + sent["value"], file_size)

        put_headers = self._signed("PUT", upload_url, {
            "ResumePolicy": "1",
            "Expect": "100-continue",
            "Edrive-UploadFileId": str(upload_id),
            "Accept": "*/*",
        })

        # 已经传了多少字节（断线后续传从这里开始）
        offset = 0
        last = ""
        for attempt in range(5):
            if should_cancel and should_cancel():
                raise Pc189Error("上传已取消")
            remaining = file_size - offset
            if remaining <= 0:
                break
            headers = dict(put_headers)
            headers["Content-Length"] = str(remaining)
            sent["value"] = 0
            try:
                resp = self.http.put(upload_url, headers=headers,
                                     content=body_stream(offset))
            except httpx.TransportError as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                log.warning("上传连接中断：%s", last)
            else:
                if resp.status_code < 400:
                    offset = file_size
                    break
                last = "HTTP %s" % resp.status_code
                log.warning("上传返回 %s", last)

            # 查服务端实际收到多少，从断点继续
            try:
                status = self._upload_status(upload_id)
                offset = int(status.get("size") or 0)
            except Pc189Error:
                offset = 0
            if offset >= file_size:
                break
            time.sleep(1 + attempt)

        if offset < file_size:
            raise Pc189NetworkError("上传失败：%s（已传 %d/%d 字节）"
                                    % (last or "连接中断", offset, file_size))

        return self._commit_whole(commit_url, upload_id)

    def _upload_status(self, upload_id: Any) -> Dict[str, Any]:
        """查上传进度（已传字节数在 ``size`` 里）。"""
        url = API_URL + "/getUploadFileStatus.action"
        try:
            resp = self.http.get(
                url,
                params=dict(_client_suffix(), uploadFileId=str(upload_id), resumePolicy="1"),
                headers=self._signed("GET", url))
            return resp.json()
        except httpx.TransportError as exc:
            raise Pc189NetworkError("查询上传状态失败：%s" % exc)

    def _commit_whole(self, commit_url: str, upload_id: Any) -> str:
        """提交整文件上传，返回云端文件 id。"""
        result = self._post_form(commit_url, {
            "opertype": "3",
            "resumePolicy": "1",
            "uploadFileId": str(upload_id),
            "isLog": "0",
        })
        if isinstance(result, str):
            # XML 响应
            match = re.search(r"<id>(\d+)</id>", result)
            if match:
                return match.group(1)
            raise Pc189Error("提交上传失败：%s" % (_parse_response_error(result) or "未知错误"))
        file_info = result.get("file") or {}
        return str(file_info.get("userFileId") or file_info.get("id") or "")

    # ---------- 上传 ----------
    @staticmethod
    def _retry(action, what: str, attempts: int = 3):
        """执行一个上传相关动作，失败时重试。

        天翼偶尔返回「服务暂时不可用」这类瞬时错误，重试即可。
        对应 OpenList 里的 ``retry.Attempts(3)``。
        """
        last = ""
        for attempt in range(attempts):
            try:
                return action()
            except Pc189Error as exc:
                last = str(exc)
                if attempt + 1 >= attempts:
                    break
                log.warning("%s失败：%s，第 %d 次重试", what, last, attempt + 1)
                time.sleep(1 + attempt)
        raise Pc189Error("%s失败：%s" % (what, last))

    @staticmethod
    def _part_size(file_size: int) -> int:
        """分片大小。

        对应 OpenList 的 ``partSize()``。**这个值不能改**：天翼按文件大小
        硬性规定分片大小，改了就报 ``InvalidPartSize``。实测：

        * 文件 ≤ 10GB  -> 必须 10MB
        * 文件 ~100GB  -> 必须 20MB
        * 文件 ~200GB  -> 必须 50MB

        公式与天翼的规则完全一致，所以照抄 OpenList。
        """
        default = 10 * 1024 * 1024
        if file_size > default * 2 * 999:
            import math
            return int(max(math.ceil((float(file_size) / 1999) / default), 5) * default)
        if file_size > default * 999:
            return default * 2
        return default

    def _put_chunk(self, url: str, headers: Dict[str, str], chunk: bytes,
                   attempts: int = 3, on_sent=None) -> None:
        """上传一个分片。

        对应 OpenList 的 ``put()``：

        * URL 要追加 ``clientSuffix()`` 查询参数（clientType / version /
          channelId / rand），缺了服务端不认这个分片
        * 响应体里也可能带错误（HTTP 200 + errorCode），不能只看状态码

        两个坑：

        1. **Accept 头必须是 ``*/*``**。天翼的对象存储前端（nginx + EastChain）
           在 Accept 是 ``application/json`` 时会走另一条处理分支：PUT 照样返回
           200、etag 也正常，但**不会把这个分片登记到 uploadFileId**，
           最后提交时报「uploaded part number less than total slice part
           number」。httpx 默认发 ``*/*``，但我们的客户端默认发 JSON，
           所以这里显式覆盖成 ``*/*``。
        2. 对象存储偶尔返回 500 InternalError，重试即可。

        ``on_sent(n)`` 在分片内每传完一块就回调，用来把进度做细——
        否则一个 10MB 分片传完前进度条都是 0。
        """
        target = url
        separator = "&" if "?" in target else "?"
        target += separator + "&".join(
            "%s=%s" % (key, value) for key, value in _client_suffix().items())
        send_headers = dict(headers)
        send_headers["Accept"] = "*/*"
        # 显式给 Content-Length：用迭代器分块发送时，httpx 默认会退化成
        # chunked 传输，天翼的对象存储不接受。
        send_headers["Content-Length"] = str(len(chunk))

        # 本次尝试已发送的字节数。重试时清零，让进度回调始终反映
        # 「当前这次尝试传了多少」，调用方按覆盖而不是累加处理。
        sent = {"value": 0}

        def body_stream():
            step = 256 * 1024
            for offset in range(0, len(chunk), step):
                piece = chunk[offset:offset + step]
                yield piece
                sent["value"] += len(piece)
                if on_sent:
                    on_sent(sent["value"])

        last_error = ""
        for attempt in range(attempts):
            sent["value"] = 0
            if on_sent:
                on_sent(0)      # 重试时把进度归零，避免界面显示虚高
            try:
                resp = self.http.put(target, headers=send_headers, content=body_stream())
            except httpx.TransportError as exc:
                # 服务端中途断开（RemoteProtocolError）、读超时等。
                # 分片没传完，重传这一片即可——服务端会覆盖同一 partNumber。
                last_error = "%s: %s" % (type(exc).__name__, exc)
                if attempt + 1 >= attempts:
                    break
                log.warning("上传分片连接中断：%s，第 %d 次重试", last_error, attempt + 1)
                time.sleep(1 + attempt)
                continue
            try:
                body = resp.json()
            except Exception:
                body = None
            if body is not None:
                error = _response_error(body)
                if error:
                    raise Pc189Error("上传分片失败：%s" % error)
            if resp.status_code < 400:
                return
            last_error = "HTTP %s %s" % (resp.status_code, (body or {}).get("msg", ""))
            if resp.status_code < 500:
                break
            log.warning("上传分片返回 %s，第 %d 次重试", resp.status_code, attempt + 1)
            time.sleep(1 + attempt)
        raise Pc189NetworkError("上传分片失败：%s" % last_error)

    def upload(self, local_path: str, folder_id: str, rename: Optional[str] = None,
               on_progress=None, should_cancel=None, threads: int = 1) -> str:
        """上传本地文件，返回云端文件 id。

        ``on_progress(done_bytes)`` 每上传完一个分片回调一次。
        ``should_cancel()`` 返回 True 时中止上传。
        ``threads`` 是分片并发数（同一个文件内）。
        """
        import hashlib
        import math
        import os

        file_size = os.path.getsize(local_path)
        slice_size = self._part_size(file_size)
        name = rename or os.path.basename(local_path)

        init = self._retry(
            lambda: self.request(
                UPLOAD_URL + "/person/initMultiUpload",
                "GET",
                params={
                    "parentFolderId": str(folder_id),
                    "fileName": quote(name),
                    "fileSize": str(file_size),
                    "sliceSize": str(slice_size),
                    "lazyCheck": "1",
                },
            ),
            "初始化上传",
        )
        data = init.get("data") or {}
        upload_file_id = data.get("uploadFileId") or ""
        if not upload_file_id:
            raise Pc189Error("初始化上传失败：%s" % (init.get("msg") or "未知错误"))

        # 秒传命中，直接提交
        if int(data.get("fileDataExists") or 0) == 1:
            return self._commit_upload(upload_file_id, "", "", file_size)

        count = int(math.ceil(file_size / float(slice_size))) if file_size else 1

        # 先把每个分片的 MD5 算出来，同时读一遍文件算整文件 MD5
        slice_md5s: List[str] = []
        chunks: List[bytes] = []
        file_md5 = hashlib.md5()
        with open(local_path, "rb") as handle:
            for _ in range(count):
                chunk = handle.read(slice_size)
                if not chunk:
                    break
                file_md5.update(chunk)
                slice_md5s.append(hashlib.md5(chunk).hexdigest().upper())
                chunks.append(chunk)

        total_parts = len(chunks)
        if total_parts == 0:
            raise Pc189Error("文件为空，无法上传")

        # 每个分片已传字节数
        part_done: Dict[int, int] = {}
        done_lock = threading.Lock()

        def report(index: int, amount: int) -> None:
            """分片内每传完一块就上报一次，让进度条走得更细。

            ``amount`` 是「本次尝试已传的字节数」（重试时从头计），
            所以这里是覆盖而不是累加。
            """
            with done_lock:
                part_done[index] = amount
                total = sum(part_done.values())
            if on_progress:
                on_progress(total)

        def upload_part(index: int) -> None:
            chunk = chunks[index - 1]
            digest = hashlib.md5(chunk).digest()
            part_info = "%d-%s" % (index, base64.b64encode(digest).decode())

            def send() -> None:
                urls = self.request(
                    UPLOAD_URL + "/person/getMultiUploadUrls",
                    "GET",
                    params={"uploadFileId": upload_file_id, "partInfo": part_info},
                )
                upload_urls = urls.get("uploadUrls") or {}
                target = upload_urls.get("partNumber_%d" % index)
                if not target:
                    raise Pc189Error("获取上传地址失败：%s" % (urls.get("msg") or "未知错误"))
                headers = self._parse_upload_headers(target.get("requestHeader") or "")
                self._put_chunk(target["requestURL"], headers, chunk,
                                on_sent=lambda n: report(index, n))

            self._retry(send, "上传第 %d 片" % index)

        if threads > 1 and total_parts > 1:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=threads,
                                   thread_name_prefix="qhs-part") as pool:
                list(pool.map(upload_part, range(1, total_parts + 1)))
        else:
            for index in range(1, total_parts + 1):
                if should_cancel and should_cancel():
                    raise Pc189Error("上传已取消")
                upload_part(index)

        file_md5_hex = file_md5.hexdigest().upper()
        slice_md5_hex = file_md5_hex
        if file_size > slice_size:
            slice_md5_hex = hashlib.md5(
                "\n".join(slice_md5s).encode("utf-8")).hexdigest().upper()
        return self._commit_upload(upload_file_id, file_md5_hex, slice_md5_hex, file_size)

    def _commit_upload(self, upload_file_id: str, file_md5: str, slice_md5: str,
                       file_size: int) -> str:
        params = {
            "uploadFileId": upload_file_id,
            "fileMd5": file_md5,
            "sliceMd5": slice_md5,
            "lazyCheck": "1",
            "isLog": "0",
            "opertype": "3",
        }
        if file_size:
            params["fileSize"] = str(file_size)
        data = self.request(UPLOAD_URL + "/person/commitMultiUploadFile", "GET", params=params)
        if data.get("code") not in (None, "SUCCESS"):
            raise Pc189Error("提交上传失败：%s" % (data.get("msg") or "未知错误"))
        file_info = data.get("file") or {}
        return str(file_info.get("userFileId") or file_info.get("id") or "")

    @staticmethod
    def _parse_upload_headers(raw: str) -> Dict[str, str]:
        """解析服务端下发的上传请求头。

        天翼下发的 requestHeader 形如
        ``Content-Type=application/octet-stream&Authorization=AWS ...&Content-MD5=...``，
        用 ``&`` 分隔、首个 ``=`` 分隔键值（值里可能还有 ``=``，例如 base64 结尾的
        ``==``，所以不能用 ``split("=")``）。

        部分环境下发的是 URL 编码形式（``x-token%3Dabc%26...``），先解码再拆，
        否则拿不到真正的鉴权头。对应 SDK 里的
        ``decodeURIComponent(upload_data['requestHeader'])``。
        """
        headers: Dict[str, str] = {}
        decoded = unquote(raw or "")
        for pair in decoded.split("&"):
            if "=" in pair:
                key, _, value = pair.partition("=")
                headers[key] = value
        return headers

    # ---------- 其它 ----------
    def download_url(self, file_id: str) -> str:
        """取文件下载地址。"""
        data = self.request(
            API_URL + "/getFileDownloadUrl.action",
            "GET",
            query={"fileId": str(file_id), "dt": "3", "flag": "1"},
        )
        url = data.get("fileDownloadUrl") or data.get("downloadUrl") or ""
        if not url:
            raise Pc189Error("获取下载地址失败")
        return url.replace("http://", "https://", 1)

    def disk_info(self) -> Dict[str, Any]:
        """取网盘容量信息。

        天翼返回的字段在 ``cloudCapacityInfo`` 里（``usedSize`` /
        ``totalSize`` / ``freeSize``），这里归一化成 ``used`` / ``total`` /
        ``free`` 三个字节数，调用方不用再关心原始结构。
        """
        data = self.request(API_URL + "/portal/getUserSizeInfo.action", "GET")
        capacity = data.get("cloudCapacityInfo") or {}
        return {
            "used": int(capacity.get("usedSize") or 0),
            "total": int(capacity.get("totalSize") or 0),
            "free": int(capacity.get("freeSize") or 0),
            "account": data.get("account") or "",
        }

    def keep_alive(self) -> None:
        """会话保活。"""
        try:
            self.request(API_URL + "/keepUserSession.action", "GET")
        except Pc189Error as exc:
            log.warning("会话保活失败：%s", exc)
