import { cpSync, mkdirSync } from "node:fs";

mkdirSync("vercel-public", { recursive: true });
cpSync("static", "vercel-public/static", { recursive: true });
