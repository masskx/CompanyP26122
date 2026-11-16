# main_server

`main_server` 是独立的 Go HTTP 服务，为前端提供健康检查、服务信息和小助手聊天
SSE 代理。它不连接 MQTT、业务数据库，也不提供前端静态文件。

## 环境要求

- Go 1.22 或更高版本
- 仅使用 Go 标准库

## 配置

服务只从运行时 JSON 文件读取配置，不读取环境变量或 `.env`。

首次运行复制示例：

```powershell
cd main_server
Copy-Item config.example.json config.json
```

以后每次只编辑本地 `config.json`，不要覆盖现场配置。示例内容：

```json
{
  "listen_addr": "127.0.0.1:8080",
  "environment": "development",
  "log_level": "info",
  "shutdown_timeout": "10s",
  "compute": {
    "base_url": "http://172.16.103.196:8000",
    "chat_path": "/api/v1/chat",
    "timeout": "3m",
    "api_key": ""
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `listen_addr` | 否 | `127.0.0.1:8080` | HTTP 监听地址 |
| `environment` | 否 | `development` | `production` 使用 JSON 日志 |
| `log_level` | 否 | `info` | `debug`、`info`、`warn`、`error` |
| `shutdown_timeout` | 否 | `10s` | 优雅关闭超时 |
| `compute.base_url` | 是 | 无 | 算力服 FastAPI 基础地址 |
| `compute.chat_path` | 否 | `/api/v1/chat` | 算力服聊天接口路径 |
| `compute.timeout` | 否 | `3m` | 单次聊天和 SSE 总超时 |
| `compute.api_key` | 否 | 空 | 发给算力服的 `X-API-Key`；为空时不发送 |

未知 JSON 字段、非法 URL、非法路径、非法超时和非法日志级别都会使服务启动失败。
配置变化后重启服务，不支持热加载。

`config.json` 被视为本地实例配置，不提交到 Git；`config.example.json` 可以提交。

## 启动

```powershell
cd main_server
go run . -config .\config.json
```

也可以显式指定其他配置文件：

```powershell
go run . -config D:\mes-config\main-server.json
```

默认监听 `http://127.0.0.1:8080`。

## 本地联调 B

当前模拟开发环境中，B FastAPI 位于另一台电脑时，配置示例：

```json
{
  "listen_addr": "127.0.0.1:8080",
  "environment": "development",
  "log_level": "debug",
  "shutdown_timeout": "10s",
  "compute": {
    "base_url": "http://172.16.103.196:8000",
    "chat_path": "/api/v1/chat",
    "timeout": "3m",
    "api_key": ""
  }
}
```

B 必须监听局域网地址，例如：

```bash
cd backend
python scripts/run.py --host 0.0.0.0 --port 8000
```

并允许本机 IP 访问 B 的 TCP `8000`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 进程存活检查 |
| `GET` | `/readyz` | 服务就绪检查 |
| `GET` | `/api/v1/system/info` | 服务名称、版本、环境和当前时间 |
| `POST` | `/api/v1/assistant/chat` | 代理算力服聊天 SSE |

除聊天 SSE 外，响应均为 JSON。所有响应都会返回 `X-Request-ID`。

聊天请求体：

```json
{"message":"你好","session_id":"可选，继续已有会话时填写"}
```

响应类型为 `text/event-stream`，事件包括 `session`、`tool`、`delta`、`citation`、
`done` 和 `error`。

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:8080/api/v1/system/info
```

## 测试

```powershell
go test ./...
go vet ./...
```

## ARM64 Linux 构建

```powershell
cd main_server

# 先在当前 Windows 架构执行测试
go test ./...
go vet ./...

$env:GOOS = "linux"
$env:GOARCH = "arm64"
$env:CGO_ENABLED = "0"
go build -trimpath -ldflags="-s -w" -o bin/main-server .
Remove-Item Env:GOOS
Remove-Item Env:GOARCH
Remove-Item Env:CGO_ENABLED
```

## 目录结构

```text
main_server/
├─ main.go              # 启动、关闭和日志
├─ config.go            # JSON 配置加载与校验
├─ api.go               # 路由、响应和中间件
├─ assistant.go         # 聊天 SSE 代理
├─ api_test.go          # 配置、路由和中间件测试
├─ assistant_test.go    # 聊天代理测试
├─ config.example.json
├─ go.mod
└─ README.md
```
