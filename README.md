# MiniCode

MiniCode is a lightweight AI coding agent prototype built from the resume requirements.

## 中文说明

MiniCode 是一个面向代码仓库的智能体原型，重点验证以下能力：

- 任务理解与计划拆解
- 工具路由与执行
- 代码索引式 RAG 检索
- 文件搜索、读取、修改、运行
- 会话记忆与工具审计
- 修改后自动校验与索引刷新
- 可选 OpenAI 兼容模型接入

## English Overview

MiniCode is a code-agent prototype focused on proving the core workflow of an AI developer assistant:

- task understanding and planning
- tool routing and execution
- code-index-based RAG retrieval
- file search, read, edit, and run
- session memory and tool audit traces
- post-edit validation and index refresh
- optional OpenAI-compatible model integration

## Architecture

- FastAPI backend
- SQLite for sessions, memory, tool runs, and role traces
- Lightweight code chunk index for RAG-style retrieval
- Simple web UI for chat, search, read, run, and index operations

## Run Locally

```bash
cd minicode
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`

## Environment Variables

- `MINICODE_WORKSPACE`: workspace root, defaults to this project
- `OPENAI_API_KEY`: optional, enables LLM planning
- `OPENAI_BASE_URL`: optional OpenAI-compatible endpoint
- `MINICODE_MODEL`: default `gpt-4.1-mini`
- `MINICODE_ALLOW_SHELL`: default `0`, shell execution stays confirm-only

## Interview Pitch

This project is not a production-grade coding platform. It is a demo-ready agent prototype that shows a complete loop:

`plan -> tool use -> result review -> memory -> validation -> index refresh`

That is enough to explain the design, implementation, and extension path in an interview.

