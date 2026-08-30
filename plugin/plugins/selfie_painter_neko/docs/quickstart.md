# NEKO 自拍画家

这是一个不修改 NEKO 核心代码的独立插件。它调用图片生成 API，把结果转换为 JPG，保存在插件静态目录中，然后在聊天气泡里发送 Markdown 图片。聊天中显示适配气泡宽度的 JPG 缩略图，点击可以打开完整 JPG。

功能思路参考 `nguspring/selfie_painter`，实现代码基于 NEKO 插件 SDK 独立编写，没有复制 MaiBot 运行时代码。

## 最小配置

编辑本插件的 `plugin.toml`：

```toml
[selfie_painter]
api_format = "openai"
base_url = "https://api.openai.com/v1"
api_key = ""
model = "gpt-image-2"
character_prompt = "1girl, cat ears, expressive eyes"
```

推荐通过环境变量提供密钥，避免把密钥写进配置文件：

```text
NEKO_SELFIE_PAINTER_API_KEY=你的密钥
```

也可以直接打开插件管理器中的“界面”栏填写配置、选择拍摄模式并生成。保存后，对 NEKO 说“拍一张在窗边的自拍”也能触发同一套功能。

## 接口类型

- `openai`：OpenAI、硅基流动、NewAPI 等 `/images/generations` 兼容接口。
- `modelscope`：魔搭社区的异步图片生成接口。
- `dashscope`：阿里云百炼 Qwen-Image 原生接口。北京共享域名使用
  `https://dashscope.aliyuncs.com/api/v1`，推荐模型为 `qwen-image-2.0`。
  也可通过 `DASHSCOPE_API_KEY` 环境变量提供百炼 Key；新加坡或业务空间
  专属 Key 必须填写与 Key 地域匹配的 Base URL。

如果使用远程或 Docker 部署，请把 `public_base_url` 设置为浏览器能访问到的插件服务器地址。本机桌面版通常可以留空。

## 角色参考图

`reference_source` 支持：

- `none`：纯文生图，兼容性最好。
- `active_character`：尝试读取 NEKO 当前角色参考图。
- `file`：读取 `reference_image_path` 指定的本地图片。

参考图会作为图生图参数发送；只有后端支持图生图时才应开启。
当前 OpenAI 官方地址的纯插件适配只使用 `/images/generations`，请保持 `reference_source = "none"`；硅基流动、NewAPI 等兼容后端是否支持参考图，以后端文档为准。
