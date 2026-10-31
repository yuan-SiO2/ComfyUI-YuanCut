"""服务端渲染：把时间线编译成 ffmpeg filter_complex 并出片。

浏览器只负责「把素材传上来 + 描述时间线」，渲染/编码全在 ComfyUI 侧用 ffmpeg 做：
  * 不再经过 WebCodecs → 没有黑屏、花屏、卡百分比
  * 20 线程 + libx264/NVENC，实测 1080p 双轨合成 326 fps

接口：
  POST /yuancut/api/asset/begin                          -> {"token": ...}
  POST /yuancut/api/asset/part?token=&index=              分片上传素材
  POST /yuancut/api/asset/finish?token=&assetId=&ext=mp4  落到 cache/<assetId>
  POST /yuancut/api/render                                时间线 JSON -> MP4
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
# 与前端 text-node.ts 保持一致：fontSize 是相对值，按画布高度换算
FONT_SIZE_SCALE_REFERENCE = 90

H264_COLOR_BSF = (
    "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
    "matrix_coefficients=1:video_full_range_flag=0"
)

_upload_tokens: dict[str, Path] = {}


# ------------------------------------------------------------------ 目录与工具
def _output_root() -> Path:
    try:
        import folder_paths  # type: ignore

        return Path(folder_paths.get_output_directory())
    except Exception:  # pragma: no cover
        return Path(tempfile.gettempdir())


def _cache_dir() -> Path:
    target = _output_root() / "YuanCut" / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ffmpeg_binary() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return str(candidate)
    return None


async def _probe_streams(path: Path) -> set[str]:
    """用 ffprobe 判断素材里到底有哪些流（避免对无声视频取 :a 而报错）。"""
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    kinds = {line.strip() for line in (out or b"").decode().splitlines() if line.strip()}
    return kinds


_ASSET_CHANNELS: dict[str, int] = {}


async def _probe_audio_channels(path: Path) -> int:
    """第一条音轨的声道数（0 = 没有音轨）。上混策略要用它，免得单声道被 -3 dB。"""
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    process = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels", "-of", "default=nw=1:nk=1",
        str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    try:
        return int((out or b"0").decode().strip() or 0)
    except ValueError:
        return 0


async def _probe_duration(path: Path) -> float:
    """读容器时长（秒）。分段渲染要靠它判断这一段是不是被截断。"""
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    try:
        return float((out or b"0").decode().strip() or 0.0)
    except ValueError:
        return 0.0


def _asset_path(asset_id: str) -> Path | None:
    """按 assetId 找素材：不限定扩展名（上传时有些 File.name 没有后缀）。"""
    if not ASSET_ID_RE.match(asset_id):
        return None

    direct = _cache_dir() / asset_id
    if direct.is_file():
        return direct

    for candidate in sorted(_cache_dir().glob(f"{asset_id}.*")):
        if candidate.is_file():
            return candidate

    return None


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _element_rate(element: dict) -> float:
    """读取元素的播放倍速（前端字段名兼容 playbackRate / rate / speed）。"""
    for key in ("playbackRate", "rate", "speed"):
        if key in element:
            rate = _num(element.get(key), 1.0)
            if rate > 0:
                return max(0.1, min(10.0, rate))
    return 1.0


def _element_keeps_pitch(element: dict) -> bool:
    """默认保音高；显式 false 时走"磁带效果"（音高随速度变）。"""
    value = element.get("preservesPitch")
    return True if value is None else bool(value)


_FILTER_AVAILABILITY: dict[str, bool] = {}


def _has_filter(name: str) -> bool:
    """缓存探测 ffmpeg 是否带某个滤镜（rubberband 是可选编译项）。"""
    cached = _FILTER_AVAILABILITY.get(name)
    if cached is not None:
        return cached

    available = False
    ffmpeg = _ffmpeg_binary()
    if ffmpeg:
        try:
            probe = subprocess.run(
                [ffmpeg, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            available = (
                re.search(rf"^\s*\S+\s+{re.escape(name)}\s", probe.stdout or "", re.M)
                is not None
            )
        except Exception:  # pragma: no cover - ffmpeg 不可用时退回 atempo
            available = False
    _FILTER_AVAILABILITY[name] = available
    return available


def _audio_tempo_filter(rate: float, *, keep_pitch: bool) -> str:
    """音频变速滤镜链。

    优先 rubberband（时间伸缩质量最好，且能同时控音高）；没有就退回 atempo
    串联（ffmpeg 内置，单级只支持 0.5~2.0）。两者都不会像 asetrate/重采样那样
    把音色拉尖变闷，这是"变速不变调"的关键。
    """
    if abs(rate - 1.0) < 1e-3:
        return ""

    if _has_filter("rubberband"):
        pitch = 1.0 if keep_pitch else rate
        return (
            f"rubberband=tempo={rate:.6f}:pitch={pitch:.6f}"
            f":formant={'preserved' if keep_pitch else 'shifted'}"
        )

    # 没有 rubberband：只能保音高（atempo 不支持变调），显式变调需求降级处理
    chain: list[str] = []
    value = rate
    while value > 2.0:
        chain.append("atempo=2.0")
        value /= 2.0
    while value < 0.5:
        chain.append("atempo=0.5")
        value /= 0.5
    chain.append(f"atempo={value:.6f}")
    return ",".join(chain)


AUDIO_RATE = 48000


def _audio_chain(
    tracks: list[dict], inputs: dict[str, int], caps: dict[str, set[str]]
) -> tuple[list[str], str]:
    """时间线的整条音轨滤镜链（只出音频，不出画面）。

    预览是 WebAudio 把每段声音直接相加，所以这里也必须是「逐样本相加」：
    统一重采样到 48 kHz → 各自的 trim/变速/音量/延时 → amix(normalize=0)。
    整条时间线一次算完、一次编码，避免分段各自编码 AAC 再拼接。
    """
    parts: list[str] = []
    labels: list[str] = []
    index = 0
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("type") not in ("video", "audio"):
                continue
            if element.get("hidden") or element.get("muted"):
                continue
            asset_id = element.get("assetId")
            stream = inputs.get(asset_id) if asset_id else None
            if stream is None:
                continue
            if "audio" not in caps.get(asset_id, set()):
                continue
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            trim_start = _num(element.get("trimStart"))
            volume = _num(element.get("volume"), 1.0)
            if length <= 0:
                continue
            label = f"a{index}"
            # 变速：源取 length*rate 秒再时间伸缩回 length 秒；默认保音高（不变调）
            rate = _element_rate(element)
            source_length = length * rate if rate != 1.0 else length
            tempo = _audio_tempo_filter(
                rate, keep_pitch=_element_keeps_pitch(element)
            )
            delay = int(round(start * 1000))
            # 单声道素材：显式 1.0 复制到左右（预览里 WebAudio 就是这么上混的）。
            # 交给 aformat 自动上混会按 0.707/声道算，导出比预览整体低 3 dB。
            upmix = "pan=stereo|c0=c0|c1=c0," if _ASSET_CHANNELS.get(asset_id or "") == 1 else ""
            parts.append(
                f"[{stream}:a]atrim=start={trim_start:.3f}:duration={source_length:.3f},"
                f"asetpts=PTS-STARTPTS,aresample={AUDIO_RATE},"
                f"{upmix}"
                f"{tempo + ',' if tempo else ''}"
                f"volume={volume:.3f},"
                f"adelay=delays={delay}:all=1[{label}]"
            )
            labels.append(f"[{label}]")
            index += 1

    if not labels:
        return [], ""
    parts.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
        + f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"
        + "[aout]"
    )
    return parts, "aout"


# --------------------------------------------------------------- filter 编译
class CompileError(Exception):
    pass


def _build_filter(
    timeline: dict,
    inputs: dict[str, int],
    caps: dict[str, set[str]],
    output_size: tuple[int, int] | None = None,
    include_audio: bool = True,
) -> tuple[str, str, str]:
    """返回 (filter_complex, video_out_label, audio_out_label)。"""
    canvas = timeline.get("canvas") or {}
    canvas_w = int(canvas.get("width") or 1920)
    canvas_h = int(canvas.get("height") or 1080)
    fps = int(timeline.get("fps") or 30)

    # 直接按「输出分辨率」合成：素材、文字、位置都一次性缩放到目标分辨率，
    # 避免「先按画布 960x544 合成、最后整体拉大到 2K」造成的画质损失。
    if output_size:
        width, height = int(output_size[0]), int(output_size[1])
    else:
        width, height = canvas_w, canvas_h
    # 画布坐标 → 输出坐标 的换算系数（位置/字号按这个等比放大）
    sxs = width / max(1, canvas_w)
    sys_ = height / max(1, canvas_h)
    duration = _num(timeline.get("duration"), 0)

    parts: list[str] = []
    # 0 号输入是黑色底图
    parts.append(
        f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}[canvas0]"
    )
    current = "canvas0"
    index = 0

    tracks = timeline.get("tracks") or []
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("hidden"):
                continue
            kind = element.get("type")
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            if length <= 0:
                continue

            if kind == "color":
                color = str(element.get("color") or "#000000").replace("#", "0x")
                parts.append(
                    f"color=c={color}:s={width}x{height}:r={fps}:d={length:.3f}"
                    f",setpts=PTS-STARTPTS+{start:.3f}/TB[c{index}]"
                )
                parts.append(
                    f"[{current}][c{index}]overlay=0:0:eof_action=pass"
                    f":enable='between(t,{start:.3f},{start + length:.3f})'[o{index}]"
                )
                current = f"o{index}"
                index += 1
                continue

            if kind == "text":
                continue

            # 只有视频/图片元素参与画面合成（音频元素只进音轨）
            if kind not in ("video", "image"):
                continue

            asset_id = element.get("assetId")
            if not asset_id:
                continue
            stream = inputs.get(asset_id)
            if stream is None:
                raise CompileError(f"素材未上传：{asset_id}")
            if "video" not in caps.get(asset_id, set()):
                continue

            trim_start = _num(element.get("trimStart"))
            scale = _num(element.get("scale"), 1.0) or 1.0
            opacity = max(0.0, min(1.0, _num(element.get("opacity"), 1.0)))
            pos_x = _num(element.get("x"))
            pos_y = _num(element.get("y"))

            label = f"v{index}"
            # 变速：先按 length*rate 取源，再用 setpts 压回 length 秒
            rate = _element_rate(element) if kind == "video" else 1.0
            source_length = length * rate if rate != 1.0 else length
            if rate != 1.0:
                timing = (
                    f"setpts=(PTS-STARTPTS)/{rate:.6f}+{start:.3f}/TB,fps={fps}"
                )
            else:
                timing = f"setpts=PTS-STARTPTS+{start:.3f}/TB"
            # 先把素材按画布「铺满再居中裁切」（与预览的 cover 一致），
            # 再套用户的 transform 缩放。之前只做 transform 缩放，
            # 素材原始尺寸和画布不一致时就会被 overlay 裁掉/缩小。
            parts.append(
                f"[{stream}:v]trim=start={trim_start:.3f}:duration={source_length:.3f},"
                f"{timing},"
                # 与预览一致：按「适应画面」缩放（不裁切），黑边由底图透出来；
                # 之前用 increase+crop 是「铺满裁切」，素材比例和画布不同时就会
                # 看起来被放大/裁掉一截。
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                # 上面的 scale 已经把素材「适应」到输出尺寸，这里的 transform.scale
                # 只是在输出像素上再乘一次；再乘 sxs/sys_ 会把缩放算两遍
                # （画布 ≠ 输出时，例如 2560x1440 画布导出 1080p，等于又缩 0.75）。
                + (
                    ""
                    if abs(scale - 1.0) < 0.001
                    else f"scale=iw*{scale:.4f}:ih*{scale:.4f},"
                )
                + "format=rgba,"
                f"colorchannelmixer=aa={opacity:.3f}[{label}]"
            )
            parts.append(
                f"[{current}][{label}]overlay="
                f"x=(W-w)/2+{pos_x * sxs:.1f}:y=(H-h)/2+{pos_y * sys_:.1f}"
                f":eof_action=pass:enable='between(t,{start:.3f},{start + length:.3f})'"
                f"[o{index}]"
            )
            current = f"o{index}"
            index += 1

    # 文字：在主画面之后画 drawtext
    text_index = 0
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("type") != "text" or element.get("hidden"):
                continue
            content = str(element.get("content") or "").strip()
            if not content:
                continue
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            if length <= 0:
                continue

            # 应用里的 fontSize 是相对值（字幕默认 5），按画布高度换算成像素
            relative_size = _num(element.get("fontSize"), 5) or 5
            font_size = max(8, round(relative_size * height / FONT_SIZE_SCALE_REFERENCE))
            # 按画布宽度换行，避免切比例后字幕横向溢出画面
            lines = _wrap_text_for_canvas(
                text=content,
                font_size=font_size,
                max_width=max(16.0, width * 0.90),
            )
            text_file = _write_text_file("\n".join(lines))
            color = str(element.get("color") or "#ffffff")
            if not color.startswith("#"):
                color = "#ffffff"
            pos_x = int(width / 2 + _num(element.get("x")) * sxs)
            pos_y = int(height / 2 + _num(element.get("y")) * sys_)
            # 文本块垂直夹紧，保证不出画面
            block_height = font_size * 1.3 * len(lines)
            if block_height * 2 <= height:
                pos_y = max(
                    int(block_height / 2),
                    min(pos_y, int(height - block_height / 2)),
                )
            else:
                pos_y = int(height / 2)

            parts.append(
                f"[{current}]drawtext=textfile='{text_file}'"
                f":fontsize={font_size}:fontcolor={color}"
                f":line_spacing={max(2, round(font_size * 0.3))}"
                f":x={pos_x}-text_w/2:y={pos_y}-text_h/2"
                f":enable='between(t,{start:.3f},{start + length:.3f})'[t{text_index}]"
            )
            current = f"t{text_index}"
            text_index += 1

    # 合成已经是输出尺寸，这里只统一像素格式（不再做二次放大）
    parts.append(f"[{current}]setsar=1,format=yuv420p[scaled]")
    current = "scaled"

    video_out = current
    audio_out = ""
    if include_audio:
        audio_parts, audio_out = _audio_chain(tracks, inputs, caps)
        parts.extend(audio_parts)
    return ";".join(parts), video_out, audio_out


def _text_char_width(char: str, font_size: int) -> float:
    """粗略估算单字符宽度：CJK 按 1em，ASCII 按 0.55em。"""
    code = ord(char)
    if code >= 0x2E80:
        return font_size * 1.0
    if char == " ":
        return font_size * 0.3
    if char in "iljI.,:;'!|":
        return font_size * 0.3
    if char.isupper() or char in "mwMW@#%&":
        return font_size * 0.72
    return font_size * 0.55


def _wrap_text_for_canvas(
    *, text: str, font_size: int, max_width: float
) -> list[str]:
    """按画布宽度折行，与前端画布渲染的换行行为保持一致。"""
    lines: list[str] = []

    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue

        current = ""
        current_width = 0.0
        for char in paragraph:
            char_width = _text_char_width(char, font_size)
            if current and current_width + char_width > max_width:
                lines.append(current)
                current = char
                current_width = char_width
            else:
                current += char
                current_width += char_width

        if current:
            lines.append(current)

    return lines or [""]


def _write_text_file(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return handle.name.replace("\\", "/").replace(":", "\\\\:")


async def _prepare_inputs(
    timeline: dict,
) -> tuple[dict[str, int], list[str], dict[str, set[str]]] | str:
    """把时间线用到的素材摊成 ffmpeg 的 -i 参数，并探测每条流。

    返回 str 表示出错（消息直接给用户看）。
    """
    assets: list[str] = []
    for track in timeline.get("tracks") or []:
        for element in track.get("elements") or []:
            asset_id = element.get("assetId")
            if asset_id and asset_id not in assets:
                assets.append(asset_id)

    inputs: dict[str, int] = {}
    input_args: list[str] = []
    paths: dict[str, Path] = {}
    for asset_id in assets:
        path = _asset_path(asset_id)
        if path is None:
            return f"素材未上传：{asset_id}"
        inputs[asset_id] = len(inputs)
        input_args += ["-i", str(path)]
        paths[asset_id] = path

    caps: dict[str, set[str]] = {}
    for asset_id, path in paths.items():
        caps[asset_id] = await _probe_streams(path)
        _ASSET_CHANNELS[asset_id] = await _probe_audio_channels(path)
    return inputs, input_args, caps

async def _run_render_once(
    timeline: dict,
    work: Path | None = None,
    job: dict | None = None,
    progress_window: tuple[float, float] = (0.0, 1.0),
    include_audio: bool = True,
) -> tuple[Path | None, str | None]:
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return None, "本机找不到 ffmpeg"

    prepared = await _prepare_inputs(timeline)
    if isinstance(prepared, str):
        return None, prepared
    inputs, input_args, caps = prepared

    canvas = timeline.get("canvas") or {}
    width = int(canvas.get("width") or 1920)
    height = int(canvas.get("height") or 1080)
    output_cfg = timeline.get("output") or {}
    out_w = int(output_cfg.get("width") or width)
    out_h = int(output_cfg.get("height") or height)

    try:
        filter_complex, video_out, audio_out = _build_filter(
            timeline, inputs, caps, (out_w, out_h), include_audio=include_audio
        )
    except CompileError as exc:
        return None, str(exc)

    duration = _num(timeline.get("duration"), 0)
    fps = int(timeline.get("fps") or 30)
    profile = timeline.get("profile") or {}

    work_dir = work or Path(tempfile.mkdtemp(prefix="yuancut-render-"))
    output = work_dir / "out.mp4"

    # 调整 / 滤镜：客户端把参数压成一份 .cube 随请求带来，这里只加一步 lut3d。
    # 预览用的是同一份 cube，所以两边不会出现两套公式。
    lut_text = timeline.get("lut")
    if isinstance(lut_text, str) and lut_text.strip():
        cube_path = work_dir / "grade.cube"
        try:
            cube_path.write_text(lut_text, encoding="utf-8")
            escaped = str(cube_path).replace("\\", "/").replace("'", "'\\''")
            filter_complex = (
                f"{filter_complex};[{video_out}]lut3d='{escaped}'[v_lut]"
            )
            video_out = "v_lut"
        except OSError:
            pass
    total_frames = max(1, int(duration * fps))

    # 分段渲染已经把内存按「片长」切开了，这里不再限制滤镜/编码线程，
    # 让 ffmpeg 吃满 CPU（之前为了压内存串行化，速度掉得厉害）。
    # 编码参数：NVENC 用 p5/vbr/cq，libx264 用 preset/crf
    # 没显式指定编码器时，有 NVENC 就默认走硬件编码（RTX 30 系实测可用）
    _vcodec = str(profile.get("vcodec") or "libx264")
    if "nvenc" in _vcodec:
        _encoder_args = [
            "-preset", "p5", "-tune", "hq", "-rc", "vbr",
            "-cq", str(int(profile.get("crf") or 19) * 2), "-b:v", "0",
        ]
    else:
        _encoder_args = [
            "-preset", str(profile.get("preset") or "veryfast"),
            "-crf", str(int(profile.get("crf") or 19)),
        ]

    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *input_args,
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{video_out}]",
        *(
            ["-map", f"[{audio_out}]", "-c:a", "aac", "-ar", str(AUDIO_RATE),
             "-b:a", str(profile.get("audioBitrate") or "192k")]
            if audio_out
            else ["-an"]
        ),
        "-c:v",
        _vcodec,
        *_encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(int(profile.get("threads") or 0)),
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(output),
    ]

    args[1:1] = ["-progress", "pipe:1", "-nostats"]

    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if job is not None:
        job["process"] = process

    async def _drain_progress() -> bytes:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("frame=") and job is not None:
                try:
                    done_frames = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                ratio = min(0.99, done_frames / total_frames)
                base, span = progress_window
                job["progress"] = min(0.985, base + ratio * span)
        return b""

    assert process.stderr is not None
    _, stderr = await asyncio.gather(_drain_progress(), process.stderr.read())
    await process.wait()  # 不 wait 的话 returncode 还是 None，会被误判成失败
    detail = (stderr or b"").decode("utf-8", "replace").strip()
    produced = output.is_file() and output.stat().st_size > 0

    if process.returncode != 0 or not produced:
        # 只有 returncode 明确为 0 且文件非空才算成功。以前「文件存在就算成功」
        # 把被中途杀掉的短段也当成功，拼出比时间线短一半的成片。
        logger.error(
            "[YuanCut] ffmpeg 渲染失败 rc=%s stderr=%s",
            process.returncode,
            detail[-1500:] or "(空)",
        )
        reason = detail[-600:] or (
            f"ffmpeg 被异常终止（返回码 {process.returncode}），"
            "stderr 为空，通常是内存或磁盘不足"
        )
        return None, f"ffmpeg 渲染失败：{reason}"

    # 补 bt709 色彩标记（ffmpeg 滤镜链常丢 primaries/trc）
    fixed = output.with_name("fixed.mp4")
    fix_args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(output),
        "-c",
        "copy",
        "-bsf:v",
        H264_COLOR_BSF,
        str(fixed),
    ]
    process = await asyncio.create_subprocess_exec(
        *fix_args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, fix_err = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "[YuanCut] 色彩标记二次封装失败（不影响出片）：%s",
            (fix_err or b"").decode("utf-8", "replace")[-400:],
        )

    return (fixed if fixed.is_file() else output), None


# ------------------------------------------------------------------- handlers
# ------------------------------- 分段（流式）渲染 -------------------------------
# 一次把整条时间线塞进 ffmpeg：内存 = 轨道数 × 线程数 × 帧缓冲，长项目能到几十 GB。
# 剪映的做法是单条流式管线；这里用「按 30 秒切片分别渲染 + concat 无损拼接」达到
# 同样的内存曲线（每片渲染完就释放，峰值只跟片长有关，跟总时长/轨道数无关）。

SEGMENT_SECONDS = 30.0


def _segment_timeline(timeline: dict, start: float, length: float) -> dict:
    """裁出 [start, start+length) 这段：时间平移、trim 前移、窗外片段丢弃。"""
    tracks: list[dict] = []
    for track in timeline.get("tracks") or []:
        elements: list[dict] = []
        for element in track.get("elements") or []:
            element_start = _num(element.get("start"))
            element_length = _num(element.get("duration"))
            if element_length <= 0:
                continue
            window_end = start + length
            if element_start + element_length <= start + 0.001 or element_start >= window_end - 0.001:
                continue
            keep_from = max(0.0, start - element_start)
            local_length = min(element_start + element_length, window_end) - max(element_start, start)
            if local_length <= 0.01:
                continue
            copy = dict(element)
            copy["start"] = max(0.0, element_start - start)
            copy["duration"] = local_length
            if "trimStart" in element:
                rate = _element_rate(element) if element.get("type") == "video" else 1.0
                copy["trimStart"] = _num(element.get("trimStart")) + keep_from * rate
            elements.append(copy)
        tracks.append({**track, "elements": elements})
    return {**timeline, "duration": length, "tracks": tracks}


async def _concat_segments(files: list[Path], output: Path) -> str | None:
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return "本机找不到 ffmpeg"
    listing = output.parent / "segments.txt"
    listing.write_text(
        "".join(f"file '{item.as_posix()}'\n" for item in files), encoding="utf-8"
    )
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", str(output),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if not output.is_file():
        return stderr.decode("utf-8", "replace")[-500:] or "concat 失败"
    return None


async def _render_audio_pass(
    timeline: dict, work_dir: Path
) -> tuple[Path | None, str | None]:
    """把整条时间线的音频一次算成 48 kHz PCM。

    这是「声音破音」的根治点：以前每 30 秒一段各自编码 AAC，再用
    `concat -c copy` 拼起来 —— AAC 是带编码器状态/预热帧的有状态编码，
    跨独立编码的码流拼接会让解码器从第二段开始报错（实测 60 s 内 800+ 次
    「channel element not allocated」），听感就是全程破音、渣噪。
    现在音频只在最后统一编码一次，段与段之间不存在任何接缝。
    """
    duration = _num(timeline.get("duration"), 0)
    if duration <= 0:
        return None, "时间线时长为 0"

    prepared = await _prepare_inputs(timeline)
    if isinstance(prepared, str):
        return None, prepared
    inputs, input_args, caps = prepared

    audio_parts, audio_out = _audio_chain(timeline.get("tracks") or [], inputs, caps)
    if not audio_out:
        return None, None  # 没有声音轨，不用生成音频

    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return None, "本机找不到 ffmpeg"

    output = work_dir / "audio.wav"
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *input_args,
        "-filter_complex", ";".join(audio_parts),
        "-map", f"[{audio_out}]",
        "-c:a", "pcm_s16le",
        "-ar", str(AUDIO_RATE),
        "-ac", "2",
        "-t", f"{duration:.3f}",
        str(output),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if not output.is_file() or output.stat().st_size == 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        return None, f"音频渲染失败：{detail[-500:] or 'ffmpeg 无输出'}"
    return output, None


async def _mux_audio(
    video: Path,
    audio: Path,
    output: Path,
    duration: float,
    profile: dict,
) -> str | None:
    """把成片视频（已拼接、无音轨）和整条音频封装一次。"""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return "本机找不到 ffmpeg"
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-ar", str(AUDIO_RATE),
        "-b:a", str(profile.get("audioBitrate") or "192k"),
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        str(output),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if not output.is_file() or output.stat().st_size == 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        return detail[-500:] or "封装失败"
    return None


async def _run_render(
    timeline: dict, work: Path | None = None, job: dict | None = None
) -> tuple[Path | None, str | None]:
    duration = _num(timeline.get("duration"), 0)
    work_dir = work or Path(tempfile.mkdtemp(prefix="yuancut-render-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1) 音频：整条时间线一次算完（不分段、只编码一次 AAC）
    audio_path, audio_error = await _render_audio_pass(timeline, work_dir)
    if audio_error:
        return None, audio_error

    # 2) 画面：按 30 秒分段流式渲染（内存只跟片长有关），段内不处理音频
    segments: list[Path] = []
    index = 0
    start = 0.0
    total_segments = max(1, int(math.ceil(duration / SEGMENT_SECONDS)))
    while start < duration - 0.01:
        if job is not None and job.get("cancelled"):
            return None, "已取消"
        length = min(SEGMENT_SECONDS, duration - start)
        piece = _segment_timeline(timeline, start, length)
        piece["no_segment"] = True
        piece_dir = work_dir / f"seg{index:03d}"
        piece_dir.mkdir(parents=True, exist_ok=True)
        # 分段渲染最怕「ffmpeg 被杀但文件还在」：以前按成功处理，
        # 结果拼出来的成片比时间线短（535 s 的项目只剩 356 s）。
        # 这里按容器时长校验，短了就单线程重试一次，还短就直接报错，绝不悄悄交付。
        attempt = 0
        while True:
            path, error = await _run_render_once(
                piece,
                piece_dir,
                job,
                progress_window=(
                    index / max(1, total_segments),
                    1 / max(1, total_segments),
                ),
                include_audio=False,
            )
            if path is None:
                return None, f"第 {index + 1} 段渲染失败：{error}"
            produced = await _probe_duration(path)
            if produced >= length - 0.35:
                break
            if attempt >= 1:
                return None, (
                    f"第 {index + 1} 段只渲染出 {produced:.2f}s / {length:.2f}s，"
                    "ffmpeg 被中途终止（内存或磁盘不足），已停止导出"
                )
            logger.warning(
                "[YuanCut] 第 %d 段只渲染出 %.2fs / %.2fs，单线程重试一次",
                index + 1,
                produced,
                length,
            )
            attempt += 1
            piece = {**piece, "profile": {**(piece.get("profile") or {}), "threads": 1}}
        segments.append(path)
        index += 1
        start += length

    silent = work_dir / "video.mp4"
    error = await _concat_segments(segments, silent)
    if error:
        return None, f"分段拼接失败：{error}"
    for item in segments:
        try:
            item.unlink(missing_ok=True)
        except OSError:
            pass

    # 3) 封装：视频流直接 copy，音频只在这里编码一次
    output = work_dir / "out.mp4"
    if audio_path is None:
        silent.replace(output)
        return output, None

    profile = timeline.get("profile") or {}
    error = await _mux_audio(silent, audio_path, output, duration, profile)
    if error:
        return None, f"音视频封装失败：{error}"
    return output, None


async def asset_begin(request: web.Request) -> web.Response:
    token = uuid.uuid4().hex
    work = Path(tempfile.mkdtemp(prefix="yuancut-asset-"))
    _upload_tokens[token] = work
    return web.json_response({"token": token})


async def asset_part(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    index = request.query.get("index", "0")
    if not TOKEN_RE.match(token) or token not in _upload_tokens:
        return web.json_response({"error": "invalid token"}, status=400)
    if not index.isdigit():
        return web.json_response({"error": "invalid index"}, status=400)

    payload = await request.read()
    (_upload_tokens[token] / f"part.{int(index):05d}").write_bytes(payload)
    return web.json_response({"ok": True, "size": len(payload)})


async def asset_finish(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    asset_id = request.query.get("assetId", "")
    ext = request.query.get("ext", "mp4")
    if not TOKEN_RE.match(token) or token not in _upload_tokens:
        return web.json_response({"error": "invalid token"}, status=400)
    if not ASSET_ID_RE.match(asset_id):
        return web.json_response({"error": "invalid assetId"}, status=400)

    work = _upload_tokens.pop(token)
    suffix = re.sub(r"[^a-z0-9]", "", ext.lower()) or "mp4"
    target = _cache_dir() / f"{asset_id}.{suffix}"

    with target.open("wb") as out:
        for part in sorted(work.glob("part.*")):
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)
    shutil.rmtree(work, ignore_errors=True)

    return web.json_response({"ok": True, "path": str(target)})


async def _stream_mp4(request: web.Request, output: Path) -> web.StreamResponse:
    response = web.StreamResponse(
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(output.stat().st_size),
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            await response.write(chunk)
    await response.write_eof()
    return response


async def render_timeline(request: web.Request) -> web.StreamResponse:
    try:
        timeline = await request.json()
    except Exception:
        return web.json_response({"error": "时间线 JSON 非法"}, status=400)

    duration = _num(timeline.get("duration"), 0)
    tracks = timeline.get("tracks") or []
    element_count = sum(len(track.get("elements") or []) for track in tracks)

    # 诊断：把前端送来的时间线摘要写进日志，便于对齐"时间轴 vs 成片"
    type_counts: dict[str, int] = {}
    texts_in: list[str] = []
    for track in tracks:
        for element in track.get("elements") or []:
            kind = str(element.get("type") or "unknown")
            type_counts[kind] = type_counts.get(kind, 0) + 1
            if kind == "text":
                texts_in.append(str(element.get("content") or "")[:12])

    supported = {"video", "image", "text", "audio", "color"}
    skipped = {k: v for k, v in type_counts.items() if k not in supported}
    logger.info(
        "[YuanCut] 时间线：%d 轨 / %d 元素 / 时长 %.2fs / 类型 %s%s%s",
        len(tracks),
        element_count,
        duration,
        type_counts,
        f" / 跳过 {skipped}" if skipped else "",
        f" / 字幕片段 {texts_in[:5]}" if texts_in else " / 无 text 元素",
    )
    if duration <= 0:
        return web.json_response({"error": "时间线时长为 0（项目为空）"}, status=400)
    if element_count == 0:
        return web.json_response({"error": "时间线没有任何元素"}, status=400)

    output, error = await _run_render(timeline)
    if output is None:
        return web.json_response({"error": error or "渲染失败"}, status=500)

    response = web.StreamResponse(
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(output.stat().st_size),
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            await response.write(chunk)
    await response.write_eof()
    shutil.rmtree(output.parent, ignore_errors=True)
    return response


async def render_selftest(request: web.Request) -> web.StreamResponse:
    """不依赖任何素材的自检：画 2 秒彩条 + 文字 + 正弦音，验证整条服务端链路。"""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return web.json_response({"error": "本机找不到 ffmpeg"}, status=501)

    work = Path(tempfile.mkdtemp(prefix="yuancut-selftest-"))
    output = work / "selftest.mp4"
    text_file = work / "t.txt"
    text_file.write_text("YuanCut selftest", encoding="utf-8")

    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex",
        f"[0:v]drawtext=textfile='{text_file}':fontsize=64:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", "2", "-movflags", "+faststart",
        str(output),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0 or not output.is_file():
        detail = (stderr or b"").decode("utf-8", "replace")[-800:]
        shutil.rmtree(work, ignore_errors=True)
        return web.json_response(
            {"error": f"自检渲染失败：{detail}"}, status=500
        )

    data = output.read_bytes()
    shutil.rmtree(work, ignore_errors=True)
    return web.Response(
        body=data,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------- 任务式渲染
# 一次 POST 拿不到中间进度，所以改成：start 建任务 -> progress 轮询 -> result 取文件
_JOBS: dict[str, dict] = {}

#: 终态任务留这么久，够客户端回来取结果；再久就回收，别让内存和临时目录越堆越多
_JOB_TTL_SECONDS = 30 * 60


def _sweep_jobs() -> None:
    now = time.time()
    for job_id, job in list(_JOBS.items()):
        if job.get("state") not in ("done", "error", "cancelled"):
            continue
        finished = job.get("finishedAt")
        if finished is None:
            job["finishedAt"] = now
            continue
        if now - finished < _JOB_TTL_SECONDS:
            continue
        work = job.get("work")
        if isinstance(work, Path):
            shutil.rmtree(work, ignore_errors=True)
        _JOBS.pop(job_id, None)


def _job(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


async def _render_job(job_id: str, timeline: dict, work: Path) -> None:
    job = _JOBS[job_id]
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        job.update(state="error", error="本机找不到 ffmpeg")
        return

    try:
        output, error = await _run_render(timeline, work=work, job=job)
        if output is None:
            job.update(state="error", error=error or "渲染失败")
        else:
            job.update(state="done", progress=1.0, output=output)
    except Exception as exc:  # pragma: no cover
        logger.exception("[YuanCut] 渲染任务异常")
        job.update(state="error", error=str(exc))


async def render_start(request: web.Request) -> web.Response:
    _sweep_jobs()
    try:
        timeline = await request.json()
    except Exception:
        return web.json_response({"error": "时间线 JSON 非法"}, status=400)

    duration = _num(timeline.get("duration"), 0)
    tracks = timeline.get("tracks") or []
    element_count = sum(len(track.get("elements") or []) for track in tracks)
    if duration <= 0:
        return web.json_response({"error": "时间线时长为 0（项目为空）"}, status=400)
    if element_count == 0:
        return web.json_response({"error": "时间线没有任何元素"}, status=400)

    job_id = uuid.uuid4().hex
    work = Path(tempfile.mkdtemp(prefix="yuancut-job-"))
    _JOBS[job_id] = {
        "state": "running",
        "progress": 0.0,
        "error": None,
        "output": None,
        "work": work,
    }
    asyncio.create_task(_render_job(job_id, timeline, work))
    return web.json_response({"jobId": job_id})


async def render_cancel(request: web.Request) -> web.Response:
    """真正杀掉 ffmpeg 进程，取消导出。"""
    job = _job(request.query.get("jobId", ""))
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)

    job["state"] = "cancelled"
    job["error"] = "已取消"
    process = job.get("process")
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    work = job.get("work")
    if isinstance(work, Path):
        shutil.rmtree(work, ignore_errors=True)
    _JOBS.pop(request.query.get("jobId", ""), None)
    logger.info("[YuanCut] 导出已取消")
    return web.json_response({"ok": True})


async def render_progress(request: web.Request) -> web.Response:
    _sweep_jobs()
    job = _job(request.query.get("jobId", ""))
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)
    return web.json_response(
        {
            "state": job["state"],
            "progress": round(float(job["progress"]), 4),
            "error": job["error"],
        }
    )


async def render_result(request: web.Request) -> web.StreamResponse:
    job_id = request.query.get("jobId", "")
    job = _job(job_id)
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)
    if job["state"] != "done" or not job.get("output"):
        return web.json_response({"error": job.get("error") or "尚未完成"}, status=409)

    output: Path = job["output"]
    work: Path = job["work"]
    response = await _stream_mp4(request, output)
    shutil.rmtree(work, ignore_errors=True)
    _JOBS.pop(job_id, None)
    return response


def register_routes(routes) -> None:  # noqa: ANN001
    routes.post("/yuancut/api/asset/begin")(asset_begin)
    routes.post("/yuancut/api/asset/part")(asset_part)
    routes.post("/yuancut/api/asset/finish")(asset_finish)
    routes.post("/yuancut/api/render")(render_timeline)
    routes.post("/yuancut/api/render/selftest")(render_selftest)
    routes.post("/yuancut/api/render/start")(render_start)
    routes.get("/yuancut/api/render/progress")(render_progress)
    routes.post("/yuancut/api/render/cancel")(render_cancel)
    routes.get("/yuancut/api/render/result")(render_result)
