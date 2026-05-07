"""用户服务 - 包含 SQL 注入漏洞（测试样例）。"""

import sqlite3


def get_db():
    return sqlite3.connect("app.db")


def get_user_by_name(username: str):
    conn = get_db()
    cursor = conn.cursor()
    # 漏洞1：字符串拼接导致 SQL 注入
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


def get_user_orders(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    # 漏洞2：+ 拼接未做类型校验
    sql = "SELECT * FROM orders WHERE user_id = " + user_id
    cursor.execute(sql)
    return cursor.fetchall()


def search_users(keyword: str):
    conn = get_db()
    cursor = conn.cursor()
    # 漏洞3：LIKE 子句未参数化
    cursor.execute(f"SELECT id, username FROM users WHERE username LIKE '%{keyword}%'")
    return cursor.fetchall()


def delete_user(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    # 漏洞4：DELETE 语句拼接，危险性极高
    cursor.execute("DELETE FROM users WHERE id = " + user_id)
    conn.commit()
