import { withBasePath } from "@/lib/base-path";
import { uploadAssetBlob } from "@/lib/export";
import type { EditorCore } from "@/core";
import type { MediaAsset } from "@/types/assets";
import type { AudioElement, VideoElement } from "@/types/timeline";

/** 参与「统一响度」的片段：音频轨的音频片段，或带音轨的视频片段 */
export type LoudnessRef = {
	element: AudioElement | VideoElement;
	trackId: string;
};

/** 音量上限：与面板滑块一致（0~2），超过就只能到 200% */
const MAX_VOLUME = 2;
/** 下限防止把某段几乎静音调成全静音 */
const MIN_VOLUME = 0.05;

function extensionOf(name: string): string {
	const match = /\.([A-Za-z0-9]{2,5})$/.exec(name);
	return (match?.[1] ?? "mp4").toLowerCase();
}

/** 真正出声的素材：勾了人声美化的视频用处理后的 wav */
function audioSourceId(
	element: AudioElement | VideoElement,
): string | undefined {
	if (element.type === "video") {
		return element.voiceBeautifyEnabled
			? (element.voiceBeautifyMediaId ?? element.mediaId)
			: element.mediaId;
	}
	// 素材库音频只有 sourceUrl、没有 mediaId，测不了响度
	return "mediaId" in element ? element.mediaId : undefined;
}

function median(values: number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const middle = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1
		? sorted[middle]
		: (sorted[middle - 1] + sorted[middle]) / 2;
}

class LoudnessRequestError extends Error {
	status: number;
	constructor(message: string, status: number) {
		super(message);
		this.status = status;
	}
}

async function requestMeasure({
	assetId,
	start,
	duration,
}: {
	assetId: string;
	start: number;
	duration: number;
}): Promise<number | null> {
	const query = new URLSearchParams({
		assetId,
		start: Math.max(0, start).toFixed(3),
		duration: Math.max(0, duration).toFixed(3),
	});
	const response = await fetch(withBasePath(`/api/loudness?${query.toString()}`), {
		cache: "no-store",
	});
	if (!response.ok) {
		const detail = (await response.json().catch(() => ({}))) as {
			error?: string;
		};
		throw new LoudnessRequestError(
			detail.error || `测量失败（${response.status}）`,
			response.status,
		);
	}
	const payload = (await response.json()) as { integrated?: number | null };
	return typeof payload.integrated === "number" ? payload.integrated : null;
}

/**
 * 测一个片段的响度（LUFS）。
 *
 * 服务端只认已经上传过的素材：404 说明素材还不在服务端，补一次上传再测；
 * 同一个区间重复测会命中服务端缓存，不会反复解码同一个文件。
 */
async function measureElement({
	asset,
	start,
	duration,
}: {
	asset: MediaAsset;
	start: number;
	duration: number;
}): Promise<number | null> {
	try {
		return await requestMeasure({ assetId: asset.id, start, duration });
	} catch (error) {
		if (!(error instanceof LoudnessRequestError) || error.status !== 404) {
			throw error;
		}
		const uploaded = await uploadAssetBlob({
			assetId: asset.id,
			blob: asset.file,
			ext: extensionOf(asset.name),
		});
		if (!uploaded) throw new Error("素材上传到服务端失败");
		return requestMeasure({ assetId: asset.id, start, duration });
	}
}

/**
 * 统一响度：把所选片段各自测一遍，按「中位数响度」算出增益写回 volume。
 *
 * 不是把滑块拖到同一个百分比 —— 每段原始声音大小本来就不同，
 * 相同百分比只会把差异原样保留。这里按实测 LUFS 分别补偿，
 * 播出来各段响度才一致。目标取中位数，整体音量不会被拉高或压低。
 */
export async function matchLoudness({
	editor,
	refs,
	onProgress,
}: {
	editor: EditorCore;
	refs: LoudnessRef[];
	onProgress?: (message: string) => void;
}): Promise<string> {
	if (refs.length < 2) {
		throw new Error("至少选两个片段才有统一的必要");
	}

	const assets = new Map(
		editor.media.getAssets().map((asset) => [asset.id, asset]),
	);
	const measured: { ref: LoudnessRef; lufs: number }[] = [];
	let skipped = 0;
	let index = 0;

	for (const ref of refs) {
		index += 1;
		onProgress?.(`测量 ${index}/${refs.length}…`);
		const element = ref.element;
		if (element.muted) {
			skipped += 1;
			continue;
		}
		const mediaId = audioSourceId(element);
		const asset = mediaId ? assets.get(mediaId) : undefined;
		if (!asset) {
			skipped += 1;
			continue;
		}
		// 变速片段听的是「源素材上更长的一段」，按倍速把测量区间补回去
		const rate = element.playbackRate ?? 1;
		const sourceDuration = element.duration * (rate > 0 ? rate : 1);
		let lufs: number | null = null;
		try {
			lufs = await measureElement({
				asset,
				start: element.trimStart ?? 0,
				duration: sourceDuration,
			});
		} catch {
			lufs = null;
		}
		if (lufs === null || !Number.isFinite(lufs)) {
			skipped += 1;
			continue;
		}
		measured.push({ ref, lufs });
	}

	if (measured.length < 2) {
		throw new Error("可测量的片段不到两个（没有音轨或测不出声音）");
	}

	const target = median(measured.map((item) => item.lufs));
	let capped = 0;
	const updates = measured.map(({ ref, lufs }) => {
		const raw = 10 ** ((target - lufs) / 20);
		const volume = Math.min(MAX_VOLUME, Math.max(MIN_VOLUME, raw));
		if (Math.abs(volume - raw) > 0.005) capped += 1;
		return {
			trackId: ref.trackId,
			elementId: ref.element.id,
			updates: { volume: Math.round(volume * 1000) / 1000 },
		};
	});

	editor.timeline.updateElements({ updates, pushHistory: true });

	const parts = [
		`已统一 ${measured.length} 个片段的响度（目标 ${target.toFixed(1)} LUFS）`,
	];
	if (skipped > 0) parts.push(`${skipped} 个没有可测的声音`);
	if (capped > 0) parts.push(`${capped} 个已到音量上限，必要时先人声美化/降噪`);
	return parts.join("；");
}
