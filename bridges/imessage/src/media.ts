// Attachment handling. The Mimi API takes JPEG, PNG or WebP photos, Ogg,
// WebM, M4A, MP3 or WAV voice notes and PDFs (checked by their first bytes,
// at most 10 MB). iMessage voice notes are Core Audio files (.caf, Opus or
// AMR inside), and iPhone photos can be HEIC, so those go through ffmpeg.

import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const MAX_BYTES = 10 * 1024 * 1024;

export type Target = "ogg" | "jpg" | "m4a";

/** Turns bytes into another format. The real one shells out to ffmpeg; tests use a fake. */
export interface Converter {
  convert: (bytes: Uint8Array, target: Target) => Promise<Uint8Array>;
}

/** What the first bytes say the file is (the API checks the same way). */
export function sniff(b: Uint8Array): string {
  const s = (o: number, n: number) => String.fromCharCode(...b.subarray(o, o + n));
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  if (b[0] === 0x89 && s(1, 3) === "PNG") return "image/png";
  if (s(0, 4) === "RIFF" && s(8, 4) === "WEBP") return "image/webp";
  if (s(0, 4) === "RIFF" && s(8, 4) === "WAVE") return "audio/wav";
  if (s(0, 4) === "OggS") return "audio/ogg";
  if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return "audio/webm";
  if (s(0, 4) === "%PDF") return "application/pdf";
  if (s(0, 3) === "ID3" || (b[0] === 0xff && ((b[1] ?? 0) & 0xe0) === 0xe0)) return "audio/mpeg";
  if (s(0, 4) === "caff") return "audio/x-caf";
  if (s(4, 4) === "ftyp") {
    const brand = s(8, 4);
    if (/^(heic|heix|hevc|heim|heis|mif1|msf1)/.test(brand)) return "image/heic";
    if (/^(M4A |M4B |mp42|isom|mp41|3gp)/.test(brand)) return "audio/mp4";
    return "video/mp4";
  }
  if (s(0, 4) === "GIF8") return "image/gif";
  return "application/octet-stream";
}

const ACCEPTED = new Set([
  "image/jpeg", "image/png", "image/webp",
  "audio/ogg", "audio/webm", "audio/mp4", "audio/mpeg", "audio/wav",
  "application/pdf",
]);

export interface Prepared {
  bytes: Uint8Array;
  mime: string;
}

/** Bytes the API will accept, converting when it has to. Throws a plain reason when it cannot. */
export async function prepareForApi(bytes: Uint8Array, kind: "image" | "voice" | "file", conv: Converter): Promise<Prepared> {
  let mime = sniff(bytes);
  let out = bytes;
  if (!ACCEPTED.has(mime)) {
    let target: Target;
    if (kind === "voice" || mime.startsWith("audio/") || mime === "video/mp4") target = "ogg";
    else if (kind === "image" || mime.startsWith("image/")) target = "jpg";
    else throw new UnsupportedMedia("that file type");
    try {
      out = await conv.convert(bytes, target);
    } catch {
      throw new UnsupportedMedia("a file I could not convert");
    }
    mime = sniff(out);
    if (!ACCEPTED.has(mime)) throw new UnsupportedMedia("that file type");
  }
  if (out.byteLength > MAX_BYTES) throw new UnsupportedMedia("a file over 10 MB");
  return { bytes: out, mime };
}

export class UnsupportedMedia extends Error {}

export function apiKind(mime: string): "image" | "voice" | "document" {
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "voice";
  return "document";
}

const ARGS: Record<Target, string[]> = {
  ogg: ["-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg"],
  m4a: ["-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-f", "ipod"],
  jpg: ["-frames:v", "1", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg"],
};

/** ffmpeg in a temp folder: in.bin to out.<target>. */
export function ffmpegConverter(ffmpeg = "ffmpeg", timeoutMs = 60_000): Converter {
  return {
    async convert(bytes, target) {
      const dir = await mkdtemp(join(tmpdir(), "imsg-bridge-"));
      const src = join(dir, "in.bin");
      const dst = join(dir, `out.${target}`);
      try {
        await writeFile(src, bytes);
        await new Promise<void>((ok, fail) => {
          execFile(ffmpeg, ["-hide_banner", "-loglevel", "error", "-y", "-i", src, ...ARGS[target], dst],
            { timeout: timeoutMs }, (err) => (err ? fail(new UnsupportedMedia("a file I could not convert")) : ok()));
        });
        return new Uint8Array(await readFile(dst));
      } finally {
        await rm(dir, { recursive: true, force: true });
      }
    },
  };
}
