# todo2api 项目级工程规则

## 生产镜像构建与部署规则

> [!IMPORTANT]
> todo2api 的生产 Docker 镜像必须在当前开发电脑上以 `linux/amd64`
> 平台构建并通过镜像内全量测试，然后通过 `docker save` 导出并上传服务器。
> US 服务器只能执行 `sha256sum -c`、`docker load` 和
> `docker compose up --no-build`。服务器上禁止执行 `docker build`、
> `docker buildx build`、`docker compose build` 或任何带 `--build` 的
> Compose 命令。本地缺少 Dockerfile 或构建失败时必须停止部署，不得降级为远程构建。

### 1. 本机构建是强制门禁

所有部署到远程服务器的 todo2api Docker 镜像，必须在当前开发电脑上完成构建和测试，然后将构建完成的镜像归档上传到目标服务器。

远程服务器只允许执行：

1. 接收镜像归档；
2. 校验镜像归档 SHA-256；
3. 使用 `docker load` 导入镜像；
4. 使用 Compose 替换业务容器；
5. 执行健康检查、业务回归和失败回滚。

禁止在远程服务器执行任何形式的镜像构建，包括但不限于：

```bash
docker build
docker buildx build
docker compose build
docker compose up --build
docker compose up -d --build
```

不得因为以下原因退回到远程构建：

- 本地缺少 Dockerfile；
- 本地缺少 Compose 文件；
- 本地构建较慢；
- 跨平台构建需要 QEMU；
- 远程服务器已有源码；
- 远程服务器已有 Docker 构建缓存；
- 本地构建首次失败。

如果当前电脑缺少构建定义，必须先把构建文件纳入项目，或将服务器上的构建定义只读下载到本地临时构建上下文。远程构建不能作为 fallback。

### 2. 目标平台固定为 Linux AMD64

US 服务器目标平台固定为：

```text
linux/amd64
```

即使当前电脑是 Apple Silicon 或其他 ARM64 设备，也必须显式执行跨平台构建：

```bash
docker buildx build \
  --platform linux/amd64 \
  --load \
  --tag "todo2api:${IMAGE_TAG}" \
  .
```

构建完成后必须验证镜像平台：

```bash
docker image inspect "todo2api:${IMAGE_TAG}" \
  --format '{{.Os}}/{{.Architecture}}'
```

期望输出必须是：

```text
linux/amd64
```

平台不是 `linux/amd64` 时禁止上传和部署。

### 3. 构建必须包含完整测试

正式镜像构建必须使用项目 Dockerfile 中的完整构建流程，并确保镜像构建阶段执行：

```bash
npm ci --no-audit --no-fund
npm run build
go test ./...
go build
```

Docker 构建失败或测试失败时：

- 不得上传不完整镜像；
- 不得跳过测试重新构建；
- 不得在服务器上继续构建；
- 不得替换现有生产容器。

本地构建成功后，还应执行：

```bash
git diff --check
go test ./internal/openai ./internal/gateway ./internal/transport
```

如果 `go test ./...` 依赖生成的 WebUI `dist`，应通过项目 Dockerfile 或本地 WebUI 构建生成，不得通过跳过对应包规避失败。

### 4. 镜像标签必须不可变

禁止使用以下可变标签部署生产：

```text
latest
dev
test
```

镜像标签必须能够唯一标识本次构建，推荐格式：

```text
<feature>-<UTC_TIMESTAMP>-<SOURCE_ID>
```

例如：

```text
system-instructions-20260902T071600Z-53fa6a7
```

如果构建包含未提交修改，`SOURCE_ID` 应使用当前源码归档或 Git Diff 的短 SHA-256，不得只使用旧的 Git Commit ID。

部署后不得用同一个标签覆盖不同镜像。

### 5. 本地导出镜像

本地构建和平台验证通过后，使用 `docker save` 导出镜像：

```bash
IMAGE="todo2api:${IMAGE_TAG}"
ARCHIVE="/tmp/todo2api-${IMAGE_TAG}.tar.gz"

docker save "${IMAGE}" | gzip -9 >"${ARCHIVE}"
shasum -a 256 "${ARCHIVE}" >"${ARCHIVE}.sha256"
```

必须记录：

- 镜像标签；
- 本地镜像 ID；
- 镜像平台；
- 镜像归档绝对路径；
- 镜像归档 SHA-256；
- 对应源码状态或 Diff SHA-256。

不得把以下内容写入镜像归档、部署记录或回复：

- API Key；
- Client Token；
- Master Key；
- 管理员密码；
- `.env`；
- `config.yaml`；
- SQLite 数据库；
- Cookie 或登录会话。

### 6. 上传方式

上传 US 服务器时，必须通过配置的 SSH Manager 服务器名：

```text
server = us
```

不得把 `us` 当作 Shell/OpenSSH 别名。

建议上传到权限受控的临时目录：

```text
/tmp/todo2api-image-upload-<UTC_TIMESTAMP>/
```

至少上传：

```text
todo2api-<IMAGE_TAG>.tar.gz
todo2api-<IMAGE_TAG>.tar.gz.sha256
```

上传完成后，服务器必须先验证 SHA-256：

```bash
sha256sum -c "todo2api-${IMAGE_TAG}.tar.gz.sha256"
```

校验失败时禁止执行 `docker load`。

### 7. 远端只允许加载镜像

服务器导入镜像时只允许：

```bash
gzip -dc "todo2api-${IMAGE_TAG}.tar.gz" | docker load
```

导入完成后必须验证：

```bash
docker image inspect "todo2api:${IMAGE_TAG}"
docker image inspect "todo2api:${IMAGE_TAG}" \
  --format '{{.Os}}/{{.Architecture}}'
```

期望平台：

```text
linux/amd64
```

必须核对服务器导入后的镜像 ID 与本地构建记录一致。

服务器不得对上传的源码或镜像执行二次构建。

### 8. 生产替换前的治理流程

涉及 US 服务器服务状态、Docker 容器、端口或 Nginx 上游时，执行前必须通过 SSH Manager 读取：

```text
/opt/ai-governance/AGENTS.md
/opt/ai-governance/docs/nginx/POLICY.md
/opt/ai-governance/docs/nginx/DEPLOYMENT.md
```

并运行：

```bash
/usr/local/bin/codeai-nginx-audit
docker exec nginx-proxy nginx -t
```

生产容器替换前必须获得用户明确确认，并获取独占部署锁。

todo2api 部署锁固定使用：

```text
/var/lock/codeai-todo2api-deploy.lock
```

### 9. 备份必须位于 Docker 构建上下文之外

生产备份不得创建在：

```text
/opt/docker_projects/todo2api/backups
```

或任何会被 Docker `COPY . .` 包含的项目子目录中。

备份必须放到项目目录之外，例如：

```text
/opt/docker_projects/todo2api-deploy-backups/<UTC_TIMESTAMP>-pre-deploy
```

这是强制规则，因为项目目录内的 `.go` 备份文件会被：

```bash
go test ./...
```

识别为额外 Go Package，造成镜像构建失败。

备份目录权限：

```text
目录：0700
.env：0600
SQLite 备份：0600
```

备份至少包含：

- 当前 `.env`；
- 当前 Compose 文件；
- 当前部署状态；
- 当前镜像标签和镜像 ID；
- SQLite 一致性备份；
- 本次需要替换的服务器文件（如果仍需同步运行时源文件）。

即使服务器不再负责构建，仍应保留部署前状态用于回滚。

### 10. 替换容器时禁止构建

镜像上传并加载成功后，只允许使用已有镜像替换容器。

允许：

```bash
docker compose up \
  -d \
  --no-build \
  --no-deps \
  --force-recreate \
  todo2api
```

禁止：

```bash
docker compose build
docker compose up --build
docker compose up -d --build
```

替换前应把 `TODO2API_IMAGE_TAG` 更新为本次不可变标签，并保留原 `.env` 备份。

不得改变：

- `TODO2API_BIND_IP`；
- `TODO2API_HOST_PORT`；
- Master Key；
- Client Token；
- 管理员凭据；
- `/data` 挂载；
- Docker 网络；
- Nginx 路由；
- TLS 或 Cloudflare 配置。

### 11. 部署后验证

容器替换后必须等待健康状态：

```bash
docker inspect todo2api \
  --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}'
```

期望：

```text
healthy
```

必须验证直接上游：

```text
http://172.17.0.1:18086/healthz
http://172.17.0.1:18086/v1/models
```

必须验证公网域名：

```text
https://todo2api.codeai.de5.net/healthz
https://todo2api.codeai.de5.net/
https://todo2api.codeai.de5.net/v1/models
```

验收状态至少包括：

```text
healthz = 200
WebUI = 200
未授权 models = 401
授权 models = 200
模型列表包含 ox-alpha
```

工具协议变更还必须完成真实非流式闭环：

1. `user + tools`；
2. `system + user + tools`；
3. 第一轮 `finish_reason=tool_calls`；
4. 工具名和 JSON 参数正确；
5. 第二轮复用同一 Todo ID；
6. 第二轮 `finish_reason=stop`；
7. 最终答案包含工具真实返回值；
8. 不出现 prompt injection 或“工具不存在”拒绝。

同时验证至少两个无关控制域名，确认共享入口没有受到影响。

最后再次执行：

```bash
docker exec nginx-proxy nginx -t
```

本次没有修改 Nginx 时，不执行 Nginx reload 或 restart。

### 12. 自动回滚

以下任一条件失败时必须恢复旧镜像：

- 镜像归档 SHA-256 不匹配；
- 镜像平台不是 `linux/amd64`；
- `docker load` 失败；
- 容器未进入 healthy；
- `/healthz` 非 200；
- 模型列表不包含 `ox-alpha`；
- 工具调用闭环失败；
- 公网域名回归失败；
- 控制域名响应发生变化。

回滚时：

1. 恢复原 `.env`；
2. 恢复原镜像标签；
3. 使用 `--no-build` 重新创建业务容器；
4. 等待旧容器恢复 healthy；
5. 重测直接上游、公网域名和控制域名；
6. 不重启 Nginx 容器；
7. 不修改 Nginx 配置。

### 13. 部署临时文件清理

部署成功并完成回归后，删除：

- 本地镜像 `.tar.gz`；
- 本地 `.sha256`；
- 服务器上传的镜像归档；
- 服务器上传的 `.sha256`；
- 临时构建上下文；
- 临时部署脚本。

保留：

- 不可变 Docker 镜像；
- 项目目录外的受限回滚备份；
- 不包含凭据的部署状态文件；
- 必要的脱敏验证结果。

清理后必须确认：

```text
本地无临时服务监听
服务器无上传归档残留
仓库无 API Key、Master Key、Token 或 .env
Git 工作区只包含预期源码和测试变更
```

### 14. Git 提交与推送顺序

固定顺序：

```text
本地测试
→ 当前电脑构建 linux/amd64 镜像
→ 导出并上传镜像
→ 服务器 docker load
→ 生产容器替换
→ 完整回归
→ 本地提交
→ 提交信息自检
→ git push
```

只有部署和完整回归成功后才允许提交、推送。

提交信息必须使用简体中文，例如：

```text
fix: 修复系统指令下的工具调用
```

提交后必须执行：

```bash
git log -1 --pretty=%s
```

确认冒号后的提交摘要为简体中文。
