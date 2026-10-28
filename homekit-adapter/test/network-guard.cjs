// Installed before any HAP import. This unit phase allows no network operations,
// including local publication; Python integration separately uses inherited FDs.
const net = require("node:net");
const dgram = require("node:dgram");
const http = require("node:http");
const https = require("node:https");
const dns = require("node:dns");
const childProcess = require("node:child_process");
const { writeFileSync } = require("node:fs");
const { syncBuiltinESMExports } = require("node:module");
const attempts = [];
const deny = name => function () {
  attempts.push(name);
  throw new Error(`unit network operation forbidden: ${name}`);
};
net.Socket.prototype.connect = deny("net.connect");
net.Server.prototype.listen = deny("net.listen");
for (const method of ["bind", "send", "connect"]) {
  dgram.Socket.prototype[method] = deny(`dgram.${method}`);
}
http.request = deny("http.request");
http.get = deny("http.get");
https.request = deny("https.request");
https.get = deny("https.get");
for (const method of Object.keys(dns)) {
  if (method === "lookup" || method === "lookupService" || method.startsWith("resolve")) {
    dns[method] = deny(`dns.${method}`);
    if (typeof dns.promises[method] === "function") dns.promises[method] = deny(`dns.promises.${method}`);
    if (typeof dns.Resolver.prototype[method] === "function") {
      dns.Resolver.prototype[method] = deny(`dns.Resolver.${method}`);
    }
  }
}
for (const method of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"]) {
  childProcess[method] = deny(`child_process.${method}`);
}
globalThis.fetch = deny("fetch");
syncBuiltinESMExports();
process.on("exit", () => {
  if (process.env.HALO_TEST_NETWORK_RECORD) {
    writeFileSync(process.env.HALO_TEST_NETWORK_RECORD, JSON.stringify({
      phase: "Node unit tests including HAP import", attempts,
      scope: "net connect/listen; UDP bind/send/connect; HTTP/HTTPS; DNS; fetch; child processes",
    }, null, 2) + "\n");
  }
  if (attempts.length) process.exitCode = 1;
});
