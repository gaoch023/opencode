const fs = require('node:fs');
const path = require('node:path');
const modules = process.argv[2] || '/workspace/.onboarding/export-tools/node_modules';
const output = process.argv[3] || '/workspace/exports/opencode-study-detailed';
const puppeteer = require(path.join(modules, 'puppeteer'));
(async () => {
  const browser = await puppeteer.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    const errors = [], requests = [];
    page.on('pageerror', error => errors.push(String(error)));
    page.on('request', request => requests.push(request.url()));
    await page.setViewport({ width: 1440, height: 1080 });
    await page.setContent(fs.readFileSync(path.join(output, 'index.html'), 'utf8'), { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    const desktop = await page.evaluate(() => ({
      chapters: document.querySelectorAll('article').length,
      tables: document.querySelectorAll('table').length,
      diagrams: [...document.querySelectorAll('svg')].map(e => [e.getBoundingClientRect().width, e.getBoundingClientRect().height]),
      codeBlocks: document.querySelectorAll('pre code').length,
      copyButtons: document.querySelectorAll('.copy-code').length,
      missingAnchors: [...document.querySelectorAll('a[href^="#"]')].filter(a => !document.getElementById(decodeURIComponent(a.hash.slice(1)))).map(a => a.hash),
      overflow: document.documentElement.scrollWidth > innerWidth,
    }));
    await page.screenshot({ path: path.join(output, '../opencode-book-overview.png') });
    await page.evaluate(() => { document.documentElement.style.scrollBehavior = 'auto'; document.querySelector('#chapter01 pre').scrollIntoView({ block: 'center' }); });
    await page.screenshot({ path: path.join(output, '../opencode-book-source.png') });
    await page.setViewport({ width: 390, height: 844 });
    await page.evaluate(() => { window.scrollTo(0, 0); return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); });
    const mobile = await page.evaluate(() => ({ overflow: document.documentElement.scrollWidth > innerWidth }));
    await page.screenshot({ path: path.join(output, '../opencode-book-mobile.png') });
    const manifest = JSON.parse(fs.readFileSync(path.join(output, 'manifest.json'), 'utf8'));
    if (desktop.chapters !== 10 || desktop.diagrams.length !== manifest.statistics.diagrams || desktop.copyButtons < 85 || desktop.missingAnchors.length || desktop.overflow || mobile.overflow || errors.length || requests.length) throw new Error(JSON.stringify({desktop,mobile,errors,requests}));
    const sourcePath = path.join(output, 'sources/N/packages/core/src/session/session.ts.html');
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(fs.readFileSync(sourcePath, 'utf8'));
    const source = await page.evaluate(() => ({ line155: !!document.getElementById('L155'), lines: document.querySelectorAll('.line').length, containsPrompt: document.getElementById('L155').textContent.includes('prompt') }));
    if (!source.line155 || source.lines < 300 || !source.containsPrompt) throw new Error(JSON.stringify(source));
    process.stdout.write(JSON.stringify({desktop,mobile,source,scriptErrors:errors.length,networkRequests:requests.length}) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
