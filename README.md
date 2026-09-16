# 安徽理工大学双校区校园网自动登录工具


在此特别感谢   https://github.com/TraceRecursion/AUST-ConnectEase
以及   https://github.com/hanasite/AUST
提供的淮南校区的数据，不然我一个合肥校区的，确实无法完成淮南校区的部分，
站在巨人肩膀了也是，真是lucky.


支持合肥校区和淮南校区的 Dr.COM 校园网认证，提供单次登录、持续断线重连和 Windows 登录后后台自启。

mj## 支持范围

| 校区 | 认证接口 | 已知出口 |
| --- | --- | --- |
| 合肥 | `http://172.24.34.2:801/eportal/portal/login` | 校内、电信 `@aust`、移动 `@hfcmcc` |
| 淮南 | `http://10.255.0.19/drcom/login` | 教职工 `@jzg`、电信 `@aust`、联通 `@unicom`、移动 `@cmcc` |

合肥流程已经在真实网络上验证。淮南流程依据 TraceRecursion/AUST-ConnectEase 的公开说明实现：Wi-Fi 使用 GET，有线使用 POST。淮南仍建议先执行手动测试再安装自启。

## 文件

```text
aust_autologin.py       主程序
安装开机启动.ps1        安装或更新后台计划任务
卸载开机启动.ps1        删除任务和本机加密凭据
README.md               使用说明
```

运行后产生的 `aust_autologin.log` 可能包含学号、IP、MAC，不要公开分享。

## 准备isa

安装 Python 3，并确认：

```powershell
python --version
```

连接校园 Wi-Fi，然后在本工具目录打开 PowerShell。

## 首次手动测试bone

合肥移动用户示例：

```powershell
$env:AUST_USERNAME = "你的学号"
$env:AUST_PASSWORD = "你的校园网密码"
$env:AUST_CAMPUS = "hefei"
$env:AUST_EXIT_SUFFIX = "@hfcmcc"
python .\aust_autologin.py --selftest
python .\aust_autologin.py --once
```

淮南移动 Wi-Fi 用户示例：

```powershell
$env:AUST_USERNAME = "你的学号"
$env:AUST_PASSWORD = "你的校园网密码"
$env:AUST_CAMPUS = "huainan"
$env:AUST_CONNECTION = "wifi"
$env:AUST_EXIT_SUFFIX = "@cmcc"
python .\aust_autologin.py --selftest
python .\aust_autologin.py --once
```

出口后缀：

| 使用场景 | 合肥 | 淮南 |
| --- | --- | --- |
| 校内/无后缀 | `""` | `""` |
| 电信 | `@aust` | `@aust` |
| 移动 | `@hfcmcc` | `@cmcc` |
| 联通 | 未确认 | `@unicom` |
| 教职工 | 未确认 | `@jzg` |

账号也可以自带后缀，但仍推荐单独设置 `AUST_EXIT_SUFFIX`。脚本不会将密码写入 Python 文件。

自动识别校区可设置 `$env:AUST_CAMPUS = "auto"`。自动模式只读取两个校区的认证首页，不会提交凭据。若两个地址都无法访问，会默认合肥，因此首次测试建议明确指定校区。

## 持续守护

```powershell
python .\aust_autologin.py --daemon --interval 60
```

按 `Ctrl+C` 停止。最小间隔 5 秒，建议 60～120 秒；登录失败时固定等待 30 秒重试。

## 安装后台自启

合肥移动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\安装开机启动.ps1 `
    -Username "你的学号" -Campus hefei -Network mobile -Interval 60
```

淮南移动 Wi-Fi：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\安装开机启动.ps1 `
    -Username "你的学号" -Campus huainan -Connection wifi -Network mobile -Interval 60
```

淮南有线把 `-Connection wifi` 改为 `-Connection wired`。

`-Network` 可选：`campus`、`telecom`、`mobile`、`unicom`、`staff`。其中 `unicom` 和 `staff` 仅有淮南校区公开资料支持。

运行后按提示输入密码，输入过程不会显示字符。密码以 Windows DPAPI 加密保存，只有当前 Windows 用户可解密。同名任务会被覆盖，所以修改账号、密码、校区、出口或间隔时直接重新运行安装命令。

```powershell
Start-ScheduledTask -TaskName "AUST Campus Network Auto Login"
Get-ScheduledTask -TaskName "AUST Campus Network Auto Login" | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName "AUST Campus Network Auto Login" | Select-Object LastRunTime,LastTaskResult
Stop-ScheduledTask -TaskName "AUST Campus Network Auto Login"
```

卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\卸载开机启动.ps1
```

## 常见问题

- 浏览器能登录但脚本失败：确认校区、出口和连接类型正确；淮南移动是 `@cmcc`，合肥移动是 `@hfcmcc`。
- 淮南显示 AC 验证失败：不要使用合肥的 `BRAS`/ePortal 参数；本版本会改走淮南 `/drcom/login`。
- 自检无法访问认证服务器：确认当前确实连接相应校区校园网，并明确设置 `AUST_CAMPUS`。
- 查看日志：`Get-Content .\aust_autologin.log -Tail 30`。
- 安装脚本乱码或 ParserError：使用压缩包内原始脚本，不要用会改变编码的编辑器另存。

## 高级覆盖

学校更换参数时可用环境变量覆盖：`AUST_PORTAL_IP`、`AUST_PORTAL_PORT`、`AUST_EPORTAL_PORT`、`AUST_WLAN_AC_IP`、`AUST_WLAN_AC_NAME`、`AUST_CHECK_INTERVAL`。

## 安全提醒

- 不要分享 `credential.xml`、日志或包含真实账号密码的截图；
- 每个人应在自己的电脑上输入密码并安装任务；
- 共享 ZIP 中不应包含运行后生成的日志和凭据；
- 本工具只用于本人合法校园网账号的正常认证。

## 淮南数据来源kfc

淮南接口和出口参数参考公开项目：<https://github.com/TraceRecursion/AUST-ConnectEase>。该项目说明的请求字段为 `callback=dr1003`、`DDDDD=账号加后缀`、`upass=密码`、`0MKKey=123456`。
