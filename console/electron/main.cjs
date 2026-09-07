// Electron's entry must be CommonJS and this package is "type": "module", so
// the build emits a .cjs file; the TypeScript main process is built
// into dist/ by `bun run start` (or `bun run build`) before this runs.
require("./dist/main.cjs");
