import { TextDecoder } from "node:util";

export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 65_536;
export const MAX_JSON_DEPTH = 32;
export const MAX_FRAMES_PER_CHUNK = 256;
export const FEATURE = "classic-camera-control-v1";
export const FEATURES = [FEATURE, "bulk-cancel-v1", "hksv-recording-v1"] as const;

export const KINDS = [
  "recording_settings_update", "recording_settings_result", "recording_open", "recording_open_result",
  "recording_next", "recording_next_result", "recording_stop", "recording_stop_result",
  "audio_settings_update", "audio_settings_result",
  "video_prepare", "video_prepare_result", "video_start", "video_start_result", "video_reconfigure", "video_reconfigure_result", "video_stop", "video_stop_result", "video_ended",
  "accessory_config", "accessory_config_ack", "event_sync", "event_sync_ack", "event_state", "camera_event",
  "hello", "hello_ack", "ping", "pong", "shutdown", "shutdown_ack", "status", "error",
  "inventory_reset", "inventory_request", "inventory_page", "camera_status", "policy_update", "policy_ack",
  "lease_request", "lease_result", "lease_renew", "lease_renew_result", "lease_stop", "lease_stop_result",
  "snapshot_request", "snapshot_result", "snapshot_cancel", "talkback_request", "talkback_result",
  "control_request", "control_result", "transfer_open", "transfer_credit", "transfer_end", "transfer_cancel", "transfer_result", "transfer_fence", "transfer_fence_ack",
] as const;
export type Kind = typeof KINDS[number];
export type JsonObject = { [key: string]: unknown };
export interface Envelope {
  v: 1;
  kind: Kind;
  boot_id: string;
  request_id: string;
  payload: JsonObject;
}

export class ProtocolError extends Error {
  constructor(public readonly code: string) {
    super(code);
    this.name = "ProtocolError";
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function record(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fields(value: JsonObject, expected: readonly string[]): void {
  const keys = Object.keys(value);
  if (keys.length !== expected.length || keys.some(key => !expected.includes(key))) {
    throw new ProtocolError("invalid-fields");
  }
}

function text(value: unknown, maximum: number, nonempty = false): value is string {
  if (typeof value !== "string" || (nonempty && !value.length) || /[\u0000-\u001f]/.test(value)) {
    return false;
  }
  const points = Array.from(value);
  return points.length <= maximum && points.every(char =>
    char.length === 2 || char.charCodeAt(0) < 0xd800 || char.charCodeAt(0) > 0xdfff,
  );
}

export const MAX_INVENTORY_DEVICES = 256;
export const MAX_INVENTORY_PAGE = 32;
export const MAX_BULK_CHUNK_BYTES = 65_536;
export const MAX_BULK_WINDOW_CHUNKS = 2;
export const MAX_BULK_WINDOW_BYTES = 131_072;
export const BULK_HEADER_BYTES = 32;
export const BULK_LIMITS = {
  snapshot: 8 * 1024 * 1024, recording_init: 1024 * 1024,
  recording_fragment: 8 * 1024 * 1024, talkback_pcm: 1920,
} as const;
export type BulkKind = keyof typeof BULK_LIMITS;
const bulkKinds = Object.keys(BULK_LIMITS) as BulkKind[];
export const POLICY_FIELDS = [
  "synchronized", "streaming", "live_audio", "snapshots", "recording", "recording_audio", "talkback", "controls",
] as const;
export const CAPABILITY_FIELDS = ["video", "audio", "snapshot", "talkback", "light", "siren", "doorbell", "battery"] as const;
export const RESULT_CODES = [
  "ok", "unavailable", "unsupported", "unauthorized", "stale-scope", "policy-denied", "busy", "timeout",
  "cancelled", "invalid-request", "not-found", "conflict", "source-failed", "expired", "protocol-error", "invalidated", "not-ready",
] as const;
export interface AccountScope { profile_id: string; account_generation: number }
export interface MediaScope extends AccountScope { camera_id: string; device_generation: number; privacy_generation: number }
export type CameraPolicy = Record<typeof POLICY_FIELDS[number], boolean>;
export interface AudioSettings {
  microphone_muted: boolean; speaker_muted: boolean; microphone_volume: number; speaker_volume: number;
}
export interface AccessoryAudio extends AudioSettings {microphone: boolean; speaker: boolean; revision: number}
export interface AudioConfiguration {
  sample_rate: 8000 | 16000 | 24000; packet_time: 20 | 30 | 40 | 60; bitrate_mode: "vbr" | "cbr";
  max_bit_rate: number; payload_type: number; rtcp_interval: number; channels: 1; clock_mode: "classic_hap";
}
export interface AudioPrepare {target_port: number; key: string; salt: string}
export interface AudioEndpoint {port: number; ssrc: number; key: string; salt: string}
export interface VideoConfiguration {
  width: number; height: number; fps: number; profile: "baseline" | "main" | "high"; level: "3.1" | "3.2" | "4.0";
  max_bit_rate: number; payload_type: number; rtcp_interval: number; mtu: number;
}
export interface RecordingConfiguration {
  video: Pick<VideoConfiguration, "width" | "height" | "fps" | "profile" | "level" | "max_bit_rate">;
  audio: {sample_rate: 16000 | 24000 | 48000; bit_rate: number; channels: 1; bitrate_mode: "vbr"} | null;
  fragment_length_ms: 4000;
  i_frame_interval_ms: number;
}
export interface AccessoryRecording {
  revision: number; enabled: boolean; active: boolean; audio_active: boolean;
  configuration: RecordingConfiguration | null;
}
export interface VideoPrepare {
  source_address: string; target_address: string; address_version: "ipv4" | "ipv6"; target_port: number; key: string; salt: string;
  audio: AudioPrepare | null;
}
export interface VideoEndpoint {address: string; port: number; ssrc: number; key: string; salt: string; audio: AudioEndpoint | null}
export interface BulkHeader { kind: BulkKind; transfer_id: string; sequence: number; length: number }

type Check = (value: unknown) => boolean;
const integer = (minimum = 1, maximum = Number.MAX_SAFE_INTEGER): Check => value =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
const choice = (...values: readonly string[]): Check => value => typeof value === "string" && values.includes(value);
const nullable = (check: Check): Check => value => value === null || check(value);
const object = (shape: Record<string, Check>): Check => value => record(value) &&
  Object.keys(value).length === Object.keys(shape).length && Object.entries(shape).every(([key, check]) =>
    Object.hasOwn(value, key) && check(value[key]));
const bool: Check = value => typeof value === "boolean";
const uuid: Check = value => typeof value === "string" && value.length === 36 && UUID.test(value);
// Frozen Unicode16 Cc/Cf/Cs ranges: do not depend on the Node Unicode database.
export const FORBIDDEN_TEXT_RANGES: readonly (readonly [number, number])[] = [
  [0x0, 0x1f],
  [0x7f, 0x9f],
  [0xad, 0xad],
  [0x600, 0x605],
  [0x61c, 0x61c],
  [0x6dd, 0x6dd],
  [0x70f, 0x70f],
  [0x890, 0x891],
  [0x8e2, 0x8e2],
  [0x180e, 0x180e],
  [0x200b, 0x200f],
  [0x202a, 0x202e],
  [0x2060, 0x2064],
  [0x2066, 0x206f],
  [0xd800, 0xdfff],
  [0xfeff, 0xfeff],
  [0xfff9, 0xfffb],
  [0x110bd, 0x110bd],
  [0x110cd, 0x110cd],
  [0x13430, 0x1343f],
  [0x1bca0, 0x1bca3],
  [0x1d173, 0x1d17a],
  [0xe0001, 0xe0001],
  [0xe0020, 0xe007f],
];
const display = (maximum: number, nonempty = true): Check => value => text(value, maximum, nonempty) &&
  Array.from(value).every(char => !FORBIDDEN_TEXT_RANGES.some(([start, end]) => {
    const point = char.codePointAt(0)!; return start <= point && point <= end;
  }));
const positive = integer();
const revision = integer(0);
const code = choice(...RESULT_CODES);
const account = object({ profile_id: uuid, account_generation: positive });
const scope = object({ profile_id: uuid, camera_id: display(128), account_generation: positive, device_generation: positive, privacy_generation: positive });
const policyFields = object(Object.fromEntries(POLICY_FIELDS.map(key => [key, bool])));
const policy: Check = value => policyFields(value) && record(value) && (!value.recording_audio || value.recording === true);
const labels = new Set(CAPABILITY_FIELDS.flatMap(name => [`sdk:${name}`, `halo:${name}`]));
const capabilitiesFields = object({ ...Object.fromEntries(CAPABILITY_FIELDS.map(key => [key, bool])), evidence: () => true });
const capabilities: Check = value => {
  if (!capabilitiesFields(value) || !record(value) || !Array.isArray(value.evidence)) return false;
  const evidence: unknown[] = value.evidence;
  return evidence.length <= 16 && evidence.every(label => typeof label === "string" && labels.has(label)) &&
    new Set(evidence).size === evidence.length && CAPABILITY_FIELDS.every(name =>
      !value[name] || ["sdk", "halo"].some(source => evidence.includes(`${source}:${name}`)));
};
const ascii32: Check = value => typeof value === "string" && /^[\x20-\x7e]{1,32}$/.test(value) && !/[^\x20-\x7e]/.test(value);
const videoFields = object({codec: choice("h264"), width: nullable(integer(1, 8192)), height: nullable(integer(1, 8192)),
  profile: nullable(ascii32), level: nullable(ascii32), frame_rate_num: nullable(integer(1, 1_000_000)), frame_rate_den: nullable(integer(1, 1_000_000))});
const video: Check = value => videoFields(value) && record(value) && (
  (value.frame_rate_num === null && value.frame_rate_den === null) ||
  (value.frame_rate_num !== null && value.frame_rate_den !== null && (value.frame_rate_num as number) <= 240 * (value.frame_rate_den as number)));
const packetTime: Check = value => typeof value === "number" && Number.isFinite(value) && value > 0 && value <= 120;
const audio = object({codec: choice("opus", "aac", "pcm_s16le"), sample_rate: nullable(integer(1, 192000)), channels: nullable(integer(1, 2)),
  rtp_clock_rate: nullable(integer(1, 192000)), packet_time_ms: nullable(packetTime)});
const sourceFormat = nullable(object({video: nullable(video), audio: nullable(audio), capture_attempt: nullable(positive), config_generation: nullable(positive)}));
const device = object({
  scope, name: display(256, false), family: display(128, false), kind: display(128, false), model: display(128, false), capabilities,
  connection_state: nullable(bool), battery_percent: nullable(integer(0, 100)), observed_format: sourceFormat,
});
const inventoryFields = object({
  scope: account, revision, cursor: nullable(uuid), next_cursor: nullable(uuid), total: integer(0, MAX_INVENTORY_DEVICES), complete: bool,
  devices: value => Array.isArray(value) && value.length <= MAX_INVENTORY_PAGE && value.every(device),
});
const inventory: Check = value => {
  if (!inventoryFields(value) || !record(value)) return false;
  const devices = value.devices as {scope: MediaScope}[];
  const accountScope = value.scope as AccountScope;
  return devices.length <= (value.total as number) && new Set(devices.map(item => item.scope.camera_id)).size === devices.length &&
    devices.every(item => item.scope.profile_id === accountScope.profile_id && item.scope.account_generation === accountScope.account_generation) &&
    (value.next_cursor === null || (devices.length > 0 && value.next_cursor !== value.cursor));
};
const snapshotRequestFields = object({scope, job_id: uuid, width: integer(1, 4096), height: integer(1, 4096), reason: choice("periodic", "event", "explicit", "unknown")});
const snapshotRequest: Check = value => snapshotRequestFields(value) && record(value) && (value.width as number) * (value.height as number) <= 8_000_000;
const snapshotResultFields = object({scope, job_id: uuid, status: choice("ready", "rejected", "cancelled"), code, transfer_id: nullable(uuid)});
const snapshotResult: Check = value => snapshotResultFields(value) && record(value) && ((value.status === "ready") === (value.transfer_id !== null)) && ((value.status === "ready") === (value.code === "ok"));
const resource = {scope, session_id: uuid};
const transfer = {scope, operation_id: uuid, transfer_id: uuid};
const leaseResultFields = object({...resource, status: choice("accepted", "rejected", "expired"), code, ttl_ms: integer(0, 30_000)});
const leaseResult: Check = value => leaseResultFields(value) && record(value) && ((value.status === "accepted") === ((value.ttl_ms as number) > 0));
const transferOpenFields = object({...transfer, kind: choice(...bulkKinds), total_bytes: integer(1, 8 * 1024 * 1024), chunk_bytes: integer(1, MAX_BULK_CHUNK_BYTES), window_chunks: integer(1, MAX_BULK_WINDOW_CHUNKS)});
const transferOpen: Check = value => transferOpenFields(value) && record(value) &&
  (value.total_bytes as number) <= BULK_LIMITS[value.kind as BulkKind] &&
  (value.chunk_bytes as number) <= Math.min(MAX_BULK_CHUNK_BYTES, value.total_bytes as number) &&
  (value.chunk_bytes as number) * (value.window_chunks as number) <= MAX_BULK_WINDOW_BYTES;
const epoch: Check = value => typeof value === "string" && value.length >= 1 && value.length <= 64 && !/[^A-Za-z0-9_-]/.test(value);
const username: Check = value => typeof value === "string" && value.length === 17 && /^[0-9A-F][26AE](?::[0-9A-F]{2}){5}$/.test(value);
const audioSettingsFields = {microphone_muted:bool,speaker_muted:bool,microphone_volume:integer(0,100),speaker_volume:integer(0,100)};
const audioSettings = object(audioSettingsFields);
const accessoryAudioFields = object({...audioSettingsFields,microphone:bool,speaker:bool,revision});
const accessoryAudio: Check = value => accessoryAudioFields(value) && record(value) && (!value.speaker || value.microphone === true);
const recordingVideoFields = object({width:value=>integer(2,1920)(value) && (value as number)%2===0,
  height:value=>integer(2,1080)(value) && (value as number)%2===0,fps:integer(1,30),
  profile:choice("baseline","main","high"),level:choice("3.1","3.2","4.0"),max_bit_rate:integer(1,20000)});
const recordingVideo: Check = value => {
  if(!recordingVideoFields(value)||!record(value))return false;
  const limits:{[key:string]:readonly[number,number]}={"3.1":[3600,108000],"3.2":[5120,216000],"4.0":[8192,245760]};
  const [frame,rate]=limits[value.level as string]!;
  const blocks=Math.ceil((value.width as number)/16)*Math.ceil((value.height as number)/16);
  return blocks<=frame && blocks*(value.fps as number)<=rate &&
    (value.level!=="3.1" || (value.max_bit_rate as number)<=(value.profile==="high"?17500:14000));
};
const recordingAudio = object({sample_rate:value=>[16000,24000,48000].includes(value as number),
  bit_rate:integer(16000,128000),channels:integer(1,1),bitrate_mode:choice("vbr")});
const recordingConfigurationFields = object({video:recordingVideo,audio:nullable(recordingAudio),
  fragment_length_ms:integer(4000,4000),i_frame_interval_ms:integer(1,4000)});
export const recordingConfiguration: Check = value => recordingConfigurationFields(value) && record(value) &&
  (value.i_frame_interval_ms as number)*((value.video as JsonObject).fps as number)>=1000;
const accessoryRecording = object({revision,enabled:bool,active:bool,audio_active:bool,configuration:nullable(recordingConfiguration)});
const accessoryEntryFields = object({camera_id:display(128),accessory_id:uuid,username,doorbell:bool,audio:nullable(accessoryAudio)});
const accessoryEntry: Check = value => {
  if(!record(value))return false;
  if(!Object.hasOwn(value,"recording"))return accessoryEntryFields(value);
  const {recording,...entry}=value;
  return accessoryRecording(recording)&&accessoryEntryFields(entry);
};
const accessoryFields = object({scope:nullable(account),revision,generation:positive,page_index:integer(0,7),total_pages:integer(1,8),
  entries:value=>Array.isArray(value)&&value.length<=32&&value.every(accessoryEntry),media_ready:bool});
const accessoryConfig: Check = value => accessoryFields(value) && record(value) &&
  (value.page_index as number)<(value.total_pages as number) &&
  (value.scope!==null || ((value.entries as unknown[]).length===0 && value.page_index===0 && value.total_pages===1)) &&
  ["camera_id","accessory_id","username"].every(key=>new Set((value.entries as JsonObject[]).map(item=>item[key])).size===(value.entries as unknown[]).length);
const address: Check = value => typeof value === "string" && value.length >= 1 && value.length <= 128 && !/[^\x21-\x7e]/.test(value);
const secret = (bytes: number): Check => value => typeof value === "string" &&
  Buffer.from(value, "base64").length === bytes && Buffer.from(value, "base64").toString("base64") === value;
const audioEndpoint = object({port:integer(1,65535),ssrc:integer(1,0xffffffff),key:secret(16),salt:secret(14)});
const audioPrepare = object({target_port:integer(1,65535),key:secret(16),salt:secret(14)});
const endpoint = object({address,port:integer(1,65535),ssrc:integer(1,0xffffffff),key:secret(16),salt:secret(14),audio:nullable(audioEndpoint)});
const rtcpInterval: Check = value => typeof value === "number" && Number.isFinite(value) && value>=0.1 && value<=10;
const audioConfiguration = object({sample_rate:value=>[8000,16000,24000].includes(value as number),
  packet_time:value=>[20,30,40,60].includes(value as number),bitrate_mode:choice("vbr","cbr"),max_bit_rate:integer(6,64),
  payload_type:integer(96,127),rtcp_interval:rtcpInterval,channels:integer(1,1),clock_mode:choice("classic_hap")});
const videoConfiguration = object({width:integer(2,1920),height:integer(2,1080),fps:integer(1,30),
  profile:choice("baseline","main","high"),level:choice("3.1","3.2","4.0"),max_bit_rate:integer(1,8000),
  payload_type:integer(96,127),rtcp_interval:rtcpInterval,mtu:integer(256,1378)});
const videoResultFields = object({...resource,status:choice("accepted","rejected"),code});
const videoResult: Check = value => videoResultFields(value) && record(value) && ((value.status === "accepted") === (value.code === "ok"));
const videoPrepareResultFields = object({...resource,status:choice("accepted","rejected"),code,endpoint:nullable(endpoint)});
const videoPrepareResult: Check = value => videoPrepareResultFields(value) && record(value) &&
  ((value.status === "accepted") === (value.code === "ok")) && ((value.status === "accepted") === (value.endpoint !== null));
const recordingJob={scope,job_id:uuid};
const recordingNextFields=object({...recordingJob,status:choice("ready","eos","rejected"),code,
  transfer_id:nullable(uuid),kind:nullable(choice("init","fragment")),sequence:integer(0,0xffffffff),
  total_bytes:integer(0,BULK_LIMITS.recording_fragment),duration_ms:integer(0,4000)});
const recordingNextResult: Check = value => {
  if(!recordingNextFields(value)||!record(value))return false;
  if(value.status!=="ready")return (value.status==="eos")===(value.code==="ok") &&
    value.transfer_id===null&&value.kind===null&&value.total_bytes===0&&value.duration_ms===0;
  if(value.code!=="ok"||value.transfer_id===null||value.kind===null)return false;
  if(value.kind==="init")return value.sequence===0&&(value.total_bytes as number)>=1&&
    (value.total_bytes as number)<=BULK_LIMITS.recording_init&&value.duration_ms===0;
  return (value.sequence as number)>=1&&(value.total_bytes as number)>=1&&(value.duration_ms as number)>=1;
};
const payloadValidators: Record<Kind, Check> = {
  recording_settings_update:object({scope,expected_revision:revision,active:bool,audio_active:bool,configuration:nullable(recordingConfiguration)}),
  recording_settings_result:value=>object({scope,accepted:bool,code,revision})(value)&&record(value)&&((value.accepted===true)===(value.code==="ok")),
  recording_open:object({...recordingJob,configuration:recordingConfiguration}),
  recording_open_result:value=>object({...recordingJob,status:choice("accepted","rejected"),code})(value)&&record(value)&&((value.status==="accepted")===(value.code==="ok")),
  recording_next:object({...recordingJob,sequence:integer(0,0xffffffff)}),recording_next_result:recordingNextResult,
  recording_stop:object(recordingJob),
  recording_stop_result:value=>object({...recordingJob,status:choice("stopped","rejected"),code})(value)&&record(value)&&((value.status==="stopped")===(value.code==="ok")),
  audio_settings_update:object({scope,expected_revision:revision,settings:audioSettings}),
  audio_settings_result:value=>object({scope,accepted:bool,code,revision})(value) && record(value) && ((value.accepted===true)===(value.code==="ok")),
  video_prepare:object({...resource,source_address:address,target_address:address,address_version:choice("ipv4","ipv6"),target_port:integer(1,65535),key:secret(16),salt:secret(14),audio:nullable(audioPrepare)}),
  video_prepare_result:videoPrepareResult,
  video_start:object({...resource,config:videoConfiguration,audio_config:nullable(audioConfiguration)}),video_start_result:videoResult,
  video_reconfigure:object({...resource,config:videoConfiguration}),video_reconfigure_result:videoResult,
  video_stop:object(resource),video_stop_result:videoResult,
  video_ended:object({...resource,code}),
  accessory_config: accessoryConfig,
  accessory_config_ack: object({scope:nullable(account),revision,generation:positive,page_index:integer(0,7),accepted:bool,code}),
  event_sync: object({scope:nullable(account),epoch:nullable(epoch),sequence:revision}),
  event_sync_ack: object({scope:nullable(account),epoch,sequence:revision,accepted:bool,code}),
  event_state: object({scope,epoch,sequence:revision,motion:nullable(bool),ttl_ms:integer(0,30000),expires_at_ms:revision,source_state:choice("connected","connecting","offline"),synchronized:bool}),
  camera_event: object({scope,epoch,sequence:revision,event_id:uuid,type:choice("doorbell"),ttl_ms:integer(1,2000),expires_at_ms:revision}),
  hello: object({role: choice("python"), build_id: value => text(value, 128, true), protocol: integer(1, 1)}),
  hello_ack: object({role: choice("hap"), build_id: value => text(value, 128, true), protocol: integer(1, 1),
    features: value => Array.isArray(value) && value.length === FEATURES.length && FEATURES.every((feature, index) => value[index] === feature), pid: positive}),
  ping: object({}), pong: object({}), shutdown: object({}), shutdown_ack: object({}),
  status: object({state: choice("ready", "degraded"), reason: value => text(value, 256)}),
  error: object({code: value => text(value, 64, true), message: value => text(value, 256)}),
  inventory_reset: object({scope: nullable(account), revision}),
  inventory_request: object({scope: account, revision, cursor: nullable(uuid)}), inventory_page: inventory,
  camera_status: object({scope, revision, state: choice("online", "offline", "unknown"), code}),
  policy_update: object({scope, policy}), policy_ack: object({scope, accepted: bool, code}),
  lease_request: object({...resource, purpose: choice("homekit_live"), ttl_ms: integer(1, 30_000)}), lease_result: leaseResult,
  lease_renew: object({...resource, ttl_ms: integer(1, 30_000)}), lease_renew_result: leaseResult,
  lease_stop: object(resource), lease_stop_result: object({...resource, status: choice("stopped", "rejected"), code}),
  snapshot_request: snapshotRequest, snapshot_result: snapshotResult, snapshot_cancel: object({scope, job_id: uuid}),
  talkback_request: object({...resource, action: choice("acquire", "stop")}),
  talkback_result: object({...resource, status: choice("accepted", "stopped", "rejected"), code}),
  control_request: object({scope, operation_id: uuid, control: choice("light", "siren"), enabled: bool}),
  control_result: object({scope, operation_id: uuid, status: choice("accepted", "rejected", "unknown"), code}),
  transfer_open: transferOpen,
  transfer_credit: object({...transfer, next_sequence: integer(0, 2 ** 32 - 1), credits: integer(1, MAX_BULK_WINDOW_CHUNKS)}),
  transfer_end: object({...transfer, sequence_count: integer(1, 2 ** 32 - 1), total_bytes: integer(1, 8 * 1024 * 1024)}),
  transfer_cancel: object({...transfer, code: choice("cancelled", "invalidated", "timeout", "protocol-error")}),
  transfer_fence: object({...transfer, sequence_count: integer(0, 2 ** 32 - 1), sent_bytes: integer(0, 8 * 1024 * 1024), code: choice("cancelled", "invalidated", "timeout")}),
  transfer_fence_ack: object({...transfer, sequence_count: integer(0, 2 ** 32 - 1), sent_bytes: integer(0, 8 * 1024 * 1024), code: choice("cancelled", "invalidated", "timeout")}),
  transfer_result: object({...transfer, status: choice("complete", "cancelled", "rejected"), code}),
};
const pythonKinds: readonly Kind[] = ["recording_settings_result", "recording_open_result", "recording_next_result", "recording_stop_result", "audio_settings_result", "video_prepare_result", "video_start_result", "video_reconfigure_result", "video_stop_result", "video_ended", "accessory_config", "event_sync_ack", "event_state", "camera_event", "hello", "ping", "shutdown", "inventory_reset", "inventory_page", "camera_status", "policy_ack", "lease_result", "lease_renew_result", "lease_stop_result", "snapshot_result", "talkback_result", "control_result"];
const hapKinds: readonly Kind[] = ["recording_settings_update", "recording_open", "recording_next", "recording_stop", "audio_settings_update", "video_prepare", "video_start", "video_reconfigure", "video_stop", "accessory_config_ack", "event_sync", "hello_ack", "pong", "shutdown_ack", "inventory_request", "policy_update", "lease_request", "lease_renew", "lease_stop", "snapshot_request", "snapshot_cancel", "talkback_request", "control_request"];
export function validateScope(value: unknown): MediaScope {
  if (!scope(value)) throw new ProtocolError("invalid-scope");
  return value as MediaScope;
}
export function validateEnvelope(value: unknown): Envelope {
  if (!record(value)) throw new ProtocolError("invalid-envelope");
  fields(value, ["v", "kind", "boot_id", "request_id", "payload"]);
  if (value.v !== 1 || !KINDS.includes(value.kind as Kind) || !uuid(value.boot_id) || !uuid(value.request_id)) {
    throw new ProtocolError("invalid-envelope");
  }
  if (!payloadValidators[value.kind as Kind](value.payload)) throw new ProtocolError("invalid-payload");
  return value as unknown as Envelope;
}
export function validateDirection(value: unknown, sender: "python" | "hap"): Envelope {
  const message = validateEnvelope(value);
  const allowed = sender === "python" ? pythonKinds : sender === "hap" ? hapKinds : null;
  if (allowed === null || (!allowed.includes(message.kind) && (pythonKinds.includes(message.kind) || hapKinds.includes(message.kind)))) {
    throw new ProtocolError("invalid-direction");
  }
  if (message.kind === "transfer_open") {
    const producer = message.payload.kind === "talkback_pcm" ? "hap" : "python";
    if (sender !== producer) throw new ProtocolError("invalid-bulk-direction");
  }
  return message;
}
export const REPLY_KINDS: Partial<Record<Kind, Kind>> = {
  recording_settings_update:"recording_settings_result",recording_open:"recording_open_result",recording_next:"recording_next_result",recording_stop:"recording_stop_result",
  audio_settings_update:"audio_settings_result",
  video_prepare:"video_prepare_result",video_start:"video_start_result",video_reconfigure:"video_reconfigure_result",video_stop:"video_stop_result",
  accessory_config:"accessory_config_ack", event_sync:"event_sync_ack",
  hello: "hello_ack", ping: "pong", shutdown: "shutdown_ack", inventory_request: "inventory_page",
  policy_update: "policy_ack", lease_request: "lease_result", lease_renew: "lease_renew_result",
  lease_stop: "lease_stop_result", snapshot_request: "snapshot_result", talkback_request: "talkback_result",
  control_request: "control_result", transfer_fence: "transfer_fence_ack", transfer_open: "transfer_credit", transfer_end: "transfer_result",
};
export function validateReply(requestValue: unknown, replyValue: unknown): Envelope {
  const request = validateEnvelope(requestValue), reply = validateEnvelope(replyValue);
  if (REPLY_KINDS[request.kind] !== reply.kind || request.boot_id !== reply.boot_id || request.request_id !== reply.request_id) {
    throw new ProtocolError("invalid-reply-correlation");
  }
  for (const key of ["scope", "session_id", "job_id", "operation_id", "transfer_id"]) {
    if (!Object.hasOwn(request.payload, key)) continue;
    const sent = request.payload[key], received = reply.payload[key];
    const equal = key === "scope" ? sent===null && received===null || record(sent) && record(received) &&
      Object.keys(sent).length === Object.keys(received).length && Object.entries(sent).every(([name, value]) => received[name] === value) : sent === received;
    if (!equal) throw new ProtocolError("invalid-reply-ownership");
  }
  if (request.kind === "transfer_fence" && ["sequence_count", "sent_bytes", "code"].some(key => request.payload[key] !== reply.payload[key])) throw new ProtocolError("invalid-fence-correlation");
  if (["audio_settings_update","recording_settings_update"].includes(request.kind) && reply.payload.accepted === true &&
      reply.payload.revision !== request.payload.expected_revision && reply.payload.revision !== (request.payload.expected_revision as number) + 1) throw new ProtocolError("invalid-audio-correlation");
  if (request.kind === "recording_next" && request.payload.sequence !== reply.payload.sequence) throw new ProtocolError("invalid-recording-sequence");
  if (request.kind === "accessory_config" && ["revision","generation","page_index"].some(key=>request.payload[key]!==reply.payload[key])) throw new ProtocolError("invalid-config-correlation");
  if (request.kind === "inventory_request" && (request.payload.cursor !== reply.payload.cursor ||
      (request.payload.revision !== 0 && request.payload.revision !== reply.payload.revision))) {
    throw new ProtocolError("invalid-inventory-revision");
  }
  return reply;
}
export function encodeBulkHeader(header: BulkHeader): Buffer {
  if (!record(header) || !object({kind: choice(...bulkKinds), transfer_id: uuid, sequence: integer(0, 2 ** 32 - 1), length: integer(1, MAX_BULK_CHUNK_BYTES)})(header) ||
      header.length > BULK_LIMITS[header.kind]) throw new ProtocolError("invalid-bulk-header");
  const bytes = Buffer.alloc(BULK_HEADER_BYTES);
  bytes.write("HBLK", 0, "ascii"); bytes[4] = 1; bytes[5] = bulkKinds.indexOf(header.kind) + 1;
  Buffer.from(header.transfer_id.replaceAll("-", ""), "hex").copy(bytes, 8);
  bytes.writeUInt32BE(header.sequence, 24); bytes.writeUInt32BE(header.length, 28);
  return bytes;
}
export function decodeBulkHeader(bytes: Uint8Array): BulkHeader {
  if (bytes.byteLength !== BULK_HEADER_BYTES) throw new ProtocolError("invalid-bulk-length");
  const body = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const kind = bulkKinds[body[5]! - 1];
  if (!body.subarray(0, 4).equals(Buffer.from("HBLK", "ascii")) || body[4] !== 1 || body.readUInt16BE(6) !== 0 || kind === undefined) {
    throw new ProtocolError("invalid-bulk-header");
  }
  const hex = body.subarray(8, 24).toString("hex");
  const header = {kind, transfer_id: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`, sequence: body.readUInt32BE(24), length: body.readUInt32BE(28)};
  encodeBulkHeader(header);
  return header;
}

// JSON.parse validates syntax. This pass additionally rejects duplicate object keys,
// excessive nesting and nonfinite numeric values before an envelope becomes authority.
function validateJson(textValue: string, value: unknown): void {
  const stack: { keys: Set<string> | null; keyNext: boolean }[] = [];
  for (let i = 0; i < textValue.length; i++) {
    const char = textValue[i];
    if (char === '"') {
      const start = i;
      while (++i < textValue.length) {
        if (textValue[i] === "\\") i++;
        else if (textValue[i] === '"') break;
      }
      const frame = stack.at(-1);
      if (frame?.keys && frame.keyNext) {
        const key = JSON.parse(textValue.slice(start, i + 1)) as string;
        if (frame.keys.has(key)) throw new ProtocolError("duplicate-key");
        frame.keys.add(key);
        frame.keyNext = false;
      }
    } else if (char === "{" || char === "[") {
      stack.push({ keys: char === "{" ? new Set() : null, keyNext: true });
      if (stack.length > MAX_JSON_DEPTH) throw new ProtocolError("json-depth");
    } else if (char === "}" || char === "]") {
      stack.pop();
    } else if (char === "," && stack.at(-1)?.keys) {
      stack.at(-1)!.keyNext = true;
    }
  }
  const pending = [value];
  while (pending.length) {
    const next = pending.pop();
    if (typeof next === "number" && !Number.isFinite(next)) {
      throw new ProtocolError("nonfinite-number");
    }
    if (next !== null && typeof next === "object") pending.push(...Object.values(next));
  }
}

export function decodeEnvelope(body: Uint8Array): Envelope {
  if (body.byteLength < 1 || body.byteLength > MAX_FRAME_BYTES) {
    throw new ProtocolError("frame-length");
  }
  try {
    const decoded = utf8.decode(body);
    const value: unknown = JSON.parse(decoded);
    validateJson(decoded, value);
    return validateEnvelope(value);
  } catch (error) {
    if (error instanceof ProtocolError) throw error;
    throw new ProtocolError("invalid-json");
  }
}

export function encodeEnvelope(value: unknown): Buffer {
  const envelope = validateEnvelope(value);
  const body = Buffer.from(JSON.stringify(envelope), "utf8");
  if (body.length < 1 || body.length > MAX_FRAME_BYTES) throw new ProtocolError("frame-length");
  // Also rejects values JSON.stringify silently rewrites (e.g. custom toJSON output).
  decodeEnvelope(body);
  const frame = Buffer.allocUnsafe(body.length + 4);
  frame.writeUInt32BE(body.length);
  body.copy(frame, 4);
  return frame;
}

export class FrameDecoder {
  private prefix = Buffer.alloc(4);
  private prefixBytes = 0;
  private body: Buffer | null = null;
  private bodyBytes = 0;
  private failed = false;

  get partial(): boolean { return this.prefixBytes > 0 || this.body !== null; }

  feed(chunk: Uint8Array): Envelope[] {
    if (this.failed) throw new ProtocolError("decoder-closed");
    const messages: Envelope[] = [];
    let offset = 0;
    try {
      while (offset < chunk.length) {
        if (this.body === null) {
          const count = Math.min(4 - this.prefixBytes, chunk.length - offset);
          this.prefix.set(chunk.subarray(offset, offset + count), this.prefixBytes);
          this.prefixBytes += count;
          offset += count;
          if (this.prefixBytes !== 4) continue;
          const length = this.prefix.readUInt32BE();
          if (length < 1 || length > MAX_FRAME_BYTES) throw new ProtocolError("frame-length");
          this.body = Buffer.allocUnsafe(length);
          this.prefixBytes = 0;
        }
        const count = Math.min(this.body.length - this.bodyBytes, chunk.length - offset);
        this.body.set(chunk.subarray(offset, offset + count), this.bodyBytes);
        this.bodyBytes += count;
        offset += count;
        if (this.bodyBytes === this.body.length) {
          messages.push(decodeEnvelope(this.body));
          this.body = null;
          this.bodyBytes = 0;
          if (messages.length > MAX_FRAMES_PER_CHUNK) throw new ProtocolError("frame-burst");
        }
      }
      return messages;
    } catch (error) {
      this.failed = true;
      this.body = null;
      throw error;
    }
  }

  end(): void {
    if (this.partial) {
      this.failed = true;
      this.body = null;
      throw new ProtocolError("truncated-frame");
    }
  }
}
