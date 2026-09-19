"""资源链接分类：真实直链 vs 跳转表格/节点。

为什么按 URL 主机判、而不是信 link 的标签 key：真实数据里 `onedrive` 这个标签
有 523 条实际指向 `docs.google.com` —— 2025 年起 up 主把 OneDrive 行写成了
「打开表格」，指向那个全频道共用的汇总表。同理 `od_node`/`cdn_node` 129 条全是
`od.catimage.work` 的根地址（一个落地页，不是某部番的深链）。

用户要的「真实链接」= 这条帖子自己的、能直接下载的网盘分享链接。判据落在
**这个 URL 指向哪里**，而不是它被标成了什么。所以标签只是兜底信号，host 才是
裁判 —— 消除了「onedrive 到底算不算真链接」这个特殊情况。
"""

from __future__ import annotations

# 链接分类
DIRECT = "direct"       # 真实网盘直链：百度/真 OneDrive/GoogleDrive/阿里/夸克
INDEX = "index"         # 跳转类：汇总表格、OD/CDN 落地节点
OFFICIAL = "official"   # 官网，不是下载

# 标签 -> 中文展示名。顺序即展示顺序（direct 组按这个排）。
# 这是链接展示名的唯一真相来源，presenter 从这里取。
LINK_LABELS: dict[str, str] = {
    "baidu": "百度网盘",
    "onedrive": "OneDrive",
    "gdrive": "谷歌网盘",
    "aliyun": "阿里网盘",
    "quark": "夸克网盘",
    "raw_disk": "原盘",
    # 下面这些是 index / official，展示在详情的次级区
    "sheet": "汇总表格",
    "od_node": "OD节点",
    "cdn_node": "CDN节点",
    "official": "官网",
}

# 标签本身就表明是跳转类的（跟 URL 无关）
_INDEX_KINDS = frozenset({"sheet", "od_node", "cdn_node"})

# direct 组的展示优先级：百度最常见排最前，其余按可用性
_DIRECT_ORDER = ("baidu", "onedrive", "gdrive", "aliyun", "quark", "raw_disk")


def classify_link(kind: str, url: str) -> str:
    """一条链接归到 direct / index / official。

    判定顺序（越确定的越先）：
      1. 标签就是 official -> official
      2. 标签本身是跳转类（表格/节点）-> index
      3. host 是 docs.google.com（表格）-> index，无论标签写的是 onedrive 还是 sheet
      4. 其余 -> direct
    """
    if kind == "official":
        return OFFICIAL
    if kind in _INDEX_KINDS:
        return INDEX
    # docs.google.com 一律是「表格」；drive.google.com（gdrive 真实文件）不在此列
    if "docs.google.com" in url:
        return INDEX
    return DIRECT


def categorize(links: dict[str, str]) -> dict[str, list[tuple[str, str, str]]]:
    """把 post.links 分成三组，每组元素是 (kind, 展示名, url)。

    direct 组按 _DIRECT_ORDER 排（百度在前），其余组按 links 里的出现顺序。
    返回 dict 一定含 direct/index/official 三个键（可能是空 list），调用方不必判 KeyError。
    """
    groups: dict[str, list[tuple[str, str, str]]] = {DIRECT: [], INDEX: [], OFFICIAL: []}
    for kind, url in links.items():
        if not url:
            continue
        label = LINK_LABELS.get(kind, kind)
        groups[classify_link(kind, url)].append((kind, label, url))

    order = {k: i for i, k in enumerate(_DIRECT_ORDER)}
    groups[DIRECT].sort(key=lambda t: order.get(t[0], 99))
    return groups
