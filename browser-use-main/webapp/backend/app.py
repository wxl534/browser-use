"""IDP 爬虫 Web 控制台后端(FastAPI).

提供:
- 只读数据 API:统计,图片分页/搜索,图片文件,运行历史,断点进度,最终报告
- 控制 API:启动 / 停止 supervisor,查询运行状态
- 实时日志:SSE 推流 + 最近日志快照
- 生产模式:托管 vite 构建出的前端静态文件

开发模式下前端用 vite dev server(:5173)反向代理 /api 到本服务(:8000),
所以这里开启 CORS 方便本地联调.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, paths
from .run_manager import manager

app = FastAPI(title='IDP 爬虫 Web 控制台', version='1.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.on_event('startup')
async def _on_startup() -> None:
    manager.bind_loop(asyncio.get_running_loop())


# ---------- 只读数据 ----------
@app.get('/api/stats')
async def api_stats() -> dict[str, Any]:
    return catalog.get_stats()


@app.get('/api/images')
async def api_images(
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    q: str = '',
    status: str = '',
    sort: str = 'sequence',
    order: str = 'asc',
) -> dict[str, Any]:
    return catalog.list_images(
        page=page, page_size=page_size, q=q.strip(), status=status.strip(), sort=sort, order=order
    )


@app.get('/api/images/{image_id}')
async def api_image_detail(image_id: str) -> dict[str, Any]:
    record = catalog.get_image(image_id)
    if not record:
        raise HTTPException(status_code=404, detail='image not found')
    return record


@app.get('/api/images/{image_id}/file')
async def api_image_file(image_id: str):
    path = catalog.resolve_image_path(image_id)
    if path is None:
        raise HTTPException(status_code=404, detail='image file not found')
    return FileResponse(path)


@app.get('/api/runs')
async def api_runs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return catalog.list_runs(limit=limit)


@app.get('/api/progress')
async def api_progress() -> dict[str, Any]:
    return {'progress': catalog.read_progress(), 'final_report': catalog.read_final_report()}


# ---------- 控制 ----------
@app.get('/api/run/status')
async def api_run_status() -> dict[str, Any]:
    return manager.status()


@app.post('/api/run/start')
async def api_run_start(request: Request) -> JSONResponse:
    params = await request.json()
    result = manager.start(params if isinstance(params, dict) else {})
    if not result.get('ok'):
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result)


@app.post('/api/run/stop')
async def api_run_stop() -> JSONResponse:
    result = manager.stop()
    if not result.get('ok'):
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result)


@app.get('/api/logs/recent')
async def api_logs_recent(limit: int = Query(500, ge=1, le=4000)) -> dict[str, Any]:
    return {'lines': manager.recent_log(limit)}


@app.get('/api/logs/stream')
async def api_logs_stream(request: Request) -> StreamingResponse:
    async def event_gen():
        q = await manager.subscribe()
        try:
            # 先把最近若干行作为初始快照推过去,避免新连接看到空白.
            for line in manager.recent_log(200):
                yield _sse(line)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield _sse(line)
                except asyncio.TimeoutError:
                    yield ': keep-alive\n\n'
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


def _sse(line: str) -> str:
    return f'data: {json.dumps(line, ensure_ascii=False)}\n\n'


# ---------- 健康检查 ----------
@app.get('/api/health')
async def api_health() -> dict[str, Any]:
    return {
        'ok': True,
        'project_root': str(paths.PROJECT_ROOT),
        'cache_dir': str(paths.CACHE_DIR),
        'db_available': catalog.db_available(),
    }


# ---------- 生产模式静态托管 ----------
if paths.FRONTEND_DIST.exists():
    # 资源文件(assets/ 等)直接由 StaticFiles 提供.
    app.mount('/assets', StaticFiles(directory=str(paths.FRONTEND_DIST / 'assets')), name='assets')

    @app.get('/{full_path:path}')
    async def spa_fallback(full_path: str):
        # 非 /api 的任意前端路由都回退到 index.html,支持 SPA 深链刷新.
        candidate = (paths.FRONTEND_DIST / full_path).resolve()
        dist_root = paths.FRONTEND_DIST.resolve()
        if full_path and dist_root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(paths.FRONTEND_DIST / 'index.html')
