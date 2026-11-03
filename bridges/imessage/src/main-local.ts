// Optional fallback: the Mac's own Messages app through @spectrum-ts/imessage-local.
// Install it only on the Mac (npm install @spectrum-ts/imessage-local); it is
// not in package.json so cloud installs never pull its native SQLite deps.
// Never import the cloud provider here (see main-cloud.ts).

import { localIMessage } from "@spectrum-ts/imessage-local";
import { Spectrum } from "spectrum-ts";
import { start } from "./run.ts";
import { type SpectrumApp, type SpectrumMessage, type SpectrumSpace, spectrumTransport } from "./spectrum.ts";

await start(async () => {
  const app = await Spectrum({ providers: [localIMessage.config()] });
  return spectrumTransport("spectrum-local-imessage", app as unknown as SpectrumApp, {
    senderOf: (_space: SpectrumSpace, message: SpectrumMessage) => message.sender?.id ?? "",
    isGroup: (space: SpectrumSpace, message: SpectrumMessage) => {
      if (message.platform !== "local_imessage") return true;
      return (localIMessage(space as never) as { type?: string }).type === "group";
    },
  });
});
