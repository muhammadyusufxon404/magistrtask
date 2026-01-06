import app
import sqlite3
print('DB:', app.DATABASE)
conn = sqlite3.connect(app.DATABASE)
rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', rows)
conn.close()
