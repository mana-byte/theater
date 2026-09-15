import { appendFile } from "node:fs/promises"
const target = process.env.THEATER_PROBE_PLUGIN_LOG ?? "/dev/null"
export const Probe = async () => {
  await appendFile(target, "loaded\n")
  return {
    dispose: async () => {},
    event: async ({ event }) => {
      await appendFile(target, event.type + "\n")
    },
  }
}
