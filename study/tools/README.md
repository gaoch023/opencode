# 重建离线教材

源码教材在 `study/book/`；`EXCERPT` 注释记录每段真实代码的完整提交、路径和行范围。构建脚本先从 Git 对象校验代码没有被改写，再渲染正文、Mermaid、完整源码行号页和 ZIP，并验证文件与锚点。

需要 Python 3、Pandoc、Node、Chromium，以及独立目录中安装的 Mermaid/Puppeteer。它不需要安装任何版本的 OpenCode 应用依赖，也不调用模型。示例：

```bash
PUPPETEER_SKIP_DOWNLOAD=true npm install --prefix /tmp/opencode-book-tools @mermaid-js/mermaid-cli@11.12.0
python3 study/tools/build-book.py --modules /tmp/opencode-book-tools/node_modules --output /tmp/opencode-study-book
node study/tools/check-reader.cjs /tmp/opencode-book-tools/node_modules /tmp/opencode-study-book
```

Chromium 若不在 `/usr/bin/chromium`，使用构建脚本的 `--browser` 参数指定。浏览器检查脚本中的 executablePath 也须对应调整。脚本只打开自己生成的页面，不连接用户正在使用的浏览器。

输出目录中 `index.html` 使用包内源码链接；同级的 `opencode-source-study.html` 是单文件版，完整源码链接保留为 GitHub 链接；`opencode-source-study.zip` 包含全部离线文件。输出路径请选在代码仓外，以免把生成的完整源码重复纳入应用工作区。

`book-sources.json` 是本版片段清单。原始 Markdown 可以直接在 GitHub 阅读；修改代码片段时应先核对固定提交，不能只改讲解里的片段而保留旧来源声明。
