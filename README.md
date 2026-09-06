# TT Switch 加密 WebSocket 反向隧道

该工具通过内网服务器主动连接公网服务器，将公网 HTTP 请求转发到本机 TT Switch。公网服务器不需要主动访问内网。

同一个 `18443` 端口同时承载：

- 内网 Agent：`ws://118.31.105.6:18443/_tunnel`
- 外网客户端模型 API：`http://118.31.105.6:18443/*`
- 中继状态：`http://118.31.105.6:18443/__tunnel_health`

支持普通 HTTP 请求和 SSE 流式响应。它面向 TT Switch 的模型 HTTP API，不是任意 TCP 端口转发器。

## 无证书时的加密方式

公网服务器没有 TLS 证书，因此默认使用 `ws://`。普通、受信任的 `wss://` 必须配置 TLS 证书；自签名证书也能建立 WSS，但每个客户端都要额外信任证书或关闭校验。

当前脚本在 WS 内增加了应用层加密：

- 隧道密钥不会通过网络发送。
- 双方使用随机挑战、HMAC-SHA256 相互确认共享密钥。
- 每次连接通过 HKDF-SHA256 派生独立会话密钥。
- 请求头、请求体和流式响应均使用 ChaCha20-Poly1305 逐帧认证加密。
- 帧序号可以检测重放、篡改和乱序。

这会保护“公网服务器到内网 Agent”这一段。外网客户端访问 `http://118.31.105.6:18443` 时仍是明文 HTTP，公网服务器必须读取请求才能转发。若外网链路也需要保护，仍需为公网入口配置 HTTPS，或让外网客户端使用并信任自签名证书。

## 文件

- `public_relay.py`：部署到公网服务器 `118.31.105.6`。
- `intranet_agent.py`：部署到能访问本机 TT Switch 的内网服务器。
- `.env.txt`：环境变量配置——`IP=` 供内网 Agent 读取，`TT_TOKEN=` 供外网 demo 读取；真实文件已被 `.gitignore` 排除，模板见 `.env.txt.example`。
- `requirements.txt`：两端共用依赖。

## 1. 核对共享密钥

两个 Python 文件顶部的 `TUNNEL_SECRET` 必须完全一致，并且至少 32 个字符。首次部署前建议自行重新生成：

```bash
python3 -c "import secrets; print('ttunnel-' + secrets.token_urlsafe(32))"
```

将输出分别填入两个文件，密钥不要用于 TT Switch 的 Bearer Token。

## 2. 公网服务器

确认云安全组和系统防火墙已放行 TCP `18443`，然后执行：

```bash
python3 -m pip install -r requirements.txt
python3 public_relay.py
```

检查监听和状态：

```bash
ss -lntp | grep 18443
curl http://127.0.0.1:18443/__tunnel_health
```

Agent 尚未连接时，状态是 `waiting_for_agent`。

## 3. 内网服务器

编辑同目录 `.env.txt`（不存在则新建，需与脚本一起拷贝到内网服务器），写入 Mac 的内网 IP：

```text
IP=10.34.105.33
```

Agent 优先读取 `.env.txt` 中的 `IP=xxx`；没有该文件或未配置时，才使用
`intranet_agent.py` 内置的 `LOCAL_TTSWITCH_IP` 默认值。该 IP 必须是内网服务器能够访问的 Mac 内网 IP，不能填写 `0.0.0.0`。确保 TT Switch 正在监听 `0.0.0.0:15721`，然后执行：

```bash
python3 -m pip install -r requirements.txt
python3 intranet_agent.py
```

看到以下文字表示已完成认证和加密协商：

```text
反向隧道已建立，载荷已启用 ChaCha20-Poly1305 加密
```

此时在公网服务器执行：

```bash
curl http://127.0.0.1:18443/__tunnel_health
```

`agent_connected` 应为 `true`，`tunnel_encryption` 应为 `ChaCha20-Poly1305`。

## 4. 外网测试

将 `<TT_SWITCH_TOKEN>` 替换成 TT Switch 的访问 Token：

```bash
curl -N \
  -H 'Authorization: Bearer <TT_SWITCH_TOKEN>' \
  http://118.31.105.6:18443/tencent/v1/models
```

外网客户端 Base URL：

```text
http://118.31.105.6:18443/tencent/v1
```

`TUNNEL_SECRET` 与 TT Switch Token 是不同凭据。外网 API 请求只需要携带 TT Switch Token，不需要携带隧道密钥。

## 5. 后续升级为 HTTPS/WSS

有域名和证书后：

1. 在 `public_relay.py` 同时填写 `SSL_CERT_FILE` 和 `SSL_KEY_FILE`。
2. 将 `intranet_agent.py` 的 `PUBLIC_TUNNEL_URL` 改成 `wss://域名:18443/_tunnel`。
3. 外网客户端改用 `https://域名:18443`。
4. 保留应用层加密也没有问题，此时是双层保护。

如使用自签名证书，内网测试可临时把 `VERIFY_PUBLIC_TLS` 改为 `False`，但更推荐把自签名 CA 安装到客户端信任库。

## 常见问题

- `内网 Agent 未连接`：检查内网服务器能否访问 `118.31.105.6:18443`，再核对两个脚本的 `TUNNEL_SECRET`。
- `隧道认证失败` 或 `加密帧认证失败`：两个脚本的密钥或版本不一致，更新后需同时重启。
- `无法连接 TT Switch`：在内网服务器执行 `curl http://<本机IP>:15721/tencent/v1/models`，检查 Mac 防火墙和 TT Switch 监听地址。
- 普通模型 API 返回 `401`：外网请求携带的 TT Switch Token 不正确，与隧道密钥无关。
- 流式中途超时：默认允许响应连续静默 `600` 秒，可修改 `STREAM_IDLE_TIMEOUT`。

## 在线演示（GitHub Pages）

`docs/index.html` 是纯前端单页演示（仿 DeepSeek Harness 风格，零依赖、零构建）：

- 模型选择（31 个内置清单，分组标「公费 / 内网·免费」，多模态带 📷）+ 推理等级
- 流式对话（SSE），推理过程折叠展示；Markdown / 代码块渲染
- 上传图片（多模态模型，OpenAI `image_url` 格式）、读取本地文本文件附加到消息
- 设置抽屉配置 TT_TOKEN / Base URL / System Prompt / 上下文条数；令牌仅存浏览器 localStorage
- 未配置令牌时页面顶部横幅提示（非弹窗）；30 秒一次隧道健康状态点

发布：GitHub 仓库 → Settings → Pages → Deploy from a branch → `main` 分支 `/docs` 目录，
访问 `https://whypilotxia.github.io/ttswitch-ws-tunnel/`。

浏览器限制与解法：

1. **CORS**：`public_relay.py` 已对 API 响应附加 CORS 头并直接应答 OPTIONS 预检
   （`Allow-Origin: *`；令牌走 `Authorization` 头不依赖 Cookie，安全）。服务器更新后需重启
   `public_relay.py`。
2. **混合内容**：GitHub Pages 是 HTTPS，浏览器禁止调用 `http://` 网关。**无域名解法**——
   sslip.io 免费把 `118-31-105-6.sslip.io` 解析到 `118.31.105.6`（IP 嵌在域名里，无需购买），
   再用 Let's Encrypt 免费签证书：
   ```bash
   # 公网服务器上（需临时空出 80 端口）
   sudo certbot certonly --standalone -d 118-31-105-6.sslip.io
   ```
   然后把 `public_relay.py` 的 `SSL_CERT_FILE` / `SSL_KEY_FILE` 指向
   `/etc/letsencrypt/live/118-31-105-6.sslip.io/` 下的 `fullchain.pem` / `privkey.pem` 并重启；
   `intranet_agent.py` 的 `PUBLIC_TUNNEL_URL` 改为
   `wss://118-31-105-6.sslip.io:18443/_tunnel`；页面设置中 Base URL 改为
   `https://118-31-105-6.sslip.io:18443/tencent/v1`。
   证书 90 天有效，`certbot renew` + 重启中继即可续期。若 sslip.io 撞上 Let's Encrypt
   公共域名周限额（与全网用户共享 50 张/周，偶发），换 nip.io 域名重签，或用
   ZeroSSL（`acme.sh --server zerossl`）。
3. **零改动兜底**：页面横幅提供「下载本页」，本地双击打开（`file://` 不受混合内容限制，
   CORS `*` 覆盖 `Origin: null`），功能完全一致。
