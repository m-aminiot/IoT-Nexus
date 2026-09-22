import sqlite3
import bcrypt
from pathlib import Path

data_base_name = str(Path(__file__).resolve().parent / "login_signup.db")

def create_table():
    with sqlite3.connect(data_base_name) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS project (user_name TEXT NOT NULL UNIQUE, password BLOB NOT NULL)")

def _validate(username, password):
    if not isinstance(username, str) or not isinstance(password, str): raise ValueError("Invalid input")
    username = username.strip()
    if not 3 <= len(username) <= 64 or not 1 <= len(password) <= 256: raise ValueError("Invalid username or password")
    return username

def signup(user_name, password):
    user_name = _validate(user_name, password)
    with sqlite3.connect(data_base_name) as conn:
        if conn.execute("SELECT 1 FROM project WHERE user_name=?", (user_name,)).fetchone(): return "Username already exists"
        conn.execute("INSERT INTO project VALUES (?,?)", (user_name, bcrypt.hashpw(password.encode(), bcrypt.gensalt())))
    return "account created successfully"

def signin(user_name, password):
    user_name = _validate(user_name, password)
    with sqlite3.connect(data_base_name) as conn: user = conn.execute("SELECT password FROM project WHERE user_name=?", (user_name,)).fetchone()
    if user and bcrypt.checkpw(password.encode(), user[0]): return "login successful"
    return "invalid credentials"

def delete_account(user_name):
    with sqlite3.connect(data_base_name) as conn: conn.execute("DELETE FROM project WHERE user_name=?", (user_name,))

create_table()
