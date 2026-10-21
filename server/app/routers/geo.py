"""``GET /api/geo/timezone`` —— 让客户端知道「该按哪个时区显示时间」。

为什么单独一个端点、而不是把时区塞进登录响应
--------------------------------------------
1. **登录页也要用**：后台的日志、用户端的"最近调用"都可能在看登录之前的
   内容（比如登录失败提示里的时间），等登录才给时区就晚了；
2. 手机 App 的时区可能在**会话期间变化**（出国、换 WiFi、切蜂窝网），
   而 token 是 30 天的 —— 塞在登录响应里就再也不会更新；
3. 它是**纯读**、无副作用、不需要任何凭证，做成独立端点后可以在启动时
   并发拉取，不拖慢登录。

⚠️ 这个端点**不需要登录**（``no auth``）。这不是疏忽：
它的输出只有一个时区名 + 国家码 + 偏移，不含任何用户数据；反过来，
如果要求登录，登录页与"还没登录的首屏"就只能吃兜底值。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from .. import geo

router = APIRouter(prefix="/api/geo", tags=["geo"])

@router.get("/timezone")
async def timezone(request: Request):
    """按客户端 IP 判定展示时区。

    入参（都可缺）：
      * ``X-Client-Timezone`` —— 设备自报的 IANA 名。**只作兜底/细化**，
        不会覆盖 IP 判定的国家（理由见 :mod:`app.geo` 的模块注释）；
      * ``?verbose=1`` —— 附带逐层尝试记录，排障用。

    出参::

        {"timezone": "Asia/Shanghai", "offset_minutes": 480,
         "country": "CN", "source": "api", "ip": "1.2.3.4"}

    ``source`` 的取值与含义：``proxy`` / ``geoip`` / ``api`` / ``proxy+device``
    / ``geoip+device`` / ``api+device`` / ``device`` / ``default``。
    **前端必须把它记进日志**（不要只在界面上静默使用）——
    这个链路一旦整体退化成 ``default``，表现就是"所有人都显示北京时间"，
    而那跟"配置完全正确"长得一模一样。
    """
    # client_ip 已经处理了「只信任可信反代的 XFF」；peer 与判定顺序都在
    # geo.resolve_request 里，这个端点只负责序列化 —— 两处各写一份判定链
    # 是这类特性最容易出的事（改一处漏一处，且两边都"看起来对"）。
    verbose = request.query_params.get("verbose", "").strip() in ("1", "true", "yes")
    guess = await run_in_threadpool(geo.resolve_request, request)
    return guess.as_dict(verbose=verbose)
