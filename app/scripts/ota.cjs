#!/usr/bin/env node
// Official Expo SDK produces the bundles/fingerprint; the signing key stays offline.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const { createFingerprintAsync } = require("@expo/fingerprint");
const root = path.resolve(__dirname, "..");
const repo = path.dirname(root);
const read = (p) => JSON.parse(fs.readFileSync(p, "utf8"));
const hash = (bytes, algorithm = "sha256", encoding = "hex") =>
  crypto.createHash(algorithm).update(bytes).digest(encoding);
const platformOK = (p) => ["android", "ios"].includes(p);
const mime = {
  bundle: "application/javascript",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
  ttf: "font/ttf",
  otf: "font/otf",
  woff: "font/woff",
  woff2: "font/woff2",
  bin: "application/octet-stream",
};

async function fingerprint(platform) {
  return (
    await createFingerprintAsync(root, { platforms: [platform], silent: true })
  ).hash;
}
function write(p, value) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(
    p,
    typeof value === "string" || Buffer.isBuffer(value)
      ? value
      : JSON.stringify(value),
    { flag: "wx" },
  );
}
function configuration() {
  const c = read(path.join(root, "app.json")).expo;
  if (
    typeof c.runtimeVersion !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(c.runtimeVersion)
  )
    throw Error("An explicit safe runtimeVersion is required");
  if (c.updates?.url !== "https://radar.yswdra.cn/updates/v1/manifest")
    throw Error("Unexpected update origin");
  return c;
}
async function compatible(platform) {
  const c = configuration();
  const baseline = read(path.join(root, "updates", `native-${platform}.json`));
  if (
    baseline.runtimeVersion !== c.runtimeVersion ||
    baseline.fingerprint !== (await fingerprint(platform))
  )
    throw Error(
      "Native fingerprint changed. Build/test/install a new base with a new runtimeVersion; OTA publication refused.",
    );
  return c;
}
function sign(body) {
  const keyPath =
    process.env.RADAR_OTA_PRIVATE_KEY ||
    path.join(repo, "credentials/ota/private-key.pem");
  if (fs.statSync(keyPath).mode & 0o077)
    throw Error("Signing key permissions must be 0600");
  const key = fs.readFileSync(keyPath);
  const cert = new crypto.X509Certificate(
    fs.readFileSync(path.join(root, "updates/certificate.pem")),
  );
  if (
    Date.now() < Date.parse(cert.validFrom) ||
    Date.now() >= Date.parse(cert.validTo)
  )
    throw Error("Signing certificate is not valid now");
  const signature = crypto.sign("RSA-SHA256", body, key);
  if (!crypto.verify("RSA-SHA256", body, cert.publicKey, signature))
    throw Error("Private key does not match the embedded certificate");
  return signature.toString("base64");
}
async function main(argv) {
  const [command, platform, target, channel = "preview"] = argv;
  if (
    !platformOK(platform) ||
    !target ||
    !["stable", "preview"].includes(channel)
  )
    throw Error(
      "Usage: node scripts/ota.cjs baseline <android|ios> <tested-base.apk|ipa> | prepare <platform> <new-output-dir> [preview|stable] | rollback <platform> <new-output-dir> [preview|stable]",
    );
  if (command === "baseline") {
    const c = configuration();
    const binary = path.resolve(target);
    if (!binary.endsWith(platform === "android" ? ".apk" : ".ipa"))
      throw Error("Supply the tested native binary");
    const data = {
      runtimeVersion: c.runtimeVersion,
      fingerprint: await fingerprint(platform),
      binarySha256: hash(fs.readFileSync(binary)),
      binaryName: path.basename(binary),
    };
    const dest = path.join(root, "updates", `native-${platform}.json`);
    if (
      fs.existsSync(dest) &&
      read(dest).runtimeVersion === c.runtimeVersion &&
      read(dest).fingerprint !== data.fingerprint
    )
      throw Error(
        "Do not replace a different native fingerprint under an existing runtimeVersion",
      );
    fs.writeFileSync(dest, JSON.stringify(data, null, 2) + "\n");
    console.log(JSON.stringify(data));
    return;
  }
  if (!["prepare", "rollback"].includes(command))
    throw Error("Unknown command");
  const c = await compatible(platform);
  const output = path.resolve(target);
  if (fs.existsSync(output)) throw Error("Output directory already exists");
  fs.mkdirSync(output, { recursive: true });
  const release = crypto.randomUUID();
  const createdAt = new Date().toISOString();
  let kind, payload;
  if (command === "prepare") {
    const exported = path.join(output, "_export");
    execFileSync(
      "pnpm",
      [
        "exec",
        "expo",
        "export",
        "--platform",
        platform,
        "--output-dir",
        exported,
      ],
      {
        cwd: root,
        stdio: "inherit",
        env: { ...process.env, NODE_ENV: "production", EXPO_NO_DOTENV: "1" },
      },
    );
    const metadata = read(path.join(exported, "metadata.json")).fileMetadata[
      platform
    ];
    const asset = (file, extension) => {
      const input = path.resolve(exported, file);
      if (
        !input.startsWith(exported + path.sep) ||
        fs.lstatSync(input).isSymbolicLink() ||
        !mime[extension]
      )
        throw Error("Unsafe exported asset");
      const bytes = fs.readFileSync(input);
      const filename = `${hash(bytes)}.${extension}`;
      const dest = path.join(output, "assets", filename);
      if (!fs.existsSync(dest)) write(dest, bytes);
      return {
        hash: hash(bytes, "sha256", "base64url"),
        key: hash(bytes, "md5"),
        contentType: mime[extension],
        fileExtension: "." + extension,
        url: `https://radar.yswdra.cn/updates/assets/${filename}`,
      };
    };
    const extra = {
      name: c.name,
      slug: c.slug,
      version: c.version,
      runtimeVersion: c.runtimeVersion,
      android: {
        package: c.android.package,
        versionCode: c.android.versionCode,
      },
      ios: {
        bundleIdentifier: c.ios.bundleIdentifier,
        buildNumber: c.ios.buildNumber,
      },
    };
    payload = {
      id: crypto.randomUUID(),
      createdAt,
      runtimeVersion: c.runtimeVersion,
      launchAsset: asset(metadata.bundle, "bundle"),
      assets: metadata.assets.map((a) => asset(a.path, a.ext)),
      metadata: { platform },
      extra: { expoClient: extra },
    };
    // The exporter output is private scratch space and never part of a publication.
    fs.rmSync(exported, { recursive: true });
    kind = "manifest";
  } else {
    payload = {
      type: "rollBackToEmbedded",
      parameters: { commitTime: createdAt },
    };
    kind = "rollback";
  }
  await compatible(platform); // Prevent a concurrent native edit from slipping through export.
  const body = Buffer.from(JSON.stringify(payload));
  write(
    path.join(output, "releases", release, `${platform}.${kind}.json`),
    body,
  );
  write(
    path.join(output, "releases", release, `${platform}.${kind}.sig`),
    sign(body),
  );
  write(
    path.join(
      output,
      "channels",
      channel,
      platform,
      `${c.runtimeVersion}.json`,
    ),
    { release, kind },
  );
  console.log(
    JSON.stringify({
      output,
      release,
      platform,
      runtimeVersion: c.runtimeVersion,
      channel,
      kind,
      updateId: payload.id,
    }),
  );
}
if (require.main === module)
  main(process.argv.slice(2)).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
module.exports = { hash, configuration, compatible, sign };
