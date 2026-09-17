export function identity(value) {
  try {
    const u = new URL(value);
    if (
      !/^https?:$/.test(u.protocol) ||
      u.username ||
      u.password ||
      u.port ||
      !/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(u.hostname) ||
      /(?:^|\.)(?:localhost|local|internal)$/.test(u.hostname)
    )
      return null;
    return (
      u.hostname.replace(/^www\./, "") +
      u.pathname.replace(/\/$/, "") +
      u.search
    );
  } catch {
    return null;
  }
}
export function permitted(url, domains) {
  return (
    identity(url) !== null &&
    domains.includes(new URL(url).hostname.replace(/^www\./, ""))
  );
}
export function deferred(entries, now = Date.now()) {
  return Object.entries(entries || {})
    .filter(([, e]) => e.until > now)
    .map(([id]) => id)
    .slice(-100);
}
export function backoff(old, verification, now = Date.now()) {
  const attempts = (old?.attempts || 0) + 1;
  return {
    attempts,
    until:
      now +
      (verification
        ? 30 * 60_000
        : Math.min(6 * 3600_000, 60_000 * 2 ** Math.min(attempts, 9))),
  };
}

export function readyDomains(domains, waiting) {
  const blocked = new Set(waiting.map((j) => j.domain.replace(/^www\./, "")));
  return domains.filter((d) => !blocked.has(d.replace(/^www\./, "")));
}
