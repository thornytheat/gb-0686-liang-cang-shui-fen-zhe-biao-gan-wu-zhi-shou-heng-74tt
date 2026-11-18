# 运行维护

主服务端口与Compose保持一致。数据保存在records命名卷，应用代码不需要写入权限。

```sh
docker compose logs --tail=80 app
docker compose restart app
docker compose exec -T app pytest -q
docker compose down --remove-orphans
```

健康检查GET /health。失败输入返回原因码，结构性错误422，过大文件413，陈旧复核409。
文件内容最大4MB。回执先落台账，事实切换与批次记录在同一事务提交，失败不留半张事实；未完成车次在下次运行时自动续跑。证据CSV按原始行号定位，字段来源用 `/api/evidence-fields.csv` 导出。
