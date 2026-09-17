# Mac 补采

Chrome Manifest V3 扩展复用 Mac 的网络和该 Chrome 配置文件的登录状态。它只读取服务端待采队列中的直接文章链接，不探索更深层网页，不导出 Cookie、密码、存储或浏览历史。手机可在「网页采集中心 → Mac 补采」查看是否在线、已保存数量和待验证数量。

## 首次安装

1. 运行 `node app/mac/build.cjs PRIVATE_CONFIG.json OUTPUT_DIR`，配置文件须为 0600，包含 `server`、`token`、`domains`（最多 30 个明确公开域名）。`token` 必须是服务端 `RADAR_COMPANION_TOKEN`，不要使用管理令牌。
2. Chrome 打开 `chrome://extensions`，开启开发者模式，选择「加载已解压的扩展程序」，选择输出目录。
3. 点击「AI Radar · Mac 补采」扩展，点击「授权网站并启动」。此后 Chrome 运行时每 30 秒检查；按顺序打开后台标签页，提交正文后关闭自己创建的标签页。

构建产物含专用凭据，只能保存到已忽略的 `dist/` 或个人应用支持目录，不能发布到 Git 或分享给别人。源码里的 manifest 不包含凭据。凭据只能访问补采队列、正文提交和心跳，不能访问 Chat 或管理员 API。轮换 `RADAR_COMPANION_TOKEN` 后重新打包即可撤销旧安装。

## 恢复与限制

- 网站验证时保留该网站标签页并暂停该域名，其他域名继续处理。用户在 Mac 完成验证；目标页重新加载后会再尝试，也可点击「已完成验证，继续」。不能保证网站永远不再要求登录或验证码。
- 断网、加载失败使用持久化指数退避，最长 6 小时；Chrome 重启恢复未完成任务。服务端按内容哈希去重，重复回执不产生重复翻译。暂停开关与执行状态分开保存，迟到的请求不能撤销暂停。
- 避免浏览器超大页面外壳导致失败，先定位 article/main，再用 Mozilla Readability 提取正文；超过 50,000 元素时以可见正文块进行有界提取。最多提交 60,000 字，超出时明确标记部分正文，不能声称已读全文。原网站的 PDF 查看器、付费权限和部分特殊页面仍可能无法提取；服务器的 PDF/Crawl4AI 通道继续负责这些格式。
- 服务器保留原采集、总结和翻译流程。Mac 不在线时不会阻塞雷达原始消息发布；Mac 捕获完成后唤醒正常网页解读队列。
- 扩展只在用户许可的网站上运行。新增域名需更新本地配置并在 Chrome 许可；不会悄悄授予全站访问。

官方机制：[Chrome 内容脚本隔离](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts)、[alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms)。正文提取复用项目已包含的 Mozilla Readability 0.6.0，许可证见 `app/src/vendor/`。
