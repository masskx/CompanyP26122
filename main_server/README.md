# main_server

`main_server` 是一个独立的 Go HTTP 服务骨架，用于后续模拟 MES 主服务。
当前版本提供健康检查、服务信息，以及面向前端小助手的 FastAPI 聊天 SSE 代理；不连接 MQTT 或业务数据库。

## 环境要求

- Go 1.22 或更高版本
- 仅使用 Go 标准库

## 启动

```powershell
cd main_server
go run .
```

服务默认监听 `http://127.0.0.1:8080`。

也可以先设置环境变量再启动：

```powershell
$env:MAIN_SERVER_ADDR = "127.0.0.1:8080"
$env:MAIN_SERVER_ENV = "development"
$env:MAIN_SERVER_LOG_LEVEL = "debug"
$env:MAIN_SERVER_SHUTDOWN_TIMEOUT = "10s"
$env:COMPUTE_SERVICE_BASE_URL = "http://172.16.103.196:8000"
$env:COMPUTE_SERVICE_TIMEOUT = "3m"
go run .
```

`.env.example` 只用于说明配置，服务不会自动读取 `.env` 文件。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAIN_SERVER_ADDR` | `127.0.0.1:8080` | HTTP 监听地址 |
| `MAIN_SERVER_ENV` | `development` | 运行环境；`production` 使用 JSON 日志 |
| `MAIN_SERVER_LOG_LEVEL` | `info` | 日志级别：`debug`、`info`、`warn`、`error` |
| `MAIN_SERVER_SHUTDOWN_TIMEOUT` | `10s` | 收到终止信号后的优雅关闭超时 |
| `COMPUTE_SERVICE_BASE_URL` | `http://172.16.103.196:8000` | 算力服 FastAPI 基础地址 |
| `COMPUTE_SERVICE_TIMEOUT` | `3m` | 单次聊天请求及流式响应的总超时 |
| `COMPUTE_SERVICE_API_KEY` | 空 | 可选的 `X-API-Key`，不会暴露给浏览器 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 进程存活检查 |
| `GET` | `/readyz` | 服务就绪检查 |
| `GET` | `/api/v1/system/info` | 服务名称、版本、环境和当前时间 |
| `POST` | `/api/v1/assistant/chat` | 代理 FastAPI `/api/v1/chat` 的 SSE 问答接口 |

除聊天 SSE 外，响应均为 JSON。所有响应都会返回 `X-Request-ID`。

聊天请求体：

```json
{"message":"你好","session_id":"可选，继续已有会话时填写"}
```

响应类型为 `text/event-stream`，事件包括 `session`、`tool`、`delta`、`citation`、`done` 和 `error`。

```powershell
Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/readyz
Invoke-RestMethod http://127.0.0.1:8080/api/v1/system/info
```

## 测试

```powershell
go test ./...
```

## ARM64 Linux 构建

```powershell
$env:GOOS = "linux"
$env:GOARCH = "arm64"
go build -o bin/main-server .
Remove-Item Env:GOOS
Remove-Item Env:GOARCH
```

## 目录结构

```text
main_server/
├─ main.go              # 程序入口
├─ internal/config/     # 环境变量配置
├─ internal/httpapi/    # HTTP 路由、响应和中间件
├─ .env.example
├─ go.mod
└─ README.md
```
