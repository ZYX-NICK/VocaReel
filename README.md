# 简介
**中文｜**追剧学英语是一款配合 mpv 使用的字幕学习工具。它支持内嵌字幕和外挂 ASS 等字幕，一键切换纯英文与中英双语。你还可以在独立选词窗口查看单词的多种释义和例句，跳转有道、剑桥词典，并自定义快捷键和窗口置顶设置。

**English｜**Learn English with your favorite shows. Switch between English-only and bilingual subtitles in one click, using embedded or external subtitles. Look up words in a separate window for multiple meanings and examples, open trusted dictionaries, and customize your shortcuts.

# 追剧学英语

使用 mpv 播放本地视频，用独立控制面板切换纯英文和中英双语字幕，并在可调整大小的选词窗口查词。界面使用 Python 标准库 Tkinter，不需要安装 Python 第三方包。Windows 为主要使用平台；macOS 和 Linux 也实现了 mpv 连接。

## 安装与启动

1. 安装 Python 3.10 或更新版本；Windows 安装时勾选「Add Python to PATH」。
2. 从 [mpv 官网](https://mpv.io/installation/)安装 mpv。播放器会单独打开视频窗口。
3. 解压后双击 `启动追剧学英语.bat`，或在项目目录执行 `python app.py`。如果程序没有自动找到 mpv，在「设置」里选择 `mpv.exe`。
4. 打开自己的本地视频。视频内若有独立的英文和中文文字字幕轨，可分别选择轨道，然后用「切换到双语」控制中文字幕的显示。

`samples/` 提供自制短视频和字幕供功能试用，不含影视作品：可打开 `sample_embedded.mkv` 试独立内置 ASS 轨，也可打开 `sample_video.mp4` 并添加 `sample_en.ass` 和 `sample_zh.ass`。添加单独的外挂 `sample_bilingual.ass` 时会尝试自动分离按行排列的英中字幕。

## 选词与词典

- 点击「选词窗口」打开独立窗口，点击其中的英文字幕单词查词。原文区只占两行，可以滚动；下方释义区可滚动，调整窗口大小时中文速览和其余释义仍可查看。可拖动窗口边框或用标题旁的「－」「＋」按钮改变大小，窗口尺寸会记住。
- [Free Dictionary API](https://dictionaryapi.dev/) 提供按词性排列的多条英文释义、音标和可用例句。未导入本地词库时，[MyMemory](https://mymemory.translated.net/doc/spec.php) 提供中文速览和前几条英文释义的机器翻译参考。联网查询在后台进行，仅发送所选单词及至多六条英文释义，不发送整段字幕。免费匿名接口有[每日额度](https://mymemory.translated.net/doc/usagelimits.php)，不足时仍可查看英文释义或使用词典链接。
- 如果希望获得更多中文词义，在「设置」中选择「导入 ECDICT CSV…」，导入从 [ECDICT 项目](https://github.com/skywind3000/ECDICT)下载的 `ecdict.csv`。首次导入会在后台建立索引；完成后重新点词。本地中文词义按词性展开，已收录的单词断网也可即时查询；词库未收录时自动使用在线词典。词库保存在用户目录的 `.series_english/ecdict.sqlite3`，程序不会再依赖原 CSV 路径。ECDICT 使用 [MIT 许可证](https://github.com/skywind3000/ECDICT/blob/master/LICENSE)。
- 选中单词后可用窗口下方的「有道词典」或「剑桥词典」链接在浏览器查看更完整、专业的释义。词义不保证适合当前剧情语境，请结合句子判断。
- 词典临时不可用时仍可以点开这些链接。查询成功的词条会在本次运行期间缓存。

## 字幕与快捷键

- 支持视频内独立文字字幕轨（包括 MKV 的 ASS），以及外挂 ASS、SSA、SRT、VTT。语言识别不准确时可在「英文」「中文」下拉框手动选择。
- 英文轨保持显示；默认 `Ctrl+B` 切换中文字幕。mpv 负责渲染 ASS 样式，辅助中文轨的样式可能有所简化。没有中文轨时仍可只看英文。
- 外挂 ASS 如果把英文和中文分在同一条事件的两行，加载时会尝试自动拆成两个轨道；混在同一行或有复杂特效的字幕可能无法拆分。视频**内置的单条双语 ASS**不提供拆分功能，可使用分开的字幕轨或外挂字幕。
- 默认快捷键：`Ctrl+O` 打开视频，`Ctrl+L` 添加字幕，`Ctrl+B` 切换字幕，空格暂停/播放，左右方向键后退/前进五秒，`Ctrl+W` 打开选词窗口，`F2` 打开设置。在「设置」里可以改键、指定 mpv 程序路径、选择是否让选词窗口始终置顶；设置会保存。
- 字幕切换、暂停、跳转快捷键在 mpv 窗口聚焦时也有效；打开文件、选词窗口和设置快捷键在控制面板聚焦时使用。

## 使用限制

烧录在视频里的硬字幕、蓝光 PGS 等图片字幕无法直接提取文字或点词。播放器画面内的字幕不可点击；请在选词窗口里点击字幕单词。某些播放器独占全屏模式可能遮挡置顶窗口，可使用窗口化或无边框全屏。

执行 `python -m unittest discover -s tests -v` 可检查字幕处理、词典解析和进程通信。开发环境没有图形桌面或 mpv 时，需要在自己的电脑上检查窗口展示和视频播放。

[mpv 手册](https://mpv.io/manual/stable/) · [Free Dictionary API](https://dictionaryapi.dev/) · [MyMemory 接口说明](https://mymemory.translated.net/doc/spec.php)
