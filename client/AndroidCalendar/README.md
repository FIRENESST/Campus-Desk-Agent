# 中传校园日程 · Android

Android 8.0 及以上可安装。应用使用校园助手签发的私有 HTTPS 日程订阅。首次同步时将点选的订阅写入本机的「中传校园日程」日历，并按服务端事件 ID 更新或删除相应条目。过程保留其他日历。用户可以手动刷新，后台任务也会约每 30 分钟请求同步，实际时间由 Android 调度。

## 安装与同步

1. 从手机能访问的校园助手 HTTPS 地址打开「校园应用 → 日历同步」。用手机扫码或直接点击「下载安卓版 APK」，按手机提示安装。
2. 在网页为这台设备创建个人订阅链接并复制。打开应用，粘贴链接，点击「连接并同步」并允许访问日历。
3. 在应用中点「打开系统日历」，在手机日历里勾选「中传校园日程」。若更换订阅或手机，在网页撤销旧链接后重新创建。应用里的「取消订阅」会移除本机的校园日历。

应用内「隐私说明」会介绍日历权限、剪贴板读取和撤销链接的方式。

## 构建与部署

使用 Windows PowerShell 7、JDK 21、Android SDK Platform 35 和 Build Tools 35.0.0。设置 `ANDROID_HOME` 与 `JAVA_HOME` 后，在本目录执行以下命令。若没有设置 `ANDROID_HOME`，脚本也可使用本目录 `.sdk` 下的工具。

```powershell
# 首次构建时创建发行签名
./build.ps1 -InitializeSigning

# 后续版本沿用原签名
./build.ps1
```

签名存储在 `.signing`，密码由 Windows DPAPI 保护。升级应用时保留原签名并在 `AndroidManifest.xml` 递增 `versionCode`。构建脚本验证 APK 签名后，将安装包更新到 `server/AndroidAppServer/releases/cuc-campus-calendar.apk`。

主管理器使用 `start user-website` 带起安卓下载服务 `android-app-download`。沿用已有的 HTTPS 网站地址和 Agent 的 FRP 隧道。安装包从 `/downloads/campus-calendar.apk` 下载。安装包更换后，新的下载请求会读取新文件。二维码编码的也是这个下载地址，个人订阅链接在登录后单独创建。
