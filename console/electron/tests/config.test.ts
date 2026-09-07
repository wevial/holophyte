import { describe, expect, test } from "bun:test";

import { DEFAULT_CONSOLE_URL, resolveConsoleUrl } from "../config.ts";

describe("resolveConsoleUrl precedence", () => {
  test("the environment variable wins over the config file and the default", () => {
    const result = resolveConsoleUrl(
      { HOLOPHYTE_CONSOLE_URL: "https://writer.example:7710/" },
      JSON.stringify({ url: "http://10.0.0.2:7710/" }),
    );
    expect(result).toEqual({
      url: "https://writer.example:7710/",
      source: "HOLOPHYTE_CONSOLE_URL",
    });
  });

  test("the config file's url is used when the variable is absent", () => {
    const result = resolveConsoleUrl({}, JSON.stringify({ url: "http://10.0.0.2:7710/" }));
    expect(result).toEqual({ url: "http://10.0.0.2:7710/", source: "console.json" });
  });

  test("an empty variable does not shadow the config file", () => {
    const result = resolveConsoleUrl(
      { HOLOPHYTE_CONSOLE_URL: "" },
      JSON.stringify({ url: "http://10.0.0.2:7710/" }),
    );
    expect(result).toEqual({ url: "http://10.0.0.2:7710/", source: "console.json" });
  });

  test("with neither source the default is the first target's port", () => {
    expect(resolveConsoleUrl({}, null)).toEqual({
      url: "http://127.0.0.1:7710/",
      source: "default",
    });
    expect(DEFAULT_CONSOLE_URL).toBe("http://127.0.0.1:7710/");
  });
});

describe("resolveConsoleUrl errors name their source and yield no url", () => {
  test("invalid JSON in the config file", () => {
    const result = resolveConsoleUrl({}, "{ url: nope");
    expect(result).not.toHaveProperty("url");
    expect(result).toMatchObject({ source: "console.json" });
    expect((result as { error: string }).error).toMatch(/JSON/);
  });

  test("a url that is not a string", () => {
    const result = resolveConsoleUrl({}, JSON.stringify({ url: 7710 }));
    expect(result).not.toHaveProperty("url");
    expect(result).toMatchObject({ source: "console.json" });
    expect((result as { error: string }).error).toMatch(/string/);
  });

  test("a file: scheme from the config file", () => {
    const result = resolveConsoleUrl({}, JSON.stringify({ url: "file:///etc/passwd" }));
    expect(result).not.toHaveProperty("url");
    expect(result).toMatchObject({ source: "console.json" });
    expect((result as { error: string }).error).toMatch(/file:/);
  });

  test("a non-http scheme from the environment does not fall through to the file", () => {
    const result = resolveConsoleUrl(
      { HOLOPHYTE_CONSOLE_URL: "ftp://writer.example/" },
      JSON.stringify({ url: "http://10.0.0.2:7710/" }),
    );
    expect(result).not.toHaveProperty("url");
    expect(result).toMatchObject({ source: "HOLOPHYTE_CONSOLE_URL" });
  });

  test("a string that is not a URL at all", () => {
    const result = resolveConsoleUrl({ HOLOPHYTE_CONSOLE_URL: "not a url" }, null);
    expect(result).not.toHaveProperty("url");
    expect(result).toMatchObject({ source: "HOLOPHYTE_CONSOLE_URL" });
  });
});
