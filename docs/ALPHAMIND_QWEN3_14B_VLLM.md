# AlphaMind qwen3-14b vLLM 运行说明

本仓库的 Phase3 默认聊天模型已按客户说明切换为 `qwen3-14b`，并假设客户机使用 vLLM 暴露 OpenAI-compatible API。Embedding 与 reranker 仍由共享 Qwen3 embedding/reranker 服务提供。

## 1. vLLM 启动约定

推荐客户机启动时固定 served model name，避免 WeKnora、验收脚本和 `/v1/models` 返回值不一致：

```bash
python -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 \
  --port 8000 \
  --model Qwen/Qwen3-14B \
  --served-model-name qwen3-14b \
  --trust-remote-code \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90
```

如启用了 API key，例如 `--api-key <secret>`，则 `.env.phase3-precision` 中 `LLM_API_KEY` 必须填写同一值；不要把真实 key 提交到 Git。

## 2. AlphaMind 环境变量

`.env.phase3-precision.example` 已给出默认值：

```env
LLM_MODEL_NAME=qwen3-14b
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_API_KEY=local-no-auth
LLM_PROVIDER=openai
PHASE3_DIRECT_CHAT_RUNTIME=vllm
PHASE3_DISABLE_THINKING=true
PHASE3_DIRECT_CHAT_URL=http://localhost:8000/v1
PHASE3_DIRECT_CHAT_MODEL=qwen3-14b
```

容器内的 WeKnora 使用 `host.docker.internal:8000` 访问宿主 vLLM；验收脚本在宿主机上使用 `localhost:8000` 直连。Linux/Ubuntu Compose 通过 `docker-compose.delivery.yml` 配置 `host-gateway`。

如果客户启动 vLLM 时使用了不同的 `--served-model-name`，必须同步修改：

- `LLM_MODEL_NAME`
- `PHASE3_DIRECT_CHAT_MODEL`

## 3. 冒烟验证

在启动 AlphaMind 前先验证 vLLM：

```bash
curl --fail http://127.0.0.1:8000/v1/models
python - <<'PY'
import json, urllib.request
payload = {
    "model": "qwen3-14b",
    "messages": [{"role": "user", "content": "只回答：pong"}],
    "stream": False,
    "chat_template_kwargs": {"enable_thinking": False},
}
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
print(urllib.request.urlopen(req, timeout=120).read().decode("utf-8")[:1000])
PY
```

验收时再运行：

```bash
python scripts/alphamind_healthcheck.py --phase phase3-precision --env-file .env.phase3-precision --check-models
python scripts/alphamind_phase3_online_eval.py --env-file .env.phase3-precision
```

## 4. 验收边界

本次变更把报价单原先的 35B 聊天模型交付目标按客户说明替换为 `qwen3-14b` + vLLM。当前 Windows 本机没有运行 vLLM 8000 端口，不能把本机旧的 Ollama `qwen3:4b` 结果冒充为客户目标机 qwen3-14b 终验结果；客户机完成上述 vLLM 启动后，需要重新生成在线评测、质量门禁和交付 manifest。
