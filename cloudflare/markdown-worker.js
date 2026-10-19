// Cloudflare Worker for getskystamp.app, deployed from the dashboard
// (Workers & Pages → skystamp-markdown), route: getskystamp.app/
//
// Markdown for Agents on the free plan: when a client asks for the home page
// with "Accept: text/markdown", serve /llms.txt as Markdown. Everyone else
// gets the normal HTML from GitHub Pages.

export default {
  async fetch(request) {
    const accept = request.headers.get("Accept") || "";
    if (request.method === "GET" && accept.includes("text/markdown")) {
      const md = await fetch(new URL("/llms.txt", request.url));
      if (md.ok) {
        return new Response(md.body, {
          headers: {
            "Content-Type": "text/markdown; charset=utf-8",
            "Vary": "Accept",
            "Cache-Control": "public, max-age=600",
          },
        });
      }
    }
    return fetch(request);
  },
};
