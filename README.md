# 乡音小戏骨-终版

这是一个基于 Flask 的方言戏剧体验项目终版，保留现有功能，并额外合入了 `3.html` 中“故事铺原创剧本可直接进入大戏台并表演”的增强逻辑。

## 本地运行

1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

2. 配置环境变量

```bash
export XFYUN_APP_ID=你的APP_ID
export XFYUN_API_KEY=你的API_KEY
export XFYUN_API_SECRET=你的API_SECRET
```

3. 确保机器已安装 FFmpeg

```bash
ffmpeg -version
```

4. 启动项目

```bash
python app.py
```

## 部署到 Render

1. 把这个目录上传到 GitHub 仓库
2. 登录 Render 后点击 `New +` -> `Web Service`
3. 选择你的 GitHub 仓库
4. Render 会自动识别当前目录中的 `render.yaml` 和 `Dockerfile`
5. 在 Render 环境变量里填写：
   `XFYUN_APP_ID`
   `XFYUN_API_KEY`
   `XFYUN_API_SECRET`
6. 点击创建服务，等待构建完成

部署完成后，Render 会生成一个公网地址，例如：

`https://xiangyinxiaoxigu-final.onrender.com`

当前项目使用 Docker 部署，镜像内已经包含 `ffmpeg`，所以比直接 Python 构建更适合语音功能。

如果你手动创建服务，请确认：

- Environment: `Docker`
- Plan: `Free`
- Auto Deploy: `Yes`

## 健康检查

部署后可访问：

`/api/health`

返回 `hasXfyunCredentials: true` 说明讯飞密钥已正确注入。

首次访问免费实例可能会慢几秒，这是 Render 免费服务休眠唤醒的正常现象。
