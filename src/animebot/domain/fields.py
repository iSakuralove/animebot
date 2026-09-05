"""字段别名表 —— 全部从 1486 个真实帖子的统计里长出来，不是设计出来的。

key 一律先经 `normalize_key()` 剥掉 emoji 装饰再查表，所以这里只写素面写法。
括号里的数字是该写法在真实数据中的出现次数，用于判断优先级和是否可以删。

改动原则：帖子已经发出去了，改不了。新增写法只往这里加，不动解析代码。
"""

from __future__ import annotations

import re

# 剥掉 key 两侧的 emoji / 符号 / 空白，只留中日文、拉丁字母、数字
_KEY_JUNK = re.compile(r"[^0-9A-Za-z一-鿿぀-ヿー]+")


def normalize_key(raw: str) -> str:
    """'☺️评分' -> '评分'; '🩶GoogleDrive' -> 'GoogleDrive'; '⛓️解压' -> '解压'"""
    return _KEY_JUNK.sub("", raw)


# ---------------------------------------------------------------- 元信息字段
# canonical -> 真实写法。解析时反查成 alias -> canonical 的扁平表。
META_ALIASES: dict[str, tuple[str, ...]] = {
    "title_cn":     ("中文名", "名字"),                       # 1446 + 9
    "title_en":     ("英文名",),                              # 114
    "title_alias":  ("别名",),                                # 5
    "episodes":     ("话数",),                                # 1481
    "air_date":     ("放送开始", "上映年度", "上映"),           # 1361 + 14
    "air_weekday":  ("放送星期",),                            # 1214
    "duration":     ("片长",),                                # 6
    "score":        ("评分", "个人评分", "豆瓣评分", "黑猫简评"),  # 1483 + 6
    "tags":         ("标签",),                                # 1596
    "index":        ("引索", "索引"),                          # 1496 + 0
    "password":     ("解压", "解压码", "解压密码"),             # 1663
    "password_alt": ("如密码不对该换",),                        # 74 备用密码
    "extract_code": ("提取码",),                              # 21
    "file_size":    ("文件大小",),                            # 29
    "volumes":      ("册数",),                                # 3
}

# ---------------------------------------------------------------- 制作人员
# 全部塞进 Post.staff 这个 dict，不为每个职位开一列 —— 职位有 20 种且长尾。
STAFF_ALIASES: dict[str, tuple[str, ...]] = {
    "director":     ("导演",),                                # 1409
    "script":       ("脚本", "系列构成"),                      # 769 + 4
    "original":     ("原作", "作者", "插图", "漫画"),           # 766
    "storyboard":   ("分镜",),                                # 617
    "performance":  ("演出",),                                # 31
    "music":        ("音乐", "音楽", "音乐目录"),               # 47
    "studio":       ("制作", "製作", "动画制作",
                     "アニメーション制作", "企画"),              # 38
    "chara_design": ("人物设定", "人物原案", "美术监督"),        # 15
    "voice":        ("配音监督", "声优"),                      # 4
    "producer":     ("监制", "企划制作人", "制作人"),            # 7
    "publisher":    ("出版社",),                              # 4
}

# ---------------------------------------------------------------- 资源链接
# 同一网盘的 emoji 前缀有 7 种变体，剥离后才收敛。
LINK_ALIASES: dict[str, tuple[str, ...]] = {
    "baidu":    ("百度网盘", "百度", "百度下载", "百度秒传",
                 "秒传", "秒传见表格"),                        # 1511 + 122
    "onedrive": ("OneDrive", "辅助网盘"),                     # 1104 + 204
    "gdrive":   ("GoogleDrive", "GD", "谷歌网盘"),            # 124 + 9 + 61
    "aliyun":   ("阿里网盘", "阿里", "阿里下载", "阿里4K"),      # 119
    "quark":    ("夸克网盘",),                                # 29
    "sheet":    ("往期番剧汇总表格", "往期番剧汇总", "信息页面"),  # 1188 + 25
    "raw_disk": ("原盘链接",),                                # 3
    "official": ("官方网站",),                                # 3
}

# 正文里出现但不是字段的行首词，命中即跳过，避免污染 extra
NOISE_KEYS: frozenset[str] = frozenset({
    "https", "http", "PS", "Re", "链接", "tg", "t",
})

# 按钮文字 -> 链接类型。按钮只有 129 个帖子有，但文字比正文 key 干净。
BUTTON_ALIASES: dict[str, str] = {
    "百度链接": "baidu", "百度网盘": "baidu", "百度": "baidu",
    "谷歌表格": "sheet", "秒传见表格": "sheet",
    "谷歌网盘": "gdrive",
    "OD节点": "od_node", "CDN节点": "cdn_node",
}

# 全频道公用的汇总表格，不入库到单条帖子（1964 次 + 90 次）
SHARED_SHEET_IDS: frozenset[str] = frozenset({
    "1q2JyP3A4lok0jhkN-Tl3N3D5uo8uDJFX",
    "1dkruirMvu78hvf6JA9sL2ADvdQe7dnjT0xwOsIFhp44",
})


def _flatten(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, aliases in groups.items():
        for a in aliases:
            out[normalize_key(a)] = canonical
    return out


META_LOOKUP = _flatten(META_ALIASES)
STAFF_LOOKUP = _flatten(STAFF_ALIASES)
LINK_LOOKUP = _flatten(LINK_ALIASES)
