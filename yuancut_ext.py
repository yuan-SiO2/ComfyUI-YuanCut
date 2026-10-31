"""通用「可选扩展」加载器。

插件本身不含任何具体扩展的业务代码，只做四件通用的事：

  1. 目录存在（`<插件>/ext/extension.py`）才认为扩展可用，否则一切照旧；
  2. 把扩展当包导入，调它的 `register_routes(routes)` 注册自己的接口；
  3. 把 `ext/` 下的静态文件挂到 `/yuancut/ext/<path>` 下；
  4. 编辑器页面里注入扩展自己的前端脚本（`ext/ui/ui.js`，存在才注入）。

扩展目录缺失、导入失败、注册异常都只会写一条日志，不会影响插件启动与其它功能。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
EXT_DIR = PLUGIN_DIR / "ext"
ENTRY_FILE = EXT_DIR / "extension.py"
UI_SCRIPT = EXT_DIR / "ui" / "ui.js"

_PACKAGE_NAME = "yuancut_ext_local"
_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

_module: Any | None = None
_load_error: str | None = None


def is_present() -> bool:
    return ENTRY_FILE.is_file()


def load() -> Any | None:
    """导入扩展包（幂等）。任何异常都只记录。"""
    global _module, _load_error
    if _module is not None or _load_error is not None:
        return _module
    if not is_present():
        return None
    try:
        if _PACKAGE_NAME not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                _PACKAGE_NAME,
                EXT_DIR / "__init__.py",
                submodule_search_locations=[str(EXT_DIR)],
            )
            if spec is None or spec.loader is None:
                raise ImportError("无法为扩展目录建立包说明")
            package = importlib.util.module_from_spec(spec)
            sys.modules[_PACKAGE_NAME] = package
            spec.loader.exec_module(package)
        _module = importlib.import_module(f"{_PACKAGE_NAME}.extension")
        logger.info("[YuanCut] 已加载可选扩展：%s", _module.__file__)
    except Exception as exc:  # noqa: BLE001 - 扩展坏了不能影响插件
        _load_error = f"{type(exc).__name__}: {exc}"
        logger.warning("[YuanCut] 可选扩展加载失败（忽略）：%s", _load_error)
        _module = None
    return _module


def register_routes(routes: Any) -> None:
    module = load()
    if module is None:
        return
    try:
        module.register_routes(routes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[YuanCut] 可选扩展注册接口失败（忽略）：%s", exc)


def script_tag(base_path: str = "/yuancut") -> bytes:
    """编辑器页面里要注入的 <script>；没有前端脚本就返回空。"""
    module = load()
    if module is None:
        return b""
    url: str | None = None
    try:
        getter = getattr(module, "ui_script_url", None)
        url = getter(base_path) if callable(getter) else None
    except Exception:  # noqa: BLE001
        url = None
    if not url and UI_SCRIPT.is_file():
        url = f"{base_path.rstrip('/')}/ext/ui/ui.js"
    if not url:
        return b""
    return f'<script type="module" src="{url}"></script>'.encode()


async def handle_static(request: web.Request) -> web.StreamResponse:
    """`/yuancut/ext/<path>` → `ext/<path>`（只读，防目录穿越）。"""
    if not is_present():
        return web.Response(text="Not found", status=404)
    rel = (request.match_info.get("path") or "").strip("/")
    segments = [part for part in rel.split("/") if part not in ("", ".")]
    if not segments or any(part == ".." for part in segments):
        return web.Response(text="Invalid path", status=400)
    target = EXT_DIR.joinpath(*segments)
    if not target.is_file():
        return web.Response(text="Not found", status=404)
    headers = {"Cache-Control": "no-cache"}
    content_type = _MIME.get(target.suffix.lower())
    if content_type:
        headers["Content-Type"] = content_type
    return web.FileResponse(target, headers=headers)
