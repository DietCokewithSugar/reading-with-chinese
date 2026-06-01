# Reading with Chinese

一个**开箱即用**的在线 PDF 翻译网站，基于
[PDFMathTranslate (`pdf2zh`)](https://github.com/Byaidu/PDFMathTranslate) +
[DeepSeek](https://platform.deepseek.com/)。打开网页即可使用：上传 PDF → 在线预览
→ 一键翻译 → 在线对照浏览 / 下载译文。

> 翻译时**保留原始排版**：标题仍然是大字，正文仍然是小字，公式、图片、表格位置不变 ——
> 因为 `pdf2zh` 会在原版式的文本框内就地重排译文。

## 功能特性

- 🚀 **开箱即用**：无需登录、无数据库，打开页面就能用。
- 🔑 **本地保存密钥**：DeepSeek API Key 只存在你浏览器的 `localStorage` 里，随请求发送，
  服务器**不会持久化**。
- 📄 **在线预览**：左侧原文、右侧译文并排预览（浏览器原生 PDF 渲染，无外部依赖）。
- 🌐 **双语对照**：译文支持「仅译文 / 双语对照」两种视图，可随时切换并下载。
- ♾️ **不限上传大小**：超长 PDF 会被**自动按页切分**，分块送翻，再**按原顺序自动拼接**。
- ⚡ **并发翻译**：多块 PDF **同时**翻译（块级并发），块内再做段落级并发，速度更快。
- 🎨 **不打乱排版**：只在页与页之间切分，单页版式永不被破坏，拼接即恢复原始顺序。
- 💾 **仅本地存储**：上传件与译文只作为本地文件按任务 ID 存放，超时自动清理。

## 快速开始

```bash
# 1. 安装依赖
pip3 install -r requirements.txt

# 2.（可选，推荐）预下载排版模型，首次翻译更快
python3 scripts/prefetch.py

# 3. 启动
./run.sh
#   或： python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 ，点右上角 **⚙ 设置** 填入 DeepSeek API Key，
上传 PDF，点 **开始翻译**。

> 首次运行需要联网，`pdf2zh` 会自动下载文档版面识别模型与字体（一次性）。之后即可离线使用
> （翻译本身仍需访问 DeepSeek API）。

## 设置项

| 选项 | 说明 | 默认 |
| --- | --- | --- |
| DeepSeek API Key | 你的密钥，仅存浏览器本地 | — |
| 模型 | `deepseek-chat` / `deepseek-reasoner` | `deepseek-chat` |
| 源 / 目标语言 | 翻译方向 | en → zh |
| 每块页数 | 长 PDF 切分时每块的页数 | 8 |
| 并发块数 | 同时翻译的块数 | 6 |
| 块内并发请求 | 每块内段落级并发数 | 4 |

## 架构

```
浏览器 (单页应用)
  · 原文预览：本地 File → object URL → <iframe>（不必上传即可看）
  · API Key / 设置：localStorage
        │  multipart 上传 + 轮询进度
        ▼
FastAPI (app/server.py)
  · 上传流式落盘（无内存大小限制），按任务 ID 存本地
  · 内存任务表 + 后台线程 + 进度
        │
        ▼
翻译引擎 (app/translator.py)
  · split_pdf：按页切分（PyMuPDF）
  · ThreadPoolExecutor：多块并发翻译
  · pdf2zh.translate_stream：保版式翻译（DeepSeek），块内段落并发
  · merge_pdfs：按原顺序拼接 → (译文版, 双语版)
```

### 顺序与排版如何保证

- **顺序**：切分时记录每块的页范围，结果数组按块索引回填，再顺序拼接，
  乱序不可能发生（见 `tests/test_split_merge.py`）。
- **排版**：只在**页边界**切分，单页布局从不被拆开；`pdf2zh` 在原始文本框与字号内
  重排译文，因此标题/正文字号、公式与图表位置都保持原样。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/translate` | 上传 PDF + 参数，返回 `{job_id}` |
| `GET` | `/api/jobs/{id}` | 任务状态与进度 |
| `POST` | `/api/jobs/{id}/cancel` | 取消任务 |
| `GET` | `/api/jobs/{id}/file/{mono\|dual}` | 预览/下载译文（`?download=1` 触发下载） |
| `GET` | `/api/health` | 健康检查 + 模型是否就绪 |

## 部署

> ⚠️ **不要用 Vercel / Netlify Functions 等 Serverless 平台。** 本应用依赖体积 >1GB
> （超过 Lambda 500MB 限制），且需要跑数分钟的后台翻译任务、共享内存任务表与本地磁盘存储 ——
> 这些都与无状态、短时的 Serverless 函数模型冲突。它需要一个**长期运行的容器/服务器**。

推荐用容器平台：**Render / Railway / Koyeb / Fly.io / Hugging Face Spaces (Docker)**，
或任意 VPS。仓库已提供 `Dockerfile`：

```bash
# 本地或任意 VPS
docker build -t reading-with-chinese .
docker run -p 8000:8000 reading-with-chinese
# 打开 http://localhost:8000
```

平台要点：
- **Render / Railway / Koyeb**：连上 GitHub 仓库，选 Docker，平台会注入 `$PORT`（已自动适配）。
- **Fly.io**：`fly launch`（用本仓库 Dockerfile），建议挂一个持久卷到模型缓存目录避免冷启动重下模型。
- 镜像已做瘦身：移除了 GUI 版 OpenCV 与 pdf2zh 自带的 gradio（本应用用自己的前端）。
- 反向代理（Nginx 等）记得放开上传大小限制（如 `client_max_body_size 0;`）。

## 测试

```bash
python3 -m pytest tests/ -v
```

离线测试覆盖切分 / 分块规划 / 顺序拼接（不需要模型、网络或 API Key）。

## 说明

- 数据不入库，仅本地磁盘；任务默认 6 小时后自动清理（`app/server.py` 中 `JOB_TTL_SECONDS`）。
- 生产部署如用 Nginx 等反向代理，注意放开其上传大小限制（如 `client_max_body_size`），
  否则会与「不限大小」相冲突。
