/* The offline (service-worker) mapper must refuse credential fields exactly
 * like the Python one — tests/test_credential_fields.py runs this.
 * Usage: node tests/js/credential_guard.mjs   (exit 0 = pass) */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const EXT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "browser-extension");
globalThis.window = globalThis;
globalThis.chrome = {
  runtime: { onMessage: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {} } },
};
const jmap = new Function(`${readFileSync(join(EXT, "service_worker.js"), "utf8")}\nreturn jmap;`)();

const profile = { identity: { email: "alex@example.com", first_name: "Alex", full_name: "Alex Rivera" } };
const fields = [
  { id: "f0", label: "Password", name: "password", type: "password" },
  { id: "f1", label: "Create a password with at least 8 characters", name: "pw", type: "text" },
  { id: "f2", label: "Enter the 6-digit verification code we emailed you", name: "code", type: "text" },
  { id: "f3", label: "Code", name: "otp", type: "text", autocomplete: "one-time-code" },
  { id: "f4", label: "Social Security Number", name: "ssn", type: "text" },
  { id: "f5", label: "Security code", name: "verify", type: "text" },
];
let bad = 0;
for (const f of fields) {
  const r = jmap(f, profile);
  if (!r || r.value != null) { console.log(`FAIL ${f.label}: ${JSON.stringify(r)}`); bad++; }
}
const ok = jmap({ id: "f9", label: "Email Address", name: "email", type: "text" }, profile);
if (!ok || ok.value !== "alex@example.com") { console.log(`FAIL email next to password: ${JSON.stringify(ok)}`); bad++; }
console.log(bad ? `${bad} failure(s)` : "credential guard: ok");
process.exit(bad ? 1 : 0);
