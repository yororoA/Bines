# TS AI SDK Gateway

一个轻量 TypeScript 网关，用 `Vercel AI SDK + DeepSeek` 统一模型调用。


## 能力

- `GET /health`：健康检查
- `POST /api/chat`：非流式文本调用（适合分类、布尔判断、摘要）

> 当前版本先用于“非工具调用场景”。


## 启动

1. 复制环境变量：
   - `.env.example` -> `.env`
2. 安装依赖并启动：
   - `npm install`
   - `npm run dev`

如果你之前已经安装过依赖，建议执行一次：

- `npm install @ai-sdk/deepseek`

默认监听：`http://127.0.0.1:3100`


## 请求示例

```json
{
  "messages": [
    {"role": "system", "content": "You are a classifier."},
    {"role": "user", "content": "Say True or False only."}
  ],
  "temperature": 0.1,
  "maxTokens": 64,
  "model": "deepseek-chat"
}
```

响应：

```json
{
  "content": "True",
  "model": "deepseek-chat"
}
```
