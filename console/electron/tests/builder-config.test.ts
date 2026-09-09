import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";

test("electron-builder.yml names the app, the product, the mac targets and the icon", () => {
  const text = readFileSync(path.resolve(import.meta.dirname, "../electron-builder.yml"), "utf8");
  const config = Bun.YAML.parse(text) as {
    appId?: string;
    productName?: string;
    mac?: { target?: string[]; icon?: string };
  };
  expect(config.appId).toBe("sh.weevil.holophyte.console");
  expect(config.productName).toBe("Holophyte");
  expect(config.mac?.target).toEqual(expect.arrayContaining(["dmg", "dir"]));
  expect(config.mac?.icon).toBe("dist/icon.png");
});
