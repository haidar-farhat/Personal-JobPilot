/* Runs the shared REJECT/ACCEPT table from tests/test_choice_matching_safety.py
 * against the extension's own matchers, so the online (Python) and offline
 * (JavaScript) paths can never disagree about what a safe match is.
 *
 * Loads the real shipped files — no reimplementation — under a DOM stub thin
 * enough that only the pure matching logic runs.
 *
 * Usage: node tests/js/choice_matching.mjs   (exit 0 = pass, 1 = fail)
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const EXT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "browser-extension");
const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// fill.js only touches `window` at top level (it hangs __jpafApply/__jpafHelpers
// off it); everything DOM-ish lives inside functions we don't call here.
globalThis.window = globalThis;
new Function(readFileSync(join(EXT, "content", "fill.js"), "utf8"))();
const { chooseChoice } = globalThis.window.__jpafHelpers;

// service_worker.js is a module-scoped script; grab its offline choice matcher
// by evaluating it with the chrome APIs it registers listeners against stubbed.
globalThis.chrome = {
  runtime: { onMessage: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {} } },
};
const swSrc = readFileSync(join(EXT, "service_worker.js"), "utf8");
const jmap = new Function(`${swSrc}\nreturn jmap;`)();

// value -> what the OFFLINE mapper resolves for a select with these options.
// Routed through a "gender" field because that is a plain C() choice path.
function offlineChoice(value, options) {
  const r = jmap({ id: "f0", label: "Gender", name: "gender", type: "select", options },
                 { eeoc: { gender: value } });
  return r && r.needs_review === false ? r.value : null;
}

const REJECT = [
  ["Bachelor of Science", ["Master of Science", "Doctor of Philosophy"]],
  ["Master of Science", ["Bachelor of Science", "Doctor of Philosophy"]],
  ["California Polytechnic State University", ["Adams State University", "Boston College"]],
  ["Bachelor's Degree", ["Associate Degree", "Doctorate Degree"]],
  ["San Francisco", ["San Diego", "Kansas City"]],
  ["Data Scientist", ["Data Engineer", "Research Scientist"]],
  ["Economics", ["Home Economics", "Agricultural Economics"]],
  ["Bachelor of Science", ["Bachelor of Science in Physics", "Bachelor of Science in Economics"]],
];

const ACCEPT = [
  ["Full-time", ["Part-time employee", "Full-time employee"], "Full-time employee"],
  ["Two or More Races", ["Asian", "Two or More Races (Not Hispanic or Latino)"],
   "Two or More Races (Not Hispanic or Latino)"],
  ["Yes", ["Yes, I have a disability", "No, I do not"], "Yes, I have a disability"],
  ["No", ["Hispanic or Latino", "Not Hispanic or Latino"], "Not Hispanic or Latino"],
  ["Male", ["Man", "Woman"], "Man"],
  ["Decline to answer", ["Male", "I don't wish to answer"], "I don't wish to answer"],
  ["California Polytechnic State University",
   ["California Polytechnic State University - San Luis Obispo", "Adams State University"],
   "California Polytechnic State University - San Luis Obispo"],
  ["Asian", ["Asian (Not Hispanic or Latino)", "White"], "Asian (Not Hispanic or Latino)"],
  ["Economics", ["Economics", "Economics and Finance"], "Economics"],
];

let failed = 0;
const fail = (m) => { console.error("FAIL " + m); failed++; };

// content-script matcher (fill.js) — used by every in-page fill
for (const [value, options] of REJECT) {
  const got = chooseChoice(value, options.map((o) => ({ el: o, t: norm(o) })));
  if (got !== null && got !== undefined)
    fail(`fill.js matched ${JSON.stringify(value)} -> ${JSON.stringify(got)} (expected no match)`);
}
for (const [value, options, expected] of ACCEPT) {
  const got = chooseChoice(value, options.map((o) => ({ el: o, t: norm(o) })));
  if (got !== expected)
    fail(`fill.js ${JSON.stringify(value)} -> ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
}

// offline mapper (service_worker.js) — used whenever the backend is down
for (const [value, options] of REJECT) {
  const got = offlineChoice(value, options);
  if (got !== null && got !== undefined)
    fail(`service_worker matched ${JSON.stringify(value)} -> ${JSON.stringify(got)} (expected no match)`);
}
for (const [value, options, expected] of ACCEPT) {
  const got = offlineChoice(value, options);
  if (got !== expected)
    fail(`service_worker ${JSON.stringify(value)} -> ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`);
}

const total = (REJECT.length + ACCEPT.length) * 2;
if (failed) {
  console.error(`\n${failed}/${total} checks failed`);
  process.exit(1);
}
console.log(`ok — ${total} choice-matching checks passed (fill.js + service_worker.js)`);
