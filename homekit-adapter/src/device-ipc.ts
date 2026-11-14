import { EventEmitter } from "node:events";
import type { Socket } from "node:net";
import { ProtocolError } from "./protocol.js";
import {
  encodeDeviceEnvelope,
  DeviceFrameDecoder,
  type DeviceEnvelope,
} from "./device-protocol.js";

export const IO_TIMEOUT_MS = 5_000;
export const MAX_PENDING_WRITES = 32;
export const MAX_PENDING_BYTES = 512 * 1024;

interface PendingWrite {
  frame: Buffer;
  timer: NodeJS.Timeout;
  resolve: () => void;
  reject: (error: Error) => void;
}

export interface ChannelOptions {
  ioTimeoutMs?: number;
  maximumPendingBytes?: number;
  maximumPendingWrites?: number;
}

/** One inherited stream, bounded framed input and bounded callback-drained output. */
export class DeviceControlChannel extends EventEmitter {
  private decoder = new DeviceFrameDecoder();
  private queue: PendingWrite[] = [];
  private queuedBytes = 0;
  private partialTimer: NodeJS.Timeout | null = null;
  private closed = false;
  private writing = false;
  private readonly timeout: number;
  private readonly maximumBytes: number;
  private readonly maximumWrites: number;

  constructor(
    private readonly socket: Socket,
    options: ChannelOptions = {},
  ) {
    super();
    this.timeout = options.ioTimeoutMs ?? IO_TIMEOUT_MS;
    this.maximumBytes = options.maximumPendingBytes ?? MAX_PENDING_BYTES;
    this.maximumWrites = options.maximumPendingWrites ?? MAX_PENDING_WRITES;
    socket.on("data", (chunk: Buffer) => this.receive(chunk));
    socket.on("error", () => this.fail(new ProtocolError("control-io")));
    socket.on("end", () => {
      try {
        this.decoder.end();
      } catch {
        this.fail(new ProtocolError("truncated-frame"));
        return;
      }
      this.close();
    });
    socket.on("close", () => this.close());
  }

  get pendingBytes(): number {
    return this.queuedBytes;
  }
  get pendingWrites(): number {
    return this.queue.length;
  }
  get isClosed(): boolean {
    return this.closed;
  }

  send(envelope: DeviceEnvelope): Promise<void> {
    if (this.closed) return Promise.reject(new ProtocolError("channel-closed"));
    let frame: Buffer;
    try {
      frame = encodeDeviceEnvelope(envelope);
    } catch (error) {
      return Promise.reject(error);
    }
    if (
      this.queue.length >= this.maximumWrites ||
      this.queuedBytes + frame.length > this.maximumBytes
    ) {
      return Promise.reject(new ProtocolError("write-capacity"));
    }
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => this.fail(new ProtocolError("write-timeout")),
        this.timeout,
      );
      const item = { frame, timer, resolve, reject };
      this.queue.push(item);
      this.queuedBytes += frame.length;
      this.pump();
    });
  }

  private pump(): void {
    if (this.closed || this.writing || !this.queue.length) return;
    const item = this.queue[0]!;
    this.writing = true;
    try {
      this.socket.write(item.frame, (error) => {
        if (this.closed) return;
        if (error) {
          this.fail(new ProtocolError("control-io"));
          return;
        }
        this.queue.shift();
        this.queuedBytes -= item.frame.length;
        clearTimeout(item.timer);
        this.writing = false;
        item.resolve();
        this.pump();
      });
    } catch {
      this.fail(new ProtocolError("control-io"));
    }
  }

  private receive(chunk: Buffer): void {
    if (this.closed) return;
    try {
      const messages = this.decoder.feed(chunk);
      if (messages.length || !this.decoder.partial) {
        if (this.partialTimer !== null) clearTimeout(this.partialTimer);
        this.partialTimer = null;
      }
      if (this.decoder.partial && this.partialTimer === null) {
        this.partialTimer = setTimeout(
          () => this.fail(new ProtocolError("partial-frame-timeout")),
          this.timeout,
        );
      }
      for (const message of messages) {
        if (this.closed) break;
        this.emit("message", message);
      }
    } catch (error) {
      this.fail(
        error instanceof ProtocolError
          ? error
          : new ProtocolError("invalid-frame"),
      );
    }
  }

  close(): void {
    this.finish(new ProtocolError("channel-closed"));
  }

  private fail(error: Error): void {
    if (this.closed) return;
    this.emit("failure", error);
    this.finish(error);
  }

  private finish(error: Error): void {
    if (this.closed) return;
    this.closed = true;
    if (this.partialTimer !== null) clearTimeout(this.partialTimer);
    this.partialTimer = null;
    for (const item of this.queue) {
      clearTimeout(item.timer);
      item.reject(error);
    }
    this.queue = [];
    this.queuedBytes = 0;
    this.socket.destroy();
    this.emit("closed");
  }
}
