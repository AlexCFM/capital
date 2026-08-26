// CAPITAL — now-playing metadata relay
//
// Browsers can't read ICY StreamTitle metadata directly from the Icecast
// stream (icecast-vgtrk.cdnvideo.ru) because sending the required
// `Icy-MetaData: 1` header triggers a CORS preflight that the CDN rejects.
// This worker makes that same request server-to-server (no CORS involved
// between servers), reads just enough of the stream to grab one metadata
// block, and returns the track title as plain text with CORS headers set
// so the site's own fetch() can read it.
//
// Deploy: Cloudflare dashboard -> Workers & Pages -> Create -> paste this
// in, deploy. Copy the resulting https://<name>.<subdomain>.workers.dev
// URL and send it back — it goes straight into METADATA_URL in index.html.

const STREAM_URL = 'https://icecast-vgtrk.cdnvideo.ru/capitalfmmp3';
const ICY_METAINT = 16000; // fixed for this stream — see index.html comment
const MAX_META_BLOCK = 4080; // ICY metadata blocks are at most 255 * 16 bytes

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Cache-Control': 'no-store',
  'Content-Type': 'text/plain; charset=utf-8',
};

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    try {
      const upstream = await fetch(STREAM_URL, {
        headers: { 'Icy-MetaData': '1' },
      });

      const reader = upstream.body.getReader();
      const needed = ICY_METAINT + 1 + MAX_META_BLOCK;
      let buffer = new Uint8Array(0);

      while (buffer.length < needed) {
        const { done, value } = await reader.read();
        if (done) break;
        const merged = new Uint8Array(buffer.length + value.length);
        merged.set(buffer, 0);
        merged.set(value, buffer.length);
        buffer = merged;
      }
      reader.cancel().catch(() => {});

      if (buffer.length <= ICY_METAINT) {
        return new Response('', { headers: CORS_HEADERS });
      }

      const lenByte = buffer[ICY_METAINT];
      const metaLen = lenByte * 16;
      const metaBytes = buffer.slice(ICY_METAINT + 1, ICY_METAINT + 1 + metaLen);
      const metaText = new TextDecoder('utf-8').decode(metaBytes);
      // The opening quote (either ' or ") is captured and reused as the
      // closing delimiter, so an apostrophe inside the title itself (e.g.
      // "Baby Don't...") doesn't get mistaken for the field's end quote —
      // only a matching quote immediately followed by ';' closes the field.
      const match = /StreamTitle=(['"])([\s\S]*?)\1;/.exec(metaText);
      const title = match ? match[2] : '';

      return new Response(title, { headers: CORS_HEADERS });
    } catch (e) {
      return new Response('', { headers: CORS_HEADERS });
    }
  },
};
