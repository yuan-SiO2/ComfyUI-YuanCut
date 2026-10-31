import { renderChunkFromChannels } from "./sonic-dsp";

const scope = self as unknown as {
	onmessage: ((event: MessageEvent) => void) | null;
	postMessage: (value: unknown, transfer?: Transferable[]) => void;
};

scope.onmessage = async (event: MessageEvent) => {
	const data = (event.data ?? {}) as {
		type?: string;
		id?: number;
		left?: ArrayBuffer;
		right?: ArrayBuffer;
		sampleRate?: number;
		rate?: number;
	};
	if (data.type !== "render" || data.id === undefined) return;
	const left = new Float32Array(data.left as ArrayBuffer);
	const right = new Float32Array(data.right as ArrayBuffer);
	const result = await renderChunkFromChannels({
		left,
		right,
		sampleRate: data.sampleRate ?? 48000,
		startSample: 0,
		inFrames: left.length,
		rate: data.rate ?? 1,
	});
	if (!result) {
		scope.postMessage({ id: data.id });
		return;
	}
	scope.postMessage(
		{ id: data.id, left: result.left.buffer, right: result.right.buffer },
		[result.left.buffer, result.right.buffer],
	);
};
