import { describe, expect, it } from "vitest";
import { dirname, join } from "path";
import { initDbPasswordFile } from "../src/main/database-paths.js";

describe("database bootstrap paths", () => {
  it("keeps the initdb password file outside the empty data directory", () => {
    const dataDir = join("user-data", "postgres", "data");
    const passwordFile = initDbPasswordFile(dataDir, "123-deadbeef");

    expect(dirname(passwordFile)).toBe(dirname(dataDir));
    expect(passwordFile.startsWith(`${dataDir}${process.platform === "win32" ? "\\" : "/"}`)).toBe(false);
    expect(passwordFile).toContain("123-deadbeef");
  });
});
