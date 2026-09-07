// Electron's entry must be CommonJS; the TypeScript main process is built
// into dist/ by `bun run start` (or `bun run build`) before this runs.
require("./dist/main.js");
