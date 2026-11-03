// One JSON line per event. Message bodies stay out unless BRIDGE_LOG_BODIES=1;
// handles are always masked to their last four.

export type Fields = Record<string, unknown>;

export interface Logger {
  info: (event: string, fields?: Fields) => void;
  warn: (event: string, fields?: Fields) => void;
  error: (event: string, fields?: Fields) => void;
  /** Only written when bodies are allowed. */
  body: (event: string, text: string, fields?: Fields) => void;
}

export function makeLogger(logBodies: boolean, sink: (line: string) => void = (l) => console.log(l)): Logger {
  const write = (level: string, event: string, fields: Fields = {}) =>
    sink(JSON.stringify({ at: new Date().toISOString(), level, event, ...fields }));
  return {
    info: (e, f) => write("info", e, f),
    warn: (e, f) => write("warn", e, f),
    error: (e, f) => write("error", e, f),
    body: (e, text, f) => {
      if (logBodies) write("debug", e, { ...f, text });
    },
  };
}

/** A logger that keeps lines in memory (tests). */
export function memoryLogger(logBodies = false): Logger & { lines: Fields[] } {
  const lines: Fields[] = [];
  const log = makeLogger(logBodies, (l) => lines.push(JSON.parse(l) as Fields));
  return Object.assign(log, { lines });
}
