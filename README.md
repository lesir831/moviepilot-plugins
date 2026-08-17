# MoviePilot Plugins

个人 MoviePilot V2 插件仓库。后续开发的 MoviePilot 插件将统一维护在本仓库。

## 安装仓库

在 MoviePilot 的插件市场添加以下第三方仓库地址：

```text
https://github.com/lesir831/moviepilot-plugins
```

刷新插件市场后即可浏览并安装本仓库提供的插件。

## 插件列表

### 蜂巢新版签到

插件 ID：`FengchaoNextSignin`

适配 `pting.club` 当前 Next.js 登录系统，实现每日自动签到。

旧版插件依赖 Flarum 的 `csrfToken`、`flarum_session` 和
`/api/users/{id}`，站点升级后已无法登录。本插件使用当前网页实际调用的
API：

- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/check-in`
- 会话 Cookie：`bbs_session`

#### 功能

- 用户名、邮箱或手机号加密码登录
- 签到前检查今日状态，避免重复请求
- 签到后再次查询状态，防止接口假成功
- 定时签到、立即运行、失败重试和 MoviePilot 通知
- 保存用户摘要及签到历史
- 支持 MoviePilot 系统代理
- 检测站点登录验证码模式；启用交互式验证码时明确停止并报告原因
- 使用仓库内维护的原蜂巢签到插件图标

安装“蜂巢新版签到”前，请停用旧版“蜂巢签到”，避免两个插件同时执行。

#### 配置

1. 填写蜂巢用户名、邮箱或手机号，以及密码。
2. 设置五段 cron 表达式，默认 `30 9 * * *`。
3. 如果 MoviePilot 访问蜂巢需要代理，开启“使用系统代理”。
4. 可先开启“立即运行一次”完成验证。

插件不会在日志、历史记录或通知中输出密码和会话 Cookie。

#### 已知限制

截至 2026-08-17，蜂巢登录页的 `loginCaptchaMode` 为 `OFF`，可以直接使用
密码 API 登录。如果站点以后启用 Turnstile、内置图形验证码或 PoW，插件会
停止自动重试并提示需要适配对应验证，不会尝试绕过交互式安全验证。

## 仓库结构

```text
package.json                         # MoviePilot 插件索引
icons/                               # 插件图标资源
plugins/
  fengchaonextsignin/                # 蜂巢新版签到
    __init__.py
    client.py
tests/                               # 自动化测试
```

新增插件时，需要同时添加 `plugins/<插件目录>/` 实现，并在根目录
`package.json` 注册插件元数据。

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

## 许可证

MIT
