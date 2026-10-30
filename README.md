# 扫描直连 · 一键注册

把理光（RICOH）打印机「**扫描到 FTP 文件夹**」的多步手工配置，压缩成 **填一个名字 → 点确认**。

原本要装 FTP 服务、查本机 IP、登录打印机后台、在通讯簿里一页页填协议/端口/服务器/路径/账号……
现在这些都自动完成，扫描件直接落到本机文件夹。

![界面](docs/界面.png)

---

## 下载

| 版本 | 文件 | 说明 |
|---|---|---|
| **单文件版** | [ScanLink-v1.1.0-portable.exe](https://github.com/shidaifufen/RICOH_FTP_scan/releases/download/v1.1.0/ScanLink-v1.1.0-portable.exe) | 一个 exe（14MB），拷走即用；首次启动解包 2~4 秒 |
| **文件夹版** | [ScanLink-v1.1.0-folder.zip](https://github.com/shidaifufen/RICOH_FTP_scan/releases/download/v1.1.0/ScanLink-v1.1.0-folder.zip) | 解压后双击 `启动.bat`，启动更快（exe 4.4MB + `_internal`） |

全部版本 / 更新日志：<https://github.com/shidaifufen/RICOH_FTP_scan/releases>

- 文件夹版**必须整个文件夹一起用**，只拷 exe 会报 `Failed to load Python DLL`。
- 未签名程序，Windows 可能弹「已保护你的电脑」→ 点「更多信息 → 仍要运行」，或直接双击 `启动.bat`。

---

## 快速开始

1. 解压 / 拷贝到任意目录
2. 双击 `启动.bat`（或 exe）
3. 填三样东西，点「确认并注册」：

   | 填什么 | 说明 |
   |---|---|
   | 打印机地址 | 打印机后台网址里的 IP，如 `192.168.1.100` |
   | 管理员密码 | 理光 Web Image Monitor 的 admin 密码（IT 设置的那个） |
   | 目的地名字 | 会显示在打印机面板上，如 `张三`、`财务` |

4. 到打印机面板：**扫描 → 选择这个名字 → 开始扫描**，文件落到 `桌面\扫描`

保存目录默认 `桌面\扫描`（自动识别 OneDrive 重定向后的真实桌面），可随时改。

---

## 它做什么

| 手工要做的 | 程序自动做的 |
|---|---|
| 安装 Slyar FTPServer、设目录/账号/权限、启动 | 内置 FTP 服务，点确认时自动启动 |
| `ipconfig` 查本机 IP | 自动选**打印机可达**的那块网卡（多网卡/虚拟网卡不会选错） |
| 手工放通防火墙 | 自动放通控制端口 + 被动端口段（需管理员权限） |
| 打开打印机后台、输入管理员账号密码 | 自动打开并登录 |
| 设备管理 → 通讯簿 → 添加用户 → 勾选目的地 | 自动点（按界面中文标签定位，不猜 CGI 参数） |
| 填注册编号/名称、协议/端口/服务器/路径/用户名 | 名称=你填的，编号自动取现有最大号+1，其余自动填 |
| 点「确定」 | 自动点，并**回地址列表校验**确实写进去了 |

**其他能力**

- **同名 / 同 IP 视为同一用户**：先建新条目、校验成功后再删旧条目，不会越积越多
- **系统托盘常驻**：悬停看状态；双击开窗口；右键可启停 FTP / 复制地址 / 检查状态 / 打开目录 / 退出
- 一次会话只开一个浏览器进程，注册完自动注销打印机后台会话
- 端口 21 被占用自动改用 2121/2122…；保存目录不可写自动降级（桌面 → 文档 → 程序目录）
- 每次启动生成 `logs\env-report.txt` 环境自检，排障先看它

---

## 环境要求

- Windows 10 / 11 + Microsoft Edge（或 Chrome）
- 打印机与电脑网络互通，**且打印机能反向连接到本机**（扫描是把文件传到本机）
- 适配 **RICOH Web Image Monitor** 系列（实测 MP C4504）；其它品牌后台结构不同，需另写自动化模块
- 目的地类型只做 **FTP 文件夹**（SMB 需另验证）

---

## 配置

首次运行会在程序目录生成 `config.json`（也可复制 `config.example.json` 改名）：

| 字段 | 说明 |
|---|---|
| `save_dir` | 扫描件保存目录，默认 `桌面\扫描` |
| `port` | FTP 控制端口，默认 21 |
| `anonymous` / `ftp_user` / `ftp_password` | 匿名或指定账号访问 |
| `printer_host` | 打印机 IP |
| `admin_user` / `admin_password` | 打印机后台管理员账号密码（明文存在本机） |
| `merge_by_ip` | 同名/同 IP 只保留一条，默认 `true` |
| `ftp_path` | 打印机里填的路径，默认 `/` |

**命令行模式**（不开界面，跑一次注册，适合现场验证 / 自动化）：

```bat
扫描直连.exe --cli-register 张三 --printer 192.168.1.100
```

结果写入 `logs\cli.log`，退出码 `0` = 成功；`--password xxx` 可临时指定管理员密码。

---

## 从源码运行 / 打包

```bat
:: 1) 装依赖（Python 3.9+，装进 vendor\，不污染系统环境）
setup.bat

:: 2) 直接运行
启动.bat

:: 3) 重新打包（onedir，产物在 build\dist\扫描直连\）
set PYTHONPATH=%~dp0tools
%PY% -m PyInstaller --noconfirm --clean --onedir --noconsole --name 扫描直连 ^
  --distpath "%~dp0build\dist" --workpath "%~dp0build\work" --specpath "%~dp0build" ^
  --add-data "%~dp0vendor;vendor" --add-data "%~dp0app\scanlink.ico;app" --icon "%~dp0app\scanlink.ico" ^
  --hidden-import pyftpdlib --hidden-import pyftpdlib.servers --hidden-import pyftpdlib.handlers ^
  --hidden-import pyftpdlib.authorizers --hidden-import pyasynchat --hidden-import pyasyncore ^
  "%~dp0main.py"
```

`tools\` 是项目内置的 PyInstaller（8MB，删掉不影响运行，只影响重新打包）。

**测试**（不需要打印机，`tests\mock_wim.py` 是复刻的假理光后台）：

```bat
%PY% tests\test_ftp.py  tests\test_cdp.py  tests\test_registration.py  tests\test_gui.py  tests\test_tray.py
%PY% tests\mock_wim.py   :: 想用浏览器手工点一遍假后台时用
```

---

## 常见问题

**打印机连不上（测试连接超时）**
本机与打印机要能互通（VLAN/交换机可能拦）；打印机 IP 会变的话请 IT 配固定 IP。程序只支持打印机后台的 HTTP 80。

**能登录但注册失败**
看 `logs\debug\` 里最新的 `.png` + `.html`（失败瞬间的界面和 DOM）。最常见是管理员密码错、固件界面差异。

**面板看不到新名字**
去面板「通讯簿 → 用户列表」按注册编号找；有的机器要刷新或重启才显示。

**扫描报"无法连接文件夹"/上传超时**
典型是 FTP **被动端口**被拦（控制端口通了、数据通道没通）。用「以管理员身份重启」跑一次让防火墙规则落地；并确认网络类型是「专用网络」。

**提示「进入服务器的用户数超过最大允许限制」**
理光后台并发会话有限，等 1~2 分钟自动释放或重启打印机。程序注册完/退出时会自动注销，正常使用不会占满。

**托盘图标不出现 / 保存目录提示"拒绝访问"**
先看 `logs\env-report.txt`：若"管理员: True"但三个写入测试全是"拒绝"，说明程序**不是在正常桌面会话里启动的**（例如通过自动化/沙箱终端）。在资源管理器里双击 `启动.bat` 即可。

**会不会多出重复条目？**
不会。同名或同一台电脑（同 FTP 服务器 IP）再注册 = 更新：先建新条目、成功后删旧的（编号会换新，理光向导不支持改编号）。
注意：正因如此，**同一台电脑只保留一条**目的地；两台电脑别注册同一个名字。

**开发提示：别反复新开浏览器**
Edge/Chrome 每启动一个新进程都会产生一次 Windows 失败登录事件；默认「10 次/10 分钟」锁定策略下，反复新开会把本机账户锁死（PIN 报 `0xC0000234`）。代码已做"一次会话复用一个浏览器"，跑测试时也请复用或先 `net accounts /lockoutthreshold:0`。

---

## 实现要点（改代码前看）

```
界面(tkinter) ─ ① FtpScanServer  pyftpdlib，被动端口 30000-30099
                        masquerade_address = 打印机可达的本机 IP
              ② netinfo       向打印机 IP 发包，看内核从哪块网卡出去
              ③ firewall      netsh 放通控制端口 + 被动端口段
              ④ RicohRegistrar  CDP 驱动 Edge 点完通讯簿向导并校验
```

1. **FTP 必须设 `masquerade_address` 和 `passive_ports`**：否则 PASV 回 `127.0.0.1` 或错网卡，表现为"能登录但传不了"。
2. **打印机自动化走真实界面**：用 CDP（Chrome DevTools Protocol）驱动浏览器，按界面中文标签点击（「添加用户」「转到下一项」「文件夹目的地」「确定」）。理光的 CGI 隐藏字段/令牌随固件变，界面文字多年稳定。
3. **cdp.py 的两个坑**：事件只在等命令响应时才被读出（每步后要 `pump()`）；frameset 子 frame 导航后上下文不一定重建（用 `Page.getFrameTree` + `Page.createIsolatedWorld` 兜底）。
4. **打包版需要给 Edge 加 `--no-sandbox`**：PyInstaller 冻结环境里渲染进程会起不来（`Page.enable` 永远超时）。该实例只访问局域网打印机后台。
5. **"同一个用户"用先建后删**：理光「更改」向导是页内模态 + AJAX，自动化点不进去；删除可靠（勾行 → 删除 → 自动确认），所以更新 = 先建新的 → 校验 → 删旧的。
6. **失败必留证据**：任何一步失败都把截图 + 各 frame HTML 存到 `logs\debug\`。

---

## 目录结构

```
扫描直连/
├─ main.py                入口（源码 / PyInstaller 都用它）
├─ 启动.bat  setup.bat     启动 / 装依赖
├─ config.example.json    配置模板（真实配置 config.json 不入库）
├─ app/
│  ├─ main.py             界面 + 一键注册流程编排
│  ├─ ftp_server.py       内置 FTP 服务
│  ├─ netinfo.py          本机 IP 探测
│  ├─ firewall.py         Windows 防火墙放通
│  ├─ cdp.py              Chrome DevTools Protocol 客户端
│  ├─ printer_ricoh.py    理光通讯簿自动注册
│  ├─ tray.py             系统托盘图标
│  └─ config.py           配置持久化
├─ tests/                 自动化测试 + 假理光后台(mock_wim) + 真机工具
├─ vendor/                运行期依赖（pyftpdlib 等，随目录拷走即可用）
└─ docs/界面.png
```

运行后还会生成：`logs\app.log`（界面日志）、`logs\env-report.txt`（环境自检）、`logs\cli.log`、`logs\debug\`（失败快照）。

---

## 隐私说明

仓库中的 IP（`192.168.1.100` / `192.168.1.10`）、主机名、用户名、路径**全部是占位示例**，不含真实内网信息；`config.json`、`logs/`、打包产物均已在 `.gitignore` 中排除。
