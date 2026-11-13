#!/usr/bin/env bash
# Rebuild yuancut-ui/ from the upstream web app source checkout.
#
#   tools/build_ui.sh /path/to/webapp-source
#
# The source checkout is never modified: everything happens in a temp copy.
set -euo pipefail

APP_SRC=${1:?usage: tools/build_ui.sh /path/to/webapp-source}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
APP_SRC=$(cd "$APP_SRC" && pwd)

for bin in pnpm rsync python3; do
  command -v "$bin" >/dev/null || { echo "missing required tool: $bin" >&2; exit 1; }
done

WORK=$(mktemp -d "${TMPDIR:-/tmp}/yuancut-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

echo "==> copying web app source to $WORK/yuancut"
rsync -a --exclude node_modules --exclude .git --exclude .next --exclude out \
  "$APP_SRC"/ "$WORK/yuancut"/

echo "==> building Sonic wasm (变速不变调引擎)"
bash "$HERE/tools/sonic/build_sonic_wasm.sh"

echo "==> applying ComfyUI /yuancut patches"
YUANCUT_WORK="$WORK/yuancut" python3 "$HERE/tools/patch_webapp.py"
echo "==> applying caption text cleanup patch"
YUANCUT_WORK="$WORK/yuancut" python3 "$HERE/tools/patch_captions.py"

echo "==> applying time-stretch / tempo patches"
YUANCUT_WORK="$WORK/yuancut" python3 "$HERE/tools/patch_time_stretch.py"
YUANCUT_WORK="$WORK/yuancut" python3 "$HERE/tools/patch_sonic_player.py"

echo "==> installing dependencies (pnpm)"
# 上游仓库原本用 bun，没有 pnpm-workspace.yaml；pnpm 11 找不到 workspace 定义会直接报
# ERR_PNPM_WORKSPACE_PKG_NOT_FOUND（@cutia/env 之类的 workspace 依赖解析不到）。
if [ ! -f "$WORK/yuancut/pnpm-workspace.yaml" ]; then
  printf 'packages:\n  - "apps/*"\n  - "packages/*"\n' > "$WORK/yuancut/pnpm-workspace.yaml"
fi
# --ignore-scripts: sharp / onnxruntime postinstalls are not needed for a static
# export and can fail on machines without npm; nothing in the tree needs them.
# verify-deps-before-run=false: 避免 pnpm exec 时又触发一次自动安装。
(cd "$WORK/yuancut" && PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN=false \
  pnpm install --reporter=append-only --ignore-scripts)

echo "==> building static export"
(
  cd "$WORK/yuancut/apps/web"
  NEXT_TELEMETRY_DISABLED=1 \
  UPSTASH_REDIS_REST_URL="http://127.0.0.1:8079" \
  UPSTASH_REDIS_REST_TOKEN="build-only" \
  PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN=false \
  pnpm exec next build
)

echo "==> pruning build-only assets"
# locales 只在构建期用于生成文案，运行时不会请求；preview 是官网素材
rm -rf "$WORK/yuancut/apps/web/out/locales" "$WORK/yuancut/apps/web/out/preview"

test -f "$WORK/yuancut/apps/web/out/en/editor/index.html" || {
  echo "build did not produce out/en/editor/index.html" >&2
  exit 1
}

echo "==> installing into $HERE/yuancut-ui"
mkdir -p "$HERE/yuancut-ui"
rsync -a --delete "$WORK/yuancut/apps/web/out/" "$HERE/yuancut-ui/"

echo "==> done: $(du -sh "$HERE/yuancut-ui" | cut -f1) in yuancut-ui/"
