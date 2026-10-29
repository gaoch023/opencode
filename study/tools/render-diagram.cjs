const fs = require('node:fs');
const path = require('node:path');
const modules = process.argv[4];
const puppeteer = require(path.join(modules, 'puppeteer'));
(async () => {
  const browser = await puppeteer.launch({ executablePath: process.argv[5], headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1600, height: 1200 });
    await page.setContent('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body><div id="container"></div></body></html>');
    await page.addScriptTag({ content: fs.readFileSync(path.join(modules, 'mermaid/dist/mermaid.min.js'), 'utf8') });
    const svg = await page.evaluate(async definition => {
      mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'strict', fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, sans-serif', flowchart: { htmlLabels: false } });
      return (await mermaid.render('study-chart', definition, document.getElementById('container'))).svg;
    }, fs.readFileSync(process.argv[2], 'utf8'));
    fs.writeFileSync(process.argv[3], svg);
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
