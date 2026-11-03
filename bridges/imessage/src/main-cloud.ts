// Spectrum Cloud iMessage (hosted lines from Photon). The default entrypoint.
// Needs SPECTRUM_PROJECT_ID and SPECTRUM_PROJECT_SECRET in the environment.
// Never import the local Mac provider here (see main-local.ts).

import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { start } from "./run.ts";
import { type SpectrumApp, type SpectrumMessage, type SpectrumSpace, spectrumTransport } from "./spectrum.ts";

function need(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set (see .env.example)`);
  return v;
}

await start(async () => {
  const app = await Spectrum({
    projectId: need("SPECTRUM_PROJECT_ID"),
    projectSecret: need("SPECTRUM_PROJECT_SECRET"),
    providers: [imessage.config()],
  });
  return spectrumTransport("spectrum-cloud-imessage", app as unknown as SpectrumApp, {
    senderOf: (_space: SpectrumSpace, message: SpectrumMessage) => {
      if (message.platform !== "imessage") return message.sender?.id ?? "";
      const narrowed = imessage(message as never) as { sender?: { id?: string; address?: string } };
      return narrowed.sender?.address || narrowed.sender?.id || message.sender?.id || "";
    },
    isGroup: (space: SpectrumSpace, message: SpectrumMessage) => {
      if (message.platform !== "imessage") return true; // only cloud iMessage DMs are handled here
      return (imessage(space as never) as { type?: string }).type === "group";
    },
  });
});
