// Publishes the widget's expectations of the gateway API as JSON Schema (consumer-driven contract).
//
//   npm run contracts          regenerate ../contracts/widget/gateway-api.schema.json
//   npm run contracts:check    fail if contracts.ts changed without regenerating (CI)
//
// Tolerant reader: extra fields are allowed (the widget ignores them); required fields and types are not.
// The Python suites validate REAL gateway/checkout responses against this schema, so a backend change
// that would break the widget fails the backend's tests, not a customer's checkout.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createGenerator } from 'ts-json-schema-generator';

const source = fileURLToPath(new URL('../src/shared/api/contracts.ts', import.meta.url));
const tsconfig = fileURLToPath(new URL('../tsconfig.json', import.meta.url));
const target = fileURLToPath(new URL('../../contracts/widget/gateway-api.schema.json', import.meta.url));

const exported = [...readFileSync(source, 'utf8').matchAll(/^export (?:interface|type) (\w+)/gm)].map((m) => m[1]);
const definitions = {};
for (const type of exported) {
  const schema = createGenerator({ path: source, tsconfig, type, expose: 'export', skipTypeCheck: true, additionalProperties: true }).createSchema(type);
  Object.assign(definitions, schema.definitions);
}
const sorted = Object.fromEntries(Object.keys(definitions).sort().map((k) => [k, definitions[k]]));
const document = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $comment: 'GENERATED from widget/src/shared/api/contracts.ts by `npm run contracts`. Do not edit.',
  definitions: sorted,
};
const text = `${JSON.stringify(document, null, 2)}\n`;

if (process.argv.includes('--check')) {
  let current = '';
  try {
    current = readFileSync(target, 'utf8');
  } catch {
    // missing file: reported below
  }
  if (current.replace(/\r\n/g, '\n') !== text) {
    console.error('contracts/widget/gateway-api.schema.json is stale: run `npm run contracts` and commit it.');
    process.exit(1);
  }
  console.log(`widget contract up to date (${exported.length} types)`);
} else {
  writeFileSync(target, text);
  console.log(`wrote ${target} (${Object.keys(sorted).length} definitions from ${exported.length} exported types)`);
}
