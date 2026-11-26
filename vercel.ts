const backend = new URL(process.env.LIBRAVERSE_RENDER_ORIGIN || "https://invalid.example");
if (
  backend.protocol !== "https:" ||
  !backend.hostname.endsWith(".onrender.com") ||
  backend.username || backend.password || backend.port ||
  backend.pathname !== "/" || backend.search || backend.hash
) {
  throw new Error("Set LIBRAVERSE_RENDER_ORIGIN to the HTTPS Render service origin.");
}

export const config = {
  framework: null,
  buildCommand: "node scripts/build_vercel.mjs",
  outputDirectory: "vercel-public",
  rewrites: [
    {
      source: "/static/admin/:path*",
      destination: `${backend.origin}/static/admin/:path*`,
    },
    {
      source: "/:path((?!static/).*)",
      destination: `${backend.origin}/:path*`,
    },
  ],
};
