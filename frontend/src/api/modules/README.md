# API 业务模块

此目录按业务领域封装 MES 主后端接口。前端不直接访问算力服或模型地址。

- `assistant.ts`：调用 Go `POST /api/v1/assistant/chat`，解析真实 SSE 回答并映射引用数据。
