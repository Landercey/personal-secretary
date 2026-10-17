**装不上 NapCat 的两种真实原因，现在分开说、也有离线办法了。**

三个地址全报 `CERTIFICATE_VERIFY_FAILED` 时，不是"被墙"，是这台电脑不信任对方的证书
（校园网没登录认证、公司代理、安全软件拦截 HTTPS 时最常见）。

- 日志会直接写明是证书问题，并给出要放的文件夹路径；
- 支持离线包：把 `NapCat.Shell.Windows.OneKey.zip` 放进 `napcat\`，程序就不联网了；
- 或者把别人装好的整个 `napcat` 文件夹拷过来，解压即用。

### 怎么升级

点「检查更新 → 一键更新」，或者下 `personal-secretary-v4.0.8-upgrade.exe` 双击。
