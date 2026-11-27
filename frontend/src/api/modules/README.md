# API 业务模块

此目录按业务领域封装算力服 FastAPI 接口，路径与 `backend/` 的真实路由一致。
开发时由 Vite 把 `/api` 和 `/internal` 代理到 `VITE_PROXY_TARGET`（默认
`http://127.0.0.1:8000`）。

- `assistant.ts`：调用 `POST /api/v1/chat`，解析 SSE 回答（`session`、`tool`、`delta`、
  `citation`、`done`、`error`）并映射引用数据；`stage`、`result` 等事件会被安全忽略。
- `conversation.ts`：调用 `/api/v1/sessions` 系列接口，完成会话列表、创建、详情、重命名、
  删除和历史消息分页。
- `maintenance.ts`：调用 `/internal/v1/maintenance/work-orders` 工单副本和
  `/review` 审核接口，以及 `/api/v1/stations` 与站点时序接口；维护页据此加载真实事件、
  保存人工审核，并按事件窗口拉取 24 小时/上月/去年同期曲线。
  注意：`/internal` 是主服务器的服务级接口；当前开发环境由 Vite 代理直连 FastAPI，
  生产环境应通过主服务器访问。
