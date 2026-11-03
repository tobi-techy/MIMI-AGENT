// Turns a Spectrum app (cloud or local iMessage) into the bridge's Transport.
// This module imports only Spectrum's core content builders, never a
// provider: each entrypoint (main-cloud.ts, main-local.ts) imports its own
// provider and passes in how to read the sender and the chat type.

import { attachment, voice } from "spectrum-ts";
import type { InboundMessage, MediaPart, Outbound, Part, Transport } from "./types.ts";

/** The parts of a Spectrum message the bridge reads (structural, so tests can fake it). */
export interface SpectrumContent {
  type: string;
  text?: string;
  markdown?: string;
  mimeType?: string;
  name?: string;
  read?: () => Promise<Uint8Array>;
  items?: { content: SpectrumContent }[];
}

export interface SpectrumMessage {
  id: string;
  platform: string;
  direction: "inbound" | "outbound";
  timestamp: Date;
  sender?: { id: string } | undefined;
  content: SpectrumContent;
}

export interface SpectrumSpace {
  id: string;
  send: (...content: never[]) => Promise<unknown>;
}

export interface SpectrumApp {
  messages: AsyncIterable<[SpectrumSpace, SpectrumMessage]>;
  stop: () => Promise<void>;
}

export interface Narrowing {
  /** The sender's phone or email (the provider's narrowed `sender.address`, else `sender.id`). */
  senderOf: (space: SpectrumSpace, message: SpectrumMessage) => string;
  /** True for a group chat. */
  isGroup: (space: SpectrumSpace, message: SpectrumMessage) => boolean;
}

function mediaKind(type: string, mime: string): MediaPart["kind"] {
  if (type === "voice" || mime.startsWith("audio/")) return "voice";
  if (mime.startsWith("image/")) return "image";
  return "file";
}

export function partsOf(c: SpectrumContent): Part[] {
  switch (c.type) {
    case "text":
      return c.text ? [{ kind: "text", text: c.text }] : [];
    case "markdown":
      return c.markdown ? [{ kind: "text", text: c.markdown }] : [];
    case "attachment":
    case "voice": {
      const read = c.read;
      if (!read) return [];
      const mime = (c.mimeType ?? "").toLowerCase();
      return [{ kind: mediaKind(c.type, mime), mime, name: c.name ?? "", read: async () => new Uint8Array(await read()) }];
    }
    case "group":
      return (c.items ?? []).flatMap((i) => partsOf(i.content));
    default:
      // Reactions, read receipts, polls, contacts and the rest are not shop messages.
      return [];
  }
}

/** An iMessage chat guid with ";+;" is a group ("any;-;+1555..." is a DM). */
export function guidLooksGroup(id: string): boolean {
  return id.includes(";+;");
}

export function toOutbound(out: Outbound): unknown {
  if (out.kind === "text") return out.text;
  const buf = Buffer.from(out.bytes);
  return out.kind === "voice"
    ? voice(buf, { name: out.name, mimeType: out.mime })
    : attachment(buf, { name: out.name, mimeType: out.mime });
}

export function spectrumTransport(name: string, app: SpectrumApp, n: Narrowing): Transport {
  return {
    name,
    async *messages(): AsyncIterable<InboundMessage> {
      for await (const [space, message] of app.messages) {
        yield {
          id: message.id,
          platform: message.platform,
          sender: n.senderOf(space, message),
          isGroup: n.isGroup(space, message) || guidLooksGroup(space.id),
          outbound: message.direction === "outbound",
          at: message.timestamp instanceof Date ? message.timestamp : new Date(),
          parts: partsOf(message.content),
          conversation: {
            id: space.id,
            send: async (out) => {
              await (space.send as (c: unknown) => Promise<unknown>)(toOutbound(out));
            },
          },
        };
      }
    },
    stop: () => app.stop(),
  };
}
