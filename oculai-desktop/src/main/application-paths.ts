import { join } from "path";

/** Resolve assets from the compiled main entry at dist/main/main/index.js. */
export function packagedRendererEntry(mainModuleDir: string): string {
  return join(mainModuleDir, "..", "..", "renderer", "index.html");
}

export function packagedPreloadEntry(mainModuleDir: string): string {
  return join(mainModuleDir, "..", "preload", "index.cjs");
}
