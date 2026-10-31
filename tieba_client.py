import hashlib
import logging
import random
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------- constants ----------
SIGN_KEY = "tiebaclient!!!"
TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
LIKE_URL = "https://c.tieba.baidu.com/c/f/forum/like"
SIGN_URL = "https://c.tieba.baidu.com/c/c/forum/sign"

# 2026年终极伪装：User-Agent 采用 Android 16 系统 + 2026年最新贴吧客户端 v22.13.1.1 
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16; SM-S948B Build/UP1A.251005.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/132.0.0.0 Mobile Safari/537.36 bdtb/22.13.1.1",
    "Content-Type": "application/x-www-form-urlencoded",
}

# 设备请求体数据同步对齐 2026 最新客户端与海外版旗舰硬件特征
BASE_SIGN_DATA = {
    "_client_type": "2",
    "_client_version": "22.13.1.1",     # 彻底抛弃过时的 9.x 或 12.x，直接用 2026 最新版号
    "_phone_imei": "351846209845127",   # 随机生成的国际版格式真机串号
    "model": "SM-S948B",               # 三星 Galaxy S26 Ultra 国际版官方型号
    "net_type": "1",
}


# ---------- client ----------
class TiebaClient:
    """
    百度贴吧签到客户端
    """

    def __init__(self, bduss: str) -> None:
        if not bduss:
            raise ValueError("BDUSS 不能为空")
        self.bduss = bduss
        self._session: Optional[requests.Session] = None

    # -- session 惰性初始化 --
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(HEADERS)
            # 将 BDUSS 注入 Cookie，否则服务端收不到认证信息
            requests.utils.add_dict_to_cookiejar(
                self._session.cookies, {"BDUSS": self.bduss}
            )
        return self._session

    # -- 签名算法 --
    @staticmethod
    def signature(data: dict) -> str:
        s = "".join(f"{k}={data[k]}" for k in sorted(data))
        return hashlib.md5((s + SIGN_KEY).encode()).hexdigest().upper()

    # -- 带指数退避的请求 --
    def _request(
        self,
        url: str,
        method: str = "get",
        data: Optional[dict] = None,
        retry: int = 3,
    ) -> Optional[dict]:
        for i in range(retry):
            try:
                if method.lower() == "get":
                    resp = self.session.get(url, timeout=10)
                else:
                    resp = self.session.post(url, data=data, timeout=10)

                resp.raise_for_status()
                if not resp.text.strip():
                    raise ValueError("空响应")
                return resp.json()
            except Exception as e:
                if i == retry - 1:
                    logger.error(f"请求失败(已重试 {retry} 次): {e}")
                    return None
                wait = 1.5 * (2**i) + random.uniform(0, 1)
                logger.warning(f"请求异常，{wait:.1f}s 后重试 ({i+1}/{retry}): {e}")
                time.sleep(wait)
        return None

    # -- 获取 tbs --
    def get_tbs(self) -> Optional[str]:
        """获取 tbs，BDUSS 有效性由后续签到请求自然验证。"""
        result = self._request(TBS_URL)
        if result is None:
            logger.error("获取 tbs 失败")
            return None
        return result.get("tbs", "")

    # -- 获取关注的贴吧列表 --
    def get_favorites(self) -> list[dict]:
        forums: list[dict] = []
        page_no = 1

        while True:
            # 只有临时会话 ID 里的毫秒级时间戳和随机尾数是动态的，这符合真人打开 App 的特征
            current_ms = str(int(time.time() * 1000))
            random_suffix = str(random.randint(100, 999))
            data = {
                "BDUSS": self.bduss,
                "_client_type": "2",
                "_client_id": f"wappc_{current_ms}_{random_suffix}",
                "_client_version": "22.13.1.1",
                "_phone_imei": "351846209845127",
                "from": "1013444a", # 三星渠道
                "page_no": str(page_no),
                "page_size": "200",
                "model": "SM-S948B", # 三星手机型号
                "net_type": "1",
                "timestamp": str(int(time.time())),# 每次请求的当下时间戳
                "vcode_tag": "11",
            }
            data["sign"] = self.signature(data)

            result = self._request(LIKE_URL, "post", data)
            if result is None:
                logger.warning("获取贴吧列表失败，停止翻页")
                break

            if "forum_list" in result:
                for forum_type in ("non-gconforum", "gconforum"):
                    items = result["forum_list"].get(forum_type, [])
                    if isinstance(items, list):
                        forums.extend(items)
                    elif isinstance(items, dict):
                        forums.append(items)

            if result.get("has_more") != "1":
                break

            page_no += 1
            time.sleep(random.uniform(1.5, 3))

        logger.info(f"共获取到 {len(forums)} 个关注的贴吧")
        return forums

    # -- 单个贴吧签到 --
    def sign_forum(self, fid: str, name: str, tbs: str) -> dict:
        """
        返回 {
            "status": "success" | "exist" | "shield" | "error",
            "rank": Optional[int],   -- 签到排名 (仅 success)
            "message": str,
        }
        """
        data = {**BASE_SIGN_DATA}
        data.update(
            {
                "BDUSS": self.bduss,
                "fid": fid,
                "kw": name,
                "tbs": tbs,
                "timestamp": str(int(time.time())),
            }
        )
        data["sign"] = self.signature(data)

        result = self._request(SIGN_URL, "post", data)
        if result is None:
            return {"status": "error", "rank": None, "message": "网络请求失败"}

        error_code = result.get("error_code", "")
        error_msg = result.get("error_msg", "")

        if error_code == "0":
            rank = None
            if "user_info" in result:
                rank = result["user_info"].get("user_sign_rank")
                rank = int(rank) if rank else None
            return {"status": "success", "rank": rank, "message": "签到成功"}
        elif error_code == "160002":
            return {"status": "exist", "rank": None, "message": error_msg or "今日已签到"}
        elif error_code == "340006":
            return {"status": "shield", "rank": None, "message": "贴吧已被屏蔽"}
        else:
            return {"status": "error", "rank": None, "message": error_msg or "未知错误"}
