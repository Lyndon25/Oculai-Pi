import { describe, expect, it } from "vitest";
import { join } from "path";
import { packagedPreloadEntry, packagedRendererEntry } from "../src/main/application-paths.js";

describe("compiled application paths", () => {
  const mainModuleDir = join("app.asar", "dist", "main", "main");

  it("resolves the renderer outside the nested main output", () => {
    expect(packagedRendererEntry(mainModuleDir)).toBe(
      join("app.asar", "dist", "renderer", "index.html"),
    );
  });

  it("resolves the CommonJS preload alongside compiled main output", () => {
    expect(packagedPreloadEntry(mainModuleDir)).toBe(
      join("app.asar", "dist", "main", "preload", "index.cjs"),
    );
  });
});
