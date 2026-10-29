#!/usr/bin/env python3
"""Build and validate the fixed-revision Markdown book and offline source readers."""
from pathlib import Path
from html import escape
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, unquote
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--output', type=Path, default=Path('/workspace/exports/opencode-study-detailed'))
parser.add_argument('--modules', type=Path, default=Path('/workspace/.onboarding/export-tools/node_modules'))
parser.add_argument('--browser', default='/usr/bin/chromium')
args = parser.parse_args()
repo, output = args.repo.resolve(), args.output.resolve()
study = repo / 'study'
output.mkdir(parents=True, exist_ok=True)
work = output.parent / '.opencode-book-build'
work.mkdir(exist_ok=True)
versions = {
    'c6262f9d4002d86a1f1795c306aa329d45361d12': ('A', 'v1.2.24'),
    '055d95bb7e278c94baf06235a52cac79dd13ba67': ('B', 'V1 1.18.35'),
    '726cf096396d5014c3b1845e231cc01efe0421ee': ('N', 'V2 2.2.0'),
}
entries = [('README.md', 'overview', '教程总览', 'start')]
titles = [
    '00 · 运行坐标与阅读方法', '01 · v1.2.24 独立实现', '02 · 最终 V1 独立实现',
    '03 · 最新 V2 执行核心', '04 · 最新 V2 工具与进程', '05 · 三版本逐项比较',
    '06 · 为什么需要 V2', '07 · 六次完整执行推演', '08 · 问答与排查手册',
]
for i, file in enumerate(sorted((study / 'book').glob('*.md'))):
    entries.append((str(file.relative_to(study)), f'chapter{i:02}', titles[i], 'start' if i < 1 else 'versions' if i < 5 else 'explain'))
assert len(entries) == 10
names = {name: ident for name, ident, _, _ in entries}
cache = {}
sources = set()
excerpts = []
diagrams = []
asts = {}
link_count = 0

def git_source(sha, path):
    key = (sha, path)
    if key not in cache:
        cache[key] = subprocess.check_output(['git', 'show', f'{sha}:{path}'], cwd=repo).decode()
    return cache[key]

def source_url(url):
    m = re.fullmatch(r'https://github.com/gaoch023/opencode/blob/([0-9a-f]{40})/(.+?)(?:#(.*))?', url)
    if not m:
        return None
    sha, path, anchor = m.groups()
    path = unquote(path)
    assert sha in versions, ('unexpected revision', sha, path)
    text = git_source(sha, path)
    if anchor and re.fullmatch(r'L\d+(?:-L\d+)?', anchor):
        numbers = [int(n) for n in re.findall(r'\d+', anchor)]
        assert 1 <= numbers[0] <= numbers[-1] <= len(text.splitlines()), (path, anchor)
    sources.add((sha, path))
    return sha, path, anchor

for name, ident, _, _ in entries:
    raw = (study / name).read_text()
    target = output / 'markdown' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(raw)
    ast = json.loads(subprocess.check_output(['pandoc', '-f', 'gfm', '-t', 'json', str(study / name)]))
    asts[name] = ast
    pending = None
    for block in ast['blocks']:
        if block.get('t') == 'RawBlock':
            m = re.search(r'<!-- EXCERPT ([0-9a-f]{40}) (\S+) (\d+) (\d+) -->', block['c'][1])
            if m:
                assert pending is None
                pending = m.groups()
        if block.get('t') == 'CodeBlock' and pending:
            sha, path, first, last = pending
            a, b = int(first), int(last)
            expected = '\n'.join(git_source(sha, path).splitlines()[a-1:b])
            assert block['c'][1] == expected, ('changed source excerpt', name, path, a, b)
            excerpts.append((sha, path, a, b))
            sources.add((sha, path))
            pending = None
    assert pending is None

    def inspect(value):
        global link_count
        if isinstance(value, dict):
            if value.get('t') == 'Link':
                link_count += 1
                url = value['c'][2][0]
                if not source_url(url):
                    parsed = urlsplit(url)
                    if not parsed.scheme and parsed.path:
                        local = (study / name).parent / unquote(parsed.path)
                        assert local.resolve().is_file(), ('broken markdown link', name, url)
            if value.get('t') == 'CodeBlock' and 'mermaid' in value['c'][0][1]:
                file = work / f'diagram-{len(diagrams)+1:02}.mmd'
                file.write_text(value['c'][1])
                diagrams.append((ident, value, file, output / 'diagrams' / (file.stem + '.svg')))
            else:
                for item in value.values(): inspect(item)
        elif isinstance(value, list):
            for item in value: inspect(item)
    inspect(ast)

(output / 'diagrams').mkdir(exist_ok=True)
def render(item):
    ident, block, infile, outfile = item
    result = subprocess.run(['node', str(study / 'tools/render-diagram.cjs'), str(infile), str(outfile), str(args.modules), args.browser], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{infile}: {result.stderr}')
    svg = outfile.read_text()
    ids = set(re.findall(r'\bid="([^"]+)"', svg))
    prefix = infile.stem + '-'
    for old in sorted(ids, key=len, reverse=True):
        svg = re.sub(rf'(?<![\w-]){re.escape(old)}(?![\w-])', prefix + old, svg)
    outfile.write_text(svg)
    block.clear()
    block.update({'t': 'RawBlock', 'c': ['html', '<figure class="diagram">' + svg + '</figure>']})
    print('Rendered', outfile.name, flush=True)
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(render, diagrams))

css = (study / 'tools/book.css').read_text()
source_rows = []
source_files = {}
for sha, path in sorted(sources):
    label, version = versions[sha]
    raw = output / 'sources' / label / path
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(git_source(sha, path))
    viewer = raw.with_name(raw.name + '.html')
    lines = git_source(sha, path).splitlines()
    back = os.path.relpath(output / 'index.html', viewer.parent)
    numberlines = ''.join(f'<span class="line" id="L{i}"><a class="ln" href="#L{i}">{i}</a><code>{escape(line)}</code></span>' for i, line in enumerate(lines, 1))
    source_css = 'body{margin:0;background:#f6f8f7;color:#24382f;font:14px/1.7 sans-serif}header{padding:20px 28px;position:sticky;top:0;background:#edf3ef;border-bottom:1px solid #cbd9d0}header p{margin:4px 0}a{color:#176b58}pre{margin:0;padding:15px 0;overflow:auto;background:#182b27;color:#e3eee5;font:12px/1.8 Consolas,"Noto Sans Mono CJK SC",monospace}.line{display:block;min-height:1.8em;scroll-margin-top:115px}.line:target{background:#385c4d}.ln{display:inline-block;width:58px;padding-right:18px;text-align:right;color:#93b5a4;text-decoration:none;user-select:none}code{font:inherit}.path{overflow-wrap:anywhere}'
    viewer.write_text(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(path)} · {version}</title><style>{source_css}</style><body><header><a href="{back}">← 返回完整教程</a><p class="path"><strong>{label} · {version}</strong> — {escape(path)}</p><small>{sha} · {len(lines)} 行 · <a href="{raw.name}">原始文件</a> · <a href="https://github.com/gaoch023/opencode/blob/{sha}/{path}">GitHub</a></small></header><pre>{numberlines}</pre></body></html>')
    relative = str(viewer.relative_to(output))
    source_files[(sha, path)] = relative
    source_rows.append(f'<tr><td>{label} · {version}</td><td><a href="{escape(str(viewer.relative_to(output / "sources")))}">{escape(path)}</a></td><td>{len(lines)}</td></tr>')
(output / 'sources/index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>完整源码索引</title><style>body{max-width:1100px;margin:30px auto;padding:20px;font:14px/1.8 sans-serif}table{width:100%;border-collapse:collapse}td,th{padding:10px;border:1px solid #ddd;text-align:left}a{color:#176b58}</style><a href="../index.html">← 返回完整教程</a><h1>固定提交完整源码索引</h1><p>这些完整文件与正文片段来自相同 Git 对象，行号页可以离线阅读。A/B/N 不是同一个版本。</p><table><tr><th>版本</th><th>文件</th><th>行数</th></tr>' + ''.join(source_rows) + '</table></html>')

def text_of(value):
    if isinstance(value, dict):
        if value.get('t') in ('CodeBlock', 'RawBlock'): return ''
        if value.get('t') == 'Str': return value['c']
        return ''.join(text_of(v) for v in value.values())
    if isinstance(value, list): return ''.join(text_of(v) for v in value)
    return ''
hanzi = len(re.findall(r'[\u4e00-\u9fff]', ''.join(text_of(ast) for ast in asts.values())))
stats = {'documents': len(entries), 'prose_hanzi': hanzi, 'source_excerpts': len(excerpts), 'excerpt_lines': sum(b-a+1 for _,_,a,b in excerpts), 'full_source_files': len(sources), 'diagrams': len(diagrams), 'markdown_links': link_count}

script = '''document.querySelectorAll('pre').forEach(pre=>{const code=pre.querySelector('code');if(!code)return;const b=document.createElement('button');b.className='copy-code';b.textContent='复制代码';b.onclick=async()=>{try{await navigator.clipboard.writeText(code.textContent);b.textContent='已复制';}catch{const t=document.createElement('textarea');t.value=code.textContent;document.body.append(t);t.select();document.execCommand('copy');t.remove();b.textContent='已复制';}};pre.append(b);});const nav=[...document.querySelectorAll('aside a[href^="#"]')];new IntersectionObserver(items=>{for(const i of items){if(i.isIntersecting){nav.forEach(a=>a.classList.toggle('active',a.hash==='#'+i.target.id));}}},{rootMargin:'-10% 0px -70% 0px'}).observe(document.querySelector('article'));document.querySelectorAll('article').forEach(a=>new IntersectionObserver(items=>{if(items[0].isIntersecting){nav.forEach(n=>n.classList.toggle('active',n.hash==='#'+a.id));}},{rootMargin:'-10% 0px -70% 0px'}).observe(a));'''

def make_page(offline):
    articles = []
    for name, ident, title, group in entries:
        ast = json.loads(json.dumps(asts[name]))
        def transform(value):
            if isinstance(value, dict):
                t = value.get('t')
                if t == 'Header':
                    value['c'][0] = min(value['c'][0]+1, 6)
                    value['c'][1][0] = ident + '--' + value['c'][1][0]
                elif t == 'RawBlock': return
                elif t == 'Link':
                    attrs, _, target = value['c']
                    url = target[0]
                    parsed = urlsplit(url)
                    if not parsed.scheme:
                        if not parsed.path:
                            if parsed.fragment: target[0] = '#' + ident + '--' + parsed.fragment
                        else:
                            absolute = ((study / name).parent / unquote(parsed.path)).resolve()
                            relative = str(absolute.relative_to(study))
                            if relative in names:
                                target[0] = '#' + names[relative] + ('--' + parsed.fragment if parsed.fragment else '')
                            else:
                                target[0] = 'https://github.com/gaoch023/opencode/raw/refs/heads/v1.2.24-study/study/' + relative
                    else:
                        source = source_url(url)
                        if offline and source:
                            sha, path, anchor = source
                            target[0] = source_files[(sha,path)] + ('#' + anchor.split('-')[0] if anchor else '#L1')
                        else:
                            attrs[2].extend([['target','_blank'],['rel','noopener noreferrer']])
                for child in value.values(): transform(child)
            elif isinstance(value, list):
                for child in value: transform(child)
        transform(ast)
        html = subprocess.check_output(['pandoc','-f','json','-t','html5','--highlight-style=tango','--wrap=none'],input=json.dumps(ast).encode()).decode()
        html = re.sub(r'<table\b', '<div class="table-scroll"><table', html).replace('</table>', '</table></div>')
        for old in sorted(set(re.findall(r'\bid="([^"]+)"', html)), key=len, reverse=True):
            if old.startswith((ident + '--', 'diagram-')):
                continue
            html = html.replace(f'id="{old}"', f'id="{ident}--{old}"').replace(f'href="#{old}"', f'href="#{ident}--{old}"')
        articles.append(f'<article class="chapter {group}" id="{ident}"><div class="chapter-meta"><span>{escape(title)}</span><a href="#top">返回目录 ↑</a></div>{html}</article>')
    nav = []
    for group, title in [('start','先建立基础'),('versions','三个版本分别讲透'),('explain','差异、动机与问题推演')]:
        nav.append(f'<div class="nav-group"><p>{title}</p>')
        for _, ident, label, g in entries:
            if g == group: nav.append(f'<a href="#{ident}">{escape(label)}</a>')
        nav.append('</div>')
    if offline: nav.append('<div class="nav-group"><a href="sources/index.html">完整源码离线索引 ↗</a></div>')
    source_note = '源码链接直接打开包内完整行号页。' if offline else '完整源码离线页包含在 ZIP 中；本文件的完整源码链接打开 GitHub。'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OpenCode 核心运行机制 · 详细源码教程</title><style>' + css + '</style></head><body id="top"><aside><div class="brand">OpenCode<br>核心源码教程<small>先分别讲透，再比较差异</small></div>' + ''.join(nav) + '<div class="nav-note">浏览器 Ctrl / ⌘ + F 全文搜索<br>A：v1.2.24<br>B：V1 1.18.35<br>N：V2 2.2.0 · 2026-10-29</div></aside><main><header class="hero"><div class="eyebrow">IMPLEMENTATION · 独立实现 / 设计目的 / 故障推演</div><h1>读到实现里面，<br>理解每一步为什么发生。</h1><p>按版本走通 agent loop、Session、工具、子 agent、前后台执行与进程；再解释 V2 的目的、收益、代价和限制。</p><div class="facts"><span>' + f'{hanzi:,} 汉字正文</span><span>{len(excerpts)} 段真实源码</span><span>{len(sources)} 个完整源码文件</span><span>{len(diagrams)} 张流程图' + '</span></div><div class="notice">代码片段从固定提交自动提取，并有逐段解释；任务时序是明确标注的教学推演，不冒充本次模型运行记录。' + source_note + '</div></header>' + ''.join(articles) + '<footer>OpenCode 核心运行机制 · 2026-10-29 详细重写版 · 固定提交与文件校验信息见 manifest.json</footer></main><script>' + script + '</script></body></html>'

class Check(HTMLParser):
    def __init__(self):
        super().__init__(); self.ids=[]; self.links=[]; self.svgs=0; self.tables=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'id' in attrs: self.ids.append(attrs['id'])
        if tag=='a': self.links.append(attrs.get('href',''))
        self.svgs += tag=='svg'; self.tables += tag=='table'

def validate(page, offline):
    checked=Check(); checked.feed(page)
    assert len(checked.ids)==len(set(checked.ids)), 'duplicate IDs'
    assert checked.svgs==len(diagrams)
    for target in checked.links:
        parsed=urlsplit(target)
        if parsed.scheme: assert parsed.scheme in ('https','http','mailto'), target
        elif not parsed.path: assert unquote(parsed.fragment) in checked.ids, target
        else:
            assert offline
            path=output/unquote(parsed.path)
            assert path.is_file(), target
            if parsed.fragment: assert f'id="{unquote(parsed.fragment)}"' in path.read_text(),target
    return checked.tables

page=make_page(True); standalone=make_page(False)
stats['tables']=validate(page,True); validate(standalone,False)
(output/'index.html').write_text(page)
single=output.parent/'opencode-source-study.html'; single.write_text(standalone)
(output/'使用说明.txt').write_text('OpenCode 核心源码教程 · 2026-10-29\n\n解压后打开 index.html。正文、表格、流程图和被引用的完整源码行号页可离线阅读。\nmarkdown/ 保存原始教材；sources/ 保存固定提交的原始源码及HTML行号页。\nA=v1.2.24，B=V1 1.18.35，N=V2 2.2.0。具体任务为教学推演，非真实运行日志。\n浏览器 Ctrl+F / Command+F 搜索全文。\n')
manifest={'edition':'2026-10-29-detailed','versions':versions,'statistics':stats,'files':{str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob('*')) if p.is_file() and p.name!='manifest.json'}}
(output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
archive=output.parent/'opencode-source-study.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for p in sorted(output.rglob('*')):
        if p.is_file(): z.write(p,'opencode-study/'+str(p.relative_to(output)))
with zipfile.ZipFile(archive) as z: assert z.testzip() is None
print(json.dumps({'statistics':stats,'zip':str(archive),'zip_bytes':archive.stat().st_size,'html':str(single),'html_bytes':single.stat().st_size},ensure_ascii=False,indent=2),flush=True)
