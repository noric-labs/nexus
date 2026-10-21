/**
 * game.ts: the pi extension that lets a lane's agent play a terminal game.
 *
 * Registers exactly two tools:
 *   send_keys(keys)  type a key sequence into the game, then return the new screen
 *   screen()         return the current screen text and cursor
 *
 * Also:
 *   - a `context` hook that keeps the goal prompt plus the last NEXUS_KEEP_TURNS
 *     exchanges and elides every screen except the newest, so the tokens sent per
 *     model call stay flat however long the lane runs;
 *   - pacing: send_keys never fires sooner than NEXUS_ACTION_INTERVAL_MS after the
 *     previous one (default 5000), so model latency counts toward the 5 s cadence;
 *   - caps: NEXUS_MAX_TURNS and NEXUS_MAX_TOKENS (tokens_in + tokens_out); when
 *     either is hit, tools are blocked and pi is asked to shut down;
 *   - a stats file (NEXUS_AGENT_STATS, default $NEXUS_OUT/agent/stats.json) that the
 *     harness heartbeat reads. Written atomically after every model turn and action.
 *
 * Key I/O goes through the harness CLI (`nexus-lane keys <text>` / `nexus-lane screen`)
 * when it is on PATH, so the harness can count keys (keystorm) and log them for replay.
 * Otherwise it talks to tmux directly (session NEXUS_TMUX_TARGET, default `game`).
 *
 * Key notation (shared with `nexus-lane keys`): plain text is typed literally; named keys
 * go in angle brackets: <Enter> <Esc> <Tab> <BSpace> <Space> <Up> <Down> <Left> <Right>
 * <Home> <End> <PgUp> <PgDn> <Del> <Ins> <F1>..<F12> <C-x> (ctrl) <M-x> (alt) <lt> (a
 * literal "<"). Unknown <...> is typed literally.
 */

import { execFile } from "node:child_process";
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const env = process.env;
const num = (name: string, dflt: number) => {
	const v = Number(env[name]);
	return Number.isFinite(v) && env[name] !== "" && env[name] !== undefined ? v : dflt;
};

const TARGET = env.NEXUS_TMUX_TARGET || "game";
const TMUX_SOCKET = env.NEXUS_TMUX_SOCKET || ""; // tmux -L name; empty = default socket
const INTERVAL_MS = num("NEXUS_ACTION_INTERVAL_MS", 5000);
const SETTLE_MS = num("NEXUS_SETTLE_MS", 400);
const KEEP_TURNS = Math.max(1, num("NEXUS_KEEP_TURNS", 3));
const MAX_TURNS = num("NEXUS_MAX_TURNS", 0); // 0 = unlimited
const MAX_TOKENS = num("NEXUS_MAX_TOKENS", 0); // 0 = unlimited
const MAX_KEYS = num("NEXUS_MAX_KEYS_PER_CALL", 40);
const OUT = env.NEXUS_OUT || "/out";
const STATS = env.NEXUS_AGENT_STATS || `${OUT}/agent/stats.json`;
const USE_HARNESS = (env.NEXUS_USE_HARNESS || "auto").toLowerCase();

const NAMED: Record<string, string> = {
	enter: "Enter", ret: "Enter", return: "Enter", cr: "Enter",
	esc: "Escape", escape: "Escape",
	tab: "Tab", btab: "BTab",
	bspace: "BSpace", backspace: "BSpace", bs: "BSpace",
	space: "Space",
	up: "Up", down: "Down", left: "Left", right: "Right",
	home: "Home", end: "End",
	pgup: "PPage", pageup: "PPage", ppage: "PPage",
	pgdn: "NPage", pagedown: "NPage", npage: "NPage",
	del: "DC", delete: "DC", dc: "DC",
	ins: "IC", insert: "IC", ic: "IC",
};

type Seg = { lit: string } | { key: string };

/** Parse the agent's key string into literal runs and tmux key names. */
export function parseKeys(s: string): { segs: Seg[]; count: number } {
	const segs: Seg[] = [];
	let lit = "";
	let count = 0;
	const flush = () => {
		if (lit) segs.push({ lit });
		lit = "";
	};
	const re = /<([^<>\s]{1,12})>/y;
	for (let i = 0; i < s.length; ) {
		if (s[i] === "<") {
			re.lastIndex = i;
			const m = re.exec(s);
			if (m) {
				const raw = m[1];
				const low = raw.toLowerCase();
				let key: string | undefined;
				if (low === "lt") {
					lit += "<";
					count++;
					i += m[0].length;
					continue;
				}
				if (NAMED[low]) key = NAMED[low];
				else if (/^f([1-9]|1[0-2])$/.test(low)) key = low.toUpperCase();
				else if (/^[cm]-.$/i.test(raw)) key = `${raw[0].toUpperCase()}-${raw[2]}`;
				else if (/^c-[cm]-.$/i.test(raw)) key = `C-M-${raw[4]}`;
				if (key) {
					flush();
					segs.push({ key });
					count++;
					i += m[0].length;
					continue;
				}
			}
		}
		// Literal newlines in the string count as Enter, which is what models usually mean.
		if (s[i] === "\n") {
			flush();
			segs.push({ key: "Enter" });
		} else if (s[i] !== "\r") {
			lit += s[i];
		}
		count++;
		i++;
	}
	flush();
	return { segs, count };
}

function run(cmd: string, args: string[], timeoutMs = 5000): Promise<string> {
	return new Promise((resolve, reject) => {
		execFile(cmd, args, { timeout: timeoutMs, maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
			if (err) reject(new Error(`${cmd} ${args[0] ?? ""}: ${(stderr || err.message).trim()}`));
			else resolve(stdout);
		});
	});
}

const tmux = (args: string[]) => run("tmux", TMUX_SOCKET ? ["-L", TMUX_SOCKET, ...args] : args);

let harness: boolean | undefined;
async function haveHarness(): Promise<boolean> {
	if (USE_HARNESS === "0" || USE_HARNESS === "no" || USE_HARNESS === "off") return false;
	if (harness !== undefined) return harness;
	try {
		await run("sh", ["-c", "command -v nexus-lane"]);
		harness = true;
	} catch {
		harness = false;
	}
	return harness;
}

async function sendKeys(raw: string, segs: Seg[]): Promise<void> {
	if (await haveHarness()) {
		await run("nexus-lane", ["keys", raw]);
		return;
	}
	// One tmux invocation: send-keys -l <lit> ; send-keys <Key> ; ...
	const args: string[] = [];
	for (const s of segs) {
		if (args.length) args.push(";");
		// tmux reads an argument ending in ";" as a command separator; escape it.
		if ("lit" in s) args.push("send-keys", "-t", TARGET, "-l", "--", s.lit.endsWith(";") ? s.lit.slice(0, -1) + "\\;" : s.lit);
		else args.push("send-keys", "-t", TARGET, s.key);
	}
	if (args.length) await tmux(args);
}

async function capture(): Promise<string> {
	let text: string;
	if (await haveHarness()) {
		text = await run("nexus-lane", ["screen"]);
	} else {
		text = await tmux(["capture-pane", "-p", "-t", TARGET]);
	}
	let cursor = "";
	try {
		const d = (await tmux(["display-message", "-p", "-t", TARGET, "#{cursor_x} #{cursor_y} #{pane_width} #{pane_height}"])).trim();
		const [x, y, w, h] = d.split(" ");
		cursor = `cursor=(${x},${y}) size=${w}x${h}`;
	} catch {
		cursor = "cursor=unknown";
	}
	// Trim trailing blank lines/spaces: they cost tokens and carry nothing.
	const lines = text.replace(/\s+$/, "").split("\n").map((l) => l.replace(/\s+$/, ""));
	return `${cursor}\n--- screen ---\n${lines.join("\n")}\n--- end ---`;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---- stats ----
const stats = {
	v: 1,
	lane_id: env.NEXUS_LANE_ID || "",
	game: env.NEXUS_GAME || "",
	model: env.NEXUS_MODEL || "",
	pid: process.pid,
	started: new Date().toISOString(),
	updated: "",
	turn: 0,
	tokens_in: 0,
	tokens_out: 0,
	last_tokens_in: 0,
	actions: 0,
	keys_sent: 0,
	tool_errors: 0,
	last_action_ts: "",
	last_error: "",
	capped: "" as "" | "turns" | "tokens",
};

function writeStats() {
	stats.updated = new Date().toISOString();
	try {
		mkdirSync(dirname(STATS), { recursive: true });
		const tmp = `${STATS}.tmp${process.pid}`;
		writeFileSync(tmp, `${JSON.stringify(stats)}\n`);
		renameSync(tmp, STATS);
	} catch {
		// Stats are best effort; never break the agent over them.
	}
}

function checkCap(): boolean {
	if (stats.capped) return true;
	if (MAX_TURNS > 0 && stats.turn >= MAX_TURNS) stats.capped = "turns";
	else if (MAX_TOKENS > 0 && stats.tokens_in + stats.tokens_out >= MAX_TOKENS) stats.capped = "tokens";
	return !!stats.capped;
}

let lastAction = 0;

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "send_keys",
		label: "Send keys",
		description:
			"Type a key sequence into the game, then return the updated screen. Batch several keys per call " +
			"(e.g. 'jjjl' or 'open mailbox<Enter>'). Plain text is typed literally; named keys use angle " +
			"brackets: <Enter> <Esc> <Tab> <BSpace> <Space> <Up> <Down> <Left> <Right> <Home> <End> <PgUp> " +
			`<PgDn> <Del> <F1>..<F12> <C-x> (ctrl) <M-x> (alt) <lt> (a literal '<'). At most ${MAX_KEYS} keys per call.`,
		parameters: Type.Object({
			keys: Type.String({ description: "Key sequence, e.g. 'look<Enter>' or '<Down><Down><Enter>'" }),
		}),
		async execute(_id, params) {
			const raw = String(params.keys ?? "");
			const { segs, count } = parseKeys(raw);
			if (count === 0) throw new Error("keys is empty");
			if (count > MAX_KEYS) throw new Error(`too many keys (${count}); send at most ${MAX_KEYS} per call`);
			const wait = lastAction + INTERVAL_MS - Date.now();
			if (wait > 0) await sleep(wait);
			lastAction = Date.now();
			try {
				await sendKeys(raw, segs);
			} catch (e) {
				stats.tool_errors++;
				stats.last_error = String((e as Error).message).slice(0, 300);
				writeStats();
				throw e;
			}
			stats.actions++;
			stats.keys_sent += count;
			stats.last_action_ts = new Date(lastAction).toISOString();
			writeStats();
			await sleep(SETTLE_MS);
			const scr = await capture();
			return { content: [{ type: "text", text: `sent ${count} keys\n${scr}` }], details: { keys: raw, count } };
		},
	});

	pi.registerTool({
		name: "screen",
		label: "Screen",
		description: "Return the current game screen (text) and the cursor position. send_keys already returns the screen after typing, so only call this when you need a fresh look without acting.",
		parameters: Type.Object({}),
		async execute() {
			const scr = await capture();
			return { content: [{ type: "text", text: scr }], details: undefined };
		},
	});

	// Block tool calls once capped, and stop the run after this batch.
	pi.on("tool_call", async () => {
		if (checkCap()) return { block: true, reason: `lane agent cap reached (${stats.capped})`, terminate: true };
	});

	pi.on("message_end", async (event) => {
		const m = event.message as { role?: string; usage?: { input?: number; output?: number; cacheRead?: number }; stopReason?: string; errorMessage?: string };
		if (m.role !== "assistant") return;
		const u = m.usage;
		if (u) {
			const inTok = (u.input ?? 0) + (u.cacheRead ?? 0);
			stats.tokens_in += inTok;
			stats.tokens_out += u.output ?? 0;
			stats.last_tokens_in = inTok;
		}
		if (m.stopReason === "error" && m.errorMessage) stats.last_error = String(m.errorMessage).slice(0, 300);
		writeStats();
	});

	pi.on("turn_end", async (_event, ctx) => {
		stats.turn++;
		writeStats();
		if (checkCap()) {
			writeStats();
			ctx.shutdown();
		}
	});

	pi.on("session_start", async () => writeStats());

	// Keep tokens per call flat: goal prompt + last KEEP_TURNS exchanges, newest screen only.
	pi.on("context", async (event) => {
		const msgs = event.messages as Array<Record<string, any>>;
		const firstUser = msgs.findIndex((m) => m.role === "user");
		if (firstUser < 0) return;
		const head = msgs.slice(0, firstUser + 1);
		const rest = msgs.slice(firstUser + 1);
		// A group starts at each assistant or user message; tool results belong to the
		// assistant message before them, so cutting at group starts keeps pairs intact.
		const starts: number[] = [];
		rest.forEach((m, i) => {
			if (m.role === "assistant" || m.role === "user") starts.push(i);
		});
		const cut = starts.length > KEEP_TURNS ? starts[starts.length - KEEP_TURNS] : 0;
		const kept = rest.slice(cut);
		let newestScreen = -1;
		kept.forEach((m, i) => {
			if (m.role === "toolResult" && (m.toolName === "send_keys" || m.toolName === "screen") && !m.isError) newestScreen = i;
		});
		const out = kept.map((m, i) => {
			if (m.role !== "toolResult" || i === newestScreen) return m;
			if (m.toolName !== "send_keys" && m.toolName !== "screen") return m;
			if (m.isError) return m;
			const first = Array.isArray(m.content) && m.content[0]?.type === "text" ? String(m.content[0].text).split("\n")[0] : "";
			const note = m.toolName === "send_keys" ? `${first} (older screen elided)` : "(older screen elided)";
			return { ...m, content: [{ type: "text", text: note }] };
		});
		return { messages: [...head, ...out] as typeof event.messages };
	});
}
