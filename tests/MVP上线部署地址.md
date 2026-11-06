# MVP 系统上线部署网站地址

> 交付项：Test-3 · 提供 MVP 系统上线部署网站地址（文档）
> 生成时间：2026-11-06 · 部署方式：WorkBuddy 应用发布（单端口 HTTP 服务）

## 一、访问地址

| 项目 | 地址 |
|---|---|
| **线上访问入口** | https://product-audit-assistant.app.workbuddy.host/?sharecode=MYYH2Y2KY5 |
| 健康检查接口 | https://product-audit-assistant.app.workbuddy.host/api/health （需携带 sharecode 参数） |
| 本地备用入口 | http://127.0.0.1:8000（`python run.py`） |

## 二、部署信息

| 项 | 值 |
|---|---|
| 应用名称 | 商品审核AI助手MVP |
| 运行方式 | Python HTTP 服务（FastAPI + uvicorn，监听 `$PORT`，绑定 `0.0.0.0`） |
| 启动命令 | `python run.py` |
| 依赖安装 | `pip install -r requirements.txt` |
| 首次启动初始化 | 自动建表（16 张）→ 种子账号 → 导入《审核制度.md》→ 构建检索索引 |
| 部署验证 | `/api/health` 返回 `status=ok`、`degraded=false`、知识快照 `stale=false`（2026-11-06 实测） |

## 三、演示账号

| 账号 | 角色 | 初始密码 | 用途 |
|---|---|---|---|
| submitter | 提交方 | 123456 | 商品提交、问答咨询 |
| reviewer | 审核员 | 123456 | 工单处理 |
| admin | 管理员 | 123456 | 后台管理、工单闭环、知识库 |

> 安全提示：初始密码仅用于演示评审，正式启用前必须修改，并替换 `SECRET_KEY` 环境变量。

## 四、上线状态核对（部署当日实测）

- [x] 登录页 HTTP 200
- [x] `/api/health` 返回 ok，SSE 水位正常（active=0, limit=200）
- [x] 知识库自动构建完成（chunks=4，stale=false）
- [x] 三账号可登录，RBAC 生效
- [x] 冲突问句（220V/110V）可触发转人工，工单可闭环

## 五、注意事项

1. 本链接为评审演示环境，数据为合成示例，请勿录入真实商品与个人信息。
2. 服务数据存储在部署沙箱内，重新部署会重置为初始状态；正式交付以客户环境部署为准（步骤见《09_项目上线交付运行手册.md》）。
3. 若链接失效，可在本地以 `python run.py` 启动同等环境演示。
