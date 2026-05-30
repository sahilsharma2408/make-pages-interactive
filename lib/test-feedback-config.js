// Extracts the endpoint-resolution block from feedback.js and evaluates it
// with / without window.__CF_CONFIG. No DOM needed.
const fs = require("fs"), assert = require("assert"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "feedback.js"), "utf8");

// Grab from the CF_CONFIG line through the FEEDBACK_URL line.
const m = src.match(/const CF_CONFIG =[\s\S]*?const FEEDBACK_URL = [^\n]*/);
assert(m, "config block not found in feedback.js");

const resolve = new Function("window", m[0] + "\nreturn { HISTORY_URL, FEEDBACK_URL };");

const withCfg = resolve({ __CF_CONFIG: { historyUrl: "/__cf/history.json", feedbackUrl: "/__cf/feedback" } });
assert.strictEqual(withCfg.HISTORY_URL, "/__cf/history.json");
assert.strictEqual(withCfg.FEEDBACK_URL, "/__cf/feedback");

const noCfg = resolve(undefined);
assert.strictEqual(noCfg.HISTORY_URL, "feedback/history.json");
assert.strictEqual(noCfg.FEEDBACK_URL, "/feedback");

const windowNoCfg = resolve({});   // browser is present but proxy didn't inject __CF_CONFIG
assert.strictEqual(windowNoCfg.HISTORY_URL, "feedback/history.json");
assert.strictEqual(windowNoCfg.FEEDBACK_URL, "/feedback");

console.log("PASS test-feedback-config");
