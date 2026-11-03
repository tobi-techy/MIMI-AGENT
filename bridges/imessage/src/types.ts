// The bridge's own view of a chat transport. Spectrum Cloud, the local Mac
// provider and the test fake all turn their messages into these shapes, so
// the bridge logic never imports an SDK.

export type PartKind = "text" | "image" | "voice" | "file";

export interface TextPart {
  kind: "text";
  text: string;
}

export interface MediaPart {
  kind: "image" | "voice" | "file";
  mime: string;
  name: string;
  /** Read the bytes (downloads on cloud). */
  read: () => Promise<Uint8Array>;
}

export type Part = TextPart | MediaPart;

/** What the bridge can send back into a conversation. */
export type Outbound =
  | { kind: "text"; text: string }
  | { kind: "image" | "voice"; bytes: Uint8Array; mime: string; name: string };

/** A conversation the bridge can reply into (a DM). */
export interface Conversation {
  /** Stable id of the chat (the iMessage chat guid). */
  id: string;
  send: (out: Outbound) => Promise<void>;
}

export interface InboundMessage {
  /** The provider's message id (the iMessage guid): the dedupe key. */
  id: string;
  /** "imessage" (cloud) or "local_imessage" (Mac). */
  platform: string;
  /** The sender's phone or email as the provider gave it. */
  sender: string;
  /** Group chats are always ignored. */
  isGroup: boolean;
  /** Our own echoes are ignored. */
  outbound: boolean;
  at: Date;
  parts: Part[];
  conversation: Conversation;
}

/** A source of inbound messages: Spectrum Cloud, the Mac or a fake. */
export interface Transport {
  readonly name: string;
  messages: () => AsyncIterable<InboundMessage>;
  stop: () => Promise<void>;
}
