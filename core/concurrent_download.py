"""
共享 HTTP 图片下载池.

把原先每张图片新建一个 ``aiohttp.ClientSession`` 的写法换成一次性持有的共享会话,
利用 keep-alive / TLS 复用,并通过 ``asyncio.Semaphore`` 控制并发上限.这里只负责
"把字节落到磁盘",SHA-256 / 去重 / JSONL 追加仍由调用方在 ``DOWNLOAD_LOCK`` 内串行执行.

并发上限由环境变量 ``BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY`` 控制,默认值
``DEFAULT_CONCURRENCY``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import aiohttp
import anyio


DEFAULT_CONCURRENCY = 2
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_PAGE_DELAY_SECONDS = 0.0

_USER_AGENT = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
	'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
	raw = os.environ.get(name, '').strip()
	if not raw:
		return default
	try:
		value = int(raw)
	except ValueError:
		return default
	return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
	raw = os.environ.get(name, '').strip()
	if not raw:
		return default
	try:
		value = float(raw)
	except ValueError:
		return default
	return max(minimum, value)


def image_download_concurrency() -> int:
	"""每批下载图片时的并发上限.可通过环境变量覆盖."""
	return _env_int('BROWSER_USE_IMAGE_DOWNLOAD_CONCURRENCY', DEFAULT_CONCURRENCY)


def page_delay_seconds() -> float:
	"""每页批量下载前的节流延时(秒),降低触发 Cloudflare 限流的概率.

	默认 0(不额外等待),可通过环境变量 ``BROWSER_USE_PAGE_DELAY_SECONDS`` 覆盖.
	"""
	return _env_float('BROWSER_USE_PAGE_DELAY_SECONDS', DEFAULT_PAGE_DELAY_SECONDS, minimum=0.0)


class ConcurrentImageDownloader:
	"""
	以 async context manager 形式持有一个共享 ``aiohttp.ClientSession``,对外提供
	并发安全的 ``fetch_to_file``.所有调用都会先抢 ``self._semaphore``,保证不会
	因为并发过高把站点打挂或把本地连接池打爆.
	"""

	def __init__(
		self,
		*,
		concurrency: Optional[int] = None,
		timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
		default_headers: Optional[dict[str, str]] = None,
	) -> None:
		self._concurrency = max(1, concurrency or image_download_concurrency())
		self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
		self._headers: dict[str, str] = {
			'User-Agent': _USER_AGENT,
			'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
			'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
		}
		if default_headers:
			self._headers.update(default_headers)
		self._semaphore = asyncio.Semaphore(self._concurrency)
		self._session: Optional[aiohttp.ClientSession] = None

	@property
	def concurrency(self) -> int:
		return self._concurrency

	async def __aenter__(self) -> 'ConcurrentImageDownloader':
		connector = aiohttp.TCPConnector(
			limit=self._concurrency,
			limit_per_host=self._concurrency,
			ttl_dns_cache=300,
		)
		self._session = aiohttp.ClientSession(
			timeout=self._timeout,
			headers=self._headers,
			connector=connector,
		)
		return self

	async def __aexit__(self, exc_type, exc, tb) -> None:
		if self._session is not None:
			await self._session.close()
			self._session = None

	async def fetch_to_file(
		self,
		url: str,
		target_path: Path,
		*,
		referer: Optional[str] = None,
		cookies: Optional[str] = None,
	) -> Path:
		if self._session is None:
			raise RuntimeError('ConcurrentImageDownloader 必须在 async with 内使用')
		target_path.parent.mkdir(parents=True, exist_ok=True)
		tmp_path = target_path.with_suffix(target_path.suffix + '.part')
		request_headers: dict[str, str] = {}
		if referer:
			request_headers['Referer'] = referer
		if cookies:
			request_headers['Cookie'] = cookies

		async with self._semaphore:
			async with self._session.get(
				url,
				headers=request_headers or None,
				allow_redirects=True,
			) as response:
				if response.status >= 400:
					raise RuntimeError(f'HTTP {response.status}: {url}')
				content_type = response.headers.get('Content-Type', '')
				if 'image' not in content_type.lower():
					raise RuntimeError(f'URL 返回的不是图片内容: {content_type or "unknown"}')
				async with await anyio.open_file(tmp_path, 'wb') as file:
					async for chunk in response.content.iter_chunked(1024 * 256):
						if chunk:
							await file.write(chunk)

		if tmp_path.stat().st_size == 0:
			tmp_path.unlink(missing_ok=True)
			raise RuntimeError(f'下载文件为空: {url}')
		tmp_path.replace(target_path)
		return target_path
