# web2rss

通过GitHub Action 自动生成网页的 RSS 订阅。
已有的RSS订阅：[点击查看](rss/readme.md)

## 站点配置
配置文件：config.yaml

每个站点的配置项如下：

- **name**: 站点的名称，用于标识不同的站点。
- **follow_desc**: 用于follow的订阅所有权描述方式的验证。
- **url**: 站点的 URL 地址。
- **block_css**: 父元素的 CSS 选择器，用于定位单个文章块。
- **title_css**: 在父元素内部的标题选择器，用于提取文章标题。
- **description_css**: 在父元素内部的描述选择器，用于提取文章描述。
- **link_css**: 在父元素内部的链接选择器，用于提取文章链接。链接在父元素的情况下，该字段置空
- **use_headless_browser**: 是否使用无头浏览器进行页面加载，布尔值（`true` 或 `false`）。

### 示例配置

```yaml
sites:
  - name: splunk_blog
    follow_desc: 
    url: "https://www.splunk.com/en_us/blog"
    block_css: ".card"  # 父元素的 CSS 选择器
    title_css: "a h3.splunk2-h4"  # 在父元素内部的标题选择器
    description_css: ".splunk-body.shorter-height"  # 在父元素内部的描述选择器
    link_css: "a.headline"  # 在父元素内部的链接选择器
    use_headless_browser: true
```