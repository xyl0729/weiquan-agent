# 国家法律法规数据库(flk.npc.gov.cn)接口备忘

补录法条时用。前端是 SPA,`/detail?id=...` 只返回外壳,真实数据走下面的接口。

## 详情接口

```
GET https://flk.npc.gov.cn/law-search/search/flfgDetails?bbbs=<ID>
Headers: User-Agent: Mozilla/5.0
         Referer: https://flk.npc.gov.cn/detail?id=<ID>
```

参数名是 **`bbbs`** 而不是 `id`,值就是详情页 URL 里的那个 id。

民法典(`ff808081729d1efe01729d50b5c500bf`)返回约 197KB,字段:

| 字段 | 含义 | 民法典取值 |
|---|---|---|
| `title` | 法规名称 | 中华人民共和国民法典 |
| `gbrq` | 公布日期 | 2020-05-28 |
| `sxrq` | 施行日期 | 2021-01-01 |
| `sxx` | 时效性 | 3 |
| `flxz` | 法律性质 | 法律 |
| `zdjgName` | 制定机关 | 全国人民代表大会 |
| `content` | **只有目录树**,不含条文正文 | — |
| `ossFile.*` | Word/PDF/OFD 的 OSS 相对路径 | — |

**注意 `content` 只是目录树**(每条一个 `{id, parentId, title, index}` 节点,`title` 是「第七百零三条」这样的条号),**没有条文正文**。想批量取正文得另找路子,这个接口给不了。

## `sxx` 时效性枚举(已核实)

| 值 | 含义 |
|---|---|
| 1 | 已废止 |
| 2 | 已修改 |
| 3 | **有效** |
| 4 | 尚未生效 |

所以民法典的 `sxx: 3` 是「有效」。

**核实方式**:从官方前端 bundle `https://flk.npc.gov.cn/assets/index-Y9B5oxpu.js` 里读到

```js
L4e=[{label:"尚未生效",key:4},{label:"有效",key:3},{label:"已修改",key:2},{label:"已废止",key:1}]
```

并确认该数组正是高级检索里 `fieldName:"sxx"` 那个筛选控件的选项表(绑定在 `l = D({fieldName:"sxx", values:[]})` 的 checkbox-group 上),不是别的状态字段。民法典 `sxx=3` 与「有效」也对得上。

**残留风险**:这是从前端筛选项反推的,不是官方字段文档;bundle 文件名带 hash,改版后 URL 会失效。如果要拿 `sxx` 做自动化的时效性拦截(比如拒绝入库 `sxx != 3` 的法条),建议先手工找一部已废止的法规验一遍 `sxx == 1`——我没做这个交叉验证,`search/list` 接口的入参格式还没摸清(POST 上面那套 body 返回 `{"code":500,"msg":"系统异常"}`)。

## 其他已探明的接口

| 路径 | 用途 |
|---|---|
| `law-search/search/enumData` | 法规分类、制定机关分类树(GET,无参)。**不含 `sxx` 枚举** |
| `law-search/search/flfgDetails` | 详情(见上) |
| `law-search/search/list` | 检索列表(POST,入参格式未摸清) |
| `law-search/amazonFile/previewLink` | 换取 OSS 文件的可访问链接(参数未摸清) |
| `law-search/search/hitDisplay` | 命中高亮(不支持 GET) |

`ossFile.ossWordPath` 之类是相对路径,直接拼 `https://flk.npc.gov.cn/` 会被 SPA 兜底返回 HTML 外壳,得走 `previewLink` 换签名链接。

## 录入纪律

流程上「法条正文必须用户本人录入」的约束已于 2026-08-28 取消,可以自己抓。但质量要求没变——**一条错的法条比没有法条更糟**,用户会拿着它去跟房东、商家、单位交涉。

正文格式与库内既有条目保持一致:条号后跟全角空格 `　`。

只录了部分条款的,必须在引用处注明。现例:`data/seed_statutes.yaml` 的第七百三十四条**只有第一款**(不定期租赁),第二款的优先承租权不在正文里,所以 `app/agent/grounding.py` 的 `_RENTAL_HOLDOVER_MARKERS` 刻意不覆盖「涨房租后想续租」这类纯二款问题,缺口记在 `data/statute_wishlist.yaml`。
