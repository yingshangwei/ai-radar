# Mozilla Readability

`readability.ts` embeds unmodified `Readability.js` from the official `@mozilla/readability` npm package, version **0.6.0**. It is packaged locally, with no third-party CDN request at runtime.

- Upstream: https://github.com/mozilla/readability
- Package: https://registry.npmjs.org/@mozilla/readability/-/readability-0.6.0.tgz
- Package SHA-512: `juG5VWh4qAivzTAeMzvY9xs9HY5rAcr2E4I7tiSSCokRFi7XIZCAu92ZkSTsIj1OPceCifL3cpfteP3pDT9/QQ==`
- Readability.js SHA-256: `34dcab3d0832d0019f02990eed6b6124e029e8c32b9f0c6f2550544ff8dff174`
- License: Apache-2.0, retained in LICENSE-readability.md. The npm distribution does not include a separate NOTICE file; the original source retains all upstream copyright notices.

To update, verify the official package integrity, JSON-encode its Readability.js into the exported string, retain its license, and run the mobile capture tests. Do not modify the embedded upstream source.

Readability parses a cloned document after forms, editable elements, hidden elements, scripts, frames and non-content controls are removed. Only plain text and article links are returned; extracted HTML is never rendered. JSON-LD metadata is disabled. Content remains untrusted and is validated again by the server.
