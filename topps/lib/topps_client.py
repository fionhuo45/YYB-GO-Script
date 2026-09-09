#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TOPPS 微信小程序 API 独立客户端

用法：
    1. 先运行一次 `python topps_client.py extract [relay.jsonl]` 从抓包文件提取登录态，
       或手动编辑 session.json 填入 token / MALLSESSION。
    2. 调用 user_info()、goods_list()、goods_info()、create_order() 等接口。

签名规则（来自反编译源码 utils/request.js）：
    1. 合并业务参数 + timestamp + v
    2. 按 key 字典序排序
    3. 对象值用 JSON.stringify，其余转字符串
    4. key=value&key2=value2... + token
    5. MD5，转大写

注意：目标 CDN/WAF 会拦截标准 requests / curl 的 TLS 指纹，本客户端使用 curl_cffi
      模拟 Chrome 116 指纹，从而可以脱离小程序与 MITM 代理直接访问接口。
"""

import base64
import json
import hashlib
import time
import random
from pathlib import Path
from typing import Dict, Any, Optional, List

try:
    from curl_cffi import requests as _requests
except ImportError as _e:
    raise ImportError("需要安装 curl_cffi: pip install curl_cffi") from _e

try:
    from Crypto.Cipher import AES
except ImportError as _e:
    raise ImportError("需要安装 pycryptodome: pip install pycryptodome") from _e

BASE_URL = "https://topps-mp.xcxd-inc.com"
APP_ID = "wx7f5b9b4a432faaf0"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; Pixel 8 Build/CP1A.260505.005; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 "
    "XWEB/1160289 MMWEBSDK/20260201 MMWEBID/2373 "
    "MicroMessenger/8.0.69.3022(0x28004542) WeChat/arm64 Weixin GPVersion/1 "
    "NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"
)
REFERER = f"https://servicewechat.com/{APP_ID}/18/page-frame.html"

ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = ROOT / "session.json"


def _sign(params: Dict[str, Any], token: str) -> str:
    items = []
    for key in sorted(params.keys()):
        value = params[key]
        if value is None:
            value = ""
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            value = str(value)
        items.append(f"{key}={value}")
    return hashlib.md5(("&".join(items) + token).encode("utf-8")).hexdigest().upper()


def _wrap_vary_key_for_request(
    vary_key: str,
    encrypt_key: str,
    iv: str,
    version: int,
) -> str:
    """复现 v12 ``wx.getUserCryptoManager`` 对 vary_key 的 AES-CBC 包装。"""
    key_bytes = base64.b64decode(encrypt_key)
    iv_bytes = iv.encode("utf-8")
    if len(key_bytes) not in {16, 24, 32} or len(iv_bytes) != 16:
        raise ValueError("微信用户加密密钥或 IV 长度无效")
    plain = vary_key.encode("utf-8")
    padding = 16 - len(plain) % 16
    ciphertext = AES.new(key_bytes, AES.MODE_CBC, iv_bytes).encrypt(
        plain + bytes([padding]) * padding
    )
    return f"{int(version)}.{base64.b64encode(ciphertext).decode('ascii')}"


def _generate_device_token() -> str:
    """生成与小程序一致的 device_token（dt_ + MD5 32 位小写）。"""
    # 与反编译源码 utils/device.js 一致：seed = 时间戳 | 随机串 | 随机串 | 随机串 | 品牌 | 型号 | 系统 | 平台 | 像素比
    info = {
        "brand": "google",
        "model": "Pixel 8",
        "system": "Android 16",
        "platform": "android",
        "pixelRatio": "3.0",
    }
    seed = "|".join([
        str(int(time.time() * 1000)),
        random.randbytes(8).hex()[:8],
        random.randbytes(8).hex()[:8],
        random.randbytes(8).hex()[:8],
        info["brand"], info["model"], info["system"], info["platform"], info["pixelRatio"],
    ])
    return "dt_" + hashlib.md5(seed.encode("utf-8")).hexdigest()


def _generate_pre_order_id() -> str:
    return "PO_" + "".join(random.choices("0123456789ABCDEF", k=16))


def _solve_slider_captcha(session, bg_url: str, drag_url: str) -> int:
    """从 challengeCreate 返回的滑块验证码中识别缺口左边缘 x 坐标

    策略：用拖拽图 alpha 通道提取真实缺口形状，在背景图中寻找形状+面积最匹配的亮色区域。
    """
    import cv2
    import numpy as np
    from pathlib import Path

    bg_data = session.get(BASE_URL + bg_url).content
    drag_data = session.get(BASE_URL + drag_url).content

    # 保存最新验证码图片便于调试
    debug_dir = ROOT
    with open(debug_dir / "captcha_bg_latest.png", "wb") as f:
        f.write(bg_data)
    with open(debug_dir / "captcha_drag_latest.png", "wb") as f:
        f.write(drag_data)

    bg = cv2.imdecode(np.frombuffer(bg_data, np.uint8), cv2.IMREAD_COLOR)
    drag = cv2.imdecode(np.frombuffer(drag_data, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None:
        raise ValueError("无法解码验证码背景图")
    if drag is None or drag.shape[2] != 4:
        raise ValueError("无法解码验证码拖拽图或缺少 alpha 通道")

    # 拖拽图轮廓 = 真实缺口形状
    alpha = drag[:, :, 3]
    _, dmask = cv2.threshold(alpha, 127, 255, cv2.THRESH_BINARY)
    dcnt = max(cv2.findContours(dmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0], key=cv2.contourArea)
    darea = cv2.contourArea(dcnt)

    gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
    best = None
    for thr in (160, 170, 180, 190, 200, 210, 220, 230, 240, 250):
        _, thresh = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 400 or area > 8000:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            ratio = area / max(darea, 1)
            if not (0.3 < ratio < 3.0):
                continue
            score = cv2.matchShapes(dcnt, cnt, cv2.CONTOURS_MATCH_I1, 0.0)
            cand = (score, abs(ratio - 1.0), area, x, y, w, h, thr)
            if best is None or cand < best:
                best = cand

    if best is None:
        raise ValueError("未识别到滑块缺口")

    _, _, _, x, y, w, h, thr = best
    dbg = bg.copy()
    cv2.rectangle(dbg, (x, y), (x + w, y + h), (0, 0, 255), 2)
    cv2.imwrite(str(debug_dir / "captcha_debug_latest.png"), dbg)
    return x


class ToppsClient:
    def __init__(self, session_path: Path = SESSION_FILE):
        self.session_path = session_path
        self.session = self._load_session()
        # curl_cffi 模拟 Chrome 116 指纹，绕过 CDN/WAF 对原生 TLS 指纹的拦截
        self.s = _requests.Session(impersonate="chrome116")
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "charset": "utf-8",
            "model": "Pixel 8",
            "version": "8.0.69",
            "platform": "android",
        })
        self._set_cookies()

    def _set_cookies(self):
        if self.session.get("mall_session"):
            self.s.cookies.set("MALLSESSION", self.session["mall_session"], domain="topps-mp.xcxd-inc.com")
        if self.session.get("acw_tc"):
            self.s.cookies.set("acw_tc", self.session["acw_tc"], domain="topps-mp.xcxd-inc.com")

    def _load_session(self) -> Dict[str, Any]:
        if not self.session_path.exists():
            return {}
        with open(self.session_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_session(self):
        with open(self.session_path, "w", encoding="utf-8") as f:
            json.dump(self.session, f, ensure_ascii=False, indent=2)

    def _token(self) -> str:
        return self.session.get("token", "")

    def device_token(self) -> str:
        """返回持久化的弱设备标识，确保 info/create 使用同一值。"""
        token = self.session.get("device_token", "")
        if not token:
            token = _generate_device_token()
            self.session["device_token"] = token
            self.save_session()
        return token

    def set_crypto_material(
        self,
        encrypt_key: str,
        iv: str,
        version: int,
        expires_at: int,
    ) -> None:
        """保存 v12 创建订单所需的微信用户加密材料。"""
        self.session.update({
            "crypto_key": encrypt_key,
            "crypto_iv": iv,
            "crypto_version": int(version),
            "crypto_expires_at": int(expires_at),
        })
        self.save_session()

    def _build_payload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(params)
        payload["timestamp"] = str(int(time.time()))
        payload["v"] = "1.0"
        payload["sign"] = _sign(payload, self._token())
        return payload

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: bool = True,
        raw: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = BASE_URL + path
        request_headers = {"Authorization": self._token()}
        request_headers.update(headers or {})
        if json_body:
            payload = self._build_payload(params or {})
            resp = self.s.request(method, url, json=payload, headers=request_headers)
        else:
            resp = self.s.request(method, url, headers=request_headers)
        if raw:
            return resp
        try:
            return resp.json()
        except Exception:
            return {"_status_code": resp.status_code, "_text": resp.text[:500]}

    # ===================== 登录态 =====================

    def login_with_wechat_code(self, code: str) -> Dict[str, Any]:
        """使用 YYB-Go 返回的一次性 ``wx.login`` code 换取 Topps 登录态。"""
        if not code or not code.strip():
            raise ValueError("微信登录 code 不能为空")

        previous = dict(self.session)
        refresh_token = previous.get("refresh_token", "")
        if refresh_token:
            try:
                refresh_payload = refresh_token.split(".")[1]
                refresh_payload += "=" * (-len(refresh_payload) % 4)
                refresh_expires_at = int(
                    json.loads(base64.urlsafe_b64decode(refresh_payload)).get("exp") or 0
                )
                if refresh_expires_at and refresh_expires_at <= int(time.time()):
                    refresh_token = ""
            except (IndexError, ValueError, TypeError, json.JSONDecodeError):
                pass
        response = self.s.post(
            BASE_URL + "/interface/account/getWechatResByCode",
            json={
                "code": code.strip(),
                "refresh_token": refresh_token,
            },
            headers={"Authorization": ""},
        )
        try:
            result = response.json()
        except Exception as exc:
            raise RuntimeError("Topps 登录响应不是有效 JSON") from exc

        raw_data = result.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        token = data.get("token")
        if result.get("code") != 1 or not token:
            raise RuntimeError(result.get("msg") or "Topps 登录失败")

        def response_cookie(name: str) -> str:
            value = response.cookies.get(name)
            if value:
                return value
            value = self.s.cookies.get(name)
            return value or ""

        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            token_payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            expires_at = token_payload.get("exp")
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = None

        session = {
            "token": token,
            # v12 小程序会把最新 token 同时保存为下一次登录的 refresh_token。
            "refresh_token": token,
            "mall_session": response_cookie("MALLSESSION")
            or previous.get("mall_session", ""),
            "acw_tc": response_cookie("acw_tc") or previous.get("acw_tc", ""),
            "openid": data.get("openid"),
            "unionid": data.get("unionid"),
            "mobile": data.get("mobile") or data.get("phoneNumber"),
            "user_id": data.get("user_id"),
            "level": data.get("user_level", data.get("level", 0)),
            "expires_at": expires_at,
            "user_agent": previous.get("user_agent", USER_AGENT),
        }
        for key in (
            "device_token",
            "crypto_key",
            "crypto_iv",
            "crypto_version",
            "crypto_expires_at",
        ):
            if previous.get(key) is not None:
                session[key] = previous[key]

        self.session = session
        self.save_session()
        self._set_cookies()
        return result

    def login_with_phone_code(self, code: str) -> Dict[str, Any]:
        """使用微信手机号一次性 code 将 OpenID 会话升级为完整商城会话。"""
        if not code or not code.strip():
            raise ValueError("微信手机号 code 不能为空")

        result = self.get_mobile(code.strip(), mtype="login")
        raw_data = result.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        token = data.get("token")
        if result.get("code") != 1 or not token:
            pending_cancel = (
                int(data.get("pending_cancel_apply") or 0) == 1
                or int(data.get("code") or 0) == 20010
            )
            if pending_cancel:
                raise RuntimeError(
                    "账号存在待审核注销申请，请先在 TOPPS 小程序选择“继续登录”撤销申请"
                )
            raise RuntimeError(result.get("msg") or "Topps 手机号登录失败")

        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            token_payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            expires_at = token_payload.get("exp")
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = None

        updated = dict(self.session)
        updated.update({
            "token": token,
            "refresh_token": token,
            "mobile": data.get("mobile") or data.get("phoneNumber"),
            "user_id": data.get("user_id"),
            "level": data.get("user_level", data.get("level", 0)),
            "is_register": data.get("is_register"),
            "expires_at": expires_at,
        })
        self.session = updated
        self.save_session()
        return result

    def set_session(self, token: str, mall_session: str, **extra):
        self.session = {"token": token, "mall_session": mall_session, **extra}
        self.save_session()
        self._set_cookies()

    def refresh_session_from_relay(self, relay_path: Optional[Path] = None):
        """从抓包文件里提取最新 token / MALLSESSION / acw_tc"""
        relay_path = relay_path or (self.session_path.parent / "relay.jsonl")
        previous_device_token = self.session.get("device_token")
        session = extract_session_from_relay(relay_path, out_path=None)
        if previous_device_token:
            session["device_token"] = previous_device_token
        self.session = session
        self.save_session()
        self._set_cookies()
        return session

    def is_token_expired(self, grace_seconds: int = 60) -> bool:
        exp = self.session.get("expires_at")
        return not exp or exp < time.time() + grace_seconds

    # ===================== 用户信息 =====================

    def user_info(self) -> Dict[str, Any]:
        """获取当前登录用户信息（依赖未过期 JWT）"""
        return self._request("POST", "/interface/user/userQuery")

    def sign_verification(self) -> Dict[str, Any]:
        """签名验证式用户信息（对 JWT 要求较宽松，可作为 user_info 备用）"""
        return self._request("POST", "/interface/user/SignVerification")

    def check_member_by_openid(self) -> Dict[str, Any]:
        """按 v12 登录流程查询当前 OpenID 的会员等级。"""
        return self._request("POST", "/interface/account/checkMemberByOpenid")

    def mine_info(self) -> Dict[str, Any]:
        """我的页面聚合信息"""
        return self._request("POST", "/interface/mine/mineInfo")

    # ===================== 会员 / 入会 =====================

    DEFAULT_AVATAR = "https://topps-static.xcxd-inc.com/images/mp_image/me/default_avatar.png"

    def get_mobile(self, code: str, mtype: str = "login") -> Dict[str, Any]:
        """手机号快捷登录 / 判断注册状态

        `code` 来自微信 getPhoneNumber（一次性 code）。响应含:
        phoneNumber / token / user_id / user_level / is_register。
        登录后若 user_level=0 表示尚未入会，应调用 user_register() 完成入会。
        """
        return self._request("POST", "/interface/account/getMobile", {
            "code": code,
            "mtype": mtype,
        })

    def member_status(self) -> Dict[str, Any]:
        """会员状态（判断是否会员）

        基于 userQuery 返回解析:
        - is_register: 是否已注册（绑定手机号）
        - user_level / level: 会员等级，0 = 非会员，>= 1 = 会员
        - is_member: 是否会员（user_level / level >= 1）
        """
        data = self.user_info().get("data") or {}
        level = int(data.get("user_level") or data.get("level") or 0)
        return {
            "is_member": level > 0,
            "is_register": bool(data.get("is_register")),
            "user_level": data.get("user_level", data.get("level", 0)),
            "level": data.get("level", 0),
            "raw": data,
        }

    def is_member(self) -> bool:
        """是否会员（user_level / level >= 1）"""
        return self.member_status()["is_member"]

    def user_register(
        self,
        nickname: str,
        mobile: str,
        avatar: str = DEFAULT_AVATAR,
        username: Optional[str] = None,
        sex: int = -1,
        birthday: str = "",
        province: str = "",
        city: str = "",
        area: str = "",
        invite_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """入会：提交会员资料完成注册

        与小程序注册页一致：avatar / nickname / username / mobile /
        sex（-1 表示未选择）/ birthday（YYYY-MM-DD）。地区仅在有值时携带，
        可选邀请人 invite_user_id。成功返回 data.level（>= 1 即为会员）。
        """
        params: Dict[str, Any] = {
            "avatar": avatar or self.DEFAULT_AVATAR,
            "nickname": nickname,
            "username": username or nickname,
            "mobile": mobile,
            "sex": sex,
            "birthday": birthday,
        }
        if province:
            params["province"] = province
        if city:
            params["city"] = city
        if area:
            params["area"] = area
        if invite_user_id is not None:
            params["invite_user_id"] = invite_user_id
        return self._request("POST", "/interface/user/userRegister", params)

    def get_member_coupon(self, get_type: str = "商城首页") -> Dict[str, Any]:
        """入会领券：领取新会员专享券（get_type 如 '商城首页'）"""
        return self._request("POST", "/interface/coupon/getMemberCoupon", {"get_type": get_type})

    def address_list(self) -> List[Dict[str, Any]]:
        """收货地址列表"""
        data = self._request("POST", "/interface/user_address/getAddressList")
        return data.get("data", []) if data.get("code") == 1 else []

    # ===================== 商品相关 =====================

    def home_index(self, scene: str = "1089") -> Dict[str, Any]:
        """首页数据（含正在发售、精选热卖等商品卡片）"""
        return self._request("POST", "/interface/index/indexData", {"scene": scene})

    def goods_list(
        self,
        page: int = 1,
        page_size: int = 10,
        keyword: str = "",
        category_id: int = 0,
        series_id: int = 0,
        sort: str = "",
        order: str = "",
        min_price: str = "",
        max_price: str = "",
        in_stock: int = 0,
        scene: str = "1089",
    ) -> Dict[str, Any]:
        """商品列表 / 搜索"""
        params = {"page": page, "page_size": page_size, "scene": scene}
        if keyword:
            params["keyword"] = keyword
        if category_id:
            params["category_id"] = category_id
        if series_id:
            params["series_id"] = series_id
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        if min_price:
            params["min_price"] = min_price
        if max_price:
            params["max_price"] = max_price
        if in_stock:
            params["in_stock"] = in_stock
        return self._request("POST", "/interface/Goods/getGoodsList", params)

    def goods_info(self, goods_id: int, item_id: int, scene: str = "1089") -> Dict[str, Any]:
        """商品详情（含 SKU、库存、价格）"""
        return self._request("POST", "/interface/goods/getGoodsInfo", {
            "goods_id": str(goods_id),
            "item_id": str(item_id),
            "show_item_id": "",
            "scene": scene,
        })

    def cart_list(self) -> Dict[str, Any]:
        """购物车列表"""
        return self._request("POST", "/interface/cart/getCartList")

    def cart_num(self) -> int:
        """购物车商品数量"""
        data = self._request("POST", "/interface/cart/getCartNum")
        return data.get("data", {}).get("cart_num", 0) if data.get("code") == 1 else 0

    def cart_add(self, item_id: int, num: int = 1, goods_id: int = 0) -> Dict[str, Any]:
        """加入购物车"""
        params = {"item_id": item_id, "num": num}
        if goods_id:
            params["goods_id"] = goods_id
        return self._request("POST", "/interface/cart/cartAdd", params)

    # ===================== 订单相关 =====================

    def order_buy_info(
        self,
        item_id: int,
        num: int = 1,
        address_id: int = 0,
        coupon_id: Optional[int] = None,
        device_token: str = "",
        pre_order_id: str = "",
        scene: str = "1089",
    ) -> Dict[str, Any]:
        """下单页信息（action=info）"""
        params = {
            "type": "",
            "action": "info",
            "coupon_id": [coupon_id] if coupon_id else [],
            "goods": [{"item_id": item_id, "num": num}],
            "address_id": address_id,
            "use_integral": 0,
            "use_coupon": 1,
            "gdt_vid": 0,
            "device_token": device_token or self.device_token(),
            "preorder_token": "",
            "risk_token": "",
            "pre_order_id": pre_order_id or _generate_pre_order_id(),
            "source_type": "商品详情页",
            "utm_source": "share",
            "utm_medium": "organic",
            "utm_term": time.strftime("%Y%m%d"),
            "utm_content": "Friends",
            "utm_campaign": "",
            "scene": scene,
        }
        return self._request("POST", "/interface/order/orderBuy", params)

    def create_order(
        self,
        item_id: int,
        num: int = 1,
        address_id: int = 0,
        coupon_id: Optional[int] = None,
        device_token: str = "",
        preorder_token: str = "",
        pre_order_id: str = "",
        vary_key: str = "",
        scene: str = "1089",
    ) -> Dict[str, Any]:
        """创建订单（action=create），自动处理 NEED_RISK_TOKEN 滑块验证"""
        device_token = device_token or self.device_token()
        params = {
            "type": "",
            "action": "create",
            "coupon_id": [coupon_id] if coupon_id else [],
            "goods": [{"item_id": item_id, "num": num}],
            "address_id": address_id,
            "use_integral": 0,
            "use_coupon": 1,
            "gdt_vid": 0,
            "device_token": device_token,
            "preorder_token": preorder_token,
            "risk_token": "",
            "pre_order_id": pre_order_id or _generate_pre_order_id(),
            "source_type": "商品详情页",
            "utm_source": "share",
            "utm_medium": "organic",
            "utm_term": time.strftime("%Y%m%d"),
            "utm_content": "Friends",
            "utm_campaign": "",
            "scene": scene,
        }
        extra_headers = None
        if vary_key:
            digest = _wrap_vary_key_for_request(
                vary_key,
                self.session.get("crypto_key", ""),
                self.session.get("crypto_iv", ""),
                int(self.session.get("crypto_version") or 0),
            )
            extra_headers = {"X-Content-Digest": digest}

        resp = self._request(
            "POST",
            "/interface/order/orderBuy",
            params,
            headers=extra_headers,
        )
        if resp.get("data", {}).get("code") != "NEED_RISK_TOKEN":
            return resp

        # 需要滑块验证：申请 challenge -> 识别缺口 -> 提交轨迹 -> 获取 risk_token -> 重试下单
        challenge = self._request("POST", "/interface/risk/challengeCreate", {
            "device_token": device_token,
            "preorder_token": preorder_token,
            "goods": params["goods"],
        })
        if challenge.get("code") != 1 or not challenge.get("data", {}).get("challenge_token"):
            return {**resp, "_risk_step": "challengeCreate", "_challenge_resp": challenge}

        cdata = challenge["data"]
        try:
            target_x = _solve_slider_captcha(self.s, cdata["captcha_bg_url"], cdata["drag_icon_url"])
        except Exception as e:
            return {**resp, "_risk_step": "solve_captcha", "_error": str(e)}

        verify = self._request("POST", "/interface/risk/challengeVerify", {
            "device_token": device_token,
            "preorder_token": preorder_token,
            "goods": params["goods"],
            "challenge_token": cdata["challenge_token"],
            "trace_payload": _build_trace_payload(target_x, cdata.get("track_max", 300)),
        })
        if verify.get("code") != 1 or not verify.get("data", {}).get("risk_token"):
            return {**resp, "_risk_step": "challengeVerify", "_verify_resp": verify}

        params["risk_token"] = verify["data"]["risk_token"]
        return self._request(
            "POST",
            "/interface/order/orderBuy",
            params,
            headers=extra_headers,
        )

    def order_list(self, page: int = 1, page_size: int = 10, status: str = "") -> Dict[str, Any]:
        """订单列表"""
        params = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return self._request("POST", "/interface/order/getOrderList", params)


def extract_session_from_relay(relay_path: Path, out_path: Path = SESSION_FILE):
    """从 mitm-proxy 抓包文件里提取最新 token、MALLSESSION、acw_tc"""
    token = None
    mall_session = None
    acw_tc = None
    with open(relay_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") == "request":
                auth = rec.get("headers", {}).get("authorization", "")
                if auth and auth.startswith("eyJ"):
                    token = auth
            elif rec.get("event") == "response":
                for c in rec.get("headers", {}).get("set-cookie", []):
                    if "MALLSESSION" in c:
                        mall_session = c.split(";")[0].split("=")[-1]
                    if c.startswith("acw_tc="):
                        acw_tc = c.split(";")[0].split("=")[-1]
    if not token or not mall_session:
        raise ValueError("未能从抓包文件提取到 token 或 MALLSESSION")
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    data = payload.get("data", {})
    session = {
        "token": token,
        "mall_session": mall_session,
        "acw_tc": acw_tc,
        "openid": data.get("openid"),
        "unionid": data.get("unionid"),
        "mobile": data.get("mobile"),
        "user_id": data.get("user_id"),
        "user_agent": data.get("user_agent", USER_AGENT),
        "expires_at": payload.get("exp"),
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        print(f"已保存登录态到 {out_path}")
        print(f"  openid: {session['openid']}")
        print(f"  mobile: {session['mobile']}")
        print(f"  expires_at: {session['expires_at']}")
    return session


def _pick_demo_sku(home: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从首页数据里挑一个带 SKU 的商品，用于演示"""
    data = home.get("data", {})
    if isinstance(data, dict):
        modules = data.get("recommend_list", []) or data.get("module_list", [])
    else:
        modules = []
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        for item in mod.get("list", []):
            if isinstance(item, dict) and item.get("item_id") and item.get("goods_id"):
                return item
    return None


def _run_demo(client: ToppsClient, create_order: bool = False):
    print("=== 1. 用户信息（JWT 模式）===")
    user = client.user_info()
    print(json.dumps(user, ensure_ascii=False, indent=2)[:2000])

    if user.get("code") == 2000:
        print("\n=== 1b. 用户信息（SignVerification 备用模式）===")
        sv = client.sign_verification()
        print(json.dumps(sv, ensure_ascii=False, indent=2)[:2000])

    print("\n=== 2. 首页 / 商品列表 ===")
    home = client.home_index()
    sku = _pick_demo_sku(home)
    if sku:
        print(f"选中商品: goods_id={sku.get('goods_id')} item_id={sku.get('item_id')} name={sku.get('name', '')[:30]}")
    elif home.get("code") == 2000 or home.get("code") == 1000:
        print("登录态异常，跳过商品详情与下单")
        return
    else:
        print("未从首页找到商品，尝试 goods_list")
        goods = client.goods_list(page=1, page_size=5)
        print(json.dumps(goods, ensure_ascii=False, indent=2)[:1500])
        items = goods.get("data", {}).get("list", []) if isinstance(goods.get("data"), dict) else []
        sku = items[0] if items else None

    if not (sku and isinstance(sku, dict) and sku.get("goods_id") and sku.get("item_id")):
        print("未能获取演示商品，跳过详情与下单")
        return

    print("\n=== 3. 商品详情 ===")
    info = client.goods_info(sku["goods_id"], sku["item_id"])
    print(json.dumps(info, ensure_ascii=False, indent=2)[:2000])

    print("\n=== 4. 收货地址 ===")
    addresses = client.address_list()
    address_id = addresses[0]["id"] if addresses else 0
    print(f"地址数量: {len(addresses)}  默认/首选地址ID: {address_id}")
    if not address_id:
        print("没有可用地址，跳过后续下单")
        return

    print("\n=== 5. 下单页信息 ===")
    device_token = client.device_token()
    pre_order_id = _generate_pre_order_id()
    buy_info = client.order_buy_info(
        sku["item_id"], num=1, address_id=address_id,
        device_token=device_token, pre_order_id=pre_order_id
    )
    print(json.dumps(buy_info, ensure_ascii=False, indent=2)[:2000])

    if buy_info.get("code") != 1:
        print("下单页信息获取失败，跳过创建订单")
        return

    preorder_token = buy_info.get("data", {}).get("preorder_token", "")
    coupon_id_list = buy_info.get("data", {}).get("coupon_id", [])
    coupon_id = coupon_id_list[0] if coupon_id_list else None
    if not preorder_token:
        print("下单页未返回 preorder_token，跳过创建订单")
        return

    if not create_order:
        print("\n=== 6. 创建订单 ===")
        print("默认只读模式，未创建订单；传入 --create-order 才会提交真实订单")
        return

    print("\n=== 6. 创建订单（自动处理滑块验证）===")
    create = client.create_order(
        sku["item_id"], num=1, address_id=address_id,
        device_token=device_token, preorder_token=preorder_token, pre_order_id=pre_order_id,
        coupon_id=coupon_id
    )
    print(json.dumps(create, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "extract":
        relay = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "relay.jsonl"
        extract_session_from_relay(relay)
        sys.exit(0)

    client = ToppsClient()
    if client.is_token_expired():
        print("[WARN] session.json 中的 token 已过期，尝试从 relay.jsonl 拉取最新登录态...")
        try:
            client.refresh_session_from_relay()
        except Exception as e:
            print(f"[WARN] 未能从 relay.jsonl 刷新: {e}")
    _run_demo(client, create_order="--create-order" in sys.argv[1:])
