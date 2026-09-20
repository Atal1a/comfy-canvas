# 桌面端

[返回展示首页](../../../README.md) · [查看手机端](../mobile/README.md)

部分图片用于界面演示；庭院日夜对比与海岸视频为实际工作流结果。

## 首页

最近作品、创作工具和共享队列放在同一页。

![桌面首页](home.png)

## 创作

上传图片，填写编辑要求，再设置画幅和参数。

![参考图和编辑要求](create-reference.png)

![参数与 LoRA](create-parameters.png)

## 结果与复用

原图、结果和参数放在一起；重跑时可以带回原有内容继续调整。

![实际编辑结果](result.png)

<details>
<summary>复用原图和提示词</summary>

![带原有内容重跑](rerun.png)

</details>

## 作品瀑布流

横幅和竖幅按各自比例排列，向下滚动继续浏览。

![作品瀑布流](gallery.png)

![桌面瀑布流实际滚动](gallery-scroll.gif)

[观看清晰版视频](gallery-scroll.mp4)

<details>
<summary>只看收藏的作品</summary>

![收藏筛选](favorites.png)

</details>

<details>
<summary>裁切图片</summary>

选择画幅，拖动框选区域，再应用裁切。

![裁切交互](crop.gif)

</details>

<details>
<summary>管理账号与共享队列</summary>

查看账号、邀请码与共享队列。

![管理员页面](admin-live.png)

</details>

## 双参考图与参数

在同一张表单里查看两张参考图，设置批量数量、画幅和 Seed。

![双参考图](krea-dual-reference.png)

![批量与 Seed](krea-dual-parameters.png)

多个 LoRA 槽位可以分别填写权重。图中条目为界面演示，未安装模型文件。

![LoRA 配置演示](lora-slots-demo.png)

## 视频创作

H3 支持从文字开始，也可以上传图片作为首帧。

![文字入口](h3-text-entry.png)

![首帧图片入口](h3-image-entry.png)

![视频参数](h3-parameters.png)

这段海岸视频由 H3 工作流实际生成。

![真实视频播放](h3-playing.png)

[播放海岸视频](coast-h3-result.mp4)

## 任务与作品管理

运行中的任务可以取消。

![运行任务](task-running.png)

![取消完成](task-cancelled.png)

删除的内容进入回收站，15 分钟内可恢复；逾期自动永久清理。

![回收站](recycle-bin.png)

[删除与恢复操作](recycle-restore.mp4)

## 原图浏览

打开原图后可以放大，再拖动查看局部。

![原图放大](original-zoom.png)

[放大与拖动视频](original-zoom-pan.mp4)

用收藏夹整理作品，分享时将输入图和参数一起交给另一个账号。

![收藏夹](collections.png)

![接收分享](share-received.png)

## 管理与帮助

查看存储、数据库和后端连接状态，参数不清楚时可以打开页面说明。

![存储管理](storage.png)

![运行诊断](diagnostics.png)

![页面帮助](page-help.png)

## LoRA 说明与结果查看

阅读说明、选择 LoRA，再填写权重。图中使用演示条目，见[素材说明](../NOTES.md)。

![LoRA 选择与说明](lora-guide-v9.png)

![LoRA 完整说明](lora-description-v9.png)

打开实际生成的蓝调庭院，放大查看细节。

![生成结果查看器](result-viewer-v8.png)
