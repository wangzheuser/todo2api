# US 服务器部署

目标域名：`https://todo2api.codeai.de5.net`

## 必填环境变量

复制 `.env.example` 为 `.env`，填写管理员凭据、永久主密钥和客户端 Token。
Compose 通过 `env_file` 把变量注入容器，Go 配置加载器再展开
`deploy/config.yaml` 中的 `${VARIABLE}`；容器启动不依赖 Shell 加载 `.env`。

主密钥只生成一次：

```bash
openssl rand -base64 32
```

客户端 Token：

```bash
openssl rand -hex 32
```

## 启动

```bash
install -d -m 0700 data
chown 10001:10001 data
chmod 0600 .env
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

## 验证

```bash
TODO2API_CLIENT_TOKEN="$(sed -n 's/^TODO2API_CLIENT_TOKEN=//p' .env)" \
  ./deploy/verify.sh
```

通过 WebUI 导入至少一个 todofor.ai API Key 后，再执行真实非流式和流式模型调用。

## 备份

SQLite 使用 WAL。获取一致备份时先停止容器，再同时备份 `.env` 和 `data/`。
`storage.master_key` 与数据库必须成对保留。
