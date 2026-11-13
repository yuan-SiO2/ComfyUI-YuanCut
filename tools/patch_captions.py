"""字幕文本清洗补丁：去掉转写结果里的 [0.12] / [S01] / 【音效】这类标注。

带说话人/时间戳的转写模型会把标记写进 segment.text；
原来只有「整段恰好是一个 [..]」才被当成噪声，多个标记连在一起就当成正文，
生成出来的字幕会变成 "[0.12][S01][0.34][8.84][S01][9.00]"。

用法（build_ui.sh 里调用）：
    YUANCUT_WORK=/path/to/checkout python3 tools/patch_captions.py
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ["YUANCUT_WORK"]).resolve()
SRC = ROOT / "apps" / "web" / "src"
TAB = "\t"


def tabsify(block: str) -> str:
    out = []
    for raw in block.strip("\n").split("\n"):
        stripped = raw.lstrip(" ")
        depth = (len(raw) - len(stripped)) // 2
        out.append(TAB * depth + stripped)
    return "\n".join(out)


CAPTION_LIB = SRC / "lib" / "transcription" / "caption.ts"
_cap = CAPTION_LIB.read_text(encoding="utf-8")

if "stripCaptionMarkup" not in _cap:
    helper = tabsify(
        """
/** 转写结果里常混着 [0.12] 时间、[S01] 说话人、【音效】这类标注。 */
const MARKUP_PATTERN = /\\[[^\\]]*\\]|\\([^)]*\\)|【[^】]*】|（[^）]*）/gu;

export function stripCaptionMarkup({ text }: { text: string }): string {
  return text
    .replace(MARKUP_PATTERN, " ")
    .replace(/\\s+/g, " ")
    .replace(/(?<=[\\u4e00-\\u9fa5]) (?=[\\u4e00-\\u9fa5])/gu, "")
    .trim();
}
"""
    )
    if "const NOISE_PATTERN =" not in _cap:
        raise SystemExit("caption.ts: NOISE_PATTERN 锚点没找到")
    _cap = _cap.replace(
        "const NOISE_PATTERN =", helper + "\n\nconst NOISE_PATTERN =", 1
    )

    old_body = (
        "\t\tconst text = segment.text.trim();\n"
        "\t\tif (isNoiseText({ text })) continue;"
    )
    new_body = (
        "\t\t// 先去掉 [0.12] / [S01] / 【音效】这类标注，否则这些标记会变成字幕正文\n"
        "\t\tconst text = stripCaptionMarkup({ text: segment.text });\n"
        "\t\tif (!text || isNoiseText({ text })) continue;"
    )
    if old_body not in _cap:
        raise SystemExit("caption.ts: 段文本清洗锚点没找到")
    _cap = _cap.replace(old_body, new_body, 1)
    CAPTION_LIB.write_text(_cap, encoding="utf-8")
    print(f"  patched {CAPTION_LIB.relative_to(ROOT)} (字幕文本清洗)")

# 导入字幕（粘贴文本）走同一套清洗
TRANSCRIPTION_IMPORT = SRC / "lib" / "transcription" / "import.ts"
_ti = TRANSCRIPTION_IMPORT.read_text(encoding="utf-8")
if "stripCaptionMarkup" not in _ti:
    _ti = _ti.replace(
        'import { i18next } from "@/lib/i18n";',
        'import { i18next } from "@/lib/i18n";\n'
        'import { stripCaptionMarkup } from "@/lib/transcription/caption";',
        1,
    )
    old_lines = (
        "\t\t.split(/\\r\\n?|\\n/)\n"
        "\t\t.map((line) => line.trim())\n"
        "\t\t.filter(Boolean);"
    )
    new_lines = (
        "\t\t.split(/\\r\\n?|\\n/)\n"
        "\t\t.map((line) => stripCaptionMarkup({ text: line }))\n"
        "\t\t.filter(Boolean);"
    )
    if old_lines not in _ti:
        raise SystemExit("import.ts: parseCaptionLines 锚点没找到")
    _ti = _ti.replace(old_lines, new_lines, 1)
    TRANSCRIPTION_IMPORT.write_text(_ti, encoding="utf-8")
    print(f"  patched {TRANSCRIPTION_IMPORT.relative_to(ROOT)} (导入字幕清洗)")
