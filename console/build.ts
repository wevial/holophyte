import { cp, readdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import tailwind from "bun-plugin-tailwind";

const here = new URL("./", import.meta.url).pathname;
const publicDir = `${here}public/`;

// Holds the git tree id of `console/` the bundle was built from, read
// before bundling and refused if it moved by the end; the host daemon
// compares it with `HEAD:console` at startup and rebuilds on a mismatch.
// Outside a git checkout there is no tree id and no stamp.
export const STAMP = "source-tree";

function sourceTree(): string | null {
  const done = Bun.spawnSync(["git", "rev-parse", "HEAD:./"], { cwd: here, stderr: "ignore" });
  return done.success ? done.stdout.toString().trim() : null;
}

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
// `outdir`, copies `public/` (icons, manifest) in unchanged and writes the
// stamp. Returns the emitted and copied paths relative to `outdir` so
// callers can report or inspect them; throws with the bundler's logs on
// failure.
export async function buildConsole(outdir: string = `${here}dist/`): Promise<string[]> {
  const tree = sourceTree();
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
  const after = sourceTree();
  if (after !== tree) {
    throw new Error(`console/ moved from tree ${tree} to ${after} during the build; not stamped`);
  }
  if (tree) await writeFile(`${outdir}${STAMP}`, `${tree}\n`);
  return [
    ...result.outputs.map((artifact) => artifact.path.slice(outdir.length)),
    ...files,
    ...(tree ? [STAMP] : []),
  ];
}

if (import.meta.main) {
  try {
    const outdir = process.argv[2] ? `${resolve(process.argv[2])}/` : undefined;
    for (const path of await buildConsole(outdir)) console.log(path);
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  }
}
