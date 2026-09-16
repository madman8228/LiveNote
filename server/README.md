# LiveNote API

```powershell
python -m pip install -r server/requirements.txt
python server/main.py
```

默认监听 `0.0.0.0:8000`，SQLite 在 `server/livenote.sqlite3`，音频二进制文件在 `server/data/`。

可选环境变量：

- `LIVENOTE_MAX_CHUNK_BYTES`：单个 Chunk 最大字节数，默认 25 MB。
- `LIVENOTE_CORS_ORIGINS`：逗号分隔的允许来源；默认允许本机 Vite HTTPS 地址。
- `LIVENOTE_API_KEY`：设置后，`/api/v1/*` 请求必须携带 `X-API-Key`；不设置时保持本地开发兼容。
