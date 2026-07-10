import sqlite3
import json

db = sqlite3.connect('data/app.db')
db.row_factory = sqlite3.Row
rows = db.execute("SELECT id, owner, title, archived, pinned FROM notes WHERE title LIKE 'Workout%'").fetchall()
print(json.dumps([dict(r) for r in rows], indent=2))
db.close()
