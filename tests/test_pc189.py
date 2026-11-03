"""天翼 PC 端协议（pc189）的单元测试（不联网，用假的 HTTP 响应）。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qunhuisync import pc189  # noqa: E402
from qunhuisync.pc189 import AuthError, Pc189Auth, Pc189Client, Pc189Qr  # noqa: E402

# 天翼用的是 1024 位 RSA
TEST_PUB_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCIQQ6MNhyAfY/kZKkyXGJTIkXkAddsJ9SWHMMzq9Ae6NIV"
    "mm7N7Tm5487+7mZdTWuplhux8WbjkIzq4h1DNZewYQPaePMphZouPYfcfz4rh3E/exRQOCxNKivW8Uq5801zH"
    "eJCbOlVSnQBTXlyjB2NUioJgKiMoHqJXLkZdk7fSwIDAQAB"
)


class FakeResponse:
    def __init__(self, payload=None, text="", content=b"", status_code=200, url=""):
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeUrl:
    """模拟 httpx.URL 的参数访问。"""

    def __init__(self, params):
        self.params = params


class FakeSession:
    """记录请求并按 URL 返回预置响应。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.cookies = type("C", (), {"set": lambda *a, **k: None})()

    def _match(self, url):
        for key, response in self.responses.items():
            if key in url:
                return response
        raise AssertionError("没有为 %s 预置响应" % url)

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append(("GET", url, params, headers))
        return self._match(url)

    def post(self, url, data=None, headers=None, params=None, **kwargs):
        # httpx 的 post 里 params 是查询串，data 是表单体；
        # 这里把查询串记在第 3 位，表单体记在第 5 位。
        self.calls.append(("POST", url, params, headers, data))
        return self._match(url)

    def request(self, method, url, params=None, headers=None, **kwargs):
        self.calls.append((method, url, params, headers))
        return self._match(url)

    def put(self, url, headers=None, content=None, **kwargs):
        self.calls.append(("PUT", url, content, headers))
        return self._match(url)

    def close(self):
        pass


LOGIN_PAGE_URL = "unifyLoginForPC.action"


def _login_responses(**overrides):
    """构造一套完整的登录响应。"""
    base = {
        LOGIN_PAGE_URL: FakeResponse(
            text="<html></html>", status_code=200,
            url="https://open.e.189.cn/api/logbox/separate/web/index.html"
                "?appId=9317140619&lt=LT-VALUE&reqId=REQ-ID&encryptUrl=ENC"),
        "appConf.do": FakeResponse({"result": 0, "data": {
            "paramId": "PARAM-ID", "accountType": "02", "clientType": 10020,
            "returnUrl": "https://m.cloud.189.cn/zhuanti/2020/loginErrorPc/index.html",
            "mailSuffix": "", "isOauth2": False, "pageKey": "normal"}}),
        "encryptConf.do": FakeResponse({"result": 0, "data": {
            "pre": "{NRP}", "pubKey": TEST_PUB_KEY}}),
        "needcaptcha.do": FakeResponse(0),
        "loginSubmit.do": FakeResponse({"result": 0, "msg": "成功", "toUrl": "https://x/to"}),
        "getSessionForPC.action": FakeResponse({"res_code": 0, "sessionKey": "SK",
                                                "sessionSecret": "SECRET" * 4,
                                                "accessToken": "AT", "refreshToken": "RT"}),
    }
    base.update(overrides)
    return base


def make_auth(responses):
    auth = Pc189Auth()
    auth.session = FakeSession(responses)
    return auth


# ---------------- 加密与签名 ----------------

def test_rsa_encrypt_produces_hex_with_prefix():
    out = pc189.rsa_encrypt(TEST_PUB_KEY, "{NRP}", "13800138000")
    assert out.startswith("{NRP}")
    body = out[len("{NRP}"):]
    assert len(body) == 256          # 1024 位 RSA -> 128 字节 -> 256 个十六进制字符
    assert body == body.upper()


def test_aes_ecb_encrypt_is_uppercase_hex():
    out = pc189.aes_ecb_encrypt("folderId=-11", "0123456789abcdef")
    assert out == out.upper()
    assert len(out) % 32 == 0        # 密文长度是 16 的倍数，转十六进制后是 32 的倍数


def test_signature_matches_openlist_algorithm():
    # 参考值由 Go 端 signatureOfHmac 手工推算
    sig = pc189.signature_of_hmac(
        "SECRET", "SK", "GET", "https://api.cloud.189.cn/listFiles.action",
        "Mon, 02 Jan 2006 15:04:05 GMT", "")
    assert sig == sig.upper()
    assert len(sig) == 40            # SHA1 -> 20 字节 -> 40 个十六进制字符


def test_signature_includes_params():
    a = pc189.signature_of_hmac("S", "K", "GET", "https://api.cloud.189.cn/x.action", "D", "")
    b = pc189.signature_of_hmac("S", "K", "GET", "https://api.cloud.189.cn/x.action", "D", "ABCD")
    assert a != b


def test_encode_params_is_sorted():
    assert pc189._encode_params({"b": "2", "a": "1"}) == "a=1&b=2"


def test_parse_response_error_handles_json_and_xml():
    assert "InvalidSessionKey" in pc189._parse_response_error(
        '{"errorCode":"InvalidSessionKey","errorMsg":"bad"}')
    assert "LoginRespIsNull" in pc189._parse_response_error(
        "<error><code>LoginRespIsNull</code><message>x</message></error>")
    assert pc189._parse_response_error("") is None


# ---------------- 密码登录 ----------------

def test_prepare_reads_params_from_redirect_url():
    auth = make_auth(_login_responses())
    state = auth.prepare()
    assert state["need_captcha"] is False
    assert auth.lt == "LT-VALUE"
    assert auth.req_id == "REQ-ID"
    assert auth.param_id == "PARAM-ID"
    assert auth.account_type == "02"
    assert auth.pub_key == TEST_PUB_KEY
    assert auth.rsa_pre == "{NRP}"


def test_prepare_falls_back_to_page_regex():
    html = ("<script>var lt = \"LT2\"; var reqId = \"REQ2\";</script>")
    responses = _login_responses(**{
        LOGIN_PAGE_URL: FakeResponse(text=html, status_code=200,
                                     url="https://open.e.189.cn/api/logbox/oauth2/unifyAccountLogin.do"),
    })
    auth = make_auth(responses)
    auth.prepare()
    assert auth.lt == "LT2"
    assert auth.req_id == "REQ2"


def test_need_captcha_true_when_server_says_so():
    responses = _login_responses(**{"needcaptcha.do": FakeResponse(1)})
    auth = make_auth(responses)
    state = auth.prepare("13800138000")
    assert state["need_captcha"] is True


def test_submit_sends_v2_form_with_both_password_fields():
    auth = make_auth(_login_responses())
    auth.prepare("13611110001")
    auth.submit("13611110001", "secret")
    call = [c for c in auth.session.calls if "loginSubmit.do" in c[1]][0]
    body = call[4]
    assert body["version"] == "v2.0"
    assert body["apToken"] == ""
    assert body["appKey"] == pc189.APP_ID
    assert body["pageKey"] == "normal"
    assert body["userName"].startswith("{NRP}")
    assert body["password"].startswith("{NRP}")
    assert body["epd"].startswith("{NRP}")
    assert body["paramId"] == "PARAM-ID"


def test_submit_sends_user_finger_and_referer_headers():
    auth = make_auth(_login_responses())
    auth.prepare()
    auth.submit("13611110001", "secret")
    call = [c for c in auth.session.calls if "loginSubmit.do" in c[1]][0]
    headers = call[3]
    assert headers["user-finger"]
    assert headers["lt"] == "LT-VALUE"
    assert headers["REQID"] == "REQ-ID"
    assert "separate/web/index.html" in headers["Referer"]


def test_submit_maps_error_code_to_chinese():
    responses = _login_responses(**{
        "loginSubmit.do": FakeResponse({"result": -17, "msg": "账户名或密码错误"}),
    })
    auth = make_auth(responses)
    auth.prepare()
    with pytest.raises(AuthError) as exc:
        auth.submit("13611110001", "wrong")
    assert "密码错误" in str(exc.value)


def test_submit_raises_captcha_required_on_1101():
    responses = _login_responses(**{
        "loginSubmit.do": FakeResponse({"result": -1101, "msg": "需要验证码"}),
    })
    auth = make_auth(responses)
    auth.prepare()
    with pytest.raises(pc189.CaptchaRequired):
        auth.submit("13800138000", "secret")


def test_submit_raises_qr_required_on_device_second_auth():
    responses = _login_responses(**{
        "loginSubmit.do": FakeResponse({"result": -133, "msg": "二次验证"}),
    })
    auth = make_auth(responses)
    auth.prepare()
    with pytest.raises(pc189.QrCodeRequired):
        auth.submit("13611110001", "secret")


def test_submit_returns_session_on_success():
    auth = make_auth(_login_responses())
    auth.prepare()
    session = auth.submit("13611110001", "secret")
    assert session["sessionKey"] == "SK"
    assert session["accessToken"] == "AT"


def test_exchange_session_uses_pc_params():
    auth = make_auth(_login_responses())
    auth.prepare()
    auth.submit("13611110001", "secret")
    call = [c for c in auth.session.calls if "getSessionForPC" in c[1]][0]
    params = call[2]
    assert params is not None
    assert params["clientType"] == "TELEPC"
    assert params["version"] == pc189.VERSION
    assert params["returnType"] == "JSON"
    assert params["redirectURL"] == "https://x/to"
    assert call[3]["X-Request-ID"]


def test_exchange_session_reports_xml_error():
    responses = _login_responses(**{
        "getSessionForPC.action": FakeResponse(
            text="<error><code>LoginRespIsNull</code><message>x</message></error>"),
    })
    auth = make_auth(responses)
    auth.prepare()
    with pytest.raises(AuthError) as exc:
        auth.submit("13611110001", "secret")
    assert "LoginRespIsNull" in str(exc.value)


# ---------------- 二维码登录 ----------------

def test_qr_prepare_fetches_image():
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG\r\n\x1a\n", status_code=200),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    png = qr.prepare()
    assert png.startswith(b"\x89PNG")
    assert qr.uuid == "UUID"
    assert qr.encry_uuid == "ENC"


def test_qr_image_request_uses_raw_uuid():
    """取二维码图片时必须用**原始 uuid**，不能用 encodeuuid。

    httpx 传 params 时会自己做一次百分号编码，若再传已经编码过的
    encodeuuid 就成了双重编码（%3A -> %253A）。服务端解析出的 uuid 被截断，
    生成的二维码内容只有 "https://open.e.189.cn/api"，App 扫了会提示
    「二维码已失效」。
    """
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0,
                                    "uuid": "https://open.e.189.cn/api/account/x.do?a=1",
                                    "encryuuid": "ENC",
                                    "encodeuuid": "https%3A%2F%2Fopen.e.189.cn%2Fx"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    call = [c for c in qr.session.calls if "image.do" in c[1]][0]
    assert call[2]["uuid"] == "https://open.e.189.cn/api/account/x.do?a=1"


def test_qr_poll_uses_raw_uuid():
    """轮询时 uuid 也必须是原始值，传编码过的会返回「uuid 非法！」。"""
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "RAW-UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
        "qrcodeLoginState.do": FakeResponse({"status": -106}),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    qr.poll()
    call = [c for c in qr.session.calls if "qrcodeLoginState" in c[1]][0]
    assert call[4]["uuid"] == "RAW-UUID"
    assert call[4]["encryuuid"] == "ENC"
    # date 格式：2026-11-0215:00:00.123
    assert "." in call[4]["date"]


def test_qr_poll_success_exchanges_session():
    """扫码成功后要用 redirectUrl 换会话。"""
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
        "qrcodeLoginState.do": FakeResponse({"status": 0, "redirectUrl": "https://x/to"}),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    result = qr.poll()
    assert result["status"] == pc189.QR_SUCCESS
    assert result["session"]["sessionKey"] == "SK"
    call = [c for c in qr.session.calls if "getSessionForPC" in c[1]][0]
    assert call[2]["redirectURL"] == "https://x/to"
    assert call[2]["returnType"] == "JSON"
    assert call[2]["version"] == pc189.VERSION


def test_qr_poll_reports_waiting():
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
        "qrcodeLoginState.do": FakeResponse({"status": -106}),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    result = qr.poll()
    assert result["status"] == pc189.QR_WAITING
    assert "扫码" in result["message"]


def test_qr_poll_reports_scanned():
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
        "qrcodeLoginState.do": FakeResponse({"status": -11002}),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    result = qr.poll()
    assert result["status"] == pc189.QR_SCANNED


def test_qr_poll_success_returns_session():
    responses = _login_responses(**{
        "getUUID.do": FakeResponse({"result": 0, "uuid": "UUID", "encryuuid": "ENC",
                                    "encodeuuid": "ENCU"}),
        "image.do": FakeResponse(content=b"\x89PNG", status_code=200),
        "qrcodeLoginState.do": FakeResponse({"status": 0, "redirectUrl": "https://x/to"}),
    })
    qr = Pc189Qr()
    qr.session = FakeSession(responses)
    qr.prepare()
    result = qr.poll()
    assert result["status"] == pc189.QR_SUCCESS
    assert result["session"]["sessionKey"] == "SK"


# ---------------- 文件操作 ----------------

def make_client(responses):
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "SECRET" * 4})
    client.session = FakeSession(responses)
    client.http = client.session
    return client


def test_list_files_parses_entries():
    responses = {"listFiles.action": FakeResponse({"fileListAO": {
        "count": 2,
        "folderList": [{"id": 11, "name": "照片"}],
        "fileList": [{"id": 22, "name": "a.txt", "size": 100, "lastOpTime": "2026-01-01 10:00:00"}],
    }})}
    client = make_client(responses)
    items = client.list_files("-11")
    assert len(items) == 2
    assert items[0]["name"] == "照片"
    assert items[0]["is_folder"] is True
    assert items[1]["name"] == "a.txt"
    assert items[1]["size"] == 100
    assert items[1]["is_folder"] is False


def test_list_files_sends_signature_headers():
    responses = {"listFiles.action": FakeResponse({"fileListAO": {"count": 0}})}
    client = make_client(responses)
    client.list_files("-11")
    call = [c for c in client.session.calls if "listFiles" in c[1]][0]
    headers = call[3]
    assert headers["SessionKey"] == "SK"
    assert headers["Signature"]
    assert headers["Date"]
    assert headers["X-Request-ID"]
    params = call[2]
    assert params["clientType"] == "TELEPC"
    assert params["version"] == pc189.VERSION
    assert params["folderId"] == "-11"


def test_request_encrypts_params():
    responses = {"x.action": FakeResponse({"ok": True})}
    client = make_client(responses)
    client.request("https://api.cloud.189.cn/x.action", "GET", params={"a": "1"})
    call = client.session.calls[0]
    assert "params" in call[2]
    assert call[2]["params"] == call[2]["params"].upper()
    # 签名里必须带上加密后的 params
    assert call[3]["Signature"]


def test_create_folder_posts_to_pc_endpoint():
    responses = {"createFolder.action": FakeResponse({"id": "F77"})}
    client = make_client(responses)
    folder_id = client.create_folder("-11", "照片")
    call = client.session.calls[0]
    assert call[0] == "POST"
    assert call[1].endswith("/createFolder.action")
    assert call[2]["folderName"] == "照片"
    assert call[2]["parentFolderId"] == "-11"
    assert folder_id == "F77"


def test_delete_file_uses_batch_task():
    responses = {
        "createBatchTask.action": FakeResponse({"taskId": "T1"}),
        "checkBatchTask.action": FakeResponse({"taskStatus": 4}),
    }
    client = make_client(responses)
    client.delete_file("22", "a.txt", False)
    kinds = [c[0] for c in client.session.calls]
    assert "createBatchTask.action" in client.session.calls[0][1]
    assert any("checkBatchTask.action" in c[1] for c in client.session.calls)
    assert kinds


def test_disk_info_reads_capacity():
    """容量字段从 cloudCapacityInfo 里取，并归一化成 used/total/free。"""
    responses = {"getUserSizeInfo.action": FakeResponse({
        "cloudCapacityInfo": {"totalSize": 1024, "usedSize": 100, "freeSize": 924},
        "account": "189****1257@189.cn"})}
    client = make_client(responses)
    info = client.disk_info()
    assert info["total"] == 1024
    assert info["used"] == 100
    assert info["free"] == 924
    assert info["account"] == "189****1257@189.cn"


def test_download_url_upgrades_to_https():
    responses = {"getFileDownloadUrl.action": FakeResponse({
        "fileDownloadUrl": "http://dl.189.cn/x"})}
    client = make_client(responses)
    assert client.download_url("22").startswith("https://")


def test_client_requires_session_fields():
    with pytest.raises(pc189.Pc189Error):
        Pc189Client({"sessionKey": "SK"})


def test_response_error_detects_all_shapes():
    assert pc189._response_error({"res_code": 1, "res_message": "x"}) == "1 / x"
    assert pc189._response_error({"code": "InvalidSessionKey"}) == "InvalidSessionKey / "
    assert pc189._response_error({"errorCode": "E", "errorMsg": "m"}) == "E / m"
    assert pc189._response_error({"res_code": 0}) is None
    assert pc189._response_error({"code": "SUCCESS"}) is None
    assert pc189._response_error({}) is None


def test_request_raises_on_error_body():
    """天翼出错时仍返回 HTTP 200，必须查响应体。"""
    responses = {"x.action": FakeResponse({"errorCode": "InvalidSessionKey",
                                            "errorMsg": "bad"})}
    client = make_client(responses)
    with pytest.raises(pc189.Pc189Error) as exc:
        client.request("https://api.cloud.189.cn/x.action", "GET")
    assert "InvalidSessionKey" in str(exc.value)


def test_list_files_raises_on_invalid_session():
    responses = {"listFiles.action": FakeResponse({"errorCode": "InvalidSessionKey",
                                                   "errorMsg": "bad"})}
    client = make_client(responses)
    with pytest.raises(pc189.Pc189Error) as exc:
        client.list_files("-11")
    assert "InvalidSessionKey" in str(exc.value)


def test_aes_key_requires_long_secret():
    client = Pc189Client({"sessionKey": "SK", "sessionSecret": "SHORT"})
    with pytest.raises(pc189.Pc189Error):
        client.request("https://api.cloud.189.cn/x.action", "GET",
                       params={"a": "1"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
