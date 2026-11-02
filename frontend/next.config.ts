/** @type {import('next').NextConfig} */
const nextConfig = {
  // Build/dev output dir is env-overridable (default .next) so an isolated e2e dev server
  // can run alongside the main one without fighting over .next/dev/lock.
  distDir: process.env.NEXT_DIST_DIR || '.next',
  output: 'standalone', // For Docker deployment
  // Freedom Ridge's analytical endpoints (field-stats, embeddings/query, map_points)
  // can legitimately exceed Next's default 30s dev-proxy timeout on large
  // datasets — let them run. The setting lives under `experimental` in
  // Next 16 (router-server.js reads `config.experimental.proxyTimeout`).
  experimental: {
    proxyTimeout: 600_000,
  },
  // Next 16 blocks the dev-only /_next resources (HMR, dev chunks) when the page is loaded
  // from a non-localhost origin, so reaching `next dev` from another machine (LAN/Tailscale)
  // serves the HTML but the app never boots. List the host(s) you load the dev server from in
  // ALLOWED_DEV_ORIGINS (comma-separated, e.g. ALLOWED_DEV_ORIGINS=192.0.2.10,dev.example.test)
  // and restart `next dev`. Dev-only; the production build ignores it.
  allowedDevOrigins: (process.env.ALLOWED_DEV_ORIGINS || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean),
  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
        port: '3000',
        pathname: '/media/**',
      },
    ],
  },
  transpilePackages: ['react-map-gl', 'mapbox-gl'],
  async headers() {
    // Baseline security headers for an internet-facing deploy. A full Content-Security-Policy is
    // intentionally NOT set here — it needs dedicated testing against Mapbox GL's blob: workers and
    // Next's inline runtime, and a wrong CSP silently breaks the app. Add it as its own tested step.
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'X-DNS-Prefetch-Control', value: 'off' },
        ],
      },
    ];
  },
  async rewrites() {
    // Backend target is env-overridable (default :8000) so an isolated dev/e2e stack can
    // point the proxy at a throwaway backend on another port without touching the default.
    const backend = process.env.FRIDGE_BACKEND_URL || 'http://localhost:8000';
    return [
      {
        source: '/api/:path*',
        destination: `${backend}/api/:path*`,
      },
      // /media and /files dev rewrites were removed alongside the backend
      // routes they targeted (unauthenticated file serving). All media now
      // flows through /api/samples/{id}/media, covered by the /api rewrite.
    ];
  },
};

export default nextConfig;
