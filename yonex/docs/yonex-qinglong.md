# YONEX 青龙登录、商品与下单接入

## 1. 已确认协议

真机抓包和 v29 小程序源码共同确认：

```text
AppID:   wx1656f93aeb347dbc
版本:    29
API:     https://yonexmall.surto.cn/api/
Referer: https://servicewechat.com/wx1656f93aeb347dbc/29/page-frame.html
```

登录顺序：

```text
YYB-Go /wxapp/getCode
  -> POST /api/sso/xcxLogin
  -> guest oid

YYB-Go /wxapp/getPhoneNumber
  -> encryptedData + iv（其 detail.code 不直接提交商城）
YYB-Go /wxapp/getCode
  -> 再取一个全新的 wx.login code
  -> code + encryptedData + iv
  -> POST /api/sso/getPhone
  -> member token

再次获取 wx.login code
  -> POST /api/sso/xcxLogin
  -> 完整会员资料 + oid + token
  -> GET /api/cart/list 验证登录态
```

商品和订单顺序：

```text
GET  /api/product/category/searchProduct?name=...&pageNum=...&pageSize=...
POST /api/product/category/getDetailList       body: [productId, ...]
GET  /api/product/category/getProductDetail    query: productId
POST /api/cart/add
GET  /api/cart/list
POST /api/order/checkOrder                     body: [cartItemId, ...]
GET  /api/member/address/list
POST /api/order/generateConfirmOrder            body: ["cartItemId", ...]
POST /api/order/generateOrder                   创建待支付订单后立即停止
GET  /api/pay/payOrder                          仅显式请求支付参数时调用
```

`items` 是购物车项 ID，不是商品 ID 或 SKU ID。v29 的订单请求体为：

```json
{
  "items": ["CART_ITEM_ID"],
  "memberReceiveAddressId": 123,
  "token": "SESSION_TOKEN"
}
```

小程序原版在 `generateOrder` 后会继续 `payOrder -> wx.requestPayment`。本实现把它们拆开，且没有 `wx.requestPayment` 等价实现。

## 2. 文件

```text
yonex_client.py                         API 客户端与本地 session
yonex_ql_login.py                       YYB-Go/青龙定时续期
scripts/python/yonex_order.py           搜索、商品、购物车、结算 CLI
scripts/python/yonex_session_api.py     登录态 sidecar
tests/                                  全离线测试
```

## 3. 青龙登录续期

本节中的 `/ql/data/scripts/yonex` 是当前 SSH/手工部署布局。通过 `ql repo`
订阅时，应替换为青龙生成的实际仓库子目录；登录任务命令仍须包含完整的
`yonex_ql_login.py` 路径。sidecar 同时用 `--renewal` 指向该实际路径。

安装依赖：

```bash
cd /ql/data/scripts/yonex
pip3 install -r requirements.txt
```

环境变量每行配置一个微信账号：

```text
YYB_SERVER=http://yyb-go:8000@微信账号ref#main
YONEX_SESSION_DIR=/ql/data/config/yonex-sessions
```

青龙任务：

```bash
python3 /ql/data/scripts/yonex/yonex_ql_login.py
```

必须在命令中保留 `yonex_ql_login.py` 的绝对路径，YYB-Go 才能识别并执行
账号失效暂停/扫码恢复门控。默认 session 目录已经是
`/ql/data/config/yonex-sessions`；`YONEX_SESSION_DIR` 仅在需要改变持久目录时设置。

可选过滤：

```text
YONEX_REFRESH_REF=微信账号ref
YONEX_REFRESH_TAG=main
```

每个账号保存为 `yonex-<安全名称>-<ref摘要>.json`，既保留可读前缀，又避免不同 ref 清洗后发生文件名碰撞。脚本会分别获取三个互不复用的 `wx.login` code：guest 登录一个、`getPhone` 一个、会员资料刷新一个。它先在同目录完成全部登录步骤和轻量验证，成功后再原子替换正式 session；一次性微信 code、`encryptedData` 和 `iv` 不会落盘或输出到日志。

## 4. 登录态 sidecar

```bash
export YONEX_API_TOKEN='change-me'
python3 scripts/python/yonex_session_api.py \
  --session-dir /ql/data/config/yonex-sessions \
  --renewal /ql/data/scripts/yonex/yonex_ql_login.py
```

默认只监听 `127.0.0.1:5802`；需要跨容器访问时显式传 `--host 0.0.0.0`，所有业务接口仍强制 Bearer 鉴权：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/yonex/accounts?tag=main` | 脱敏账号列表，不返回 token/oid 原值 |
| GET | `/yonex/session?ref=1` | 返回完整 session |
| GET | `/yonex/account?ID=1` | 兼容按账号 ID 获取 session |
| POST | `/yonex/refresh?ref=1` | 后台触发登录续期 |
| GET | `/docs`、`/openapi.json` | 接口说明 |

业务接口默认要求：

```text
Authorization: Bearer <YONEX_API_TOKEN>
```

与其他项目共用 `5800` 端口时，使用
[`../deploy/universal_session_api/`](../deploy/universal_session_api/) 中的
Universal Session API 发布副本和自启动环境示例。

## 5. 商品与购物车

设置 session 文件：

```bash
export YONEX_SESSION_FILE=/ql/data/config/yonex-sessions/yonex-ACCOUNT-HASH.json
```

无参数时只读本地摘要，不发商城请求：

```bash
python3 scripts/python/yonex_order.py
```

搜索、批量加载、详情：

```bash
python3 scripts/python/yonex_order.py --search ASTROX --page 1 --page-size 6
python3 scripts/python/yonex_order.py --product-ids 1260,1262,1263
python3 scripts/python/yonex_order.py --product-id 1263
```

按 SKU 加购物车是显式写操作：

```bash
python3 scripts/python/yonex_order.py \
  --product-id 1263 \
  --sku-code AX-BLUE-4U \
  --quantity 1 \
  --add-to-cart
```

也可以按属性选 SKU：

```bash
python3 scripts/python/yonex_order.py \
  --product-id 1263 \
  --attributes '{"颜色":"蓝色","规格":"4U"}' \
  --add-to-cart
```

读取购物车和结算预览：

```bash
python3 scripts/python/yonex_order.py --cart
python3 scripts/python/yonex_order.py --preflight --cart-item-ids CART_ITEM_ID
```

预览输出只保留地址 ID/默认标记，不输出姓名、手机号和详细地址。

## 6. 创建待支付订单

创建订单需要同时满足：

1. `--create-order`；
2. `--ack-create CREATE_UNPAID_ORDER`；
3. 交互输入精确的 `CREATE`。

```bash
python3 scripts/python/yonex_order.py \
  --cart-item-ids CART_ITEM_ID \
  --address-id ADDRESS_ID \
  --create-order \
  --ack-create CREATE_UNPAID_ORDER
```

命令先执行结算预览，确认后只调用 `POST /api/order/generateOrder`，输出待支付订单摘要并结束。

## 7. 单独获取支付参数

该动作不创建订单，也不执行支付：

```bash
python3 scripts/python/yonex_order.py \
  --payment-params \
  --order-sn ORDER_SN \
  --ack-payment-params FETCH_PAYMENT_PARAMS
```

CLI 会隐藏 `paySign`、`nonceStr` 和 `packageValue`；Python 客户端的 `payment_parameters()` 会把原始字段返回给明确调用它的程序。

## 8. 验证

全部验证均使用离线桩，不会创建订单或请求支付：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile yonex_client.py yonex_ql_login.py \
  scripts/python/yonex_order.py scripts/python/yonex_session_api.py
python3 scripts/python/yonex_order.py --help
python3 scripts/python/yonex_order.py
```
