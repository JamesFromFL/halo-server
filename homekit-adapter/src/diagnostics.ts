import {VIDEO_DIAGNOSTIC_CODES} from "./video.js";

/** Fixed diagnostic vocabulary; peer data and transport secrets never enter notices. */
export const INTEROP_STAGES = ["snapshot", "video-prepare", "video-start", "video-reconfigure", "video-stop", "video-request", "audio-settings"] as const;
export const INTEROP_CODES = [
  ...VIDEO_DIAGNOSTIC_CODES,
  "invalid-value", "retired", "timeout", "conflict", "cleanup-failed", "persistence-failed", "policy-failed",
  "policy-update-busy", "policy-denied", "not-ready", "busy", "stale-scope", "source-lost", "unavailable",
] as const;

export interface InteropDiagnostic { code: string; message: string }
export function diagnosticPayload(stage: string, code: string): InteropDiagnostic | null {
  if (!(INTEROP_STAGES as readonly string[]).includes(stage)) return null;
  return {code: `interop-${stage}`, message: (INTEROP_CODES as readonly string[]).includes(code) ? code : "operation-failed"};
}

/** At most one outstanding write and one notice per stage per five seconds. */
export class InteropDiagnostics {
  private readonly last = new Map<string, number>();
  private sending = false;
  constructor(private readonly send: (payload: InteropDiagnostic) => Promise<void>,
    private readonly now: () => number = () => performance.now()) { }

  readonly report = (stage: string, code: string): void => {
    const payload = diagnosticPayload(stage, code), now = this.now();
    if (!payload || this.sending || now - (this.last.get(stage) ?? -Infinity) < 5_000) return;
    this.last.set(stage, now); this.sending = true;
    try { void this.send(payload).catch(() => {}).finally(() => { this.sending = false; }); }
    catch { this.sending = false; }
  };
}
