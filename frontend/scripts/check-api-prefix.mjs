import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';

const sourceRoot = path.resolve('src');
const sourceFiles = [];
async function collect(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) await collect(file);
    else if (/\.[cm]?[jt]sx?$/.test(entry.name)) sourceFiles.push(file);
  }
}
await collect(sourceRoot);

const targetUrl = /\/(?:api\/)?(?:projects\/(?:\$\{[^}]+\}|[^/`"'?#\s]+)\/(?:video|media)(?=[?`"'?#]|$)|media\/[^/`"'?#\s]*preview(?=[?`"'?#]|$))/g;
const violations = [];
for (const file of sourceFiles) {
  const source = await readFile(file, 'utf8');
  for (const match of source.matchAll(targetUrl)) {
    const before = source.slice(Math.max(0, match.index - 120), match.index);
    const wrapper = Math.max(before.lastIndexOf('apiUrl('), before.lastIndexOf('apiFetch('), Math.max(before.lastIndexOf('request('), before.lastIndexOf('request<')));
    const lastClose = before.lastIndexOf(')');
    const apiRoot = before.lastIndexOf('${API}');
    if (wrapper <= lastClose && apiRoot < lastClose) {
      const line = source.slice(0, match.index).split('\n').length;
      violations.push(`${path.relative(process.cwd(), file)}:${line}: ${match[0]}`);
    }
  }
}
if (violations.length) {
  console.error('Media and project API URLs must use apiUrl(), apiFetch(), or request():');
  for (const violation of violations) console.error(`  ${violation}`);
  process.exitCode = 1;
} else {
  console.log(`API URL prefix check passed (${sourceFiles.length} source files scanned).`);
}


