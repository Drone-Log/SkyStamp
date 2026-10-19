// Cloudflare Worker for getskystamp.app, deployed from the dashboard
// (Workers & Pages → skystamp-markdown), route: getskystamp.app/
//
// Markdown for Agents on the free plan: when a client asks for the home page
// with "Accept: text/markdown", serve /llms.txt as Markdown. Everyone else
// gets the normal HTML from GitHub Pages. Both responses carry Link headers
// pointing agents to the Markdown version and the sitemap.

const LINK = '</llms.txt>; rel="alternate"; type="text/markdown", </sitemap.xml>; rel="sitemap"; type="application/xml"';

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
            "Link": LINK,
          },
        });
      }
    }
    const origin = await fetch(request);
    const res = new Response(origin.body, origin);
    res.headers.append("Link", LINK);
    res.headers.append("Vary", "Accept");
    return res;
  },
};
