# Topps 青龙登录续期部署包

本地归档按用途拆分，部署时保持本目录结构：

```text
jobs/topps_ql_login.py
jobs/verify_topps_flow.py
lib/topps_client.py
orders/topps_v12_cached_order.py
services/session_api_server.py
tools/export_topps_session.py
```

安装依赖：

```bash
pip3 install 'curl_cffi>=0.13,<0.14'
```

青龙环境变量：

```text
YYB_SERVER=http://yyb-go:8000@1
```

建议任务：

```text
名称：Topps 登录自动续期
命令：python3 jobs/topps_ql_login.py
定时：17,47 * * * *
```

该任务只刷新登录态并验证会员，不会查询商品或创建订单。
