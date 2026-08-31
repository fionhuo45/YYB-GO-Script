# YONEX 微信小程序接入

本目录实现 YONEX 小程序（AppID `wx1656f93aeb347dbc`，v29）的完整业务链：

- YYB-Go → 青龙定时刷新会员登录态；
- 商品名称搜索、批量商品加载、详情与 SKU 选择；
- 加购物车、结算校验、地址与金额预览；
- 需要三重显式确认的“创建待支付订单”；
- 独立的支付参数读取方法，不包含实际支付调用；
- 带 Bearer 鉴权的登录态 sidecar。

详细部署与命令见 [`docs/yonex-qinglong.md`](docs/yonex-qinglong.md)。

快速验证：

```bash
python -m unittest discover -s tests -v
python -m py_compile yonex_client.py yonex_ql_login.py \
  scripts/python/yonex_order.py scripts/python/yonex_session_api.py
python scripts/python/yonex_order.py --help
```
