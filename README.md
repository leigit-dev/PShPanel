# PSh Panel

> 一个基于 Flask + WebSocket 的跨平台服务器控制面板，支持 PTY / PIPE 双模式任务管理、内置文件管理器、实时性能监控与多标签终端。

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)



## 简介

**PSh Panel** 是一个轻量级、可自托管的服务器控制面板，通过PTY技术在浏览器中实现与直接操作终端相似的体验，并以此为依托进行服务器的管理与监视。



## 功能

- 任务管理

- 终端

- 文件管理

- 性能监控



## 安装

### 环境要求

- Python 3.8 或更高
- Windows 或 Linux（macOS 未测试）

### 依赖

```bash
pip install -r requirements.txt
```

> **注意**：依赖项 `pywinpty` 仅在 Windows 上安装。Linux 使用标准库的 `pty` 模块，无需额外依赖。



## 使用

### 启动

```bash
python psh_panel.py
```


浏览器打开 `http://<服务器IP>:5000`，首次访问会提示设置密码。


### 新建任务

1. 点击任务列表右上角 **+ 新建**
2. 填写：
   - **任务名称**：任意标识
   - **启动命令**：如 `python -u main.py`、`./run.sh`、`node server.js`
   - **工作目录**：点击「浏览…」选择，或手输路径
   - **模式**：
     - `PIPE`：只读，推荐跑 Web 服务器、后台进程
     - `PTY`：可交互，推荐跑 Shell、REPL
   - **开机自启**：勾选后，面板启动时会自动拉起该任务
3. 保存后即可在详情页启动

### 终端交互

- 任务详情页的预览区域：**点击即进入全屏终端**
- 全屏终端内：
  - PTY 任务可直接敲键盘
  - 顶部的「新建终端」按钮可开独立 Shell 会话（与任务无关）
  - 多个标签之间可自由切换，每个终端保持独立状态



## 配置

面板设置页面可调整：

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| 面板标题 | 显示在顶栏与登录页 | `PSh Panel` |
| 终端保留行数 | 每个任务的环形缓冲大小 | `5000` |
| 终端字号 | xterm.js 字号 | `14` |
| 历史保留次数 | 每个任务保留多少次运行历史 | `10` |
| 性能刷新间隔 | WebSocket 推送间隔（毫秒） | `2000` |

数据存储于 `psh_data.json`.



## 跨平台说明

| 功能 | Windows | Linux |
| :--- | :--- | :--- |
| PTY 后端 | `pywinpty` | 标准库 `pty` |
| PTY 命令执行 | `PtyProcess.spawn` | `pty.fork` + `/bin/sh -c` |
| PIPE 启动 | `subprocess.Popen(shell=True)` | 同上 + `os.setsid` |
| 进程树终止 | `taskkill /F /T /PID` | `os.killpg(SIGTERM → SIGKILL)` |
| 退出码获取 | `proc.exitstatus` | `os.waitpid` + `waitstatus_to_exitcode` |
| 文件根目录 | `psutil.disk_partitions` → 盘符 | `~` + `/` + 挂载点 |



## 安全建议

1. **不要直接暴露到公网**。默认监听 `0.0.0.0`，如需公网访问，请在前面加一层反向代理（Nginx / Caddy）并启用 HTTPS。
2. **使用强密码**。虽然密码经过 PBKDF2 加盐哈希，但弱密码仍可能被暴力破解。
3. **限制访问 IP**。如果只在局域网使用，建议通过防火墙只允许可信网段访问。
4. **定期备份 `psh_data.json`**，其中包含任务配置与运行历史。
5. **谨慎使用文件管理器的删除功能**。虽然内置了关键目录保护，但删除操作不可逆。



## 已知限制

- **终端页多标签**：一个 SocketIO 连接对应多个终端会话（通过 `term_id` 区分），但如果浏览器标签页崩溃，PTY 会话可能残留，需要手动清理。
- **按进程网络吞吐**：当前仅提供全局网络速率。按任务采集网络吞吐需要平台特定 API（Windows 需要 ETW 或 IP Helper API），暂未实现。
- **历史日志长度**：每次运行最多保留最后 500 个输出块。
- **macOS**：未做测试，PTY 分支理论上可用（走 `pty.fork`），但未验证。

  这些限制会在后续更新中进一步解决



## 许可证

本项目采用 **MIT License** 发布。

[LICENSE](LICENSE)



## 第三方许可

本项目在运行时依赖以下第三方组件，各自遵循其原有许可证：

| 组件 | 许可证 | 项目地址 |
| :--- | :--- | :--- |
| Flask | BSD-3-Clause | https://github.com/pallets/flask |
| Flask-SocketIO | MIT | https://github.com/miguelgrinberg/Flask-SocketIO |
| python-socketio | MIT | https://github.com/miguelgrinberg/python-socketio |
| python-engineio | MIT | https://github.com/miguelgrinberg/python-engineio |
| simple-websocket | MIT | https://github.com/miguelgrinberg/simple-websocket |
| pywinpty | MIT | https://github.com/andfoy/pywinpty |
| psutil | BSD-3-Clause | https://github.com/giampaolo/psutil |
| xterm.js | MIT | https://github.com/xtermjs/xterm.js |
| Socket.IO Client | MIT | https://github.com/socketio/socket.io-client |

前端通过 CDN 加载的 xterm.js、Socket.IO Client 等资源，其版权归各自项目与作者所有。




## 贡献

欢迎提交 Issue 和 Pull Request。




**PSh Panel** —— 一个为个人与小团队设计的轻量级服务器管理面板。
