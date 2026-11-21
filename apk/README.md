# 鹤辞安卓壳（APK-1）

> 桌面壳 `src/desktop.py` 的安卓对应物：壳只管「窗和连」，逻辑全在 webui 服务侧。
> 与《手机与桌面整合调研-20261107.md》路线图的对应：这是「一个入口」的落地，
> tailscale 内嵌是 APK-2（同一工程增量，不换包名）。

## 架构

```
APK（本工程）
  └─ WebView ──▶ webui（本地核心 127.0.0.1:8901）
                    ├─ 在家：局域网直连   http://192.168.x.x:8901（webui 自动放行本机网卡 IP）
                    └─ 外网：tailscale    https://<desktop-id>.<tailnet>.ts.net:8445
                                            （2026-11-21 已开：tailscale serve --https=8445 → 127.0.0.1:8901）
```

- 鉴权零新代码：首次 `GET /?token=…` 引导（webui 四道校验的设计流程）→ 服务端种
  `xiaoman_token` Cookie → WebView 持久化 Cookie，之后免管。地址/令牌变更自动重新引导。
- 零第三方依赖：WebView / Cookie / UI 全部框架 API，装好 SDK 一次构建通过。

## 构建

前置（本机 2026-11-21 实测：JDK / Android SDK / Gradle 均未装，需先装）：

1. **JDK 17**（AGP 8.5 要求）
2. **Android SDK**：platform 34 + build-tools 34（Android Studio 装最省事，或 cmdline-tools）
3. **Gradle 8.7+**（或直接用 Android Studio 打开本目录）

```bash
cd android
gradle wrapper --gradle-version 8.7     # 首次生成 wrapper
./gradlew assembleDebug                 # 产物 app/build/outputs/apk/debug/app-debug.apk
```

安装：`adb install app-debug.apk` 或把 apk 拷到手机点开（允许未知来源）。

## 首次配置

1. 打开 App → 连接设置
2. 服务地址：
   - 在家：`http://<电脑局域网IP>:8901`
   - 外出：`https://<desktop-id>.<tailnet>.ts.net:8445`
     （外网直连需手机装系统 Tailscale App 加入 tailnet——**APK-2 会去掉这一步**）
3. 访问令牌：电脑上读 `%LOCALAPPDATA%\xiaoman\webui-token`（桌面壳用的同一份）
4. 保存并连接 → 之后 App 菜单（右上角）可「重新配置」

## 诚实标注

- ✅ 已验证（2026-11-21）：tailscale serve 8445 → webui 8901 全链路通（tailnet HTTPS 返回
  401 = Host 放行生效、令牌门正常）；webui Host/Origin 校验**原生支持** `*.ts.net`
  与 100.64.0.0/10（webui.py:182-205），本次零代码改动
- ✅ **APK-1 已构建成功（2026-11-21，ZCode 本机工具链）**：`assembleDebug` 33 任务全过，
  aapt2 badging 验证包名 `ai.gongsheng.companion` / 标签 `鹤辞` / minSdk 24 / targetSdk 34。
  构建环境见 `<home>/tools/android-env.sh`（JDK17 Temurin + SDK 34 + Gradle 8.7，
  均在用户目录，未写系统 PATH）。gradle.properties 含 `android.overridePathCheck=true`
  （项目路径含中文的官方逃生口）。首次依赖下载走 dl.google.com 官方 + 腾讯 maven-public
  镜像（本机网络：dl.google.com/腾讯可达，mavenCentral/gradle.org/github 不可达）
- ⚠️ APK 未真机安装验证 —— 装到手机 + 两种地址连接测试待用户执行
- ⚠️ 手机→VM 方向的 tailnet 连通性未实测（VM→本机方向 8443 已通）

## APK-2 路线（内嵌 tailscale，去掉系统 Tailscale 依赖）

- [tailscale/libtailscale](https://github.com/tailscale/libtailscale)：官方 gomobile 绑定，
  BSD-3 许可（与 Apache-2.0 兼容），Android 官方 App 同源
- 形态：userspace 节点 + **应用内 127.0.0.1 反代**（WebView 加载本地代理地址，
  代理经 tailnet 拨号到 desktop-…:8445）——不装系统 Tailscale、不要整机 VPN 权限
- 接入 tailnet：首次贴 auth key（或交互登录跳浏览器）
- 代价：APK +15~25MB；首次配 gomobile/NDK 构建链是主要工作量
- iOS 无对应物：系统 Tailscale App + 浏览器 PWA（M0 通道已通，添加到主屏幕即可）
