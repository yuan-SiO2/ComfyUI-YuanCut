"use client";

import { useState } from "react";
import { useEditor } from "@/hooks/use-editor";
import { matchLoudness, type LoudnessRef } from "@/lib/audio/loudness";

/**
 * 「统一响度」按钮：音频 / 视频属性面板共用。
 *
 * 与人声美化那一行不同，这里不需要勾选 —— 点一次就测量所选片段并写回音量，
 * 状态里汇报目标响度和跳过的片段数。
 */
export function LoudnessMatchRow({ refs }: { refs: LoudnessRef[] }) {
	const editor = useEditor();
	const [busy, setBusy] = useState(false);
	const [status, setStatus] = useState("");

	const run = async () => {
		if (busy) return;
		setBusy(true);
		setStatus("准备测量…");
		try {
			const message = await matchLoudness({
				editor,
				refs,
				onProgress: (progress) => setStatus(progress),
			});
			setStatus(message);
		} catch (error) {
			setStatus(
				`失败：${String((error as Error)?.message ?? error)
					.replace(/\s+/g, " ")
					.slice(0, 160)}`,
			);
		} finally {
			setBusy(false);
		}
	};

	return (
		<div className="space-y-2">
			<button
				type="button"
				disabled={busy || refs.length < 2}
				onClick={() => {
					void run();
				}}
				className="bg-accent hover:bg-accent/80 w-full rounded-sm px-3 py-1.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50"
			>
				{busy ? "正在测量并统一…" : "统一响度（按实测响度对齐）"}
			</button>
			<p className="text-muted-foreground text-[11px] leading-relaxed">
				{status ||
					"各段原始声音大小不同时点这个：逐段测量响度再分别补偿，音量数值不会一样大，但听起来一样响。"}
			</p>
		</div>
	);
}
