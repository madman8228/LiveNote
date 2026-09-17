# LiveNote 部署模板

这些文件是生产部署模板，不包含真实域名、证书路径或密钥。

## 推荐结构

```text
/opt/livenote/app/       LiveNote 代码和 server/
/opt/livenote/web/       npm run build 后的 dist/
/var/lib/livenote/       SQLite、Chunk、重建音频、ASR 结果
```

API 使用一个 worker：

```bash
uvicorn main:app --app-dir /opt/livenote/app/server --host 127.0.0.1 --port 8000 --workers 1
```

Nginx 对外提供 HTTPS，并把 `/api/` 代理到本机 8000 端口。请先配置环境变量，再启动服务；不要把真实 API Key 写入仓库。

Nginx 可以直接参考 [`nginx/livenote.conf.example`](./nginx/livenote.conf.example)。它包含：

- HTTPS 和 HTTP → HTTPS 跳转
- Vue/Vite history fallback 到 `index.html`
- `/api/` 反向代理到 FastAPI
- 30 MB 上传上限，关闭上传请求缓冲，避免音频 Chunk 被代理截断
- 长达 1 小时的 API 超时，覆盖音频重建和处理轮询

建议把服务端配置保存为 `/etc/livenote/livenote.env`，至少包含：

```text
LIVENOTE_ENV=production
LIVENOTE_CORS_ORIGINS=https://你的域名
LIVENOTE_API_KEY=随机长密钥
LIVENOTE_DATA_DIR=/var/lib/livenote/data
LIVENOTE_DB_PATH=/var/lib/livenote/livenote.sqlite3
LIVENOTE_WHISPER_MODEL=base
LIVENOTE_WHISPER_CACHE=/var/lib/livenote/models
```

如果需要语义总结，再加入 `LIVENOTE_LLM_BASE_URL`、`LIVENOTE_LLM_API_KEY` 和
`LIVENOTE_LLM_MODEL`。该文件应限制为 API 服务用户可读，不能提交到 Git。

部署后的最低检查顺序：

1. 浏览器访问 `https://你的域名/`。
2. 确认浏览器能力检测和麦克风授权正常。
3. 录制 30 秒，确认 Chunk 上传到 API。
4. 点击本地处理，确认后台任务完成并能读取报告。
5. 再进行长时间录音。
