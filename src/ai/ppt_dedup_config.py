"""Configurable rules for PPT page dedup and OCR noise cleaning.

Edit this file to add/remove patterns without touching the logic code.
The main dedup module (ppt_dedup.py) reads these constants on import.

Each section documents which function consumes it and the match strategy.
"""

from __future__ import annotations

# ── DHASH dedup config ──────────────────────────────────────────────────────
# Used by dedup_dhash() before OCR — drops near-duplicate frames by perceptual
# hash so OCR only runs on visually distinct pages.
DHASH_WINDOW: int = 30       # sliding window size (images to compare against)
DHASH_THRESHOLD: int = 3     # max Hamming-distance bits to consider "duplicate"

# ── Text subset dedup config ────────────────────────────────────────────────
# Used by dedup_text_subset() after OCR — removes pages whose text is a
# near-subset of a nearby page (common with PPT animation reveals).
SUBSET_CONFIG: dict = {
    "window": 20,                  # look-back window (kept pages)
    "ngram_n": 3,                 # n-gram size for containment scoring
    "containment_threshold": 0.85,  # min n-gram containment to flag subset
    "min_length_ratio": 1.10,     # long/short length ratio must exceed this
    "protect_min_chars": 10,      # pages shorter than this get a stricter 0.95 threshold
}

# ── Full-page invalidation patterns (is_invalid_page) ───────────────────────
# After OCR, is_invalid_page() normalises the entire page text (strips
# whitespace + punctuation, lowercases ASCII) and checks whether any of these
# substrings appear.  If yes, the page is discarded entirely.
#
# PICK patterns that are:
#   - Specific enough that they never occur in real lecture material
#   - Long enough (>=6 normalised chars) that incidental OCR noise won't hit
#   - Drawn from unique features of known noise screens
INVALID_PAGE_PATTERNS: list[str] = [
    # ── Type 1: classroom desktop wallpaper ──
    "请不要关闭设备",
    "避免耽误第34节上课",
    "触控显示器无线话筒hdmi",
    "多媒体值班室",
    "本教室装有摄录及安全装置",
    # ── Type 2: e-learning resource portal screen ──
    # "cfdfudaneducn",                       # cfd.fudan.edu.cn URL
    "icoursefudaneducn",                   # icourse.fudan.edu.cn URL
    "智慧教学资源平台使用规范",
    "教育部等九部门",
    "欢迎使用eLearning",
    "加快推进教育数字化",
    "本科课程评教提醒",
    "请于期末考试前完成评教",
    "微信搜索并关注复旦课评",
    "国务院关于深入实施",
    "板书效果展示",
    "双屏效果展示",
    "课程录制exe",
    "PowerPoint演示文稿",
    # "录像指南",
    "ev去噪",
    "录制完成桌面会生成",
    "推荐上传至elearning",
    "ppt演示者视图会影响录屏",
    # ── Type 3: Windows file explorer (full-screen) ──
    # Normalised breadcrumb: "此电脑>U盘(D:)>..." → "此电脑u盘"
    # "此电脑", "改日期", "全屏模式", "此电脑本地磁盘",
    # File-list column headers (normalised: "名称修改日期类型大小")
    # "名称修改日期类型大小",
    # File picker title bar
    "选择要上传的文件",
    "选择要打开的文件",
    # "打开此文件",
    # ── Type 4: Windows lock screen / Ctrl+Alt+Delete ──
    "ctrlaltdel",
    "请按ctrlaltdelete",
    # ── Type 5: system update / shutdown screens ──
    "正在配置windows",
    "请不要关闭计算机",
    "正在更新你的系统",
    # ── Type 6: end-of-class desktop / slideshow-end screens ──
    "正在关机",
    "放映结束单击鼠标退出",
    "要退出全屏请按",
    # ── Type 7: browser / overlay / video-player full-screen noise ──
    "要调出全屏",                                  # "要调出全屏，需接Esc"
    "checkvideosource",                           # video player overlay
    "无痕模式",                                    # browser incognito mode badge
    # ── Type 8: WPS Office desktop (full-screen, no slide visible) ──
    # "wwpsoffice",
    # ── Type 9: Chrome / browser chrome ──
    "googlechrome不是您的默认浏览器",
    # ── Type 10: WeChat file-helper URL ──
    "filehelperweixinqqcom",
    # ── Type 11: Tencent Docs online editor (full-screen) ──
    # "腾讯文档手机发令电脑自动干活",
]

# ── Per-line noise stopwords (clean_ppt_text exact match) ───────────────────
# clean_ppt_text() splits OCR'd text into lines and drops any line that
# exactly matches one of these (after whitespace normalisation).
#
# Strategy: only match when the ENTIRE line IS the label.  Substring matching
# would risk removing real content — e.g. "幻灯片" is a stopword, but
# "幻灯片设计原则" passes through.
PPT_UI_STOPWORDS: set[str] = {
    # ── Ribbon tabs ──
    "文件", "开始", "插入", "设计", "切换", "动画",
    "幻灯片放映", "审阅", "视图", "加载项", "帮助",
    # ── Home tab clusters ──
    "粘贴", "剪切", "复制", "格式刷", "新建", "重置",
    "剪贴板", "字体", "段落", "快速样式", "样式",
    "绘图", "编辑", "排列", "形状填充", "形状轮廓",
    "形状效果", "选择", "查找", "替换", "a替换",
    "ac替换", "目复制", "突出显示", "擦除",
    # ── Insert / Design / Transitions tabs ──
    "表格", "图片", "形状", "图标", "SmartArt", "图表",
    "文本框", "页眉和页脚", "艺术字", "公式", "符号",
    "视频", "音频", "屏幕录制",
    "主题", "变体", "格式", "背景格式",
    "切换到此幻灯片",
    # ── Animations / Slide Show tabs ──
    "动画窗格", "添加动画", "触发",
    "从头开始", "从当前幻灯片开始", "自定义放映",
    "设置幻灯片放映", "隐藏幻灯片",
    # ── Review / View tabs ──
    "拼写和语法", "同义词库", "字数统计", "批注",
    "显示批注", "比较", "接受", "拒绝",
    "页面视图", "阅读视图", "大纲视图",
    "备注", "备注页", "显示比例", "适应窗口",
    "标尺", "网格线", "参考线",
    "拆分", "新建窗口", "全部重排", "层叠",
    "切换窗口", "宏",
    # ── Status bar ──
    "中文（中国）", "简体", "登录", "共享",
    "备注", "批注", "幻灯片", "+创建", "十创建",
    "告诉我您想要做什么",
    "A朗读此页内容", "朗读此页内容",
    # ── Drawing / Shape tools (contextual tab sub-labels) ──
    "绘制", "编辑形状", "文本填充", "文本轮廓",
    "文本效果", "转换为SmartArt", "选择窗格",
    "上移一层", "下移一层",
    # ── Single-char icon labels (appear 15-100+ pages each) ──
    # These are icon-only PowerPoint buttons that OCR reads as a
    # single character.  The list is restricted to characters that
    # appeared on >=10 distinct pages across our 7-lecture sample
    # AND are consistent with ribbon/gallery icons.
    # Note: with the <=2-char catch-all in clean_ppt_text, this
    # section is only a documentation / override layer.
    "口", "品", "日", "昆", "国", "田", "单", "回",
    "器", "三",
    # Keyboard-shortcut hint letters (Alt-key ribbon navigation).
    # Each appears on 8-40+ distinct pages, evenly across courses.
    "A", "B", "C", "D", "H", "I", "K", "M", "P", "Q", "S",
    "X", "a", "b", "k", "w", "x",
    # ── Font/typeface labels in the ribbon ──
    "楷体", "五号", "五号AA", "A字", "Aa",
    # ── Common OCR garbage from UI chrome ──
    "三菜单", "国版式", "目复制",
    "AaBbCc", "AaBbCcDAaBbCcDAaBbCcAaBbCc",
    "登录共享", "）简体",
    # ── Ribbon paragraph-formatting labels ──
    "I文字方向", "文字方向", "[对齐文本", "[]对齐文本",
    "对齐文本", "↑←",
    "abc替换", "c替换",
    # ── Truncated/fused toolbar labels ──
    "告诉我您想要做什", "形状轮廊",
    # Fused adjacent ribbon labels (OCR treats them as one line)
    "幻灯片节",
    # ── Style gallery labels from the Home tab ──
    "日期", "邮件",
    # ── IDE / dev-tool panel headers ──
    "大纲", "时间线", "源", "导航", "运行",
    "rundebug", "RunDebug",
    "Bloop", "Usage", "CUE", "CuePro",
    "问题", "输出", "筛选器", "任务", "终端", "控制台",
    "暂无编辑建议",
    "暂无编辑建议请先进行编码操作",
    "试试8U与AI聊天8I与AI一起编写代码",
    # ── IDE sidebars ──
    "资源管理器",
    # ── IDE plugin / tool status ──
    "Java:Ready", "Go:Ready", "Python:Ready",
    # ── System dialog buttons ──
    "继续使用此应用", "始终使用此应用", "确定",
    "Google Chrome", "Microsoft Edge", "Internet Explorer",
    "是否保留墨迹注释", "保留", "放弃",
    # ── Browser download-manager chrome ──
    "全部清除", "搜索下载记录", "下载记录",
    # ── Input method / language-bar indicators ──
    "认", "证", "门", "退",
    "EM", "PN",
    # ── WeChat / IM input placeholders ──
    "说点什么",
    # ── IDE panels and tool names ──
    "调试控制台", "版本库",
    # ── IDE / terminal indicators ──
    "zsh", "bash", "fish",
    # ── Right-click / context menu items ──
    "移动到", "复制到", "移动到复制到",
    # ── Browser-based PDF / SmallPDF toolbar labels ──
    "所有工具",                              # SmallPDF "All Tools" sidebar
    "查找文本或工具Q",                       # SmallPDF search bar
    "电子签名",                              # SmallPDF e-signature
    # ── Acrobat / PDF reader toolbar buttons ──
    "转换",                                 # Convert
    "签名",                                 # Sign
    "压缩PDF",                              # Compress PDF
    "保护PDF",                              # Protect PDF
    "将PDF标记为密文",                      # Mark as Confidential
    "准备表单",                             # Prepare Form
    "扫描和OCR",                            # Scan & OCR
    "主页工具",                             # Home Tool
    "查看更多",                             # See more
    "填写并签名",                           # Fill & Sign
    "转换为PDF",                            # Convert to PDF
    "编辑转换电子签名",                     # Fused toolbar labels
    "转换电子签名",                         # Fused toolbar labels
    "转换签名",                             # Fused toolbar labels
    "子签名",                               # Sub-menu sign
    "PowerPoint演示文稿",                   # PDF reader window title
    # ── Browser/PDF menu bar (full line) ──
    "文件（F）编辑（E）视图（V）签名（S）窗口（W）帮助（H）",
    # ── Keyboard shortcut / UI artifacts from PDF readers ──
    "AL", "LO", "[AL",
    # ── PowerPoint slide-sorter status ──
    "回", "□",
    # ── PowerPoint thumbnail sidebar ├──
    "大纲视图",
    # ── PowerPoint inking / drawing context toolbars ──
    "墨迹书写工具",                                # inking contextual tab
    "绘图工具",                                    # drawing tool contextual tab
    # ── Windows file-dialog chrome ──
    "打开", "保存", "取消",
    "搜索结果", "没有搜索到结果",
    # ── Browser-based PDF toolbar buttons ──
    "导出PDF", "编辑PDF", "创建PDF",
    "合并文件", "整理页面", "添加注释",
    # ── Tencent Docs online editor ──
    "腾讯文档",
    "手机发令，电脑自动干活",
    "效率工具", "默认字体",
    # ── WPS premium / features ──
    "会员专享", "WPSAI",
    # ── WPS ribbon / toolbar labels ──
    "分享",                              # Share button
    "工具",                              # Tools tab
    "校对",                              # Review > Proofreading
    "智能美化",                          # WPS Smart Beautify
    "放映",                              # Slide Show tab (WPS uses "放映" not "幻灯片放映")
    "条件格式",                          # Conditional Formatting (spreadsheet in PPT context)
    "表格样式",                          # Table Styles
    "当页开始",                          # "从当前页开始" (Start from current) missing leading char
    "版式",                              # Layout gallery
    "新建幻灯片",                        # New Slide
    "WWPSOffice",                        # WPS branding
    "单击此处添加备注AI演讲稿",           # WPS AI speech draft placeholder
    "三文件",                            # Fused OCR: hamburger icon + File tab
    "分组",                              # Group button
    # ── Academic database / reader UI ──
    "文献解读", "文献评述",
    # ── Video player overlay ──
    "退出播放",
    # ── OCR variant of existing stopwords ──
    "aac替换",                                    # OCR variant of "ac替换"
}

# ── Per-line regex patterns (clean_ppt_text fullmatch) ──────────────────────
# For lines whose exact text varies (numbers, timestamps, counts) but which
# are clearly UI chrome.  Each is a raw regex string; ppt_dedup compiles them
# on import and applies re.fullmatch against the normalised line.
UI_NOISE_LINE_PATTERNS: list[str] = [
    # Page/slide counters: "第2页，共5页", "幻灯片第22张，共33张"
    r"^(?:幻灯片\s*)?第\d+[页张][，,]?\s*共\d+[页张]$",
    # Zoom level: "62%", "200%"
    r"^\d{1,3}%$",
    # Word count: "3401个字"
    r"^\d{1,6}个字$",
    # URI fragment: "/19"
    r"^/\d{1,3}$",
    # Isolated date: "2026-03-24"
    r"^\d{4}-\d{2}-\d{2}$",
    # Timestamp: "10:02" or "10:02:17"
    r"^\d{1,2}:\d{2}(:\d{2})?$",
    # English date: "May29,2025"
    r"^[A-Z][a-z]+\d{1,2},\s*\d{4}$",
    # Truncated Word/PDF labels
    r"^First Pa\.\.$",
    r"^AbstractJAbs?tra\.+$",
    r"^AuthorJCompact$",
    # Formula OCR noise: "Uabexx²²²..."
    r"^Uabexx²+$",
    # Single initial with dot: "A."
    r"^[A-Z]\.$",
    # University name: "FudanUniversity)"
    r"^[A-Z][a-z]+University\)?$",
    # Truncated window titles (star + text): "☆..."
    r"^☆.{4,}.+$",
    # Document window title: "...docx - Word", "...pptx - PowerPoint",
    # also ".ppt[兼容模式]-PowerPoint" and ".ppt - PowerPoint"
    r"^.*[.·]?docx[^-]*[-(（\s]*Word.*$",
    r"^.*[.·]?pptx?[^-]*[-(（\s]*PowerPoint.*$",
    # PPT placeholder text
    r"^单击此处添加(?:备注|标题|副标题|正文)$",
    # Standalone PPT placeholder labels (appear in thumbnail sidebar / master view)
    r"^(?:标题[1-5]|副标题|正文|[1-5]级标题)$",
    # PowerPoint thumbnail sidebar: slide number alone
    r"^\d+/\d+$",
    # IDE / dev-tool indicators
    r"^demo[（(]\d+[）)]$",                          # "demo(3)" or "demo（3）"
    r"^行\d+[，,]\s*列\d+",                          # "行6,列27" cursor position
    r"^[（(]已选择\d+[）)]$",                         # "(已选择1)" line selection
    r"^Cue[ -]?Pro$",                                 # "Cue-Pro" / "CuePro"
    r"^run\s*[|│]\s*debug$",                          # "run|debug" IDE button
    r"^Compiled [a-zA-Z]+\(",                          # "Compiled demo(" IDE compile status
    r"^Activating [a-z ]+",                            # "Activating task providers java"
    # IDE problems / changes panel
    r"^已处理\d+/\d+个变更点",                         # "已处理0/0个变更点"
    r"^问题\d+",                                       # "问题70" - IDE Problems counter
    # IDE placeholder split across OCR lines
    r"^暂无编辑建议[，,].*",                            # partially visible IDE placeholder
    r"^码操作\.\.\.$",                                 # continuation: "...码操作..."
    # IDE search bar
    r"^Q搜索",                                         # "Q搜索" - IDE/app search box
    # Keyboard shortcut hints from context menus: "Ctrl+R", "Alt+向左键"
    r"^(?:Alt|Ctrl|Shift)(?:\+(?:[A-Za-z0-9\[\]]|[一-鿿]{1,4}))*$",
    # Word Protected View banner (multi-line)
    r"^受保护的视图请注意",                             # "受保护的视图请注意..."
    r"^启用编辑[(（]E[)）]",                            # "启用编辑（E)"
    # Word ribbon sub-labels
    r"^布局引用",                                      # "布局引用"
    r"^审阅引用",                                      # "审阅引用" variants
    # File explorer status bar
    r"^\d+个项目$",                                    # "15个项目"
    r"^\d+[KMGTP]B$",                                  # "423KB" / "5MB"
    # Windows system dialog: "你要如何打开此文件？"
    r"^你要如何打开此文件",
    # PowerPoint ink retention dialog
    r"是否保留墨迹注释",
    # Input method status line
    r"^认$",
    r"^证$",
    r"^门$",
    r"^退$",
    # PowerPoint slide-sorter / thumbnail sidebar trash icon single chars
    r"^口$",
    # File explorer breadcrumb prefix
    r"^[>＞]此电脑",
    # IDE sidebar file-path root
    r"^src/main/java",
    r"^\.(bloop|metals|vscode|gradle|idea|mvn)$",
    # IDE tree file entries with expand indicators
    r"^[▸▶▷>][^，。；一-鿿]{0,30}$",
    # IDE sidebar section title
    r"^文件$",
    # File list column headers
    r"^(名称|修改日期|类型|大小)\s+(名称|修改日期|类型|大小)",
    # PDF viewer / browser window title with file path
    r"^[①-⑩]?文件\|(?:[A-Z]:)?[/\\]Users[/\\].*",
    # Slash-separated date: "2026/4/13" (different from dash-separated above)
    r"^\d{4}/\d{1,2}/\d{1,2}$",
    # PowerPoint contextual tab label fused with adjacent number: "1文字方向"
    r"^\d+文字方向$",
    # Font dropdown: "Arial(标题）" or "Calibri(正文）"
    r"^[A-Z][A-Za-z]+\s*[（(][^）)]{1,6}[）)]$",
    # PDF filename in viewer title bar: "Reading pie charts.pdf"
    r"^.+\.[pP][dD][fF]$",
    # Standalone two-digit page numbers from PDF reader: "10", "21"
    r"^\d{2}$",
    # PowerPoint window title without .pptx: "PowerPoint演示文稿 - Adobe Acrobat Reader"
    r"^PowerPoint演示文稿.*$",
    # OCR variants of pptx: "ppbx-PowerPoint", "ppx-PowerPoint"
    r"^.*[.·]?pp[bt8]x?[^-]*[-(（\s]*PowerPoint.*$",
]
