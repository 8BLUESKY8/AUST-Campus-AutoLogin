# -*- coding: utf-8 -*-
"""安徽理工大学校园网 Dr.COM 自动登录脚本。

配置通过环境变量提供，避免把密码提交到脚本或命令行历史：

    $env:AUST_USERNAME = "你的学号"
    $env:AUST_PASSWORD = "你的密码"
    $env:AUST_CAMPUS = "auto"       # auto / hefei / huainan
    $env:AUST_EXIT_SUFFIX = "@hfcmcc"  # 后缀按校区填写；校内资源可留空
    python .\aust_autologin.py --once

不带参数时进入守护模式。认证服务器、接入名称和检测间隔也可以用
AUST_PORTAL_IP、AUST_PORTAL_PORT、AUST_WLAN_AC_NAME、AUST_WLAN_AC_IP、
AUST_CAMPUS、AUST_CONNECTION、AUST_CHECK_INTERVAL
覆盖。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
DRCOM_JS_VERSION = "4.2"
HEFEI_IP = "172.24.34.2"
HUAINAN_IP = "10.255.0.19"
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aust_autologin.log")

ONLINE_HINTS = (
    "已经在线",
    "已在线",
    "认证成功",
    "登录成功",
    "success",
)
FAIL_HINTS = (
    "密码错误",
    "账号错误",
    "账号不存在",
    "用户不存在",
    "用户名或密码",
    "认证失败",
    "登录失败",
    "密码不能为空",
    "余额不足",
    "欠费",
    "无权限",
    "达到并发",
    "not online",
    "error",
    "failed",
    "failure",
)
PORTAL_HINTS = (
    "eportal",
    "drcom",
    "srun",
    "captive",
    "portal",
    "认证",
    "校园网",
    "请输入用户名",
    "用户名登录",
)


class ConfigError(ValueError):
    """配置缺失或格式不正确。"""


def _authority(host: str, port: int) -> str:
    """构造 HTTP authority；默认 80 端口必须省略，匹配浏览器入口。"""
    return host if port == 80 else "%s:%d" % (host, port)


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    campus: str = "hefei"
    connection: str = "wifi"
    exit_suffix: str = ""
    portal_ip: str = HEFEI_IP
    portal_port: int = 80
    eportal_port: int = 801
    wlan_ac_name: str = "BRAS"
    wlan_ac_ip: str = "172.24.255.254"
    check_interval: int = 60

    @property
    def account(self) -> str:
        """ePortal 兼容接口使用的账号格式：,0,学号+出口后缀。"""
        return ",0," + self.legacy_account

    @property
    def legacy_account(self) -> str:
        """当前 AUST 登录页 /drcom/login 使用不带 ,0, 前缀的账号。"""
        username = self.username
        selected_suffix = self.exit_suffix
        suffix_aliases = {
            "@hfcmcc": "@hfcmcc",
            "@cmcc": "@cmcc",
            "@unicom": "@unicom",
            "@jzg": "@jzg",
            "@aust": "@aust",
            "@yd": "@hfcmcc",
            "@dx": "@aust",
        }
        lowered = username.lower()
        for suffix, normalized in suffix_aliases.items():
            if lowered.endswith(suffix):
                username = username[: -len(suffix)]
                if not selected_suffix:
                    selected_suffix = normalized
                break
        return username + selected_suffix


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ConfigError("环境变量 %s 必须是整数" % name)
    if parsed < minimum:
        raise ConfigError("环境变量 %s 必须大于等于 %d" % (name, minimum))
    return parsed


def _detect_portal_ip() -> str:
    """自动选择校区；只探测认证首页，不提交账号密码。"""
    for candidate in (HEFEI_IP, HUAINAN_IP):
        try:
            request = urllib.request.Request(
                "http://%s/" % candidate,
                headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                method="GET",
            )
            with _DIRECT_OPENER.open(request, timeout=1.5) as response:
                body = _read_response(response, 4096)
                if response.status in (200, 302, 303) or body:
                    return candidate
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, OSError):
            continue
    return HEFEI_IP


def load_config(require_credentials: bool = True) -> Config:
    username = os.environ.get("AUST_USERNAME", "").strip()
    password = os.environ.get("AUST_PASSWORD", "")
    campus = os.environ.get("AUST_CAMPUS", "auto").strip().lower()
    if campus not in ("auto", "hefei", "huainan"):
        raise ConfigError("AUST_CAMPUS 只能是 auto、hefei 或 huainan")
    connection = os.environ.get("AUST_CONNECTION", "wifi").strip().lower()
    if connection not in ("wifi", "wired"):
        raise ConfigError("AUST_CONNECTION 只能是 wifi 或 wired")
    suffix = os.environ.get("AUST_EXIT_SUFFIX", "").strip().lower()
    # 兼容旧脚本中的运营商简称；值以当前认证页的实际 option 为准。
    suffix = {"@dx": "@aust", "@yd": "@hfcmcc"}.get(suffix, suffix)
    if suffix not in ("", "@aust", "@hfcmcc", "@cmcc", "@unicom", "@jzg"):
        raise ConfigError("AUST_EXIT_SUFFIX 不是支持的出口后缀")
    if require_credentials and not username:
        raise ConfigError("未设置 AUST_USERNAME")
    if require_credentials and not password:
        raise ConfigError("未设置 AUST_PASSWORD")

    configured_ip = os.environ.get("AUST_PORTAL_IP", "").strip()
    if configured_ip:
        portal_ip = configured_ip
    elif campus == "huainan":
        portal_ip = HUAINAN_IP
    elif campus == "hefei":
        portal_ip = HEFEI_IP
    else:
        portal_ip = _detect_portal_ip()
        campus = "huainan" if portal_ip == HUAINAN_IP else "hefei"
    # “移动”在两校区使用不同后缀；允许计划任务的 auto 模式自动纠正。
    if campus == "huainan" and suffix == "@hfcmcc":
        suffix = "@cmcc"
    elif campus == "hefei" and suffix == "@cmcc":
        suffix = "@hfcmcc"
    if campus == "hefei" and suffix in ("@unicom", "@jzg"):
        raise ConfigError("@unicom 和 @jzg 目前只确认适用于淮南校区")
    if not portal_ip:
        raise ConfigError("AUST_PORTAL_IP 不能为空")
    try:
        portal_port = _env_int("AUST_PORTAL_PORT", 80)
        eportal_port = _env_int("AUST_EPORTAL_PORT", 801)
    except ConfigError:
        raise
    if portal_port > 65535:
        raise ConfigError("AUST_PORTAL_PORT 必须不超过 65535")
    if eportal_port > 65535:
        raise ConfigError("AUST_EPORTAL_PORT 必须不超过 65535")

    return Config(
        username=username,
        password=password,
        campus=campus,
        connection=connection,
        exit_suffix=suffix,
        portal_ip=portal_ip,
        portal_port=portal_port,
        eportal_port=eportal_port,
        wlan_ac_name=os.environ.get("AUST_WLAN_AC_NAME", "BRAS").strip(),
        wlan_ac_ip=os.environ.get("AUST_WLAN_AC_IP", "172.24.255.254").strip(),
        check_interval=_env_int("AUST_CHECK_INTERVAL", 60, minimum=5),
    )


def _safe_print(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass


def log(message: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    _safe_print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor()
)
_DIRECT_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(), _NoRedirect()
)


def http_get(
    url: str,
    timeout: float = 6,
    allow_redirect: bool = True,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    opener = _DIRECT_OPENER if allow_redirect else _DIRECT_NO_REDIRECT_OPENER
    request_headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        headers=request_headers,
        method="GET",
    )
    return opener.open(request, timeout=timeout)


def http_post(
    url: str,
    data: Dict[str, str],
    timeout: float = 8,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    request_headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Connection": "keep-alive",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    return _DIRECT_OPENER.open(request, timeout=timeout)


def _decode_body(raw: bytes, headers: Any = None) -> str:
    charset = ""
    if headers is not None:
        content_type = headers.get("Content-Type", "")
        match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
    for encoding in (charset, "utf-8", "gb18030", "gbk"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _read_response(response: Any, limit: int = 8192) -> str:
    try:
        return _decode_body(response.read(limit), response.headers)
    finally:
        response.close()


def _ipconfig_adapters() -> List[Tuple[str, List[str]]]:
    """解析 Windows ipconfig /all，返回 [(无分隔符的大写 MAC, IPv4 列表)]。"""
    try:
        completed = subprocess.run(
            ["ipconfig", "/all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    text = ""
    for encoding in ("utf-8", "gb18030", "gbk", "cp1252"):
        try:
            text = completed.stdout.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []

    result: List[Tuple[str, List[str]]] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text)
    mac_pattern = re.compile(r"([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})")
    ip_pattern = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
    for block in blocks:
        if not re.search(r"物理地址|Physical Address", block, re.I):
            continue
        mac_match = mac_pattern.search(block)
        if not mac_match:
            continue
        mac = mac_match.group(1).replace("-", "").replace(":", "").upper()
        ips: List[str] = []
        for candidate in ip_pattern.findall(block):
            try:
                address = ipaddress.ip_address(candidate)
                if address.version == 4 and candidate not in ips:
                    ips.append(candidate)
            except ValueError:
                pass
        result.append((mac, ips))
    return result


def _route_local_ip(config: Config) -> Optional[str]:
    """取到认证服务器的路由所使用的本机 IPv4，断网时也通常可用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((config.portal_ip, config.portal_port))
        address = sock.getsockname()[0]
        ipaddress.ip_address(address)
        return address
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def get_local_identity(config: Config) -> Tuple[str, str]:
    """返回 (本机 IPv4, MAC)，优先选择通往认证服务器的网卡。"""
    adapters = _ipconfig_adapters()
    ip = _route_local_ip(config)
    if not ip:
        preferred = [address for _, addresses in adapters for address in addresses if address.startswith("10.140.")]
        ip = preferred[0] if preferred else None
    if not ip:
        # UDP connect 不会真正访问外网，只用于让系统选择默认路由。
        fallback = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            fallback.connect(("8.8.8.8", 80))
            ip = fallback.getsockname()[0]
        except OSError:
            ip = None
        finally:
            fallback.close()
    ip = ip or "0.0.0.0"

    mac = ""
    for adapter_mac, addresses in adapters:
        if ip in addresses:
            mac = adapter_mac
            break
    if not mac:
        for adapter_mac, addresses in adapters:
            if adapter_mac != "000000000000" and any(address.startswith("10.140.") for address in addresses):
                mac = adapter_mac
                break
    if not mac:
        try:
            mac = "%012X" % (uuid.getnode() & 0xFFFFFFFFFFFF)
        except Exception:
            mac = "000000000000"
    return ip, mac


def _looks_like_portal(value: str) -> bool:
    lowered = value.lower()
    return any(hint.lower() in lowered for hint in PORTAL_HINTS)


def query_drcom_status(config: Config) -> Tuple[Optional[str], str]:
    """查询 Dr.COM 状态，返回 (状态, 失败原因)。"""
    params = urllib.parse.urlencode(
        {
            "callback": "dr1001",
            "jsVersion": DRCOM_JS_VERSION,
            "v": str(int(time.time() * 1000) % 10000 + 500),
            "lang": "zh",
        }
    )
    url = "http://%s/drcom/chkstatus?%s" % (_authority(config.portal_ip, config.portal_port), params)
    try:
        with http_get(url, timeout=5, allow_redirect=True) as response:
            data = _extract_json(_read_response(response, 8192))
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, OSError) as error:
        return None, str(error)
    if not data:
        return None, "接口返回内容不是可识别的 JSONP"
    result = _as_int(data.get("result"))
    if result == 1:
        return "online", ""
    if result == 0:
        return "portal", ""
    return None, "接口返回未知状态 result=%r" % data.get("result")


def probe_drcom_status(config: Config) -> Optional[str]:
    state, _ = query_drcom_status(config)
    return state


def probe_network(config: Config) -> str:
    """探测认证及外网状态。返回 online、portal 或 offline。"""
    drcom_state = probe_drcom_status(config)
    if drcom_state is not None:
        return drcom_state

    candidates = (
        ("http://cp.cloudflare.com/", "cp.cloudflare.com"),
        ("http://www.gstatic.com/generate_204", "www.gstatic.com"),
        ("http://www.baidu.com/", "baidu.com"),
    )
    saw_portal = False
    for url, expected_host in candidates:
        try:
            with http_get(url, timeout=6, allow_redirect=True) as response:
                final_url = response.geturl()
                body = _read_response(response, 4096)
                status = getattr(response, "status", 200)
            final_host = (urllib.parse.urlparse(final_url).hostname or "").lower()
            if _looks_like_portal(final_url + " " + body[:1000]):
                saw_portal = True
                continue
            if status == 204:
                return "online"
            if status == 200 and (
                expected_host in final_host or expected_host.endswith(final_host) or final_host.endswith(expected_host)
            ):
                return "online"
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location", "") if error.headers else ""
            if _looks_like_portal(location):
                saw_portal = True
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            continue
    return "portal" if saw_portal else "offline"


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """解析 dr1002({...})，不使用贪婪正则以免尾随 HTML 破坏 JSON。"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _classify_login(text: str) -> Tuple[bool, str]:
    data = _extract_json(text)
    values: Iterable[Any] = ()
    if data:
        values = (data.get(key, "") for key in (
            "msg", "res_msg", "message", "error_msg", "error", "msga", "errmsg"
        ))
    message = " ".join(str(value) for value in values if value not in (None, "")).strip()
    if data:
        details = []
        for key in ("result", "ret_code", "res_code", "msg", "msga", "error", "error_msg"):
            if key in data and data[key] not in (None, ""):
                details.append("%s=%s" % (key, data[key]))
        if details and message in ("1", "0"):
            message = "%s (%s)" % (message, ", ".join(details))
    if not message:
        message = re.sub(r"\s+", " ", text[:240]).strip()
    lowered = message.lower()

    if any(hint.lower() in lowered for hint in FAIL_HINTS):
        return False, "登录失败: " + message
    if any(hint.lower() in lowered for hint in ONLINE_HINTS):
        return True, "已在线/登录成功: " + message

    result = _as_int(data.get("result")) if data else None
    ret_code = _as_int(data.get("ret_code")) if data else None
    res_code = _as_int(data.get("res_code")) if data else None

    # 常见 Dr.COM 版本：result=1 成功，或 ret_code/res_code=0 成功。
    if result == 1:
        return True, "登录成功" + ((": " + message) if message else "")
    if result == 0:
        return False, "登录失败" + ((": " + message) if message else "")
    if ret_code == 0 or res_code == 0:
        return True, "登录成功" + ((": " + message) if message else "")
    if (ret_code is not None and ret_code != 0) or (res_code is not None and res_code != 0):
        return False, "登录失败" + ((": " + message) if message else "")
    if any(hint.lower() in lowered for hint in ("成功", "success", "ok")):
        return True, "登录成功: " + message
    return False, "登录结果未知: " + (message or "认证服务器没有返回内容")


def _is_generic_login_failure(message: str) -> bool:
    """本地 Dr.COM 常把多种失败都压缩为 msg=1，需尝试新版接口。"""
    normalized = message.strip().lower()
    return normalized in {"登录失败: 1", "登录结果未知: 1", "登录失败: 1 1"}


def _eportal_login(config: Config) -> Tuple[bool, str]:
    """调用当前无线学生页面 login_method=1 使用的 ePortal 登录接口。"""
    ip, mac = get_local_identity(config)
    params = {
        "callback": "dr1003",
        "login_method": "1",
        "user_account": ",0," + config.legacy_account,
        "user_password": config.password,
        "wlan_user_ip": ip,
        "wlan_user_mac": mac,
        "wlan_ac_ip": config.wlan_ac_ip,
        "wlan_ac_name": config.wlan_ac_name,
        "wlan_user_ipv6": "",
        "wlan_vlan_id": "0",
        "jsVersion": DRCOM_JS_VERSION,
        "terminal_type": "1",
        "lang": "zh-cn",
        "v": str(int(time.time() * 1000) % 10000 + 500),
    }
    url = "http://%s:%d/eportal/portal/login?%s" % (
        config.portal_ip,
        config.eportal_port,
        urllib.parse.urlencode(params),
    )
    try:
        with http_get(url, timeout=8, allow_redirect=True) as response:
            text = _read_response(response, 8192)
    except urllib.error.HTTPError as error:
        try:
            text = _decode_body(error.read(8192), error.headers)
        except Exception:
            text = ""
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        return False, "ePortal 登录接口请求失败: %s" % error
    return _classify_login(text)


def _huainan_login(config: Config) -> Tuple[bool, str]:
    """淮南校区旧版 Dr.COM：Wi-Fi 页面使用 GET /drcom/login。"""
    suffix = config.exit_suffix
    if suffix == "@hfcmcc":
        suffix = "@cmcc"
    account = config.username
    embedded_suffix = ""
    for known in ("@aust", "@cmcc", "@unicom", "@jzg", "@hfcmcc"):
        if account.lower().endswith(known):
            account = account[: -len(known)]
            embedded_suffix = "@cmcc" if known == "@hfcmcc" else known
            break
    account += suffix or embedded_suffix
    params = {
        "callback": "dr1003",
        "DDDDD": account,
        "upass": config.password,
        "0MKKey": "123456",
    }
    endpoint = "http://%s/drcom/login" % _authority(config.portal_ip, config.portal_port)
    try:
        if config.connection == "wired":
            response = http_post(endpoint, params, timeout=8)
        else:
            url = endpoint + "?" + urllib.parse.urlencode(params)
            response = http_get(url, timeout=8, allow_redirect=True)
        with response:
            text = _read_response(response, 8192)
    except urllib.error.HTTPError as error:
        try:
            text = _decode_body(error.read(8192), error.headers)
        except Exception:
            text = ""
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        return False, "淮南 Dr.COM 登录请求失败: %s" % error
    ok, message = _classify_login(text)
    if ok:
        return ok, message
    # 旧接口常返回 HTML/脚本中的明确中文提示，补充成功重定向判断。
    lowered = text.lower()
    if "success" in lowered or "online" in lowered or "already" in lowered:
        return True, "淮南 Dr.COM 登录成功"
    return False, message


def drcom_login(config: Config) -> Tuple[bool, str]:
    """按当前 AUST 页面 login_method=1 调用 ePortal 登录。"""
    if config.campus == "huainan" or config.portal_ip == HUAINAN_IP:
        return _huainan_login(config)
    ip, mac = get_local_identity(config)
    # 先访问与浏览器相同的 a79 入口，令网关建立/刷新本次认证上下文。
    # 该请求不携带账号密码，只提交当前终端网络标识。
    display_mac = "%s-%s-%s" % (mac[:4], mac[4:8], mac[8:12])
    entry_query = urllib.parse.urlencode(
        {
            "wlanuserip": ip,
            "wlanacname": config.wlan_ac_name,
            "wlanusermac": display_mac,
            "wlanacip": config.wlan_ac_ip,
        }
    )
    entry_url = "http://%s/a79.htm?%s" % (
        _authority(config.portal_ip, config.portal_port),
        entry_query,
    )
    try:
        with http_get(
            entry_url,
            timeout=8,
            allow_redirect=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": entry_url,
            },
        ) as response:
            _read_response(response, 4096)
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, OSError) as error:
        return False, "无法打开认证入口 %s: %s" % (entry_url, error)

    return _eportal_login(config)


def _verify_online(config: Config, attempts: int = 3) -> bool:
    for index in range(attempts):
        if index:
            time.sleep(index)
        if probe_network(config) == "online":
            return True
    return False


def run_once(config: Config) -> Tuple[bool, str]:
    state = probe_network(config)
    if state == "online":
        return True, "online"

    log("检测到需要认证（%s），正在自动登录..." % state)
    ok, message = drcom_login(config)
    if ok:
        log("OK - " + message)
        if _verify_online(config):
            log("联网复核通过")
            return True, "online"
        log("WARN - 认证接口成功，但联网复核仍未通过")
        return True, "ok"

    # 探测可能被临时网络波动影响；若此时外网已恢复，不把它报告成失败。
    if probe_network(config) == "online":
        log("WARN - 登录响应异常，但联网探测已通过: " + message)
        return True, "online"
    log("FAIL - " + message)
    return False, "fail"


def run_selftest(config: Config) -> bool:
    ip, mac = get_local_identity(config)
    credentials = "已设置" if config.username and config.password else "未完整设置"
    log("SELFTEST: 本机IP=%s MAC=%s 账号=%s" % (
        ip,
        mac,
        config.legacy_account if config.username else "<未设置>",
    ))
    log("SELFTEST: 校区=%s，连接=%s，凭据%s，认证入口=%s:%d，ePortal=%s:%d，接入名称=%s" % (
        config.campus,
        config.connection,
        credentials,
        config.portal_ip,
        config.portal_port,
        config.portal_ip,
        config.eportal_port,
        config.wlan_ac_name or "<空>",
    ))
    portal_state, portal_error = query_drcom_status(config)
    if portal_state is None:
        if config.campus == "huainan":
            try:
                with http_get("http://%s/" % _authority(config.portal_ip, config.portal_port), timeout=5) as response:
                    _read_response(response, 4096)
                log("SELFTEST: 淮南认证首页可访问；状态接口不可用不影响登录")
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, OSError):
                log("SELFTEST: 无法访问淮南认证服务器: %s" % portal_error)
                return False
        else:
            log("SELFTEST: 无法访问 Dr.COM 状态接口: %s" % portal_error)
            log("SELFTEST: 请确认已连接校园 Wi-Fi，并检查 http://%s:%d/a79.htm 能否打开" % (
                config.portal_ip,
                config.portal_port,
            ))
            return False
    else:
        log("SELFTEST: Dr.COM 状态接口正常，当前状态=%s" % portal_state)
    if config.username and config.password:
        log("SELFTEST: 配置检查通过")
        return True
    log("SELFTEST: 请先设置 AUST_USERNAME 和 AUST_PASSWORD")
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安徽理工大学 Dr.COM 自动登录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="检测并登录一次后退出")
    mode.add_argument("--daemon", action="store_true", help="持续检测并自动重连（默认）")
    mode.add_argument("--selftest", action="store_true", help="只检查配置和本机网卡信息")
    parser.add_argument("--campus", choices=("auto", "hefei", "huainan"), help="选择校区")
    parser.add_argument("--connection", choices=("wifi", "wired"), help="淮南校区连接类型")
    parser.add_argument("--interval", type=int, help="覆盖守护模式检测间隔（秒）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.campus:
        os.environ["AUST_CAMPUS"] = args.campus
    if args.connection:
        os.environ["AUST_CONNECTION"] = args.connection
    try:
        config = load_config(require_credentials=not args.selftest)
    except ConfigError as error:
        log("配置错误: " + str(error))
        return 2

    if args.selftest:
        return 0 if run_selftest(config) else 2
    if args.once:
        ok, _ = run_once(config)
        return 0 if ok else 1

    interval = args.interval if args.interval is not None else config.check_interval
    if interval < 5:
        log("配置错误: --interval 必须大于等于 5")
        return 2
    log("守护模式启动：每 %d 秒检测一次，Ctrl+C 停止" % interval)
    try:
        while True:
            try:
                _, status = run_once(config)
                time.sleep(30 if status == "fail" else interval)
            except Exception as error:
                log("运行异常（稍后继续）: %s" % error)
                time.sleep(30)
    except KeyboardInterrupt:
        log("守护模式已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
