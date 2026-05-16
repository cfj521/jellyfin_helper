"""
一次性运维脚本：把 PG 里的 jellyfin_tools 数据库 + 用户重命名为 jellyfin_helper。

为啥要这步：
  - 项目改名 jellyfin-tools → jellyfin-helper
  - PG 端的 database 名 + role 名也想保持一致
  - ALTER DATABASE/USER RENAME 需要 superuser，普通用户改不了自己 → 必须用 postgres 超管

执行：
  python scripts/rename_pg_to_helper.py
       [--host 127.0.0.1] [--port 5432]
       [--from-db jellyfin_tools] [--to-db jellyfin_helper]
       [--from-user jellyfin_tools] [--to-user jellyfin_helper]
       [--force-disconnect]   # 强制 kill 掉旧 db 上的活动连接，再 RENAME
       [--dry-run]            # 只打印将执行的 SQL，不真跑

执行后请记得手动更新 config.yaml 的 database.name / database.user 两个字段。
脚本本身不动 config.yaml —— 因为它跑在 PG 服务器侧上下文，跟项目文件解耦更安全。
"""
import argparse
import getpass
import sys

import psycopg2
from psycopg2 import sql


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', default=5432, type=int)
    ap.add_argument('--super-user', default='postgres', help='超管账号（默认 postgres）')
    ap.add_argument('--from-db',   default='jellyfin_tools')
    ap.add_argument('--to-db',     default='jellyfin_helper')
    ap.add_argument('--from-user', default='jellyfin_tools')
    ap.add_argument('--to-user',   default='jellyfin_helper')
    ap.add_argument('--force-disconnect', action='store_true',
                    help='强制 kill 旧 db 上的活动连接（先 pg_terminate_backend 再 RENAME）')
    ap.add_argument('--dry-run', action='store_true', help='只打印 SQL，不真执行')
    args = ap.parse_args()

    print(f"目标：")
    print(f"  database  {args.from_db!r} → {args.to_db!r}")
    print(f"  user      {args.from_user!r} → {args.to_user!r}")
    print(f"  host      {args.host}:{args.port}")
    print(f"  super     {args.super_user}")
    print(f"  dry-run   {args.dry_run}")
    print()

    super_pwd = getpass.getpass(f"请输入 {args.super_user} 超管密码（隐藏输入）：")
    if not super_pwd:
        print("超管密码为空，退出"); sys.exit(1)

    # 连默认 postgres db（不连目标 db，否则 RENAME 时自己卡住）
    try:
        con = psycopg2.connect(
            host=args.host, port=args.port, dbname='postgres',
            user=args.super_user, password=super_pwd, connect_timeout=5,
        )
    except Exception as e:
        print(f"❌ 连不上 PG：{e}"); sys.exit(1)
    con.autocommit = True   # ALTER DATABASE / USER 不能在事务里跑
    cur = con.cursor()

    # ---- 前置 sanity check ----
    print("\n=== sanity check ===")

    # 源 db / user 必须存在
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.from_db,))
    if not cur.fetchone():
        print(f"❌ 源数据库 {args.from_db!r} 不存在（也许已经 RENAME 过了？）"); sys.exit(1)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (args.from_user,))
    if not cur.fetchone():
        print(f"❌ 源用户 {args.from_user!r} 不存在"); sys.exit(1)
    print(f"  ✓ 源 db / user 存在")

    # 目标 db / user 必须不存在（防覆盖）
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.to_db,))
    if cur.fetchone():
        print(f"❌ 目标数据库 {args.to_db!r} 已存在，请先清理"); sys.exit(1)
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (args.to_user,))
    if cur.fetchone():
        print(f"❌ 目标用户 {args.to_user!r} 已存在，请先清理"); sys.exit(1)
    print(f"  ✓ 目标 db / user 都不存在（可建）")

    # 活动连接检查（RENAME DB 要求 0 连接）
    cur.execute(
        "SELECT pid, usename, application_name, client_addr FROM pg_stat_activity "
        "WHERE datname = %s",
        (args.from_db,)
    )
    conns = cur.fetchall()
    if conns:
        print(f"⚠️  {args.from_db!r} 上有 {len(conns)} 个活动连接：")
        for r in conns:
            print(f"     pid={r[0]} user={r[1]} app={r[2]} client={r[3]}")
        if not args.force_disconnect:
            print(f"  → 用 --force-disconnect 强制断开，或先停掉这些连接再跑")
            sys.exit(1)
        print(f"  → --force-disconnect 已传，准备 kill 这些连接")
    else:
        print(f"  ✓ {args.from_db!r} 当前无活动连接")

    # ---- 准备要执行的 SQL ----
    statements = []
    if conns and args.force_disconnect:
        statements.append((
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (args.from_db,),
        ))
    # ALTER DATABASE / USER RENAME 不能 parameterize 标识符；用 psycopg2.sql.Identifier 安全拼接
    statements.append((
        sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
            sql.Identifier(args.from_db), sql.Identifier(args.to_db)
        ),
        None,
    ))
    statements.append((
        sql.SQL("ALTER USER {} RENAME TO {}").format(
            sql.Identifier(args.from_user), sql.Identifier(args.to_user)
        ),
        None,
    ))

    print("\n=== 将执行的 SQL ===")
    for stmt, params in statements:
        rendered = stmt.as_string(con) if hasattr(stmt, 'as_string') else stmt
        print(f"  {rendered}  -- params={params}")

    if args.dry_run:
        print("\n[dry-run] 不执行，退出"); con.close(); return

    confirm = input(f"\n确认执行？(y/N): ").strip().lower()
    if confirm != 'y':
        print("已取消"); con.close(); return

    # ---- 执行 ----
    print("\n=== 执行中 ===")
    for stmt, params in statements:
        cur.execute(stmt, params)
        rendered = stmt.as_string(con) if hasattr(stmt, 'as_string') else stmt
        print(f"  ✓ {rendered}")

    con.close()

    # ---- 验证：用新 user 名 + 旧密码（PG ALTER USER RENAME 不改密码）连新 db ----
    print("\n=== 验证 ===")
    new_pwd = getpass.getpass(f"输入 {args.to_user!r}（rename 前的 {args.from_user!r}）的密码做连接验证："
                              "（直接回车 = 跳过验证）：")
    if not new_pwd:
        print("跳过验证")
    else:
        try:
            test_con = psycopg2.connect(
                host=args.host, port=args.port, dbname=args.to_db,
                user=args.to_user, password=new_pwd, connect_timeout=5,
            )
            test_cur = test_con.cursor()
            test_cur.execute("SELECT current_user, current_database()")
            row = test_cur.fetchone()
            print(f"  ✓ 连接验证 OK: user={row[0]}  db={row[1]}")
            test_con.close()
        except Exception as e:
            print(f"  ❌ 连接失败：{e}")

    print(f"\n完成。记得更新 config.yaml 的 database.name / database.user 为 {args.to_db!r} / {args.to_user!r}")


if __name__ == '__main__':
    main()
