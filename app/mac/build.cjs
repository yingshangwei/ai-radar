// Package a scoped collector token locally. Never put private-config.js in Git.
const fs = require("node:fs"),
  path = require("node:path");
const ts = require("../node_modules/typescript");
const [configPath, destination] = process.argv.slice(2);
if (!configPath || !destination)
  throw Error("Usage: node app/mac/build.cjs PRIVATE_JSON OUTPUT_DIRECTORY");
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
if (
  config.server !== "https://radar.yswdra.cn" ||
  !config.token ||
  !config.domains.length ||
  config.domains.length > 30
)
  throw Error("Invalid private configuration");
if (fs.statSync(configPath).mode & 0o077)
  throw Error("Private config requires mode 0600");
function load(file) {
  const exports = {};
  const source = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  new Function("exports", "require", source)(exports, (name) =>
    load(path.resolve(path.dirname(file), name + ".ts")),
  );
  return exports;
}
fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
fs.cpSync(path.join(__dirname, "extension"), destination, { recursive: true });
const extraction = load(
  path.resolve(__dirname, "../src/mobileCapture.ts"),
).extractionScript("maccompanion");
const script = extraction.replace(
  'window.ReactNativeWebView.postMessage(JSON.stringify(Object.assign({type: "radar.article", nonce: requestNonce}, value)));',
  "radarResult = value;",
);
fs.writeFileSync(
  path.join(destination, "capture.js"),
  "(function(){ var radarResult; " + script + "; return radarResult; })();",
);
fs.writeFileSync(
  path.join(destination, "private-config.js"),
  "export const config = " + JSON.stringify(config) + ";\n",
  { mode: 0o600 },
);
console.log("Packaged Mac companion at " + path.resolve(destination));
