# Universal Session API（5800）YONEX 接入

本目录保存已部署到青龙 `5800` 端口的 Universal Session API 发布副本。
它在原有 `topps`、`wdngm`、`ln` 路由之外注册 YONEX，并提供：

- `/yonex/accounts`
- `/yonex/session`
- `/yonex/account`
- `/yonex/refresh`

YONEX 路由即使服务以 `--no-auth` 兼容旧项目运行，仍强制要求
`Authorization: Bearer ...`，且不接受 query-string token。账号列表只返回
脱敏状态；哈希 Session 文件按 JSON 内原始 `ref` 精确匹配。

## 文件

```text
session_api_server_generic.py   5800 服务实现
extra.sh.example                青龙自启动环境示例
tests/test_generic_api.py       离线集成测试
```

发布源码 SHA-256：

```text
b9a4309e652ac4561c2b7c6dd9e826c520ffda285ff808f8fbd6be0fd9457940
```

## 青龙部署约定

服务文件部署到：

```text
/ql/data/scripts/topps/scripts/python/session_api_server_generic.py
```

YONEX 配置：

```text
YONEX_RENEWAL_ENTRY=/ql/data/scripts/yonex/yonex_ql_login.py
YONEX_SESSION_DIR=/ql/data/config/yonex-sessions
Token 文件：/ql/data/config/yonex_api_token.txt
```

代码实际从 `YONEX_API_TOKEN` 或
`/ql/data/config/yonex_api_token.txt` 读取访问令牌。令牌文件应为非空且权限
`0600`；不要把令牌或 Session 提交到 Git。

若服务器没有完整的 canonical `scripts/project/index.json`，启动进程必须设置
四个 `*_RENEWAL_ENTRY`，示例见 [`extra.sh.example`](extra.sh.example)。

## 离线验证

```bash
python3 -B -m unittest discover -s tests -p 'test*.py' -v
python3 -m py_compile session_api_server_generic.py
```

测试不会触发 `/yonex/refresh`、查询商品或创建订单。
