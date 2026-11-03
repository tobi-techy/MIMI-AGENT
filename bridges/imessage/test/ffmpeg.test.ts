// The real ffmpeg converter on real files. Skips when ffmpeg is not installed.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { ffmpegConverter, prepareForApi, sniff } from "../src/media.ts";

let have = true;
try {
  execFileSync("ffmpeg", ["-version"], { stdio: "ignore" });
} catch {
  have = false;
}

function make(args: string[], name: string): Uint8Array {
  const dir = mkdtempSync(join(tmpdir(), "ff-"));
  const out = join(dir, name);
  execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", ...args, out]);
  return new Uint8Array(readFileSync(out));
}

describe("ffmpeg converter", { skip: !have && "ffmpeg not installed" }, () => {
  it("turns a Core Audio voice note (.caf) into Ogg the API accepts", async () => {
    const caf = make(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", "-f", "caf"], "a.caf");
    assert.equal(sniff(caf), "audio/x-caf");
    const ready = await prepareForApi(caf, "voice", ffmpegConverter());
    assert.equal(ready.mime, "audio/ogg");
  });

  it("turns Mimi's Ogg voice reply into m4a for iMessage", async () => {
    const ogg = make(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", "-f", "ogg"], "a.ogg");
    const m4a = await ffmpegConverter().convert(ogg, "m4a");
    assert.equal(sniff(m4a), "audio/mp4");
  });

  it("turns a BMP or TIFF photo into JPEG", async () => {
    const bmp = make(["-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", "-f", "image2", "-c:v", "bmp"], "a.bmp");
    const ready = await prepareForApi(bmp, "image", ffmpegConverter());
    assert.equal(ready.mime, "image/jpeg");
  });
});
