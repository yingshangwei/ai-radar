import { isWebURL, type MobileArticle } from "./mobileCapture";

export type DeviceSitePermission = {
  allowed: boolean;
  needsVerification: boolean;
  updatedAt: number;
};
export type DeviceSitePermissions = Record<string, DeviceSitePermission>;
export type DeviceDocument = {
  document_id: string;
  url: string;
  domain: string;
};

export function mobilePageURL(value: string): string {
  if (!isWebURL(value)) return value;
  // Keep this usable with React Native's getter-only URL implementation.
  return new URL(value.replace(/^http:/i, "https:")).href;
}

export function siteHost(value: string): string {
  try {
    const url = new URL(value.includes("://") ? value : `https://${value}`);
    return url.hostname
      .toLowerCase()
      .replace(/^www\./, "")
      .replace(/\.$/, "");
  } catch {
    return "";
  }
}

export function permittedPage(url: string, domain: string): boolean {
  return (
    isWebURL(url) && !!siteHost(domain) && siteHost(url) === siteHost(domain)
  );
}

export function targetPage(url: string, target: string): boolean {
  if (!isWebURL(url) || !isWebURL(target) || siteHost(url) !== siteHost(target))
    return false;
  const identity = (value: string) => {
    const parsed = new URL(value);
    for (const key of Array.from(parsed.searchParams.keys()))
      if (/^utm_/i.test(key) || ["fbclid", "gclid"].includes(key.toLowerCase()))
        parsed.searchParams.delete(key);
    parsed.searchParams.sort();
    return `${parsed.pathname.replace(/\/$/, "")}?${parsed.searchParams.toString()}`;
  };
  return identity(url) === identity(target);
}

export function automaticDomains(permissions: DeviceSitePermissions): string[] {
  return Object.entries(permissions)
    .filter(
      ([domain, value]) =>
        !!siteHost(domain) && value.allowed && !value.needsVerification,
    )
    .map(([domain]) => siteHost(domain))
    .slice(0, 30);
}

export function eligibleDeviceDocument(
  doc: DeviceDocument,
  domains: string[],
  deferred: Record<string, number>,
  now: number,
): boolean {
  return (
    /^[a-f0-9]{64}$/.test(doc.document_id) &&
    domains.includes(siteHost(doc.domain)) &&
    permittedPage(doc.url, doc.domain) &&
    (deferred[doc.document_id] || 0) <= now
  );
}

export function canRunDeviceReading(
  appState: string,
  paused: boolean,
  demo: boolean,
): boolean {
  return appState === "active" && !paused && !demo;
}

export function deferredDocumentIds(
  deferred: Record<string, number>,
  now: number,
): string[] {
  return Object.entries(deferred)
    .filter(
      ([id, time]) =>
        /^[a-f0-9]{64}$/.test(id) && typeof time === "number" && time > now,
    )
    .sort((a, b) => b[1] - a[1])
    .slice(0, 100)
    .map(([id]) => id);
}

export function automaticCapture(
  document: DeviceDocument,
  article: MobileArticle,
  permissions: DeviceSitePermissions,
) {
  if (
    !permissions[siteHost(document.domain)]?.allowed ||
    !permittedPage(article.url, document.domain) ||
    !targetPage(article.url, document.url)
  )
    return null;
  return {
    document_id: document.document_id,
    ...article,
    title: article.title || `${document.domain} 文章正文`,
    method: "mobile_browser",
  };
}
