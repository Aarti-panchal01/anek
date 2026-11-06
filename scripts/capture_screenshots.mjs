import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:net';
import { copyFile, mkdir, mkdtemp, rm, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const imageDir = path.join(root, 'docs', 'images');
const backendChildren = [];
const frontendChildren = [];
let browser;

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}

function run(binary, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(binary, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], ...options });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', code => code === 0
      ? resolve(stdout)
      : reject(new Error(`${binary} ${args.join(' ')} failed (${code}): ${(stderr || stdout).slice(-4000)}`)));
  });
}

function start(binary, args, options = {}, bucket) {
  const child = spawn(binary, args, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'], ...options });
  let output = '';
  child.stdout.on('data', chunk => { output += chunk.toString(); });
  child.stderr.on('data', chunk => { output += chunk.toString(); });
  child.once('error', error => { output += `\n${error.message}`; });
  bucket.push(child);
  child.captureOutput = () => output;
  return child;
}

async function waitFor(url, child, label) {
  const deadline = Date.now() + 60_000;
  let lastError = '';
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`${label} stopped before it became ready: ${child.captureOutput()}`);
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) { lastError = error.message; }
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  throw new Error(`${label} did not become ready (${lastError}): ${child.captureOutput()}`);
}

async function stop(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore' });
  } else {
    child.kill('SIGTERM');
    await Promise.race([
      new Promise(resolve => child.once('close', resolve)),
      new Promise(resolve => setTimeout(resolve, 5000)),
    ]);
    if (child.exitCode === null) child.kill('SIGKILL');
  }
}

async function quietSensitiveChrome(page) {
  await page.addStyleTag({ content: `
    .footer-maker, .workspace-chip { visibility: hidden !important; }
    [data-secret], input[type=password] { visibility: hidden !important; }
  ` });
  await page.evaluate(() => {
    const privateText = /[A-Z]:\\Users\\|\/Users\/|\/home\/|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:api[_ -]?key|token)\s*[:=]/i;
    for (const element of document.querySelectorAll('body *')) {
      if (element.children.length === 0 && privateText.test(element.textContent || '')) {
        (element.closest('section,article,aside,footer,div') || element).style.visibility = 'hidden';
      }
    }
  });
}

async function waitForRender(page, expected, label) {
  await page.goto(expected.url, { waitUntil: 'networkidle', timeout: 45_000 });
  await page.locator('body').waitFor({ state: 'visible', timeout: 15_000 });
  await page.getByText(expected.text, { exact: false }).first().waitFor({ state: 'visible', timeout: 20_000 })
    .catch(() => { throw new Error(`${label} did not render expected content: ${expected.text}`); });
  await page.evaluate(async () => { await document.fonts.ready; });
  await page.waitForTimeout(250);
}

async function waitForVideos(page, label) {
  const count = await page.locator('video').count();
  for (let index = 0; index < count; index += 1) {
    const video = page.locator('video').nth(index);
    const hasSource = await video.evaluate(element => Boolean(element.currentSrc || element.getAttribute('src')));
    if (!hasSource) continue;
    await video.evaluate(async element => {
      const video = element;
      if (video.readyState < 2) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Timed out loading video frame')), 20_000);
          video.addEventListener('loadeddata', () => { clearTimeout(timer); resolve(); }, { once: true });
          video.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Video failed to load')); }, { once: true });
        });
      }
      if (!video.videoWidth || !video.videoHeight) throw new Error('Video has no decoded frame');
      video.pause();
    }).catch(error => { throw new Error(`${label} has an unavailable or black video: ${error.message}`); });
  }
}

async function waitForAudio(page, label) {
  const count = await page.locator('audio').count();
  for (let index = 0; index < count; index += 1) {
    const audio = page.locator('audio').nth(index);
    const hasSource = await audio.evaluate(element => Boolean(element.currentSrc || element.getAttribute('src')));
    if (!hasSource) continue;
    await audio.evaluate(async element => {
      const audio = element;
      audio.preload = 'metadata';
      audio.load();
      if (audio.readyState < 1) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error('Timed out loading audio metadata')), 20_000);
          audio.addEventListener('loadedmetadata', () => { clearTimeout(timer); resolve(); }, { once: true });
          audio.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Audio failed to load')); }, { once: true });
        });
      }
    }).catch(error => { throw new Error(`${label} has an unavailable audio preview: ${error.message}`); });
  }
}

async function capture(page, filename) {
  await page.waitForLoadState('networkidle', { timeout: 45_000 });
  await page.evaluate(async () => { await document.fonts.ready; });
  await waitForVideos(page, filename);
  await waitForAudio(page, filename);
  await quietSensitiveChrome(page);
  const destination = path.join(imageDir, filename);
  await page.screenshot({ path: destination, animations: 'disabled' });
  if ((await stat(destination)).size >= 600 * 1024) {
    throw new Error(`${filename} exceeds the 600 KB image size limit`);
  }
}

async function main() {
  await mkdir(imageDir, { recursive: true });
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'anek-readme-demo-'));
  const backendPort = await freePort();
  const frontendPort = await freePort();
  const backendOrigin = `http://127.0.0.1:${backendPort}`;
  const frontendOrigin = `http://127.0.0.1:${frontendPort}`;
  const baseEnv = {
    ...process.env,
    ANEK_DATA_DIR: dataDir,
    GNANI_MOCK: '1',
    LLM_MOCK: '1',
    REQUIRE_ACCESS_AUTH: 'false',
    CORS_ORIGINS: frontendOrigin,
    DEMO_API_BASE_URL: backendOrigin,
  };

  try {
    const backend = start('python', ['-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', String(backendPort), '--no-access-log'], { cwd: root, env: baseEnv }, backendChildren);
    await waitFor(`${backendOrigin}/health`, backend, 'Backend');

    const buildEnv = { ...baseEnv, VERCEL: '1', VITE_API_BASE_URL: backendOrigin };
    await run(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['run', 'build', '--', '--outDir', 'dist'], {
      cwd: path.join(root, 'frontend'), env: buildEnv, shell: process.platform === 'win32',
    });

    const preview = start(process.execPath, [
      path.join(root, 'frontend', 'node_modules', 'vite', 'bin', 'vite.js'), 'preview',
      '--host', '127.0.0.1', '--port', String(frontendPort), '--strictPort',
    ], { cwd: path.join(root, 'frontend'), env: buildEnv }, frontendChildren);
    await waitFor(frontendOrigin, preview, 'Frontend preview');

    const seedOutput = await run('python', [path.join(root, 'scripts', 'seed_demo.py')], { cwd: root, env: baseEnv });
    const projectId = JSON.parse(seedOutput.trim().split(/\r?\n/).at(-1)).project_id;
    const videoPath = path.join(dataDir, 'uploads', projectId, 'source.mp4');
    const selectedVideoPath = path.join(dataDir, 'Demo.mp4');
    await copyFile(videoPath, selectedVideoPath);

    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
    const page = await context.newPage();
    page.setDefaultTimeout(20_000);
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));

    await waitForRender(page, { url: frontendOrigin, text: 'Say it once.' }, 'landing.png');
    await capture(page, 'landing.png');

    await waitForRender(page, { url: `${frontendOrigin}/studio/new`, text: 'Source video' }, 'new-dub.png');
    await page.locator('input[type="file"]').setInputFiles(selectedVideoPath);
    await page.locator('.create-form input').first().fill('Demo');
    await page.locator('.language-option').filter({ hasText: 'Kannada' }).click();
    await page.locator('.attached-file').waitFor({ state: 'visible' });
    await page.getByText('Demo.mp4', { exact: false }).waitFor({ state: 'visible' }).catch(() => {});
    await capture(page, 'new-dub.png');

    const reviewUrl = `${frontendOrigin}/studio/project/${projectId}`;
    await waitForRender(page, { url: reviewUrl, text: 'Demo' }, 'review-studio.png');
    await page.locator('.clip-row').first().click();
    await page.waitForTimeout(400);
    await capture(page, 'review-studio.png');

    await page.getByRole('button', { name: /हिन्दी/ }).click();
    await page.locator('.clip-row').first().waitFor({ state: 'visible' });
    await page.getByRole('heading', { name: /Clips/ }).waitFor({ state: 'visible' });
    await capture(page, 'clips-hindi.png');

    await page.getByRole('button', { name: /ಕನ್ನಡ/ }).click();
    await page.locator('.clip-row').first().waitFor({ state: 'visible' });
    await capture(page, 'clips-kannada.png');

    await waitForRender(page, { url: `${frontendOrigin}/studio/terms`, text: 'Term memory' }, 'term-memory.png');
    await page.getByText('Anek', { exact: true }).first().waitFor({ state: 'visible' });
    await capture(page, 'term-memory.png');

    if (pageErrors.length) throw new Error(`Browser page error: ${pageErrors[0]}`);
    await context.close();
    console.log('Generated: landing.png, new-dub.png, review-studio.png, clips-hindi.png, clips-kannada.png, term-memory.png');
  } catch (error) {
    console.error(`Screenshot capture failed: ${error.message}`);
    throw error;
  } finally {
    await browser?.close().catch(() => {});
    for (const child of frontendChildren.reverse()) await stop(child);
    for (const child of backendChildren.reverse()) await stop(child);
    await rm(dataDir, { recursive: true, force: true });
  }
}

main().catch(() => { process.exitCode = 1; });
