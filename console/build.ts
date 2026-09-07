import { cp, readdir } from "node:fs/promises";
import tailwind from "bun-plugin-tailwind";

const here = new URL("./", import.meta.url).pathname;
const publicDir = `${here}public/`;

// Bun's HTML bundler resolves every `href` it finds, including the
// root-relative icon and manifest links, and would hash and rename them.
// They live in `public/`, are copied to `dist/` unchanged, and must keep
// their names because the manifest names the PNGs by path; this plugin
// answers each such link with its site path, marked external, once the
// file is known to exist, so a typo fails the build rather than becoming
// a dead link.
async function publicFiles(): Promise<Set<string>> {
  return new Set((await readdir(publicDir, { recursive: true })).map(String));
}

function keepPublic(files: Set<string>): Bun.BunPlugin {
  return {
    name: "keep-public",
    setup(build) {
      build.onResolve({ filter: /.*/ }, (args) => {
        if (!args.importer.endsWith(".html") || !args.path.startsWith(here)) return;
        const site = args.path.slice(here.length);
        return files.has(site) ? { path: `/${site}`, external: true } : undefined;
      });
    },
  };
}

// Bundles index.html (and the script and stylesheet it references) into
// `outdir`, then copies `public/` (icons, manifest) in unchanged. Returns
// the emitted and copied paths relative to `outdir` so callers can report
// or inspect them; throws with the bundler's logs on failure.
export async function buildConsole(outdir: string = `${here}dist/`): Promise<string[]> {
  const files = await publicFiles();
  const result = await Bun.build({
    entrypoints: [`${here}index.html`],
    outdir,
    plugins: [tailwind, keepPublic(files)],
    minify: true,
    sourcemap: "linked",
  });
  if (!result.success) {
    throw new Error(result.logs.map((log) => String(log)).join("\n"));
  }
  await cp(publicDir, outdir, { recursive: true });
  return [...result.outputs.map((artifact) => artifact.path.slice(outdir.length)), ...files];
}

if (import.meta.main) {
  try {
    for (const path of await buildConsole()) console.log(path);
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  }
}
