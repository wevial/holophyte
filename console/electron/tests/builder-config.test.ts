import { expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

test("electron-builder.yml names the app, the product, the mac targets and the icon", () => {
  const text = readFileSync(path.resolve(import.meta.dirname, "../electron-builder.yml"), "utf8");
  const config = Bun.YAML.parse(text) as {
    appId?: string;
    productName?: string;
    mac?: { target?: string[]; icon?: string; identity?: string | null; hardenedRuntime?: boolean };
    extraResources?: { from: string; to: string }[];
  };
  expect(config.appId).toBe("sh.weevil.holophyte.console");
  expect(config.productName).toBe("Holophyte");
  expect(config.mac?.target).toEqual(expect.arrayContaining(["dmg", "dir"]));
  expect(config.mac?.icon).toBe("dist/icon.png");
});

test("electron-builder.yml asks for ad-hoc signing explicitly", () => {
  // Unset `identity` means "search the keychain, else skip signing" in the
  // locked builder; only the literal "-" yields a signed app without a
  // certificate, and the hardened runtime must be off for that identity to
  // accept Electron's pre-signed frameworks.
  const text = readFileSync(path.resolve(import.meta.dirname, "../electron-builder.yml"), "utf8");
  const config = Bun.YAML.parse(text) as { mac?: { identity?: string | null; hardenedRuntime?: boolean } };
  expect(config.mac?.identity).toBe("-");
  expect(config.mac?.hardenedRuntime).toBe(false);
});

test("electron-builder.yml carries both tray template PNGs into the bundle's resources", () => {
  const text = readFileSync(path.resolve(import.meta.dirname, "../electron-builder.yml"), "utf8");
  const config = Bun.YAML.parse(text) as { extraResources?: { from: string; to: string }[] };
  const targets = (config.extraResources ?? []).map((r) => r.to);
  expect(targets).toEqual(
    expect.arrayContaining(["assets/menubar-template@1x.png", "assets/menubar-template@2x.png"]),
  );
  for (const r of config.extraResources ?? []) {
    expect(existsSync(path.resolve(import.meta.dirname, "..", r.from))).toBe(true);
  }
});
