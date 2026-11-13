"""响度测量（Loudness / EBU R128）：给「统一响度」提供每段的实测值。

    GET /yuancut/api/loudness?assetId=&start=&duration=

多个片段声音忽大忽小时，靠人工拉音量滑块是逐条试错；这里用 ffmpeg 的
loudnorm 单遍测量，拿到**片段区间**（不是整素材）的积分响度（LUFS），
前端再按「目标响度 − 实测响度」算出每段该乘多少增益，一次性写回 volume。

测量只读素材、不改文件，也不占工作线程：用子进程跑，同时最多两份。
结果按 (素材, 大小, mtime, 起点, 时长) 缓存，重复点「统一响度」不必重测。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from aiohttp import web

from .yuancut_render import ASSET_ID_RE, _asset_path, _probe_streams
from .yuancut_render import logger as _log

#: 同时测量的份数：ffmpeg 解码很快，限两份既快又不抢渲染
_MAX_PARALLEL = 2
#: 单次测量的上限（秒）：超长素材也不至于把请求挂死
_TIMEOUT_SECONDS = 180.0
#: 缓存上限：几十个素材 × 几个片段，几百条足够
_CACHE_LIMIT = 2000

_cache: dict[tuple, dict] = {}
_semaphore: asyncio.Semaphore | None = None


def _ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return str(candidate)
    return None


def _parse_loudnorm(text: str) -> dict:
    """从 loudnorm 的 stderr 里抠出那段 JSON（它前面有大量解码日志）。"""
    anchor = text.rfind('"input_i"')
    if anchor < 0:
        raise ValueError("没有测量输出")
    start = text.rfind("{", 0, anchor)
    end = text.find("}", anchor)
    if start < 0 or end < 0:
        raise ValueError("测量输出格式不对")
    return json.loads(text[start : end + 1])


def _number(raw: object) -> float | None:
    """loudnorm 把无声写成 "-inf"，统一成 None。"""
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("-inf"), float("inf")):  # NaN / ±inf
        return None
    return value


async def _measure(path: Path, start: float, duration: float) -> dict:
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("找不到 ffmpeg")

    args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-i", str(path)]
    # -ss/-t 放在 -i 之后是「精确解码定位」：短视频多花零点几秒，换来区间对得上
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    if duration > 0:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-map",
        "0:a:0",
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("测量超时") from None

    text = (stderr or b"").decode("utf-8", "ignore")
    if process.returncode not in (0, None) and '"input_i"' not in text:
        detail = text.strip().splitlines()[-1][:200] if text.strip() else "未知错误"
        raise RuntimeError(detail)

    payload = _parse_loudnorm(text)
    return {
        "integrated": _number(payload.get("input_i")),
        "peak": _number(payload.get("input_tp")),
        "lra": _number(payload.get("input_lra")),
        "threshold": _number(payload.get("input_thresh")),
    }

async def loudness_measure(request: web.Request) -> web.Response:  # noqa: ANN001
    asset_id = request.query.get("assetId", "")
    if not ASSET_ID_RE.match(asset_id):
        return web.json_response({"error": "invalid assetId"}, status=400)

    source = _asset_path(asset_id)
    if source is None or not source.is_file():
        return web.json_response({"error": "素材还没上传到服务端"}, status=404)

    try:
        start = max(0.0, float(request.query.get("start", "0") or 0))
    except ValueError:
        start = 0.0
    try:
        duration = max(0.0, float(request.query.get("duration", "0") or 0))
    except ValueError:
        duration = 0.0

    stat = source.stat()
    key = (asset_id, stat.st_size, int(stat.st_mtime), round(start, 2), round(duration, 2))
    cached = _cache.get(key)
    if cached is not None:
        return web.json_response({**cached, "cached": True})

    # 没有音轨就没有响度可测（图片、无声视频、已分离音频的画面）：
    # 先探测再跑 ffmpeg，报错文案才说得清
    try:
        streams = await _probe_streams(source)
    except Exception:  # noqa: BLE001 - 探测失败就交给 ffmpeg 兜底
        streams = {"audio"}
    if "audio" not in streams:
        return web.json_response({"error": "素材没有音轨"}, status=422)

    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_PARALLEL)

    async with _semaphore:
        try:
            measured = await _measure(source, start, duration)
        except RuntimeError as exc:
            _log.info(f"[loudness] 测量失败 {asset_id}: {exc}")
            return web.json_response({"error": str(exc)}, status=422)

    payload = {
        "assetId": asset_id,
        "start": round(start, 2),
        "duration": round(duration, 2),
        **measured,
    }
    if len(_cache) > _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = payload
    _log.info(
        f"[loudness] {asset_id} {start:.2f}s+{duration:.2f}s → "
        f"{payload['integrated']} LUFS"
    )
    return web.json_response({**payload, "cached": False})


def register_routes(routes) -> None:  # noqa: ANN001 - aiohttp RouteTableDef
    routes.get("/yuancut/api/loudness")(loudness_measure)
